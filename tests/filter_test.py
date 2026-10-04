#!/usr/bin/env python3
"""Event-type filtering (docs/design.md section 35; production.md P1.1).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/filter_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

  1. the matching rules, against a model written apart from the service: an endpoint with no list gets everything; an exact type is byte for byte
     (case, a longer or shorter type, a prefix are not it); `user.*` is the types that begin `user.`; `*` is everything, an event with no type too; a
     list is any of its patterns. Events whose type cannot be kept (empty, over 128 bytes, a control character) have no type, and only an endpoint that
     takes everything gets them. Every endpoint's cursor ends at the last event; `/stats` counts what was passed over; the history has no attempt
     for an event an endpoint was not sent
  2. events no endpoint wants are final at once at every endpoint, 3,000 of them (past the window of 1,024), and the ones in between that are wanted
     arrive: the window is not held back
  3. a dead endpoint that subscribes to a rare type: the events it does not want go by it with no attempt, the rare one waits for its retry and holds the
     cursor (it is not final), and the events behind it are passed over in the window; when it is up the cursor goes to the end
  4. `kill -9` under filtering, driven by the progress the receivers saw (a kill when 100 more deliveries have arrived since the last start): every wanted event
     arrives at least once, none that is not wanted ever does, repeats are only what was in flight, and every cursor ends at the last event
  5. a log written before the type was stored (records of one pair, and of three with a key): the service reads it, every event there has no type, only
     the endpoints that take everything are sent them, and the idempotency keys in it still answer; typed records written after it and a restart
  6. the API: POST /endpoints and PATCH /endpoints/:id name `types`, GET shows them (never the other way), each way a list can be wrong is a 400 with its
     reason, `[]` and `null` mean everything, a change reaches the events not yet looked at, and the row in the database holds the list
  7. a replay to every endpoint goes to those that subscribe to the event's type; a replay to one named endpoint goes whatever it subscribes to
  8. `endpoints.conf` with `types=` filters without a database, `--import-endpoints` copies it into the table, and a bad `types=` is a refusal to start
  9. a list that changes, and a restart: an event with an attempt behind it or already final is not passed over because the list now says no (it is retried, or
     not sent again); an event passed over in memory but not yet behind the cursor is decided again under the list then in force
"""
import json
import os
import random
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


def matches(pattern, typ):
    """The rule, written again here: `typ` is None for an event with no type."""
    if pattern == "*":
        return True
    if typ is None:
        return False
    if pattern.endswith(".*") and len(pattern) >= 3:
        return typ.startswith(pattern[:-1])
    return typ == pattern


def wants(types, typ):
    return not types or any(matches(p, typ) for p in types)


def stored_type(typ):
    """What the record keeps of a type: itself, or None if it is empty, over 128 bytes or has a control character."""
    raw = typ.encode()
    if not raw or len(raw) > 128 or any(b < 32 or b == 127 for b in raw):
        return None
    return typ


def conf(d, rows):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for r in rows:
            f.write(r + "\n")


def settled(recv, want):
    return all(sorted(r.events()) == sorted(w) for r, w in zip(recv, want))


