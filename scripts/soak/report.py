#!/usr/bin/env python3
"""The report of a soak run (docs/soak.md section 5): `report.md` and `report.json`, made from the files the run left in its directory and from nothing else.

    python3 scripts/soak/report.py soak-out

The method as it was run (the parameters of `run.json`, the seed, the SHA-256 of the binary, the commit, the machine), the counts, the verdict table with the measured value
of each criterion, the percentiles, the series of memory, descriptors and disk per incarnation of the service, the probe's distribution and worst windows, and the cost
of the harness. Standard library only.
"""
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import series  # noqa: E402


def load_jsonl(path):
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def fnum(rows, key):
    return [float(r[key]) for r in rows if r.get(key) not in (None, "")]


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def stats(v):
    v = sorted(v)
    if not v:
        return None
    return {"min": v[0], "median": series.median(v), "p99": pct(v, 0.99), "max": v[-1]}


def build(d):
    run = json.load(open(os.path.join(d, "run.json")))
    verdict = json.load(open(os.path.join(d, "verdict.json"))) if os.path.exists(os.path.join(d, "verdict.json")) else None
    rows = list(csv.DictReader(open(os.path.join(d, "metrics.csv")))) if os.path.exists(os.path.join(d, "metrics.csv")) else []
    chaos = load_jsonl(os.path.join(d, "chaos.jsonl"))
    probe = load_jsonl(os.path.join(d, "probe.jsonl"))
    kinds = Counter(c["kind"] for c in chaos)
    pgk = Counter()
    for c in chaos:
        if c["kind"] == "pg" and c.get("start") is True:
            pgk[c.get("which")] += 1
    incs = series.incarnations(rows)
    per_inc = []
    for k in sorted(incs):
        r = incs[k]
        t0, t1 = float(r[0]["t"]), float(r[-1]["t"])
        xs = [float(x["t"]) for x in r]
        rss = fnum(r, "rss_kb")
        fds = fnum(r, "fds")
        slope, _ = series.linfit(xs, rss)
        per_inc.append({"inc": k, "span_s": round(t1 - t0), "samples": len(r), "rss_kb": stats(rss), "rss_slope_kb_per_h": round(slope * 3600, 1), "hwm_kb": max(fnum(r, "hwm_kb") or [0]),
                        "fds": stats(fds), "threads": sorted({int(float(x["threads"])) for x in r})})
    win = [w for w in probe]
    lat = sorted(x for w in win for x in w.get("l", []))
    worst = sorted(win, key=lambda w: -float(w.get("max", 0)))[:5]
    ing = rows
    out = {
        "run": {k: run.get(k) for k in ("commit", "pin", "binary_sha256", "machine", "pinned", "duration_s", "quiet_s", "shim", "service_settings")},
        "parameters": {k: run["args"].get(k) for k in ("seed", "rate", "burst_rate", "endpoints", "churn", "workers", "chaos", "no_chaos", "chaos_scale", "tmpfs_data", "sample_s", "fault", "hours", "duration_s")},
        "verdict": verdict["verdict"] if verdict else "NO VERDICT (the run did not finish)",
        "failed": verdict["failed"] if verdict else None,
        "not_evaluated": verdict["not_evaluated"] if verdict else None,
        "validity": verdict["validity"] if verdict else None,
        "checks": verdict["checks"] if verdict else None,
        "violations": verdict["violations"] if verdict else None,
        "examples": verdict["examples"] if verdict else None,
        "summary": verdict["summary"] if verdict else None,
        "counts": verdict["counts"] if verdict else None,
        "poster": verdict["poster"] if verdict else None,
        "final": verdict["final"] if verdict else None,
        "elapsed_s": verdict["elapsed_s"] if verdict else None,
        "resumed": verdict["resumed"] if verdict else None,
        "actions": dict(kinds),
        "pg_faults": dict(pgk),
        "incarnations": per_inc,
        "latency": {"ingest_p50_ms": stats(fnum(ing, "ingest_p50_ms")), "ingest_p99_ms": stats(fnum(ing, "ingest_p99_ms")), "ingest_max_ms": stats(fnum(ing, "ingest_max_ms")),
                    "delivery_p50_ms": stats(fnum(ing, "deliv_lat_p50_ms")), "delivery_p99_ms": stats(fnum(ing, "deliv_lat_p99_ms")), "delivery_max_ms": stats(fnum(ing, "deliv_lat_max_ms"))},
        "probe": {"requests": len(lat), "p50_ms": pct(lat, 0.5), "p99_ms": pct(lat, 0.99), "p999_ms": pct(lat, 0.999), "max_ms": lat[-1] if lat else None,
                  "worst_windows": [{"t": w["t"], "max_ms": w.get("max"), "control_ms": w.get("ctl"), "errors": w.get("err")} for w in worst]},
        "harness": {"cpu_pct": stats(fnum(rows, "harness_cpu_pct")), "receivers_cpu_pct": stats(fnum(rows, "receivers_cpu_pct")), "loadavg1": stats(fnum(rows, "loadavg1")),
                    "receivers_loop_lag_max_ms": stats(fnum(rows, "recv_loop_lag_max_ms")), "service_cpu_s_last": (fnum(incs[max(incs)], "cpu_s") or [None])[-1] if incs else None},
        "disk": {"data_bytes": stats(fnum(rows, "data_bytes")), "files": stats(fnum(rows, "files")), "segments_sealed_last": (fnum(rows, "sealed") or [None])[-1],
                 "segments_dropped_last": (fnum(rows, "dropped") or [None])[-1], "lag_max": stats(fnum(rows, "lag_max"))},
    }
    return out


