#!/usr/bin/env python3
"""More than 62 endpoints (docs/design.md section 41, docs/production.md P1.6): the service has 1,024.

    [STAGES=limit,flags] HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/many_test.py build/hooks

One receiver stands for every endpoint (tests/manykit.py): endpoint `k` is the address 127.0.x.y of the same port, and the signature is checked with that endpoint's own
key. Stages (all by default; `STAGES=name,name` runs some):

  limit      1,024 endpoints created through `POST /endpoints` (the 1,025th is a 409 and nothing is stored), the listing in pages that follow X-Next-Offset, a delete frees
             a slot, a table of 1,025 rows is status 13; the log carries the marker of a wide slot once, before the first, and a restart writes none; one event reaches
             every endpoint once, signed with its own key, and the 16 pages of /metrics (page 0 with the service's series, none of them over 64 KiB) are every endpoint once
  chaos      `kill -9` as a power cut four times while events are posted to 1,024 endpoints whose receivers fail the first attempt of a third of the events (a status, a reset,
             a close): every acknowledged event reaches every endpoint at least once, every cursor is at the last event, the same after one more restart
  flags      disabled (410), paused (a record of the breaker) and enabled in slots 0 to 1,023 (the bit-sets of one integer are words now): nothing is sent to them, what is read
             of them is the same after a restart, enabling one in a wide slot resumes it and only it; a backup and a restore keep all of it
  replay     dead letters, a named replay and the bulk replay at a slot above 62; a replay to every endpoint that subscribes (types) and the refusal when too many would
  quiet      the loop does not look at endpoints that have nothing to do: after every endpoint has caught up, ten turns look at none; an event is one look at each
  retention  an event pinned by a paused endpoint in slot 1,000 is not dropped, across a snapshot of the outcomes log and a restart; when it is enabled and caught up the
             events go (the snapshot carries the marker of a wide slot and the paused flag)
  compactnow `--compact-now 1` with a database: the table is read first (status 20 and nothing changed if it cannot be in pg-start-wait-ms), then the pass; a paused endpoint's events stay
  pool       a database that is away at the start, 1,024 endpoints read from it when it comes back (a reply over 128 KiB), the events taken meanwhile delivered once to each
  formats    a log from before (62 endpoints, no marker) is read as it was and written as it was; a kind of record this build does not know is refused (15); a table of
             1,025 endpoints and a table too large to read are refused
  oldbuild   (HOOKS_BIN_MAIN=the binary of the build before: not run in CI) that build reads this one's logs, and refuses one with a wide slot (15)
"""
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import manykit as mk  # noqa: E402
import opslib  # noqa: E402
from manykit import Fan, addr, wait_for  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
STAGES = [x for x in os.environ.get("STAGES", "").split(",") if x]   # (not argv: tests/chaos.py reads the arguments as numbers)
N = 1024
TOKEN = "correct-horse-battery-staple"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
check = opslib.Checks()
DIRS = []


def tmp(prefix="hooks-many-"):
    d = tempfile.mkdtemp(prefix=prefix)
    DIRS.append(d)
    return d


def service(d, extra=(), **kw):
    return opslib.Service(BIN, d, ["--admin-token", TOKEN, *opslib.pg_flags(), *extra], **kw)


