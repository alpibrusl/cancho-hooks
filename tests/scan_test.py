#!/usr/bin/env python3
"""A dead endpoint does not stop the others (docs/production.md 0.1, docs/design.md section 31).

    python3 tests/scan_test.py build/hooks

Before this change every endpoint read the events log through one scan that stopped 1,024 events past the *slowest* endpoint's cursor, so
an endpoint more than 1,024 events behind (a dead receiver, with a retry schedule of hours) stopped delivery to every other endpoint
(`scripts/bench/stall_probe.py`: the healthy endpoint ended at cursor 1,024 of 3,000). Each endpoint reads the log forward from its own cursor
now, and is bounded only by its own window of 1,024 cells. Two endpoints, A healthy and B with nothing listening, 3,000 events:

  1. A is sent all 3,000 events, each once, and its cursor is 3,000 while B's is 0 (the gate; the old binary stops A at 1,024)
  2. B's receiver comes up: B is sent all 3,000, each once, and never an event more than a window (1,024) past what it has received in order
     (events 1 to 1,024 by their retries, 1,025 to 3,000 as the window moves up); B's cursor ends at 3,000
  3. the same with `kill -9` twelve times in the middle: nothing is lost, and what is sent twice is at most what was in flight when a kill came
     (8 attempts an endpoint); B is revived in the middle of it; the cursors end at 3,000
  4. a long-dead endpoint and three healthy ones at once, from a file of four lines: the healthy ones are each sent everything
"""
import base64
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAILS = []
TOTAL = 3000
SECRET = "whsec_" + base64.b64encode(os.urandom(24)).decode()
# Sixteen delays of 2.5 s: an event that fails is tried again every 2.5 s for 40 s before it is a dead letter, so a receiver that comes up
# within a few seconds is sent every event by its retries, and the retry of 1,024 events does not need an hour to be seen.
SCHEDULE = ",".join(["2500"] * 16)
CPU_LIMIT_S = 4


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class Receiver:
    """Records the `n` of every request in arrival order. `port` 0 takes one; `stop()` and `start()` make it refuse and accept again."""

    def __init__(self, port=0):
        self.seen = []
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                with outer.lock:
                    outer.seen.append(json.loads(body)["n"])
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.handler = H
        self.srv = None
        self.port = port or chaos.free_port()
        self.start()

    def start(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), self.handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = None

    def got(self):
        with self.lock:
            return list(self.seen)


class Service:
    # The attempt deadline is not what any check here is of: the healthy receivers answer at once and the dead one refuses at once. At 1 s a stopped machine (a stall of 1.2 s) made an
    # attempt run out of time that the receiver had read, it was made again, and "each once" found 8 repeats in 3,000 deliveries: a timeout, and the at-least-once contract, not the scan.
    def __init__(self, datadir, schedule=SCHEDULE, deadline="10000", extra=()):
        self.datadir, self.port, self.proc = datadir, chaos.free_port(), None
        self.args = ["--schedule", schedule, "--deadline-ms", deadline, *extra]

    def start(self):
        self.proc = subprocess.Popen([BIN, "--port", str(self.port), "--dir", self.datadir, "--allow-private-hosts", "1", *self.args],
                                     stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        assert self.proc.stderr.readline().strip() == b"listening"

    def cpu_seconds(self):
        """The processor time this process has used (user and system), from /proc: what the service did, not how long the machine took to let it."""
        f = open(f"/proc/{self.proc.pid}/stat").read().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read())

    def cursors(self):
        return {e["id"]: e["cursor"] for e in self.get("/endpoints")}


def conf(datadir, receivers):
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        for ident, r in receivers:
            f.write(f"{ident} 127.0.0.1 {r.port} {SECRET}\n")


