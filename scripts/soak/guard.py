"""The validity guard of the soak (docs/soak.md, "When a run is valid"), and what it needs of the host.

A run is only evidence about the service while the harness and the host are not the limit: a receiver that answers late is a failed attempt to the service, and everything that
follows (repeats, lag, late deliveries) is the harness's. The third 24 h run found that out at hour 15, from a report written at the end. The guard judges **continuously**: every
sample is fed to it, it keeps a sliding window of the last `WINDOW_S` seconds, and the moment a criterion is met it says so (`invalid`: reason by reason, once each) and, for the
two bounds the harness watches on the service's behalf (the disk against what retention allows, and the table of waiting replays), it raises violations. The run marks itself INVALID
in `run.json` from that moment and (without `--no-early-stop`) ends.

No I/O and no clock of its own except in the functions that read /proc (`host_sample`, `check_start`, `top_processes`): `Guard.feed` takes the time from the sample.
"""
import os
from collections import deque

WINDOW_S = 600.0
MIN_SAMPLES = 12                 # fewer samples than this are never judged; until the window is nine tenths full only a gross case is (SEVERE_FRACTION of the samples)
SEVERE_FRACTION = 0.5
RECV_CPU_MAX = 85.0              # per cent of a core, of the busiest receiver process
RECV_LAG_MS = 250.0
BAD_FRACTION = 0.05              # of the samples of the window
HARNESS_CPU_MAX = 60.0           # per cent of a core, the harness's own processes (the poster, the checker, the watcher, the probe: not the receivers)
RATE_RATIO_MIN = 0.9
MEM_AVAIL_MIN_MB = 1024.0        # while running
SWAP_IO_MAX = 200.0              # pages a second, swapped in and out together, mean over the window
PSI_MEM_FULL_MAX = 5.0           # per cent of the time all tasks stalled on memory (avg60), for three samples in a row
LATE_UNPLANNED_MAX = 20          # prompt answers sent more than a second late, in a window
START_MEM_AVAIL_MB = 2048.0
START_SWAP_FREE_MB = 64.0        # with this little swap left, memory must be plentiful (START_SWAP_RAM_MB) or the run does not start
START_SWAP_RAM_MB = 6144.0


def read_meminfo():
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            out[k] = float(v.split()[0]) / 1024.0      # MB
    except (OSError, ValueError, IndexError):
        pass
    return out


def check_start(meminfo=None, allow_low=False):
    """Reasons the run must not start on this host (empty: it may). Free memory and swap are looked at once, here, and watched while the run goes on (`Guard.feed`)."""
    m = read_meminfo() if meminfo is None else meminfo
    if not m or allow_low:
        return []
    out = []
    avail, swap_total, swap_free = m.get("MemAvailable", 1e9), m.get("SwapTotal", 0.0), m.get("SwapFree", 0.0)
    if avail < START_MEM_AVAIL_MB:
        out.append(f"only {avail:.0f} MB of memory is available (at least {START_MEM_AVAIL_MB:.0f} MB are needed)")
    if swap_total > 0 and swap_free < START_SWAP_FREE_MB and avail < START_SWAP_RAM_MB:
        out.append(f"swap is exhausted ({swap_free:.0f} of {swap_total:.0f} MB free) and only {avail:.0f} MB of memory is available (a run needs {START_SWAP_RAM_MB:.0f} MB then, since nothing can be paged out)")
    return out


def _read_psi(name):
    """{'some': (avg10, avg60), 'full': (avg10, avg60)} of /proc/pressure/<name>, or {}."""
    out = {}
    try:
        for line in open(f"/proc/pressure/{name}"):
            kind, *kv = line.split()
            d = dict(x.split("=") for x in kv)
            out[kind] = (float(d["avg10"]), float(d["avg60"]))
    except (OSError, ValueError, KeyError):
        pass
    return out


