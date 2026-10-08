#!/usr/bin/env python3
"""The logs are bounded (docs/production.md 0.2, docs/retention.md).

    [STAGES=bounded,pins] python3 tests/retention_test.py build/hooks        stages: bounded pins dormant snapshot formats keys killmatrix compactnow stall memory

Retention is a number of seconds here (`--retention-ms`, `--window-ms`: the test knobs of docs/retention.md section 3), a segment 256 KiB and the
outcomes log's limit 64 KiB, so what takes a month in production takes seconds. Every file is read with this directory's independent reader
(chaos.py), not through the service, and every `kill -9` is a power cut (the fsync shim of tests/fsync_shim.c cuts each *.seg file back to what its
last fsync covered, plus a random part of the rest).

  bounded     4,000 events through one endpoint: segments are sealed and dropped, the outcomes log is replaced by snapshots, the disk holds a header
              and not the history, event 1 is a 410 (a tombstone) that says why, the next id is 4,001 and a restart changes nothing
  pins        an event that is not final at a paused endpoint, at a disabled one (a 410), or that has a replay waiting is never dropped, across
              snapshots and restarts; when the pin goes, the segments go
  snapshot    what the outcomes log replays to is the same after it is replaced by a snapshot: attempts so far, final events above the cursor
  formats     a header in each file; an unknown version is a refusal that writes nothing; a format 1 directory is read and upgraded; a hole, a
              broken chain, leftovers of a roll or a drop
  keys        an idempotency key expires with its window and with its event; a key inside the window deduplicates across compaction and restart;
              the limit is a setting; `cron:` is the schedules' own
  killmatrix  [KM_STEPS=5,6 QUICK=1] `kill -9` at each of the 19 steps of a compaction (both logs; in the loop and by `compact-now`; with everything final and with
              events that are not): nothing lost, nothing sent again, ids never reused
  compactnow  the operator's one-off, and its lock
  stall       how long the loop is held by a step, with 62 endpoints and full windows
  memory      the resident size is flat while events are posted and delivered: a function that loses its memory on every call (design.md section
              38.5) is a leak per event or per delivery and shows here within seconds
"""
import base64
import fcntl
import hashlib
import http.server
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
from loadmeter import LoadMeter  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
WANT = [a for a in os.environ.get("STAGES", "").split(",") if a]
SHIM = os.path.join(os.path.dirname(BIN), "fsync_shim.so")
FAILS = []
SECRET = "whsec_" + base64.b64encode(os.urandom(24)).decode()
RNG = random.Random(11)
SCRATCH = os.environ.get("RETENTION_TMP") or tempfile.gettempdir()


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


def wait_for(cond, secs, every=0.05):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            pass
        time.sleep(every)
    return False


def tmpdir(prefix):
    return tempfile.mkdtemp(prefix="hooks-ret-" + prefix + "-", dir=SCRATCH)


# ---- a receiver ----------------------------------------------------------------------------------------------------------------------------

class Receiver:
    """Counts the requests per `n`; `status` is what it answers (or `answer(n)`); `stop()` and `start()` make it refuse and accept again."""

    def __init__(self, port=0, status=204):
        self.count = {}
        self.order = []
        self.lock = threading.Lock()
        self.status = status
        self.answer = None
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                n = json.loads(body)["n"]
                with outer.lock:
                    outer.count[n] = outer.count.get(n, 0) + 1
                    outer.order.append(n)
                code = outer.answer(n) if outer.answer else outer.status
                self.send_response(code)
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
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def total(self):
        with self.lock:
            return len(self.order)

    def distinct(self):
        with self.lock:
            return len(self.count)

    def repeats(self):
        with self.lock:
            return sum(c - 1 for c in self.count.values())

    def reset(self):
        with self.lock:
            self.count, self.order = {}, []


def conf(datadir, receivers):
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        for ident, r in receivers:
            f.write(f"{ident} 127.0.0.1 {r.port} {SECRET}\n")


# ---- the service -----------------------------------------------------------------------------------------------------------------------------

