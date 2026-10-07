#!/usr/bin/env python3
"""Dead letters you can list and replay in bulk (docs/design.md section 39.1; production.md P1.4).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/dead_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

  1. `GET /endpoints/:id/dead`: every dead letter with its event id, type, attempts, reason and the time it died, checked against what the test made fail, and the
     same read out of `delivery.seg` by a reader written apart from the service; the healthy endpoint has none
  2. pages: `limit`, `order`, `after`: the pages put together are the whole list in either order, with no event twice and none missing; a cursor holds while dead
     letters are added; every refusal (a limit of 0, 1001, x; an order; an after) is a 400 and an unknown endpoint a 404
  3. `kill -9` while 300 events are dying: after the restart the list is exactly what the log says (300 events, ids 1 to 300, 2 attempts each), and the
     same after a second kill and a restart; the reasons survive; a `410` is a dead letter with the reason `gone` and one attempt
  4. `POST /endpoints/:id/replay-dead`: 100 dead letters, the receiver mended, called again and again: no call takes more than 32 or more than the table of
     waiting replays has room for (`waiting` never above 32), `taken + remaining` is what was there, and in the end every one of the 100 was sent
     once and the list is empty; a second call at once takes nothing (they are replaying); `limit`, `types` and `after`, and every refusal
  5. a replay that fails again is a dead letter again, with its new attempts and time; while it waits the list says `replaying`; `remaining` after a call
     counts only what is not replaying, so a loop that goes on until it is 0 ends
  6. `kill -9` in the middle of a bulk replay, over and over: no dead letter is lost (all 200 are sent at least once), none is sent twice beyond what was in
     flight at a kill, and the list is empty in the end
  7. the table is bounded (2,048 newest): 2,200 events dead at one endpoint hold 2,048 with `evicted` 152 and `truncated` true; a restart builds the same;
     replaying them all and restarting brings back the 152 that were left out, with nothing evicted
     (a start with the database away: the four routes about dead letters are 503 until the endpoints are read, and then the list is whole, with no restart)
  8. a log written before the time of death was recorded (its dead records end in 0) is read: `died_at` 0, everything else as before
  9. retention (design 39.1): a snapshot of the outcomes log keeps the dead letters (kind 17) and the floor and does not make a delivered event one; a
     table of 2,048 that was snapshotted is the same after a kill and a restart; a dead letter expires with its event when retention drops it
"""
import json
import os
import shutil
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from endpoint_kit import *  # noqa: E402,F401,F403
import endpoint_kit as K  # noqa: E402
import chaos  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

REASONS = {10: "status_3xx", 11: "status_4xx", 12: "status_5xx", 13: "gone"}
TYPES = ["user.created", "invoice.paid", "user.deleted"]


def dead_list(svc, ident, query=""):
    st, body = req(svc, "GET", f"/endpoints/{ident}/dead{query}")
    return st, body


def all_dead(svc, ident, order="desc"):
    """Every entry, by pages of 1000."""
    out, after = [], None
    while True:
        st, body = dead_list(svc, ident, f"?order={order}&limit=1000" + (f"&after={after}" if after is not None else ""))
        assert st == 200, (st, body)
        out += body["dead"]
        if body["next"] is None:
            return out, body
        after = body["next"]


