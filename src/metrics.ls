edition 5;

module metrics;

import std.buffer;
import ops;
import reason;

// `metrics` -- the service's numbers in the Prometheus text exposition format, version 0.0.4 (`docs/design.md` section 34.2).
//
// This module only formats. The caller (`GET /metrics` in `hooks.ls`, which knows where everything is kept) fills two arrays of integers and
// hands them over: `g`, the numbers of the whole service, one cell each (`g_*` below), and `ep`, a row of `row()` cells for each endpoint
// (`e_*`). The label values are all fixed names or endpoint ids, so nothing needs escaping, and the number of series is bounded: about 70 for
// the service, and 9 for each endpoint of a page, a page being `page_size()` endpoints (`docs/design.md` section 41.4: the server's queue for an answer is 64 KiB, so an
// answer is a page, and the per-endpoint series of 1,024 endpoints are 16 of them).
//
// Counters are since this start (a restart is a counter reset, which Prometheus' `rate` and `increase` expect). A number the service does not
// keep is not made up.

// ---- the cells of `g`

pub fn g_size() -> [] int {
    return 47;
}

// The endpoints one page of `/metrics` holds, and the most pages there can be.
pub fn page_size() -> [] int {
    return 64;
}

// How many pages there are (1 at least: page 0 is the service's own series).
pub fn g_pages() -> [] int {
    return 46;
}

pub fn pages_for(endpoints: int) -> [] int {
    if endpoints <= 0 {
        return 1;
    }
    return (endpoints + page_size() - 1) / page_size();
}

pub fn g_uptime_ms() -> [] int {
    return 0;
}

pub fn g_ready() -> [] int {
    return 1;
}

pub fn g_stopping() -> [] int {
    return 2;
}

pub fn g_accepted() -> [] int {
    return 3;
}

pub fn g_duplicate() -> [] int {
    return 4;
}

pub fn g_refused() -> [] int {
    return 5;
}

// Six cells: the refused requests by status (`ops.refused_status`).
pub fn g_refused_by() -> [] int {
    return 6;
}

pub fn g_commits_events() -> [] int {
    return 12;
}

pub fn g_commits_delivery() -> [] int {
    return 13;
}

pub fn g_size_events() -> [] int {
    return 14;
}

pub fn g_size_delivery() -> [] int {
    return 15;
}

pub fn g_last_event() -> [] int {
    return 16;
}

pub fn g_delivered() -> [] int {
    return 17;
}

pub fn g_failed() -> [] int {
    return 18;
}

pub fn g_dead() -> [] int {
    return 19;
}

pub fn g_in_flight() -> [] int {
    return 20;
}

pub fn g_retries() -> [] int {
    return 21;
}

pub fn g_replays() -> [] int {
    return 22;
}

pub fn g_endpoints() -> [] int {
    return 23;
}

pub fn g_keys() -> [] int {
    return 24;
}

pub fn g_trips() -> [] int {
    return 25;
}

pub fn g_history_enabled() -> [] int {
    return 26;
}

pub fn g_history_live() -> [] int {
    return 27;
}

pub fn g_history_queue() -> [] int {
    return 28;
}

pub fn g_history_written() -> [] int {
    return 29;
}

pub fn g_history_failed() -> [] int {
    return 30;
}

pub fn g_history_dropped() -> [] int {
    return 31;
}

pub fn g_cron_fired() -> [] int {
    return 32;
}

pub fn g_cron_errors() -> [] int {
    return 33;
}

pub fn g_cron_skipped() -> [] int {
    return 34;
}

pub fn g_synced_events() -> [] int {
    return 35;
}

pub fn g_synced_delivery() -> [] int {
    return 36;
}

// The pool's counters (`docs/design.md` section 37): connections being made, connections made again after a loss, attempts to connect that failed,
// connections lost with a request on them; and whether the endpoints have been read from the table.
pub fn g_db_connecting() -> [] int {
    return 37;
}

pub fn g_db_reconnects() -> [] int {
    return 40;
}

pub fn g_db_failures() -> [] int {
    return 39;
}

pub fn g_db_losses() -> [] int {
    return 40;
}

pub fn g_endpoints_loaded() -> [] int {
    return 41;
}

