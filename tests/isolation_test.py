#!/usr/bin/env python3
"""A bad endpoint, and what it costs the others (docs/design.md, section 6 criterion 3, and section 16).

    python3 tests/isolation_test.py build/hooks

One endpoint is bad and two (or one) are healthy and answer at once. Delivery attempts do not hold the loop, so a bad endpoint
should cost neither ingest nor the healthy endpoints more than a few milliseconds. The bad endpoint is, in turn:

  silent     accepts connections and never answers
  slow       answers after 300 ms
  blackhole  a listener whose accept queue is full, so the SYN is dropped and a blocking connect would wait for minutes

and for each, 80 events are posted at about 20 a second. Thresholds, fixed before the run (the first two are the ones this
test was first written with, when delivery did hold the loop and the slow case failed the first of them by a factor of five):

  * ingest: the median POST under 50 ms, and at most 5% of POSTs over 500 ms
  * every event reaches the healthy endpoints, and the 99th percentile of the time from the POST's answer to the request at a
    healthy endpoint is under 1 s

Two bursts then test the limits on in-flight attempts:

  * 100 events queued at once behind a *silent* endpoint, one healthy endpoint: every event reaches the healthy one within 1 s
    of the last POST's answer. Without a per-endpoint cap the silent endpoint's attempts fill every slot and the healthy
    endpoint waits for their deadlines (2 s).
  * 40 events behind a *slow* endpoint, then 25 probe POSTs 100 ms apart: the 99th percentile of the probes under 500 ms.
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

http.server.HTTPServer.request_queue_size = 128   # the default is 5: a burst of connections would lose a SYN and wait a second


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
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


class Bad:
    """The bad endpoint, and whatever keeps it bad open."""

    def __init__(self, kind):
        self.keep = []
        self.slow = Healthy(0.3) if kind == "slow" else None
        if kind == "slow":
            self.port = self.slow.port
            return
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1 if kind == "blackhole" else 64)
        self.keep.append(s)
        self.port = s.getsockname()[1]
        if kind == "silent":
            def eat():
                while True:
                    c, _ = s.accept()
                    self.keep.append(c)
            threading.Thread(target=eat, daemon=True).start()
        else:
            # Fill the accept queue: connect and never accept, until a connect no longer completes.
            for _ in range(5000):
                try:
                    self.keep.append(socket.create_connection(("127.0.0.1", self.port), timeout=0.15))
                except OSError:
                    break


def start(bad_port, healthy):
    datadir = tempfile.mkdtemp(prefix="hooks-isolation-")
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        n = 0
        if bad_port is not None:
            f.write(f"0 127.0.0.1 {bad_port} {secret()}\n")
            n = 1
        for h in healthy:
            f.write(f"{n} 127.0.0.1 {h.port} {secret()}\n")
            n += 1
    svc = chaos.Service(chaos.free_port(), datadir)
    svc.start()
    return svc, datadir


def stop(svc, datadir):
    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)


def steady(kind, events=80):
    healthy = [Healthy(), Healthy()]
    bad = Bad(kind) if kind != "none" else None
    svc, datadir = start(bad.port if bad else None, healthy)
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
    delays, missing = [], 0
    for h in healthy:
        for eid, t1 in posted.items():
            got = h.seen.get(f"evt_{eid}")
            if got is None:
                missing += 1
            else:
                delays.append(got - t1)
    stop(svc, datadir)
    return lat, delays, missing


def burst_silent(events=100):
    healthy = Healthy()
    bad = Bad("silent")
    svc, datadir = start(bad.port, [healthy])

    def post(n):
        return json.loads(chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=60.0)[1])["id"]

    ids, lock = [], threading.Lock()

    def worker(k):
        for j in range(events // 10):
            i = post(1000 * k + j)
            with lock:
                ids.append(i)

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    last = time.time()
    end = last + 20
    while time.time() < end and len(healthy.seen) < events:
        time.sleep(0.05)
    arrived = [healthy.seen.get(f"evt_{i}") for i in ids]
    stop(svc, datadir)
    missing = sum(1 for a in arrived if a is None)
    late = max((a - last for a in arrived if a is not None), default=float("inf"))
    return missing, late


def burst_slow():
    slow = Healthy(0.3)
    svc, datadir = start(slow.port, [])
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
    stop(svc, datadir)
    return probes


def main():
    bad = []
    for label, kind in (("baseline (no bad endpoint)", "none"), ("one endpoint silent", "silent"),
                        ("one endpoint slow (300 ms)", "slow"), ("one endpoint blackholed (accept queue full)", "blackhole")):
        lat, delays, missing = steady(kind)
        over = sum(1 for x in lat if x > 500) / len(lat)
        print(f"{label}: ingest p50 {percentile(lat, .5):.1f} ms, p99 {percentile(lat, .99):.0f} ms, {over:.0%} over 500 ms; "
              f"healthy delivery p50 {percentile(delays, .5) * 1000:.0f} ms, p99 {percentile(delays, .99) * 1000:.0f} ms, "
              f"max {max(delays) * 1000:.0f} ms, {missing} missing")
        if kind != "none":
            if percentile(lat, .5) >= 50:
                bad.append(f"{label}: ingest median {percentile(lat, .5):.1f} ms, wanted under 50")
            if over > 0.05:
                bad.append(f"{label}: {over:.0%} of POSTs took over 500 ms, wanted at most 5%")
            if percentile(delays, .99) >= 1:
                bad.append(f"{label}: healthy delivery p99 {percentile(delays, .99) * 1000:.0f} ms, wanted under 1000")
        if missing:
            bad.append(f"{label}: {missing} deliveries to healthy endpoints never arrived")
    missing, late = burst_silent()
    print(f"burst of 100 events behind a silent endpoint: the healthy endpoint missed {missing}; the last arrived {late * 1000:.0f} ms after the last POST")
    if missing or late >= 1:
        bad.append(f"burst behind a silent endpoint: {missing} missing, last {late:.2f} s after the POSTs, wanted none and under 1 s")
    probes = burst_slow()
    print(f"burst of 40 events behind a slow endpoint: probe POSTs p50 {percentile(probes, .5):.0f} ms, "
          f"p99 {percentile(probes, .99):.0f} ms, max {max(probes):.0f} ms")
    if percentile(probes, .99) >= 500:
        bad.append(f"burst behind a slow endpoint: probe p99 {percentile(probes, .99):.0f} ms, wanted under 500")
    if bad:
        for b in bad:
            print("FAIL:", b)
        return 1
    print("PASS: a bad endpoint costs ingest and the healthy endpoints almost nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
