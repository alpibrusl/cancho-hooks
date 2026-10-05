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
    worst, end = 0.0, time.time() + secs
    while time.time() < end:
        worst = max(worst, healthz_ms(svc))
        time.sleep(0.02)
    return worst


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
    worst = watch_health(svc, 1.8)
    ready = L.wait_for(lambda: svc.has_line("endpoints loaded"), 20)
    check(f"2. a name server that takes 2 s: /healthz answered in at most {worst:.0f} ms meanwhile (under 100), and then the database is there", worst < 100 and ready, f"{worst} {ready}")
    svc.kill()
    dns.close()

    # 3.
    dns = K.DnsStub({"other.test": ["127.0.0.1"]})
    svc = L.Service(BIN, tempfile.mkdtemp(prefix="hooks-dbname-"), flags(dns.port))
    svc.start(timeout=30, loaded=False)
    worst = watch_health(svc, 3.0)
    code, body = svc.get("/readyz")
    st = svc.stats()
    check(f"3. a name the server does not know: /readyz is 503 naming the database, {st.get('database_lookup_failures')} lookups failed, /healthz at most {worst:.0f} ms",
          code == 503 and b"database" in body and st.get("database_lookup_failures", 0) >= 2 and worst < 100, f"{code} {body} {st.get('database_lookup_failures')} {worst}")
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
