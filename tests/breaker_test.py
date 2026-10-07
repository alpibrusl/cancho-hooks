#!/usr/bin/env python3
"""The circuit breaker (docs/production.md 0.1, docs/design.md section 31): an endpoint whose every attempt has failed for `breaker-days`
days (default 5; 0 is off) is paused: the disabled mechanism of section 22, with a reason of its own. Its events wait in the log and are
sent when a person enables it.

    python3 tests/breaker_test.py build/hooks

The service cannot be made to wait five days, so the test writes the records the service itself would have written five days ago: a
`streak` record (kind 12: endpoint, and in the fifth field when its run of failed attempts began, Unix ms) is put in `delivery.seg` before
the service starts, with a time 5 days and an hour back (an old binary reads it as an ordinary record of the slot). The service
counts days in milliseconds on the Unix clock, the rule itself is unit-tested at its boundary (`tests/state_test.cho`), and here the
end-to-end part is checked at the granularity of an hour either side of a day.

  1. a failed attempt begins a run, written once with its time (one `streak` record however many attempts fail), shown as `failing_since`
     in `GET /endpoints`, and kept across `kill -9`
  2. a delivery ends the run (shown 0, kept across a restart), and the next failure begins a new one (a second record)
  3. a run of 5 days and an hour: the next failed attempt pauses the endpoint: `disabled` and `paused` in `GET /endpoints`, one `paused`
     record (kind 13), a line on stderr naming the endpoint, `/stats` counting it; the other endpoint is not touched
  4. its events wait: nothing is sent to it while paused, though its receiver is up; the same after `kill -9` (still paused, no second
     record); then `POST /endpoints/:id/enable`: not disabled, not paused, `failing_since` 0, and every event is sent to it, once each
  5. enabling ended the run: the next failure starts a new one and does not pause the endpoint at once
  6. `breaker-days = 0` never pauses, however old the run; `breaker-days = 1` pauses on a run of 25 hours and not on one of 23
  7. a `410` disables an endpoint and it is not `paused`: the two reasons are told apart (and enabling clears either)
  8. a replay that is delivered ends a run (a replay is an attempt like any other), and a replay that fails ends it in a pause
  9. a slot that is freed and given again inherits neither the pause nor the run
 10. enabling ends the run even when nothing is delivered afterwards: the receiver is still down, the next failed attempt is the first of a
     new run (a delivery would end the old one anyway, which is why stage 4 cannot tell)
"""
import base64
import http.server
import json
import os
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

http.server.HTTPServer.request_queue_size = 128
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAILS = []
DAY = 86400 * 1000
HOUR = 3600 * 1000
DELIVERED, FAILED, DEAD, DISABLED, ENABLED, CREATED, REMOVED, STREAK, PAUSED = 1, 2, 3, 4, 5, 10, 11, 12, 13
SECRET = "whsec_" + base64.b64encode(os.urandom(24)).decode()


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


# ---- the outcome log, read and written here (the format of src/state.cho `put_outcome`) ------------------------------

def put_record(seq, kind, slot, event=0, attempts=0, next_at=0):
    value = struct.pack("<5Q", kind, slot, event, attempts, next_at)
    body = struct.pack("<QQI", seq, 0, 1) + struct.pack("<I", 1) + b"o" + struct.pack("<I", len(value)) + value
    return struct.pack("<I", 4 + len(body)) + struct.pack("<I", chaos.crc32c(body)) + body


def read_outcomes(path):
    if not os.path.exists(path):
        return []
    recs, _ = chaos.read_log(open(path, "rb").read())
    out = []
    for ms, pairs in recs:
        k, e, ev, at, nx = struct.unpack("<5Q", pairs[0][1])
        out.append((ms, k, e, ev, at, nx))
    return out


def append_outcome(path, kind, slot, event=0, attempts=0, next_at=0):
    rows = read_outcomes(path)
    seq = rows[-1][0] + 1 if rows else 0
    with open(path, "ab") as f:
        f.write(put_record(seq, kind, slot, event, attempts, next_at))


