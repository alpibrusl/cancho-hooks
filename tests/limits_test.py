#!/usr/bin/env python3
"""The pace of an endpoint's attempts: its concurrency and its rate (docs/design.md section 39.4; production.md P1.2).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/limits_test.py build/hooks

The database must exist; the test applies sql/schema.sql and empties `endpoints` and `attempts` itself.

  1. concurrency, observed at the receiver: with `--endpoint-concurrency 2` an endpoint that follows the service never has more than 2 requests in
     flight and does have 2; an endpoint with `"concurrency": 4` of its own has 4 and not 5; one with 1 has 1; the default (no flag) is 8 and not 9
  2. `GET /endpoints` shows the limits in force; `PATCH` changes them with no restart (3, then `null` follows the service again)
  3. rate, observed at the receiver by counting the requests in every window of one second: an endpoint with `"rate": 10` is never sent more than
     rate + depth (10 + 1) in a second plus a request of slack for the receiver's own scheduling, and over the run it is sent about 10 a second (not
     fewer: a limit that stalls is a bug too); every event arrives exactly once, no attempt failed, no retry was counted: a held-back event is not a failure
  4. a slow limited endpoint does not slow another: one at 2 a second beside one with no limit, which is sent 300 events in a couple of seconds
     while the limited one is still being sent its first
  5. `/metrics` counts the attempts that were held back, per endpoint: the limited one, and not the other
  6. the service's `--endpoint-rate` is the default of an endpoint with none of its own, which overrides it either way (faster and slower)
  7. `kill -9` while an endpoint is held back and events wait: every event is delivered, none is repeated beyond what was in flight, and the rate holds
     across the restart (one burst of the bucket's depth is allowed to cross it: the bucket is memory)
  8. every way a limit can be wrong is a 400 with its reason, in `POST` and `PATCH`; the limits are in the table's columns, in `endpoints.conf`
     (`concurrency=` and `rate=`), and `--import-endpoints` carries them; a bad word is a refusal to start naming the line
  9. the settings: `endpoint-concurrency`, `endpoint-rate` and `retry-jitter` from a file and from flags, in `GET /config`, and every bad value refused
 10. `psql -f sql/schema.sql` upgrades a table made before the columns: the rows keep their values and follow the service
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from endpoint_kit import *  # noqa: E402,F401,F403
import endpoint_kit as K  # noqa: E402
import chaos  # noqa: E402
from loadmeter import LoadMeter  # noqa: E402

http.server.HTTPServer.request_queue_size = 512


class Probe:
    """A receiver that holds every request for `hold` seconds, and remembers when each began, which event it was, and the most it ever had at once."""

    def __init__(self, hold=0.0, status=204, port=0):
        self.times, self.inflight, self.max_inflight, self.hold, self.status = [], 0, 0, hold, status
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    n = json.loads(body).get("n")
                except ValueError:
                    n = None
                with outer.lock:
                    outer.inflight += 1
                    outer.max_inflight = max(outer.max_inflight, outer.inflight)
                    outer.times.append((time.time(), n))
                if outer.hold:
                    time.sleep(outer.hold)
                code = outer.status(n) if callable(outer.status) else outer.status
                # No longer in flight from the moment the answer is sent: the service may start the next attempt as soon as it has read it, and a count
                # taken after the send would see two attempts of a limit of one (the receiver's thread, not the service, would be late).
                with outer.lock:
                    outer.inflight -= 1
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self):
        with self.lock:
            return len(self.times)

    def events(self):
        with self.lock:
            return sorted(n for _, n in self.times)

    def stamps(self):
        with self.lock:
            return sorted(t for t, _ in self.times)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def busiest_second(stamps):
    """The most requests in any window of one second (a window starts at a request)."""
    best, j = 0, 0
    for i, t in enumerate(stamps):
        while j < len(stamps) and stamps[j] < t + 1.0:
            j += 1
        best = max(best, j - i)
    return best


def median_gap(stamps):
    """The middle of the gaps between one request and the next at a receiver: what a rate is, when a few requests were late or were seen together."""
    g = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    return g[len(g) // 2] if g else float("nan")


# The receiver is a Python thread, and it stamps a request when it gets to it: if the machine takes the processor from this process for a time `stall` (the longest is measured, by
# `LoadMeter`, while the check is made), the requests that came in that time are stamped together, and `stall * rate` more of them fall in one second. That is what the bound on
# a second is given on top of its 12 (the rate and the depth and one). A rate that was reached is judged by the middle gap between requests and not by the whole span, which one
# late request lengthens (3.72 s for the 3.2 of nine a second, with the processor shared with six busy loops) and the middle does not move.


def flags(*more):
    return pg_flags() + ["--admin-token", TOKEN, *more]


def make(svc, port, **extra):
    st, body = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": port, **extra})
    return st, body


def post_many(svc, first, last, typ="t"):
    for n in range(first, last + 1):
        st, _ = post_event(svc, n, typ)
        assert st == 202, st


def metric(svc, name, endpoint):
    st, text = req(svc, "GET", "/metrics", raw=True)
    for line in text.decode().splitlines():
        if line.startswith(name + '{endpoint="' + str(endpoint) + '"}'):
            return int(float(line.rsplit(" ", 1)[1]))
    return None


def main():
    reset_db()
    # ---- 1. concurrency observed at the receiver ---------------------------------------------------------------
    d = tmp()
    svc = start(d, extra=flags("--endpoint-concurrency", "2"))
    ra, rb, rc = Probe(hold=0.3), Probe(hold=0.3), Probe(hold=0.3)
    st_a, a = make(svc, ra.port)
    st_b, b = make(svc, rb.port, concurrency=4)
    st_c, c = make(svc, rc.port, concurrency=1)
    check("1. three endpoints are made, the second and third with a limit of their own", (st_a, st_b, st_c) == (201, 201, 201), str((a, b, c)))
    n_events = 16
    t0 = time.time()
    post_many(svc, 1, n_events)
    done = wait_for(lambda: ra.count() == n_events and rb.count() == n_events and rc.count() == n_events, 30)
    check("1. every event reaches every endpoint, once", done and ra.events() == rb.events() == rc.events() == list(range(1, n_events + 1)),
          str((ra.count(), rb.count(), rc.count())))
    check("1. an endpoint that follows the service (2) has 2 in flight and not 3", ra.max_inflight == 2, str(ra.max_inflight))
    check("1. an endpoint with 4 of its own has 4 and not 5", rb.max_inflight == 4, str(rb.max_inflight))
    check("1. an endpoint with 1 of its own has 1", rc.max_inflight == 1, str(rc.max_inflight))
    wait_for(lambda: svc.proc.poll() is None and get(svc, "/stats")["delivered"] == 3 * n_events, 10)
    s = get(svc, "/stats")
    check("1. no attempt failed and none was repeated: held-back events are not failures", s["failed"] == 0 and s["dead"] == 0 and s["attempts"] == 3 * n_events, str(s))
    # ---- 2. GET shows the limits; PATCH changes them -------------------------------------------------------------
    lim = {i: (e["concurrency"], e["rate"]) for i, e in endpoints(svc).items()}
    check("2. GET /endpoints shows the limits in force: the service's for the first, the others' own", lim == {a["id"]: (2, 0), b["id"]: (4, 0), c["id"]: (1, 0)}, str(lim))
    one = get(svc, f"/endpoints/{c['id']}")
    check("2. GET /endpoints/:id shows them too", (one["concurrency"], one["rate"]) == (1, 0), str(one))
    st, _ = req(svc, "PATCH", f"/endpoints/{c['id']}", {"concurrency": 3})
    check("2. PATCH {concurrency: 3} is accepted and GET says 3 at once", st == 200 and get(svc, f"/endpoints/{c['id']}")["concurrency"] == 3, str(st))
    rc.max_inflight = 0
    n0 = rc.count()
    post_many(svc, 101, 112)
    wait_for(lambda: rc.count() == n0 + 12, 20)
    check("2. ... and the next events are sent 3 at a time, with no restart", rc.max_inflight == 3, str(rc.max_inflight))
    st, _ = req(svc, "PATCH", f"/endpoints/{c['id']}", {"concurrency": None})
    check("2. PATCH {concurrency: null} follows the service again (2)", st == 200 and get(svc, f"/endpoints/{c['id']}")["concurrency"] == 2, str(st))
    st, _ = req(svc, "PATCH", f"/endpoints/{c['id']}", {"concurrency": 0, "rate": 5})
    one = get(svc, f"/endpoints/{c['id']}")
    check("2. 0 is the same, and a rate alone or beside it is a change (PATCH {rate: 5} is not 'nothing to change')", st == 200 and (one["concurrency"], one["rate"]) == (2, 5), str((st, one)))
    st, _ = req(svc, "PATCH", f"/endpoints/{c['id']}", {"rate": 0})
    check("2. rate 0 follows the service again (no limit)", st == 200 and get(svc, f"/endpoints/{c['id']}")["rate"] == 0, str(st))
    rows = {int(r[0]): (int(r[1]), int(r[2])) for r in psql("select id, concurrency, rate from endpoints")}
    check("2. the table's columns hold the endpoint's own limits (0: follows the service)", rows == {a["id"]: (0, 0), b["id"]: (4, 0), c["id"]: (0, 0)}, str(rows))
    stop(svc)
    for r in (ra, rb, rc):
        r.close()
    shutil.rmtree(d)

    # the default: 8, and not 9
    reset_db()
    d = tmp()
    svc = start(d)
    ra = Probe(hold=0.4)
    make(svc, ra.port)
    post_many(svc, 1, 40)
    wait_for(lambda: ra.count() == 40, 30)
    check("1. by default an endpoint has 8 in flight and not 9", ra.max_inflight == 8 and ra.count() == 40, str((ra.max_inflight, ra.count())))
    stop(svc)
    ra.close()
    shutil.rmtree(d)

    # ---- 3. rate observed at the receiver ------------------------------------------------------------------------
    reset_db()
    d = tmp()
    svc = start(d)
    rr = Probe()
    st, r = make(svc, rr.port, rate=10)
    check("3. an endpoint with a rate of 10 is made", st == 201, str(r))
    total = 60
    meter = LoadMeter()
    t0 = time.time()
    post_many(svc, 1, total)
    posted = time.time() - t0
    done = wait_for(lambda: rr.count() == total, 30)
    meter.stop()
    stamps = rr.stamps()
    span = stamps[-1] - stamps[0]
    check("3. all 60 events arrive, once each", done and rr.events() == list(range(1, total + 1)), str(rr.count()))
    busy = busiest_second(stamps)
    extra = int(meter.stall_s * 10)
    check("3. no second sees more than rate + depth + 1 slack (12), and what the machine took from the receiver (%d)" % extra, busy <= 12 + extra, f"{busy} in a second, the receiver waited {meter.stall_s * 1000:.0f} ms for its turn")
    check("3. the limit is reached and not undershot: 59 intervals of 100 ms take 5.9 s (not under 85%), the middle gap is 100 ms (within 15%)",
          span >= 5.9 * 0.85 and 0.085 <= median_gap(stamps) <= 0.115, f"{span:.2f} s, {median_gap(stamps) * 1000:.0f} ms")
    # (posting 60 events took 9.8 s, 160 ms a request, with the processor shared with six busy loops and a disk in use: the posts were then slower than the rate and nothing was held back by
    # it. What is asked is that the limit does not slow the posting, so posting is allowed as many times as long as the machine gave this process less of the processor)
    check("3. ... so they were held back, and posting them took far less than that", posted < span * 0.5 * max(1.0, meter.slowdown()), f"posted in {posted:.2f} s, sent over {span:.2f} s (scale {meter.slowdown():.1f})")
    s = get(svc, "/stats")
    check("3. no attempt failed and none was repeated; the retry counter did not move", s["attempts"] == total and s["failed"] == 0 and s["dead"] == 0 and s["delivered"] == total, str(s))
    held = metric(svc, "hooks_endpoint_throttled_total", r["id"])
    check("5. /metrics counts the attempts that were held back for the limited endpoint", held is not None and held >= 10, str(held))
    stop(svc)
    rr.close()
    shutil.rmtree(d)

    # a rate whose interval is not a multiple of the loop's 50 ms (9 a second: 111 ms) is still reached, because the loop sleeps until the next token is due
    reset_db()
    d = tmp()
    svc = start(d)
    r9 = Probe()
    make(svc, r9.port, rate=9)
    post_many(svc, 1, 30)
    wait_for(lambda: r9.count() == 30, 30)
    span9 = r9.stamps()[-1] - r9.stamps()[0]
    check("3. a rate of 9 a second: 29 intervals of 111 ms take 3.2 s (not under 88%), the middle gap is 111 ms (within 12 percent), not the 150 of waking every 50 ms",
          span9 >= 29 / 9 * 0.88 and 1 / 9 * 0.88 <= median_gap(r9.stamps()) <= 1 / 9 * 1.12, f"{span9:.2f} s, {median_gap(r9.stamps()) * 1000:.0f} ms")
    stop(svc)
    r9.close()
    shutil.rmtree(d)

    # the replays of an endpoint obey its rate too
    reset_db()
    d = tmp()
    mode = {"code": 500}
    svc = start(d, schedule="40")
    rd = Probe(status=lambda n: mode["code"])
    ident = make(svc, rd.port)[1]["id"]
    post_many(svc, 1, 20)
    wait_for(lambda: get(svc, "/stats")["dead"] == 20, 20)
    st, _ = req(svc, "PATCH", f"/endpoints/{ident}", {"rate": 5})
    mode["code"] = 204
    base = rd.count()
    meter = LoadMeter()
    st2, r = req(svc, "POST", f"/endpoints/{ident}/replay-dead")
    wait_for(lambda: rd.count() == base + 20, 30)
    meter.stop()
    stamps = rd.stamps()[base:]
    check("3. 20 replays at a rate of 5: all sent, no second sees more than 5 + 1 + 1 slack (and what the machine took from the receiver: %d), and they take 19 intervals of 200 ms (not under 85 percent), the middle gap 200 ms (within 15 percent)" % int(meter.stall_s * 5),
          st == 200 and st2 == 202 and len(stamps) == 20 and busiest_second(stamps) <= 7 + int(meter.stall_s * 5) and stamps[-1] - stamps[0] >= 19 / 5 * 0.85 and 0.17 <= median_gap(stamps) <= 0.23,
          str((st, st2, r, len(stamps), busiest_second(stamps), stamps[-1] - stamps[0], median_gap(stamps), meter.stall_s)))
    stop(svc)
    rd.close()
    shutil.rmtree(d)

    # ---- 4. a slow limited endpoint does not slow another; 5. the metric is per endpoint ----------------------------
    reset_db()
    d = tmp()
    svc = start(d)
    slow, fast = Probe(), Probe()
    st1, s1 = make(svc, slow.port, rate=2)
    st2, f1 = make(svc, fast.port)
    t0 = time.time()
    post_many(svc, 1, 300)
    ok = wait_for(lambda: fast.count() == 300, 20)
    fast_took = time.time() - t0
    at_that_time = slow.count()
    # (a couple of seconds on a quiet machine; the limited endpoint would take 150 s for the same 300, so "not slowed by it" is well under that: a fifth)
    check("4. the unlimited endpoint gets all 300 events in far less than the 150 s the limited one needs (took %.1f s, under 30)" % fast_took, ok and fast_took < 30, str((fast.count(), fast_took)))
    check("4. ... while the limited one (2 a second) has had a handful (%d: at most what 2 a second and a bucket give in that time) and not all" % at_that_time, 1 <= at_that_time <= 2 * fast_took + 10, str((at_that_time, fast_took)))
    check("4. the limited one's cursor is behind and the other's is at the end", cursors(svc)[f1["id"]] == 300 and cursors(svc)[s1["id"]] < 300, str(cursors(svc)))
    check("5. /metrics: the limited endpoint was held back, the other never", (metric(svc, "hooks_endpoint_throttled_total", s1["id"]) or 0) >= 1 and metric(svc, "hooks_endpoint_throttled_total", f1["id"]) == 0,
          str((metric(svc, "hooks_endpoint_throttled_total", s1["id"]), metric(svc, "hooks_endpoint_throttled_total", f1["id"]))))
    # the held-back events are delivered later: all of them, once, in the end (raise the rate with no restart to finish sooner)
    st, _ = req(svc, "PATCH", f"/endpoints/{s1['id']}", {"rate": 2000})
    ok = wait_for(lambda: slow.count() >= 300, 30)
    check("4. the held-back events are delivered later, all 300, each once", ok and slow.events() == list(range(1, 301)) and st == 200, str(slow.count()))
    stop(svc)
    slow.close()
    fast.close()
    shutil.rmtree(d)

    # ---- 6. the service's rate is the default; an endpoint's own overrides it either way -------------------------------
    reset_db()
    d = tmp()
    svc = start(d, extra=flags("--endpoint-rate", "5"))
    follows, quick, lazy = Probe(), Probe(), Probe()
    _, f = make(svc, follows.port)
    _, q = make(svc, quick.port, rate=100)
    _, z = make(svc, lazy.port, rate=1)
    cfg = get(svc, "/config")
    check("6. GET /config says endpoint-rate 5, endpoint-concurrency 8, retry-jitter 10", (cfg["endpoint-rate"], cfg["endpoint-concurrency"], cfg["retry-jitter"]) == (5, 8, 10), str(cfg))
    lim = {i: e["rate"] for i, e in endpoints(svc).items()}
    check("6. GET /endpoints: 5 for the one that follows, 100 and 1 for the others", lim == {f["id"]: 5, q["id"]: 100, z["id"]: 1}, str(lim))
    t0 = time.time()
    post_many(svc, 1, 30)
    wait_for(lambda: quick.count() == 30, 15)
    quick_took = time.time() - t0
    # at 5 a second (the service's) 30 events take 6 s; at 100 a second they are sent as they are posted: "well under" is under half of what the default would take
    check("6. at 100 a second 30 events take well under the 6 s of the default, a second or two (%.2f s, under 3)" % quick_took, quick.count() == 30 and quick_took < 3.0, str(quick_took))
    # the pace of the other two is their middle gap (200 ms at 5 a second, 1 s at 1): ten requests of the first and four of the second are enough to see it
    wait_for(lambda: follows.count() >= 10 and lazy.count() >= 4, 20)
    gf, gz = median_gap(follows.stamps()), median_gap(lazy.stamps())
    check("6. the endpoint that follows the service sends at 5 a second (middle gap %.0f ms of 200, within 15 percent) and the one at 1 a second at 1 (%.0f ms of 1,000)" % (gf * 1000, gz * 1000),
          0.17 <= gf <= 0.23 and 0.85 <= gz <= 1.15, str((follows.count(), lazy.count(), gf, gz)))
    stop(svc)
    for r in (follows, quick, lazy):
        r.close()
    shutil.rmtree(d)

    # ---- 7. kill -9 while events are held back ----------------------------------------------------------------------
    reset_db()
    d = tmp()
    svc = start(d)
    rk = Probe()
    st, k = make(svc, rk.port, rate=20)
    total = 120
    meter = LoadMeter()
    post_many(svc, 1, total)
    kills, starts = 0, [time.time()]
    while rk.count() < total and kills < 6:
        # a kill when 12 more have arrived since the last start (driven by progress, not by the clock)
        target = min(total, rk.count() + 12)
        if not wait_for(lambda: rk.count() >= target, 20):
            break
        if rk.count() >= total:
            break
        kill9(svc)
        kills += 1
        svc = start(d)
        starts.append(time.time())
    ok = wait_for(lambda: rk.count() >= total and set(rk.events()) == set(range(1, total + 1)), 40)
    meter.stop()
    extra = int(meter.stall_s * 20)
    ev = rk.events()
    repeats = len(ev) - len(set(ev))
    check("7. %d kill -9 while 120 events were held back at 20 a second: every event arrived" % kills, ok and kills >= 3, str((kills, rk.count())))
    check("7. ... and what was repeated is at most what was in flight at each kill (8 each)", repeats <= 8 * kills, str((repeats, kills)))
    # a window that does not cross a restart obeys the limit; one that does may hold one bucket's depth (2 tokens at 20 a second) more
    stamps = rk.stamps()
    seg = [[t for t in stamps if lo <= t < hi] for lo, hi in zip(starts, starts[1:] + [time.time() + 1])]
    worst_in = max((busiest_second(sg) for sg in seg if sg), default=0)
    check("7. inside each run of the service no second sees more than rate + depth + 1 (23, and what the machine took from the receiver: %d)" % extra, worst_in <= 23 + extra, str((worst_in, meter.stall_s)))
    worst_all = busiest_second(stamps)
    check("7. across the restarts no second sees more than the rate plus a bucket for each restart in it (it is memory)", worst_all <= 23 + 2 * kills + extra, str((worst_all, kills, meter.stall_s)))
    stop(svc)
    rk.close()
    shutil.rmtree(d)

    # ---- 8. the API refuses what is not a limit; the table, endpoints.conf and the import -----------------------------------
    reset_db()
    d = tmp()
    svc = start(d)
    rx = Probe()
    _, x = make(svc, rx.port)
    bad = [({"concurrency": 9}, "concurrency"), ({"concurrency": -1}, "concurrency"), ({"concurrency": 1.5}, "concurrency"), ({"concurrency": "2"}, "concurrency"),
           ({"concurrency": True}, "concurrency"), ({"concurrency": [2]}, "concurrency"), ({"rate": 100001}, "rate"), ({"rate": -1}, "rate"), ({"rate": 2.5}, "rate"),
           ({"rate": "10"}, "rate"), ({"rate": False}, "rate"), ({"rate": {}}, "rate"), ({"rate": 99999999999999999999}, "rate")]
    for body, word in bad:
        st1, o1 = req(svc, "PATCH", f"/endpoints/{x['id']}", body)
        st2, o2 = make(svc, rx.port, **body)
        check(f"8. {json.dumps(body)} is a 400 that names \"{word}\" at PATCH and at POST", st1 == 400 and st2 == 400 and word in json.dumps(o1) and word in json.dumps(o2), str((st1, o1, st2, o2)))
    ok_bodies = [({"concurrency": 8, "rate": 100000}, (8, 100000)), ({"concurrency": 1, "rate": 1}, (1, 1)), ({"rate": None, "concurrency": None}, (8, 0))]
    for body, want in ok_bodies:
        st, o = req(svc, "PATCH", f"/endpoints/{x['id']}", body)
        got = get(svc, f"/endpoints/{x['id']}")
        check(f"8. the edges {json.dumps(body)} are accepted", st == 200 and (got["concurrency"], got["rate"]) == want, str((st, o, got)))
    check("8. nothing was stored by the refusals: the table has one endpoint", psql("select count(*) from endpoints") == [("1",)], str(psql("select count(*) from endpoints")))
    st, o = req(svc, "PATCH", f"/endpoints/{x['id']}", {"concurrency": 3, "rate": 7, "types": ["a.*"]})
    row = psql(f"select concurrency, rate, types from endpoints where id = {x['id']}")
    check("8. the limits and the other members are stored together", st == 200 and row == [("3", "7", "a.*")], str((st, row)))
    stop(svc)
    # a restart reads them from the table
    svc = start(d)
    got = get(svc, f"/endpoints/{x['id']}")
    check("8. a restart reads the limits from the table", (got["concurrency"], got["rate"]) == (3, 7), str(got))
    stop(svc)
    # endpoints.conf, and the import
    sec = secret()
    d2 = tmp()
    with open(os.path.join(d2, "endpoints.conf"), "w") as f:
        f.write(f"1 127.0.0.1 {rx.port} {sec} concurrency=2 rate=9\n2 127.0.0.1 {rx.port} {sec} types=a.* rate=3 concurrency=5\n3 127.0.0.1 {rx.port} {sec}\n")
    svc = start(d2, extra=["--endpoint-concurrency", "6"])
    lim = {i: (e["concurrency"], e["rate"]) for i, e in endpoints(svc).items()}
    check("8. endpoints.conf: concurrency= and rate= in either order, and none follows the service", lim == {1: (2, 9), 2: (5, 3), 3: (6, 0)}, str(lim))
    stop(svc)
    reset_db()
    out = subprocess.run([BIN, "--port", "1", "--dir", d2, "--import-endpoints", "1", "--allow-private-hosts", "1", *pg_flags()], capture_output=True, text=True, timeout=30)
    rows = psql("select id, concurrency, rate from endpoints order by id")
    check("8. --import-endpoints carries them into the table", out.returncode == 0 and rows == [("1", "2", "9"), ("2", "5", "3"), ("3", "0", "0")], str((out.returncode, out.stderr, rows)))
    svc = start(d2)
    lim = {i: (e["concurrency"], e["rate"]) for i, e in endpoints(svc).items()}
    check("8. ... and the service reads them back from the table", lim == {1: (2, 9), 2: (5, 3), 3: (8, 0)}, str(lim))
    stop(svc)
    for word in ("concurrency=0", "concurrency=9", "concurrency=", "concurrency=x", "concurrency=-1", "rate=0", "rate=100001", "rate=", "rate=1.5", "rate=2 rate=3",
                 "concurrency=2 concurrency=2", "concurency=2", "rates=2"):
        with open(os.path.join(d2, "endpoints.conf"), "w") as f:
            f.write(f"1 127.0.0.1 {rx.port} {sec} {word}\n")
        out = subprocess.run([BIN, "--port", str(chaos.free_port()), "--dir", d2, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=20)
        check(f"8. endpoints.conf with `{word}` is a refusal to start naming the line", out.returncode == 13 and "line 1" in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)
    shutil.rmtree(d2)
    rx.close()

    # ---- 9. the settings ----------------------------------------------------------------------------------------------------
    d = tmp()
    base = ["--port", str(chaos.free_port()), "--dir", d, "--allow-private-hosts", "1"]
    cfgfile = os.path.join(d, "hooks.conf")
    with open(cfgfile, "w") as f:
        f.write("endpoint-concurrency = 3\nendpoint-rate = 50\nretry-jitter = 25\n")
    svc = start(d, extra=["--config", cfgfile])
    cfg = get(svc, "/config")
    check("9. the settings from a file", (cfg["endpoint-concurrency"], cfg["endpoint-rate"], cfg["retry-jitter"]) == (3, 50, 25), str(cfg))
    stop(svc)
    svc = start(d, extra=["--config", cfgfile, "--endpoint-concurrency", "5", "--endpoint-rate=0", "--retry-jitter", "0"])
    cfg = get(svc, "/config")
    check("9. flags beat the file", (cfg["endpoint-concurrency"], cfg["endpoint-rate"], cfg["retry-jitter"]) == (5, 0, 0), str(cfg))
    stop(svc)
    svc = start(d, extra=[])
    cfg = get(svc, "/config")
    check("9. the defaults are 8, 0 and 10", (cfg["endpoint-concurrency"], cfg["endpoint-rate"], cfg["retry-jitter"]) == (8, 0, 10), str(cfg))
    stop(svc)
    for flag, value in (("endpoint-concurrency", "0"), ("endpoint-concurrency", "9"), ("endpoint-concurrency", "-1"), ("endpoint-concurrency", "x"), ("endpoint-concurrency", ""),
                        ("endpoint-rate", "-1"), ("endpoint-rate", "100001"), ("endpoint-rate", "1.5"), ("endpoint-rate", "x"),
                        ("retry-jitter", "51"), ("retry-jitter", "-1"), ("retry-jitter", "10%"), ("retry-jitter", "x")):
        out = subprocess.run([BIN, *base, f"--{flag}={value}"], capture_output=True, text=True, timeout=20)
        check(f"9. --{flag}={value} is refused (exit 2) and names the flag", out.returncode == 2 and flag in out.stderr, str((out.returncode, out.stderr)))
    shutil.rmtree(d)

    # ---- 10. psql -f sql/schema.sql upgrades a table made before the columns ----------------------------------------------------
    # the endpoints table as the file before this section made it (docs/design.md section 35.7), and the other tables the file makes: what a database made then has
    cut = """
