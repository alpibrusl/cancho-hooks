"""The poster of the soak (docs/soak.md section 1): a steady stream of events at a rate the harness changes, posted by a few threads that pace themselves from one shared
schedule (open loop: a thread that is held up does not make the others hurry, and a schedule that has fallen more than a second behind is reset, as a client in an outage
would), every event numbered (`n`), nine in ten under an Idempotency-Key, some of them posted again at once (same key: the same id) and some posted again with a
different body (a 422). **Every event that was acknowledged, and every one that was not and may have been stored, is written to the poster's ledger** (`acked.bin`).
"""
import http.client
import json
import os
import random
import threading
import time
from collections import Counter, deque

from common import ACK, A_BIG, A_INDOUBT, A_KEYED, TYPE_CODE, TYPES, crc_pad

_PAD = None


def pad_source(seed):
    global _PAD
    if _PAD is None:
        r = random.Random(seed)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:-_"
        _PAD = "".join(r.choice(alphabet) for _ in range(4096))
    return _PAD * 14


class Poster:
    def __init__(self, port, token, seed, ledger_path, violations, rate, workers=6, start_n=1):
        self.port, self.token, self.seed, self.v = port, token, seed, violations
        self.rate = rate
        self.workers = workers
        self.fd = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.pl = threading.Lock()
        self.n_next = start_n
        self.t_next = time.time()
        self.stop_flag = threading.Event()
        self.threads = []
        self.counts = Counter()
        self.lat = []
        self.bytes_ingested = 0
        self.max_id = 0
        self.recent = {}                  # id -> (n, t_ack) of the last few minutes
        self.recent_q = deque()
        self.window_t = time.time()
        self.window_acks = 0
        self.pad = pad_source(seed)
        self.paused = threading.Event()   # set: do not post (the harness is in a phase that wants quiet)
        self.types, self.weights = [t for t, _ in TYPES], [w for _, w in TYPES]

    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self.work, args=(i,), daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self, timeout=90):
        self.stop_flag.set()
        end = time.time() + timeout
        for t in self.threads:
            t.join(max(0.1, end - time.time()))

    # ---- one event
    def make(self, n, t_send):
        rng = random.Random(self.seed * 1_000_003 + n)
        typ = rng.choices(self.types, self.weights)[0]
        r = rng.random()
        if r < 0.80:
            size = rng.randint(20, 280)
        elif r < 0.95:
            size = rng.randint(1000, 4000)
        elif r < 0.99:
            size = rng.randint(8000, 20000)
        else:
            size = rng.randint(40000, 50000)
        off = rng.randint(0, 3000)
        pad = self.pad[off:off + size]
        keyed = rng.random() < 0.9
        body = json.dumps({"type": typ, "n": n, "t": round(t_send, 6), "pad": pad, "c": crc_pad(pad)}).encode()
        extra = rng.random()
        return typ, body, (f"s{self.seed}-{n}" if keyed else None), size >= 40000, extra

    def claim(self):
        with self.pl:
            now = time.time()
            rate = max(self.rate, 0.1)
            if self.t_next < now - 1.0:
                self.counts["slipped"] += int((now - self.t_next) * rate)
                self.t_next = now
            t_s = self.t_next
            self.t_next += 1.0 / rate
            n = self.n_next
            self.n_next += 1
        return n, t_s

    def request(self, st, body, key):
        h = {"Content-Type": "application/json", "Authorization": "Bearer " + self.token}
        if key:
            h["Idempotency-Key"] = key
        try:
            if st["conn"] is None:
                st["conn"] = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            st["conn"].request("POST", "/events", body=body, headers=h)
            r = st["conn"].getresponse()
            data = r.read()
            if r.getheader("Connection", "").lower() == "close":
                st["conn"].close()
                st["conn"] = None
            return r.status, data
        except (OSError, http.client.HTTPException):
            try:
                st["conn"].close()
            except Exception:  # noqa: BLE001
                pass
            st["conn"] = None
            return 0, b""

    def record(self, t_ack, t_send, n, ident, typ, flags):
        os.write(self.fd, ACK.pack(t_ack, t_send, n, ident, TYPE_CODE[typ], flags, 0))

    def work(self, _i):
        st = {"conn": None}
        while not self.stop_flag.is_set():
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            n, t_s = self.claim()
            d = t_s - time.time()
            if d > 0:
                time.sleep(d)
            t_send = time.time()
            typ, body, key, big, extra = self.make(n, t_send)
            flags = (A_KEYED if key else 0) | (A_BIG if big else 0)
            t_begin = time.time()
            attempt = 0
            done = False
            while not done:
                status, data = self.request(st, body, key)
                if status == 202:
                    try:
                        ident = int(json.loads(data)["id"])
                    except (ValueError, KeyError, TypeError):
                        self.v.add("P_bad_ack", n=n, body=data[:100].decode(errors="replace"))
                        ident = 0
                    t_ack = time.time()
                    self.record(t_ack, t_send, n, ident, typ, flags | (A_INDOUBT if not ident else 0))
                    with self.pl:
                        self.counts["acked"] += 1
                        self.lat.append((t_ack - t_begin) * 1000.0 if attempt == 0 else 0.0)
                        self.bytes_ingested += len(body) + len(key or "") + 80
                        self.max_id = max(self.max_id, ident)
                        self.window_acks += 1
                        self.recent[ident] = (n, t_ack)
                        self.recent_q.append((t_ack, ident))
                        while self.recent_q and self.recent_q[0][0] < t_ack - 900:
                            self.recent.pop(self.recent_q.popleft()[1], None)
                    if key and ident:
                        self.followups(st, n, key, body, ident, extra)
                    done = True
                elif status in (0, 503, 504, 507, 500, 502):
                    self.counts["retried"] += 1
                    if not key or time.time() - t_begin > 120 or (self.stop_flag.is_set() and time.time() - t_begin > 60):
                        # not stored, or stored and not known: either way it may turn up delivered, and the poster says so
                        self.record(time.time(), t_send, n, 0, typ, flags | A_INDOUBT)
                        self.counts["indoubt"] += 1
                        done = True
                    else:
                        time.sleep(min(0.05 * (2 ** min(attempt, 5)), 1.0))
                else:
                    self.v.add("P_unexpected_status", n=n, status=status, body=data[:200].decode(errors="replace"))
                    self.record(time.time(), t_send, n, 0, typ, flags | A_INDOUBT)
                    done = True
                attempt += 1

    def followups(self, st, n, key, body, ident, extra):
        """A repost under the same key must give the same id; the same key with another body must be refused with a 422."""
        if extra < 0.03:
            s, d = self.request(st, body, key)
            self.counts["reposts"] += 1
            if s == 202:
                try:
                    again = int(json.loads(d)["id"])
                except (ValueError, KeyError, TypeError):
                    again = -1
                if again != ident:
                    self.v.add("P_idem_repost", n=n, key=key, id=ident, again=again)
            elif s not in (0, 503, 504):
                self.v.add("P_idem_repost", n=n, key=key, id=ident, status=s)
        elif extra < 0.04:
            other = json.dumps({"type": "ping", "n": n, "t": 0, "pad": "different"}).encode()
            s, d = self.request(st, other, key)
            self.counts["conflicts"] += 1
            if s == 202:
                self.v.add("P_idem_conflict", n=n, key=key, id=ident, answer=d[:100].decode(errors="replace"))
            elif s not in (0, 422, 503, 504):
                self.v.add("P_idem_conflict", n=n, key=key, status=s)

    # ---- numbers for the watcher
    def window(self):
        """What happened since the last call: acknowledged events, a second, and the latency percentiles in ms."""
        with self.pl:
            lat, self.lat = [x for x in self.lat if x > 0], []
            acks, self.window_acks = self.window_acks, 0
            now = time.time()
            dt = max(1e-3, now - self.window_t)
            self.window_t = now
        lat.sort()

        def pct(p):
            return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0
        return {"per_s": acks / dt, "p50": pct(0.5), "p99": pct(0.99), "max": lat[-1] if lat else 0.0, "n": len(lat)}
