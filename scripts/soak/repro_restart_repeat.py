#!/usr/bin/env python3
"""A reproducer of what the soak found (docs/soak.md section 8): **a service that is stopped and started again delivers, a second time, events it had recorded as delivered.**

    python3 scripts/soak/repro_restart_repeat.py build/hooks [N]          # exit 0: no repeat; exit 1: events were delivered again after the restart

One endpoint with a list of event types (`types=t.a`, so that half of the events are passed over for it and leave no record), and one event it wants that its receiver refuses until it is
released, so that the endpoint's cursor stays behind it while the events after it are delivered window by window (1,024 ids at a time). When the receiver is released the cursor goes
to the end and all the events are delivered exactly once. The service is then stopped with SIGTERM (nothing is on the wire) and started again, and the receiver counts: with N = 3,000,
987 of the 1,500 wanted events are delivered a second time, about 30 s after the start, as the cursor, which the start works up again from the beginning (at about 50 ids a second), reaches
the ids that its window had not covered when the records of their delivery were read. Without a list of types, or with the whole stream inside one window, nothing repeats.

It also prints the cursor after the start, once a second: `GET /endpoints` says 41, 91, 143 ... where it said 3,000 before the stop.
"""
import collections
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
binary = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(HERE)), "build", "hooks")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
sys.argv = sys.argv[:1]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests"))
import opslib as L  # noqa: E402

d = tempfile.mkdtemp(prefix="repro-restart-")
got, first, lock, release = collections.Counter(), [None], threading.Lock(), threading.Event()
peer = L.Peer("ok")


def serve(c):
    try:
        g = peer._read_request(c)
        if g is None:
            return
        eid = int(g[0][b"webhook-id"][4:])
        with lock:
            if first[0] is None:
                first[0] = eid
        if eid == first[0] and not release.is_set():
            c.sendall(b"HTTP/1.1 500 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        else:
            with lock:
                got[eid] += 1
            c.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


peer._serve = serve
open(os.path.join(d, "endpoints.conf"), "w").write(f"0 127.0.0.1 {peer.port} {L.secret()} types=t.a\n")
args = ["--schedule", "1000,1000,1000,1000,1000,1000,1000,1000,1000"]


def cursor(s):
    return json.loads(s.get("/endpoints")[1])[0]["cursor"]


svc = L.Service(binary, d, args)
svc.start()
for n in range(N):
    svc.request("POST", "/events", json.dumps({"type": "t.a" if n % 2 == 0 else "t.b", "n": n}).encode(), {"Content-Type": "application/json"})
time.sleep(3)
print(f"{N} events posted, half of them wanted; the first wanted one refused: {len(got)} delivered behind it, cursor {cursor(svc)}")
release.set()
L.wait_for(lambda: cursor(svc) >= N, 60)
time.sleep(0.5)
print(f"released: {len(got)} of {N // 2} delivered, cursor {cursor(svc)}; stopping with SIGTERM (exit {svc.stop(10)})")
svc2 = L.Service(binary, d, args)
svc2.start()
for i in range(60):
    time.sleep(1)
    if i in (0, 1, 2, 9, 19, 29, 39, 59):
        print(f"  {i + 1:3d} s after the start: cursor {cursor(svc2)}, attempts {svc2.stats()['attempts']}")
repeats = sum(1 for v in got.values() if v > 1)
print(f"after the restart: {repeats} of {len(got)} events delivered again")
svc2.stop(10)
sys.exit(1 if repeats else 0)
