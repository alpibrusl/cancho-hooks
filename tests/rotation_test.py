#!/usr/bin/env python3
"""Secret rotation with two signatures (docs/design.md section 35; production.md P1.3), checked with the reference library `standardwebhooks`.

    pip install standardwebhooks
    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/rotation_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

  1. before any rotation a delivery carries one signature, and the library verifies it with the secret
  2. `PATCH {"secret": new, "keep_old_ms": N}`: while it lasts every delivery carries `v1,<new> v1,<old>`, the new first; the library verifies it with
     the old secret alone, with the new alone, and not with a third; the header is the two signatures the library itself computes
  3. when it ends (observed through GET /endpoints, which says 0) a delivery carries the new signature only: the old secret no longer verifies
  4. a retry is signed afresh: an event first tried inside the period (two signatures) and retried after it carries one
  5. a restart (`kill -9`) in the period keeps it, with the time it was given; a restart after it ends does not bring the old secret back; a row whose
     period ran out while the service was down is read as no overlap
  6. `keep_old: true` is the setting `rotation-grace-ms` (the default a day, GET /config says), `rotate: true` with `keep_old_ms` makes a secret and keeps the old
  7. `{"keep_old_ms": 0}` ends it now, a time alone moves it, with nothing to keep it is a 400; a second rotation keeps the secret it replaced and
     not the one before; a new secret without a period ends a running one
  8. every way a period can be wrong is a 400 with its reason; neither secret is in GET /endpoints, /stats or /config; the old secret is in the row
  9. a replay in the period carries both; `endpoints.conf` with `old=` and the database's columns give the same
 11. the period is counted from the commit, in memory and in the row: a change the database held 2.5 s keeps its whole period after the answer, also after a kill -9 that reads the row
"""
import json
import os
import shutil
from datetime import datetime, timezone
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


def verifies(rec, sec):
    try:
        Webhook(sec).verify(rec["body"], dict(rec["headers"]))
        return True
    except WebhookVerificationError:
        return False


def sigs(rec):
    h = {k.lower(): v for k, v in rec["headers"]}
    return h["webhook-signature"].split(" ")


def expected(rec, sec):
    """The signature the reference library computes for the request the receiver saw."""
    h = {k.lower(): v for k, v in rec["headers"]}
    return Webhook(sec).sign(h["webhook-id"], datetime.fromtimestamp(int(h["webhook-timestamp"]), tz=timezone.utc), rec["body"].decode())


def last(rc):
    with rc.lock:
        return rc.seen[-1]


def until_of(svc, ident):
    return get(svc, f"/endpoints/{ident}")["secret_old_until"]


def of_event(rc, n):
    """The last request the receiver saw for event n (a kill can repeat an attempt, and that is another request of the same event)."""
    with rc.lock:
        return [r for r in rc.seen if r["n"] == n][-1]


def now_ms():
    return int(time.time() * 1000)


def patch(svc, ident, body, **kw):
    return req(svc, "PATCH", f"/endpoints/{ident}", body, **kw)