class HostSampler:
    """What the host was doing, at each sample, so that a stall can be put on something: load, pressure stall information, memory and swap, paging, context switches, the machine's CPU
    split. Rates are over the time since the last call. Linux only; a field the kernel does not give is left out."""

    def __init__(self):
        self.last = None

    def sample(self, now):
        row = {}
        try:
            row["loadavg1"], row["loadavg5"], row["loadavg15"] = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
        except (OSError, ValueError):
            pass
        for name, kinds in (("cpu", ("some",)), ("memory", ("some", "full")), ("io", ("some", "full"))):
            psi = _read_psi(name)
            for k in kinds:
                if k in psi:
                    short = {"memory": "mem"}.get(name, name)
                    row[f"psi_{short}_{k}10"] = psi[k][0]
                    row[f"psi_{short}_{k}60"] = psi[k][1]
        m = read_meminfo()
        if m:
            row["mem_avail_mb"] = round(m.get("MemAvailable", 0.0), 1)
            row["swap_free_mb"] = round(m.get("SwapFree", 0.0), 1)
            row["swap_used_mb"] = round(m.get("SwapTotal", 0.0) - m.get("SwapFree", 0.0), 1)
        cur = {}
        try:
            for line in open("/proc/vmstat"):
                k, v = line.split()
                if k in ("pswpin", "pswpout", "pgmajfault"):
                    cur[k] = int(v)
            for line in open("/proc/stat"):
                f = line.split()
                if f[0] == "cpu":
                    cur["cpu"] = [int(x) for x in f[1:9]]
                elif f[0] == "ctxt":
                    cur["ctxt"] = int(f[1])
                elif f[0] == "procs_running":
                    row["procs_running"] = int(f[1])
                elif f[0] == "procs_blocked":
                    row["procs_blocked"] = int(f[1])
        except (OSError, ValueError, IndexError):
            pass
        if self.last is not None and cur:
            t0, prev = self.last
            dt = max(1e-3, now - t0)
            if "pswpin" in cur and "pswpin" in prev:
                row["swapin_s"] = round((cur["pswpin"] - prev["pswpin"]) / dt, 1)
                row["swapout_s"] = round((cur["pswpout"] - prev["pswpout"]) / dt, 1)
                row["majflt_s"] = round((cur["pgmajfault"] - prev["pgmajfault"]) / dt, 1)
            if "ctxt" in cur and "ctxt" in prev:
                row["ctxt_s"] = round((cur["ctxt"] - prev["ctxt"]) / dt)
            if "cpu" in cur and "cpu" in prev:
                d = [a - b for a, b in zip(cur["cpu"], prev["cpu"])]
                tot = sum(d) or 1
                row["host_iowait_pct"] = round(100.0 * d[4] / tot, 1)
                row["host_steal_pct"] = round(100.0 * d[7] / tot, 1)
                row["host_busy_pct"] = round(100.0 * (tot - d[3] - d[4]) / tot, 1)
        if cur:
            self.last = (now, cur)
        return row


def proc_ticks():
    """{pid: (ticks used so far, command name, cpu it last ran on)} for every process (Linux)."""
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            raw = open(f"/proc/{n}/stat").read()
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            f = raw[raw.rindex(")") + 2:].split()
            out[int(n)] = (int(f[11]) + int(f[12]), comm, int(f[36]))
        except (OSError, ValueError, IndexError):
            continue
    return out


def top_processes(before, after, seconds, n=3, mine=()):
    """The `n` processes that used most CPU between two `proc_ticks()`: [{pid, comm, cpu_pct, cpu, mine}]; `mine`: pids of the harness's and the service's own."""
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    rows = []
    for pid, (t, comm, cpu) in after.items():
        b = before.get(pid)
        d = t - b[0] if b else t
        if d > 0:
            rows.append({"pid": pid, "comm": comm, "cpu_pct": round(100.0 * d / hz / max(seconds, 1e-3), 1), "cpu": cpu, "mine": pid in mine})
    rows.sort(key=lambda r: -r["cpu_pct"])
    return rows[:n]