def of_kind(datadir, kind):
    return [r for r in read_outcomes(os.path.join(datadir, "delivery.seg")) if r[1] == kind]


def now_ms():
    return int(time.time() * 1000)


# ---- receivers and the service -------------------------------------------------------------------------------------

class Receiver:
    """Records the `n` of every request; answers `status`. `stop()` makes the port refuse connections, `start()` accepts again."""

    def __init__(self):
        self.seen = []
        self.status = 204
        self.lock = threading.Lock()
        self.port = chaos.free_port()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                with outer.lock:
                    outer.seen.append(json.loads(body)["n"])
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.handler = H
        self.srv = None
        self.start()

    def start(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), self.handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def got(self):
        with self.lock:
            return list(self.seen)


class Service:
    def __init__(self, datadir, schedule="200,200,200", extra=()):
        self.datadir, self.port, self.proc = datadir, chaos.free_port(), None
        self.args = ["--schedule", schedule, "--deadline-ms", "800", *extra]
        self.lines = []

    def start(self):
        self.proc = subprocess.Popen([BIN, "--port", str(self.port), "--dir", self.datadir, "--allow-private-hosts", "1", *self.args],
                                     stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        assert self.proc.stderr.readline().strip() == b"listening"
        proc = self.proc

        def pump():
            for line in proc.stderr:
                self.lines.append(line.decode().strip())

        threading.Thread(target=pump, daemon=True).start()

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=body)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        return self.call("GET", path)[1]

    def endpoints(self):
        return {e["id"]: e for e in self.get("/endpoints")}

    def post_event(self, n):
        status, _ = chaos.post(self.port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)
        assert status == 202


def workdir(receivers, rows=()):
    """A data directory with endpoints 0, 1, ... for `receivers`, and `rows` ((kind, slot, event, attempts, next_at) ...) as its delivery.seg."""
    d = tempfile.mkdtemp(prefix="hooks-breaker-")
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i, r in enumerate(receivers):
            f.write(f"{i} 127.0.0.1 {r.port} {SECRET}\n")
    for i, row in enumerate(rows):
        with open(os.path.join(d, "delivery.seg"), "ab") as f:
            f.write(put_record(i, *row))
    return d


def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except (OSError, KeyError):
            pass
        time.sleep(0.02)
    return False


