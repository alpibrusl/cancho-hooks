#!/usr/bin/env python3
"""Why a delivery attempt failed (docs/production.md 0.4; docs/design.md section 34.3), against a receiver made for each reason.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/reason_test.py build/hooks      (its tables `endpoints` and `attempts` are emptied)

One event is sent to ten endpoints at once, each of which fails in its own way; the schedule is a minute, so each is attempted once. What is checked, for every one:
the reason in `GET /events/1/attempts` (and the `status` column, which keeps the coarse value it always had), in `/metrics`' counters by reason, in the `attempts`
table's `reason` column, and in the delivery log as a record of kind 14 next to the outcome (read here with the independent reader); then, after a stop and a start,
that the last failure of each endpoint is read back from the log; and that a replay's reason is told from a window's.

    refused port         connect_refused        status -1       silent past the deadline   no_response     status -3
    reset mid-response   reset                  status -4       closes without a word      closed_early    status -4
    garbage              bad_response           status -4       500 / 404 / 301            status_5xx / status_4xx / status_3xx
    410                  gone (and the endpoint is disabled)    a blackholed address       connect_timeout status -3 (when this host has one)
    204                  none
"""
import json
import os
import shutil
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()
BLACKHOLE = "10.255.255.1"


def blackholed():
    """Does a connection to the blackhole address hang (SYN unanswered) here? Not every network does that: some refuse at once."""
    s = socket.socket()
    s.setblocking(False)
    s.connect_ex((BLACKHOLE, 9))
    import select
    ready = select.select([], [s], [], 1.0)[1]
    s.close()
    return not ready