create table endpoints (id integer primary key check (id between 0 and 999999), host text not null, port int not null check (port between 1 and 65535), secret text not null,
    types text not null default '', headers text not null default '', secret_old text not null default '', secret_old_until bigint not null default 0);
"""
    pg_env = dict(PSQL_ENV)
    psql(f"drop schema if exists limits_old cascade")
    psql("create schema limits_old")
    base_cmd = ["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1"]
    pg_env["PGOPTIONS"] = "-c search_path=limits_old"
    subprocess.run(base_cmd + ["-c", "set search_path = limits_old"], check=True, capture_output=True, env=pg_env)
    # the old table, made by the old file (without the columns), with rows in it
    old_file = os.path.join(tmp("hooks-limits-schema-"), "old.sql")
    with open(old_file, "w") as f:
        f.write(cut)
    subprocess.run(base_cmd + ["-f", old_file], check=True, capture_output=True, env=pg_env)
    subprocess.run(base_cmd + ["-c", "insert into endpoints (id, host, port, secret) values (7, '127.0.0.1', 9, 'whsec_x'), (8, '127.0.0.1', 10, 'whsec_y')"], check=True, capture_output=True, env=pg_env)
    cols = subprocess.run(base_cmd + ["-At", "-c", "select column_name from information_schema.columns where table_schema = 'limits_old' and table_name = 'endpoints' order by ordinal_position"],
                          capture_output=True, text=True, env=pg_env).stdout.split()
    check("10. the table made by the old file has no limits columns", "concurrency" not in cols and "rate" not in cols, str(cols))
    subprocess.run(base_cmd + ["-f", os.path.join(ROOT, "sql", "schema.sql")], check=True, capture_output=True, env=pg_env)
    rows = subprocess.run(base_cmd + ["-At", "-F", "|", "-c", "select id, concurrency, rate from endpoints order by id"], capture_output=True, text=True, env=pg_env).stdout.split()
    check("10. after `psql -f sql/schema.sql` the rows have the columns, 0 and 0 (they follow the service)", rows == ["7|0|0", "8|0|0"], str(rows))
    again = subprocess.run(base_cmd + ["-f", os.path.join(ROOT, "sql", "schema.sql")], capture_output=True, text=True, env=pg_env)
    check("10. ... and running the file again changes nothing", again.returncode == 0, again.stderr)
    bad = subprocess.run(base_cmd + ["-c", "insert into endpoints (id, host, port, secret, concurrency) values (9, 'h', 9, 's', 9)"], capture_output=True, text=True, env=pg_env)
    bad2 = subprocess.run(base_cmd + ["-c", "insert into endpoints (id, host, port, secret, rate) values (9, 'h', 9, 's', -1)"], capture_output=True, text=True, env=pg_env)
    check("10. the table refuses a concurrency above 8 and a negative rate itself", bad.returncode != 0 and bad2.returncode != 0, str((bad.stderr, bad2.stderr)))
    psql("drop schema limits_old cascade")
    finish("limits")


main()
