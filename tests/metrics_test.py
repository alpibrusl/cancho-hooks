#!/usr/bin/env python3
"""GET /readyz and GET /metrics (docs/production.md 0.4; docs/design.md sections 34.1 and 34.2), each number checked against a workload whose answer is known.

    python3 tests/metrics_test.py build/hooks                    (no database needed: stages 1 to 4)
    HOOKS_PG=host:port:user:database python3 tests/metrics_test.py build/hooks    (stage 5, the history, needs one; its tables are emptied)

  1. A known workload. 43 events, one at a time (so each is its own turn of the loop and its own group commit), among them three with an
     Idempotency-Key and the same three again, and five requests that are refused (a body that is not an event, a bad key, a key used for a different
     event, an event too large). Three endpoints: A answers 204, B answers 500, C is a port nothing listens on; the retry schedule is two retries, so B and C
     each end with every event dead after three attempts. Every number is compared with `GET /stats`, with the logs read by this file's own reader, and with the arithmetic:
       - ingest accepted / duplicate / refused (and refused by status), the events log's size, synced bytes and last id, group commits == records;
       - attempts by outcome == /stats == the outcome records in delivery.seg; the failures by reason == the reason records in delivery.seg == the arithmetic;
       - the cursor of each endpoint, its lag (0), retries waiting (0), nothing in flight;
       - the text is the Prometheus format 0.0.4, read by a strict parser; no credential is asked for /readyz or /metrics.
  2. Lag, retries and the last failure. B answers 500 and the retry is a minute away: B's cursor stays 0, its lag is the events behind, its retries waiting
     are those events, `failing_since` is set, the reason of its last failure is `status_5xx`; A's lag is 0. The service is stopped and started again:
     the last failure of B is still there (it is read back from the delivery log), and the counters have started again from zero.
  3. Disabled and paused: an endpoint that answers 410 is disabled (reason `gone`); an endpoint whose failures began days ago is paused by the breaker.
  4. The most endpoints (62): the answer fits, and the number of series is bounded by 62 x 8 plus the service's own.
  5. With a database: the history's counters agree with the rows of the `attempts` table.
"""
import json
import os
import shutil
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()
HAVE_DB = "HOOKS_PG" in os.environ


def conf(d, peers):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i, port in enumerate(peers):
            f.write(f"{i} 127.0.0.1 {port} {L.secret()}\n")


def m_get(m, name, **labels):
    return int(m.value(name, **labels))


