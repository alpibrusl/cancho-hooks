#!/usr/bin/env python3
"""Cron: scheduled events (docs/design.md section 32), end to end.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/schedules_test.py build/hooks [stages]

`stages` is a list of the numbers below to run (default: all). A schedule's expression has a seconds field when the service runs with
`--cron-seconds 1`, which is how most stages see a fire in seconds; stage 7 runs the real five-field service and waits for a real minute.
The test needs the whole `schedules` table to itself (it empties it), and so a database of its own while it runs.

  1. who may call it: no admin-token configured is a 403; a missing, wrong or doubled bearer token is a 401, on all five routes, the reads too;
     no database named is a 503
  2. what a request may say: every refusal is a 400 with its reason and creates nothing; unknown ids are 404s
  3. create, read, list, change, delete: the next fire is worked out from the expression; enabled and disabled; 64 schedules and no more
  4. a fire is an ordinary event: type, schedule, scheduled second and body, under the key `cron:<id>:<second>`, delivered signed
  5. not for the time before it existed; disabled schedules do not fire; enabling and changing the expression count from then
  6. a database that refuses an insert (503, nothing changed) and one that is slow past five seconds (504)
  7. real clock, five fields: after a stop, one fire for the missed minutes (not sixty) and then the next minute's on time; and `cron-catchup 0`
  8. missed fires after a stop, in seconds mode: one for the window, the ones within the grace period each on their own, and none with `cron-catchup 0`
  9. kill -9 between the append of an event and the database hearing of it (a proxy that swallows the update): after the restart the
     fire is not made twice, over several rounds, with a power cut half of them, and with a downtime longer than the grace period
 10. kill -9 as a power cut, within milliseconds of an event reaching the log and at random instants, under a schedule of every second:
     one event per scheduled second, none twice, none missing
 11. (`FULL=1`) the idempotency index full (65,536 keys): a fire is refused, not made without its key, and counted as an error
"""
import atexit
import base64
import calendar
import hashlib
import hmac
import http.client
import http.server
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import pgwait  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

http.server.HTTPServer.request_queue_size = 128
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FULL = os.environ.get("FULL") == "1"
STAGES = {int(a) for a in sys.argv[2:]} or set(range(1, 12 if FULL else 11))
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(os.path.dirname(BIN), "fsync_shim.so")
TOKEN = "correct-horse-battery-staple"
FAILS = []
GRACE = 10
RUNNING = []


