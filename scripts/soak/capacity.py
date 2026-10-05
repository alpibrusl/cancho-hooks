#!/usr/bin/env python3
"""The capacity measurements of docs/capacity.md: how many events a second one core of the service ingests, how many deliveries a second it makes (plain HTTP and https) with 1, 10
and 62 endpoints, at what cost in CPU, memory and disk, and with what latency to the sender.

    python3 scripts/soak/capacity.py --binary build/hooks [--out capacity-out] [--events 20000] [--reps 3] [--only ingest,deliver,https] [--service-cpu 3]

The tools that make the load are C (`scripts/bench/loadgen.c`, an open-connection load generator that prints percentiles; `scripts/bench/sink.c`, a receiver that answers 204), so that the
receivers are not what limits the figure; they are built here with `gcc` into the output directory and run on the cores the service is *not* pinned to. The service gets one core
(`taskset`), and what it costs is read from its own CPU time in `/proc` before and after: the cost per event or delivery in microseconds, from which "per core" follows (1e6 / cost).
https is measured by `scripts/bench/https_cost.py`, whose receiver is Python and whose cost figure is the service's own CPU, not the receiver's. The harness's CPU is not in any figure.

Writes `capacity.json` and `capacity.md`. The machine is part of the result: the CPU model, cores, load average at the start and at the end, and whether the machine was shared, are in both.
Not a benchmark of anyone else's service. Standard library only.
"""
import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BENCH = os.path.join(ROOT, "scripts", "bench")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def cpu(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def status(pid):
    rss = hwm = 0
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1])
        elif line.startswith("VmHWM:"):
            hwm = int(line.split()[1])
    return rss, hwm


def dir_bytes(d):
    t = 0
    for n in os.listdir(d):
        if n.endswith(".synced"):
            continue
        try:
            t += os.path.getsize(os.path.join(d, n))
        except OSError:
            pass
    return t


def stats(port):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=10).read())


def machine():
    cpu_model = ""
    for line in open("/proc/cpuinfo"):
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    return {"cpu": cpu_model, "cores": os.cpu_count(), "kernel": platform.release(), "loadavg_start": os.getloadavg()}


def build_tools(out):
    for name in ("loadgen", "sink"):
        exe = os.path.join(out, name)
        subprocess.run(["gcc", "-O2", "-o", exe, os.path.join(BENCH, name + ".c")], check=True)


def one(a, endpoints, events, conns, body, tools):
    """One run: a fresh service on one core, `endpoints` endpoints on one sink, `events` events over `conns` connections. Returns the numbers."""
    d = tempfile.mkdtemp(prefix="cap-", dir=a.tmp)
    sink_port = free_port()
    sink = subprocess.Popen([os.path.join(tools, "sink"), str(sink_port)], stderr=subprocess.PIPE, preexec_fn=lambda: os.sched_setaffinity(0, a.other_cpus))
    secret = "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(endpoints):
            f.write(f"{i} 127.0.0.1 {sink_port} {secret}\n")
    port = free_port()
    cmd = [a.binary, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--schedule", "100,100,100", "--retention-days", "0"]
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, preexec_fn=lambda: os.sched_setaffinity(0, {a.service_cpu}))
    assert p.stderr.readline().strip() == b"listening"
    rss0, _ = status(p.pid)
    c0, t0 = cpu(p.pid), time.time()
    out = subprocess.run([os.path.join(tools, "loadgen"), str(port), str(conns), str(events), str(body)], capture_output=True, text=True, timeout=1200,
                         preexec_fn=lambda: os.sched_setaffinity(0, a.other_cpus))
    t_ingest, c_ingest = time.time() - t0, cpu(p.pid) - c0
    m = re.search(r"([\d.]+) req/s, p50 ([\d.]+) ms, p99 ([\d.]+) ms, non-202 (\d+)", out.stdout)
    res = {"endpoints": endpoints, "events": events, "conns": conns, "body": body, "ingest_per_s": float(m.group(1)), "ingest_p50_ms": float(m.group(2)), "ingest_p99_ms": float(m.group(3)),
           "non_202": int(m.group(4)), "ingest_cpu_us_per_event": c_ingest / events * 1e6}
    if endpoints:
        want = events * endpoints
        end = time.time() + 300
        t_del = None
        while time.time() < end:
            if stats(port)["delivered"] >= want:
                t_del = time.time() - t0
                break
            time.sleep(0.05)
        time.sleep(0.2)
        c_all = cpu(p.pid) - c0
        res.update({"deliveries": want, "all_delivered": t_del is not None, "end_to_end_per_s": want / t_del if t_del else None, "cpu_total_s": c_all, "wall_s": t_del})
    rss, hwm = status(p.pid)
    res.update({"rss_kb_before": rss0, "rss_kb": rss, "hwm_kb": hwm, "data_bytes": dir_bytes(d)})
    p.terminate()
    p.wait()
    sink.terminate()
    sink.wait()
    shutil.rmtree(d, ignore_errors=True)
    return res


def med(v):
    return statistics.median(v) if v else None


