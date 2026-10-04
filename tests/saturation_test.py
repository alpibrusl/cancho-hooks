#!/usr/bin/env python3
"""Attempts beyond the 64 connections the service has are not failures (docs/design.md section 28).

    python3 tests/saturation_test.py build/hooks

Ten endpoints may each have 8 attempts in flight, 80 in all, and the service has 64 connection slots. Found while measuring capacity
(`scripts/bench/run.py`): `start_attempts` capped the starts of a turn and the in flight of an endpoint but not the connections of
the service, so the 65th start of a burst reached `attempt.begin`, which answers "no connection", and that was recorded as a
**failed attempt**: a step of the retry schedule used (with the default schedule the next try is 5 s later, then 5 min, and after
the last one a dead letter) although no receiver was ever called.

Ten endpoints share one receiver that holds each request for 60 to 400 ms (at random, so that the number of connections in use
takes every value on the way up and not only multiples of the turn's 16 starts); 60 events make 600 deliveries. The receiver is healthy: every delivery must be attempted exactly once.

  1. stats: 600 attempts, 600 delivered, 0 failed, 0 dead
  2. the receiver saw each (endpoint, event) pair exactly once, and never more than 64 requests at the same time
  3. the outcome log has no `failed` record
"""
import base64
import http.server
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class Receiver:
    def __init__(self, hold):
        self.seen, self.now, self.peak, self.lock = [], 0, 0, threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                with outer.lock:
                    outer.now += 1
                    outer.peak = max(outer.peak, outer.now)
                time.sleep(hold * (0.15 + 0.85 * random.random()))
                with outer.lock:
                    outer.seen.append(json.loads(body)["n"])
                    outer.now -= 1
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def main():
    endpoints, events = 10, 60
    rc = Receiver(0.4)
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    d = tempfile.mkdtemp(prefix="hooks-sat-")
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(endpoints):
            f.write(f"{i} 127.0.0.1 {rc.port} {secret}\n")
    port = chaos.free_port()
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    assert proc.stderr.readline().strip() == b"listening"

    def stats():
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=5).read())

    for n in range(1, events + 1):
        chaos.post(port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)
    want = endpoints * events
    end = time.time() + 60
    while time.time() < end and stats()["delivered"] < want:
        time.sleep(0.05)
    time.sleep(0.5)
    s = stats()
    check("1. 600 attempts, 600 delivered, none failed, none dead", s["attempts"] == want and s["delivered"] == want and s["failed"] == 0 and s["dead"] == 0, str(s))
    per_event = {n: rc.seen.count(n) for n in range(1, events + 1)}
    check("2. each event reached the receiver once per endpoint (10 times), and the receiver saw more than 64 only if the service had more in flight than it has slots",
          all(c == endpoints for c in per_event.values()) and rc.peak <= 64, str((per_event, rc.peak)))
    check("2. ... and the burst really did fill the connections (the test would prove nothing otherwise)", rc.peak >= 60, str(rc.peak))
    proc.terminate()
    proc.wait()
    log = os.path.join(d, "delivery.seg")
    recs, _ = chaos.read_log(open(log, "rb").read()) if os.path.exists(log) else ([], 0)
    kinds = [struct.unpack("<5Q", pairs[0][1])[0] for _ms, pairs in recs]
    check("3. the outcome log has delivered records only (kind 1) and no failed one (kind 2)", kinds.count(2) == 0 and kinds.count(1) == want, str((kinds.count(1), kinds.count(2))))
    shutil.rmtree(d, ignore_errors=True)
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all saturation checks passed")


main()
