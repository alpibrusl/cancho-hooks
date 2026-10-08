#!/usr/bin/env python3
"""Compare cancho-hooks with other open-source webhook services on one machine, with one workload, measured the same way.

    python3 scripts/bench/compare/compare.py --out bench-out [--systems hooks,hooks-pg,svix] [--reps 3] [--smoke]

What is measured, for each system and each number of endpoints E (default 1 and 10), `reps` times, each time on a fresh system:

  throughput  N events posted over 64 keep-alive connections as fast as the service answers; the time until every one of the N x E deliveries has reached the
              receivers; missing and duplicate deliveries (counted per endpoint and event at the receiver); the CPU the whole system used (every container of it,
              from the cgroups) per delivery; its peak memory.
  latency     events posted at a fixed rate (open loop: a request is timed from when it was due, so a slow service cannot hide a queue); time from the post to the first
              arrival at the receiver, p50, p99, max.
  idle        the same system with E endpoints and nothing sent, for 30 s: its CPU and memory.

The receivers (`sink.c`: E ports, one process) and the load (`load.c`) are C, on cores of their own, and are checked: a run in which either used more than 85% of
its core is marked INVALID. The system under test gets `--sut-cpus` (every one of its containers, the database and the queue included, share them). The CPU governor
of every core used must be `performance`, or the script refuses to run (--allow-governor to override; the result says so).

The systems are started as the project documents it, with the defaults, apart from what a local receiver needs (private addresses allowed), and each is described
in the result (image and digest, settings). Nothing is tuned for one and not for another. See README.md in this directory for what the comparison is and is not.
Needs docker, gcc and a Linux host (epoll, cgroups). Standard library only.
"""
import argparse
import atexit
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SINK_BASE = 20000
PADDING = "x" * 120
SECRET = "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"
RUN = f"bench{os.getpid()}"
CLEANUP = []
STARTS = [0]          # every start of a system has names of its own: a container that is still being removed keeps its name for a moment


def sh(*cmd, input=None, check=True, timeout=300):
    r = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)[:200]}: {r.stderr.strip()[:600] or r.stdout.strip()[:600]}")
    return r.stdout.strip()