def stage1_and_2():
    a, b = Receiver(), Receiver()
    b.stop()
    d = workdir([a, b])
    # sixteen delays of 400 ms: an event is tried again for 6 s before it is a dead letter, long enough to be kept across a restart and delivered
    svc = Service(d, schedule=",".join(["400"] * 16))
    svc.start()
    t0 = now_ms()
    svc.post_event(1)
    check("1. a failing endpoint: at least three attempts have failed", wait_for(lambda: svc.get("/stats")["failed"] >= 3, 5), str(svc.get("/stats")))
    eps = svc.endpoints()
    since = eps[1]["failing_since"]
    check("1. B's run began at its first failed attempt (just after the post)", t0 <= since <= now_ms(), f"{t0} {since} {now_ms()}")
    check("1. ... A has no run, and neither is disabled or paused", eps[0]["failing_since"] == 0 and not eps[0]["paused"] and not eps[1]["paused"] and not eps[1]["disabled"], str(eps))
    recs = of_kind(d, STREAK)
    check("1. one `streak` record however many attempts failed, with the endpoint and the time", len(recs) == 1 and recs[0][2] == 1 and recs[0][5] == since, str(recs))
    svc.kill()
    svc.start()
    check("1. a kill and a restart: the run began when it did", svc.endpoints()[1]["failing_since"] == since, str(svc.endpoints()))
    check("1. ... and the next failure writes no second record", wait_for(lambda: svc.get("/stats")["failed"] >= 1, 5) and len(of_kind(d, STREAK)) == 1, str(of_kind(d, STREAK)))

    # 2. a delivery ends the run
    b.start()
    check("2. B comes up and the event is delivered", wait_for(lambda: b.got() == [1], 5), str(b.got()))
    check("2. a delivery ends the run: failing_since is 0", wait_for(lambda: svc.endpoints()[1]["failing_since"] == 0, 5), str(svc.endpoints()))
    svc.kill()
    svc.start()
    check("2. ... also after a kill and a restart", svc.endpoints()[1]["failing_since"] == 0, str(svc.endpoints()))
    b.stop()
    t1 = now_ms()
    svc.post_event(2)
    check("2. B fails again: a new run begins", wait_for(lambda: svc.endpoints()[1]["failing_since"] >= t1, 5), str(svc.endpoints()))
    recs = of_kind(d, STREAK)
    check("2. ... as a second `streak` record, later than the first", len(recs) == 2 and recs[1][5] > recs[0][5], str(recs))
    svc.stop()
    a.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage3_4_5():
    a, b = Receiver(), Receiver()
    b.stop()
    old = now_ms() - 5 * DAY - HOUR
    d = workdir([a, b], [(STREAK, 1, 0, 0, old)])
    svc = Service(d)
    svc.start()
    check("3. a run read from the log: failing_since is what the record says", svc.endpoints()[1]["failing_since"] == old, str(svc.endpoints()))
    check("3. ... and nothing is paused before an attempt fails", not svc.endpoints()[1]["paused"], str(svc.endpoints()))
    svc.post_event(1)
    check("3. the first failed attempt of a run older than 5 days pauses the endpoint",
          wait_for(lambda: svc.endpoints()[1]["paused"], 5), str(svc.endpoints()))
    eps = svc.endpoints()
    check("3. ... it is disabled as well (the mechanism of section 22), and the run is kept", eps[1]["disabled"] and eps[1]["failing_since"] == old, str(eps))
    check("3. ... the other endpoint is untouched and gets the event", not eps[0]["paused"] and not eps[0]["disabled"] and wait_for(lambda: a.got() == [1], 5), str((eps, a.got())))
    check("3. one `paused` record, for slot 1", [(r[2]) for r in of_kind(d, PAUSED)] == [1], str(of_kind(d, PAUSED)))
    check("3. a line on stderr names the endpoint and says who paused it",
          wait_for(lambda: any("endpoint 1 paused by the circuit breaker" in l for l in svc.lines), 5), str(svc.lines))
    st = svc.get("/stats")
    check("3. /stats counts it", st["paused"] == 1 and st["breaker_trips"] == 1, str(st))
    time.sleep(0.5)
    check("3. ... and the line is said once, not on every turn", sum(1 for l in svc.lines if "paused by the circuit breaker" in l) == 1, str(svc.lines))

    # 4. the events wait
    b.start()
    for n in range(2, 7):
        svc.post_event(n)
    check("4. the other endpoint is sent the later events", wait_for(lambda: sorted(a.got()) == [1, 2, 3, 4, 5, 6], 5), str(a.got()))
    time.sleep(1.2)
    check("4. nothing is sent to the paused endpoint though its receiver is up", b.got() == [], str(b.got()))
    check("4. its cursor stays where it was", svc.endpoints()[1]["cursor"] == 0, str(svc.endpoints()))
    svc.kill()
    svc.start()
    eps = svc.endpoints()
    check("4. after a kill and a restart it is still paused and disabled, the run unchanged", eps[1]["paused"] and eps[1]["disabled"] and eps[1]["failing_since"] == old, str(eps))
    check("4. ... with no second `paused` record, and still nothing sent", len(of_kind(d, PAUSED)) == 1 and (time.sleep(0.8) or b.got() == []), str((of_kind(d, PAUSED), b.got())))
    status, _ = svc.call("POST", "/endpoints/1/enable")
    check("4. enable answers 200", status == 200, str(status))
    eps = svc.endpoints()
    check("4. ... and it is not disabled or paused and has no run", not eps[1]["disabled"] and not eps[1]["paused"] and eps[1]["failing_since"] == 0, str(eps))
    check("4. every event that waited is sent to it, once each", wait_for(lambda: sorted(b.got()) == [1, 2, 3, 4, 5, 6], 8) and len(b.got()) == 6, str(b.got()))
    check("4. its cursor ends at 6", wait_for(lambda: svc.endpoints()[1]["cursor"] == 6, 5), str(svc.endpoints()))
    check("4. one `enabled` record", len(of_kind(d, ENABLED)) == 1, str(of_kind(d, ENABLED)))
    svc.kill()
    svc.start()
    eps = svc.endpoints()
    check("4. a restart after the enable: not paused, no run, nothing repeated",
          not eps[1]["paused"] and not eps[1]["disabled"] and eps[1]["failing_since"] == 0 and (time.sleep(0.8) or len(b.got()) == 6), str((eps, b.got())))

    # 5. enabling ended the run
    b.stop()
    t1 = now_ms()
    svc.post_event(7)
    check("5. a failure after the enable begins a new run", wait_for(lambda: svc.endpoints()[1]["failing_since"] >= t1, 5), str(svc.endpoints()))
    time.sleep(0.8)
    eps = svc.endpoints()
    check("5. ... and does not pause the endpoint at once (the old run was ended by the enable)", not eps[1]["paused"] and not eps[1]["disabled"], str(eps))
    check("5. ... one `paused` record in all", len(of_kind(d, PAUSED)) == 1, str(of_kind(d, PAUSED)))
    svc.stop()
    a.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage6():
    for days, age, paused, name in ((0, 400 * DAY, False, "breaker-days 0 does not pause on a run of 400 days"),
                                    (1, 25 * HOUR, True, "breaker-days 1 pauses on a run of 25 hours"),
                                    (1, 23 * HOUR, False, "breaker-days 1 does not pause on a run of 23 hours"),
                                    (5, 4 * DAY + 23 * HOUR, False, "the default (5) does not pause on a run of 4 days 23 hours")):
        a, b = Receiver(), Receiver()
        b.stop()
        old = now_ms() - age
        d = workdir([a, b], [(STREAK, 1, 0, 0, old)])
        extra = ["--breaker-days", str(days)] if days != 5 else []
        svc = Service(d, extra=extra)
        svc.start()
        svc.post_event(1)
        wait_for(lambda: svc.get("/stats")["failed"] >= 3, 5)
        time.sleep(0.5)
        eps = svc.endpoints()
        check("6. " + name, eps[1]["paused"] == paused and eps[1]["disabled"] == paused and eps[1]["failing_since"] == old, str(eps))
        svc.stop()
        a.stop()
        shutil.rmtree(d, ignore_errors=True)


