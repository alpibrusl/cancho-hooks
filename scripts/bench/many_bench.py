#!/usr/bin/env python3
"""What 1,024 endpoints cost (docs/design.md section 41.9), measured. A report, not a gate.

    gcc -O2 -o scripts/bench/mklog scripts/bench/mklog.c
    HOOKS_BIN=build/hooks HOOKS_PG=host:port:user:database python3 scripts/bench/many_bench.py idle|worst|busy|api [N]

  idle    the resident and virtual size, the time to `listening` and the CPU at rest, for 1, 62 and N endpoints of `endpoints.conf` (no database)
  worst   the start with N endpoints and N x 2,048 dead letters in delivery.seg (2,097,152 records at N = 1,024): the time to `listening`, the size
  busy    N endpoints that nothing listens for, N events: every window full of failed attempts (1,048,576 of them at 1,024); the resident size, the longest
          answer of /healthz while it ran (what a client sees of the loop's longest turn), the longest step of retention (`maintenance_ms_max`)
  api     N endpoints from the table: the time of GET /metrics (every page), GET /endpoints (every page), a DELETE of the first, and the start (to `endpoints loaded`)
The machine is part of the result.
"""
import base64, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, socket
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
BIN = os.environ.get("HOOKS_BIN") or os.path.join(ROOT, "build", "hooks")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def status_kb(pid, key):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return 0


def cpu(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def conf(d, n, port=9):
    sec = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(n):
            f.write(f"{i} 127.0.0.1 {port} {sec}\n")


def start(d, extra=(), wait="listening"):
    port = free_port()
    t0 = time.time()
    p = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", *extra], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = p.stderr.readline().decode().strip()
        lines.append(line)
        if wait in line or line == "":
            break
    took = time.time() - t0
    threading.Thread(target=lambda: [None for _ in p.stderr], daemon=True).start()
    return p, port, took, lines


def get(port, path, timeout=60):
    t = time.time()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        data = r.read()
    return data, (time.time() - t) * 1000


def idle(n_max):
    for n in sorted({1, 62, n_max}):
        d = tempfile.mkdtemp(prefix="bench-idle-")
        conf(d, n)
        p, port, took, _ = start(d)
        time.sleep(1.0)
        c0 = cpu(p.pid)
        time.sleep(10)
        c1 = cpu(p.pid)
        print(f"idle {n:5d} endpoints: start {took * 1000:6.0f} ms, VmRSS {status_kb(p.pid, 'VmRSS'):7d} kB, RssAnon {status_kb(p.pid, 'RssAnon'):7d} kB, VmSize {status_kb(p.pid, 'VmSize'):8d} kB, CPU at rest {(c1 - c0) / 10 * 100:.2f} % of a core", flush=True)
        p.kill(); p.wait(); shutil.rmtree(d, ignore_errors=True)


def worst(n):
    d = tempfile.mkdtemp(prefix="bench-worst-")
    p, port, _, _ = start(d)
    body = b'{"type":"t","n":1}'
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port)
    for i in range(2050):
        c.request("POST", "/events", body=body, headers={"Content-Type": "application/json"}); c.getresponse().read()
    p.terminate(); p.wait()
    t = time.time()
    subprocess.run([os.path.join(HERE, "mklog"), os.path.join(d, "delivery.seg"), str(n), "2048"], check=True)
    size = os.path.getsize(os.path.join(d, "delivery.seg"))
    print(f"worst: delivery.seg of {n} slots x 2,048 dead letters: {size / 1e6:.0f} MB, made in {time.time() - t:.1f} s", flush=True)
    conf(d, n)
    for k in range(3):
        p, port, took, lines = start(d)
        print(f"worst start {k + 1}: {took:.2f} s to listening, VmRSS {status_kb(p.pid, 'VmRSS') / 1024:.0f} MB, VmHWM {status_kb(p.pid, 'VmHWM') / 1024:.0f} MB; {lines[-1]!r}", flush=True)
        dead = json.loads(get(port, "/endpoints/1000/dead?limit=5")[0]) if n > 1000 else None
        if dead:
            print("     endpoint 1000 holds", dead["held"], "dead letters", flush=True)
        p.kill(); p.wait()
    shutil.rmtree(d, ignore_errors=True)