class Guard:
    """Feed it one dict per sample (`feed`); it says which criteria have been met. `args`: rate (events a second asked for), replay_cap, retention terms for the disk bound."""

    def __init__(self, rate=40.0, replay_cap=32, replay_pinned_s=180.0, disk_params=None, window_s=WINDOW_S):
        self.rate, self.replay_cap, self.replay_pinned_s = rate, replay_cap, replay_pinned_s
        self.window_s = window_s
        self.win = deque()
        self.invalid = {}               # code -> {"since": t, "why": text}
        self.mem_low_run = 0
        self.psi_run = 0
        self.replays_since = None
        self.replays_raised = False
        self.disk = deque()
        self.disk_over_run = 0
        self.disk_raised = False
        self.dp = disk_params or {"retention_s": 180.0, "window_s": 120.0, "segment_bytes": 1 << 20, "delivery_log_bytes": 1 << 18}
        self.late_base = None

    def _flag(self, code, t, why, out):
        if code not in self.invalid:
            self.invalid[code] = {"since": t, "why": why}
            out.append((code, why))

    def feed(self, s):
        """`s`: t, recv_cpu_max_pct, recv_lag_ms, harness_cpu_pct, mem_avail_mb, swap_io_s, psi_mem_full60, ingest_per_s, steady (bool: counts for the rate), bind_stuck (list),
        late_unplanned (cumulative), replays_waiting, data_bytes, ingested_bytes. Missing keys are not judged. Returns (new invalid reasons, new violations) as [(code, text)]."""
        t = s["t"]
        inv, viol = [], []
        self.win.append(s)
        while self.win and self.win[0]["t"] < t - self.window_s:
            self.win.popleft()
        w = list(self.win)
        full = w[-1]["t"] - w[0]["t"] >= 0.9 * self.window_s
        frac = BAD_FRACTION if full else SEVERE_FRACTION
        if len(w) >= MIN_SAMPLES:
            cpu = [x["recv_cpu_max_pct"] for x in w if x.get("recv_cpu_max_pct") is not None]
            lag = [x["recv_lag_ms"] for x in w if x.get("recv_lag_ms") is not None]
            if cpu and sum(1 for x in cpu if x > RECV_CPU_MAX) / len(cpu) > frac:
                self._flag("receivers_cpu", t, f"the busiest receiver process was over {RECV_CPU_MAX:.0f} % of a core in {100 * sum(1 for x in cpu if x > RECV_CPU_MAX) / len(cpu):.0f} % of the last "
                           f"{self.window_s / 60:.0f} minutes of samples: it answers late, which the service counts as a failed attempt", inv)
            if lag and sum(1 for x in lag if x > RECV_LAG_MS) / len(lag) > frac:
                self._flag("receivers_lag", t, f"the receivers' loop was late by more than {RECV_LAG_MS:.0f} ms in {100 * sum(1 for x in lag if x > RECV_LAG_MS) / len(lag):.0f} % of the last "
                           f"{self.window_s / 60:.0f} minutes of samples", inv)
            hc = [x["harness_cpu_pct"] for x in w if x.get("harness_cpu_pct") is not None]
            if hc and full and sum(hc) / len(hc) > HARNESS_CPU_MAX:
                self._flag("harness_cpu", t, f"the harness's own processes used {sum(hc) / len(hc):.0f} % of a core on average over the last {self.window_s / 60:.0f} minutes (limit {HARNESS_CPU_MAX:.0f} %)", inv)
            sw = [x["swap_io_s"] for x in w if x.get("swap_io_s") is not None]
            if sw and full and sum(sw) / len(sw) > SWAP_IO_MAX:
                self._flag("swapping", t, f"the host swapped {sum(sw) / len(sw):.0f} pages a second on average over the last {self.window_s / 60:.0f} minutes", inv)
            steady = sorted(x["ingest_per_s"] for x in w if x.get("steady") and x.get("ingest_per_s") is not None)
            if full and len(steady) >= 20 and steady[len(steady) // 2] < RATE_RATIO_MIN * self.rate:
                self._flag("poster_rate", t, f"the poster held {100 * steady[len(steady) // 2] / self.rate:.0f} % of the requested rate (median of {len(steady)} steady samples)", inv)
            late = [x["late_unplanned"] for x in w if x.get("late_unplanned") is not None]
            if full and len(late) >= 2 and late[-1] - late[0] > LATE_UNPLANNED_MAX:
                self._flag("receivers_late", t, f"the receivers sent {late[-1] - late[0]} answers more than a second late that were due at once, in the last {self.window_s / 60:.0f} minutes", inv)
        if s.get("bind_stuck"):
            self._flag("bind_stuck", t, f"a receiver could not listen on the port of {', '.join(s['bind_stuck'][:5])} for more than 10 s", inv)
        if s.get("mem_avail_mb") is not None:
            self.mem_low_run = self.mem_low_run + 1 if s["mem_avail_mb"] < MEM_AVAIL_MIN_MB else 0
            if self.mem_low_run >= 2:
                self._flag("memory", t, f"only {s['mem_avail_mb']:.0f} MB of memory is available on the host", inv)
        if s.get("psi_mem_full60") is not None:
            self.psi_run = self.psi_run + 1 if s["psi_mem_full60"] > PSI_MEM_FULL_MAX else 0
            if self.psi_run >= 3:
                self._flag("memory_pressure", t, f"all tasks stalled on memory {s['psi_mem_full60']:.1f} % of the time (PSI avg60)", inv)
        # the two bounds the harness keeps for the service: violations, not invalidity (a service that keeps data for ever is a finding)
        rw = s.get("replays_waiting")
        if rw is not None and rw != "":
            if rw >= self.replay_cap:
                self.replays_since = self.replays_since if self.replays_since is not None else t
                if t - self.replays_since > self.replay_pinned_s and not self.replays_raised:
                    self.replays_raised = True
                    viol.append(("G_replays_pinned", f"the table of waiting replays has been full ({rw} of {self.replay_cap}) for {t - self.replays_since:.0f} s: replays that never start pin the log"))
            else:
                self.replays_since, self.replays_raised = None, False
        if s.get("data_bytes") is not None and s.get("ingested_bytes") is not None:
            self.disk.append((t, float(s["ingested_bytes"]), float(s["data_bytes"])))
            span = self.dp["retention_s"] + self.dp["window_s"] + 120.0
            while len(self.disk) > 1 and self.disk[1][0] < t - span:
                self.disk.popleft()
            recent = self.disk[-1][1] - self.disk[0][1] if self.disk[0][0] >= t - span - 30 else self.disk[-1][1]
            if t - self.disk[0][0] >= span * 0.9:
                bound = 1.5 * recent + 2 * self.dp["segment_bytes"] + 4 * self.dp["delivery_log_bytes"] + 4 * 1048576
                size = self.disk[-1][2]
                self.disk_over_run = self.disk_over_run + 1 if size > bound else 0
                if self.disk_over_run >= 3 and not self.disk_raised:
                    self.disk_raised = True
                    viol.append(("G_disk_over_bound", f"the data directory is {size / 1048576:.0f} MB, over the {bound / 1048576:.0f} MB that retention allows for what was ingested in the last {span:.0f} s"))
                elif size <= bound:
                    self.disk_raised = False
        return inv, viol