// TLS handshakes since the start, and how many resumed a session.
pub fn g_tls_handshakes() -> [] int {
    return 44;
}

pub fn g_tls_resumed() -> [] int {
    return 45;
}

// ---- the cells of an endpoint's row

pub fn row() -> [] int {
    return 11;
}

pub fn e_id() -> [] int {
    return 0;
}

pub fn e_cursor() -> [] int {
    return 1;
}

pub fn e_lag() -> [] int {
    return 2;
}

pub fn e_disabled() -> [] int {
    return 3;
}

pub fn e_paused() -> [] int {
    return 4;
}

// When the endpoint's current run of failed attempts began, Unix ms, 0 if none.
pub fn e_failing_since() -> [] int {
    return 5;
}

// The reason of its last failed attempt (`reason.ls`), 0 if it has none.
pub fn e_last_reason() -> [] int {
    return 6;
}

// Events that have failed at least once, are not final and wait for their next attempt.
pub fn e_retries() -> [] int {
    return 7;
}

pub fn e_in_flight() -> [] int {
    return 8;
}

// Attempts held back by the endpoint's rate limit since the start (`lim.held_back`).
pub fn e_throttled() -> [] int {
    return 9;
}

// Dead letters the endpoint holds now (`dead.count`).
pub fn e_dead_letters() -> [] int {
    return 10;
}

// ---- the text

fn head[&h, &n, &k, &t](heap: &!h Heap, q: buffer.Buffer, name: &n [byte], kind: &k [byte], help: &t [byte]) -> [heap] buffer.Buffer {
    var b = buffer.append(heap, q, "# HELP ");
    b = buffer.append(heap, b, name);
    b = buffer.append(heap, b, " ");
    b = buffer.append(heap, b, help);
    b = buffer.append(heap, b, "\n# TYPE ");
    b = buffer.append(heap, b, name);
    b = buffer.append(heap, b, " ");
    b = buffer.append(heap, b, kind);
    return buffer.append(heap, b, "\n");
}

// `name value`
fn plain[&h, &n](heap: &!h Heap, q: buffer.Buffer, name: &n [byte], value: int) -> [heap] buffer.Buffer {
    var b = buffer.append(heap, q, name);
    b = buffer.append(heap, b, " ");
    b = buffer.push_nat(heap, b, value);
    return buffer.append(heap, b, "\n");
}

// `name{label="text"} value`
fn labelled[&h, &n, &l, &v](heap: &!h Heap, q: buffer.Buffer, name: &n [byte], label: &l [byte], text: &v [byte], value: int) -> [heap] buffer.Buffer {
    var b = buffer.append(heap, q, name);
    b = buffer.append(heap, b, "{");
    b = buffer.append(heap, b, label);
    b = buffer.append(heap, b, "=\"");
    b = buffer.append(heap, b, text);
    b = buffer.append(heap, b, "\"} ");
    b = buffer.push_nat(heap, b, value);
    return buffer.append(heap, b, "\n");
}

// `name{endpoint="id"} value`
fn per_endpoint[&h, &n](heap: &!h Heap, q: buffer.Buffer, name: &n [byte], id: int, value: int) -> [heap] buffer.Buffer {
    var b = buffer.append(heap, q, name);
    b = buffer.append(heap, b, "{endpoint=\"");
    b = buffer.push_nat(heap, b, id);
    b = buffer.append(heap, b, "\"} ");
    b = buffer.push_nat(heap, b, value);
    return buffer.append(heap, b, "\n");
}

// One gauge per endpoint: the header, then a sample for each row, taking the value from cell `cell` of the row.
fn endpoint_series[&h, &n, &k, &t, &e](heap: &!h Heap, q: buffer.Buffer, ep: &e [int], count: int, name: &n [byte], kind: &k [byte], help: &t [byte], cell: int) -> [heap] buffer.Buffer {
    var b = head(heap, q, name, kind, help);
    var i = 0;
    while i < count {
        b = per_endpoint(heap, b, name, ep[i * row() + e_id()], ep[i * row() + cell]);
        i = i + 1;
    }
    return b;
}