def port_free(p):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # as a server does: the TIME_WAIT sockets of the last run do not hold the port
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http(method, url, body=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method, headers=dict(headers or {}, **({"Content-Type": "application/json"} if body is not None else {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def wait_for(fn, what, secs=90):
    end = time.time() + secs
    last = None
    while time.time() < end:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {what}: {last}")


# ---- cgroups: the CPU and memory of a container, however docker lays them out -------------------------------------------------------------------------------

def cgroup_dir(cid):
    for p in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}", f"/sys/fs/cgroup/cpu/docker/{cid}"):
        if os.path.exists(os.path.join(p, "cpu.stat")):
            return p
    return None


def cg_cpu_s(d):
    for line in open(os.path.join(d, "cpu.stat")):
        if line.startswith("usage_usec"):
            return int(line.split()[1]) / 1e6
    return None


def cg_mem(d):
    try:
        return int(open(os.path.join(d, "memory.current")).read())
    except OSError:
        return None


class Group:
    """The containers of one system: their CPU time, and their memory sampled every 200 ms (the peak of the sum, and of each)."""

    def __init__(self, roles):
        self.dirs = {r: cgroup_dir(sh("docker", "inspect", "-f", "{{.Id}}", n)) for r, n in roles.items()}
        self.ok = all(self.dirs.values())
        self.peak = {r: 0 for r in roles}
        self.peak_sum = 0
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def cpu(self):
        return {r: cg_cpu_s(d) for r, d in self.dirs.items()} if self.ok else {}

    def _run(self):
        while not self._stop.is_set():
            tot = 0
            for r, d in self.dirs.items():
                m = cg_mem(d)
                if m:
                    self.peak[r] = max(self.peak[r], m)
                    tot += m
            self.peak_sum = max(self.peak_sum, tot)
            time.sleep(0.2)

    def start(self):
        if self.ok:
            self.t.start()

    def stop(self):
        self._stop.set()


# ---- the systems ----------------------------------------------------------------------------------------------------------------------------------------------

class System:
    name = ""
    notes = ""

    def __init__(self, a):
        self.a = a
        self.names = {}          # role -> container name
        self.volumes = []
        STARTS[0] += 1
        self.tag = f"{RUN}-{STARTS[0]}"

    def container(self, role, image, args, env=None, extra=None, cpus=None):
        n = f"{self.tag}-{self.name}-{role}"
        cmd = ["docker", "run", "-d", "--name", n, "--network", "host", "--cpuset-cpus", cpus or self.a.sut_cpus]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        sh(*cmd, *(extra or []), image, *args)
        self.names[role] = n
        CLEANUP.append(n)

    def postgres(self, port):
        self.container("postgres", self.a.pg_image, ["-c", f"port={port}", "-c", "max_connections=200"], env={"POSTGRES_HOST_AUTH_METHOD": "trust"})
        wait_for(lambda: sh("docker", "exec", self.names["postgres"], "pg_isready", "-h", "127.0.0.1", "-p", str(port), check=False).endswith("accepting connections"), "postgres")
        time.sleep(1.0)

    def stop(self):
        for n in self.names.values():
            sh("docker", "rm", "-f", "-v", n, check=False)
        for v in self.volumes:
            sh("docker", "volume", "rm", "-f", v, check=False)
        self.names = {}

    def describe(self):
        imgs = {}
        for role, n in self.names.items():
            imgs[role] = sh("docker", "inspect", "-f", "{{.Config.Image}} {{.Image}}", n, check=False)
        return {"images": imgs, "notes": self.notes}


class HooksFiles(System):
    name = "hooks"
    notes = "cancho-hooks as its README runs it: the container image, its own log in a volume, endpoints.conf, no database."

    def start(self, E, sink_base):
        for p in (18080,):
            assert port_free(p), f"port {p} is in use"
        vol = f"{self.tag}-hooks-data"
        sh("docker", "volume", "create", vol)
        self.volumes.append(vol)
        conf = "".join(f"{i} 127.0.0.1 {sink_base + i} {SECRET}\n" for i in range(E))
        sh("docker", "run", "--rm", "-i", "--user", "root", "-v", f"{vol}:/d", "--entrypoint", "sh", self.a.hooks_image, "-c", "cat > /d/endpoints.conf && chown -R 10001 /d && chmod 700 /d", input=conf)
        self.container("service", self.a.hooks_image, ["--port", "18080", "--dir", "/var/lib/hooks", "--allow-private-hosts", "1"], extra=["-v", f"{vol}:/var/lib/hooks"])
        wait_for(lambda: http("GET", "http://127.0.0.1:18080/readyz")[0] == 200, "hooks /readyz")
        return {"port": 18080, "path": "/events", "headers": "", "body": '{"type":"bench.event","n":"@@@@@@@@@@","pad":"' + PADDING + '"}', "stats": "http://127.0.0.1:18080/stats"}


class HooksPG(HooksFiles):
    name = "hooks-pg"
    notes = "cancho-hooks with a PostgreSQL (the endpoints from the table, every delivery attempt a row of the history): the same data work as Svix does."

    def start(self, E, sink_base):
        assert port_free(18080) and port_free(55440), "port 18080 or 55440 is in use"
        self.postgres(55440)
        pg = self.names["postgres"]
        schema = sh("docker", "run", "--rm", "--entrypoint", "cat", self.a.hooks_image, "/usr/share/hooks/schema.sql")
        sh("docker", "exec", pg, "psql", "-h", "127.0.0.1", "-p", "55440", "-U", "postgres", "-c", "create database hooks")
        sh("docker", "exec", "-i", pg, "psql", "-h", "127.0.0.1", "-p", "55440", "-U", "postgres", "-d", "hooks", "-q", "-f", "-", input=schema)
        rows = ",".join(f"({i}, '127.0.0.1', {sink_base + i}, '{SECRET}')" for i in range(E))
        sh("docker", "exec", pg, "psql", "-h", "127.0.0.1", "-p", "55440", "-U", "postgres", "-d", "hooks", "-q", "-c", f"insert into endpoints (id, host, port, secret) values {rows}")
        vol = f"{self.tag}-hooks-data"
        sh("docker", "volume", "create", vol)
        self.volumes.append(vol)
        sh("docker", "run", "--rm", "--user", "root", "-v", f"{vol}:/d", "--entrypoint", "sh", self.a.hooks_image, "-c", "chown -R 10001 /d && chmod 700 /d")
        self.container("service", self.a.hooks_image, ["--port", "18080", "--dir", "/var/lib/hooks", "--allow-private-hosts", "1", "--pg-host", "127.0.0.1", "--pg-port", "55440",
                                                       "--pg-user", "postgres", "--pg-database", "hooks"], extra=["-v", f"{vol}:/var/lib/hooks"])
        wait_for(lambda: http("GET", "http://127.0.0.1:18080/readyz")[0] == 200, "hooks /readyz (reads the endpoints table)")
        return {"port": 18080, "path": "/events", "headers": "", "body": '{"type":"bench.event","n":"@@@@@@@@@@","pad":"' + PADDING + '"}', "stats": "http://127.0.0.1:18080/stats"}


class Svix(System):
    name = "svix"
    notes = ("Svix open-source server as its documentation runs it: svix-server, PostgreSQL and Redis (queue), default settings, apart from the JWT secret, the listen address, "
             "and a whitelist for 127.0.0.0/8 because the receivers are local.")

    def start(self, E, sink_base):
        for p in (18071, 55441, 56379):
            assert port_free(p), f"port {p} is in use"
        self.postgres(55441)
        pg = self.names["postgres"]
        sh("docker", "exec", pg, "psql", "-h", "127.0.0.1", "-p", "55441", "-U", "postgres", "-c", "create database svix")
        self.container("redis", self.a.redis_image, ["redis-server", "--port", "56379", "--save", "", "--appendonly", "no"])
        wait_for(lambda: sh("docker", "exec", self.names["redis"], "redis-cli", "-p", "56379", "ping", check=False) == "PONG", "redis")
        secret = "bench-secret-" + "k" * 24
        env = {"SVIX_JWT_SECRET": secret, "SVIX_DB_DSN": "postgresql://postgres@127.0.0.1:55441/svix", "SVIX_REDIS_DSN": "redis://127.0.0.1:56379", "SVIX_QUEUE_TYPE": "redis",
               "SVIX_LISTEN_ADDRESS": "127.0.0.1:18071", "SVIX_WHITELIST_SUBNETS": '["127.0.0.0/8"]'}
        # the command validates the whole configuration, so it is given the same environment as the server
        token = sh("docker", "run", "--rm", *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], self.a.svix_image, "svix-server", "jwt", "generate").split()[-1]
        self.container("service", self.a.svix_image, [], env=env)
        auth = {"Authorization": f"Bearer {token}"}
        wait_for(lambda: http("GET", "http://127.0.0.1:18071/api/v1/health/", headers=auth)[0] in (200, 204), "svix health", secs=120)
        _, app = http("POST", "http://127.0.0.1:18071/api/v1/app/", {"name": "bench"}, auth)
        for i in range(E):
            http("POST", f"http://127.0.0.1:18071/api/v1/app/{app['id']}/endpoint/", {"url": f"http://127.0.0.1:{sink_base + i}/", "version": 1, "description": f"bench {i}"}, auth)
        payload = '{"type":"bench.event","n":"@@@@@@@@@@","pad":"' + PADDING + '"}'
        return {"port": 18071, "path": f"/api/v1/app/{app['id']}/msg/", "headers": f"Authorization: Bearer {token}\n", "body": '{"eventType":"bench.event","payload":' + payload + '}', "stats": None}


SYSTEMS = {"hooks": HooksFiles, "hooks-pg": HooksPG, "svix": Svix}


# ---- one run --------------------------------------------------------------------------------------------------------------------------------------------------

def pct(v, q):
    return v[min(len(v) - 1, int(len(v) * q))] if v else None


def proc_cpu(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def count(sink_ctl):
    with urllib.request.urlopen(f"http://127.0.0.1:{sink_ctl}/count", timeout=10) as r:
        return int(r.read())


def dump(sink_ctl):
    with urllib.request.urlopen(f"http://127.0.0.1:{sink_ctl}/dump", timeout=120) as r:
        return r.read().decode().split("\n")


def run_load(a, tgt, work, conns, total, rate, tag):
    hf, bf, of = (os.path.join(work, f"{tag}.{x}") for x in ("headers", "body", "out"))
    open(hf, "w").write(tgt["headers"])
    open(bf, "w").write(tgt["body"])
    p = subprocess.Popen(["taskset", "-c", str(a.load_cpu), os.path.join(a.out, "load"), "127.0.0.1", str(tgt["port"]), str(conns), str(total), str(rate), tgt["path"], hf, bf, of],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    _, _, ru = os.wait4(p.pid, 0)
    out, err = p.stdout.read(), p.stderr.read()
    wall = time.time() - t0
    p.returncode = 0
    if not out.strip():
        raise RuntimeError(f"load failed: {err.strip()}")
    return out.strip(), (ru.ru_utime + ru.ru_stime) / max(wall, 1e-9), of


def analyse(out_file, dump_lines, N, E):
    send, ack_bad = {}, 0
    for line in open(out_file):
        n, due, snd, ack, st = line.split()
        send[int(n)] = int(snd)
        if not 200 <= int(st) < 300:
            ack_bad += 1
    first, seen = {}, {}
    last = 0
    for line in dump_lines:
        if not line:
            continue
        ep, n, t = line.split()
        k = (int(ep), int(n))
        seen[k] = seen.get(k, 0) + 1
        if k not in first:
            first[k] = int(t)
        last = max(last, int(t))
    lat = sorted((first[k] - send[k[1]]) / 1e6 for k in first if k[1] in send)
    dups = sum(c - 1 for c in seen.values() if c > 1)
    missing = N * E - len(first)
    t_first_send = min(send.values()) if send else 0
    span = (last - t_first_send) / 1e9 if last else None
    return {"missing": missing, "duplicates": dups, "non_2xx": ack_bad, "e2e_p50_ms": pct(lat, 0.50), "e2e_p99_ms": pct(lat, 0.99), "e2e_max_ms": lat[-1] if lat else None,
            "span_s": span, "deliveries_per_s": (len(first) / span) if span else None}


def one(a, sysname, E, mode, N, rate, rep):
    s = SYSTEMS[sysname](a)
    work = os.path.join(a.out, "work", f"{sysname}-{E}-{mode}-{rep}")
    os.makedirs(work, exist_ok=True)
    cap = N * E + 100000
    sink = subprocess.Popen(["taskset", "-c", str(a.sink_cpu), os.path.join(a.out, "sink"), str(SINK_BASE), str(E), str(cap)], stderr=subprocess.PIPE)
    CLEANUP.append(("pid", sink.pid))
    assert sink.stderr.readline().strip() == b"listening"
    ctl = SINK_BASE + E
    g = None
    try:
        tgt = s.start(E, SINK_BASE)
        g = Group(s.names)
        # warm up, and prove that the setup delivers: 200 events, every one to every endpoint, then forget them
        w = 200
        out, _, _ = run_load(a, tgt, work, 8, w, 0, "warm")
        wait_for(lambda: count(ctl) >= w * E, f"the {w * E} warm-up deliveries (the system accepted the events but does not deliver them: see its logs)", secs=60)
        time.sleep(2.0)
        urllib.request.urlopen(f"http://127.0.0.1:{ctl}/reset", timeout=5).read()
        res = {"system": sysname, "endpoints": E, "mode": mode, "rep": rep}
        if mode == "idle":
            g.start()
            c0, t0 = g.cpu(), time.time()
            time.sleep(a.idle_s)
            c1 = g.cpu()
            res.update({"idle_cpu_core_pct": {r: (c1[r] - c0[r]) / (time.time() - t0) * 100 for r in c0} if c0 else None})
        else:
            g.start()
            c0, sink_c0 = g.cpu(), proc_cpu(sink.pid)
            t0 = time.time()
            out, load_util, of = run_load(a, tgt, work, 64 if rate == 0 else 16, N, rate, "m")
            last_n, still = -1, time.time()
            while True:      # until all arrived, or nothing arrives for 30 s
                n = count(ctl)
                if n >= N * E:
                    break
                if n != last_n:
                    last_n, still = n, time.time()
                elif time.time() - still > 30:
                    break
                time.sleep(0.1)
            time.sleep(3.0)  # background work after the last delivery (history rows, queue acknowledgements) is part of the cost
            c1 = g.cpu()
            wall = time.time() - t0
            res.update(analyse(of, dump(ctl), N, E))
            sink_util = (proc_cpu(sink.pid) - sink_c0) / wall
            res.update({"events": N, "rate": rate, "load_line": out, "load_util": load_util, "sink_util": sink_util,
                        "valid": load_util < 0.85 and sink_util < 0.85 and res["span_s"] is not None})
            if c0:
                cpu = {r: c1[r] - c0[r] for r in c0}
                res.update({"cpu_s": cpu, "cpu_ms_per_delivery": {r: v / (N * E) * 1000 for r, v in cpu.items()}, "cpu_ms_per_delivery_total": sum(cpu.values()) / (N * E) * 1000})
            if tgt["stats"]:
                try:
                    res["service_stats"] = http("GET", tgt["stats"])[1]
                except Exception as e:  # noqa: BLE001
                    res["service_stats"] = str(e)
            line = out.split(", ")
            res["ingest_per_s"] = float(line[3].split()[0])
            res["ack_p50_ms"] = float(line[4].split()[2])
            res["ack_p99_ms"] = float(line[5].split()[2])
        g.stop()
        res.update({"peak_mem_mib": {r: v / 2**20 for r, v in g.peak.items()}, "peak_mem_total_mib": g.peak_sum / 2**20, "cgroups": g.ok, "loadavg": os.getloadavg()})
        res["system_describe"] = s.describe()
        return res
    finally:
        if g:
            g.stop()
        s.stop()
        sink.terminate()
        sink.wait()


# ---- the machine, the report ----------------------------------------------------------------------------------------------------------------------------------

def parse_cpus(spec):
    out = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out |= set(range(int(lo), int(hi or lo) + 1))
    return out


def governors(cpus):
    g = {}
    for c in cpus:
        try:
            g[c] = open(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor").read().strip()
        except OSError:
            g[c] = "unknown"
    return g


def machine(a):
    model = ""
    for line in open("/proc/cpuinfo"):
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    sha = os.environ.get("HARNESS_COMMIT") or sh("git", "-C", REPO, "rev-parse", "HEAD", check=False) or "unknown (not a git checkout: set HARNESS_COMMIT)"
    return {"cpu": model, "cores": os.cpu_count(), "kernel": platform.release(), "docker": sh("docker", "version", "-f", "{{.Server.Version}}", check=False), "loadavg_start": os.getloadavg(),
            "governors": governors(parse_cpus(a.sut_cpus) | {a.sink_cpu, a.load_cpu}), "harness_commit": sha, "date": time.strftime("%Y-%m-%d %H:%M:%S %z")}


def med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def cell(runs, key, nd=0, sub=None):
    v = sorted(x for x in ((r[key][sub] if sub and r.get(key) else r.get(key)) for r in runs) if x is not None)
    return "-" if not v else f"{statistics.median(v):.{nd}f} ({v[0]:.{nd}f} to {v[-1]:.{nd}f})"


def markdown(o):
    m = o["machine"]
    L = ["# Comparison of webhook services", "",
         f"Machine: {m['cpu']}, {m['cores']} cores, kernel {m['kernel']}, docker {m['docker']}; governors {sorted(set(m['governors'].values()))}; load average at the start {m['loadavg_start']}, at the end {o['loadavg_end']}. "
         f"System under test on cores {o['sut_cpus']}, receivers on {o['sink_cpu']}, load on {o['load_cpu']}. {o['reps']} runs of each row: the median, then (least to most). Harness commit {m['harness_commit'][:12] if len(m['harness_commit']) == 40 else m['harness_commit']}, {m['date']}.", ""]
    for s, d in o["systems"].items():
        L.append(f"* **{s}**: {d['notes']} Images: " + "; ".join(f"{r} `{i}`" for r, i in d["images"].items()))
    L += ["", "## Throughput (every post as fast as the service answers; 64 connections)", "",
          "| system | endpoints | events | posts/s acknowledged | ack p50 / p99 (ms) | deliveries/s to the receivers | CPU per delivery, whole system (ms) | of which service | peak memory, whole system (MiB) | missing | duplicates | valid |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, runs in o["throughput"].items():
        s, E = key
        L.append(f"| {s} | {E} | {runs[0]['events']} | {cell(runs, 'ingest_per_s')} | {cell(runs, 'ack_p50_ms', 2)} / {cell(runs, 'ack_p99_ms', 2)} | {cell(runs, 'deliveries_per_s')} | "
                 f"{cell(runs, 'cpu_ms_per_delivery_total', 3)} | {cell(runs, 'cpu_ms_per_delivery', 3, 'service')} | {cell(runs, 'peak_mem_total_mib', 0)} | "
                 f"{cell(runs, 'missing')} | {cell(runs, 'duplicates')} | {'yes' if all(r['valid'] for r in runs) else 'INVALID'} |")
    L += ["", "## Latency (open loop at a fixed rate; the time from the post to the first arrival at the receiver)", "",
          "| system | endpoints | rate (events/s) | e2e p50 (ms) | e2e p99 (ms) | e2e max (ms) | ack p99 (ms) | missing | duplicates | valid |", "|---|---|---|---|---|---|---|---|---|---|"]
    for key, runs in o["latency"].items():
        s, E, rate = key
        L.append(f"| {s} | {E} | {rate} | {cell(runs, 'e2e_p50_ms', 2)} | {cell(runs, 'e2e_p99_ms', 2)} | {cell(runs, 'e2e_max_ms', 1)} | {cell(runs, 'ack_p99_ms', 2)} | "
                 f"{cell(runs, 'missing')} | {cell(runs, 'duplicates')} | {'yes' if all(r['valid'] for r in runs) else 'INVALID'} |")
    L += ["", f"## Idle ({o['idle_s']} s, endpoints configured, nothing sent)", "", "| system | endpoints | CPU of the whole system (% of one core) | memory, whole system (MiB) |", "|---|---|---|---|"]
    for key, runs in o["idle"].items():
        s, E = key
        tot = [sum(r["idle_cpu_core_pct"].values()) for r in runs if r.get("idle_cpu_core_pct")]
        L.append(f"| {s} | {E} | {f'{statistics.median(tot):.1f}' if tot else '-'} | {cell(runs, 'peak_mem_total_mib', 0)} |")
    return "\n".join(L) + "\n"


def cleanup():
    for c in CLEANUP:
        if isinstance(c, tuple):
            try:
                os.kill(c[1], 15)     # only the receivers this script started
            except OSError:
                pass
        else:
            sh("docker", "rm", "-f", "-v", c, check=False)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="bench-out")
    p.add_argument("--systems", default="hooks,hooks-pg,svix")
    p.add_argument("--endpoints", type=lambda v: [int(x) for x in v.split(",")], default=[1, 10])
    p.add_argument("--events", type=int, default=20000, help="events in a throughput run (the deliveries are events x endpoints)")
    p.add_argument("--rates", type=lambda v: [int(x) for x in v.split(",")], default=[100, 500], help="events a second for the latency runs")
    p.add_argument("--latency-s", type=int, default=30)
    p.add_argument("--idle-s", type=int, default=30)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--sut-cpus", default="2-5")
    p.add_argument("--sink-cpu", type=int, default=0)
    p.add_argument("--load-cpu", type=int, default=1)
    p.add_argument("--hooks-image", default="ghcr.io/alpibrusl/cancho-hooks:0.1.0-alpha.1")
    p.add_argument("--svix-image", default="svix/svix-server:latest", help="pin a tag for a published result; the digest is recorded either way")
    p.add_argument("--pg-image", default="postgres:16")
    p.add_argument("--redis-image", default="redis:7-alpine")
    p.add_argument("--allow-governor", action="store_true", help="run although a core is not on the performance governor (the result says so)")
    p.add_argument("--smoke", action="store_true", help="a few hundred events, one run, to check that every system is set up right")
    a = p.parse_args()
    if a.smoke:
        a.events, a.reps, a.rates, a.latency_s, a.idle_s = 300, 1, [20], 5, 3
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    atexit.register(cleanup)
    for t in ("sink", "load"):
        sh("gcc", "-O2", "-o", os.path.join(a.out, t), os.path.join(HERE, t + ".c"))
    mach = machine(a)
    bad = {c: g for c, g in mach["governors"].items() if g != "performance"}
    if bad and not a.allow_governor and not a.smoke:
        sys.exit(f"refusing to run: cores {bad} are not on the performance governor (sudo cpupower frequency-set -g performance, and set it back afterwards); --allow-governor to override")
    systems = a.systems.split(",")
    o = {"machine": mach, "sut_cpus": a.sut_cpus, "sink_cpu": a.sink_cpu, "load_cpu": a.load_cpu, "reps": a.reps, "idle_s": a.idle_s, "throughput": {}, "latency": {}, "idle": {}, "systems": {}}
    plan = []
    for E in a.endpoints:
        plan.append(("throughput", E, 0, a.events if E == 1 else max(1000, a.events // E)))
        for r in a.rates:
            plan.append(("latency", E, r, r * a.latency_s))
        plan.append(("idle", E, 0, 0))
    for mode, E, rate, N in plan:
        for rep in range(a.reps):
            order = systems[rep % len(systems):] + systems[:rep % len(systems)]      # the order rotates, so that none is always first or last
            for sname in order:
                print(f"[{mode} E={E} rate={rate or 'max'} rep {rep + 1}/{a.reps}] {sname} ...", flush=True)
                res = one(a, sname, E, mode, N, rate, rep)
                key = (sname, E) if mode != "latency" else (sname, E, rate)
                o[{"throughput": "throughput", "latency": "latency", "idle": "idle"}[mode]].setdefault(key, []).append(res)
                o["systems"].setdefault(sname, res["system_describe"])
                print("   ", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in res.items() if k in ("ingest_per_s", "deliveries_per_s", "e2e_p50_ms", "e2e_p99_ms", "missing", "duplicates", "valid", "cpu_ms_per_delivery_total")}, flush=True)
    o["loadavg_end"] = os.getloadavg()
    ser = {k: {" ".join(map(str, kk)): v for kk, v in o[k].items()} if k in ("throughput", "latency", "idle") else o[k] for k in o}
    json.dump(ser, open(os.path.join(a.out, "compare.json"), "w"), indent=1)
    open(os.path.join(a.out, "compare.md"), "w").write(markdown(o))
    print(f"wrote {a.out}/compare.md and compare.json")


if __name__ == "__main__":
    main()
