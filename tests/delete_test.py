#!/usr/bin/env python3
"""DELETE /endpoints/:id (docs/design.md section 25.5, slice 4).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/delete_test.py build/hooks

The receivers verify the Standard Webhooks signature themselves; the outcome log (delivery.seg) is read with this file's own reader, so the
records the service writes (kinds 10 and 11 above all) are checked and not only the service's agreement with itself.

   1. who may call it and what it may name: 403 with no admin-token, 401 for a missing or wrong one, 503 with no database, 404 for an id
      that is not there, 400 for one that is not a number; each leaves the endpoint, the row and the log as they were; the method is listed
   2. after the delete no new attempt starts (events after it never reach the receiver), the endpoint is gone from `GET /endpoints` at once,
      its rows in `attempts` stay, its slot is free at once (a `removed` record) and a restart does not bring it back
   3. an attempt on the wire finishes and is recorded (log and history, under the endpoint's id); the slot is not reusable until then (a
      `POST /endpoints` in that window gets another slot) and the `removed` record comes after the outcome, not at the request
   3b. an attempt on the wire that fails is recorded as failed and is not tried again
   3c. the only endpoint, deleted with an attempt on the wire: the attempt still ends and the slot is freed
   3d. a replay on the wire is recorded too; 3e. a replay on the wire that fails is recorded and then dropped
   3f. an attempt on the wire answering 410 is a dead letter and does not disable the deleted endpoint
   4. replays that wait are dropped, each with a record, before `removed`; a restart does not bring them back
   5. a database that refuses (a trigger): 503, and nothing changed in the service or the log
   6. a row that was already gone: the endpoint is removed from the service all the same (200 that says so), then 404
   7. one change at a time: a second DELETE, a PATCH and a POST while one waits are 409, and a DELETE while a POST waits is 409
   8. a slot given to a new endpoint inherits nothing of the old one, across a restart: not the cursor, the window, the outcomes, the disabled
      bit or the replays (the log's created/removed sequence is shown); a row added by hand at the restart takes a freed slot and inherits nothing
   9. the history keeps the deleted endpoint's rows and the new endpoint's do not collide with them
  10. the id of a deleted endpoint is not given again (a row added by hand, a row made by the API, all of them deleted)
  11. 62 endpoints: a 63rd is 409; delete one, create one works; 70 times, the live number never above 62; a restart keeps all 62
  12. a restart (kill -9) while an endpoint is draining: the row is gone, the slot is dormant, and `take_slot` reclaims it when it is needed
  12b. ... and a row put back by hand before the next start is the same endpoint resuming, with the replays that were dropped still dropped
  13. a deleted endpoint is sent nothing under load, and the others lose no event
  14. the slowest endpoint is deleted: the others are served either way (they are not held to its window: docs/production.md 0.1)
  15. a database that does not answer in five seconds: 504, nothing changed, and the next start reconciles
  16. dormant slots beside a draining one: the draining slot is not mistaken for a dormant one
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import pgwait  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "correct-horse-battery-staple"
FAILS = []

DELIVERED, FAILED, DEAD, DISABLED, ENABLED = 1, 2, 3, 4, 5
REPLAY, REPLAY_FAILED, REPLAY_DELIVERED, REPLAY_DEAD, CREATED, REMOVED = 6, 7, 8, 9, 10, 11
REASON = 14      # why an attempt failed (docs/design.md section 34.3): written right after the outcome it explains


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    sys.stdout.flush()
    if not ok:
        FAILS.append(name)


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def reset_db():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("drop trigger if exists hooks_test_trigger on endpoints")
    psql("truncate endpoints")
    psql("truncate attempts")
    psql("alter sequence endpoint_ids restart with 0")


def signed_with(headers, body, key):
    mid, ts, sig = headers["webhook-id"], headers["webhook-timestamp"], headers["webhook-signature"]
    want = base64.b64encode(hmac.new(base64.b64decode(key[6:]), f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return "v1," + want in sig.split()


class Receiver:
    """Verifies the signature under any of `keys`; `plan` maps an event number to a list of (delay, status), one per request for that event."""

    def __init__(self, keys=(), plan=None):
        self.seen = []  # (n, signature ok, arrival time)
        self.keys = list(keys)
        self.plan = {n: list(v) for n, v in (plan or {}).items()}
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                n = json.loads(body)["n"]
                ok = any(signed_with(self.headers, body, k) for k in outer.keys)
                with outer.lock:
                    outer.seen.append((n, ok, time.time()))
                    step = outer.plan[n].pop(0) if outer.plan.get(n) else (0, 204)
                if step[0]:
                    time.sleep(step[0])
                self.send_response(step[1])
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def events(self):
        return [n for n, _, _ in self.seen]

    def all_signed(self):
        return all(ok for _, ok, _ in self.seen)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def pg_flags():
    f = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


class Svc:
    pass


def start(d, extra=None, schedule="100", deadline="5000"):
    port = chaos.free_port()
    flags = pg_flags() + ["--admin-token", TOKEN] if extra is None else extra
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--schedule", schedule, "--deadline-ms", deadline, *flags],
                            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    gone = line == "listening" and pgwait.after_listening(proc, lines, flags)
    svc = Svc()
    svc.port, svc.proc, svc.lines, svc.exited, svc.dir = port, proc, lines, line != "listening" or gone, d
    return svc


def stop(svc):
    if not svc.exited:
        svc.proc.terminate()
    svc.proc.wait()


def kill(svc):
    svc.proc.send_signal(signal.SIGKILL)
    svc.proc.wait()


def req(svc, method, path, body=None, token=TOKEN, raw=False, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=20)
    h = {"Authorization": "Bearer " + token} if token is not None else {}
    h.update(headers or {})
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    if raw:
        return r.status, out, hdrs
    return r.status, (json.loads(out) if out else None)


def delete(svc, ident, **kw):
    return req(svc, "DELETE", f"/endpoints/{ident}", **kw)


def create(svc, port, secret_=None, **kw):
    body = {"host": "127.0.0.1", "port": port}
    if secret_:
        body["secret"] = secret_
    return req(svc, "POST", "/endpoints", body, **kw)


def post_event(svc, n):
    chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=10.0)


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


def listing(svc):
    return req(svc, "GET", "/endpoints", token=None)[1]


def cursors(svc):
    return {e["id"]: e["cursor"] for e in listing(svc)}


def stats(svc):
    return req(svc, "GET", "/stats", token=None)[1]


def ids(svc):
    return sorted(e["id"] for e in listing(svc))


# ---- the outcome log, read with this file's own reader ---------------------------------------------------------

def outcomes(d):
    path = os.path.join(d, "delivery.seg")
    if not os.path.exists(path):
        return []
    recs, _ = chaos.read_log(open(path, "rb").read())
    out = []
    for ms, pairs in recs:
        k, e, ev, at, nx = struct.unpack("<5Q", pairs[0][1])
        out.append((k, e, ev, at, nx))
    return out


def of_kind(d, kind):
    return [r for r in outcomes(d) if r[0] == kind]


def created_slot(d, ident):
    got = [r[1] for r in outcomes(d) if r[0] == CREATED and r[2] == ident]
    return got[-1] if got else None


def index_of(rows, pred):
    for i, r in enumerate(rows):
        if pred(r):
            return i
    return None


def tmp():
    return tempfile.mkdtemp(prefix="hooks-delete-")


def add_rows(rows):
    """rows: (id, receiver, secret)"""
    for ident, r, s in rows:
        psql(f"insert into endpoints values ({ident}, '127.0.0.1', {r.port}, '{s}')")


def db_ids():
    return sorted(int(r[0]) for r in psql("select id from endpoints"))


def attempts(where):
    return [tuple(int(x) for x in r) for r in psql(f"select endpoint, event, replay, attempt, outcome, status from attempts where {where} order by endpoint, event, replay, attempt")]


def main():
    # 1. who may call it, and what it may name ---------------------------------------------------------------------
    reset_db()
    s1 = secret()
    r1 = Receiver([s1])
    add_rows([(1, r1, s1)])
    d = tmp()
    svc = start(d)
    log_before = outcomes(d)
    check("1. no token is a 401", delete(svc, 1, token=None)[0] == 401)
    st, body, hdrs = req(svc, "DELETE", "/endpoints/1", token=None, raw=True)
    check("1. ... with WWW-Authenticate", hdrs.get("WWW-Authenticate") == "Bearer", str(hdrs))
    check("1. a wrong token is a 401", delete(svc, 1, token="wrong-token-value")[0] == 401)
    check("1. a prefix of the token is a 401", delete(svc, 1, token=TOKEN[:-1])[0] == 401)
    check("1. the token with one byte more is a 401", delete(svc, 1, token=TOKEN + "x")[0] == 401)
    check("1. the check of the token comes before the id: 401 for an unknown id too", delete(svc, 77, token=None)[0] == 401)
    check("1. an id that is not there is a 404", delete(svc, 77)[0] == 404)
    check("1. an id that is not a number is a 400", delete(svc, "abc")[0] == 400)
    check("1. a negative id is a 400", delete(svc, "-1")[0] == 400)
    check("1. an id with a suffix is a 400", delete(svc, "1x")[0] == 400)
    check("1. an id of a million is a 404 (no such endpoint)", delete(svc, 1000000)[0] == 404)
    st, body, hdrs = req(svc, "PUT", "/endpoints/1", raw=True)
    check("1. the path lists DELETE among the methods it takes (a PUT is a 405 that says so)", st == 405 and "DELETE" in hdrs.get("Allow", ""), str((st, hdrs)))
    check("1. each refusal left the endpoint, the row and the log as they were",
          ids(svc) == [1] and db_ids() == [1] and outcomes(d) == log_before and stats(svc)["draining"] == 0, str((ids(svc), db_ids(), outcomes(d))))
    stop(svc)
    svc = start(d, extra=pg_flags())
    check("1. with no admin-token configured a DELETE is a 403, with a token or without, whatever the id",
          delete(svc, 1)[0] == 403 and delete(svc, 1, token=None)[0] == 403 and delete(svc, 77)[0] == 403 and delete(svc, "abc")[0] == 403)
    stop(svc)
    svc = start(d, extra=["--admin-token", TOKEN])
    st, out = delete(svc, 1)
    check("1. with no database named a DELETE is a 503 that says so", st == 503 and "--pg-host" in json.dumps(out), str((st, out)))
    stop(svc)
    svc = start(d)
    check("1. ... and all of it changed nothing: the endpoint is still deliverable", ids(svc) == [1] and db_ids() == [1])
    post_event(svc, 1)
    check("1. ... it gets the event, signed", wait_for(lambda: r1.events() == [1] and r1.all_signed(), 5), str(r1.seen))
    stop(svc)
    shutil.rmtree(d)

    # 2. no new attempt after it ------------------------------------------------------------------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(1, ra, sa), (2, rb, sb)])
    d = tmp()
    svc = start(d)
    for n in (1, 2, 3):
        post_event(svc, n)
    check("2. both get events 1 to 3", wait_for(lambda: sorted(ra.events()) == [1, 2, 3], 8) and wait_for(lambda: sorted(rb.events()) == [1, 2, 3], 8))
    check("2. ... and both cursors are at 3", wait_for(lambda: cursors(svc) == {1: 3, 2: 3}, 5), str(cursors(svc)))
    slot_a = created_slot(d, 1)
    st, out = delete(svc, 1)
    check("2. DELETE is a 200 that says deleted, not draining", st == 200 and out == {"id": 1, "deleted": True, "draining": False}, str((st, out)))
    check("2. the endpoint is out of GET /endpoints at once", ids(svc) == [2], str(listing(svc)))
    check("2. ... GET /endpoints/1 is a 404 and /endpoints/2 is not", req(svc, "GET", "/endpoints/1", token=None)[0] == 404 and req(svc, "GET", "/endpoints/2", token=None)[0] == 200)
    check("2. ... the row is gone from the database and /stats counts one endpoint", db_ids() == [2] and stats(svc)["endpoints"] == 1 and stats(svc)["draining"] == 0, str((db_ids(), stats(svc))))
    check("2. ... its slot is free at once: a `removed` record for it, and nothing for slot of 2",
          of_kind(d, REMOVED) == [(REMOVED, slot_a, 0, 0, 0)], str(of_kind(d, REMOVED)))
    for n in range(4, 9):
        post_event(svc, n)
    check("2. events 4 to 8 reach the other endpoint", wait_for(lambda: sorted(rb.events()) == list(range(1, 9)), 8) and wait_for(lambda: cursors(svc) == {2: 8}, 5), str((rb.events(), cursors(svc))))
    time.sleep(0.8)
    check("2. ... and never the deleted one", sorted(ra.events()) == [1, 2, 3], str(ra.seen))
    check("2. its rows in `attempts` are kept (three, all delivered)", wait_for(lambda: [r[:3] + r[4:5] for r in attempts("endpoint = 1")] == [(1, 1, 0, 1), (1, 2, 0, 1), (1, 3, 0, 1)], 5), str(attempts("endpoint = 1")))
    check("2. a second DELETE is a 404", delete(svc, 1)[0] == 404)
    check("2. a replay to it is a 404, and a replay to all goes to the one that is left",
          req(svc, "POST", "/events/4/replay/1")[0] == 404 and req(svc, "POST", "/events/4/replay") == (202, {"event": 4, "endpoints": [2]}))
    st, out = req(svc, "POST", "/endpoints/1/enable")
    check("2. enabling it is a 404", st == 404)
    stop(svc)
    records = outcomes(d)
    svc = start(d)
    time.sleep(0.5)
    check("2. a restart does not bring it back", ids(svc) == [2] and cursors(svc) == {2: 8}, str(listing(svc)))
    post_event(svc, 9)
    check("2. ... nor send it anything", wait_for(lambda: 9 in rb.events(), 5) and sorted(ra.events()) == [1, 2, 3], str(ra.seen))
    stop(svc)
    check("2. ... and writes no slot record", [r for r in outcomes(d)[: len(records)]] == records and not [r for r in outcomes(d)[len(records):] if r[0] in (CREATED, REMOVED)], str(outcomes(d)[len(records):]))
    check("2. every delivery was signed", ra.all_signed() and rb.all_signed())
    shutil.rmtree(d)

    # 3. an attempt on the wire ------------------------------------------------------------------------------------
    reset_db()
    sa, sb, sc = secret(), secret(), secret()
    ra, rb, rc = Receiver([sa], plan={1: [(1.4, 204)]}), Receiver([sb]), Receiver([sc])
    # ids above 62, so that a new endpoint takes the lowest *free slot* and not the slot of its own number: A has slot 0, B slot 1
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    slot_a, slot_b = created_slot(d, 100), created_slot(d, 101)
    check("3. (ids 100 and 101 have slots 0 and 1)", (slot_a, slot_b) == (0, 1), str((slot_a, slot_b)))
    post_event(svc, 1)
    check("3. the first receiver has the event and holds its answer", wait_for(lambda: ra.events() == [1], 5), str(ra.seen))
    t0 = time.time()
    st, out = delete(svc, 100)
    check("3. the delete is accepted while the attempt is on the wire, and says it is draining", st == 200 and out == {"id": 100, "deleted": True, "draining": True}, str((st, out)))
    check("3. the endpoint is out of GET /endpoints at once, and /stats says one is draining", ids(svc) == [101] and stats(svc)["draining"] == 1 and stats(svc)["endpoints"] == 1, str((listing(svc), stats(svc))))
    check("3. its row is gone", db_ids() == [101])
    post_event(svc, 2)
    post_event(svc, 3)
    check("3. events 2 and 3 go to the other endpoint", wait_for(lambda: sorted(rb.events()) == [1, 2, 3], 5), str(rb.seen))
    check("3. no `removed` record while the attempt is on the wire", not of_kind(d, REMOVED), str(of_kind(d, REMOVED)))
    st, out = create(svc, rc.port)
    check("3. a POST /endpoints in the window is a 201 and does not get the slot that is draining",
          st == 201 and created_slot(d, out["id"]) not in (None, slot_a) and created_slot(d, out["id"]) == 2, str((st, out, of_kind(d, CREATED))))
    new_id = out["id"]
    rc.keys = [out["secret"]]
    check("3. ... nor did any outcome of the old endpoint reach it: its cursor is where it started", cursors(svc)[new_id] == out["cursor"], str((cursors(svc), out)))
    check("3. the attempt on the wire ends: delivered, recorded in the log under its slot",
          wait_for(lambda: (DELIVERED, slot_a, 1, 1, 0) in of_kind(d, DELIVERED), 6), str(outcomes(d)))
    elapsed = time.time() - t0
    check("3. ... only after the receiver answered (not at the request)", elapsed > 0.9, str(elapsed))
    check("3. then the slot is freed: a `removed` record, after the delivery",
          wait_for(lambda: of_kind(d, REMOVED) == [(REMOVED, slot_a, 0, 0, 0)], 5)
          and index_of(outcomes(d), lambda r: r == (REMOVED, slot_a, 0, 0, 0)) > index_of(outcomes(d), lambda r: r == (DELIVERED, slot_a, 1, 1, 0)), str(outcomes(d)))
    check("3. the history has the row under the endpoint's id, delivered with the receiver's status",
          wait_for(lambda: attempts("endpoint = 100") == [(100, 1, 0, 1, 1, 204)], 5), str(attempts("endpoint = 100")))
    check("3. /stats: nothing draining any more, three endpoints' worth of work counted", stats(svc)["draining"] == 0 and stats(svc)["endpoints"] == 2, str(stats(svc)))
    time.sleep(0.4)
    check("3. the deleted endpoint's receiver saw event 1 once and nothing else, signed", ra.events() == [1] and ra.all_signed(), str(ra.seen))
    st, out = create(svc, rc.port)
    check("3. now a POST /endpoints takes the freed slot (the lowest free)", st == 201 and created_slot(d, out["id"]) == slot_a, str((st, out, of_kind(d, CREATED))))
    third = out
    rc2 = Receiver([third["secret"]])
    psql(f"update endpoints set port = {rc2.port} where id = {third['id']}")
    stop(svc)
    svc = start(d)
    post_event(svc, 4)
    check("3. after a restart the endpoints are 101, the one made in the window and the one that took the slot; each gets event 4 once",
          ids(svc) == sorted([101, new_id, third["id"]]) and wait_for(lambda: 4 in rb.events() and 4 in rc.events() and 4 in rc2.events(), 8), str((ids(svc), rb.events(), rc.events(), rc2.events())))
    stop(svc)
    shutil.rmtree(d)

    # 3b. the attempt on the wire fails: recorded, not tried again
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa], plan={1: [(0.8, 500)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d, schedule="100,100,100")
    post_event(svc, 1)
    check("3b. the attempt is on the wire", wait_for(lambda: ra.events() == [1], 5), str(ra.seen))
    st, out = delete(svc, 100)
    check("3b. deleted, draining", st == 200 and out["draining"] is True, str((st, out)))
    check("3b. the attempt ends in a failure that is recorded (log: failed, 1 attempt; history: outcome 2, status 500)",
          wait_for(lambda: of_kind(d, FAILED) and attempts("endpoint = 100") == [(100, 1, 0, 1, 2, 500)], 6), str((outcomes(d), attempts("endpoint = 100"))))
    check("3b. and then the slot is freed", wait_for(lambda: of_kind(d, REMOVED) == [(REMOVED, 0, 0, 0, 0)], 5), str(outcomes(d)))
    time.sleep(1.0)
    check("3b. the failed attempt is not tried again (its schedule says 100 ms)", ra.events() == [1], str(ra.seen))
    stop(svc)
    shutil.rmtree(d)

    # 3c. the only endpoint, deleted with an attempt on the wire
    reset_db()
    sa = secret()
    ra = Receiver([sa], plan={1: [(1.0, 204)]})
    add_rows([(100, ra, sa)])
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: ra.events() == [1], 5)
    st, out = delete(svc, 100)
    check("3c. the only endpoint is deleted while an attempt is on the wire", st == 200 and out["draining"] is True and listing(svc) == [] and stats(svc)["endpoints"] == 0, str((st, out)))
    check("3c. the attempt still ends: recorded as delivered, and the slot is freed (the loop keeps settling attempts with no endpoint left)",
          wait_for(lambda: (DELIVERED, 0, 1, 1, 0) in of_kind(d, DELIVERED) and of_kind(d, REMOVED) == [(REMOVED, 0, 0, 0, 0)] and attempts("endpoint = 100") == [(100, 1, 0, 1, 1, 204)], 6),
          str((outcomes(d), stats(svc))))
    check("3c. /stats: nothing draining", stats(svc)["draining"] == 0)
    rn = Receiver()
    st, out = create(svc, rn.port)
    rn.keys = [out["secret"]]
    post_event(svc, 2)
    check("3c. a new endpoint in the service that had none gets what comes after", st == 201 and wait_for(lambda: rn.events() == [2] and rn.all_signed(), 6), str((st, out, rn.seen)))
    stop(svc)
    shutil.rmtree(d)

    # 3d. a replay on the wire
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa], plan={1: [(0, 204), (1.2, 204)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: cursors(svc) == {100: 1, 101: 1}, 5)
    st, out = req(svc, "POST", "/events/1/replay/100")
    check("3d. a replay to the endpoint is on the wire (the receiver holds it)", st == 202 and wait_for(lambda: ra.events() == [1, 1], 5), str((st, out, ra.seen)))
    st, out = delete(svc, 100)
    check("3d. deleted while it is on the wire: draining", st == 200 and out["draining"] is True, str((st, out)))
    check("3d. the replay's outcome is recorded (log: replay delivered; history: a replay row), and then the slot is freed; a replay on the wire is not dropped",
          wait_for(lambda: of_kind(d, REPLAY_DELIVERED) == [(REPLAY_DELIVERED, 0, 1, 1, 0)] and of_kind(d, REMOVED) == [(REMOVED, 0, 0, 0, 0)], 6)
          and [r[0] for r in outcomes(d) if r[1] == 0] == [CREATED, DELIVERED, REPLAY, REPLAY_DELIVERED, REMOVED], str(outcomes(d)))
    check("3d. ... and the history has both rows of event 1 under 100", wait_for(lambda: attempts("endpoint = 100") == [(100, 1, 0, 1, 1, 204), (100, 1, 1, 1, 1, 204)], 5), str(attempts("endpoint = 100")))
    check("3d. no replay is left", stats(svc)["replays"] == 0, str(stats(svc)))
    stop(svc)
    shutil.rmtree(d)

    # 3e. a replay on the wire that fails: recorded, then dropped (not tried again)
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa], plan={1: [(0, 204), (1.0, 500)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d, schedule="100,100,100")
    post_event(svc, 1)
    wait_for(lambda: cursors(svc) == {100: 1, 101: 1}, 5)
    req(svc, "POST", "/events/1/replay/100")
    wait_for(lambda: ra.events() == [1, 1], 5)
    st, out = delete(svc, 100)
    check("3e. deleted while a replay is on the wire", st == 200 and out["draining"] is True and stats(svc)["replays"] == 1, str((st, out, stats(svc))))
    check("3e. the failure is recorded (replay_failed, and why), the replay is dropped (replay_dead), then the slot is freed, in that order",
          wait_for(lambda: of_kind(d, REMOVED), 6) and [r[0] for r in outcomes(d) if r[1] == 0][-4:] == [REPLAY_FAILED, REASON, REPLAY_DEAD, REMOVED], str(outcomes(d)))
    check("3e. no replay waits, and it is not tried again", stats(svc)["replays"] == 0, str(stats(svc)))
    time.sleep(0.8)
    check("3e. ... (the receiver saw the event twice: the delivery and the replay)", ra.events() == [1, 1], str(ra.seen))
    check("3e. the history has the failed replay row under the id", attempts("endpoint = 100 and replay = 1") == [(100, 1, 1, 1, 2, 500)], str(attempts("endpoint = 100")))
    stop(svc)
    shutil.rmtree(d)

    # 3f. an attempt on the wire answers 410: it is a dead letter, and the deleted endpoint is not disabled (it has nothing to disable)
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa], plan={1: [(0.8, 410)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: ra.events() == [1], 5)
    delete(svc, 100)
    check("3f. the 410 is recorded as dead (and why), with no `disabled` record, then the slot is freed",
          wait_for(lambda: of_kind(d, REMOVED), 6) and [r[0] for r in outcomes(d) if r[1] == 0] == [CREATED, DEAD, REASON, REMOVED], str(outcomes(d)))
    stop(svc)
    shutil.rmtree(d)

    # 4. replays that wait are dropped ----------------------------------------------------------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa], plan={2: [(0, 410)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    for n in (1, 2, 3):
        post_event(svc, n)
    check("4. a 410 disables the first endpoint", wait_for(lambda: any(e["disabled"] for e in listing(svc)), 6), str(listing(svc)))
    wait_for(lambda: sorted(rb.events()) == [1, 2, 3], 5)
    st1 = req(svc, "POST", "/events/1/replay/100")
    st2 = req(svc, "POST", "/events/3/replay/100")
    check("4. two replays wait for the disabled endpoint", st1[0] == 202 and st2[0] == 202 and stats(svc)["replays"] == 2, str((st1, st2, stats(svc))))
    seen_before = len(ra.seen)
    st, out = delete(svc, 100)
    check("4. the delete drops them at once", st == 200 and out["draining"] is False and stats(svc)["replays"] == 0, str((st, out, stats(svc))))
    rows = [r for r in outcomes(d) if r[1] == 0]
    check("4. each has its record: replay_dead for events 1 and 3, then `removed`, in that order",
          [r[:3] for r in rows[-3:]] == [(REPLAY_DEAD, 0, 1), (REPLAY_DEAD, 0, 3), (REMOVED, 0, 0)] and [r[:3] for r in rows if r[0] == REPLAY] == [(REPLAY, 0, 1), (REPLAY, 0, 3)], str(rows[-6:]))
    check("4. the receiver of the deleted endpoint was never sent a replay", len(ra.seen) == seen_before, str(ra.seen))
    stop(svc)
    svc = start(d)
    check("4. after a restart: no replay waits, only the other endpoint", stats(svc)["replays"] == 0 and ids(svc) == [101], str((stats(svc), listing(svc))))
    # the row comes back by hand: the slot was freed (removed), so it is a new endpoint: not disabled, no replay, starting at the others' cursor
    add_rows([(100, ra, sa)])
    stop(svc)
    svc = start(d)
    check("4. a row put back by hand is a new endpoint: not disabled, no replay, no event sent",
          stats(svc)["replays"] == 0 and not [e for e in listing(svc) if e["id"] == 100 and e["disabled"]] and len(ra.seen) == seen_before, str((stats(svc), listing(svc), ra.seen)))
    stop(svc)
    shutil.rmtree(d)

    # 5. a database that refuses ------------------------------------------------------------------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    psql("create or replace function hooks_test_refuse() returns trigger as $$ begin raise exception 'no'; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before delete on endpoints for each row execute function hooks_test_refuse()")
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: ra.events() == [1] and rb.events() == [1], 5)
    wait_for(lambda: cursors(svc) == {100: 1, 101: 1}, 5)
    log_before = outcomes(d)
    st, out = delete(svc, 100)
    check("5. a database that refuses the delete is a 503", st == 503, str((st, out)))
    check("5. nothing changed: the endpoint is listed, enabled, the row is there, nothing draining, the log has no new record",
          ids(svc) == [100, 101] and db_ids() == [100, 101] and stats(svc)["draining"] == 0 and outcomes(d) == log_before, str((listing(svc), db_ids(), outcomes(d), log_before)))
    post_event(svc, 2)
    check("5. the endpoint still gets events", wait_for(lambda: ra.events() == [1, 2] and rb.events() == [1, 2], 5), str((ra.seen, rb.seen)))
    psql("drop trigger hooks_test_trigger on endpoints")
    st, out = delete(svc, 100)
    check("5. and the next DELETE works", st == 200 and ids(svc) == [101] and db_ids() == [101], str((st, out)))
    stop(svc)
    shutil.rmtree(d)

    # 6. a row that was already gone ---------------------------------------------------------------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: ra.events() == [1] and rb.events() == [1], 5)
    psql("delete from endpoints where id = 100")
    st, out = req(svc, "PATCH", "/endpoints/100", {"port": 9})
    check("6. (a PATCH of a row that is gone is a 404 that says so, and the service goes on delivering to it)", st == 404 and ids(svc) == [100, 101], str((st, out)))
    st, out = delete(svc, 100)
    check("6. a DELETE of a row that is gone is a 200 that says the row was already gone", st == 200 and out.get("row") == "was already gone" and out["deleted"] is True, str((st, out)))
    check("6. ... and the endpoint is removed from the service all the same (the database is the owner and says it is not there)", ids(svc) == [101], str(listing(svc)))
    post_event(svc, 2)
    check("6. ... so it is sent nothing", wait_for(lambda: rb.events() == [1, 2], 5) and ra.events() == [1], str((ra.seen, rb.seen)))
    check("6. ... its slot is freed (a `removed` record)", of_kind(d, REMOVED) == [(REMOVED, 0, 0, 0, 0)], str(of_kind(d, REMOVED)))
    check("6. ... and a second DELETE is a 404", delete(svc, 100)[0] == 404)
    stop(svc)
    shutil.rmtree(d)

    # 7. one change at a time ----------------------------------------------------------------------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    psql("create or replace function hooks_test_slow() returns trigger as $$ begin perform pg_sleep(1.5); return old; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before delete on endpoints for each row execute function hooks_test_slow()")
    d = tmp()
    svc = start(d)
    first = {}

    def slow_delete():
        first["r"] = delete(svc, 100)

    t = threading.Thread(target=slow_delete)
    t.start()
    time.sleep(0.4)
    st2, out2 = delete(svc, 101)
    st3, out3 = create(svc, ra.port)
    st4, out4 = req(svc, "PATCH", "/endpoints/101", {"port": 9})
    t.join()
    check("7. a second DELETE while one waits is a 409", st2 == 409, str((st2, out2)))
    check("7. a POST /endpoints while it waits is a 409", st3 == 409, str((st3, out3)))
    check("7. a PATCH while it waits is a 409", st4 == 409, str((st4, out4)))
    check("7. the first completes, and the refused ones changed nothing", first["r"][0] == 200 and ids(svc) == [101] and db_ids() == [101], str((first, listing(svc), db_ids())))
    psql("drop trigger hooks_test_trigger on endpoints")
    psql("create or replace function hooks_test_slow() returns trigger as $$ begin perform pg_sleep(1.5); return new; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before insert on endpoints for each row execute function hooks_test_slow()")
    first2 = {}

    def slow_create():
        first2["r"] = create(svc, rb.port)

    t = threading.Thread(target=slow_create)
    t.start()
    time.sleep(0.4)
    st5, out5 = delete(svc, 101)
    t.join()
    check("7. a DELETE while a POST waits is a 409, and the POST completes", st5 == 409 and first2["r"][0] == 201 and 101 in ids(svc), str((st5, out5, first2, listing(svc))))
    psql("drop trigger hooks_test_trigger on endpoints")
    stop(svc)
    shutil.rmtree(d)

    # 8. a slot reused inherits nothing, across a restart ------------------------------------------------------------
    reset_db()
    s98, s99 = secret(), secret()
    # event 2 fails at 99 and waits an hour (its cursor stays at 1, event 3 is final above it); event 4 is a 410 (dead, and 99 is disabled);
    # a replay then waits for the disabled endpoint
    r98, r99 = Receiver([s98]), Receiver([s99], plan={2: [(0, 500)], 4: [(0, 410)]})
    add_rows([(98, r98, s98), (99, r99, s99)])
    d = tmp()
    svc = start(d, schedule="3600000")
    check("8. (98 has slot 0 and 99 slot 1)", (created_slot(d, 98), created_slot(d, 99)) == (0, 1), str(of_kind(d, CREATED)))
    for n in range(1, 5):
        post_event(svc, n)
    check("8. endpoint 99 reaches the state to be inherited: cursor 1, disabled", wait_for(lambda: cursors(svc).get(99) == 1 and [e for e in listing(svc) if e["id"] == 99][0]["disabled"], 8), str((listing(svc), r99.seen)))
    post_event(svc, 5)  # after the 410: 99 is disabled and is not sent it (posted with the others, it could have been attempted before the 410 came back)
    wait_for(lambda: sorted(r98.events()) == [1, 2, 3, 4, 5], 5)
    st, out = req(svc, "POST", "/events/1/replay/99")
    check("8. ... with a replay waiting", st == 202 and stats(svc)["replays"] == 1, str((st, stats(svc))))
    wait_for(lambda: cursors(svc).get(98) == 5, 5)
    st, out = delete(svc, 99)
    check("8. delete 99", st == 200 and out["draining"] is False, str((st, out)))
    sn = secret()
    rn = Receiver([sn])
    st, new = create(svc, rn.port, sn)
    check("8. the new endpoint is id 100 and takes slot 1, starting at the last event (5)", st == 201 and new["id"] == 100 and new["cursor"] == 5 and created_slot(d, 100) == 1, str((st, new, of_kind(d, CREATED))))
    seq = [r for r in outcomes(d) if r[1] == 1 and r[0] in (CREATED, REMOVED, DISABLED, REPLAY, REPLAY_DEAD, FAILED, DELIVERED, DEAD)]
    check("8. the log's story of slot 1: created for 99, its outcomes (a failure, a 410) and the disable, then replay, replay dropped, removed, created for 100",
          seq[0][0] == CREATED and [r[0] for r in seq[-4:]] == [REPLAY, REPLAY_DEAD, REMOVED, CREATED]
          and {FAILED, DEAD, DISABLED} <= {r[0] for r in seq[1:-4]}, str(seq))
    check("8. ... the first created says id 99, the last id 100 and start 5", seq[0][2] == 99 and seq[-1][2:4] == (100, 5), str((seq[0], seq[-1])))
    check("8. the new endpoint inherits nothing: enabled, no replay, cursor 5", stats(svc)["replays"] == 0 and [e for e in listing(svc) if e["id"] == 100] == [{"id": 100, "port": rn.port, "scheme": "http", "cursor": 5, "disabled": False, "paused": False, "failing_since": 0, "types": [], "headers": [], "secret_old_until": 0, "concurrency": 8, "rate": 0}], str((listing(svc), stats(svc))))
    # post enough events that the ring of the slot (1,024 cells) would reach the cells the old endpoint left above its cursor (events 3 and 4)
    total = 1040
    for n in range(6, total + 1):
        post_event(svc, n)
    check("8. the new endpoint is sent every event from 6 to 1040: events 1027 and 1028 (the old endpoint's final cells 3 and 4 above its cursor) too",
          wait_for(lambda: sorted(set(rn.events())) == list(range(6, total + 1)), 60), str((len(rn.events()), sorted(set(range(6, total + 1)) - set(rn.events()))[:10])))
    check("8. ... and the other endpoint is sent all of them", wait_for(lambda: sorted(set(r98.events())) == list(range(1, total + 1)), 60))
    check("8. the deleted endpoint's receiver saw nothing after the delete", sorted(r99.events()) == [1, 2, 3, 4], str(r99.events()))
    check("8. the new one's cursor reaches the end", wait_for(lambda: cursors(svc) == {98: total, 100: total}, 30), str(cursors(svc)))
    n_seen = len(rn.events())
    # state for the next generation: 100 gets a replay waiting (disabled by a 410 on event 1041)
    rn.plan[1041] = [(0, 410)]
    post_event(svc, 1041)
    check("8. (100 is disabled by a 410)", wait_for(lambda: [e for e in listing(svc) if e["id"] == 100][0]["disabled"], 8), str(listing(svc)))
    req(svc, "POST", "/events/6/replay/100")
    check("8. (and has a replay waiting)", stats(svc)["replays"] == 1)
    stop(svc)
    svc = start(d, schedule="3600000")
    check("8. restart: endpoints 98 and 100, 100 still disabled, its replay waiting (nothing was lost), cursors kept",
          ids(svc) == [98, 100] and [e for e in listing(svc) if e["id"] == 100][0]["disabled"] and stats(svc)["replays"] == 1, str((listing(svc), stats(svc))))
    created_before = of_kind(d, CREATED)
    removed_before = of_kind(d, REMOVED)
    time.sleep(0.5)
    st, out = delete(svc, 100)
    check("8. delete 100 (disabled, with a replay waiting)", st == 200 and stats(svc)["replays"] == 0, str((st, out)))
    # a row added by hand: at the restart it takes the freed slot 1 and must inherit nothing of 100 (disabled bit, replay, cursor)
    s150 = secret()
    r150 = Receiver([s150])
    add_rows([(150, r150, s150)])
    stop(svc)
    svc = start(d, schedule="3600000")
    check("8. restart with a row added by hand (150): it takes the slot that 100 left (1)", created_slot(d, 150) == 1 and ids(svc) == [98, 150], str((of_kind(d, CREATED), listing(svc))))
    check("8. ... at the cursor of the slowest endpoint it knows (the other one's), enabled, no replay",
          [e for e in listing(svc) if e["id"] == 150] == [{"id": 150, "port": r150.port, "scheme": "http", "cursor": 1041, "disabled": False, "paused": False, "failing_since": 0, "types": [], "headers": [], "secret_old_until": 0, "concurrency": 8, "rate": 0}] and stats(svc)["replays"] == 0, str((listing(svc), stats(svc))))
    post_event(svc, 1042)
    check("8. ... it gets event 1042 and nothing before it", wait_for(lambda: r150.events() == [1042], 6), str(r150.seen))
    time.sleep(0.5)
    check("8. ... once, and no other receiver is sent an old event again", r150.events() == [1042] and len(rn.events()) == n_seen + 1, str((r150.seen, len(rn.events()), n_seen)))
    check("8. the log: created for 100, then (removed 1) and (created 150) after the delete, in this order",
          index_of(outcomes(d), lambda r: r == (REMOVED, 1, 0, 0, 0)) is not None and
          [r for r in outcomes(d) if r[0] in (CREATED, REMOVED) and r[1] == 1][-3:] == [(CREATED, 1, 100, 5, 0), (REMOVED, 1, 0, 0, 0), (CREATED, 1, 150, 1041, 0)],
          str([r for r in outcomes(d) if r[0] in (CREATED, REMOVED) and r[1] == 1]))
    check("8. (the older slot records are unchanged: %d created, %d removed before the last delete)" % (len(created_before), len(removed_before)),
          [r for r in outcomes(d) if r[0] == CREATED][: len(created_before)] == created_before)
    stop(svc)

    # 9. the history keeps the old rows, the new ones do not collide ------------------------------------------------
    old = attempts("endpoint = 99")
    check("9. the deleted endpoint's rows are all still there: events 1 to 4 (event 2 failed once, event 4 a 410)",
          [(r[1], r[2], r[4], r[5]) for r in old] == [(1, 0, 1, 204), (2, 0, 2, 500), (3, 0, 1, 204), (4, 0, 3, 410)], str(old))
    rows100 = attempts("endpoint = 100")
    check("9. the new endpoint's rows are its own: events 6 and up, none of 1 to 5, and its 410 at 1041",
          rows100 and min(r[1] for r in rows100) == 6 and max(r[1] for r in rows100) == 1041 and (1041, 0, 3, 410) in [(r[1], r[2], r[4], r[5]) for r in rows100], str(rows100[:3]))
    # the same (event, replay, attempt) under both ids: a replay of event 3 (which 99 had) to a new endpoint is a different row
    check("9. no row is under a slot number that is not an id, and none under -1", not attempts("endpoint < 0") and not attempts("endpoint = 1 or endpoint = 0"), str(attempts("endpoint < 100 and endpoint <> 98 and endpoint <> 99")[:3]))
    shutil.rmtree(d)

    # 9b. a replay of an event the deleted endpoint had, to the endpoint that has its slot now: two rows, not one lost
    reset_db()
    s98, s99 = secret(), secret()
    r98, r99 = Receiver([s98]), Receiver([s99])
    add_rows([(98, r98, s98), (99, r99, s99)])
    d = tmp()
    svc = start(d)
    for n in (1, 2, 3):
        post_event(svc, n)
    wait_for(lambda: cursors(svc) == {98: 3, 99: 3}, 6)
    wait_for(lambda: len(attempts("endpoint = 99")) == 3, 6)
    delete(svc, 99)
    sn = secret()
    rn = Receiver([sn])
    st, new = create(svc, rn.port, sn)
    st2, out2 = req(svc, "POST", "/events/3/replay/100")
    check("9b. a replay of event 3 to the new endpoint is sent", st2 == 202 and wait_for(lambda: rn.events() == [3], 5), str((st2, out2, rn.seen)))
    check("9b. the history has (99, event 3, first run) and (100, event 3, replay), both",
          wait_for(lambda: attempts("event = 3 and endpoint >= 99") == [(99, 3, 0, 1, 1, 204), (100, 3, 1, 1, 1, 204)], 6), str(attempts("event = 3")))
    check("9b. the old rows are as they were: three", len(attempts("endpoint = 99")) == 3)
    stop(svc)
    shutil.rmtree(d)

    # 10. a deleted id is not given again ----------------------------------------------------------------------------
    reset_db()
    sx = secret()
    rx = Receiver([sx])
    add_rows([(1, rx, sx), (2, rx, sx), (3, rx, sx)])
    d = tmp()
    svc = start(d)
    check("10. a row added by hand that is the highest (3) is deleted", delete(svc, 3)[0] == 200 and db_ids() == [1, 2])
    st, out = create(svc, rx.port)
    check("10. the next id is not 3 (the delete moved the sequence past it): it is above 3", st == 201 and out["id"] > 3, str((st, out)))
    made = [out["id"]]
    for _ in range(2):
        made.append(create(svc, rx.port)[1]["id"])
    ever = {1, 2, 3, *made}
    mx = max(ever)
    check("10. ids made by the API only rise", made == sorted(made) and len(set(made)) == 3 and made[0] > 3, str(made))
    check("10. the highest, made by the API, is deleted: the next is above it", delete(svc, mx)[0] == 200 and create(svc, rx.port)[1]["id"] > mx)
    for i in list(ids(svc)):
        delete(svc, i)
    check("10. every endpoint deleted: the service has none and the table is empty", listing(svc) == [] and db_ids() == [], str((listing(svc), db_ids())))
    st, out = create(svc, rx.port)
    ever |= {mx, out["id"]}
    check("10. a new one still gets an id above all that were ever given", st == 201 and out["id"] > max(ever - {out["id"]}), str((st, out, sorted(ever))))
    top = out["id"]
    stop(svc)
    svc = start(d)
    check("10. after a restart too", create(svc, rx.port)[1]["id"] > top)
    stop(svc)
    shutil.rmtree(d)

    # 11. 62 endpoints: delete one, create one ----------------------------------------------------------------------
    reset_db()
    keys = {}
    rall = Receiver()
    for i in range(62):
        keys[i] = secret()
        psql(f"insert into endpoints values ({i}, '127.0.0.1', {rall.port}, '{keys[i]}')")
    rall.keys = list(keys.values())
    d = tmp()
    svc = start(d)
    check("11. 62 endpoints are live", len(listing(svc)) == 62 and stats(svc)["endpoints"] == 62)
    st, out = create(svc, rall.port)
    check("11. a 63rd is a 409 that says 62, and no row was stored", st == 409 and "62" in json.dumps(out) and len(db_ids()) == 62, str((st, out)))
    st, out = delete(svc, 10)
    check("11. delete one", st == 200 and stats(svc)["endpoints"] == 61)
    st, out = create(svc, rall.port)
    check("11. create one works, and it has the freed slot", st == 201 and created_slot(d, out["id"]) == 10, str((st, out)))
    rall.keys.append(out["secret"])
    check("11. at 62 again, a POST is a 409", create(svc, rall.port)[0] == 409)
    over = 0
    bad = []
    live = set(ids(svc))
    n_created = 62 + 1
    for k in range(70):
        victim = sorted(live)[(k * 7) % len(live)]
        st, out = delete(svc, victim)
        if st != 200:
            bad.append(("delete", victim, st, out))
            continue
        live.discard(victim)
        st, out = create(svc, rall.port)
        if st != 201:
            bad.append(("create", st, out))
            continue
        rall.keys.append(out["secret"])
        live.add(out["id"])
        n_created += 1
        if len(live) > 62:
            over += 1
        if k % 10 == 0 and create(svc, rall.port)[0] != 409:
            bad.append(("62 and a POST was not a 409", k))
    check("11. 70 times delete one and create one: every request answered as it should", not bad, str(bad[:3]))
    check("11. the live number was never above 62 and is 62 (service, table and what the API says)", over == 0 and len(listing(svc)) == 62 and len(db_ids()) == 62 and ids(svc) == db_ids() == sorted(live), str((over, len(listing(svc)), len(db_ids()))))
    check("11. the log: %d created, 71 removed (70 and the first)" % n_created, len(of_kind(d, CREATED)) == n_created and len(of_kind(d, REMOVED)) == 71, str((len(of_kind(d, CREATED)), len(of_kind(d, REMOVED)))))
    stop(svc)
    svc = start(d)
    check("11. a restart keeps the 62 (and writes no slot record)", ids(svc) == sorted(live) and len(of_kind(d, CREATED)) == n_created and len(of_kind(d, REMOVED)) == 71, str((len(listing(svc)), len(of_kind(d, CREATED)))))
    post_event(svc, 1)
    check("11. one event reaches all 62, each signed with its own key", wait_for(lambda: len(rall.seen) == 62, 15) and rall.all_signed(), str((len(rall.seen), rall.all_signed())))
    time.sleep(0.5)
    check("11. ... and no more", len(rall.seen) == 62 and set(rall.events()) == {1}, str(len(rall.seen)))
    stop(svc)
    shutil.rmtree(d)

    # 12. a restart while an endpoint is draining ---------------------------------------------------------------------
    reset_db()
    rfast = Receiver()
    rslow = Receiver(plan={1: [(1.5, 204)]})
    keys = {}
    for i in range(62):
        keys[i] = secret()
        r = rslow if i == 5 else rfast
        psql(f"insert into endpoints values ({i}, '127.0.0.1', {r.port}, '{keys[i]}')")
    rfast.keys = [k for i, k in keys.items() if i != 5]
    rslow.keys = [keys[5]]
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    check("12. 61 endpoints get event 1, and the slow one holds its attempt", wait_for(lambda: len(rfast.seen) == 61 and rslow.events() == [1], 10), str((len(rfast.seen), rslow.seen)))
    wait_for(lambda: sum(1 for e in listing(svc) if e["cursor"] == 1) == 61, 10)
    st, out = delete(svc, 5)
    check("12. endpoint 5 is deleted while its attempt is on the wire: draining", st == 200 and out["draining"] is True and stats(svc)["draining"] == 1 and len(listing(svc)) == 61, str((st, out)))
    st, out = create(svc, rfast.port)
    check("12. 61 live and one draining: every slot is taken, so a POST is a 409 and stores nothing (it would have a row and nowhere to put it)",
          st == 409 and len(db_ids()) == 61, str((st, out, len(db_ids()))))
    kill(svc)
    check("12. (killed: no `removed` record for slot 5 was written)", not of_kind(d, REMOVED), str(of_kind(d, REMOVED)))
    svc = start(d)
    check("12. restart: the row is gone, so 61 endpoints; slot 5 is dormant and untouched", len(listing(svc)) == 61 and 5 not in ids(svc) and not of_kind(d, REMOVED) and stats(svc)["draining"] == 0, str((len(listing(svc)), of_kind(d, REMOVED))))
    st, out = create(svc, rfast.port)
    check("12. a POST needs a slot: dormant slot 5 is reclaimed (removed, then created, in that order)",
          st == 201 and [r for r in outcomes(d) if r[0] in (CREATED, REMOVED) and r[1] == 5][-2:] == [(REMOVED, 5, 0, 0, 0), (CREATED, 5, out["id"], out["cursor"], 0)], str((st, out, [r for r in outcomes(d) if r[1] == 5])))
    rfast.keys.append(out["secret"])
    n_before = len(rfast.seen)
    post_event(svc, 2)
    check("12. all 62 get event 2", wait_for(lambda: len(rfast.seen) - n_before == 62 and rfast.all_signed(), 10), str((len(rfast.seen) - n_before)))
    stop(svc)
    shutil.rmtree(d)

    # 12b. ... and a row put back is the same endpoint resuming: the replays that were dropped stay dropped
    reset_db()
    sa, sb = secret(), secret()
    # event 1 is delivered, its replay fails (500) and waits an hour; event 2 is on the wire (held) when the endpoint is deleted
    ra, rb = Receiver([sa], plan={1: [(0, 204), (0, 500)], 2: [(2.0, 204)]}), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d, schedule="3600000")
    post_event(svc, 1)
    wait_for(lambda: cursors(svc) == {100: 1, 101: 1}, 6)
    req(svc, "POST", "/events/1/replay/100")
    check("12b. a replay of event 1 failed and waits (an hour)", wait_for(lambda: ra.events() == [1, 1] and of_kind(d, REPLAY_FAILED), 6) and stats(svc)["replays"] == 1, str((ra.seen, stats(svc))))
    post_event(svc, 2)
    wait_for(lambda: ra.events() == [1, 1, 2], 5)
    st, out = delete(svc, 100)
    check("12b. deleted with event 2 on the wire: draining, and the waiting replay is dropped", st == 200 and out["draining"] is True and stats(svc)["replays"] == 0, str((st, out, stats(svc))))
    check("12b. ... with its record", of_kind(d, REPLAY_DEAD) == [(REPLAY_DEAD, 0, 1, 1, 0)], str(of_kind(d, REPLAY_DEAD)))
    kill(svc)
    add_rows([(100, ra, sa)])
    svc = start(d, schedule="3600000")
    check("12b. the row is put back before the next start: endpoint 100 is back in its slot (no `removed` was written), and the replay is not waiting again",
          ids(svc) == [100, 101] and not of_kind(d, REMOVED) and stats(svc)["replays"] == 0 and created_slot(d, 100) == 0, str((listing(svc), stats(svc), of_kind(d, REMOVED))))
    stop(svc)
    shutil.rmtree(d)

    # 13. under load: nothing new is sent to a deleted endpoint, and the others lose nothing -------------------------
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    d = tmp()
    svc = start(d)
    total = 400
    stopped = {}

    def poster():
        for n in range(1, total + 1):
            post_event(svc, n)
            if n == 150:
                stopped["at"] = time.time()
            time.sleep(0.002)

    th = threading.Thread(target=poster)
    th.start()
    wait_for(lambda: len(ra.seen) >= 100, 10)
    st, out = delete(svc, 100)
    t_del = time.time()
    th.join()
    check("13. the delete is accepted in the middle of the stream", st == 200, str((st, out)))
    check("13. the other endpoint gets all %d events" % total, wait_for(lambda: sorted(set(rb.events())) == list(range(1, total + 1)), 30), str(len(set(rb.events()))))
    time.sleep(1.0)
    late = [(n, round(t - t_del, 3)) for n, _, t in ra.seen if t > t_del + 0.25]
    check("13. the deleted endpoint's receiver is sent nothing after a quarter of a second past the answer (%d seen before)" % len(ra.seen), not late, str(late[:5]))
    check("13. the deleted endpoint got fewer than all (it was deleted in the middle) and each signed", len(set(ra.events())) < total and ra.all_signed(), str(len(set(ra.events()))))
    stop(svc)
    shutil.rmtree(d)

    # 14. the slowest endpoint is deleted: the others are served either way ------------------------------------------
    # (This test used to say the opposite: that the fast endpoint was *held to the slow one's window*, 1,024 events, until the slow one
    # was deleted. That was the defect of docs/production.md 0.1, pinned as if it were a design; each endpoint now reads the log from its own
    # cursor, so the fast one is served everything while the slow one is dead, and deleting the slow one changes nothing for it.)
    reset_db()
    ss, sf = secret(), secret()
    rs, rf = Receiver([ss]), Receiver([sf])
    add_rows([(100, rs, ss), (101, rf, sf)])
    rs.close()  # nothing listens: every attempt at the slow endpoint fails, and (schedule: an hour) waits
    d = tmp()
    svc = start(d, schedule="3600000", deadline="1000")
    total = 1300
    for n in range(1, total + 1):
        post_event(svc, n)
    check("14. the fast endpoint is sent all 1,300 events, though the slow one is dead and 1,024 events behind it",
          wait_for(lambda: sorted(set(rf.events())) == list(range(1, total + 1)), 40), str((len(set(rf.events())), max(rf.events() or [0]))))
    check("14. the slow endpoint's cursor is 0, the fast one's 1300", wait_for(lambda: cursors(svc) == {100: 0, 101: total}, 10), str(cursors(svc)))
    st, out = delete(svc, 100)
    check("14. delete the slow one", st == 200, str((st, out)))
    check("14. the fast one's cursor stays at the end and nothing was sent twice", wait_for(lambda: cursors(svc) == {101: total}, 10) and len(rf.events()) == total, str((cursors(svc), len(rf.events()))))
    st, out = create(svc, rf.port)
    rf.keys.append(out["secret"])
    post_event(svc, total + 1)
    check("14. a new event reaches the one left and the new one", wait_for(lambda: rf.events().count(total + 1) == 2, 10), str(rf.events()[-3:]))
    stop(svc)
    shutil.rmtree(d)

    # 16. dormant slots and a draining one: the slot that is draining is not mistaken for a dormant one (its id is not in the table either)
    reset_db()
    rfast = Receiver()
    rslow = Receiver(plan={1: [(1.5, 204)]})
    keys = {}
    for i in range(62):
        keys[i] = secret()
        r = rslow if i == 5 else rfast
        psql(f"insert into endpoints values ({i}, '127.0.0.1', {r.port}, '{keys[i]}')")
    rfast.keys = [k for i, k in keys.items() if i != 5]
    rslow.keys = [keys[5]]
    d = tmp()
    svc = start(d)
    stop(svc)
    psql("delete from endpoints where id between 10 and 30")  # 21 rows: their slots are dormant at the next start
    svc = start(d)
    check("16. 41 live endpoints and 21 dormant slots", len(listing(svc)) == 41 and stats(svc)["draining"] == 0, str(len(listing(svc))))
    post_event(svc, 1)
    check("16. the slow endpoint (5, slot 5) holds its attempt", wait_for(lambda: rslow.events() == [1], 10), str(rslow.seen))
    st, out = delete(svc, 5)
    check("16. deleted while on the wire: draining", st == 200 and out["draining"] is True, str((st, out)))
    st, out = create(svc, rfast.port)
    got = [r for r in outcomes(d) if r[0] in (CREATED, REMOVED) and r[1] in (5, 10)][-2:]
    check("16. a POST needs a slot: it reclaims the lowest dormant one (10), not slot 5, which is draining (and has a lower number)",
          st == 201 and got == [(REMOVED, 10, 0, 0, 0), (CREATED, 10, out["id"], out["cursor"], 0)] and not [r for r in of_kind(d, REMOVED) if r[1] == 5], str((st, out, got)))
    rfast.keys.append(out["secret"])
    check("16. when the attempt ends, slot 5 is freed", wait_for(lambda: [r for r in of_kind(d, REMOVED) if r[1] == 5] == [(REMOVED, 5, 0, 0, 0)] and (DELIVERED, 5, 1, 1, 0) in of_kind(d, DELIVERED), 6), str(outcomes(d)[-6:]))
    stop(svc)
    shutil.rmtree(d)

    # 15. a database that does not answer in five seconds: a 504, nothing changed, and the next start reconciles (D3, D4)
    reset_db()
    sa, sb = secret(), secret()
    ra, rb = Receiver([sa]), Receiver([sb])
    add_rows([(100, ra, sa), (101, rb, sb)])
    psql("create or replace function hooks_test_slow() returns trigger as $$ begin perform pg_sleep(6.5); return old; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before delete on endpoints for each row execute function hooks_test_slow()")
    d = tmp()
    svc = start(d)
    t0 = time.time()
    st, out = delete(svc, 100)
    check("15. a delete the database does not answer in five seconds is a 504 after about five", st == 504 and 4.5 < time.time() - t0 < 6.4, str((st, out, time.time() - t0)))
    check("15. nothing changed in the service: still listed, still delivered to, nothing draining, no record", ids(svc) == [100, 101] and stats(svc)["draining"] == 0 and not of_kind(d, REMOVED), str((listing(svc), of_kind(d, REMOVED))))
    post_event(svc, 1)
    check("15. ... and the endpoint still gets events", wait_for(lambda: ra.events() == [1], 5), str(ra.seen))
    check("15. the row does go (the transaction was not assumed to have failed)", wait_for(lambda: db_ids() == [101], 6), str(db_ids()))
    psql("drop trigger hooks_test_trigger on endpoints")
    stop(svc)
    svc = start(d)
    check("15. the next start reconciles: the row is gone, so the endpoint is not there", ids(svc) == [101], str(listing(svc)))
    post_event(svc, 2)
    check("15. ... and is sent nothing", wait_for(lambda: 2 in rb.events(), 5) and ra.events() == [1], str(ra.seen))
    stop(svc)
    shutil.rmtree(d)

    reset_db()
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all delete checks passed")


main()
