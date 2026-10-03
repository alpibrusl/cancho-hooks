#!/usr/bin/env python3
"""The history of attempts in PostgreSQL (docs/design.md section 24).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/history_test.py build/hooks
                                                                    (default 127.0.0.1:5432:postgres:hooks, no password)

The database must exist; the test applies sql/schema.sql and empties `attempts` itself. With HOOKS_PG_PASSWORD the service logs in
with SCRAM-SHA-256 using it (and `psql` gets it as PGPASSWORD); without, the server must trust the connection.

  1. every attempt that ends is a row, with what it was: the receiver's status, the outcome, the attempt number, when it ended
     and how long it took; and there are as many rows as the service counted attempts
  2. the reasons that are not a status: nothing listening (-1) and a receiver that never answers (-3, after the deadline)
  3. a replay's attempts are rows of their own (replay = 1, numbered from 1)
  4. a database that is not there at start: the service starts, says so, delivers, and counts the rows it could not write
  5. a database that is up and never answers: delivery is as fast as with it, the ring fills and rows are dropped and counted
  6. a database that goes away in the middle: delivery goes on, the service goes on, the rows are counted as lost
  7. no database named: nothing is counted and nothing is written
  8. a database that answers with an SQL error (the table is gone): rows counted as failed, delivery unaffected, writing resumes
  9. the defaults (user and database `hooks`, no password), when the server trusts the connection
 10. `GET /events/:id/attempts` (C3b): the rows of an event as JSON, in order, with a replay's among them; an event with no rows is
     `[]`; an unknown event is a 404 and event 0 a 400; no database named is a 503; concurrent requests are each answered with their
     own event's rows; on a connection kept alive two requests in a row work
 11. the same request when the database is slow, gone or wrong: a database that never answers is a 504 after five seconds, the
     slots of the requests that waited are given back (the 65th waiting request is a 503 and a later one is accepted again),
     a cut database or an SQL error is a 503, and delivery is not touched by any of it
"""
import base64
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
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
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


def psql(sql, db=None):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", db or PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def apply_schema():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f",
                    os.path.join(ROOT, "sql", "schema.sql")], check=True, capture_output=True, env=PSQL_ENV)
    psql("truncate attempts")


