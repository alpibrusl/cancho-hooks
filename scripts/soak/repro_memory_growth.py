#!/usr/bin/env python3
"""A reproducer of the third thing the soak found (docs/soak.md section 8): **resident memory grows with every call that goes to the database for a change**: a cron fire, a `PATCH` of an
endpoint, the creation and deletion of one, a bulk replay of dead letters.

    HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 scripts/soak/repro_memory_growth.py build/hooks      # the database is truncated (endpoints, attempts, schedules)
                                                                                                         # exit 0: nothing above 2 KiB a call; exit 1: something leaks

The service is started with a database and an admin token, three endpoints are made, 500 events are posted, and each kind of call is made hundreds of times while the resident size
is read before and after. The reads that do not go to the database (`/healthz`, `/stats`, `/endpoints`, `/metrics`, a replay, an enable) and the delivery of 360,000 events to three endpoints
with a history row for each delivery (260,000 rows) leave the resident size where it was (measured separately: 360,000 events to three endpoints with 260,000 history rows kept the resident size at 10.1 MB).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import http.client

HERE = os.path.dirname(os.path.abspath(__file__))
binary = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(HERE)), "build", "hooks")
sys.argv = sys.argv[:1]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests"))
import opslib as L  # noqa: E402

L.psql("truncate endpoints, attempts, schedules")
d = tempfile.mkdtemp(prefix="repro-memory-")
peer = L.Peer("ok")
port = L.chaos.free_port()
proc = subprocess.Popen([binary, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--admin-token", "adminadmin", "--cron-seconds", "1", *L.pg_flags()],
                        stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def rss():
    for line in open(f"/proc/{proc.pid}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1])


def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer adminadmin"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


L.wait_for(lambda: call("GET", "/readyz")[0] == 200, 30)
ids = [json.loads(call("POST", "/endpoints", {"host": "127.0.0.1", "port": peer.port})[1])["id"] for _ in range(3)]
for n in range(500):
    call("POST", "/events", {"type": "t", "n": n})
time.sleep(2)
worst = 0.0


def phase(name, f, n):
    global worst
    a = rss()
    for i in range(n):
        f(i)
    time.sleep(0.5)
    b = rss()
    per = (b - a) * 1024 / n
    worst = max(worst, per)
    print(f"{name:40} x{n:4}: {a:6} -> {b:6} KiB, {per:7.0f} bytes a call", flush=True)


phase("GET /endpoints, /stats, /metrics", lambda i: [call("GET", p) for p in ("/endpoints", "/stats", "/metrics")], 500)
phase("replay of an event", lambda i: call("POST", f"/events/{1 + i % 400}/replay"), 500)
phase("enable", lambda i: call("POST", f"/endpoints/{ids[0]}/enable"), 500)
phase("PATCH rate and concurrency", lambda i: call("PATCH", f"/endpoints/{ids[1]}", {"rate": 100 + i % 5, "concurrency": 2 + i % 5}), 300)
phase("PATCH, a new secret kept beside the old", lambda i: call("PATCH", f"/endpoints/{ids[1]}", {"rotate": True, "keep_old_ms": 5000}), 300)
phase("POST then DELETE an endpoint", lambda i: call("DELETE", f"/endpoints/{json.loads(call('POST', '/endpoints', {'host': '127.0.0.1', 'port': peer.port})[1])['id']}"), 100)
phase("POST /endpoints/:id/replay-dead", lambda i: call("POST", f"/endpoints/{ids[0]}/replay-dead", {}), 200)
s, _ = call("POST", "/schedules", {"expr": "* * * * * *", "type": "cron.tick"})
a, t0 = rss(), time.time()
time.sleep(40)
fires = json.loads(call("GET", "/stats")[1])["cron_fired"]
b = rss()
per = (b - a) * 1024 / max(1, fires)
worst = max(worst, per)
print(f"{'a schedule of every second (a fire)':40} x{fires:4}: {a:6} -> {b:6} KiB, {per:7.0f} bytes a fire", flush=True)
proc.terminate()
proc.wait()
print(f"the most a call kept: {worst:.0f} bytes")
sys.exit(1 if worst > 2048 else 0)
