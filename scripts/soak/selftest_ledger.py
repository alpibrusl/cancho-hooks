#!/usr/bin/env python3
"""The checker, tested against itself (docs/soak.md, "Validating the checker"): a synthetic run of a *correct* service is built (acknowledged events, deliveries, kills with
the repeats they excuse, cursors), the checker must pass it; then each mutation (a delivery taken away, one added, a signature broken, a cursor moved back, ...) is
applied to a copy and the checker must report it under the tag that names it.

    python3 scripts/soak/selftest_ledger.py            # exit 0 if the clean run passes and every mutant is caught

No service, no network: a few seconds. `soak.py --selftest-ledger` runs this.
"""
import copy
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import series  # noqa: E402
from common import (CLASSES, F_BODY, F_EFF, F_NEW, F_RISK, F_SIG, F_TS, F_TWO, TYPE_CODE, TYPES, UNKNOWN_TYPE, poison)  # noqa: E402
from ledger import Verifier, Violations  # noqa: E402

SEED = 7
T0 = 1_000_000.0
GOOD = F_SIG | F_EFF | F_TS | F_BODY | F_NEW
EPS = [(0, "oracle", "oracle", []), (1, "filter", "filter", ["user.*"]), (2, "gone", "gone", ["invoice.*", "ping"]), (3, "rare", "healthy2", ["rare.event"]),
       (4, "sick", "sick", ["ping", "order.paid"]), (5, "slowpoke", "slow", ["order.*"])]


def build(n_events=1500, seed=3):
    """The plan of a correct service: events (id, t, type, n, src), per endpoint the records it makes and the time each event is final for it."""
    rng = random.Random(seed)
    types, weights = [t for t, _ in TYPES], [w for _, w in TYPES]
    events, n, t = [], 0, T0
    next_cron = T0 + 1
    while len(events) < n_events:
        t += rng.uniform(0.01, 0.04)
        while next_cron <= t:
            events.append((next_cron + 0.05, "cron.tick", int(next_cron), 1))
            next_cron += 1
        n += 1
        events.append((t, rng.choices(types, weights)[0], n, 0))
    events.sort(key=lambda e: e[0])
    evs = [(i + 1, *e) for i, e in enumerate(events)]
    kills = [T0 + 4.0, T0 + 9.0]
    recs, final = [], {ep[1]: {} for ep in EPS}          # label -> id -> time final
    acks = []
    for (ident, t, typ, n, src) in evs:
        if src == 0:
            acks.append((t + 0.004, t, n, ident, TYPE_CODE[typ], 1, 0))
        for (idx, label, cls, types_) in EPS:
            from common import wanted
            if not wanted(types_, typ):
                final[label][ident] = t
                continue
            if cls == "gone" and poison(SEED, label, ident, 0.05):
                recs.append((t + 0.02, idx, ident, n, src, TYPE_CODE[typ], F_SIG | F_TS | F_BODY | F_NEW, 410, 5))
                final[label][ident] = t + 0.02
                continue
            if cls == "sick" and 6.0 <= t - T0 <= 7.0:
                # fails for a second, then is replayed at the end (dead letter, then replay-dead)
                recs.append((t + 0.02, idx, ident, n, src, TYPE_CODE[typ], F_SIG | F_TS | F_BODY | F_NEW, 503, 5))
                final[label][ident] = t + 0.5
                recs.append((T0 + 40.0 + rng.random(), idx, ident, n, src, TYPE_CODE[typ], GOOD, 204, 5))
                continue
            td = t + 0.01 + rng.random() * 0.2
            recs.append((td, idx, ident, n, src, TYPE_CODE[typ], GOOD, 204, int((td - t) * 1000)))
            final[label][ident] = td + 0.001
            for k in kills:       # a kill within 2 s after the delivery: its outcome may be lost, the delivery is repeated after the restart
                if td < k <= td + 1.5 and rng.random() < 0.5:
                    recs.append((k + 0.3 + rng.random() * 0.1, idx, ident, n, src, TYPE_CODE[typ], GOOD, 204, 0))
    recs.sort(key=lambda r: r[0])
    return {"evs": evs, "recs": recs, "acks": acks, "final": final, "kills": kills, "last_id": evs[-1][0], "end": T0 + 60.0}