class Receiver:
    """`status` is what it answers; `stall` makes it hold the request for that many seconds first."""

    def __init__(self):
        self.status = 204
        self.stall = 0
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(self.headers["webhook-id"])
                if outer.stall:
                    time.sleep(outer.stall)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(svc, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def stats(svc):
    return get(svc, "/stats")


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


def start_service(endpoints, pg_port, schedule="100", deadline=800, extra_flags=(), pg_flags=True):
    datadir = tempfile.mkdtemp(prefix="hooks-history-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        for i, port in enumerate(endpoints):
            f.write(f"{i} 127.0.0.1 {port} {secret}\n")
    port = chaos.free_port()
    flags = [BIN, "--port", str(port), "--dir", datadir, "--schedule", schedule, "--deadline-ms", str(deadline)]
    if pg_flags is True:
        flags += ["--pg-host", PG_HOST, "--pg-port", str(pg_port), "--pg-user", PG_USER, "--pg-database", PG_DB]
        if PG_PASSWORD:
            flags += ["--pg-password", PG_PASSWORD]
    elif pg_flags:
        flags += list(pg_flags)
    flags += list(extra_flags)
    proc = subprocess.Popen(flags, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line == "listening":
            break
    svc = type("Svc", (), {})()
    svc.port, svc.proc, svc.datadir, svc.lines = port, proc, datadir, lines
    return svc


def stop(svc):
    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(svc.datadir, ignore_errors=True)


def rows(where=""):
    return psql("select endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms from attempts " + where + " order by endpoint, event, replay, attempt")


def main():
    apply_schema()
    a, b, d = Receiver(), Receiver(), Receiver()
    a.status = 500
    d.stall = 3
    dead_port = closed_port()
    t0 = int(time.time() * 1000)

    # 1 and 2: four endpoints: A fails, B delivers, C is not listening, D never answers in time
    svc = start_service([a.port, b.port, dead_port, d.port], PG_PORT)
    post(svc, 1)
    check("1. every attempt is counted as written", wait_for(lambda: stats(svc)["dead"] == 3 and stats(svc)["history_written"] == stats(svc)["attempts"], 15),
          str(stats(svc)))
    st = stats(svc)
    check("1. both of the service's connections to the database are live", st["history_live"] == 2, str(st))
    check("1. the database has a row for each (as many rows as attempts)", int(psql("select count(*) from attempts")[0][0]) == st["attempts"], f"{st}")
    r = {(int(x[0]), int(x[3])): x for x in rows()}
    check("1. A: attempt 1 failed with the receiver's 500, attempt 2 is the dead letter",
          r[(0, 1)][4:6] == ("2", "500") and r[(0, 2)][4:6] == ("3", "500"), str(r))
    check("1. B: one attempt, delivered, 204", r[(1, 1)][4:6] == ("1", "204") and (1, 2) not in r, str(r))
    check("1. every row is for event 1, not a replay", all(x[1] == "1" and x[2] == "0" for x in r.values()), str(r))
    check("1. when it ended is the clock at the time, in Unix ms", all(t0 - 1000 <= int(x[6]) <= int(time.time() * 1000) + 1000 for x in r.values()),
          str([x[6] for x in r.values()]))
    check("1. B's latency is small (a local receiver)", 0 <= int(r[(1, 1)][7]) < 500, str(r[(1, 1)]))
    check("2. C (nothing listening): -1 on both attempts", r[(2, 1)][5] == "-1" and r[(2, 2)][5] == "-1", str(r))
    check("2. D (never answers): -3, and it took about the deadline (800 ms)",
          r[(3, 1)][5] == "-3" and 700 <= int(r[(3, 1)][7]) <= 2500, str(r[(3, 1)]))

    # 3. a replay
    a.status = 204
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/1/replay/0", method="POST", data=b""), timeout=5).read()
    check("3. the replay's attempt is a row of its own (replay 1, attempt 1, delivered, 204)",
          wait_for(lambda: [x[:6] for x in rows("where replay = 1")] == [("0", "1", "1", "1", "1", "204")], 5), str(rows("where replay = 1")))
    check("3. and the first run's rows are untouched", len(rows("where replay = 0 and endpoint = 0")) == 2, str(rows("where endpoint = 0")))
    stop(svc)

    # 4. no database at start
    psql("truncate attempts")
    svc = start_service([b.port], closed_port())
    check("4. the service starts and says the database is not there", any("the database: 0 of 2" in l for l in svc.lines), str(svc.lines))
    b.seen.clear()
    post(svc, 1)
    check("4. delivery works", wait_for(lambda: stats(svc)["delivered"] == 1, 5), str(stats(svc)))
    st = stats(svc)
    check("4. history is off and the row is counted as dropped", st["history_live"] == 0 and st["history_dropped"] == 1 and st["history_written"] == 0, str(st))
    stop(svc)

    if PG_PASSWORD:
        svc = start_service([b.port], PG_PORT, pg_flags=["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB,
                                                         "--pg-password", PG_PASSWORD + "-wrong"])
        check("4. a wrong password is refused (the service starts, with 0 of 2 connections)", any("the database: 0 of 2" in l for l in svc.lines), str(svc.lines))
        stop(svc)

    # 5. a database that never answers
    psql("truncate attempts")
    proxy = PgProxy(PG_HOST, PG_PORT)
    svc = start_service([b.port], proxy.port)
    t = time.time()
    for n in range(100):
        post(svc, n)
    wait_for(lambda: stats(svc)["delivered"] == 100, 30)
    base = time.time() - t
    check("5. with the database answering: 100 events delivered, all rows written", stats(svc)["delivered"] == 100 and wait_for(lambda: stats(svc)["history_written"] == 100, 10), str(stats(svc)))
    proxy.mode = "hold"
    t = time.time()
    for n in range(100, 700):
        post(svc, n)
    ok = wait_for(lambda: stats(svc)["delivered"] == 700, 60)
    held = time.time() - t
    st = stats(svc)
    check("5. with it never answering: 600 more events are all delivered", ok, str(st))
    check("5. ... as fast as the 100 were, per event (within 4x: it measures that nothing waits)", held / 600 <= 4 * (base / 100) + 0.01, f"{held / 600:.4f} s each vs {base / 100:.4f}")
    check("5. the ring filled and rows were dropped, and counted", st["history_dropped"] > 0, str(st))
    check("5. the service still answers", get(svc, "/healthz") == {"ok": True})
    stop(svc)
    proxy.close()

    # 6. a database that goes away
    psql("truncate attempts")
    proxy = PgProxy(PG_HOST, PG_PORT)
    svc = start_service([b.port], proxy.port)
    for n in range(20):
        post(svc, n)
    wait_for(lambda: stats(svc)["history_written"] == 20, 10)
    proxy.cut()
    time.sleep(0.2)
    for n in range(20, 120):
        post(svc, n)
    check("6. delivery goes on after the database is cut: all 120 delivered", wait_for(lambda: stats(svc)["delivered"] == 120, 20), str(stats(svc)))
    wait_for(lambda: stats(svc)["history_failed"] + stats(svc)["history_dropped"] > 0, 10)
    st = stats(svc)
    check("6. the rows that could not be written are counted (failed or dropped), the ones before the cut stay",
          st["history_failed"] + st["history_dropped"] > 0 and int(psql("select count(*) from attempts")[0][0]) >= 20, str(st))
    check("6. the service still answers", get(svc, "/healthz") == {"ok": True})
    stop(svc)
    proxy.close()

    # 7. no database named: history is off and costs nothing, and nothing is counted
    svc = start_service([b.port], 0, pg_flags=False)
    for n in range(300):
        post(svc, n)
    wait_for(lambda: stats(svc)["delivered"] == 300, 30)
    st = stats(svc)
    check("7. without --pg-host: 300 delivered, and no history counter moves", st["delivered"] == 300 and (st["history_live"], st["history_written"], st["history_failed"], st["history_dropped"]) == (0, 0, 0, 0), str(st))
    stop(svc)

    # 8. a database that answers with an SQL error: the rows are counted as failed, delivery does not care
    psql("truncate attempts")
    svc = start_service([b.port], PG_PORT)
    post(svc, 1)
    wait_for(lambda: stats(svc)["history_written"] == 1, 10)
    psql("alter table attempts rename to attempts_away")
    try:
        for n in range(2, 12):
            post(svc, n)
        check("8. every row of the ten events is counted as failed (the table is not there), and the service delivered them",
              wait_for(lambda: stats(svc)["history_failed"] == 10 and stats(svc)["delivered"] == 11, 15), str(stats(svc)))
    finally:
        psql("alter table attempts_away rename to attempts")
    post(svc, 12)
    check("8. and when the table is back, rows are written again (the connections were never lost)", wait_for(lambda: stats(svc)["history_written"] == 2, 10), str(stats(svc)))
    stop(svc)

    # 9. the defaults: user hooks, database hooks, no password
    psql("truncate attempts")
    psql("drop role if exists hooks")
    psql("create role hooks login")
    psql("grant insert, select on attempts to hooks")
    if PG_DB != "hooks" or PG_PASSWORD:
        print("skipped 9: the defaults are database `hooks` and no password")
    else:
        svc = start_service([b.port], PG_PORT, pg_flags=["--pg-host", PG_HOST, "--pg-port", str(PG_PORT)])
        post(svc, 1)
        check("9. only --pg-host and --pg-port: user and database `hooks`, no password", wait_for(lambda: stats(svc)["history_written"] == 1, 10), str(stats(svc)))
        check("9. ... and the server sees the connections as user hooks on database hooks",
              int(psql("select count(*) from pg_stat_activity where usename = 'hooks' and datname = 'hooks'")[0][0]) == 2,
              str(psql("select usename, datname from pg_stat_activity where datname = 'hooks'")))
        stop(svc)
    psql("revoke insert, select on attempts from hooks")
    psql("drop role hooks")

    # 10. reading the history back
    psql("truncate attempts")
    a.status, b.status, d.stall = 500, 204, 3
    svc = start_service([a.port, b.port, dead_port, d.port], PG_PORT)
    post(svc, 1)
    wait_for(lambda: stats(svc)["dead"] == 3 and stats(svc)["history_written"] == stats(svc)["attempts"], 15)
    a.status = 204
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/1/replay/0", method="POST", data=b""), timeout=5).read()
    wait_for(lambda: stats(svc)["history_written"] == stats(svc)["attempts"] and stats(svc)["attempts"] >= 8, 10)
    got = get(svc, "/events/1/attempts")
    want = [(0, False, 1, "failed", 500), (0, False, 2, "dead", 500), (0, True, 1, "delivered", 204), (1, False, 1, "delivered", 204),
            (2, False, 1, "failed", -1), (2, False, 2, "dead", -1), (3, False, 1, "failed", -3), (3, False, 2, "dead", -3)]
    check("10. the attempts of event 1, in order of endpoint, replay and attempt, with their outcomes and statuses",
          [(r["endpoint"], r["replay"], r["attempt"], r["outcome"], r["status"]) for r in got] == want, str(got))
    check("10. each row also says when it ended and how long it took",
          all(isinstance(r["at"], int) and r["at"] >= t0 - 1000 and isinstance(r["latency_ms"], int) for r in got)
          and all(700 <= r["latency_ms"] <= 2500 for r in got if r["status"] == -3), str(got))
    check("10. the answer is the database's, row for row", [(int(x[0]), x[2] == "1", int(x[3]), int(x[5])) for x in rows("where event = 1")] ==
          [(r["endpoint"], r["replay"], r["attempt"], r["status"]) for r in got], str(got))
    post(svc, 2)
    wait_for(lambda: len(rows("where event = 2")) == 6, 15)
    check("10. event 2's answer holds only event 2's rows", {r["endpoint"] for r in get(svc, "/events/2/attempts")} == {0, 1, 2, 3} and
          len(get(svc, "/events/2/attempts")) == 6, str(get(svc, "/events/2/attempts")))
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    c.request("GET", "/events/1/attempts")
    r1 = c.getresponse()
    body1 = r1.read()
    c.request("GET", "/events/2/attempts")
    r2 = c.getresponse()
    body2 = r2.read()
    check("10. two requests on one connection kept alive are each answered", r1.status == 200 and r2.status == 200 and len(json.loads(body1)) == 8 and len(json.loads(body2)) == 6,
          f"{r1.status} {r2.status}")
    c.close()
    results = {}

    def ask(k):
        try:
            results[k] = (get(svc, f"/events/{1 + k % 2}/attempts"), 1 + k % 2)
        except Exception as e:
            results[k] = (str(e), 0)

    threads = [threading.Thread(target=ask, args=(k,)) for k in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("10. 50 requests at once are each answered with their own event's rows",
          all(isinstance(v[0], list) and len(v[0]) == (8 if v[1] == 1 else 6) for v in results.values()), str({k: v for k, v in results.items() if not isinstance(v[0], list)}))
    code404 = urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/99/attempts")
    try:
        urllib.request.urlopen(code404, timeout=5)
        c404 = 200
    except urllib.error.HTTPError as e:
        c404 = e.code
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/0/attempts", timeout=5)
        c400 = 200
    except urllib.error.HTTPError as e:
        c400 = e.code
    check("10. an unknown event is a 404 and event 0 a 400", (c404, c400) == (404, 400), f"{c404} {c400}")
    ok_all = True
    for k in range(150):
        try:
            if len(get(svc, "/events/1/attempts")) != 8:
                ok_all = False
        except Exception:
            ok_all = False
    check("10. 150 requests one after another are all answered (a slot is given back when its request is answered)", ok_all)
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    c.request("GET", "/events/1/attempts", headers={"Connection": "close"})
    rc = c.getresponse()
    rc.read()
    check("10. a request that asked for Connection: close is answered with it", rc.status == 200 and (rc.getheader("Connection") or "").lower() == "close", str(rc.getheaders()))
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    c.request("GET", "/events/1/attempts")
    rk = c.getresponse()
    rk.read()
    check("10. and one that did not is kept alive", rk.status == 200 and (rk.getheader("Connection") or "keep-alive").lower() != "close", str(rk.getheaders()))
    c.close()
    stop(svc)

    # an event nobody tried to deliver (no endpoints) has no rows: []
    psql("truncate attempts")
    svc = start_service([], PG_PORT)
    post(svc, 1)
    check("10. an event with no attempts is an empty list", get(svc, "/events/1/attempts") == [], str(get(svc, "/events/1/attempts")))
    stop(svc)
    svc = start_service([b.port], 0, pg_flags=False)
    post(svc, 1)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/1/attempts", timeout=5)
        cn, msg = 200, b""
    except urllib.error.HTTPError as e:
        cn, msg = e.code, e.read()
    check("10. without a database named it is a 503 that says why", cn == 503 and b"no database is named" in msg, f"{cn} {msg}")
    stop(svc)

    # 11. the same request, when the database is not right
    psql("truncate attempts")
    proxy = PgProxy(PG_HOST, PG_PORT)
    svc = start_service([b.port], proxy.port)
    for n in range(5):
        post(svc, n)
    wait_for(lambda: stats(svc)["history_written"] == 5, 10)
    proxy.mode = "hold"
    t = time.time()
    codes = {}

    def ask_held(k):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/1/attempts", timeout=30) as r:
                codes[k] = (r.status, time.time() - t)
        except urllib.error.HTTPError as e:
            codes[k] = (e.code, time.time() - t)
        except Exception as e:
            codes[k] = (str(e), time.time() - t)

    threads = [threading.Thread(target=ask_held, args=(k,)) for k in range(70)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    fives = sorted(v[0] for v in codes.values())
    check("11. a database that never answers: 64 requests wait and are a 504 after about five seconds, the other 6 are a 503 at once",
          fives.count(504) == 64 and fives.count(503) == 6 and all(4.5 <= v[1] <= 8 for v in codes.values() if v[0] == 504)
          and all(v[1] < 2 for v in codes.values() if v[0] == 503), str(sorted(codes.values())[:3]) + str(fives[:3]) + str(fives[-3:]))
    t = time.time()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/1/attempts", timeout=30)
        cs, took = 200, time.time() - t
    except urllib.error.HTTPError as e:
        cs, took = e.code, time.time() - t
    check("11. the slots were given back: the next request is accepted again (waits, then 504), not refused", cs == 504 and took >= 4.5, f"{cs} {took:.1f}")
    check("11. and delivery was never touched", get(svc, "/healthz") == {"ok": True})
    stop(svc)
    proxy.close()

    psql("truncate attempts")
    proxy = PgProxy(PG_HOST, PG_PORT)
    svc = start_service([b.port], proxy.port)
    post(svc, 1)
    wait_for(lambda: stats(svc)["history_written"] == 1, 10)
    proxy.cut()
    time.sleep(0.3)
    t = time.time()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/1/attempts", timeout=30)
        cc = 200
    except urllib.error.HTTPError as e:
        cc = e.code
    check("11. a database that was cut: a 503 (not a wait)", cc == 503 and time.time() - t < 3, f"{cc} {time.time() - t:.1f}")
    stop(svc)
    proxy.close()

    psql("truncate attempts")
    svc = start_service([b.port], PG_PORT)
    post(svc, 1)
    wait_for(lambda: stats(svc)["history_written"] == 1, 10)
    psql("alter table attempts rename to attempts_away")
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/1/attempts", timeout=10)
            ce = 200
        except urllib.error.HTTPError as e:
            ce = e.code
        check("11. an SQL error (the table is gone): a 503", ce == 503, str(ce))
    finally:
        psql("alter table attempts_away rename to attempts")
    check("11. and when the table is back the same connections answer", get(svc, "/events/1/attempts") != [] and len(get(svc, "/events/1/attempts")) == 1, str(get(svc, "/events/1/attempts")))
    stop(svc)

    print("FAILED: " + ", ".join(FAILS) if FAILS else "all history checks passed")
    sys.exit(1 if FAILS else 0)


main()