def log_dead(d):
    """The dead letters the delivery log says there are, read by this test's own reader: {(endpoint id, event): [attempts, died, reason]}."""
    path = os.path.join(d, "delivery.seg")
    recs, _ = chaos.read_log(open(path, "rb").read())
    ident, dead = {}, {}
    for _, pairs in recs:
        kind, slot, ev, att, nxt = struct.unpack("<5q", dict(pairs)[b"o"])
        if kind == 10:
            ident[slot] = ev
            dead = {k: v for k, v in dead.items() if k[0] != slot}
        elif kind == 11:
            ident.pop(slot, None)
            dead = {k: v for k, v in dead.items() if k[0] != slot}
        elif kind in (3, 9):
            dead[(slot, ev)] = [att, nxt, 0]
        elif kind == 17 and att > 0:
            # a dead letter of a snapshot: attempts * 65536 + reason + 1, and when it died (with attempts 0 it is the floor, which is no entry)
            dead[(slot, ev)] = [(att - 1) // 65536, nxt, (att - 1) % 65536]
        elif kind == 8:
            dead.pop((slot, ev), None)
        elif kind == 14:
            v = dead.get((slot, ev))
            if v is not None and v[0] == att and v[2] == 0:
                v[2] = nxt % 256
    return {(ident[s], ev): v for (s, ev), v in dead.items() if s in ident}


def rewrite_log(path, fn):
    """The log with every record passed through `fn(kind, slot, event, attempts, fifth) -> fifth`: how a log written by an older version looks."""
    recs, _ = chaos.read_log(open(path, "rb").read())
    out = b""
    for ms, pairs in recs:
        vals = list(struct.unpack("<5q", dict(pairs)[b"o"]))
        vals[4] = fn(*vals)
        value = struct.pack("<5q", *vals)
        body = struct.pack("<QQI", ms, 0, 1) + struct.pack("<I", 1) + b"o" + struct.pack("<I", len(value)) + value
        out += struct.pack("<I", 4 + len(body)) + struct.pack("<I", chaos.crc32c(body)) + body
    open(path, "wb").write(out)


def make(svc, port, **extra):
    st, body = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": port, **extra})
    assert st == 201, (st, body)
    return body["id"]


def main():
    reset_db()
    # ---- 1. the list ------------------------------------------------------------------------------------------------
    d = tmp()
    mode = {"a": 500}
    ra = Receiver(status=lambda i, n: mode["a"])
    rb = Receiver(status=204)
    svc = start(d, schedule="40")
    a, b = make(svc, ra.port), make(svc, rb.port)
    total = 40
    before = int(time.time() * 1000)
    for n in range(1, total + 1):
        post_event(svc, n, TYPES[n % 3])
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == total, 20)
    after_t = int(time.time() * 1000)
    check("1. 40 events are dead at the failing endpoint and delivered at the other", ok and rb.count() == total, str(get(svc, "/stats")))
    ents, top = all_dead(svc, a)
    ids = [e["event"] for e in ents]
    check("1. the list holds all 40, newest first", ids == list(range(total, 0, -1)), str(ids))
    check("1. each entry has its event's type", all(e["type"] == TYPES[e["event"] % 3] for e in ents), str(ents[:3]))
    check("1. each entry has 2 attempts (the first and the one retry) and the reason status_5xx", all(e["attempts"] == 2 and e["reason"] == "status_5xx" for e in ents), str(ents[:2]))
    check("1. each entry says when it died, between the first post and the last wait", all(before - 50 <= e["died_at"] <= after_t + 50 for e in ents), str((before, after_t, [e["died_at"] for e in ents[:3]])))
    check("1. none is replaying; the head says 40 held, not truncated, complete above 0", not any(e["replaying"] for e in ents) and top["held"] == total and top["complete_above"] == 0 and not top["truncated"] and top["endpoint"] == a, str(top))
    _, none = dead_list(svc, b)
    check("1. the healthy endpoint has none", none["dead"] == [] and none["held"] == 0 and none["next"] is None, str(none))
    asc, _ = all_dead(svc, a, "asc")
    check("1. oldest first is the same entries the other way round", [e["event"] for e in asc] == list(range(1, total + 1)) and asc[::-1] == ents, "")
    model = log_dead(d)
    check("1. the log, read apart from the service, says the same dead letters with the same attempts, time and reason",
          {(a, e["event"]): [e["attempts"], e["died_at"], 12] for e in ents} == {k: v for k, v in model.items() if k[0] == a} and not any(k[0] == b for k in model), str(list(model.items())[:2]))
    # ---- 2. pages -------------------------------------------------------------------------------------------------------
    for order, want in (("desc", list(range(total, 0, -1))), ("asc", list(range(1, total + 1)))):
        got, after, pages = [], None, 0
        while True:
            st, body = dead_list(svc, a, f"?limit=7&order={order}" + (f"&after={after}" if after is not None else ""))
            got += [e["event"] for e in body["dead"]]
            pages += 1
            if body["next"] is None:
                break
            after = body["next"]
        check(f"2. pages of 7, {order}: 6 pages, the whole list, no event twice, none missing", got == want and pages == 6, str((pages, got)))
    st, body = dead_list(svc, a, "?limit=7&after=20")
    check("2. after=20 newest first goes on with 19, strictly older", [e["event"] for e in body["dead"]] == list(range(19, 12, -1)) and body["next"] == 13, str(body["dead"][:1]))
    st, body = dead_list(svc, a, "?limit=7&after=20&order=asc")
    check("2. after=20 oldest first goes on with 21, strictly newer", [e["event"] for e in body["dead"]] == list(range(21, 28)) and body["next"] == 27, "")
    st, body = dead_list(svc, a, "?after=1")
    check("2. after the oldest, newest first, is the end (an empty last page, next null)", st == 200 and body["dead"] == [] and body["next"] is None, str(body))
    st, body = dead_list(svc, a, "?after=999999&limit=3")
    check("2. an after beyond the newest starts at the newest", [e["event"] for e in body["dead"]] == [40, 39, 38] and body["next"] == 38, str(body["next"]))
    st, body = dead_list(svc, a, "?limit=40")
    check("2. a page exactly as long as the list has next null", len(body["dead"]) == 40 and body["next"] is None, str(body["next"]))
    st, body = dead_list(svc, a, "?limit=39")
    check("2. a page one short has a next", len(body["dead"]) == 39 and body["next"] == 2, str(body["next"]))
    # a cursor holds while dead letters are added: page one, 3 events die, page two continues with no duplicate and no gap
    st, first = dead_list(svc, a, "?limit=10&order=asc")
    for n in range(total + 1, total + 4):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: get(svc, "/stats")["dead"] == total + 3, 20)
    st, rest = dead_list(svc, a, f"?limit=100&order=asc&after={first['next']}")
    ids2 = [e["event"] for e in first["dead"]] + [e["event"] for e in rest["dead"]]
    check("2. a cursor holds while 3 more die: the pages are 1 to 43 with none twice or missing", ids2 == list(range(1, total + 4)), str(ids2))
    total += 3
    for q, what in (("?limit=0", "a limit of 0"), ("?limit=1001", "a limit of 1001"), ("?limit=x", "a limit of x"), ("?limit=", "an empty limit"), ("?limit=-1", "a negative limit"),
                    ("?order=up", "an order"), ("?order=", "an empty order"), ("?after=x", "an after"), ("?after=-1", "a negative after"), ("?after=", "an empty after")):
        st, body = dead_list(svc, a, q)
        check(f"2. {what} is a 400 with a reason", st == 400 and body.get("error"), str((st, body)))
    st, body = dead_list(svc, 9999)
    check("2. an endpoint that is not there is a 404", st == 404, str((st, body)))
    st, body = req(svc, "GET", "/endpoints/x/dead")
    check("2. an endpoint id that is not a number is a 400", st == 400, str((st, body)))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()

    # ---- 3. kill -9 while events are dying -----------------------------------------------------------------------------
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    total = 300
    kills = 0
    for n in range(1, total + 1):
        post_event(svc, n, TYPES[n % 3])
        # a kill when 40 more attempts have arrived since the last start (driven by progress): the first kills land with events dying
        if n in (60, 120, 180):
            target = ra.count() + 40
            wait_for(lambda: ra.count() >= target, 10)
            kill9(svc)
            kills += 1
            svc = start(d, schedule="40")
    ok = wait_for(lambda: len(all_dead(svc, a)[0]) == total, 40)
    ents, top = all_dead(svc, a, "asc")
    check("3. after %d kills and restarts every one of the 300 events is a dead letter, once" % kills, ok and [e["event"] for e in ents] == list(range(1, total + 1)), str((len(ents), top["held"])))
    check("3. each has 2 attempts and its reason, and its type", all(e["attempts"] == 2 and e["reason"] == "status_5xx" and e["type"] == TYPES[e["event"] % 3] for e in ents), str([e for e in ents if not (e["attempts"] == 2 and e["reason"] == "status_5xx" and e["type"] == TYPES[e["event"] % 3])]))
    live = {(a, e["event"]): [e["attempts"], e["died_at"], 12] for e in ents}
    _ld = log_dead(d)
    check("3. the list is what the log says", live == _ld, str([(k, live.get(k), _ld.get(k)) for k in set(live) | set(_ld) if live.get(k) != _ld.get(k)][:10]))
    kill9(svc)
    svc = start(d, schedule="40")
    again, _ = all_dead(svc, a, "asc")
    check("3. after one more kill and restart it is the same list, entry for entry (type, attempts, reason and time)", again == ents, str((len(again), again[:1], ents[:1])))
    # a 410 is a dead letter with the reason gone and one attempt
    rg = Receiver(status=410)
    g = make(svc, rg.port)
    post_event(svc, 1000, "user.created")
    wait_for(lambda: len(dead_list(svc, g)[1]["dead"]) == 1, 10)
    _, gl = dead_list(svc, g)
    e0 = gl["dead"][0] if gl["dead"] else {}
    check("3. a 410 is a dead letter at once: one attempt, the reason gone, and the endpoint is disabled", e0.get("attempts") == 1 and e0.get("reason") == "gone" and e0.get("type") == "user.created" and get(svc, f"/endpoints/{g}")["disabled"], str(gl))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rg.close()

    # ---- 4. bulk replay ---------------------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"a": 500}
    ra = Receiver(status=lambda i, n: mode["a"])
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    total = 100
    for n in range(1, total + 1):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: get(svc, "/stats")["dead"] == total, 20)
    base = ra.count()
    mode["a"] = 204
    calls, taken_total, worst_taken, worst_waiting, sizes = 0, 0, 0, 0, []
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"limit": 10})
    check("4. limit 10 takes 10 (202), 90 remain, 10 wait", st == 202 and r["taken"] == 10 and r["remaining"] == 90 and r["waiting"] == 10 and r["next"] == 10, str((st, r)))
    taken_total += r["taken"]
    st, r2 = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"limit": 10})
    check("4. a second call with the first still replaying never takes the same ones twice: 90 or fewer remain", st in (200, 202) and r2["taken"] + r2["remaining"] <= 90 and r2["taken"] <= 10, str(r2))
    taken_total += r2["taken"]
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
        calls += 1
        assert st in (200, 202), (st, r)
        taken_total += r["taken"]
        worst_taken = max(worst_taken, r["taken"])
        worst_waiting = max(worst_waiting, r["waiting"], get(svc, "/stats")["replays"])
        sizes.append(r["taken"])
        if r["remaining"] == 0 and r["taken"] == 0:
            break
        if r["taken"] == 0:
            time.sleep(0.05)
        assert calls < 400
    ok = wait_for(lambda: len(set(n for n in ra.events()[base:] if n)) == total, 20)
    check("4. calling again and again until remaining is 0 takes every one of the 100 exactly once (%d calls)" % calls, taken_total == total and ok, str((taken_total, len(set(ra.events()[base:])))))
    check("4. no call took more than 32 and the table of waiting replays never held more than 32", worst_taken <= 32 and worst_waiting <= 32, str((worst_taken, worst_waiting)))
    time.sleep(0.5)
    sent = ra.events()[base:]
    check("4. each of the 100 was sent once (a replay delivered is not sent again)", sorted(sent) == list(range(1, total + 1)), str((len(sent), sorted(sent)[:5])))
    ents, top = all_dead(svc, a)
    check("4. the list is empty: a replay that was delivered is no longer a dead letter", ents == [] and top["held"] == 0, str(top))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
    check("4. nothing left is a 200 with taken 0 and remaining 0", st == 200 and r["taken"] == 0 and r["remaining"] == 0 and r["next"] is None, str((st, r)))
    check("4. the log, read apart, has no dead letter either", [k for k in log_dead(d) if k[0] == a] == [], str(log_dead(d)))
    # types, limit, after
    mode["a"] = 500
    for n in range(101, 131):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: len(all_dead(svc, a)[0]) == 30, 20)
    mode["a"] = 204
    base = ra.count()
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"types": ["user.*"]})
    users = [n for n in range(101, 131) if TYPES[n % 3].startswith("user.")]
    check("4. types [user.*] takes the user events only (%d), the others remain dead" % len(users), st == 202 and r["taken"] == len(users) and r["remaining"] == 0, str((st, r)))
    wait_for(lambda: len(all_dead(svc, a)[0]) == 30 - len(users), 20)
    left = [e["event"] for e in all_dead(svc, a, "asc")[0]]
    check("4. ... and the list is the invoice events", left == [n for n in range(101, 131) if TYPES[n % 3] == "invoice.paid"] and sorted(ra.events()[base:]) == users, str((left, ra.events()[base:])))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"types": ["nothing.here", "user.*"], "limit": 3, "after": left[2]})
    check("4. after and limit: the 3 events after the third, of the types asked (none are users any more): taken 0, remaining 0", st == 200 and r["taken"] == 0 and r["remaining"] == 0, str((st, r)))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"types": ["invoice.paid"], "limit": 3, "after": left[2]})
    check("4. after the third, limit 3, invoice.paid: 3 taken, the rest remain, next is the last taken", st == 202 and r["taken"] == 3 and r["remaining"] == len(left) - 3 - 3 and r["next"] == left[5], str((st, r, left)))
    wait_for(lambda: len(all_dead(svc, a)[0]) == len(left) - 3, 20)
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"types": []})
    check("4. an empty types list is every type: the rest are taken", st == 202 and r["taken"] == len(left) - 3 and r["remaining"] == 0, str((st, r)))
    wait_for(lambda: all_dead(svc, a)[0] == [], 20)
    check("4. and the list is empty", all_dead(svc, a)[0] == [], "")
    for body, what in (({"limit": 0}, "limit 0"), ({"limit": 2049}, "limit 2049"), ({"limit": "x"}, "limit x"), ({"limit": 1.5}, "limit 1.5"), ({"after": -1}, "after -1"),
                       ({"after": "x"}, "after x"), ({"types": "user.*"}, "types a string"), ({"types": [1]}, "types a number"), ({"types": ["a b"]}, "a pattern with a space"),
                       ({"types": ["a*b"]}, "a * in the middle"), ({"types": [""]}, "an empty pattern"), ({"limt": 5}, "a misspelt member"), ({"types": ["*"], "extra": 1}, "an extra member")):
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", body)
        check(f"4. {what} is a 400 with a reason and takes nothing", st == 400 and r.get("error"), str((st, r)))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", b"[1]")
    check("4. a body that is not an object is a 400", st == 400, str((st, r)))
    st, r = req(svc, "POST", "/endpoints/9999/replay-dead")
    check("4. an endpoint that is not there is a 404", st == 404, str(st))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", token="wrong-wrong-wrong")
    check("4. a wrong token is a 401; the read token (none configured here) is not asked", st == 401, str(st))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 5. replaying in the list, the loop ends, and a replay that dies again ---------------------------------------------------------
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    for n in range(1, 13):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: len(all_dead(svc, a)[0]) == 12, 20)
    stop(svc)
    svc = start(d, schedule="40,600000")
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"limit": 5})
    time.sleep(0.8)
    ents, _ = all_dead(svc, a, "asc")
    rep = [e for e in ents if e["replaying"]]
    check("5. 5 replays are taken, fail once and wait a long time: 12 are still dead letters and 5 say replaying", st == 202 and r["taken"] == 5 and len(ents) == 12 and len(rep) == 5, str((st, r, len(rep))))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {})
    check("5. a call now takes the 7 that are not replaying, and remaining counts none of the 5 that are", st == 202 and r["taken"] == 7 and r["remaining"] == 0 and r["waiting"] == 12, str((st, r)))
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {})
    check("5. a call after that takes nothing and has nothing remaining: a loop that goes on until remaining is 0 ends", st == 200 and r["taken"] == 0 and r["remaining"] == 0, str((st, r)))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # a replay that dies again is a dead letter again, with the attempts and the time of its new death: the receiver now answers 410
    reset_db()
    d = tmp()
    mode = {"a": 500}
    ra = Receiver(status=lambda i, n: mode["a"])
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    for n in range(1, 4):
        post_event(svc, n, "t")
    wait_for(lambda: len(all_dead(svc, a)[0]) == 3, 20)
    first = {e["event"]: e for e in all_dead(svc, a)[0]}
    time.sleep(0.2)
    mode["a"] = 410
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"limit": 1})
    wait_for(lambda: get(svc, f"/endpoints/{a}")["disabled"], 10)
    now1 = {e["event"]: e for e in all_dead(svc, a)[0]}
    check("5. a replay answered 410 is a dead letter again at once: 1 attempt of the replay, the reason gone, a later time", st == 202 and now1[1]["attempts"] == 1 and now1[1]["reason"] == "gone" and now1[1]["died_at"] > first[1]["died_at"] and not now1[1]["replaying"], str((st, now1.get(1), first.get(1))))
    check("5. ... the others did not change, and the endpoint is disabled", now1[2] == first[2] and now1[3] == first[3] and len(now1) == 3, str(now1))
    mode["a"] = 204
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 6. kill -9 in the middle of a bulk replay ----------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"a": 500}
    ra = Receiver(status=lambda i, n: mode["a"])
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    total = 200
    for n in range(1, total + 1):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: get(svc, "/stats")["dead"] == total, 30)
    mode["a"] = 204
    base = ra.count()
    kills, calls = 0, 0
    next_kill = base + 25
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
        calls += 1
        if r["taken"] == 0 and r["remaining"] == 0:
            if len(all_dead(svc, a)[0]) == 0:
                break
        if ra.count() >= next_kill and kills < 6:
            kill9(svc)
            kills += 1
            svc = start(d, schedule="40")
            next_kill = ra.count() + 25
        else:
            time.sleep(0.02)
        assert calls < 3000
    ok = wait_for(lambda: set(ra.events()[base:]) >= set(range(1, total + 1)), 30)
    sent = ra.events()[base:]
    repeats = len(sent) - len(set(sent))
    check("6. %d kill -9 in the middle of a bulk replay: all 200 dead letters were sent" % kills, ok and kills >= 3, str((kills, len(set(sent)))))
    check("6. ... none more than once beyond what was in flight at each kill (8 each)", repeats <= 8 * kills, str((repeats, kills)))
    ents, _ = all_dead(svc, a)
    check("6. the list is empty at the end, and the log says so too", ents == [] and [k for k in log_dead(d) if k[0] == a] == [], str(len(ents)))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 7. the table is bounded ----------------------------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    mode = {"a": 500}
    ra = Receiver(status=lambda i, n: mode["a"])
    svc = start(d, schedule="10")
    a = make(svc, ra.port)
    total = 2200
    for n in range(1, total + 1):
        post_event(svc, n, "t")
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == total, 120)
    _, top = dead_list(svc, a, "?limit=1")
    check("7. 2,200 events are dead", ok, str(get(svc, "/stats")))
    ents, top = all_dead(svc, a, "asc")
    check("7. the table holds the newest 2,048 (153 to 2,200), the list says it is truncated and complete above 152", [e["event"] for e in ents] == list(range(153, total + 1)) and top["held"] == 2048 and top["complete_above"] == 152 and top["truncated"], str((len(ents), top)))
    kill9(svc)
    svc = start(d, schedule="10")
    ents2, top2 = all_dead(svc, a, "asc")
    check("7. after a kill and a restart the table built from the log is the same 2,048, complete above 152", ents2 == ents and top2["complete_above"] == 152, str((len(ents2), top2["complete_above"])))
    mode["a"] = 204
    base = ra.count()
    calls = 0
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead", {"after": 152})
        calls += 1
        if r["taken"] == 0 and r["remaining"] == 0:
            break
        if r["taken"] == 0:
            time.sleep(0.05)
        assert calls < 5000
    wait_for(lambda: len(set(ra.events()[base:])) == 2048 and get(svc, "/stats")["replays"] == 0, 60)
    check("7. replaying what is above 152 sends 2,048 events, each once (%d calls)" % calls, sorted(ra.events()[base:]) == list(range(153, total + 1)), str(len(ra.events()[base:])))
    # the table is empty, and the 152 that were left out are in the log: the next look at the list completes it from there, with no restart
    ents3, top3 = all_dead(svc, a, "asc")
    check("7. with no restart the next look at the list completes it from the log: the 152 left out (1 to 152), and it is complete", [e["event"] for e in ents3] == list(range(1, 153)) and top3["complete_above"] == 0 and not top3["truncated"], str((len(ents3), top3)))
    kill9(svc)
    svc = start(d, schedule="10")
    ents4, top4 = all_dead(svc, a, "asc")
    check("7. and a restart finds the same 152 (the table recovery builds is empty and is completed from the log when the endpoints are read, before anything is listed)", ents4 == ents3 and top4["complete_above"] == 0, str((len(ents4), top4)))
    # the same again, with the database away at the start: the routes about dead letters are 503 until the endpoints are read, and the table is completed from the log
    # only then (design 39.1 and 37.2: nothing is listed, and nothing completed, before the endpoints are known)
    kill9(svc)
    proxy = PgProxy(K.PG_HOST, K.PG_PORT)
    proxy.cut()
    via = pg_flags()
    via[via.index("--pg-host") + 1], via[via.index("--pg-port") + 1] = "127.0.0.1", str(proxy.port)
    svc = start(d, schedule="10", extra=via + ["--admin-token", TOKEN], wait_loaded=False)
    time.sleep(1.0)
    codes = [req(svc, m, p, {})[0] for m, p in (("GET", f"/endpoints/{a}/dead"), ("POST", f"/endpoints/{a}/replay-dead"), ("DELETE", f"/endpoints/{a}/replays"), ("DELETE", f"/events/1/replay/{a}"))]
    check("7. a start with the database away: the four routes about dead letters are 503 (the endpoints are not loaded), and the service listens", codes == [503] * 4, str(codes))
    proxy.restore()
    ok = wait_for(lambda: req(svc, "GET", f"/endpoints/{a}/dead?limit=1")[0] == 200, 20)
    ents5, top5 = all_dead(svc, a, "asc") if ok else ([], {})
    check("7. the database comes: no restart, and the list is whole at once (the 152, complete)", ok and ents5 == ents3 and top5["complete_above"] == 0 and not top5["truncated"], str((ok, len(ents5), top5)))
    kill9(svc)
    proxy.close()
    svc = start(d, schedule="10")
    # everything that is left, and the log agrees
    base = ra.count()
    calls = 0
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
        calls += 1
        if r["taken"] == 0 and r["remaining"] == 0:
            break
        if r["taken"] == 0:
            time.sleep(0.05)
    wait_for(lambda: len(set(ra.events()[base:])) == 152, 30)
    check("7. replaying the rest sends the 152, and the list and the log are empty", sorted(ra.events()[base:]) == list(range(1, 153)) and all_dead(svc, a)[0] == [] and [k for k in log_dead(d) if k[0] == a] == [], str(len(ra.events()[base:])))
    stop(svc)
    shutil.rmtree(d)
    ra.close()

    # ---- 8. a log from before the time of death was recorded -------------------------------------------------------------------------------------------
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    svc = start(d, schedule="40")
    a = make(svc, ra.port)
    for n in range(1, 11):
        post_event(svc, n, TYPES[n % 3])
    wait_for(lambda: get(svc, "/stats")["dead"] == 10, 20)
    ents, _ = all_dead(svc, a, "asc")
    stop(svc)
    rewrite_log(os.path.join(d, "delivery.seg"), lambda kind, slot, ev, att, fifth: 0 if kind == 3 else fifth)
    svc = start(d, schedule="40")
    old, top = all_dead(svc, a, "asc")
    check("8. a log whose dead records end in 0 is read: the same 10 dead letters, died_at 0, everything else as before",
          [(e["event"], e["type"], e["attempts"], e["reason"]) for e in old] == [(e["event"], e["type"], e["attempts"], e["reason"]) for e in ents] and all(e["died_at"] == 0 for e in old), str(old[:2]))
    ra.status = 204
    st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
    wait_for(lambda: all_dead(svc, a)[0] == [], 10)
    check("8. and they replay like any other", st == 202 and r["taken"] == 10 and all_dead(svc, a)[0] == [], str((st, r)))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    # ---- 9. retention ---------------------------------------------------------------------------------------------------------------------
    # a. a snapshot of the outcomes log keeps the dead letters, and a delivered event is not one after it
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    rb = Receiver(status=204)
    svc = start(d, schedule="40", extra=pg_flags() + ["--admin-token", TOKEN, "--delivery-log-bytes", "65536"])
    a, b = make(svc, ra.port), make(svc, rb.port)
    total = 400
    for n in range(1, total + 1):
        post_event(svc, n, TYPES[n % 3])
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == total and rb.count() == total and get(svc, "/stats")["snapshots"] >= 1, 60)
    check("9a. 400 events are dead at one endpoint and delivered at the other, and the outcomes log has been replaced by a snapshot", ok, str(get(svc, "/stats")))
    ents, top = all_dead(svc, a, "asc")
    check("9a. the list holds the 400", [e["event"] for e in ents] == list(range(1, total + 1)) and top["held"] == total, str((len(ents), top)))
    kill9(svc)
    recs, _ = chaos.read_log(open(os.path.join(d, "delivery.seg"), "rb").read())
    kinds = [struct.unpack("<5q", dict(pairs)[b"o"])[0] for _, pairs in recs]
    check("9a. the log, read apart from the service, has the dead letters of the snapshot (kind 17), and says what the list says",
          kinds.count(17) >= 1 and {(a, e["event"]): [e["attempts"], e["died_at"], 12] for e in ents} == {k: v for k, v in log_dead(d).items() if k[0] == a}, str((kinds[:3], kinds.count(17))))
    svc = start(d, schedule="40", extra=pg_flags() + ["--admin-token", TOKEN, "--delivery-log-bytes", "65536"])
    ents2, top2 = all_dead(svc, a, "asc")
    check("9a. after the kill and a restart the table is the same 400 (attempts, reason, time of death)", ents2 == ents, str((len(ents2), top2)))
    _, none = dead_list(svc, b)
    check("9a. the endpoint that delivered everything has none: the snapshot did not write its final cells as dead letters", none["dead"] == [] and none["held"] == 0, str(none))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()
    # b. a table of 2,048 with a floor, snapshotted while it filled
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    flags = pg_flags() + ["--admin-token", TOKEN, "--delivery-log-bytes", "65536"]
    svc = start(d, schedule="10", extra=flags)
    a = make(svc, ra.port)
    total = 2200
    for n in range(1, total + 1):
        post_event(svc, n, "t")
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == total and get(svc, "/stats")["snapshots"] >= 1, 120)
    check("9b. 2,200 events are dead and the outcomes log was replaced by a snapshot while they died", ok, str(get(svc, "/stats")))
    ents, top = all_dead(svc, a, "asc")
    check("9b. 2,048 are held, complete above 152", [e["event"] for e in ents] == list(range(153, total + 1)) and top["held"] == 2048 and top["complete_above"] == 152 and top["truncated"], str((len(ents), top)))
    kill9(svc)
    svc = start(d, schedule="10", extra=flags)
    ents2, top2 = all_dead(svc, a, "asc")
    check("9b. after a kill and a restart: the same 2,048 and the same floor (the snapshot carried it)", ents2 == ents and top2["complete_above"] == 152 and top2["truncated"], str((len(ents2), top2)))
    t0 = time.time()
    ents3, top3 = all_dead(svc, a, "asc")
    check("9b. and the list does not read the log for a floor it cannot lower (settled): the second look is quick", ents3 == ents and time.time() - t0 < 2.0, "%.2f s" % (time.time() - t0))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    # c. a dead letter expires with its event
    reset_db()
    d = tmp()
    ra = Receiver(status=500)
    # a second endpoint holds the floor still: it is sent events 1 to 300 and answers 410 to the 301st, which disables it, so no event above 301 is ever final there and
    # retention can drop what is below 301 and nothing more, however slow the machine is (without it everything is final and old, and a slow run finds all of it gone)
    rb = Receiver(status=lambda i, n: 410 if n == 301 else 204)
    knobs = ["--segment-bytes", "262144", "--retention-ms", "1500", "--window-ms", "500"]
    svc = start(d, schedule="10", extra=pg_flags() + ["--admin-token", TOKEN, *knobs])
    a = make(svc, ra.port)
    b = make(svc, rb.port)
    total = 700
    for n in range(1, total + 1):
        post_event(svc, n, "t", extra={"pad": "x" * 1000})
    ok = wait_for(lambda: get(svc, "/stats")["dead"] == total + 1, 60)
    check("9c. 700 events of a kilobyte are dead at one endpoint, and the 301st at the other, which it disabled", ok, str(get(svc, "/stats")))
    ok = wait_for(lambda: get(svc, "/stats")["events_first_id"] > 1, 30)
    first = get(svc, "/stats")["events_first_id"]
    check("9c. retention has dropped the oldest segment (first id %d)" % first, ok and first > 1, str(get(svc, "/stats")))
    ents, top = all_dead(svc, a, "asc")
    check("9c. the dead letters of the dropped events are gone from the list, those of the events that are left are not", ents and ents[0]["event"] >= first and [e["event"] for e in ents] == list(range(ents[0]["event"], total + 1)) and top["held"] == len(ents), str((first, len(ents), ents[:1], top)))
    check("9c. no floor is left for events that are gone: the list is complete", not top["truncated"] and top["complete_above"] == 0, str(top))
    check("9c. an event that is gone is 410 (a tombstone), and a dead letter that is left can still be asked for", req(svc, "GET", "/events/1")[0] == 410 and req(svc, "GET", f"/events/{total}")[0] == 200, "")
    ra.status = 204
    base = ra.count()
    taken, calls = 0, 0
    while True:
        st, r = req(svc, "POST", f"/endpoints/{a}/replay-dead")
        calls += 1
        taken += r["taken"]
        if r["taken"] == 0 and r["remaining"] == 0:
            break
        if r["taken"] == 0:
            time.sleep(0.05)
        assert calls < 5000
    ok = wait_for(lambda: len(set(ra.events()[base:])) == len(ents) and get(svc, "/stats")["replays"] == 0, 30)
    check("9c. replaying all of them sends the ones that are left, each once, and nothing for the dropped", ok and taken == len(ents) and sorted(ra.events()[base:]) == [e["event"] for e in ents], str((taken, len(ents), len(ra.events()[base:]))))
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()
    finish("dead letters")


main()