def authed(svc, method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    status, out, hdrs = svc.request(method, path, data, headers, timeout=timeout)
    # a change of the endpoints is a 503 for as long as the connections to the database that stores it are not up (just after a start, on a loaded machine): it is asked again
    end = time.time() + 30
    while status == 503 and method != "GET" and path.startswith("/endpoints") and time.time() < end:
        time.sleep(0.2)
        status, out, hdrs = svc.request(method, path, data, headers, timeout=timeout)
    try:
        return status, json.loads(out) if out else None, hdrs
    except ValueError:
        return status, out, hdrs


def post_events(svc, ns, typ="t", retry=True, pad=0):
    """Post events `ns`; answers {n: event id} of the acknowledged ones. A failed request is tried again (the service may be down: the caller restarts it)."""
    acked = {}
    for n in ns:
        body = json.dumps({"type": typ, "n": n, **({"pad": "x" * pad} if pad else {})}).encode()
        for _ in range(200 if retry else 1):
            try:
                status, data, _ = svc.request("POST", "/events", body, {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}, timeout=5)
            except OSError:
                time.sleep(0.05)
                continue
            if status == 202:
                acked[n] = json.loads(data)["id"]
                break
            time.sleep(0.05)
    return acked


def power_cut(d):
    """Leave the files as a power cut could (what the last fsync covered, and a random part of the rest), as tests/chaos.py does."""
    dummy = types.SimpleNamespace(datadir=d)
    for name in sorted(os.listdir(d)):
        if name.endswith((".seg", ".first")):
            chaos.Service.cut_file(dummy, os.path.join(d, name))


def cursors(svc):
    rows, _, _, total = mk.listing(svc)
    return {r["id"]: r for r in rows}, total


def append_records(d, rows):
    """Append outcome records [(kind, slot, event, attempts, next_at)] to delivery.seg of a stopped service, with the sequence numbers after the last."""
    path = os.path.join(d, "delivery.seg")
    data = open(path, "rb").read()
    recs, end = chaos.read_log(data, headers=True)
    seq = max(ms for ms, _ in recs) + 1
    with open(path, "r+b") as f:
        f.truncate(end)
        f.seek(end)
        for kind, slot, event, attempts, nxt in rows:
            f.write(opslib.put_record(seq, kind, slot, event, attempts, nxt))
            seq += 1


def fresh(count=N, port=0, **kw):
    mk.reset_db()
    return mk.insert_endpoints(count, port, **kw)


def all_pages_of_metrics(svc):
    """Every page of /metrics, parsed strictly: ([Metrics], the largest page in bytes). Page k is asked for until a 404."""
    out, biggest, k = [], 0, 0
    while True:
        status, data = mk.metrics_page(svc, k)
        if status == 404:
            return out, biggest, k
        assert status == 200, (k, status, data[:200])
        biggest = max(biggest, len(data))
        out.append(opslib.parse_metrics(data.decode()))
        k += 1
        assert k < 200


# --------------------------------------------------------------------------------------------------------------------------------

def stage_limit():
    print("== limit: 1,024 endpoints through the API", flush=True)
    mk.reset_db()
    d = tmp()
    fan = Fan({})
    svc = service(d, ["--schedule", "200,200", "--deadline-ms", "3000"])
    check("limit: the service starts with an empty table", svc.start() and svc.has_line("endpoints loaded: 0"), svc.stderr())
    check("limit: the limit is 1,024", svc.stats()["max_endpoints"] == N)
    made, bad = {}, []
    t0 = time.time()
    for k in range(N):
        status, out, _ = authed(svc, "POST", "/endpoints", {"host": addr(k), "port": fan.port})
        if status != 201:
            bad.append((k, status, out))
            break
        made[addr(k)] = (out["id"], out["secret"])
    check("limit: 1,024 endpoints are created, each 201 with its id and its secret", len(made) == N and not bad, str(bad[:2]))
    print(f"     ({time.time() - t0:.1f} s for {len(made)} POSTs)", flush=True)
    fan.add(made)
    check("limit: /stats says 1,024 endpoints and the table has 1,024 rows", svc.stats()["endpoints"] == N and len(mk.table_ids()) == N)
    status, out, _ = authed(svc, "POST", "/endpoints", {"host": addr(N), "port": fan.port})
    check("limit: the 1,025th is a 409 that says 1024, and no row was stored", status == 409 and "1024" in json.dumps(out) and len(mk.table_ids()) == N, str((status, out)))
    # the log: one `created` for each endpoint, in slots 0 to 1,023, and the marker once, before the first slot of 62 or above
    recs = mk.delivery_records(d)
    created = [r for r in recs if r[0] == 10]
    wide = [i for i, r in enumerate(recs) if r[0] == 18]
    first_wide_created = next(i for i, r in enumerate(recs) if r[0] == 10 and r[1] >= 62)
    check("limit: a `created` for each of the 1,024 slots, once each", sorted(r[1] for r in created) == list(range(N)), str(len(created)))
    check("limit: the marker of a wide slot (kind 18) is in the log once, before the `created` of slot 62", len(wide) == 1 and wide[0] < first_wide_created, str((wide, first_wide_created)))
    check("limit: the log begins with the header: kind 15, the number 62 where the limit was, format 2", recs[0][0] == 15 and recs[0][1] == 62 and recs[0][2] == 2, str(recs[:1]))
    # the listing in pages
    rows, pages, biggest, total = mk.listing(svc)
    check("limit: GET /endpoints follows X-Next-Offset through 16 pages of 64 to every endpoint once", pages == 16 and total == N and sorted(r["id"] for r in rows) == mk.table_ids(), f"{pages} {total} {len(rows)}")
    check("limit: ... and no page is near the 64 KiB the server can send", biggest < 40000, str(biggest))
    rows, pages, biggest, total = mk.listing(svc, limit=256)
    check("limit: with limit=256 a page ends where its text passes 40 KiB, so it is a few more pages (every endpoint once, none over 48 KiB)", 4 <= pages <= 8 and sorted(r["id"] for r in rows) == mk.table_ids() and biggest < 49152, f"{pages} {len(rows)} {biggest}")
    status, out, hdrs = authed(svc, "GET", "/endpoints?limit=257")
    check("limit: limit=257 is a 400 that says so", status == 400 and "256" in json.dumps(out), str((status, out)))
    status, out, hdrs = authed(svc, "GET", "/endpoints?offset=-1")
    check("limit: offset=-1 is a 400", status == 400, str((status, out)))
    status, out, hdrs = authed(svc, "GET", f"/endpoints?offset={N + 5}")
    check("limit: an offset past the end is an empty list that still says how many there are", status == 200 and out == [] and {k.lower(): v for k, v in hdrs.items()}["x-total-count"] == str(N), str((status, out)))
    status, out, hdrs = authed(svc, "GET", "/endpoints")
    check("limit: with no query it is the first 64, and says where to go on", status == 200 and len(out) == 64 and {k.lower(): v for k, v in hdrs.items()}["x-next-offset"] == "64", str(len(out)))
    # one event to every endpoint
    acked = post_events(svc, [1])
    eid = acked[1]
    check("limit: one event reaches every one of the 1,024 endpoints", wait_for(lambda: fan.reached([eid]) == N, 60), f"{fan.reached([eid])}")
    time.sleep(0.5)
    check("limit: ... exactly once each, every signature the endpoint's own", all(v == 1 for v in fan.hits.values()) and not fan.bad, str(fan.bad[:2]))
    cur, total = cursors(svc)
    check("limit: every cursor is 1", len(cur) == N and all(r["cursor"] == eid for r in cur.values()))
    # /metrics in pages
    ms, biggest, npages = all_pages_of_metrics(svc)
    check("limit: /metrics is 16 pages and a 17th is a 404", npages == 16, str(npages))
    check("limit: the longest page is under 64 KiB (the server's queue for an answer)", biggest < 56000, str(biggest))
    check("limit: page 0 has the service's series and says there are 16 pages", ms[0].value("hooks_metrics_pages") == 16 and ms[0].value("hooks_endpoints") == N)
    check("limit: the other pages have none of the service's series", all("hooks_uptime_seconds" not in {k[0] for k in m} for m in ms[1:]))
    ids = []
    for m in ms:
        ids += [int(dict(k)["endpoint"]) for k in m.series("hooks_endpoint_cursor")]
    check("limit: the pages together have each endpoint once", sorted(ids) == mk.table_ids(), str(len(ids)))
    check("limit: every endpoint's cursor series says 1", all(v == eid for m in ms for v in m.series("hooks_endpoint_cursor").values()))
    check("limit: 9 per-endpoint families for each endpoint over the pages (and no last failure: none has failed)",
          sum(len([k for k in m if k[0].startswith("hooks_endpoint_") and k[0] != "hooks_endpoint_last_failure"]) for m in ms) == N * 9)
    status, data = mk.metrics_page(svc, "x")
    check("limit: page=x is a 400", status == 400, str(status))
    # a restart: 1,024 read from the table, nothing sent again, no new records for them
    n_created = len(created)
    svc.stop()
    svc2 = service(d, ["--schedule", "200,200", "--deadline-ms", "3000"], port=svc.port)
    check("limit: a restart reads the 1,024 from the table", svc2.start() and svc2.has_line("endpoints loaded: 1024"), svc2.stderr())
    recs2 = mk.delivery_records(d)
    check("limit: ... writes no slot record and no second marker", len([r for r in recs2 if r[0] == 10]) == n_created and len([r for r in recs2 if r[0] == 18]) == 1)
    acked2 = post_events(svc2, [2])
    check("limit: ... and event 2 reaches all of them", wait_for(lambda: fan.reached([acked2[2]]) == N, 60))
    time.sleep(0.3)
    check("limit: ... event 1 was not sent again", all(fan.hits.get((a, eid), 0) == 1 for a in made))
    # a delete frees a slot at once
    some = made[addr(1000)][0]
    status, out, _ = authed(svc2, "DELETE", f"/endpoints/{some}")
    check("limit: a delete is 200", status == 200 and out["deleted"], str((status, out)))
    status, out, _ = authed(svc2, "POST", "/endpoints", {"host": addr(1000), "port": fan.port})
    check("limit: ... and a POST is a 201 again (it takes the freed slot)", status == 201, str((status, out)))
    status, out, _ = authed(svc2, "POST", "/endpoints", {"host": addr(N + 1), "port": fan.port})
    check("limit: ... and the 1,025th is a 409 again", status == 409, str((status, out)))
    svc2.stop()
    recs3 = mk.delivery_records(d)
    check("limit: ... and the endpoint given a wide slot after a restart brings no second marker (the first, read at the start, is known)",
          len([r for r in recs3 if r[0] == 18]) == 1 and len([r for r in recs3 if r[0] == 10]) == n_created + 1, str((len([r for r in recs3 if r[0] == 18]), len([r for r in recs3 if r[0] == 10]), n_created)))
    # a table of 1,025 rows is refused at the start
    mk.insert_endpoints(1, fan.port, ids=[900000], host=addr(N + 2))
    svc3 = service(d, ["--schedule", "200,200"])
    svc3.start(timeout=20, loaded=False)
    code = svc3.wait_exit(30)
    check("limit: a table of 1,025 endpoints ends the service with status 13, and says which row", code == 13 and any("row" in l and "1025" in l for l in svc3.lines), f"{code} {svc3.stderr()[-300:]}")
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_chaos(events=18, kills=4):
    print("== chaos: kill -9 as a power cut, 1,024 endpoints that fail", flush=True)
    d = tmp()
    fan = Fan({}, policy=lambda k, n, attempt: ("close" if k % 20 == 1 else "reset" if k % 20 == 2 else 500) if attempt == 1 and (k * 7 + n * 13) % 10 < 3 else 204)
    table = fresh(port=fan.port)
    fan.add(table)
    args = ["--schedule", "150,150,150,150,150,150", "--deadline-ms", "2000"]
    svc = service(d, args, power_loss=True)
    port = svc.port
    acked, round_no = {}, 0
    per = events // (kills + 1)
    while round_no <= kills:
        check(f"chaos: start {round_no + 1} reads the 1,024", svc.start(timeout=60) and svc.has_line("endpoints loaded: 1024"), svc.stderr()[-300:])
        got = post_events(svc, range(round_no * per + 1, (round_no + 1) * per + 1))
        acked.update(got)
        if round_no < kills:
            # kill when the deliveries have made progress, not after a time: some of this round's events have reached some endpoints
            before = fan.count()
            wait_for(lambda: fan.count() >= before + 600 + 40 * round_no, 60)
            svc.kill()
            power_cut(d)
            svc = service(d, args, port=port, power_loss=True)
        round_no += 1
    ids = sorted(acked.values())
    ok = wait_for(lambda: not fan.missing(ids), 180)
    check(f"chaos: every one of the {len(ids)} acknowledged events reaches every one of the 1,024 endpoints (after {kills} power cuts)", ok, f"missing {len(fan.missing(ids))}: {fan.missing(ids)[:3]}")
    last = max(ids)

    def caught_up():
        cur, total = cursors(svc)
        return total == N and all(r["cursor"] >= last for r in cur.values())
    check("chaos: every cursor is at the last event", wait_for(caught_up, 120), str(sorted({r['cursor'] for r in cursors(svc)[0].values()})[:5]))
    check("chaos: no delivery was signed with another endpoint's key", not fan.bad, str(fan.bad[:2]))
    check("chaos: the failing first attempts were really made (a third of the pairs were tried twice or more)", sum(1 for v in fan.hits.values() if v > 1) > len(ids) * N // 10, str(sum(1 for v in fan.hits.values() if v > 1)))
    logged, _, _ = opslib.events_log(d)
    check("chaos: every acknowledged event is in the events log", all(i in logged for i in ids))
    time.sleep(1.0)
    before = fan.count()
    svc.stop()
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    time.sleep(2.0)
    check("chaos: one more restart sends nothing again (no more than what was in flight when it was stopped)", fan.count() - before <= 64, str(fan.count() - before))
    svc.stop()
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

DIS = sorted({i for i in range(N) if i % 5 == 2} | {61, 62, 63, 64, 127, 128, 255, 256, 511, 512, 1000, 1023})
PAU = sorted({i for i in range(N) if i % 7 == 3} | {0, 61, 62, 63, 64, 127, 128, 511, 512, 1001, 1022, 1023})


def stage_flags():
    print("== flags: disabled, paused and enabled in every slot", flush=True)
    d = tmp()
    gone_events = set()

    def policy(k, n, attempt):
        if n in gone_events and k in DIS and attempt == 1:
            return 410
        return 204
    fan = Fan({}, policy=policy)
    table = fresh(port=fan.port)           # ids 0..1023: each takes the slot of its own number
    fan.add(table)
    by_id = {v[0]: a for a, v in table.items()}
    args = ["--schedule", "150,150,150", "--deadline-ms", "2000"]
    svc = service(d, args)
    svc.start(timeout=60)
    acked = post_events(svc, [1, 2, 3])
    e = [acked[1], acked[2], acked[3]]
    check("flags: three events reach every endpoint", wait_for(lambda: fan.reached(e) == 3 * N, 90), str(fan.reached(e)))
    wait_for(lambda: all(r["cursor"] >= e[2] for r in cursors(svc)[0].values()), 60)
    port = svc.port
    svc.stop()
    # the breaker's records, written the way the service writes them, for the slots that are paused (breaker_test does the same)
    now_ms = int(time.time() * 1000)
    rows = [(12, i, 0, 0, now_ms - 3600000) for i in PAU[::3]] + [(13, i, 0, 0, 0) for i in PAU]
    append_records(d, rows)
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    cur, total = cursors(svc)
    pau_only = set(PAU)
    check("flags: after the start the paused slots are paused and disabled, and the others are not", all(cur[i]["paused"] and cur[i]["disabled"] for i in PAU) and not any(cur[i]["paused"] or cur[i]["disabled"] for i in range(N) if i not in pau_only))
    check("flags: /stats counts the paused endpoints", svc.stats()["paused"] == len(PAU), str(svc.stats()["paused"]))
    # event 4: the receivers answer 410 to the endpoints of DIS (the first attempt), the paused ones are not called at all
    gone_events.add(e[2] + 1)        # (the ids are dense: the fourth event is the next id, and the receivers must know it before the first delivery of it)
    acked4 = post_events(svc, [4])
    e4 = acked4[4]
    check("flags: the fourth event has the id the receivers were told", e4 == e[2] + 1, str((e4, e)))
    want = [by_id[i] for i in range(N) if i not in pau_only]
    check("flags: event 4 reaches every endpoint that is not paused, and none that is", wait_for(lambda: fan.reached([e4], want) == len(want), 90) and fan.reached([e4], [by_id[i] for i in PAU]) == 0, f"{fan.reached([e4], want)} of {len(want)}")
    time.sleep(0.5)
    cur, total = cursors(svc)
    dis_only = [i for i in DIS if i not in pau_only]
    check("flags: a 410 disabled each endpoint of DIS (their cursors passed the event: it is a dead letter)", all(cur[i]["disabled"] and not cur[i]["paused"] and cur[i]["cursor"] == e4 for i in dis_only), str([(i, cur[i]) for i in dis_only if not cur[i]["disabled"]][:2]))
    check("flags: the others are at event 4, the paused ones still at 3", all(cur[i]["cursor"] == e4 for i in range(N) if i not in pau_only) and all(cur[i]["cursor"] == e[2] for i in PAU))
    ms, biggest, npages = all_pages_of_metrics(svc)
    check("flags: /metrics says the same: disabled and paused by endpoint over all 16 pages", sum(v for m in ms for v in m.series("hooks_endpoint_disabled").values()) == len(set(DIS) | pau_only)
          and sum(v for m in ms for v in m.series("hooks_endpoint_paused").values()) == len(PAU))
    # event 5: nobody disabled or paused is called
    acked5 = post_events(svc, [5])
    e5 = acked5[5]
    active_ids = {i for i in range(N) if i not in pau_only and i not in set(DIS)}
    active = [by_id[i] for i in sorted(active_ids)]
    check("flags: event 5 reaches the others", wait_for(lambda: fan.reached([e5], active) == len(active), 90))
    time.sleep(0.5)
    check("flags: ... and none of the disabled or paused", fan.reached([e5], [by_id[i] for i in set(DIS) | pau_only]) == 0)
    # a restart: the same flags, and still nothing sent to them
    svc.stop()
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    cur2, _ = cursors(svc)
    same = [i for i in range(N) if i not in active_ids and (cur2[i]["disabled"], cur2[i]["paused"], cur2[i]["cursor"]) != (cur[i]["disabled"], cur[i]["paused"], cur[i]["cursor"])]
    check("flags: after a restart the disabled and paused are the same, endpoint by endpoint, with the cursors where they were", not same and all(not cur2[i]["disabled"] and cur2[i]["cursor"] >= e5 for i in active_ids), str([(i, cur[i], cur2[i]) for i in same[:2]]))
    acked6 = post_events(svc, [6])
    e6 = acked6[6]
    check("flags: event 6 reaches the others and none of the disabled or paused", wait_for(lambda: fan.reached([e6], active) == len(active), 90) and fan.reached([e6], [by_id[i] for i in set(DIS) | pau_only]) == 0)
    # the loop does not look, turn after turn, at endpoints that are disabled or paused and behind (they have events to read and are sent nothing)
    def looks_settle():
        last = None
        end = time.time() + 40
        while time.time() < end:
            a = svc.stats()
            if not wait_for(lambda: svc.stats()["turns"] > a["turns"] + 10, 10):
                return None
            b = svc.stats()
            if b["endpoints_looked_at"] == a["endpoints_looked_at"]:
                return b["endpoints_looked_at"] - a["endpoints_looked_at"]
            last = b["endpoints_looked_at"] - a["endpoints_looked_at"]
        return last
    check("flags: with 214 disabled and 158 paused endpoints behind the events, ten turns look at no endpoint (they are skipped, not read each turn)", looks_settle() == 0)
    # enabling: one wide slot of each kind, and only they resume
    dis_wide = max(i for i in dis_only if i >= 62)
    pau_wide = max(i for i in PAU if i not in set(DIS))      # (one of DIS would answer 410 to event 4 when it gets it, which is its own story: it is disabled again)
    for ident in (dis_wide, pau_wide):
        status, out, _ = authed(svc, "POST", f"/endpoints/{ident}/enable")
        check(f"flags: POST /endpoints/{ident}/enable is 200", status == 200, str((status, out)))
    ok = wait_for(lambda: fan.reached([e5, e6], [by_id[dis_wide]]) == 2 and fan.reached([e4, e5, e6], [by_id[pau_wide]]) == 3, 60)
    check("flags: the enabled endpoints get what they missed (the disabled one 5 and 6, the paused one 4, 5 and 6), once each", ok and all(fan.hits[(by_id[i], n)] == 1 for i, ns in ((dis_wide, (e5, e6)), (pau_wide, (e4, e5, e6))) for n in ns), str(fan.missing([e4, e5, e6], [by_id[pau_wide]])))
    check("flags: and no other disabled or paused endpoint was sent anything", fan.reached([e5, e6], [by_id[i] for i in set(DIS) | pau_only if i not in (dis_wide, pau_wide)]) == 0)
    svc.stop()
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    cur3, _ = cursors(svc)
    still = [i for i in set(DIS) | pau_only if i not in (dis_wide, pau_wide) and not cur3[i]["disabled"]]
    check("flags: a restart keeps them enabled (the log has the record) and the rest as it was", not cur3[dis_wide]["disabled"] and not cur3[pau_wide]["disabled"] and not cur3[pau_wide]["paused"] and not still,
          str((dis_wide, pau_wide, cur3[dis_wide], cur3[pau_wide], still[:3], [(r[0], r[2]) for r in mk.delivery_records(d) if r[1] == pau_wide and r[0] in (4, 5, 10, 11, 12, 13)])))
    # a backup and a restore keep all of it
    svc.stop()
    out_dir = tmp("hooks-many-backup-")
    run = lambda cmd: subprocess.run(cmd, capture_output=True, text=True, env=opslib.pg_env())  # noqa: E731
    pgf = ["--pg-host", opslib.PG_HOST, "--pg-port", opslib.PG_PORT, "--pg-user", opslib.PG_USER, "--pg-database", opslib.PG_DB]
    p = run([os.path.join(ROOT, "scripts", "backup.sh"), "--dir", d, "--out", out_dir, "--mode", "stopped", *pgf])
    check("flags: backup.sh with 1,024 endpoints and wide slots in the log verifies (kind 18 is a kind the checker knows)", p.returncode == 0, p.stderr[-400:])
    made = sorted(os.listdir(out_dir))
    bk = os.path.join(out_dir, made[-1]) if made else ""
    restored = tmp("hooks-many-restored-")
    p = run([os.path.join(ROOT, "scripts", "restore.sh"), "--backup", bk, "--dir", restored, *pgf])
    check("flags: restore.sh restores it", p.returncode == 0, p.stderr[-400:])
    svc = service(restored, args)
    svc.start(timeout=60)
    cur4, total4 = cursors(svc)
    check("flags: the restored service has the 1,024 endpoints and the same flags and cursors, endpoint by endpoint", total4 == N and all((cur4[i]["disabled"], cur4[i]["paused"], cur4[i]["cursor"]) == (cur3[i]["disabled"], cur3[i]["paused"], cur3[i]["cursor"]) for i in range(N)),
          str([i for i in range(N) if (cur4[i]["disabled"], cur4[i]["paused"], cur4[i]["cursor"]) != (cur3[i]["disabled"], cur3[i]["paused"], cur3[i]["cursor"])][:5]))
    svc.stop()
    check("flags: no delivery was signed with another endpoint's key", not fan.bad, str(fan.bad[:2]))
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_replay():
    print("== replay: dead letters and replays in a wide slot", flush=True)
    d = tmp()
    state = {"dead_event": -1, "flip": False}
    WANT = {3, 600, 1023}

    def policy(k, n, attempt):
        return 410 if k == 1000 and not state["flip"] and n == state["dead_event"] and attempt == 1 else 204
    fan = Fan({}, policy=policy)
    table = fresh(port=fan.port, types=lambda k: "t.a" if k in WANT else "other.*")
    fan.add(table)
    by_id = {v[0]: a for a, v in table.items()}
    args = ["--schedule", "100,100", "--deadline-ms", "2000"]
    svc = service(d, args)
    svc.start(timeout=60)
    # events of a type that three endpoints subscribe to: every endpoint reads each and passes over it, and the three are called
    got = post_events(svc, [1, 2, 3], typ="t.a")
    e = [got[1], got[2], got[3]]
    ids_want = [by_id[i] for i in WANT]
    check("replay: events of a type three endpoints subscribe to reach those three, once each, and nobody else", wait_for(lambda: fan.reached(e, ids_want) == 9, 60) and fan.reached(e) == 9, str(fan.reached(e)))
    check("replay: every endpoint is at the last of them (the others passed over them: nothing was sent)", wait_for(lambda: all(r["cursor"] >= e[2] for r in cursors(svc)[0].values()), 60) and svc.stats()["filtered"] >= (N - 3) * 3, str(svc.stats()["filtered"]))
    status, out, _ = authed(svc, "POST", f"/events/{e[0]}/replay")
    check("replay: POST /events/:id/replay goes to the three that subscribe, by id", status == 202 and sorted(out["endpoints"]) == sorted(WANT), str((status, out)))
    check("replay: ... and they get it again, once", wait_for(lambda: all(fan.hits.get((a, e[0])) == 2 for a in ids_want), 30), str({a: fan.hits.get((a, e[0])) for a in ids_want}))
    status, out, _ = authed(svc, "POST", f"/events/{e[1]}/replay/1022")
    check("replay: POST /events/:id/replay/1022 to an endpoint that does not subscribe is accepted", status == 202 and out["endpoints"] == [1022], str((status, out)))
    check("replay: ... and 1022 gets that event, once", wait_for(lambda: fan.hits.get((by_id[1022], e[1])) == 1, 30))
    # events of another type go to the 1,021 others; the receiver answers 410 to endpoint 1000 for the second of them
    got = post_events(svc, [4], typ="other.x")
    e4 = got[4]
    others = [by_id[i] for i in range(N) if i not in WANT]
    check("replay: an event of the other type goes to the 1,021 that subscribe to it", wait_for(lambda: fan.reached([e4], others) == len(others), 90), str(fan.reached([e4], others)))
    state["dead_event"] = e4 + 1
    got = post_events(svc, [5], typ="other.x")
    e5 = got[5]
    check("replay: the next event has the id the receiver was told (ids follow one another)", e5 == e4 + 1, f"{e5} {e4}")
    check("replay: ... and reaches the others", wait_for(lambda: fan.reached([e5], others) == len(others), 90), str(fan.reached([e5], others)))
    cur, _ = cursors(svc)
    check("replay: endpoint 1000 answered 410 to it: disabled, and the event is its dead letter (the cursor passed it)", cur[1000]["disabled"] and cur[1000]["cursor"] == e5, str(cur[1000]))
    status, out, _ = authed(svc, "GET", "/endpoints/1000/dead")
    check("replay: GET /endpoints/1000/dead lists it with the reason gone", status == 200 and [x["event"] for x in out["dead"]] == [e5] and out["dead"][0]["reason"] == "gone", str((status, out)))
    status, out, _ = authed(svc, "GET", "/endpoints/1023/dead")
    check("replay: and nothing for 1023", status == 200 and out["dead"] == [], str((status, out)))
    state["flip"] = True
    authed(svc, "POST", "/endpoints/1000/enable")
    status, out, _ = authed(svc, "POST", "/endpoints/1000/replay-dead")
    check("replay: POST /endpoints/1000/replay-dead takes it", status in (200, 202) and out["taken"] == 1 and out["remaining"] == 0, str((status, out)))
    check("replay: ... and it is delivered (the 410 and the replay: two requests), and the list is empty", wait_for(lambda: fan.hits.get((by_id[1000], e5)) == 2, 30) and wait_for(lambda: authed(svc, "GET", "/endpoints/1000/dead")[1]["dead"] == [], 30))
    status, out, _ = authed(svc, "POST", f"/events/{e4}/replay")
    check("replay: a replay to every endpoint that subscribes, 1,021 of them, is a 507 (32 replays wait at most), and nothing is stored", status == 507 and svc.stats()["replays"] == 0, str((status, out)))
    svc.stop()
    check("replay: no delivery was signed with another endpoint's key", not fan.bad, str(fan.bad[:2]))
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_quiet():
    print("== quiet: the loop looks at endpoints that have work", flush=True)
    d = tmp()
    fan = Fan({})
    table = fresh(port=fan.port, types=lambda k: "t.a" if k == 700 else "other.*")
    fan.add(table)
    svc = service(d, ["--schedule", "100,100", "--deadline-ms", "2000"])
    svc.start(timeout=60)
    s0 = svc.stats()
    check("quiet: at the start every endpoint is looked at once or twice (it has not said it has nothing to do), not more", s0["endpoints_looked_at"] <= 2 * N, str(s0))
    time.sleep(1.5)
    s1 = svc.stats()
    check("quiet: with nothing to do, turns go by (a request is one) and no endpoint is looked at", s1["turns"] > s0["turns"] + 10 and s1["endpoints_looked_at"] == s0["endpoints_looked_at"], f"{s0['turns']}->{s1['turns']} {s0['endpoints_looked_at']}->{s1['endpoints_looked_at']}")
    got = post_events(svc, [1], typ="t.a")
    wait_for(lambda: fan.count() == 1, 30)
    wait_for(lambda: all(r["cursor"] >= got[1] for r in cursors(svc)[0].values()), 30)
    time.sleep(0.5)
    s2 = svc.stats()
    check("quiet: one event is a look at each of the 1,024 endpoints once (1,023 pass it over, one is sent it), not a look at each in every turn after", s2["endpoints_looked_at"] - s1["endpoints_looked_at"] <= 2 * N and s2["endpoints_looked_at"] - s1["endpoints_looked_at"] >= N, f"{s2['endpoints_looked_at'] - s1['endpoints_looked_at']}")
    time.sleep(1.5)
    s3 = svc.stats()
    check("quiet: and then none again", s3["endpoints_looked_at"] == s2["endpoints_looked_at"] and s3["turns"] > s2["turns"] + 10, str(s3["endpoints_looked_at"] - s2["endpoints_looked_at"]))
    # (the backlog of events nobody wants, passed over at the speed of the loop and not of its timer, is the `pool` stage's and the next check's)
    # an endpoint whose attempts fail is looked at when its retry is due, not before, and not every turn: 1,024 endpoints, nothing listening on the port of the receiver
    svc.stop()
    fan.close()
    mk.reset_db()
    dead_port = chaos.free_port()
    table = mk.insert_endpoints(N, dead_port)
    d2 = tmp()
    svc = service(d2, ["--schedule", "1500,1500,1500", "--deadline-ms", "1000"])
    svc.start(timeout=60)
    post_events(svc, [1])
    wait_for(lambda: svc.stats()["failed"] >= N, 60)
    time.sleep(0.4)
    a = svc.stats()
    time.sleep(1.0)
    b = svc.stats()
    turns, looks = b["turns"] - a["turns"], b["endpoints_looked_at"] - a["endpoints_looked_at"]
    check("quiet: 1,024 endpoints whose attempts failed and wait are not all looked at in each turn (fewer than a quarter on average, in at least 4 turns)", turns >= 4 and looks < N * turns / 4, f"{looks} looks in {turns} turns")
    check("quiet: ... and their retries are made when due (the second attempt of each, then a third)", wait_for(lambda: svc.stats()["attempts"] >= 3 * N, 90), str(svc.stats()["attempts"]))
    svc.stop()
    # A turn walks a bounded number of cells of windows (hooks.ls most_walk(): 65,536). 1,024 endpoints that have failed 100 events each have 102,400 cells that are not
    # final and not due, which each endpoint reads once to learn when to look again: more than one turn may walk. The turns that stop for it do not wait for the timer
    # (/stats waits_skipped), the walk is finished, and then nothing is looked at while the retries wait. (Without the bound it is one turn of about 100 ms.)
    mk.reset_db()
    mk.insert_endpoints(N, dead_port)
    d5 = tmp()
    svc = service(d5, ["--schedule", "600000,600000", "--deadline-ms", "1000"])
    svc.start(timeout=60)
    posted = post_events(svc, range(1, 101))
    ok = wait_for(lambda: svc.stats()["failed"] >= 100 * N, 120)
    check("quiet: 100 events at 1,024 endpoints that refuse them: every first attempt fails (%d)" % svc.stats()["failed"], ok and len(posted) == 100, str(svc.stats()["failed"]))

    # (the cells are walked in the turns after a start, which finds every window full of failures that wait: that is the walk this checks)
    svc.stop()
    svc = service(d5, ["--schedule", "600000,600000", "--deadline-ms", "1000"])
    svc.start(timeout=120)

    def settled():
        x = svc.stats()["endpoints_looked_at"]
        time.sleep(1.0)
        return svc.stats()["endpoints_looked_at"] == x
    check("quiet: ... after a restart the walk of their 102,400 cells ends, and then no endpoint is looked at while the retries wait", wait_for(settled, 60, 0.0))
    st = svc.stats()
    check("quiet: ... and it took more than one turn without a wait between them (/stats waits_skipped %d)" % st["waits_skipped"], st["waits_skipped"] >= 1, str(st))
    svc.stop()
    # an attempt that ends while the turn is still looking at the endpoint (a multicast address: the kernel refuses the connection at once) is not a pass that "has seen
    # everything": its retries are made, to the last (a dead letter), though no event comes to wake the endpoint
    mk.reset_db()
    mk.insert_endpoints(3, 9, host="224.0.0.1")
    d3 = tmp()
    svc = service(d3, ["--schedule", "200,200,200", "--deadline-ms", "1000"])
    svc.start(timeout=60)
    post_events(svc, [1])
    check("quiet: attempts that end at once (the connection refused by the kernel) are retried by the schedule to the end: 4 attempts at each of 3 endpoints, then dead letters",
          wait_for(lambda: svc.stats()["attempts"] >= 12 and svc.stats()["dead"] >= 3, 60), str(svc.stats()))
    svc.stop()
    # A backlog of events that nobody wants is read at the speed of the loop, not at the speed of its timer: 1,500 events are taken by a service with no endpoint at all,
    # and 1,024 endpoints (endpoints.conf, no database) that subscribe to another type come to a log that has them all, with no request to wake the loop. A budget of 4,096
    # pairs for each turn of 50 ms would take 19 s for the 1.5 million pairs; the loop goes on at once while there is more to pass over.
    d4 = tmp()
    args4 = ["--schedule", "100,100", "--deadline-ms", "2000"]
    svc = opslib.Service(BIN, d4, args4)
    svc.start()
    backlog = post_events(svc, range(1, 1501), typ="nobody.wants")
    svc.stop()
    sec = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(d4, "endpoints.conf"), "w") as f:
        for i in range(N):
            f.write(f"{i} 127.0.0.1 9 {sec} types=only.this\n")
    svc = opslib.Service(BIN, d4, args4)
    t0 = time.time()
    svc.start()
    ok = wait_for(lambda: svc.stats()["filtered"] >= 1500 * N, 60)
    took = time.time() - t0
    st = svc.stats()
    check("quiet: 1,500 events nobody subscribes to are passed over by 1,024 endpoints in %.1f s (under the %.0f s of a budget a turn of the timer would allow), and the loop did not wait between "
          "the turns that stopped for the budget (/stats waits_skipped: %d of the 375 or so)" % (took, 1500 * N / 82000.0, st["waits_skipped"]),
          len(backlog) == 1500 and ok and took < 12 and st["waits_skipped"] >= 100, f"{len(backlog)} {st['filtered']} {took:.1f} {st['waits_skipped']}")
    svc.stop()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_retention():
    print("== retention: an event pinned by a paused endpoint in slot 1,000", flush=True)
    d = tmp()
    WANT = {0, 600, 1000}
    fan = Fan({})
    table = fresh(port=fan.port, types=lambda k: "t.x" if k in WANT else "never.*")
    fan.add(table)
    by_id = {v[0]: a for a, v in table.items()}
    # the knobs of the pins of tests/retention_test.py: retention 1.5 s, an idempotency window of 0.5 s, segments of 256 KiB, an outcomes log snapshotted at 64 KiB
    # (the log is over that at the start: it has a `created` for each of the 1,024)
    args = ["--schedule", "100,100", "--deadline-ms", "2000", "--segment-bytes", "262144", "--retention-ms", "1500", "--window-ms", "500", "--delivery-log-bytes", "65536"]
    svc = service(d, args)
    svc.start(timeout=60)
    first = post_events(svc, [1], typ="t.x")
    wait_for(lambda: fan.reached(list(first.values()), [by_id[i] for i in WANT]) == 3, 30)
    wait_for(lambda: all(r["cursor"] >= first[1] for r in cursors(svc)[0].values()), 30)
    port = svc.port
    svc.stop()
    append_records(d, [(13, 1000, 0, 0, 0)])        # the breaker has paused endpoint 1000 (slot 1000)
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    ns = list(range(2, 202))
    got = post_events(svc, ns, typ="t.x", pad=2500)
    last = max(got.values())
    live = [by_id[0], by_id[600]]
    check("retention: 200 events of 2.5 KB reach the two endpoints that are not paused, and not the third", wait_for(lambda: fan.reached(list(got.values()), live) == 2 * len(ns), 120) and fan.reached(list(got.values()), [by_id[1000]]) == 0, str(fan.reached(list(got.values()), live)))
    wait_for(lambda: all(r["cursor"] >= last for i, r in cursors(svc)[0].items() if i != 1000), 60)
    time.sleep(4)      # the loop has had its chances: the snapshots, and the drops it would make
    s = svc.stats()
    check("retention: the events log kept every event: the paused endpoint holds them (nothing was dropped)", s["events_dropped"] == 0 and s["events_first_id"] == 1 and s["segments_dropped"] == 0, str(s))
    check("retention: the outcomes log was replaced by a snapshot meanwhile (the log is over its limit)", s["snapshots"] >= 1, str(s["snapshots"]))
    recs = mk.delivery_records(d)
    check("retention: the snapshot of a log with a wide slot carries the marker (kind 18), once, right after the header", [r[0] for r in recs[:2]] == [15, 18] and len([r for r in recs if r[0] == 18]) == 1, str([r[0] for r in recs[:4]]))
    p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "logcheck.py"), "check", d], capture_output=True, text=True)
    check("retention: the backup's checker reads the directory with the snapshot (one header, kind 18 known), and finds nothing wrong", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    svc.stop()
    svc = service(d, args, port=port)
    svc.start(timeout=60)
    cur, _ = cursors(svc)
    check("retention: after a restart the snapshot says what it did: 1000 is paused and at 1, the others at the last", cur[1000]["paused"] and cur[1000]["disabled"] and cur[1000]["cursor"] == first[1] and cur[0]["cursor"] == last and cur[600]["cursor"] == last, str(cur[1000]))
    time.sleep(2)
    check("retention: ... and still nothing is dropped", svc.stats()["events_dropped"] == 0)
    status, out, _ = authed(svc, "POST", "/endpoints/1000/enable")
    ok = wait_for(lambda: fan.reached(list(got.values()), [by_id[1000]]) == len(ns), 120)
    check("retention: enabled, endpoint 1000 receives the 200 events it missed, once each", ok and all(fan.hits[(by_id[1000], n)] == 1 for n in got.values()), str(fan.reached(list(got.values()), [by_id[1000]])))
    check("retention: ... and the events go (segments dropped, the first retained event is past 1)", wait_for(lambda: svc.stats()["segments_dropped"] >= 1 and svc.stats()["events_first_id"] > 1, 90), str(svc.stats()))
    svc.stop()
    check("retention: no delivery was signed with another endpoint's key", not fan.bad, str(fan.bad[:2]))
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_pool():
    print("== pool: the database is away at the start, and 1,024 endpoints are read when it comes back", flush=True)
    d = tmp()
    fan = Fan({})
    # (each endpoint has a subscription of 125 bytes, so that the table's answer is over 128 KiB, as the next check says, and about 215 KiB)
    table = fresh(port=fan.port, types=lambda k: "t," + ",".join(f"p{j}.{'y' * 56}" for j in range(2)))
    fan.add(table)
    proxy = PgProxy(opslib.PG_HOST, int(opslib.PG_PORT))
    proxy.cut()
    args = ["--schedule", "150,150", "--deadline-ms", "2000", "--pg-backoff-min-ms", "50", "--pg-backoff-max-ms", "200", "--pg-start-wait-ms", "0"]
    svc = opslib.Service(BIN, d, ["--admin-token", TOKEN, *opslib.pg_flags(port=proxy.port), *args])
    check("pool: the service listens with the database away", svc.start(timeout=20, loaded=False))
    status, data, _ = svc.request("GET", "/readyz")
    check("pool: /readyz is 503 and says the database", status == 503 and b"database" in data, str((status, data)))
    got = post_events(svc, [1, 2, 3, 4, 5])
    check("pool: events are taken meanwhile (202)", len(got) == 5)
    # (and a backlog of events that nobody subscribes to, which every endpoint will have to pass over when it comes: 1,500 x 1,024 pairs, with no request to wake the loop)
    backlog = post_events(svc, range(10, 1510), typ="nobody.wants")
    check("pool: ... and 1,500 more of a type nobody wants", len(backlog) == 1500)
    time.sleep(1.0)
    check("pool: ... and nothing is delivered while the endpoints are unknown", fan.count() == 0 and svc.stats()["endpoints_loaded"] is False)
    proxy.restore()
    check("pool: when the database is back the 1,024 are read (the table's answer is over 128 KiB)", wait_for(lambda: svc.has_line("endpoints loaded: 1024"), 60), svc.stderr()[-400:])
    t0 = time.time()
    ok = wait_for(lambda: svc.stats()["filtered"] >= 1500 * N, 60)
    took = time.time() - t0
    check("pool: ... and the 1,500 events nobody wants are passed over by every endpoint in %.1f s, well under the %.0f s of a budget a turn of the timer (50 ms) would allow" % (took, 1500 * N / 82000.0),
          ok and took < 12, f"{svc.stats()['filtered']} {took:.1f}")
    ids = sorted(got.values())
    check("pool: ... and the five events reach every endpoint once", wait_for(lambda: fan.reached(ids) == 5 * N, 120), str(fan.reached(ids)))
    time.sleep(0.5)
    check("pool: ... exactly once, signed with their own keys", all(v == 1 for v in fan.hits.values()) and not fan.bad)
    check("pool: /readyz is 200", svc.request("GET", "/readyz")[0] == 200)
    rows, pages, biggest, total = mk.listing(svc, limit=256)
    check("pool: GET /endpoints with limit=256 and long subscriptions: a page ends at 40 KiB of text (5 or more pages, none over 48 KiB, every endpoint once)",
          total == N and len(rows) == N and len({r["id"] for r in rows}) == N and pages >= 5 and biggest < 49152, str((total, len(rows), pages, biggest)))
    svc.stop()
    proxy.close()
    fan.close()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_formats():
    print("== formats: a log from before, a kind this build does not know, a table that does not fit", flush=True)
    # 1. a directory written the way main wrote it, 62 endpoints, no marker
    d = tmp()
    fan = Fan({})
    mk.reset_db()
    table = mk.insert_endpoints(62, fan.port)
    fan.add(table)
    args = ["--schedule", "100,100", "--deadline-ms", "2000"]
    svc = service(d, args)
    svc.start(timeout=30)
    got = post_events(svc, [1, 2])
    ids = sorted(got.values())
    wait_for(lambda: fan.reached(ids) == 2 * 62, 30)
    wait_for(lambda: all(r["cursor"] >= ids[-1] for r in cursors(svc)[0].values()), 30)
    svc.stop()
    recs = mk.delivery_records(d)
    check("formats: 62 endpoints write the log main wrote: the header (kind 15, 62, format 2), a `created` each, no marker", recs[0][:3] == (15, 62, 2) and not [r for r in recs if r[0] == 18] and len([r for r in recs if r[0] == 10]) == 62 and max(r[1] for r in recs if r[0] == 10) == 61, str(recs[:2]))
    size = os.path.getsize(os.path.join(d, "delivery.seg"))
    svc = service(d, args, port=svc.port)
    svc.start(timeout=30)
    time.sleep(1)
    svc.stop()
    check("formats: a restart reads it and writes nothing (62 endpoints, nothing to do)", os.path.getsize(os.path.join(d, "delivery.seg")) == size)
    # 2. a snapshot of 62 slots is a log main can read: header, `created`s, no marker (compact-now with the database: the endpoints are read from it first)
    p = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", "--retention-ms", "1", "--window-ms", "1", *opslib.pg_flags()], capture_output=True, text=True)
    recs = mk.delivery_records(d)
    check("formats: compact-now with a database (it reads the table first) snapshots it: exit 0, and the snapshot is header, `created` and cells, no marker", p.returncode == 0 and "compacted" in p.stderr and recs[0][:3] == (15, 62, 2) and not [r for r in recs if r[0] == 18], f"{p.returncode} {p.stderr[-300:]}")
    # the header's endpoint is the number 62, which is a slot now: a new endpoint (id 5000) takes slot 62, the lowest free, and nothing of the header is in it
    mk.insert_endpoints(1, fan.port, ids=[5000], host=addr(70))
    svc = service(d, args, port=svc.port)
    svc.start(timeout=30)
    svc.stop()
    created = [r for r in mk.delivery_records(d) if r[0] == 10]
    check("formats: with 62 endpoints in the log the 63rd (id 5000) takes slot 62, the lowest free (the header's 62 is not an owner), and as the first wide slot it brings the marker",
          created[-1][1:3] == (62, 5000) and len([r for r in mk.delivery_records(d) if r[0] == 18]) == 1, str(created[-1:]))
    # a snapshot made while the only wide slot is 62 still carries the marker (every slot from 62 is looked at, not only the last ones)
    p = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", "--retention-ms", "1", "--window-ms", "1", *opslib.pg_flags()], capture_output=True, text=True)
    recs = mk.delivery_records(d)
    check("formats: a snapshot made when the only endpoint in a wide slot is the one in slot 62 carries the marker once, right after the header",
          p.returncode == 0 and [r[0] for r in recs[:2]] == [15, 18] and len([r for r in recs if r[0] == 18]) == 1, p.stderr[-200:] + str([r[0] for r in recs[:3]]))
    # 3. a record of a kind this build does not know is a refusal with status 15 (what a build from before does with kind 18; 19 is `advanced`, section 42, 20 `erased`, section 47.3)
    append_records(d, [(21, 0, 0, 0, 0)])
    before = {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d) if n.endswith((".seg", ".first"))}
    svc = service(d, args, port=svc.port)
    svc.start(timeout=20, loaded=False)
    svc.wait_exit(30)
    code = svc.proc.poll()
    svc.kill()
    after = {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d) if n.endswith((".seg", ".first"))}
    check("formats: a record of a kind the build does not know ends the start with status 15 (the one a build from before gives kind 18) and changes nothing", code == 15 and before == after, f"{code}")
    # 4. a table of 1,025 rows, and a table too large to read
    fan.close()
    mk.reset_db()
    mk.insert_endpoints(1025, 9)
    d2 = tmp()
    svc = service(d2, args)
    svc.start(timeout=20, loaded=False)
    code = svc.wait_exit(30)
    check("formats: a table of 1,025 endpoints is status 13, naming row 1025", code == 13 and any("row 1025" in l for l in svc.lines), f"{code} {svc.stderr()[-200:]}")
    mk.reset_db()
    mk.insert_endpoints(N, 9, types=lambda k: ",".join(f"pattern{j:03d}.{'x' * 50}" for j in range(8)))
    d3 = tmp()
    svc = service(d3, args)
    svc.start(timeout=20, loaded=False)
    code = svc.wait_exit(60)
    check("formats: a table whose text is over 540,672 bytes is status 20 and says too large", code == 20 and svc.has_line("too large"), f"{code} {svc.stderr()[-200:]}")
    mk.reset_db()