def main():
    # ---- 1. the matching rules ----------------------------------------------------------------------------
    reset_db()
    long129, long128 = "x" * 129, "x" * 128
    events = [
        ("invoice.paid", "invoice.paid"), ("invoice.created", "invoice.created"), ("invoice.paid2", "invoice.paid2"), ("Invoice.paid", "Invoice.paid"),
        ("user.created", "user.created"), ("user.address.changed", "user.address.changed"), ("user", "user"), ("users.created", "users.created"),
        ("user.", "user."), ("order.created", "order.created"), ("ping", "ping"), ("pingx", "pingx"), ("Order Created", "Order Created"),
        ("café.ok", "café.ok"), (long129, long129), (long128, long128), ("tab\there", "tab\there"), ("", ""), ("ping", "ping"),
    ]
    lists = {1: [], 2: ["invoice.paid"], 3: ["user.*"], 4: ["order.created", "ping"], 5: ["*"], 6: ["nothing.such"], 7: ["invoice.*", "user"], 8: ["x.*", "ping", "*"]}
    recv = {i: Receiver() for i in lists}
    for i, t in lists.items():
        add_endpoint(i, recv[i].port, secret(), types=",".join(t))
    d = tmp()
    svc = start(d)
    posted = []
    for n, (typ, _) in enumerate(events, 1):
        st, _ = post_event(svc, n, typ)
        posted.append(st)
    # the escape in a type is the decoded type: "escA" is "escA" (an event whose body is written by hand)
    n_esc = len(events) + 1
    st, _ = post_event(svc, n_esc, raw=b'{"type":"esc\\u0041","n":%d}' % n_esc)
    posted.append(st)
    events.append(("escA", "escA"))
    n_all = len(events)
    check("1. every event is accepted (202)", all(s == 202 for s in posted), str(posted))
    want = {i: [n for n, (_, t) in enumerate(events, 1) if wants(lists[i], stored_type(t))] for i in lists}
    check("1. the model: an endpoint with a list of its own gets fewer than all", len(want[2]) < n_all and len(want[1]) == n_all and want[6] == [] and len(want[5]) == n_all, str({i: len(w) for i, w in want.items()}))
    ok = wait_for(lambda: all(sorted(recv[i].events()) == want[i] for i in lists) and set(cursors(svc).values()) == {n_all}, 15)
    got = {i: sorted(recv[i].events()) for i in lists}
    check("1. each endpoint is sent exactly the events its list wants, once each", ok and got == want, str({i: (got[i], want[i]) for i in lists if got[i] != want[i]}))
    check("1. every cursor ends at the last event, the endpoint that wants nothing too", cursors(svc) == {i: n_all for i in lists}, str(cursors(svc)))
    expect_filtered = sum(n_all - len(want[i]) for i in lists if lists[i])
    st = get(svc, "/stats")
    check("1. /stats counts the events passed over (and only those of endpoints with a list)", st["filtered"] == expect_filtered, str((st["filtered"], expect_filtered)))
    check("1. ... and the deliveries: one attempt for each event sent, none for the others", st["delivered"] == sum(len(w) for w in want.values()) and st["attempts"] == st["delivered"], str(st))
    wait_for(lambda: len(psql("select 1 from attempts")) == st["delivered"], 10)
    hist = {int(a): int(b) for a, b in psql("select endpoint, count(*) from attempts group by endpoint")}
    check("1. the history has an attempt for each event sent and none for an event passed over", hist == {i: len(w) for i, w in want.items() if w}, str(hist))
    ep = endpoints(svc)
    check("1. GET /endpoints shows each list", all(ep[i]["types"] == lists[i] for i in lists), str({i: ep[i]["types"] for i in ep}))
    check("1. an event no endpoint but the ones that take everything wants is nevertheless final at the others",
          cursors(svc)[6] == n_all and st["filtered"] >= n_all, str(cursors(svc)))
    # the rule in the record: one pair `typ` after the body, for an event that has a type
    recs, _ = chaos.read_log(open(os.path.join(d, "events.seg"), "rb").read())
    shapes = {}
    for ms, pairs in recs:
        shapes[ms] = [k for k, _ in pairs]
    check("1. the record has the type as its second pair: `typ`, for an event that has one, and no pair for one that has none",
          all(shapes[n] == ([b"event", b"typ"] if stored_type(events[n - 1][1]) else [b"event"]) for n in range(1, n_all + 1)), str(shapes))
    check("1. ... and the pair holds the decoded type", dict(recs[n_all - 1][1]).get(b"typ") == b"escA", str(recs[-1]))
    stop(svc)
    shutil.rmtree(d)
    for r in recv.values():
        r.close()

    # ---- 2. events nobody wants do not hold the window ------------------------------------------------------
    reset_db()
    ra, rb = Receiver(), Receiver()
    add_endpoint(1, ra.port, secret(), types="rare.a")
    add_endpoint(2, rb.port, secret(), types="rare.b,rare.c")
    d = tmp()
    svc = start(d)
    total = 3000
    wanted_a, wanted_b = [], []
    t0 = time.time()
    for n in range(1, total + 1):
        typ = "common.%d" % (n % 7)
        if n % 500 == 0:
            typ = "rare.a"
            wanted_a.append(n)
        elif n % 500 == 250:
            typ = "rare.b" if n % 1000 == 250 else "rare.c"
            wanted_b.append(n)
        post_event(svc, n, typ)
    t_posted = time.time() - t0
    ok = wait_for(lambda: set(cursors(svc).values()) == {total}, 30)
    t_final = time.time() - t0
    check("2. both cursors reach 3,000 events (past the 1,024 window) with no endpoint wanting nearly all of them", ok, str(cursors(svc)))
    check("2. ... and the wanted events arrived, in order, once each", sorted(ra.events()) == wanted_a and sorted(rb.events()) == wanted_b, str((ra.events(), rb.events())))
    st = get(svc, "/stats")
    check("2. nothing was attempted for the others: attempts equal the wanted events, the rest are counted as passed over",
          st["attempts"] == len(wanted_a) + len(wanted_b) and st["filtered"] == 2 * total - st["attempts"], str(st))
    print(f"     3,000 events posted in {t_posted:.1f} s, final at both endpoints {t_final - t_posted:.1f} s after the last post")
    stop(svc)
    shutil.rmtree(d)
    ra.close()
    rb.close()

    # ---- 3. a dead endpoint that subscribes to a rare type ----------------------------------------------------
    reset_db()
    dead_port = chaos.free_port()      # a port nobody listens on, until the receiver is made there
    alive = Receiver()
    add_endpoint(1, dead_port, secret(), types="rare")
    add_endpoint(2, alive.port, secret(), types="")
    d = tmp()
    svc = start(d, schedule=",".join(["1000"] * 16), deadline="500")
    for n in range(1, 2501):
        post_event(svc, n, "common")
    ok = wait_for(lambda: cursors(svc) == {1: 2500, 2: 2500}, 30)
    check("3. the dead endpoint's cursor goes over 2,500 events it does not want with no attempt at it", ok and get(svc, "/stats")["attempts"] == 2500, str((cursors(svc), get(svc, "/stats"))))
    post_event(svc, 2501, "rare")
    for n in range(2502, 2502 + 1500):
        post_event(svc, n, "common")
    ok = wait_for(lambda: cursors(svc)[2] == 4001 and get(svc, "/stats")["failed"] >= 2, 30)
    check("3. the rare event fails and waits for its retry, and holds the dead endpoint's cursor just before it", ok and cursors(svc)[1] == 2500, str((cursors(svc), get(svc, "/stats"))))
    time.sleep(0.5)
    st = get(svc, "/stats")
    check("3. ... the events behind it, inside its window, are passed over meanwhile (the window is 1,024 cells and the rare event is one), the healthy endpoint is not held",
          st["filtered"] == 2500 + 1023 and cursors(svc)[2] == 4001, str(st))
    revived = Receiver(port=dead_port)
    ok = wait_for(lambda: cursors(svc) == {1: 4001, 2: 4001}, 30)
    check("3. when the endpoint is up the rare event is delivered once, and the cursor goes to the end", ok and revived.events() == [2501], str((cursors(svc), revived.events())))
    stop(svc)
    shutil.rmtree(d)
    alive.close()
    revived.close()

    # ---- 4. kill -9 under filtering ---------------------------------------------------------------------------
    reset_db()
    types_of = {1: [], 2: ["type.a"], 3: ["type.*", "other"], 4: ["other"]}
    recv = {i: Receiver() for i in types_of}
    for i, t in types_of.items():
        add_endpoint(i, recv[i].port, secret(), types=",".join(t))
    cycle = ["type.a", "type.b", "other", "misc", "type.a", "type.c.d"]
    total = 1800
    ev_type = {n: cycle[n % len(cycle)] for n in range(1, total + 1)}
    want = {i: sorted(n for n in ev_type if wants(types_of[i], ev_type[n])) for i in types_of}
    d = tmp()
    svc = start(d)
    cur = [svc]
    acked = set()
    stop_post = threading.Event()

    def poster():
        for n in range(1, total + 1):
            while not stop_post.is_set():
                try:
                    st, _ = post_event(cur[0], n, ev_type[n], key="k%d" % n)
                    if st == 202:
                        acked.add(n)
                        break
                except Exception:
                    time.sleep(0.02)
            time.sleep(0.001)

    th = threading.Thread(target=poster, daemon=True)
    th.start()
    kills, seen_at_kill = 0, 0
    while th.is_alive() or kills < 8:
        # a kill when the receivers have seen 100 more deliveries since the start (or the end of the posting, with the kills still owed)
        progress = lambda: sum(r.count() for r in recv.values())  # noqa: E731
        base = progress()
        ok = wait_for(lambda: progress() >= base + 100 or not th.is_alive(), 20, 0.005)
        if not th.is_alive() and kills >= 8:
            break
        kill9(cur[0])
        kills += 1
        cur[0] = start(d)
        if kills > 40:
            break
    th.join(30)
    stop_post.set()
    ok = wait_for(lambda: acked == set(range(1, total + 1)) and settled([recv[i] for i in types_of], [want[i] for i in types_of]) and set(cursors(cur[0]).values()) == {total}, 40)
    got = {i: sorted(set(recv[i].events())) for i in types_of}
    check(f"4. after {kills} kills -9 every acknowledged event is in the log and every endpoint was sent every event it wants", ok or got == want, str({i: (len(got[i]), len(want[i])) for i in types_of}))
    extra = {i: sorted(set(recv[i].events()) - set(want[i])) for i in types_of}
    check("4. ... and never one it does not want, though the cursors were rebuilt from the log at each start", all(not e for e in extra.values()), str(extra))
    dups = sum(len(recv[i].events()) - len(set(recv[i].events())) for i in types_of)
    check("4. ... repeats are only attempts that were in flight at a kill, or ended in the turn it cut (8 in flight an endpoint, twice that to be safe)", dups <= 16 * len(types_of) * kills, f"{dups} repeats, {kills} kills")
    check("4. every cursor ends at the last event", cursors(cur[0]) == {i: total for i in types_of}, str(cursors(cur[0])))
    print(f"     {kills} kills, {dups} repeats, {sum(len(r.events()) for r in recv.values())} deliveries")
    stop(cur[0])
    shutil.rmtree(d)
    for r in recv.values():
        r.close()

    # ---- 5. a log from before the type was stored -------------------------------------------------------------
    reset_db()
    ra, rb, rc = Receiver(), Receiver(), Receiver()
    add_endpoint(1, ra.port, secret())
    add_endpoint(2, rb.port, secret(), types="x.y")
    add_endpoint(3, rc.port, secret(), types="*")
    d = tmp()

    log = b""
    for n in range(1, 6):
        ev = json.dumps({"type": "x.y", "n": n}).encode()
        pairs = [(b"event", ev)]
        if n in (3, 4):
            pairs += [(b"key", b"old-%d" % n), (b"t", struct.pack("<Q", int(time.time() * 1000)))]
        rest = struct.pack("<QQI", n, 0, len(pairs)) + b"".join(struct.pack("<I", len(k)) + k + struct.pack("<I", len(v)) + v for k, v in pairs)
        log += struct.pack("<I", len(rest) + 4) + struct.pack("<I", chaos.crc32c(rest)) + rest
    open(os.path.join(d, "events.seg"), "wb").write(log)
    recs, end = chaos.read_log(log)
    check("5. (the log written here is one the reader of the tests reads: 5 records of one pair and of three)", len(recs) == 5 and end == len(log) and [len(p) for _, p in recs] == [1, 1, 3, 3, 1], str((len(recs), end, len(log))))
    svc = start(d)
    ok = wait_for(lambda: ra.events() and sorted(ra.events()) == [1, 2, 3, 4, 5] and sorted(rc.events()) == [1, 2, 3, 4, 5] and set(cursors(svc).values()) == {5}, 15)
    check("5. the old events have no type: the endpoint with no list and the one that takes `*` are sent them, the one that wants `x.y` none (though their bodies say x.y)",
          ok and rb.events() == [], str((ra.events(), rb.events(), rc.events(), cursors(svc))))
    st, out = post_event(svc, 3, "x.y", key="old-3")
    check("5. a key in the old log still answers: the same key and event is the old id (3), nothing new is stored",
          st == 202 and json.loads(out)["id"] == 3, str((st, out)))
    st, out = post_event(svc, 6, "x.y")
    check("5. a typed event after them is sent to all three", st == 202 and wait_for(lambda: 6 in ra.events() and 6 in rb.events() and 6 in rc.events(), 10), str((ra.events(), rb.events(), rc.events())))
    st, out = post_event(svc, 7, "x.y", key="new-7")
    stop(svc)
    svc = start(d)
    check("5. a restart reads the mixed log (records of one pair, three, two and four) and the key written with a type still answers",
          not svc.exited and post_event(svc, 7, "x.y", key="new-7")[0] == 202 and json.loads(post_event(svc, 7, "x.y", key="new-7")[1])["id"] == 7, str(svc.lines))
    check("5. ... and so does a legacy key; nothing was sent twice", json.loads(post_event(svc, 4, "x.y", key="old-4")[1])["id"] == 4 and sorted(ra.events()) == [1, 2, 3, 4, 5, 6, 7], str(ra.events()))
    check("5. GET /events/:id reads an old event", get(svc, "/events/2")["event"]["n"] == 2)
    stop(svc)
    shutil.rmtree(d)
    for r in (ra, rb, rc):
        r.close()

    # ---- 6. the API ---------------------------------------------------------------------------------------------
    reset_db()
    rp = Receiver()
    d = tmp()
    svc = start(d)
    st, out = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": rp.port, "types": ["order.*", "ping"]})
    ident = out["id"]
    check("6. POST /endpoints takes a list of types", st == 201, str((st, out)))
    check("6. GET shows it, and the row holds it", get(svc, f"/endpoints/{ident}")["types"] == ["order.*", "ping"] and psql(f"select types from endpoints where id = {ident}") == [("order.*,ping",)],
          str((get(svc, f"/endpoints/{ident}"), psql(f"select types from endpoints where id = {ident}"))))
    rq = Receiver()
    st, out2 = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": rq.port})
    check("6. an endpoint made without a list has an empty one: everything", st == 201 and get(svc, f"/endpoints/{out2['id']}")["types"] == [], str((st, out2)))
    bad = [
        ({"types": "order.*"}, "a string"), ({"types": {"a": 1}}, "an object"), ({"types": [1]}, "a number in it"), ({"types": [None]}, "a null in it"),
        ({"types": [""]}, "an empty pattern"), ({"types": ["a b"]}, "a space"), ({"types": ["a,b"]}, "a comma"), ({"types": ["a*"]}, "a star in the middle"),
        ({"types": ["*a"]}, "a leading star"), ({"types": [".*"]}, "a bare .*"), ({"types": ["a.**"]}, "two stars"), ({"types": ["x" * 129]}, "129 characters"),
        ({"types": ["t%d" % i for i in range(17)]}, "17 patterns"), ({"types": ["y" * 100] * 6}, "over 512 characters in all"), ({"types": ["café"]}, "non-ASCII"),
        ({"types": ["a\nb"]}, "a newline"),
    ]
    miss = []
    for body, what in bad:
        st, out = req(svc, "PATCH", f"/endpoints/{ident}", body)
        if st != 400 or not out.get("error"):
            miss.append((what, st, out))
    check(f"6. each of {len(bad)} lists that are wrong is a 400 with its reason", not miss, str(miss[:3]))
    miss = []
    for body, what in bad:
        st, out = req(svc, "POST", "/endpoints", dict(body, host="127.0.0.1", port=rp.port))
        if st != 400 or not out.get("error"):
            miss.append((what, st, out))
    check("6. ... and in POST /endpoints too", not miss, str(miss[:3]))
    check("6. ... and the endpoint is as it was", get(svc, f"/endpoints/{ident}")["types"] == ["order.*", "ping"] and len(get(svc, "/endpoints")) == 2, str(get(svc, "/endpoints")))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["x" * 128, "*"] + ["p%d" % i for i in range(14)]})
    check("6. 16 patterns, one of 128 characters, are good", st == 200, str((st, out)))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["order.created"]})
    check("6. PATCH changes the list", st == 200 and get(svc, f"/endpoints/{ident}")["types"] == ["order.created"], str((st, out)))
    post_event(svc, 1, "order.deleted")
    post_event(svc, 2, "order.created")
    check("6. a change reaches the events not yet looked at: only order.created is sent", wait_for(lambda: rp.events() == [2], 5) and wait_for(lambda: cursors(svc)[ident] == 2, 5), str((rp.events(), cursors(svc))))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["order.deleted", "order.created"]})
    post_event(svc, 3, "order.deleted")
    check("6. a wider list sends the next such event", wait_for(lambda: rp.events() == [2, 3], 5), str(rp.events()))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": []})
    check("6. [] means everything", st == 200 and get(svc, f"/endpoints/{ident}")["types"] == [] and psql(f"select length(types) from endpoints where id = {ident}") == [("0",)], str((st, out)))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["a"]})
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": None})
    check("6. null means everything", st == 200 and get(svc, f"/endpoints/{ident}")["types"] == [], str((st, out)))
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["kept.*", "also"]})
    st, out = req(svc, "PATCH", f"/endpoints/{ident}", {"port": rp.port})
    check("6. a change that does not name the list leaves it", get(svc, f"/endpoints/{ident}")["types"] == ["kept.*", "also"], str(get(svc, f"/endpoints/{ident}")))
    stop(svc)
    svc = start(d)
    check("6. after a restart the list is the database's", get(svc, f"/endpoints/{ident}")["types"] == ["kept.*", "also"], str(get(svc, "/endpoints")))
    st, _ = req(svc, "PATCH", f"/endpoints/{ident}", {"types": ["a"]}, token=None)
    check("6. the token is still needed", st == 401)
    stop(svc)
    shutil.rmtree(d)
    rp.close()
    rq.close()

    # ---- 7. replay ---------------------------------------------------------------------------------------------
    reset_db()
    r1, r2, r3 = Receiver(), Receiver(), Receiver()
    add_endpoint(1, r1.port, secret(), types="")
    add_endpoint(2, r2.port, secret(), types="a")
    add_endpoint(3, r3.port, secret(), types="b")
    d = tmp()
    svc = start(d)
    post_event(svc, 1, "a")
    post_event(svc, 2, "zzz")
    check("7. the events arrive where they are wanted", wait_for(lambda: r1.events() == [1, 2] or sorted(r1.events()) == [1, 2], 5) and wait_for(lambda: r2.events() == [1], 5) and r3.events() == [], str((r1.events(), r2.events(), r3.events())))
    wait_for(lambda: set(cursors(svc).values()) == {2}, 5)
    st, out = req(svc, "POST", "/events/1/replay", token=None)
    check("7. a replay to all names the endpoints that subscribe to the type (1 takes everything, 2 wants `a`, 3 does not)", st == 202 and sorted(out["endpoints"]) == [1, 2], str((st, out)))
    check("7. ... and they are sent it again", wait_for(lambda: sorted(r1.events()) == [1, 1, 2] and r2.events() == [1, 1], 5) and r3.events() == [], str((r1.events(), r2.events(), r3.events())))
    st, out = req(svc, "POST", "/events/2/replay", token=None)
    check("7. an event of a type nobody lists goes to the endpoint that takes everything", st == 202 and out["endpoints"] == [1], str((st, out)))
    st, out = req(svc, "POST", "/events/1/replay/3", token=None)
    check("7. a replay to one endpoint, named, goes whatever it subscribes to", st == 202 and out["endpoints"] == [3] and wait_for(lambda: r3.events() == [1], 5), str((st, out, r3.events())))
    stop(svc)
    shutil.rmtree(d)
    for r in (r1, r2, r3):
        r.close()

    # ---- 8. endpoints.conf --------------------------------------------------------------------------------------
    reset_db()
    r1, r2 = Receiver(), Receiver()
    d = tmp()
    s1, s2 = secret(), secret()
    conf(d, ["# a list, and a line without one", f"1 127.0.0.1 {r1.port} {s1} types=a.*,b", f"2 127.0.0.1 {r2.port} {s2}"])
    svc = start(d, extra=[])
    post_event(svc, 1, "a.x")
    post_event(svc, 2, "c")
    post_event(svc, 3, "b")
    check("8. endpoints.conf without a database: the `types=` word filters", wait_for(lambda: r1.events() == [1, 3] and sorted(r2.events()) == [1, 2, 3], 5), str((r1.events(), r2.events())))
    check("8. ... and GET shows it", get(svc, "/endpoints/1")["types"] == ["a.*", "b"] and get(svc, "/endpoints/2")["types"] == [], str(get(svc, "/endpoints")))
    stop(svc)
    out = subprocess.run([BIN, "--port", "1", "--dir", d, "--import-endpoints", "1", "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=30)
    check("8. --import-endpoints copies the words into the table", out.returncode == 0 and psql("select id, types from endpoints order by id") == [("1", "a.*,b"), ("2", "")], str((out.returncode, out.stderr, psql("select id, types from endpoints order by id"))))
    for bad_line in ("types=a*", "types=a,,b", "types=a types=b", "types="):
        conf(d, [f"1 127.0.0.1 {r1.port} {s1} {bad_line}"])
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=20)
        check(f"8. a bad list ({bad_line}) is a refusal to start, naming the line", out.returncode == 13 and "line 1" in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    r1.close()
    r2.close()
    # ---- 9. a list that changes, and a restart ----------------------------------------------------------------
    reset_db()
    seen_first = set()
    r1 = Receiver(status=lambda i, n: 500 if n in (1, 3) and n not in seen_first and not seen_first.add(n) else 204)
    add_endpoint(1, r1.port, secret(), types="a")
    d = tmp()
    svc = start(d, schedule="1500")
    for n, typ in ((1, "a"), (2, "a"), (3, "a"), (4, "b"), (5, "a")):
        post_event(svc, n, typ)
    ok = wait_for(lambda: sorted(r1.events()) == [1, 2, 3, 5] and get(svc, "/stats")["filtered"] == 1, 5)
    check("9. events 1 and 3 failed once (retry in 1.5 s), 2 and 5 were delivered above the stuck cursor, 4 (type b) was passed over", ok and cursors(svc) == {1: 0}, str((r1.events(), cursors(svc), get(svc, "/stats"))))
    st, out = req(svc, "PATCH", "/endpoints/1", {"types": ["b"]})
    check("9. the list is narrowed to b", st == 200 and get(svc, "/endpoints/1")["types"] == ["b"])
    kill9(svc)
    n_before = r1.count()
    svc = start(d, schedule="1500")
    ok = wait_for(lambda: cursors(svc) == {1: 5}, 10)
    seen = r1.events()[n_before:]
    check("9. after the restart the two events with an attempt behind them are retried, though the list no longer wants type a", ok and sorted(x for x in seen if x in (1, 3)) == [1, 3], str((seen, cursors(svc))))
    check("9. ... the ones already delivered are not sent again", 2 not in seen and 5 not in seen, str(seen))
    check("9. ... event 4, passed over before the restart but not behind the cursor, is decided again under the list now in force (b): sent", 4 in seen, str(seen))
    check("9. ... and nothing was passed over by the new process (the delivered and the retried are not counted)", get(svc, "/stats")["filtered"] == 0, str(get(svc, "/stats")))
    stop(svc)
    shutil.rmtree(d)
    r1.close()
    finish("filter")


main()
