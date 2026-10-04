#!/usr/bin/env python3
"""POST /endpoints and GET /endpoints/:id (docs/design.md section 25.2, slice 2).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/manage_test.py build/hooks

  1. who may call it: no admin-token configured is a 403; a missing, wrong, shorter, longer or doubled bearer token is a 401; the
     scheme is not case sensitive; the reads need no token
  2. what a request may say: every refusal is a 400 with its reason, an unknown member is ignored
  3. a created endpoint: 201 with the id, host, port and a secret the service made (`whsec_` and 24 bytes of base64) that no read gives
     back; the row in the table says the same; a secret the request brought is kept as it came; ids come from the sequence
  4. it starts from now: the five events already in the log are not sent to it, the sixth is, signed with the secret it was given; the
     endpoints that were there go on; the first endpoint of a service that had none starts from now too
  5. it is still there after a restart, with its cursor, and its slot is in the log (`created`: slot, id, the cursor it started at)
  6. ids are not given twice (a row deleted, a row inserted by hand: the next id is larger than both)
  7. a database that refuses the insert (a trigger): 503, nothing changed in the service or the log, and the next one works
  8. one change at a time: a second request while one waits for the database is a 409
  9. a database that is slow past five seconds: 504, and the row, which commits later, is an endpoint at the next start (starting from the
     beginning of the log, as every row the service did not create does)
 10. 62 endpoints and no more (409); no database named (503); the secret and the token are in no read
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 128
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "correct-horse-battery-staple"
FAILS = []
CREATED = 10


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


class Receiver:
    """Records the event numbers it is sent, and whether each signature is right for `key` (set later)."""

    def __init__(self):
        self.seen, self.key, self.bad = [], None, 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(json.loads(body)["n"])
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


def pg_flags():
    f = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


def start(d, extra=None):
    port = chaos.free_port()
    flags = pg_flags() + ["--admin-token", TOKEN] if extra is None else extra
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--schedule", "100", "--deadline-ms", "800", *flags], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
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


def req(svc, method, path, body=None, token=TOKEN, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=15)
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = "Bearer " + token
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (out if raw else (json.loads(out) if out else None))


def create(svc, body, **kw):
    return req(svc, "POST", "/endpoints", body, **kw)


def get(svc, path):
    return req(svc, "GET", path, token=None)


def post_event(svc, n):
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


def created_records(d):
    path = os.path.join(d, "delivery.seg")
    if not os.path.exists(path):
        return []
    recs, _ = chaos.read_log(open(path, "rb").read())
    out = []
    for ms, pairs in recs:
        k, slot, ident, start, _ = struct.unpack("<5Q", pairs[0][1])
        if k == CREATED:
            out.append((slot, ident, start))
    return out


def records(d, kind):
    path = os.path.join(d, "delivery.seg")
    if not os.path.exists(path):
        return []
    recs, _ = chaos.read_log(open(path, "rb").read())
    return [struct.unpack("<5Q", pairs[0][1])[1:4] for ms, pairs in recs if struct.unpack("<5Q", pairs[0][1])[0] == kind]


def fresh_dir():
    return tempfile.mkdtemp(prefix="hooks-manage-")


def main():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("truncate endpoints")
    psql("drop sequence if exists endpoint_ids")
    psql("create sequence endpoint_ids minvalue 0 start 0")
    good = {"host": "127.0.0.1", "port": 9}

    # 1. who may call it
    d = fresh_dir()
    svc = start(d, pg_flags())
    check("1. no admin-token configured: 403 even with a token", create(svc, good)[0] == 403)
    stop(svc)
    svc = start(d)
    check("1. no Authorization header: 401", create(svc, good, token=None)[0] == 401)
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    c.request("POST", "/endpoints", body=json.dumps(good).encode())
    r = c.getresponse()
    r.read()
    c.close()
    check("1. ... and it says how to authenticate (WWW-Authenticate: Bearer)", r.status == 401 and r.getheader("WWW-Authenticate") == "Bearer", str((r.status, r.getheaders())))
    check("1. a wrong token of the same length: 401", create(svc, good, token=TOKEN[:-1] + "!")[0] == 401)
    check("1. a prefix of the token: 401", create(svc, good, token=TOKEN[:-1])[0] == 401)
    check("1. the token with more after it: 401", create(svc, good, token=TOKEN + "x")[0] == 401)
    check("1. a token of 300 bytes: 401", create(svc, good, token="a" * 300)[0] == 401)
    check("1. not the bearer scheme: 401", create(svc, good, token=None, headers={"Authorization": "Basic " + base64.b64encode(b"u:" + TOKEN.encode()).decode()})[0] == 401)
    check("1. another scheme with the token after seven characters (`Digest <token>`): 401", create(svc, good, token=None, headers={"Authorization": "Digest " + TOKEN})[0] == 401)
    check("1. the token in the query is not a token: 401", req(svc, "POST", "/endpoints?token=" + TOKEN, good, token=None)[0] == 401)
    s = socket.create_connection(("127.0.0.1", svc.port))
    body = json.dumps(good).encode()
    s.sendall(b"POST /endpoints HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    first = s.recv(100)
    s.close()
    check("1. two Authorization headers: 401", first.startswith(b"HTTP/1.1 401"), str(first))
    check("1. none of those created anything", psql("select count(*) from endpoints")[0][0] == "0" and get(svc, "/stats")[1]["endpoints"] == 0)
    check("1. the reads need no token", get(svc, "/endpoints")[0] == 200 and get(svc, "/endpoints/0")[0] == 404)
    r, _ = create(svc, good, token=TOKEN.upper())
    check("1. the token is case sensitive (the scheme is not)", r == 401)
    r, b = req(svc, "POST", "/endpoints", good, token=None, headers={"Authorization": "bEaReR " + TOKEN})
    check("1. `bEaReR <token>`: 201", r == 201, str((r, b)))
    psql("truncate endpoints")
    stop(svc)
    shutil.rmtree(d)

    # 2. what a request may say
    d = fresh_dir()
    svc = start(d)
    bad = [("not JSON", b"{nope"), ("an array", b"[]"), ("no host", {"port": 9}), ("a host that is not a string", {"host": 5, "port": 9}), ("an empty host", {"host": "", "port": 9}),
           ("a host with a space", {"host": "a b", "port": 9}), ("a host of 254 characters", {"host": "h" * 254, "port": 9}), ("a host with a newline", {"host": "a\nb", "port": 9}),
           ("no port", {"host": "h"}), ("port 0", {"host": "h", "port": 0}), ("port 65536", {"host": "h", "port": 65536}), ("a port that is a string", {"host": "h", "port": "80"}),
           ("a port that is a float", {"host": "h", "port": 80.5}), ("a secret that is not base64", {"host": "h", "port": 9, "secret": "whsec_!!!!!!!!"}),
           ("a secret that is a number", {"host": "h", "port": 9, "secret": 5}), ("a secret that is too short", {"host": "h", "port": 9, "secret": "abc"}),
           ("from start", {"host": "h", "port": 9, "from": "start"}), ("from a number", {"host": "h", "port": 9, "from": 5})]
    for name, body in bad:
        r, b = create(svc, body)
        check(f"2. {name}: 400 with a reason", r == 400 and isinstance(b, dict) and len(b.get("error", "")) > 5, str((r, b)))
    check("2. none of them created anything", psql("select count(*) from endpoints")[0][0] == "0")
    r, b = create(svc, {"host": "h", "port": 9, "colour": "red", "from": "now"})
    check("2. an unknown member is ignored and \"from\": \"now\" is accepted", r == 201, str((r, b)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 3 and 4. a created endpoint, and it starts from now
    d = fresh_dir()
    ra, rb = Receiver(), Receiver()
    sec_a = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    psql(f"insert into endpoints values (0, '127.0.0.1', {ra.port}, '{sec_a}')")
    psql("select setval('endpoint_ids', 0)")
    ra.key = sec_a
    svc = start(d)
    for n in range(1, 6):
        post_event(svc, n)
    check("4. (endpoint 0 from the table gets the five events)", wait_for(lambda: sorted(ra.seen) == [1, 2, 3, 4, 5], 8), str(ra.seen))
    wait_for(lambda: get(svc, "/endpoints")[1][0]["cursor"] == 5, 5)
    r, b = create(svc, {"host": "127.0.0.1", "port": rb.port})
    check("3. 201 with an id, the host, the port and a secret", r == 201 and b["host"] == "127.0.0.1" and b["port"] == rb.port and isinstance(b["id"], int), str((r, b)))
    check("3. the secret is whsec_ and the base64 of 24 bytes", re.fullmatch(r"whsec_[A-Za-z0-9+/]{32}", b["secret"]) is not None and len(base64.b64decode(b["secret"][6:])) == 24, b["secret"])
    check("3. the id is the sequence's next one, above the one in the table", b["id"] == 1, str(b))
    check("3. the row in the table is what the answer said", psql(f"select host, port, secret from endpoints where id = {b['id']}") == [("127.0.0.1", str(rb.port), b["secret"])], str(psql("select * from endpoints")))
    rb.key = b["secret"]
    check("4. it starts from now: its cursor is 5 (the last event)", b["cursor"] == 5 and b["from"] == "now", str(b))
    r1, one = get(svc, f"/endpoints/{b['id']}")
    check("3. GET /endpoints/:id says id, port, cursor, disabled, paused and failing_since, and neither the secret nor the host",
          r1 == 200 and one == {"id": b["id"], "port": rb.port, "cursor": 5, "disabled": False, "paused": False, "failing_since": 0}, str((r1, one)))
    check("3. GET /endpoints lists both", [e["id"] for e in get(svc, "/endpoints")[1]] == [0, b["id"]], str(get(svc, "/endpoints")))
    check("3. GET /endpoints/x is a 400 and /endpoints/99 a 404", get(svc, "/endpoints/x")[0] == 400 and get(svc, "/endpoints/99")[0] == 404)
    post_event(svc, 6)
    check("4. event 6 reaches both, signed with the secret each was given", wait_for(lambda: 6 in ra.seen and 6 in rb.seen, 8) and ra.bad == 0 and rb.bad == 0, str((ra.seen, rb.seen, ra.bad, rb.bad)))
    time.sleep(0.5)
    check("4. the new endpoint was sent nothing before event 6, and the old one nothing twice", rb.seen == [6] and sorted(ra.seen) == [1, 2, 3, 4, 5, 6], str((ra.seen, rb.seen)))
    r, c = create(svc, {"host": "127.0.0.1", "port": rb.port, "secret": "whsec_" + base64.b64encode(b"my own secret bytes").decode()})
    check("3. a secret the request brings is kept as it came", r == 201 and c["secret"] == "whsec_" + base64.b64encode(b"my own secret bytes").decode() and c["id"] == 2, str((r, c)))
    # 10 (part). the secret and the token are in no read
    reads = json.dumps([get(svc, p)[1] for p in ("/endpoints", "/endpoints/1", "/endpoints/2", "/stats", "/config")])
    check("10. no read has a secret, a host or the token", b["secret"] not in reads and sec_a not in reads and TOKEN not in reads and "127.0.0.1" not in reads, reads[:200])
    stop(svc)

    # 5. after a restart
    svc = start(d)
    check("5. after a restart the created endpoints are there, with their cursors", [(e["id"], e["cursor"]) for e in get(svc, "/endpoints")[1]] == [(0, 6), (1, 6), (2, 6)], str(get(svc, "/endpoints")))
    time.sleep(0.6)
    check("5. nothing was sent again", rb.seen == [6] and sorted(ra.seen) == [1, 2, 3, 4, 5, 6], str((ra.seen, rb.seen)))
    stop(svc)
    check("5. the log says each created endpoint's slot, id and starting cursor", (1, 1, 5) in created_records(d) and (2, 2, 6) in created_records(d), str(created_records(d)))
    shutil.rmtree(d)

    # 4b. the first endpoint of a service that had none
    psql("truncate endpoints")
    d = fresh_dir()
    svc = start(d)
    for n in range(1, 6):
        post_event(svc, n)
    rc = Receiver()
    r, b = create(svc, {"host": "127.0.0.1", "port": rc.port})
    check("4. a service with no endpoints and five events: the first endpoint starts at 5", r == 201 and b["cursor"] == 5, str((r, b)))
    rc.key = b["secret"]
    post_event(svc, 6)
    check("4. ... and gets event 6, and none of 1 to 5", wait_for(lambda: rc.seen == [6], 8) and rc.bad == 0, str((rc.seen, rc.bad)))
    time.sleep(0.4)
    check("4. ... once", rc.seen == [6], str(rc.seen))
    stop(svc)
    shutil.rmtree(d)

    # 6. ids are not given twice
    psql("truncate endpoints")
    psql("drop sequence endpoint_ids")
    psql("create sequence endpoint_ids minvalue 0 start 0")
    d = fresh_dir()
    svc = start(d)
    post_event(svc, 1)
    ids = [create(svc, good)[1]["id"] for _ in range(3)]
    check("6. three endpoints get 0, 1, 2", ids == [0, 1, 2], str(ids))
    psql("delete from endpoints where id = 2")
    r, b = create(svc, good)
    check("6. after the highest row is deleted the next id is 3, not 2", r == 201 and b["id"] == 3, str((r, b)))
    psql("insert into endpoints values (50, 'hand', 9, 'whsec_" + base64.b64encode(b"x" * 16).decode() + "')")
    r, b = create(svc, good)
    check("6. after a row inserted by hand with id 50 the next is 51", r == 201 and b["id"] == 51, str((r, b)))
    psql("select setval('endpoint_ids', 100, false)")
    r, b = create(svc, good)
    check("6. an id of 100 has a slot below 62, and is read by its id (cursor 1, the last event; port as given)",
          r == 201 and b["id"] == 100 and get(svc, "/endpoints/100")[1] == {"id": 100, "port": 9, "cursor": 1, "disabled": False, "paused": False, "failing_since": 0}, str((r, b, get(svc, "/endpoints"))))
    check("6. GET /endpoints says ids, not slots", [e["id"] for e in get(svc, "/endpoints")[1]][-1] == 100, str(get(svc, "/endpoints")))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 6b. an id the log remembers is a different endpoint: it does not resume
    d = fresh_dir()
    re7 = Receiver()
    sec7 = "whsec_" + base64.b64encode(b"z" * 16).decode()
    psql(f"insert into endpoints values (7, '127.0.0.1', {re7.port}, '{sec7}')")
    svc = start(d)
    for n in range(1, 4):
        post_event(svc, n)
    wait_for(lambda: sorted(re7.seen) == [1, 2, 3], 8)
    stop(svc)
    psql("delete from endpoints")
    svc = start(d)
    for n in range(4, 6):
        post_event(svc, n)
    psql("select setval('endpoint_ids', 7, false)")
    re7b = Receiver()
    r, b = create(svc, {"host": "127.0.0.1", "port": re7b.port})
    check("6b. the sequence gives 7, an id the log remembers", r == 201 and b["id"] == 7 and b["cursor"] == 5, str((r, b)))
    re7b.key = b["secret"]
    post_event(svc, 6)
    check("6b. the new endpoint 7 starts from now: it gets event 6 and not 4 and 5 (which the old 7 would have)", wait_for(lambda: re7b.seen == [6], 8), str(re7b.seen))
    time.sleep(0.4)
    check("6b. ... and the old one's receiver is sent nothing", re7b.seen == [6] and sorted(re7.seen) == [1, 2, 3], str((re7b.seen, re7.seen)))
    stop(svc)
    check("6b. the slot the log remembered for 7 was freed (removed) before it was given again", (7, 0, 0) in records(d, 11), str(records(d, 11)))
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 7. a database that refuses
    d = fresh_dir()
    svc = start(d)
    psql("create or replace function refuse() returns trigger language plpgsql as $$ begin raise exception 'no'; end $$")
    psql("create trigger refuse before insert on endpoints for each row execute function refuse()")
    before = get(svc, "/stats")[1]["endpoints"]
    r, b = create(svc, good)
    check("7. a refused insert is a 503", r == 503, str((r, b)))
    check("7. nothing changed in the service", get(svc, "/stats")[1]["endpoints"] == before and get(svc, "/endpoints")[1] == [], str(get(svc, "/endpoints")))
    check("7. ... or in the log", created_records(d) == [], str(created_records(d)))
    psql("drop trigger refuse on endpoints")
    r, b = create(svc, good)
    check("7. and the next request works", r == 201, str((r, b)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 8. one change at a time
    d = fresh_dir()
    svc = start(d)
    psql("create or replace function slow() returns trigger language plpgsql as $$ begin perform pg_sleep(1.5); return new; end $$")
    psql("create trigger slow before insert on endpoints for each row execute function slow()")
    results = []

    def one():
        results.append(create(svc, good)[0])

    t1 = threading.Thread(target=one)
    t1.start()
    time.sleep(0.4)
    r2, b2 = create(svc, good)
    t1.join()
    check("8. a second request while one waits for the database is a 409, and the first is a 201", r2 == 409 and results == [201], str((r2, b2, results)))
    check("8. one endpoint was created", get(svc, "/stats")[1]["endpoints"] == 1 and psql("select count(*) from endpoints")[0][0] == "1")
    psql("drop trigger slow on endpoints")
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 9. a database that is slow past five seconds
    d = fresh_dir()
    svc = start(d)
    psql("create or replace function slow() returns trigger language plpgsql as $$ begin perform pg_sleep(7); return new; end $$")
    psql("create trigger slow before insert on endpoints for each row execute function slow()")
    t0 = time.time()
    r, b = create(svc, good)
    took = time.time() - t0
    check("9. a database that takes seven seconds: a 504 after about five", r == 504 and 4.5 < took < 6.5, str((r, b, took)))
    check("9. the service does not know the endpoint (it was not told commit)", get(svc, "/stats")[1]["endpoints"] == 0 and created_records(d) == [])
    check("9. it still answers", get(svc, "/healthz")[0] == 200)
    time.sleep(3)
    psql("drop trigger slow on endpoints")
    check("9. the row committed anyway", psql("select count(*) from endpoints")[0][0] == "1")
    stop(svc)
    rd = Receiver()
    psql(f"update endpoints set port = {rd.port}")
    d2 = d
    svc = start(d2)
    post_event(svc, 1)
    check("9. at the next start it is an endpoint, and as a row the service did not create it starts from the beginning of the log", wait_for(lambda: rd.seen == [1], 8), str((rd.seen, svc.lines)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 10. the limits, and no database
    d = fresh_dir()
    sec = "whsec_" + base64.b64encode(b"y" * 16).decode()
    psql(f"insert into endpoints select g, 'h', 9, '{sec}' from generate_series(0, 61) g")
    svc = start(d)
    check("10. 62 endpoints in the table: all are loaded", get(svc, "/stats")[1]["endpoints"] == 62)
    r, b = create(svc, good)
    check("10. a 63rd is a 409", r == 409, str((r, b)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)
    d = fresh_dir()
    svc = start(d, ["--admin-token", TOKEN])
    r, b = create(svc, good)
    check("10. no database named: 503 and why", r == 503 and "database" in b["error"], str((r, b)))
    stop(svc)
    shutil.rmtree(d)
    for short in ("short", "has a space in it"):
        p = subprocess.run([BIN, "--port", "1", "--dir", "/tmp", "--admin-token", short], capture_output=True, timeout=10)
        check(f"10. an admin-token of {short!r} is refused", p.returncode != 0 and b"admin-token" in p.stderr, str(p.stderr))

    psql("truncate endpoints")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all manage checks passed")


main()