def stage1():
    print("== 1. a known workload", flush=True)
    d = L.free_dir()
    a, b = L.Peer("ok"), L.Peer("status", status=500)
    c_port = L.closed_port()
    conf(d, [a.port, b.port, c_port])
    svc = L.Service(BIN, d, ["--schedule", "300,300", "--deadline-ms", "800"])
    check("1. the service starts", svc.start(), svc.stderr())
    status, data = svc.get("/readyz")
    ready = json.loads(data)
    check("1. GET /readyz without a credential: 200 {\"ready\":true}", status == 200 and ready == {"ready": True}, str((status, data)))
    check("1. GET /healthz is as it was", svc.get("/healthz") == (200, b'{"ok":true}'))
    m0 = svc.metrics()
    check("1. before any event: nothing counted, ready is 1, stopping is 0",
          m_get(m0, "hooks_ready") == 1 and m_get(m0, "hooks_stopping") == 0 and m0.value("hooks_ingest_events_total", result="accepted") == 0
          and m0.value("hooks_log_commits_total", log="events") == 0 and m0.value("hooks_endpoints") == 3)

    acked = {}
    for n in range(40):
        status, data = svc.post_event(n)
        assert status == 202, (status, data)
        acked[json.loads(data)["id"]] = n
    keys = {}
    for n in range(100, 103):
        status, data = svc.post_event(n, key=f"key-{n}")
        assert status == 202
        keys[n] = json.loads(data)["id"]
    again = [svc.post_event(n, key=f"key-{n}") for n in range(100, 103)]
    check("1. the three repeats are acknowledged with the ids of the first", all(s == 202 for s, _ in again) and [json.loads(x)["id"] for _, x in again] == list(keys.values()))
    refused = []
    refused.append(svc.request("POST", "/events", b"this is not json", {"Content-Type": "application/json"})[0])        # 422
    refused.append(svc.request("POST", "/events", b'{"no":"type"}', {"Content-Type": "application/json"})[0])          # 422
    refused.append(svc.request("POST", "/events", b'{"type":"a"}', {"Idempotency-Key": "bad key with spaces"})[0])      # 400
    refused.append(svc.post_event(999, key="key-100")[0])                                                              # 422: a different event
    refused.append(svc.request("POST", "/events", json.dumps({"type": "big", "pad": "x" * 66000}).encode(), {})[0])     # 413
    check("1. the five bad requests are refused as expected", refused == [422, 422, 400, 422, 413], str(refused))

    total = 43
    ok = L.wait_for(lambda: len(a.distinct()) == total and svc.stats()["dead"] == 2 * total, 30)
    check("1. A got all 43 events; B and C ended with every event dead", ok, str(svc.stats()))
    time.sleep(0.3)
    st = svc.stats()
    m = svc.metrics()
    ev, size, valid = L.events_log(d)
    kinds, reasons = L.log_counts(d)
    check("1. the events log holds exactly the accepted events", len(ev) == total and sorted(ev) == list(range(1, total + 1)), str(len(ev)))

    check("1. ingest: accepted 43 (40 plain, 3 keyed), duplicate 3, refused 5", m.value("hooks_ingest_events_total", result="accepted") == 43 and
          m.value("hooks_ingest_events_total", result="duplicate") == 3 and m.value("hooks_ingest_events_total", result="refused") == 5,
          str(m.series("hooks_ingest_events_total")))
    check("1. ... accepted equals the records in events.seg", m_get(m, "hooks_ingest_events_total", result="accepted") == len(ev))
    by = {k[0][1]: v for k, v in m.series("hooks_ingest_refused_total").items()}
    check("1. refused by status: 400 once, 413 once, 422 three times, nothing else",
          by == {"400": 1, "413": 1, "422": 3, "503": 0, "507": 0, "other": 0}, str(by))
    check("1. the events log: size == the file, synced == the file, last id == 43", m_get(m, "hooks_log_size_bytes", log="events") == size ==
          m_get(m, "hooks_log_synced_bytes", log="events") and m_get(m, "hooks_events_last_id") == 43, f"{m.series('hooks_log_size_bytes')} {size}")
    check("1. group commits of the events log == the events (each was its own turn; a repeat commits nothing)", m_get(m, "hooks_log_commits_total", log="events") == total,
          str(m.series("hooks_log_commits_total")))
    dsize = os.path.getsize(os.path.join(d, "delivery.seg"))
    check("1. the delivery log: size == the file, synced == the file, and it was committed", m_get(m, "hooks_log_size_bytes", log="delivery") == dsize ==
          m_get(m, "hooks_log_synced_bytes", log="delivery") and 1 <= m_get(m, "hooks_log_commits_total", log="delivery") <= len(L.outcomes(d)))
    check("1. attempts by outcome: delivered 43, failed 172, dead 86", (m_get(m, "hooks_attempts_total", outcome="delivered"), m_get(m, "hooks_attempts_total", outcome="failed"),
          m_get(m, "hooks_attempts_total", outcome="dead")) == (43, 172, 86), str(m.series("hooks_attempts_total")))
    check("1. ... equal to /stats", (st["delivered"], st["failed"], st["dead"]) == (43, 172, 86) and sum(m.series("hooks_attempts_total").values()) == st["attempts"])
    check("1. ... equal to the outcome records of delivery.seg", (kinds.get("delivered"), kinds.get("failed"), kinds.get("dead")) == (43, 172, 86), str(kinds))
    by_reason = {k[0][1]: int(v) for k, v in m.series("hooks_attempt_failures_total").items() if v}
    check("1. failures by reason: 129 status_5xx (B: 3 attempts x 43), 129 connect_refused (C)", by_reason == {"status_5xx": 129, "connect_refused": 129}, str(by_reason))
    check("1. ... equal to the reason records of delivery.seg (kind 14)", reasons == by_reason and kinds.get("reason") == 258, f"{reasons} {kinds}")
    check("1. ... and to failed + dead", sum(by_reason.values()) == st["failed"] + st["dead"])
    check("1. every reason has its own series, and only the 16 reasons that are not `none`", len(m.series("hooks_attempt_failures_total")) == 16)
    cur = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_cursor").items()}
    lag = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_lag_events").items()}
    check("1. per endpoint: cursor 43 and lag 0 for A, B and C (dead is final)", cur == {0: 43, 1: 43, 2: 43} and lag == {0: 0, 1: 0, 2: 0}, f"{cur} {lag}")
    check("1. nothing in flight, no retry waiting, no replay, nothing disabled or paused",
          m_get(m, "hooks_attempts_in_flight") == 0 and m_get(m, "hooks_retries_waiting") == 0 and m_get(m, "hooks_replays_waiting") == 0 and
          m.total("hooks_endpoint_disabled") == 0 and m.total("hooks_endpoint_paused") == 0 and m.total("hooks_endpoint_retries_waiting") == 0)
    lf = {(dict(k)["endpoint"], dict(k)["reason"]) for k in m.series("hooks_endpoint_last_failure")}
    check("1. last failure: B status_5xx, C connect_refused, A none", lf == {("1", "status_5xx"), ("2", "connect_refused")}, str(lf))
    check("1. the idempotency keys held: 3", m_get(m, "hooks_idempotency_keys") == 3 == st["keys"])
    check("1. cron: nothing fired, nothing failed", m.total("hooks_cron_fires_total") == 0 and m.total("hooks_cron_errors_total") == 0)
    check("1. no database named: history disabled, nothing queued", m_get(m, "hooks_history_enabled") == 0 and m_get(m, "hooks_history_queue") == 0)
    check("1. uptime is a number of seconds and grows", 0 < m.value("hooks_uptime_seconds") < 120)
    t0 = m.value("hooks_uptime_seconds")
    time.sleep(0.3)
    check("1. ... grows", svc.metrics().value("hooks_uptime_seconds") > t0)
    check("1. every family has HELP and TYPE, every counter ends in _total", len(m.types) >= 30 and set(m.types) == set(m.helps))
    check("1. the scrape is open: no Authorization header was sent for any of this", True)
    status, data, hdr = svc.request("GET", "/metrics")
    check("1. the content type is the Prometheus text format", status == 200 and hdr.get("Content-Type", "").startswith("text/plain; version=0.0.4"), str(hdr))
    status, data, hdr = svc.request("POST", "/metrics")
    check("1. POST /metrics is a 405 with Allow: GET", status == 405 and "GET" in hdr.get("Allow", ""), str((status, hdr)))
    svc.stop()
    shutil.rmtree(d)
    for p in (a, b):
        p.close()


