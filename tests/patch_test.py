#!/usr/bin/env python3
"""PATCH /endpoints/:id (docs/design.md section 25.4, slice 3).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/patch_test.py build/hooks

  1. who may call it, and what it may name: 403 with no admin-token, 401 for a missing or wrong one, 404 for an id that is not there, 400
     for an id that is not a number, and a 400 with its reason for each body that is not a change
  2. a new host and port reach the next attempt and not the one on the wire (a receiver that holds its answer for a second is sent the
     event, the endpoint is moved, and the held attempt still ends there: delivered, and the new address is not sent that event)
  3. a new secret signs the next attempt: the receiver verifies it with the new secret and not the old; the answer carries it once
  4. a retry of an event first tried under the old secret is signed with the new one
  5. `"rotate": true` makes a secret (`whsec_` and base64), puts it in the row and in the answer, and the next event verifies under it
  6. a database that refuses the update (a trigger): 503, and the service still delivers to the old address with the old secret
  7. a row that is gone: 404 and nothing changed in the service
  8. one change at a time: a second PATCH, and a POST /endpoints, while one waits for the database are 409
  9. after a restart the endpoint is what the database says (the new address and secret), with its cursor
 10. with private hosts not allowed, a PATCH to a loopback host is a 400 and the row is unchanged; to a public one it is a 200
 11. 400 changes of the host, between a 253 byte name and an address, leave the other endpoint's host, port and key intact (the blob
     fills and is compacted several times) and the last change is the one in force
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
import shutil
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


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


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


def signed_with(headers, body, key):
    mid, ts, sig = headers["webhook-id"], headers["webhook-timestamp"], headers["webhook-signature"]
    want = base64.b64encode(hmac.new(base64.b64decode(key[6:]), f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return "v1," + want in sig.split()


class Receiver:
    """Records, for each request, the event number and which of `keys` (name -> secret) its signature verifies under."""

    def __init__(self, keys=None, delay=0.0, fail_first=False):
        self.seen, self.keys, self.delay, self.fail_first = [], dict(keys or {}), delay, fail_first
        self.count = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                n = json.loads(body)["n"]
                ok = sorted(name for name, key in outer.keys.items() if signed_with(self.headers, body, key))
                outer.seen.append((n, ok))
                outer.count += 1
                if outer.delay:
                    time.sleep(outer.delay)
                status = 500 if outer.fail_first and outer.count == 1 else 204
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def events(self):
        return [n for n, _ in self.seen]


def pg_flags():
    f = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


class Svc:
    pass


def start(d, extra=None, schedule="100", deadline="5000", private=True):
    port = chaos.free_port()
    flags = pg_flags() + ["--admin-token", TOKEN] if extra is None else extra
    priv = ["--allow-private-hosts", "1"] if private else []
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, *priv, "--schedule", schedule, "--deadline-ms", deadline, *flags], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    svc = Svc()
    svc.port, svc.proc, svc.lines, svc.exited = port, proc, lines, line != "listening"
    return svc


def stop(svc):
    if not svc.exited:
        svc.proc.terminate()
    svc.proc.wait()


def req(svc, method, path, body=None, token=TOKEN, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=15)
    h = {"Authorization": "Bearer " + token} if token is not None else {}
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (out if raw else (json.loads(out) if out else None))


def patch(svc, ident, body, **kw):
    return req(svc, "PATCH", f"/endpoints/{ident}", body, **kw)


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


def cursors(svc):
    return {e["id"]: e["cursor"] for e in req(svc, "GET", "/endpoints", token=None)[1]}


def reset_db():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("drop trigger if exists hooks_test_trigger on endpoints")
    psql("truncate endpoints")


def row(ident):
    return psql(f"select host, port, secret from endpoints where id = {ident}")[0]


def tmp():
    return tempfile.mkdtemp(prefix="hooks-patch-")


def main():
    reset_db()

    # 1. who may call it, and what it may say
    s1 = secret()
    r1 = Receiver({"s1": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {r1.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    check("1. no token is a 401", patch(svc, 1, {"port": 9}, token=None)[0] == 401)
    check("1. a wrong token is a 401", patch(svc, 1, {"port": 9}, token="wrong-token-value")[0] == 401)
    check("1. a prefix of the token is a 401", patch(svc, 1, {"port": 9}, token=TOKEN[:-1])[0] == 401)
    check("1. an id that is not there is a 404", patch(svc, 77, {"port": 9})[0] == 404)
    check("1. an id that is not a number is a 400", patch(svc, "abc", {"port": 9})[0] == 400)
    check("1. the check of the token comes before the id: 401 for an unknown id too", patch(svc, 77, {"port": 9}, token=None)[0] == 401)
    bad = [
        (b"[]", "an array"), (b'"x"', "a string"), (b"not json", "not json"), (b"{}", "nothing named"), (b'{"color":"red"}', "only an unknown member"),
        (b'{"host":1}', "host not a string"), (b'{"host":""}', "empty host"), (b'{"host":"a b"}', "a space in the host"),
        (b'{"port":0}', "port 0"), (b'{"port":65536}', "port 65536"), (b'{"port":"80"}', "port a string"), (b'{"port":1.5}', "port a fraction"),
        (b'{"port":null}', "port null"), (b'{"secret":"short"}', "short secret"), (b'{"secret":"whsec_!!!!!!!!!!"}', "secret not base64"),
        (b'{"secret":5}', "secret not a string"), (b'{"rotate":false}', "rotate false"), (b'{"rotate":"yes"}', "rotate a string"),
        (b'{"rotate":true,"secret":"whsec_MDEyMzQ1Njc4OWFiY2RlZg=="}', "rotate with a secret"),
    ]
    miss = []
    for body, what in bad:
        st, out = patch(svc, 1, body)
        if st != 400 or not out or not out.get("error"):
            miss.append((what, st, out))
    check(f"1. each of {len(bad)} bodies that are not a change is a 400 with a reason", not miss, str(miss[:3]))
    check("1. ... and the row is as it was", row(1) == ("127.0.0.1", str(r1.port), s1), str(row(1)))
    st, out = patch(svc, 1, {"port": r1.port, "color": "red"})
    check("1. an unknown member beside a valid one is ignored (200)", st == 200 and out["port"] == r1.port and "secret" not in out, str((st, out)))
    stop(svc)
    svc = start(d, extra=pg_flags())
    check("1. with no admin-token configured a PATCH is a 403", patch(svc, 1, {"port": 9}, token=TOKEN)[0] == 403)
    stop(svc)
    svc = start(d, extra=["--admin-token", TOKEN])
    check("1. with no database named a PATCH is a 503", patch(svc, 1, {"port": 9})[0] == 503)
    stop(svc)
    shutil.rmtree(d)

    # 2. a new address reaches the next attempt and not the one on the wire
    reset_db()
    s1 = secret()
    slow, fast = Receiver({"s1": s1}, delay=1.2), Receiver({"s1": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {slow.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    check("2. the first receiver has the event and is holding its answer", wait_for(lambda: slow.events() == [1], 5), str(slow.seen))
    st, out = patch(svc, 1, {"host": "127.0.0.1", "port": fast.port})
    check("2. the move is accepted while an attempt is on the wire", st == 200 and out == {"id": 1, "host": "127.0.0.1", "port": fast.port}, str((st, out)))
    check("2. ... and the row says so", row(1) == ("127.0.0.1", str(fast.port), s1), str(row(1)))
    check("2. the attempt on the wire ends where it began: delivered", wait_for(lambda: cursors(svc) == {1: 1}, 5), str(cursors(svc)))
    time.sleep(0.3)
    check("2. ... and the new address is not sent that event", fast.events() == [], str(fast.seen))
    post_event(svc, 2)
    check("2. the next event goes to the new address and not the old", wait_for(lambda: fast.events() == [2], 5) and slow.events() == [1], str((fast.seen, slow.seen)))
    stop(svc)
    shutil.rmtree(d)

    # 3. a new secret signs the next attempt
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver({"old": s1, "new": s2})
    psql(f"insert into endpoints values (1, '127.0.0.1', {rc.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    check("3. before the change an event is signed with the old secret only", wait_for(lambda: rc.seen == [(1, ["old"])], 5), str(rc.seen))
    st, out = patch(svc, 1, {"secret": s2})
    check("3. the answer carries the secret it was given", st == 200 and out["secret"] == s2 and out["host"] == "127.0.0.1", str((st, out)))
    check("3. the row has it", row(1)[2] == s2, str(row(1)))
    post_event(svc, 2)
    check("3. the next event verifies under the new secret and not the old", wait_for(lambda: len(rc.seen) == 2, 5) and rc.seen[1] == (2, ["new"]), str(rc.seen))
    check("3. no read gives either secret back", s1 not in json.dumps(req(svc, "GET", "/endpoints", token=None)[1]) and s2 not in json.dumps(req(svc, "GET", "/endpoints/1", token=None)[1]))
    stop(svc)
    shutil.rmtree(d)

    # 4. a retry of an event first tried under the old secret
    reset_db()
    s1, s2 = secret(), secret()
    rf = Receiver({"old": s1, "new": s2}, fail_first=True)
    psql(f"insert into endpoints values (1, '127.0.0.1', {rf.port}, '{s1}')")
    d = tmp()
    svc = start(d, schedule="1500")
    post_event(svc, 1)
    check("4. the first attempt is signed with the old secret and fails", wait_for(lambda: rf.seen == [(1, ["old"])], 5), str(rf.seen))
    st, out = patch(svc, 1, {"secret": s2})
    check("4. the secret is changed between the attempt and its retry", st == 200, str((st, out)))
    check("4. the retry of the same event is signed with the new secret", wait_for(lambda: len(rf.seen) == 2, 8) and rf.seen[1] == (1, ["new"]), str(rf.seen))
    stop(svc)
    shutil.rmtree(d)

    # 5. rotate
    reset_db()
    s1 = secret()
    rr = Receiver({"old": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {rr.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    st, out = patch(svc, 1, {"rotate": True})
    made = out.get("secret", "") if out else ""
    check("5. rotate answers a new secret: whsec_ and base64 of 24 bytes, not the old one", st == 200 and made.startswith("whsec_") and len(base64.b64decode(made[6:])) == 24 and made != s1, str((st, out)))
    check("5. ... which is in the row", row(1)[2] == made, str(row(1)))
    rr.keys = {"made": made, "old": s1}
    post_event(svc, 1)
    check("5. the next event verifies under it and not the old secret", wait_for(lambda: rr.seen == [(1, ["made"])], 5), str(rr.seen))
    st2, out2 = patch(svc, 1, {"rotate": True})
    check("5. a second rotation makes another", st2 == 200 and out2["secret"] != made, str((st2, out2)))
    stop(svc)
    shutil.rmtree(d)

    # 6. a database that refuses
    reset_db()
    s1, s2 = secret(), secret()
    ra, rb = Receiver({"old": s1, "new": s2}), Receiver({"old": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {ra.port}, '{s1}')")
    psql("create or replace function hooks_test_refuse() returns trigger as $$ begin raise exception 'no'; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before update on endpoints for each row execute function hooks_test_refuse()")
    d = tmp()
    svc = start(d)
    st, out = patch(svc, 1, {"host": "127.0.0.1", "port": rb.port, "secret": s2})
    check("6. a database that refuses the update is a 503", st == 503, str((st, out)))
    post_event(svc, 1)
    check("6. the service still delivers to the old address with the old secret", wait_for(lambda: ra.seen == [(1, ["old"])], 5) and rb.seen == [], str((ra.seen, rb.seen)))
    psql("drop trigger hooks_test_trigger on endpoints")
    st, out = patch(svc, 1, {"secret": s2})
    check("6. and the next change works", st == 200, str((st, out)))
    stop(svc)
    shutil.rmtree(d)

    # 7. a row that is gone
    reset_db()
    s1 = secret()
    rg = Receiver({"old": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {rg.port}, '{s1}'), (2, '127.0.0.1', {rg.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    psql("delete from endpoints where id = 1")
    st, out = patch(svc, 1, {"port": 9})
    check("7. a row that is gone is a 404 that says so", st == 404 and "gone" in json.dumps(out), str((st, out)))
    post_event(svc, 1)
    check("7. nothing changed in the service: the event still goes to the old address", wait_for(lambda: sorted(rg.events()) == [1, 1], 5), str(rg.seen))
    stop(svc)
    shutil.rmtree(d)

    # 8. one change at a time
    reset_db()
    s1 = secret()
    r8 = Receiver({"old": s1})
    psql(f"insert into endpoints values (1, '127.0.0.1', {r8.port}, '{s1}')")
    psql("create or replace function hooks_test_slow() returns trigger as $$ begin perform pg_sleep(1.5); return new; end $$ language plpgsql")
    psql("create trigger hooks_test_trigger before update on endpoints for each row execute function hooks_test_slow()")
    d = tmp()
    svc = start(d)
    first = {}

    def slow_patch():
        first["r"] = patch(svc, 1, {"port": r8.port})

    t = threading.Thread(target=slow_patch)
    t.start()
    time.sleep(0.4)
    st2, out2 = patch(svc, 1, {"port": r8.port})
    st3, out3 = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": 9})
    t.join()
    check("8. a second PATCH while one waits is a 409", st2 == 409, str((st2, out2)))
    check("8. a POST /endpoints while one waits is a 409", st3 == 409, str((st3, out3)))
    check("8. the first completes", first["r"][0] == 200, str(first))
    psql("drop trigger hooks_test_trigger on endpoints")
    stop(svc)
    shutil.rmtree(d)

    # 9. a restart: the database is the owner
    reset_db()
    s1, s2 = secret(), secret()
    r_old, r_new = Receiver({"old": s1, "new": s2}), Receiver({"old": s1, "new": s2})
    psql(f"insert into endpoints values (1, '127.0.0.1', {r_old.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    wait_for(lambda: r_old.events() == [1], 5)
    wait_for(lambda: cursors(svc) == {1: 1}, 5)
    patch(svc, 1, {"port": r_new.port, "secret": s2})
    stop(svc)
    svc = start(d)
    check("9. after a restart the cursor is kept", cursors(svc) == {1: 1}, str(cursors(svc)))
    post_event(svc, 2)
    check("9. ... and the new address and secret are in force: event 2 goes there, signed with the new secret",
          wait_for(lambda: r_new.seen == [(2, ["new"])], 5) and r_old.events() == [1], str((r_new.seen, r_old.seen)))
    time.sleep(0.3)
    check("9. ... and event 1 is not sent again", r_new.events() == [2], str(r_new.seen))
    stop(svc)
    shutil.rmtree(d)

    # 10. the destination rule
    reset_db()
    d = tmp()
    svc = start(d, private=False)
    st, out = req(svc, "POST", "/endpoints", {"host": "1.1.1.1", "port": 9})
    ident = out["id"] if st == 201 else -1
    before = row(ident)
    st, out = patch(svc, ident, {"host": "127.0.0.1"})
    check("10. a PATCH to a loopback host is a 400 that says why", st == 400 and "public IPv4" in json.dumps(out), str((st, out)))
    st2, out2 = patch(svc, ident, {"host": "localhost"})
    check("10. ... and to a name", st2 == 400, str((st2, out2)))
    check("10. ... and the row is unchanged", row(ident) == before, str((row(ident), before)))
    st, out = patch(svc, ident, {"host": "8.8.8.8", "port": 8443})
    check("10. to a public address it is a 200 and the row follows", st == 200 and row(ident)[:2] == ("8.8.8.8", "8443"), str((st, out, row(ident))))
    st, out = patch(svc, ident, {"port": 9})
    check("10. a change that names only the port keeps the host", st == 200 and out["host"] == "8.8.8.8" and row(ident)[:2] == ("8.8.8.8", "9"), str((st, out, row(ident))))
    st, out = req(svc, "POST", "/endpoints", {"host": "1.1.1.1", "port": 9})
    check("10. a POST /endpoints after PATCHes still creates (the pending change is not mistaken for a patch)",
          st == 201 and out["id"] != ident and row(out["id"])[:2] == ("1.1.1.1", "9") and row(ident)[:2] == ("8.8.8.8", "9"), str((st, out)))
    stop(svc)
    shutil.rmtree(d)

    # 11. the blob fills and is compacted (endpoint 1 changes, endpoint 2 is the one a compaction must move)
    reset_db()
    s1, s2 = secret(), secret()
    keep, mover = Receiver({"k": s1}), Receiver({"m": s2})
    # the endpoint that changes is first in the blob, and its first host (13 bytes) is the length of neither host it is changed to (253, 9),
    # so that every compaction has to move the other one to a place it was not at
    psql(f"insert into endpoints values (1, 'mover.example', 9, '{s2}'), (2, '127.0.0.1', {keep.port}, '{s1}')")
    d = tmp()
    svc = start(d)
    longname = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 53 + ".example"
    assert len(longname) == 253, len(longname)
    bad_change = 0
    for k in range(400):
        host = longname if k % 2 == 0 else "127.0.0.1"
        st, out = patch(svc, 1, {"host": host, "port": 9})
        if st != 200:
            bad_change += 1
    check("11. 400 changes of the host of one endpoint are all accepted", bad_change == 0, str(bad_change))
    st, out = patch(svc, 1, {"host": "127.0.0.1", "port": mover.port})
    post_event(svc, 1)
    check("11. the last change is the one in force: the moved endpoint gets the event, signed with its own key", wait_for(lambda: mover.seen == [(1, ["m"])], 5), str(mover.seen))
    check("11. the other endpoint's host, port and key are intact after the compactions", wait_for(lambda: keep.seen == [(1, ["k"])], 5), str(keep.seen))
    stop(svc)
    shutil.rmtree(d)

    reset_db()
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all patch checks passed")


main()
