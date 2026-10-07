#!/usr/bin/env python3
"""The checker's bookkeeping (ledger.py), each case built so that the old behaviour was wrong, and a twin that must still be flagged (docs/soak.md, "Validating the harness"):
expiry by the hard maximum age (only events older than the age), the floor of the purge (late replay deliveries, late deliveries below the floor, the numbers of posted events), the
acknowledgement time of an event at a `sick` endpoint (the service's lag), and what is forgotten.

    python3 scripts/soak/tests/test_checker.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import F_BODY, F_EFF, F_NEW, F_SIG, F_TS, TYPE_CODE  # noqa: E402
from ledger import Verifier, Violations  # noqa: E402

RESULTS = []
GOOD = F_SIG | F_EFF | F_TS | F_BODY | F_NEW
SEED = 5


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


def ver(**kw):
    v = Violations()
    return Verifier(SEED, violations=v, keep_s=5.0, **kw), v


def ack(ver_, ident, n, t, typ="user.created"):
    ver_.ingest_acks([(t, t - 0.01, n, ident, TYPE_CODE[typ], 1, 0)])


def rec(t, idx, ident, n, typ="user.created", flags=GOOD, status=204):
    return (t, idx, ident, n, 0, TYPE_CODE[typ], flags, status, 5)


def main():
    # ---- a schedule that stays silent after a database fault
    def cron_gap_case(kind, away, gap_a, gap_b):
        vr, v = ver()
        vr.cron_period["s"] = 1
        for sec in (gap_a - 2, gap_a - 1, gap_a, gap_b, gap_b + 1):
            vr.cron["s"][sec].add(sec)
        vr.note_away(away[0], away[1], kind)
        vr._cron_final()
        return v

    v = cron_gap_case("pg", (200.0, 240.0), 255, 261)       # the schedule is silent again from 15 s after the database came back
    check("a schedule that falls silent 15 s after a database fault ended is excused (the service documents up to 20 s)", v.count["I_cron_gap"] == 0, f"{dict(v.count)}")
    v = cron_gap_case("pg", (200.0, 240.0), 265, 271)       # from 25 s after
    check("... and one that falls silent 25 s after it is still I_cron_gap", v.count["I_cron_gap"] == 1, f"{dict(v.count)}")
    v = cron_gap_case("service", (200.0, 240.0), 255, 261)  # the same 15 s after a service that was away
    check("... and 15 s after a service that was away is still I_cron_gap (the service has no such delay)", v.count["I_cron_gap"] == 1, f"{dict(v.count)}")

    # ---- expiry
    def expiry_case(max_age, cursor_at, extra_young=False):
        """events 1..10 acknowledged at t=100..109, never delivered to `h`; its cursor is found past them at `cursor_at`."""
        vr, v = ver(max_age_s=max_age)
        vr.add_endpoint(0, "h", "healthy", [], c0=0, created=0.0)
        for i in range(1, 11):
            ack(vr, i, i, 100.0 + i)
        vr.cursor_sample("h", 0, 99.0, 1)
        vr.cursor_sample("h", 10, cursor_at, 1)
        vr.settle("h", 10, now=cursor_at)
        return vr, v

    vr, v = expiry_case(1200.0, 100.0 + 10 + 1210.0)
    check("losses of events older than the maximum age are excused as expired", v.total() == 0 and vr.expired_excused["h"] == 10, f"{dict(v.count)} {vr.expired_excused}")
    vr, v = expiry_case(1200.0, 100.0 + 10 + 600.0)
    check("the same losses at half the age are A_missing", v.count["A_missing"] == 10 and not vr.expired_excused, f"{dict(v.count)}")
    vr, v = expiry_case(None, 100.0 + 10 + 1210.0)
    check("and so are they in a run that has no maximum age", v.count["A_missing"] == 10, f"{dict(v.count)}")
    # a mixed cursor: with a maximum age of 20 s (less 2 s of slack) and the cursor found at 125, the events acknowledged at 101..107 are older than 18 s, those at 108..110 are not
    vr, v = expiry_case(20.0, 125.0)
    check("only the events that are older than the age are excused (7 of 10 here, the 3 younger are flagged)", vr.expired_excused["h"] == 7 and v.count["A_missing"] == 3, f"{vr.expired_excused} {dict(v.count)}")
    vr, v = expiry_case(1200.0, 100.0 + 10 + 1210.0)
    vr.finish(10, {"h": 10}, expired_by_service=10)
    check("the number excused is held against what the service says it expired: it agrees", v.count["A_expired_unexplained"] == 0, str(dict(v.count)))
    vr, v = expiry_case(1200.0, 100.0 + 10 + 1210.0)
    vr.finish(10, {"h": 10}, expired_by_service=0)
    vr.v.add("x") if False else None
    n_ex = sum(vr.expired_excused.values())
    big, vb = ver(max_age_s=1200.0)
    big.add_endpoint(0, "h", "healthy", [], c0=0, created=0.0)
    for i in range(1, 301):
        ack(big, i, i, 100.0 + i * 0.01)
    big.settle("h", 300, now=2000.0)
    big.finish(300, {"h": 300}, now=2000.0, expired_by_service=0)
    check("and when the service says it expired far fewer, it is A_expired_unexplained (300 excused, none expired)", vb.count["A_expired_unexplained"] == 1, f"{dict(vb.count)} {n_ex}")

    # ---- a late delivery of a replay, below the floor
    vr, v = ver()
    vr.add_endpoint(0, "oracle", "oracle", [], c0=0, created=0.0)
    vr.add_endpoint(1, "filter", "filter", ["user.*"], c0=0, created=0.0)
    ack(vr, 1, 1, 10.0, "order.paid")
    vr.ingest([rec(10.1, 0, 1, 1, "order.paid")])
    vr.note_replay("filter", 1)                 # a replay of an order to the endpoint that only wants users: the service sends it anyway
    for lb in ("oracle", "filter"):
        vr.cursor_sample(lb, 1, 11.0, 1)
        vr.settle(lb, 1, now=11.0)
    vr.purge(1000.0)                             # the floor is past event 1: it is forgotten
    vr.ingest([rec(1001.0, 1, 1, 1, "order.paid")])      # the replay is delivered late
    check("a replay delivered after the floor passed it is not C_filter (the replay is kept while its endpoint can still be sent to)", v.count["C_filter"] == 0, str(dict(v.count)))
    check("and a late delivery of an event below the floor is not a phantom", v.count["P_phantom"] == 0 and vr.stats["late_after_purge"] == 1, f"{dict(v.count)} {dict(vr.stats)}")
    vr.purge(1010.0)
    check("(nor when the next purge comes)", v.count["P_phantom"] == 0, str(dict(v.count)))
    vr2, v2 = ver()
    vr2.add_endpoint(0, "oracle", "oracle", [], c0=0, created=0.0)
    vr2.add_endpoint(1, "filter", "filter", ["user.*"], c0=0, created=0.0)
    ack(vr2, 1, 1, 10.0, "order.paid")
    vr2.ingest([rec(10.1, 0, 1, 1, "order.paid")])
    for lb in ("oracle", "filter"):
        vr2.cursor_sample(lb, 1, 11.0, 1)
        vr2.settle(lb, 1, now=11.0)
    vr2.purge(1000.0)
    vr2.ingest([rec(1001.0, 1, 1, 1, "order.paid")])
    check("but the same delivery that nobody asked for is still C_filter", v2.count["C_filter"] == 1, str(dict(v2.count)))
    # a retired endpoint keeps its replays only for the time a delivery can still come
    vr.retire("filter", 1001.0)
    vr.purge(1001.0 + Verifier.RETIRED_GRACE_S + 5)
    check("a retired endpoint's replays go when no delivery can come to it", not [k for k in vr.repl if k[0] == 1], str(vr.repl))

    # ---- the numbers of posted events are not trimmed from under the window
    vr, v = ver()
    vr.add_endpoint(0, "h", "healthy", [], c0=0, created=0.0)
    N = 650_000
    vr.ingest_acks([(1.0 + i * 1e-6, 1.0, i, i, TYPE_CODE["ping"], 1, 0) for i in range(1, N)])
    vr.purge(1e6, min_settled=0)
    check("the number of the oldest unpurged event is still known after 650,000 posted (it was trimmed at 600,000)", 1 in vr.posted_n and (N - 1) in vr.posted_n, f"{len(vr.posted_n)}")
    vr.ingest([rec(2.0, 0, 5, 5, "ping")])
    vr.purge(1e6, min_settled=10)
    check("and purging an event takes its number with it", 5 not in vr.posted_n and v.count["P_phantom"] == 0, f"{dict(v.count)}")

    # ---- an event at a sick endpoint: judged by when its first attempt could begin
    def sick_case(lag_events, ack_t=100.0):
        vr, v = ver()
        vr.add_endpoint(0, "sick", "sick", ["ping"], c0=0, created=0.0)
        vr.note_window("sick", 160.0, 250.0)          # stored as (159, 250)
        vr.note_lag("sick", lag_events, 40.0)
        ack(vr, 1, 1, ack_t, "ping")
        vr.cursor_sample("sick", 0, 99.0, 1)
        vr.cursor_sample("sick", 1, 300.0, 1)
        vr.settle("sick", 1, now=300.0)
        return vr, v

    vr, v = sick_case(lag_events=2400)             # the service is 60 s behind: the first attempt was about at 160, in the window
    check("an event acknowledged before a window, whose first attempt the service's lag put inside it, is deferred, not A_missing", v.count["A_missing"] == 0 and 1 in vr.by_label["sick"].deferred, f"{dict(v.count)}")
    vr.finish(1, {"sick": 1})
    check("(deferred is not forgiven: if it is never delivered it is A_missing_final)", v.count["A_missing_final"] == 1, str(dict(v.count)))
    vr, v = sick_case(lag_events=0)
    check("with no lag the same event is A_missing (acknowledged 60 s before the window)", v.count["A_missing"] == 1, str(dict(v.count)))
    vr, v = sick_case(lag_events=2400, ack_t=20.0)
    check("and one acknowledged far before the window is A_missing whatever the lag (the margin is capped at the lag, 60 s)", v.count["A_missing"] == 1, str(dict(v.count)))

    # ---- memory: what is forgotten
    vr, v = ver()
    vr.add_endpoint(0, "churn1-1", "churn", [], c0=0, created=0.0)
    vr.add_endpoint(1, "healthy", "healthy", [], c0=0, created=0.0)
    vr.retire("churn1-1", 100.0)
    notes = vr.export_notes(now=100.0 + 700.0)
    check("a retired endpoint is not in the notes after ten minutes", "churn1-1" not in notes["eps"] and "healthy" in notes["eps"], str(list(notes["eps"])))
    vr.purge(100.0 + Verifier.RETIRED_FORGET_S + 10)
    check("it is forgotten from the checker's memory after an hour", 0 not in vr.by_idx and "churn1-1" not in vr.by_label and 1 in vr.by_idx)
    vr.ingest([rec(100.0 + Verifier.RETIRED_FORGET_S + 11, 0, 3, 3)])
    check("and a delivery to it then is a finding (a zombie), not silence", v.count["U_unknown_ep"] == 1, str(dict(v.count)))

    print("checker: all checks passed" if all(RESULTS) else "checker: FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
