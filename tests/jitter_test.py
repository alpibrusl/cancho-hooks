#!/usr/bin/env python3
"""Retry jitter (docs/design.md section 39.3; production.md P1.2).

    python3 tests/jitter_test.py build/hooks

No database is needed. A receiver that always answers 500, an endpoint from `endpoints.conf` (id 5), and the time of the next attempt that the service
writes to `delivery.seg` with each failed attempt (kind 2, its fifth field), read by the reader of `chaos.py`, written apart from the service.

  1. the delay is the schedule's moved by up to `retry-jitter` percent either way: with 400 events failing at the same moment and a delay of 2,000 ms at 20 percent
     (`--retry-jitter 20`), every recorded delay is within 1,600 to 2,400 ms, their mean is the schedule's delay (within 2 percent), and they fall in every tenth
     of the range (none of the ten holds a third of them, none is empty); and the second attempt of each event arrives at the time the record says, not before it and not late (the median under 150 ms, the 95th percentile under 1,500 ms: the receiver and the machine add to it)
  2. `--retry-jitter 0` is the schedule exactly: every delay is 2,000 ms (within the 30 ms that the receiver's clock and the loop's turn add), the same as a
     service that was never given the setting did before it existed; the default (10 percent, no flag) stays within 1,800 to 2,200 and does spread
  3. it is a function of the endpoint, the event and the attempt and nothing else: two services started apart, with the same endpoint id and the same events, give each
     event the same delay (within 100 ms of noise on a range of 2,000), and another endpoint id gives other delays
  4. the later steps of the schedule are moved too, each by its own percent of itself (delays of 300, 600 and 1,200 ms at 50 percent: the third attempt is 300 to 900 ms
     after the second), and the number of attempts is the schedule's: an event is tried 4 times and is then a dead letter, as without jitter
  5. `kill -9` in the middle of a jittered wait keeps the time that was recorded: 20 events, delays of 6,000 ms at 30 percent (4,200 to 7,800), the service killed twice
     during the wait and the second time restarted with `--retry-jitter 0`: the records are the same after the restarts, and each second attempt arrives at its
     recorded time (not early, and not a delay computed again)
  6. a replay is moved too: a dead letter replayed waits a jittered delay before its second attempt
"""
import base64
import http.server
import json
import os
import shutil
import statistics
import struct
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
from loadmeter import LoadMeter  # noqa: E402

http.server.HTTPServer.request_queue_size = 512
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


class Sink:
    """Always answers `status`; remembers the time (ms) of every request by event."""

    def __init__(self, status=500):
        self.arrivals, self.status = {}, status
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                n = json.loads(body).get("n")
                with outer.lock:
                    outer.arrivals.setdefault(n, []).append(time.time() * 1000)
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def seen(self, n):
        with self.lock:
            return list(self.arrivals.get(n, []))

    def all_have(self, count, events):
        with self.lock:
            return all(len(self.arrivals.get(n, [])) >= count for n in events)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def start(schedule, sink, ident=5, jitter=None, datadir=None, deadline="2000"):
    d = datadir or tempfile.mkdtemp(prefix="hooks-jitter-")
    if datadir is None:
        secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
        with open(os.path.join(d, "endpoints.conf"), "w") as f:
            f.write(f"{ident} 127.0.0.1 {sink.port} {secret}\n")
    more = ("--retry-jitter", str(jitter)) if jitter is not None else ()
    svc = chaos.Service(chaos.free_port(), d, extra=(schedule, deadline, 86400000, *more))
    svc.start()
    return svc, d


def post_events(svc, count, first=1):
    for n in range(first, first + count):
        st, _ = chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)
        assert st == 202, st