def busy(n):
    d = tempfile.mkdtemp(prefix="bench-busy-")
    conf(d, n, port=free_port())          # nothing listens there
    p, port, took, _ = start(d, ["--schedule", "3600000,3600000", "--deadline-ms", "1000", "--delivery-log-bytes", "33554432"])
    worst, stop = [0.0], threading.Event()

    def probe():
        while not stop.is_set():
            t = time.time()
            try:
                get(port, "/healthz", 30)
            except Exception:
                pass
            worst[0] = max(worst[0], (time.time() - t) * 1000)
            time.sleep(0.002)
    threading.Thread(target=probe, daemon=True).start()
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port)
    t0 = time.time()
    for i in range(n):
        c.request("POST", "/events", body=b'{"type":"t","n":%d}' % i, headers={"Content-Type": "application/json"}); c.getresponse().read()
    want = n * n
    while time.time() - t0 < 900:
        s = json.loads(get(port, "/stats")[0])
        if s["failed"] + s["dead"] >= want:
            break
        time.sleep(2)
    time.sleep(3)
    s = json.loads(get(port, "/stats")[0])
    stop.set()
    print(f"busy {n} endpoints x {n} events: {s['failed']} failed attempts in {time.time() - t0:.0f} s, VmRSS {status_kb(p.pid, 'VmRSS') / 1024:.0f} MB, VmHWM {status_kb(p.pid, 'VmHWM') / 1024:.0f} MB; "
          f"longest /healthz {worst[0]:.0f} ms; snapshots {s['snapshots']}, longest step {s['maintenance_ms_max']} ms; delivery.seg {os.path.getsize(os.path.join(d, 'delivery.seg')) / 1e6:.0f} MB; turns {s['turns']}, looks {s['endpoints_looked_at']}", flush=True)
    time.sleep(3)
    a, b = json.loads(get(port, "/stats")[0]), None
    time.sleep(2)
    b = json.loads(get(port, "/stats")[0])
    print(f"     at rest with every window full: {(b['endpoints_looked_at'] - a['endpoints_looked_at']) / max(1, b['turns'] - a['turns']):.0f} endpoints looked at a turn", flush=True)
    c0 = cpu(p.pid); time.sleep(10); c1 = cpu(p.pid)
    print(f"     CPU at rest with every window full: {(c1 - c0) / 10 * 100:.1f} % of a core", flush=True)
    p.kill(); p.wait(); shutil.rmtree(d, ignore_errors=True)


def api(n):
    import manykit as mk, opslib
    mk.reset_db()
    mk.insert_endpoints(n, 9)
    d = tempfile.mkdtemp(prefix="bench-api-")
    p, port, took, _ = start(d, ["--admin-token", "correct-horse-battery-staple", *opslib.pg_flags()], wait="endpoints loaded")
    print(f"api: {n} endpoints read from the table: {took * 1000:.0f} ms from exec to `endpoints loaded`, VmRSS {status_kb(p.pid, 'VmRSS') / 1024:.0f} MB", flush=True)
    ms, sizes, k = [], [], 0
    while True:
        try:
            data, t = get(port, f"/metrics?page={k}")
        except Exception:
            break
        ms.append(t); sizes.append(len(data)); k += 1
    print(f"     GET /metrics: {k} pages, each {min(ms):.1f} to {max(ms):.1f} ms, the largest {max(sizes)} bytes", flush=True)
    ms, off, sizes = [], 0, []
    while True:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/endpoints?offset={off}")
        t = time.time()
        with urllib.request.urlopen(req) as r:
            data = r.read(); nxt = r.headers.get("X-Next-Offset")
        ms.append((time.time() - t) * 1000); sizes.append(len(data))
        if nxt is None:
            break
        off = int(nxt)
    print(f"     GET /endpoints: {len(ms)} pages, each {min(ms):.1f} to {max(ms):.1f} ms, the largest {max(sizes)} bytes", flush=True)
    first = sorted(int(r[0]) for r in opslib.psql("select id from endpoints"))[0]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/endpoints/{first}", method="DELETE", headers={"Authorization": "Bearer correct-horse-battery-staple"})
    t = time.time()
    with urllib.request.urlopen(req) as r:
        r.read()
    print(f"     DELETE of the first endpoint (row 0 of {n}): {(time.time() - t) * 1000:.1f} ms (the database's answer included)", flush=True)
    p.kill(); p.wait(); shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "idle"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
    {"idle": idle, "worst": worst, "busy": busy, "api": api}[what](n)
