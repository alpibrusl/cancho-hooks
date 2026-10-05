#!/usr/bin/env python3
"""No memory is kept by an administrative call or a schedule's fire (docs/design.md section 38.5; docs/soak.md, finding 3).

    HOOKS_PG=host:port:user:database python3 tests/leak_test.py build/hooks        (the database's endpoints, attempts and schedules are truncated)

A region that is left by a `return` is not given back (lex-sys #252): at least one 64 KiB chunk a call, of which at least a page becomes resident. The soak
found it on every call that changes something in the database: a `PATCH` kept 134 to 203 KB, a create and a delete 27 KB, a bulk replay of dead letters 9 KB,
a fire 18 KB. Each kind of call is made here in two rounds; the first warms what is resident only as it is used (the windows, the hash slots of the two
idempotency indexes, which are made small with `--idem-keys`, so that they warm in seconds), and the second may keep at most 1 KiB a call. A page a call is
four times that.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
TOKEN = "leakleakleak"
BOUND = 1024
check = L.Checks()


def main():
    L.apply_schema()
    L.psql("truncate endpoints, attempts, schedules")
    d = tempfile.mkdtemp(prefix="hooks-leak-")
    peer = L.Peer("ok")
    port = L.chaos.free_port()
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--admin-token", TOKEN, "--cron-seconds", "1",
                             "--idem-keys", "4096", "--schedule", "100,200", *L.pg_flags()], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def rss():
        for line in open(f"/proc/{proc.pid}/status"):
            if line.startswith("VmRSS"):
                return int(line.split()[1])
        return 0

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        h = {"Authorization": f"Bearer {TOKEN}", **(headers or {})}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def stats():
        return json.loads(call("GET", "/stats")[1])

    try:
        assert L.wait_for(lambda: call("GET", "/readyz")[0] == 200, 60), "the service did not become ready"
        ids = [json.loads(call("POST", "/endpoints", {"host": "127.0.0.1", "port": peer.port})[1])["id"] for _ in range(3)]
        dead = json.loads(call("POST", "/endpoints", {"host": "127.0.0.1", "port": L.closed_port()})[1])["id"]
        # every window warmed (more than its 1,024 cells), and dead letters at the endpoint nobody listens on
        for n in range(1300):
            call("POST", "/events", {"type": "t", "n": n})
        assert L.wait_for(lambda: stats()["delivered"] >= 3 * 1300 and stats()["dead"] >= 1000, 300), f"the warm-up did not finish: {stats()}"
        serial = [0]

        def keyed(i):
            serial[0] += 1
            return call("POST", "/events", {"type": "t", "k": serial[0]}, {"Idempotency-Key": f"leak-{serial[0]}"})

        def create_delete(i):
            s, body = call("POST", "/endpoints", {"host": "127.0.0.1", "port": peer.port})
            return call("DELETE", f"/endpoints/{json.loads(body)['id']}") if s == 201 else (s, body)

        def schedule(i):
            s, body = call("POST", "/schedules", {"expr": "0 0 0 1 1 *", "type": "leak.never"})
            return call("DELETE", f"/schedules/{json.loads(body)['id']}") if s == 201 else (s, body)

        kinds = [
            ("PATCH of the rate and the concurrency", lambda i: call("PATCH", f"/endpoints/{ids[1]}", {"rate": 100 + i % 5, "concurrency": 2 + i % 5}), 300),
            ("PATCH of the headers and the types", lambda i: call("PATCH", f"/endpoints/{ids[1]}", {"headers": {"X-Leak": f"v{i % 7}"}, "types": ["t", f"x.{i % 3}"]}), 300),
            ("PATCH, a new secret kept beside the old", lambda i: call("PATCH", f"/endpoints/{ids[2]}", {"rotate": True, "keep_old_ms": 5000}), 300),
            ("POST of an endpoint (its secret made by the service) then DELETE", create_delete, 150),
            ("POST /endpoints/:id/replay-dead", lambda i: call("POST", f"/endpoints/{dead}/replay-dead", {}), 300),
            ("POST of a schedule then DELETE", schedule, 150),
            ("an event with an Idempotency-Key", keyed, 600),
        ]
        for name, f, n in kinds:
            kept, bad = [], []
            for _ in range(2):
                m0 = K.mappings_kb(proc.pid)
                a = rss()
                for i in range(n):
                    st_, body_ = f(i)
                    if not 200 <= st_ < 300:
                        bad.append((st_, body_[:120]))
                time.sleep(0.3)
                kept.append((rss() - a) * 1024 / n)
                where = K.grown(m0, K.mappings_kb(proc.pid))
            check(f"{name}: every call answered 2xx", not bad, f"{len(bad)} not: {bad[:2]}")
            check(f"{name}: the second round of {n} keeps {kept[1]:.0f} bytes a call (the first {kept[0]:.0f}; at most {BOUND})", kept[1] <= BOUND, f"{kept}; the second round grew in {where}")

        # a schedule of every second: its fires, after the first minute
        s, body = call("POST", "/schedules", {"expr": "* * * * * *", "type": "leak.tick"})
        check("a schedule of every second is made", s == 201, body.decode(errors="replace"))
        assert L.wait_for(lambda: stats()["cron_fired"] >= 60, 120), f"the schedule did not fire: {stats()}"
        a, f0, m0 = rss(), stats()["cron_fired"], K.mappings_kb(proc.pid)
        assert L.wait_for(lambda: stats()["cron_fired"] >= f0 + 120, 240), f"the schedule stopped firing: {stats()}"
        time.sleep(0.3)
        st = stats()
        per = (rss() - a) * 1024 / (st["cron_fired"] - f0)
        check(f"a fire keeps {per:.0f} bytes (after the first 60; {st['cron_fired'] - f0} fires; at most {BOUND})", per <= BOUND and st["cron_errors"] == 0,
              f"grew in {K.grown(m0, K.mappings_kb(proc.pid), top=8)}; {st}")
    finally:
        proc.terminate()
        proc.wait()
        peer.close()
    return check.finish("leak")


if __name__ == "__main__":
    sys.exit(main())