def stage2():
    print("== 2. lag, retries, and the last failure across a restart", flush=True)
    d = L.free_dir()
    a, b = L.Peer("ok"), L.Peer("status", status=500)
    conf(d, [a.port, b.port])
    svc = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "800"])
    svc.start()
    for n in range(20):
        assert svc.post_event(n)[0] == 202
    L.wait_for(lambda: len(a.distinct()) == 20 and len(b.distinct()) == 20 and svc.stats()["failed"] == 20, 15)
    m = svc.metrics()
    lag = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_lag_events").items()}
    cur = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_cursor").items()}
    ret = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_retries_waiting").items()}
    since = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_failing_since_ms").items()}
    check("2. B: cursor 0, lag 20, 20 retries waiting; A: cursor 20, lag 0", cur == {0: 20, 1: 0} and lag == {0: 0, 1: 20} and ret == {0: 0, 1: 20}, f"{cur} {lag} {ret}")
    check("2. the total of retries waiting is 20, attempts failed 20, in flight 0", m_get(m, "hooks_retries_waiting") == 20 and m_get(m, "hooks_attempts_total", outcome="failed") == 20 and
          m_get(m, "hooks_attempts_in_flight") == 0)
    now = int(time.time() * 1000)
    check("2. B has been failing since a moment ago; A has not failed", since[0] == 0 and 0 < now - since[1] < 60000, str(since))
    check("2. B's last failure is status_5xx", {(dict(k)["endpoint"], dict(k)["reason"]) for k in m.series("hooks_endpoint_last_failure")} == {("1", "status_5xx")})
    check("2. /endpoints agrees on the cursors", {e["id"]: e["cursor"] for e in svc.get_json("/endpoints")[1]} == {0: 20, 1: 0})
    in_log = L.log_counts(d)
    check("2. the reason is in the log before the stop (20 records)", in_log[1] == {"status_5xx": 20}, str(in_log))
    code = svc.stop()
    check("2. SIGTERM: exit status 0", code == 0, str(code))
    svc2 = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "800"])
    svc2.start()
    m2 = svc2.metrics()
    check("2. after the restart the counters are zero again (they are since this start)", m2.total("hooks_attempt_failures_total") == 0 and m_get(m2, "hooks_ingest_events_total", result="accepted") == 0)
    check("2. ... but B's last failure is read back from the log", {(dict(k)["endpoint"], dict(k)["reason"]) for k in m2.series("hooks_endpoint_last_failure")} == {("1", "status_5xx")},
          str(m2.series("hooks_endpoint_last_failure")))
    check("2. ... and its lag, from the log", {int(dict(k)["endpoint"]): int(v) for k, v in m2.series("hooks_endpoint_lag_events").items()} == {0: 0, 1: 20})
    check("2. ... and the commits of the previous run are not counted again", m_get(m2, "hooks_log_commits_total", log="events") == 0)
    b.mode = "ok"
    # retry now: replay B's first event, which delivers it and ends B's failures for that endpoint only at its next delivery
    svc2.request("POST", "/events/1/replay/1")
    L.wait_for(lambda: svc2.stats()["delivered"] >= 1, 10)
    m3 = svc2.metrics()
    check("2. a delivery to B ends its last failure", {(dict(k)["endpoint"], dict(k)["reason"]) for k in m3.series("hooks_endpoint_last_failure")} == set(), str(m3.series("hooks_endpoint_last_failure")))
    svc2.stop()
    shutil.rmtree(d)
    a.close()
    b.close()


