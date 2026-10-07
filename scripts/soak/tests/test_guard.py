#!/usr/bin/env python3
"""The validity guard against synthetic samples (docs/soak.md, "When a run is valid"): a healthy host is never flagged; each way the harness or the host can become the limit is flagged
within the window, and a short spike is not. No service.

    python3 scripts/soak/tests/test_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import guard  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


def good(t, **kw):
    s = {"t": t, "recv_cpu_max_pct": 30.0, "recv_lag_ms": 20.0, "harness_cpu_pct": 25.0, "mem_avail_mb": 20000.0, "swap_io_s": 0.0, "psi_mem_full60": 0.0, "ingest_per_s": 40.0, "steady": True,
         "bind_stuck": [], "late_unplanned": 0, "replays_waiting": 3, "data_bytes": 6e6, "ingested_bytes": t * 90000.0}
    s.update(kw)
    return s


def run(fn, n=180, step=10.0, **kw):
    """Feed n samples; fn(i) gives the overrides of sample i. Returns (guard, index of the sample at which each code was first raised, violations)."""
    g = guard.Guard(**kw)
    first, viols = {}, []
    for i in range(n):
        inv, viol = g.feed(good(1000.0 + i * step, **fn(i)))
        for c, _ in inv:
            first.setdefault(c, i)
        viols += viol
    return g, first, viols


def main():
    g, first, v = run(lambda i: {})
    check("a healthy run is never flagged (3 hours of samples)", not g.invalid and not v, str(g.invalid))

    g, first, v = run(lambda i: {"recv_cpu_max_pct": 96.0} if i >= 20 else {})
    check("receivers over 85 % of a core are flagged within the window", "receivers_cpu" in first and first["receivers_cpu"] <= 20 + 60, str(first))
    g, first, v = run(lambda i: {"recv_cpu_max_pct": 96.0} if 20 <= i < 22 else {})
    check("a spike of two samples (under 5 % of the window) is not", "receivers_cpu" not in first, str(first))
    g, first, v = run(lambda i: {"recv_lag_ms": 900.0} if i % 8 == 0 and i > 20 else {})
    check("a loop that is late by 900 ms in one sample of eight is flagged", "receivers_lag" in first, str(first))
    g, first, v = run(lambda i: {"harness_cpu_pct": 75.0} if i >= 30 else {})
    check("the harness's own processes over 60 % of a core are flagged", "harness_cpu" in first, str(first))
    g, first, v = run(lambda i: {"mem_avail_mb": 400.0} if i >= 50 else {})
    check("memory under 1 GB is flagged at the second sample", first.get("memory") == 51, str(first))
    g, first, v = run(lambda i: {"mem_avail_mb": 400.0} if i == 50 else {})
    check("one low sample is not", "memory" not in first, str(first))
    g, first, v = run(lambda i: {"swap_io_s": 800.0} if i >= 40 else {})
    check("a host that is swapping is flagged", "swapping" in first, str(first))
    g, first, v = run(lambda i: {"psi_mem_full60": 12.0} if i >= 40 else {})
    check("memory pressure is flagged", "memory_pressure" in first, str(first))
    g, first, v = run(lambda i: {"ingest_per_s": 25.0} if i >= 30 else {})
    check("a poster that holds 60 % of the rate is flagged", "poster_rate" in first, str(first))
    g, first, v = run(lambda i: {"ingest_per_s": 5.0, "steady": False} if i >= 30 else {})
    check("a rate that is low while the service is away or bursting is not", "poster_rate" not in first, str(first))
    g, first, v = run(lambda i: {"bind_stuck": ["churn0-3"]} if i == 60 else {})
    check("a receiver that cannot bind its port is flagged at once", first.get("bind_stuck") == 60, str(first))
    g, first, v = run(lambda i: {"late_unplanned": i * 2})
    check("answers that were due at once and went out late are flagged", "receivers_late" in first, str(first))

    g, first, v = run(lambda i: {"replays_waiting": 32} if i >= 20 else {})
    check("a table of waiting replays that stays full is a violation after 180 s", [c for c, _ in v] == ["G_replays_pinned"], str(v))
    g, first, v = run(lambda i: {"replays_waiting": 32} if 20 <= i < 26 else {})
    check("a full table for a minute is not", not v, str(v))
    g, first, v = run(lambda i: {"data_bytes": 6e6 + max(0, i - 60) * 4e6})
    check("a data directory that grows past what retention allows is a violation", [c for c, _ in v] == ["G_disk_over_bound"], str(v))
    g, first, v = run(lambda i: {"data_bytes": 60e6 if i == 80 else 6e6})
    check("a single sample over the bound is not", not v, str(v))

    # the start
    ok = guard.check_start({"MemAvailable": 27000.0, "SwapTotal": 2047.0, "SwapFree": 1.0})
    check("swap full with plenty of memory may start (nothing is paged out)", ok == [], str(ok))
    check("swap full and memory tight does not", len(guard.check_start({"MemAvailable": 3000.0, "SwapTotal": 2047.0, "SwapFree": 1.0})) == 1)
    check("little memory does not", len(guard.check_start({"MemAvailable": 900.0, "SwapTotal": 0.0, "SwapFree": 0.0})) == 1)
    check("the override starts it", guard.check_start({"MemAvailable": 900.0, "SwapTotal": 0.0, "SwapFree": 0.0}, allow_low=True) == [])

    # who else was using the cores when a stall was seen
    hz = os.sysconf("SC_CLK_TCK")
    before = {1: (0, "hooks", 15), 2: (0, "firefox", 3), 3: (0, "idle", 4), 4: (10, "other", 5)}
    after = {1: (int(hz * 4), "hooks", 15), 2: (int(hz * 8), "firefox", 3), 3: (0, "idle", 4), 4: (10 + int(hz * 0.5), "other", 5), 5: (int(hz * 6), "new", 9)}
    top = guard.top_processes(before, after, 10.0, 3, mine={1})
    check("the three processes that used most CPU in the interval are named, with whose they are", [t["comm"] for t in top] == ["firefox", "new", "hooks"] and top[0]["cpu_pct"] == 80.0 and top[2]["mine"], str(top))

    print("guard: all checks passed" if all(RESULTS) else "guard: FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