@atexit.register
def _stop_all():
    """A test that dies must not leave a service reading the table: it would fire the next test's schedules."""
    for p in RUNNING:
        if p.poll() is None:
            p.kill()


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def pg_flags(port=None):
    f = ["--pg-host", PG_HOST, "--pg-port", str(port or PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


class Svc:
    def __init__(self, d, seconds=True, catchup=None, pg=True, token=True, pg_port=None, shim=False, extra=()):
        self.d, self.port, self.shim = d, chaos.free_port(), shim
        self.args = ["--port", str(self.port), "--dir", d, "--allow-private-hosts", "1", "--schedule", "100", "--deadline-ms", "800"]
        if pg:
            self.args += pg_flags(pg_port)
        if token:
            self.args += ["--admin-token", TOKEN]
        if seconds:
            self.args += ["--cron-seconds", "1"]
        if catchup is not None:
            self.args += ["--cron-catchup", str(catchup)]
        self.args += list(extra)
        self.proc, self.lines = None, []

    def start(self):
        env = dict(os.environ)
        if self.shim and os.path.exists(SHIM):
            env["LD_PRELOAD"] = SHIM
        self.proc = subprocess.Popen([BIN, *self.args], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env)
        RUNNING.append(self.proc)
        self.lines = []
        while True:
            line = self.proc.stderr.readline().decode().strip()
            self.lines.append(line)
            if line in ("listening", ""):
                break
        self.up = line == "listening" and not pgwait.after_listening(self.proc, self.lines, self.args)
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.proc:
            self.proc.wait()

    def kill9(self, power=False):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()
        if power:
            for name in sorted(os.listdir(self.d)):
                if name.endswith(".seg"):
                    chaos.Service.cut_file(self, os.path.join(self.d, name))

    def req(self, method, path, body=None, token=TOKEN, headers=None, raw=False, timeout=15):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = "Bearer " + token
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        if raw:
            return r.status, out
        try:
            return r.status, (json.loads(out) if out else None)
        except ValueError:
            return r.status, out

    def create(self, body, **kw):
        return self.req("POST", "/schedules", body, **kw)

    def get(self, path):
        return self.req("GET", path, token=None)

    def stats(self):
        return self.req("GET", "/stats", token=None)[1]


def wait_for(cond, secs, step=0.05):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def events(d):
    """Every event in the log: (id, the event, the idempotency key or None)."""
    recs, _ = chaos.read_events(d)
    out = []
    for ms, pairs in recs:
        kv = dict(pairs)
        out.append((ms, json.loads(kv[b"event"]), kv.get(b"key", b"").decode() or None))
    return out


def fires(d, schedule=None):
    return [(e["scheduled_at"], key) for _, e, key in events(d) if "scheduled_at" in e and (schedule is None or e["schedule"] == schedule)]


def fresh_dir():
    return tempfile.mkdtemp(prefix="hooks-sched-")


def reset():
    psql("truncate schedules restart identity")


def py_next_minute(t):
    return (t // 60 + 1) * 60


class Receiver:
    def __init__(self):
        self.seen, self.key, self.bad = [], None, 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(json.loads(body))
                if outer.key:
                    mid, ts, sig = self.headers["webhook-id"], self.headers["webhook-timestamp"], self.headers["webhook-signature"]
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


# ---------------------------------------------------------------------------------------------------------------------------

def stage1():
    reset()
    good = {"expr": "* * * * *", "type": "t"}
    routes = [("POST", "/schedules", good), ("GET", "/schedules", None), ("GET", "/schedules/1", None), ("PATCH", "/schedules/1", {"enabled": False}), ("DELETE", "/schedules/1", None)]
    d = fresh_dir()
    svc = Svc(d, seconds=False, token=False).start()
    for m, p, b in routes:
        check(f"1. {m} {p} with no admin-token configured: 403 even with a token", svc.req(m, p, b)[0] == 403)
    svc.stop()
    svc = Svc(d, seconds=False).start()
    for m, p, b in routes:
        r, _ = svc.req(m, p, b, token=None)
        check(f"1. {m} {p} with no Authorization header: 401", r == 401, str(r))
        check(f"1. {m} {p} with a wrong token of the same length: 401", svc.req(m, p, b, token=TOKEN[:-1] + "!")[0] == 401)
        check(f"1. {m} {p} with a prefix of the token: 401", svc.req(m, p, b, token=TOKEN[:-1])[0] == 401)
        check(f"1. {m} {p} with the token and more: 401", svc.req(m, p, b, token=TOKEN + "x")[0] == 401)
        check(f"1. {m} {p} with another scheme: 401", svc.req(m, p, b, token=None, headers={"Authorization": "Basic " + base64.b64encode(b"u:" + TOKEN.encode()).decode()})[0] == 401)
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    c.request("GET", "/schedules")
    r = c.getresponse()
    r.read()
    c.close()
    check("1. a 401 says how to authenticate", r.status == 401 and r.getheader("WWW-Authenticate") == "Bearer")
    s = __import__("socket").create_connection(("127.0.0.1", svc.port))
    s.sendall(b"GET /schedules HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\nConnection: close\r\n\r\n")
    first = s.recv(100)
    s.close()
    check("1. two Authorization headers: 401", first.startswith(b"HTTP/1.1 401"), str(first))
    check("1. the token in the query is not a token", svc.req("GET", "/schedules?token=" + TOKEN, token=None)[0] == 401)
    check("1. none of those created anything", psql("select count(*) from schedules")[0][0] == "0")
    r, b = svc.create(good)
    check("1. with the token: 201", r == 201, str((r, b)))
    svc.stop()
    svc = Svc(d, seconds=False, pg=False).start()
    for m, p, b in routes:
        check(f"1. {m} {p} with no database named: 503", svc.req(m, p, b)[0] == 503)
        check(f"1. ... and the token is judged first: {m} {p} without it is a 401", svc.req(m, p, b, token=None)[0] == 401)
    svc.stop()
    shutil.rmtree(d)


def stage2():
    reset()
    d = fresh_dir()
    svc = Svc(d, seconds=False).start()
    bad = [("not JSON", b"{nope"), ("an array", b"[]"), ("an empty body", b""), ("no expr", {"type": "t"}), ("no type", {"expr": "* * * * *"}),
           ("expr is a number", {"expr": 5, "type": "t"}), ("type is a number", {"expr": "* * * * *", "type": 5}), ("an empty type", {"expr": "* * * * *", "type": ""}),
           ("a type of 65 characters", {"expr": "* * * * *", "type": "x" * 65}), ("a type with a newline", {"expr": "* * * * *", "type": "a\nb"}),
           ("a type that is not ASCII", {"expr": "* * * * *", "type": "café"}),
           ("four fields", {"expr": "* * * *", "type": "t"}), ("six fields (the seconds field is off)", {"expr": "* * * * * *", "type": "t"}),
           ("minute 60", {"expr": "60 * * * *", "type": "t"}), ("hour 24", {"expr": "0 24 * * *", "type": "t"}), ("day 0", {"expr": "0 0 0 * *", "type": "t"}),
           ("day 32", {"expr": "0 0 32 * *", "type": "t"}), ("month 13", {"expr": "0 0 1 13 *", "type": "t"}), ("weekday 8", {"expr": "0 0 * * 8", "type": "t"}),
           ("a range that runs backwards", {"expr": "0 22-2 * * *", "type": "t"}), ("a step of 0", {"expr": "*/0 * * * *", "type": "t"}),
           ("a step on a single number", {"expr": "5/10 * * * *", "type": "t"}), ("names", {"expr": "0 0 * * MON", "type": "t"}), ("@daily", {"expr": "@daily", "type": "t"}),
           ("an empty list item", {"expr": "1,,2 * * * *", "type": "t"}), ("the 31st of February", {"expr": "0 0 31 2 *", "type": "t"}),
           ("an expression of 101 bytes", {"expr": "0 0 " + ",".join(str(i % 28 + 1) for i in range(40)) + " * *", "type": "t"}),
           ("a body nested 13 deep", {"expr": "* * * * *", "type": "t", "body": json.loads("[" * 13 + "]" * 13)}),
           ("a body of more than 1024 bytes", {"expr": "* * * * *", "type": "t", "body": {"pad": "x" * 1100}}),
           ("enabled that is a string", {"expr": "* * * * *", "type": "t", "enabled": "yes"})]
    for name, body in bad:
        r, b = svc.create(body)
        check(f"2. {name}: 400 with a reason", r == 400 and isinstance(b, dict) and len(b.get("error", "")) > 10, str((r, b)))
    check("2. none of them created anything", psql("select count(*) from schedules")[0][0] == "0")
    msgs = {svc.create({"expr": e, "type": "t"})[1]["error"] for e in ["* * * *", "60 * * * *", "5-1 * * * *", "*/0 * * * *", "a * * * *", "0 0 31 2 *"]}
    check("2. each kind of wrong expression says what is wrong in its own words", len(msgs) == 6, str(msgs))
    r, b = svc.create({"expr": "* * * * *", "type": "t", "colour": "red", "body": {"deep": [[[[[[[[[[[1]]]]]]]]]]]}})
    check("2. an unknown member is ignored; a body 12 deep is fine", r == 201, str((r, b)))
    sid = b["id"]
    r, b = svc.create({"expr": "  0\t5   * * *  ", "type": "t"})
    check("2. blanks of any width separate the fields", r == 201, str((r, b)))
    r, b = svc.create({"expr": "* * * * *", "type": "t", "body": [1, "a", None, True, 1.5e3, {"k": "é\n"}]})
    check("2. a body is any JSON, kept as JSON", r == 201 and b["body"] == [1, "a", None, True, 1.5e3, {"k": "é\n"}], str((r, b)))
    r, b = svc.create({"expr": "* * * * *", "type": "t"})
    check("2. no body means {}", r == 201 and b["body"] == {}, str((r, b)))
    check("2. an id that is not a number: 400", svc.req("GET", "/schedules/x")[0] == 400)
    check("2. an id that does not exist: 404 on GET, PATCH and DELETE", [svc.req("GET", "/schedules/99")[0], svc.req("PATCH", "/schedules/99", {"enabled": True})[0], svc.req("DELETE", "/schedules/99")[0]] == [404, 404, 404])
    check("2. an id of 20 digits is refused, not a crash", svc.req("GET", "/schedules/99999999999999999999")[0] in (400, 404) and svc.req("GET", "/schedules")[0] == 200)
    for name, body in [("nothing to change", {}), ("not JSON", b"nope"), ("a bad expr", {"expr": "61 * * * *"}), ("a bad type", {"type": ""}), ("enabled that is a number", {"enabled": 1}),
                       ("only unknown members", {"colour": "red"}), ("a bad body", {"body": {"pad": "x" * 1100}})]:
        r, b = svc.req("PATCH", f"/schedules/{sid}", body)
        check(f"2. PATCH with {name}: 400", r == 400 and isinstance(b, (dict, bytes)), str((r, b)))
    before = psql(f"select expr, event_type, body, enabled from schedules where id = {sid}")
    check("2. ... and the row is as it was", before == [("* * * * *", "t", '{"deep":[[[[[[[[[[[1]]]]]]]]]]]}', "t")], str(before))
    check("2. PUT is a 405", svc.req("PUT", "/schedules")[0] == 405 and svc.req("PUT", f"/schedules/{sid}")[0] == 405)
    svc.stop()
    shutil.rmtree(d)
    reset()


def stage3():
    reset()
    d = fresh_dir()
    svc = Svc(d, seconds=False).start()
    t0 = int(time.time())
    r, c = svc.create({"expr": "*/5 * * * *", "type": "report.daily", "body": {"k": "v"}})
    t1 = int(time.time())
    check("3. 201 with the schedule: id, expr, type, body, enabled", r == 201 and c["id"] == 1 and c["expr"] == "*/5 * * * *" and c["type"] == "report.daily" and c["body"] == {"k": "v"} and c["enabled"] is True, str((r, c)))
    want = {(t // 300 + 1) * 300 for t in (t0, t1)}
    check("3. next_fire is the next five minutes, in seconds and as UTC text", c["next_fire"] in want and c["next_fire_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(c["next_fire"])), str((c, want)))
    check("3. last_fired is null: it has not fired", c["last_fired"] is None and t0 <= c["created_at"] <= t1 + 1, str(c))
    r, one = svc.req("GET", "/schedules/1")
    check("3. GET one says the same", r == 200 and one == c, str((r, one)))
    r, b = svc.create({"expr": "0 0 29 2 *", "type": "leap"})
    year = time.gmtime().tm_year
    leaps = [calendar.timegm((y, 2, 29, 0, 0, 0)) for y in range(year, year + 9) if calendar.isleap(y)]
    check("3. a leap-day schedule's next fire is the next 29 February", r == 201 and b["next_fire"] == min(t for t in leaps if t > time.time()), str(b))
    r, lst = svc.req("GET", "/schedules")
    check("3. GET /schedules lists them by id", r == 200 and [s["id"] for s in lst] == [1, 2], str((r, lst)))
    # change
    r, p = svc.req("PATCH", "/schedules/1", {"enabled": False})
    check("3. PATCH enabled false: disabled, and no next fire", r == 200 and p["enabled"] is False and p["next_fire"] is None and p["next_fire_at"] is None, str((r, p)))
    check("3. the row says so", psql("select enabled from schedules where id = 1") == [("f",)])
    r, p = svc.req("PATCH", "/schedules/1", {"enabled": True, "type": "report.hourly", "body": {"x": [1, 2]}})
    check("3. PATCH enabled true with a new type and body", r == 200 and p["enabled"] is True and p["type"] == "report.hourly" and p["body"] == {"x": [1, 2]} and p["expr"] == "*/5 * * * *" and p["next_fire"] in want, str((r, p)))
    r, p = svc.req("PATCH", "/schedules/1", {"expr": "0 0 1 1 *"})
    nyd = calendar.timegm((time.gmtime().tm_year + 1, 1, 1, 0, 0, 0))
    check("3. PATCH expr: the next fire is the new expression's", r == 200 and p["expr"] == "0 0 1 1 *" and p["next_fire"] == nyd and p["type"] == "report.hourly", str((r, p, nyd)))
    check("3. ... and the table's next_fire is worked out by the tick (it was 0)", wait_for(lambda: psql("select next_fire from schedules where id = 1") == [(str(nyd),)], 4), str(psql("select next_fire, base from schedules")))
    r, dl = svc.req("DELETE", "/schedules/2")
    check("3. DELETE: 200 and the id", r == 200 and dl == {"id": 2, "deleted": True}, str((r, dl)))
    check("3. ... it is gone: GET 404, DELETE again 404, not in the list", svc.req("GET", "/schedules/2")[0] == 404 and svc.req("DELETE", "/schedules/2")[0] == 404 and [s["id"] for s in svc.req("GET", "/schedules")[1]] == [1])
    check("3. ids are not given twice", svc.create({"expr": "* * * * *", "type": "t"})[1]["id"] == 3)
    # 64
    psql("truncate schedules")
    psql("insert into schedules (expr, event_type, created_at, base, next_fire) select '0 0 1 1 *', 't', 1, 1, 253402300800 from generate_series(1, 64)")
    r, b = svc.create({"expr": "* * * * *", "type": "t"})
    check("3. 64 schedules and no more: a 65th is a 409", r == 409 and psql("select count(*) from schedules")[0][0] == "64", str((r, b)))
    r, lst = svc.req("GET", "/schedules")
    check("3. GET /schedules answers with all 64", r == 200 and len(lst) == 64, str((r, len(lst) if isinstance(lst, list) else lst)))
    svc.req("DELETE", f"/schedules/{lst[0]['id']}")
    check("3. ... and after one is deleted a new one is a 201", svc.create({"expr": "* * * * *", "type": "t"})[0] == 201)
    # the largest answer: 64 schedules with the largest body each
    psql("truncate schedules")
    psql("insert into schedules (expr, event_type, body, created_at, base, next_fire) select '0 0 1 1 *', repeat('t', 64), '\"' || repeat('x', 1022) || '\"', 1, 1, 253402300800 from generate_series(1, 64)")
    r, lst = svc.req("GET", "/schedules")
    check("3. GET /schedules with 64 schedules of 1024 bytes of body each (the largest answer): 200 with all of them", r == 200 and len(lst) == 64 and all(len(x["body"]) == 1022 for x in lst), str((r, len(lst) if isinstance(lst, list) else lst)))
    # a mode mismatch: six fields in a service without seconds, and the other way
    svc.stop()
    psql("truncate schedules")
    svc = Svc(d, seconds=True).start()
    check("3. with cron-seconds 1 a five-field expression is a 400, and six fields are a 201",
          svc.create({"expr": "* * * * *", "type": "t"})[0] == 400 and svc.create({"expr": "0 * * * * *", "type": "t"})[0] == 201)
    svc.stop()
    shutil.rmtree(d)
    reset()


def stage4():
    reset()
    d = fresh_dir()
    rc = Receiver()
    sec = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    rc.key = sec
    psql("truncate endpoints")
    psql(f"insert into endpoints values (0, '127.0.0.1', {rc.port}, '{sec}')")
    svc = Svc(d).start()
    r, c = svc.create({"expr": "*/2 * * * * *", "type": "cron.tick", "body": {"n": 7, "tags": ["a", "b"]}})
    sid = c["id"]
    check("4. created in seconds mode", r == 201, str((r, c)))
    ok = wait_for(lambda: len(rc.seen) >= 4, 15)
    check("4. the receiver is sent the fires, signed", ok and rc.bad == 0, str((len(rc.seen), rc.bad)))
    ev = rc.seen[:4]
    check("4. each is an ordinary event: type, schedule, scheduled_at, body", all(e["type"] == "cron.tick" and e["schedule"] == sid and e["body"] == {"n": 7, "tags": ["a", "b"]} for e in ev), str(ev))
    secs = [e["scheduled_at"] for e in ev]
    check("4. the scheduled seconds are every second one, once each", all(s % 2 == 0 for s in secs) and secs == sorted(set(secs)) and all(b - a == 2 for a, b in zip(secs, secs[1:])), str(secs))
    log = events(d)
    check("4. the log holds them with the key cron:<id>:<second>", all(key == f"cron:{sid}:{e['scheduled_at']}" for _, e, key in log), str([(e, k) for _, e, k in log][:3]))
    r, one = svc.get(f"/events/{log[0][0]}")
    check("4. GET /events/:id reads one back like any event", r == 200 and one["event"] == log[0][1], str((r, one)))
    check("4. /stats counts the fires and no error", svc.stats()["cron_fired"] >= 4 and svc.stats()["cron_errors"] == 0 and svc.stats()["cron_skipped"] == 0, str(svc.stats()))
    r, cfg = svc.get("/config")
    check("4. /config shows cron-catchup 1 and cron-seconds 1", cfg["cron-catchup"] == 1 and cfg["cron-seconds"] == 1, str(cfg))
    row = psql(f"select last_fired, next_fire, base from schedules where id = {sid}")[0]
    check("4. the table's last_fired is the last scheduled second, next_fire the one after", int(row[1]) - int(row[0]) == 2 and int(row[0]) >= secs[3], str(row))
    # an event posted by hand keeps working beside the schedule
    status, data = chaos.post(svc.port, json.dumps({"type": "hand"}).encode())
    check("4. POST /events still works beside a schedule", status == 202, str((status, data)))
    svc.stop()
    psql("truncate endpoints")
    shutil.rmtree(d)
    reset()


def stage5():
    reset()
    d = fresh_dir()
    svc = Svc(d).start()
    r, c = svc.create({"expr": "* * * * * *", "type": "t", "enabled": False})
    sid = c["id"]
    check("5. a schedule created disabled: enabled false, no next fire", r == 201 and c["enabled"] is False and c["next_fire"] is None, str((r, c)))
    time.sleep(3.2)
    check("5. a disabled schedule does not fire", fires(d) == [] and svc.stats()["cron_fired"] == 0, str(fires(d)))
    t0 = int(time.time())
    r, p = svc.req("PATCH", f"/schedules/{sid}", {"enabled": True})
    check("5. PATCH enabled true", r == 200 and p["enabled"] is True and p["next_fire"] > t0 - 1, str((r, p)))
    check("5. it fires now", wait_for(lambda: len(fires(d)) >= 3, 8), str(fires(d)))
    first = fires(d)[0][0]
    check("5. and not for the time before it was enabled (the first fire is after the PATCH)", first > t0 - 1, str((first, t0)))
    r, p = svc.req("PATCH", f"/schedules/{sid}", {"expr": "*/3 * * * * *"})
    t1 = int(time.time())
    check("5. PATCH expr", r == 200 and p["expr"] == "*/3 * * * * *", str((r, p)))
    n = len(fires(d))
    wait_for(lambda: len(fires(d)) >= n + 3, 12)
    after = [s for s, _ in fires(d) if s > t1 + 1]
    check("5. after the change the fires are the new expression's (every third second)", len(after) >= 2 and all(s % 3 == 0 for s in after), str((after, fires(d))))
    check("5. no second was fired twice", len({s for s, _ in fires(d)}) == len(fires(d)), str(fires(d)))
    svc.req("PATCH", f"/schedules/{sid}", {"enabled": False})
    time.sleep(1.5)
    n = len(fires(d))
    time.sleep(3.2)
    check("5. disabled again: it stops", len(fires(d)) == n, str((n, len(fires(d)))))
    t2 = int(time.time())
    svc.req("PATCH", f"/schedules/{sid}", {"enabled": True})
    wait_for(lambda: len(fires(d)) > n, 6)
    check("5. enabled again: no fire for the seconds it was off", all(s > t2 - 1 for s, _ in fires(d)[n:]) and len(fires(d)) > n, str((fires(d)[n:], t2)))
    r, dl = svc.req("DELETE", f"/schedules/{sid}")
    time.sleep(1.5)
    n = len(fires(d))
    time.sleep(3.2)
    check("5. a deleted schedule stops firing", r == 200 and len(fires(d)) == n, str((r, n, len(fires(d)))))
    svc.stop()
    shutil.rmtree(d)
    reset()


def stage6():
    reset()
    d = fresh_dir()
    svc = Svc(d, seconds=False).start()
    psql("create or replace function sched_refuse() returns trigger language plpgsql as $$ begin raise exception 'no'; end $$")
    psql("create trigger sched_refuse before insert on schedules for each row execute function sched_refuse()")
    r, b = svc.create({"expr": "* * * * *", "type": "t"})
    check("6. a database that refuses the insert: 503", r == 503, str((r, b)))
    check("6. nothing was created", psql("select count(*) from schedules")[0][0] == "0")
    psql("drop trigger sched_refuse on schedules")
    r, b = svc.create({"expr": "* * * * *", "type": "t"})
    check("6. and the next request works", r == 201, str((r, b)))
    sid = b["id"]
    psql("create or replace function sched_slow() returns trigger language plpgsql as $$ begin perform pg_sleep(7); return new; end $$")
    psql("create trigger sched_slow before update on schedules for each row execute function sched_slow()")
    t0 = time.time()
    r, p = svc.req("PATCH", f"/schedules/{sid}", {"enabled": False})
    took = time.time() - t0
    check("6. a database that takes seven seconds: a 504 after about five", r == 504 and 4.5 < took < 6.5, str((r, p, took)))
    time.sleep(2.5)
    psql("drop trigger sched_slow on schedules")
    check("6. the service answers other requests meanwhile and after", svc.req("GET", f"/schedules/{sid}")[0] == 200)
    svc.stop()
    psql("drop function sched_refuse()")
    psql("drop function sched_slow()")
    shutil.rmtree(d)
    reset()


def stage7():
    """The real service, five fields, a real minute."""
    reset()
    d = fresh_dir()
    svc = Svc(d, seconds=False).start()
    r, c = svc.create({"expr": "* * * * *", "type": "minute", "body": {}})
    sid = c["id"]
    svc.stop()
    # an hour of minutes were missed: next_fire an hour ago
    while True:
        now = int(time.time())
        if 15 <= now % 60 <= 38:
            break
        time.sleep(0.5)
    boundary = now // 60 * 60
    psql(f"update schedules set base = {boundary - 3600}, next_fire = {boundary - 3600}, last_fired = 0 where id = {sid}")
    svc = Svc(d, seconds=False).start()
    check("7. cron-catchup is on by default", svc.get("/config")[1]["cron-catchup"] == 1)
    check("7. after a stop of an hour, the missed minutes make one event", wait_for(lambda: len(fires(d)) >= 1, 6), str(fires(d)))
    time.sleep(2.5)
    got = fires(d)
    check("7. ... exactly one (not sixty), for the last minute before now, with its key", len(got) == 1 and got[0] == (boundary, f"cron:{sid}:{boundary}"), str((got, boundary)))
    row = psql(f"select last_fired, next_fire from schedules where id = {sid}")
    check("7. the table: last_fired is that minute, next_fire the next", row == [(str(boundary), str(boundary + 60))], str(row))
    wait_for(lambda: len(fires(d)) >= 2, 62)
    got = fires(d)
    check("7. and then the next minute fires on time: its scheduled second is the next minute, with its key",
          len(got) == 2 and got[1][0] == boundary + 60 and got[1][1] == f"cron:{sid}:{boundary + 60}", str(got))
    t_fire = time.time()
    check("7. ... a minute boundary (the second is 0), seen within six seconds of it", got[1][0] % 60 == 0 and t_fire - got[1][0] < 6, str((got, t_fire)))
    svc.stop()
    # cron-catchup 0: the missed minutes are skipped
    reset()
    shutil.rmtree(d)
    d = fresh_dir()
    svc = Svc(d, seconds=False, catchup=0).start()
    r, c = svc.create({"expr": "* * * * *", "type": "minute"})
    sid = c["id"]
    svc.stop()
    while True:
        now = int(time.time())
        if 15 <= now % 60 <= 38:
            break
        time.sleep(0.5)
    boundary = now // 60 * 60
    psql(f"update schedules set base = {boundary - 3600}, next_fire = {boundary - 3600}, last_fired = 0 where id = {sid}")
    svc = Svc(d, seconds=False, catchup=0).start()
    check("7. cron-catchup 0 is what /config says", svc.get("/config")[1]["cron-catchup"] == 0)
    check("7. cron-catchup 0: the missed minutes are skipped, the next_fire moves to the next minute and last_fired stays 0",
          wait_for(lambda: psql(f"select next_fire from schedules where id = {sid}") == [(str(boundary + 60),)], 6) and fires(d) == [] and psql(f"select last_fired from schedules where id = {sid}") == [("0",)],
          str((psql("select last_fired, next_fire from schedules"), fires(d), boundary)))
    check("7. ... and it says so in /stats", svc.stats()["cron_skipped"] == 1 and svc.stats()["cron_fired"] == 0, str(svc.stats()))
    svc.stop()
    shutil.rmtree(d)
    reset()


def stage8():
    """Seconds mode: `*/5 * * * * *` after a stop of an hour."""
    for catchup in (1, 0):
        reset()
        d = fresh_dir()
        svc = Svc(d, catchup=catchup).start()
        r, c = svc.create({"expr": "*/5 * * * * *", "type": "t"})
        sid = c["id"]
        svc.stop()
        gone = int(time.time()) // 5 * 5 - 3600
        psql(f"update schedules set base = {gone}, next_fire = {gone}, last_fired = 0 where id = {sid}")
        t0 = int(time.time())
        svc = Svc(d, catchup=catchup).start()
        t1 = int(time.time())
        time.sleep(4.5)
        got = [s for s, _ in fires(d)]
        check(f"8. catchup {catchup}: every second is a multiple of 5, none twice, one after another with no gap", len(set(got)) == len(got) and all(s % 5 == 0 for s in got) and all(b - a == 5 for a, b in zip(got, got[1:])), str(got))
        # The first tick judged the schedule at `now`, between t0 and t1 + 3: a scheduled second older than GRACE seconds then is missed, a younger one is not.
        if catchup:
            check("8. catchup 1: the first fire is the one for the hour that was missed: the last multiple of 5 that was more than the grace period old",
                  t0 - 17 < got[0] <= t1 + 3 - GRACE - 1, str((got, t0, t1)))
            check("8. catchup 1: then the ones within the grace period, each on its own", len(got) >= 3 and got[1] >= t0 - GRACE, str((got, t0)))
        else:
            check("8. catchup 0: nothing for the hour that was missed: the first fire is within the grace period of the start", len(got) >= 2 and got[0] >= t0 - GRACE, str((got, t0, t1)))
        check(f"8. catchup {catchup}: not an hour of fires (720): {len(got)} in all", len(got) <= 6, str(got))
        st = svc.stats()
        check(f"8. catchup {catchup}: /stats cron_skipped is {0 if catchup else 1}, cron_fired {len(got)}", st["cron_skipped"] == (0 if catchup else 1) and st["cron_fired"] == len(got) and st["cron_errors"] == 0, str(st))
        svc.stop()
        shutil.rmtree(d)
    reset()


class HoldProxy(PgProxy):
    """A PgProxy that, when armed, swallows the one request that moves a schedule's next fire (`advance_schedule`) and says so."""

    def __init__(self, host, port):
        self.armed = False
        self.swallowed = threading.Event()
        super().__init__(host, port)

    def _pipe(self, a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                if self.mode == "hold":
                    continue
                if self.armed and b"advance_schedule" in data and a in self.client_sides:
                    self.armed = False
                    self.swallowed.set()
                    continue
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass

    @property
    def client_sides(self):
        return [c for c in self.conns[0::2]]


def stage9():
    reset()
    d = fresh_dir()
    proxy = HoldProxy(PG_HOST, PG_PORT)
    svc = Svc(d, shim=True, pg_port=proxy.port)
    svc.start()
    r, c = svc.create({"expr": "*/2 * * * * *", "type": "t", "body": {"round": 0}})
    sid = c["id"]
    check("9. a schedule through the proxy", r == 201, str((r, c)))
    wait_for(lambda: len(fires(d)) >= 2, 8)
    stops = []
    for rnd in range(1, 7):
        proxy.swallowed.clear()
        proxy.armed = True
        got = proxy.swallowed.wait(10)
        # The request that tells the database is swallowed: the event is already in the log (it is flushed first). Kill the service now.
        svc.kill9(power=rnd % 2 == 0)
        proxy.armed = False
        held = fires(d)[-1][0]
        row = psql(f"select next_fire from schedules where id = {sid}")
        stops.append((got, held, int(row[0][0])))
        # 6 is a long stop: past the grace period, so that some of the fires it missed are skipped
        if rnd == 6:
            time.sleep(GRACE + 6)
        svc = Svc(d, shim=True, pg_port=proxy.port)
        svc.args[svc.args.index("--port") + 1] = str(svc.port)
        svc.start()
        n = len(fires(d))
        wait_for(lambda: len(fires(d)) >= n + 2, 10)
    check("9. each round, the update was swallowed after the event was in the log (the table was one behind the log)", all(g and held >= nf for g, held, nf in stops), str(stops))
    got = fires(d)
    secs = [s for s, _ in got]
    check("9. no scheduled second has two events (the restart did not fire the held one again)", len(set(secs)) == len(secs), str([s for s in secs if secs.count(s) > 1]))
    check("9. every event has its own key, cron:<id>:<second>", all(k == f"cron:{sid}:{s}" for s, k in got), str(got[:3]))
    gaps = [(a, b) for a, b in zip(secs, secs[1:]) if b - a != 2]
    check("9. the seconds are every second one with no gap, except the one stop longer than the grace period (only the last fire it missed is made)", len(gaps) == 1 and 2 < gaps[0][1] - gaps[0][0] <= GRACE + 8, str((gaps, secs)))
    check("9. the held fires were each made once: the keys in the log are all different", len({k for _, k in got}) == len(got))
    check("9. the log has no other event", len(events(d)) == len(got), str(len(events(d))))
    svc.stop()
    proxy.close()
    shutil.rmtree(d)
    reset()


def stage10():
    reset()
    d = fresh_dir()
    svc = Svc(d, shim=True)
    svc.start()
    r, c = svc.create({"expr": "* * * * * *", "type": "t", "body": {"chaos": True}})
    sid = c["id"]
    rng = random.Random(11)
    end = time.time() + 30
    kills = near = 0
    path = os.path.join(d, "events.seg")
    while time.time() < end:
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if kills % 3 == 2:
            # at a random instant
            time.sleep(rng.uniform(0.2, 1.5))
        else:
            # as soon as the log grows: the event was just appended, and is flushed, and the database is told, in the next milliseconds
            wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > size, 3, step=0.0003)
            time.sleep(rng.uniform(0.0, 0.004))
            near += 1
        svc.kill9(power=True)
        kills += 1
        time.sleep(rng.uniform(0.0, 0.3))
        s2 = Svc(d, shim=True)
        s2.args[s2.args.index("--port") + 1] = str(s2.port)
        s2.start()
        svc = s2
    time.sleep(2.5)
    svc.stop()
    got = fires(d)
    secs = [s for s, _ in got]
    check(f"10. {kills} kills as power cuts ({near} of them within milliseconds of an event reaching the log) under a schedule of every second: {len(got)} events", kills >= 12 and len(got) >= 12, str((kills, len(got))))
    check("10. no scheduled second has two events", len(set(secs)) == len(secs), str([s for s in secs if secs.count(s) > 1]))
    check("10. every event has its own key", all(k == f"cron:{sid}:{s}" for s, k in got))
    check("10. one event per scheduled second: no second is missing between the first and the last", secs == list(range(secs[0], secs[-1] + 1)), str([s for s in range(secs[0], secs[-1] + 1) if s not in set(secs)][:10]))
    shutil.rmtree(d)
    reset()


def stage11():
    # The schedules' keys have an index of their own, a quarter of `idem-keys` (docs/retention.md section 7), so clients' keys cannot fill it and it
    # cannot be filled by them. `--idem-keys 64` makes it 16 keys, which a schedule of every second fills in 16 seconds.
    reset()
    d = fresh_dir()
    svc = Svc(d, extra=["--idem-keys", "64"]).start()
    codes = [chaos.post(svc.port, b'{"type":"t"}', timeout=5)[0] for _ in range(1)]
    refused = []
    for i in range(64):
        c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=30)
        c.request("POST", "/events", body=b'{"type":"t"}', headers={"Idempotency-Key": f"full-{i}"})
        resp = c.getresponse()
        resp.read()
        if resp.status != 202:
            refused.append((i, resp.status))
    check("11. 64 clients' keys held (the whole of that index)", not refused and svc.stats()["keys"] == 64, str((refused[:3], svc.stats())))
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=30)
    c.request("POST", "/events", body=b'{"type":"t"}', headers={"Idempotency-Key": "one-too-many"})
    check("11. ... and the 65th is a 507", c.getresponse().status == 507)
    r, c = svc.create({"expr": "* * * * * *", "type": "t"})
    check("11. a schedule is made", r == 201, str((r, c)))
    ok = wait_for(lambda: svc.stats()["cron_keys"] == 16, 40)
    st = svc.stats()
    check("11. with the clients' index full the schedule still fires: its keys have their own (%d held)" % st["cron_keys"], ok and len(fires(d)) == 16, str((st, len(fires(d)))))
    time.sleep(3.5)
    st = svc.stats()
    check("11. with ITS index full (16) a schedule does not fire (the fire would have no key to be made once by)", len(fires(d)) == 16 and st["cron_fired"] == 16 and st["cron_errors"] >= 2, str(st))
    check("11. and its row is untouched, due again at every cycle", psql(f"select last_fired from schedules where id = {c['id']}") == [(str(fires(d)[-1][0]),)])
    check("11. the service goes on: an event without a key is stored", chaos.post(svc.port, b'{"type":"t"}')[0] == 202)
    svc.stop()
    shutil.rmtree(d)
    reset()


def main():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    for n, fn in enumerate([stage1, stage2, stage3, stage4, stage5, stage6, stage7, stage8, stage9, stage10, stage11], 1):
        if n in STAGES:
            print(f"-- stage {n}", flush=True)
            fn()
    reset()
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