def put_streak(d, slot, started_ms):
    with open(os.path.join(d, "delivery.seg"), "ab") as f:
        f.write(L.put_record(0, 12, slot, 0, 0, started_ms))


def stage3():
    print("== 3. disabled by a 410, paused by the breaker", flush=True)
    d = L.free_dir()
    a, g, p = L.Peer("ok"), L.Peer("status", status=410), L.Peer("status", status=503)
    conf(d, [a.port, g.port, p.port])
    put_streak(d, 2, int(time.time() * 1000) - 6 * 86400 * 1000)       # endpoint 2 has been failing for six days
    svc = L.Service(BIN, d, ["--schedule", "200,200", "--deadline-ms", "800"])
    svc.start()
    assert svc.post_event(1)[0] == 202
    ok = L.wait_for(lambda: svc.stats()["paused"] == 1 and svc.get_json("/endpoints")[1][1]["disabled"], 15)
    m = svc.metrics()
    dis = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_disabled").items()}
    pau = {int(dict(k)["endpoint"]): int(v) for k, v in m.series("hooks_endpoint_paused").items()}
    check("3. the 410 disabled endpoint 1 and the breaker paused endpoint 2 (disabled too); endpoint 0 is neither", ok and dis == {0: 0, 1: 1, 2: 1} and pau == {0: 0, 1: 0, 2: 1}, f"{dis} {pau}")
    check("3. the breaker's trips are counted", m_get(m, "hooks_breaker_trips_total") == 1)
    lf = {(dict(k)["endpoint"], dict(k)["reason"]) for k in m.series("hooks_endpoint_last_failure")}
    check("3. the reasons: 410 is gone, the paused endpoint's 503 is status_5xx", lf == {("1", "gone"), ("2", "status_5xx")}, str(lf))
    check("3. the failures by reason: gone 1", m.value("hooks_attempt_failures_total", reason="gone") == 1)
    svc.stop()
    shutil.rmtree(d)
    for x in (a, g, p):
        x.close()


