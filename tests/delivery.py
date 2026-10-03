#!/usr/bin/env python3
"""Delivery to several endpoints under chaos (docs/design.md sections 4, 6 and 15).

    python3 tests/delivery.py build/hooks [events] [threads] [mean-ms-between-kills]

Three endpoints, each its own receiver with its own secret, behave differently:

  0  mostly healthy: 20% of requests answered 500, 1% stall past the service's deadline and then answer 200
  1  flaky: each event fails its first 0 to 3 attempts, then succeeds
  2  poisoned: every event whose "n" is a multiple of 17 is always answered 500; the rest succeed

The retry schedule is scaled down to 100, 150, 200 and 250 ms (so an event is dead-lettered after its fifth attempt) and the
service is `kill -9`'d at random instants as a power cut (see chaos.py) while 300 events are posted and delivered. When the
clients are done the kills continue until MIN_KILLS (default 100), then stop, and the backlog must drain with no more help.
Then, from the receivers' record and the files:

  * every request carried a Standard Webhooks signature that the reference library (`standardwebhooks`) verifies, with a
    timestamp inside its tolerance
  * on endpoints 0 and 1, every acknowledged event was answered 2xx at least once, with the bytes the client sent
  * on endpoint 2, every non-poisoned event was, and no poisoned event ever was; each poisoned event is dead-lettered
  * `delivery.seg` is a valid log in which, for each endpoint, every event is final exactly as above (delivered or dead)
  * a poisoned event does not hold up the events after it: for most of them the next event was delivered before the poisoned
    one's last attempt (no head-of-line blocking)
  * a poisoned event was attempted at least 5 times, and at most 5 plus one per kill
  * once drained, the service sends nothing more, and after one last power cut and a restart it still sends nothing
"""
import base64
import http.server
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402  (reads the same argv: binary, events, threads, mean ms)
import struct  # noqa: E402
from standardwebhooks import Webhook  # noqa: E402

MIN_KILLS = int(os.environ.get("MIN_KILLS", "100"))
SCHEDULE = "100,150,200,250"
ATTEMPTS = 5
POISON = 17


class Receiver:
    def __init__(self, behaviour, secret):
        self.behaviour = behaviour
        self.wh = Webhook(secret)
        self.got = []            # (webhook-id, body, status answered, signature ok / reason, time)
        self.stalls = 0
        self.lock = threading.Lock()
        self.rng = random.Random(11 + behaviour)
        self.seen = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hid = self.headers.get("webhook-id", "")
                try:
                    outer.wh.verify(body, dict(self.headers))
                    sig = True
                except Exception as e:  # noqa: BLE001
                    sig = str(e) or repr(e)
                n = json.loads(body)["n"]
                stall = False
                with outer.lock:
                    outer.seen[hid] = outer.seen.get(hid, 0) + 1
                    count = outer.seen[hid]
                    if outer.behaviour == 0:
                        roll = outer.rng.random()
                        stall = roll < 0.01
                        status = 200 if stall or roll >= 0.21 else 500
                    elif outer.behaviour == 1:
                        status = 500 if count <= n % 4 else 200
                    else:
                        status = 500 if n % POISON == 0 else 200
                    if stall:
                        outer.stalls += 1
                    outer.got.append((hid, body, status, sig, time.time()))
                if stall:
                    time.sleep(2.6)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self):
        with self.lock:
            return len(self.got)


def outcomes(path):
    """delivery.seg through the independent reader: [(seq, kind, endpoint, event, attempts, next_at)]."""
    data = open(path, "rb").read() if os.path.exists(path) else b""
    records, end = chaos.read_log(data)
    out = []
    for seq, pairs in records:
        assert len(pairs) == 1 and pairs[0][0] == b"o" and len(pairs[0][1]) == 40, "not an outcome record"
        out.append((seq, *struct.unpack("<5q", pairs[0][1])))
    return out, len(data) - end


