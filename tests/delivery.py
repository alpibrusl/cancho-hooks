#!/usr/bin/env python3
"""Delivery under chaos (docs/design.md section 6, the first criterion carried through to the receiver).

    python3 tests/delivery.py build/hooks [events] [threads] [mean-ms-between-kills]

A receiver this script controls answers `POST /hook`: mostly 200, sometimes 500, rarely it stalls past the service's
attempt deadline and then answers 200 (so the receiver *has* the event and the service believes it failed: a repeat is
the only correct outcome). The service is `kill -9`'d at random instants as a power cut (see chaos.py). When the clients
have finished posting, the harness waits for the backlog to drain, then checks, from the receiver's record and from the
files:

  * every acknowledged event reached the receiver at least once, with the body the client sent
  * the first delivery of each event comes in id order (delivery is in order while the receiver is up)
  * `delivered.seg` is a valid log whose last id is the last event: the cursor survived every cut
  * once drained, the service sends nothing more
  * repeats are counted and reported, not forbidden: at least once is the contract
"""
import http.server
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402  (reads the same argv: binary, events, threads, mean ms)

P_FAIL, P_STALL = 0.2, 0.01
MIN_KILLS = int(os.environ.get("MIN_KILLS", "100"))


class Receiver:
    def __init__(self):
        self.got = []            # (webhook-id, body, the status it answered)
        self.stalls = 0
        self.lock = threading.Lock()
        self.rng = random.Random(11)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hid = self.headers.get("webhook-id", "")
                with outer.lock:
                    roll = outer.rng.random()
                    if roll < P_STALL:
                        status = 200
                        outer.stalls += 1
                    elif roll < P_STALL + P_FAIL:
                        status = 500
                    else:
                        status = 200
                    outer.got.append((hid, body, status))
                if roll < P_STALL:
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


def main():
    events, threads_n, mean_ms = chaos.EVENTS, chaos.THREADS, chaos.MEAN_MS
    datadir = tempfile.mkdtemp(prefix="hooks-delivery-")
    recv = Receiver()
    port = chaos.free_port()
    svc = chaos.Service(port, datadir, extra=("127.0.0.1", recv.port))
    svc.start()
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
    # The kills go on while the backlog drains: delivery is what is under test here.
    last_id = max(acked) if acked else 0
    seg = os.path.join(datadir, "delivered.seg")

    def cursor():
        data = open(seg, "rb").read() if os.path.exists(seg) else b""
        records, _ = chaos.read_log(data)
        return records[-1][0] if records else 0

    # Kill until MIN_KILLS have happened (and the posting is done), then stop and require the backlog to drain with no
    # more help: a service that only makes progress because it is restarted is not delivering.
    deadline = time.time() + 240
    while time.time() < deadline and kills[0] < MIN_KILLS:
        time.sleep(0.2)
    stop.set()
    ct.join()
    if not svc.alive():
        svc.start()
    drain_started = time.time()
    drained_at = None
    while time.time() < drain_started + 90:
        if cursor() >= last_id:
            drained_at = time.time()
            break
        time.sleep(0.2)
    if drained_at is None:
        failures.append(f"the backlog did not drain in 90 s without kills: the cursor is at {cursor()} of {last_id}")
    # Once drained, it must go quiet: count requests over a quiet period.
    n0 = recv.count()
    time.sleep(2.5)
    n1 = recv.count()
    # The cursor is durable once a turn has ended: cut the power now and it must still be at the last event, and a
    # restart must not deliver anything again. (A cursor that is written but not flushed passes every other check here,
    # since a repeat is allowed; this is the one that sees it.)
    cursor_before = cursor()
    svc.kill()
    after_cut = cursor()
    if drained_at is not None and after_cut < cursor_before:
        failures.append(f"a power cut took the cursor from {cursor_before} back to {after_cut}: it was not flushed")
    svc.start()
    m0 = recv.count()
    time.sleep(2.5)
    if recv.count() != m0 and after_cut >= cursor_before:
        failures.append(f"after the cut and a restart the service delivered {recv.count() - m0} events again")
    if svc.proc and svc.proc.poll() is None:
        svc.proc.terminate()
        svc.proc.wait()
    elapsed = time.time() - started

    got = list(recv.got)
    first_seen, per_id = {}, {}
    answered_ok = {}
    for hid, body, status in got:
        i = int(hid.split("_")[1]) if hid.startswith("evt_") else -1
        per_id.setdefault(i, []).append(body)
        if status == 200:
            answered_ok[i] = answered_ok.get(i, 0) + 1
        first_seen.setdefault(i, len(first_seen))
    # Delivered means the receiver answered it 2xx: a request that got a 500 is an attempt, not a delivery.
    undelivered = [i for i in acked if i not in answered_ok]
    if undelivered:
        failures.append(f"{len(undelivered)} acknowledged events were never delivered (answered 2xx), e.g. {sorted(undelivered)[:5]}")
    wrong = [i for i, b in acked.items() if i in per_id and any(x != b for x in per_id[i])]
    if wrong:
        failures.append(f"{len(wrong)} events reached the receiver with a different body, e.g. {sorted(wrong)[:5]}")
    order = sorted(first_seen, key=first_seen.get)
    # Among events delivered, first deliveries must be ascending except for ones a failing stall let through out of
    # step: none can be, since the service delivers one at a time, in order.
    if order != sorted(order):
        bad = next(k for k in range(len(order) - 1) if order[k] > order[k + 1])
        failures.append(f"first deliveries are not in id order: {order[bad]} came before {order[bad + 1]}")
    if n1 != n0:
        failures.append(f"the service sent {n1 - n0} more requests after the backlog drained")
    seg = os.path.join(datadir, "delivered.seg")
    data = open(seg, "rb").read() if os.path.exists(seg) else b""
    records, end = chaos.read_log(data)
    ids = [ms for ms, _ in records]
    if ids != sorted(set(ids)):
        failures.append("delivered.seg ids are not strictly increasing")
    if drained_at is not None and (not ids or ids[-1] < last_id):
        failures.append(f"delivered.seg ends at {ids[-1] if ids else 0}, the last acknowledged event is {last_id}")
    repeats = sum(len(v) - 1 for v in per_id.values())
    # A repeat after a 2xx is a crash (the cursor was behind the receiver by at most one turn of deliveries) or a stall
    # that answered after the deadline. Anything beyond that bound means the cursor is not doing its job.
    again = sum(n - 1 for n in answered_ok.values())
    bound = kills[0] * 17 + recv.stalls + 2
    if again > bound:
        failures.append(f"{again} events were delivered again after the receiver had answered 2xx; "
                        f"{kills[0]} kills and {recv.stalls} stalls account for at most {bound}")
    print(f"{events} events, {threads_n} threads, {kills[0]} kills as "
          f"{'power cuts' if chaos.POWER_LOSS else 'process kills'}, {elapsed:.1f}s; receiver fails {P_FAIL:.0%} and stalls {P_STALL:.0%}")
    print(f"acknowledged {len(acked)}; receiver saw {len(got)} requests for {len(per_id)} events ({repeats} repeats, {again} of them after a 2xx);"
          f" delivered.seg holds {len(records)} records ({len(data) - end} bytes of torn tail)")
    shutil.rmtree(datadir, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: every acknowledged event was delivered, intact and in order, the cursor survived, and it went quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