def stage7():
    a, b = Receiver(), Receiver()
    d = workdir([a, b])
    svc = Service(d)
    svc.start()
    b.status = 410
    svc.post_event(1)
    check("7. a 410 disables B", wait_for(lambda: svc.endpoints()[1]["disabled"], 5), str(svc.endpoints()))
    eps = svc.endpoints()
    check("7. ... and B is not paused: the breaker is not the reason, and it began no run", not eps[1]["paused"] and eps[1]["failing_since"] == 0, str(eps))
    check("7. ... no `paused` or `streak` record", not of_kind(d, PAUSED) and not of_kind(d, STREAK), str(read_outcomes(os.path.join(d, "delivery.seg"))))
    check("7. /stats counts no pause", svc.get("/stats")["paused"] == 0 and svc.get("/stats")["breaker_trips"] == 0, str(svc.get("/stats")))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage8():
    a, b = Receiver(), Receiver()
    b.stop()
    d = workdir([a, b])
    # run one: breaker off, B down, a schedule of one delay: event 1 is a dead letter at B (its cursor moves past it)
    svc = Service(d, schedule="100", extra=["--breaker-days", "0"])
    svc.start()
    svc.post_event(1)
    check("8. (setup) event 1 is a dead letter at B", wait_for(lambda: svc.endpoints()[1]["cursor"] == 1, 5), str(svc.endpoints()))
    svc.stop()
    old = now_ms() - 6 * DAY
    append_outcome(os.path.join(d, "delivery.seg"), STREAK, 1, 0, 0, old)
    svc = Service(d, schedule="100")
    svc.start()
    eps = svc.endpoints()
    check("8. (setup) B has a run of 6 days and is not paused", eps[1]["failing_since"] == old and not eps[1]["paused"], str(eps))
    status, out = svc.call("POST", "/events/1/replay/1")
    check("8. a replay to B is accepted", status == 202, str((status, out)))
    check("8. its failed attempt (a replay is an attempt like any other) pauses B on a run of 6 days",
          wait_for(lambda: svc.endpoints()[1]["paused"], 5), str(svc.endpoints()))
    svc.stop()

    # B comes up and a person enables it: the replay that was waiting for it is sent, once
    b.start()
    svc = Service(d, schedule="100")
    svc.start()
    status, _ = svc.call("POST", "/endpoints/1/enable")
    check("8. enabled, B is sent the replay that waited for it", status == 200 and wait_for(lambda: b.got() == [1], 5), str((status, b.got())))
    svc.stop()
    # a delivered replay ends a run: B has one of two hours (written here), and a second replay of event 1 delivers
    append_outcome(os.path.join(d, "delivery.seg"), STREAK, 1, 0, 0, now_ms() - 2 * HOUR)
    svc = Service(d, schedule="100")
    svc.start()
    check("8. (setup) B has a run of 2 hours again", svc.endpoints()[1]["failing_since"] > 0, str(svc.endpoints()))
    status, _ = svc.call("POST", "/events/1/replay/1")
    check("8. a delivered replay ends the run", status == 202 and wait_for(lambda: svc.endpoints()[1]["failing_since"] == 0, 5) and b.got() == [1, 1], str((svc.endpoints(), b.got())))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage9():
    a, b = Receiver(), Receiver()
    old = now_ms() - 6 * DAY
    # slot 1 had a run and was paused; then it was freed and given to endpoint 1 again (the log says so: removed, created)
    d = workdir([a, b], [(STREAK, 1, 0, 0, old), (PAUSED, 1, 0, 0, 0), (REMOVED, 1, 0, 0, 0), (CREATED, 1, 1, 0, 0)])
    svc = Service(d)
    svc.start()
    eps = svc.endpoints()
    check("9. a slot freed and given again is not paused, not disabled and has no run", not eps[1]["paused"] and not eps[1]["disabled"] and eps[1]["failing_since"] == 0, str(eps))
    svc.post_event(1)
    check("9. ... and is sent events", wait_for(lambda: b.got() == [1], 5), str(b.got()))
    svc.stop()
    a.stop()
    b.stop()
    shutil.rmtree(d, ignore_errors=True)