// Seconds with three decimals, from milliseconds.
fn seconds[&h, &n](heap: &!h Heap, q: buffer.Buffer, name: &n [byte], ms: int) -> [heap] buffer.Buffer {
    var b = buffer.append(heap, q, name);
    b = buffer.append(heap, b, " ");
    b = buffer.push_nat(heap, b, ms / 1000);
    b = buffer.append(heap, b, ".");
    let frac = ms % 1000;
    if frac < 100 {
        b = buffer.append(heap, b, "0");
    }
    if frac < 10 {
        b = buffer.append(heap, b, "0");
    }
    b = buffer.push_nat(heap, b, frac);
    return buffer.append(heap, b, "\n");
}

pub fn render[&h, &g, &e, &r](heap: &!h Heap, g: &g [int], ep: &e [int], n: int, reasons: &r [int]) -> [heap] buffer.Buffer {
    return render_page(heap, g, ep, n, reasons, true);
}

// One answer: with `service`, the service's own series and then the per-endpoint series of the `n` rows of `ep`; without it only the latter (a page after the
// first, so that scraping every page counts nothing twice).
pub fn render_page[&h, &g, &e, &r](heap: &!h Heap, g: &g [int], ep: &e [int], n: int, reasons: &r [int], service: bool) -> [heap] buffer.Buffer {
    var b = buffer.empty(heap, 8192);
    if service {
        b = render_service(heap, b, g, reasons);
    }
    return render_endpoints(heap, b, ep, n);
}

