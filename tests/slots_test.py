#!/usr/bin/env python3
"""Endpoint ids and slots (docs/design.md section 25, slice 1).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/slots_test.py build/hooks

An endpoint has an id (what the API and the history call it, up to six digits) and a slot (its place in the delivery state, 0 to 1,023 since design section 41; it was 0 to 61);
the log says which endpoint has which slot (`created` and `removed` records, kinds 10 and 11 of delivery.seg). The test reads and
writes that log with its own reader and writer, so it checks the format and not only the service's agreement with itself.

  1. a first start writes a `created` record for each endpoint (slot, id, start 0), and a restart writes none
  2. a log from before slots had records (the `created` records removed from one the service wrote) is read as it always was: no
     event is sent again, the cursors are where they were, and nothing is written for the endpoints it already knows
  3. ids above 15: 20 and 999999 are endpoints; each gets the events; the API takes the id (enable, replay, GET /endpoints)
  4. an endpoint missing from the table at a start is dormant, and when it comes back it resumes where it was (no event is sent
     again, those it missed are)
  5. no free slot: dormant slots are freed (a `removed` record) for the endpoints that need them, and the lowest first
  6. a slot given to a second endpoint inherits nothing from the first: its outcomes and an outcome that arrives after the `removed`
     are not the new endpoint's (the log is written by hand to say so)
  7. the history (PostgreSQL) says the id, not the slot
  8. an endpoint the log does not know (a row added by hand, a line added to endpoints.conf) starts at the cursor of the slowest endpoint
     it does know, not at 0 (section 25.3). That rule was made so that it could not widen the window every endpoint was held to; since
     section 31 each endpoint has its own window and the rule is only a policy (a new row is sent the slowest one's backlog, not the whole log)
"""
import base64
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import pgwait  # noqa: E402

http.server.HTTPServer.request_queue_size = 128
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
LIMIT = 1024          # the slots there are (src/state.cho `max_endpoints`; design section 41: it was 62)
CREATED, REMOVED = 10, 11
DELIVERED, FAILED = 1, 2


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


# ---- the outcome log, read and written here --------------------------------------------------------------------

def put_record(seq, kind, slot, event=0, attempts=0, next_at=0):
    value = struct.pack("<5Q", kind, slot, event, attempts, next_at)
    body = struct.pack("<QQI", seq, 0, 1) + struct.pack("<I", 1) + b"o" + struct.pack("<I", len(value)) + value
    return struct.pack("<I", 4 + len(body)) + struct.pack("<I", chaos.crc32c(body)) + body


def read_outcomes(path):
    if not os.path.exists(path):
        return []
    recs, _ = chaos.read_log(open(path, "rb").read())
    out = []
    for ms, pairs in recs:
        k, e, ev, at, nx = struct.unpack("<5Q", pairs[0][1])
        out.append((ms, k, e, ev, at, nx))
    return out


def write_outcomes(path, rows):
    with open(path, "wb") as f:
        for i, (kind, slot, event, attempts, next_at) in enumerate(rows):
            f.write(put_record(i, kind, slot, event, attempts, next_at))


def kinds(path, which):
    return [(r[2], r[3], r[4]) for r in read_outcomes(path) if r[1] == which]


# ---- receivers and the service ---------------------------------------------------------------------------------

class Receiver:
    def __init__(self):
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(json.loads(body)["n"])
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


SECRET = "whsec_" + base64.b64encode(os.urandom(24)).decode()


def conf(d, rows):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for ident, r in rows:
            f.write(f"{ident} 127.0.0.1 {r.port} {SECRET}\n")


def start(d, extra=(), schedule="100"):
    port = chaos.free_port()
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--schedule", schedule, "--deadline-ms", "800", *extra],
                            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    gone = line == "listening" and pgwait.after_listening(proc, lines, extra)
    svc = type("Svc", (), {})()
    svc.port, svc.proc, svc.lines, svc.exited = port, proc, lines, line != "listening" or gone
    return svc


def stop(svc):
    if not svc.exited:
        svc.proc.terminate()
    svc.proc.wait()


