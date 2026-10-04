#!/usr/bin/env python3
"""Cancel a waiting replay (docs/design.md section 39.2).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/cancel_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

A replay that fails waits for its next attempt, up to 24 hours; until now there was no way to take it back. Here a replay waits because the service is started with
the schedule `40,600000` (a retry 40 ms after the first failure, the next 10 minutes after that) over dead letters made with the schedule `40`.

  1. `DELETE /events/:id/replay/:endpoint` cancels one: `200 {cancelled: 1}`, the table of waiting replays (`/stats`) has one fewer, the receiver is sent nothing more for
     it, the event is still a dead letter (and says it is not replaying), and a record of kind 16 is in the log; the others keep waiting
  2. `DELETE /endpoints/:id/replays` cancels all of an endpoint's, and only that endpoint's
  3. durable: after `kill -9` the cancelled replay does not come back (not at the restart, not after a second), and the ones not cancelled do
  4. exact across a power cut: the service under the `fsync` shim, a cancel acknowledged, the log cut to what the last `fsync` covered: still cancelled
  5. a replay with an attempt on the wire is not cancelled: `409` for one, `busy` for all; asked again when it has ended it is cancelled
  6. what is not a waiting replay is a 404 (an event with none, an endpoint that is not there, a replay already cancelled), and a bad id a 400; a `GET` is a 405
  7. a cancelled replay can be asked for again and then goes through (delivered once); the cancel does not touch the dead letter's attempts, reason or time
  8. the bound of 32: 32 bulk replays wait, a bulk replay is refused room (taken 0, remaining > 0), cancel-all frees the table, and the next call takes the next 32;
     nothing was lost along the way (every dead letter is sent in the end)
  9. the records, read apart from the service: one kind-16 record for each cancelled replay and none for a refused cancel
 10. retention (design 39.1): a snapshot replaces the outcomes log while two replays wait and one was cancelled; read apart, it holds the dead letters (kind 17), a replay
     record for the two that wait and nothing for the cancelled one; after a kill and a restart (twice) the list is the same, the cancelled one is not replaying and is not sent
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from endpoint_kit import *  # noqa: E402,F401,F403
import endpoint_kit as K  # noqa: E402
import chaos  # noqa: E402

SHIM = os.path.join(K.ROOT, "build", "fsync_shim.so")


def make(svc, port, **extra):
    st, body = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": port, **extra})
    assert st == 201, (st, body)
    return body["id"]


def dead_ids(svc, ident):
    st, body = req(svc, "GET", f"/endpoints/{ident}/dead?limit=1000&order=asc")
    assert st == 200, (st, body)
    return [(e["event"], e["replaying"]) for e in body["dead"]], body


def records(d, kinds):
    """The delivery log's records of the given kinds, read apart from the service: [(kind, slot, event, attempts, fifth)]."""
    recs, _ = chaos.read_log(open(os.path.join(d, "delivery.seg"), "rb").read())
    out = [struct.unpack("<5q", dict(pairs)[b"o"]) for _, pairs in recs]
    return [r for r in out if r[0] in kinds]


def waiting(svc):
    return get(svc, "/stats")["replays"]


def make_dead(d, count, mode, ports=1):
    """A service that makes `count` dead letters at `ports` failing endpoints (events 1 to count), stopped. Answers the endpoint ids."""
    svc = start(d, schedule="40")
    receivers = [Receiver(status=lambda i, n: mode["code"]) for _ in range(ports)]
    ids = [make(svc, r.port) for r in receivers]
    for n in range(1, count + 1):
        post_event(svc, n, "t")
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == count * ports, 30)
    assert ok, get(svc, "/stats")
    stop(svc)
    return ids, receivers


def logcheck_ok(d):
    """scripts/logcheck.py (the restore's check) reads the data directory and finds it consistent: it knows every kind the service writes (16, 17 among them)."""
    v = subprocess.run([sys.executable, os.path.join(K.ROOT, "scripts", "logcheck.py"), "check", d], capture_output=True, text=True)
    return v.returncode == 0, v.stdout[-300:] + v.stderr[-300:]


def sent_after(rc, base):
    return rc.count() - base