def post_all(svc, total=TOTAL, threads=16):
    """Post events 1..total (the `n` in each body), `threads` at a time. Not in order across threads, so the ids are not the n's."""
    def work(first):
        for n in range(first, total + 1, threads):
            while True:
                try:
                    status, _ = chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)
                    if status == 202:
                        break
                except OSError:
                    time.sleep(0.01)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(1, threads + 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def bound_violations(seen, window=1024):
    """Events received more than a window past the prefix received in order so far: the service never starts such an attempt (the n's are the
    events' ids only up to the order the posting threads interleaved, so this is checked on the order of the log: n is read as the id)."""
    have, prefix, bad = set(), 0, []
    for n in seen:
        if n > prefix + window:
            bad.append((n, prefix))
        have.add(n)
        while prefix + 1 in have:
            prefix += 1
    return bad


def ids_of_n(svc):
    """n -> the id the service gave it, from the events log (the posting threads interleave, so the id of event n is not n)."""
    records, _ = chaos.read_log(open(os.path.join(svc.datadir, "events.seg"), "rb").read())
    out = {}
    for ms, pairs in records:
        out[json.loads(dict(pairs)[b"event"])["n"]] = ms
    return out


def stage1_and_2():
    a, b = Receiver(), Receiver()
    dead_port = b.port
    b.stop()  # nothing listens on B's port
    d = tempfile.mkdtemp(prefix="hooks-scan-")
    conf(d, [(0, a), (1, b)])
    svc = Service(d)
    svc.start()
    t0 = time.time()
    post_all(svc)
    posted = time.time() - t0
    ok = wait_for(lambda: len(a.got()) >= TOTAL, 300)      # (60 s were not enough with the processor shared with six busy loops: posting alone took 56 s)
    took = time.time() - t0
    cpu = svc.cpu_seconds()
    seen = a.got()
    check("1. A, healthy beside a dead endpoint, is sent all %d events" % TOTAL, ok and sorted(seen) == list(range(1, TOTAL + 1)),
          f"{len(seen)} requests, {len(set(seen))} distinct (posted in {posted:.1f}s)")
    check("1. ... each once", len(seen) == len(set(seen)), f"{len(seen) - len(set(seen))} repeats")
    # A timing guard, and the only thing that can see the position of an endpoint's scan: `scan_next` passes over records below the one it
    # wants, so a position that is wrong (never moved on, or too early) costs reads and is never wrong in what is sent. A mutant that never
    # moves the position reads the log from the start for every event (4.5 million reads for 3,000) and took 17.0 s here against 2.9 s.
    # The wall clock of this is the machine's as much as the service's (2.9 s on a quiet one; 32.5 s with the processor shared with six busy loops, for the same service), so
    # what is judged is the processor time the service used, which waiting for a turn does not add to: 0.2 to 0.4 s in four runs here, where the mutant, running for 17 s of wall clock on its reads, can have spent little less than that in the processor. The limit is ten times the first.
    print("     %d events to A beside a dead B: %.1f s of wall clock, %.2f s of the service's processor time" % (TOTAL, took, cpu), flush=True)
    check("1. ... the service used under %d s of processor time (%.2f s; the scan moves on, it does not read the log from its start for each event)" % (CPU_LIMIT_S, cpu), cpu < CPU_LIMIT_S, f"{cpu:.2f} s ({took:.1f} s of wall clock)")
    check("1. A's cursor is %d and B's is 0" % TOTAL, wait_for(lambda: svc.cursors() == {0: TOTAL, 1: 0}, 10), str(svc.cursors()))
    s = svc.get("/stats")
    check("1. B has failed attempts, A none (one attempt each: %d delivered)" % s["delivered"], s["delivered"] == TOTAL and s["failed"] >= 1024, str(s))

    # 2. B comes up while its events are waiting for their retries
    b.port = dead_port
    b.start()
    ok = wait_for(lambda: len(set(b.got())) >= TOTAL, 90)
    got = b.got()
    check("2. B comes up and is sent all %d events" % TOTAL, ok and sorted(set(got)) == list(range(1, TOTAL + 1)), f"{len(set(got))} distinct")
    check("2. ... each once (a refused connection is not a delivery)", len(got) == len(set(got)), f"{len(got) - len(set(got))} repeats")
    ids = ids_of_n(svc)
    in_id_order = [ids[n] for n in got]
    bad = bound_violations(in_id_order)
    check("2. ... never an event more than its window (1,024) past what it has been sent in order", not bad, str(bad[:5]))
    check("2. both cursors end at %d" % TOTAL, wait_for(lambda: svc.cursors() == {0: TOTAL, 1: TOTAL}, 15), str(svc.cursors()))
    s = svc.get("/stats")
    check("2. nothing is dead: every event was delivered to both", s["dead"] == 0 and s["delivered"] == 2 * TOTAL, str(s))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage3():
    a, b = Receiver(), Receiver()
    dead_port = b.port
    b.stop()
    d = tempfile.mkdtemp(prefix="hooks-scan-")
    conf(d, [(0, a), (1, b)])
    svc = Service(d)
    svc.start()
    post_all(svc)  # acknowledged: from here on a kill must lose none of them
    kills = 0
    t0 = time.time()
    mid = []  # how far B had got at each kill: the kills must land while B is being sent events, or the stage proves nothing
    step = TOTAL // 12  # B's progress between two kills once it is up: the kills follow what B has been sent, not the clock (a fast machine
    #                    finishes B's 3,000 events in less than the old fixed 0.6 s apart, and then no kill lands in the middle)
    while kills < 12:
        if kills < 2:
            time.sleep(0.6)
        else:
            goal, give_up = len(set(b.got())) + step, time.time() + 3.0
            while len(set(b.got())) < goal and time.time() < give_up:
                time.sleep(0.01)
        svc.kill()
        kills += 1
        mid.append(len(set(b.got())))
        if kills == 2:
            b.port = dead_port
            b.start()  # B comes up in the middle of it: its events 1..1,024 are due again within 2.5 s
        svc.start()
    ok = wait_for(lambda: len(set(a.got())) >= TOTAL and len(set(b.got())) >= TOTAL and svc.cursors() == {0: TOTAL, 1: TOTAL}, 90)
    ga, gb = a.got(), b.got()
    check("3. after %d kills in %.1fs both endpoints were sent all %d events and the cursors are at the end" % (kills, time.time() - t0, TOTAL),
          ok and sorted(set(ga)) == list(range(1, TOTAL + 1)) and sorted(set(gb)) == list(range(1, TOTAL + 1)),
          f"A {len(set(ga))}, B {len(set(gb))}, {svc.cursors()}")
    ra, rb = len(ga) - len(set(ga)), len(gb) - len(set(gb))
    print("     B had been sent %s distinct events at each kill" % mid)
    check("3. at least three kills landed while B was being sent events (neither at the start nor at the end)", sum(1 for m in mid if 0 < m < TOTAL) >= 3, str(mid))
    check("3. what was sent twice is at most what was in flight at a kill (8 an endpoint a kill): A %d, B %d, limit %d" % (ra, rb, 8 * kills),
          ra <= 8 * kills and rb <= 8 * kills, f"{ra} {rb}")
    # a last restart with everything final repeats nothing
    before = (len(a.got()), len(b.got()))
    svc.kill()
    svc.start()
    time.sleep(1.5)
    check("3. a restart when everything is final sends nothing", (len(a.got()), len(b.got())) == before and svc.cursors() == {0: TOTAL, 1: TOTAL},
          f"{before} -> {(len(a.got()), len(b.got()))} {svc.cursors()}")
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage4():
    healthy = [Receiver() for _ in range(3)]
    dead = Receiver()
    dead.stop()
    d = tempfile.mkdtemp(prefix="hooks-scan-")
    conf(d, [(0, healthy[0]), (1, dead), (2, healthy[1]), (3, healthy[2])])
    svc = Service(d)
    svc.start()
    post_all(svc, 2500)
    ok = wait_for(lambda: all(len(set(r.got())) >= 2500 for r in healthy), 90)
    check("4. three healthy endpoints beside a dead one are each sent all 2,500 events",
          ok and all(sorted(r.got()) == list(range(1, 2501)) for r in healthy), str([len(r.got()) for r in healthy]))
    check("4. their cursors are 2,500 and the dead one's is 0", wait_for(lambda: svc.cursors() == {0: 2500, 1: 0, 2: 2500, 3: 2500}, 10), str(svc.cursors()))
    svc.stop()
    for r in healthy:
        r.stop()
    shutil.rmtree(d, ignore_errors=True)


for stage in (stage1_and_2, stage3, stage4):
    stage()
print("FAILED: " + "; ".join(FAILS) if FAILS else "all scan checks passed")
sys.exit(1 if FAILS else 0)