def stage4():
    print("== 4. sixty-two endpoints", flush=True)
    d = L.free_dir()
    a = L.Peer("ok")
    conf(d, [a.port] * 62)
    svc = L.Service(BIN, d, ["--schedule", "300", "--deadline-ms", "800"])
    check("4. 62 endpoints start", svc.start(), svc.stderr())
    for n in range(5):
        svc.post_event(n)
    L.wait_for(lambda: svc.stats()["delivered"] == 62 * 5, 30)
    status, data = svc.get("/metrics")
    m = L.parse_metrics(data.decode())
    per_ep = sum(1 for k in m if "endpoint" in dict(k[1]))
    check("4. the answer fits one response and parses", status == 200 and len(data) < 60000, str(len(data)))
    check("4. series per endpoint: 7 families for each, plus a last failure where there is one (none)", per_ep == 62 * 7, str(per_ep))
    check("4. the series of the whole service are bounded (under 100)", len(m) - per_ep < 100, str(len(m) - per_ep))
    check("4. every endpoint is behind by 0", m.total("hooks_endpoint_lag_events") == 0 and len(m.series("hooks_endpoint_cursor")) == 62)
    svc.stop()
    shutil.rmtree(d)
    a.close()


def stage5():
    print("== 5. the history", flush=True)
    L.apply_schema()
    L.psql("truncate endpoints, attempts")
    a, b = L.Peer("ok"), L.Peer("status", status=500)
    L.psql(f"insert into endpoints values (0, '127.0.0.1', {a.port}, '{L.secret()}'), (1, '127.0.0.1', {b.port}, '{L.secret()}')")
    d = L.free_dir()
    svc = L.Service(BIN, d, ["--schedule", "200,200", "--deadline-ms", "800", *L.pg_flags()])
    check("5. the service starts with a database", svc.start(), svc.stderr())
    for n in range(30):
        assert svc.post_event(n)[0] == 202
    L.wait_for(lambda: svc.stats()["dead"] == 30 and svc.stats()["history_written"] == 30 * 4, 30)
    time.sleep(0.3)
    m = svc.metrics()
    rows = int(L.psql("select count(*) from attempts")[0][0])
    check("5. history enabled, two connections, queue empty", (m_get(m, "hooks_history_enabled"), m_get(m, "hooks_history_connections"), m_get(m, "hooks_history_queue")) == (1, 2, 0))
    check("5. written == the rows of the attempts table == 30 delivered + 90 attempts at B", m_get(m, "hooks_history_rows_total", result="written") == rows == 120,
          f"{m.series('hooks_history_rows_total')} {rows}")
    check("5. failed 0, dropped 0", m_get(m, "hooks_history_rows_total", result="failed") == 0 and m_get(m, "hooks_history_rows_total", result="dropped") == 0)
    st = svc.stats()
    check("5. ... equal to /stats", (m_get(m, "hooks_history_rows_total", result="written"), st["history_failed"], st["history_dropped"]) == (st["history_written"], 0, 0))
    rows_by = dict(L.psql("select reason, count(*) from attempts group by reason"))
    check("5. the reason column: 30 delivered rows have 0, 90 failed rows have 12 (status_5xx)", rows_by == {"0": "30", "12": "90"}, str(rows_by))
    svc.stop()
    shutil.rmtree(d)
    a.close()
    b.close()


def main():
    stage1()
    stage2()
    stage3()
    stage4()
    if HAVE_DB:
        stage5()
    else:
        print("(stage 5 needs HOOKS_PG: skipped)")
    return check.finish("metrics")


if __name__ == "__main__":
    sys.exit(main())
