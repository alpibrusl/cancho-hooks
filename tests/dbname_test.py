#!/usr/bin/env python3
"""The database's host may be a name, and resolving it never holds the loop (docs/design.md section 45).

    HOOKS_PG=host:port:user:database python3 tests/dbname_test.py build/hooks        (host must be 127.0.0.1: the name server of the test answers that)

A name server of the test's own (`tlskit.DnsStub`) answers `db.test` with 127.0.0.1, and the service is given `--pg-host db.test` and that name server.

  1. the endpoints are read through the name and an event is delivered and its row written; `/stats` counts the lookup; the pool was never given the name
  2. a name server that takes 2 s to answer: `/healthz` answers in under 100 ms all the while (the loop is not held), and then the database is there
  3. a name the server does not know: `/readyz` is 503 (`database`), lookups are tried again with a growing wait, `/healthz` stays quick; when the name is
     added the service connects with no restart
  4. the connections are ended under the service (`pg_terminate_backend`): the name is looked up again before they are made again
"""
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402
from loadmeter import LoadMeter  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


def flags(dns_port):
    host, port, user, db = os.environ["HOOKS_PG"].split(":")
    out = ["--pg-host", "db.test", "--pg-port", port, "--pg-user", user, "--pg-database", db, "--dns-server", f"127.0.0.1:{dns_port}"]
    if os.environ.get("HOOKS_PG_PASSWORD"):
        out += ["--pg-password", os.environ["HOOKS_PG_PASSWORD"]]
    return out


def healthz_ms(svc):
    t = time.time()
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/healthz", timeout=5) as r:
        r.read()
    return (time.time() - t) * 1000


def watch_health(svc, secs):
    """The waits for /healthz, in milliseconds, for `secs` seconds of one request after another (a probe at a time, 20 ms apart)."""
    waits, end = [], time.time() + secs
    while time.time() < end:
        waits.append(healthz_ms(svc))
        time.sleep(0.02)
    return sorted(waits)


# What would show a loop held by a lookup is a request that waits for the lookup: for the 2 s that a slow name server takes, one probe waits most of it (the next ones are not made until it
# answers), and at the end of the wait the loop is free. A single slow probe among eighty is a machine that took its processor away from the service, the client or the kernel for
# a moment (105 ms with the processor shared with six busy loops, on a service whose probes took 1 to 5 ms), so the probes are judged by what nearly all of them saw (the 95th
# percentile: at most 100 ms) and by a cap on the worst that is half of the lookup's time, which a held loop exceeds by seconds.
LOOKUP_S = 2.0
P95_MS = 100        # for a machine that gives the test a processor of its own; times what this one gave (`LoadMeter.slowdown`), for the probes of a client that is waiting for its turn
CAP_MS = LOOKUP_S * 1000 / 2
METER = LoadMeter()


def p95_limit():
    return P95_MS * max(1.0, METER.slowdown())


def p95(waits):
    return waits[min(len(waits) - 1, int(len(waits) * 0.95))]


def main():
    L.apply_schema()
    L.psql("truncate endpoints, attempts")
    peer = L.Peer("ok")
    L.psql(f"insert into endpoints (id, host, port, secret) values (1, '127.0.0.1', {peer.port}, '{L.secret()}')")

    # 1.
    dns = K.DnsStub({"db.test": ["127.0.0.1"]})
    svc = L.Service(BIN, tempfile.mkdtemp(prefix="hooks-dbname-"), flags(dns.port))
    ok = svc.start(timeout=30)
    check("1. the service reads its endpoints through the name db.test", ok and svc.has_line("endpoints loaded: 1"), svc.stderr()[-400:])
    s, _ = svc.post_event(1)
    got = L.wait_for(lambda: svc.stats()["delivered"] >= 1 and L.psql("select count(*) from attempts")[0][0] == "1", 20)
    st = svc.stats()
    check(f"1. an event is delivered and its row written; /stats counts {st.get('database_lookups')} lookup(s) and no failure", s == 202 and got
          and st.get("database_lookups", 0) >= 1 and st.get("database_lookup_failures") == 0, str(st))
    check("1. the name server was asked for db.test", "db.test" in [n.lower().rstrip(".") for n in dns.asked], str(dns.asked))
    svc.kill()
    dns.close()

    # 2.
    dns = K.DnsStub({"db.test": ["127.0.0.1"]}, delay=2.0)
    svc = L.Service(BIN, tempfile.mkdtemp(prefix="hooks-dbname-"), flags(dns.port))
    svc.start(timeout=30, loaded=False)
    waits = watch_health(svc, 1.8)
    ready = L.wait_for(lambda: svc.has_line("endpoints loaded"), 20)
    check(f"2. a name server that takes 2 s: /healthz answered meanwhile in at most {p95(waits):.0f} ms for 95 of 100 (under {p95_limit():.0f}), the longest {waits[-1]:.0f} ms (under {CAP_MS:.0f}), and then the database is there",
          p95(waits) <= p95_limit() and waits[-1] < CAP_MS and ready, f"{len(waits)} probes, p95 {p95(waits)}, max {waits[-1]}, {ready}, limit {p95_limit():.0f}")
    svc.kill()
    dns.close()

    # 3.
    dns = K.DnsStub({"other.test": ["127.0.0.1"]})
    svc = L.Service(BIN, tempfile.mkdtemp(prefix="hooks-dbname-"), flags(dns.port))
    svc.start(timeout=30, loaded=False)
    waits = watch_health(svc, 3.0)
    # the lookups are tried again after a wait that grows; two of them have failed by the time the third second is over on a machine that is not stopped now and then (a stall of
    # half a second in the first one moved the second past it), so it is waited for
    L.wait_for(lambda: svc.stats().get("database_lookup_failures", 0) >= 2, 20)
    code, body = svc.get("/readyz")
    st = svc.stats()
    check(f"3. a name the server does not know: /readyz is 503 naming the database, {st.get('database_lookup_failures')} lookups failed, /healthz at most {p95(waits):.0f} ms for 95 of 100, the longest {waits[-1]:.0f} ms",
          code == 503 and b"database" in body and st.get("database_lookup_failures", 0) >= 2 and p95(waits) <= p95_limit() and waits[-1] < CAP_MS, f"{code} {body} {st.get('database_lookup_failures')} p95 {p95(waits)} max {waits[-1]}")
    tried = st.get("database_lookups", 0)
    time.sleep(3)
    later = svc.stats().get("database_lookups", 0)
    check(f"3. ... the lookups are tried again with a growing wait, not every turn ({tried} then {later} three seconds later)", later - tried <= 6, f"{tried} {later}")
    dns.names["db.test"] = ["127.0.0.1"]
    back = L.wait_for(lambda: svc.get("/readyz")[0] == 200, 60)
    check("3. when the name is added the service connects, with no restart", back, str(svc.get("/readyz")))

    # 4.
    before = svc.stats().get("database_lookups", 0)
    L.psql("select pg_terminate_backend(pid) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()")
    again = L.wait_for(lambda: svc.stats().get("database_lookups", 0) > before and svc.get("/readyz")[0] == 200, 60)
    check(f"4. the connections ended under it: the name is looked up again ({before} then {svc.stats().get('database_lookups')}) and the database is back", again, str(svc.stats()))
    svc.kill()
    dns.close()
    peer.close()
    return check.finish("dbname")


if __name__ == "__main__":
    sys.exit(main())