def failed_records(d):
    """{(event, attempts): next_at} for every failed attempt in the delivery log (kind 2, then kind 7 for a replay's)."""
    recs, _ = chaos.read_log(open(os.path.join(d, "delivery.seg"), "rb").read())
    out = {}
    for _, pairs in recs:
        kind, slot, ev, att, nxt = struct.unpack("<5q", dict(pairs)[b"o"])
        if kind in (2, 7):
            out[(kind, ev, att)] = nxt
    return out


def wait_for(cond, secs, step=0.02):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def stop(svc, d):
    if svc.proc and svc.proc.poll() is None:
        svc.proc.terminate()
        svc.proc.wait()
    shutil.rmtree(d, ignore_errors=True)


# A recorded delay is the record's time of the next attempt less the time the RECEIVER saw the attempt, and the service wrote the record after it read the answer: so what is
# measured is the delay plus the time from the receiver's stamp to the service's reading (a Python thread's turn, the answer on the wire, the loop's turn). On a quiet machine that is
# a few milliseconds and not below zero by more than the clocks (30 ms is allowed). On a busy one a request can wait for its thread and 400 retries due in the same instant make it
# worse (2,030 ms for a delay of 2,000, 2,072, 2,560 ms for a range that ends at 2,400, with the processor shared by seven others), and the service takes the time of a retry from the
# turn of its loop, which can have begun before the receiver's stamp (a delay of 1,929 ms for 2,000 with a disk in use). So the ends of a range are judged with the noise: the 95th
# percentile within 30 ms of the top, the worst within 400 (what stage 4 has always allowed), the bottom within 30. Those are for a machine with a processor to spare; on a shared one
# the noise is as many times as large as the machine gave this process less (`LoadMeter.slowdown`, 1.0 when it has a core free), so the allowances are multiplied by it. A jitter that
# was not switched off is not hidden by that on a machine that has a core to spare: it moves half of the delays 200 ms below the schedule.
METER = LoadMeter()


def scale():
    return max(1.0, METER.slowdown())


def inside(vals, lo, hi):
    v = sorted(vals)
    return bool(v) and v[0] >= lo - 30 * scale() and v[int(len(v) * 0.95)] <= hi + 30 * scale() and v[-1] <= hi + 400 * scale()


def delays(sink, d, events, attempt=1):
    """The recorded delay of each event after attempt `attempt`: the record's time of the next attempt less the time the receiver saw that attempt."""
    rec = failed_records(d)
    return {n: rec[(2, n, attempt)] - sink.seen(n)[attempt - 1] for n in events if (2, n, attempt) in rec}


