"""The two threads that watch (docs/soak.md sections 2 and 3): the **checker**, which once a second reads each endpoint's cursor and *then* both ledgers and hands them to the
verifier, and the **watcher**, which every `--sample-s` seconds writes one row of `metrics.csv` and one line of `samples.jsonl`.

The order in the checker is the point: the cursor is the service's claim that everything up to it is delivered, and a receiver writes its ledger record before it answers, so
whatever the service could have counted is in the ledger by the time the claim is read after it.
"""
import csv
import json
import os
import re
import threading
import time

from common import ACK, REC, read_records
import guard
from service import dir_usage

COLUMNS = ["t", "el", "inc", "pid", "up", "rss_kb", "hwm_kb", "threads", "fds", "cpu_s", "data_bytes", "seg_files", "files", "events_first", "events_last", "segments", "sealed",
           "dropped", "snapshots", "maint_ms_max", "maint_errors", "lock_skips", "delivered", "failed", "dead", "filtered", "in_flight", "retries_waiting", "replays_waiting",
           "lag_max", "lag_sum", "lag_over_1024", "db_reconnects", "db_failures", "db_losses", "hist_written", "hist_failed", "hist_dropped", "probe_max_ms", "probe_p99_ms",
           "probe_errors", "control_max_ms", "ingest_per_s", "ingest_p50_ms", "ingest_p99_ms", "ingest_max_ms", "ingested_bytes", "deliv_lat_p50_ms", "deliv_lat_p99_ms",
           "deliv_lat_max_ms", "harness_cpu_pct", "receivers_cpu_pct", "receivers_cpu_max_pct", "harness_total_cpu_pct", "loadavg1", "recv_loop_lag_max_ms", "bursting", "acked", "phase",
           # what the host was doing (guard.HostSampler), and what the receivers' writer and ports did
           "loadavg5", "loadavg15", "psi_cpu_some10", "psi_cpu_some60", "psi_mem_some10", "psi_mem_some60", "psi_mem_full10", "psi_mem_full60", "psi_io_some10", "psi_io_some60",
           "psi_io_full10", "psi_io_full60", "mem_avail_mb", "swap_free_mb", "swap_used_mb", "swapin_s", "swapout_s", "majflt_s", "ctxt_s", "procs_running", "procs_blocked",
           "host_iowait_pct", "host_steal_pct", "host_busy_pct", "recv_late_unplanned", "recv_conn_lost", "recv_bind_failures", "recv_writer_wait_max_ms", "recv_sync_fallbacks",
           "recv_writer_queue_max", "recv_records", "valid", "events_expired"]

SERIES = re.compile(r'^(hooks_[a-z_]+)\{endpoint="(\d+)"\} (\S+)$', re.M)
PLAIN = re.compile(r'^(hooks_[a-z_]+) (\S+)$', re.M)


def parse_series(text, name):
    return {int(e): float(v) for n, e, v in SERIES.findall(text) if n == name}


def sweep_sidecars(d):
    """The fsync shim leaves `<file>.synced` beside every log it has seen; the service deletes the segment and the shim does not know. Take away the ones whose file is gone."""
    try:
        for n in os.listdir(d):
            if n.endswith(".synced") and not os.path.exists(os.path.join(d, n[:-7])):
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
    except OSError:
        pass