# --------------------------------------------------------------------------------------------------------------------------------

def stage_compactnow():
    print("== compactnow: --compact-now with a database (design section 41.8)", flush=True)
    d = tmp()
    fan = Fan({})
    table = fresh(3, port=fan.port)
    fan.add(table)
    by_id = {v[0]: a for a, v in table.items()}
    knobs = ["--segment-bytes", "262144", "--retention-ms", "1", "--window-ms", "1"]
    svc = service(d, ["--schedule", "100,100", "--deadline-ms", "2000", "--segment-bytes", "262144", "--window-ms", "1"])
    svc.start(timeout=30)
    got = post_events(svc, range(1, 701), pad=280)
    last = max(got.values())
    check("compactnow: 700 events (more than two segments) reach the three endpoints", wait_for(lambda: all(r["cursor"] >= last for r in cursors(svc)[0].values()), 90), str(cursors(svc)[0]))
    svc.stop()

    def files():
        return {n: open(os.path.join(d, n), "rb").read() for n in sorted(os.listdir(d)) if n.endswith((".seg", ".first"))}

    def now(extra=(), port=None, timeout=90):
        p = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", *knobs, *opslib.pg_flags(port), *extra], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stderr
    # the database is not there: status 20 after the wait, nothing touched
    before = files()
    t = time.time()
    code, err = now(["--pg-start-wait-ms", "1500"], port=chaos.free_port())
    took = time.time() - t
    check("compactnow: the database cannot be reached: status 20 after about pg-start-wait-ms, it says it could not read the endpoints, and nothing changed", code == 20 and "cannot connect" in err and 1.2 < took < 15 and files() == before, f"{code} {err!r} {took:.1f}")
    # the endpoints are read: the pass is done and the segments go (all three are at the last event)
    code, err = now()
    check("compactnow: with the database there it reads the endpoints (3), does its pass and exits 0", code == 0 and "endpoints loaded: 3" in err and "compacted" in err, f"{code} {err!r}")
    code, err = now()
    first_after = chaos.read_events(d)[0]
    check("compactnow: ... and the events that are final at every endpoint go (a second run takes the segment the first sealed)", code == 0 and len(first_after) == 0, f"{code} {err!r} {len(first_after)}")
    # the table is read, and what a paused endpoint has not been sent is not dropped
    svc = service(d, ["--schedule", "100,100", "--deadline-ms", "2000"] + knobs[:2])
    svc.start(timeout=30)
    got2 = post_events(svc, range(1001, 1401), pad=280)
    wait_for(lambda: all(r["cursor"] >= max(got2.values()) for r in cursors(svc)[0].values()), 90)
    svc.stop()
    # a record of the breaker for slot 0's endpoint: it is paused and its cursor stays where it is
    append_records(d, [(13, 0, 0, 0, 0)])
    svc = service(d, ["--schedule", "100,100", "--deadline-ms", "2000"] + knobs[:2])
    svc.start(timeout=30)
    more = post_events(svc, range(2001, 2401), pad=280)
    wait_for(lambda: sum(1 for r in cursors(svc)[0].values() if r["cursor"] >= max(more.values())) == 2, 90)
    svc.stop()
    ev_before = len(chaos.read_events(d)[0])
    code, err = now()
    code2, err2 = now()
    ev_after = len(chaos.read_events(d)[0])
    first_id = chaos.read_events(d)[0][0][0] if ev_after else None
    check("compactnow: an endpoint that is paused and has not been sent the last 400 events pins them (and the 400 before it were final): compact-now drops no event it still owes", code == 0 and code2 == 0 and ev_after >= 400 and first_id is not None and first_id <= max(got2.values()) + 1, f"{code} {code2} {ev_before} {ev_after} {first_id}")
    fan.close()