def stage10():
    a, b = Receiver(), Receiver()
    b.stop()
    old = now_ms() - 5 * DAY - HOUR
    d = workdir([a, b], [(STREAK, 1, 0, 0, old)])
    svc = Service(d)
    svc.start()
    svc.post_event(1)
    check("10. (setup) B is paused on its old run, and its receiver is still down", wait_for(lambda: svc.endpoints()[1]["paused"], 5), str(svc.endpoints()))
    t1 = now_ms()
    status, _ = svc.call("POST", "/endpoints/1/enable")
    check("10. enable answers 200", status == 200, str(status))
    check("10. the next failed attempt begins a new run", wait_for(lambda: svc.endpoints()[1]["failing_since"] >= t1, 5), str(svc.endpoints()))
    time.sleep(0.8)
    eps = svc.endpoints()
    check("10. ... which does not pause B again (three more attempts have failed)", not eps[1]["paused"] and not eps[1]["disabled"] and svc.get("/stats")["failed"] >= 3, str((eps, svc.get("/stats"))))
    check("10. ... one `paused` record in all, and two `streak` records (the old one, hand-written, and the new one)", len(of_kind(d, PAUSED)) == 1 and len(of_kind(d, STREAK)) == 2, str(of_kind(d, STREAK)))
    svc.stop()
    a.stop()
    shutil.rmtree(d, ignore_errors=True)


for stage in (stage1_and_2, stage3_4_5, stage6, stage7, stage8, stage9, stage10):
    stage()
print("FAILED: " + "; ".join(FAILS) if FAILS else "all breaker checks passed")
sys.exit(1 if FAILS else 0)