def main():
    # ---- 1 to 3. one signature, two, one --------------------------------------------------------------------
    reset_db()
    s1, s2, s3 = secret(), secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d)
    post_event(svc, 1)
    check("1. before a rotation the first event arrives", wait_for(lambda: rc.count() == 1, 5))
    r = last(rc)
    check("1. it carries one signature and the library verifies it under the secret", len(sigs(r)) == 1 and verifies(r, s1) and not verifies(r, s2), str(sigs(r)))
    check("1. GET /endpoints says there is no previous secret", until_of(svc, 1) == 0 and get(svc, "/endpoints/1")["secret_old_until"] == 0)

    t_before = now_ms()
    st, out = patch(svc, 1, {"secret": s2, "keep_old_ms": 4000})
    t_after = now_ms()
    check("2. PATCH with a new secret and a period is a 200 that carries the secret and the time the old one lasts until",
          st == 200 and out["secret"] == s2 and t_before + 4000 <= out["secret_old_until"] <= t_after + 4000, str((st, out)))
    until = out["secret_old_until"]
    check("2. GET shows that time (and never a secret)", until_of(svc, 1) == until, str(get(svc, "/endpoints/1")))
    row = psql("select secret, secret_old, secret_old_until from endpoints where id = 1")[0]
    # the row's time is fixed when the change is sent, the answer's at the commit (section 35): the row's is at most the answer's, and not before the request's plus the period
    check("2. the row has the new secret, the old one and the time (as the database counted it: between the request's and the answer's)",
          row[:2] == (s2, s1) and t_before + 4000 <= int(row[2]) <= until + 50, str((row, t_before, until)))
    post_event(svc, 2)
    check("2. the next event arrives", wait_for(lambda: rc.count() == 2, 5))
    r = last(rc)
    sg = sigs(r)
    check("2. it carries two signatures, space separated, each `v1,`", len(sg) == 2 and all(x.startswith("v1,") for x in sg), str(sg))
    check("2. the first is the new secret's and the second the old one's, as the library computes them", sg == [expected(r, s2), expected(r, s1)], str((sg, expected(r, s2), expected(r, s1))))
    check("2. a receiver that knows only the old secret verifies it", verifies(r, s1))
    check("2. a receiver that knows only the new secret verifies it", verifies(r, s2))
    check("2. a receiver that knows another secret does not", not verifies(r, s3))
    check("2. both are in one webhook-signature header (one message, one id, one timestamp), not two headers", len([1 for k, _ in r["headers"] if k.lower() == "webhook-signature"]) == 1, str(r["headers"]))

    ok = wait_for(lambda: until_of(svc, 1) == 0, 10)
    check("3. when the period is over GET says so", ok and now_ms() >= until)
    post_event(svc, 3)
    check("3. the next event arrives", wait_for(lambda: rc.count() == 3, 5))
    r = last(rc)
    check("3. it carries the new signature only: the old secret no longer verifies it", len(sigs(r)) == 1 and verifies(r, s2) and not verifies(r, s1), str(sigs(r)))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 4. a retry is signed afresh ------------------------------------------------------------------------
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver(status=lambda i: 500 if i == 0 else 204)
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d, schedule="2500")
    st, out = patch(svc, 1, {"secret": s2, "keep_old_ms": 1500})
    post_event(svc, 1)
    check("4. the first attempt (refused, 500) is inside the period: two signatures", wait_for(lambda: rc.count() == 1, 5) and len(sigs(last(rc))) == 2, str(rc.seen))
    check("4. the retry, after the period, is signed with the new secret only", wait_for(lambda: rc.count() == 2, 10) and len(sigs(last(rc))) == 1 and verifies(last(rc), s2) and not verifies(last(rc), s1),
          str([sigs(r) for r in rc.seen]))
    check("4. ... and it is the same event and message id", rc.events() == [1, 1] and dict(rc.seen[0]["headers"])["webhook-id"] == dict(rc.seen[1]["headers"])["webhook-id"])
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 5. restarts ----------------------------------------------------------------------------------------
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d)
    st, out = patch(svc, 1, {"secret": s2, "keep_old_ms": 60000})
    until = out["secret_old_until"]
    post_event(svc, 1)
    wait_for(lambda: rc.count() == 1, 5)
    kill9(svc)
    svc = start(d)
    row_until = int(psql("select secret_old_until from endpoints where id = 1")[0][0])
    check("5. after kill -9 the period is the one the row has (its time as the database counted it, at most the answer's)",
          until_of(svc, 1) == row_until and until - 1000 <= row_until <= until + 50, str((until_of(svc, 1), row_until, until)))
    post_event(svc, 2)
    check("5. ... and the next event carries both signatures", wait_for(lambda: 2 in rc.events(), 5) and len(sigs(of_event(rc, 2))) == 2 and verifies(of_event(rc, 2), s1) and verifies(of_event(rc, 2), s2), str([sigs(r) for r in rc.seen]))
    stop(svc)
    # the period ends while the service is down
    psql(f"update endpoints set secret_old_until = {now_ms() - 1000} where id = 1")
    svc = start(d)
    check("5. a row whose period ran out while the service was down is read as no overlap", until_of(svc, 1) == 0, str(get(svc, "/endpoints/1")))
    post_event(svc, 3)
    check("5. ... and signs with the new secret only", wait_for(lambda: 3 in rc.events(), 5) and len(sigs(of_event(rc, 3))) == 1 and verifies(of_event(rc, 3), s2) and not verifies(of_event(rc, 3), s1), str([sigs(r) for r in rc.seen]))
    # the period set from the other side of a restart: a short one that ends with the service stopped
    st, out = patch(svc, 1, {"secret": secret(), "keep_old_ms": 1500})
    stop(svc)
    time.sleep(2.0)
    svc = start(d)
    check("5. a period that ended while the service was stopped is over at the next start", until_of(svc, 1) == 0)
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 6. the default period, and rotate ------------------------------------------------------------------
    reset_db()
    s1 = secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d)
    check("6. GET /config says rotation-grace-ms: a day unless set", get(svc, "/config")["rotation-grace-ms"] == 86400000, str(get(svc, "/config")))
    t0 = now_ms()
    st, out = patch(svc, 1, {"secret": secret(), "keep_old": True})
    check("6. keep_old: true keeps the old secret for the default: a day", st == 200 and t0 + 86400000 <= out["secret_old_until"] <= now_ms() + 86400000, str((st, out)))
    stop(svc)
    svc = start(d, extra=pg_flags() + ["--admin-token", TOKEN, "--rotation-grace-ms", "2500"])
    check("6. the setting is read back", get(svc, "/config")["rotation-grace-ms"] == 2500)
    s_cur = psql("select secret from endpoints where id = 1")[0][0]
    t0 = now_ms()
    s_new = secret()
    st, out = patch(svc, 1, {"secret": s_new, "keep_old": True})
    check("6. with the setting at 2,500 ms it is 2,500 ms", st == 200 and t0 + 2500 <= out["secret_old_until"] <= now_ms() + 2500, str((st, out)))
    post_event(svc, 1)
    check("6. the period is over soon, by itself", wait_for(lambda: rc.count() == 1, 5) and len(sigs(last(rc))) == 2 and wait_for(lambda: until_of(svc, 1) == 0, 10))
    # rotate: true makes the secret
    st, out = patch(svc, 1, {"rotate": True, "keep_old_ms": 60000})
    made = out.get("secret", "")
    check("6. rotate: true with a period makes a secret (whsec_ and base64) and keeps the old one", st == 200 and made.startswith("whsec_") and made != s_new and out["secret_old_until"] > now_ms(), str((st, out)))
    post_event(svc, 2)
    check("6. ... both verify: the made secret and the one it replaced", wait_for(lambda: rc.count() == 2, 5) and verifies(last(rc), made) and verifies(last(rc), s_new) and not verifies(last(rc), s_cur), str(sigs(last(rc))))
    st, out = patch(svc, 1, {"rotate": True})
    check("6. rotate: true alone keeps nothing, as before", st == 200 and "secret_old_until" not in out and until_of(svc, 1) == 0, str((st, out)))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 7. end it, move it, rotate twice -----------------------------------------------------------------
    reset_db()
    s1, s2, s3, s4 = secret(), secret(), secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d)
    st, out = patch(svc, 1, {"keep_old_ms": 5000})
    check("7. with no previous secret a period alone is a 400 saying so", st == 400 and "previous secret" in out["error"], str((st, out)))
    st, out = patch(svc, 1, {"keep_old_ms": 0})
    check("7. ... but ending nothing is harmless: a 200", st == 200, str((st, out)))
    patch(svc, 1, {"secret": s2, "keep_old_ms": 60000})
    st, out = patch(svc, 1, {"secret": s3, "keep_old_ms": 60000})
    post_event(svc, 1)
    check("7. a second rotation keeps the secret it replaced (s2) and not the one before (s1)", wait_for(lambda: rc.count() == 1, 5) and len(sigs(last(rc))) == 2 and verifies(last(rc), s3) and verifies(last(rc), s2) and not verifies(last(rc), s1), str(sigs(last(rc))))
    st, out = patch(svc, 1, {"keep_old_ms": 90000})
    check("7. a period alone moves the time of the previous secret", st == 200 and out["secret_old_until"] > now_ms() + 80000 and until_of(svc, 1) == out["secret_old_until"], str((st, out)))
    check("7. ... in the row too (as the database counted it)", out["secret_old_until"] - 1000 <= int(psql("select secret_old_until from endpoints where id = 1")[0][0]) <= out["secret_old_until"] + 50)
    st, out = patch(svc, 1, {"keep_old_ms": 0})
    check("7. {keep_old_ms: 0} ends it now", st == 200 and until_of(svc, 1) == 0 and psql("select secret_old, secret_old_until from endpoints where id = 1") == [("", "0")], str(psql("select secret_old, secret_old_until from endpoints where id = 1")))
    post_event(svc, 2)
    check("7. ... the next event carries the new signature only", wait_for(lambda: rc.count() == 2, 5) and len(sigs(last(rc))) == 1 and verifies(last(rc), s3) and not verifies(last(rc), s2), str(sigs(last(rc))))
    patch(svc, 1, {"secret": s4, "keep_old_ms": 60000})
    st, out = patch(svc, 1, {"secret": secret()})
    check("7. a new secret without a period ends a running one", st == 200 and until_of(svc, 1) == 0 and psql("select secret_old from endpoints where id = 1") == [], str((st, out)))
    post_event(svc, 3)
    check("7. ... one signature", wait_for(lambda: rc.count() == 3, 5) and len(sigs(last(rc))) == 1)
    st, out = patch(svc, 1, {"host": "127.0.0.1", "keep_old_ms": 10})
    check("7. a period beside only a new address, with nothing to keep, is a 400 too", st == 400, str((st, out)))
    st, out = patch(svc, 1, {"secret": s1, "keep_old_ms": 60000, "port": rc.port})
    check("7. a secret and an address and a period in one change", st == 200 and out["port"] == rc.port and out["secret"] == s1 and out["secret_old_until"] > now_ms(), str((st, out)))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 8. refusals, and what is never shown ----------------------------------------------------------------
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    d = tmp()
    svc = start(d)
    bad = [
        ({"secret": s2, "keep_old_ms": -1}, "negative"), ({"secret": s2, "keep_old_ms": 2592000001}, "over 30 days"), ({"secret": s2, "keep_old_ms": "5000"}, "a string"),
        ({"secret": s2, "keep_old_ms": 1.5}, "a fraction"), ({"secret": s2, "keep_old_ms": None}, "null"), ({"secret": s2, "keep_old_ms": True}, "a bool"),
        ({"secret": s2, "keep_old": "yes"}, "keep_old a string"), ({"secret": s2, "keep_old": 1}, "keep_old a number"), ({"secret": s2, "keep_old": True, "keep_old_ms": 5}, "both"),
        ({"secret": s2, "rotate": True, "keep_old_ms": 5}, "rotate and a secret"),
    ]
    miss = []
    for body, what in bad:
        st, out = patch(svc, 1, body)
        if st != 400 or not out.get("error"):
            miss.append((what, st, out))
    check(f"8. each of {len(bad)} wrong periods is a 400 with a reason", not miss, str(miss[:3]))
    check("8. ... and the row is as it was", psql("select secret, secret_old, secret_old_until from endpoints where id = 1") == [(s1, "", "0")], str(psql("select secret, secret_old, secret_old_until from endpoints where id = 1")))
    st, out = patch(svc, 1, {"secret": s2, "keep_old_ms": 2592000000})
    check("8. 30 days exactly is allowed", st == 200)
    reads = json.dumps([get(svc, p) for p in ("/endpoints", "/endpoints/1", "/stats", "/config")])
    check("8. neither secret is in any read", s1 not in reads and s2 not in reads and s1[6:] not in reads and s2[6:] not in reads)
    st, _ = patch(svc, 1, {"secret": secret(), "keep_old_ms": 5}, token=None)
    check("8. the token is needed", st == 401)
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    # ---- 9. replay, endpoints.conf, the roster ---------------------------------------------------------------
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s2, old=s1, until=now_ms() + 600000)
    add_endpoint(2, rc.port, s2, old=s1, until=now_ms() - 600000)
    d = tmp()
    svc = start(d)
    check("9. the rows' columns are read at the start: endpoint 1 has a previous secret, endpoint 2's ran out",
          until_of(svc, 1) > now_ms() and until_of(svc, 2) == 0, str(get(svc, "/endpoints")))
    post_event(svc, 1)
    check("9. an event is sent to both: two signatures at one, one at the other", wait_for(lambda: rc.count() == 2, 5) and sorted(len(sigs(r)) for r in rc.seen) == [1, 2], str([sigs(r) for r in rc.seen]))
    n0 = rc.count()
    st, out = req(svc, "POST", "/events/1/replay/1")
    check("9. a replay in the period carries both signatures", st == 202 and wait_for(lambda: rc.count() == n0 + 1, 5) and len(sigs(last(rc))) == 2 and verifies(last(rc), s1) and verifies(last(rc), s2), str([sigs(r) for r in rc.seen]))
    stop(svc)
    shutil.rmtree(d)
    rc.close()

    reset_db()
    rc = Receiver()
    d = tmp()
    s1, s2 = secret(), secret()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"1 127.0.0.1 {rc.port} {s2} old={s1}@{now_ms() + 600000}\n2 127.0.0.1 {rc.port} {s2} old={s1}@{now_ms() - 5}\n")
    svc = start(d, extra=[])
    post_event(svc, 1)
    check("9. endpoints.conf: old=<secret>@<until> gives the overlap, and a time in the past none", wait_for(lambda: rc.count() == 2, 5) and sorted(len(sigs(r)) for r in rc.seen) == [1, 2], str([sigs(r) for r in rc.seen]))
    stop(svc)
    out = subprocess.run([BIN, "--port", "1", "--dir", d, "--import-endpoints", "1", "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=30)
    rows = psql("select id, secret_old = '" + s1 + "', secret_old_until > 0 from endpoints order by id")
    check("9. --import-endpoints copies it into the table", out.returncode == 0 and rows == [("1", "t", "t"), ("2", "t", "t")], str((out.returncode, out.stderr, rows)))
    for bad_line in ("old=" + s1, "old=" + s1 + "@", "old=" + s1 + "@12x", "old=@5", "old=" + s1 + "@0", "old=not*base64@100"):
        with open(os.path.join(d, "endpoints.conf"), "w") as f:
            f.write(f"1 127.0.0.1 {rc.port} {s2} {bad_line}\n")
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=20)
        check(f"9. a bad old= ({bad_line[:12]}...{bad_line[-6:]}) is a refusal to start naming the line", out.returncode == 13 and "line 1" in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    rc.close()
    # ---- 10. a previous secret belongs to its endpoint ------------------------------------------------------------
    reset_db()
    s1, s2 = secret(), secret()
    ra, rb, rn = Receiver(), Receiver(), Receiver()
    add_endpoint(1, ra.port, s2, old=s1, until=now_ms() + 600000)
    add_endpoint(2, rb.port, s2, old=s1, until=now_ms() + 600000)
    d = tmp()
    svc = start(d)
    st, out = req(svc, "DELETE", "/endpoints/1")
    st2, made = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": rn.port, "secret": s2})
    post_event(svc, 1)
    check("10. after a DELETE shifts the rows and a new endpoint takes the last: the new one signs once, the survivor still twice",
          st == 200 and st2 == 201 and wait_for(lambda: ra.count() == 0 and rb.count() == 1 and rn.count() == 1, 5) and len(sigs(last(rb))) == 2 and len(sigs(last(rn))) == 1 and verifies(last(rn), s2) and not verifies(last(rn), s1),
          str((st, st2, [sigs(r) for r in rb.seen], [sigs(r) for r in rn.seen])))
    check("10. ... GET agrees", until_of(svc, 2) > now_ms() and get(svc, f"/endpoints/{made['id']}")["secret_old_until"] == 0, str(get(svc, "/endpoints")))
    stop(svc)
    shutil.rmtree(d)
    for r in (ra, rb, rn):
        r.close()

    # ---- 11. the period is counted from the commit, not from the request ---------------------------------------------
    # A change that waits for the database (here 2.5 s, through a proxy that holds every byte) keeps its whole overlap: counted from the request,
    # the soak's rotation held 4 s by an unreachable database lost 4 s of it. The period is 8 s, so that two moments fit between where counting
    # from the request would end it (the answer + 5.5 s) and where it really ends (the answer + 8 s): an event while the service lives, and one after
    # a `kill -9` right after the answer, which reads the row (the soak's second finding: the row had the request's time and the restart ended the overlap
    # early, 64 deliveries with one signature).
    reset_db()
    s1, s2 = secret(), secret()
    rc = Receiver()
    add_endpoint(1, rc.port, s1)
    from pgproxy import PgProxy
    px = PgProxy(PG_HOST, PG_PORT)
    flags = ["--pg-host", "127.0.0.1", "--pg-port", str(px.port), "--pg-user", PG_USER, "--pg-database", PG_DB, "--admin-token", TOKEN]
    if PG_PASSWORD:
        flags += ["--pg-password", PG_PASSWORD]
    d = tmp()
    svc = start(d, extra=flags)
    px.mode = "freeze"
    t_asked = now_ms()
    import threading
    threading.Timer(2.5, px.restore).start()
    st, out = patch(svc, 1, {"secret": s2, "keep_old_ms": 8000})
    t_answered = now_ms()
    check(f"11. a PATCH held by the database for {t_answered - t_asked} ms is a 200 whose period ends 8000 ms after the commit, not after the request",
          st == 200 and t_answered - t_asked >= 2000 and t_answered + 8000 - 300 <= out["secret_old_until"] <= t_answered + 8000, str((st, out, t_asked, t_answered)))
    row = psql("select secret_old_until from endpoints where id = 1")[0][0]
    check("11. the row's time is counted by the database when the statement ran, from the commit: the answer's plus the period, not the request's",
          t_answered + 8000 - 400 <= int(row) <= t_answered + 8000 + 100, str((row, t_asked, t_answered)))
    while now_ms() < t_answered + 6200:
        time.sleep(0.05)
    post_event(svc, 1)
    check("11. an event 6.2 s after the answer (past the request's time plus the period) still carries both signatures",
          wait_for(lambda: rc.count() == 1, 5) and len(sigs(last(rc))) == 2 and verifies(last(rc), s1) and verifies(last(rc), s2), str([sigs(r) for r in rc.seen]))
    # the restart reads the row: the service is killed and started again, and an event before the real end still carries both
    kill9(svc)
    px.restore()
    svc = start(d, extra=flags)
    late = now_ms() >= t_answered + 7600
    post_event(svc, 2)
    if late:
        print("skip 11. the restart took too long to judge the row against the period's end")
        rc.count()
    else:
        check("11. after a kill -9 the restart reads the row: an event before the real end still carries both signatures (the request's time would have ended it)",
              wait_for(lambda: rc.count() >= 2, 5) and len(sigs(last(rc))) == 2 and verifies(last(rc), s1) and verifies(last(rc), s2), str([sigs(r) for r in rc.seen]))
    stop(svc)
    shutil.rmtree(d)
    rc.close()
    px.alive = False
    finish("rotation")


main()