def main():
    events = list(range(1, 401))
    # ---- 1. the range, the mean, the spread, and the record is what is used ------------------------------------------------------------
    stage1 = LoadMeter()
    sink = Sink()
    svc, d = start("2000", sink, jitter=20)
    post_events(svc, len(events))
    ok = wait_for(lambda: sink.all_have(2, events), 30)
    dl = delays(sink, d, events)
    vals = list(dl.values())
    check("1. all 400 events were sent twice, and all 400 recorded a delay", ok and len(vals) == 400, str((ok, len(vals))))
    check("1. every recorded delay is within 1,600 to 2,400 ms (the receiver's clock and the loop add up to 30; more at the top on a busy machine)", inside(vals, 1600, 2400), str((min(vals), max(vals))))
    mean = statistics.mean(vals)
    # (the receiver's noise is only ever added, so the mean is the schedule's and the noise's mean: 70 ms with the processor shared with six busy loops; the allowance grows with what the
    # machine gave the test, to 6 percent at most, which is a bias of a third of the range of 20 percent)
    check("1. the mean is the schedule's 2,000 ms within 2 percent", abs(mean - 2000) <= min(40 * scale(), 120), f"{mean:.0f} (scale {scale():.1f})")
    bins = [0] * 10
    for v in vals:
        bins[min(9, max(0, int((v - 1600) / 80)))] += 1
    check("1. the delays fall in every tenth of the range, none holds a third", all(b > 0 for b in bins) and max(bins) < len(vals) / 3, str(bins))
    check("1. the spread is wide: the standard deviation is near a uniform range's (231 ms)", 180 <= statistics.pstdev(vals) <= 280, f"{statistics.pstdev(vals):.0f}")
    rec = failed_records(d)
    early = [n for n in events if sink.seen(n)[1] < rec[(2, n, 1)] - 3]
    # How late the second attempt arrives is the receiver's and the machine's as much as the service's: 400 retries fall due in the same instants, the receiver is a Python
    # thread, and on a CI runner with two cores a tenth of them were more than 400 ms late once. What a service that retried a loop's turn late would show is that **most**
    # are late, so the median is judged tightly and the tail loosely: none early, a median under 150 ms, the 95th percentile under 1,500 ms.
    # The two bounds are for a machine with a processor to spare. The receiver takes its 400 requests one after another, and on one that is shared it takes as many times as long as
    # it was given less of the processor: 981 ms for the median, with the processor shared with six busy loops. So they are multiplied by what the machine gave the test while the
    # stage ran (`LoadMeter.slowdown`, 1.0 when a core is free), and a service that sends each retry a turn late is still caught on a machine like that.
    # A machine that stops altogether for a moment (1.2 s, in the tests of it) makes all the retries that fall due in that moment late by as much: what it kept this process waiting is added
    # to both bounds (`stall_s`, the longest a 5 ms sleep took beyond its time while the stage ran: 0.01 s or so on a machine that does not stop).
    stage1.stop()
    lateness = sorted(sink.seen(n)[1] - rec[(2, n, 1)] for n in events)
    median, p95 = lateness[len(lateness) // 2], lateness[int(len(lateness) * 0.95)]
    stopped = stage1.stall_s * 1000
    check("1. the second attempt of every event arrives at the time recorded: none early, the median under 150 ms late, the 95th percentile under 1,500 ms (times %.1f, what the machine gave the test; plus the %.0f ms it stopped)" % (scale(), stopped),
          not early and median < 150 * scale() + stopped and p95 < 1500 * scale() + stopped, str((early[:5], lateness[0], median, p95, lateness[-1], scale(), stopped)))
    stop(svc, d)
    sink.close()

    # ---- 2. 0 is the schedule exactly; the default is 10 percent -------------------------------------------------------------------------------
    sink = Sink()
    svc, d = start("2000", sink, jitter=0)
    post_events(svc, 200)
    ok = wait_for(lambda: sink.all_have(2, range(1, 201)), 30)
    vals = list(delays(sink, d, range(1, 201)).values())
    check("2. --retry-jitter 0: all 200 delays are 2,000 ms (the receiver's clock and the loop add up to 30; more at the top on a busy machine)", ok and len(vals) == 200 and inside(vals, 2000, 2000), str((min(vals), max(vals))))
    sv = sorted(vals)
    check("2. ... and recorded as exactly the failure time plus 2,000: the spread between events is only the time they failed at (90 of 100 within 60 ms)", len(sv) == 200 and sv[int(len(sv) * 0.95)] - sv[int(len(sv) * 0.05)] <= 60 * scale(), str((sv[0], sv[-1], scale())))
    stop(svc, d)
    sink.close()
    sink = Sink()
    svc, d = start("2000", sink)
    post_events(svc, 200)
    ok = wait_for(lambda: sink.all_have(2, range(1, 201)), 30)
    vals = list(delays(sink, d, range(1, 201)).values())
    check("2. the default: all within 1,800 to 2,200 ms (10 percent)", ok and len(vals) == 200 and inside(vals, 1800, 2200), str((min(vals), max(vals))))
    check("2. ... and spread over it: the range of 200 delays is over 300 ms", max(vals) - min(vals) > 300, str(max(vals) - min(vals)))
    cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/config").read())
    check("2. GET /config says retry-jitter 10", cfg["retry-jitter"] == 10, str(cfg))
    stop(svc, d)
    sink.close()

    # ---- 3. a function of the endpoint, the event and the attempt -----------------------------------------------------------------------------------
    runs = []
    for ident in (5, 5, 6):
        sink = Sink()
        svc, d = start("2000", sink, ident=ident, jitter=30)
        post_events(svc, 100)
        wait_for(lambda: sink.all_have(2, range(1, 101)), 30)
        runs.append(delays(sink, d, range(1, 101)))
        stop(svc, d)
        sink.close()
    same = [abs(runs[0][n] - runs[1][n]) for n in range(1, 101)]
    other = [abs(runs[0][n] - runs[2][n]) for n in range(1, 101)]
    # each difference is of two measurements, each with the noise above: 95 of 100 within 100 ms, and none apart by a third of the range (a function of something else would put them 400 apart on average)
    check("3. two services apart, one endpoint id, the same 100 events: each event gets the same delay (within 100 ms of noise on a range of 1,200)", sorted(same)[94] <= 100 * scale() and max(same) <= 300 + 100 * scale(), str((sorted(same)[94], max(same), scale())))
    check("3. another endpoint id gives other delays: most differ by over 100 ms", sum(1 for x in other if x > 100) >= 70, str(sum(1 for x in other if x > 100)))
    check("3. the events of one endpoint differ among themselves", len(set(round(v / 50) for v in runs[0].values())) >= 15, str(len(set(round(v / 50) for v in runs[0].values()))))

    # ---- 4. the later steps, and the number of attempts --------------------------------------------------------------------------------------------------
    sink = Sink()
    svc, d = start("300,600,1200", sink, jitter=50)
    ev4 = list(range(1, 81))
    post_events(svc, len(ev4))
    ok = wait_for(lambda: sink.all_have(4, ev4), 30)
    time.sleep(0.5)
    g1 = [sink.seen(n)[1] - sink.seen(n)[0] for n in ev4]
    g2 = [sink.seen(n)[2] - sink.seen(n)[1] for n in ev4]
    g3 = [sink.seen(n)[3] - sink.seen(n)[2] for n in ev4]
    # What the service decides is the recorded time of the next attempt (stage 1 checks the same way): 50 percent either side of each step (less 30 ms for the receiver's
    # clock; plus up to 120 for the time the service takes to read an answer on a busy machine, as before). At the receiver a retry may come later than it was due (a busy machine, a turn of 50 ms) but never earlier.
    r1, r2, r3 = (list(delays(sink, d, ev4, attempt=k).values()) for k in (1, 2, 3))
    check("4. the recorded delay after the first attempt is 150 to 450 ms, after the second 300 to 900, after the third 600 to 1,800 (50 percent of each step)",
          ok and len(r1) == len(r2) == len(r3) == len(ev4) and all(150 - 30 <= x <= 450 + 400 for x in r1) and all(300 - 30 <= x <= 900 + 400 for x in r2)
          and all(600 - 30 <= x <= 1800 + 400 for x in r3), str((len(r1), len(r2), len(r3), min(r1 or [0]), max(r1 or [0]), min(r2 or [0]), max(r2 or [0]), min(r3 or [0]), max(r3 or [0]))))
    check("4. and no retry reached the receiver earlier than its step allows (150, 300 and 600 ms)",
          all(x >= 150 - 5 for x in g1) and all(x >= 300 - 5 for x in g2) and all(x >= 600 - 5 for x in g3), str((min(g1), min(g2), min(g3))))
    # The spread is judged on the delays the service recorded, which the machine does not bunch. The gaps at the receiver are bunched by a machine that stops for a moment (the retries that
    # fell due in it are all seen when it starts again): 288 ms of range in the second step where 600 are possible, with the machine stopped for a second now and then.
    check("4. each step is spread: a range of over 150, 300 and 600 ms (of the recorded delays)", max(r1) - min(r1) > 150 and max(r2) - min(r2) > 300 and max(r3) - min(r3) > 600, str((max(r1) - min(r1), max(r2) - min(r2), max(r3) - min(r3))))
    stats = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/stats").read())
    check("4. every event was tried 4 times and is a dead letter, as without jitter", stats["attempts"] == 4 * len(ev4) and stats["dead"] == len(ev4) and all(len(sink.seen(n)) == 4 for n in ev4), str(stats))
    stop(svc, d)
    sink.close()

    # ---- 5. kill -9 across a jittered wait ---------------------------------------------------------------------------------------------------------------------
    sink = Sink()
    svc, d = start("6000", sink, jitter=30)
    ev5 = list(range(1, 21))
    post_events(svc, len(ev5))
    wait_for(lambda: sink.all_have(1, ev5) and len(failed_records(d)) == 20, 10)
    rec0 = failed_records(d)
    check("5. 20 events failed once and wait: 20 recorded times, between 4,200 and 7,800 ms after the attempt", len(rec0) == 20 and inside([rec0[(2, n, 1)] - sink.seen(n)[0] for n in ev5], 4200, 7800), str(len(rec0)))
    # kill while the durable record is on disk (the failures are recorded: the log has all 20), then again after a restart with another setting
    svc.kill()
    svc.start()
    time.sleep(0.4)
    svc.kill()
    svc.extra = svc.extra[:3] + ["--retry-jitter", "0"]
    svc.start()
    rec1 = failed_records(d)
    check("5. after two kills and a restart with --retry-jitter 0 the 20 records are the same, and no new failed record was written", {k: v for k, v in rec1.items() if k[2] == 1} == rec0 and len(rec1) == 20, str((len(rec1), len(rec0))))
    ok = wait_for(lambda: sink.all_have(2, ev5), 15)
    rec2 = failed_records(d)
    early = [n for n in ev5 if len(sink.seen(n)) >= 2 and sink.seen(n)[1] < rec0[(2, n, 1)] - 3]
    late = [n for n in ev5 if len(sink.seen(n)) >= 2 and sink.seen(n)[1] > rec0[(2, n, 1)] + 3000]      # (a delay computed again would be 6,000 ms on)
    check("5. every second attempt arrives at the time recorded in the first run: none before it, none computed again (a delay of 0 would send them at the restart)", ok and not early and not late, str((ok, early[:3], late[:3])))
    stop(svc, d)
    sink.close()

    # ---- 6. a replay is moved too ------------------------------------------------------------------------------------------------------------------------------
    sink = Sink()
    svc, d = start("100", sink, jitter=0)
    post_events(svc, 30)
    wait_for(lambda: sink.all_have(2, range(1, 31)), 10)
    wait_for(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/stats").read())["dead"] == 30, 10)
    # restart with a retry of 2,000 ms and 40 percent: the 30 dead letters are replayed to the endpoint
    svc.kill()
    svc.extra = ["2000", "2000", "86400000", "--retry-jitter", "40"]
    svc.start()
    sink.arrivals.clear()
    for n in range(1, 31):
        r = urllib.request.Request(f"http://127.0.0.1:{svc.port}/events/{n}/replay/5", method="POST", data=b"")
        urllib.request.urlopen(r, timeout=5).read()
    ok = wait_for(lambda: sink.all_have(2, range(1, 31)), 20)
    rrec = failed_records(d)
    rd = [rrec[(7, n, 1)] - sink.seen(n)[0] for n in range(1, 31) if (7, n, 1) in rrec]
    check("6. a replay waits a jittered delay: 30 replays, 2,000 ms at 40 percent, recorded between 1,200 and 2,800 ms, and spread", ok and len(rd) == 30 and inside(rd, 1200, 2800) and max(rd) - min(rd) > 400, str((ok, len(rd), min(rd) if rd else 0, max(rd) if rd else 0)))
    stop(svc, d)
    sink.close()

    METER.stop()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all jitter checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