def cursors_at(plan, label, now):
    c = 0
    fin = plan["final"][label]
    for ident, *_ in plan["evs"]:
        if fin.get(ident, 1e18) <= now:
            c = ident
        else:
            break
    return c


def run(plan, recs=None, acks=None, kills=None, cursor_hook=None, dead_hook=None, rotations=(), away=(), final_cursors=None):
    """Feed the checker as the harness would: each second read the cursors, then drain the ledgers, then settle. Returns the Violations."""
    v = Violations()
    ver = Verifier(SEED, violations=v, keep_s=5.0)
    for (idx, label, cls, types_) in EPS:
        ver.add_endpoint(idx, label, cls, types_, params=dict(CLASSES[cls][1], **({"poison": 0.05} if cls == "gone" else {})), created=T0 - 1000)
    ver.cron_period[1] = 1
    for k in (plan["kills"] if kills is None else kills):
        ver.note_kill(k)
    for (label, a, b) in rotations:
        ver.note_rotation(label, a, b)
    for (a, b, kind) in away:
        ver.note_away(a, b, kind)
    recs = plan["recs"] if recs is None else recs
    acks = plan["acks"] if acks is None else acks
    ia = ir = 0
    now = T0
    inc = 1
    kills_seen = list(plan["kills"])
    while now < plan["end"]:
        now += 0.5
        for k in list(kills_seen):
            if k <= now:
                kills_seen.remove(k)
                inc += 1
                ver.note_restart(inc, k, "kill")
        cur = {}
        for (idx, label, cls, types_) in EPS:
            cur[label] = cursors_at(plan, label, now)
            if cursor_hook:
                cur[label] = cursor_hook(label, now, cur[label])
        j = ia
        while j < len(acks) and acks[j][0] <= now:
            j += 1
        ver.ingest_acks(acks[ia:j])
        ia = j
        j = ir
        while j < len(recs) and recs[j][0] <= now:
            j += 1
        ver.ingest(recs[ir:j])
        ir = j
        for label, c in cur.items():
            ver.cursor_sample(label, c, now, inc, last_id=plan["last_id"])
            ver.settle(label, c)
            if dead_hook:
                dead_hook(ver, label, now)
        ver.purge(now)
    ver.ingest(recs[ir:])
    ver.ingest_acks(acks[ia:])
    fc = {label: cursors_at(plan, label, plan["end"] + 100) for (_i, label, _c, _t) in EPS} if final_cursors is None else final_cursors
    ver.finish(plan["last_id"], fc)
    return v, ver


