#!/usr/bin/env python3
"""The retry schedule, on its own (docs/design.md section 4): delays are honoured, are kept across a restart, and end in a
dead letter.

    python3 tests/retry_test.py build/hooks

  1. schedule 200,400,800,1600 ms, a receiver that fails an event's first four attempts: the arrivals are spaced at least
     each delay apart (and not much more), then one success, then silence
  2. schedule 100,100,100 ms, a receiver that always fails: four attempts, one dead letter, then silence
  3. schedule 500,1000,2000 ms, the service killed (as a power cut) 100 ms after each failed attempt and restarted at once:
     each next attempt still waits out the time the earlier run chose (not zero, not the whole delay again), and the delays
     keep stepping up, which they only do if the count of attempts survived the restart
"""
import base64
import http.server
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
from loadmeter import LoadMeter  # noqa: E402

http.server.HTTPServer.request_queue_size = 128   # the default is 5: a burst of connections would lose a SYN and wait a second


class Receiver:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.times = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.times.append(time.time())
                status = 500 if len(outer.times) <= outer.fail_first else 200
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def start(schedule, recv):
    datadir = tempfile.mkdtemp(prefix="hooks-retry-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {recv.port} {secret}\n")
    # `--retry-jitter 0`: this test measures the schedule itself, to the millisecond (jitter has its own, tests/jitter_test.py)
    svc = chaos.Service(chaos.free_port(), datadir, extra=(schedule, 2000, 86400000, "--retry-jitter", "0"))
    svc.start()
    return svc, datadir


def post_event(svc):
    chaos.post(svc.port, json.dumps({"type": "t", "n": 1}).encode(), timeout=5.0)


def stats(svc):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/stats", timeout=5) as r:
        return json.loads(r.read())


def stop(svc, datadir):
    if svc.proc and svc.proc.poll() is None:
        svc.proc.terminate()
        svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)


def durable_outcomes(datadir):
    """How many outcome records `delivery.seg` holds that a power cut would leave: the whole file, or with the fsync shim the
    prefix its side file says was synced."""
    path = os.path.join(datadir, "delivery.seg")
    if not os.path.exists(path):
        return 0
    data = open(path, "rb").read()
    side = path + ".synced"
    if os.path.exists(side):
        data = data[: struct.unpack("<q", open(side, "rb").read(8))[0]]
    # The slot records (kinds 10 and 11: an endpoint was given a slot) are written when the service first sees an endpoint, before any
    # attempt: they are not outcomes, and counting them made this return early and the test kill the service before the failure was durable.
    # Nor are 12 and 13 (a run of failures began, an endpoint was paused: written beside the first failure, so that the second attempt's wait
    # was already "durable" and a loaded machine killed the service before that attempt was recorded) or 14 (the log's header).
    return len([1 for _, pairs in chaos.read_log(data)[0] if struct.unpack("<5q", dict(pairs)[b"o"])[0] not in (10, 11, 12, 13, 14)])


def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def main():
    bad = []

    # A gap at the receiver is the delay and whatever the machine added at the two ends of it (a Python thread that stamps the request when it gets its turn, the service's loop that
    # sends it when it gets its): never less than the delay, and on a busy machine more by as much as a process can be kept waiting. The upper end of a gap is 0.4 s on a quiet one and
    # twice what the machine kept this process waiting while the gaps were being made (`LoadMeter`) besides.
    meter = LoadMeter()

    # 1. the delays
    delays = [0.2, 0.4, 0.8, 1.6]
    r = Receiver(fail_first=4)
    svc, d = start("200,400,800,1600", r)
    post_event(svc)
    wait_for(lambda: len(r.times) >= 5, 10)
    time.sleep(2.0)
    gaps = [b - a for a, b in zip(r.times, r.times[1:])]
    print(f"1. arrivals: {len(r.times)}; gaps {[round(g, 3) for g in gaps]} for delays {delays}")
    if len(r.times) != 5:
        bad.append(f"1: expected 5 requests (4 failures, 1 success, then silence), saw {len(r.times)}")
    for g, want in zip(gaps, delays):
        if not (want - 0.02 <= g <= want + 0.4 + 2 * meter.stall_s):
            bad.append(f"1: gap {g:.3f}s outside [{want - 0.02:.2f}, {want + 0.4 + 2 * meter.stall_s:.2f}] (the receiver waited {meter.stall_s:.2f}s for its turn)")
    stop(svc, d)

    # 2. dead letter
    r = Receiver(fail_first=10**9)
    svc, d = start("100,100,100", r)
    post_event(svc)
    # (it was 2.5 s of waiting for four attempts 100 ms apart to have been made: the attempts are waited for, and then a second for a fifth that must not come)
    wait_for(lambda: len(r.times) >= 4 and stats(svc)["dead"] >= 1, 15)
    time.sleep(1.0)
    s = stats(svc)
    print(f"2. requests {len(r.times)}; stats {s}")
    if len(r.times) != 4 or s["dead"] != 1 or s["delivered"] != 0 or s["attempts"] != 4:
        bad.append(f"2: wanted 4 attempts and 1 dead letter, got {len(r.times)} requests and {s}")
    stop(svc, d)

    # 3. the schedule across restarts: killed after every failed attempt, the next still waits out what the last run chose, and
    #    the count of attempts survives too (a lost count would pick an earlier, shorter delay).
    delays3 = [0.5, 1.0, 2.0]
    r = Receiver(fail_first=3)
    svc, d = start("500,1000,2000", r)
    post_event(svc)
    for k in range(3):
        wait_for(lambda: len(r.times) >= k + 1, 6)
        # Kill once the failure is *durable*, not a fixed time after the receiver saw the request: the service records an
        # outcome after the answer has come back, and a late answer (a slow receiver, a busy runner) made a fixed wait kill
        # it first. The retry then came at the restart instead of after the delay: correct for what the log held, wrong for
        # what this test means to ask.
        if not wait_for(lambda: durable_outcomes(d) >= k + 1, 6):
            bad.append(f"3: the outcome of attempt {k + 1} never became durable")
        svc.kill()
        svc.start()
    wait_for(lambda: len(r.times) >= 4, 6)
    gaps3 = [b - a for a, b in zip(r.times, r.times[1:])]
    print(f"3. killed after each failure: gaps {[round(g, 3) for g in gaps3]} for delays {delays3}")
    for g, want in zip(gaps3, delays3):
        if not (want - 0.05 <= g <= want + 0.6 + 2 * meter.stall_s):
            bad.append(f"3: gap {g:.3f}s outside [{want - 0.05:.2f}, {want + 0.6 + 2 * meter.stall_s:.2f}] (the receiver waited {meter.stall_s:.2f}s for its turn)")
    if len(gaps3) < 3:
        bad.append(f"3: only {len(r.times)} requests arrived, wanted 4")
    time.sleep(1.5)
    if len(r.times) != 4:
        bad.append(f"3: {len(r.times)} requests in all, wanted 4")
    stop(svc, d)
    meter.stop()

    if bad:
        for b in bad:
            print("FAIL:", b)
        return 1
    print("PASS: delays honoured, kept across restarts, ending in a dead letter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
