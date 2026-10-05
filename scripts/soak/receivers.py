#!/usr/bin/env python3
"""The receivers of the soak (docs/soak.md): every endpoint's HTTP or TLS listener in one asyncio process, which is also the load generator's far end and the
**receivers' ledger**: for every request that reaches a receiver one fixed-size record is written to `<dir>/ledger/recv.bin` *before* the receiver answers, so that
whatever the service could have counted as delivered is already there when it counts it.

    receivers.py --dir DIR --seed S [--cpus 0,1,2]

It reads `DIR/spec.json` (the endpoints, their ports, secrets and classes), listens, writes its control port to `DIR/ctl.port`, and takes commands as one line of
JSON each over that port: add, remove, secrets, mode, sick, fault, stats. Not a general HTTP server: it understands the one request the service makes, verifies its
Standard Webhooks signature itself (HMAC-SHA256 over `id.timestamp.body` under the endpoint's secrets, with the roles and validity the harness told it), and says what it
saw. It does not decide whether what it saw is right: the checker (ledger.py) does, from the records and from the poster's ledger.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import resource
import signal
import socket
import ssl
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CLASSES, F_BODY, F_EFF, F_NEW, F_OLD, F_RISK, F_SIG, F_TS, F_TWO, FLAP_CYCLE, REC, TYPE_CODE, UNKNOWN_TYPE, crc_pad, fail_count,  # noqa: E402
                    flap_window, poison, secret_bytes)

EFF_MAX_DELAY = 1.6      # an answer later than this is past the service's deadline (2 s from the start of the attempt): not an acknowledgement
# the end-to-end latency of the first delivery is read only at endpoints that do not fail on purpose (a retry's is the schedule's delay, a replay's is its age)
LAT_CLASSES = ("oracle", "healthy", "healthy2", "filter", "https", "rate", "slow")
TS_TOLERANCE = 300       # Standard Webhooks: five minutes


class Ep:
    def __init__(self, spec, seed, t0):
        self.idx, self.label, self.cls, self.port = spec["idx"], spec["label"], spec["cls"], spec["port"]
        self.tls = bool(spec.get("tls"))
        self.params = dict(CLASSES.get(self.cls, ([], {}))[1])
        self.params.update(spec.get("params", {}))
        self.seed, self.t0 = seed, t0
        self.set_secrets(spec.get("secrets", []))
        self.sock = None
        self.server = None
        self.removed = False
        self.vanish_until = 0.0
        self.hold_until = 0.0
        self.slow_until, self.slow_delay = 0.0, 0.0
        self.sick_until = 0.0
        self.down_mode = None            # a flapping endpoint's current down kind: reset or close (refuse closes the listener)
        self.attempts = {}               # event -> (attempts so far, last time): for the class that fails the first few
        self.n_req = self.n_eff = self.n_bad = 0

    def set_secrets(self, items):
        # oldest first; the last is the new one
        self.secrets = [(secret_bytes(it["s"]), it.get("from"), it.get("to")) for it in items]

    def flap_mode(self, now):
        cycle = int((now - self.t0) // FLAP_CYCLE)
        start, length, mode = flap_window(self.seed, self.label, cycle)
        off = (now - self.t0) - cycle * FLAP_CYCLE
        return mode if start <= off < start + length else None

    def want_listen(self, now):
        if self.removed:
            return False
        if self.vanish_until > now:
            return False
        if self.cls == "flapping" and self.flap_mode(now) == "refuse":
            return False
        return True


class Receivers:
    def __init__(self, a):
        self.dir = a.dir
        self.seed = a.seed
        self.loop = None
        with open(os.path.join(a.dir, "spec.json")) as f:
            self.spec = json.load(f)
        self.t0 = self.spec.get("t0", time.time())
        self.eps = {}
        self.fd = os.open(os.path.join(a.dir, "ledger", "recv.bin"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.lat = []
        self.lag_max = 0.0
        self.records = 0
        self.faults = {}
        self.rng = random.Random(a.seed ^ 0x5eed)
        self.ssl = None
        cert = self.spec.get("tls")
        if cert:
            self.ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl.load_cert_chain(cert["cert"], cert["key"])
        self.accepted = 0

    # ---- the ledger
    def write(self, ep, now, ev, n, src, typ, flags, status, e2e):
        rec = REC.pack(now, ep.idx, ev, n, src, typ, flags, status, e2e)
        f = self.faults
        if f.get("lose") and self.rng.random() < f["lose"]:
            return      # a mutant: the delivery happened and the ledger does not know
        os.write(self.fd, rec)
        self.records += 1
        if f.get("dup") and self.rng.random() < f["dup"]:
            os.write(self.fd, rec)
            self.records += 1

    # ---- listeners
    def open_ep(self, ep):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setblocking(False)
        s.bind(("127.0.0.1", ep.port))
        s.listen(1024)
        ep.sock = s
        kw = {"ssl": self.ssl, "ssl_handshake_timeout": 5} if ep.tls else {}
        ep.server = asyncio.ensure_future(self.loop.create_server(lambda: Conn(self, ep), sock=s, **kw))
        ep.server.add_done_callback(lambda fut: fut.exception() and print(f"receivers: {ep.label}: cannot serve: {fut.exception()!r}", file=sys.stderr, flush=True))
        return ep.server

    def close_ep(self, ep):
        srv, ep.server = ep.server, None
        if srv is not None:
            def done(fut):
                try:
                    fut.result().close()
                except Exception:  # noqa: BLE001
                    pass
            srv.add_done_callback(done) if not srv.done() else done(srv)
        if ep.sock is not None:
            try:
                ep.sock.close()
            except OSError:
                pass
            ep.sock = None

    def add(self, spec):
        ep = Ep(spec, self.seed, self.t0)
        old = self.eps.get(ep.idx)
        if old:
            self.close_ep(old)
        self.eps[ep.idx] = ep
        return ep

    def by_label(self, label):
        for ep in self.eps.values():
            if ep.label == label:
                return ep
        return None

    async def ticker(self):
        """Opens and closes listeners as the class and the harness say, and measures how late the loop itself runs."""
        period = 0.05
        last = time.monotonic()
        while True:
            await asyncio.sleep(period)
            m = time.monotonic()
            self.lag_max = max(self.lag_max, m - last - period)
            last = m
            now = time.time()
            for ep in self.eps.values():
                want = ep.want_listen(now)
                if want and ep.sock is None:
                    try:
                        self.open_ep(ep)
                    except OSError as e:
                        print(f"receivers: cannot listen on {ep.port}: {e}", file=sys.stderr, flush=True)
                elif not want and ep.sock is not None:
                    self.close_ep(ep)
                ep.down_mode = None
                if ep.cls == "flapping":
                    fm = ep.flap_mode(now)
                    ep.down_mode = fm if fm in ("reset", "close") else None
            if int(now) % 30 == 0:
                for ep in self.eps.values():
                    if len(ep.attempts) > 2000:
                        ep.attempts = {k: v for k, v in ep.attempts.items() if now - v[1] < 120}

    # ---- control
    async def control(self, reader, writer):
        try:
            line = await reader.readline()
            cmd = json.loads(line)
            out = self.command(cmd)
        except Exception as e:  # noqa: BLE001
            out = {"error": repr(e)}
        writer.write((json.dumps(out) + "\n").encode())
        await writer.drain()
        writer.close()

    def command(self, c):
        op = c["op"]
        if op == "ping":
            return {"ok": True}
        if op == "add":
            ep = self.add(c["spec"])
            return {"ok": True, "idx": ep.idx}
        if op == "remove":
            ep = self.by_label(c["label"])
            if ep:
                ep.removed = True
            return {"ok": ep is not None}
        if op == "secrets":
            ep = self.by_label(c["label"])
            ep.set_secrets(c["secrets"])
            return {"ok": True}
        if op == "mode":
            ep = self.by_label(c["label"])
            until = c["until"]
            if c["mode"] == "vanish":
                ep.vanish_until = until
            elif c["mode"] == "slow":
                ep.slow_until, ep.slow_delay = until, c["delay"]
            elif c["mode"] == "hold":
                ep.hold_until = until
            elif c["mode"] == "normal":
                ep.vanish_until = ep.slow_until = ep.hold_until = 0.0
            return {"ok": True}
        if op == "sick":
            ep = self.by_label(c["label"])
            ep.sick_until = c["until"]
            return {"ok": True}
        if op == "params":
            ep = self.by_label(c["label"])
            ep.params.update(c["params"])
            return {"ok": True}
        if op == "fault":
            self.faults = c.get("faults", {})
            return {"ok": True, "faults": self.faults}
        if op == "stats":
            lat, self.lat = self.lat, []
            lag, self.lag_max = self.lag_max, 0.0
            lat.sort()

            def pct(p):
                return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0
            ru = resource.getrusage(resource.RUSAGE_SELF)
            return {"records": self.records, "accepted": self.accepted, "lat_n": len(lat), "lat_p50": pct(0.5), "lat_p99": pct(0.99), "lat_max": lat[-1] if lat else 0,
                    "loop_lag_max_ms": lag * 1000, "cpu_s": ru.ru_utime + ru.ru_stime, "rss_kb": ru.ru_maxrss,
                    "eps": {ep.label: [ep.n_req, ep.n_eff, ep.n_bad] for ep in self.eps.values()}}
        return {"error": "unknown op"}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        for s in self.spec["endpoints"]:
            ep = self.add(s)
            if ep.want_listen(time.time()):
                self.open_ep(ep)
        srv = await asyncio.start_server(self.control, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        tmp = os.path.join(self.dir, "ctl.port.tmp")
        with open(tmp, "w") as f:
            f.write(str(port))
        os.replace(tmp, os.path.join(self.dir, "ctl.port"))
        asyncio.ensure_future(self.ticker())
        await asyncio.Event().wait()


class Conn(asyncio.Protocol):
    __slots__ = ("r", "ep", "tr", "buf", "done")

    def __init__(self, r, ep):
        self.r, self.ep, self.tr, self.buf, self.done = r, ep, None, bytearray(), False

    def connection_made(self, tr):
        self.tr = tr
        self.r.accepted += 1
        ep = self.ep
        dm = ep.down_mode
        if dm == "close":
            self.done = True
            tr.close()
            return
        if dm == "reset":
            self.done = True
            sock = tr.get_extra_info("socket")
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            tr.abort()
            return
        now = time.time()
        if ep.hold_until > now:
            self.done = True
            self.r.loop.call_later(ep.hold_until - now, tr.close)

    def data_received(self, data):
        if self.done:
            return
        self.buf += data
        i = self.buf.find(b"\r\n\r\n")
        if i < 0:
            if len(self.buf) > 70000:
                self.tr.close()
            return
        head = bytes(self.buf[:i]).split(b"\r\n")
        headers = {}
        for line in head[1:]:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        try:
            need = int(headers.get(b"content-length", b"0"))
        except ValueError:
            need = 0
        if len(self.buf) < i + 4 + need:
            return
        self.done = True
        body = bytes(self.buf[i + 4:i + 4 + need])
        try:
            self.handle(head[0], headers, body)
        except Exception as e:  # noqa: BLE001
            print(f"receivers: {self.ep.label}: {e!r}", file=sys.stderr, flush=True)
            self.tr.close()

    def handle(self, reqline, headers, body):
        r, ep = self.r, self.ep
        now = time.time()
        ep.n_req += 1
        wid = headers.get(b"webhook-id", b"")
        ev = int(wid[4:]) if wid.startswith(b"evt_") and wid[4:].isdigit() else 0
        ts = headers.get(b"webhook-timestamp", b"")
        sig = headers.get(b"webhook-signature", b"")
        flags = 0
        try:
            if abs(int(ts) - now) <= TS_TOLERANCE:
                flags |= F_TS
        except ValueError:
            pass
        tokens = [t[3:] for t in sig.split() if t.startswith(b"v1,")]
        if len(tokens) >= 2:
            flags |= F_TWO
        msg = wid + b"." + ts + b"." + body
        last = len(ep.secrets) - 1
        for k, (key, frm, to) in enumerate(ep.secrets):
            want = base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest())
            if want in tokens:
                valid = (frm is None or now >= frm) and (to is None or now <= to)
                if valid:
                    flags |= F_SIG
                    flags |= F_NEW if k == last else F_OLD
        typ_code, n, src, t_send, body_ok = UNKNOWN_TYPE, 0, 0, None, False
        try:
            d = json.loads(body)
            typ = d.get("type")
            if isinstance(typ, str):
                typ_code = TYPE_CODE.get(typ, 254)
            n = int(d.get("n", d.get("scheduled_at", 0)))
            src = int(d.get("schedule", 0))
            t_send = d.get("t")
            body_ok = isinstance(typ, str) and (("c" not in d) or (d["c"] == crc_pad(d.get("pad", ""))))
        except (ValueError, TypeError, AttributeError):
            pass
        if body_ok:
            flags |= F_BODY
        e2e = 0
        if t_send:
            e2e = max(0, min(4294967295, int((now - t_send) * 1000)))
            if ep.cls in LAT_CLASSES:
                r.lat.append(e2e)
        status, delay = self.decide(ep, now, ev)
        eff = 200 <= status < 300 and delay <= EFF_MAX_DELAY
        if eff:
            flags |= F_EFF
            ep.n_eff += 1
            if delay > 0.5:
                flags |= F_RISK
        if not (flags & F_SIG):
            ep.n_bad += 1
        r.write(ep, now, ev, n, src, typ_code, flags, status, e2e)
        if delay > 0:
            r.loop.call_later(delay, self.answer, status)
        else:
            self.answer(status)

    def decide(self, ep, now, ev):
        status, delay = 204, 0.0
        cls = ep.cls
        if cls == "slow":
            lo, hi = ep.params["delay"]
            delay = lo + (hi - lo) * self.r.rng.random()
        elif cls == "http5xx":
            seen = ep.attempts.get(ev, (0, 0))[0]
            ep.attempts[ev] = (seen + 1, now)
            if seen < fail_count(ep.seed, ep.label, ev):
                status = (500, 502, 503)[self.r.rng.randrange(3)]
        elif cls == "gone":
            if poison(ep.seed, ep.label, ev, ep.params.get("poison", 0.01)):
                status = 410
        elif cls == "sick":
            if ep.sick_until > now:
                status = 503
        if ep.slow_until > now:
            delay = max(delay, ep.slow_delay)
        return status, delay

    def answer(self, status):
        tr = self.tr
        if tr is None or tr.is_closing():
            return
        tr.write(f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        tr.close()

    def connection_lost(self, exc):
        self.tr = None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--cpus", default="")
    a = p.parse_args()
    if a.cpus:
        try:
            os.sched_setaffinity(0, {int(c) for c in a.cpus.split(",")})
        except (OSError, AttributeError):
            pass
    os.makedirs(os.path.join(a.dir, "ledger"), exist_ok=True)
    r = Receivers(a)
    # the name server of the https endpoints (tlskit's, which speaks DNS over TCP, the way the service asks)
    if r.spec.get("dns"):
        import tlskit
        tlskit.DnsStub({r.spec["dns"]["name"]: ["127.0.0.1"]}, port=r.spec["dns"]["port"])
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        asyncio.run(r.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
