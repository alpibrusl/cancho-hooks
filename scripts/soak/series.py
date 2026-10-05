"""The criteria of docs/soak.md section 4 that are judged on the *series* the watcher writes (memory, descriptors, threads, disk, the loop probe), as plain functions of
the rows, so that they can be tested on synthetic series (selftest_ledger.py) and run again on the files of a real run (report.py).

Each check returns a dict `{"id", "ok", "what", ...numbers}`; `ok` is True, False, or None for "not evaluated" (a run too short for the criterion), with the reason.
"""
import bisect
import math

MIB_KB = 1024.0


def median(v):
    v = sorted(v)
    if not v:
        return 0.0
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def linfit(xs, ys):
    """Least squares: (slope, intercept). A series of fewer than two points, or of one instant, has no slope."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return b, my - b * mx


def percentile(sorted_v, p):
    if not sorted_v:
        return 0.0
    return sorted_v[min(len(sorted_v) - 1, int(len(sorted_v) * p))]


def incarnations(rows):
    out = {}
    for r in rows:
        if r.get("up") in (1, "1", True) and r.get("rss_kb") not in (None, "") and str(r.get("phase", "0")) != "2":      # phase 2: after the clean stop, the service started once more to be watched
            out.setdefault(int(r["inc"]), []).append(r)
    return out


def _col(rows, key):
    return [float(r[key]) for r in rows]


def _thirds(rows):
    t0, t1 = float(rows[0]["t"]), float(rows[-1]["t"])
    cut = t0 + (t1 - t0) / 3.0
    first = [r for r in rows if float(r["t"]) <= cut]
    late = [r for r in rows if float(r["t"]) >= cut]
    return t0, t1, first, late


def check_rss(rows, ident, p):
    """Growth of resident memory on one incarnation: the fitted slope over the last two thirds, as growth over that window, against max(5 % of the median of the
    first third, 4 MiB); and the median of the last tenth against 110 % of the first third's plus 8 MiB. `p`: min_s (an incarnation shorter than this is not judged)."""
    if len(rows) < 6:
        return {"id": ident, "ok": None, "what": "resident memory", "why": f"only {len(rows)} samples"}
    t0, t1, first, late = _thirds(rows)
    if t1 - t0 < p["min_s"]:
        return {"id": ident, "ok": None, "what": "resident memory", "why": f"the incarnation lived {t1 - t0:.0f} s, less than the {p['min_s']:.0f} s the criterion needs"}
    base = median(_col(first, "rss_kb"))
    xs = [float(r["t"]) for r in late]
    ys = _col(late, "rss_kb")
    slope, _ = linfit(xs, ys)
    growth = slope * (xs[-1] - xs[0])
    limit = max(0.05 * base, 4 * MIB_KB)
    tail = rows[int(len(rows) * 0.9):] or rows[-1:]
    end = median(_col(tail, "rss_kb"))
    end_limit = 1.10 * base + 8 * MIB_KB
    hwm = max(_col(rows, "hwm_kb")) if "hwm_kb" in rows[0] else None
    return {"id": ident, "ok": growth <= limit and end <= end_limit, "what": "resident memory", "inc": int(rows[0]["inc"]), "span_s": round(t1 - t0), "base_kb": round(base),
            "end_kb": round(end), "slope_kb_per_h": round(slope * 3600, 1), "growth_kb": round(growth), "growth_limit_kb": round(limit), "end_limit_kb": round(end_limit),
            "hwm_kb": hwm}


def check_fds(rows, ident, p):
    if len(rows) < 6:
        return {"id": ident, "ok": None, "what": "descriptors and threads", "why": f"only {len(rows)} samples"}
    t0, t1, first, late = _thirds(rows)
    if t1 - t0 < p["min_s"]:
        return {"id": ident, "ok": None, "what": "descriptors and threads", "why": f"the incarnation lived {t1 - t0:.0f} s, less than {p['min_s']:.0f} s"}
    early_max = max(_col(first, "fds"))
    late_max = max(_col(late, "fds"))
    xs = [float(r["t"]) for r in late]
    slope, _ = linfit(xs, _col(late, "fds"))
    growth = slope * (xs[-1] - xs[0])
    threads = sorted({int(float(r["threads"])) for r in rows})
    ok = late_max <= early_max + 16 and growth <= 8 and len(threads) == 1
    return {"id": ident, "ok": ok, "what": "descriptors and threads", "inc": int(rows[0]["inc"]), "fds_early_max": early_max, "fds_late_max": late_max, "growth": round(growth, 2),
            "thread_counts": threads}