fn render_service[&h, &g, &r](heap: &!h Heap, q: buffer.Buffer, g: &g [int], reasons: &r [int]) -> [heap] buffer.Buffer {
    var b = head(heap, q, "hooks_uptime_seconds", "gauge", "Seconds since the process started.");
    b = seconds(heap, b, "hooks_uptime_seconds", g[g_uptime_ms()]);
    b = head(heap, b, "hooks_ready", "gauge", "1 if GET /readyz answers 200, else 0.");
    b = plain(heap, b, "hooks_ready", g[g_ready()]);
    b = head(heap, b, "hooks_stopping", "gauge", "1 while the service drains after SIGTERM or SIGINT.");
    b = plain(heap, b, "hooks_stopping", g[g_stopping()]);

    b = head(heap, b, "hooks_ingest_events_total", "counter", "POST /events by result: accepted (stored, flushed, 202), duplicate (an Idempotency-Key repeat, 202), refused.");
    b = labelled(heap, b, "hooks_ingest_events_total", "result", "accepted", g[g_accepted()]);
    b = labelled(heap, b, "hooks_ingest_events_total", "result", "duplicate", g[g_duplicate()]);
    b = labelled(heap, b, "hooks_ingest_events_total", "result", "refused", g[g_refused()]);
    b = head(heap, b, "hooks_ingest_refused_total", "counter", "Refused POST /events by HTTP status (other: any status not listed).");
    var s = 0;
    while s < 5 {
        b = buffer.append(heap, b, "hooks_ingest_refused_total{status=\"");
        b = buffer.push_nat(heap, b, ops.refused_status(s));
        b = buffer.append(heap, b, "\"} ");
        b = buffer.push_nat(heap, b, g[g_refused_by() + s]);
        b = buffer.append(heap, b, "\n");
        s = s + 1;
    }
    b = labelled(heap, b, "hooks_ingest_refused_total", "status", "other", g[g_refused_by() + 5]);

    b = head(heap, b, "hooks_log_commits_total", "counter", "Group commits: turns of the loop in which a flush made a log's new records durable.");
    b = labelled(heap, b, "hooks_log_commits_total", "log", "events", g[g_commits_events()]);
    b = labelled(heap, b, "hooks_log_commits_total", "log", "delivery", g[g_commits_delivery()]);
    b = head(heap, b, "hooks_log_size_bytes", "gauge", "Bytes in the log file, written or not yet flushed.");
    b = labelled(heap, b, "hooks_log_size_bytes", "log", "events", g[g_size_events()]);
    b = labelled(heap, b, "hooks_log_size_bytes", "log", "delivery", g[g_size_delivery()]);
    b = head(heap, b, "hooks_log_synced_bytes", "gauge", "Bytes of the log file that a flush has made durable.");
    b = labelled(heap, b, "hooks_log_synced_bytes", "log", "events", g[g_synced_events()]);
    b = labelled(heap, b, "hooks_log_synced_bytes", "log", "delivery", g[g_synced_delivery()]);
    b = head(heap, b, "hooks_events_last_id", "gauge", "The id of the newest event in the events log.");
    b = plain(heap, b, "hooks_events_last_id", g[g_last_event()]);
    b = head(heap, b, "hooks_idempotency_keys", "gauge", "Idempotency keys held (limit 65536).");
    b = plain(heap, b, "hooks_idempotency_keys", g[g_keys()]);

    b = head(heap, b, "hooks_attempts_total", "counter", "Delivery attempts that ended, by outcome. A dead attempt is a dead letter.");
    b = labelled(heap, b, "hooks_attempts_total", "outcome", "delivered", g[g_delivered()]);
    b = labelled(heap, b, "hooks_attempts_total", "outcome", "failed", g[g_failed()]);
    b = labelled(heap, b, "hooks_attempts_total", "outcome", "dead", g[g_dead()]);
    b = head(heap, b, "hooks_attempt_failures_total", "counter", "Failed and dead attempts by the reason they failed (docs/design.md section 34.3).");
    var k = 1;
    while k < reason.count() {
        b = labelled(heap, b, "hooks_attempt_failures_total", "reason", reason.name(k), reasons[k]);
        k = k + 1;
    }
    b = head(heap, b, "hooks_tls_handshakes_total", "counter", "TLS handshakes made to https endpoints, by result: full (the certificate chain was verified) or resumed (a session of the endpoint's was resumed).");
    b = labelled(heap, b, "hooks_tls_handshakes_total", "result", "full", g[g_tls_handshakes()] - g[g_tls_resumed()]);
    b = labelled(heap, b, "hooks_tls_handshakes_total", "result", "resumed", g[g_tls_resumed()]);
    b = head(heap, b, "hooks_attempts_in_flight", "gauge", "Attempts on the wire now.");
    b = plain(heap, b, "hooks_attempts_in_flight", g[g_in_flight()]);
    b = head(heap, b, "hooks_retries_waiting", "gauge", "Events that failed at least once and wait for their next attempt, over all endpoints.");
    b = plain(heap, b, "hooks_retries_waiting", g[g_retries()]);
    b = head(heap, b, "hooks_replays_waiting", "gauge", "Replay requests not finished (limit 32).");
    b = plain(heap, b, "hooks_replays_waiting", g[g_replays()]);
    b = head(heap, b, "hooks_breaker_trips_total", "counter", "Times the circuit breaker paused an endpoint.");
    b = plain(heap, b, "hooks_breaker_trips_total", g[g_trips()]);
    b = head(heap, b, "hooks_endpoints", "gauge", "Endpoints the service delivers to.");
    b = plain(heap, b, "hooks_endpoints", g[g_endpoints()]);

    b = head(heap, b, "hooks_history_enabled", "gauge", "1 if a database was named for the history of attempts.");
    b = plain(heap, b, "hooks_history_enabled", g[g_history_enabled()]);
    b = head(heap, b, "hooks_history_connections", "gauge", "Live database connections (0 to 2).");
    b = plain(heap, b, "hooks_history_connections", g[g_history_live()]);
    b = head(heap, b, "hooks_history_queue", "gauge", "Rows of the history waiting for the database.");
    b = plain(heap, b, "hooks_history_queue", g[g_history_queue()]);
    b = head(heap, b, "hooks_history_rows_total", "counter", "Rows of the history by result: written, failed (the database refused them, or the connection was lost with them on it), dropped (the queue was full).");
    b = labelled(heap, b, "hooks_history_rows_total", "result", "written", g[g_history_written()]);
    b = labelled(heap, b, "hooks_history_rows_total", "result", "failed", g[g_history_failed()]);
    b = labelled(heap, b, "hooks_history_rows_total", "result", "dropped", g[g_history_dropped()]);
    b = head(heap, b, "hooks_database_connecting", "gauge", "Database connections being made now (dialed or logging in), of 2.");
    b = plain(heap, b, "hooks_database_connecting", g[g_db_connecting()]);
    b = head(heap, b, "hooks_database_reconnects_total", "counter", "Database connections made again after one that was live was lost.");
    b = plain(heap, b, "hooks_database_reconnects_total", g[g_db_reconnects()]);
    b = head(heap, b, "hooks_database_connect_failures_total", "counter", "Attempts to make a database connection that failed (refused, no answer, a login or a statement refused).");
    b = plain(heap, b, "hooks_database_connect_failures_total", g[g_db_failures()]);
    b = head(heap, b, "hooks_database_connection_losses_total", "counter", "Live database connections lost: closed by the server, broken, or given up after pg-request-ms.");
    b = plain(heap, b, "hooks_database_connection_losses_total", g[g_db_losses()]);
    b = head(heap, b, "hooks_endpoints_loaded", "gauge", "1 once the endpoints have been read (from the table, if a database was named; always 1 otherwise).");
    b = plain(heap, b, "hooks_endpoints_loaded", g[g_endpoints_loaded()]);

    b = head(heap, b, "hooks_cron_fires_total", "counter", "Scheduled events made.");
    b = plain(heap, b, "hooks_cron_fires_total", g[g_cron_fired()]);
    b = head(heap, b, "hooks_cron_errors_total", "counter", "Schedule rows or updates that could not be handled.");
    b = plain(heap, b, "hooks_cron_errors_total", g[g_cron_errors()]);
    b = head(heap, b, "hooks_cron_skipped_total", "counter", "Fires skipped (catch-up off, or a window that passed).");
    b = plain(heap, b, "hooks_cron_skipped_total", g[g_cron_skipped()]);
    b = head(heap, b, "hooks_metrics_pages", "gauge", "Pages of /metrics: page 0 has the service's series and the first 64 endpoints, page k the 64 after the 64 k endpoints before (GET /metrics?page=k).");
    b = plain(heap, b, "hooks_metrics_pages", g[g_pages()]);
    return b;
}