def seek_time(path, st, target):
    """The offset of the first record of a ledger whose time (its first field) is not before `target` (the records are in time order, near enough for a window of a minute)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    lo, hi = 0, size // st.size
    with open(path, "rb") as f:
        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(mid * st.size)
            if st.unpack(f.read(st.size))[0] < target:
                lo = mid + 1
            else:
                hi = mid
    return lo * st.size


class Checker(threading.Thread):
    def __init__(self, run):
        super().__init__(daemon=True, name="checker")
        self.r = run
        self.recv_paths = list(run.recv_paths)
        self.recv_off = [0] * len(self.recv_paths)
        self.ack_off = 0
        self.last_purge = 0.0
        self.cycles = 0
        self.ack_path = os.path.join(run.out, "acked.bin")

    def seek_end_minus(self, seconds):
        """On a resume: start the ledgers again from where the open window began, so that the counts of the newest events are rebuilt."""
        target = time.time() - seconds
        for k, path in enumerate(self.recv_paths):
            self.recv_off[k] = seek_time(path, REC, target)
        self.ack_off = seek_time(self.ack_path, ACK, target)

    def drain(self):
        """Both ledgers, to their ends. The poster's first, so that an event is acknowledged before its delivery is looked at. The receivers' ledgers (one for each receiver process)
        are read one after the other and merged by the time of the request, so that what the checker is given is in the order it happened whichever process wrote it."""
        r = self.r
        recs, self.ack_off = read_records(self.ack_path, self.ack_off, ACK, limit=64 << 20)
        if recs:
            r.verifier.ingest_acks(recs)
        batch = []
        for k, path in enumerate(self.recv_paths):
            part, self.recv_off[k] = read_records(path, self.recv_off[k], REC, limit=64 << 20)
            batch += part
        if len(self.recv_paths) > 1:
            batch.sort(key=lambda x: x[0])
        if batch:
            r.verifier.ingest(batch)
        return len(batch)

    def cycle(self):
        r = self.r
        inc, pid = r.svc.inc, r.svc.pid
        s1, eps = r.read("/endpoints", timeout=3)
        s2, st = r.read("/stats", timeout=3)
        t = time.time()
        with r.vlock:
            self.drain()
            if s1 == 200 and isinstance(eps, list) and r.svc.inc == inc and r.svc.pid == pid:
                last_id = st.get("events_last_id") if s2 == 200 and isinstance(st, dict) else None
                for e in eps:
                    label = r.label_of.get(e["id"])
                    if label is None or label not in r.verifier.by_label:
                        continue
                    ep = r.eps[label]
                    r.verifier.cursor_sample(label, e["cursor"], t, inc, last_id)
                    r.verifier.settle(label, e["cursor"], now=t)
                    ep.cursor = e["cursor"]
                    if e.get("disabled") and ep.cls in ("oracle", "healthy", "healthy2", "filter", "slow", "flapping", "http5xx", "https", "rate", "churn"):
                        r.verifier.v.add("F_disabled", ep=label, paused=e.get("paused"), t=t)
                if s2 == 200 and isinstance(st, dict):
                    r.last_stats = st
                    r.last_stats_t = t
            if t - self.last_purge > 10:
                self.last_purge = t
                r.verifier.purge(t)
        self.cycles += 1

    def sync_settle(self, label):
        """A churn endpoint is about to go: its cursor has reached the newest event; read the ledger once more and judge everything up to it."""
        r = self.r
        ep = r.eps[label]
        s, e = r.read(f"/endpoints/{ep.svc_id}", timeout=5)
        if s != 200:
            return
        with r.vlock:
            self.drain()
            r.verifier.settle(label, e["cursor"], now=time.time())

    def run(self):
        r = self.r
        while not r.stop_all.is_set():
            t0 = time.time()
            try:
                self.cycle()
            except Exception as ex:  # noqa: BLE001
                r.log("checker_error", error=repr(ex))
            r.stop_all.wait(max(0.05, 1.0 - (time.time() - t0)))


class Watcher(threading.Thread):
    def __init__(self, run):
        super().__init__(daemon=True, name="watcher")
        self.r = run
        path = os.path.join(run.out, "metrics.csv")
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="", buffering=1)
        self.w = csv.DictWriter(self.f, fieldnames=COLUMNS, extrasaction="ignore")
        if fresh:
            self.w.writeheader()
        self.js = open(os.path.join(run.out, "samples.jsonl"), "a", buffering=1)
        self.probe_off = 0
        self.last_sweep = 0.0
        self.last_trim = 0.0
        self.last = {"t": time.time(), "cpu": time.process_time(), "recv_cpu": None, "probe_cpu": None}
        self.rows = []
        self.last_threads = ({}, time.time())
        self.host = guard.HostSampler()
        self.last_recv_cpu = {}          # shard -> cpu seconds at the last sample
        self.proc_snap = (time.time(), guard.proc_ticks())
        self.last_sample_t = time.time()

    def thread_cpu(self):
        """The three threads of the harness that used the most CPU since the last sample, as `{name: percent of a core}`: a thread that spins holds the interpreter lock
        and starves the rest (the second 24 h run could not say which one it was). Linux only; empty elsewhere."""
        out = {}
        try:
            names = {t.native_id: t.name for t in threading.enumerate()}
            now = time.time()
            ticks = {}
            for tid in os.listdir("/proc/self/task"):
                with open(f"/proc/self/task/{tid}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                ticks[int(tid)] = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
            last, last_t = self.last_threads
            dt = max(1e-3, now - last_t)
            deltas = {tid: (c - last.get(tid, c)) / dt * 100 for tid, c in ticks.items()}
            for tid, pct in sorted(deltas.items(), key=lambda kv: -kv[1])[:3]:
                if pct >= 1.0:
                    out[names.get(tid, f"tid{tid}")] = round(pct, 1)
            self.last_threads = (ticks, now)
        except (OSError, IndexError, ValueError):
            pass
        return out

    def probe_windows(self):
        out = []
        path = os.path.join(self.r.out, "probe.jsonl")
        try:
            with open(path, "rb") as f:
                f.seek(self.probe_off)
                data = f.read()
        except OSError:
            return out
        end = data.rfind(b"\n") + 1
        self.probe_off += end
        for line in data[:end].splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def proc_cpu(self, pid):
        try:
            f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, TypeError):
            return 0.0

    def sample(self):
        r = self.r
        now = time.time()
        row = {"t": round(now, 3), "el": round(r.elapsed(), 1), "inc": r.svc.inc, "pid": r.svc.pid or "", "up": 0, "bursting": int(r.bursting), "phase": r.phase, "acked": r.poster.counts["acked"],
               "ingested_bytes": r.poster.bytes_ingested}
        try:
            row["loadavg1"] = os.getloadavg()[0]
        except OSError:
            pass
        info = r.svc.proc_info()
        if info:
            row["up"] = 1
            row["rss_kb"], row["hwm_kb"], row["threads"], row["fds"] = info
            row["cpu_s"] = round(r.svc.cpu_s(), 2)
        row["data_bytes"], row["files"], row["seg_files"] = dir_usage(r.datadir)
        if now - self.last_sweep > 60:
            self.last_sweep = now
            sweep_sidecars(r.datadir)
        if now - self.last_trim > 300:
            self.last_trim = now
            # the history of attempts is a row for each delivery attempt: 38 million a day at the soak's rate. Nobody reads it here; keep half an hour of it (it is written through the service, and what is
            # trimmed is behind the service's back, which it does not mind: it only inserts)
            try:
                r.psql_rows("delete from attempts where at_ms < (extract(epoch from now()) * 1000)::bigint - 1800000")
            except Exception:  # noqa: BLE001
                pass
        detail = {"t": row["t"], "lag": {}, "dead": {}, "threads": self.thread_cpu()}
        s, st = r.read("/stats", timeout=3)
        if s == 200 and isinstance(st, dict):
            for col, key in (("events_first", "events_first_id"), ("events_last", "events_last_id"), ("segments", "events_segments"), ("sealed", "segments_sealed"),
                             ("dropped", "segments_dropped"), ("snapshots", "snapshots"), ("maint_ms_max", "maintenance_ms_max"), ("maint_errors", "maintenance_errors"),
                             ("lock_skips", "maintenance_lock_skips"), ("delivered", "delivered"), ("failed", "failed"), ("dead", "dead"), ("filtered", "filtered"),
                             ("db_reconnects", "database_reconnects"), ("db_failures", "database_failures"), ("db_losses", "database_losses"), ("hist_written", "history_written"),
                             ("hist_failed", "history_failed"), ("hist_dropped", "history_dropped"), ("replays_waiting", "replays")):
                row[col] = st.get(key, "")
            if isinstance(st.get("events_expired"), int):
                r.expired_seen[r.svc.inc] = max(r.expired_seen.get(r.svc.inc, 0), st["events_expired"])
                row["events_expired"] = st["events_expired"]
        s, text = r.read("/metrics", timeout=4, raw=True)
        if s == 200 and isinstance(text, str):
            plain = {n: float(v) for n, v in PLAIN.findall(text)}
            row["in_flight"] = plain.get("hooks_attempts_in_flight", "")
            row["retries_waiting"] = plain.get("hooks_retries_waiting", "")
            lag = parse_series(text, "hooks_endpoint_lag_events")
            dead = parse_series(text, "hooks_endpoint_dead_letters")
            if lag:
                row["lag_max"], row["lag_sum"], row["lag_over_1024"] = max(lag.values()), sum(lag.values()), sum(1 for v in lag.values() if v > 1024)
            for sid, v in lag.items():
                detail["lag"][r.label_of.get(sid, f"id{sid}")] = v
                lb = r.label_of.get(sid)
                if lb is not None:
                    with r.vlock:
                        r.verifier.note_lag(lb, v, r.args.rate)
            for sid, v in dead.items():
                lb = r.label_of.get(sid)
                detail["dead"][lb or f"id{sid}"] = v
                if lb is not None:
                    with r.vlock:
                        r.verifier.dead_letters(lb, int(v), now)
        pw = self.probe_windows()
        if pw:
            lat = sorted(x for w in pw for x in w.get("l", []))
            row["probe_max_ms"] = max(w.get("max", 0) for w in pw)
            row["probe_p99_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.99))] if lat else 0
            row["probe_errors"] = sum(w.get("err", 0) for w in pw)
            row["control_max_ms"] = max(w.get("ctl", 0) for w in pw)
        w = r.poster.window()
        row["ingest_per_s"], row["ingest_p50_ms"], row["ingest_p99_ms"], row["ingest_max_ms"] = round(w["per_s"], 1), round(w["p50"], 2), round(w["p99"], 2), round(w["max"], 2)
        rs = r.recv({"op": "stats"})
        dt = max(1e-3, now - self.last["t"])
        recv_pct = recv_max = None
        if isinstance(rs, dict) and "cpu_s" in rs:
            row["deliv_lat_p50_ms"], row["deliv_lat_p99_ms"], row["deliv_lat_max_ms"] = rs["lat_p50"], rs["lat_p99"], rs["lat_max"]
            row["recv_loop_lag_max_ms"] = round(rs["loop_lag_max_ms"], 1)
            pcts = []
            for pr in rs["procs"]:
                prev = self.last_recv_cpu.get(pr["shard"])
                if prev is not None:
                    pcts.append((pr["cpu_s"] - prev) / dt * 100)
                self.last_recv_cpu[pr["shard"]] = pr["cpu_s"]
            if len(pcts) == len(rs["procs"]):
                recv_pct, recv_max = sum(pcts), max(pcts)
                row["receivers_cpu_pct"], row["receivers_cpu_max_pct"] = round(recv_pct, 1), round(recv_max, 1)
            for col, key in (("recv_late_unplanned", "late_unplanned"), ("recv_conn_lost", "conn_lost"), ("recv_bind_failures", "bind_failures"), ("recv_writer_wait_max_ms", "writer_wait_max_ms"),
                             ("recv_sync_fallbacks", "sync_fallbacks"), ("recv_writer_queue_max", "writer_queue_max"), ("recv_records", "records")):
                row[col] = rs[key]
            r.recv_stats = rs
            if rs.get("writer_errors"):
                r.violate("P_receiver_ledger_write", errors=rs["writer_errors"])
        cpu = time.process_time()
        pcpu = self.proc_cpu(r.probe.pid) if r.probe else 0.0
        main_pct = None
        if self.last["probe_cpu"] is not None:
            main_pct = ((cpu - self.last["cpu"]) + (pcpu - self.last["probe_cpu"])) / dt * 100
            row["harness_cpu_pct"] = round(main_pct, 1)       # the harness's own processes: the poster, the checker, the watcher, the workers and the probe; the receivers are counted apart
            if recv_pct is not None:
                row["harness_total_cpu_pct"] = round(main_pct + recv_pct, 1)
        self.last.update(t=now, cpu=cpu, probe_cpu=pcpu)
        # the host, so that a stall can be put on something; and who else was using the cores when one was seen
        row.update(self.host.sample(now))
        snap_t, before = self.proc_snap
        after = guard.proc_ticks()
        self.proc_snap = (now, after)
        gap = now - self.last_sample_t - r.args.sample_s
        self.last_sample_t = now
        stalled = gap > 5.0 or (row.get("recv_loop_lag_max_ms") or 0) > 5000 or (row.get("probe_max_ms") or 0) > 5000
        if stalled:
            mine = {os.getpid(), r.svc.pid or 0, *(p.pid for p in r.receivers), r.probe.pid if r.probe else 0}
            top = guard.top_processes(before, after, now - snap_t, 3, mine)
            r.log("harness-event", what="stall", sample_gap_s=round(gap + r.args.sample_s, 1), recv_loop_lag_max_ms=row.get("recv_loop_lag_max_ms"), probe_max_ms=row.get("probe_max_ms"),
                  top_processes=top, loadavg1=row.get("loadavg1"), psi_cpu_some10=row.get("psi_cpu_some10"), psi_mem_full10=row.get("psi_mem_full10"), psi_io_some10=row.get("psi_io_some10"))
        # the validity guard: one sample at a time
        steady = row.get("up") == 1 and not r.bursting and not any(x - 1 <= now <= y + 1 for x, y in list(r.excused))
        inv, viol = r.guard.feed({"t": now, "recv_cpu_max_pct": recv_max, "recv_lag_ms": row.get("recv_loop_lag_max_ms"), "harness_cpu_pct": row.get("harness_cpu_pct"),
                                  "mem_avail_mb": row.get("mem_avail_mb"), "swap_io_s": (row["swapin_s"] + row["swapout_s"]) if "swapin_s" in row and "swapout_s" in row else None,
                                  "psi_mem_full60": row.get("psi_mem_full60"), "ingest_per_s": row.get("ingest_per_s"), "steady": steady,
                                  "bind_stuck": rs.get("bind_stuck") if isinstance(rs, dict) else None, "late_unplanned": row.get("recv_late_unplanned"),
                                  "replays_waiting": row.get("replays_waiting"), "data_bytes": row.get("data_bytes"), "ingested_bytes": row.get("ingested_bytes")})
        for code, why in inv:
            r.mark_invalid(code, why)
        for code, why in viol:
            r.violate(code, why=why)
        row["valid"] = 0 if r.guard.invalid else 1
        self.w.writerow(row)
        self.js.write(json.dumps(detail) + "\n")
        self.rows.append(row)
        return row

    def run(self):
        r = self.r
        while not r.stop_all.is_set():
            t0 = time.time()
            try:
                self.sample()
            except Exception as ex:  # noqa: BLE001
                r.log("watcher_error", error=repr(ex))
            r.stop_all.wait(max(0.2, r.args.sample_s - (time.time() - t0)))
