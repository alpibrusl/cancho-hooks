"""The service under test, as the soak starts, kills and stops it: one `hooks` process at a time on one data directory, pinned to its cores, its standard error kept (every
incarnation, with a marker line between them) in `service.log`, and the power cut that follows a `kill -9`.
"""
import os
import random
import signal
import struct
import subprocess
import threading
import time


def _small_pages():
    """Small pages for the service (`prctl(PR_SET_THP_DISABLE)`, run in the child before `exec` and kept across it): with transparent huge pages
    set to `always` the service's memory would follow the host's page size, not what it holds (docs/design.md section 46). Linux only."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(41, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass

# lines the service says on its own (docs/runbook.md 3.1); anything else on standard error is invariant L
KNOWN_STDERR = ("listening", "hooks: the database: ", "hooks: endpoints loaded: ", "hooks: stopping on SIG", "hooks: stopped: ", "hooks: endpoint ",
                # what retention says at each step (src/compact.ls); docs/runbook.md 3.1 lists only the lines of the start and the stop
                "hooks: events log: sealed a segment", "hooks: events log: dropped a segment", "hooks: events log: removed ", "hooks: events log: an endpoint that was away",
                "hooks: delivery.seg: replaced by a snapshot")


def power_cut(datadir, rng):
    """A power cut: every *.seg file (and a snapshot being made, and the manifest events.first) is cut back to what its last fsync covered, plus a random part of the rest, and
    the last block of that part may be zeros (what `tests/fsync_shim.c` is for). Returns the bytes discarded."""
    lost = 0
    for name in sorted(os.listdir(datadir)):
        if not name.endswith((".seg", ".seg.tmp", ".first", ".first.tmp")):
            continue
        path = os.path.join(datadir, name)
        side = path + ".synced"
        try:
            synced = struct.unpack("<q", open(side, "rb").read(8))[0] if os.path.exists(side) else 0
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= synced:
            continue
        keep = synced + rng.randint(0, size - synced)
        with open(path, "r+b") as f:
            f.truncate(keep)
            if keep > synced and rng.random() < 0.5:
                start = max(synced, keep - rng.randint(1, 64))
                f.seek(start)
                f.write(bytes(keep - start))
        lost += size - keep
    return lost


class Svc:
    def __init__(self, binary, datadir, port, flags, log_path, shim=None, cpus=None, rng=None):
        self.binary, self.dir, self.port, self.flags = os.path.abspath(binary), datadir, port, list(flags)
        self.log_path, self.shim, self.cpus = log_path, shim, cpus
        self.rng = rng or random.Random(1)
        self.proc = None
        self.lines = []
        self.lock = threading.Lock()
        self.reader = None
        self.log = open(log_path, "a", buffering=1)
        self.inc = 0
        self.unexpected = []         # lines on standard error that are not the service's own
        self.started_at = 0.0

    def _pre(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        _small_pages()
        if self.cpus:
            try:
                os.sched_setaffinity(0, self.cpus)
            except OSError:
                pass

    def start(self, extra=(), timeout=90.0, wait_loaded=True):
        """Spawn the service; wait for `listening` and (with a database) `endpoints loaded`. Returns (ok, seconds)."""
        self.inc += 1
        env = dict(os.environ)
        if self.shim:
            env["LD_PRELOAD"] = self.shim
        self.lines = []
        self.log.write(f"=== incarnation {self.inc} at {time.time():.3f} {' '.join(extra)}\n")
        t0 = time.time()
        self.proc = subprocess.Popen([self.binary, "--port", str(self.port), "--dir", self.dir, *self.flags, *extra], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     env=env, preexec_fn=self._pre)
        self.started_at = t0
        self.reader = threading.Thread(target=self._read, args=(self.proc, self.inc), daemon=True)
        self.reader.start()
        end = t0 + timeout
        pg = "--pg-host" in self.flags
        while time.time() < end:
            if self.proc.poll() is not None:
                return False, time.time() - t0
            with self.lock:
                listening = "listening" in self.lines
                loaded = any(l.startswith("hooks: endpoints loaded") for l in self.lines)
            if listening and (loaded or not pg or not wait_loaded):
                return True, time.time() - t0
            time.sleep(0.01)
        return False, time.time() - t0

    def _read(self, proc, inc):
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip("\n")
            with self.lock:
                self.lines.append(line)
            self.log.write(f"[{inc}] {line}\n")
            if not (line.startswith(KNOWN_STDERR) or "cut a torn tail" in line):
                self.unexpected.append((inc, line))

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def has_line(self, text):
        with self.lock:
            return any(text in l for l in self.lines)

    def kill9(self, cut=True):
        """SIGKILL, and (with the fsync shim in use) the files cut as a power cut leaves them. Returns (time of death, bytes the cut discarded)."""
        t = time.time()
        if self.alive():
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
        if self.reader:
            self.reader.join(2)
        lost = power_cut(self.dir, self.rng) if (cut and self.shim) else 0
        return t, lost

    def term(self, timeout):
        """SIGTERM, wait. Returns (time, exit code or None if it had to be killed, lines it said while stopping)."""
        t = time.time()
        self.proc.send_signal(signal.SIGTERM)
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
            code = None
        if self.reader:
            self.reader.join(2)
        with self.lock:
            lines = list(self.lines)
        return t, code, lines

    def cpu_s(self):
        try:
            f = open(f"/proc/{self.proc.pid}/stat").read().rsplit(")", 1)[1].split()
            return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, AttributeError):
            return 0.0

    def proc_info(self):
        """(rss kB, hwm kB, threads, descriptors) of the running service, or None."""
        pid = self.pid
        if not pid or not self.alive():
            return None
        rss = hwm = thr = 0
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    hwm = int(line.split()[1])
                elif line.startswith("Threads:"):
                    thr = int(line.split()[1])
            fds = len(os.listdir(f"/proc/{pid}/fd"))
        except OSError:
            return None
        return rss, hwm, thr, fds


def dir_usage(d):
    """(bytes, files, segment files) of a data directory; the `.synced` files the shim writes are not the service's."""
    total = files = segs = 0
    try:
        for name in os.listdir(d):
            if name.endswith(".synced"):
                continue
            try:
                total += os.path.getsize(os.path.join(d, name))
            except OSError:
                continue
            files += 1
            if name.startswith("events") and name.endswith(".seg"):
                segs += 1
    except OSError:
        pass
    return total, files, segs