fn render_endpoints[&h, &e](heap: &!h Heap, q: buffer.Buffer, ep: &e [int], n: int) -> [heap] buffer.Buffer {
    var b = q;
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_cursor", "gauge", "The largest event id such that every event up to it is final (delivered or dead) for the endpoint.", e_cursor());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_lag_events", "gauge", "Events behind: the newest event id minus the endpoint's cursor.", e_lag());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_disabled", "gauge", "1 if the endpoint gets no attempts (a 410, a person, or the circuit breaker).", e_disabled());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_paused", "gauge", "1 if the circuit breaker is what disabled the endpoint.", e_paused());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_retries_waiting", "gauge", "Events that failed at least once and wait for the next attempt.", e_retries());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_in_flight", "gauge", "Attempts on the wire to the endpoint.", e_in_flight());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_throttled_total", "counter", "Attempts held back by the endpoint's rate limit (a wait, not a failure), since the start.", e_throttled());
    b = endpoint_series(heap, b, ep, n, "hooks_endpoint_dead_letters", "gauge", "Dead letters the endpoint holds, newest 2048 at most (GET /endpoints/:id/dead).", e_dead_letters());
    b = head(heap, b, "hooks_endpoint_failing_since_ms", "gauge", "Unix ms at which the endpoint's current run of failed attempts began, 0 if it has none.");
    var i = 0;
    while i < n {
        b = per_endpoint(heap, b, "hooks_endpoint_failing_since_ms", ep[i * row() + e_id()], ep[i * row() + e_failing_since()]);
        i = i + 1;
    }
    b = head(heap, b, "hooks_endpoint_last_failure", "gauge", "1 for the reason of the endpoint's last failed attempt, while its last attempt did not deliver. (Kept across restarts.)");
    i = 0;
    while i < n {
        let why = ep[i * row() + e_last_reason()];
        if why > 0 {
            b = buffer.append(heap, b, "hooks_endpoint_last_failure{endpoint=\"");
            b = buffer.push_nat(heap, b, ep[i * row() + e_id()]);
            b = buffer.append(heap, b, "\",reason=\"");
            b = buffer.append(heap, b, reason.name(why));
            b = buffer.append(heap, b, "\"} 1\n");
        }
        i = i + 1;
    }
    return b;
}
