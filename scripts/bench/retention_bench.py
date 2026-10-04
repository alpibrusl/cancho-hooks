#!/usr/bin/env python3
"""The logs are bounded by retention, not by history (docs/production.md 0.2, docs/retention.md), measured.

    python3 scripts/bench/retention_bench.py through --events 10000000        # millions of events through one endpoint, the disk sampled
    python3 scripts/bench/retention_bench.py start --events 1000000           # the start with that many events retained

`through`: the C load generator posts the events (64 keep-alive connections, in chunks, each chunk after the deliveries of the one before are nearly all made: an event not delivered is not droppable, and a backlog is not what retention bounds) while the C sink receives every delivery; retention
is a few seconds (`--retention-ms`, the test knob of docs/retention.md), a segment 32 MiB and the outcomes log's limit 16 MiB, so what takes a
month in production happens in seconds. Every two seconds the script writes down how many bytes the data directory holds, what the service
says (first and last retained id, segments, snapshots, the longest step of the loop) and its resident memory. The result is a table and the
peak against what the history would have taken.

`start`: the events are posted with retention off and no endpoint (and then again with one endpoint that has been sent all of them), the
service is killed, and the time from `exec` to `listening` is measured on the restart, with the files in the page cache.

The machine is part of the result. Not a benchmark of anyone else's service.
"""
import argparse
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def build_tools():
    for name in ("loadgen", "sink"):
        exe = os.path.join(HERE, name)
        if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(exe + ".c"):
            subprocess.run(["gcc", "-O2", "-o", exe, exe + ".c"], check=True)


def stats(port):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=10).read())


def disk_bytes(d):
    total = 0
    for n in os.listdir(d):
        if n.endswith(".synced"):
            continue
        try:
            total += os.path.getsize(os.path.join(d, n))
        except OSError:
            pass
    return total


def rss_kb(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1])
    return 0