def mutants(plan):
    """name -> (expected tag, kwargs for `run`)"""
    out = {}
    recs = plan["recs"]
    eff = [i for i, r in enumerate(recs) if r[6] & F_EFF and r[1] == 1 and 200 < r[2] < 1200]       # the `filter` endpoint's deliveries
    gone_eff = [i for i, r in enumerate(recs) if r[1] == 2 and r[6] & F_EFF][3]
    oracle_eff = [i for i, r in enumerate(recs) if r[1] == 0 and r[6] & F_EFF]

    def without(i):
        return recs[:i] + recs[i + 1:]

    out["a delivery is lost"] = ("A_missing", dict(recs=without(eff[40])))
    out["the last delivery of an endpoint is lost"] = ("A_missing", dict(recs=without(max(i for i, r in enumerate(recs) if r[1] == 1 and r[6] & F_EFF))))
    out["a non-poison event is not delivered at `gone`"] = ("A_missing", dict(recs=without(gone_eff)))

    def dup(i, dt):
        r = recs[i]
        return sorted(recs + [(r[0] + dt, *r[1:])], key=lambda x: x[0])

    # a repeat nothing explains: far from any kill (the kills are at +4 and +9 s: 5 s after the first, 2 before the second, so 1.2 s after a delivery at +5.5 is safe)
    quiet = [i for i in oracle_eff if T0 + 5.5 < recs[i][0] < T0 + 7.0][3]
    out["a repeat outside any kill"] = ("B_repeat", dict(recs=dup(quiet, 0.4)))
    out["a repeat in the same instant (the receiver wrote it twice)"] = ("B_repeat", dict(recs=dup(quiet, 0.0)))
    near = [i for i in oracle_eff if T0 + 8.0 < recs[i][0] < T0 + 8.9][2]
    out["a repeat after a kill is excused"] = (None, dict(recs=dup(near, 1.2)))
    out["but not when the kill was more than 2 s after the first delivery"] = ("B_repeat", dict(recs=dup(oracle_eff[5], 0.5), kills=[T0 + 0.2 + recs[oracle_eff[5]][0] - T0 + 3.0]))

    early = [i for i in oracle_eff if T0 + 1.0 < recs[i][0] < T0 + 1.8][0]
    out["a delivery made again after a restart, long after the kill's window"] = ("B_restart_repeat", dict(recs=dup(early, T0 + 5.0 - recs[early][0])))

    def retype(i, typ):
        r = list(recs[i])
        r[5] = TYPE_CODE[typ]
        return recs[:i] + [tuple(r)] + recs[i + 1:]

    to_filter = [i for i, r in enumerate(recs) if r[1] == 0 and r[5] == TYPE_CODE["order.paid"]][0]
    stray = (T0 + 3.3, 1, plan["evs"][to_filter][0], plan["evs"][to_filter][3], 0, TYPE_CODE["order.paid"], GOOD, 204, 3)
    out["a delivery to an endpoint whose list excludes the type"] = ("C_filter", dict(recs=sorted(recs + [stray], key=lambda x: x[0])))
    old = (T0 + 3.3, 1, 0, 1, 0, TYPE_CODE["user.created"], GOOD, 204, 3)
    out["a delivery of an event older than the endpoint"] = ("C_old_event", dict(recs=sorted(recs + [old], key=lambda x: x[0]),))

    def flip(i, flags):
        r = list(recs[i])
        r[6] = flags
        return recs[:i] + [tuple(r)] + recs[i + 1:]

    out["a bad signature"] = ("D_signature", dict(recs=flip(eff[10], GOOD & ~F_SIG)))
    out["a stale timestamp"] = ("D_timestamp", dict(recs=flip(eff[11], GOOD & ~F_TS)))
    out["a corrupt body"] = ("D_body", dict(recs=flip(eff[12], GOOD & ~F_BODY)))
    ov = [i for i in eff if recs[i][0] > T0 + 14][0]

    def two_sigs(skip=None):
        """every delivery of the `filter` endpoint carries two signatures, except record `skip`"""
        return [(*r[:6], r[6] | F_TWO, *r[7:]) if (r[1] == 1 and r[6] & F_SIG and i != skip) else r for i, r in enumerate(recs)]

    out["one signature while a rotation overlaps"] = ("D_overlap", dict(recs=two_sigs(skip=ov), rotations=[("filter", T0 + 10, T0 + 25)]))
    out["two signatures while a rotation overlaps is fine"] = (None, dict(recs=two_sigs(), rotations=[("filter", T0 + 10, T0 + 25)]))
    out["a repeat after a slow answer is excused"] = (None, dict(recs=sorted(flip(quiet, GOOD | F_RISK) + [(recs[quiet][0] + 0.4, *recs[quiet][1:])], key=lambda x: x[0])))

    def back(label, now, c):
        return c - 40 if label == "oracle" and 12 < now - T0 < 12.6 and c > 100 else c

    out["a cursor goes backwards"] = ("E_cursor_back", dict(cursor_hook=back))

    def ahead(label, now, c):
        return plan["last_id"] + 5 if label == "oracle" and now - T0 > 20 else c

    out["a cursor beyond the newest event"] = ("E_cursor_ahead", dict(cursor_hook=ahead))

    def dead(ver, label, now):
        if label == "filter" and now - T0 > 15:
            ver.dead_letters(label, 3, now)

    out["a dead letter where none may be"] = ("F_dead_letter", dict(dead_hook=dead))
    poison_id = [r for r in recs if r[1] == 2 and r[7] == 410][1]
    forged = (poison_id[0] + 0.3, 2, poison_id[2], poison_id[3], 0, poison_id[5], GOOD, 204, 3)
    out["a poison event delivered at `gone`"] = ("F_never", dict(recs=sorted(recs + [forged], key=lambda x: x[0])))
    cr = [i for i, r in enumerate(recs) if r[1] == 0 and r[4] == 1]
    out["a scheduled second made twice"] = ("I_cron_twice", dict(recs=sorted(recs + [(recs[cr[3]][0] + 0.2, 0, 9_999_999, recs[cr[3]][3], 1, recs[cr[3]][5], GOOD, 204, 0)], key=lambda x: x[0])))
    cut = [i for i, r in enumerate(recs) if r[4] == 1 and T0 + 14 <= r[0] <= T0 + 18]
    without_cron = [r for i, r in enumerate(recs) if i not in set(cut)]
    # those events are lost too (A_missing) but the gap is what this mutant is about
    out["a gap in a schedule that nothing explains"] = ("I_cron_gap", dict(recs=without_cron))
    out["a gap in a schedule while the service was away is excused"] = ("!I_cron_gap", dict(recs=without_cron, away=[(T0 + 13, T0 + 19, "kill")]))
    ghost = (T0 + 3.3, 0, 8_888_888, 424242, 0, TYPE_CODE["user.created"], GOOD, 204, 3)
    out["a delivery of an event nobody posted"] = ("P_phantom", dict(recs=sorted(recs + [ghost], key=lambda x: x[0])))
    out["an acknowledged event the service never delivered anywhere"] = ("A_unseen_event", dict(recs=[r for r in recs if r[2] != plan["evs"][400][0]], acks=[a for a in plan["acks"] if a[3] != plan["evs"][400][0]]))
    sick_final = [i for i, r in enumerate(recs) if r[1] == 4 and r[0] > T0 + 39.5]
    out["a dead letter of the sick endpoint that is never replayed"] = ("A_missing_final", dict(recs=without(sick_final[0])))
    out["the end: an endpoint that has not caught up"] = ("END_lag", dict(final_cursors={"oracle": plan["last_id"] - 3, "filter": plan["last_id"], "gone": plan["last_id"], "dead": plan["last_id"], "sick": plan["last_id"],
                                                                                  "slowpoke": plan["last_id"]}))
    wrong_id = list(plan["acks"])
    a = list(wrong_id[100])
    a[3] = wrong_id[101][3]
    wrong_id[100] = tuple(a)
    out["two events with one id"] = ("P_id_mismatch", dict(acks=wrong_id))
    return out