class Svc:
    def __init__(self, datadir, args=(), shim=True):
        self.datadir, self.port, self.proc, self.args, self.shim = datadir, chaos.free_port(), None, list(args), shim
        self.lines = []

    def command(self):
        return [BIN, "--port", str(self.port), "--dir", self.datadir, "--allow-private-hosts", "1", *self.args]

    def env(self):
        env = dict(os.environ)
        if self.shim and os.path.exists(SHIM):
            env["LD_PRELOAD"] = SHIM
        self.preloaded = env.get("LD_PRELOAD") == SHIM  # what `kill` asks before a power cut
        return env

    def start(self):
        self.proc = subprocess.Popen(self.command(), stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=self.env())
        while True:
            line = self.proc.stderr.readline().strip()
            self.lines.append(line)
            if line == b"listening":
                return True
            if not line and self.proc.poll() is not None:
                return False
            if line.startswith(b"hooks: ") and b"events log" not in line and b"delivery.seg" not in line:
                return False

    def run_once(self, timeout=60):
        """For `--compact-now 1` and the refusals: run to the end. Answers (exit status, stderr text)."""
        p = subprocess.run(self.command(), stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=self.env(), timeout=timeout)
        return p.returncode, p.stderr.decode()

    def kill(self, power=True):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
        if power:
            power_cut_needs_shim(getattr(self, "preloaded", False))
            power_cut(self.datadir)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()

    def get(self, path, raw=False):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            body = r.read()
            return body if raw else json.loads(body)

    def status_of(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stats(self):
        return self.get("/stats")

    def endpoints(self):
        return {e["id"]: e for e in self.get("/endpoints")}

    def cursors(self):
        return {i: e["cursor"] for i, e in self.endpoints().items()}

    def post(self, body, key=None, timeout=10):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/events", data=json.dumps(body).encode(), method="POST")
        if key:
            req.add_header("Idempotency-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def killpoint(self):
        try:
            return int(open(os.path.join(self.datadir, "killpoint")).read())
        except (OSError, ValueError):
            return None


def cut_file(path):
    side = path + ".synced"
    synced = struct.unpack("<q", open(side, "rb").read(8))[0] if os.path.exists(side) else 0
    size = os.path.getsize(path)
    if size <= synced:
        return
    keep = synced + RNG.randint(0, size - synced)
    with open(path, "r+b") as f:
        f.truncate(keep)
        if keep > synced and RNG.random() < 0.5:
            start = max(synced, keep - RNG.randint(1, 64))
            f.seek(start)
            f.write(bytes(keep - start))


def power_cut_needs_shim(preloaded):
    """A power cut cuts each file back to what its last fsync covered, which only the shim records (`<file>.synced`). Without it there is no
    sidecar, every file reads as never flushed, and the cut takes flushed and acknowledged records too: the restart then refuses the logs (status 18,
    `events.seg` shorter than what `delivery.seg` refers to) for a reason the service did not cause. So the harness refuses a power cut of a service
    the shim was not loaded into. (The pure build's binary is in `pure/build`, and its shim was not there until `scripts/build.sh --pure` built it.)"""
    if not preloaded:
        raise SystemExit(f"retention_test: a power cut needs the fsync shim loaded into the service, and {SHIM} was not "
                         "(scripts/build.sh builds it beside build/hooks, and scripts/build.sh --pure beside pure/build/hooks-pure)")


def power_cut(datadir):
    """A power cut: every *.seg file (a snapshot being made, and the manifest events.first) is cut back to what its last fsync covered, plus a random part of the rest."""
    for name in sorted(os.listdir(datadir)):
        if name.endswith((".seg", ".seg.tmp", ".first", ".first.tmp")):
            cut_file(os.path.join(datadir, name))


def post_many(svc, first, last, pad=250, threads=8):
    """Events n = first..last, each `{"type": "t", "n": n, "pad": ...}`. Answers when all are acknowledged."""
    def work(start):
        c = None
        for n in range(start, last + 1, threads):
            body = json.dumps({"type": "t", "n": n, "pad": "x" * pad}).encode()
            while True:
                try:
                    status, _ = chaos.post(svc.port, body, timeout=10.0)
                    if status == 202:
                        break
                except OSError:
                    time.sleep(0.01)

    ts = [threading.Thread(target=work, args=(first + i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def files_of(d):
    return sorted(n for n in os.listdir(d) if not n.endswith(".synced"))


def events_bytes(d):
    return sum(os.path.getsize(os.path.join(d, n)) for n in files_of(d) if n.startswith("events") and n.endswith(".seg"))


def tree_hash(d):
    h = hashlib.sha256()
    for n in sorted(os.listdir(d)):
        h.update(n.encode())
        p = os.path.join(d, n)
        if os.path.isfile(p):
            h.update(open(p, "rb").read())
    return h.hexdigest()


# ---- a record, written here -----------------------------------------------------------------------------------------------------------------

def record(ms, pairs, seq=0):
    body = struct.pack("<QQI", ms, seq, len(pairs)) + b"".join(struct.pack("<I", len(k)) + k + struct.pack("<I", len(v)) + v for k, v in pairs)
    return struct.pack("<II", 4 + len(body), chaos.crc32c(body)) + body


def event_header(k, base, first_id, created, version=2):
    return record(0, [(b"format", b"lexsys-hooks events %d" % version), (b"segment", struct.pack("<4Q", k, base, first_id, created)),
                      (b"created", struct.pack("<Q", created))])


def outcome(seq, kind, e, ev, att, nxt):
    return record(seq, [(b"o", struct.pack("<5q", kind, e, ev, att, nxt))])


def outcomes_of(d, headers=False):
    """[(kind, endpoint, event, attempts, next_at)] of delivery.seg."""
    data = open(os.path.join(d, "delivery.seg"), "rb").read()
    recs, _ = chaos.read_log(data, headers=headers)
    return [struct.unpack("<5q", dict(p)[b"o"]) for _, p in recs]


NOW = lambda: int(time.time() * 1000)  # noqa: E731


# =========================================================================================================================================
# bounded
# =========================================================================================================================================

KNOBS = ["--segment-bytes", "262144", "--retention-ms", "1500", "--window-ms", "500", "--delivery-log-bytes", "65536", "--schedule", "100,100"]


def stage_bounded():
    total = 4000
    r = Receiver()
    d = tmpdir("bounded")
    conf(d, [(0, r)])
    svc = Svc(d, KNOBS)
    svc.start()
    t0 = time.time()
    post_many(svc, 1, total)
    ok = wait_for(lambda: svc.cursors() == {0: total}, 60)
    check("bounded: %d events through one endpoint, all delivered once (%.1fs)" % (total, time.time() - t0), ok and r.distinct() == total and r.repeats() == 0,
          f"{r.distinct()} distinct, {r.repeats()} repeats, {svc.cursors()}")
    peak = events_bytes(d)
    # the history is gone from the disk once the retention has passed: only the header of the active segment is left
    ok = wait_for(lambda: svc.stats()["events_first_id"] == total + 1 and events_bytes(d) < 200, 40)
    s = svc.stats()
    check("bounded: after the retention nothing but a header is on disk (events: %d bytes at the most, %d now)" % (peak, events_bytes(d)), ok, str(files_of(d)))
    check("bounded: segments were sealed and dropped (%d sealed, %d dropped, %d events)" % (s["segments_sealed"], s["segments_dropped"], s["events_dropped"]),
          s["segments_sealed"] >= 4 and s["segments_dropped"] >= 4 and s["events_dropped"] == total, str(s))
    names = files_of(d)
    k0 = int(open(os.path.join(d, "events.first")).read())
    check("bounded: events.first names the one segment that is left and no segment below it exists", names.count("events.seg") == 0 and f"events-{k0}.seg" in names
          and not any(n.startswith("events-") and n.endswith(".seg") and int(n[7:-4]) < k0 for n in names), str(names))
    check("bounded: the outcomes log was replaced by snapshots (%d) and is small (%d bytes against %d outcomes of 77 bytes)" % (s["snapshots"], os.path.getsize(os.path.join(d, "delivery.seg")), total),
          s["snapshots"] >= 3 and os.path.getsize(os.path.join(d, "delivery.seg")) < 80000, str(s))
    code, body = svc.status_of("/events/1")
    check("bounded: event 1 is a 410 that says retention dropped it (a tombstone: the id was an event)", code == 410 and b"retention" in body, f"{code} {body}")
    code, body = svc.status_of(f"/events/{total}")
    check("bounded: ... and so is the last", code == 410, f"{code}")
    code, body = svc.status_of(f"/events/{total + 50}")
    check("bounded: an id that was never given is a 404", code == 404, f"{code} {body}")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/1/replay", data=b"", method="POST"), timeout=10) as rr:
            code, body = rr.status, rr.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    check("bounded: a replay of a dropped event is a 410 that says it cannot be replayed", code == 410 and b"replayed" in body, f"{code} {body}")
    status, ans = svc.post({"type": "t", "n": total + 1, "pad": "y"})
    check("bounded: ids are not reused: the next event is %d" % (total + 1), status == 202 and ans["id"] == total + 1, f"{status} {ans}")
    ok = wait_for(lambda: r.distinct() == total + 1, 10)
    check("bounded: ... and it is delivered", ok, str(r.distinct()))
    time.sleep(0.5)
    cur = svc.cursors()
    svc.kill()
    r.reset()
    svc.start()
    time.sleep(1.0)
    s2 = svc.stats()
    check("bounded: after a power cut and a restart the cursor, the ids and the files are as they were, and nothing is sent again",
          svc.cursors() == cur and s2["events_last_id"] == total + 1 and r.total() == 0, f"{cur} {svc.cursors()} {s2['events_last_id']} {r.total()}")
    status, ans = svc.post({"type": "t", "n": total + 2, "pad": "z"})
    check("bounded: ... and the next id is %d" % (total + 2), status == 202 and ans["id"] == total + 2, f"{status} {ans}")
    svc.stop()
    r.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# pins
# =========================================================================================================================================

PIN_KNOBS = ["--segment-bytes", "262144", "--retention-ms", "1500", "--window-ms", "500", "--delivery-log-bytes", "65536"]


def settle(secs):
    """Let the retention pass several times over (age is max(retention, window) = 1.5 s)."""
    time.sleep(secs)


def stage_pins():
    total = 1500
    # --- a paused endpoint
    a, b = Receiver(), Receiver()
    b.stop()
    d = tmpdir("paused")
    conf(d, [(0, a), (1, b)])
    # a run of failures that began six days ago, as the breaker's own record (tests/breaker_test.py): the first failed attempt pauses endpoint 1
    with open(os.path.join(d, "delivery.seg"), "wb") as f:
        f.write(outcome(1, 12, 1, 0, 0, NOW() - 6 * 86400 * 1000))
    svc = Svc(d, PIN_KNOBS + ["--schedule", ",".join(["100"] * 16)])
    svc.start()
    post_many(svc, 1, total)
    check("pins: the healthy endpoint is sent everything", wait_for(lambda: svc.endpoints()[0]["cursor"] == total, 40) and a.repeats() == 0, str(svc.cursors()))
    check("pins: the other is paused by the breaker", wait_for(lambda: svc.endpoints()[1]["paused"], 20), str(svc.endpoints()))
    settle(5)
    s = svc.stats()
    check("pins: paused endpoint: its events are final at one endpoint and not at the other, and none is dropped though the retention has passed three times (%d segments, none dropped)" % s["events_segments"],
          s["events_first_id"] == 1 and s["segments_dropped"] == 0 and s["events_segments"] >= 2 and s["snapshots"] >= 1, str(s))
    check("pins: ... and the paused state survived a snapshot of the outcomes log (%d made)" % s["snapshots"], svc.endpoints()[1]["paused"] and svc.endpoints()[1]["disabled"], str(svc.endpoints()))
    svc.kill()
    svc.start()
    check("pins: ... and a restart", svc.endpoints()[1]["paused"] and svc.stats()["events_first_id"] == 1, str(svc.endpoints()))
    b.start()
    req = urllib.request.Request(f"http://127.0.0.1:{svc.port}/endpoints/1/enable", data=b"", method="POST")
    urllib.request.urlopen(req, timeout=5).read()
    ok = wait_for(lambda: b.distinct() == total and svc.cursors().get(1) == total, 60)
    check("pins: enabled, the paused endpoint is sent every one of the %d events, each once" % total, ok and b.repeats() == 0, f"{b.distinct()} {b.repeats()} {svc.cursors()}")
    ok = wait_for(lambda: svc.stats()["events_first_id"] == total + 1, 30)
    check("pins: ... and then, with nothing left owed, the segments go", ok, str(svc.stats()))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- a disabled endpoint (a 410)
    a, b = Receiver(), Receiver(status=410)
    d = tmpdir("gone")
    conf(d, [(0, a), (1, b)])
    svc = Svc(d, PIN_KNOBS + ["--schedule", ",".join(["100"] * 16)])
    svc.start()
    post_many(svc, 1, total)
    check("pins: the first endpoint is sent everything", wait_for(lambda: svc.endpoints()[0]["cursor"] == total, 40), str(svc.cursors()))
    check("pins: the other answered 410 and is disabled", wait_for(lambda: svc.endpoints()[1]["disabled"], 20), str(svc.endpoints()))
    settle(5)
    s = svc.stats()
    check("pins: disabled endpoint: its events wait, none is dropped (%d segments)" % s["events_segments"], s["events_first_id"] == 1 and s["segments_dropped"] == 0 and s["events_segments"] >= 2, str(s))
    svc.kill()
    svc.start()
    check("pins: ... across a restart", svc.endpoints()[1]["disabled"] and svc.stats()["events_first_id"] == 1, str(svc.endpoints()))
    b.status = 204
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{svc.port}/endpoints/1/enable", data=b"", method="POST"), timeout=5).read()
    ok = wait_for(lambda: svc.cursors().get(1) == total, 60)
    check("pins: enabled, it is sent the events that waited (all but the first, which was dead at once)", ok, str(svc.cursors()))
    check("pins: ... and then the segments go", wait_for(lambda: svc.stats()["events_first_id"] == total + 1, 30), str(svc.stats()))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- a replay that is waiting
    a = Receiver()
    broken3 = [True]
    # event 3 is delivered the first time and refused afterwards (its replay), until the flag is cleared
    a.answer = lambda n: 204 if (n != 3 or a.count[3] <= 1 or not broken3[0]) else 500
    d = tmpdir("replay")
    conf(d, [(0, a)])
    svc = Svc(d, PIN_KNOBS + ["--schedule", ",".join(["1500"] * 16)])
    svc.start()
    post_many(svc, 1, 5, threads=1)       # in order: the id of an event is its n, which the receiver's answer goes by
    check("pins: replay: five events delivered", wait_for(lambda: svc.cursors() == {0: 5}, 20), str(svc.cursors()))
    code = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/3/replay", data=b"", method="POST"), timeout=5).status
    check("pins: replay of event 3 is accepted and fails (the receiver refuses it the second time)", code == 202 and wait_for(lambda: svc.stats()["replays"] == 1 and a.count.get(3, 0) >= 2, 8), str(svc.stats()))
    post_many(svc, 6, total)
    check("pins: replay: the rest is delivered", wait_for(lambda: svc.cursors() == {0: total}, 40), str(svc.cursors()))
    settle(6)
    s = svc.stats()
    check("pins: an event with a replay waiting is not dropped, nor any after it, though all is final and old (first id %d, %d segments, none dropped)" % (s["events_first_id"], s["events_segments"]),
          s["events_first_id"] == 1 and s["segments_dropped"] == 0 and s["replays"] == 1 and s["events_segments"] >= 2, str(s))
    svc.kill()
    svc.start()
    check("pins: ... across a power cut and a restart", svc.stats()["replays"] == 1 and svc.stats()["events_first_id"] == 1, str(svc.stats()))
    broken3[0] = False
    check("pins: the replay is delivered at its next retry", wait_for(lambda: svc.stats()["replays"] == 0, 20), str(svc.stats()))
    check("pins: ... and then the segments go", wait_for(lambda: svc.stats()["events_first_id"] == total + 1, 30), str(svc.stats()))
    svc.stop()
    a.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage_dormant():
    # An endpoint whose row is gone does not hold events back (it is dormant, not owed anything); if its row comes back after the events it missed
    # were dropped, it starts at the oldest that is left, and the start says so.
    a, b = Receiver(), Receiver()
    d = tmpdir("dormant")
    conf(d, [(0, a), (1, b)])
    knobs = KNOBS
    svc = Svc(d, knobs)
    svc.start()
    post_many(svc, 1, 300)
    check("dormant: two endpoints are sent 300 events", wait_for(lambda: svc.cursors() == {0: 300, 1: 300}, 30), str(svc.cursors()))
    svc.kill()
    conf(d, [(0, a)])
    svc = Svc(d, knobs)
    svc.start()
    post_many(svc, 301, 900)
    check("dormant: endpoint 1's row is gone; endpoint 0 is sent 600 more", wait_for(lambda: svc.cursors() == {0: 900}, 30), str(svc.cursors()))
    check("dormant: the dormant endpoint holds nothing back: every event is dropped after the retention", wait_for(lambda: svc.stats()["events_first_id"] == 901, 30), str(svc.stats()))
    svc.kill()
    conf(d, [(0, a), (1, b)])
    b.reset()
    svc = Svc(d, knobs)
    ok = svc.start()
    said = [l for l in svc.lines if b"away while events were dropped" in l]
    check("dormant: its row comes back after the events it missed were dropped: it starts at the oldest that is left, and the start says so", ok and said and b"600" in said[0], str(svc.lines))
    check("dormant: ... its cursor is 900, not 300 (nothing it missed can be sent)", svc.cursors() == {0: 900, 1: 900}, str(svc.cursors()))
    svc.post({"type": "t", "n": 901})
    check("dormant: ... and the next event is sent to both", wait_for(lambda: b.distinct() == 1 and svc.cursors() == {0: 901, 1: 901}, 10), f"{sorted(b.count)} {svc.cursors()}")
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# snapshot
# =========================================================================================================================================

def stage_snapshot():
    a = Receiver()
    # event 2 fails (500) and every other is delivered: cells above the cursor that are final, one that is waiting for its third attempt
    a.answer = lambda n: 500 if n == 2 else 204
    d = tmpdir("snap")
    conf(d, [(0, a)])
    sched = "400,400,400,400"
    svc = Svc(d, ["--schedule", sched])
    svc.start()
    for n in range(1, 7):
        svc.post({"type": "t", "n": n})
    ok = wait_for(lambda: a.count.get(2, 0) >= 2 and all(a.count.get(n) == 1 for n in (1, 3, 4, 5, 6)), 10)
    check("snapshot: events 3 to 6 are delivered while 2 has failed twice", ok, str(a.count))
    time.sleep(0.1)
    # kill between the 2nd and the 3rd attempt (400 ms apart): the 3rd is not due yet
    svc.kill()
    seen2 = a.count.get(2, 0)
    before = outcomes_of(d)
    svc2 = Svc(d, ["--schedule", sched, "--compact-now", "1"])
    code, text = svc2.run_once()
    check("snapshot: compact-now exits 0 and says what it did", code == 0 and "compacted" in text, f"{code} {text}")
    after = outcomes_of(d)
    head = outcomes_of(d, headers=True)[0]
    check("snapshot: the outcomes log now begins with its header (kind 15, format 2)", head[0] == 15 and head[2] == 2, str(head))
    kinds = sorted((k, e, ev, att) for k, e, ev, att, nxt in after)
    check("snapshot: and holds a slot, four final events above the cursor (written as delivered: final is all a window needs, and a dead letter is the table's kind 17), the failing one with its attempts, and its run of failures",
          kinds == sorted([(10, 0, 0, 1), (1, 0, 3, 1), (1, 0, 4, 1), (1, 0, 5, 1), (1, 0, 6, 1), (2, 0, 2, seen2), (12, 0, 0, 0)]), f"{kinds} (was {len(before)} records)")
    a.reset()
    svc3 = Svc(d, ["--schedule", sched])
    svc3.start()
    ok = wait_for(lambda: svc3.stats()["dead"] >= 1, 10)
    time.sleep(0.5)
    check("snapshot: after the replacement the failing event is tried until its schedule is out (5 attempts in all, %d before): %d more, then a dead letter" % (seen2, a.count.get(2, 0)),
          ok and a.count.get(2, 0) == 5 - seen2, str(a.count))
    check("snapshot: ... and the final events above the cursor are not sent again", all(n not in a.count for n in (1, 3, 4, 5, 6)), str(a.count))
    check("snapshot: ... and the cursor moves over them when the failing one is dead", svc3.cursors() == {0: 6}, str(svc3.cursors()))
    svc3.stop()
    a.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# formats
# =========================================================================================================================================

def stage_formats():
    # a new directory: a header in each file
    d = tmpdir("fmt")
    svc = Svc(d)
    svc.start()
    svc.post({"type": "t"})
    svc.stop()
    ev = open(os.path.join(d, "events.seg"), "rb").read()
    recs, _ = chaos.read_log(ev, headers=True)
    check("formats: a new events.seg begins with a header: id 0, three pairs, format 2", recs[0][0] == 0 and [k for k, _ in recs[0][1]] == [b"format", b"segment", b"created"]
          and recs[0][1][0][1] == b"lexsys-hooks events 2" and len(recs) == 2, str(recs[0]))
    k, base, first, created = struct.unpack("<4Q", recs[0][1][1][1])
    check("formats: ... saying it is segment 0, at logical offset 0, whose first event is 1", (k, base, first) == (0, 0, 1) and abs(created - NOW()) < 60000, str((k, base, first, created)))
    o = outcomes_of(d, headers=True)
    check("formats: a new delivery.seg begins with kind 15, format 2, and no slot", o[0][0] == 15 and o[0][1] == 62 and o[0][2] == 2, str(o))
    shutil.rmtree(d, ignore_errors=True)

    # an unknown version is refused, and nothing is written
    for which, status, text in (("events", 40, "events log"), ("delivery", 41, "delivery.seg")):
        d = tmpdir("fmt-v3")
        with open(os.path.join(d, "events.seg"), "wb") as f:
            f.write(event_header(0, 0, 1, NOW(), version=3 if which == "events" else 2) + record(1, [(b"event", b'{"type":"t"}')]))
        with open(os.path.join(d, "delivery.seg"), "wb") as f:
            f.write(outcome(1, 15, 62, 3 if which == "delivery" else 2, 0, NOW()))
        r = Receiver()
        conf(d, [(0, r)])
        before = tree_hash(d)
        code, err = Svc(d, shim=False).run_once(timeout=15)
        check("formats: a %s from format 3 is refused: status %d, a message, and the directory is as it was" % (which, status),
              code == status and "format" in err and tree_hash(d) == before, f"{code} {err!r} {before == tree_hash(d)}")
        r.stop()
        shutil.rmtree(d, ignore_errors=True)
    # and an unreadable one
    d = tmpdir("fmt-junk")
    with open(os.path.join(d, "events.seg"), "wb") as f:
        f.write(record(0, [(b"format", b"something else entirely"), (b"segment", b"x" * 32), (b"created", b"y" * 8)]))
    code, err = Svc(d, shim=False).run_once(timeout=15)
    check("formats: a header that is not this program's is a refusal too (status 42)", code == 42, f"{code} {err!r}")
    shutil.rmtree(d, ignore_errors=True)

    # format 1: no headers at all. Read as they are, upgraded when the log is compacted.
    r = Receiver()
    d = tmpdir("fmt-v1")
    conf(d, [(0, r)])
    with open(os.path.join(d, "events.seg"), "wb") as f:
        for i in range(1, 6):
            f.write(record(i, [(b"event", json.dumps({"type": "t", "n": i}).encode())]))
    with open(os.path.join(d, "delivery.seg"), "wb") as f:
        for i in range(1, 4):
            f.write(outcome(i, 1, 0, i, 1, 0))
    before = tree_hash(d)
    svc = Svc(d)
    svc.start()
    ok = wait_for(lambda: r.distinct() == 2, 10)
    check("formats: format 1 files are read as they were: the cursor is 3, so events 4 and 5 are sent and no other", ok and sorted(r.count) == [4, 5], str(r.count))
    check("formats: ... and event 3 is there, and the next id is 6", svc.get("/events/3")["event"]["n"] == 3 and svc.post({"type": "t", "n": 6})[1]["id"] == 6, "")
    wait_for(lambda: svc.cursors() == {0: 6}, 10)
    time.sleep(0.3)
    svc.stop()
    d1 = outcomes_of(d, headers=True)
    check("formats: ... and nothing was rewritten at the start: the outcomes log has no header yet", d1[0][0] != 15, str(d1[:2]))
    code, text = Svc(d, ["--compact-now", "1"]).run_once()
    o = outcomes_of(d, headers=True)
    check("formats: compact-now upgrades it: the snapshot has the header", code == 0 and o[0][0] == 15 and o[0][2] == 2, f"{code} {text} {o[:2]}")
    svc = Svc(d)
    svc.start()
    r.reset()
    ids = [svc.get(f"/events/{i}")["event"]["n"] for i in range(1, 7)]
    check("formats: ... and events 1 to 6 are all still there across the format 1 segment and the new ones", ids == [1, 2, 3, 4, 5, 6], str(ids))
    check("formats: ... and the cursor is 6 and nothing is sent again", wait_for(lambda: svc.cursors() == {0: 6}, 10) and r.total() == 0, str(svc.cursors()))
    recs, _ = chaos.read_events(d)
    check("formats: ... and the independent reader sees ids 1 to 6 across the segments", [i for i, _ in recs] == [1, 2, 3, 4, 5, 6], str([i for i, _ in recs]))
    svc.stop()
    r.stop()
    shutil.rmtree(d, ignore_errors=True)

    # holes and breaks
    def segs(d, spec):
        """spec: [(k, base, first_id, n_events)]"""
        for k, base, first, n in spec:
            name = "events.seg" if k == 0 else f"events-{k}.seg"
            data = event_header(k, base, first, NOW()) if (k > 0 or True) else b""
            for i in range(n):
                data += record(first + i, [(b"event", b'{"type":"t"}')])
            open(os.path.join(d, name), "wb").write(data)

    size = len(record(1, [(b"event", b'{"type":"t"}')]))
    d = tmpdir("hole")
    segs(d, [(0, 0, 1, 3), (1, 3 * size, 4, 3), (3, 9 * size, 10, 3)])
    code, err = Svc(d, shim=False).run_once(timeout=15)
    check("formats: a hole in the segments (0, 1, 3) is a refusal, status 42", code == 42, f"{code} {err!r}")
    shutil.rmtree(d, ignore_errors=True)
    d = tmpdir("chain")
    segs(d, [(0, 0, 1, 3), (1, 4 * size, 4, 3)])
    code, err = Svc(d, shim=False).run_once(timeout=15)
    check("formats: a segment that does not begin where the one before it ends is a refusal, status 42", code == 42, f"{code} {err!r}")
    shutil.rmtree(d, ignore_errors=True)
    d = tmpdir("manifest")
    segs(d, [(2, 0, 1, 3)])
    open(os.path.join(d, "events.first"), "w").write("1\n")
    code, err = Svc(d, shim=False).run_once(timeout=15)
    check("formats: a manifest that names a segment that is not there is a refusal, status 42", code == 42, f"{code} {err!r}")
    shutil.rmtree(d, ignore_errors=True)

    # what a roll or a drop that was cut leaves is repaired
    d = tmpdir("leftovers")
    segs(d, [(2, 7 * size, 8, 3)])
    open(os.path.join(d, "events.first"), "w").write("2\n")
    open(os.path.join(d, "events-1.seg"), "wb").write(b"junk" * 10)               # a drop that was cut: below the manifest
    open(os.path.join(d, "events-3.seg"), "wb").write(event_header(3, 10 * size, 11, NOW())[:50])      # a roll cut in the header
    open(os.path.join(d, "events.first.tmp"), "w").write("3\n")
    open(os.path.join(d, "delivery.seg.tmp"), "wb").write(b"half a snapshot")
    svc = Svc(d)
    ok = svc.start()
    st = svc.stats() if ok else {}
    left = files_of(d)
    check("formats: leftovers of a cut drop and a cut roll are removed at the start, and the log is as it was", ok and st.get("events_first_id") == 8 and st.get("events_last_id") == 10
          and "events-1.seg" not in left and "events-3.seg" not in left and "events.first.tmp" not in left and "delivery.seg.tmp" not in left, f"{st} {left}")
    check("formats: ... and the next event is 11", svc.post({"type": "t"})[1].get("id") == 11, "")
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)

    # a segment sealed with the header only, then an event: the cut roll's header may be whole
    d = tmpdir("emptyroll")
    segs(d, [(0, 0, 1, 3), (1, 3 * size, 4, 0)])
    svc = Svc(d)
    ok = svc.start()
    check("formats: a roll cut after its header was durable leaves an empty segment that is the active one: ids go on from 3", ok and svc.post({"type": "t"})[1].get("id") == 4, "")
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# keys
# =========================================================================================================================================

def stage_keys():
    # --- a key expires with its window, and is a new event after it
    d = tmpdir("keys")
    svc = Svc(d, ["--window-ms", "1500", "--retention-ms", "1500", "--segment-bytes", "262144"])
    svc.start()
    s1, a1 = svc.post({"type": "t", "n": 1}, key="k1")
    s2, a2 = svc.post({"type": "t", "n": 1}, key="k1")
    check("keys: a key inside its window answers the first event's id and stores nothing", s1 == s2 == 202 and a1 == a2 and svc.stats()["events_last_id"] == 1, f"{a1} {a2}")
    check("keys: ... and is held (keys 1)", svc.stats()["keys"] == 1, str(svc.stats()))
    time.sleep(2.3)
    s3, a3 = svc.post({"type": "t", "n": 1}, key="k1")
    check("keys: after its window the same key and the same event are a NEW event (documented)", s3 == 202 and a3["id"] == 2, f"{s3} {a3}")
    time.sleep(0.5)
    check("keys: ... and the index still holds one key for it, not two", svc.stats()["keys"] == 1, str(svc.stats()))
    # the eviction: the key is gone from the index with its window, whether or not anyone posts it again
    svc.post({"type": "t", "n": 9}, key="k9")
    ok = wait_for(lambda: svc.stats()["keys"] == 0, 6)
    check("keys: a key nobody posts again leaves the index when its window has passed", ok, str(svc.stats()))
    s, a = svc.post({"type": "t"}, key="cron:1:5")
    check("keys: a client key that begins with cron: is refused (the schedules' namespace)", s == 400, f"{s} {a}")
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- a key inside the window deduplicates across a compaction and a restart (the events stay: retention is longer than the window)
    d = tmpdir("keys2")
    knobs = ["--window-ms", "600000", "--retention-ms", "700000", "--segment-bytes", "262144"]
    svc = Svc(d, knobs)
    svc.start()
    ids = {}
    for i in range(40):
        s, a = svc.post({"type": "t", "n": i, "pad": "p" * 300}, key=f"key-{i}")
        ids[i] = a["id"]
    svc.kill()
    code, text = Svc(d, knobs + ["--compact-now", "1"]).run_once()
    check("keys: compact-now seals the segment, drops nothing (the events are inside the window) and replaces the outcomes log", code == 0 and "dropped 0 segments" in text, f"{code} {text}")
    svc = Svc(d, knobs)
    svc.start()
    ok = all(svc.post({"type": "t", "n": i, "pad": "p" * 300}, key=f"key-{i}") == (202, {"id": ids[i]}) for i in range(40))
    check("keys: the 40 keys inside the window answer the same ids after the compaction and a power cut and restart", ok and svc.stats()["events_last_id"] == 40, str(svc.stats()))
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- a key whose event retention dropped is forgotten with it
    d = tmpdir("keys3")
    knobs = ["--window-ms", "1000", "--retention-ms", "1000", "--segment-bytes", "262144"]
    svc = Svc(d, knobs)
    svc.start()
    s, a = svc.post({"type": "t", "n": 1}, key="kk")
    time.sleep(0.3)
    ok = wait_for(lambda: svc.stats()["events_first_id"] == 2, 10)
    check("keys: the event under key kk is dropped by retention", ok, str(svc.stats()))
    s, b = svc.post({"type": "t", "n": 1}, key="kk")
    check("keys: ... and the same key and event are then a new event, id %d" % b["id"], s == 202 and b["id"] == 2, f"{s} {b}")
    svc.kill()
    time.sleep(2.2)
    svc.start()
    check("keys: a key that expired while the service was down is not read back at the start (keys 0)", svc.stats()["keys"] == 0, str(svc.stats()))
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- the limit is a setting; an index that is full of fresh keys refuses, and gives room back as the keys expire
    d = tmpdir("keys4")
    svc = Svc(d, ["--idem-keys", "200", "--window-ms", "2000", "--retention-ms", "2000"])
    svc.start()
    codes = [svc.post({"type": "t", "n": i}, key=f"f{i}")[0] for i in range(201)]
    check("keys: --idem-keys 200: the 200 keys are taken and the 201st is a 507", codes[:200] == [202] * 200 and codes[200] == 507, str(codes[198:]))
    check("keys: ... and a repeat of one of them still answers", svc.post({"type": "t", "n": 5}, key="f5")[0] == 202 and svc.stats()["events_last_id"] == 200, "")
    time.sleep(2.6)
    s, a = svc.post({"type": "t", "n": 1000}, key="late")
    check("keys: ... and once the window has passed the room is back: the next key is taken", s == 202, f"{s} {a}")
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)

    # --- more than the old 65,536 (the old limit: docs/design.md section 17)
    d = tmpdir("keys5")
    n = 66000
    svc = Svc(d, ["--idem-keys", "70000", "--window-ms", "86400000"], shim=False)
    svc.start()
    errors = []

    def work(first):
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", svc.port, timeout=20)
        for i in range(first, n, 32):
            c.request("POST", "/events", body=json.dumps({"type": "t"}).encode(), headers={"Idempotency-Key": f"bulk-{i}"})   # as Svc.post writes it
            r = c.getresponse()
            r.read()
            if r.status != 202:
                errors.append((i, r.status))

    t0 = time.time()
    ts = [threading.Thread(target=work, args=(i,)) for i in range(32)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    s = svc.stats()
    check("keys: %d keys are held, past the old limit of 65,536 (%.1fs, %d errors)" % (s["keys"], time.time() - t0, len(errors)), not errors and s["keys"] == n, str(s))
    svc.kill(power=False)
    t0 = time.time()
    svc.start()
    took = time.time() - t0
    check("keys: ... and a restart rebuilds all %d of them (%.2fs)" % (n, took), svc.stats()["keys"] == n, str(svc.stats()))
    first = svc.post({"type": "t"}, key="bulk-7")
    check("keys: ... and a repeat answers the id it had and stores nothing", first[0] == 202 and svc.post({"type": "t"}, key="bulk-7") == first and svc.stats()["events_last_id"] == n, str(first))
    svc.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# killmatrix
# =========================================================================================================================================

STEPS = {1: "roll: flushed", 2: "roll: new file created", 3: "roll: header written", 4: "roll: header synced", 5: "roll: directory synced", 6: "roll: switched",
         7: "drop: manifest written", 8: "drop: manifest renamed", 9: "drop: manifest durable", 10: "drop: segment deleted", 11: "drop: directory synced",
         12: "snapshot: outcomes flushed", 13: "snapshot: temporary file created", 14: "snapshot: half written", 15: "snapshot: written",
         16: "snapshot: synced", 17: "snapshot: renamed", 18: "snapshot: directory synced", 19: "snapshot: switched"}
M_KNOBS = ["--segment-bytes", "262144", "--retention-ms", "1", "--window-ms", "1", "--delivery-log-bytes", "65536", "--schedule", "3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000,3000"]
# the template is made with the retention off (a month), so nothing is dropped while it is built
T_KNOBS = ["--segment-bytes", "262144", "--delivery-log-bytes", "33554432", "--schedule", M_KNOBS[-1]]


def make_template(pinned):
    """A data directory, made by a real run: events through one endpoint (all final), or through two with the second one down (not final)."""
    n = 1000 if not pinned else 600
    a = Receiver()
    b = Receiver()
    if pinned:
        b.stop()
    d = tmpdir("tmpl")
    conf(d, [(0, a)] + ([(1, b)] if pinned else []))
    svc = Svc(d, T_KNOBS)
    svc.start()
    post_many(svc, 1, n, pad=280)
    ok = wait_for(lambda: svc.cursors().get(0) == n, 60)
    if pinned:
        ok = ok and wait_for(lambda: svc.stats()["failed"] >= n, 60)
    else:
        ok = ok and wait_for(lambda: svc.stats()["delivered"] == n, 10)
    time.sleep(0.3)
    cur = svc.endpoints()
    svc.kill(power=False)
    a.stop()
    b.stop()
    return d, n, ok, cur


def one_kill(template, n, pinned, driver, step, cur, nofn):
    """Kill -9 (as a power cut) at `step`; then (1) start the service with retention off, so that the state the kill left is looked at and does not move,
    and check it; (2) finish the compaction with `compact-now` and check the end."""
    d = tmpdir("km")
    shutil.rmtree(d)
    shutil.copytree(template, d)
    if os.path.exists(os.path.join(d, "killpoint")):
        os.remove(os.path.join(d, "killpoint"))
    a, b = Receiver(), Receiver()
    if pinned:
        b.stop()
    conf(d, [(0, a)] + ([(1, b)] if pinned else []))
    extra = ["--compact-now", "1"] if driver == "now" else []
    tag = f"{driver}/{'pinned' if pinned else 'final'}/step {step} ({STEPS[step]})"
    svc = Svc(d, M_KNOBS + extra + ["--compact-kill-at", str(step)])
    if driver == "loop":
        svc.start()
    else:
        # compact-now never says "listening": it compacts and exits, or stops at the step
        svc.proc = subprocess.Popen(svc.command(), stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=svc.env())
    reached = wait_for(lambda: svc.killpoint() == step, 20, every=0.01)
    if not reached:
        rc = svc.proc.poll()
        svc.kill(power=False)
        a.stop()
        b.stop()
        shutil.rmtree(d, ignore_errors=True)
        return tag, False, f"the kill point was not reached (exit {rc})"
    svc.kill()                      # kill -9 at the step, as a power cut
    os.remove(os.path.join(d, "killpoint"))
    problems = []

    # --- 1. the state the kill left, looked at on a service that does nothing to it
    svc2 = Svc(d, T_KNOBS)
    if not svc2.start():
        a.stop()
        b.stop()
        return tag, False, f"does not start: {svc2.lines}"
    s = svc2.stats()
    if s["events_last_id"] != n:
        problems.append(f"last id {s['events_last_id']} not {n}")
    first = s["events_first_id"]
    if pinned and first != 1:
        problems.append(f"an event that is not final was dropped: first id {first}")
    if not (1 <= first <= n + 1):
        problems.append(f"first id {first}")
    bad = []
    for i in list(range(first, n + 1, 7)) + ([n] if first <= n else []):
        code, body = svc2.status_of(f"/events/{i}")
        if code != 200 or json.loads(body)["event"]["n"] != nofn[i]:
            bad.append((i, code))
    for i in range(1, first, 97):
        code, body = svc2.status_of(f"/events/{i}")
        if code != 410:
            bad.append((i, code))
    if bad:
        problems.append(f"events unreadable or not gone: {bad[:4]}")
    e0 = svc2.endpoints()[0]
    if e0["cursor"] != n or e0["disabled"]:
        problems.append(f"endpoint 0 is not where it was: {e0}")
    if pinned:
        e1 = svc2.endpoints()[1]
        if e1["cursor"] != 0 or e1["disabled"] or e1["failing_since"] == 0:
            problems.append(f"endpoint 1 is not where it was: {e1} (was {cur[1]})")
    status, ans = svc2.post({"type": "t", "n": n + 1, "pad": "new"})
    if status != 202 or ans["id"] != n + 1:
        problems.append(f"the next id is {ans}, not {n + 1}")
    if pinned:
        b.start()
    wait_for(lambda: a.distinct() >= 1 and (not pinned or b.distinct() >= n + 1), 180)      # it returns as soon as B has them all; the bound is for a loaded machine
    time.sleep(0.4)
    if a.count != {n + 1: 1}:
        problems.append(f"endpoint 0 was sent {len(a.count)} events ({a.repeats()} repeats): {sorted(a.count)[:5]}, not just the new one")
    if pinned and (sorted(b.count) != list(range(1, n + 2)) or b.repeats() != 0):
        problems.append(f"endpoint 1 was sent {b.distinct()} distinct events, {b.repeats()} repeats, of {n + 1}")
    wait_for(lambda: svc2.cursors().get(0) == n + 1, 10)
    sent = (a.total(), b.total())
    svc2.kill()

    # --- 2. the compaction finished by compact-now (twice: what it seals is not yet old enough to go in the same run), and nothing is owed any more
    for _ in range(2):
        code, text = Svc(d, M_KNOBS + ["--compact-now", "1"]).run_once()
        if code != 0:
            problems.append(f"compact-now after the kill: {code} {text!r}")
        time.sleep(0.05)
    left = files_of(d)
    if any(f.endswith(".tmp") for f in left):
        problems.append(f"leftovers: {left}")
    k0 = int(open(os.path.join(d, "events.first")).read()) if os.path.exists(os.path.join(d, "events.first")) else 0
    nums = sorted(0 if f == "events.seg" else int(f[7:-4]) for f in left if f.startswith("events") and f.endswith(".seg"))
    if not nums or nums[0] != k0 or nums != list(range(nums[0], nums[-1] + 1)):
        problems.append(f"segments {nums} with the manifest at {k0}")
    recs, _ = chaos.read_events(d)
    if recs:
        problems.append(f"{len(recs)} events are left, all final and old")
    svc3 = Svc(d, T_KNOBS)
    svc3.start()
    time.sleep(0.6)
    s3 = svc3.stats()
    if (a.total(), b.total()) != sent:
        problems.append(f"the finished compaction and a restart sent more: {sent} -> {(a.total(), b.total())}")
    if s3["events_last_id"] != n + 1 or s3["events_first_id"] != n + 2:
        problems.append(f"after compact-now: first {s3['events_first_id']}, last {s3['events_last_id']}")
    if svc3.endpoints()[0]["cursor"] != n + 1:
        problems.append(f"endpoint 0 ends at {svc3.endpoints()[0]['cursor']}")
    o = outcomes_of(d, headers=True)
    if o[0][0] != 15:
        problems.append("the outcomes log has no header after a snapshot")
    svc3.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)
    return tag, not problems, "; ".join(problems)


def stage_killmatrix():
    for pinned in (False, True):
        template, n, ok, cur = make_template(pinned)
        check("killmatrix: the %s template is made by a real run (%d events)" % ("pinned" if pinned else "all-final", n), ok, "")
        nofn = {i: json.loads(dict(p)[b"event"])["n"] for i, p in chaos.read_events(template)[0]}
        for driver in ("loop", "now"):
            steps = [s for s in STEPS if not (pinned and 7 <= s <= 11)]
            if os.environ.get("QUICK"):
                steps = [s for s in steps if s in (3, 8, 14, 17)]
            if os.environ.get("KM_STEPS"):
                steps = [s for s in steps if s in [int(x) for x in os.environ["KM_STEPS"].split(",")]]
            for step in steps:
                tag, ok, detail = one_kill(template, n, pinned, driver, step, cur, nofn)
                check("killmatrix: " + tag + ": loses nothing, repeats nothing, ids go on, a second restart changes nothing", ok, detail)
        shutil.rmtree(template, ignore_errors=True)


# =========================================================================================================================================
# compactnow
# =========================================================================================================================================

def stage_compactnow():
    r = Receiver()
    d = tmpdir("now")
    conf(d, [(0, r)])
    knobs = ["--segment-bytes", "262144", "--retention-ms", "1", "--window-ms", "1"]
    svc = Svc(d, knobs)
    svc.start()
    post_many(svc, 1, 700, pad=280)
    wait_for(lambda: svc.cursors() == {0: 700}, 30)
    svc.kill(power=False)
    # (what a kill in the middle of the loop's own step leaves is repaired by the next start; do that first, with retention off, so that what
    # is compared below is what the refused compact-now does)
    settle = Svc(d, ["--segment-bytes", "262144"])
    settle.start()
    settle.stop()
    lock = open(os.path.join(d, "compact.lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    before = {n: hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest()[:10] for n in sorted(os.listdir(d))}
    code, text = Svc(d, knobs + ["--compact-now", "1"]).run_once()
    after = {n: hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest()[:10] for n in sorted(os.listdir(d))}
    check("compactnow: while another process holds compact.lock (a backup) it does nothing and exits 43", code == 43 and "compact.lock" in text and after == before,
          f"{code} {text!r} {[(n, before.get(n), after.get(n)) for n in sorted(set(before) | set(after)) if before.get(n) != after.get(n)]}")
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    code, text = Svc(d, knobs + ["--compact-now", "1"]).run_once()
    check("compactnow: with the lock free it seals, drops and replaces, says what it did and exits 0", code == 0 and "dropped" in text and "delivery.seg" in text, f"{code} {text!r}")
    recs, _ = chaos.read_events(d)
    if recs:
        # The age an event must have is 1 ms here, and the segment the run itself seals is younger than that when the drop is looked at in the same millisecond (found
        # as 1 run in 8 with the binary of the retention change itself): the events are final and nothing is wrong, and the next run takes them.
        code, text = Svc(d, knobs + ["--compact-now", "1"]).run_once()
        check("compactnow: ... a run that found the segment it sealed younger than the retention is followed by one that drops it", code == 0 and "dropped" in text, f"{code} {text!r}")
        recs, _ = chaos.read_events(d)
    check("compactnow: ... every event was final and past the retention, so the log holds none; the next id is 701", recs == [], str(len(recs)))
    svc = Svc(d, knobs)
    svc.start()
    check("compactnow: ... and a service started on it goes on from 701", svc.post({"type": "t", "n": 701})[1] == {"id": 701}, "")
    svc.stop()
    r.stop()
    shutil.rmtree(d, ignore_errors=True)
    # the lock is taken by the loop too: a held lock defers a drop and a replacement, and does not stop ingest
    r = Receiver()
    d = tmpdir("now2")
    conf(d, [(0, r)])
    lock = open(os.path.join(d, "compact.lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    svc = Svc(d, ["--segment-bytes", "262144", "--retention-ms", "1000", "--window-ms", "500", "--delivery-log-bytes", "65536"])
    svc.start()
    post_many(svc, 1, 1500, pad=280)
    wait_for(lambda: svc.cursors() == {0: 1500}, 30)
    time.sleep(4)
    s = svc.stats()
    check("compactnow: the loop respects the lock: nothing dropped or replaced while it is held, ingest and delivery go on (lock skips %d)" % s["maintenance_lock_skips"],
          s["segments_dropped"] == 0 and s["snapshots"] == 0 and s["maintenance_lock_skips"] >= 1 and s["segments_sealed"] >= 1, str(s))
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    ok = wait_for(lambda: svc.stats()["events_first_id"] == 1501 and svc.stats()["snapshots"] >= 1, 20)
    check("compactnow: ... and when it is released the work is done", ok, str(svc.stats()))
    svc.stop()
    r.stop()
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# stall
# =========================================================================================================================================

def stage_stall():
    # 62 endpoints, none of them listening, a long retry schedule: every event is a failed attempt at every endpoint, and each endpoint's window
    # is full (1,024 cells waiting for a retry): the largest snapshot there can be
    d = tmpdir("stall")
    dead = Receiver()
    dead.stop()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(62):
            f.write(f"{i} 127.0.0.1 {dead.port} {SECRET}\n")
    svc = Svc(d, ["--schedule", "3600000", "--deadline-ms", "300", "--delivery-log-bytes", "65536", "--segment-bytes", "1048576"], shim=False)
    svc.start()
    lat = []
    stop = threading.Event()
    meter = LoadMeter()      # how much of a processor this machine is giving a process while the stage runs (see the checks at the end)

    def probe():
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", svc.port, timeout=30)
        while not stop.is_set():
            t = time.time()
            c.request("POST", "/events", body=json.dumps({"type": "probe"}).encode())
            c.getresponse().read()
            lat.append((time.time() - t) * 1000)
            time.sleep(0.005)

    th = threading.Thread(target=probe)
    th.start()
    post_many(svc, 1, 1100, pad=100, threads=4)
    ok = wait_for(lambda: svc.stats()["failed"] >= 62 * 1024, 600)       # (63,488 failed attempts: 120 s were not enough with the processor shared with six busy loops, 40,160 by then)
    time.sleep(1)
    stop.set()
    th.join()
    meter.stop()
    slow = meter.slowdown()
    s = svc.stats()
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
    print("     %d probes during the run: p50 %.1f ms, p99 %.1f ms, max %.1f ms; the loop's own step: at most %d ms (%d snapshots, %d segments sealed, %d outcome bytes)"
          % (len(lat), p(0.5), p(0.99), lat[-1], s["maintenance_ms_max"], s["snapshots"], s["segments_sealed"], s["delivery_bytes"]), flush=True)
    check("stall: 62 endpoints with full windows: %d failed attempts recorded, %d snapshots made" % (s["failed"], s["snapshots"]), ok and s["snapshots"] >= 1, str(s))
    # a snapshot of all of it, by itself
    svc.kill(power=False)
    t0 = time.time()
    code, text = Svc(d, ["--schedule", "3600000", "--compact-now", "1"], shim=False).run_once(timeout=120)
    took = time.time() - t0
    size = os.path.getsize(os.path.join(d, "delivery.seg"))
    # exactly one record for: the header, each slot (created), each cell of each window (62 x 1,024 failed attempts), each failure streak
    check("stall: the snapshot of the largest state is exactly %d records of 77 bytes: header, 62 slots, 62 x 1,024 cells, 62 failure streaks" % (1 + 62 + 62 * 1024 + 62),
          size == 77 * (1 + 62 + 62 * 1024 + 62), str(size))
    print("     compact-now on the full state: %.2f s including the start and the replay of the log; delivery.seg is now %d bytes" % (took, size), flush=True)
    # the stated bound (docs/retention.md section 12): the largest snapshot there can be (62 full windows, 63,488 records, 4.9 MB) holds the loop for
    # 40 to 70 ms measured (0.5 to 0.96 s before `state.put_outcome` stopped losing its region: docs/cancho-log-retention.md gap 6); every other step
    # is a few milliseconds. The gate leaves room for a loaded machine.
    # The step is measured in wall-clock time by the service, so a machine that gives the service a fraction of a processor makes it longer in proportion: 990 ms was the step
    # with the processor shared with six busy loops, a snapshot that took 40 to 70 ms of processor time. The 300 ms is for a machine that gives a whole processor; what this one gave
    # is measured while the stage runs (`LoadMeter.slowdown`: the wall clock of a short spin over its processor time, 1.0 when a core is free; the 90th percentile of those taken
    # every 250 ms) and the bound is that many times as long. The region loss that this check exists for held the loop for 0.5 to 0.96 s on a machine with a free core.
    scale = max(1.0, slow)
    check("stall: no step held the loop for more than 300 ms (times %.1f, what this machine gave the stage), the largest state there can be (measured: %d ms)" % (scale, s["maintenance_ms_max"]),
          s["maintenance_ms_max"] <= 300 * scale, str((s["maintenance_ms_max"], slow)))
    # What bounds the loop is the line above: the service measures its own longest step (`maintenance_ms_max`), and that is the gate that caught the
    # region loss (0.5 to 0.96 s). The longest PROBE is the worst of several hundred requests on a machine that is not ours: on a shared CI runner it was
    # 528 ms with the loop's own step inside its bound (CI run 37629892445), where eight runs on a quiet machine gave a longest probe of 14 to 112 ms, a
    # 99th percentile of 4 to 12 ms and a longest step of 14 to 22 ms. So the requests are judged by what they mostly saw (the 99th percentile) and by a
    # loose cap on the worst, which still fails a loop that is held for a second.
    check("stall: and the requests waited for it little: 99th percentile at most 100 ms (times %.1f; measured: %.1f ms)" % (scale, p(0.99)), p(0.99) <= 100 * scale, str((p(0.99), slow)))
    check("stall: and none waited a second (times %.1f; the longest probe, measured: %.0f ms)" % (scale, lat[-1]), lat[-1] <= 1000 * scale, str((lat[-1], slow)))
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================================================================================
# memory
# =========================================================================================================================================

def rss_mb(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024.0
    return 0.0


def stage_memory():
    # The C load generator and sink of scripts/bench: a few tens of thousands of events until it settles, then 60,000 more, each delivered; the resident size between must not move
    # (8 MB of room: a function that loses 64 KiB a turn loses about 0.2 KB an event, 11 MB over these).
    # (The leaks this guards against: 3.5 KB for each outcome record and about 11 KB for each delivery attempt, 330 MB over the second step; and a
    # region lost on each flush, 11 MB.)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = tmpdir("tools")
    for name in ("loadgen", "sink"):
        subprocess.run(["gcc", "-O2", "-o", os.path.join(tools, name), os.path.join(here, "scripts", "bench", name + ".c")], check=True)
    d = tmpdir("mem")
    sink_port = chaos.free_port()
    sink = subprocess.Popen([os.path.join(tools, "sink"), str(sink_port)], stderr=subprocess.PIPE)
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {sink_port} {SECRET}\n")
    svc = Svc(d, ["--schedule", "100,100,100"], shim=False)
    svc.start()

    def run(n):
        subprocess.run([os.path.join(tools, "loadgen"), str(svc.port), "16", str(n), "200"], capture_output=True, timeout=300)

    def done(n):
        return wait_for(lambda: svc.stats()["delivered"] >= n, 120)

    # warm up until the resident size stops moving (the arrays of the service are touched as the ids pass, and the plateau depends on the number of
    # connections); a leak never stops, and is caught below
    sent, before, prev = 0, 0.0, 0.0
    ok1 = True
    for _ in range(8):
        run(20000)
        sent += 20000
        ok1 = done(sent) and ok1
        time.sleep(0.5)
        before = rss_mb(svc.proc.pid)
        if prev and before - prev <= 3:
            break
        prev = before
    run(60000)
    sent += 60000
    ok2 = done(sent)
    time.sleep(0.5)
    after = rss_mb(svc.proc.pid)
    check("memory: %d events posted and every one delivered (%s)" % (sent, svc.stats()["delivered"]), ok1 and ok2, str(svc.stats()))
    check("memory: the resident size did not grow with them: %.1f MB when it had settled, %.1f MB after 60,000 more" % (before, after), after - before <= 8, f"{before} {after}")
    svc.stop()
    sink.terminate()
    sink.wait()
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(tools, ignore_errors=True)


STAGES = [("bounded", stage_bounded), ("pins", stage_pins), ("dormant", stage_dormant), ("snapshot", stage_snapshot), ("formats", stage_formats), ("keys", stage_keys),
          ("killmatrix", stage_killmatrix), ("compactnow", stage_compactnow), ("stall", stage_stall), ("memory", stage_memory)]

if __name__ == "__main__":
    for name, fn in STAGES:
        if WANT and name not in WANT:
            continue
        print(f"--- {name}", flush=True)
        fn()
    print("FAILED: " + "; ".join(FAILS) if FAILS else "all retention checks passed")
    sys.exit(1 if FAILS else 0)
