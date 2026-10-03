#!/usr/bin/env python3
"""The first criterion of docs/design.md section 6: **no acknowledged event is lost.**

    python3 tests/chaos.py build/hooks [events] [threads] [mean-ms-between-kills]

Starts the service, posts events from several threads, and `kill -9`s the service at random instants, restarting it each
time. A thread whose request fails (the connection died, or no answer came) tries the same event again after the service
is back, so the log may hold an event twice: at-least-once is the contract. What is never allowed is an event the client
was told (`202`, with an id) was stored and which the log does not hold.

The check is made on the file, not through the service: after the last kill the segment is read with a reader written
here (the format of lexsys-log's docs/design.md section 4, its own CRC-32C) and every acknowledged `(id, payload)` must be
in the valid prefix, byte for byte.
"""
import http.client
import json
import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
EVENTS = int(sys.argv[2]) if len(sys.argv) > 2 else 600
THREADS = int(sys.argv[3]) if len(sys.argv) > 3 else 6
MEAN_MS = int(sys.argv[4]) if len(sys.argv) > 4 else 120
# With the shim, a kill is a power cut: the file is cut back to what the last fsync covered, plus a random part of the
# rest, and the last block of that part may be zeroed. Without it, a kill only stops the process and the kernel keeps
# every byte it was given, which cannot show a missing flush.
SHIM = os.environ.get("FSYNC_SHIM", "build/fsync_shim.so")
POWER_LOSS = os.path.exists(SHIM) and os.environ.get("POWER_LOSS", "1") == "1"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- an independent reader of the log file (lexsys-log design section 4) ------------------------------------------

def _table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t


_T = _table()


def crc32c(data):
    c = 0xFFFFFFFF
    for b in data:
        c = (c >> 8) ^ _T[(c ^ b) & 0xFF]
    return c ^ 0xFFFFFFFF


def read_log(data):
    """The longest valid prefix: [(id, [(key, value), ...])], and where it ends."""
    at, out = 0, []
    while len(data) - at >= 4:
        (length,) = struct.unpack_from("<I", data, at)
        if length < 24 or length > 65536 or len(data) - at < 4 + length:
            break
        total = 4 + length
        (stored,) = struct.unpack_from("<I", data, at + 4)
        if stored != crc32c(data[at + 8 : at + total]):
            break
        ms, seq, fields = struct.unpack_from("<QQI", data, at + 8)
        p, pairs, end, ok = at + 28, [], at + total, True
        for _ in range(fields):
            if end - p < 4:
                ok = False
                break
            (kl,) = struct.unpack_from("<I", data, p)
            p += 4
            key = data[p : p + kl]
            p += kl
            if end - p < 4:
                ok = False
                break
            (vl,) = struct.unpack_from("<I", data, p)
            p += 4
            pairs.append((key, data[p : p + vl]))
            p += vl
            if p > end:
                ok = False
                break
        if not ok or p != end:
            break
        out.append((ms, pairs))
        at += total
    return out, at


# ---- the service under chaos -----------------------------------------------------------------------------------