def run_scenarios(a, tools):
    rows = {}
    for body in (200,):
        for name, endpoints, conns in (("ingest, 64 connections", 0, 64), ("ingest, one connection", 0, 1)):
            events = a.events if conns > 1 else max(2000, a.events // 4)
            runs = [one(a, endpoints, events, conns, body, tools) for _ in range(a.reps)]
            rows[name] = runs
            print(f"{name}: {med([r['ingest_per_s'] for r in runs]):.0f} events/s (p50 {med([r['ingest_p50_ms'] for r in runs]):.2f} ms, p99 {med([r['ingest_p99_ms'] for r in runs]):.2f} ms), "
                  f"{med([r['ingest_cpu_us_per_event'] for r in runs]):.1f} us of CPU an event", flush=True)
    base_ingest_us = med([r["ingest_cpu_us_per_event"] for r in rows["ingest, 64 connections"]])
    for endpoints in a.endpoint_counts:
        events = max(1000, a.events // max(1, endpoints // 2 or 1)) if endpoints > 1 else a.events
        runs = [one(a, endpoints, events, 64, 200, tools) for _ in range(a.reps)]
        for r in runs:
            if r["all_delivered"]:
                r["cpu_us_per_delivery"] = (r["cpu_total_s"] - r["events"] * base_ingest_us / 1e6) / r["deliveries"] * 1e6
        rows[f"deliver, {endpoints} endpoint{'s' if endpoints > 1 else ''}"] = runs
        us = [r["cpu_us_per_delivery"] for r in runs if "cpu_us_per_delivery" in r]
        print(f"deliver to {endpoints}: {med([r['end_to_end_per_s'] for r in runs if r['end_to_end_per_s']]):.0f} deliveries/s end to end, {med(us):.0f} us of CPU a delivery "
              f"({1e6 / med(us):.0f} a second a core), RSS {med([r['rss_kb'] for r in runs]) / 1024:.1f} MiB", flush=True)
    return rows


def run_https(a):
    env = dict(os.environ, HOOKS_BIN=a.binary, REPS=str(a.reps), PIN="1")
    r = subprocess.run([sys.executable, os.path.join(BENCH, "https_cost.py"), str(a.https_deliveries)], capture_output=True, text=True, env=env, timeout=3600)
    return r.stdout


def markdown(o):
    L = [f"# Capacity measurements", "", f"Machine: {o['machine']['cpu']}, {o['machine']['cores']} cores, kernel {o['machine']['kernel']}; load average at the start {o['machine']['loadavg_start']}, at the end {o['loadavg_end']}. "
         f"Service on core {o['service_cpu']}, load generator and receivers on cores {sorted(o['other_cpus'])}. {o['reps']} runs of each row (the median, with the least and the most).", ""]
    L.append("| row | events/s (ingest) | p50 / p99 to the sender (ms) | CPU per event (us) | deliveries/s end to end | CPU per delivery (us) | deliveries/s per core | RSS (MiB) | VmHWM (MiB) | data dir (MB) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, runs in o["rows"].items():
        def rng(key, nd=0, scale=1.0):
            v = sorted(r[key] / scale for r in runs if r.get(key) is not None)
            return "" if not v else (f"{statistics.median(v):.{nd}f} ({v[0]:.{nd}f} to {v[-1]:.{nd}f})")
        us = [r["cpu_us_per_delivery"] for r in runs if "cpu_us_per_delivery" in r]
        per_core = f"{1e6 / statistics.median(us):.0f}" if us else ""
        L.append(f"| {name} | {rng('ingest_per_s')} | {statistics.median([r['ingest_p50_ms'] for r in runs]):.2f} / {statistics.median([r['ingest_p99_ms'] for r in runs]):.2f} | {rng('ingest_cpu_us_per_event', 1)} | "
                 f"{rng('end_to_end_per_s')} | {rng('cpu_us_per_delivery')} | {per_core} | {rng('rss_kb', 1, 1024)} | {rng('hwm_kb', 1, 1024)} | {rng('data_bytes', 1, 1e6)} |")
    if o.get("https"):
        L += ["", "https (`scripts/bench/https_cost.py`, the receiver is Python; the figure is the service's CPU per delivery):", "", "```", o["https"].strip(), "```"]
    return "\n".join(L) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary", default=os.path.join(ROOT, "build", "hooks"))
    p.add_argument("--out", default="capacity-out")
    p.add_argument("--events", type=int, default=20000)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--only", default="ingest,deliver,https")
    p.add_argument("--service-cpu", type=int, default=-1, help="the core of the service (default: the last)")
    p.add_argument("--endpoint-counts", type=lambda v: [int(x) for x in v.split(",")], default=[1, 10, 62], help="the numbers of endpoints to deliver to, the last being the most the build takes (default 1,10,62)")
    p.add_argument("--https-deliveries", type=int, default=600)
    p.add_argument("--tmp", default=None, help="where the data directories go (a tmpfs makes the disk not part of the figure)")
    a = p.parse_args()
    a.binary = os.path.abspath(a.binary)
    n = os.cpu_count() or 1
    a.service_cpu = a.service_cpu if a.service_cpu >= 0 else n - 1
    a.other_cpus = set(range(n)) - {a.service_cpu} or {a.service_cpu}
    os.makedirs(a.out, exist_ok=True)
    build_tools(a.out)
    o = {"machine": machine(), "service_cpu": a.service_cpu, "other_cpus": sorted(a.other_cpus), "reps": a.reps, "binary_sha256": None, "rows": {}}
    import hashlib
    o["binary_sha256"] = hashlib.sha256(open(a.binary, "rb").read()).hexdigest()
    only = set(a.only.split(","))
    if only & {"ingest", "deliver"}:
        o["rows"] = run_scenarios(a, a.out)
    if "https" in only:
        o["https"] = run_https(a)
    o["loadavg_end"] = os.getloadavg()
    with open(os.path.join(a.out, "capacity.json"), "w") as f:
        json.dump(o, f, indent=1)
    with open(os.path.join(a.out, "capacity.md"), "w") as f:
        f.write(markdown(o))
    print(f"wrote {a.out}/capacity.md and capacity.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