def series_rows(kind, n=240, step=10.0):
    rows = []
    for i in range(n):
        t = 2_000_000 + i * step
        rss = 12000 + (i % 7) * 3
        fds = 40 + (i % 5)
        if kind == "leak":
            rss += i * 40
        if kind == "step":
            rss += 9000 if i > n // 2 else 0
        if kind == "fdleak":
            fds += i // 6
        rows.append({"t": t, "inc": 1, "up": 1, "rss_kb": rss, "hwm_kb": rss + 100, "threads": 1 if kind != "threads" or i < 100 else 2, "fds": fds, "data_bytes": 5_000_000 + (i % 9) * 1000,
                     "ingested_bytes": i * step * 90_000, "maint_ms_max": 20 if kind != "maint" else 400})
    if kind == "disk":
        for i, r in enumerate(rows):
            r["data_bytes"] = 5_000_000 + i * 2_000_000
    return rows


def series_tests():
    p = {"min_tail_s": 600, "min_inc_s": 600, "retention_s": 180, "window_s": 120, "segment_bytes": 1 << 20, "delivery_log_bytes": 1 << 18, "stall_ms": 500, "p999_ms": 50}
    results = []

    def failed(rows, ident, probe=None, excused=()):
        res = series.check_all(rows, probe or [], list(excused), p)
        return [r for r in res if r["id"].startswith(ident) and r["ok"] is False], res

    results.append(("a flat series passes every check", not [r for r in series.check_all(series_rows("flat"), [], [], p) if r["ok"] is False]))
    results.append(("a leak of 40 KB a sample is caught (G1)", bool(failed(series_rows("leak"), "G1")[0])))
    results.append(("a step up in memory is caught (G1)", bool(failed(series_rows("step"), "G1")[0])))
    results.append(("descriptors that grow are caught (G3)", bool(failed(series_rows("fdleak"), "G3")[0])))
    results.append(("a second thread is caught (G3)", bool(failed(series_rows("threads"), "G3")[0])))
    results.append(("a disk that grows without bound is caught (G4)", bool(failed(series_rows("disk"), "G4")[0])))
    results.append(("a long step of the loop is caught (H1b)", bool(failed(series_rows("maint"), "H1b")[0])))
    ok_probe = [{"t": 2_000_000 + i, "n": 10, "max": 3.0, "ctl": 0.2, "err": 0, "l": [1.0, 2.0, 3.0]} for i in range(100)]
    stall = copy.deepcopy(ok_probe)
    stall[50]["max"] = 900.0
    results.append(("a clean probe passes (H1, H2)", not [r for r in series.check_probe(ok_probe, [], p) if r["ok"] is False]))
    results.append(("a 900 ms window is a stall (H1)", bool(failed(series_rows("flat"), "H1", probe=stall)[0])))
    results.append(("the same window is excused during a kill", not failed(series_rows("flat"), "H1", probe=stall, excused=[(2_000_049, 2_000_052)])[0]))
    shared = copy.deepcopy(stall)
    shared[50]["ctl"] = 700.0
    results.append(("a window the host was late in too is not the service's", not failed(series_rows("flat"), "H1", probe=shared)[0]))
    errs = copy.deepcopy(ok_probe)
    errs[20]["err"] = 3
    results.append(("failed probes outside an excused window are a stall", bool(failed(series_rows("flat"), "H1", probe=errs)[0])))
    slow_tail = [{"t": 2_000_000 + i, "n": 10, "max": 90.0, "ctl": 0.2, "err": 0, "l": [1.0] * 8 + [90.0, 80.0]} for i in range(100)]
    results.append(("a slow 99.9th percentile is caught (H2)", bool(failed(series_rows("flat"), "H2", probe=slow_tail)[0])))
    return results