def main():
    events, threads_n, mean_ms = chaos.EVENTS, chaos.THREADS, chaos.MEAN_MS
    datadir = tempfile.mkdtemp(prefix="hooks-delivery-")
    secrets = ["whsec_" + base64.b64encode(os.urandom(24 + 8 * i)).decode() for i in range(3)]
    recvs = [Receiver(i, secrets[i]) for i in range(3)]
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write("# test endpoints\n")
        for i, r in enumerate(recvs):
            f.write(f"{i} 127.0.0.1 {r.port} {secrets[i]}\n")
    port = chaos.free_port()
    svc = chaos.Service(port, datadir, extra=(SCHEDULE,))
    svc.start()
    overtaken = "not measured"
    failures = []
    acked, acked_lock = {}, threading.Lock()
    stop = threading.Event()
    kills = [0]
    rng = random.Random(5)

    def chaos_loop():
        while not stop.is_set():
            time.sleep(rng.expovariate(1000.0 / mean_ms))
            if stop.is_set():
                break
            svc.kill()
            kills[0] += 1
            time.sleep(rng.uniform(0.0, 0.05))
            svc.start()

    def worker(first, step):
        for n in range(first, events, step):
            body = json.dumps({"type": "load.test", "n": n, "pad": "x" * (n % 50)}).encode()
            while True:
                try:
                    status, data = chaos.post(port, body, timeout=8.0)
                except (OSError, chaos.http.client.HTTPException):
                    time.sleep(0.01)
                    continue
                if status == 202:
                    eid = json.loads(data)["id"]
                    with acked_lock:
                        assert acked.get(eid, body) == body
                        acked[eid] = body
                    break
                time.sleep(0.01)

    started = time.time()
    ct = threading.Thread(target=chaos_loop, daemon=True)
    ct.start()
    ts = [threading.Thread(target=worker, args=(i, threads_n)) for i in range(threads_n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    seg = os.path.join(datadir, "delivery.seg")

    def final_sets():
        outs, _ = outcomes(seg)
        fin = [set(), set(), set()]
        for _, kind, e, ev, _, _ in outs:
            if kind in (1, 3) and 0 <= e < 3:
                fin[e].add(ev)
        return fin

    def all_ids():
        data = open(os.path.join(datadir, "events.seg"), "rb").read()
        records, _ = chaos.read_log(data)
        return {ms: json.loads(dict(pairs)[b"event"])["n"] for ms, pairs in records}

    # Kill until MIN_KILLS, then stop and require the backlog to drain unaided.
    deadline = time.time() + 240
    while time.time() < deadline and kills[0] < MIN_KILLS:
        time.sleep(0.2)
    stop.set()
    ct.join()
    if not svc.alive():
        svc.start()
    drain_started = time.time()
    drained_at = None
    while time.time() < drain_started + 120:
        ids = all_ids()
        fin = final_sets()
        if all(set(ids) <= f for f in fin):
            drained_at = time.time()
            break
        time.sleep(0.2)
    if drained_at is None:
        failures.append("the backlog did not drain in 120 s without kills")
    ids = all_ids()
    n0 = [r.count() for r in recvs]
    time.sleep(2.5)
    n1 = [r.count() for r in recvs]
    if n0 != n1 and drained_at is not None:
        failures.append(f"the service kept sending after the backlog drained: {n1[0]-n0[0]}, {n1[1]-n0[1]}, {n1[2]-n0[2]} more requests")

    # The last power cut: the outcomes are durable once a turn has ended, and a restart repeats nothing.
    before = final_sets()
    svc.kill()
    after = final_sets()
    if drained_at is not None and any(a != b for a, b in zip(before, after)):
        failures.append("a power cut took outcomes back: the outcome log was not flushed at the end of a turn")
    svc.start()
    m0 = [r.count() for r in recvs]
    time.sleep(2.5)
    if [r.count() for r in recvs] != m0 and before == after:
        failures.append("after the cut and a restart the service sent requests again")
    if svc.proc and svc.proc.poll() is None:
        svc.proc.terminate()
        svc.proc.wait()
    elapsed = time.time() - started

    # --- the checks on what the receivers saw ---------------------------------------------------------------
    n_of = {i: ids[i] for i in ids}
    for e, r in enumerate(recvs):
        got = list(r.got)
        bad_sig = [(h, s) for h, _, _, s, _ in got if s is not True]
        if bad_sig:
            failures.append(f"endpoint {e}: {len(bad_sig)} requests failed signature verification, e.g. {bad_sig[0]}")
        ok_by_id = {}
        for hid, body, status, _, t in got:
            if status == 200:
                ok_by_id.setdefault(int(hid.split("_")[1]), []).append(body)
        poisoned = {i for i, n in n_of.items() if e == 2 and n % POISON == 0}
        want = [i for i in acked if i not in poisoned]
        missing = [i for i in want if i not in ok_by_id]
        if missing:
            failures.append(f"endpoint {e}: {len(missing)} events never answered 2xx, e.g. {sorted(missing)[:5]}")
        wrong = [i for i in want if i in ok_by_id and any(b != acked[i] for b in ok_by_id[i])]
        if wrong:
            failures.append(f"endpoint {e}: {len(wrong)} events arrived with different bytes")
        if e == 2:
            leaked = [i for i in poisoned if i in ok_by_id]
            if leaked:
                failures.append(f"endpoint 2: poisoned events answered 2xx: {sorted(leaked)[:5]}")
            tries = {i: sum(1 for h, *_ in got if h == f"evt_{i}") for i in poisoned}
            few = [i for i, t in tries.items() if t < ATTEMPTS]
            many = [i for i, t in tries.items() if t > ATTEMPTS + kills[0]]
            if few:
                failures.append(f"endpoint 2: poisoned events attempted fewer than {ATTEMPTS} times: {sorted(few)[:5]}")
            if many:
                failures.append(f"endpoint 2: poisoned events attempted more than {ATTEMPTS} + {kills[0]} times: {sorted(many)[:5]}")
            # No head-of-line blocking: the event after a poisoned one was delivered before the poisoned one gave up.
            last_try = {i: max(t for h, _, _, _, t in got if h == f"evt_{i}") for i in poisoned}
            first_ok = {}
            for hid, _, status, _, t in got:
                if status == 200:
                    i = int(hid.split("_")[1])
                    first_ok[i] = min(first_ok.get(i, t), t)
            checked = passed = 0
            for i in poisoned:
                if i + 1 in first_ok and i in last_try:
                    checked += 1
                    passed += first_ok[i + 1] < last_try[i]
            head = f"{passed} of {checked} poisoned events were overtaken by the next one"
            overtaken = head
            if checked and passed * 10 < checked * 8:
                failures.append(f"endpoint 2: head-of-line blocking: only {head}")
    outs, torn = outcomes(seg)
    print(f"head-of-line: {overtaken}")
    fin = final_sets()
    for e in range(3):
        if drained_at is not None and fin[e] != set(ids):
            failures.append(f"delivery.seg: endpoint {e} has {len(fin[e])} final events of {len(ids)}")
    dead = {e: {ev for _, k, ee, ev, _, _ in outs if k == 3 and ee == e} for e in range(3)}
    want_dead = {i for i, n in n_of.items() if n % POISON == 0}
    if drained_at is not None and (dead[2] != want_dead or dead[0] or dead[1]):
        failures.append(f"dead letters: endpoint 2 has {len(dead[2])} of {len(want_dead)} poisoned events, others {len(dead[0])}/{len(dead[1])}")
    if [s for s in (outs[i][0] for i in range(len(outs)))] != sorted({s for s, *_ in outs}):
        failures.append("delivery.seg sequence numbers are not strictly increasing")

    reqs = [r.count() for r in recvs]
    print(f"{events} events, {threads_n} threads, {kills[0]} kills as {'power cuts' if chaos.POWER_LOSS else 'process kills'}, {elapsed:.1f}s; "
          f"{len(ids)} events in the log, {len(acked)} acknowledged")
    print(f"requests per endpoint: {reqs} ({sum(r.stalls for r in recvs)} stalls); delivery.seg: {len(outs)} outcome records "
          f"({sum(1 for o in outs if o[1]==1)} delivered, {sum(1 for o in outs if o[1]==2)} failed attempts, "
          f"{sum(1 for o in outs if o[1]==3)} dead letters), {torn} bytes of torn tail")
    shutil.rmtree(datadir, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: signed deliveries to three endpoints, retried and dead-lettered as designed, nothing lost, nothing repeated after the cut")
    return 0


if __name__ == "__main__":
    sys.exit(main())