def main():
    # ---- 1. one --------------------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 6, mode)
    svc = start(d, schedule="40,600000")
    base = ra.count()
    for n in (1, 2, 3):
        st, _ = req(svc, "POST", f"/events/{n}/replay/{a}")
        assert st == 202
    # each replay is sent twice (the first attempt and the 40 ms retry) and then waits ten minutes
    ok = wait_for(lambda: ra.count() >= base + 6 and waiting(svc) == 3, 10)
    time.sleep(0.5)
    check("1. three replays have each been sent twice and now wait", ok and ra.count() == base + 6 and waiting(svc) == 3, str((ra.count() - base, waiting(svc))))
    dead_before = {e["event"]: e for e in req(svc, "GET", f"/endpoints/{a}/dead?limit=100&order=asc")[1]["dead"]}
    check("1. the dead list says which are replaying", [(i, e["replaying"]) for i, e in sorted(dead_before.items())] == [(1, True), (2, True), (3, True), (4, False), (5, False), (6, False)], str(dead_before.keys()))
    st, r = req(svc, "DELETE", f"/events/2/replay/{a}")
    check("1. DELETE /events/2/replay/:endpoint is a 200 that says cancelled 1, busy 0", st == 200 and r == {"event": 2, "endpoint": a, "cancelled": 1, "busy": 0}, str((st, r)))
    check("1. the table of waiting replays has two", waiting(svc) == 2, str(waiting(svc)))
    dead_after = {e["event"]: e for e in req(svc, "GET", f"/endpoints/{a}/dead?limit=100&order=asc")[1]["dead"]}
    check("1. event 2 is still a dead letter, no longer replaying, with its attempts, reason and time as they were",
          dead_after[2]["replaying"] is False and {k: v for k, v in dead_after[2].items() if k != "replaying"} == {k: v for k, v in dead_before[2].items() if k != "replaying"} and dead_after[1]["replaying"] and dead_after[3]["replaying"], str(dead_after[2]))
    check("1. a record of kind 16 for (the endpoint's slot, event 2) is in the log", [(r[2]) for r in records(d, (16,))] == [2], str(records(d, (16,))))
    n0 = ra.count()
    time.sleep(0.4)
    check("1. the receiver is sent nothing more", ra.count() == n0, str((ra.count(), n0)))
    kill9(svc)
    ents = None
    # ---- 3. durable ------------------------------------------------------------------------------------------------
    svc = start(d, schedule="40,600000")
    check("3. after kill -9 and a restart the cancelled replay stays cancelled and the other two are still waiting", waiting(svc) == 2, str(waiting(svc)))
    ids, _ = dead_ids(svc, a)
    check("3. ... the list says so: 1 and 3 replaying, 2 not", ids == [(1, True), (2, False), (3, True), (4, False), (5, False), (6, False)], str(ids))
    kill9(svc)
    svc = start(d, schedule="40,600000")
    check("3. and a second kill and restart say the same", waiting(svc) == 2 and dead_ids(svc, a)[0][1] == (2, False), str(waiting(svc)))
    # ---- 2. all of an endpoint's, and only its ---------------------------------------------------------------------------
    st, r = req(svc, "DELETE", f"/endpoints/{a}/replays")
    check("2. DELETE /endpoints/:id/replays cancels the two that are left (cancelled 2, busy 0)", st == 200 and r == {"endpoint": a, "cancelled": 2, "busy": 0}, str((st, r)))
    check("2. nothing waits", waiting(svc) == 0, str(waiting(svc)))
    st, r = req(svc, "DELETE", f"/endpoints/{a}/replays")
    check("2. cancelling again is a 200 with nothing cancelled", st == 200 and r["cancelled"] == 0 and r["busy"] == 0, str((st, r)))
    kill9(svc)
    svc = start(d, schedule="40,600000")
    check("3. after a kill all four cancellations hold: nothing waits", waiting(svc) == 0 and all(not rep for _, rep in dead_ids(svc, a)[0]), str(waiting(svc)))
    check("9. the log has one kind-16 record for each of the three cancels", sorted(r[2] for r in records(d, (16,))) == [1, 2, 3], str(records(d, (16,))))
    stop(svc)
    ok, why = logcheck_ok(d)
    check("9. scripts/logcheck.py accepts the directory with the kind-16 records", ok, why)
    shutil.rmtree(d)
    ra.close()

    # ---- 2b. another endpoint's replays are not touched; 7. asking again ---------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a, b), (ra, rb) = make_dead(d, 5, mode, ports=2)
    svc = start(d, schedule="40,600000")
    for n in range(1, 6):
        req(svc, "POST", f"/events/{n}/replay")
    ok = wait_for(lambda: waiting(svc) == 10 and ra.count() >= 10 + 10 and rb.count() >= 10 + 10, 15)
    time.sleep(0.4)
    check("2. ten replays wait, five at each endpoint", ok and waiting(svc) == 10, str((waiting(svc), ra.count(), rb.count())))
    st, r = req(svc, "DELETE", f"/endpoints/{a}/replays")
    check("2. cancelling A's takes A's five and none of B's", st == 200 and r["cancelled"] == 5 and waiting(svc) == 5, str((st, r, waiting(svc))))
    ida, _ = dead_ids(svc, a)
    idb, _ = dead_ids(svc, b)
    check("2. A's dead letters are not replaying, B's all are", not any(rep for _, rep in ida) and all(rep for _, rep in idb) and len(ida) == 5 and len(idb) == 5, str((ida, idb)))
    kill9(svc)
    svc = start(d, schedule="40,600000")
    ida, _ = dead_ids(svc, a)
    idb, _ = dead_ids(svc, b)
    check("3. after a kill, B's five still wait and A's are cancelled", waiting(svc) == 5 and not any(rep for _, rep in ida) and all(rep for _, rep in idb), str((waiting(svc), ida, idb)))
    # a cancelled replay can be asked for again: the receiver is mended, the replay goes through, once
    ra.status = lambda i, n: 204
    mode["code"] = 204
    n0 = ra.count()
    st, _ = req(svc, "POST", f"/events/3/replay/{a}")
    ok = wait_for(lambda: ra.count() == n0 + 1, 10)
    time.sleep(0.4)
    ida, _ = dead_ids(svc, a)
    check("7. a cancelled replay asked for again is sent once and delivered (the dead letter goes)", st == 202 and ok and ra.count() == n0 + 1 and [i for i, _ in ida] == [1, 2, 4, 5], str((st, ra.count() - n0, ida)))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()

    # ---- 4. exact across a power cut -----------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 4, mode)
    env = dict(os.environ, LD_PRELOAD=SHIM)
    svc = start(d, schedule="40,600000", env=env)
    for n in (1, 2, 3, 4):
        req(svc, "POST", f"/events/{n}/replay/{a}")
    wait_for(lambda: waiting(svc) == 4 and ra.count() >= 8 + 8, 10)
    time.sleep(0.4)
    st, r = req(svc, "DELETE", f"/events/3/replay/{a}")
    st2, r2 = req(svc, "DELETE", f"/events/4/replay/{a}")
    kill9(svc)
    path = os.path.join(d, "delivery.seg")
    synced = struct.unpack("<q", open(path + ".synced", "rb").read(8))[0]
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(synced)
    svc = start(d, schedule="40,600000", env=env)
    check("4. two cancels acknowledged, the log cut to what the last fsync covered (%d of %d bytes): still two waiting" % (synced, size), st == 200 and st2 == 200 and waiting(svc) == 2, str((st, st2, waiting(svc), synced, size)))
    ids, _ = dead_ids(svc, a)
    check("4. ... the ones that were not cancelled", ids == [(1, True), (2, True), (3, False), (4, False)], str(ids))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 4b. a bulk replay that was acknowledged survives a power cut
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 6, mode)
    svc = start(d, schedule="40,600000", env=dict(os.environ, LD_PRELOAD=SHIM))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"limit": 4})
    kill9(svc)
    path = os.path.join(d, "delivery.seg")
    synced = struct.unpack("<q", open(path + ".synced", "rb").read(8))[0]
    with open(path, "r+b") as f:
        f.truncate(synced)
    svc = start(d, schedule="40,600000", env=dict(os.environ, LD_PRELOAD=SHIM))
    check("4. a bulk replay that took 4 and was acknowledged, the log cut to the last fsync: 4 replays are waiting", st == 202 and r["taken"] == 4 and waiting(svc) == 4, str((st, r, waiting(svc))))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 5. an attempt on the wire is not cancelled ------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 2, mode)
    ra.delay = 1.2
    svc = start(d, schedule="40,600000")
    n0 = ra.count()
    req(svc, "POST", f"/events/1/replay/{a}")
    wait_for(lambda: ra.count() == n0 + 1, 5)
    st, r = req(svc, "DELETE", f"/events/1/replay/{a}")
    check("5. a replay with an attempt on the wire is a 409 for one", st == 409 and r.get("error"), str((st, r)))
    st, r = req(svc, "DELETE", f"/endpoints/{a}/replays")
    check("5. ... and busy 1, cancelled 0 for all of the endpoint's", st == 200 and r["cancelled"] == 0 and r["busy"] == 1, str((st, r)))
    check("5. ... and it is still waiting", waiting(svc) == 1, str(waiting(svc)))
    ra.delay = 0.0
    # it ends (500 after 1.2 s), is retried 40 ms later, fails, and waits ten minutes
    ok = wait_for(lambda: ra.count() >= n0 + 2 and waiting(svc) == 1, 10)
    time.sleep(0.6)
    st, r = req(svc, "DELETE", f"/events/1/replay/{a}")
    check("5. asked again when the attempt has ended, it is cancelled", ok and st == 200 and r["cancelled"] == 1 and waiting(svc) == 0, str((st, r, waiting(svc))))
    # ---- 6. what is not a waiting replay --------------------------------------------------------------------------------------------------
    st, r = req(svc, "DELETE", f"/events/1/replay/{a}")
    check("6. a replay already cancelled is a 404", st == 404 and r.get("error"), str((st, r)))
    st, r = req(svc, "DELETE", f"/events/2/replay/{a}")
    check("6. an event with no replay waiting is a 404", st == 404, str((st, r)))
    st, r = req(svc, "DELETE", f"/events/1/replay/9999")
    check("6. an endpoint that is not there is a 404", st == 404, str((st, r)))
    st, r = req(svc, "DELETE", f"/endpoints/9999/replays")
    check("6. ... for cancel-all too", st == 404, str((st, r)))
    for path in ("/events/0/replay/%d" % a, "/events/x/replay/%d" % a, "/events/1/replay/x", "/endpoints/x/replays"):
        st, r = req(svc, "DELETE", path)
        check(f"6. DELETE {path} is a 400", st == 400, str((st, r)))
    for method, path in (("GET", f"/events/1/replay/{a}"), ("PUT", f"/events/1/replay/{a}"), ("POST", f"/endpoints/{a}/replays"), ("GET", f"/endpoints/{a}/replays"), ("DELETE", f"/endpoints/{a}/dead"), ("PATCH", f"/endpoints/{a}/dead")):
        st, r = req(svc, method, path)
        check(f"6. {method} {path} is a 405", st == 405, str((st, r)))
    st, r = req(svc, "DELETE", f"/events/1/replay/{a}", token="wrong-wrong-wrong")
    check("6. a wrong token is a 401", st == 401, str(st))
    check("9. the refusals wrote nothing: the only kind-16 record is the one cancel", len(records(d, (16,))) == 1, str(records(d, (16,))))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 8. the bound of 32 ---------------------------------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 100, mode)
    svc = start(d, schedule="40,600000")
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
    check("8. the first call takes 32 and 68 remain", st == 202 and r["taken"] == 32 and r["remaining"] == 68 and r["waiting"] == 32, str((st, r)))
    wait_for(lambda: ra.count() >= 200 + 64, 10)
    time.sleep(0.4)
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
    check("8. a call with the table full takes none (200, taken 0) and says 68 remain", st == 200 and r["taken"] == 0 and r["remaining"] == 68 and r["waiting"] == 32, str((st, r)))
    st, r = req(svc, "POST", "/events/99/replay/%d" % a)
    check("8. and a single replay is a 507, as it always was", st == 507, str((st, r)))
    st, c = req(svc, "DELETE", f"/endpoints/{a}/replays")
    check("8. cancel-all frees the table: 32 cancelled, nothing busy, nothing waits", st == 200 and c["cancelled"] == 32 and c["busy"] == 0 and waiting(svc) == 0, str((st, c)))
    # the receiver is mended: the next replays go through and free the table themselves
    ra.status = lambda i, n: 204
    taken_total, calls, most = 0, 0, 0
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"after": 32})
        calls += 1
        taken_total += r["taken"]
        most = max(most, r["taken"])
        if r["taken"] == 0 and r["remaining"] == 0:
            break
        if r["taken"] == 0:
            time.sleep(0.05)
        assert calls < 100
    wait_for(lambda: waiting(svc) == 0, 10)
    ids, _ = dead_ids(svc, a)
    # (how many calls it takes depends on how fast the receiver answers: a call that finds the table full takes none and is made again)
    check("8. the 68 after the first 32 went through in %d calls of at most 32 each" % calls, taken_total == 68 and most <= 32, str((taken_total, most, calls)))
    # the first 32 were cancelled and are still dead letters; the other 68 were delivered
    check("8. the dead letters left are exactly the 32 that were cancelled and not asked for again", [i for i, _ in ids] == list(range(1, 33)) and not any(rep for _, rep in ids), str(ids[:5]))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
    ok = wait_for(lambda: dead_ids(svc, a)[0] == [], 10)
    check("8. and a last call takes them: nothing was lost, the list is empty", st == 202 and r["taken"] == 32 and ok, str((st, r)))
    sent = ra.events()
    check("8. every one of the 100 events has been sent after its death", set(sent) >= set(range(1, 101)), str(len(set(sent))))
    check("9. the log has a kind-16 record for each of the 32 cancelled", len(records(d, (16,))) >= 32, str(len(records(d, (16,)))))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    # ---- 10. retention: a snapshot of the outcomes log keeps the dead letters and a cancelled replay stays cancelled ---------------------------------------------
    reset_db()
    d = tmp()
    mode = {"code": 500}
    (a,), (ra,) = make_dead(d, 6, mode)
    rb = Receiver(status=204)
    flags = K.pg_flags() + ["--admin-token", K.TOKEN, "--delivery-log-bytes", "65536"]
    svc = start(d, schedule="40,600000", extra=flags)
    b = make(svc, rb.port)
    base = ra.count()
    for n in (1, 2, 3):
        st, _ = req(svc, "POST", f"/events/{n}/replay/{a}")
        assert st == 202
    ok = wait_for(lambda: ra.count() >= base + 6 and waiting(svc) == 3, 10)
    st, r = req(svc, "DELETE", f"/events/2/replay/{a}")
    ids_before, top_before = dead_ids(svc, a)
    check("10. before the snapshot: replays of 1 and 3 wait, 2 was cancelled, six dead letters", ok and st == 200 and waiting(svc) == 2 and ids_before == [(1, True), (2, False), (3, True), (4, False), (5, False), (6, False)], str((ok, st, ids_before)))
    check("10. the cancel is a record of kind 16 in the log, for now", [r[2] for r in records(d, (16,))] == [2], str(records(d, (16,))))
    for n in range(7, 1007):
        post_event(svc, n, "t")
    ok = wait_for(lambda: get(svc, "/stats")["snapshots"] >= 1 and rb.count() == 1000, 60)
    check("10. a thousand more events later the outcomes log has been replaced by a snapshot", ok, str(get(svc, "/stats")))
    ids_snap, top_snap = dead_ids(svc, a)
    sent2 = ra.events().count(2)
    check("10. the list and the table of waiting replays are what they were", ids_snap == ids_before and waiting(svc) == 2 and top_snap["held"] == 6, str((ids_snap, waiting(svc))))
    kill9(svc)
    recs = records(d, tuple(range(1, 18)))
    kinds = [r[0] for r in recs]
    dead_recs = [r for r in recs if r[0] == 17 and r[3] > 0]
    check("10. read apart from the service, the log (without its header, which the reader skips) holds the six dead letters as kind 17, and no record of the cancel (the snapshot's table of replays has no entry for it)",
          sorted(r[2] for r in dead_recs) == [1, 2, 3, 4, 5, 6] and kinds.count(16) == 0, str((kinds[:4], kinds.count(17), kinds.count(16))))
    check("10. and the snapshot has a replay record for the two that wait and none for the one cancelled", sorted(r[2] for r in recs if r[0] == 6) == [1, 3], str(sorted(r[2] for r in recs if r[0] == 6)))
    ok, why = logcheck_ok(d)
    check("10. scripts/logcheck.py accepts the directory with the snapshot's kind-17 records", ok, why)
    for round_ in (1, 2):
        svc = start(d, schedule="40,600000", extra=flags)
        ids_after, top_after = dead_ids(svc, a)
        check("10. after the kill and a restart (%d): the same dead letters, 2 still not replaying, 1 and 3 still waiting, nothing resent for 2" % round_, ids_after == ids_before and waiting(svc) == 2 and top_after["held"] == 6, str((ids_after, waiting(svc))))
        time.sleep(0.4)
        check("10. ... the receiver was not sent event 2 again", ra.events().count(2) == sent2, str((sent2, ra.events().count(2))))
        if round_ == 1:
            kill9(svc)
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()
    finish("cancel")


main()