def cpu_s(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


class Service:
    def __init__(self, binary, d, args):
        self.port = free_port()
        self.cmd = [binary, "--port", str(self.port), "--dir", d, "--allow-private-hosts", "1", *args]
        self.proc = None
        self.lines = []

    def start(self):
        t0 = time.time()
        self.proc = subprocess.Popen(self.cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        first = self.proc.stderr.readline().strip()
        self.took = time.time() - t0
        assert first == b"listening", first

        def drain():
            for line in self.proc.stderr:
                self.lines.append(line)

        threading.Thread(target=drain, daemon=True).start()
        return self.took

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def stop(self):
        self.proc.terminate()
        self.proc.wait()


def loadgen(port, events, conns, body):
    out = subprocess.run([os.path.join(HERE, "loadgen"), str(port), str(conns), str(events), str(body)], capture_output=True, text=True, timeout=3600)
    return out.stdout.strip()


def through(a):
    d = a.dir or tempfile.mkdtemp(prefix="hooks-bench-", dir=os.environ.get("RETENTION_TMP"))
    sink_port = free_port()
    sink = subprocess.Popen([os.path.join(HERE, "sink"), str(sink_port)], stderr=subprocess.PIPE)
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {sink_port} {secret}\n")
    svc = Service(a.binary, d, ["--schedule", "100,100,100", "--retention-ms", str(a.retention_ms), "--window-ms", str(a.window_ms),
                                "--segment-bytes", str(a.segment_bytes), "--delivery-log-bytes", str(a.delivery_log_bytes)])
    svc.start()
    samples = []
    stop = threading.Event()
    t0 = time.time()

    def sampler():
        while not stop.is_set():
            try:
                s = stats(svc.port)
                samples.append((time.time() - t0, disk_bytes(d), s, rss_kb(svc.proc.pid)))
            except Exception:
                pass
            time.sleep(2)

    th = threading.Thread(target=sampler)
    th.start()
    done = 0
    while done < a.events:
        n = min(a.chunk, a.events - done)
        out = loadgen(svc.port, n, a.conns, a.body)
        done += n
        print(f"[{time.time() - t0:7.1f}s] {done:>10} events posted: {out}", flush=True)
        # the poster waits for the deliveries: an event not delivered yet is not droppable, and a backlog is not what retention bounds
        while stats(svc.port)["delivered"] < done - a.backlog:
            time.sleep(0.5)
    posted = time.time() - t0
    ok = False
    while time.time() - t0 < posted + 3600:
        s = stats(svc.port)
        if s["delivered"] >= a.events:
            ok = True
            break
        time.sleep(1)
    delivered_at = time.time() - t0
    # the retention drains what is left
    drained = False
    end = time.time() + 60
    while time.time() < end:
        s = stats(svc.port)
        if s["events_first_id"] == s["events_last_id"] + 1 and s["events_bytes"] == 0:
            drained = True
            break
        time.sleep(1)
    stop.set()
    th.join()
    s = stats(svc.port)
    final_disk = disk_bytes(d)
    cpu = cpu_s(svc.proc.pid)
    rss = rss_kb(svc.proc.pid)
    print()
    print(f"{a.events} events posted in {posted:.0f} s, all {s['delivered']} delivered after {delivered_at:.0f} s ({s['delivered'] / delivered_at:.0f}/s end to end), "
          f"hooks CPU {cpu:.0f} s; retention {a.retention_ms} ms, window {a.window_ms} ms, segment {a.segment_bytes} B, outcomes limit {a.delivery_log_bytes} B")
    print(f"{'t (s)':>8} {'disk (MB)':>10} {'events retained':>16} {'segments':>9} {'outcomes (MB)':>14} {'snapshots':>10} {'RSS (MB)':>9} {'step (ms)':>10}")
    step = max(1, len(samples) // 24)
    for i, (t, disk, st, rss_k) in enumerate(samples):
        if i % step == 0 or i == len(samples) - 1:
            print(f"{t:8.0f} {disk / 1e6:10.1f} {st['events_last_id'] - st['events_first_id'] + 1:16} {st['events_segments']:9} {st['delivery_bytes'] / 1e6:14.2f} "
                  f"{st['snapshots']:10} {rss_k / 1024:9.1f} {st['maintenance_ms_max']:10}")
    peak = max(x[1] for x in samples) if samples else final_disk
    sizes = [(st["events_bytes"], st["events_last_id"] - st["events_first_id"] + 1) for _, _, st, _ in samples if st["events_last_id"] - st["events_first_id"] > 1000]
    per_event = sum(b for b, _ in sizes) / max(1, sum(n for _, n in sizes)) if sizes else 0
    history = per_event * a.events + a.events * 77
    print(f"peak disk {peak / 1e6:.1f} MB; at the end {final_disk / 1e6:.2f} MB ({'drained' if drained else 'NOT drained'}); the history would be about {history / 1e6:.0f} MB "
          f"({per_event:.0f} B an event, 77 B an outcome): {history / max(1, peak):.0f}x the peak")
    print(f"segments sealed {s['segments_sealed']}, dropped {s['segments_dropped']}, snapshots {s['snapshots']}; the longest step of the loop: {s['maintenance_ms_max']} ms; "
          f"errors {s['maintenance_errors']}; resident {rss / 1024:.0f} MB at the end")
    svc.stop()
    sink.terminate()
    sink.wait()
    if not a.dir:
        shutil.rmtree(d, ignore_errors=True)
    return 0 if ok and drained else 1


def start(a):
    d = a.dir or tempfile.mkdtemp(prefix="hooks-bench-", dir=os.environ.get("RETENTION_TMP"))
    sink_port = free_port()
    sink = subprocess.Popen([os.path.join(HERE, "sink"), str(sink_port)], stderr=subprocess.PIPE)
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    knobs = ["--retention-days", "0", "--segment-bytes", str(a.segment_bytes), "--delivery-log-bytes", str(a.delivery_log_bytes)]
    rows = []
    for endpoints in (0, 1):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)
        if endpoints:
            with open(os.path.join(d, "endpoints.conf"), "w") as f:
                f.write(f"0 127.0.0.1 {sink_port} {secret}\n")
        svc = Service(a.binary, d, knobs + ["--schedule", "100,100,100"])
        svc.start()
        done = 0
        t0 = time.time()
        while done < a.events:
            n = min(a.chunk, a.events - done)
            loadgen(svc.port, n, a.conns, a.body)
            done += n
        if endpoints:
            while stats(svc.port)["delivered"] < a.events:
                time.sleep(1)
        s = stats(svc.port)
        time.sleep(1)
        svc.kill()
        took = []
        for _ in range(3):
            svc2 = Service(a.binary, d, knobs + ["--schedule", "100,100,100"])
            took.append(svc2.start())
            if len(took) == 1:
                s2 = stats(svc2.port)
                rss = rss_kb(svc2.proc.pid)
            svc2.kill()
        files = sorted(n for n in os.listdir(d) if not n.endswith(".synced"))
        ev_bytes = sum(os.path.getsize(os.path.join(d, n)) for n in files if n.startswith("events"))
        dl_bytes = os.path.getsize(os.path.join(d, "delivery.seg"))
        rows.append((endpoints, s2["events_last_id"] - s2["events_first_id"] + 1, len([n for n in files if n.startswith("events") and n.endswith(".seg")]), ev_bytes, dl_bytes, took, rss))
    print(f"{'endpoints':>9} {'events retained':>16} {'segments':>9} {'events (MB)':>12} {'outcomes (MB)':>14} {'start (s), three runs':>26} {'RSS (MB)':>9}")
    for e, n, segs, eb, db, took, rss in rows:
        print(f"{e:9} {n:16} {segs:9} {eb / 1e6:12.1f} {db / 1e6:14.2f} {'  '.join(f'{t:.2f}' for t in took):>26} {rss / 1024:9.0f}")
    sink.terminate()
    sink.wait()
    if not a.dir:
        shutil.rmtree(d, ignore_errors=True)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["through", "start"])
    p.add_argument("--binary", default=os.environ.get("HOOKS_BIN") or os.path.join(ROOT, "build", "hooks"))
    p.add_argument("--events", type=int, default=1000000)
    p.add_argument("--chunk", type=int, default=100000)
    p.add_argument("--backlog", type=int, default=20000, help="events posted but not yet delivered that the poster tolerates between chunks")
    p.add_argument("--conns", type=int, default=64)
    p.add_argument("--body", type=int, default=200)
    p.add_argument("--retention-ms", type=int, default=20000)
    p.add_argument("--window-ms", type=int, default=5000)
    p.add_argument("--segment-bytes", type=int, default=33554432)
    p.add_argument("--delivery-log-bytes", type=int, default=16777216)
    p.add_argument("--dir")
    a = p.parse_args()
    build_tools()
    return through(a) if a.mode == "through" else start(a)


if __name__ == "__main__":
    sys.exit(main())