def fmt(x, nd=1):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def markdown(o):
    m = o["run"].get("machine") or {}
    p = o["parameters"]
    L = []
    L.append(f"# Soak run report: {o['verdict']}")
    L.append("")
    L.append(f"Seed {p['seed']}, {fmt((o['elapsed_s'] or 0) / 3600.0, 2)} h run ({o['elapsed_s']} s of {o['run'].get('duration_s')} s planned){', resumed' if o.get('resumed') else ''}. "
             f"Commit `{(o['run'].get('commit') or '')[:12]}`, compiler pin `{(o['run'].get('pin') or '')[:12]}`, binary SHA-256 `{(o['run'].get('binary_sha256') or '')[:16]}...`.")
    L.append("")
    L.append(f"Machine: {m.get('cpu')}, {m.get('cores')} cores, memory {m.get('memory')}, kernel {m.get('kernel')}, Python {m.get('python')}; load average at the start {m.get('loadavg_at_start')}. "
             f"Service pinned to cores {o['run'].get('pinned', {}).get('service')}, harness to {o['run'].get('pinned', {}).get('harness')}. "
             "A shared machine unless said otherwise: the load average over the run is below.")
    L.append("")
    if o.get("validity"):
        v = o["validity"]
        L.append(f"**Valid run:** {'yes' if v['valid'] else 'NO: ' + '; '.join(v['reasons'])}. The poster held {fmt(v['poster_rate_ratio'] * 100, 0)} % of the requested rate (median of the steady windows); "
                 f"the harness used {fmt(v['harness_cpu_pct_mean'], 0)} % of a core on average; the load average was above the number of cores in {fmt(v['loadavg_over_cores_fraction'] * 100, 0)} % of the samples.")
        L.append("")
    L.append("## Method")
    L.append("")
    L.append("As `docs/soak.md`. Parameters of this run: " + ", ".join(f"`{k}={v}`" for k, v in p.items() if v not in (None, "", False)) + ".")
    L.append("")
    L.append("Service settings: `" + " ".join(o["run"].get("service_settings") or []) + "`.")
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append("| criterion | result | what | measured |")
    L.append("|---|---|---|---|")
    for c in o.get("checks") or []:
        res = {True: "pass", False: "**FAIL**", None: "not evaluated"}[c["ok"]]
        meas = {k: v for k, v in c.items() if k not in ("id", "ok", "what", "examples", "tags") and v not in (None, [], {})}
        L.append(f"| {c['id']} | {res} | {c['what']} | {json.dumps(meas, default=str)[:300] if meas else (json.dumps(c.get('tags')) if c.get('tags') else '')} |")
    L.append("")
    if o.get("examples"):
        L.append("### Violations (up to five examples of each)")
        L.append("")
        for tag, ex in o["examples"].items():
            L.append(f"* `{tag}` ({o['violations'].get(tag)}): " + "; ".join(json.dumps(e, default=str)[:200] for e in ex[:5]))
        L.append("")
    s = o.get("summary") or {}
    L.append("## Counts")
    L.append("")
    po = o.get("poster") or {}
    L.append(f"Events acknowledged (a `202`): {po.get('acked')}; in doubt: {po.get('indoubt', 0)}; posted again under a key: {po.get('reposts', 0)}; with another body: {po.get('conflicts', 0)}; retried requests: {po.get('retried', 0)}; "
             f"schedule slips: {po.get('slipped', 0)}. Deliveries recorded by the receivers: {s.get('records')}. Repeats: {s.get('repeats')}, explained: {s.get('repeats_explained')}; "
             f"repeats per kill: {s.get('repeats_per_kill')}.")
    L.append("")
    L.append("| class | endpoints | requests that reached a receiver | acknowledged with a 2xx in time | failed attempts |")
    L.append("|---|---|---|---|---|")
    for cls, c in sorted((s.get("by_class") or {}).items()):
        L.append(f"| {cls} | {c.get('endpoints')} | {c.get('records')} | {c.get('delivered')} | {c.get('failed_attempts')} |")
    L.append("")
    cn = o.get("counts") or {}
    L.append("Faults and work: " + ", ".join(f"{k} {v}" for k, v in sorted((o.get("actions") or {}).items())) + ". Database faults by kind: " + json.dumps(o.get("pg_faults")) + ". "
             "Counters: " + json.dumps(cn) + ".")
    L.append("")
    L.append("## Latency")
    L.append("")
    lat = o["latency"]
    L.append("Over the samples (one per sample interval; the median of the interval percentiles, and the worst):")
    L.append("")
    L.append("| | median of windows | 99th percentile of windows | worst window |")
    L.append("|---|---|---|---|")
    for name, key in (("ingest p50 (ms)", "ingest_p50_ms"), ("ingest p99 (ms)", "ingest_p99_ms"), ("ingest max (ms)", "ingest_max_ms"), ("end-to-end delivery p50 (ms)", "delivery_p50_ms"),
                      ("end-to-end delivery p99 (ms)", "delivery_p99_ms"), ("end-to-end delivery max (ms)", "delivery_max_ms")):
        st = lat.get(key) or {}
        L.append(f"| {name} | {fmt(st.get('median'), 2)} | {fmt(st.get('p99'), 2)} | {fmt(st.get('max'), 2)} |")
    L.append("")
    pr = o["probe"]
    L.append(f"The loop probe ({pr['requests']} requests of `/healthz`): p50 {fmt(pr['p50_ms'], 2)} ms, p99 {fmt(pr['p99_ms'], 2)} ms, p99.9 {fmt(pr['p999_ms'], 2)} ms, largest {fmt(pr['max_ms'], 1)} ms. Worst windows: "
             + json.dumps(pr["worst_windows"]) + ".")
    L.append("")
    L.append("## Memory, descriptors and disk, per incarnation of the service")
    L.append("")
    L.append("| inc | span (s) | samples | RSS min / median / max (KiB) | RSS slope (KiB/h) | VmHWM (KiB) | fds min / median / max | threads |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i in o["incarnations"]:
        r, f = i["rss_kb"] or {}, i["fds"] or {}
        L.append(f"| {i['inc']} | {i['span_s']} | {i['samples']} | {fmt(r.get('min'), 0)} / {fmt(r.get('median'), 0)} / {fmt(r.get('max'), 0)} | {i['rss_slope_kb_per_h']} | {fmt(i['hwm_kb'], 0)} | "
                 f"{fmt(f.get('min'), 0)} / {fmt(f.get('median'), 0)} / {fmt(f.get('max'), 0)} | {i['threads']} |")
    L.append("")
    dk = o["disk"]
    d = dk.get("data_bytes") or {}
    L.append(f"Data directory: {fmt((d.get('min') or 0) / 1e6, 2)} MB least, median {fmt((d.get('median') or 0) / 1e6, 2)} MB, largest {fmt((d.get('max') or 0) / 1e6, 2)} MB; segments sealed {dk.get('segments_sealed_last')} and dropped {dk.get('segments_dropped_last')} "
             f"in the last incarnation; the largest lag of any endpoint {fmt((dk.get('lag_max') or {}).get('max'), 0)} events.")
    L.append("")
    h = o["harness"]
    L.append("## The cost of the harness")
    L.append("")
    L.append(f"Harness CPU (main process, receivers, probe), per sample, in per cent of one core: median {fmt((h['cpu_pct'] or {}).get('median'), 1)}, worst {fmt((h['cpu_pct'] or {}).get('max'), 1)}. Receivers alone: median "
             f"{fmt((h['receivers_cpu_pct'] or {}).get('median'), 1)}. The receivers' loop was late by at most {fmt((h['receivers_loop_lag_max_ms'] or {}).get('max'), 1)} ms. Load average of the host (1 minute): median "
             f"{fmt((h['loadavg1'] or {}).get('median'), 2)}, worst {fmt((h['loadavg1'] or {}).get('max'), 2)}.")
    L.append("")
    if o.get("not_evaluated"):
        L.append("Criteria not evaluated in this run: " + "; ".join(f"{x['id']} ({x['why']})" for x in o["not_evaluated"]) + ".")
        L.append("")
    return "\n".join(L) + "\n"


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    d = sys.argv[1]
    o = build(d)
    with open(os.path.join(d, "report.json"), "w") as f:
        json.dump(o, f, indent=1, default=str)
    with open(os.path.join(d, "report.md"), "w") as f:
        f.write(markdown(o))
    print(f"wrote {os.path.join(d, 'report.md')} and report.json: {o['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