def main():
    ok = True
    plan = build()
    v, ver = run(plan)
    clean = v.total() == 0
    print(("ok   " if clean else "FAIL ") + f"the unmutated run passes ({len(plan['evs'])} events, {len(plan['recs'])} deliveries, {ver.stats['repeats']} repeats, "
          f"{ver.explained['kill']} of them explained by kills)" + ("" if clean else f"  violations: {dict(v.count)} {dict(v.examples)}"))
    ok &= clean
    ok &= ver.explained["kill"] > 0
    caught = 0
    ms = mutants(plan)
    for name, (tag, kw) in ms.items():
        v, _ = run(plan, **kw)
        if tag is None:
            good = v.total() == 0
            print(("ok   " if good else "FAIL ") + f"{name}: no violation" + ("" if good else f"  but {dict(v.count)}"))
        elif tag.startswith("!"):
            good = v.count[tag[1:]] == 0
            print(("ok   " if good else "FAIL ") + f"{name}: not {tag[1:]}" + ("" if good else f"  but {dict(v.count)}"))
        else:
            good = v.count[tag] > 0
            caught += good
            print(("ok   " if good else "FAIL ") + f"{name}: {tag}" + ("" if good else f"  not caught; got {dict(v.count)}"))
        ok &= good
    for name, good in series_tests():
        print(("ok   " if good else "FAIL ") + name)
        ok &= good
    print(f"{caught} of {sum(1 for t, _ in ms.values() if t and not t.startswith('!'))} mutants of the ledgers caught")
    print("all checks passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