def get(svc, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def call(svc, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{svc.port}{path}", method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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


def cursors(svc):
    return {e["id"]: e["cursor"] for e in get(svc, "/endpoints")}


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def main():
    a, b = Receiver(), Receiver()

    # 1. a first start writes created records; a restart writes none
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    log = os.path.join(d, "delivery.seg")
    conf(d, [(0, a), (1, b)])
    svc = start(d)
    for n in range(1, 6):
        post(svc, n)
    check("1. five events are delivered to both", wait_for(lambda: len(a.seen) == 5 and len(b.seen) == 5, 10), str((a.seen, b.seen)))
    wait_for(lambda: cursors(svc) == {0: 5, 1: 5}, 5)
    stop(svc)
    check("1. the log has a `created` record for each endpoint: slot, id, start 0", sorted(kinds(log, CREATED)) == [(0, 0, 0), (1, 1, 0)], str(kinds(log, CREATED)))
    check("1. ... before any outcome", [r[1] for r in read_outcomes(log)][:2] == [CREATED, CREATED], str(read_outcomes(log)[:3]))
    svc = start(d)
    time.sleep(0.5)
    stop(svc)
    check("1. a restart writes no more", len(kinds(log, CREATED)) == 2 and not kinds(log, REMOVED), str(read_outcomes(log)))

    # 2. a log from before slots had records
    rows = [r for r in read_outcomes(log) if r[1] not in (CREATED, REMOVED)]
    write_outcomes(log, [(r[1], r[2], r[3], r[4], r[5]) for r in rows])
    check("2. (the legacy log has outcomes and no slot records)", len(rows) == 10 and not kinds(log, CREATED), str(read_outcomes(log)))
    a.seen.clear()
    b.seen.clear()
    svc = start(d)
    time.sleep(1.2)
    check("2. nothing is sent again", a.seen == [] and b.seen == [], str((a.seen, b.seen)))
    check("2. the cursors are where they were", cursors(svc) == {0: 5, 1: 5}, str(cursors(svc)))
    post(svc, 6)
    check("2. and a new event is delivered to both", wait_for(lambda: a.seen == [6] and b.seen == [6], 5), str((a.seen, b.seen)))
    stop(svc)
    check("2. no slot record is written for them: in a legacy log an endpoint's slot is its id, and that needs no record", not kinds(log, CREATED) and not kinds(log, REMOVED), str(read_outcomes(log)[-4:]))
    shutil.rmtree(d)

    # 3. ids above 15
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    log = os.path.join(d, "delivery.seg")
    ra, rb = Receiver(), Receiver()
    conf(d, [(20, ra), (999999, rb)])
    svc = start(d)
    post(svc, 1)
    check("3. ids 20 and 999999 each get the event", wait_for(lambda: ra.seen == [1] and rb.seen == [1], 5), str((ra.seen, rb.seen)))
    check("3. GET /endpoints says the ids, in the table's order", [e["id"] for e in get(svc, "/endpoints")] == [20, 999999], str(get(svc, "/endpoints")))
    check("3. the slots: 20 has slot 20 (its own number is free), 999999 the lowest free", sorted(kinds(log, CREATED)) == [(0, 999999, 0), (20, 20, 0)], str(kinds(log, CREATED)))
    check("3. enable takes the id", call(svc, "POST", "/endpoints/999999/enable")[0] == 200)
    check("3. ... and 21 is no endpoint (404), and neither is slot 0's number as an id", call(svc, "POST", "/endpoints/21/enable")[0] == 404 and call(svc, "POST", "/endpoints/0/enable")[0] == 404)
    check("3. an id that is not a number is a 400", call(svc, "POST", "/endpoints/x/enable")[0] == 400)
    ra.seen.clear()
    rb.seen.clear()
    code, body = call(svc, "POST", "/events/1/replay/999999")
    check("3. replay takes the id, answers it, and reaches that endpoint only",
          code == 202 and json.loads(body)["endpoints"] == [999999] and wait_for(lambda: rb.seen == [1], 5) and ra.seen == [], str((code, body, ra.seen, rb.seen)))
    check("3. replay of an id that is not there is a 404", call(svc, "POST", "/events/1/replay/0")[0] == 404)
    stop(svc)
    svc = start(d)
    time.sleep(0.5)
    ra.seen.clear()
    rb.seen.clear()
    post(svc, 2)
    check("3. after a restart both still get events, once each", wait_for(lambda: ra.seen == [2] and rb.seen == [2], 5) and cursors(svc) == {20: 2, 999999: 2}, str((ra.seen, rb.seen, cursors(svc))))
    stop(svc)
    shutil.rmtree(d)

    # 4. dormant, then back
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    log = os.path.join(d, "delivery.seg")
    r0, r1 = Receiver(), Receiver()
    conf(d, [(0, r0), (1, r1)])
    svc = start(d)
    for n in range(1, 6):
        post(svc, n)
    # The preconditions are checks of their own: if either times out the later checks are about a state that was never reached, and
    # without these a failure there looks like a bug in what comes after.
    check("4. (before) both endpoints got events 1 to 5", wait_for(lambda: len(r0.seen) == 5 and len(r1.seen) == 5, 10), str((r0.seen, r1.seen)))
    check("4. (before) both cursors are at 5 before the stop", wait_for(lambda: cursors(svc) == {0: 5, 1: 5}, 5), str(cursors(svc)))
    stop(svc)
    conf(d, [(1, r1)])
    svc = start(d)
    for n in range(6, 9):
        post(svc, n)
    check("4. with endpoint 0 out of the table, only endpoint 1 gets events 6 to 8", wait_for(lambda: sorted(r1.seen[5:]) == [6, 7, 8], 5), str((r0.seen, r1.seen)))
    time.sleep(0.5)
    check("4. ... and nothing is written about 0's slot", not kinds(log, REMOVED) and sorted(r0.seen) == [1, 2, 3, 4, 5], str((kinds(log, REMOVED), r0.seen)))
    stop(svc)
    # a new endpoint beside a dormant one: it does not take the dormant one's slot (the lowest free is 2, not 0)
    r7000 = Receiver()
    conf(d, [(1, r1), (7000, r7000)])
    svc = start(d)
    post(svc, 9)
    check("4. a new endpoint while 0 is dormant is served from the slowest cursor (8): it gets event 9", wait_for(lambda: r7000.seen == [9], 8), str(r7000.seen))
    wait_for(lambda: cursors(svc) == {1: 9, 7000: 9}, 5)
    stop(svc)
    check("4. ... and takes slot 2 (0 and 1 are taken), freeing nothing", kinds(log, CREATED)[-1] == (2, 7000, 8), str(kinds(log, CREATED)))
    check("4. ... and nothing was freed", not kinds(log, REMOVED), str(kinds(log, REMOVED)))
    conf(d, [(0, r0), (1, r1)])
    svc = start(d)
    check("4. when it is back it resumes where it was: it gets 6 to 9 and nothing before", wait_for(lambda: sorted(r0.seen[5:]) == [6, 7, 8, 9], 5) and sorted(r0.seen[:5]) == [1, 2, 3, 4, 5], str(r0.seen))
    time.sleep(0.5)
    check("4. ... and endpoint 1 is sent nothing again", sorted(r1.seen) == [1, 2, 3, 4, 5, 6, 7, 8, 9], str(r1.seen))
    stop(svc)
    shutil.rmtree(d)

    # 5. no free slot: dormant ones are freed, lowest first
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    log = os.path.join(d, "delivery.seg")
    write_outcomes(log, [(CREATED, k, 1000 + k, 0, 0) for k in range(LIMIT)])
    r5a, r5b = Receiver(), Receiver()
    conf(d, [(5000, r5a), (5001, r5b)])
    svc = start(d)
    post(svc, 1)
    check("5. two new endpoints with every slot taken: both are served", wait_for(lambda: r5a.seen == [1] and r5b.seen == [1], 5), str((svc.lines, r5a.seen, r5b.seen)))
    stop(svc)
    tail = read_outcomes(log)[LIMIT:]
    check("5. slots 0 and 1 were freed (`removed`) and given (`created`), in that order",
          [(r[1], r[2], r[3]) for r in tail[:4]] == [(REMOVED, 0, 0), (CREATED, 0, 5000), (REMOVED, 1, 0), (CREATED, 1, 5001)], str(tail[:6]))
    check("5. no other slot was touched", all(r[2] in (0, 1) for r in tail if r[1] in (CREATED, REMOVED)), str(tail))
    # the id whose slot was freed is a new endpoint when it comes back: it starts at the slowest cursor (1), not where the old one was
    r5c = Receiver()
    conf(d, [(5000, r5a), (5001, r5b), (1000, r5c)])
    svc = start(d)
    post(svc, 2)
    check("5. an id whose slot was freed is a new endpoint when it comes back: it starts at the slowest cursor and gets event 2, not 1", wait_for(lambda: r5c.seen == [2], 5), str(r5c.seen))
    time.sleep(0.3)
    check("5. ... once", r5c.seen == [2], str(r5c.seen))
    stop(svc)
    check("5. ... and its `created` record says start 1", kinds(log, CREATED)[-1][1:] == (1000, 1), str(kinds(log, CREATED)[-1:]))
    shutil.rmtree(d)

    # 5b. a dormant slot is freed, a live endpoint's never: slot 0 holds id 1000, which is in the table
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    log = os.path.join(d, "delivery.seg")
    write_outcomes(log, [(CREATED, k, 1000 + k, 0, 0) for k in range(LIMIT)])
    r1000, r6000 = Receiver(), Receiver()
    conf(d, [(1000, r1000), (6000, r6000)])
    svc = start(d)
    post(svc, 1)
    check("5b. a live endpoint and a new one with every slot taken: both are served", wait_for(lambda: r1000.seen == [1] and r6000.seen == [1], 5), str((r1000.seen, r6000.seen)))
    stop(svc)
    tail = [(r[1], r[2], r[3]) for r in read_outcomes(log)[LIMIT:] if r[1] in (CREATED, REMOVED)]
    check("5b. the lowest slot that is not a live endpoint's (1) was freed and given, and slot 0 was not touched", tail == [(REMOVED, 1, 0), (CREATED, 1, 6000)], str(tail))
    shutil.rmtree(d)

    # 6. nothing leaks from one endpoint of a slot to the next
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    svc = start(d)  # no endpoints: it only ingests
    for n in range(1, 6):
        post(svc, n)
    stop(svc)
    log = os.path.join(d, "delivery.seg")
    DISABLED, REPLAY = 4, 6
    rows = [(CREATED, 3, 7, 0, 0)] + [(DELIVERED, 3, ev, 1, 0) for ev in range(1, 6)] + [(DISABLED, 3, 0, 0, 0), (REPLAY, 3, 2, 0, 0)]
    rows += [(REMOVED, 3, 0, 0, 0), (FAILED, 3, 3, 1, 4102444800000), (CREATED, 3, 9, 0, 0)]
    write_outcomes(log, rows)
    r9 = Receiver()
    conf(d, [(9, r9)])
    svc = start(d)
    check("6. endpoint 9 in slot 3 gets all five events: endpoint 7's deliveries are not its own, and the stale `failed` after `removed` is nobody's",
          wait_for(lambda: sorted(r9.seen) == [1, 2, 3, 4, 5], 8), str((r9.seen, svc.lines)))
    check("6. ... it is not disabled (endpoint 7 was) and has no replay waiting (endpoint 7 had one)",
          get(svc, "/endpoints")[0]["disabled"] is False and get(svc, "/stats")["replays"] == 0, str((get(svc, "/endpoints"), get(svc, "/stats"))))
    time.sleep(0.5)
    check("6. ... and each event once (the replay of 7's event 2 was not carried over)", sorted(r9.seen) == [1, 2, 3, 4, 5], str(r9.seen))
    stop(svc)
    # 6c. a stale record arriving after the last `removed` does not make the slot an endpoint's
    write_outcomes(log, [(CREATED, 3, 7, 0, 0)] + [(DELIVERED, 3, ev, 1, 0) for ev in range(1, 6)] + [(REMOVED, 3, 0, 0, 0)] + [(DELIVERED, 3, ev, 1, 0) for ev in range(1, 6)])
    r3 = Receiver()
    conf(d, [(3, r3)])
    svc = start(d)
    check("6c. an endpoint that takes the freed slot gets every event: the stale deliveries after `removed` are not its own", wait_for(lambda: sorted(r3.seen) == [1, 2, 3, 4, 5], 8), str(r3.seen))
    stop(svc)
    conf(d, [(9, r9)])
    # 6b. the cursor a `created` record starts at
    write_outcomes(log, [(CREATED, 3, 9, 3, 0)])
    r9.seen.clear()
    svc = start(d)
    check("6b. `created` with start 3: the endpoint gets events 4 and 5 and not 1 to 3", wait_for(lambda: sorted(r9.seen) == [4, 5], 8), str(r9.seen))
    time.sleep(0.5)
    check("6b. ... and its cursor is 5", cursors(svc) == {9: 5} and sorted(r9.seen) == [4, 5], str((cursors(svc), r9.seen)))
    stop(svc)
    shutil.rmtree(d)

    # 8. a new endpoint starts where the slowest known endpoint is
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    svc = start(d)  # no endpoints: it only ingests
    for n in range(1, 9):
        post(svc, n)
    stop(svc)
    log = os.path.join(d, "delivery.seg")
    rows = [(CREATED, 0, 10, 0, 0)] + [(DELIVERED, 0, ev, 1, 0) for ev in range(1, 4)]
    rows += [(CREATED, 1, 11, 0, 0)] + [(DELIVERED, 1, ev, 1, 0) for ev in range(1, 9)]
    write_outcomes(log, rows)
    down, r11, r12 = Receiver(), Receiver(), Receiver()
    down.srv.shutdown()
    down.srv.server_close()
    conf(d, [(10, down), (11, r11), (12, r12)])
    svc = start(d)
    check("8. the new endpoint (12) gets the events after the slowest cursor (3): 4 to 8, not 1 to 3",
          wait_for(lambda: sorted(r12.seen) == [4, 5, 6, 7, 8], 8), str((r12.seen, svc.lines)))
    time.sleep(0.5)
    check("8. ... each once, and endpoint 11, which had them all, gets none", sorted(r12.seen) == [4, 5, 6, 7, 8] and r11.seen == [], str((r12.seen, r11.seen)))
    stop(svc)
    check("8. its `created` record says where it started: slot 12, id 12, start 3", kinds(log, CREATED)[-1] == (12, 12, 3), str(kinds(log, CREATED)))
    shutil.rmtree(d)

    # 8b. the stall that motivated it: A is far ahead, B is added with a dead receiver. Started at 0, B held every endpoint to a window of
    # 1,024 events from 0 and A could not go past it.
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    ra, rb = Receiver(), Receiver()
    conf(d, [(0, ra)])
    svc = start(d)
    total = 1300
    for n in range(1, total + 1):
        post(svc, n)
    check("8b. A, alone, gets 1,300 events", wait_for(lambda: len(ra.seen) == total, 60), str(len(ra.seen)))
    wait_for(lambda: cursors(svc) == {0: total}, 10)
    stop(svc)
    rb.srv.shutdown()
    rb.srv.server_close()
    conf(d, [(0, ra), (1, rb)])
    ra.seen.clear()
    svc = start(d, schedule="3600000,3600000")  # B's failures are retried in an hour: it is behind for the whole test
    post(svc, total + 1)
    check("8b. B is added with a receiver that is down: A still gets the next event", wait_for(lambda: ra.seen == [total + 1], 10), str((ra.seen, cursors(svc))))
    check("8b. ... and B began at A's cursor, not 0", kinds(os.path.join(d, "delivery.seg"), CREATED)[-1] == (1, 1, total), str(kinds(os.path.join(d, "delivery.seg"), CREATED)))
    stop(svc)
    shutil.rmtree(d)

    # 7. the history says the id
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("truncate attempts")
    psql("truncate endpoints")
    d = tempfile.mkdtemp(prefix="hooks-slots-")
    h1, h2 = Receiver(), Receiver()
    psql(f"insert into endpoints values (7, '127.0.0.1', {h1.port}, '{SECRET}'), (999999, '127.0.0.1', {h2.port}, '{SECRET}')")
    flags = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        flags += ["--pg-password", PG_PASSWORD]
    svc = start(d, flags)
    post(svc, 1)
    check("7. the history has a row for each endpoint, under its id", wait_for(lambda: sorted(r[0] for r in psql("select endpoint from attempts")) == ["7", "999999"], 10),
          str(psql("select endpoint from attempts")))
    got = get(svc, "/events/1/attempts")
    check("7. GET /events/:id/attempts says the ids", sorted(r["endpoint"] for r in got) == [7, 999999], str(got))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d, ignore_errors=True)

    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all slot checks passed")


main()