def main():
    L.apply_schema()
    L.psql("truncate endpoints, attempts")
    peers = {
        "ok": L.Peer("ok"),
        "silent": L.Peer("silent"),
        "reset": L.Peer("reset"),
        "close": L.Peer("close"),
        "garbage": L.Peer("garbage"),
        "500": L.Peer("status", status=500),
        "404": L.Peer("status", status=404),
        "301": L.Peer("status", status=301),
        "410": L.Peer("status", status=410),
    }
    rows = [("ok", peers["ok"].port, None), ("refused", L.closed_port(), None)] + [(k, p.port, None) for k, p in peers.items() if k != "ok"]
    hole = blackholed()
    if hole:
        rows.append(("blackhole", 9, BLACKHOLE))
    ids = {}
    for i, (name, port, host) in enumerate(rows):
        ids[name] = i
        L.psql(f"insert into endpoints values ({i}, '{host or '127.0.0.1'}', {port}, '{L.secret()}')")
    want = {   # name: (reason, legacy status in the history, attempts row `status` is the HTTP status for the ones that answered)
        "ok": ("none", 204), "refused": ("connect_refused", -1), "silent": ("no_response", -3), "reset": ("reset", -4), "close": ("closed_early", -4),
        "garbage": ("bad_response", -4), "500": ("status_5xx", 500), "404": ("status_4xx", 404), "301": ("status_3xx", 301), "410": ("gone", 410),
    }
    if hole:
        want["blackhole"] = ("connect_timeout", -3)
    d = L.free_dir()
    args = ["--schedule", "60000", "--deadline-ms", "800", *L.pg_flags()]
    svc = L.Service(BIN, d, args)
    check("0. the service starts with a database and %d endpoints" % len(rows), svc.start(), svc.stderr())
    status, body = svc.post_event(1)
    check("0. one event", status == 202 and json.loads(body)["id"] == 1)
    n = len(rows)
    ok = L.wait_for(lambda: svc.stats()["attempts"] == n and svc.stats()["history_written"] == n, 20)
    check("0. every endpoint was attempted once and every attempt reached the history", ok, str(svc.stats()))

    status, data = svc.get("/events/1/attempts")
    att = {a["endpoint"]: a for a in json.loads(data)}
    check("0. GET /events/1/attempts has a row for each", status == 200 and sorted(att) == sorted(ids.values()), str(att))
    for name, (reason, legacy) in want.items():
        a = att[ids[name]]
        check(f"1. {name}: /attempts says reason {reason!r} and status {legacy}", a["reason"] == reason and a["status"] == legacy, str(a))
    check("1. the delivered attempt says outcome delivered; the 410 says dead; every other says failed",
          att[ids["ok"]]["outcome"] == "delivered" and att[ids["410"]]["outcome"] == "dead" and
          all(att[ids[k]]["outcome"] == "failed" for k in want if k not in ("ok", "410")))
    check("1. the silent receiver's attempt took about the deadline (800 ms), not less", 700 <= att[ids["silent"]]["latency_ms"] <= 2500, str(att[ids["silent"]]))
    if hole:
        check("1. the blackholed address took about the deadline as well", 700 <= att[ids["blackhole"]]["latency_ms"] <= 2500, str(att[ids["blackhole"]]))

    m = svc.metrics()
    by_reason = {dict(k)["reason"]: int(v) for k, v in m.series("hooks_attempt_failures_total").items() if v}
    expect = {reason: 1 for name, (reason, _) in want.items() if reason != "none"}
    check("2. /metrics counts one failure for each reason, and none for the delivery", by_reason == expect, f"{by_reason} {expect}")
    check("2. ... and every reason that did not happen is a series at 0 (26 series: 16 until names and TLS, docs/design.md section 40)", len(m.series("hooks_attempt_failures_total")) == 26)
    check("2. the outcomes: 1 delivered, 1 dead (the 410), the rest failed", (m.value("hooks_attempts_total", outcome="delivered"), m.value("hooks_attempts_total", outcome="dead"),
          m.value("hooks_attempts_total", outcome="failed")) == (1, 1, n - 2))

    table = dict((int(e), (int(r), int(s))) for e, r, s in L.psql("select endpoint, reason, status from attempts where event = 1"))
    rid = {v: k for k, v in L.REASONS.items()}
    check("3. the attempts table's reason column: the number of each reason", all(table[ids[k]][0] == (0 if r == "none" else rid[r]) for k, (r, _) in want.items()), str(table))
    check("3. ... and its status column kept the coarse values", all(table[ids[k]][1] == s for k, (_, s) in want.items()), str(table))

    kinds, reasons = L.log_counts(d)
    log_reason = {}
    slot_of = {i: i for i in ids.values()}
    for kind, e, ev, attempts, nxt in L.outcomes(d):
        if kind == 14:
            log_reason[e] = L.REASONS[nxt % 256]
            assert ev == 1 and attempts == 1 and nxt < 256, (e, ev, attempts, nxt)
    check("4. the delivery log has a record of kind 14 for each failure, next to its outcome", log_reason == {slot_of[ids[k]]: r for k, (r, _) in want.items() if r != "none"}, str(log_reason))
    seq = [k for k, e, *_ in L.outcomes(d) if e == ids["500"] and k in (2, 14)]
    check("4. ... right after the outcome record it explains (failed, then reason)", seq == [2, 14], str(seq))
    check("4. the delivered attempt has no reason record, the 410 has one after its `dead` record", not [1 for k, e, *_ in L.outcomes(d) if k == 14 and e == ids["ok"]] and
          [k for k, e, *_ in L.outcomes(d) if e == ids["410"] and k in (3, 14)] == [3, 14])
    check("4. the record is 40 bytes of outcome like the others (the log still parses whole)", L.events_log(d)[0] and kinds["reason"] == n - 1)

    # a replay's reason is told from a window's
    st, body, _ = svc.request("POST", "/events/1/replay/%d" % ids["500"])
    check("5. a replay of the event to the 500 endpoint is accepted", st == 202, str((st, body)))
    ok = L.wait_for(lambda: svc.stats()["attempts"] == n + 1 and svc.stats()["history_written"] == n + 1, 15)
    st, data = svc.get("/events/1/attempts")
    rep = [a for a in json.loads(data) if a["endpoint"] == ids["500"] and a["replay"]]
    check("5. the replay's attempt is a row of its own with reason status_5xx", ok and len(rep) == 1 and rep[0]["reason"] == "status_5xx" and rep[0]["status"] == 500, str(rep))
    rr = [nxt for k, e, ev, at, nxt in L.outcomes(d) if k == 14 and e == ids["500"]]
    check("5. in the log it is the same reason plus 256 (a replay's attempt)", rr == [12, 12 + 256], str(rr))
    check("5. /metrics counts it", int(svc.metrics().value("hooks_attempt_failures_total", reason="status_5xx")) == 2)

    # the last failure of each endpoint survives a restart
    code = svc.stop()
    check("6. SIGTERM: exit status 0", code == 0, str(code))
    svc2 = L.Service(BIN, d, args)
    svc2.start()
    m2 = svc2.metrics()
    lf = {int(dict(k)["endpoint"]): dict(k)["reason"] for k in m2.series("hooks_endpoint_last_failure")}
    check("6. after a restart each endpoint's last failure is read back from the log", lf == {ids[k]: r for k, (r, _) in want.items() if r != "none"}, str(lf))
    check("6. ... and the counters are zero again", m2.total("hooks_attempt_failures_total") == 0)
    svc2.stop()

    # a delivery after failures ends the last failure, and is what recovery reads too
    peers["500"].mode = "ok"
    svc3 = L.Service(BIN, d, args)
    svc3.start()
    svc3.request("POST", "/events/1/replay/%d" % ids["500"])
    L.wait_for(lambda: svc3.stats()["delivered"] >= 1, 10)
    lf3 = {int(dict(k)["endpoint"]) for k in svc3.metrics().series("hooks_endpoint_last_failure")}
    check("7. a delivery ends the endpoint's last failure", ids["500"] not in lf3 and ids["404"] in lf3, str(lf3))
    svc3.stop()
    svc4 = L.Service(BIN, d, args)
    svc4.start()
    lf4 = {int(dict(k)["endpoint"]) for k in svc4.metrics().series("hooks_endpoint_last_failure")}
    check("7. ... also after another restart", ids["500"] not in lf4 and ids["404"] in lf4, str(lf4))
    svc4.stop()
    shutil.rmtree(d)
    for p in peers.values():
        p.close()
    return check.finish("reason")


if __name__ == "__main__":
    sys.exit(main())