def check_disk(rows, p):
    """`p`: retention_s, window_s, segment_bytes, delivery_log_bytes. The bound is computed from what was ingested (the column `ingested_bytes`, cumulative)."""
    rows = [r for r in rows if r.get("data_bytes") not in (None, "")]
    if not rows:
        return {"id": "G4", "ok": None, "what": "disk", "why": "no samples"}
    span = p["retention_s"] + p["window_s"] + 120.0
    ts = [float(r["t"]) for r in rows]
    ing = [float(r.get("ingested_bytes") or 0) for r in rows]
    worst, worst_ratio = None, 0.0
    peak = 0.0
    for i, r in enumerate(rows):
        if ts[i] - span <= ts[0]:
            recent = ing[i]
        else:
            recent = ing[i] - ing[min(bisect.bisect_left(ts, ts[i] - span), i)]
        bound = 1.5 * recent + 2 * p["segment_bytes"] + 4 * p["delivery_log_bytes"] + 4 * 1048576
        size = float(r["data_bytes"])
        peak = max(peak, size)
        if size / bound > worst_ratio:
            worst_ratio, worst = size / bound, {"t": ts[i], "bytes": size, "bound": round(bound)}
    over = worst_ratio > 1.0
    out = {"id": "G4", "ok": not over, "what": "disk against what retention allows", "peak_bytes": peak, "worst_ratio_to_bound": round(worst_ratio, 3), "worst": worst}
    run = ts[-1] - ts[0]
    if run >= 3 * span:
        q2 = [float(r["data_bytes"]) for r in rows if ts[0] + run * 0.25 <= float(r["t"]) <= ts[0] + run * 0.5]
        h2 = [float(r["data_bytes"]) for r in rows if float(r["t"]) >= ts[0] + run * 0.5]
        growth = max(h2) / max(q2) if q2 and h2 and max(q2) else 1.0
        out["last_half_max_over_second_quarter_max"] = round(growth, 3)
        out["ok"] = out["ok"] and growth <= 1.3
    else:
        out["stationarity"] = f"not evaluated: the run ({run:.0f} s) is shorter than three times the window the bound uses ({span:.0f} s)"
    return out


def check_probe(windows, excused, p):
    """`windows`: dicts {t, n, max, ctl, err, l} (per second: requests, the largest answer time in ms, the largest answer time of the host's control request, failures,
    the times). `excused`: [(from, to)] times the service was away or deliberately frozen. `p`: stall_ms (500), p999_ms (50)."""
    stall_ms, p999 = p["stall_ms"], p["p999_ms"]

    def is_excused(t):
        return any(a - 1.0 <= t <= b + 2.0 for a, b in excused)

    stalls, host, errs, lat, worst = [], 0, 0, [], 0.0
    n_windows = 0
    for w in windows:
        t = float(w["t"])
        if is_excused(t):
            continue
        n_windows += 1
        mx = float(w.get("max", 0))
        ctl = float(w.get("ctl", 0))
        shared = ctl > max(50.0, stall_ms / 4.0)
        if w.get("err"):
            if shared:
                host += 1
            else:
                errs += int(w["err"])
                stalls.append({"t": t, "errors": w["err"], "max_ms": mx})
        if mx > stall_ms:
            if shared:
                host += 1
            else:
                stalls.append({"t": t, "max_ms": mx, "ctl_ms": ctl})
        if not shared:
            lat.extend(float(x) for x in w.get("l", []))
            worst = max(worst, mx)
    lat.sort()
    p99 = percentile(lat, 0.99)
    p999v = percentile(lat, 0.999)
    return [{"id": "H1", "ok": not stalls, "what": f"no stall above {stall_ms:.0f} ms outside the excused cases", "windows": n_windows, "stalls": len(stalls),
             "worst_ms": round(worst, 1), "windows_where_the_host_was_late_too": host, "examples": stalls[:10]},
            {"id": "H2", "ok": (p999v <= p999) if lat else None, "what": f"99.9th percentile of the probe at most {p999:.0f} ms", "samples": len(lat),
             "p50_ms": round(percentile(lat, 0.5), 2), "p99_ms": round(p99, 2), "p999_ms": round(p999v, 2), "max_ms": round(lat[-1], 1) if lat else 0}]


def check_maintenance(rows, limit_ms=250):
    worst = max((float(r["maint_ms_max"]) for r in rows if r.get("maint_ms_max") not in (None, "")), default=0.0)
    return {"id": "H1b", "ok": worst <= limit_ms, "what": f"the service's own longest step of the loop (maintenance_ms_max) at most {limit_ms} ms", "worst_ms": worst}


def check_all(rows, probe_windows, excused, p):
    """Everything judged on series. `rows`: dicts as read from metrics.csv. Returns the list of results."""
    out = []
    incs = incarnations(rows)
    if incs:
        last = max(incs)
        out.append(check_rss(incs[last], "G1", {"min_s": p["min_tail_s"]}))
        out.append(check_fds(incs[last], "G3", {"min_s": p["min_tail_s"]}))
        for k in sorted(incs):
            if k == last:
                continue
            r = check_rss(incs[k], "G2", {"min_s": p["min_inc_s"]})
            if r["ok"] is not None:
                out.append(r)
            r = check_fds(incs[k], "G3", {"min_s": p["min_inc_s"]})
            if r["ok"] is not None:
                out.append(r)
        if not any(r["id"] == "G2" for r in out):
            out.append({"id": "G2", "ok": None, "what": "resident memory of earlier incarnations", "why": "no earlier incarnation lived long enough"})
    out.append(check_disk(rows, p))
    out += check_probe(probe_windows, excused, p)
    out.append(check_maintenance(rows))
    return out
