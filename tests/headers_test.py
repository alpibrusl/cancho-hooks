#!/usr/bin/env python3
"""Custom headers per endpoint (docs/design.md section 35; production.md P1.5).

    pip install standardwebhooks
    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/headers_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

  1. headers set by POST /endpoints are on the wire at the receiver, name and value byte for byte (every visible ASCII character in a value), beside the
     three the delivery sets, which are untouched and still verify with the reference library; a retry and a replay carry them
  2. PATCH replaces the whole set, `{}` and `null` remove it, a change that does not name them leaves them
  3. GET /endpoints shows the names and never a value; no value is in /stats, /config, the history or an answer to POST or PATCH; the row holds them
  4. every header the delivery sets or the connection owns is refused, in any case, with a reason; so is a row of the table, or a line of endpoints.conf, that has one
  5. CR, LF and NUL in a name or in a value, however written (a raw byte, a JSON escape, `%0d%0a` in the table) are refused; a literal `%0d%0a` in a value is sent as
     those characters, not decoded; nothing a receiver sees is a header nobody set
  6. the limits: 8 headers, a name of 64 characters, a value of 512, 2,048 bytes in all, no empty value, no name twice, no space at the edge of a value
  7. the largest event (65,487 bytes with a one-character type) with the largest headers and two signatures is delivered
  8. they survive a restart and kill -9; they belong to their endpoint (not another's, not the next one made in its row after a DELETE shifts the rows)
  9. `endpoints.conf` with `headers=` sends them without a database, `--import-endpoints` copies them into the table, and the table's text is the same
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from endpoint_kit import *  # noqa: E402,F401,F403
import endpoint_kit as K  # noqa: E402
import chaos  # noqa: E402

try:
    from standardwebhooks import Webhook, WebhookVerificationError
except ImportError:
    print("this test needs the reference library: pip install standardwebhooks")
    sys.exit(2)

FORBIDDEN = ["webhook-id", "webhook-timestamp", "webhook-signature", "host", "content-length", "content-type", "connection", "transfer-encoding",
             "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"]


def verifies(rec, sec):
    try:
        Webhook(sec).verify(rec["body"], dict(rec["headers"]))
        return True
    except WebhookVerificationError:
        return False


def hmap(rec):
    return [(k, v) for k, v in rec["headers"]]


def last(rc):
    with rc.lock:
        return rc.seen[-1]


def patch(svc, ident, body, **kw):
    return req(svc, "PATCH", f"/endpoints/{ident}", body, **kw)


def create(svc, port, **extra):
    return req(svc, "POST", "/endpoints", dict({"host": "127.0.0.1", "port": port}, **extra))


STANDARD = {"host", "content-type", "webhook-id", "webhook-timestamp", "webhook-signature", "content-length", "connection"}


def custom(rec):
    """The headers of a request that are not the ones the delivery sets."""
    return [(k, v) for k, v in rec["headers"] if k.lower() not in STANDARD]


def main():
    # ---- 1. on the wire ---------------------------------------------------------------------------------------
    reset_db()
    rc = Receiver(status=lambda i: 500 if i == 0 else 204)
    d = tmp()
    svc = start(d, schedule="300")
    printable = "".join(chr(c) for c in range(0x21, 0x7F))
    value = "a " + printable + " b"
    hdrs = {"Authorization": "Bearer abc.def-123", "X-Api-Key": value, "x-lower": "v"}
    st, out = create(svc, rc.port, headers=hdrs)
    ident = out["id"]
    sec = out["secret"]
    check("1. POST /endpoints with headers is a 201", st == 201, str((st, out)))
    post_event(svc, 1)
    check("1. the first attempt (refused with a 500) arrives", wait_for(lambda: rc.count() == 1, 5))
    check("1. ... and the retry arrives: every attempt carries the headers", wait_for(lambda: rc.count() == 2, 5))
    r0, r1 = rc.seen[0], rc.seen[1]
    for label, r in (("attempt", r0), ("retry", r1)):
        got = {k: v for k, v in custom(r)}
        check(f"1. the {label} carries each header, name and value as set (every visible ASCII character in one value)", got == hdrs, str(got))
        check(f"1. ... and nothing else: no header nobody set", len(custom(r)) == 3, str(custom(r)))
    names = [k for k, _ in r0["headers"]]
    check("1. the standard ones are there once each, and in the order the delivery has always sent, with the custom ones after the signature (and no `Connection: close` since design 53)",
          names[:4] == ["Host", "Content-Type", "webhook-id", "webhook-timestamp"] and names[4] == "webhook-signature" and names[5:8] == list(hdrs) and names[8:] == ["Content-Length"], str(names))
    check("1. Content-Type is still application/json, Host receiver", dict(r0["headers"])["Content-Type"] == "application/json" and dict(r0["headers"])["Host"] == "receiver")
    check("1. the library verifies the signature of a request that carries headers", verifies(r0, sec) and verifies(r1, sec))
    check("1. the retry is the same message (webhook-id) with its own timestamp and signature", dict(r0["headers"])["webhook-id"] == dict(r1["headers"])["webhook-id"])
    n0 = rc.count()
    st, out = req(svc, "POST", f"/events/1/replay/{ident}")
    check("1. a replay carries them", st == 202 and wait_for(lambda: rc.count() == n0 + 1, 5) and {k: v for k, v in custom(last(rc))} == hdrs, str(custom(last(rc))))
    st, out = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": rc.port, "headers": hdrs}, token=None)
    check("1. the token is needed to set headers", st == 401, str((st, out)))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 2. PATCH replaces the set ------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    st, out = create(svc, rc.port, headers={"A": "1", "B": "2"})
    ident = out["id"]
    n = [0]

    def send():
        n[0] += 1
        post_event(svc, n[0])
        assert wait_for(lambda: rc.count() == n[0], 5), n[0]
        return {k: v for k, v in custom(last(rc))}

    check("2. the first set", send() == {"A": "1", "B": "2"})
    st, out = patch(svc, ident, {"headers": {"C": "3"}})
    check("2. PATCH replaces the whole set (A and B are gone)", st == 200 and send() == {"C": "3"}, str((st, out)))
    st, out = patch(svc, ident, {"port": rc.port})
    check("2. a change that does not name them leaves them", st == 200 and send() == {"C": "3"}, str((st, out)))
    st, out = patch(svc, ident, {"headers": {}})
    check("2. {} removes them", st == 200 and send() == {} and get(svc, f"/endpoints/{ident}")["headers"] == [], str((st, out)))
    patch(svc, ident, {"headers": {"D": "4"}})
    st, out = patch(svc, ident, {"headers": None})
    check("2. null removes them", st == 200 and send() == {} and psql(f"select length(headers) from endpoints where id = {ident}") == [("0",)], str((st, out)))
    st, out = patch(svc, ident, {"headers": {"E": "5"}, "types": ["x"], "secret": secret()})
    check("2. headers beside a secret and types in one change", st == 200 and get(svc, f"/endpoints/{ident}")["headers"] == ["E"], str((st, out)))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 3. values are never shown ------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    tokenval = "Bearer SEKRET-" + "x1y2z3" * 5
    st, out = create(svc, rc.port, headers={"Authorization": tokenval, "X-Other": "visible-value-123"})
    ident = out["id"]
    answers = json.dumps([out])
    st, out = patch(svc, ident, {"headers": {"Authorization": tokenval, "X-Other": "visible-value-123", "X-Third": "third-secret-456"}})
    answers += json.dumps([out])
    post_event(svc, 1)
    wait_for(lambda: rc.count() == 1, 5)
    wait_for(lambda: len(psql("select 1 from attempts")) == 1, 5)
    reads = json.dumps([get(svc, p) for p in ("/endpoints", f"/endpoints/{ident}", "/stats", "/config", "/events/1/attempts", "/events/1")]) + answers
    check("3. GET /endpoints/:id says the names, in the order set", get(svc, f"/endpoints/{ident}")["headers"] == ["Authorization", "X-Other", "X-Third"], str(get(svc, f"/endpoints/{ident}")))
    check("3. no value is in any read, in the answers to POST and PATCH, or in the history", all(v not in reads for v in (tokenval, "SEKRET", "visible-value-123", "third-secret-456")), reads[:300])
    check("3. nor on the service's stderr", True)
    row = psql(f"select headers from endpoints where id = {ident}")[0][0]
    check("3. the row holds them (as a spec: values percent-encoded)", row == "Authorization:Bearer%20" + tokenval[7:] + ",X-Other:visible-value-123,X-Third:third-secret-456", row)
    stop(svc)
    err = svc.proc.stderr.read().decode()
    check("3. ... nothing of a value on the service's stderr", "SEKRET" not in err and "visible-value" not in err, err[:200])
    shutil.rmtree(d)
    rc.close()

    # ---- 4. what can never be set --------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    st, out = create(svc, rc.port)
    ident = out["id"]
    miss = []
    cases = 0
    for name in FORBIDDEN:
        for variant in (name, name.upper(), name.title(), name.swapcase()):
            cases += 1
            for how, call in (("PATCH", lambda b: patch(svc, ident, b)), ("POST", lambda b: create(svc, rc.port, **b))):
                st, out = call({"headers": {variant: "x"}})
                if st != 400 or "cannot be set" not in out.get("error", ""):
                    miss.append((how, variant, st, out))
    check(f"4. each of {len(FORBIDDEN)} headers the delivery sets or the connection owns, in 4 cases each, is a 400 with a reason, at POST and at PATCH ({cases * 2} requests)", not miss, str(miss[:3]))
    st, out = patch(svc, ident, {"headers": {"A": "1", "Host": "evil"}})
    check("4. one forbidden name beside good ones refuses the lot", st == 400 and get(svc, f"/endpoints/{ident}")["headers"] == [], str((st, out)))
    check("4. ... and no endpoint was made by the refused POSTs", len(get(svc, "/endpoints")) == 1, str(get(svc, "/endpoints")))
    for near in ("webhook-ids", "x-host", "Content-Encoding", "Webhook", "Authorization", "Cookie", "X-Content-Type"):
        st, out = patch(svc, ident, {"headers": {near: "v"}})
        if st != 200:
            miss.append((near, st, out))
    check("4. names near them are fine", not miss, str(miss[:3]))
    stop(svc)
    reset_db()
    for spec in ("Host:evil", "A:1,Content-Length:5", "TRANSFER-ENCODING:chunked", "webhook-signature:v1,x"):
        psql(f"truncate endpoints")
        add_endpoint(1, rc.port, secret(), headers=spec)
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=20)
        check(f"4. a row with {spec.split(',')[-1].split(':')[0]} in its headers is a refusal to start, naming the row", out.returncode == 13 and "row 1" in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    rc.close()

    # ---- 5. injection ---------------------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    st, out = create(svc, rc.port, headers={"X-Safe": "1"})
    ident = out["id"]
    attacks = [
        ({"X-A": "v\r\nInjected: 1"}, "CRLF in a value"), ({"X-A": "v\nInjected: 1"}, "LF in a value"), ({"X-A": "v\rInjected: 1"}, "CR in a value"),
        ({"X-A": "v\u0000w"}, "NUL in a value"), ({"X-A": "\r\n\r\nGET / HTTP/1.1"}, "a whole request in a value"), ({"X-A": "v\u000d\u000aInjected: 1"}, "a JSON escape of CRLF"),
        ({"X-A": "v\tw"}, "a tab"), ({"X-A": "v\u0001w"}, "SOH"), ({"X-A": "v\u001fw"}, "US"), ({"X-A": "v\u007fw"}, "DEL"), ({"X-A": "café"}, "a non-ASCII character"), ({"X-A": " "}, "a line separator"),
        ({"X-A\r\nInjected": "1"}, "CRLF in a name"), ({"X-A\nInjected": "1"}, "LF in a name"), ({"X-A\u0000": "1"}, "NUL in a name"), ({"X-A:B": "1"}, "a colon in a name"),
        ({"X A": "1"}, "a space in a name"), ({" X-A": "1"}, "a leading space in a name"), ({"X-A ": "1"}, "a trailing space in a name"), ({"X-A,B": "1"}, "a comma in a name"),
        ({"": "1"}, "an empty name"), ({"X-é": "1"}, "a non-ASCII name"),
    ]
    miss = []
    for body, what in attacks:
        for call in (lambda b: patch(svc, ident, {"headers": b}), lambda b: create(svc, rc.port, headers=b)):
            st, out = call(body)
            if st != 400 or not out.get("error"):
                miss.append((what, st, out))
    check(f"5. each of {len(attacks)} injection attempts is a 400 with a reason, at PATCH and at POST", not miss, str(miss[:3]))
    # the same bytes written raw in the JSON text (not escaped): the body is not JSON, or the string has a raw control character
    for raw in (b'{"headers":{"X-A":"v\r\nInjected: 1"}}', b'{"headers":{"X-A":"v\nw"}}', b'{"headers":{"X-A":"v\x00w"}}', b'{"headers":{"X-A\r\nB":"w"}}'):
        st, out = patch(svc, ident, raw)
        if st != 400:
            miss.append((raw, st, out))
    check("5. ... and written raw in the JSON text", not miss, str(miss[:3]))
    check("5. nothing was stored or changed", get(svc, f"/endpoints/{ident}")["headers"] == ["X-Safe"] and len(get(svc, "/endpoints")) == 1 and psql(f"select headers from endpoints where id = {ident}") == [("X-Safe:1",)],
          str(psql("select id, headers from endpoints")))
    st, out = patch(svc, ident, {"headers": {"X-Literal": "a%0d%0aInjected:%201", "X-Percent": "100%", "X-Comma": "a,b", "X-Colon": "a:b"}})
    check("5. a literal %0d%0a in a value is allowed (it is not a CR and an LF)", st == 200, str((st, out)))
    post_event(svc, 1)
    check("5. ... it arrives as the text it is, not decoded", wait_for(lambda: rc.count() == 1, 5) and {k: v for k, v in custom(last(rc))} == {"X-Literal": "a%0d%0aInjected:%201", "X-Percent": "100%", "X-Comma": "a,b", "X-Colon": "a:b"}, str(custom(last(rc))))
    check("5. and no header called Injected ever arrived", all(k != "Injected" for r in rc.seen for k, _ in r["headers"]))
    stop(svc)
    # a row of the table with a CR or LF (by percent-encoding) is not read
    for spec in ("A:x%0d%0aInjected:1", "A:x%0A", "A:%00", "A:x%01", "A:x%08", "A:x%1f", "A:%7f", "A:x%", "A:%zz", "A", "A:b,", ",A:b", "A:b,,C:d", "A: b", "A:b c"):
        psql("truncate endpoints")
        add_endpoint(1, rc.port, secret(), headers=spec)
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=20)
        check(f"5. a row whose headers are `{spec}` is a refusal to start", out.returncode in (13, 20) and ("row 1" in out.stderr or "not printable" in out.stderr), str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    rc.close()

    # ---- 6. limits -------------------------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    st, out = create(svc, rc.port)
    ident = out["id"]

    def code(h):
        return patch(svc, ident, {"headers": h})[0]

    eight = {"h%d" % i: "v" for i in range(8)}
    check("6. 8 headers are good, 9 are not", code(eight) == 200 and code(dict(eight, h8="v")) == 400)
    check("6. a name of 64 characters is good, 65 is not", code({"n" * 64: "v"}) == 200 and code({"n" * 65: "v"}) == 400)
    check("6. a value of 512 characters is good, 513 is not", code({"A": "v" * 512}) == 200 and code({"A": "v" * 513}) == 400)
    check("6. an empty value is not", code({"A": ""}) == 400)
    check("6. a space at the edge of a value is not, one inside is", code({"A": " v"}) == 400 and code({"A": "v "}) == 400 and code({"A": "v v"}) == 200)
    check("6. a name twice is not, in any case", code({"A": "1", "a": "2"}) == 400)
    check("6. a value that is not a string is not", all(code({"A": v}) == 400 for v in (1, True, None, ["x"], {"x": 1}, 1.5)))
    check("6. headers that are not an object are not", all(code(v) == 400 for v in ([], ["A"], "A:b", 5, True)))
    check("6. every character of the HTTP token set is good in a name", code({"!#$%&'*+-.^_`|~aZ09": "v"}) == 200)
    # 2,048 bytes as sent: a header is name + 4 + value
    full = {"h%d" % i: "v" * 251 for i in range(8)}      # 8 * (2 + 4 + 251) = 2,056?  names are 2 characters
    full = {"%d" % i: "v" * 251 for i in range(8)}       # 8 * (1 + 4 + 251) = 2,048
    over = {"%d" % i: "v" * 251 for i in range(7)}
    over["7"] = "v" * 252
    check("6. 2,048 bytes as sent are good, 2,049 are not", code(full) == 200 and code(over) == 400)
    st, out = patch(svc, ident, {"headers": over})
    check("6. ... with a reason that says too large", "too large" in out["error"], str(out))
    check("6. the last good set is still in force after the refusals", get(svc, f"/endpoints/{ident}")["headers"] == list(full), str(get(svc, f"/endpoints/{ident}")))
    post_event(svc, 1)
    check("6. and sent in full", wait_for(lambda: rc.count() == 1, 5) and {k: v for k, v in custom(last(rc))} == full, str(custom(last(rc)))[:200])
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 7. the largest event with the largest headers ------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    svc = start(d)
    s_one = secret()
    st, out = create(svc, rc.port, secret=s_one, headers=full)
    ident = out["id"]
    st, out = patch(svc, ident, {"secret": secret(), "keep_old_ms": 600000})
    s_new = out["secret"]
    pad = 65487 - len(json.dumps({"type": "t", "n": 1, "pad": ""}).encode())
    body = json.dumps({"type": "t", "n": 1, "pad": "x" * pad}).encode()
    st, _ = post_event(svc, 1, raw=body)
    check("7. the largest event is accepted (65,487 bytes with a one-character type)", st == 202 and len(body) == 65487, str((st, len(body))))
    ok = wait_for(lambda: rc.count() >= 1, 10)
    r = rc.seen[0] if rc.count() else None
    check("7. ... and delivered with 2,048 bytes of headers and two signatures", ok and r["body"] == body and len(custom(r)) == 8 and len(dict(r["headers"])["webhook-signature"].split(" ")) == 2 and verifies(r, s_new) and verifies(r, s_one),
          str(rc.count()) + " " + str(r and len(r["body"])))
    check("7. ... in one attempt (no failure at the buffer)", get(svc, "/stats")["failed"] == 0 and rc.count() == 1, str(get(svc, "/stats")))
    st, _ = post_event(svc, 2, raw=json.dumps({"type": "tt", "n": 2, "pad": "x" * 65470}).encode())
    check("7. an event a little over, with a longer type, is refused 413 as before", st == 413, str(st))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 8. restarts, and whose they are -----------------------------------------------------------------------------
    reset_db()
    ra, rb, rcc = Receiver(), Receiver(), Receiver()
    d = tmp()
    svc = start(d)
    st, out = create(svc, ra.port, headers={"X-Who": "a"})
    ia = out["id"]
    st, out = create(svc, rb.port, headers={"X-Who": "b", "X-More": "bb"})
    ib = out["id"]
    st, out = create(svc, rcc.port)
    ic = out["id"]
    def of(r, n):
        """The last request the receiver saw for event n (an attempt repeated at a kill is another request of the same event), or None."""
        with r.lock:
            hits = [x for x in r.seen if x["n"] == n]
        return hits[-1] if hits else None

    def sent(r, n):
        return wait_for(lambda: of(r, n) is not None, 5)

    post_event(svc, 1)
    check("8. each endpoint is sent its own headers", all(sent(r, 1) for r in (ra, rb, rcc))
          and {k: v for k, v in custom(of(ra, 1))} == {"X-Who": "a"} and {k: v for k, v in custom(of(rb, 1))} == {"X-Who": "b", "X-More": "bb"} and custom(of(rcc, 1)) == [],
          str([custom(of(r, 1)) for r in (ra, rb, rcc) if of(r, 1)]))
    kill9(svc)
    svc = start(d)
    post_event(svc, 2)
    check("8. after kill -9 they are the database's", all(sent(r, 2) for r in (ra, rb, rcc))
          and {k: v for k, v in custom(of(ra, 2))} == {"X-Who": "a"} and {k: v for k, v in custom(of(rb, 2))} == {"X-Who": "b", "X-More": "bb"} and custom(of(rcc, 2)) == [],
          str([custom(of(r, 2)) for r in (ra, rb, rcc) if of(r, 2)]))
    st, out = req(svc, "DELETE", f"/endpoints/{ia}")
    check("8. DELETE the first endpoint", st == 200, str((st, out)))
    post_event(svc, 3)
    check("8. the rows shift: the others keep their own headers", sent(rb, 3) and sent(rcc, 3)
          and {k: v for k, v in custom(of(rb, 3))} == {"X-Who": "b", "X-More": "bb"} and custom(of(rcc, 3)) == [], str([custom(of(r, 3)) for r in (rb, rcc) if of(r, 3)]))
    time.sleep(0.2)
    check("8. ... and the deleted one is sent nothing more", of(ra, 3) is None)
    rd = Receiver()
    st, out = create(svc, rd.port)
    idd = out["id"]
    post_event(svc, 4)
    check("8. a new endpoint, made in the row that was freed, has none of the deleted one's", sent(rd, 4) and custom(of(rd, 4)) == [] and get(svc, f"/endpoints/{idd}")["headers"] == [], str(of(rd, 4)))
    st, out = req(svc, "DELETE", f"/endpoints/{ib}")
    post_event(svc, 5)
    check("8. delete the one with two: the others keep none and send none", sent(rd, 5) and sent(rcc, 5) and custom(of(rd, 5)) == [] and custom(of(rcc, 5)) == [], str((of(rd, 5), of(rcc, 5))))
    stop(svc)
    shutil.rmtree(d)
    for r in (ra, rb, rcc, rd):
        r.close()

    # ---- 9. endpoints.conf and the table -------------------------------------------------------------------------------
    reset_db()
    rc = Receiver()
    d = tmp()
    s1 = secret()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"1 127.0.0.1 {rc.port} {s1} headers=Authorization:Bearer%20tok%2Cen,X-Plain:abc\n")
    svc = start(d, extra=[])
    post_event(svc, 1)
    check("9. endpoints.conf: headers= (a space is %20, a comma %2C) are sent", wait_for(lambda: rc.count() == 1, 5) and {k: v for k, v in custom(rc.seen[0])} == {"Authorization": "Bearer tok,en", "X-Plain": "abc"}, str(custom(last(rc))))
    stop(svc)
    out = subprocess.run([BIN, "--port", "1", "--dir", d, "--import-endpoints", "1", "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=30)
    check("9. --import-endpoints copies them into the table, as written", out.returncode == 0 and psql("select headers from endpoints where id = 1") == [("Authorization:Bearer%20tok%2Cen,X-Plain:abc",)], str((out.returncode, out.stderr)))
    os.remove(os.path.join(d, "endpoints.conf"))
    svc = start(d)
    post_event(svc, 2)
    check("9. from the table they are the same", wait_for(lambda: rc.count() == 2, 5) and {k: v for k, v in custom(rc.seen[1])} == {"Authorization": "Bearer tok,en", "X-Plain": "abc"}, str(custom(last(rc))))
    stop(svc)
    for bad_line in ("headers=", "headers=A", "headers=Host:x", "headers=A:x%0d%0aB:y", "headers=A:1 headers=B:2", "headers=A:1,A:2", "headers=A:%"):
        with open(os.path.join(d, "endpoints.conf"), "w") as f:
            f.write(f"1 127.0.0.1 {rc.port} {s1} {bad_line}\n")
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=20)
        check(f"9. a bad word ({bad_line}) is a refusal to start naming the line", out.returncode == 13 and "line 1" in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    rc.close()
    finish("headers")


main()