def stage_oldbuild():
    """Only with HOOKS_BIN_MAIN=<the binary of the build before section 41> (CI does not build it): a log written by that build is read by this one without a byte
    changed; a log with a wide slot is refused by it with status 15; and a log that is back to 62 slots after a snapshot is read by it again."""
    old = os.environ.get("HOOKS_BIN_MAIN")
    if not old:
        print("== oldbuild: skipped (HOOKS_BIN_MAIN names the binary of the build before)", flush=True)
        return
    print("== oldbuild: the build before, on this build's logs and the reverse", flush=True)
    fan = Fan({})
    mk.reset_db()
    fan.add(mk.insert_endpoints(62, fan.port))
    d = tmp()
    args = ["--schedule", "100,100", "--deadline-ms", "2000"]
    svc = opslib.Service(old, d, [*opslib.pg_flags(), *args])
    svc.start(timeout=30)
    post_events(svc, [1, 2, 3], retry=False)
    wait_for(lambda: len(fan.hits) >= 62 * 3, 30)
    svc.stop()
    written = open(os.path.join(d, "delivery.seg"), "rb").read()
    svc = service(d, args, port=svc.port)
    svc.start(timeout=30)
    time.sleep(1)
    svc.stop()
    check("oldbuild: a directory the build before wrote is read by this one, and delivery.seg is not changed by a start and a stop", open(os.path.join(d, "delivery.seg"), "rb").read() == written)
    svc = service(d, ["--schedule", "100,100"], port=svc.port)
    svc.start(timeout=30)
    ids = []
    for k in range(3):
        status, out, _ = authed(svc, "POST", "/endpoints", {"host": addr(900 + k), "port": fan.port})
        ids.append(out["id"])
    svc.stop()
    check("oldbuild: with three endpoints in wide slots the log has the marker", [r[0] for r in mk.delivery_records(d)].count(18) == 1)
    opslib.psql("delete from endpoints where id in (%s)" % ",".join(map(str, ids)))
    o = opslib.Service(old, d, [*opslib.pg_flags(), *args])
    o.start(timeout=20, loaded=False)
    code = o.wait_exit(20)
    check("oldbuild: the build before refuses that log with status 15 (the table back at 62 rows)", code == 15, f"{code} {o.stderr()[-200:]}")
    for i in ids:
        opslib.psql("insert into endpoints (id, host, port, secret) values (%d, '%s', %d, '%s')" % (i, addr(900 + i % 7), fan.port, opslib.secret()))
    svc = service(d, args, port=svc.port)
    svc.start(timeout=30)
    for i in ids:
        authed(svc, "DELETE", f"/endpoints/{i}")
    svc.stop()
    p = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", "--retention-ms", "1", "--window-ms", "1", *opslib.pg_flags()], capture_output=True, text=True)
    check("oldbuild: the three deleted and a snapshot made (compact-now with the database), the marker is gone", p.returncode == 0 and [r[0] for r in mk.delivery_records(d)].count(18) == 0, p.stderr[-200:])
    o = opslib.Service(old, d, [*opslib.pg_flags(), *args])
    check("oldbuild: ... and the build before reads it again", o.start(timeout=30) and o.has_line("endpoints loaded: 62"), o.stderr()[-200:])
    o.stop()
    fan.close()


STAGE_FUNCS = {"limit": stage_limit, "chaos": stage_chaos, "flags": stage_flags, "replay": stage_replay, "quiet": stage_quiet, "retention": stage_retention, "compactnow": stage_compactnow, "pool": stage_pool, "formats": stage_formats, "oldbuild": stage_oldbuild}


def main():
    mk.fd_limit()
    try:
        for name, fn in STAGE_FUNCS.items():
            if not STAGES or name in STAGES:
                fn()
    finally:
        for d in DIRS:
            shutil.rmtree(d, ignore_errors=True)
    return check.finish("many-endpoints")


if __name__ == "__main__":
    sys.exit(main())
