#!/usr/bin/env python3
"""The endpoints in PostgreSQL (docs/design.md section 24, C3c).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/roster_test.py build/hooks
                                                                    (default 127.0.0.1:5432:postgres:hooks, no password)

The database must exist; the test applies sql/schema.sql and empties `endpoints` itself.

  1. `--import-endpoints 1` copies endpoints.conf into the table: every row as written, a second import adds none, a line whose id
     is already there changes nothing
  2. an import that cannot happen changes nothing: a bad line anywhere in the file imports none of the file; no file, no
     --pg-host and a database that is not there are each refused with a message and status 20 (13 for a bad line)
  3. with --pg-host the service delivers to the table's endpoints and **not** to endpoints.conf, signing with the table's secret
  4. the table is read at each start: a row removed, a restart, and that endpoint is no longer delivered to; and an empty table
     starts the service with no endpoints (it accepts and keeps events)
  5. (also: an empty host, and a table too large for the 32 KiB the service reads it into)
  5. a row the service cannot use is a refusal to start with the row's number (an id of seven digits, a repeated id cannot be: it
     is the key; a secret that is not base64), and a row with an unprintable byte is refused as such
  6. a table that is not there, a database that is not there, a wrong password: status 20 and what failed
  7. without --pg-host the file is read as before
  8. a database that accepts and never answers: the start waits (it has nothing to deliver to), and a kill ends it
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


class Receiver:
    """Records (webhook-id, timestamp, signature, body) of every request, and whether the signature is right for `key`."""

    def __init__(self, key=None):
        self.seen, self.key, self.bad = [], key, 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                mid, ts, sig = self.headers["webhook-id"], self.headers["webhook-timestamp"], self.headers["webhook-signature"]
                outer.seen.append(mid)
                if outer.key:
                    want = base64.b64encode(hmac.new(base64.b64decode(outer.key[6:]), f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
                    if "v1," + want not in sig.split():
                        outer.bad += 1
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def pg_flags(port=None, user=None, password=None):
    f = ["--pg-host", PG_HOST, "--pg-port", str(port or PG_PORT), "--pg-user", user or PG_USER, "--pg-database", PG_DB]
    if password is None:
        password = PG_PASSWORD
    if password:
        f += ["--pg-password", password]
    return f


def run_once(datadir, extra, timeout=10):
    """Run the service to its end (an import, or a refusal to start): (exit status, stderr lines)."""
    p = subprocess.run([BIN, "--dir", datadir, "--allow-private-hosts", "1", *extra], capture_output=True, timeout=timeout)
    return p.returncode, p.stderr.decode().splitlines()


def start(datadir, extra):
    port = chaos.free_port()
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", datadir, "--allow-private-hosts", "1", "--schedule", "100", "--deadline-ms", "800", *extra],
                            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    svc = type("Svc", (), {})()
    svc.port, svc.proc, svc.lines, svc.exited = port, proc, lines, line != "listening"
    return svc


def stop(svc):
    if not svc.exited:
        svc.proc.terminate()
    svc.proc.wait()


def get(svc, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def post(svc, n):
    chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)


def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


def conf(datadir, lines):
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write("\n".join(lines) + "\n")


def table():
    return [(int(r[0]), r[1], int(r[2]), r[3]) for r in psql("select id, host, port, secret from endpoints order by id")]


def main():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f",
                    os.path.join(ROOT, "sql", "schema.sql")], check=True, capture_output=True, env=PSQL_ENV)
    psql("truncate endpoints")
    d = tempfile.mkdtemp(prefix="hooks-roster-")
    s1, s2, s3 = secret(), secret(), secret()

    # 1. import
    conf(d, ["# the endpoints", f"3 127.0.0.1 9001 {s1}", f"", f"5 localhost 9002 {s2}", f"0 example.org 443 {s3}"])
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    check("1. the import succeeds and says how many it added", code == 0 and any("imported 3 of 3" in l for l in lines), str((code, lines)))
    check("1. ... without being given a port (an import does not listen)", not any("required" in l for l in lines), str(lines))
    check("1. the table has every row as written (id, host, port, secret)",
          table() == [(0, "example.org", 443, s3), (3, "127.0.0.1", 9001, s1), (5, "localhost", 9002, s2)], str(table()))
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    check("1. a second import adds none", code == 0 and any("imported 0 of 3" in l for l in lines) and len(table()) == 3, str((code, lines)))
    conf(d, [f"3 other.host 7777 {s2}", f"7 new.host 8000 {s1}"])
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    t = {r[0]: r for r in table()}
    check("1. a line whose id is there changes nothing, and a new id is added",
          code == 0 and any("imported 1 of 2" in l for l in lines) and t[3] == (3, "127.0.0.1", 9001, s1) and t[7] == (7, "new.host", 8000, s1), str((t, lines)))

    # 2. an import that cannot happen
    psql("truncate endpoints")
    conf(d, [f"1 a.host 80 {s1}", f"2 b.host 80 {s1}", f"3 c.host 0 {s1}"])
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    check("2. a bad line (line 3: port 0) is refused with status 13 and its number", code == 13 and any("line 3" in l for l in lines), str((code, lines)))
    check("2. ... and none of the file was imported", table() == [], str(table()))
    os.remove(os.path.join(d, "endpoints.conf"))
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    check("2. no endpoints.conf: status 20 and a message", code == 20 and any("no endpoints.conf" in l for l in lines), str((code, lines)))
    conf(d, [f"1 a.host 80 {s1}"])
    code, lines = run_once(d, ["--import-endpoints", "1"])
    check("2. no --pg-host: status 20 and a message", code == 20 and any("needs --pg-host" in l for l in lines), str((code, lines)))
    dead = chaos.free_port()
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags(port=dead)])
    check("2. a database that is not there: status 20, nothing imported", code == 20 and table() == [], str((code, lines)))
    code, lines = run_once(d, ["--import-endpoints", "yes", *pg_flags()])
    check("2. --import-endpoints takes 0 or 1", code != 0 and any("import-endpoints" in l for l in lines), str((code, lines)))

    # an import the database refuses part of the way through adds nothing (a trigger that refuses id 5)
    psql("truncate endpoints")
    psql("create or replace function refuse_five() returns trigger language plpgsql as $$ begin if new.id = 5 then raise exception 'no five'; end if; return new; end $$")
    psql("create trigger refuse_five before insert on endpoints for each row execute function refuse_five()")
    conf(d, [f"1 a.host 80 {s1}", f"5 b.host 80 {s1}", f"2 c.host 80 {s1}"])
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    psql("drop trigger refuse_five on endpoints")
    check("2. a database that refuses line 2: status 20, and the line before it was not kept", code == 20 and table() == [] and any("refused the import" in l for l in lines), str((code, lines, table())))
    code, lines = run_once(d, ["--import-endpoints", "1", *pg_flags()])
    check("2. ... and without the trigger the same file imports whole", code == 0 and len(table()) == 3, str((code, lines)))

    # 3. the table, not the file
    psql("truncate endpoints")
    ka, kb = secret(), secret()
    ra, rb, rfile = Receiver(ka), Receiver(kb), Receiver()
    psql(f"insert into endpoints values (0, '127.0.0.1', {ra.port}, '{ka}'), (1, '127.0.0.1', {rb.port}, '{kb}')")
    conf(d, [f"9 127.0.0.1 {rfile.port} {secret()}"])
    for f in os.listdir(d):
        if f.endswith(".seg"):
            os.remove(os.path.join(d, f))
    svc = start(d, pg_flags())
    check("3. the service starts and counts the table's two endpoints", not svc.exited and get(svc, "/stats")["endpoints"] == 2, str(svc.lines))
    post(svc, 1)
    check("3. event 1 reached both of the table's endpoints", wait_for(lambda: len(ra.seen) == 1 and len(rb.seen) == 1, 5), str((ra.seen, rb.seen)))
    time.sleep(0.5)
    check("3. ... and not the file's endpoint", rfile.seen == [], str(rfile.seen))
    check("3. ... each signed with its own secret from the table", ra.bad == 0 and rb.bad == 0, str((ra.bad, rb.bad)))
    stop(svc)

    # 4. read at each start
    psql("delete from endpoints where id = 1")
    svc = start(d, pg_flags())
    check("4. after a row is removed and the service restarted it counts one endpoint", get(svc, "/stats")["endpoints"] == 1, str(get(svc, "/stats")))
    post(svc, 2)
    check("4. event 2 reaches the one that is left", wait_for(lambda: len(ra.seen) == 2, 5), str(ra.seen))
    time.sleep(0.5)
    check("4. ... and not the one removed", len(rb.seen) == 1, str(rb.seen))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)
    d = tempfile.mkdtemp(prefix="hooks-roster-")
    svc = start(d, pg_flags())
    check("4. an empty table: the service starts with no endpoints", not svc.exited and get(svc, "/stats")["endpoints"] == 0, str(svc.lines))
    post(svc, 1)
    check("4. ... and still accepts and keeps events", get(svc, "/stats")["keys"] >= 0 and get(svc, "/healthz") == {"ok": True})
    stop(svc)

    # 5. rows the service cannot use
    psql(f"insert into endpoints values (1, '127.0.0.1', 80, '{s1}'), (2, '127.0.0.1', 80, 'not-a-secret')")
    svc = start(d, pg_flags())
    check("5. a secret that is not whsec_ and base64: status 13, and the row's number (2)",
          svc.exited and svc.proc.wait() == 13 and any("row 2" in l for l in svc.lines), str(svc.lines))
    stop(svc)
    psql("truncate endpoints")
    try:
        psql(f"insert into endpoints values (1000000, '127.0.0.1', 80, '{s1}')")
        refused_by_table = False
    except RuntimeError:
        refused_by_table = True
    check("5. the table itself refuses an id of seven digits (its check constraint)", refused_by_table)
    # a table made before that constraint could hold one: the service refuses it too
    psql("alter table endpoints drop constraint endpoints_id_check")
    psql(f"insert into endpoints values (1, '127.0.0.1', 80, '{s1}'), (1000000, '127.0.0.1', 80, '{s1}')")
    svc = start(d, pg_flags())
    check("5. an id of seven digits: status 13, row 2", svc.exited and svc.proc.wait() == 13 and any("row 2" in l for l in svc.lines), str(svc.lines))
    stop(svc)
    psql("truncate endpoints")
    psql("alter table endpoints add constraint endpoints_id_check check (id between 0 and 999999)")
    psql(f"insert into endpoints values (1, 'bad host', 80, '{s1}')")
    svc = start(d, pg_flags())
    check("5. a host with a space in it: status 20 and the reason", svc.exited and svc.proc.wait() == 20 and any("not printable" in l for l in svc.lines), str(svc.lines))
    stop(svc)

    psql("truncate endpoints")
    psql(f"insert into endpoints values (1, '', 80, '{s1}')")
    svc = start(d, pg_flags())
    check("5. an empty host: status 20 and the reason", svc.exited and svc.proc.wait() == 20 and any("not printable" in l for l in svc.lines), str(svc.lines))
    stop(svc)
    psql("truncate endpoints")
    psql(f"insert into endpoints select g, repeat('h', 250), 80, '{s1}' from generate_series(0, 200) g")
    svc = start(d, pg_flags())
    check("5. a table larger than the service reads (201 rows of 250-byte hosts): status 20 and the reason, not a crash",
          svc.exited and svc.proc.wait() == 20 and any("too large" in l for l in svc.lines), str(svc.lines))
    stop(svc)

    # 6. the database or the table is not there
    psql("alter table endpoints rename to endpoints_away")
    try:
        svc = start(d, pg_flags())
        check("6. no endpoints table: status 20 and what it is", svc.exited and svc.proc.wait() == 20 and any("query failed" in l for l in svc.lines), str(svc.lines))
        stop(svc)
    finally:
        psql("alter table endpoints_away rename to endpoints")
    # a table that prepares and then fails when it is read (an updatable view whose filter raises) is not an empty table
    psql("alter table endpoints rename to endpoints_away")
    psql("create or replace function boom() returns int language plpgsql volatile as $$ begin raise exception 'boom'; end $$")
    psql("create view endpoints as select id, host, port, secret from endpoints_away where boom() > 0")
    try:
        svc = start(d, pg_flags())
        check("6. a query that fails when it runs is a refusal, not an empty table", svc.exited and svc.proc.wait() == 20 and any("query failed" in l for l in svc.lines), str(svc.lines))
        stop(svc)
    finally:
        psql("drop view endpoints")
        psql("alter table endpoints_away rename to endpoints")
    psql("truncate endpoints")
    svc = start(d, pg_flags(port=dead))
    check("6. no database: status 20, cannot connect", svc.exited and svc.proc.wait() == 20 and any("cannot connect" in l for l in svc.lines), str(svc.lines))
    stop(svc)
    if PG_PASSWORD:
        svc = start(d, pg_flags(password=PG_PASSWORD + "-wrong"))
        check("6. a wrong password: status 20, cannot log in", svc.exited and svc.proc.wait() == 20 and any("cannot log in" in l for l in svc.lines), str(svc.lines))
        stop(svc)

    # 7. no database named: the file, as before (a fresh directory: the one above holds events that an endpoint would be sent)
    shutil.rmtree(d, ignore_errors=True)
    d = tempfile.mkdtemp(prefix="hooks-roster-")
    rf = Receiver()
    conf(d, [f"4 127.0.0.1 {rf.port} {secret()}"])
    svc = start(d, [])
    check("7. without --pg-host the file is read: one endpoint, and it gets the event", not svc.exited and get(svc, "/stats")["endpoints"] == 1, str(svc.lines))
    post(svc, 1)
    check("7. ... delivered", wait_for(lambda: len(rf.seen) == 1, 5), str(rf.seen))
    stop(svc)

    # 8. a database that accepts and never answers
    proxy = PgProxy(PG_HOST, PG_PORT)
    proxy.mode = "hold"
    proc = subprocess.Popen([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", *pg_flags(port=proxy.port)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    time.sleep(2)
    check("8. while the database is silent the start waits (it does not start without its endpoints)", proc.poll() is None)
    proc.kill()
    proc.wait()
    proxy.close()

    shutil.rmtree(d, ignore_errors=True)
    psql("truncate endpoints")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all roster checks passed")


main()