class Service:
    def __init__(self, port, datadir):
        self.port, self.datadir, self.proc, self.starts = port, datadir, None, 0
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            env = dict(os.environ)
            if POWER_LOSS:
                env["LD_PRELOAD"] = os.path.abspath(SHIM)
            self.proc = subprocess.Popen([BIN, str(self.port), self.datadir], stderr=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, env=env)
            self.starts += 1
            self.proc.stderr.readline()  # "listening"

    def kill(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait()
            if POWER_LOSS:
                self.power_cut()

    def power_cut(self):
        """Leave the file as a power cut could: all of what the last fsync covered, a random part of the rest."""
        path = os.path.join(self.datadir, "events.seg")
        if not os.path.exists(path):
            return
        side = path + ".synced"
        synced = struct.unpack("<q", open(side, "rb").read(8))[0] if os.path.exists(side) else 0
        size = os.path.getsize(path)
        if size <= synced:
            return
        keep = synced + random.randint(0, size - synced)
        with open(path, "r+b") as f:
            f.truncate(keep)
            if keep > synced and random.random() < 0.5:
                # The last block that was only partly written may be zeros: a page that never reached the disk.
                start = max(synced, keep - random.randint(1, 64))
                f.seek(start)
                f.write(bytes(keep - start))
        self.cuts = getattr(self, "cuts", 0) + 1
        self.lost = getattr(self, "lost", 0) + (size - keep)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


def post(port, body, timeout=2.0):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("POST", "/events", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        return r.status, data
    finally:
        c.close()


def main():
    datadir = tempfile.mkdtemp(prefix="hooks-chaos-")
    port = free_port()
    svc = Service(port, datadir)
    svc.start()
    acked = {}                    # id -> payload bytes
    acked_lock = threading.Lock()
    posted_attempts = [0]
    stop_chaos = threading.Event()
    kills = [0]
    rng = random.Random(7)

    def chaos():
        while not stop_chaos.is_set():
            time.sleep(rng.expovariate(1000.0 / MEAN_MS))
            if stop_chaos.is_set():
                break
            svc.kill()
            kills[0] += 1
            time.sleep(rng.uniform(0.0, 0.05))
            svc.start()

    def worker(first, step):
        for n in range(first, EVENTS, step):
            body = json.dumps({"type": "load.test", "n": n, "pad": "x" * (n % 50)}).encode()
            while True:
                try:
                    status, data = post(port, body)
                except (OSError, http.client.HTTPException):
                    time.sleep(0.01)
                    continue
                if status == 202:
                    eid = json.loads(data)["id"]
                    with acked_lock:
                        # An id the service has answered twice with different bodies would be a bug in the service.
                        assert acked.get(eid, body) == body, f"id {eid} acknowledged for two different events"
                        acked[eid] = body
                        posted_attempts[0] += 1
                    break
                time.sleep(0.01)

    chaos_thread = threading.Thread(target=chaos, daemon=True)
    chaos_thread.start()
    started = time.time()
    threads = [threading.Thread(target=worker, args=(i, THREADS)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop_chaos.set()
    chaos_thread.join()
    svc.kill()
    elapsed = time.time() - started

    data = open(os.path.join(datadir, "events.seg"), "rb").read()
    records, end = read_log(data)
    by_id = {ms: dict(pairs) for ms, pairs in records}
    ids = [ms for ms, _ in records]
    failures = []
    if ids != sorted(set(ids)):
        failures.append("ids in the log are not strictly increasing")
    if ids and ids != list(range(1, len(ids) + 1)):
        failures.append("ids in the log are not dense from 1")
    lost = [i for i in acked if i not in by_id]
    if lost:
        failures.append(f"{len(lost)} acknowledged events are not in the log, e.g. ids {sorted(lost)[:5]}")
    wrong = [i for i, body in acked.items() if i in by_id and by_id[i].get(b"event") != body]
    if wrong:
        failures.append(f"{len(wrong)} acknowledged events are in the log with a different body")
    n_values = {json.loads(by_id[i][b"event"])["n"] for i in by_id}
    missing_n = [n for n in range(EVENTS) if n not in n_values]
    if missing_n:
        failures.append(f"{len(missing_n)} events the clients finished posting are not in the log by value")
    torn = len(data) - end
    mode = "power cuts" if POWER_LOSS else "process kills only (no fsync shim: this cannot show a missing flush)"
    print(f"{EVENTS} events, {THREADS} threads, {kills[0]} kills as {mode}, {svc.starts} starts, {elapsed:.1f}s")
    if POWER_LOSS:
        print(f"unflushed bytes discarded by the {getattr(svc, 'cuts', 0)} cuts that had any: {getattr(svc, 'lost', 0)}")
    print(f"log: {len(records)} records, {len(data)} bytes ({torn} bytes of torn tail), {len(acked)} acknowledged,"
          f" {len(records) - len(acked)} stored but never acknowledged to a client")
    shutil.rmtree(datadir, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: every acknowledged event is in the log, intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
