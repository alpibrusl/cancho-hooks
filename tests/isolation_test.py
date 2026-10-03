#!/usr/bin/env python3
"""A stalled endpoint, and what it costs the others (docs/design.md section 6, criterion 3; section 15 for the bounds).

    python3 tests/isolation_test.py build/hooks

Endpoint 0 accepts connections and never answers. Endpoints 1 and 2 answer at once. 80 events are posted at about 20 a second.
The thresholds were fixed before the first run:

  * ingest: the median POST under 50 ms, and at most 15% of POSTs over 500 ms
  * with a *slow* endpoint (answers after 300 ms) instead, **reported and not gated**: the median POST was 255 ms when this
    was written, because each POST is followed by a 300 ms attempt on the same thread. A 50 ms median would fail, and
    does; that is the finding, not something to loosen (docs/design.md section 15)
  * a burst of 40 events queued behind that slow endpoint, then 25 probe POSTs 100 ms apart: the 99th percentile of the
    probes under 1.5 s. This is what a turn's time budget is for: without it one turn makes up to 16 attempts of 300 ms
  * every event reaches both healthy endpoints, and their delivery latency (from the POST's answer to the receiver's request)
    has a 99th percentile under 5 s: the 2 s deadline of one stalled attempt, one turn's 250 ms, and room

The same run without the stalled endpoint is the baseline, and both are printed. This is not criterion 3 met: delivery runs on
the thread that serves ingest, so a stalled endpoint still costs everyone one deadline at a time. The numbers say how much.
"""
import base64
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402


class Healthy:
    def __init__(self, delay=0.0):
        self.seen = {}      # webhook-id -> first arrival time
        self.delay = delay
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.setdefault(self.headers["webhook-id"], time.time())
                time.sleep(outer.delay)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def run(kind, events=80):
    healthy = [Healthy(), Healthy()]
    slow = Healthy(0.3)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(64)
    held = []
    threading.Thread(target=lambda: [held.append(silent.accept()[0]) for _ in iter(int, 1)], daemon=True).start()
    datadir = tempfile.mkdtemp(prefix="hooks-isolation-")
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        secret = lambda: "whsec_" + base64.b64encode(os.urandom(24)).decode()  # noqa: E731
        if kind == "silent":
            f.write(f"0 127.0.0.1 {silent.getsockname()[1]} {secret()}\n")
        elif kind == "slow":
            f.write(f"0 127.0.0.1 {slow.srv.server_address[1]} {secret()}\n")
        f.write(f"1 127.0.0.1 {healthy[0].port} {secret()}\n2 127.0.0.1 {healthy[1].port} {secret()}\n")
    svc = chaos.Service(chaos.free_port(), datadir)
    svc.start()
    posted, lat = {}, []
    for n in range(events):
        t0 = time.time()
        status, data = chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=30.0)
        t1 = time.time()
        lat.append((t1 - t0) * 1000)
        posted[json.loads(data)["id"]] = t1
        time.sleep(0.05)
    end = time.time() + 20
    while time.time() < end and any(len(h.seen) < events for h in healthy):
        time.sleep(0.1)
    delays = []
    missing = 0
    for h in healthy:
        for eid, t1 in posted.items():
            got = h.seen.get(f"evt_{eid}")
            if got is None:
                missing += 1
            else:
                delays.append(got - t1)
    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)
    return lat, delays, missing


def burst():
    slow = Healthy(0.3)
    datadir = tempfile.mkdtemp(prefix="hooks-burst-")
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {slow.srv.server_address[1]} whsec_" + base64.b64encode(os.urandom(24)).decode() + "\n")
    svc = chaos.Service(chaos.free_port(), datadir)
    svc.start()

    def post(n):
        chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=60.0)

    threads = [threading.Thread(target=lambda k=k: [post(100 * k + j) for j in range(5)]) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    probes = []
    for n in range(25):
        t0 = time.time()
        post(10000 + n)
        probes.append((time.time() - t0) * 1000)
        time.sleep(0.1)
    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)
    return probes


def main():
    bad = []
    for label, kind in (("baseline (no bad endpoint)", "none"), ("one endpoint stalled (never answers)", "silent"),
                        ("one endpoint slow (answers after 300 ms)", "slow")):
        lat, delays, missing = run(kind)
        over = sum(1 for x in lat if x > 500) / len(lat)
        print(f"{label}: ingest p50 {percentile(lat, .5):.1f} ms, p99 {percentile(lat, .99):.0f} ms, {over:.0%} over 500 ms; "
              f"healthy delivery p50 {percentile(delays, .5) * 1000:.0f} ms, p99 {percentile(delays, .99) * 1000:.0f} ms, "
              f"max {max(delays) * 1000:.0f} ms, {missing} missing")
        if kind == "silent":
            if percentile(lat, .5) >= 50:
                bad.append(f"{label}: ingest median {percentile(lat, .5):.1f} ms, wanted under 50")
            if over > 0.15:
                bad.append(f"{label}: {over:.0%} of POSTs took over 500 ms, wanted at most 15%")
            if percentile(delays, .99) >= 5:
                bad.append(f"{label}: healthy delivery p99 {percentile(delays, .99):.2f} s, wanted under 5")
        if missing:
            bad.append(f"{label}: {missing} deliveries to healthy endpoints never arrived")
    probes = burst()
    print(f"burst of 40 events behind a slow endpoint: probe POSTs p50 {percentile(probes, .5):.0f} ms, "
          f"p99 {percentile(probes, .99):.0f} ms, max {max(probes):.0f} ms")
    if percentile(probes, .99) >= 1500:
        bad.append(f"burst: probe p99 {percentile(probes, .99):.0f} ms, wanted under 1500")
    if bad:
        for b in bad:
            print("FAIL:", b)
        return 1
    print("PASS: a bad endpoint costs ingest and the healthy endpoints a bounded, stated amount")
    return 0


if __name__ == "__main__":
    sys.exit(main())
