edition 6;

// `hooks` -- a webhook delivery service (`docs/design.md`).
//
//     hooks --port <port> --dir <data-dir> [--schedule <ms,ms,...>] [--deadline-ms <ms>] [--window-ms <ms>] [--config <file>]
//
// This is step H1a: **ingest**. `POST /events` takes a JSON object with a string `"type"`, appends it to a durable log
// (`lexsys-log`'s `log.ls`), and answers `202` with its id **only after the flush that covers it**. Requests that arrive in
// the same turn of the loop share one flush (group commit), which is the whole reason the server holds a request and
// answers it later. `GET /events/:id` reads one back, and `GET /healthz` says the service is up.
//
// **Step H1b: delivery to one fixed receiver** (once `hooks <port> <data-dir> <receiver-host> <receiver-port>`; section 13). After each turn the
// service sends the events the log has flushed, in order, to `POST http://<receiver>/hook`, and counts one delivered on any
// `2xx`. What is delivered is remembered in a second log, `delivered.seg`, one record per delivered event whose id *is* the
// event's id: because delivery is strictly in order, the last record is the cursor, and recovery of that log yields it.
// The record is written *after* the receiver answered, so a crash between the two redelivers: at least once, never zero
// (`docs/design.md` section 4). A failed attempt stops the turn's deliveries and is retried after a short fixed pause, a
// placeholder for the backoff schedule of H1c.
//
// What it does not do yet, and `docs/design.md` section 10 is the order: several endpoints, signing, the retry schedule.
//
// **The id.** An event's id is its position in the log, counted from 1: the record's `ms` field, with `seq` 0. It is
// strictly increasing, survives a restart (recovery finds the last one), and says nothing about the time.

import std.buffer;
import std.bytes;
import std.http;
import std.io;
import std.json;
import std.route;
import http.server;
import log;
import evlog;
import history;
import view;
import queries;
import pg.pool;
import manage;
import pg;
import roster;
import record;
import attempt;
import thp;
import dbname;
import audit;
import bodies;
import crc;
import idem;
import std.conns;
import endpoints;
import config;
import sign;
import state;
import sched;
import ops;
import logguard;
import reason;
import metrics;
import authz;
import perm;
import epx;
import filter;
import wire;
import hdrs;
import dbup;
import jitter;
import lim;
import dead;
import bulk;
import tls;
import destination;
import resolve;

fn max_len() -> [] int {
    return 65536;
}

// The most requests one turn can answer together: a ticket per request held, at most one per connection.
fn most_held() -> [] int {
    return 1024;
}

fn number_of[&t](text: &t [byte]) -> [] int {
    if len(text) == 0 || len(text) > 17 {
        return 0 - 1;
    }
    var n = 0;
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if c < 48 || c > 57 {
            return 0 - 1;
        }
        n = n * 10 + (c - 48);
        i = i + 1;
    }
    return n;
}

// `<dir>/<name>` into `out`; answers its length.
fn path_of[&d, &n, &o](out: &!o [byte], dir: &d [byte], name: &n [byte]) -> [] int {
    var i = 0;
    while i < len(dir) {
        out[i] = dir[i];
        i = i + 1;
    }
    out[i] = byte_of('/');
    var j = 0;
    while j < len(name) {
        out[i + 1 + j] = name[j];
        j = j + 1;
    }
    return i + 1 + len(name);
}

// A log, or why there is not one.
enum Opening {
    Ok(log.Log),
    Failed(int),
}

// Open `<dir>/<name>` for appending and reading, recovering it first: the three-handle sequence `lexsys-log` describes, with `logguard.recover_known` in the place of
// `log.recover`: `logguard.preflight` has already looked at both logs and judged them, and this applies the cut that was judged (`docs/design.md` section 34.5; `report` says what
// it found). `gate` is what `preflight` answered.
fn open_log[&c, &d, &n, &w, &q](fs: &c Fs(""), dir: &d [byte], name: &n [byte], window: &!w [byte], repair: bool, report: &!q [int], gate: int) -> [fs_read(""), fs_write(""), file_read, file_write] Opening {
    if gate != 0 {
        // `logguard.preflight` refused the start (18 or 19) before any log was changed.
        return Opening::Failed(3000 + gate);
    }
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        let path = path_buf[0..path_of(path_buf, dir, name)];
        match open_append(fs, path) {
            Opened::Failed(e) => {
                return Opening::Failed(e);
            }
            Opened::Ok(w) => {
                match open_rw(fs, path) {
                    Opened::Failed(e) => {
                        file_close(w);
                        return Opening::Failed(e);
                    }
                    Opened::Ok(rw0) => {
                        var rw = rw0;
                        var rec = (0, 0, 0, 0 - 1, 0 - 1);
                        borrow mut rw as &!x in {
                            rec = logguard.recover_known(x, report, repair, fs, dir, name, window);
                        }
                        file_close(rw);
                        if rec.0 != 0 {
                            file_close(w);
                            return Opening::Failed(rec.0);
                        }
                        match open_read(fs, path) {
                            Opened::Failed(e) => {
                                file_close(w);
                                return Opening::Failed(e);
                            }
                            Opened::Ok(rd) => {
                                return Opening::Ok(log.attach(w, rd, rec.1, rec.2, rec.3, rec.4, max_len()));
                            }
                        }
                    }
                }
            }
        }
    }
}

// The same for a log that was just made by this process (the snapshot of the outcomes log, `compact.ls`), by its path: nothing was judged before it, so `log.recover` cuts
// what there is to cut. (No region here: the caller's path is used, and a function that leaves a region by `return` loses its memory, `docs/lexsys-log-retention.md` gap 6.)
fn open_tmp_log[&c, &p, &w](fs: &c Fs(""), path: &p [byte], window: &!w [byte]) -> [fs_read(""), fs_write(""), file_read, file_write] Opening {
    match open_append(fs, path) {
        Opened::Failed(e) => {
            return Opening::Failed(e);
        }
        Opened::Ok(w) => {
            match open_rw(fs, path) {
                Opened::Failed(e) => {
                    file_close(w);
                    return Opening::Failed(e);
                }
                Opened::Ok(rw0) => {
                    var rw = rw0;
                    var rec = (0, 0, 0, 0 - 1, 0 - 1);
                    borrow mut rw as &!x in {
                        rec = log.recover(x, window, max_len());
                    }
                    file_close(rw);
                    if rec.0 != 0 {
                        file_close(w);
                        return Opening::Failed(rec.0);
                    }
                    match open_read(fs, path) {
                        Opened::Failed(e) => {
                            file_close(w);
                            return Opening::Failed(e);
                        }
                        Opened::Ok(rd) => {
                            return Opening::Ok(log.attach(w, rd, rec.1, rec.2, rec.3, rec.4, max_len()));
                        }
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------
// The handlers
// ---------------------------------------------------------------------

// `{"id":N}`
fn id_body[&h](heap: &!h Heap, id: int) -> [heap] buffer.Buffer {
    var w = json.writer(heap, 32);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "id");
    w = json.put_int(heap, w, id);
    w = json.end_object(heap, w);
    return json.finish(w);
}

// Is `body` a JSON object with a string `"type"`? Answers the reason as a static message (nonzero length) or none, and, for an event that is good, how many
// integers of `typ` (128 at least, a byte to an integer) are its type as the record keeps it (`filter.storable`: 0 for a type that is empty, longer than 128 bytes or has a control
// character in it, which the record then keeps as no type at all).
fn invalid_event[&h, &b, &y](heap: &!h Heap, body: &b [byte], typ: &!y [int]) -> [heap] (&static [byte], int) {
    var reason = "";
    var tlen = 0;
    let tape = box_slice(heap, json.tape_len(body), 0);
    borrow mut tape as &!tw in {
        let t = contents(tw);
        let nodes = json.parse(body, t);
        if nodes < 0 {
            reason = json.error_message(json.error_code(nodes));
        } else if !json.is_object(t, 0) {
            reason = "the event must be a JSON object";
        } else {
            let kind = json.get(body, t, 0, "type");
            if kind < 0 || !json.is_string(t, kind) {
                reason = "the event needs a string \"type\"";
            } else {
                region a {
                    let decoded = alloc_slice[a](filter.max_type() + 8, byte_of(0));
                    let n = json.string_into(body, t, kind, decoded);
                    if n > 0 {
                        tlen = filter.storable(decoded[0..n]);
                    }
                    var k = 0;
                    while k < tlen {
                        typ[k] = int_of(decoded[k]);
                        k = k + 1;
                    }
                }
            }
        }
    }
    unbox_slice(heap, tape);
    return (reason, tlen);
}

// The index of the `Idempotency-Key` header, -1 if there is none, -2 if there is more than one (which one the client meant
// is not ours to guess).
fn key_header[&q, &t](request: &q [byte], table: &t [int]) -> [] int {
    let first = http.find_header(request, table, "idempotency-key");
    if first < 0 {
        return first;
    }
    var i = first + 1;
    while i < http.header_count(table) {
        if bytes_equal_lower(http.header_name(request, table, i), "idempotency-key") {
            return 0 - 2;
        }
        i = i + 1;
    }
    return first;
}

// Is `name` (any case) `want` (lowercase)?
fn bytes_equal_lower[&a, &b](name: &a [byte], want: &b [byte]) -> [] bool {
    if len(name) != len(want) {
        return false;
    }
    var i = 0;
    while i < len(name) {
        var c = int_of(name[i]);
        if c >= 'A' && c <= 'Z' {
            c = c + 32;
        }
        if c != int_of(want[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Build the record of event `ms` in `scratch`: the body, then its type if it has one (`typ`, the pair `typ`: design section 35), and for a keyed event
// the key and the time `now` (Unix ms, eight bytes). Answers the record's length.
fn event_record[&c, &b, &y, &k](scratch: &!c [byte], ms: int, body: &b [byte], typ: &y [int], tlen: int, key: &k [byte], keyed: bool, now: int) -> [] int {
    var fields = 1;
    if tlen > 0 {
        fields = fields + 1;
    }
    if keyed {
        fields = fields + 2;
    }
    let p = record.begin(scratch, 0, ms, 0, fields);
    var end = record.put_pair(scratch, p, "event", body);
    if tlen > 0 {
        region ty {
            let tb = alloc_slice[ty](filter.max_type() + 8, byte_of(0));
            var k = 0;
            while k < tlen {
                tb[k] = byte_of(typ[k]);
                k = k + 1;
            }
            end = record.put_pair(scratch, end, "typ", tb[0..tlen]);
        }
    }
    if !keyed {
        return record.seal(scratch, 0, end);
    }
    end = record.put_pair(scratch, end, "key", key);
    region a {
        let stamp = alloc_slice[a](8, byte_of(0));
        record.put_u64(stamp, 0, now);
        end = record.put_pair(scratch, end, "t", stamp);
    }
    return record.seal(scratch, 0, end);
}

// `event_record` with the pair `x` that names the key a sealed body was sealed with (`bodies.ls`, section 47.4), last.
fn event_record_x[&c, &b, &y, &k, &f](scratch: &!c [byte], ms: int, body: &b [byte], typ: &y [int], tlen: int, key: &k [byte], keyed: bool, now: int, fp: &f [byte]) -> [] int {
    var fields = 2;
    if tlen > 0 {
        fields = fields + 1;
    }
    if keyed {
        fields = fields + 2;
    }
    let p = record.begin(scratch, 0, ms, 0, fields);
    var end = record.put_pair(scratch, p, "event", body);
    if tlen > 0 {
        region ty {
            let tb = alloc_slice[ty](filter.max_type() + 8, byte_of(0));
            var k = 0;
            while k < tlen {
                tb[k] = byte_of(typ[k]);
                k = k + 1;
            }
            end = record.put_pair(scratch, end, "typ", tb[0..tlen]);
        }
    }
    if !keyed {
        end = record.put_pair(scratch, end, "x", fp);
        return record.seal(scratch, 0, end);
    }
    end = record.put_pair(scratch, end, "key", key);
    region a {
        let stamp = alloc_slice[a](8, byte_of(0));
        record.put_u64(stamp, 0, now);
        end = record.put_pair(scratch, end, "t", stamp);
    }
    end = record.put_pair(scratch, end, "x", fp);
    return record.seal(scratch, 0, end);
}

// Append the event `body` (already judged) to the log, under `key` if it is `keyed`, and note the key in the index. This is the whole of storing an event: `POST /events`
// and a schedule's fire (`fire_cron`) both end here, so a fire is an ordinary event. `sum` is the CRC-32C of `body`, `entry` the index entry of a key that is
// held or -1 (an entry that had expired has been removed by the caller, and room made). `typ[0..tlen]` is the type the record keeps (`tlen` 0 for none). Answers `(code, id)`: 0 and the event's id, or what `evlog.append` refused with.
fn store_event[&c, &b, &t, &k, &l, &x, &y, &z](scratch: &!c [byte], body: &b [byte], typ: &t [int], tlen: int, key: &k [byte], keyed: bool, sum: int, entry: int, lg: &!l evlog.Ev, ix: &!x [int], arena: &!y [byte], now: int, bd: &!z [int]) -> [] (int, int) {
    var ms = 1;
    if evlog.last_id(lg) >= 1 {
        ms = evlog.last_id(lg) + 1;
    }
    var total = 0;
    if bodies.on(bd) {
        // At rest the body is sealed (`docs/design.md` section 47.4): nonce, ciphertext and tag, and a pair `x` naming the key. The room is a region's, left by
        // falling out of it.
        region sb {
            let sealed = alloc_slice[sb](len(body) + bodies.overhead(), byte_of(0));
            let n = bodies.seal(bd, ms, body, sealed);
            if n > 0 {
                let fp = alloc_slice[sb](4, byte_of(0));
                bodies.print_bytes(bd, fp);
                total = event_record_x(scratch, ms, sealed[0..n], typ, tlen, key, keyed, now, fp);
            }
        }
        if total == 0 {
            return (evlog.is_broken(), 0);
        }
    } else {
        total = event_record(scratch, ms, body, typ, tlen, key, keyed, now);
    }
    let code = evlog.append(lg, scratch[0..total], ms);
    if code != 0 {
        return (code, 0);
    }
    if keyed {
        // Only now that the record is appended: the index never holds a key the log does not. The caller has made room (an entry that had expired was removed
        // before: `entry` is -1), so the add cannot fail.
        let e = idem.add(ix, arena, key);
        idem.set(ix, e, ms, now, sum, len(body));
    }
    return (0, ms);
}

// The counters of `ops.ls`, as a slice of the delivery state.
// The scheme of an endpoint as the API prints it: `https` or `http` (`endpoints.scheme_of`).
fn scheme_name(scheme: int) -> [] &static [byte] {
    if scheme == 1 {
        return "https";
    }
    return "http";
}

fn ops_of[&d](dv: &d [int]) -> [] &d [int] {
    return dv[off_ops()..off_ops() + ops.size()];
}

fn ops_of_mut[&d](dv: &!d [int]) -> [] &!d [int] {
    return dv[off_ops()..off_ops() + ops.size()];
}

// A refused `POST /events` (`docs/design.md` section 34.2): the answer, and the refusal counted by its status.
fn refuse_event[&h, &m, &s](heap: &!h Heap, out: buffer.Buffer, stats: &!s [int], status: int, message: &m [byte], keep: bool) -> [heap] buffer.Buffer {
    ops.refused(ops_of_mut(stats), status);
    return server.failure(heap, out, status, message, keep);
}

// Why the service is not ready (`ops.not_ready`), 0 if it is: the logs are not broken, the data directory took the last probe, a database that was named
// has a live connection and its endpoints have been read from it (section 37), and the service has not been asked to stop.
fn readiness[&d, &l, &g](dv: &d [int], lg: &l evlog.Ev, done: &g log.Log) -> [] int {
    let why = ops.not_ready(ops_of(dv), evlog.broken(lg), log.broken(done), history.enabled(dv[off_hq()..off_hq() + history.size()]), history.serving(dv[off_hq()..off_hq() + history.size()]));
    if why == 0 && audit.broken(dv[off_aud()..off_aud() + audit.size()]) {
        return 6;
    }
    return why;
}

// `GET /readyz`: 200 `{"ready":true}`, or 503 `{"ready":false,"check":...,"reason":...}`. It reads three flags and the last probe; it waits for nothing.
fn readyz_reply[&h, &d, &l, &g](heap: &!h Heap, dv: &d [int], lg: &l evlog.Ev, done: &g log.Log, keep: bool, out: buffer.Buffer) -> [heap] buffer.Buffer {
    let why = readiness(dv, lg, done);
    var w = json.writer(heap, 256);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "ready");
    w = json.put_bool(heap, w, why == 0);
    if why != 0 {
        w = json.put_key(heap, w, "check");
        w = json.put_string(heap, w, ops.check_name(why));
        w = json.put_key(heap, w, "reason");
        w = json.put_string(heap, w, ops.why_not(why));
    }
    w = json.end_object(heap, w);
    let body = json.finish(w);
    var answer = out;
    borrow body as &sb in {
        if why == 0 {
            answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
        } else {
            answer = server.reply(heap, answer, 503, buffer.bytes(sb), keep);
        }
    }
    buffer.drop(heap, body);
    return answer;
}

// The numbers `/metrics` shows, into the arrays `metrics.render` formats: the whole service in `g`, a row for each endpoint in `ep`, a count for each
// reason in `rs`. Reads only; the one loop that is not constant (the events of each endpoint's window that wait for a retry) is 1,024 cells an endpoint.
fn gather[&a, &b, &c, &d, &l, &m, &x, &j](g: &!a [int], ep: &!b [int], rs: &!c [int], dv: &d [int], lg: &l evlog.Ev, done: &m log.Log, ix: &x [int], sg: &j [int], now: int, ready: bool, first: int, rows: int, totals: bool) -> [] int {
    let o = ops_of(dv);
    let hq = dv[off_hq()..off_hq() + history.size()];
    let last = evlog.last_id(lg);
    g[metrics.g_uptime_ms()] = now - ops.started(o);
    if ready {
        g[metrics.g_ready()] = 1;
    }
    if ops.stopping(o) {
        g[metrics.g_stopping()] = 1;
    }
    g[metrics.g_accepted()] = ops.accepted_count(o);
    g[metrics.g_duplicate()] = ops.duplicate_count(o);
    g[metrics.g_refused()] = ops.refused_count(o);
    var s = 0;
    while s < ops.statuses() {
        g[metrics.g_refused_by() + s] = ops.refused_by(o, s);
        s = s + 1;
    }
    g[metrics.g_commits_events()] = ops.commits(o, 0);
    g[metrics.g_commits_delivery()] = ops.commits(o, 1);
    g[metrics.g_tls_handshakes()] = ops.tls_handshakes(o);
    g[metrics.g_tls_resumed()] = ops.tls_resumed(o);
    g[metrics.g_size_events()] = evlog.disk_bytes(lg);
    g[metrics.g_size_delivery()] = log.size(done);
    g[metrics.g_synced_events()] = evlog.disk_synced(lg);
    g[metrics.g_synced_delivery()] = log.synced(done);
    if last > 0 {
        g[metrics.g_last_event()] = last;
    }
    g[metrics.g_delivered()] = dv[c_delivered()];
    g[metrics.g_failed()] = dv[c_failed()];
    g[metrics.g_dead()] = dv[c_dead()];
    g[metrics.g_replays()] = rp_cap() - rp_free(dv);
    g[metrics.g_endpoints()] = dv[c_endpoints()];
    g[metrics.g_keys()] = idem.count(ix);
    g[metrics.g_trips()] = dv[c_trips()];
    if history.enabled(hq) {
        g[metrics.g_history_enabled()] = 1;
    }
    g[metrics.g_history_live()] = history.live(hq);
    g[metrics.g_history_queue()] = history.pending(hq);
    g[metrics.g_history_written()] = history.written(hq);
    g[metrics.g_history_failed()] = history.failed(hq);
    g[metrics.g_history_dropped()] = history.dropped(hq);
    g[metrics.g_db_connecting()] = history.connecting(hq);
    g[metrics.g_db_reconnects()] = history.reconnects(hq);
    g[metrics.g_db_failures()] = history.failures(hq);
    g[metrics.g_db_losses()] = history.losses(hq);
    if history.endpoints_known(hq) {
        g[metrics.g_endpoints_loaded()] = 1;
    }
    g[metrics.g_cron_fired()] = sched.fired(sg);
    g[metrics.g_cron_errors()] = sched.errors(sg);
    g[metrics.g_cron_skipped()] = sched.skipped(sg);
    var r = 1;
    while r < reason.count() {
        rs[r] = ops.failures_for(o, r);
        r = r + 1;
    }
    g[metrics.g_pages()] = metrics.pages_for(dv[c_endpoints()]);
    var flying = 0;
    var retries = 0;
    // The endpoints `first` to `first + rows - 1` get a row; the totals over all of them are only the first page's (the others read nothing but their own).
    var i = first;
    var stop = first + rows;
    if totals {
        i = 0;
        stop = dv[c_endpoints()];
    }
    while i < stop && i < dv[c_endpoints()] {
        let e = endpoints.slot_of(dv[off_table()..off_table() + endpoints.table_size()], i);
        let at = (i - first) * metrics.row();
        let cursor = dv[off_cur() + e];
        if i >= first && i < first + rows {
            ep[at + metrics.e_id()] = endpoints.ident_of(dv[off_table()..off_table() + endpoints.table_size()], i);
            ep[at + metrics.e_cursor()] = cursor;
            if last > cursor {
                ep[at + metrics.e_lag()] = last - cursor;
            }
            if is_disabled(dv, e) {
                ep[at + metrics.e_disabled()] = 1;
            }
            if is_paused(dv, e) {
                ep[at + metrics.e_paused()] = 1;
            }
            ep[at + metrics.e_failing_since()] = dv[off_streak() + e];
            ep[at + metrics.e_last_reason()] = ops.last_reason(o, e);
            ep[at + metrics.e_in_flight()] = dv[off_flying() + e];
            ep[at + metrics.e_throttled()] = lim.held_back(lim_of(dv), e);
            ep[at + metrics.e_dead_letters()] = dead.count(dead_of(dv), e);
        }
        flying = flying + dv[off_flying() + e];
        var waiting = 0;
        var id = cursor + 1;
        // (an event that is not in the log has no cell: an endpoint that is caught up reads none, and its window is not made resident by a scrape)
        var to = cursor + state.span();
        if last < to {
            to = last;
        }
        while id <= to {
            if state.attempts(dv[off_cells()..off_flight()], e, id) > 0 && !state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) {
                waiting = waiting + 1;
            }
            id = id + 1;
        }
        if i >= first && i < first + rows {
            ep[at + metrics.e_retries()] = waiting;
        }
        retries = retries + waiting;
        i = i + 1;
    }
    g[metrics.g_in_flight()] = flying;
    g[metrics.g_retries()] = retries;
    return 0;
}

// `GET /metrics`: the Prometheus text format. The route's scope is in `authz.scope_of` (read); it was open until the scoped tokens of production item 0.3 name a read scope.
fn metrics_reply[&h, &d, &l, &g, &x, &j, &q, &t](heap: &!h Heap, dv: &d [int], lg: &l evlog.Ev, done: &g log.Log, ix: &x [int], sg: &j [int], now: int, keep: bool, out: buffer.Buffer, request: &q [byte], table: &t [int]) -> [heap] buffer.Buffer {
    let n = dv[c_endpoints()];
    // The page (`docs/design.md` section 41.4): 64 endpoints, and the service's own series on the first only.
    let want = bulk.parse_metrics_page(http.query(request, table));
    if want.0 != 0 {
        return server.failure(heap, out, 400, bulk.why(want.0), keep);
    }
    if want.1 >= metrics.pages_for(n) {
        return server.failure(heap, out, 404, "no such page of /metrics", keep);
    }
    let first = want.1 * metrics.page_size();
    var rows = n - first;
    if rows > metrics.page_size() {
        rows = metrics.page_size();
    }
    let gv = box_slice(heap, metrics.g_size(), 0);
    let ev = box_slice(heap, (rows + 1) * metrics.row(), 0);
    let rv = box_slice(heap, reason.count(), 0);
    borrow mut gv as &!gw in {
        borrow mut ev as &!ew in {
            borrow mut rv as &!rw in {
                gather(contents(gw), contents(ew), contents(rw), dv, lg, done, ix, sg, now, readiness(dv, lg, done) == 0, first, rows, want.1 == 0);
            }
        }
    }
    var text = buffer.empty(heap, 1);
    borrow gv as &gr in {
        borrow ev as &er in {
            borrow rv as &rr in {
                buffer.drop(heap, text);
                text = metrics.render_page(heap, contents(gr), contents(er), rows, contents(rr), want.1 == 0);
            }
        }
    }
    unbox_slice(heap, gv);
    unbox_slice(heap, ev);
    unbox_slice(heap, rv);
    var answer = out;
    borrow text as &tb in {
        answer = server.reply_as(heap, answer, 200, "text/plain; version=0.0.4; charset=utf-8", buffer.bytes(tb), keep, "");
    }
    buffer.drop(heap, text);
    return answer;
}

// One request, answered or noted for later. `note[0]` is set to the event's id if the request was an accepted
// `POST /events` (answer it after the flush, with `202`: a new event, or the one an earlier request with the same
// `Idempotency-Key` made), and to -1 otherwise (the answer in `out` goes out now). `now` is the Unix time in ms.
fn handle[&h, &r, &q, &t, &p, &b, &l, &w, &c, &n, &s, &x, &y, &z, &u](heap: &!h Heap, router: &r route.Router, request: &q [byte], table: &t [int], params: &!p [int], body: &b [byte], lg: &!l evlog.Ev, done: &!z log.Log, window: &!w [byte], scratch: &!c [byte], note: &!n [int], stats: &!s [int], ix: &!x [int], arena: &!y [byte], sg: &!u [int], now: int, out: buffer.Buffer) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] buffer.Buffer {
    note[0] = 0 - 1;
    let keep = http.keeps_alive(table);
    let path = http.path(request, table);
    let id = route.find(router, http.method(request, table), path, params);
    // the route, for the audit log (`audit.ls`), which the loop writes once the answer is known
    note[2] = id;
    // Who may call this route (`src/authz.ls`: the scope of every route is there, and a route with none is admin-only).
    let verdict = authz.judge(id, request, table, stats[off_token()..off_token() + authz.tokens_size()]);
    if verdict != authz.allowed() {
        return authz.refuse(heap, out, verdict, id, keep);
    }
    if ops.stopping(ops_of(stats)) && !bytes.equal(http.method(request, table), "GET") {
        // Asked to stop (`docs/design.md` section 34.4): nothing new is taken, and the connection is closed after the answer.
        return server.failure(heap, out, 503, ops.stopping_message(), false);
    }
    if (id >= 6 && id <= 9 || id >= 11 && id <= 14 || id >= 20 && id <= 23) && !history.endpoints_known(stats[off_hq()..off_hq() + history.size()]) {
        // The endpoints are read from the table once the database is there (section 37.2); until then the service does not know who they are, and neither
        // lists, changes, replays nor enables them (an event is still taken: it waits in the log for them).
        return server.failure(heap, out, 503, "the endpoints are not loaded yet: the database has not answered since the service started", keep);
    }
    if id == 40 {
        // GET /readyz (section 34.1): open, like /healthz (`authz.scope_of`).
        return readyz_reply(heap, stats, lg, done, keep, out);
    }
    if id == 41 {
        // GET /metrics (section 34.2). Scope: read (`authz.scope_of`).
        return metrics_reply(heap, stats, lg, done, ix, sg, now, keep, out, request, table);
    }
    if id >= 15 && id <= 19 {
        // /schedules (`docs/design.md` section 32): judged here, sent to the database by the loop (`note[0]` is -4), the answer held.
        return sched.judge(heap, id, request, table, path, params, body, scratch, sg, note, stats[off_token()..off_token() + manage.token_size()], history.enabled(stats[off_hq()..off_hq() + history.size()]), now / 1000, keep, out);
    }
    if id >= 20 && id <= 23 {
        // Dead letters and cancelling replays (section 39.1).
        if id == 20 {
            return dead_list(heap, request, table, path, params, lg, window, done, stats, keep, out);
        }
        if id == 21 {
            return dead_replay(heap, path, params, body, lg, window, done, stats, keep, out);
        }
        return cancel_route(heap, id, path, params, done, stats, keep, out);
    }
    if id == 1 {
        return server.reply(heap, out, 200, "{\"ok\":true}", keep);
    }
    if id == 2 {
        // POST /events
        let (invalid, tlen) = invalid_event(heap, body, stats[off_ex() + ex_type()..off_ex() + ex_type() + filter.max_type()]);
        if len(invalid) > 0 {
            return refuse_event(heap, out, stats, 422, invalid, keep);
        }
        let kh = key_header(request, table);
        if kh == 0 - 2 {
            return refuse_event(heap, out, stats, 400, "more than one Idempotency-Key header", keep);
        }
        let keyed = kh >= 0;
        var key = request[0..0];
        var entry = 0 - 1;
        var sum = 0;
        if keyed {
            key = http.header_value(request, table, kh);
            if !idem.valid(key) {
                return refuse_event(heap, out, stats, 400, "the Idempotency-Key must be 1 to 255 visible ASCII characters", keep);
            }
            if bytes.starts_with(key, "cron:") {
                return server.failure(heap, out, 400, "Idempotency-Keys that begin with cron: are the schedules' own", keep);
            }
            sum = crc.of(body);
            entry = idem.find(ix, arena, key);
            if entry >= 0 && idem.fresh(ix, entry, now) {
                if !idem.matches(ix, entry, sum, len(body)) {
                    return refuse_event(heap, out, stats, 422, "this Idempotency-Key was used for a different event", keep);
                }
                // The same event again: nothing is written, and the answer is the first one's, after the flush that covers it
                // (a broken log fails that flush, so the answer is then 503, as for any other request held).
                note[0] = idem.id_of(ix, entry);
                note[1] = 1;
                return out;
            }
            // A key that has expired (its window has passed, or retention has dropped its event) is a new key: take the old entry out and go on.
            if entry >= 0 {
                idem.remove(ix, entry);
                entry = 0 - 1;
            }
            if !idem.room(ix, len(key)) {
                idem.evict(ix, now, 4096);
                if !idem.room(ix, len(key)) {
                    return refuse_event(heap, out, stats, 507, "too many Idempotency-Keys are held", keep);
                }
            }
        }
        let stored = store_event(scratch, body, stats[off_ex() + ex_type()..off_ex() + ex_type() + filter.max_type()], tlen, key, keyed, sum, entry, lg, ix, arena, now, stats[off_body()..off_body() + bodies.size()]);
        if stored.0 == log.too_long() {
            return refuse_event(heap, out, stats, 413, "the event is too large", keep);
        }
        if stored.0 != 0 {
            return refuse_event(heap, out, stats, 503, "the event could not be stored", keep);
        }
        note[0] = stored.1;
        note[1] = 0;
        return out;
    }
    if id == 24 {
        // DELETE /events/:id (`docs/design.md` section 47.3): erase one event.
        let want = route.param_nat(path, params, 0);
        if want < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        if want < evlog.first_id(lg) {
            return server.failure(heap, out, 410, "the event was dropped by retention: there is nothing left of it to erase", keep);
        }
        if want > evlog.last_id(lg) {
            return server.failure(heap, out, 404, "no such event", keep);
        }
        return erase_event(heap, lg, done, window, stats, want, now, keep, out);
    }
    if id == 3 {
        // GET /events/:id
        let want = route.param_nat(path, params, 0);
        if want < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        if event_erased(lg, window, want) {
            return server.failure(heap, out, 410, "the event was erased (DELETE /events/:id)", keep);
        }
        let found = find_event(heap, lg, window, want, stats[off_body()..off_body() + bodies.size()]);
        var answer = out;
        var empty = false;
        borrow found as &sz in {
            empty = buffer.size(sz) == 0;
        }
        if empty && want < evlog.first_id(lg) {
            // A tombstone (`docs/design.md` section 44): the ids are dense and never given twice, so an id below the oldest event the log keeps was an event,
            // and retention dropped it. Gone for good, which is what 410 says; an id that was never given is a 404.
            answer = server.failure(heap, answer, 410, "the event was dropped by retention (it was final at every endpoint and older than retention-days, or older than max-age-days)", keep);
        } else if empty {
            answer = server.failure(heap, answer, 404, "no such event", keep);
        } else {
            borrow found as &fb in {
                answer = server.reply(heap, answer, 200, buffer.bytes(fb), keep);
            }
        }
        buffer.drop(heap, found);
        return answer;
    }
    if id == 4 {
        // GET /stats: the delivery counters, for the tests and for a human.
        var w = json.writer(heap, 352);
        w = json.begin_object(heap, w);
        w = json.put_key(heap, w, "endpoints");
        w = json.put_int(heap, w, stats[c_endpoints()]);
        w = json.put_key(heap, w, "attempts");
        w = json.put_int(heap, w, stats[c_attempts()]);
        w = json.put_key(heap, w, "delivered");
        w = json.put_int(heap, w, stats[c_delivered()]);
        w = json.put_key(heap, w, "failed");
        w = json.put_int(heap, w, stats[c_failed()]);
        w = json.put_key(heap, w, "dead");
        w = json.put_int(heap, w, stats[c_dead()]);
        w = json.put_key(heap, w, "keys");
        w = json.put_int(heap, w, idem.count(ix));
        w = json.put_key(heap, w, "replays");
        w = json.put_int(heap, w, rp_cap() - rp_free(stats));
        w = json.put_key(heap, w, "draining");
        w = json.put_int(heap, w, draining_count(stats));
        w = json.put_key(heap, w, "paused");
        w = json.put_int(heap, w, paused_count(stats));
        w = json.put_key(heap, w, "breaker_trips");
        w = json.put_int(heap, w, stats[c_trips()]);
        w = json.put_key(heap, w, "history_live");
        w = json.put_int(heap, w, history.live(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_written");
        w = json.put_int(heap, w, history.written(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_failed");
        w = json.put_int(heap, w, history.failed(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_dropped");
        w = json.put_int(heap, w, history.dropped(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_pruned");
        w = json.put_int(heap, w, history.pruned(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "bodies_sealed");
        w = json.put_int(heap, w, bodies.sealed_count(stats[off_body()..off_body() + bodies.size()]));
        w = json.put_key(heap, w, "bodies_refused");
        w = json.put_int(heap, w, bodies.refused_count(stats[off_body()..off_body() + bodies.size()]));
        w = json.put_key(heap, w, "events_erased");
        w = json.put_int(heap, w, stats[off_ex() + ex_erased()]);
        w = json.put_key(heap, w, "events_expired");
        w = json.put_int(heap, w, stats[rt_at() + r_expired()]);
        w = json.put_key(heap, w, "segments_expired");
        w = json.put_int(heap, w, stats[rt_at() + r_expired_segments()]);
        w = json.put_key(heap, w, "audit_lines");
        w = json.put_int(heap, w, audit.written(stats[off_aud()..off_aud() + audit.size()]));
        w = json.put_key(heap, w, "audit_failures");
        w = json.put_int(heap, w, audit.failures(stats[off_aud()..off_aud() + audit.size()]));
        w = json.put_key(heap, w, "database_lookups");
        w = json.put_int(heap, w, dbname.lookups(stats[off_dbn()..off_dbn() + dbname.size()]));
        w = json.put_key(heap, w, "database_lookup_failures");
        w = json.put_int(heap, w, dbname.failures(stats[off_dbn()..off_dbn() + dbname.size()]));
        w = json.put_key(heap, w, "endpoints_loaded");
        w = json.put_bool(heap, w, history.endpoints_known(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "database_reconnects");
        w = json.put_int(heap, w, history.reconnects(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "database_failures");
        w = json.put_int(heap, w, history.failures(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "database_losses");
        w = json.put_int(heap, w, history.losses(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_queue");
        w = json.put_int(heap, w, history.pending(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "cron_fired");
        w = json.put_int(heap, w, sched.fired(sg));
        w = json.put_key(heap, w, "cron_errors");
        w = json.put_int(heap, w, sched.errors(sg));
        w = json.put_key(heap, w, "cron_skipped");
        w = json.put_int(heap, w, sched.skipped(sg));
        w = json.put_key(heap, w, "filtered");
        w = json.put_int(heap, w, stats[off_ex() + ex_filtered()]);
        w = json.put_key(heap, w, "advanced");
        w = json.put_int(heap, w, stats[off_ex() + ex_advanced()]);
        w = json.put_key(heap, w, "max_endpoints");
        w = json.put_int(heap, w, state.max_endpoints());
        w = json.put_key(heap, w, "turns");
        w = json.put_int(heap, w, stats[off_ex() + ex_turns()]);
        w = json.put_key(heap, w, "endpoints_looked_at");
        w = json.put_int(heap, w, stats[off_ex() + ex_looked()]);
        w = json.put_key(heap, w, "waits_skipped");
        w = json.put_int(heap, w, stats[off_ex() + ex_hurried()]);
        w = json.put_key(heap, w, "cron_keys");
        w = json.put_int(heap, w, idem.count(ix[idem.second_at(ix)..len(ix)]));
        w = json.put_key(heap, w, "events_first_id");
        w = json.put_int(heap, w, evlog.first_id(lg));
        w = json.put_key(heap, w, "events_last_id");
        w = json.put_int(heap, w, evlog.last_id(lg));
        w = json.put_key(heap, w, "events_segments");
        w = json.put_int(heap, w, evlog.segments(lg));
        w = json.put_key(heap, w, "events_bytes");
        w = json.put_int(heap, w, evlog.retained_bytes(lg));
        w = json.put_key(heap, w, "events_dropped");
        w = json.put_int(heap, w, evlog.dropped_events(lg));
        w = json.put_key(heap, w, "segments_dropped");
        w = json.put_int(heap, w, evlog.dropped_segments(lg));
        w = json.put_key(heap, w, "segments_sealed");
        w = json.put_int(heap, w, evlog.rolls(lg));
        w = json.put_key(heap, w, "delivery_bytes");
        w = json.put_int(heap, w, log.size(done));
        w = json.put_key(heap, w, "snapshots");
        w = json.put_int(heap, w, stats[rt_at() + r_snapshots()]);
        w = json.put_key(heap, w, "maintenance_ms_max");
        w = json.put_int(heap, w, stats[rt_at() + r_stall_max()]);
        w = json.put_key(heap, w, "maintenance_errors");
        w = json.put_int(heap, w, stats[rt_at() + r_errors()]);
        w = json.put_key(heap, w, "maintenance_lock_skips");
        w = json.put_int(heap, w, stats[rt_at() + r_lock_skips()]);
        w = json.put_key(heap, w, "events_skipped");
        w = json.put_int(heap, w, evlog.clamped(lg));
        w = json.end_object(heap, w);
        let body = json.finish(w);
        var answer = out;
        borrow body as &sb in {
            answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
        }
        buffer.drop(heap, body);
        return answer;
    }
    if id == 5 {
        // GET /config: the settings in force (`src/config.ls`), as they ended up after the file and the flags. The endpoints and
        // their secrets are not settings and are not here.
        var w = json.writer(heap, 160);
        w = json.begin_object(heap, w);
        w = json.put_key(heap, w, "schedule");
        w = json.begin_array(heap, w);
        var k = 0;
        while k < stats[off_sched()] {
            w = json.put_int(heap, w, stats[off_sched() + 1 + k]);
            k = k + 1;
        }
        w = json.end_array(heap, w);
        w = json.put_key(heap, w, "deadline-ms");
        w = json.put_int(heap, w, stats[c_deadline()]);
        w = json.put_key(heap, w, "window-ms");
        w = json.put_int(heap, w, idem.window_ms(ix));
        w = json.put_key(heap, w, "allow-private-hosts");
        w = json.put_int(heap, w, stats[c_private()]);
        w = json.put_key(heap, w, "breaker-days");
        w = json.put_int(heap, w, stats[c_breaker()]);
        w = json.put_key(heap, w, "production");
        w = json.put_int(heap, w, stats[c_production()]);
        w = json.put_key(heap, w, "cron-catchup");
        w = json.put_int(heap, w, sg[sched.catchup_at()]);
        w = json.put_key(heap, w, "cron-seconds");
        w = json.put_int(heap, w, sg[sched.seconds_at()]);
        w = json.put_key(heap, w, "stop-deadline-ms");
        w = json.put_int(heap, w, ops.stop_deadline(ops_of(stats)));
        w = json.put_key(heap, w, "repair-logs");
        w = json.put_int(heap, w, ops.repair_flag(ops_of(stats)));
        w = json.put_key(heap, w, "rotation-grace-ms");
        w = json.put_int(heap, w, stats[off_ex() + ex_grace()]);
        w = json.put_key(heap, w, "retention-days");
        w = json.put_int(heap, w, stats[rt_at() + r_retention_days()]);
        w = json.put_key(heap, w, "max-age-days");
        w = json.put_int(heap, w, stats[rt_at() + r_max_age_days()]);
        w = json.put_key(heap, w, "history-days");
        w = json.put_int(heap, w, history.prune_days(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "segment-bytes");
        w = json.put_int(heap, w, evlog.limit(lg));
        w = json.put_key(heap, w, "delivery-log-bytes");
        w = json.put_int(heap, w, stats[rt_at() + r_delivery_limit()]);
        w = json.put_key(heap, w, "idem-keys");
        w = json.put_int(heap, w, idem.capacity(ix));
        w = json.put_key(heap, w, "pg-backoff-min-ms");
        w = json.put_int(heap, w, history.setting(stats[off_hq()..off_hq() + history.size()], 0));
        w = json.put_key(heap, w, "pg-backoff-max-ms");
        w = json.put_int(heap, w, history.setting(stats[off_hq()..off_hq() + history.size()], 1));
        w = json.put_key(heap, w, "pg-attempt-ms");
        w = json.put_int(heap, w, history.setting(stats[off_hq()..off_hq() + history.size()], 2));
        w = json.put_key(heap, w, "pg-request-ms");
        w = json.put_int(heap, w, history.setting(stats[off_hq()..off_hq() + history.size()], 3));
        w = json.put_key(heap, w, "pg-start-wait-ms");
        w = json.put_int(heap, w, history.setting(stats[off_hq()..off_hq() + history.size()], 4));
        w = json.put_key(heap, w, "retry-jitter");
        w = json.put_int(heap, w, lim.jitter_percent(lim_of(stats)));
        w = json.put_key(heap, w, "endpoint-concurrency");
        w = json.put_int(heap, w, lim.conc_default(lim_of(stats)));
        w = json.put_key(heap, w, "endpoint-rate");
        w = json.put_int(heap, w, lim.rate_default(lim_of(stats)));
        w = json.end_object(heap, w);
        let body = json.finish(w);
        var answer = out;
        borrow body as &sb in {
            answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
        }
        buffer.drop(heap, body);
        return answer;
    }
    if id == 6 {
        // POST /endpoints/:id/enable: let a disabled endpoint be tried again. Answers 200 whether it was disabled or not.
        let want = route.param_nat(path, params, 0);
        if want < 0 {
            return server.failure(heap, out, 400, "the id must be a number", keep);
        }
        let wi = index_of_id(stats, want);
        if wi < 0 {
            return server.failure(heap, out, 404, "no such endpoint", keep);
        }
        let slot = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], wi);
        if is_disabled(stats, slot) {
            if note_endpoint(done, stats, state.enabled(), slot) == 0 || log.flush(done) != 0 {
                return server.failure(heap, out, 503, "the change could not be stored", keep);
            }
            set_disabled(stats, slot, false);
            // Whatever disabled it (a 410, or the circuit breaker), it starts a new run of failures from here.
            set_paused(stats, slot, false);
            stats[off_streak() + slot] = 0;
        }
        return server.reply(heap, out, 200, "{\"enabled\":true}", keep);
    }
    if id == 7 {
        // GET /endpoints: each endpoint's id, port, scheme, cursor (every event up to it is final), whether it is disabled, whether the circuit
        // breaker is what disabled it, and when its current run of failed attempts began (Unix ms, 0 if it has none). Not the host and
        // not the secret. A page of `limit` (64 unless the query says; 256 at most) from `offset`, in the table's order: the server's queue for an answer is 64 KiB, so
        // an answer cannot be all of 1,024 endpoints (`docs/design.md` section 41.4). The page also ends where its text passes 40 KiB (endpoints with the longest
        // lists of types and headers are 1.5 KB each). `X-Total-Count` says how many there are, and `X-Next-Offset` where to go on, if there is more.
        let page = bulk.parse_listing(http.query(request, table));
        if page.0 != 0 {
            return server.failure(heap, out, 400, bulk.why(page.0), keep);
        }
        var w = json.writer(heap, 160);
        w = json.begin_array(heap, w);
        var i = page.2;
        var more = true;
        while i < stats[c_endpoints()] && i < page.2 + page.1 && more {
            let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "id");
            w = json.put_int(heap, w, endpoints.ident_of(stats[off_table()..off_table() + endpoints.table_size()], i));
            w = json.put_key(heap, w, "port");
            w = json.put_int(heap, w, endpoints.port_of(stats[off_table()..off_table() + endpoints.table_size()], i));
            w = json.put_key(heap, w, "scheme");
            w = json.put_string(heap, w, scheme_name(endpoints.scheme_of(stats[off_table()..off_table() + endpoints.table_size()], i)));
            w = json.put_key(heap, w, "cursor");
            w = json.put_int(heap, w, stats[off_cur() + e]);
            w = json.put_key(heap, w, "disabled");
            w = json.put_bool(heap, w, is_disabled(stats, e));
            w = json.put_key(heap, w, "paused");
            w = json.put_bool(heap, w, is_paused(stats, e));
            w = json.put_key(heap, w, "failing_since");
            w = json.put_int(heap, w, stats[off_streak() + e]);
            w = epx.put_members(heap, w, stats[off_xt()..off_xt() + epx.xt_size()], i, now);
            w = lim.put_members(heap, w, lim_of(stats), stats[off_xt()..off_xt() + epx.xt_size()], i);
            w = json.end_object(heap, w);
            i = i + 1;
            var written = 0;
            borrow w as &wr in {
                written = len(json.bytes(wr));
            }
            if written >= 40960 {
                more = false;
            }
        }
        w = json.end_array(heap, w);
        let body = json.finish(w);
        var extra = buffer.append(heap, buffer.empty(heap, 64), "X-Total-Count: ");
        extra = buffer.push_nat(heap, extra, stats[c_endpoints()]);
        extra = buffer.append(heap, extra, "\r\n");
        if i < stats[c_endpoints()] {
            extra = buffer.append(heap, extra, "X-Next-Offset: ");
            extra = buffer.push_nat(heap, extra, i);
            extra = buffer.append(heap, extra, "\r\n");
        }
        var answer = out;
        borrow body as &sb in {
            borrow extra as &eb in {
                answer = server.reply_with(heap, answer, 200, buffer.bytes(sb), keep, buffer.bytes(eb));
            }
        }
        buffer.drop(heap, body);
        buffer.drop(heap, extra);
        return answer;
    }
    if id == 11 {
        // POST /endpoints (`docs/design.md` section 25.2): register an endpoint. It is written to the database first, and the answer waits
        // for the database (`run` holds the connection), so here the request is only judged and kept.
        let auth = manage.authorize(request, table, stats[off_token()..off_token() + manage.token_size()]);
        if auth == 1 {
            return server.failure(heap, out, 403, "endpoint management is off: the service was not given an admin-token", keep);
        }
        if auth == 2 {
            return server.failure_with(heap, out, 401, "a valid bearer token is required", keep, "WWW-Authenticate: Bearer\r\n");
        }
        if !history.enabled(stats[off_hq()..off_hq() + history.size()]) {
            return server.failure(heap, out, 503, "endpoints are managed in the database and none is named (--pg-host)", keep);
        }
        if stats[off_mg() + manage.mg_state()] != 0 {
            return server.failure(heap, out, 409, "another change is waiting for the database", keep);
        }
        if stats[c_endpoints()] >= state.max_endpoints() {
            return server.failure(heap, out, 409, "the service has as many endpoints as it can (1024)", keep);
        }
        if stats[c_endpoints()] + draining_count(stats) >= state.max_endpoints() {
            return server.failure(heap, out, 409, "every slot is taken: a deleted endpoint is still finishing an attempt, try again in a moment", keep);
        }
        let parsed = manage.parse_create(heap, body, scratch, stats[off_mg()..off_mg() + manage.mg_size()], stats[c_private()] == 1);
        if parsed.0 != 0 {
            return server.failure(heap, out, 400, manage.why(parsed.0), keep);
        }
        if parsed.3 > 0 && sign.secret_key(scratch[256..256 + parsed.3], scratch[400..496]) < 0 {
            return server.failure(heap, out, 400, manage.why(4), keep);
        }
        // The subscription and the headers (`epx.ls`): judged here, kept in the delivery state until the database answers.
        let named = epx.parse(heap, body, stats[off_xg()..off_xg() + epx.xg_size()], false, stats[off_ex() + ex_grace()], now);
        if named != 0 {
            return server.failure(heap, out, 400, epx.why(named), keep);
        }
        if endpoints.blob_used(stats[off_table()..off_table() + endpoints.table_size()], stats[c_endpoints()]) + parsed.2 + 48 + epx.text_used(stats[off_xt()..off_xt() + epx.xt_size()], stats[c_endpoints()]) + epx.pending_types_len(stats[off_xg()..off_xg() + epx.xg_size()]) + epx.pending_spec_len(stats[off_xg()..off_xg() + epx.xg_size()]) > endpoints.text_limit() {
            return server.failure(heap, out, 507, "there is no room for another host name", keep);
        }
        stats[off_mg() + manage.mg_state()] = 1;
        note[0] = 0 - 3;
        return out;
    }
    if id == 13 {
        // PATCH /endpoints/:id (`docs/design.md` section 25.3): change an endpoint's host, port or secret. Judged here like `POST /endpoints` and
        // kept for the turn that sends it to the database; the table in memory changes only when the database says commit.
        let auth = manage.authorize(request, table, stats[off_token()..off_token() + manage.token_size()]);
        if auth == 1 {
            return server.failure(heap, out, 403, "endpoint management is off: the service was not given an admin-token", keep);
        }
        if auth == 2 {
            return server.failure_with(heap, out, 401, "a valid bearer token is required", keep, "WWW-Authenticate: Bearer\r\n");
        }
        if !history.enabled(stats[off_hq()..off_hq() + history.size()]) {
            return server.failure(heap, out, 503, "endpoints are managed in the database and none is named (--pg-host)", keep);
        }
        let want = route.param_nat(path, params, 0);
        if want < 0 {
            return server.failure(heap, out, 400, "the id must be a number", keep);
        }
        if index_of_id(stats, want) < 0 {
            return server.failure(heap, out, 404, "no such endpoint", keep);
        }
        if stats[off_mg() + manage.mg_state()] != 0 {
            return server.failure(heap, out, 409, "another change is waiting for the database", keep);
        }
        let parsed = manage.parse_patch(heap, body, scratch, stats[off_mg()..off_mg() + manage.mg_size()], stats[c_private()] == 1);
        if parsed.0 != 0 {
            return server.failure(heap, out, 400, manage.why(parsed.0), keep);
        }
        if parsed.1 & 4 != 0 && sign.secret_key(scratch[256..256 + stats[off_mg() + manage.mg_secret_len()]], scratch[400..496]) < 0 {
            return server.failure(heap, out, 400, manage.why(4), keep);
        }
        // The subscription, the headers, and how long the previous secret stays valid (`epx.ls`).
        epx.clear_pending(stats[off_xg()..off_xg() + epx.xg_size()]);
        if parsed.1 & 16 != 0 {
            let named = epx.parse(heap, body, stats[off_xg()..off_xg() + epx.xg_size()], true, stats[off_ex() + ex_grace()], now);
            if named != 0 {
                return server.failure(heap, out, 400, epx.why(named), keep);
            }
            // Keeping a previous secret without making a new one needs one to keep (to end the overlap there need not be).
            if epx.pending_mask(stats[off_xg()..off_xg() + epx.xg_size()]) & epx.m_keep() != 0 && parsed.1 & 12 == 0 && epx.pending_keep_until(stats[off_xg()..off_xg() + epx.xg_size()]) > 0 && !epx.old_active(stats[off_xt()..off_xt() + epx.xt_size()], index_of_id(stats, want), now) {
                return server.failure(heap, out, 400, epx.why(305), keep);
            }
        }
        stats[off_mg() + manage.mg_target()] = want;
        stats[off_mg() + manage.mg_state()] = 1;
        note[0] = 0 - 3;
        return out;
    }
    if id == 14 {
        // DELETE /endpoints/:id (`docs/design.md` section 25.5): remove an endpoint. Judged here like `PATCH` and kept for the turn that sends it to the
        // database; memory and the log change only when the database says commit (`finish_delete`).
        let auth = manage.authorize(request, table, stats[off_token()..off_token() + manage.token_size()]);
        if auth == 1 {
            return server.failure(heap, out, 403, "endpoint management is off: the service was not given an admin-token", keep);
        }
        if auth == 2 {
            return server.failure_with(heap, out, 401, "a valid bearer token is required", keep, "WWW-Authenticate: Bearer\r\n");
        }
        if !history.enabled(stats[off_hq()..off_hq() + history.size()]) {
            return server.failure(heap, out, 503, "endpoints are managed in the database and none is named (--pg-host)", keep);
        }
        let want = route.param_nat(path, params, 0);
        if want < 0 {
            return server.failure(heap, out, 400, "the id must be a number", keep);
        }
        if index_of_id(stats, want) < 0 {
            return server.failure(heap, out, 404, "no such endpoint", keep);
        }
        if stats[off_mg() + manage.mg_state()] != 0 {
            return server.failure(heap, out, 409, "another change is waiting for the database", keep);
        }
        stats[off_mg() + manage.mg_target()] = want;
        stats[off_mg() + manage.mg_kind()] = 2;
        stats[off_mg() + manage.mg_fields()] = 0;
        stats[off_mg() + manage.mg_host_len()] = 0;
        stats[off_mg() + manage.mg_secret_len()] = 0;
        stats[off_mg() + manage.mg_make()] = 0;
        stats[off_mg() + manage.mg_state()] = 1;
        note[0] = 0 - 3;
        return out;
    }
    if id == 12 {
        // GET /endpoints/:id: one endpoint, as `GET /endpoints` lists it. Not the host and not the secret.
        let want = route.param_nat(path, params, 0);
        if want < 0 {
            return server.failure(heap, out, 400, "the id must be a number", keep);
        }
        let wi = index_of_id(stats, want);
        if wi < 0 {
            return server.failure(heap, out, 404, "no such endpoint", keep);
        }
        let slot = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], wi);
        var w = json.writer(heap, 160);
        w = json.begin_object(heap, w);
        w = json.put_key(heap, w, "id");
        w = json.put_int(heap, w, want);
        w = json.put_key(heap, w, "port");
        w = json.put_int(heap, w, endpoints.port_of(stats[off_table()..off_table() + endpoints.table_size()], wi));
        w = json.put_key(heap, w, "scheme");
        w = json.put_string(heap, w, scheme_name(endpoints.scheme_of(stats[off_table()..off_table() + endpoints.table_size()], wi)));
        w = json.put_key(heap, w, "cursor");
        w = json.put_int(heap, w, stats[off_cur() + slot]);
        w = json.put_key(heap, w, "disabled");
        w = json.put_bool(heap, w, is_disabled(stats, slot));
        w = json.put_key(heap, w, "paused");
        w = json.put_bool(heap, w, is_paused(stats, slot));
        w = json.put_key(heap, w, "failing_since");
        w = json.put_int(heap, w, stats[off_streak() + slot]);
        w = epx.put_members(heap, w, stats[off_xt()..off_xt() + epx.xt_size()], wi, now);
        w = lim.put_members(heap, w, lim_of(stats), stats[off_xt()..off_xt() + epx.xt_size()], wi);
        w = json.end_object(heap, w);
        let body = json.finish(w);
        var answer = out;
        borrow body as &sb in {
            answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
        }
        buffer.drop(heap, body);
        return answer;
    }
    if id == 8 || id == 9 {
        // POST /events/:id/replay[/:endpoint]: send the event again to every endpoint, or to one, whatever happened to it there.
        let want = route.param_nat(path, params, 0);
        if want < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        var only = 0 - 1;
        if id == 9 {
            only = route.param_nat(path, params, 1);
            if only < 0 {
                return server.failure(heap, out, 400, "the endpoint must be a number", keep);
            }
            let oi = index_of_id(stats, only);
            if oi < 0 {
                return server.failure(heap, out, 404, "no such endpoint", keep);
            }
            only = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], oi);
        }
        let offset = find_offset(lg, window, want);
        if offset < 0 && want < evlog.first_id(lg) {
            return server.failure(heap, out, 410, "the event was dropped by retention and cannot be replayed", keep);
        }
        if offset < 0 {
            return server.failure(heap, out, 404, "no such event", keep);
        }
        if erased_record(window) {
            return server.failure(heap, out, 410, "the event was erased and cannot be replayed", keep);
        }
        // A replay to every endpoint goes to those whose subscription wants the event's type; a replay to one endpoint, named, goes whatever it
        // subscribes to (section 35). The type is read from the record, which is left in `window` for the loops below.
        var tstart = 0;
        var tlen = 0;
        if only < 0 {
            if evlog.read_at(lg, offset, window).0 == 0 {
                let t = filter.type_in(window);
                tstart = t.0;
                tlen = t.1;
            }
        }
        var needed = 0;
        var i = 0;
        while i < stats[c_endpoints()] {
            let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if (e == only || only < 0 && epx.accepts(stats[off_xt()..off_xt() + epx.xt_size()], i, window[tstart..tstart + tlen])) && rp_find(stats, e, want) < 0 {
                needed = needed + 1;
            }
            i = i + 1;
        }
        if needed > rp_free(stats) {
            return server.failure(heap, out, 507, "too many replays are waiting", keep);
        }
        // The records first, then one flush, and only then the table: a replay that was not stored is not started.
        i = 0;
        while i < stats[c_endpoints()] {
            let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if e == only || only < 0 && epx.accepts(stats[off_xt()..off_xt() + epx.xt_size()], i, window[tstart..tstart + tlen]) {
                if note_outcome(done, stats, state.replay(), e, want, 0, 0) == 0 {
                    return server.failure(heap, out, 503, "the replay could not be stored", keep);
                }
            }
            i = i + 1;
        }
        if log.flush(done) != 0 {
            return server.failure(heap, out, 503, "the replay could not be stored", keep);
        }
        var w = json.writer(heap, 96);
        w = json.begin_object(heap, w);
        w = json.put_key(heap, w, "event");
        w = json.put_int(heap, w, want);
        w = json.put_key(heap, w, "endpoints");
        w = json.begin_array(heap, w);
        i = 0;
        while i < stats[c_endpoints()] {
            let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if e == only || only < 0 && epx.accepts(stats[off_xt()..off_xt() + epx.xt_size()], i, window[tstart..tstart + tlen]) {
                rp_put(stats, e, want, offset);
                w = json.put_int(heap, w, endpoints.ident_of(stats[off_table()..off_table() + endpoints.table_size()], i));
            }
            i = i + 1;
        }
        w = json.end_array(heap, w);
        w = json.end_object(heap, w);
        let body = json.finish(w);
        var answer = out;
        borrow body as &sb in {
            answer = server.reply(heap, answer, 202, buffer.bytes(sb), keep);
        }
        buffer.drop(heap, body);
        return answer;
    }
    if id == 10 {
        // GET /events/:id/attempts: what happened to the event, from the database. The request is held until it answers (`run`).
        let want = route.param_nat(path, params, 0);
        if want < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        if want > evlog.last_id(lg) {
            return server.failure(heap, out, 404, "no such event", keep);
        }
        if !history.enabled(stats[off_hq()..off_hq() + history.size()]) {
            return server.failure(heap, out, 503, "no database is named, so no history is kept", keep);
        }
        note[0] = 0 - 2;
        note[1] = want;
        return out;
    }
    if id == 0 - 2 {
        var extra = buffer.append(heap, buffer.empty(heap, 48), "Allow: ");
        extra = route.allowed(heap, router, path, params, extra);
        extra = buffer.append(heap, extra, "\r\n");
        var answer = out;
        borrow extra as &eb in {
            answer = server.failure_with(heap, answer, 405, "method not allowed", keep, buffer.bytes(eb));
        }
        buffer.drop(heap, extra);
        return answer;
    }
    return server.failure(heap, out, 404, "not found", keep);
}

// ---------------------------------------------------------------------
// Dead letters, bulk replay, and cancelling a replay (`docs/design.md` section 39.1 and 36.2)
// ---------------------------------------------------------------------

// Where in the events log each dead letter of slot `e` starts, for the ones whose place is not known (a table built at start from the delivery log, which
// does not say). One pass over the events log, from the record of the last entry whose place is known, to the last entry that needs it; the entries
// and the log are both in event order, so it is a merge. Answers how many places were found.
fn resolve_offsets[&l, &w, &d](lg: &!l evlog.Ev, window: &!w [byte], dv: &!d [int], e: int) -> [fs_read(""), file_read] int {
    let n = dead.count(dead_of(dv), e);
    var first = 0 - 1;
    var k = 0;
    while k < n && first < 0 {
        if dead.offset_at(dead_of(dv), e, k) < 0 {
            first = k;
        }
        k = k + 1;
    }
    if first < 0 {
        return 0;
    }
    var p = first;
    var at = 0;
    if first > 0 {
        p = first - 1;
        at = dead.offset_at(dead_of(dv), e, first - 1);
    }
    var found = 0;
    var going = true;
    while going && p < n {
        let r = evlog.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else {
            let id = record.ms_of(window, 0);
            while p < n && dead.id_at(dead_of(dv), e, p) < id {
                p = p + 1;
            }
            if p < n && dead.id_at(dead_of(dv), e, p) == id {
                if dead.offset_at(dead_of(dv), e, p) < 0 {
                    dead.set_offset(dead_of_mut(dv), e, p, at);
                    found = found + 1;
                }
                p = p + 1;
            }
            at = at + r.1;
        }
    }
    return found;
}

// Read the event of entry `k` of slot `e`'s dead letters into `window`. Answers 1 if it is there (the place is known and the record at it is that event), else 0.
fn load_dead[&l, &w, &d](lg: &!l evlog.Ev, window: &!w [byte], dv: &d [int], e: int, k: int) -> [fs_read(""), file_read] int {
    let off = dead.offset_at(dead_of(dv), e, k);
    if off < 0 {
        return 0;
    }
    if evlog.read_at(lg, off, window).0 != 0 || record.ms_of(window, 0) != dead.id_at(dead_of(dv), e, k) {
        return 0;
    }
    return 1;
}

fn put_dead_entry[&h, &l, &w, &d](heap: &!h Heap, wr: json.Writer, lg: &!l evlog.Ev, window: &!w [byte], dv: &d [int], e: int, k: int) -> [heap, fs_read(""), file_read] json.Writer {
    var w = json.begin_object(heap, wr);
    w = json.put_key(heap, w, "event");
    w = json.put_int(heap, w, dead.id_at(dead_of(dv), e, k));
    w = json.put_key(heap, w, "type");
    var typed = false;
    if load_dead(lg, window, dv, e, k) == 1 {
        let t = filter.type_in(window);
        if t.1 > 0 {
            w = json.put_string(heap, w, window[t.0..t.0 + t.1]);
            typed = true;
        }
    }
    if !typed {
        w = json.put_null(heap, w);
    }
    w = json.put_key(heap, w, "attempts");
    w = json.put_int(heap, w, dead.attempts_at(dead_of(dv), e, k));
    w = json.put_key(heap, w, "reason");
    if dead.reason_at(dead_of(dv), e, k) == 0 {
        w = json.put_string(heap, w, "unrecorded");
    } else {
        w = json.put_string(heap, w, reason.name(dead.reason_at(dead_of(dv), e, k)));
    }
    w = json.put_key(heap, w, "died_at");
    w = json.put_int(heap, w, dead.died_at(dead_of(dv), e, k));
    w = json.put_key(heap, w, "replaying");
    w = json.put_bool(heap, w, rp_find(dv, e, dead.id_at(dead_of(dv), e, k)) >= 0);
    return json.end_object(heap, w);
}

// `GET /endpoints/:id/dead`: a page of the endpoint's dead letters, newest first (or oldest, `order=asc`), from the entry after `after` in that order.
fn dead_list[&h, &q, &t, &p, &l, &w, &g, &s](heap: &!h Heap, request: &q [byte], table: &t [int], path: &q [byte], params: &!p [int], lg: &!l evlog.Ev, window: &!w [byte], done: &!g log.Log, stats: &!s [int], keep: bool, out: buffer.Buffer) -> [heap, fs_read(""), file_read] buffer.Buffer {
    let want = route.param_nat(path, params, 0);
    if want < 0 {
        return server.failure(heap, out, 400, "the id must be a number", keep);
    }
    let wi = index_of_id(stats, want);
    if wi < 0 {
        return server.failure(heap, out, 404, "no such endpoint", keep);
    }
    let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], wi);
    let page = bulk.parse_page(http.query(request, table));
    if page.0 != 0 {
        return server.failure(heap, out, 400, bulk.why(page.0), keep);
    }
    // a table that was short of dead letters and has room now is completed from the log first; the ones whose events retention has dropped are gone
    dead_complete(done, window, stats, e);
    dead.expire(dead_of_mut(stats), e, evlog.first_id(lg));
    let n = dead.count(dead_of(stats), e);
    // where the page starts, and which way it goes
    var at = 0;
    if page.2 {
        at = n - 1;
        if page.3 >= 0 {
            at = dead.below(dead_of(stats), e, page.3) - 1;
        }
    } else if page.3 >= 0 {
        at = dead.upto(dead_of(stats), e, page.3);
    }
    // the places of the events of the page, if they are not known: one pass for all of them
    var need = false;
    var j = 0;
    var k = at;
    while j < page.1 && k >= 0 && k < n {
        if dead.offset_at(dead_of(stats), e, k) < 0 {
            need = true;
        }
        if page.2 {
            k = k - 1;
        } else {
            k = k + 1;
        }
        j = j + 1;
    }
    if need {
        resolve_offsets(lg, window, stats, e);
    }
    var wr = json.writer(heap, 1024);
    wr = json.begin_object(heap, wr);
    wr = json.put_key(heap, wr, "endpoint");
    wr = json.put_int(heap, wr, want);
    wr = json.put_key(heap, wr, "order");
    if page.2 {
        wr = json.put_string(heap, wr, "desc");
    } else {
        wr = json.put_string(heap, wr, "asc");
    }
    wr = json.put_key(heap, wr, "held");
    wr = json.put_int(heap, wr, n);
    wr = json.put_key(heap, wr, "truncated");
    wr = json.put_bool(heap, wr, dead.floor(dead_of(stats), e) > 0);
    wr = json.put_key(heap, wr, "complete_above");
    wr = json.put_int(heap, wr, dead.floor(dead_of(stats), e));
    wr = json.put_key(heap, wr, "dead");
    wr = json.begin_array(heap, wr);
    var last = 0 - 1;
    j = 0;
    k = at;
    while j < page.1 && k >= 0 && k < n {
        wr = put_dead_entry(heap, wr, lg, window, stats, e, k);
        last = dead.id_at(dead_of(stats), e, k);
        if page.2 {
            k = k - 1;
        } else {
            k = k + 1;
        }
        j = j + 1;
    }
    wr = json.end_array(heap, wr);
    wr = json.put_key(heap, wr, "next");
    if j == page.1 && k >= 0 && k < n {
        wr = json.put_int(heap, wr, last);
    } else {
        wr = json.put_null(heap, wr);
    }
    wr = json.end_object(heap, wr);
    let body = json.finish(wr);
    var answer = out;
    borrow body as &sb in {
        answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, body);
    return answer;
}

// `POST /endpoints/:id/replay-dead`: send the endpoint's dead letters again, oldest first, as replays (section 23), as many as the table of waiting replays has room
// for (32 in all) and `limit` allows; the answer says how many were taken and how many are left, and a caller goes on until none is.
fn dead_replay[&h, &q, &p, &b, &l, &w, &g, &s](heap: &!h Heap, path: &q [byte], params: &!p [int], body: &b [byte], lg: &!l evlog.Ev, window: &!w [byte], done: &!g log.Log, stats: &!s [int], keep: bool, out: buffer.Buffer) -> [heap, fs_read(""), file_read, file_write] buffer.Buffer {
    let want = route.param_nat(path, params, 0);
    if want < 0 {
        return server.failure(heap, out, 400, "the id must be a number", keep);
    }
    let wi = index_of_id(stats, want);
    if wi < 0 {
        return server.failure(heap, out, 404, "no such endpoint", keep);
    }
    let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], wi);
    // The region is left by falling out of it: one left by a `return` is not given back (lex-sys #252), and this runs for every bulk replay.
    var answer = out;
    region a {
        let list = alloc_slice[a](filter.max_list() + 8, 0);
        let picked = alloc_slice[a](2 * rp_cap() + 2, 0);
        answer = dead_replay_in(heap, body, lg, window, done, stats, keep, answer, want, e, list, picked);
    }
    return answer;
}

// The body of `dead_replay`, with the room it works in given to it (so that `dead_replay` can leave its region by falling out of it).
fn dead_replay_in[&h, &b, &l, &w, &g, &s, &t, &k](heap: &!h Heap, body: &b [byte], lg: &!l evlog.Ev, window: &!w [byte], done: &!g log.Log, stats: &!s [int], keep: bool, out: buffer.Buffer, want: int, e: int, list: &!t [int], picked: &!k [int]) -> [heap, fs_read(""), file_read, file_write] buffer.Buffer {
    let parsed = bulk.parse_body(heap, body, list);
    if parsed.0 != 0 {
        return server.failure(heap, out, 400, bulk.why(parsed.0), keep);
    }
    dead_complete(done, window, stats, e);
    dead.expire(dead_of_mut(stats), e, evlog.first_id(lg));
    var room = rp_free(stats);
    if parsed.1 > 0 && parsed.1 < room {
        room = parsed.1;
    }
    let n = dead.count(dead_of(stats), e);
    if parsed.3 > 0 {
        // a subscription is read from the events themselves
        resolve_offsets(lg, window, stats, e);
    }
    // oldest first, past `after`: the ones that are not already replaying and are of the types asked for. The first `room` are taken; the rest are counted.
    var taken = 0;
    var remaining = 0;
    var k = dead.upto(dead_of(stats), e, parsed.2);
    while k < n {
        let id = dead.id_at(dead_of(stats), e, k);
        var wanted = rp_find(stats, e, id) < 0;
        if wanted && parsed.3 > 0 {
            wanted = false;
            if load_dead(lg, window, stats, e, k) == 1 {
                let t = filter.type_in(window);
                wanted = filter.accepts(list, parsed.3, window[t.0..t.0 + t.1]);
            }
        }
        if wanted {
            if taken < room && taken < rp_cap() {
                picked[2 * taken] = id;
                picked[2 * taken + 1] = dead.offset_at(dead_of(stats), e, k);
                taken = taken + 1;
            } else {
                remaining = remaining + 1;
            }
        }
        k = k + 1;
    }
    // The records first, then one flush, and only then the table: a replay that was not stored is not started (as `POST /events/:id/replay`).
    var i = 0;
    while i < taken {
        if note_outcome(done, stats, state.replay(), e, picked[2 * i], 0, 0) == 0 {
            return server.failure(heap, out, 503, "the replays could not be stored", keep);
        }
        i = i + 1;
    }
    if taken > 0 && log.flush(done) != 0 {
        return server.failure(heap, out, 503, "the replays could not be stored", keep);
    }
    i = 0;
    while i < taken {
        rp_put(stats, e, picked[2 * i], picked[2 * i + 1]);
        i = i + 1;
    }
    var wr = json.writer(heap, 160);
    wr = json.begin_object(heap, wr);
    wr = json.put_key(heap, wr, "endpoint");
    wr = json.put_int(heap, wr, want);
    wr = json.put_key(heap, wr, "taken");
    wr = json.put_int(heap, wr, taken);
    wr = json.put_key(heap, wr, "remaining");
    wr = json.put_int(heap, wr, remaining);
    wr = json.put_key(heap, wr, "waiting");
    wr = json.put_int(heap, wr, rp_cap() - rp_free(stats));
    wr = json.put_key(heap, wr, "next");
    if taken > 0 {
        wr = json.put_int(heap, wr, picked[2 * (taken - 1)]);
    } else {
        wr = json.put_null(heap, wr);
    }
    wr = json.end_object(heap, wr);
    let payload = json.finish(wr);
    var status = 200;
    if taken > 0 {
        status = 202;
    }
    var answer = out;
    borrow payload as &sb in {
        answer = server.reply(heap, answer, status, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, payload);
    return answer;
}

// Cancel the waiting replays of endpoint slot `e`: of the event `only` (0: of all). A replay with an attempt on the wire is left (its outcome is recorded when
// it ends). Each cancelled replay is a record (`state.replay_cancelled()`), one flush for all of them, and the table changes after it. Answers
// `(cancelled, busy)`, or `(-1, 0)` if the records could not be stored (nothing changed).
fn cancel_replays[&g, &d](done: &!g log.Log, dv: &!d [int], e: int, only: int) -> [file_write] (int, int) {
    var cancelled = 0;
    var busy = 0;
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 1] == e && (only == 0 || dv[b + 2] == only) {
            if dv[b + 6] == 1 {
                busy = busy + 1;
            } else if note_outcome(done, dv, state.replay_cancelled(), e, dv[b + 2], 0, 0) == 1 {
                cancelled = cancelled + 1;
            } else {
                return (0 - 1, 0);
            }
        }
        r = r + 1;
    }
    if cancelled > 0 && log.flush(done) != 0 {
        return (0 - 1, 0);
    }
    // the table, after the records are down: the same entries again, those that were not busy
    r = 0;
    while r < rp_cap() && cancelled > 0 {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 1] == e && (only == 0 || dv[b + 2] == only) && dv[b + 6] == 0 {
            dv[b] = 0;
        }
        r = r + 1;
    }
    return (cancelled, busy);
}

// `DELETE /events/:id/replay/:endpoint` (id 22) and `DELETE /endpoints/:id/replays` (id 23).
fn cancel_route[&h, &q, &p, &g, &s](heap: &!h Heap, id: int, path: &q [byte], params: &!p [int], done: &!g log.Log, stats: &!s [int], keep: bool, out: buffer.Buffer) -> [heap, file_write] buffer.Buffer {
    var event = 0;
    var endpoint = 0 - 1;
    if id == 22 {
        event = route.param_nat(path, params, 0);
        if event < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        endpoint = route.param_nat(path, params, 1);
    } else {
        endpoint = route.param_nat(path, params, 0);
    }
    if endpoint < 0 {
        return server.failure(heap, out, 400, "the endpoint must be a number", keep);
    }
    let wi = index_of_id(stats, endpoint);
    if wi < 0 {
        return server.failure(heap, out, 404, "no such endpoint", keep);
    }
    let e = endpoints.slot_of(stats[off_table()..off_table() + endpoints.table_size()], wi);
    if id == 22 && rp_find(stats, e, event) < 0 {
        return server.failure(heap, out, 404, "no replay of that event is waiting for that endpoint", keep);
    }
    let (cancelled, busy) = cancel_replays(done, stats, e, event);
    if cancelled < 0 {
        return server.failure(heap, out, 503, "the cancellation could not be stored", keep);
    }
    if id == 22 && busy > 0 {
        return server.failure(heap, out, 409, "an attempt of that replay is on the wire; ask again when it has ended", keep);
    }
    var wr = json.writer(heap, 96);
    wr = json.begin_object(heap, wr);
    if id == 22 {
        wr = json.put_key(heap, wr, "event");
        wr = json.put_int(heap, wr, event);
    }
    wr = json.put_key(heap, wr, "endpoint");
    wr = json.put_int(heap, wr, endpoint);
    wr = json.put_key(heap, wr, "cancelled");
    wr = json.put_int(heap, wr, cancelled);
    wr = json.put_key(heap, wr, "busy");
    wr = json.put_int(heap, wr, busy);
    wr = json.end_object(heap, wr);
    let payload = json.finish(wr);
    var answer = out;
    borrow payload as &sb in {
        answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, payload);
    return answer;
}

fn routes[&h](heap: &!h Heap) -> [heap] route.Router {
    var r = route.empty(heap);
    r = route.add(heap, r, "GET", "/healthz", 1);
    r = route.add(heap, r, "POST", "/events", 2);
    r = route.add(heap, r, "GET", "/events/:id", 3);
    r = route.add(heap, r, "GET", "/stats", 4);
    r = route.add(heap, r, "GET", "/config", 5);
    r = route.add(heap, r, "POST", "/endpoints/:id/enable", 6);
    r = route.add(heap, r, "GET", "/endpoints", 7);
    r = route.add(heap, r, "POST", "/events/:id/replay", 8);
    r = route.add(heap, r, "POST", "/events/:id/replay/:endpoint", 9);
    r = route.add(heap, r, "GET", "/events/:id/attempts", 10);
    r = route.add(heap, r, "POST", "/endpoints", 11);
    r = route.add(heap, r, "GET", "/endpoints/:id", 12);
    r = route.add(heap, r, "PATCH", "/endpoints/:id", 13);
    r = route.add(heap, r, "DELETE", "/endpoints/:id", 14);
    r = route.add(heap, r, "POST", "/schedules", 15);
    r = route.add(heap, r, "GET", "/schedules", 16);
    r = route.add(heap, r, "GET", "/schedules/:id", 17);
    r = route.add(heap, r, "PATCH", "/schedules/:id", 18);
    r = route.add(heap, r, "DELETE", "/schedules/:id", 19);
    r = route.add(heap, r, "GET", "/endpoints/:id/dead", 20);
    r = route.add(heap, r, "POST", "/endpoints/:id/replay-dead", 21);
    r = route.add(heap, r, "DELETE", "/events/:id/replay/:endpoint", 22);
    r = route.add(heap, r, "DELETE", "/endpoints/:id/replays", 23);
    r = route.add(heap, r, "DELETE", "/events/:id", 24);
    r = route.add(heap, r, "GET", "/readyz", 40);
    r = route.add(heap, r, "GET", "/metrics", 41);
    return r;
}

// ---------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------

// The delivery state is one heap array of integers, `dv`, laid out as follows (`docs/design.md` sections 15 and 25). Every offset
// is computed from the one before it (`off_*` below): the offsets table once overlapped the cells because two of them were written
// by hand.
//
//     ctl      16    counters and settings (the `c_*` indices below)
//     table    8 x N the endpoints, eight integers each (`endpoints.ls`); the first is the *slot*, the last the *id*   (N is `state.max_endpoints()`, 1,024)
//     cur      N     per slot: every event up to this one is final
//     sched    17    the retry schedule: the number of delays, then the delays in ms
//     flying   N     per slot: how many attempts are in flight
//     slotid   N     per slot: the id of the endpoint that has it, or free, or never seen
//     scan     2 N   per slot: the last event of the events log that endpoint has looked at, and where the next record starts
//     streak   N     per slot: when the endpoint's current run of failed attempts began (Unix ms), 0 if it has none
//     flags    N     per slot: disabled, paused, draining, tripped (`f_*`)
//     wake     N     per slot: when the loop next has anything to do for the endpoint (`off_wake`)
//     token    768   the admin, ingest and read tokens (`authz.ls`), 256 each: the length, then the bytes
//     mg       368   the change that waits for the database (`manage.ls`)
//     offs     N x 1024  per slot: where in the events log each event of that endpoint's window starts, by `id % window`
//     cells    ...   `state.cells(N)`: final / attempts / next attempt, per slot and `id % window`
//     ...      the flight flags, the replays, the history ring, the counters of `ops.ls` (`ops`), and last:
//     xt       164920  per table entry: its subscription, custom headers and previous secret (`epx.ls`)
//     xg       4616  the subscription and headers a change waits to apply (`epx.ls`)
//     ex       136   counters and a setting of the extras, and the type of the event being accepted (`ex_*` below)

// Cell 10 of the control block was the disabled set, a bit a slot, and cell 0 the paused set (one integer each, which is where 62 came from). The flags are a
// word a slot now (`off_flags()`, `docs/design.md` section 41.4); cell 10 is free.

// 1 once `delivery.seg` is known to hold the marker that it uses a slot of 62 or above (`state.wide()`, `docs/design.md` section 41.5): read at the start, or written when
// the first such slot was given. A snapshot writes the marker again if a slot of 62 or above has an owner.
fn c_wide() -> [] int {
    return 0;
}

// The circuit breaker's setting: days of failure after which an endpoint is paused, 0 for never (`breaker-days`).
fn c_breaker() -> [] int {
    return 1;
}

fn c_seq() -> [] int {
    return 2;
}

fn c_endpoints() -> [] int {
    return 3;
}

fn c_turn() -> [] int {
    return 4;
}

fn c_attempts() -> [] int {
    return 5;
}

fn c_delivered() -> [] int {
    return 6;
}

fn c_failed() -> [] int {
    return 7;
}

fn c_dead() -> [] int {
    return 8;
}

// How long an attempt may take in all, in ms.
fn c_deadline() -> [] int {
    return 9;
}

// 1 if endpoints may be names and non-public addresses (`allow-private-hosts`, section 26), else 0.
fn c_private() -> [] int {
    return 11;
}

// How many slots have the *draining* flag: the endpoint that had the slot has been deleted (its row is gone and it is not in the table) but an attempt it
// began is still on the wire. The slot is not free and not dormant, and cannot be given to anyone, until `flying` is 0 and the `removed` record is written
// (`docs/design.md` section 25.5).
fn c_draining() -> [] int {
    return 12;
}

// How many slots have the *tripped* flag: the breaker has paused the endpoint since the loop last said so on stderr.
fn c_tripped() -> [] int {
    return 13;
}

// How many times the breaker has paused an endpoint since the service started.
fn c_trips() -> [] int {
    return 14;
}

// 1 if the service was started with `production = 1` (`docs/design.md` section 33), else 0; it only shows in `GET /config`.
fn c_production() -> [] int {
    return 15;
}

// The flags of a slot (`off_flags()`): a word each, where there were four integers of one bit a slot.
fn f_disabled() -> [] int {
    return 1;
}

// The breaker paused it (`docs/design.md` section 31). `f_disabled` is set as well, so everything that skips a disabled endpoint skips this one; this flag says why.
fn f_paused() -> [] int {
    return 2;
}

fn f_draining() -> [] int {
    return 4;
}

fn f_tripped() -> [] int {
    return 8;
}

fn has_flag[&d](dv: &d [int], e: int, flag: int) -> [] bool {
    return dv[off_flags() + e] & flag != 0;
}

// Set or clear `flag` of slot `e`; answers 1 if that changed it.
fn put_flag[&d](dv: &!d [int], e: int, flag: int, on: bool) -> [] int {
    let was = dv[off_flags() + e] & flag != 0;
    if on && !was {
        dv[off_flags() + e] = dv[off_flags() + e] | flag;
        return 1;
    }
    if !on && was {
        dv[off_flags() + e] = dv[off_flags() + e] & ~flag;
        return 1;
    }
    return 0;
}

fn is_draining[&d](dv: &d [int], e: int) -> [] bool {
    return has_flag(dv, e, f_draining());
}

fn set_draining[&d](dv: &!d [int], e: int, on: bool) -> [] int {
    if put_flag(dv, e, f_draining(), on) == 1 {
        if on {
            dv[c_draining()] = dv[c_draining()] + 1;
        } else {
            dv[c_draining()] = dv[c_draining()] - 1;
        }
    }
    return 0;
}

fn draining_count[&d](dv: &d [int]) -> [] int {
    return dv[c_draining()];
}

fn is_disabled[&d](dv: &d [int], e: int) -> [] bool {
    return has_flag(dv, e, f_disabled());
}

// An endpoint that is enabled again is looked at at once (`off_wake()`).
fn set_disabled[&d](dv: &!d [int], e: int, on: bool) -> [] int {
    put_flag(dv, e, f_disabled(), on);
    if !on {
        dv[off_wake() + e] = 0;
    }
    return 0;
}

fn is_paused[&d](dv: &d [int], e: int) -> [] bool {
    return has_flag(dv, e, f_paused());
}

fn set_paused[&d](dv: &!d [int], e: int, on: bool) -> [] int {
    put_flag(dv, e, f_paused(), on);
    return 0;
}

// The breaker has paused slot `e` and the loop has not said so yet; `take_tripped` says it was said.
fn set_tripped[&d](dv: &!d [int], e: int) -> [] int {
    if put_flag(dv, e, f_tripped(), true) == 1 {
        dv[c_tripped()] = dv[c_tripped()] + 1;
    }
    return 0;
}

fn take_tripped[&d](dv: &!d [int], e: int) -> [] bool {
    if put_flag(dv, e, f_tripped(), false) == 1 {
        dv[c_tripped()] = dv[c_tripped()] - 1;
        return true;
    }
    return false;
}

// How many endpoints the breaker has paused now.
fn paused_count[&d](dv: &d [int]) -> [] int {
    var n = 0;
    var e = 0;
    while e < state.max_endpoints() {
        if is_paused(dv, e) {
            n = n + 1;
        }
        e = e + 1;
    }
    return n;
}

// Append an outcome record of any kind to `done`, not yet flushed. Answers 1 if it was appended, 0 if the log refused it.
fn note_outcome[&g, &d](done: &!g log.Log, dv: &!d [int], kind: int, e: int, id: int, attempts: int, next_at: int) -> [file_write] int {
    if before_outcome(done, dv, kind, e, id) == 0 {
        return 0;
    }
    var ok = 0;
    region a {
        let rec = alloc_slice[a](128, byte_of(0));
        let total = state.put_outcome(rec, 0, dv[c_seq()], kind, e, id, attempts, next_at);
        if log.append(done, rec[0..total], dv[c_seq()], 0) == 0 {
            dv[c_seq()] = dv[c_seq()] + 1;
            ok = 1;
        }
    }
    return ok;
}

// Erase event `want` (`docs/design.md` section 47.3), in this order, each step durable before the next: the `erased` record, flushed (from then on it is never
// sent, replayed or served); in memory, final in every window that holds it (an attempt on the wire finishes), its waiting replays ended, its dead letters gone;
// the segment that holds it sealed if it is the one being written; its body replaced in the segment (`evlog.redact`). A start finds an `erased` record whose
// body is not yet replaced and replaces it (`redo_erasures`).
fn erase_event[&h, &l, &g, &w, &d](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], want: int, now: int, keep: bool, out: buffer.Buffer) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] buffer.Buffer {
    let at = find_offset(lg, window, want);
    if at < 0 {
        return server.failure(heap, out, 404, "no such event", keep);
    }
    var already = erased_record(window);
    if !already {
        if note_outcome(done, dv, state.erased(), 0, want, 0, 0) == 0 || log.flush(done) != 0 {
            return server.failure(heap, out, 503, "the erasure could not be stored; nothing was changed", keep);
        }
        erase_in_memory(done, dv, want);
        log.flush(done);
        var rc = evlog.flush(lg);
        if rc == 0 {
            rc = evlog.redact(heap, lg, at);
            if rc == 1 {
                rc = evlog.roll(lg, now);
                if rc == 0 {
                    rc = evlog.redact(heap, lg, at);
                }
            }
        }
        dv[off_ex() + ex_erased()] = dv[off_ex() + ex_erased()] + 1;
        if rc != 0 && rc != 4 {
            return server.failure(heap, out, 503, "the event is erased for delivery, replay and reading, but its segment could not be rewritten now (the disk?); it is rewritten at the next start", keep);
        }
    }
    var wr = json.writer(heap, 96);
    wr = json.begin_object(heap, wr);
    wr = json.put_key(heap, wr, "id");
    wr = json.put_int(heap, wr, want);
    wr = json.put_key(heap, wr, "erased");
    wr = json.put_bool(heap, wr, true);
    if already {
        wr = json.put_key(heap, wr, "already");
        wr = json.put_bool(heap, wr, true);
    }
    wr = json.end_object(heap, wr);
    let body = json.finish(wr);
    var answer = out;
    borrow body as &sb in {
        answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, body);
    return answer;
}

// What an erasure does to the state (section 47.3), at the request and when the `erased` record is replayed: the event is final in every window that holds it and
// is not on the wire, its dead letters are gone, and (with `done`, at the request) its waiting replays are ended with a `replay_cancelled` record each.
fn erase_in_memory[&g, &d](done: &!g log.Log, dv: &!d [int], want: int) -> [file_write] int {
    erase_cells(dv, want);
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 6] == 0 && dv[b + 2] == want {
            note_outcome(done, dv, state.replay_cancelled(), dv[b + 1], want, 0, 0);
            dv[b] = 0;
        }
        r = r + 1;
    }
    return 0;
}

fn erase_cells[&d](dv: &!d [int], want: int) -> [] int {
    var e = 0;
    while e < state.max_endpoints() {
        if dv[off_slotid() + e] >= 0 {
            if state.in_window(dv[off_cur()..off_cur() + state.max_endpoints()], e, want) && !state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, want) && dv[flight_at(e, want)] == 0 {
                apply_outcome(dv, e, state.delivered(), want, 0, 0);
            }
            dead.remove(dead_of_mut(dv), e, want);
        }
        e = e + 1;
    }
    return 0;
}

// At the start (section 47.3): every event the outcomes log says was erased whose body is still in its segment (the service stopped between the record and the
// rename) is erased in its segment now. Answers how many were.
fn redo_erasures[&h, &l, &g, &w](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], now: int) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] int {
    var redone = 0;
    var at = 0;
    var going = true;
    var ids = buffer.empty(heap, 64);
    var n = 0;
    while going {
        let r = log.read_at(done, at, window);
        if r.0 != 0 {
            going = false;
        } else {
            let o = state.outcome_at(window, 0);
            if o.0 == state.erased() && n < 4096 {
                ids = buffer.push_nat(heap, ids, o.2);
                ids = buffer.append(heap, ids, ",");
                n = n + 1;
            }
            at = at + r.1;
        }
    }
    var k = 0;
    var v = 0;
    var bl = 0;
    borrow ids as &ir in {
        bl = len(buffer.bytes(ir));
    }
    while k < bl {
        var c = 0;
        borrow ids as &ir in {
            c = int_of(buffer.bytes(ir)[k]);
        }
        if c == ',' {
            if v >= evlog.first_id(lg) && !event_erased(lg, window, v) {
                let off = find_offset(lg, window, v);
                if off >= 0 {
                    var rc = evlog.redact(heap, lg, off);
                    if rc == 1 && evlog.roll(lg, now) == 0 {
                        rc = evlog.redact(heap, lg, off);
                    }
                    if rc == 0 {
                        redone = redone + 1;
                    }
                }
            }
            v = 0;
        } else {
            v = v * 10 + c - '0';
        }
        k = k + 1;
    }
    buffer.drop(heap, ids);
    return redone;
}

// Is the record in `window` an event whose body was erased (`docs/design.md` section 47.3)? Its first pair is the body.
fn erased_record[&w](window: &w [byte]) -> [] bool {
    let p = record.pair_at(window, record.first_pair(0));
    return evlog.is_erased(window[p.2..p.2 + p.3]);
}

// Is event `id` in the log and erased? Reads its record into `window`.
fn event_erased[&l, &w](lg: &!l evlog.Ev, window: &!w [byte], id: int) -> [fs_read(""), file_read] bool {
    let at = find_offset(lg, window, id);
    if at < 0 {
        return false;
    }
    let r = evlog.read_at(lg, at, window);
    if r.0 != 0 || record.ms_of(window, 0) != id {
        return false;
    }
    return erased_record(window);
}

// What goes before a window outcome that a replay could not place (`docs/design.md` section 42.3): the endpoint has passed over events that leave no record,
// and this event is more than a window above the cursor the log states, so where the cursor is goes first, in the same flush. Every record of a window outcome
// (`note_outcome`, `finish_attempt`) is preceded by this. Answers 1, or 0 if the log refused the record.
fn before_outcome[&g, &d](done: &!g log.Log, dv: &!d [int], kind: int, e: int, id: int) -> [file_write] int {
    if (kind == state.delivered() || kind == state.failed() || kind == state.dead()) && e >= 0 && e < state.max_endpoints() && dv[off_lag() + e] == 1 && id > dv[off_adv() + e] + state.span() {
        return note_advanced(done, dv, e);
    }
    return 1;
}

// Append `advanced(e, cursor)` (`docs/design.md` section 42), not yet flushed: from now on the log states the slot's cursor, and nothing has been passed over
// since. Answers 1 if it was appended, 0 if the log refused it.
fn note_advanced[&g, &d](done: &!g log.Log, dv: &!d [int], e: int) -> [file_write] int {
    return note_advanced_to(done, dv, e, dv[off_cur() + e]);
}

// `advanced(e, c)` for a cursor `c` the slot is about to have (`expire_through`), not yet flushed.
fn note_advanced_to[&g, &d](done: &!g log.Log, dv: &!d [int], e: int, c: int) -> [file_write] int {
    var ok = 0;
    region a {
        let rec = alloc_slice[a](128, byte_of(0));
        let total = state.put_outcome(rec, 0, dv[c_seq()], state.advanced(), e, c, 0, 0);
        if log.append(done, rec[0..total], dv[c_seq()], 0) == 0 {
            dv[c_seq()] = dv[c_seq()] + 1;
            dv[off_adv() + e] = c;
            dv[off_lag() + e] = 0;
            ok = 1;
        }
    }
    dv[off_ex() + ex_advanced()] = dv[off_ex() + ex_advanced()] + ok;
    return ok;
}

// Append the record that endpoint `e` was disabled or enabled (`state.disabled()`, `state.enabled()`) to `done`, not yet
// flushed. Answers 1 if it was appended, 0 if the log refused it.
fn note_endpoint[&g, &d](done: &!g log.Log, dv: &!d [int], kind: int, e: int) -> [file_write] int {
    return note_outcome(done, dv, kind, e, 0, 0, 0);
}

// Append the record that says why attempt number `tries` at (endpoint `e`, event `id`) failed, right after the record of its outcome (`docs/design.md`
// section 34.3). Nothing for a delivery. `replay` is 1 for a replay's attempt. Not flushed: it goes out with the outcome's flush.
fn note_reason[&g, &d](done: &!g log.Log, dv: &!d [int], e: int, id: int, tries: int, why: int, replay: int) -> [file_write] int {
    if why == 0 {
        return 0;
    }
    return note_outcome(done, dv, state.reason(), e, id, tries, why + replay * state.reason_replay());
}

fn off_table() -> [] int {
    return 16;
}

fn off_cur() -> [] int {
    return off_table() + endpoints.table_size();
}

fn off_sched() -> [] int {
    return off_cur() + state.max_endpoints();
}

fn off_flying() -> [] int {
    return off_sched() + 17;
}

// Per slot: the id of the endpoint that has it, `slot_free()` if none does, `slot_unseen()` if the log has never mentioned it.
fn off_slotid() -> [] int {
    return off_flying() + state.max_endpoints();
}

// Per slot, two integers: the id of the last event of the events log the endpoint has looked at, and the offset in the log where the record
// after it starts (`docs/design.md` section 31). Each endpoint reads the log forward from its own place.
fn off_scan() -> [] int {
    return off_slotid() + state.max_endpoints();
}

fn scan_id(e: int) -> [] int {
    return off_scan() + 2 * e;
}

fn scan_off(e: int) -> [] int {
    return off_scan() + 2 * e + 1;
}

// Per slot: when the endpoint's current run of failed attempts began (Unix ms), or 0 if it has none (the last attempt that ended was a delivery).
fn off_streak() -> [] int {
    return off_scan() + 2 * state.max_endpoints();
}

// Per slot: the flags (`f_disabled()` and the others above), one word each.
fn off_flags() -> [] int {
    return off_streak() + state.max_endpoints();
}

// Per slot: the Unix ms before which the loop has nothing to do for the endpoint (`docs/design.md` section 41.3), or `wake_never()`. 0 is "look now", which is every
// slot's value until a whole pass over its window has said otherwise, and what anything that gives the endpoint work sets it back to.
fn off_wake() -> [] int {
    return off_flags() + state.max_endpoints();
}

fn wake_never() -> [] int {
    return 1152921504606846976;
}

// Per slot: 1 if the cells of its window may hold something (an outcome was applied since the window was last cleared). A window that was never written is not
// read or written to clear it: the cells are one page in six for each slot, and an idle endpoint must not make them resident (`docs/design.md` section 41.2).
fn off_wused() -> [] int {
    return off_wake() + state.max_endpoints();
}

// The bearer tokens (the admin token first; each is its length, then its bytes: `authz.ls`), and the one change that may wait for the
// database (`manage.ls`).
// Per slot, two integers (`docs/design.md` section 42): the largest cursor the outcomes log states for the slot (`adv`), and 1 if an event has been passed over
// since (made final with no record, so that a replay of the log would leave the cursor behind the live one: `lag`).
fn off_adv() -> [] int {
    return off_wused() + state.max_endpoints();
}

fn off_lag() -> [] int {
    return off_adv() + state.max_endpoints();
}

// The database's host, when it is a name, resolved by the service (`dbname.ls`, `docs/design.md` section 45).
fn off_dbn() -> [] int {
    return off_lag() + state.max_endpoints();
}

// The audit log (`audit.ls`, `docs/design.md` section 47.1).
fn off_aud() -> [] int {
    return off_dbn() + dbname.size();
}

// The key that seals the bodies at rest (`bodies.ls`, `docs/design.md` section 47.4).
fn off_body() -> [] int {
    return off_aud() + audit.size();
}

fn off_token() -> [] int {
    return off_body() + bodies.size();
}

fn off_mg() -> [] int {
    return off_token() + authz.tokens_size();
}

fn off_offs() -> [] int {
    return off_mg() + manage.mg_size();
}

fn off_cells() -> [] int {
    return off_offs() + state.max_endpoints() * state.span();
}

// Where in the events log event `id` starts, for the endpoint in slot `e`: a ring of `span()` entries a slot, like the cells, so an entry is
// valid while the event is in the endpoint's window.
fn offs_at(e: int, id: int) -> [] int {
    return off_offs() + e * state.span() + id % state.span();
}

// A slot that has just been given to an endpoint, or freed: it is not disabled or paused, has no run of failures, and has looked at nothing
// of the events log (`seek_slots` or the caller says where it stands).
fn clear_slot[&d](dv: &!d [int], e: int) -> [] int {
    set_disabled(dv, e, false);
    set_paused(dv, e, false);
    take_tripped(dv, e);
    dv[off_wake() + e] = 0;
    dv[off_streak() + e] = 0;
    dv[scan_id(e)] = 0;
    dv[scan_off(e)] = 0 - 1;
    ops.set_last_reason(dv[off_ops()..off_ops() + ops.size()], e, 0);
    lim.clear_slot(lim_of_mut(dv), e);
    dead.clear(dead_of_mut(dv), e);
    return 0;
}

// Apply an outcome to the window of slot `e` (`state.apply`), noting that the window has been written.
fn apply_outcome[&d](dv: &!d [int], e: int, kind: int, id: int, attempts: int, next_at: int) -> [] int {
    if kind == state.failed() || kind == state.delivered() || kind == state.dead() {
        dv[off_wused() + e] = 1;
    }
    return state.apply(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, kind, id, attempts, next_at);
}

// Forget everything about slot `e`'s window and start its cursor at `start` (`state.reset`), reading the cells only if they may hold something. The log states
// that cursor (a `created` record, or a snapshot's), and nothing has been passed over since (`docs/design.md` section 42).
fn reset_window[&d](dv: &!d [int], e: int, start: int) -> [] int {
    if dv[off_wused() + e] != 0 {
        state.reset(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, start);
        dv[off_wused() + e] = 0;
    } else {
        dv[off_cur() + e] = start;
    }
    dv[off_adv() + e] = start;
    dv[off_lag() + e] = 0;
    return 0;
}

// The hard maximum age (`docs/design.md` section 47.2): the oldest segment, whose last event is `last`, is about to go though something still needs it. Every
// slot whose cursor is below `last` is moved to it, with an `advanced` record (what was not final is counted as expired); a waiting replay of an event up
// to `last` is ended (`replay_cancelled`; one already on the wire finishes). Not flushed: the drop's own flush takes the records before the segment goes.
// Answers 0, or -1 if the log refused a record.
fn expire_through[&g, &d](done: &!g log.Log, dv: &!d [int], last: int) -> [file_write] int {
    var e = 0;
    while e < state.max_endpoints() {
        let c = dv[off_cur() + e];
        if dv[off_slotid() + e] >= 0 && c < last {
            var finals = 0;
            var id = c + 1;
            while id <= last && id <= c + state.span() {
                if state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) {
                    finals = finals + 1;
                }
                id = id + 1;
            }
            if note_advanced_to(done, dv, e, last) == 0 {
                return 0 - 1;
            }
            advance_window(dv, e, last);
            dv[rt_at() + r_expired()] = dv[rt_at() + r_expired()] + last - c - finals;
        }
        e = e + 1;
    }
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 6] == 0 && dv[b + 2] <= last {
            if note_outcome(done, dv, state.replay_cancelled(), dv[b + 1], dv[b + 2], 0, 0) == 0 {
                return 0 - 1;
            }
            dv[b] = 0;
        }
        r = r + 1;
    }
    dv[rt_at() + r_expired_segments()] = dv[rt_at() + r_expired_segments()] + 1;
    return 0;
}

// An `advanced` record replayed (`state.advance`): the cursor of `e` moves up to `to`, reading the cells only if they may hold something.
fn advance_window[&d](dv: &!d [int], e: int, to: int) -> [] int {
    if dv[off_wused() + e] != 0 {
        state.advance(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, to);
    } else if to > dv[off_cur() + e] {
        dv[off_cur() + e] = to;
    }
    return 0;
}

// After recovery, or after a snapshot has become the outcomes log: what the log states of every cursor is where the cursor is, and nothing has been passed over
// since (`docs/design.md` section 42.3).
fn cursors_stated[&d](dv: &!d [int]) -> [] int {
    var e = 0;
    while e < state.max_endpoints() {
        dv[off_adv() + e] = dv[off_cur() + e];
        dv[off_lag() + e] = 0;
        e = e + 1;
    }
    return 0;
}

// A clean stop (`docs/design.md` section 42.3): an `advanced` record for every slot that has passed over events since the log last stated its cursor, so that the
// start after it re-walks nothing. Not flushed: the stop's own flush takes them. Answers how many were written.
fn state_cursors[&g, &d](done: &!g log.Log, dv: &!d [int]) -> [file_write] int {
    var n = 0;
    var e = 0;
    while e < state.max_endpoints() {
        if dv[off_lag() + e] == 1 && dv[off_cur() + e] > dv[off_adv() + e] && dv[off_slotid() + e] >= 0 {
            n = n + note_advanced(done, dv, e);
        }
        e = e + 1;
    }
    return n;
}

fn slot_free() -> [] int {
    return 0 - 1;
}

fn slot_unseen() -> [] int {
    return 0 - 2;
}

// One flag per cell: is an attempt at this (endpoint, event) in flight? (An event with one is not started again.)
fn off_flight() -> [] int {
    return off_cells() + state.cells(state.max_endpoints());
}

// The replays asked for and not finished (`docs/design.md` section 23): `rp_cap()` entries of `rp_stride()` integers, in the order
// state, endpoint, event, attempts, next attempt, offset of the event in the events log (-1 until looked up), in flight.
fn off_rp() -> [] int {
    return off_flight() + state.max_endpoints() * state.span();
}

fn rp_cap() -> [] int {
    return 32;
}

fn rp_stride() -> [] int {
    return 7;
}

// The history ring and its counters (`history.ls`).
fn off_hq() -> [] int {
    return off_rp() + rp_cap() * rp_stride();
}

// The counters of `ops.ls` (`docs/design.md` section 34): ingest, group commits, why attempts failed, the stop.
fn off_ops() -> [] int {
    return off_hq() + history.size();
}

// The extras of the endpoints (`epx.ls`, `docs/design.md` section 35): a row for each entry of the table, and the change that waits.
fn off_xt() -> [] int {
    return off_ops() + ops.size();
}

fn off_xg() -> [] int {
    return off_xt() + epx.xt_size();
}

fn off_ex() -> [] int {
    return off_xg() + epx.xg_size();
}

// How many (endpoint, event) pairs a subscription has passed over since the start.
fn ex_filtered() -> [] int {
    return 0;
}

// `rotation-grace-ms`: how long a previous secret is kept when a change asks for the default.
fn ex_grace() -> [] int {
    return 1;
}

// How many turns of delivery there have been since the start, and how many times an endpoint was looked at in them (`start_attempts`: the others were skipped as quiet,
// `docs/design.md` section 41.3). `/stats` says both.
fn ex_turns() -> [] int {
    return 2;
}

fn ex_looked() -> [] int {
    return 3;
}

// 1 if the last turn stopped because it had passed over `most_skips()` events that endpoints do not subscribe to, with more to pass: the loop's next wait is 0, not
// up to 50 ms (without it a backlog of events nobody wants drains at `most_skips()` every 50 ms: 82,000 pairs a second, which at 1,024 endpoints is 80 events a second).
fn ex_again() -> [] int {
    return 4;
}

// How many times that wait was left out (`/stats` says so: a backlog being drained is seen, and not only timed).
fn ex_hurried() -> [] int {
    return 5;
}

// How many events have been erased since the start (`docs/design.md` section 47.3).
fn ex_erased() -> [] int {
    return 7;
}

// How many `advanced` records the service has written since the start (`docs/design.md` section 42; `/stats` says so).
fn ex_advanced() -> [] int {
    return 6;
}

// The type of the event being accepted, a byte to an integer: room for `filter.max_type()` after the counters.
fn ex_type() -> [] int {
    return 8;
}

// The limits on the pace of attempts and the spread of retries (`lim.ls`, section 39), and the dead letters of each endpoint (`dead.ls`, section 39.1): the last
// regions of the state, after retention's block (`compact.ls`).
fn off_lim() -> [] int {
    return rt_at() + rt_size();
}

fn off_dead() -> [] int {
    return off_lim() + lim.size();
}

fn dv_size() -> [] int {
    return off_dead() + dead.size();
}

fn lim_of[&d](dv: &d [int]) -> [] &d [int] {
    return dv[off_lim()..off_lim() + lim.size()];
}

fn lim_of_mut[&d](dv: &!d [int]) -> [] &!d [int] {
    return dv[off_lim()..off_lim() + lim.size()];
}

fn dead_of[&d](dv: &d [int]) -> [] &d [int] {
    return dv[off_dead()..off_dead() + dead.size()];
}

fn dead_of_mut[&d](dv: &!d [int]) -> [] &!d [int] {
    return dv[off_dead()..off_dead() + dead.size()];
}

// A replay attempt's id for the attempt machinery: the event's id plus this, so `finish_attempt` can tell it from a window's.
fn replay_base() -> [] int {
    return 1099511627776;
}

// The entry for (endpoint, event), or -1.
fn rp_find[&d](dv: &d [int], e: int, id: int) -> [] int {
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 1] == e && dv[b + 2] == id {
            return r;
        }
        r = r + 1;
    }
    return 0 - 1;
}

fn rp_free[&d](dv: &d [int]) -> [] int {
    var n = 0;
    var r = 0;
    while r < rp_cap() {
        if dv[off_rp() + r * rp_stride()] == 0 {
            n = n + 1;
        }
        r = r + 1;
    }
    return n;
}

// Ask for (endpoint, event) to be sent again: take the entry for it, or a free one, and reset it. Answers the entry, or -1 if
// there is none free.
fn rp_put[&d](dv: &!d [int], e: int, id: int, offset: int) -> [] int {
    var r = rp_find(dv, e, id);
    if r < 0 {
        var k = 0;
        while k < rp_cap() && r < 0 {
            if dv[off_rp() + k * rp_stride()] == 0 {
                r = k;
            }
            k = k + 1;
        }
    }
    if r < 0 {
        return 0 - 1;
    }
    let b = off_rp() + r * rp_stride();
    dv[b] = 1;
    dv[b + 1] = e;
    dv[b + 2] = id;
    dv[b + 3] = 0;
    dv[b + 4] = 0;
    dv[b + 5] = offset;
    dv[b + 6] = 0;
    return r;
}

fn flight_at(e: int, id: int) -> [] int {
    return off_flight() + e * state.span() + id % state.span();
}

// The most attempts one turn starts, so a long backlog does not starve the requests behind it.
fn most_starts() -> [] int {
    return 16;
}

// The requests for the history that may wait for the database at once, and how long one waits (ms) before it is a 504.
fn pq_cap() -> [] int {
    return 64;
}

fn query_wait_ms() -> [] int {
    return 5000;
}

// How long an attempt may take in all (connect, send, and the wait for the status line) unless the fourth argument says.
fn default_deadline_ms() -> [] int {
    return 2000;
}

// The Standard Webhooks schedule (https://www.standardwebhooks.com): after the first attempt, retries after 5 s, 5 min, 30 min,
// 2 h, 5 h, 10 h, 14 h, 20 h and 24 h. An event is dead-lettered when the last of them has failed.
fn default_schedule[&s](sched: &!s [int]) -> [] int {
    sched[0] = 9;
    sched[1] = 5000;
    sched[2] = 300000;
    sched[3] = 1800000;
    sched[4] = 7200000;
    sched[5] = 18000000;
    sched[6] = 36000000;
    sched[7] = 50400000;
    sched[8] = 72000000;
    sched[9] = 86400000;
    return 0;
}

// `a,b,c` in milliseconds into `sched`; answers how many, or -1 if the text is not that (or has more than 16).
fn parse_schedule[&t, &s](text: &t [byte], sched: &!s [int]) -> [] int {
    var count = 0;
    var n = 0;
    var digits = 0;
    var i = 0;
    while i <= len(text) {
        let c = 0 - 1;
        var ch = c;
        if i < len(text) {
            ch = int_of(text[i]);
        }
        if ch >= 48 && ch <= 57 && digits < 12 {
            n = n * 10 + (ch - 48);
            digits = digits + 1;
        } else if (ch == ',' || i == len(text)) && digits > 0 && count < 16 {
            sched[1 + count] = n;
            count = count + 1;
            n = 0;
            digits = 0;
        } else {
            return 0 - 1;
        }
        i = i + 1;
    }
    sched[0] = count;
    return count;
}

// The smallest cursor over the configured endpoints: events at or below it are final for all of them. It decides where an endpoint
// that the log has never seen starts (`place_new`); it bounds nothing: each endpoint reads the events log from its own cursor
// (`docs/design.md` section 31, which replaced the scan this once limited).
fn slowest_cursor[&d](dv: &d [int]) -> [] int {
    var low = 0 - 1;
    var i = 0;
    while i < dv[c_endpoints()] {
        let e = dv[off_table() + i * endpoints.stride()];
        if e >= 0 {
            let c = dv[off_cur() + e];
            if low < 0 || c < low {
                low = c;
            }
        }
        i = i + 1;
    }
    if low < 0 {
        return 0;
    }
    return low;
}

// The index in the table of the endpoint in slot `e`, or -1.
fn index_of[&d](dv: &d [int], e: int) -> [] int {
    var i = 0;
    while i < dv[c_endpoints()] {
        if dv[off_table() + i * endpoints.stride()] == e {
            return i;
        }
        i = i + 1;
    }
    return 0 - 1;
}

// The index in the table of the endpoint with id `ident` (what the API and the history call it), or -1.
fn index_of_id[&d](dv: &d [int], ident: int) -> [] int {
    var i = 0;
    while i < dv[c_endpoints()] {
        if dv[off_table() + i * endpoints.stride() + 6] == ident {
            return i;
        }
        i = i + 1;
    }
    return 0 - 1;
}

// The id of the endpoint that has slot `e`, or -1 if none does. It is read from the slot map and not from the table: an endpoint that was deleted
// while an attempt of it was on the wire is no longer in the table, and that attempt's outcome is still recorded under its id (section 25.5).
fn ident_of_slot[&d](dv: &d [int], e: int) -> [] int {
    if dv[off_slotid() + e] < 0 {
        return 0 - 1;
    }
    return dv[off_slotid() + e];
}

// Append the record that endpoint `ident` was given slot `e` with its cursor at `start`, or that the slot was freed, to `done`,
// not yet flushed. Answers 1 if it was appended, 0 if the log refused it.
fn note_created[&g, &d](done: &!g log.Log, dv: &!d [int], e: int, ident: int, start: int) -> [file_write] int {
    if e >= state.first_wide() && dv[c_wide()] == 0 {
        // A slot that a build from before 1,024 endpoints would ignore: the record that makes it refuse the log goes first (`docs/design.md` section 41.5). The caller flushes.
        if note_outcome(done, dv, state.wide(), 0, 0, 0, 0) == 0 {
            return 0;
        }
        dv[c_wide()] = 1;
    }
    return note_outcome(done, dv, state.created(), e, ident, start, 0);
}

fn note_removed[&g, &d](done: &!g log.Log, dv: &!d [int], e: int) -> [file_write] int {
    return note_outcome(done, dv, state.removed(), e, 0, 0, 0);
}

// Pass one over the outcome log (`docs/design.md` section 25): which endpoint has each slot, and the largest sequence number. A
// log written before slots had records mentions an endpoint only by the number of its outcomes, and that number was its id, so a
// slot first met in an outcome belongs to the endpoint with that id. An outcome for a slot that has been freed is stale and does
// not give it back. Records that are not outcomes are counted by the second pass, which refuses the log.
fn scan_slots[&l, &w, &d](done: &!l log.Log, window: &!w [byte], dv: &!d [int]) -> [file_read] int {
    var k = 0;
    while k < state.max_endpoints() {
        dv[off_slotid() + k] = slot_unseen();
        k = k + 1;
    }
    var at = 0;
    var going = true;
    while going {
        let r = log.read_at(done, at, window);
        if r.0 != 0 {
            going = false;
        } else {
            let o = state.outcome_at(window, 0);
            // The header and the marker are about the log, not about a slot: the header's endpoint field is 62, which is a slot (`state.format_slot()`).
            if o.0 != 0 && o.0 != state.format() && o.0 != state.wide() && o.1 >= 0 && o.1 < state.max_endpoints() {
                if o.0 == state.created() {
                    dv[off_slotid() + o.1] = o.2;
                } else if o.0 == state.removed() {
                    dv[off_slotid() + o.1] = slot_free();
                } else if dv[off_slotid() + o.1] == slot_unseen() {
                    dv[off_slotid() + o.1] = o.1;
                }
            }
            if record.ms_of(window, 0) >= dv[c_seq()] {
                dv[c_seq()] = record.ms_of(window, 0) + 1;
            }
            at = at + r.1;
        }
    }
    return 0;
}

// A slot for the endpoint `ident`, which has none: the slot with its own number if that is free, else the lowest that is, else the lowest
// dormant one (an endpoint in the log and not in the table), freed with a `removed` record. Answers the slot, or -1 if the log refused the
// record. The caller writes the `created` record, for it knows the cursor.
fn take_slot[&g, &d](done: &!g log.Log, dv: &!d [int], ident: int) -> [file_write] int {
    let most = state.max_endpoints();
    var slot = 0 - 1;
    if ident < most && dv[off_slotid() + ident] < 0 {
        slot = ident;
    }
    var k = 0;
    while k < most && slot < 0 {
        if dv[off_slotid() + k] < 0 {
            slot = k;
        }
        k = k + 1;
    }
    k = 0;
    while k < most && slot < 0 {
        if dv[off_slotid() + k] >= 0 && index_of_id(dv, dv[off_slotid() + k]) < 0 && !is_draining(dv, k) {
            if note_removed(done, dv, k) == 0 {
                return 0 - 1;
            }
            dv[off_slotid() + k] = slot_free();
            slot = k;
        }
        k = k + 1;
    }
    return slot;
}

// Give each endpoint of the table the slot the log says it has; one the log does not know keeps -1 until `place_new`. An endpoint that is in
// the log and not in the table is dormant: its slot stays its own and its state is rebuilt if it comes back, until a new endpoint needs
// the slot (`take_slot`).
fn match_slots[&d](dv: &!d [int]) -> [] int {
    let n = dv[c_endpoints()];
    let most = state.max_endpoints();
    var i = 0;
    while i < n {
        let ident = dv[off_table() + i * endpoints.stride() + 6];
        var found = 0 - 1;
        var k = 0;
        while k < most {
            if dv[off_slotid() + k] == ident && found < 0 {
                found = k;
            }
            k = k + 1;
        }
        dv[off_table() + i * endpoints.stride()] = found;
        i = i + 1;
    }
    return 0;
}

// Give a slot, a `created` record (flushed) and a cursor to each endpoint of the table that has none: a row added by hand, or a line added
// to `endpoints.conf`. It starts at the cursor of the slowest endpoint the log knows (`slowest_cursor`; 0 if there is none). That rule was
// made so that the newcomer could not widen the one window the scan of the events log was shared through (`docs/design.md` sections 25.1 and
// 25.3); section 31 gave every endpoint its own scan, so it is no longer needed for that, and it is kept because it is what a person was told
// and what the tests pin: a new row is sent the backlog of the slowest endpoint, not the whole log. Called after `replay`, so the cursors are
// known. Answers 0, or 1 if the log refused a record.
fn place_new[&g, &d](done: &!g log.Log, dv: &!d [int]) -> [file_write] int {
    let n = dv[c_endpoints()];
    let start = slowest_cursor(dv);
    var wrote = 0;
    var i = 0;
    while i < n {
        if dv[off_table() + i * endpoints.stride()] < 0 {
            let ident = dv[off_table() + i * endpoints.stride() + 6];
            let slot = take_slot(done, dv, ident);
            if slot < 0 || note_created(done, dv, slot, ident, start) == 0 {
                return 1;
            }
            reset_window(dv, slot, start);
            clear_slot(dv, slot);
            dv[off_flying() + slot] = 0;
            dv[off_slotid() + slot] = ident;
            dv[off_table() + i * endpoints.stride()] = slot;
            wrote = wrote + 1;
        }
        i = i + 1;
    }
    if wrote > 0 && log.flush(done) != 0 {
        return 1;
    }
    return 0;
}

// What one record of the delivery log does to the table of dead letters (`dead.ls`): a dead letter (`dead`, or a replay's `replay_dead`) enters it, with the
// time it died (the record's fifth field) and no reason yet; a delivered replay takes its event out; the reason a dead letter died of is in the record behind
// its outcome, and is taken only while the entry has none (a later failure of a replay is not it). Recovery and `dead_fold` both go through here.
fn dead_note[&d](dv: &!d [int], kind: int, e: int, id: int, tries: int, fifth: int) -> [] int {
    if kind == state.dead() || kind == state.replay_dead() {
        dead.put(dead_of_mut(dv), e, id, tries, 0, fifth, 0 - 1);
    } else if kind == state.replay_delivered() {
        dead.remove(dead_of_mut(dv), e, id);
    } else if kind == state.dead_entry() {
        // a snapshot's: the entry (`tries` is attempts * 65536 + reason + 1), or, with 0, the floor it was written with
        if tries > 0 {
            dead.put(dead_of_mut(dv), e, id, (tries - 1) / 65536, (tries - 1) % 65536, fifth, 0 - 1);
        } else {
            dead.raise_floor(dead_of_mut(dv), e, id);
        }
    } else if kind == state.reason() {
        let k = dead.find(dead_of(dv), e, id);
        if k >= 0 && dead.reason_at(dead_of(dv), e, k) == 0 {
            dead.set_reason(dead_of_mut(dv), e, id, tries, fifth % state.reason_replay());
        }
    }
    return 0;
}

// One pass over the delivery log for the dead letters of slot `e` with an event id below `below` (0: every one), put into the endpoint's table as recovery
// would. A `created` or a `removed` record is the slot passing to another endpoint: what the table holds below `below` from before is not this one's.
fn dead_fold[&l, &w, &d](done: &!l log.Log, window: &!w [byte], dv: &!d [int], e: int, below: int) -> [file_read] int {
    var at = 0;
    var going = true;
    while going {
        let r = log.read_at(done, at, window);
        if r.0 != 0 {
            going = false;
        } else {
            let o = state.outcome_at(window, 0);
            if o.0 != 0 && o.1 == e {
                if o.0 == state.created() || o.0 == state.removed() {
                    if below == 0 {
                        dead.clear(dead_of_mut(dv), e);
                    } else {
                        dead.clear_below(dead_of_mut(dv), e, below);
                    }
                } else if below == 0 || o.2 < below {
                    dead_note(dv, o.0, o.1, o.2, o.3, o.4);
                }
            }
            at = at + r.1;
        }
    }
    return 0;
}

// Complete slot `e`'s table from the log while it is short of dead letters it could hold (some were left out, and it has room now): one fold for the events
// below the floor, as many times as it takes (each lowers the floor), at most 64. Answers the passes made; 0 for a table that is as complete as it can be.
fn dead_complete[&l, &w, &d](done: &!l log.Log, window: &!w [byte], dv: &!d [int], e: int) -> [file_read] int {
    var rounds = 0;
    while dead.wants_refold(dead_of(dv), e) && rounds < 64 {
        // the events at or below the floor: a fold takes the ids below its argument
        let below = dead.floor(dead_of(dv), e) + 1;
        let before = dead.count(dead_of(dv), e);
        dead.reset_floor(dead_of_mut(dv), e);
        dead_fold(done, window, dv, e, below);
        rounds = rounds + 1;
        if dead.count(dead_of(dv), e) == before {
            // the log has nothing more below the floor (after a snapshot it never does): not asked again until another is left out
            dead.settle(dead_of_mut(dv), e);
        }
    }
    return rounds;
}

// Apply one replay record to the table of replays (`docs/design.md` section 23): asked for, failed (with its count and the time of
// the next attempt), or ended.
fn recover_replay[&d](dv: &!d [int], kind: int, e: int, id: int, tries: int, next_at: int) -> [] int {
    if kind == state.replay() {
        rp_put(dv, e, id, 0 - 1);
        return 0;
    }
    let r = rp_find(dv, e, id);
    if r < 0 {
        return 0;
    }
    let b = off_rp() + r * rp_stride();
    if kind == state.replay_failed() {
        dv[b + 3] = tries;
        dv[b + 4] = next_at;
    } else {
        dv[b] = 0;
    }
    return 0;
}

// Read every outcome in `done` and apply it: this is how the cursors, the attempts and the times of the next attempts survive a
// restart. (The records of a slot that has an endpoint in the log but not in the table, a dormant one, are applied to its place too, so that a snapshot of the
// state keeps what it has; its waiting replays are the exception, as they always were: they wait for the endpoint's row.) Answers 0, or the number of records that were not outcomes (a log from something else), in which case the caller
// refuses to start.
fn replay[&l, &w, &d](done: &!l log.Log, window: &!w [byte], dv: &!d [int]) -> [file_read] int {
    var at = 0;
    var odd = 0;
    var going = true;
    while going {
        let r = log.read_at(done, at, window);
        if r.0 != 0 {
            going = false;
        } else {
            let o = state.outcome_at(window, 0);
            if o.0 == 0 {
                odd = odd + 1;
            } else if o.0 == state.format() || o.0 == state.wide() {
                // about the log and not a slot (`state.format_slot()` is 62, a slot since 1,024 endpoints): nothing to apply
                if o.0 == state.wide() {
                    dv[c_wide()] = 1;
                }
            } else if o.0 >= state.replay() && o.0 <= state.replay_dead() || o.0 == state.replay_cancelled() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && index_of(dv, o.1) >= 0 {
                    recover_replay(dv, o.0, o.1, o.2, o.3, o.4);
                    if o.0 == state.replay_delivered() {
                        dv[off_streak() + o.1] = 0;
                        ops.set_last_reason(dv[off_ops()..off_ops() + ops.size()], o.1, 0);
                    }
                    // a replay that was delivered is no longer a dead letter, and one that died is one again (one of a deleted endpoint is skipped above)
                    dead_note(dv, o.0, o.1, o.2, o.3, o.4);
                }
            } else if o.0 == state.created() || o.0 == state.removed() {
                if o.1 >= 0 && o.1 < state.max_endpoints() {
                    var start = 0;
                    if o.0 == state.created() {
                        start = o.3;
                    }
                    reset_window(dv, o.1, start);
                    clear_slot(dv, o.1);
                    var r2 = 0;
                    while r2 < rp_cap() {
                        if dv[off_rp() + r2 * rp_stride() + 1] == o.1 {
                            dv[off_rp() + r2 * rp_stride()] = 0;
                        }
                        r2 = r2 + 1;
                    }
                }
            } else if o.0 == state.disabled() || o.0 == state.enabled() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                    set_disabled(dv, o.1, o.0 == state.disabled());
                    // Whoever disabled or enabled it, the breaker is not the reason now; and an endpoint a person enabled starts a new run of failures.
                    set_paused(dv, o.1, false);
                    if o.0 == state.enabled() {
                        dv[off_streak() + o.1] = 0;
                    }
                }
            } else if o.0 == state.streak() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                    dv[off_streak() + o.1] = o.4;
                }
            } else if o.0 == state.paused() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                    set_disabled(dv, o.1, true);
                    set_paused(dv, o.1, true);
                }
            } else if o.0 == state.advanced() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                    advance_window(dv, o.1, o.2);
                }
            } else if o.0 == state.erased() {
                // about an event, not a slot (section 47.3): final in every window that holds it, its dead letters gone
                erase_cells(dv, o.2);
            } else if o.0 == state.reason() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                    ops.set_last_reason(dv[off_ops()..off_ops() + ops.size()], o.1, o.4 % state.reason_replay());
                    dead_note(dv, o.0, o.1, o.2, o.3, o.4);
                }
            } else if o.1 >= 0 && o.1 < state.max_endpoints() && slot_known(dv, o.1) {
                if o.0 == state.delivered() {
                    dv[off_streak() + o.1] = 0;
                    ops.set_last_reason(dv[off_ops()..off_ops() + ops.size()], o.1, 0);
                }
                dead_note(dv, o.0, o.1, o.2, o.3, o.4);
                apply_outcome(dv, o.1, o.0, o.2, o.3, o.4);
            }
            if record.ms_of(window, 0) >= dv[c_seq()] {
                dv[c_seq()] = record.ms_of(window, 0) + 1;
            }
            at = at + r.1;
        }
    }
    return odd;
}

// The circuit breaker (`docs/design.md` section 31). An attempt of endpoint `e` that was recorded ended in a delivery or not. A delivery ends the
// endpoint's run of failures. A failure (anything but a `2xx`; a `410` is the endpoint saying it is gone and disables it on its own) either
// begins a run, which is written to the log with its time so that a restart does not forget when it began, or, if one is under way and has
// lasted `breaker-days` days, pauses the endpoint: the same disabled bit a `410` sets, plus the paused bit that says it was the breaker, and a
// record. Its events wait where they are and are sent when a person enables it (`POST /endpoints/:id/enable`), which also ends the run.
fn track_health[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, delivered: bool, code: int) -> [file_write, clock] int {
    if delivered {
        dv[off_streak() + e] = 0;
        return 0;
    }
    if code == 410 || is_draining(dv, e) {
        return 0;
    }
    let now = clock_unix_ms(clock);
    if dv[off_streak() + e] == 0 {
        if note_outcome(done, dv, state.streak(), e, 0, 0, now) == 1 {
            dv[off_streak() + e] = now;
        }
        return 0;
    }
    if !is_disabled(dv, e) && state.breaker_trips(dv[c_breaker()], dv[off_streak() + e], now) {
        if note_endpoint(done, dv, state.paused(), e) == 1 {
            set_disabled(dv, e, true);
            set_paused(dv, e, true);
            set_tripped(dv, e);
            dv[c_trips()] = dv[c_trips()] + 1;
        }
    }
    return 0;
}

// An attempt at a replay ended (`docs/design.md` section 23): the same rules as a window's attempt (a `2xx` delivers, a `410` kills
// the event and disables the endpoint, the schedule running out kills it, anything else waits for the next delay), recorded
// under the kinds of a replay. Answers 1 if an outcome record was appended.
fn finish_replay[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, id: int, code: int, latency: int) -> [file_write, clock] int {
    dv[c_attempts()] = dv[c_attempts()] + 1;
    dv[off_wake() + e] = 0;
    if dv[off_flying() + e] > 0 {
        dv[off_flying() + e] = dv[off_flying() + e] - 1;
    }
    let r = rp_find(dv, e, id);
    if r < 0 {
        return 0;
    }
    let b = off_rp() + r * rp_stride();
    dv[b + 6] = 0;
    let tries = dv[b + 3] + 1;
    var kind = state.replay_failed();
    var next_at = 0;
    if code >= 200 && code < 300 {
        kind = state.replay_delivered();
    } else if code == 410 || tries >= dv[off_sched()] + 1 {
        kind = state.replay_dead();
        next_at = clock_unix_ms(clock);
    } else {
        next_at = clock_unix_ms(clock) + jitter.delay(dv[off_sched() + tries], lim.jitter_percent(lim_of(dv)), ident_of_slot(dv, e), id, tries);
    }
    var ok = 0;
    if note_outcome(done, dv, kind, e, id, tries, next_at) == 1 {
        ok = 1;
        // Why it failed goes right behind the record of the outcome, before any record the outcome causes (a replay of a deleted endpoint is ended below).
        note_reason(done, dv, e, id, tries, reason.of(code), 1);
        if kind == state.replay_failed() {
            dv[b + 3] = tries;
            dv[b + 4] = next_at;
            dv[c_failed()] = dv[c_failed()] + 1;
            if is_draining(dv, e) {
                // The endpoint was deleted while this replay was on the wire: its outcome is recorded above, and it is not tried again.
                if note_outcome(done, dv, state.replay_dead(), e, id, tries, 0) == 1 {
                    dv[b] = 0;
                }
            }
        } else {
            // A replay ends: delivered, it is no longer a dead letter; dead, it is one again, with this death's attempts, reason and time.
            if kind == state.replay_delivered() {
                dead.remove(dead_of_mut(dv), e, id);
            } else {
                dead.put(dead_of_mut(dv), e, id, tries, reason.of(code), next_at, dv[b + 5]);
            }
            dv[b] = 0;
            if kind == state.replay_delivered() {
                dv[c_delivered()] = dv[c_delivered()] + 1;
            } else {
                dv[c_dead()] = dv[c_dead()] + 1;
            }
        }
    }
    if ok == 1 {
        var outcome = state.failed();
        if kind == state.replay_delivered() {
            outcome = state.delivered();
        } else if kind == state.replay_dead() {
            outcome = state.dead();
        }
        let why = reason.of(code);
        history.push(dv[off_hq()..off_hq() + history.size()], ident_of_slot(dv, e), id, 1, tries, outcome, reason.legacy_status(code), clock_unix_ms(clock), latency, why);
        ops.attempt_ended(dv[off_ops()..off_ops() + ops.size()], e, why);
        track_health(done, dv, clock, e, kind == state.replay_delivered(), code);
    }
    if ok == 1 && code == 410 && !is_disabled(dv, e) && !is_draining(dv, e) {
        if note_endpoint(done, dv, state.disabled(), e) == 1 {
            set_disabled(dv, e, true);
        }
    }
    return ok;
}

// An attempt ended: `code` is what `attempt` answered (an HTTP status, or a negative reason). Count it, write its outcome to
// `done` (not yet flushed), apply it to the cells, and free the event to be tried again when its time comes. Answers 1 if an
// outcome record was appended, 0 if the log refused it (then the cells are left alone and a restart repeats the attempt).
fn finish_attempt[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, id: int, code: int, latency: int) -> [file_write, clock] int {
    if id >= replay_base() {
        return finish_replay(done, dv, clock, e, id - replay_base(), code, latency);
    }
    dv[c_attempts()] = dv[c_attempts()] + 1;
    dv[off_wake() + e] = 0;
    dv[flight_at(e, id)] = 0;
    if dv[off_flying() + e] > 0 {
        dv[off_flying() + e] = dv[off_flying() + e] - 1;
    }
    let tries = state.attempts(dv[off_cells()..off_flight()], e, id) + 1;
    var kind = state.failed();
    var next_at = 0;
    if code >= 200 && code < 300 {
        kind = state.delivered();
    } else if code == 410 || tries >= dv[off_sched()] + 1 {
        // A `410 Gone` is the receiver saying the endpoint no longer exists: this event is a dead letter at once, and the
        // endpoint stops being tried (below).
        kind = state.dead();
        // The fifth field of a dead letter's record is when it died (Unix ms): the list of dead letters (section 39.1) says so.
        next_at = clock_unix_ms(clock);
    } else {
        next_at = clock_unix_ms(clock) + jitter.delay(dv[off_sched() + tries], lim.jitter_percent(lim_of(dv)), ident_of_slot(dv, e), id, tries);
    }
    let placed = before_outcome(done, dv, kind, e, id);
    var ok = 0;
    region a {
        let rec = alloc_slice[a](128, byte_of(0));
        let total = state.put_outcome(rec, 0, dv[c_seq()], kind, e, id, tries, next_at);
        if placed == 1 && log.append(done, rec[0..total], dv[c_seq()], 0) == 0 {
            dv[c_seq()] = dv[c_seq()] + 1;
            apply_outcome(dv, e, kind, id, tries, next_at);
            if kind == state.delivered() {
                dv[c_delivered()] = dv[c_delivered()] + 1;
            } else if kind == state.dead() {
                dv[c_dead()] = dv[c_dead()] + 1;
            } else {
                dv[c_failed()] = dv[c_failed()] + 1;
            }
            ok = 1;
        }
    }
    if ok == 1 {
        let why = reason.of(code);
        history.push(dv[off_hq()..off_hq() + history.size()], ident_of_slot(dv, e), id, 0, tries, kind, reason.legacy_status(code), clock_unix_ms(clock), latency, why);
        note_reason(done, dv, e, id, tries, why, 0);
        if kind == state.dead() {
            dead.put(dead_of_mut(dv), e, id, tries, why, next_at, dv[offs_at(e, id)]);
        }
        ops.attempt_ended(dv[off_ops()..off_ops() + ops.size()], e, why);
        track_health(done, dv, clock, e, kind == state.delivered(), code);
    }
    if ok == 1 && code == 410 && !is_disabled(dv, e) && !is_draining(dv, e) {
        if note_endpoint(done, dv, state.disabled(), e) == 1 {
            set_disabled(dv, e, true);
        }
    }
    return ok;
}

// The poller woke the attempt in `slot`: move it along, and if it ended, free its slot and record how. Answers 1 if an outcome
// was written. An attempt whose name has just been resolved is not ended: `delivery_turn` redials it.
fn settle[&f, &g, &d, &k, &t, &p, &a, &r, &s](ffi: &f Ffi("libcrypto,libssl"), done: &!g log.Log, dv: &!d [int], clock: &k Clock, atab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], slot: int, token0: int) -> [ffi("libcrypto"), ffi("libssl"), file_write, clock, conn_read, conn_write, poll] int {
    let code = attempt.advance(ffi, atab, poller, at, req, resp, slot, token0);
    if code == attempt.pending() || code == attempt.resolved() {
        return 0;
    }
    return conclude(ffi, done, dv, clock, atab, at, slot, code);
}

// The attempt in `slot` ended with `code`: free its slot and record how. Answers 1 if an outcome was written.
fn conclude[&f, &g, &d, &k, &t, &a](ffi: &f Ffi("libssl"), done: &!g log.Log, dv: &!d [int], clock: &k Clock, atab: &!t conns.Table, at: &!a [int], slot: int, code: int) -> [ffi("libssl"), file_write, clock] int {
    let e = attempt.endpoint_of(at, slot);
    let id = attempt.event_of(at, slot);
    let latency = clock_ms(clock) - (attempt.deadline_of(at, slot) - dv[c_deadline()]);
    attempt.finish(ffi, atab, at, slot);
    return finish_attempt(done, dv, clock, e, id, code, latency);
}

// End every attempt that has run past its deadline, as a timeout. Answers how many outcomes were written.
fn sweep[&f, &g, &d, &k, &t, &a](ffi: &f Ffi("libssl"), done: &!g log.Log, dv: &!d [int], clock: &k Clock, atab: &!t conns.Table, at: &!a [int]) -> [ffi("libssl"), file_write, clock] int {
    let now = clock_ms(clock);
    var written = 0;
    var slot = 0;
    while slot < attempt.slots() {
        if attempt.expired(at, slot, now) {
            written = written + conclude(ffi, done, dv, clock, atab, at, slot, attempt.timeout_of(at, slot));
        }
        slot = slot + 1;
    }
    return written;
}

// Start an attempt at event `id` for the endpoint in table slot `i`. `loaded` says that the scan has just read the event into `window`
// (a first attempt: the record is read once, not twice); otherwise it is read from where the endpoint's ring says it starts (a retry).
// Answers the table and 1 if an outcome was written at once (the connection failed before it began), else 0.
fn start_one[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], atab: conns.Table, i: int, id: int, loaded: bool, token0: int) -> [heap, fs_read(""), file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
    let e = endpoints.slot_of(dv[off_table()..off_table() + endpoints.table_size()], i);
    if !loaded {
        let r0 = evlog.read_at(lg, dv[offs_at(e, id)], window);
        if r0.0 != 0 || record.ms_of(window, 0) != id {
            return (atab, 0);
        }
    }
    let p = record.pair_at(window, record.first_pair(0));
    if evlog.is_erased(window[p.2..p.2 + p.3]) {
        // Erased while it waited for a retry (`docs/design.md` section 47.3): final here, never sent.
        apply_outcome(dv, e, state.delivered(), id, 0, 0);
        dv[off_lag() + e] = 1;
        return (atab, 0);
    }
    // A sealed body is opened for the ask (`bodies.ls`, section 47.4), in a room left by falling out of it; one that does not open is not sent.
    var ask = buffer.empty(heap, 0);
    var opened = true;
    let fp = bodies.sealed_by(window, 0);
    if fp < 0 {
        buffer.drop(heap, ask);
        ask = wire.request(heap, id, window[p.2..p.2 + p.3], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), dv[off_xt()..off_xt() + epx.xt_size()], i, clock_unix_ms(clock), endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i));
    } else {
        region ob {
            let plain = alloc_slice[ob](p.3, byte_of(0));
            let n = bodies.open(dv[off_body()..off_body() + bodies.size()], id, fp, window[p.2..p.2 + p.3], plain);
            if n >= 0 {
                buffer.drop(heap, ask);
                ask = wire.request(heap, id, plain[0..n], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), dv[off_xt()..off_xt() + epx.xt_size()], i, clock_unix_ms(clock), endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i));
            } else {
                opened = false;
            }
        }
    }
    if !opened {
        buffer.drop(heap, ask);
        dv[flight_at(e, id)] = 1;
        dv[off_flying() + e] = dv[off_flying() + e] + 1;
        return (atab, finish_attempt(done, dv, clock, e, id, attempt.no_connect(), 0));
    }
    var table = atab;
    var started = 0 - 1;
    var code = attempt.no_connect();
    borrow ask as &qb in {
        let (grown, slot, answer) = attempt.begin(heap, table, poller, net, endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i), buffer.bytes(qb), at, req, token0, e, id, clock_ms(clock) + dv[c_deadline()]);
        table = grown;
        started = slot;
        code = answer;
    }
    buffer.drop(heap, ask);
    if started >= 0 {
        dv[flight_at(e, id)] = 1;
        dv[off_flying() + e] = dv[off_flying() + e] + 1;
        return (table, 0);
    }
    // It could not even begin: that is an outcome like any other, and the event is not in flight.
    dv[flight_at(e, id)] = 1;
    dv[off_flying() + e] = dv[off_flying() + e] + 1;
    return (table, finish_attempt(done, dv, clock, e, id, code, 0));
}

// Start an attempt at the replay in entry `r`, for the endpoint in table slot `i`. Answers the table and 1 if an outcome was
// written at once (the connection failed before it began), else 0.
fn start_replay[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &q](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!q [byte], atab: conns.Table, i: int, r: int, token0: int) -> [heap, fs_read(""), file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
    let base = off_rp() + r * rp_stride();
    let id = dv[base + 2];
    let e = dv[base + 1];
    if dv[base + 5] < 0 {
        dv[base + 5] = find_offset(lg, window, id);
    }
    dv[base + 6] = 1;
    dv[off_flying() + e] = dv[off_flying() + e] + 1;
    if dv[base + 5] < 0 {
        return (atab, finish_replay(done, dv, clock, e, id, attempt.no_connect(), 0));
    }
    let r0 = evlog.read_at(lg, dv[base + 5], window);
    if r0.0 != 0 || record.ms_of(window, 0) != id {
        return (atab, finish_replay(done, dv, clock, e, id, attempt.no_connect(), 0));
    }
    let p = record.pair_at(window, record.first_pair(0));
    if evlog.is_erased(window[p.2..p.2 + p.3]) {
        // Erased while the replay waited (section 47.3): the replay ends, nothing is sent.
        dv[base] = 0;
        dv[off_flying() + e] = dv[off_flying() + e] - 1;
        note_outcome(done, dv, state.replay_cancelled(), e, id, 0, 0);
        return (atab, 1);
    }
    var ask = buffer.empty(heap, 0);
    var opened = true;
    let fp = bodies.sealed_by(window, 0);
    if fp < 0 {
        buffer.drop(heap, ask);
        ask = wire.request(heap, id, window[p.2..p.2 + p.3], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), dv[off_xt()..off_xt() + epx.xt_size()], i, clock_unix_ms(clock), endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i));
    } else {
        region ob {
            let plain = alloc_slice[ob](p.3, byte_of(0));
            let n = bodies.open(dv[off_body()..off_body() + bodies.size()], id, fp, window[p.2..p.2 + p.3], plain);
            if n >= 0 {
                buffer.drop(heap, ask);
                ask = wire.request(heap, id, plain[0..n], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), dv[off_xt()..off_xt() + epx.xt_size()], i, clock_unix_ms(clock), endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i));
            } else {
                opened = false;
            }
        }
    }
    if !opened {
        buffer.drop(heap, ask);
        return (atab, finish_replay(done, dv, clock, e, id, attempt.no_connect(), 0));
    }
    var table = atab;
    var started = 0 - 1;
    var code = attempt.no_connect();
    borrow ask as &qb in {
        let (grown, slot, answer) = attempt.begin(heap, table, poller, net, endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i), buffer.bytes(qb), at, req, token0, e, id + replay_base(), clock_ms(clock) + dv[c_deadline()]);
        table = grown;
        started = slot;
        code = answer;
    }
    buffer.drop(heap, ask);
    if started >= 0 {
        return (table, 0);
    }
    return (table, finish_replay(done, dv, clock, e, id, code, 0));
}

// The most events one turn of `start_attempts` passes over for endpoints that do not subscribe to them, in all: they cost a read each and no attempt, and
// a backlog of them must not hold the loop (ingest, the other endpoints) for as long as it is long.
fn most_skips() -> [] int {
    return 4096;
}

// The most cells of windows one turn of `start_attempts` walks, in all (the check is made before an endpoint's pass, so a turn walks at most this and one window more: 66,560).
// An endpoint that fails everything has a window of 1,024 events that are neither final nor due: its pass reads every cell once to learn when to look again (`off_wake`), and
// 1,024 such endpoints would be a turn of 1,048,576 cells, about 100 ms in which no request is read (measured, section 41.12). A turn that stops for this reason makes the next
// wait 0 (`ex_again`), and the endpoints it did not reach are the ones whose `off_wake` is still 0. Up to 62 full windows (63,488 cells) a turn is what it was.
fn most_walk() -> [] int {
    return 65536;
}

// Is the event `id`, which `scan_next` has just left in `window`, one that endpoint `e` (table index `i`) does not subscribe to? An endpoint with no
// subscription wants everything and the record is not looked at; an event the endpoint has a trace of (final, or an attempt made: a restart finding
// it under a subscription that has changed since) is not passed over. The type is read from the record's `typ` pair: no JSON is parsed.
fn unwanted[&w, &d](window: &w [byte], dv: &d [int], i: int, e: int, id: int) -> [] bool {
    if epx.types_len(dv[off_xt()..off_xt() + epx.xt_size()], i) == 0 {
        return false;
    }
    if state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) || state.attempts(dv[off_cells()..off_flight()], e, id) > 0 {
        return false;
    }
    let t = filter.type_in(window);
    return !epx.accepts(dv[off_xt()..off_xt() + epx.xt_size()], i, window[t.0..t.0 + t.1]);
}

// The next event of the events log that endpoint `e` has not looked at, which must be `id` (`docs/design.md` section 31): read it where the
// endpoint's own scan stands, note where it starts in the endpoint's ring, and move the scan past it. Records before `id` are passed over
// (a cursor that moved without the scan, which nothing does today, must not stall the endpoint). The record is left in `window`. Answers 1,
// or 0 if there is no such record yet (it is not flushed) or the log does not hold `id` next, in which case nothing changes.
fn scan_next[&l, &w, &d](lg: &!l evlog.Ev, window: &!w [byte], dv: &!d [int], e: int, id: int) -> [fs_read(""), file_read] int {
    var at = dv[scan_off(e)];
    if at < 0 {
        return 0;
    }
    while true {
        let r = evlog.read_at(lg, at, window);
        if r.0 != 0 {
            return 0;
        }
        let found = record.ms_of(window, 0);
        if found < id {
            at = at + r.1;
        } else if found == id {
            dv[offs_at(e, id)] = at;
            dv[scan_off(e)] = at + r.1;
            dv[scan_id(e)] = id;
            return 1;
        } else {
            return 0;
        }
    }
    return 0;
}

// Start attempts: for each endpoint in turn, starting from a different one each time, every event in its window that is not
// final, not in flight and whose time has come gets one, up to `most_starts()` in all and `per_endpoint()` in flight for each
// endpoint. An event the endpoint has not looked at yet is read from the events log at the endpoint's own scan position, only when
// the endpoint is about to start it or to pass over it: how far one endpoint has read says nothing about another, and the log is
// read forward from each endpoint's cursor, bounded only by that endpoint's window. Answers the table and how many outcomes were written
// at once.
fn start_attempts[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], atab: conns.Table, token0: int) -> [heap, fs_read(""), file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
    if ops.stopping(dv[off_ops()..off_ops() + ops.size()]) {
        // Asked to stop (`docs/design.md` section 34.4): what is on the wire finishes, and nothing new starts.
        return (atab, 0);
    }
    if log.broken(done) {
        // The outcomes log took a write that failed (a full disk): only a restart clears that (`/readyz` says `delivery_log`). An attempt started now could not
        // be recorded, so it would count for nothing and be due again at once: the same events sent as fast as the receivers answer (`docs/soak.md`, finding 2).
        // What is on the wire finishes; nothing new starts, and the restart sends again what was not recorded, once.
        return (atab, 0);
    }
    let count = dv[c_endpoints()];
    var table = atab;
    // The service has `attempt.slots()` connections in all, and `attempt.begin` answers "no connection" for a start beyond them, which
    // `start_one` records as a failed attempt: a step of the retry schedule used for a receiver that was never called. So a turn
    // starts no more attempts than there are free slots (found by `scripts/bench/run.py` and `tests/saturation_test.py`:
    // ten endpoints can want 80 in flight).
    var held = 0;
    borrow table as &tt in {
        held = conns.live(tt);
    }
    var budget = state.starts_allowed(held, attempt.slots(), most_starts());
    var written = 0;
    var skips = 0;
    var walked = 0;
    var turn = dv[c_turn()];
    dv[c_turn()] = turn + 1;
    dv[off_ex() + ex_turns()] = dv[off_ex() + ex_turns()] + 1;
    lim.begin_turn(lim_of_mut(dv));
    let mono = clock_ms(clock);
    // What the loop need not look at (`docs/design.md` section 41.3): an endpoint that is disabled, or that has read every event it may (the newest, or the end of
    // its window) and has nothing whose time comes before `wake` is skipped by comparing two numbers. The newest event and the time are taken once for the turn.
    let newest = evlog.last_id(lg);
    let turn_now = clock_unix_ms(clock);
    var step = 0;
    while step < count && budget > 0 && walked < most_walk() {
        let i = (turn + step) % count;
        let e = dv[off_table() + i * endpoints.stride()];
        var reach = dv[off_cur() + e] + state.span();
        if newest < reach {
            reach = newest;
        }
        var quiet = is_disabled(dv, e);
        if !quiet && dv[scan_id(e)] >= reach && dv[off_wake() + e] > turn_now {
            quiet = true;
        }
        if !quiet {
            dv[off_ex() + ex_looked()] = dv[off_ex() + ex_looked()] + 1;
            let now = clock_unix_ms(clock);
            // The endpoint's own limits, or the service's (`lim.ls`): attempts in flight together, and attempts started a second.
            let cap = lim.conc_of(lim_of(dv), dv[off_xt()..off_xt() + epx.xt_size()], i);
            let rate = lim.rate_of(lim_of(dv), dv[off_xt()..off_xt() + epx.xt_size()], i);
            var id = dv[off_cur() + e] + 1;
            var going = true;
            // The first time a waiting event of the window is due, among those this pass looked at and did not start; `held` if the endpoint's rate limit stopped it.
            var wake = wake_never();
            var held = false;
            let ended_before = dv[c_attempts()];
            while going && budget > 0 && skips < most_skips() && !is_disabled(dv, e) && state.in_window(dv[off_cur()..off_cur() + state.max_endpoints()], e, id) && dv[off_flying() + e] < cap {
                var loaded = false;
                if id > dv[scan_id(e)] {
                    if scan_next(lg, window, dv, e, id) == 1 {
                        loaded = true;
                    } else {
                        going = false;
                    }
                }
                if going {
                    if loaded && (unwanted(window, dv, i, e, id) || erased_record(window)) {
                        // The endpoint's subscription does not want this event (section 35): it is final here at once, with no attempt and no record;
                        // the cursor moves over it as over a delivered one, and a restart decides it again from the log.
                        apply_outcome(dv, e, state.delivered(), id, 0, 0);
                        dv[off_lag() + e] = 1;
                        dv[off_ex() + ex_filtered()] = dv[off_ex() + ex_filtered()] + 1;
                        skips = skips + 1;
                    } else if !state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) && dv[flight_at(e, id)] == 0 {
                        let due = state.next_at(dv[off_cells()..off_flight()], e, id);
                        if due <= now {
                            if lim.admit(lim_of_mut(dv), e, mono, rate) {
                                budget = budget - 1;
                                let (grown, w) = start_one(heap, lg, done, window, dv, blob, net, clock, poller, at, req, table, i, id, loaded, token0);
                                table = grown;
                                written = written + w;
                            } else {
                                // Held back by the endpoint's rate limit: not a failure. The event waits where it is and is looked at again when a token is due.
                                going = false;
                                held = true;
                            }
                        } else if due < wake {
                            wake = due;
                        }
                    }
                    id = id + 1;
                    walked = walked + 1;
                }
            }
            // A pass that went through the whole window (it read every event there was to read, or it reached the end of the window) says when to look again. One that
            // stopped for the turn's budget, the skip budget, the endpoint's concurrency or its rate limit says nothing: the endpoint is looked at on the next turn.
            // (An attempt of this endpoint that ended during the pass, because the connection could not even begin, set its own next time after the ones looked at: not said.)
            if !held && dv[c_attempts()] == ended_before && (!going || !state.in_window(dv[off_cur()..off_cur() + state.max_endpoints()], e, id)) && !is_disabled(dv, e) {
                dv[off_wake() + e] = wake;
            } else {
                dv[off_wake() + e] = 0;
            }
        }
        step = step + 1;
    }
    if skips >= most_skips() || walked >= most_walk() && step < count {
        dv[off_ex() + ex_again()] = 1;
    }
    // Then the replays that are due, with what is left of the budget.
    let now = clock_unix_ms(clock);
    var rr = 0;
    while rr < rp_cap() && budget > 0 {
        let base = off_rp() + rr * rp_stride();
        if dv[base] == 1 && dv[base + 6] == 0 && dv[base + 4] <= now && !is_disabled(dv, dv[base + 1]) {
            let i = index_of(dv, dv[base + 1]);
            if i >= 0 && dv[off_flying() + dv[base + 1]] < lim.conc_of(lim_of(dv), dv[off_xt()..off_xt() + epx.xt_size()], i) && lim.admit(lim_of_mut(dv), dv[base + 1], mono, lim.rate_of(lim_of(dv), dv[off_xt()..off_xt() + epx.xt_size()], i)) {
                budget = budget - 1;
                let (grown, w) = start_replay(heap, lg, done, window, dv, blob, net, clock, poller, at, req, table, i, rr, token0);
                table = grown;
                written = written + w;
            }
        }
        rr = rr + 1;
    }
    return (table, written);
}

// One turn of delivery. `ev` holds the `(token, readiness)` pairs the poller reported for handles that are not the server's:
// the attempts' connections. Move each of those attempts along, end the ones past their deadline, start new ones, and flush the
// outcomes once. Answers the attempts' connection table and how many outcomes were written.
fn delivery_turn[&f, &h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r, &s, &e](ffi: &f Ffi("libcrypto,libssl"), heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], ev: &e [int], nev: int, token0: int, atab: conns.Table) -> [ffi("libcrypto"), ffi("libssl"), heap, fs_read(""), file_read, file_write, net_out(""), conn_read, conn_write, poll, clock] (conns.Table, int) {
    var table = atab;
    var written = 0;
    var j = 0;
    while j < nev {
        let slot = ev[2 * j] - token0;
        if attempt.busy(at, slot) {
            borrow mut table as &!tw in {
                written = written + settle(ffi, done, dv, clock, tw, poller, at, req, resp, slot, token0);
            }
            if attempt.redial_due(at, slot) {
                // The name is resolved and every address judged: connect to the one that was (`attempt.redial`), or end the attempt if that cannot be done.
                let (redialed, code) = attempt.redial(heap, table, poller, net, at, slot, token0);
                table = redialed;
                if code != attempt.pending() {
                    borrow mut table as &!tw in {
                        written = written + conclude(ffi, done, dv, clock, tw, at, slot, code);
                    }
                }
            }
        }
        j = j + 1;
    }
    borrow mut table as &!tw in {
        written = written + sweep(ffi, done, dv, clock, tw, at);
    }
    // A deleted endpoint's slot is free once its last attempt has ended (`finish_drains`).
    written = written + finish_drains(done, dv);
    let (grown, started) = start_attempts(heap, lg, done, window, dv, blob, net, clock, poller, at, req, table, token0);
    written = written + started;
    if written > 0 {
        // What a restart resumes from; a lost tail only means a repeat.
        log.flush(done);
    }
    return (grown, written);
}

// ---------------------------------------------------------------------
// Schedules (`docs/design.md` section 32)
// ---------------------------------------------------------------------

// One fire of the schedule in row `row` of the reply `rep` of the query for what is due: the ordinary event
// `{"type": <type>, "schedule": <id>, "scheduled_at": <second>, "body": <body>}`, stored by `store_event` (the path of `POST /events`) under the idempotency
// key `cron:<id>:<second>`. A key that the index holds means the event is in the log already: a fire whose update the database never saw because the service was
// stopped between the two, and nothing is appended (this is the whole of "exactly once"; the key is held whatever its age). Answers 0 (the event is in
// the log, now or before), 1 (the schedule cannot make an event: its type or body is not what the service wrote, or the event is too large), or 2 (the
// log or the key index would not take it: the row stays due).
fn fire_cron[&h, &m, &c, &l, &x, &y, &z](heap: &!h Heap, rep: &m [byte], row: int, second: int, now: int, scratch: &!c [byte], lg: &!l evlog.Ev, ix: &!x [int], arena: &!y [byte], bd: &!z [int]) -> [heap] int {
    let id = queries.schedules_due_id(rep, row);
    var outcome = 2;
    // The region is left by falling out of it, on every path: one left by a `return` is not given back (lex-sys #252), and this runs for every fire.
    region a {
        let key_buf = alloc_slice[a](64, byte_of(0));
        let key = key_buf[0..sched.key_into(key_buf, id, second)];
        // The keys of schedules have an index of their own (`docs/retention.md` section 7).
        let cix = ix[idem.second_at(ix)..len(ix)];
        let carena = arena[idem.second_arena_at(ix)..len(arena)];
        var go = true;
        if idem.find(cix, carena, key) >= 0 {
            outcome = 0;
            go = false;
        } else if !idem.room(cix, len(key)) {
            idem.evict(cix, now, 4096);
            if !idem.room(cix, len(key)) {
                go = false;
            }
        }
        if go {
            let (ta, tb) = queries.schedules_due_event_type(rep, row);
            let (ba, bb) = queries.schedules_due_body(rep, row);
            var w = json.writer(heap, 256);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "type");
            w = json.put_string(heap, w, rep[ta..tb]);
            w = json.put_key(heap, w, "schedule");
            w = json.put_int(heap, w, id);
            w = json.put_key(heap, w, "scheduled_at");
            w = json.put_int(heap, w, second);
            w = json.put_key(heap, w, "body");
            w = json.put_fragment(heap, w, rep[ba..bb]);
            w = json.end_object(heap, w);
            let event = json.finish(w);
            borrow event as &er in {
                let text = buffer.bytes(er);
                let tbuf = alloc_slice[a](filter.max_type() + 8, 0);
                let (why, tlen) = invalid_event(heap, text, tbuf);
                if len(why) > 0 {
                    outcome = 1;
                } else {
                    let stored = store_event(scratch, text, tbuf, tlen, key, true, crc.of(text), 0 - 1, lg, cix, carena, now, bd);
                    if stored.0 == 0 {
                        outcome = 0;
                    } else if stored.0 == log.too_long() {
                        outcome = 1;
                    }
                }
            }
            buffer.drop(heap, event);
        }
    }
    return outcome;
}

// The rows of the tick's select (`rep`): each is judged (`sched.plan`), the ones that fire are appended to the log (not yet flushed), and what each decided is kept
// in `sg` for `tick_send`. Answers how many rows are kept and how many events are in the log for them. A row whose event the log would not take is left as it
// is, and so is due again at the next cycle.
fn tick_rows[&h, &m, &c, &l, &x, &y, &s, &z](heap: &!h Heap, rep: &m [byte], unix_ms: int, scratch: &!c [byte], lg: &!l evlog.Ev, ix: &!x [int], arena: &!y [byte], sg: &!s [int], bd: &!z [int]) -> [heap] (int, int) {
    var kept = 0;
    var appended = 0;
    var row = pg.first_row(rep);
    while row >= 0 && kept < sched.rows_most() {
        let id = queries.schedules_due_id(rep, row);
        let base = queries.schedules_due_base(rep, row);
        let was = queries.schedules_due_next_fire(rep, row);
        let (action, second, after) = sched.plan(rep, row, unix_ms / 1000, sg);
        var keep_row = true;
        var fired = 0;
        var next = after;
        if action == 2 {
            let done = fire_cron(heap, rep, row, second, unix_ms, scratch, lg, ix, arena, bd);
            if done == 0 {
                fired = second;
                appended = appended + 1;
                sched.tick_progress(sg);
            } else if done == 1 {
                // An event that cannot be made is parked, as a row that does not parse is.
                sched.count_error(sg);
                next = sched.far();
            } else {
                sched.count_error(sg);
                keep_row = false;
            }
        } else if action == 0 {
            sched.count_error(sg);
        } else if action == 3 {
            sched.count_skipped(sg);
            sched.tick_progress(sg);
        }
        if keep_row {
            sched.row_put(sg, kept, id, base, was, fired, next);
            kept = kept + 1;
        }
        row = pg.next_row(rep, row);
    }
    return (kept, appended);
}

// One flush for every event the cycle appended, and only after it the updates that say so (`advance_schedule`: a compare-and-set on the row as it was read,
// so a change made meanwhile is not overwritten). If the flush fails the fires are not recorded and the rows stay due; the keys are in the index, so the
// next cycle will not append them again.
fn tick_send[&h, &q, &l, &s](heap: &!h Heap, qw: &!q pool.Pool, lg: &!l evlog.Ev, sg: &!s [int], kept: int, appended: int, mono_ms: int) -> [heap, fs_write(""), file_write] int {
    var flushed = true;
    if appended > 0 && evlog.flush(lg) != 0 {
        flushed = false;
        sched.count_error(sg);
    }
    var waiting = 0;
    var j = 0;
    while j < kept {
        let second = sched.row_second(sg, j);
        if second == 0 || flushed {
            let request = queries.advance_schedule_start(heap, sched.row_id(sg, j), sched.row_base(sg, j), sched.row_was_next(sg, j), second, sched.row_next(sg, j));
            var sent = 0 - 1;
            borrow request as &rb in {
                sent = pool.submit(qw, sched.tick_tag(sg, 1 + j), buffer.bytes(rb));
            }
            buffer.drop(heap, request);
            if sent == 0 {
                waiting = waiting + 1;
                if second > 0 {
                    sched.count_fired(sg);
                }
            } else {
                sched.count_error(sg);
            }
        }
        j = j + 1;
    }
    if waiting == 0 {
        sched.tick_end(sg, mono_ms);
    } else {
        sched.tick_updates(sg, waiting, mono_ms);
    }
    return 0;
}

// The pool has answered a request the tick sent: the select (part 0) names what is due, an update (part 1 and up) only counts the cycle down. An answer to a
// cycle that was given up (it took too long) is not this cycle's and is dropped.
fn tick_answer[&h, &q, &l, &x, &y, &c, &s, &z](heap: &!h Heap, qw: &!q pool.Pool, lg: &!l evlog.Ev, ix: &!x [int], arena: &!y [byte], scratch: &!c [byte], sg: &!s [int], tag: int, unix_ms: int, mono_ms: int, bd: &!z [int]) -> [heap, fs_write(""), file_write] int {
    let part = sched.tick_part(sg, tag);
    if part < 0 {
        return 0;
    }
    var failed = pool.status(qw) != 0 || pg.failure(pool.reply(qw)) >= 0;
    if part > 0 {
        if sched.tick_state(sg) == 2 {
            if failed {
                sched.count_error(sg);
            }
            if sched.tick_answered(sg) == 0 {
                sched.tick_end(sg, mono_ms);
            }
        }
        return 0;
    }
    if sched.tick_state(sg) != 1 {
        return 0;
    }
    if failed {
        sched.count_error(sg);
        sched.tick_end(sg, mono_ms);
        return 0;
    }
    let (kept, appended) = tick_rows(heap, pool.reply(qw), unix_ms, scratch, lg, ix, arena, sg, bd);
    return tick_send(heap, qw, lg, sg, kept, appended, mono_ms);
}

// Start a cycle if it is time: ask the database for the schedules that are due. A cycle that is overdue is given up first.
fn tick_start[&h, &q, &s](heap: &!h Heap, qw: &!q pool.Pool, sg: &!s [int], unix_ms: int, mono_ms: int) -> [heap] int {
    sched.tick_expire(sg, mono_ms);
    if !sched.tick_due(sg, mono_ms) {
        return 0;
    }
    let tag = sched.tick_begin(sg, mono_ms);
    let request = queries.schedules_due_start(heap, unix_ms / 1000);
    var sent = 0 - 1;
    borrow request as &rb in {
        sent = pool.submit(qw, tag, buffer.bytes(rb));
    }
    buffer.drop(heap, request);
    if sent != 0 {
        sched.count_error(sg);
        sched.tick_unsent(sg, mono_ms);
    }
    return 0;
}

// ---------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------

// The keys that seal the bodies at rest (`bodies.ls`, `docs/design.md` section 47.4), read from their files at the start, and four random bytes for the nonces.
// Answers 0, or 46 if a file cannot be read or holds no key (32 bytes, or 64 hexadecimal digits), or an old key is given without a key.
fn load_keys[&f, &c, &b, &d](fs: &f Fs(""), cfg: &c [int], blob: &b [byte], bd: &!d [int]) -> [fs_read(""), file_read] int {
    if config.key_file_len(cfg) == 0 {
        if config.old_key_file_len(cfg) > 0 {
            return 46;
        }
        return 0;
    }
    var status = 0;
    region a {
        let raw = alloc_slice[a](96, byte_of(0));
        let key = alloc_slice[a](32, byte_of(0));
        let rnd = alloc_slice[a](4, byte_of(0));
        let n = store.read_range(fs, blob[config.key_file_at()..config.key_file_at() + config.key_file_len(cfg)], 0, raw);
        if n <= 0 || bodies.parse_key(raw[0..n], key) != 0 {
            status = 46;
        } else {
            var prefix = 0;
            // (bound first: compared directly, a file operation's answer is one the LLVM backend cannot type, `docs/design.md` section 34.9)
            let got = fs_read(fs, "/dev/urandom", rnd);
            if got == 4 {
                prefix = int_of(rnd[0]) * 16777216 + int_of(rnd[1]) * 65536 + int_of(rnd[2]) * 256 + int_of(rnd[3]);
            }
            bodies.set_key(bd, 0, key, prefix);
            if config.old_key_file_len(cfg) > 0 {
                let m = store.read_range(fs, blob[config.old_key_file_at()..config.old_key_file_at() + config.old_key_file_len(cfg)], 0, raw);
                if m <= 0 || bodies.parse_key(raw[0..m], key) != 0 {
                    status = 46;
                } else {
                    bodies.set_key(bd, 1, key, 0);
                }
            }
        }
        var i = 0;
        while i < 96 {
            raw[i] = byte_of(0);
            i = i + 1;
        }
        i = 0;
        while i < 32 {
            key[i] = byte_of(0);
            i = i + 1;
        }
    }
    return status;
}

// A start that cannot open what the log holds (section 47.4): the newest event's body is sealed by a key this start was not given. Answers 0, or 47.
fn check_sealed[&l, &w, &d](lg: &!l evlog.Ev, window: &!w [byte], bd: &d [int]) -> [fs_read(""), file_read] int {
    let last = evlog.last_id(lg);
    if last < 1 || last < evlog.first_id(lg) {
        return 0;
    }
    let at = find_offset(lg, window, last);
    if at < 0 {
        return 0;
    }
    let r = evlog.read_at(lg, at, window);
    if r.0 != 0 {
        return 0;
    }
    let fp = bodies.sealed_by(window, 0);
    if fp < 0 {
        return 0;
    }
    if bodies.on(bd) && (bd[2] == fp || bd[1] == 1 && bd[3] == fp) {
        return 0;
    }
    return 47;
}

// The outcome of a request that was held for the database, for the audit log (`audit.ls`), when it is answered.
fn audit_answer[&h, &d, &b](heap: &!h Heap, lines: buffer.Buffer, dv: &!d [int], now: int, ticket: int, answer: &b [byte]) -> [heap] buffer.Buffer {
    if !audit.on(dv[off_aud()..off_aud() + audit.size()]) {
        return lines;
    }
    return audit.answered(heap, lines, dv[off_aud()..off_aud() + audit.size()], now, server.ticket_slot(ticket), audit.status_of(answer));
}

// The poller token of the claim on `SIGINT` and `SIGTERM`, counted from `server.first_token`: the attempts' tokens come first (`attempt.slots()` of them),
// the database's pool after them (its lanes, 2 at most), and the claim's is clear of both. The delivery and history code ignore a token that is not theirs.
fn signal_token() -> [] int {
    return attempt.slots() + 16;
}

// The poller token of the lookup of the database's host (`dbname.ls`), after the claim's.
fn dbname_token() -> [] int {
    return attempt.slots() + 17;
}

// Why the service ends because the endpoints could not be read from the database after the start (section 37.2), on stderr: `status` is 20 and `detail` a reason
// of `dbup`, or 13 and the number of the row the parser refused, or 15 and 17 for the log.
fn say_unreadable[&i](out: &!i Io, status: int, detail: int) -> [err_write] int {
    if status == 13 {
        say(out, "hooks: the endpoints table: row ");
        ops.say_number(out, detail);
        say(out, " is not valid (an id of seven digits or more, a repeated id, a port, a host that is not a public IPv4 address unless allow-private-hosts is 1, a secret that is not whsec_ and base64, or more than 1024 endpoints)\n");
        return 0;
    }
    if status == 20 {
        say(out, "hooks: the database's endpoints cannot be read: ");
        say(out, dbup.message(detail));
        say(out, "\n");
        return 0;
    }
    say(out, "hooks: the delivery log does not agree with the endpoints of the table (status ");
    ops.say_number(out, status);
    say(out, ")\n");
    return 0;
}

// Serve until killed. Each turn: `wait`, then every request that is ready. An accepted event is appended and its request
// *held*; after the turn's last request one `flush` covers every append of the turn, and then each held request is answered
// `202`. If the flush fails nothing is acknowledged: each gets a `503` and the log refuses everything after
// (`lexsys-log` design section 5).
fn run[&h, &r, &k, &l, &g, &w, &n, &x, &v, &i, &a, &j, &o, &y, &e, &c](heap: &!h Heap, router: &r route.Router, clock: &k Clock, listener: &!l Listener, lg: &!g evlog.Ev, done0: log.Log, window: &!w [byte], net: &n Net(""), blob: &!x [byte], dv: &!v [int], ix: &!i [int], arena: &!a [byte], sg: &!j [int], io: &!o Io, pl0: pool.Pool, claim: SignalWatch, dir: &y [byte], stop_ms: int, dbhost: &e [byte], dbport: int, ssl: &c Ffi("libcrypto,libssl"), tls_ctx: int, ns: int, ns_port: int, resume: bool) -> [heap, conn_accept, conn_read, conn_write, poll, clock, file_read, file_write, fs_read(""), fs_write(""), net_out(""), ffi("libcrypto"), ffi("libssl"), err_write] int {
    // The outcomes log is owned here, by value: a snapshot replaces it (`compact.ls`), and a resource can only be replaced by its owner.
    var done = done0;
    match poller_new() {
        Polling::Ok(p) => {
            var srv = server.open(heap, p, listener, max_len() + 4096, 0, 9);
            // The database's connections (`history.ls`), watched in the same poller under tokens above the attempts'.
            var pl = pl0;
            borrow mut srv as &!sw in {
                borrow mut pl as &!qw in {
                    pool.start(qw, server.poller(sw), server.first_token(sw) + attempt.slots());
                }
            }
            var widest = 1;
            if route.most_params(router) > 1 {
                widest = route.most_params(router);
            }
            let params = box_slice(heap, 2 * widest, 0);
            let scratch = box_slice(heap, max_len() + 8192, byte_of(0));
            let note = box_slice(heap, 4, 0);
            // The requests for the history that wait for the database: per slot, the pool's tag (0 for a free slot), the ticket of the
            // held connection, whether to keep it alive, and when to give up.
            let pq = box_slice(heap, pq_cap() * 4, 0);
            var next_tag = history.query_base();
            // The delivery attempts in flight: their state, request and response bytes, the poller events that concern them, and
            // their connections.
            let at = box_slice(heap, attempt.at_size(), 0);
            // What the attempts share (`docs/design.md` section 40): the TLS context (the trust store is read once, here, never per attempt), the name server, the
            // address policy, and whether sessions are kept.
            borrow mut at as &!aw0 in {
                attempt.configure(contents(aw0), tls_ctx, ns, ns_port, dv[c_private()] == 1, resume);
            }
            let req = box_slice(heap, attempt.req_size(), byte_of(0));
            let resp = box_slice(heap, attempt.resp_size(), byte_of(0));
            let events = box_slice(heap, 256, 0);
            var atab = conns.empty(heap, attempt.slots());
            // The database's host, when it is a name, is resolved by the service (`dbname.ls`, section 45): the lookup's one connection, and its query and answer.
            var dtab = conns.empty(heap, 1);
            // The audit log (`audit.ls`): the lines of a turn, written once with the turn's group commit; and the request being answered, kept until its line is written.
            var alines = buffer.empty(heap, 4096);
            var astash = buffer.empty(heap, 512);
            if audit.on(dv[off_aud()..off_aud() + audit.size()]) {
                region ap {
                    let apath = alloc_slice[ap](2112, byte_of(0));
                    let al = store.path_join(apath, dir, "audit.log");
                    let have = store.size_of(evlog.lend(lg), apath[0..al]);
                    if have > 0 {
                        dv[off_aud() + 3] = have;
                    }
                }
            }
            let dnb = box_slice(heap, dbname.buf_size(), byte_of(0));
            dbname.init(dv[off_dbn()..off_dbn() + dbname.size()], dbhost, ns, ns_port);
            let tickets = box_slice(heap, most_held(), 0);
            let ids = box_slice(heap, most_held(), 0);
            let keeps = box_slice(heap, most_held(), 0);
            var out = buffer.empty(heap, 4096);
            // Stopping (`docs/design.md` section 34.4): `SIGTERM` and `SIGINT` are claimed (`main` did it), and the claim is watched in the same poller as
            // everything else, so a stop wakes the wait at once. The token is above the attempts', the pool's and the connections'.
            var watched = claim;
            borrow mut srv as &!sw in {
                borrow watched as &wr in {
                    ops.wake_on(server.poller(sw), wr, server.first_token(sw) + signal_token());
                }
            }
            var held = ops.Held::Live(watched);
            var done_synced = 0;
            borrow done as &dr0 in {
                done_synced = log.synced(dr0);
            }
            ops.begin(ops_of_mut(dv), clock_unix_ms(clock), evlog.synced(lg), done_synced);
            var running = true;
            // What `run` answers (0, or the status the service ends with when the endpoints cannot be read from the database: section 37.2), when the
            // service began to wait for the database, and how many of its connections were live when last looked at (a change is said on stderr).
            var code = 0;
            let began_ms = clock_ms(clock);
            var seen_live = 0;
            while running {
                // The longest the wait may be: 50 ms (less while an endpoint waits for a token of its rate limit: `lim.wait_ms`), or less when the pool has something to do sooner (a backoff that ends, a login with a key to
                // derive, an attempt or a request that runs out of time). The poller wakes the loop for the rest of what the pool waits for.
                var nap = lim.wait_ms(lim_of(dv));
                if dv[off_ex() + ex_again()] == 1 {
                    nap = 0;
                    dv[off_ex() + ex_again()] = 0;
                    dv[off_ex() + ex_hurried()] = dv[off_ex() + ex_hurried()] + 1;
                }
                if history.enabled(dv[off_hq()..off_hq() + history.size()]) {
                    borrow pl as &qr in {
                        let due = pool.next_wake(qr, clock_ms(clock));
                        if due >= 0 && due < nap {
                            nap = due;
                        }
                    }
                }
                srv = server.wait(heap, srv, clock, listener, nap);
                if !ops.stopping(ops_of(dv)) {
                    let (kept, caught) = ops.look(held);
                    held = kept;
                    if caught != 0 {
                        // The first signal: from here the next one ends the process at once, nothing new is started, and the loop runs until what
                        // is on the wire has ended or `stop-deadline-ms` has passed. (`ops.look` has closed the claim: the signals have their default
                        // action again, so the next one ends the process at once.)
                        ops.begin_stop(ops_of_mut(dv), caught, clock_ms(clock), stop_ms);
                        say(io, "hooks: stopping on ");
                        if caught == ops.sigint() {
                            say(io, "SIGINT");
                        } else {
                            say(io, "SIGTERM");
                        }
                        say(io, ": nothing new is taken or started; attempts on the wire may finish for ");
                        ops.say_number(io, stop_ms);
                        say(io, " ms; a second signal ends the process at once\n");
                    }
                }
                if ops.probe_due(ops_of(dv), clock_ms(clock)) {
                    ops.probe_set(ops_of_mut(dv), ops.probe_write(evlog.lend(lg), dir, ops.probe_round(ops_of(dv))), clock_ms(clock));
                }
                var held = 0;
                var more = true;
                while more {
                    var slot = 0 - 1;
                    borrow mut srv as &!sw in {
                        slot = server.next(heap, sw);
                    }
                    if slot < 0 {
                        more = false;
                    } else {
                        borrow mut out as &!ob in {
                            buffer.clear(ob);
                        }
                        var keep = 1;
                        var ascope = 0;
                        var amlen = 0;
                        var aplen = 0;
                        var aflen = 0;
                        borrow srv as &sr in {
                            borrow mut params as &!pw in {
                                borrow mut scratch as &!cw in {
                                    borrow mut note as &!nw in {
                                        borrow mut done as &!dgw in {
                                            out = handle(heap, router, server.head(sr), server.parsed(sr), contents(pw), server.body(sr), lg, dgw, window, contents(cw), contents(nw), dv[0..dv_size()], ix, arena, sg, clock_unix_ms(clock), out);
                                        }
                                    }
                                }
                            }
                            if !http.keeps_alive(server.parsed(sr)) {
                                keep = 0;
                            }
                            // What the audit line needs of the request, while it is there: who (the token presented), the method, the path and X-Forwarded-For.
                            if audit.on(dv[off_aud()..off_aud() + audit.size()]) {
                                borrow mut astash as &!asw in {
                                    buffer.clear(asw);
                                }
                                ascope = authz.presented(server.head(sr), server.parsed(sr), dv[off_token()..off_token() + authz.tokens_size()]);
                                let am = http.method(server.head(sr), server.parsed(sr));
                                let apth = http.path(server.head(sr), server.parsed(sr));
                                amlen = len(am);
                                aplen = len(apth);
                                astash = buffer.append(heap, astash, am);
                                astash = buffer.append(heap, astash, apth);
                                let fh = http.find_header(server.head(sr), server.parsed(sr), "x-forwarded-for");
                                aflen = 0;
                                if fh >= 0 {
                                    let fv = http.header_value(server.head(sr), server.parsed(sr), fh);
                                    if len(fv) <= 256 {
                                        aflen = len(fv);
                                        astash = buffer.append(heap, astash, fv);
                                    }
                                }
                            }
                        }
                        var accepted = 0 - 1;
                        var asked = 0 - 1;
                        var again = 0;
                        var creating = false;
                        var scheduling = false;
                        borrow note as &nr in {
                            accepted = contents(nr)[0];
                            if contents(nr)[0] == 0 - 2 {
                                asked = contents(nr)[1];
                            }
                            if contents(nr)[0] >= 0 {
                                again = contents(nr)[1];
                            }
                            if contents(nr)[0] == 0 - 3 {
                                creating = true;
                            }
                            if contents(nr)[0] == 0 - 4 {
                                scheduling = true;
                            }
                        }
                        if creating {
                            // A new endpoint (`finish_create` has the rest): make its secret if it brought none, send the insert to the
                            // database and hold the connection until the database answers; or say at once that it cannot be done.
                            var sent = 0 - 1;
                            var made = 0;
                            var unfit = false;
                            if dv[off_mg() + manage.mg_kind()] == 1 {
                                let filled = fill_change(dv, blob);
                                if filled != 0 {
                                    made = 0 - 1;
                                }
                                if filled == 2 {
                                    unfit = true;
                                }
                            }
                            if made == 0 && dv[off_mg() + manage.mg_make()] == 1 {
                                made = manage.make_secret(heap, evlog.lend(lg), dv[off_mg()..off_mg() + manage.mg_size()]);
                            }
                            if made == 0 {
                                region ra {
                                    let hl = dv[off_mg() + manage.mg_host_len()];
                                    let sl = dv[off_mg() + manage.mg_secret_len()];
                                    let host = alloc_slice[ra](264, byte_of(0));
                                    let secret = alloc_slice[ra](96, byte_of(0));
                                    manage.bytes_of(dv[off_mg()..off_mg() + manage.mg_size()], manage.mg_host(), hl, host);
                                    manage.bytes_of(dv[off_mg()..off_mg() + manage.mg_size()], manage.mg_secret(), sl, secret);
                                    let request = change_request(heap, dv, host[0..hl], secret[0..sl]);
                                    borrow request as &rb in {
                                        borrow mut pl as &!qw in {
                                            sent = pool.submit(qw, next_tag, buffer.bytes(rb));
                                        }
                                    }
                                    buffer.drop(heap, request);
                                }
                            }
                            if unfit {
                                dv[off_mg() + manage.mg_state()] = 0;
                                out = server.failure(heap, out, 400, manage.why(6), keep == 1);
                                borrow mut srv as &!sw in {
                                    borrow out as &ob in {
                                        server.respond(sw, buffer.bytes(ob));
                                    }
                                }
                            } else if sent == 0 {
                                var ticket = 0 - 1;
                                borrow mut srv as &!sw in {
                                    ticket = server.hold(sw);
                                }
                                dv[off_mg() + manage.mg_state()] = 2;
                                dv[off_mg() + manage.mg_tag()] = next_tag;
                                dv[off_mg() + manage.mg_ticket()] = ticket;
                                dv[off_mg() + manage.mg_keep()] = keep;
                                dv[off_mg() + manage.mg_deadline()] = clock_ms(clock) + query_wait_ms();
                                next_tag = next_tag + 1;
                            } else {
                                dv[off_mg() + manage.mg_state()] = 0;
                                out = server.failure(heap, out, 503, "the endpoint cannot be stored now", keep == 1);
                                borrow mut srv as &!sw in {
                                    borrow out as &ob in {
                                        server.respond(sw, buffer.bytes(ob));
                                    }
                                }
                            }
                        } else if asked >= 0 {
                            // A request for the history: queue it on the pool and hold the connection until the answer comes, or
                            // say at once that it cannot be done (every slot taken, the pool full, no connection live).
                            var qslot = 0 - 1;
                            borrow pq as &pr in {
                                var k = 0;
                                while k < pq_cap() {
                                    if contents(pr)[4 * k] == 0 && qslot < 0 {
                                        qslot = k;
                                    }
                                    k = k + 1;
                                }
                            }
                            var sent = 0 - 1;
                            if qslot >= 0 {
                                let request = queries.attempts_of_start(heap, asked);
                                borrow request as &rb in {
                                    borrow mut pl as &!qw in {
                                        sent = pool.submit(qw, next_tag, buffer.bytes(rb));
                                    }
                                }
                                buffer.drop(heap, request);
                            }
                            if sent == 0 {
                                var ticket = 0 - 1;
                                borrow mut srv as &!sw in {
                                    ticket = server.hold(sw);
                                }
                                borrow mut pq as &!pw in {
                                    contents(pw)[4 * qslot] = next_tag;
                                    contents(pw)[4 * qslot + 1] = ticket;
                                    contents(pw)[4 * qslot + 2] = keep;
                                    contents(pw)[4 * qslot + 3] = clock_ms(clock) + query_wait_ms();
                                }
                                next_tag = next_tag + 1;
                            } else {
                                out = server.failure(heap, out, 503, "the history cannot be read now", keep == 1);
                                borrow mut srv as &!sw in {
                                    borrow out as &ob in {
                                        server.respond(sw, buffer.bytes(ob));
                                    }
                                }
                            }
                        } else if scheduling {
                            // A request about schedules (`sched.judge` kept it in `sg`): send it to the database and hold the connection until the answer
                            // comes (`sched.answer`), or say at once that it cannot be done (every slot taken, the pool full, no connection live).
                            var sent = 0 - 1;
                            let slot = sched.free_slot(sg);
                            let tag = sched.fresh_tag(sg);
                            if slot >= 0 {
                                let request = sched.request_for(heap, sg);
                                borrow request as &rb in {
                                    borrow mut pl as &!qw in {
                                        sent = pool.submit(qw, tag, buffer.bytes(rb));
                                    }
                                }
                                buffer.drop(heap, request);
                            }
                            if sent == 0 {
                                var ticket = 0 - 1;
                                borrow mut srv as &!sw in {
                                    ticket = server.hold(sw);
                                }
                                sched.hold(sg, slot, tag, ticket, keep == 1, sched.deadline_for(clock_ms(clock)));
                            } else {
                                out = server.failure(heap, out, 503, "the schedule cannot be stored now", keep == 1);
                                borrow mut srv as &!sw in {
                                    borrow out as &ob in {
                                        server.respond(sw, buffer.bytes(ob));
                                    }
                                }
                            }
                        } else if accepted >= 0 && held < most_held() {
                            var ticket = 0 - 1;
                            borrow mut srv as &!sw in {
                                ticket = server.hold(sw);
                            }
                            borrow mut tickets as &!tw in {
                                contents(tw)[held] = ticket;
                            }
                            borrow mut ids as &!iw in {
                                contents(iw)[held] = accepted;
                            }
                            borrow mut keeps as &!kw in {
                                // bit 0: keep the connection alive; bit 1: an `Idempotency-Key` repeat (counted apart in `/metrics`)
                                contents(kw)[held] = keep + 2 * again;
                            }
                            held = held + 1;
                        } else {
                            borrow mut srv as &!sw in {
                                borrow out as &ob in {
                                    server.respond(sw, buffer.bytes(ob));
                                }
                            }
                        }
                        // The audit line (section 47.1): the answer's status, or 0 for a request held for the database (its outcome is written when it is answered).
                        if audit.on(dv[off_aud()..off_aud() + audit.size()]) {
                            var astatus = 0;
                            var aroute = 0;
                            borrow out as &ob in {
                                astatus = audit.status_of(buffer.bytes(ob));
                            }
                            borrow note as &nr in {
                                aroute = contents(nr)[2];
                                if contents(nr)[0] >= 0 && aroute == 2 {
                                    // an event taken: its acknowledgement waits for the flush, and an ingest that was taken is not written
                                    astatus = 202;
                                }
                            }
                            if audit.wanted(aroute, astatus) {
                                borrow astash as &asr in {
                                    let ab = buffer.bytes(asr);
                                    alines = audit.request(heap, alines, dv[off_aud()..off_aud() + audit.size()], clock_unix_ms(clock), ascope, ab[0..amlen], ab[amlen..amlen + aplen], ab[amlen + aplen..amlen + aplen + aflen], astatus, slot);
                                }
                            }
                        }
                    }
                }
                if held > 0 {
                    // One flush for the turn, then the acknowledgements.
                    let stored = evlog.flush(lg);
                    var n = 0;
                    while n < held {
                        var ticket = 0;
                        var id = 0;
                        var keep_alive = true;
                        var repeat = false;
                        borrow tickets as &tr in {
                            ticket = contents(tr)[n];
                        }
                        borrow ids as &ir in {
                            id = contents(ir)[n];
                        }
                        borrow keeps as &kr in {
                            keep_alive = contents(kr)[n] & 1 == 1;
                            repeat = contents(kr)[n] >> 1 == 1;
                        }
                        var resp = buffer.empty(heap, 256);
                        buffer.drop(heap, out);
                        if stored == 0 {
                            if repeat {
                                ops.duplicate(ops_of_mut(dv));
                            } else {
                                ops.accepted(ops_of_mut(dv));
                            }
                            let body = id_body(heap, id);
                            borrow body as &bb in {
                                resp = server.reply(heap, resp, 202, buffer.bytes(bb), keep_alive);
                            }
                            buffer.drop(heap, body);
                        } else {
                            ops.refused(ops_of_mut(dv), 503);
                            resp = server.failure(heap, resp, 503, "the event could not be stored", keep_alive);
                        }
                        borrow mut srv as &!sw in {
                            borrow resp as &ab in {
                                alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), ticket, buffer.bytes(ab));
                                server.answer(sw, ticket, buffer.bytes(ab));
                            }
                        }
                        buffer.drop(heap, resp);
                        out = buffer.empty(heap, 4096);
                        n = n + 1;
                    }
                }
                var token0 = 0;
                var nev = 0;
                borrow srv as &sr in {
                    token0 = server.first_token(sr);
                    nev = server.foreign_count(sr);
                    if nev > 128 {
                        nev = 128;
                    }
                    borrow mut events as &!ew in {
                        var j = 0;
                        while j < 2 * nev {
                            contents(ew)[j] = server.foreign(sr)[j];
                            j = j + 1;
                        }
                    }
                }
                if dv[c_endpoints()] > 0 || dv[c_draining()] != 0 {
                    borrow mut srv as &!sw in {
                        borrow mut at as &!aw in {
                            borrow mut req as &!qw in {
                                borrow mut resp as &!pw in {
                                    borrow events as &er in {
                                        borrow mut done as &!dgw in {
                                            let (grown, written) = delivery_turn(ssl, heap, lg, dgw, window, dv, blob, net, clock, server.poller(sw), contents(aw), contents(qw), contents(pw), contents(er), nev, token0, atab);
                                            atab = grown;
                                        }
                                        ops.set_tls(ops_of_mut(dv), attempt.handshakes(contents(aw)), attempt.resumed(contents(aw)));
                                    }
                                }
                            }
                        }
                    }
                    if dv[c_tripped()] != 0 {
                        report_trips(io, dv);
                    }
                }
                // Retention (`docs/retention.md`): at most one step a turn, and what it did said once.
                done = rt_maintain(heap, lg, done, dv, ix, arena, sg, clock, window);
                if dv[rt_at() + r_msg()] != 0 {
                    rt_report(io, dv);
                }
                // The history: what the poller said about the database's connections, the answers, the rows that ended attempts
                // left, and one write for the turn. Nothing here waits for the database.
                if history.enabled(dv[off_hq()..off_hq() + history.size()]) {
                    borrow mut srv as &!sw in {
                        borrow mut pl as &!qw in {
                            borrow events as &er in {
                                var j = 0;
                                while j < nev {
                                    if pool.owns(qw, contents(er)[2 * j]) {
                                        pool.pump(qw, server.poller(sw), contents(er)[2 * j], contents(er)[2 * j + 1]);
                                    } else if contents(er)[2 * j] == server.first_token(sw) + dbname_token() {
                                        borrow mut dtab as &!dt in {
                                            borrow mut dnb as &!dn in {
                                                dbname.advance(dt, server.poller(sw), dv[off_dbn()..off_dbn() + dbname.size()], contents(dn), server.first_token(sw) + dbname_token(), clock_ms(clock));
                                            }
                                        }
                                    }
                                    j = j + 1;
                                }
                            }
                        }
                        // The pool keeps itself full (section 37.1): the logins move on, what ran out of time is given up, and a connection that is due is dialed
                        // (without waiting) and handed over. It takes the pool by value, so it is between the borrows.
                        if dbname.active(dv[off_dbn()..off_dbn() + dbname.size()]) {
                            // The host is a name (section 45): the service resolves it and dials the address; the pool is never given the name.
                            let now_db = clock_ms(clock);
                            borrow mut dtab as &!dt in {
                                dbname.expire(dt, dv[off_dbn()..off_dbn() + dbname.size()], now_db);
                            }
                            var want = 0;
                            borrow mut pl as &!qw in {
                                dbname.note_losses(dv[off_dbn()..off_dbn() + dbname.size()], pool.losses(qw));
                                want = pool.tick(heap, qw, server.poller(sw), now_db);
                            }
                            if want > 0 && !dbname.known(dv[off_dbn()..off_dbn() + dbname.size()]) {
                                // no address yet: a lookup is begun (or is on the wire), and each connection that was due waits its backoff instead of being due every turn
                                borrow mut dnb as &!dn in {
                                    dtab = dbname.start(heap, net, dtab, server.poller(sw), dv[off_dbn()..off_dbn() + dbname.size()], contents(dn), dbhost, server.first_token(sw) + dbname_token(), now_db);
                                }
                                borrow mut pl as &!qw in {
                                    while want > 0 {
                                        pool.dial_failed(qw, now_db, 0 - 2);
                                        want = want - 1;
                                    }
                                }
                            }
                            while want > 0 {
                                var dialed = false;
                                region dt {
                                    let literal = alloc_slice[dt](16, byte_of(0));
                                    let n_lit = dbname.dotted(dv[off_dbn()..off_dbn() + dbname.size()], literal);
                                    match tcp_connect_start(net, literal[0..n_lit], dbport) {
                                        Dialed::Ok(c) => {
                                            let (grown, lane) = pool.adopt(heap, pl, server.poller(sw), now_db, c);
                                            pl = grown;
                                            dialed = true;
                                        }
                                        Dialed::Failed(e) => {
                                        }
                                    }
                                }
                                if !dialed {
                                    borrow mut pl as &!qw in {
                                        pool.dial_failed(qw, now_db, 0 - 3);
                                    }
                                    dbname.stale(dv[off_dbn()..off_dbn() + dbname.size()]);
                                }
                                want = want - 1;
                            }
                        } else {
                            pl = pool.revive(heap, pl, net, dbhost, dbport, server.poller(sw), clock_ms(clock));
                        }
                        borrow mut pl as &!qw in {
                            // every request the pool has an answer for: an insert is counted, a request for the API is answered
                            var tag = pool.next_done(qw);
                            while tag >= 0 {
                                if tag == dbup.load_tag() {
                                    // the answer to the read of the endpoints table (section 37.2)
                                    if pool.status(qw) == 8 {
                                        // the answer does not fit the pool's input slab (1 MiB): a table far over the 540,672 bytes of text the service reads
                                        say_unreadable(io, 20, 6);
                                        code = 20;
                                        running = false;
                                    } else if pool.status(qw) != 0 {
                                        // the connection went with the request on it: it is asked again when one is live
                                        history.set_load_state(dv[off_hq()..off_hq() + history.size()], 0);
                                    } else {
                                        var loaded = 0;
                                        var detail = 0;
                                        borrow mut done as &!dgw in {
                                            let (st, dt) = load_late(heap, lg, dgw, window, dv, blob, pool.reply(qw));
                                            loaded = st;
                                            detail = dt;
                                        }
                                        if loaded == 0 {
                                            history.set_load_state(dv[off_hq()..off_hq() + history.size()], 1);
                                            say(io, "hooks: endpoints loaded: ");
                                            ops.say_number(io, detail);
                                            say(io, "\n");
                                        } else {
                                            say_unreadable(io, loaded, detail);
                                            code = loaded;
                                            running = false;
                                        }
                                    }
                                } else if tag == dv[off_mg() + manage.mg_tag()] && dv[off_mg() + manage.mg_state()] == 2 {
                                    // the database has answered the insert of a new endpoint
                                    // The saved TLS session of the endpoint a change or a delete is for is dropped, whatever the change was (`attempt.drop_session`): after a
                                    // change of host, port, scheme or secret the next attempt makes a full handshake and verifies the certificate again.
                                    var changed = 0 - 1;
                                    if dv[off_mg() + manage.mg_kind()] != 0 {
                                        let ti = index_of_id(dv, dv[off_mg() + manage.mg_target()]);
                                        if ti >= 0 {
                                            changed = endpoints.slot_of(dv[off_table()..off_table() + endpoints.table_size()], ti);
                                        }
                                    }
                                    var created = buffer.empty(heap, 0);
                                    borrow mut done as &!dgw in {
                                        buffer.drop(heap, created);
                                        created = finish_change(heap, dv, blob, lg, dgw, pool.reply(qw), pool.status(qw), dv[off_mg() + manage.mg_keep()] == 1);
                                    }
                                    if changed >= 0 {
                                        borrow mut at as &!aw2 in {
                                            attempt.drop_session(ssl, contents(aw2), changed);
                                        }
                                    }
                                    borrow created as &cb in {
                                        alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), dv[off_mg() + manage.mg_ticket()], buffer.bytes(cb));
                                        server.answer(sw, dv[off_mg() + manage.mg_ticket()], buffer.bytes(cb));
                                    }
                                    buffer.drop(heap, created);
                                    dv[off_mg() + manage.mg_state()] = 0;
                                } else if sched.is_tick(tag) {
                                    // the answer to what the tick asked the database: what is due, or that an update was made
                                    borrow mut scratch as &!cw in {
                                        tick_answer(heap, qw, lg, ix, arena, contents(cw), sg, tag, clock_unix_ms(clock), clock_ms(clock), dv[off_body()..off_body() + bodies.size()]);
                                    }
                                } else if sched.is_admin(tag) {
                                    // the database has answered a request about schedules: the held connection gets the answer
                                    let slots = sched.find_slot(sg, tag);
                                    if slots >= 0 {
                                        let reply = schedule_answer(heap, sched.slot_kind(sg, slots), sched.slot_target(sg, slots), pool.reply(qw), pool.status(qw), sched.slot_keep(sg, slots), clock_unix_ms(clock) / 1000, sg[sched.seconds_at()] == 1);
                                        borrow reply as &rb in {
                                            alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), sched.slot_ticket(sg, slots), buffer.bytes(rb));
                                            server.answer(sw, sched.slot_ticket(sg, slots), buffer.bytes(rb));
                                        }
                                        buffer.drop(heap, reply);
                                        sched.slot_free(sg, slots);
                                    }
                                } else if tag == history.prune_tag() {
                                    // a batch of history rows older than `history-days` (section 43)
                                    history.prune_done(qw, dv[off_hq()..off_hq() + history.size()], clock_unix_ms(clock));
                                } else if tag < history.query_base() {
                                    history.account(qw, dv[off_hq()..off_hq() + history.size()]);
                                } else {
                                    var slotq = 0 - 1;
                                    borrow pq as &pr in {
                                        var k = 0;
                                        while k < pq_cap() {
                                            if contents(pr)[4 * k] == tag {
                                                slotq = k;
                                            }
                                            k = k + 1;
                                        }
                                    }
                                    // (a slot that is not found timed out already and was answered then)
                                    if slotq >= 0 {
                                        var ticket = 0 - 1;
                                        var keep_it = true;
                                        borrow pq as &pr in {
                                            ticket = contents(pr)[4 * slotq + 1];
                                            keep_it = contents(pr)[4 * slotq + 2] == 1;
                                        }
                                        let reply = view.attempts_reply(heap, pool.reply(qw), pool.status(qw), keep_it);
                                        borrow reply as &rb in {
                                            alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), ticket, buffer.bytes(rb));
                                            server.answer(sw, ticket, buffer.bytes(rb));
                                        }
                                        buffer.drop(heap, reply);
                                        borrow mut pq as &!pw in {
                                            contents(pw)[4 * slotq] = 0;
                                        }
                                    }
                                }
                                tag = pool.next_done(qw);
                            }
                            history.sync(qw, dv[off_hq()..off_hq() + history.size()]);
                            // the ones the database has not answered in time are answered now, and forgotten
                            let now_ms = clock_ms(clock);
                            var q = 0;
                            while q < pq_cap() {
                                var due = false;
                                var ticket = 0 - 1;
                                var keep_it = true;
                                borrow pq as &pr in {
                                    due = contents(pr)[4 * q] != 0 && now_ms >= contents(pr)[4 * q + 3];
                                    ticket = contents(pr)[4 * q + 1];
                                    keep_it = contents(pr)[4 * q + 2] == 1;
                                }
                                if due {
                                    let late = server.failure(heap, buffer.empty(heap, 256), 504, "the database did not answer in time", keep_it);
                                    borrow late as &lb in {
                                        alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), ticket, buffer.bytes(lb));
                                        server.answer(sw, ticket, buffer.bytes(lb));
                                    }
                                    buffer.drop(heap, late);
                                    borrow mut pq as &!pw in {
                                        contents(pw)[4 * q] = 0;
                                    }
                                }
                                q = q + 1;
                            }
                            if dv[off_mg() + manage.mg_state()] == 2 && now_ms >= dv[off_mg() + manage.mg_deadline()] {
                                // The insert may still commit: the next start finds the row (section 25.2), and `GET /endpoints/:id` says what this
                                // process believes.
                                let late = server.failure(heap, buffer.empty(heap, 256), 504, "the database did not answer in time; the change may still have been stored", dv[off_mg() + manage.mg_keep()] == 1);
                                borrow late as &lb in {
                                    alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), dv[off_mg() + manage.mg_ticket()], buffer.bytes(lb));
                                    server.answer(sw, dv[off_mg() + manage.mg_ticket()], buffer.bytes(lb));
                                }
                                buffer.drop(heap, late);
                                dv[off_mg() + manage.mg_state()] = 0;
                            }
                            // the requests about schedules that the database has not answered in time are answered now, and forgotten
                            var stale = sched.overdue(sg, now_ms, 0);
                            while stale >= 0 {
                                let late = server.failure(heap, buffer.empty(heap, 256), 504, "the database did not answer in time; the change may still have been stored", sched.slot_keep(sg, stale));
                                borrow late as &lb in {
                                    alines = audit_answer(heap, alines, dv, clock_unix_ms(clock), sched.slot_ticket(sg, stale), buffer.bytes(lb));
                                    server.answer(sw, sched.slot_ticket(sg, stale), buffer.bytes(lb));
                                }
                                buffer.drop(heap, late);
                                sched.slot_free(sg, stale);
                                stale = sched.overdue(sg, now_ms, stale);
                            }
                            if pool.live(qw) != seen_live {
                                seen_live = pool.live(qw);
                                say(io, "hooks: the database: ");
                                ops.say_number(io, seen_live);
                                say(io, " of 2 connections live\n");
                            }
                            // The endpoints are read from the table once, as soon as a connection is live (section 37.2); until they have been, nothing is
                            // delivered, and the service ends with status 20 if the database cannot give them (`dbup.verdict`).
                            if history.load_state(dv[off_hq()..off_hq() + history.size()]) == 0 && pool.live(qw) > 0 {
                                let ask = queries.endpoints_all_start(heap);
                                var asked_it = 0 - 1;
                                borrow ask as &ab in {
                                    asked_it = pool.submit(qw, dbup.load_tag(), buffer.bytes(ab));
                                }
                                buffer.drop(heap, ask);
                                if asked_it == 0 {
                                    history.set_load_state(dv[off_hq()..off_hq() + history.size()], 2);
                                }
                            }
                            if history.load_state(dv[off_hq()..off_hq() + history.size()]) != 1 && running {
                                region ra {
                                    let state5 = alloc_slice[ra](5, byte_of(0));
                                    let known = pool.sqlstate(qw, state5);
                                    let why = dbup.verdict(pool.last_failure(qw), state5[0..known], now_ms - began_ms, history.start_wait_ms(dv[off_hq()..off_hq() + history.size()]));
                                    if why != 0 {
                                        say_unreadable(io, 20, why);
                                        code = 20;
                                        running = false;
                                    }
                                }
                            }
                            // the schedules (`docs/design.md` section 32): ask the database what is due, if it is time (and a connection is live to ask it on)
                            if !ops.stopping(ops_of(dv)) && pool.live(qw) > 0 {
                                tick_start(heap, qw, sg, clock_unix_ms(clock), now_ms);
                            }
                            history.drain(heap, qw, dv[off_hq()..off_hq() + history.size()], 64);
                            if !ops.stopping(ops_of(dv)) {
                                history.prune_start(heap, qw, dv[off_hq()..off_hq() + history.size()], clock_unix_ms(clock));
                            }
                            pool.flush(qw, server.poller(sw));
                        }
                    }
                }
                // The audit log's lines of the turn, appended and synced once (section 47.1).
                borrow mut alines as &!alw in {
                    audit.flush(evlog.lend(lg), dir, alw, dv[off_aud()..off_aud() + audit.size()]);
                }
                // The end of the turn: a flush that made a log's new records durable is a group commit (`/metrics` counts them); and if the service
                // was asked to stop, the loop ends when nothing is on the wire and the history has been handed to the database, or when the
                // deadline has passed.
                ops.look_at_log(ops_of_mut(dv), 0, evlog.synced(lg));
                var done_at = 0;
                borrow done as &dr1 in {
                    done_at = log.synced(dr1);
                }
                ops.look_at_log(ops_of_mut(dv), 1, done_at);
                if ops.stopping(ops_of(dv)) {
                    var on_wire = 0;
                    borrow atab as &tt in {
                        on_wire = conns.live(tt);
                    }
                    var busy = false;
                    if history.enabled(dv[off_hq()..off_hq() + history.size()]) && history.live(dv[off_hq()..off_hq() + history.size()]) > 0 {
                        var asked = 0;
                        borrow pl as &qr in {
                            asked = pool.in_flight(qr);
                        }
                        busy = history.pending(dv[off_hq()..off_hq() + history.size()]) > 0 || asked > 0;
                    }
                    if on_wire == 0 && !busy || ops.deadline_passed(ops_of(dv), clock_ms(clock)) {
                        running = false;
                    }
                }
            }
            ops.release_claim(held);
            // Whatever the loop ended on, what the logs hold is made durable once more before they are closed. A clean stop first states the cursors of the
            // endpoints that have passed over events (`docs/design.md` section 42.3), so that the start after it re-walks nothing.
            borrow mut done as &!dfw in {
                if ops.stopping(ops_of(dv)) {
                    state_cursors(dfw, dv);
                }
                log.flush(dfw);
            }
            evlog.flush(lg);
            if ops.stopping(ops_of(dv)) {
                ops.probe_clean(evlog.lend(lg), dir);
                var on_wire = 0;
                borrow atab as &tt in {
                    on_wire = conns.live(tt);
                }
                say(io, "hooks: stopped: ");
                if on_wire == 0 {
                    say(io, "nothing was left on the wire\n");
                } else {
                    ops.say_number(io, on_wire);
                    if on_wire == 1 {
                        say(io, " attempt was still on the wire at the deadline; it is made again at the next start\n");
                    } else {
                        say(io, " attempts were still on the wire at the deadline; they are made again at the next start\n");
                    }
                }
            }
            pool.close(heap, pl);
            conns.drop(heap, atab);
            conns.drop(heap, dtab);
            buffer.drop(heap, alines);
            buffer.drop(heap, astash);
            unbox_slice(heap, dnb);
            borrow mut at as &!aw1 in {
                attempt.close_tls(ssl, contents(aw1));
            }
            unbox_slice(heap, at);
            unbox_slice(heap, req);
            unbox_slice(heap, resp);
            unbox_slice(heap, events);
            server.close(heap, srv);
            buffer.drop(heap, out);
            unbox_slice(heap, params);
            unbox_slice(heap, scratch);
            unbox_slice(heap, note);
            unbox_slice(heap, pq);
            unbox_slice(heap, tickets);
            unbox_slice(heap, ids);
            unbox_slice(heap, keeps);
            log.close(done);
            return code;
        }
        Polling::Failed(e) => {
            ops.release_claim(ops.Held::Live(claim));
            pool.close(heap, pl0);
            log.close(done);
            return 4;
        }
    }
}

// Read `<dir>/endpoints.conf` into `out` (`endpoints.text_limit()`, 540,672 bytes). Answers the number of bytes, 0 if there is no such file (the service then only
// ingests), or -1000 if the file cannot be read or is too large.
fn read_endpoints_file[&c, &d, &o](fs: &c Fs(""), dir: &d [byte], out: &!o [byte]) -> [fs_read(""), file_read] int {
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        let path = path_buf[0..path_of(path_buf, dir, "endpoints.conf")];
        match open_read(fs, path) {
            Opened::Failed(e) => {
                if e == 2 {
                    return 0;
                }
                return 0 - 1000;
            }
            Opened::Ok(rd0) => {
                var rd = rd0;
                var got = 0 - 1;
                borrow mut rd as &!rh in {
                    match file_pread(rh, 0, out) {
                        Read::Got(n) => {
                            got = n;
                        }
                        Read::End => {
                            got = 0;
                        }
                        Read::Failed(e) => {
                            got = 0 - 1;
                        }
                    }
                }
                file_close(rd);
                if got >= 0 && got < endpoints.text_limit() {
                    return got;
                }
                return 0 - 1000;
            }
        }
    }
}

// Read `<dir>/endpoints.conf` into the delivery state. Answers the number of endpoints, 0 if there is no such file (the service
// then only ingests), or a negative number: `0 - line` for the first bad line, -1000 if the file cannot be read or is too large.
fn load_endpoints[&h, &c, &d, &v, &b](heap: &!h Heap, fs: &c Fs(""), dir: &d [byte], dv: &!v [int], blob: &!b [byte], open: bool) -> [heap, fs_read(""), file_read] int {
    let text = box_slice(heap, endpoints.text_limit(), byte_of(0));
    var result = 0 - 1000;
    borrow mut text as &!tw in {
        let got = read_endpoints_file(fs, dir, contents(tw));
        if got >= 0 {
            result = endpoints.parse_x(contents(tw)[0..got], dv[off_table()..off_table() + endpoints.table_size()], blob, open, dv[off_xt()..off_xt() + epx.xt_size()]);
        }
    }
    unbox_slice(heap, text);
    return result;
}

// Everything delivery needs before the loop starts: the schedule, the endpoints, the outcomes of earlier runs replayed, and the
// place of each endpoint in the events log found. Answers 0, or a status for `main` to exit with.
fn prepare[&h, &d, &g, &l, &w, &v, &b, &t, &x, &y, &e](heap: &!h Heap, dir: &d [byte], lg: &!g evlog.Ev, done: &!l log.Log, window: &!w [byte], dv: &!v [int], blob: &!b [byte], schedule: &t [byte], deadline_ms: int, ix: &!x [int], arena: &!y [byte], etext: &e [byte], from_db: bool, open: bool, now: int) -> [heap, fs_read(""), file_read, file_write] int {
    default_schedule(dv[off_sched()..off_sched() + 17]);
    dv[c_private()] = 0;
    if open {
        dv[c_private()] = 1;
    }
    dv[c_deadline()] = default_deadline_ms();
    if deadline_ms > 0 {
        dv[c_deadline()] = deadline_ms;
    }
    if len(schedule) > 0 && parse_schedule(schedule, dv[off_sched()..off_sched() + 17]) < 0 {
        return 14;
    }
    let rebuilt = rebuild(lg, ix, arena, now);
    if rebuilt != 0 {
        return rebuilt;
    }
    // The outcomes log's format (`docs/retention.md` section 4): a new one gets its header, an unknown version is refused.
    let format = rt_check_format(done, window, now);
    if format != 0 {
        return format;
    }
    // The endpoints: the database's, as `roster.fetch` wrote them, or `endpoints.conf`.
    var n = 0;
    if from_db {
        n = endpoints.parse_x(etext, dv[off_table()..off_table() + endpoints.table_size()], blob, open, dv[off_xt()..off_xt() + epx.xt_size()]);
    } else {
        n = load_endpoints(heap, evlog.lend(lg), dir, dv, blob, open);
    }
    if n < 0 {
        return 13;
    }
    dv[c_endpoints()] = n;
    // The slot map and the sequence number are read whether or not there are endpoints now: an endpoint can be created later (`POST
    // /endpoints`), and it needs to know which slots the log has given and where its records go.
    scan_slots(done, window, dv);
    return settle_endpoints(lg, done, window, dv, n);
}

// What the table of `n` endpoints means for the state: each takes the slot the log gave it, the outcomes of earlier runs are replayed, the ones the log
// did not know are placed, and each endpoint's place in the events log is found. At the start for an endpoints file, and for the table when the database was
// there; later, once, for a table that was read after the start (`load_late`, `docs/design.md` section 37.2). Answers 0, or the status for the service to end with.
fn settle_endpoints[&g, &l, &w, &d](lg: &!g evlog.Ev, done: &!l log.Log, window: &!w [byte], dv: &!d [int], n: int) -> [file_read, file_write, fs_read("")] int {
    if n > 0 {
        match_slots(dv);
        if replay(done, window, dv) > 0 {
            return 15;
        }
        if place_new(done, dv) != 0 {
            return 17;
        }
        // The tables of dead letters that recovery could not fill (a fold drops the old ones while newer ones are there, and cannot bring them back): completed from the log
        var de = 0;
        while de < state.max_endpoints() {
            if dv[off_slotid() + de] >= 0 && index_of_id(dv, dv[off_slotid() + de]) >= 0 {
                dead_complete(done, window, dv, de);
            }
            de = de + 1;
        }
        dead_expire_all(lg, dv);
        if dv[c_seq()] < 1 {
            dv[c_seq()] = 1;
        }
        // An endpoint that was away while events were dropped starts at the oldest that is left.
        rt_clamp(lg, dv);
        // What the log states of every cursor is where the replay left it (`docs/design.md` section 42.3).
        cursors_stated(dv);
        seek_slots(lg, window, dv);
    }
    return 0;
}

// The first read of the endpoints table, when the database came up after the start (`docs/design.md` section 37.2): `rep` is the reply to `endpoints_all`. The
// text is judged by the rule that judges a line of `endpoints.conf` (`endpoints.parse_x`), and what follows is `prepare`'s own (`settle_endpoints`); until it
// has happened nothing was delivered, so the state is the one a start with no endpoints left. Answers the status the service ends with and a detail: 0 and
// the number of endpoints; 13 and the number of the bad row; 15 or 17 (the log); 20 and a reason of `dbup`.
fn load_late[&h, &g, &l, &w, &d, &b, &m](heap: &!h Heap, lg: &!g evlog.Ev, done: &!l log.Log, window: &!w [byte], dv: &!d [int], blob: &!b [byte], rep: &m [byte]) -> [heap, file_read, file_write, fs_read("")] (int, int) {
    let text = box_slice(heap, endpoints.text_limit(), byte_of(0));
    var status = 0;
    var detail = 0;
    borrow mut text as &!tw in {
        let got = roster.text_of(rep, contents(tw));
        if got < 0 {
            status = 20;
            detail = dbup.reason_of_text(got);
        } else {
            let n = endpoints.parse_x(contents(tw)[0..got], dv[off_table()..off_table() + endpoints.table_size()], blob, dv[c_private()] == 1, dv[off_xt()..off_xt() + epx.xt_size()]);
            if n < 0 {
                status = 13;
                detail = 0 - n;
            } else {
                dv[c_endpoints()] = n;
                status = settle_endpoints(lg, done, window, dv, n);
                detail = n;
            }
        }
    }
    unbox_slice(heap, text);
    return (status, detail);
}

// The settings file named by `--config`, read into `cfg` and `blob` (`src/config.ls`). Answers 0, -1 if it cannot be read, -2 if
// it is 16 KiB or more, or the number of the first bad line (`config.why` says what is wrong with it).
fn load_config[&f, &p, &c, &b](fs: &f Fs(""), path: &p [byte], cfg: &!c [int], blob: &!b [byte]) -> [fs_read(""), file_read] int {
    region a {
        let text = alloc_slice[a](16384, byte_of(0));
        match open_read(fs, path) {
            Opened::Failed(e) => {
                return 0 - 1;
            }
            Opened::Ok(rd0) => {
                var rd = rd0;
                var got = 0 - 1;
                borrow mut rd as &!rh in {
                    match file_pread(rh, 0, text) {
                        Read::Got(n) => {
                            got = n;
                        }
                        Read::End => {
                            got = 0;
                        }
                        Read::Failed(e) => {
                            got = 0 - 1;
                        }
                    }
                }
                file_close(rd);
                if got < 0 {
                    return 0 - 1;
                }
                if got >= 16384 {
                    return 0 - 2;
                }
                return config.parse_file(text[0..got], cfg, blob);
            }
        }
    }
}

// Go through the command line, `--key value` or `--key=value`. In pass 0 only `--config` is looked at and its value is copied
// into `path`; in pass 1 every other flag is applied to `cfg` and `blob` in the order written. Answers `(code, path length)`:
// code 0, or `8 * the index of the argument + why`, where `why` is `config.why_key()`, `config.why_value()`, 3 for an argument
// that is not a flag, or 4 for a flag with no value.
fn apply_flags[&g, &c, &b, &p](a: &g Args, cfg: &!c [int], blob: &!b [byte], path: &!p [byte], pass: int) -> [args] (int, int) {
    var plen = 0;
    var i = 1;
    while i < arg_count(a) {
        let x = arg(a, i);
        let at = i;
        let f = config.split_flag(x);
        if f.0 < 0 {
            return (8 * i + 3, plen);
        }
        var value = x[0..0];
        if f.2 >= 0 {
            value = x[f.2..f.3];
        } else {
            if i + 1 >= arg_count(a) {
                return (8 * i + 4, plen);
            }
            i = i + 1;
            value = arg(a, i);
        }
        let key = x[f.0..f.1];
        if bytes.equal(key, "config") {
            if pass == 0 {
                if len(value) < 1 || len(value) > 4000 {
                    return (8 * at + config.why_value(), plen);
                }
                var k = 0;
                while k < len(value) {
                    path[k] = value[k];
                    k = k + 1;
                }
                plen = len(value);
            }
        } else if pass == 1 {
            let why = config.set(cfg, blob, key, value);
            if why != 0 {
                return (8 * at + why, plen);
            }
        }
        i = i + 1;
    }
    return (0, plen);
}

// The decimal digits of `n` into `buf`; answers how many.
fn digits_of[&b](n: int, buf: &!b [byte]) -> [] int {
    var width = 1;
    var t = n;
    while t >= 10 {
        t = t / 10;
        width = width + 1;
    }
    var k = width;
    var m = n;
    while k > 0 {
        buf[k - 1] = byte_of('0' + m % 10);
        m = m / 10;
        k = k - 1;
    }
    return width;
}

fn say[&i, &t](out: &!i Io, text: &t [byte]) -> [err_write] int {
    return io.error_all(out, text);
}

// One line on stderr for each endpoint the circuit breaker has paused since the last turn that said so, and the bit cleared
// (`docs/design.md` section 31). The durable record is the `paused` record in the delivery log; this is for whoever reads the service's output.
fn report_trips[&i, &d](out: &!i Io, dv: &!d [int]) -> [err_write] int {
    var e = 0;
    while e < state.max_endpoints() {
        if take_tripped(dv, e) {
            region a {
                let nb = alloc_slice[a](24, byte_of(0));
                say(out, "hooks: endpoint ");
                say(out, nb[0..digits_of(ident_of_slot(dv, e), nb)]);
                say(out, " paused by the circuit breaker: every attempt has failed for ");
                say(out, nb[0..digits_of(dv[c_breaker()], nb)]);
                say(out, " days or more; its events wait, and POST /endpoints/");
                say(out, nb[0..digits_of(ident_of_slot(dv, e), nb)]);
                say(out, "/enable resumes it\n");
            }
        }
        e = e + 1;
    }
    return 0;
}

// The database has answered the insert of a new endpoint (`docs/design.md` section 25.2): give it a slot, say so in the log (flushed), start its
// cursor at the last event so that it gets what comes after and not the log's past, add it to the table, and answer `201` with the secret,
// which nothing answers again. Answers the whole HTTP response. Nothing is changed in memory or in the log unless the database said commit.
// The members of a change that the request did not name, filled in from the endpoint as it is now (a `PATCH` that names only the
// secret still sends the host and the port, so one statement serves). Answers 0, 1 if the endpoint is not there, or 2 if the host and the scheme together are not
// acceptable (an address with `https`).
fn fill_change[&d, &b](dv: &!d [int], blob: &b [byte]) -> [] int {
    let f = dv[off_mg() + manage.mg_fields()];
    let i = index_of_id(dv, dv[off_mg() + manage.mg_target()]);
    if i < 0 {
        return 1;
    }
    // The host as it will be stored (`destination.ls`): the host the change names, or the endpoint's own, and the scheme the change names, or the endpoint's own,
    // judged together. A host alone is judged against the scheme the endpoint has: an address is not a host for `https`.
    var https = endpoints.scheme_of(dv[off_table()..off_table() + endpoints.table_size()], i) == 1;
    if f & 32 != 0 {
        https = dv[off_mg() + manage.mg_scheme()] == 1;
    }
    // The region is left by falling out of it: one left by a `return` is not given back (lex-sys #252), and this runs for every `PATCH`.
    var refused = false;
    region r {
        let named = alloc_slice[r](264, byte_of(0));
        let stored = alloc_slice[r](264, byte_of(0));
        var n = 0;
        if f & 1 != 0 {
            n = dv[off_mg() + manage.mg_host_len()];
            manage.bytes_of(dv[off_mg()..off_mg() + manage.mg_size()], manage.mg_host(), n, named);
        } else {
            let own = destination.bare(endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i));
            while n < len(own) {
                named[n] = own[n];
                n = n + 1;
            }
        }
        let m = manage.stored_host(named[0..n], https, dv[c_private()] == 1, stored);
        if m < 0 {
            refused = true;
        } else {
            var k = 0;
            while k < m {
                dv[off_mg() + manage.mg_host() + k] = int_of(stored[k]);
                k = k + 1;
            }
            dv[off_mg() + manage.mg_host_len()] = m;
        }
    }
    if refused {
        return 2;
    }
    if f & 2 == 0 {
        dv[off_mg() + manage.mg_port()] = endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i);
    }
    return 0;
}

// The statement for the change that waits in `mg`: the insert of a new endpoint, the update of an address, or of the address and the secret.
fn change_request[&h, &d, &a, &b](heap: &!h Heap, dv: &d [int], host: &a [byte], secret: &b [byte]) -> [heap] buffer.Buffer {
    if dv[off_mg() + manage.mg_kind()] == 2 {
        return queries.delete_endpoint_start(heap, dv[off_mg() + manage.mg_target()]);
    }
    if dv[off_mg() + manage.mg_kind()] == 1 && dv[off_mg() + manage.mg_fields()] & ~35 == 0 {
        return queries.patch_address_start(heap, dv[off_mg() + manage.mg_target()], host, dv[off_mg() + manage.mg_port()]);
    }
    // The region is left by falling out of it: one left by a `return` is not given back (lex-sys #252), and this runs for every `POST` and `PATCH`.
    let xg = dv[off_xg()..off_xg() + epx.xg_size()];
    var q = buffer.empty(heap, 0);
    region a {
        let types = alloc_slice[a](filter.max_list() + 8, byte_of(0));
        let spec = alloc_slice[a](hdrs.max_spec() + 8, byte_of(0));
        let tn = epx.pending_types_into(xg, types);
        let sn = epx.pending_spec_into(xg, spec);
        buffer.drop(heap, q);
        if dv[off_mg() + manage.mg_kind()] != 1 {
            q = queries.create_endpoint_start(heap, host, dv[off_mg() + manage.mg_port()], secret, types[0..tn], spec[0..sn], epx.pending_conc(xg), epx.pending_rate(xg));
        } else {
            let mask = epx.pending_mask(xg);
            q = queries.patch_endpoint_start(heap, dv[off_mg() + manage.mg_target()], host, dv[off_mg() + manage.mg_port()], secret, dv[off_mg() + manage.mg_fields()] & 12 != 0, types[0..tn], mask & epx.m_types() != 0, spec[0..sn], mask & epx.m_headers() != 0, epx.pending_keep_until(xg), mask & epx.m_keep() != 0, epx.pending_conc(xg), mask & epx.m_conc() != 0, epx.pending_rate(xg), mask & epx.m_rate() != 0);
        }
    }
    return q;
}

// The answer to a change that was sent to the database on a connection that was then lost (`pool.lost`): the outcome is **unknown**, which is not the same as
// "the database is not there" (a `503`, nothing was sent) or "the database refused" (a `503`, nothing was changed). The row may have been stored. Nothing in memory
// was changed; the next start (or a restart) reads the table, and a client that wants to know before then asks for the endpoint or the schedule (section 37.4).
fn lost_answer[&h](heap: &!h Heap, keep: bool) -> [heap] buffer.Buffer {
    return server.failure(heap, buffer.empty(heap, 256), 504, "the connection to the database was lost while the change was being stored; the change may have been stored", keep);
}

// The answer to a request about schedules (`sched.answer`), except that a change (create, patch, delete) whose connection was lost has an unknown outcome.
fn schedule_answer[&h, &m](heap: &!h Heap, kind: int, target: int, rep: &m [byte], status: int, keep: bool, now: int, seconds: bool) -> [heap] buffer.Buffer {
    if pool.lost(status) && sched.writes(kind) {
        return lost_answer(heap, keep);
    }
    return sched.answer(heap, kind, target, rep, status, keep, now, seconds);
}

fn finish_change[&h, &b, &l, &g, &d, &m](heap: &!h Heap, dv: &!d [int], blob: &!b [byte], lg: &!l evlog.Ev, done: &!g log.Log, rep: &m [byte], status: int, keep: bool) -> [heap, file_write] buffer.Buffer {
    if pool.lost(status) {
        // The connection went with the change on it (section 37.4): nothing in memory changes, and the row may or may not be in the table.
        return lost_answer(heap, keep);
    }
    if dv[off_mg() + manage.mg_kind()] == 2 {
        return finish_delete(heap, dv, blob, done, rep, status, keep);
    }
    if dv[off_mg() + manage.mg_kind()] == 1 {
        return finish_patch(heap, dv, blob, rep, status, keep);
    }
    return finish_create(heap, dv, blob, lg, done, rep, status, keep);
}

// Free slot `e` in memory: its window is empty, its cursor 0, nothing disabled or paused, no run of failures, no place in the events log, no replay waiting for it, and no id, so `take_slot` may give it to
// anyone. The `removed` record that says so is written by the caller, **before** this (a restart reads the record and does the same).
fn retire_slot[&d](dv: &!d [int], e: int) -> [] int {
    reset_window(dv, e, 0);
    clear_slot(dv, e);
    var r = 0;
    while r < rp_cap() {
        if dv[off_rp() + r * rp_stride() + 1] == e {
            dv[off_rp() + r * rp_stride()] = 0;
        }
        r = r + 1;
    }
    dv[off_slotid() + e] = slot_free();
    set_draining(dv, e, false);
    return 0;
}

// Every slot whose endpoint was deleted and whose last attempt on the wire has ended gets its `removed` record (not yet flushed) and is free again
// (`docs/design.md` section 25.5: the record is written when the slot can be reused, not when the endpoint was deleted). Answers how many were freed.
// A slot whose record the log refuses stays draining and is tried again on the next turn.
fn finish_drains[&g, &d](done: &!g log.Log, dv: &!d [int]) -> [file_write] int {
    if dv[c_draining()] == 0 {
        return 0;
    }
    var freed = 0;
    var e = 0;
    while e < state.max_endpoints() {
        if is_draining(dv, e) && dv[off_flying() + e] == 0 {
            if note_removed(done, dv, e) == 1 {
                retire_slot(dv, e);
                freed = freed + 1;
            }
        }
        e = e + 1;
    }
    return freed;
}

// The database has answered a `DELETE` (`docs/design.md` section 25.5). On commit, and only then: the replays that wait for the endpoint are dropped (a
// record each, flushed), the endpoint leaves the table at once (so it is not in `GET /endpoints`, is sent nothing new and cannot be found by id), and its
// slot is freed with a `removed` record if no attempt of it is on the wire, else kept as *draining* until the last one ends (`finish_drains`). A row
// that was already gone (somebody deleted it by hand) is removed from memory all the same: the database is the owner, and it says there is no such endpoint.
fn finish_delete[&h, &b, &g, &d, &m](heap: &!h Heap, dv: &!d [int], blob: &!b [byte], done: &!g log.Log, rep: &m [byte], status: int, keep: bool) -> [heap, file_write] buffer.Buffer {
    let out = buffer.empty(heap, 256);
    if status != 0 || pg.failure(rep) >= 0 {
        return server.failure(heap, out, 503, "the database did not delete the endpoint; nothing was changed", keep);
    }
    let target = dv[off_mg() + manage.mg_target()];
    let i = index_of_id(dv, target);
    let row = pg.first_row(rep);
    if i < 0 || row >= 0 && queries.delete_endpoint_id(rep, row) != target {
        return server.failure(heap, out, 503, "the database answered for another endpoint; nothing was changed", keep);
    }
    let e = endpoints.slot_of(dv[off_table()..off_table() + endpoints.table_size()], i);
    // The records first: a replay that waits is over (`replay_dead`, which recovery reads as "ended"), and with nothing on the wire the slot is free.
    var wrote = 0;
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 1] == e && dv[b + 6] == 0 {
            if note_outcome(done, dv, state.replay_dead(), e, dv[b + 2], dv[b + 3], 0) == 0 {
                return server.failure(heap, out, 503, "the change could not be stored", keep);
            }
            wrote = wrote + 1;
        }
        r = r + 1;
    }
    let idle = dv[off_flying() + e] == 0;
    if idle {
        if note_removed(done, dv, e) == 0 {
            return server.failure(heap, out, 503, "the change could not be stored", keep);
        }
        wrote = wrote + 1;
    }
    if wrote > 0 && log.flush(done) != 0 {
        return server.failure(heap, out, 503, "the change could not be stored", keep);
    }
    // Memory, after the records are down.
    r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 1] == e && dv[b + 6] == 0 {
            dv[b] = 0;
        }
        r = r + 1;
    }
    epx.drop_row(dv[off_xt()..off_xt() + epx.xt_size()], dv[c_endpoints()], i);
    dv[c_endpoints()] = endpoints.remove(dv[off_table()..off_table() + endpoints.table_size()], blob, dv[c_endpoints()], i);
    if idle {
        retire_slot(dv, e);
    } else {
        set_draining(dv, e, true);
    }
    var wr = json.writer(heap, 128);
    wr = json.begin_object(heap, wr);
    wr = json.put_key(heap, wr, "id");
    wr = json.put_int(heap, wr, target);
    wr = json.put_key(heap, wr, "deleted");
    wr = json.put_bool(heap, wr, true);
    wr = json.put_key(heap, wr, "draining");
    wr = json.put_bool(heap, wr, !idle);
    if row < 0 {
        wr = json.put_key(heap, wr, "row");
        wr = json.put_string(heap, wr, "was already gone");
    }
    wr = json.end_object(heap, wr);
    let body = json.finish(wr);
    var answer = out;
    borrow body as &sb in {
        answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, body);
    return answer;
}

// The database has answered a `PATCH`: on commit the table in memory takes the new address (and key), so the next attempt, a retry of an event
// first tried under the old secret included, uses them; an attempt on the wire finishes against what it began with. The answer carries the
// secret if the change made or brought one, as `POST /endpoints` does.
fn finish_patch[&h, &b, &d, &m](heap: &!h Heap, dv: &!d [int], blob: &!b [byte], rep: &m [byte], status: int, keep: bool) -> [heap] buffer.Buffer {
    let out = buffer.empty(heap, 512);
    if status != 0 || pg.failure(rep) >= 0 {
        return server.failure(heap, out, 503, "the database did not store the change", keep);
    }
    let row = pg.first_row(rep);
    if row < 0 {
        return server.failure(heap, out, 404, "the endpoint is not in the database (its row is gone); nothing was changed", keep);
    }
    let mg = off_mg();
    let target = dv[mg + manage.mg_target()];
    let i = index_of_id(dv, target);
    if i < 0 || queries.patch_address_id(rep, row) != target {
        return server.failure(heap, out, 503, "the database answered for another endpoint; nothing was changed", keep);
    }
    let hl = dv[mg + manage.mg_host_len()];
    let sl = dv[mg + manage.mg_secret_len()];
    let port = dv[mg + manage.mg_port()];
    let fields = dv[mg + manage.mg_fields()];
    let scratch = box_slice(heap, endpoints.text_limit(), byte_of(0));
    var failed = false;
    // The region is left by falling out of it: one left by a `return` is not given back (lex-sys #252), and this runs for every `PATCH`.
    var answer = out;
    region a {
        let host = alloc_slice[a](264, byte_of(0));
        let secret = alloc_slice[a](96, byte_of(0));
        let key = alloc_slice[a](96, byte_of(0));
        manage.bytes_of(dv[mg..mg + manage.mg_size()], manage.mg_host(), hl, host);
        manage.bytes_of(dv[mg..mg + manage.mg_size()], manage.mg_secret(), sl, secret);
        // The key in force until this change: while a rotation overlaps it signs beside the new one (copied now, `replace` may move the blob).
        let prev = alloc_slice[a](96, byte_of(0));
        let was = endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i);
        let prev_len = len(was);
        var pk = 0;
        while pk < prev_len && pk < 96 {
            prev[pk] = was[pk];
            pk = pk + 1;
        }
        var klen = 0;
        if fields & 12 != 0 {
            klen = sign.secret_key(secret[0..sl], key);
        } else {
            let old = endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i);
            klen = len(old);
            var k = 0;
            while k < klen {
                key[k] = old[k];
                k = k + 1;
            }
        }
        if klen < 0 {
            failed = true;
        } else {
            borrow mut scratch as &!sc in {
                if endpoints.replace(dv[off_table()..off_table() + endpoints.table_size()], blob, dv[c_endpoints()], i, port, host[0..hl], key[0..klen], contents(sc)) != 0 {
                    failed = true;
                }
            }
        }
        if !failed {
            // The subscription, the headers and the previous secret (`epx.ls`): the database has them, now the delivery does.
            let mask = epx.pending_mask(dv[off_xg()..off_xg() + epx.xg_size()]);
            let until = epx.pending_keep_until(dv[off_xg()..off_xg() + epx.xg_size()]);
            let ex_types = alloc_slice[a](filter.max_list() + 8, byte_of(0));
            let ex_spec = alloc_slice[a](hdrs.max_spec() + 8, byte_of(0));
            if mask & epx.m_types() != 0 {
                epx.set_types(dv[off_xt()..off_xt() + epx.xt_size()], i, ex_types[0..epx.pending_types_into(dv[off_xg()..off_xg() + epx.xg_size()], ex_types)]);
            }
            if mask & epx.m_headers() != 0 {
                epx.set_spec(dv[off_xt()..off_xt() + epx.xt_size()], i, ex_spec[0..epx.pending_spec_into(dv[off_xg()..off_xg() + epx.xg_size()], ex_spec)]);
            }
            if mask & epx.m_conc() != 0 {
                epx.set_conc(dv[off_xt()..off_xt() + epx.xt_size()], i, epx.pending_conc(dv[off_xg()..off_xg() + epx.xg_size()]));
            }
            if mask & epx.m_rate() != 0 {
                epx.set_rate(dv[off_xt()..off_xt() + epx.xt_size()], i, epx.pending_rate(dv[off_xg()..off_xg() + epx.xg_size()]));
            }
            var overlap = 0;
            if fields & 12 != 0 {
                // A new secret: the one it replaces stays only if asked to.
                if mask & epx.m_keep() != 0 && until > 0 {
                    epx.set_old(dv[off_xt()..off_xt() + epx.xt_size()], i, prev[0..prev_len], until);
                    overlap = until;
                } else {
                    epx.set_old(dv[off_xt()..off_xt() + epx.xt_size()], i, prev[0..0], 0);
                }
            } else if mask & epx.m_keep() != 0 {
                if until > 0 && epx.old_len(dv[off_xt()..off_xt() + epx.xt_size()], i) > 0 {
                    epx.set_old_until(dv[off_xt()..off_xt() + epx.xt_size()], i, until);
                    overlap = until;
                } else {
                    epx.set_old(dv[off_xt()..off_xt() + epx.xt_size()], i, prev[0..0], 0);
                }
            }
            var wr = json.writer(heap, 256);
            wr = json.begin_object(heap, wr);
            wr = json.put_key(heap, wr, "id");
            wr = json.put_int(heap, wr, target);
            wr = json.put_key(heap, wr, "host");
            wr = json.put_string(heap, wr, destination.bare(host[0..hl]));
            wr = json.put_key(heap, wr, "scheme");
            wr = json.put_string(heap, wr, scheme_name(endpoints.scheme_of(dv[off_table()..off_table() + endpoints.table_size()], i)));
            wr = json.put_key(heap, wr, "port");
            wr = json.put_int(heap, wr, port);
            if fields & 12 != 0 {
                wr = json.put_key(heap, wr, "secret");
                wr = json.put_string(heap, wr, secret[0..sl]);
            }
            if overlap > 0 {
                wr = json.put_key(heap, wr, "secret_old_until");
                wr = json.put_int(heap, wr, overlap);
            }
            wr = json.end_object(heap, wr);
            let body = json.finish(wr);
            borrow body as &sb in {
                answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
            }
            buffer.drop(heap, body);
        }
    }
    unbox_slice(heap, scratch);
    if failed {
        return server.failure(heap, answer, 507, "the change was stored in the database but the service has no room for it (restart it)", keep);
    }
    return answer;
}

fn finish_create[&h, &b, &l, &g, &d, &m](heap: &!h Heap, dv: &!d [int], blob: &!b [byte], lg: &!l evlog.Ev, done: &!g log.Log, rep: &m [byte], status: int, keep: bool) -> [heap, file_write] buffer.Buffer {
    let out = buffer.empty(heap, 512);
    if status != 0 || pg.failure(rep) >= 0 {
        return server.failure(heap, out, 503, "the database did not store the endpoint", keep);
    }
    let row = pg.first_row(rep);
    if row < 0 {
        return server.failure(heap, out, 503, "the database did not store the endpoint", keep);
    }
    let ident = queries.create_endpoint_id(rep, row);
    if ident < 0 || ident > 999999 {
        return server.failure(heap, out, 503, "the database gave an id the service cannot use", keep);
    }
    let mg = off_mg();
    let hl = dv[mg + manage.mg_host_len()];
    let sl = dv[mg + manage.mg_secret_len()];
    let port = dv[mg + manage.mg_port()];
    // The region is left by falling out of it: one left by a `return` is not given back (lex-sys #252), and this runs for every `POST /endpoints`.
    var answer = out;
    region a {
        let host = alloc_slice[a](264, byte_of(0));
        let secret = alloc_slice[a](96, byte_of(0));
        let key = alloc_slice[a](96, byte_of(0));
        let ex_types = alloc_slice[a](filter.max_list() + 8, byte_of(0));
        let ex_spec = alloc_slice[a](hdrs.max_spec() + 8, byte_of(0));
        answer = add_created(heap, dv, blob, lg, done, ident, hl, sl, port, host, secret, key, ex_types, ex_spec, answer, keep);
    }
    return answer;
}

// The body of `finish_create`, with the room it works in given to it (so that `finish_create` can leave its region by falling out of it).
fn add_created[&h, &b, &l, &g, &d, &o, &s, &k, &t, &x](heap: &!h Heap, dv: &!d [int], blob: &!b [byte], lg: &!l evlog.Ev, done: &!g log.Log, ident: int, hl: int, sl: int, port: int, host: &!o [byte], secret: &!s [byte], key: &!k [byte], ex_types: &!t [byte], ex_spec: &!x [byte], out: buffer.Buffer, keep: bool) -> [heap, file_write] buffer.Buffer {
    let mg = off_mg();
    manage.bytes_of(dv[mg..mg + manage.mg_size()], manage.mg_host(), hl, host);
    manage.bytes_of(dv[mg..mg + manage.mg_size()], manage.mg_secret(), sl, secret);
    let klen = sign.secret_key(secret[0..sl], key);
    if klen < 0 {
        return server.failure(heap, out, 503, "the secret cannot be used", keep);
    }
    // An endpoint of this id that the log remembers is a different endpoint (an id is never given twice): its slot is freed.
    var k = 0;
    while k < state.max_endpoints() {
        if dv[off_slotid() + k] == ident {
            if note_removed(done, dv, k) == 0 {
                return server.failure(heap, out, 503, "the change could not be stored", keep);
            }
            dv[off_slotid() + k] = slot_free();
        }
        k = k + 1;
    }
    let slot = take_slot(done, dv, ident);
    var start = evlog.last_id(lg);
    if start < 0 {
        start = 0;
    }
    if slot < 0 || note_created(done, dv, slot, ident, start) == 0 || log.flush(done) != 0 {
        return server.failure(heap, out, 503, "the change could not be stored", keep);
    }
    reset_window(dv, slot, start);
    clear_slot(dv, slot);
    // It starts from now: it has looked at everything up to the last event, and the next record is at the end of the log.
    dv[scan_id(slot)] = start;
    dv[scan_off(slot)] = evlog.tail(lg);
    dv[off_flying() + slot] = 0;
    dv[off_slotid() + slot] = ident;
    let count = dv[c_endpoints()];
    let grown = endpoints.append(dv[off_table()..off_table() + endpoints.table_size()], blob, count, slot, ident, port, host[0..hl], key[0..klen]);
    if grown < 0 {
        return server.failure(heap, out, 503, "the endpoint could not be added", keep);
    }
    // Its subscription and headers (`epx.ls`), in the row of the entry just added.
    epx.clear_row(dv[off_xt()..off_xt() + epx.xt_size()], count);
    epx.set_types(dv[off_xt()..off_xt() + epx.xt_size()], count, ex_types[0..epx.pending_types_into(dv[off_xg()..off_xg() + epx.xg_size()], ex_types)]);
    epx.set_spec(dv[off_xt()..off_xt() + epx.xt_size()], count, ex_spec[0..epx.pending_spec_into(dv[off_xg()..off_xg() + epx.xg_size()], ex_spec)]);
    epx.set_conc(dv[off_xt()..off_xt() + epx.xt_size()], count, epx.pending_conc(dv[off_xg()..off_xg() + epx.xg_size()]));
    epx.set_rate(dv[off_xt()..off_xt() + epx.xt_size()], count, epx.pending_rate(dv[off_xg()..off_xg() + epx.xg_size()]));
    dv[c_endpoints()] = grown;
    var wr = json.writer(heap, 256);
    wr = json.begin_object(heap, wr);
    wr = json.put_key(heap, wr, "id");
    wr = json.put_int(heap, wr, ident);
    wr = json.put_key(heap, wr, "host");
    wr = json.put_string(heap, wr, destination.bare(host[0..hl]));
    wr = json.put_key(heap, wr, "scheme");
    wr = json.put_string(heap, wr, scheme_name(endpoints.scheme_of(dv[off_table()..off_table() + endpoints.table_size()], count)));
    wr = json.put_key(heap, wr, "port");
    wr = json.put_int(heap, wr, port);
    wr = json.put_key(heap, wr, "secret");
    wr = json.put_string(heap, wr, secret[0..sl]);
    wr = json.put_key(heap, wr, "from");
    wr = json.put_string(heap, wr, "now");
    wr = json.put_key(heap, wr, "cursor");
    wr = json.put_int(heap, wr, start);
    wr = json.end_object(heap, wr);
    let body = json.finish(wr);
    var answer = out;
    borrow body as &sb in {
        answer = server.reply(heap, answer, 201, buffer.bytes(sb), keep);
    }
    buffer.drop(heap, body);
    return answer;
}

// A bearer token into the delivery state, at `at` (`off_token()`, plus `authz.ingest_at()` or `authz.read_at()` for the others): its
// length, then its bytes, one to an integer.
fn put_token[&d, &t](dv: &!d [int], at: int, token: &t [byte]) -> [] int {
    dv[at] = len(token);
    var i = 0;
    while i < len(token) {
        dv[at + 1 + i] = int_of(token[i]);
        i = i + 1;
    }
    return 0;
}

// A flag as the message that refuses it names it. A value that is a secret (`--admin-token=...`, `--pg-password=...`: a token that was refused
// is most likely the real one with a typo) is not repeated.
fn say_flag[&i, &x](out: &!i Io, flag: &x [byte]) -> [err_write] int {
    var eq = 0;
    while eq < len(flag) && int_of(flag[eq]) != '=' {
        eq = eq + 1;
    }
    if eq < len(flag) && (bytes.starts_with(flag, "--admin-token=") || bytes.starts_with(flag, "--ingest-token=") || bytes.starts_with(flag, "--read-token=") || bytes.starts_with(flag, "--pg-password=")) {
        say(out, flag[0..eq + 1]);
        say(out, "<hidden>");
        return 0;
    }
    return say(out, flag);
}

// What `production = 1` found wrong, on stderr, naming the setting or the path (`docs/design.md` section 33). `status` is what
// `config.production_status` or `perm.files` answered; `dir` is the data directory and `name` the file in it that was judged (empty: the directory).
fn say_unsafe[&i, &d, &n](out: &!i Io, status: int, dir: &d [byte], name: &n [byte]) -> [err_write] int {
    say(out, "hooks: production = 1 refuses to start: ");
    if status == 33 || status == 35 {
        say(out, dir);
        if len(name) > 0 {
            say(out, "/");
            say(out, name);
        }
        say(out, ": ");
    }
    say(out, config.unsafe_message(status));
    say(out, "\n");
    return 0;
}

// Parse `text` into scratch tables, to find out whether `endpoints.parse` accepts it: the number of endpoints, or `0 - line`. The scratch
// is on the heap because it is as large as the text, and a region of `main` has room for the text and not for a second copy of it.
fn parse_check[&h, &t](heap: &!h Heap, text: &t [byte], open: bool) -> [heap] int {
    let table = box_slice(heap, endpoints.table_size(), 0);
    let blob = box_slice(heap, endpoints.text_limit(), byte_of(0));
    var count = 0;
    borrow mut table as &!tw in {
        borrow mut blob as &!bw in {
            count = endpoints.parse(text, contents(tw), contents(bw), open);
        }
    }
    unbox_slice(heap, table);
    unbox_slice(heap, blob);
    return count;
}

// The TLS client's context (`tls.context`): the system's trust store, or exactly the file `cafile` names (empty: the system's). 0 if it cannot be made.
fn start_tls[&f, &c](libssl: &f Ffi("libssl"), cafile: &c [byte]) -> [ffi("libssl")] int {
    region r {
        let path = alloc_slice[r](len(cafile) + 1, byte_of(0));
        var i = 0;
        while i < len(cafile) {
            path[i] = cafile[i];
            i = i + 1;
        }
        if len(cafile) == 0 {
            return tls.context(libssl, path[0..0]);
        }
        return tls.context(libssl, path);
    }
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock, signals } = split(world);
    // The only foreign authority the service holds, by library: libc (two functions: `statx`, for the modes of the data directory, `src/perm.ls`, the
    // production profile only; `prctl`, once, to ask for small pages, `src/thp.ls`), libssl and libcrypto (the TLS client of an `https` endpoint, `src/tls.ls`). `scripts/check-authority.sh` pins the exact
    // list of symbols. How it learns that it was asked to stop is not foreign: a claim on `SIGINT` and `SIGTERM` (`src/ops.ls`), made just before the loop.
    let libc = narrow(ffi, "libc,libcrypto,libssl");
    let stop = narrow(signals, "INT,TERM");
    // Small pages for the whole process, before the large zero-filled blocks are made (`src/thp.ls`, `docs/design.md` section 46).
    borrow libc as &lt in {
        thp.small_pages(lt);
    }
    var fs = fs;
    var port = 0 - 1;
    var status = 2;
    region a {
        // The settings (`src/config.ls`): the defaults, then the file named by `--config`, then the flags.
        let cfg = alloc_slice[a](config.size(), 0);
        let cblob = alloc_slice[a](config.blob_size(), byte_of(0));
        let cpath = alloc_slice[a](4096, byte_of(0));
        // What the start found in each log (`logguard.ls`): the events log's report, the delivery log's, and the pair's.
        let report = alloc_slice[a](logguard.report_size(), 0);
        config.defaults(cfg);
        var bad = 0;
        borrow args as &g in {
            let first = apply_flags(g, cfg, cblob, cpath, 0);
            var code = first.0;
            if code == 0 && first.1 > 0 {
                var file = 0;
                borrow fs as &fsr in {
                    file = load_config(fsr, cpath[0..first.1], cfg, cblob);
                }
                if file != 0 {
                    borrow mut io as &!i in {
                        say(i, "hooks: --config ");
                        say(i, cpath[0..first.1]);
                        if file == 0 - 1 {
                            say(i, ": cannot be read\n");
                        } else if file == 0 - 2 {
                            say(i, ": is 16 KiB or more\n");
                        } else {
                            let nb = alloc_slice[a](12, byte_of(0));
                            say(i, ": line ");
                            say(i, nb[0..digits_of(file, nb)]);
                            if config.why(cfg) == config.why_key() {
                                say(i, " names a setting there is none of\n");
                            } else if config.why(cfg) == config.why_value() {
                                say(i, " has a value that setting does not take\n");
                            } else {
                                say(i, " is not `key = value`\n");
                            }
                        }
                    }
                    code = 1;
                }
            }
            if code == 0 {
                code = apply_flags(g, cfg, cblob, cpath, 1).0;
            }
            if code >= 8 {
                borrow mut io as &!i in {
                    let why = code % 8;
                    say(i, "hooks: `");
                    say_flag(i, arg(g, code / 8));
                    if why == config.why_key() {
                        say(i, "` is not a setting\n");
                    } else if why == config.why_value() {
                        say(i, "` has a value that setting does not take\n");
                    } else if why == 3 {
                        say(i, "` is not a flag: settings are `--key value` or `--key=value`\n");
                    } else {
                        say(i, "` needs a value\n");
                    }
                }
            }
            bad = code;
        }
        if bad == 0 && config.pg_status(cfg) != 0 {
            borrow mut io as &!i in {
                say(i, "hooks: pg-backoff-max-ms is below pg-backoff-min-ms (the wait after a failed connection starts at the one and doubles up to the other)\n");
            }
            bad = 1;
        }
        if bad == 0 {
            port = config.port_of(cfg);
            if port < 1 && !config.import_endpoints(cfg) || config.dir_len(cfg) == 0 {
                borrow mut io as &!i in {
                    say(i, "hooks: --port and --dir are required\n");
                }
            }
        }
        let dir_buf = cblob;
        let dir_len = config.dir_len(cfg);
        let sched_buf = cblob[config.sched_at()..config.sched_at() + config.sched_len(cfg)];
        let sched_len = config.sched_len(cfg);
        let deadline_ms = config.deadline_ms(cfg);
        let window_ms = config.window_ms(cfg);
        // The endpoints (section 24, C3c): with a database named they are its `endpoints` table, read here, before the logs are
        // opened and before the service listens, and a database that cannot be read is a refusal to start (the file is not a
        // fallback: a stale list delivers to the wrong receivers). `--import-endpoints 1` copies the file into the table and exits.
        var from_db = false;
        var go = bad == 0 && dir_len > 0;
        // The production profile (`docs/design.md` section 33): refuse to start, with a status for each cause, unless the settings are safe on the
        // internet (`config.production_status`) and the data directory and its files are private (`perm.files`). Nothing is opened before this.
        let probe = alloc_slice[a](2304, byte_of(0));
        if go && config.production(cfg) {
            var why = config.production_status(cfg, cblob);
            var which = 0;
            if why == 0 {
                var found = (0, 0);
                borrow libc as &lh in {
                    borrow fs as &fs1 in {
                        found = perm.files(lh, fs1, cblob[0..dir_len], probe);
                    }
                }
                why = found.0;
                which = found.1;
            }
            if why != 0 {
                go = false;
                status = why;
                borrow mut io as &!i in {
                    say_unsafe(i, why, cblob[0..dir_len], perm.name_of(which));
                }
            }
        }
        if go && config.pg_host_len(cfg) > 0 {
            if config.pg_user_len(cfg) == 0 {
                config.set(cfg, cblob, "pg-user", "hooks");
            }
            if config.pg_database_len(cfg) == 0 {
                config.set(cfg, cblob, "pg-database", "hooks");
            }
            if config.pg_password_len(cfg) == 0 {
                config.set(cfg, cblob, "pg-password", "-");
            }
        }
        if go && config.import_endpoints(cfg) {
            go = false;
            status = 20;
            if config.pg_host_len(cfg) == 0 {
                borrow mut io as &!i in {
                    say(i, "hooks: --import-endpoints needs --pg-host\n");
                }
            } else {
                // The text of the file is 540,672 bytes at most: on the heap, not in this region's 64 KiB arena (`docs/design.md` section 41.4).
                var text_len = 0;
                var count = 0;
                var added = 0 - 1;
                borrow mut heap as &!h0 in {
                    let eb = box_slice(h0, endpoints.text_limit(), byte_of(0));
                    borrow mut eb as &!ew in {
                        borrow fs as &fs0 in {
                            text_len = read_endpoints_file(fs0, cblob[0..dir_len], contents(ew));
                        }
                        if text_len > 0 {
                            count = parse_check(h0, contents(ew)[0..text_len], config.allow_private_hosts(cfg));
                        }
                        if text_len > 0 && count >= 0 {
                            borrow net as &nn0 in {
                                borrow fs as &fs0 in {
                                    added = roster.copy_in(h0, nn0, cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg), cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], fs0, contents(ew)[0..text_len]);
                                }
                            }
                        }
                    }
                    unbox_slice(h0, eb);
                }
                if text_len <= 0 {
                    borrow mut io as &!i in {
                        say(i, "hooks: no endpoints.conf to import in --dir\n");
                    }
                } else if count < 0 {
                    borrow mut io as &!i in {
                        let nb = alloc_slice[a](12, byte_of(0));
                        say(i, "hooks: endpoints.conf: line ");
                        say(i, nb[0..digits_of(0 - count, nb)]);
                        say(i, " is not valid (see --allow-private-hosts for hosts); nothing was imported\n");
                    }
                    status = 13;
                } else {
                    borrow mut io as &!i in {
                        let nb = alloc_slice[a](12, byte_of(0));
                        if added < 0 {
                            say(i, "hooks: the database refused the import; nothing was imported\n");
                        } else {
                            say(i, "hooks: imported ");
                            say(i, nb[0..digits_of(added, nb)]);
                            say(i, " of ");
                            say(i, nb[0..digits_of(count, nb)]);
                            say(i, " endpoints; one already there is not changed\n");
                            status = 0;
                        }
                    }
                }
            }
        }
        if go && config.pg_host_len(cfg) > 0 {
            // The endpoints are the table's (section 24.1), and the table is read when the database is there, **after** the start (section 37.2): the service
            // listens and takes events meanwhile, delivers nothing, and ends with status 20 if the database cannot give them. The file is not a fallback.
            from_db = true;
        }
        // `endpoints.conf` is judged here as well, so that a refusal says which line (`prepare` reads it again and answers only 13).
        if go && !from_db && dir_len > 0 {
            var flen = 0;
            var count = 0;
            borrow mut heap as &!h0 in {
                let eb = box_slice(h0, endpoints.text_limit(), byte_of(0));
                borrow mut eb as &!ew in {
                    borrow fs as &fs0 in {
                        flen = read_endpoints_file(fs0, cblob[0..dir_len], contents(ew));
                    }
                    if flen > 0 {
                        count = parse_check(h0, contents(ew)[0..flen], config.allow_private_hosts(cfg));
                    }
                }
                unbox_slice(h0, eb);
            }
            if flen > 0 {
                if count < 0 {
                    go = false;
                    status = 13;
                    borrow mut io as &!i in {
                        let nb = alloc_slice[a](12, byte_of(0));
                        say(i, "hooks: endpoints.conf: line ");
                        say(i, nb[0..digits_of(0 - count, nb)]);
                        say(i, " is not valid (an id of seven digits or more, a repeated id, a port, a host that is not a public IPv4 address unless allow-private-hosts is 1, a secret that is not whsec_ and base64, or more than 1024 endpoints)\n");
                    }
                }
            }
        }
        if go && port > 0 && port < 65536 && dir_len > 0 {
            status = 3;
            borrow mut heap as &!h in {
                var wbuf = buffer.empty(h, max_len() + 4096);
                let dvb = box_slice(h, dv_size(), 0);
                let blob = box_slice(h, endpoints.text_limit(), byte_of(0));
                // Two indexes of idempotency keys: clients' (`idem-keys`) and the schedules' (a quarter of that, at least 16).
                let keys = config.idem_keys(cfg);
                var cron_keys = keys / 4;
                if cron_keys < 16 {
                    cron_keys = 16;
                }
                let ixb = box_slice(h, idem.ix_size(keys) + idem.ix_size(cron_keys), 0);
                let arenab = box_slice(h, idem.arena_size(keys) + idem.arena_size(cron_keys), byte_of(0));
                let sgb = box_slice(h, sched.size(), 0);
                // Both logs are looked at, and the pair judged, before either is changed (section 34.5).
                var gate = 0;
                borrow mut wbuf as &!wb0 in {
                    borrow fs as &fsp in {
                        gate = logguard.preflight(fsp, dir_buf[0..dir_len], buffer.room(wb0), max_len(), config.repair_logs(cfg), report);
                    }
                }
                // The events log holds the capability for the file system from here on (`evlog.lend` lends it back): a resource cannot be shared.
                var ev = evlog.new(h, fs, dir_buf[0..dir_len], config.segment_bytes(cfg), config.compact_kill_at(cfg));
                var now0 = 0;
                borrow clock as &ck0 in {
                    now0 = clock_unix_ms(ck0);
                }
                borrow mut wbuf as &!wb in {
                    borrow mut ev as &!lw in {
                        // The events log is judged before anything is changed (section 34.5, `docs/retention.md` section 5), and then opened; `gate` is what the judgement said.
                        status = 0;
                        if gate != 0 {
                            status = gate;
                            borrow mut io as &!i in {
                                if gate == 18 {
                                    logguard.say_pair(i, report[logguard.pair_at()..logguard.pair_at() + 2]);
                                } else {
                                    logguard.say_damage_of(i, report, 0);
                                }
                            }
                        } else {
                            status = evlog.open(lw, now0, config.repair_logs(cfg), report[0..logguard.rep_size()]);
                            if status == logguard.refused() || status == logguard.not_kept() {
                                // Damage in the middle of the log (section 34.5): refused, with where and how much.
                                borrow mut io as &!i in {
                                    logguard.say_damage_of(i, report, status);
                                }
                                status = 19;
                            }
                        }
                        if status == 0 {
                            // The pair, judged again against the last event the open log holds (a last segment with no event says nothing before it is open).
                            if report[logguard.pair_at()] > evlog.last_id(lw) {
                                status = 18;
                                report[logguard.pair_at() + 1] = evlog.last_id(lw);
                                borrow mut io as &!i in {
                                    logguard.say_pair(i, report[logguard.pair_at()..logguard.pair_at() + 2]);
                                }
                            }
                        }
                        if status == 0 {
                            match open_delivery(lw, dir_buf[0..dir_len], buffer.room(wb), config.repair_logs(cfg), report[logguard.rep_size()..2 * logguard.rep_size()], gate) {
                                Opening::Failed(e) => {
                                    status = 12;
                                    if e == logguard.not_kept() {
                                        status = 19;
                                        borrow mut io as &!i in {
                                            logguard.say_damage_of(i, report, e);
                                        }
                                    }
                                }
                                Opening::Ok(dl0) => {
                                    var dl = dl0;
                                    borrow mut dvb as &!dvw in {
                                        borrow mut blob as &!bw in {
                                            borrow mut ixb as &!ixw in {
                                                borrow mut arenab as &!arw in {
                                                    idem.init(contents(ixw), keys, idem.arena_size(keys), window_ms);
                                                    idem.init(contents(ixw)[idem.ix_size(keys)..len(contents(ixw))], cron_keys, idem.arena_size(cron_keys), 1152921504606846976);
                                                    contents(ixw)[1] = window_ms;
                                                    ops.init(ops_of_mut(contents(dvw)));
                                                    ops.set_settings(ops_of_mut(contents(dvw)), config.stop_deadline_ms(cfg), config.repair_logs(cfg));
                                                    borrow mut dl as &!dw in {
                                                        // The keys of the bodies at rest are read before the logs are (section 47.4): a log sealed by a key this start was not given is refused.
                                                        status = load_keys(evlog.lend(lw), cfg, cblob, contents(dvw)[off_body()..off_body() + bodies.size()]);
                                                        if status == 46 {
                                                            borrow mut io as &!i46 in {
                                                                say(i46, "hooks: encryption-key-file (or encryption-key-file-old) cannot be read, or holds no key: 32 bytes, or 64 hexadecimal digits; an old key needs a key\n");
                                                            }
                                                        } else {
                                                            status = prepare(h, dir_buf[0..dir_len], lw, dw, buffer.room(wb), contents(dvw), contents(bw), sched_buf[0..sched_len], deadline_ms, contents(ixw), contents(arw), cblob[0..0], from_db, config.allow_private_hosts(cfg), now0);
                                                        }
                                                        if status == 0 {
                                                            // An erasure the last run recorded and did not finish in the segment is finished now (section 47.3).
                                                            redo_erasures(h, lw, dw, buffer.room(wb), now0);
                                                            status = check_sealed(lw, buffer.room(wb), contents(dvw)[off_body()..off_body() + bodies.size()]);
                                                            if status == 47 {
                                                                borrow mut io as &!i47 in {
                                                                    say(i47, "hooks: the events log holds bodies sealed by a key this start was not given (encryption-key-file, encryption-key-file-old); nothing was changed\n");
                                                                }
                                                            }
                                                        }
                                                    }
                                                    if status == 0 && config.production(cfg) {
                                                        // The logs exist now, if this start made them: judge their modes too (a umask of 022 makes them 0644).
                                                        var found = (0, 0);
                                                        borrow libc as &lh in {
                                                            found = perm.files(lh, evlog.lend(lw), dir_buf[0..dir_len], probe);
                                                        }
                                                        if found.0 != 0 {
                                                            status = found.0;
                                                            borrow mut io as &!i in {
                                                                say_unsafe(i, found.0, dir_buf[0..dir_len], perm.name_of(found.1));
                                                            }
                                                        }
                                                    }
                                                    if status == 0 {
                                                        borrow mut io as &!iwr in {
                                                            rt_say_start(iwr, lw);
                                                        }
                                                    }
                                                    // The TLS client's trust store is read once, here (`src/tls.ls`): a store that cannot be read is a refusal to start, never a
                                                    // client that does not verify. And the name server for the names of endpoints (`src/resolve.ls`). (Not in `compact-now`, which serves nothing.)
                                                    var tls_ctx = 0;
                                                    var ns = config.dns_server(cfg);
                                                    if status == 0 && !config.compact_now(cfg) {
                                                        borrow libc as &lt in {
                                                            tls_ctx = start_tls(lt, cblob[config.ca_file_at()..config.ca_file_at() + config.ca_file_len(cfg)]);
                                                        }
                                                        if tls_ctx == 0 {
                                                            status = 21;
                                                            borrow mut io as &!i in {
                                                                say(i, "hooks: the TLS trust store cannot be loaded (tls-ca-file, or the system's certificates); an https endpoint could not be verified\n");
                                                            }
                                                        }
                                                        if ns == 0 {
                                                            ns = resolve.from_system(evlog.lend(lw));
                                                        }
                                                    }
                                                    if status != 0 {
                                                        log.close(dl);
                                                    } else if config.compact_now(cfg) {
                                                        // With a database the endpoints are read from it first, as the service reads them and for as long as `pg-start-wait-ms` allows
                                                        // (`compact_read_table`, section 41.8): what is final everywhere is what the cursors of the table's endpoints say.
                                                        var table_status = 0;
                                                        if config.pg_host_len(cfg) > 0 {
                                                            history.enable(contents(dvw)[off_hq()..off_hq() + history.size()]);
                                                            history.set_timing(contents(dvw)[off_hq()..off_hq() + history.size()], config.pg_backoff_min_ms(cfg), config.pg_backoff_max_ms(cfg), config.pg_attempt_ms(cfg), config.pg_request_ms(cfg), config.pg_start_wait_ms(cfg));
                                                            history.set_prune_days(contents(dvw)[off_hq()..off_hq() + history.size()], config.history_days(cfg));
                                                            audit.init(contents(dvw)[off_aud()..off_aud() + audit.size()], config.audit_log(cfg), config.audit_log_bytes(cfg), config.audit_log_files(cfg), 0);
                                                            let fresh = pool.empty(h, history.lanes(), history.depth(), 1048576, 131072);
                                                            let (made, rc) = history.configure(h, evlog.lend(lw), fresh, cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], config.pg_backoff_min_ms(cfg), config.pg_backoff_max_ms(cfg), config.pg_attempt_ms(cfg), config.pg_request_ms(cfg));
                                                            borrow mut dl as &!dw1 in {
                                                                borrow net as &nn1 in {
                                                                    borrow clock as &ck1 in {
                                                                        borrow mut io as &!iw1 in {
                                                                            table_status = compact_read_table(h, lw, dw1, buffer.room(wb), contents(dvw), contents(bw), nn1, ck1, iw1, made, cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg));
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                        if table_status != 0 {
                                                            status = table_status;
                                                            log.close(dl);
                                                        } else {
                                                            rt_init(contents(dvw), config.retention_days(cfg), config.retention_ms_knob(cfg), window_ms, config.delivery_log_bytes(cfg), config.max_age_days(cfg), config.max_age_ms_knob(cfg));
                                                            borrow mut io as &!iw0 in {
                                                                status = compact_once(h, lw, dl, contents(dvw), contents(ixw), contents(arw), buffer.room(wb), now0, iw0);
                                                            }
                                                        }
                                                    } else {
                                                        rt_init(contents(dvw), config.retention_days(cfg), config.retention_ms_knob(cfg), window_ms, config.delivery_log_bytes(cfg), config.max_age_days(cfg), config.max_age_ms_knob(cfg));
                                                        borrow net as &nn in {
                                                            match tcp_listen(nn, port, 1024, 0) {
                                                                Listening::Ok(l) => {
                                                                    var listener = l;
                                                                    borrow mut listener as &!lh in {
                                                                        listener_nonblocking(lh);
                                                                        let router = routes(h);
                                                                        put_token(contents(dvw), off_token(), cblob[config.token_at()..config.token_at() + config.token_len(cfg)]);
                                                                        put_token(contents(dvw), off_token() + authz.ingest_at(), cblob[config.ingest_token_at()..config.ingest_token_at() + config.ingest_token_len(cfg)]);
                                                                        if config.read_token_len(cfg) > 0 {
                                                                            put_token(contents(dvw), off_token() + authz.read_at(), cblob[config.read_token_at()..config.read_token_at() + config.read_token_len(cfg)]);
                                                                        } else if config.production(cfg) {
                                                                            // No read token: in production the reads need the admin token.
                                                                            put_token(contents(dvw), off_token() + authz.read_at(), cblob[config.token_at()..config.token_at() + config.token_len(cfg)]);
                                                                        }
                                                                        contents(dvw)[c_breaker()] = config.breaker_days(cfg);
                                                                        if config.production(cfg) {
                                                                            contents(dvw)[c_production()] = 1;
                                                                        }
                                                                        contents(dvw)[off_ex() + ex_grace()] = config.rotation_grace_ms(cfg);
                                                                        lim.init(lim_of_mut(contents(dvw)), config.retry_jitter(cfg), config.endpoint_concurrency(cfg), config.endpoint_rate(cfg));
                                                                        // The database, if one was named (section 37): the pool is told how to log in and how to come back, and the loop makes its
                                                                        // connections in the background, without waiting; nothing is dialed here. The history, the endpoints, the schedules and the management
                                                                        // routes all use it (`history.ls`, `dbup.ls`).
                                                                        var hpool = pool.empty(h, 1, 1, 4096, 4096);
                                                                        history.set_timing(contents(dvw)[off_hq()..off_hq() + history.size()], config.pg_backoff_min_ms(cfg), config.pg_backoff_max_ms(cfg), config.pg_attempt_ms(cfg), config.pg_request_ms(cfg), config.pg_start_wait_ms(cfg));
                                                                        history.set_prune_days(contents(dvw)[off_hq()..off_hq() + history.size()], config.history_days(cfg));
                                                                        audit.init(contents(dvw)[off_aud()..off_aud() + audit.size()], config.audit_log(cfg), config.audit_log_bytes(cfg), config.audit_log_files(cfg), 0);
                                                                        if config.pg_host_len(cfg) > 0 {
                                                                            pool.close(h, hpool);
                                                                            let fresh = pool.empty(h, history.lanes(), history.depth(), 1048576, 131072);
                                                                            let (made, rc) = history.configure(h, evlog.lend(lw), fresh, cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], config.pg_backoff_min_ms(cfg), config.pg_backoff_max_ms(cfg), config.pg_attempt_ms(cfg), config.pg_request_ms(cfg));
                                                                            hpool = made;
                                                                            history.enable(contents(dvw)[off_hq()..off_hq() + history.size()]);
                                                                        }
                                                                        borrow mut io as &!i in {
                                                                            io.error_all(i, "listening\n");
                                                                            // A torn tail cut at this start, or damage `repair-logs` cut, is said after `listening` (section 34.5).
                                                                            logguard.say_cut_events(i, report);
                                                                            logguard.say_cut(i, "delivery.seg", report[logguard.rep_size()..2 * logguard.rep_size()]);
                                                                        }
                                                                        borrow mut sgb as &!sgw in {
                                                                            sched.init(contents(sgw), config.cron_catchup(cfg), config.cron_seconds(cfg));
                                                                            borrow router as &r in {
                                                                                borrow clock as &c in {
                                                                                    borrow mut io as &!iw in {
                                                                                        // The claim on the two signals is made here, after the recovery (a stop during it is the default action, as it
                                                                                        // always was) and before anything else could start a thread (nothing does).
                                                                                        borrow stop as &sr in {
                                                                                            match signals_watch(sr) {
                                                                                                Watching::Ok(claim) => {
                                                                                                    borrow libc as &lb in {
                                                                                                        status = run(h, r, c, lh, lw, dl, buffer.room(wb), nn, contents(bw), contents(dvw), contents(ixw), contents(arw), contents(sgw), iw, hpool, claim, dir_buf[0..dir_len], config.stop_deadline_ms(cfg), cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg), lb, tls_ctx, ns, config.dns_port(cfg), config.tls_resume(cfg));
                                                                                                    }
                                                                                                }
                                                                                                Watching::Failed(e) => {
                                                                                                    say(iw, "hooks: SIGINT and SIGTERM cannot be claimed (errno ");
                                                                                                    ops.say_number(iw, e);
                                                                                                    say(iw, "): the service would not hear a request to stop\n");
                                                                                                    pool.close(h, hpool);
                                                                                                    log.close(dl);
                                                                                                    status = 5;
                                                                                                }
                                                                                            }
                                                                                        }
                                                                                    }
                                                                                }
                                                                            }
                                                                        }
                                                                        route.drop(h, router);
                                                                    }
                                                                    // (the context is freed by `attempt.close_tls`, at the end of `run`)
                                                                    listener_close(listener);
                                                                }
                                                                Listening::Failed(e) => {
                                                                    status = 11;
                                                                    log.close(dl);
                                                                    borrow libc as &lt in {
                                                                        tls.free_context(lt, tls_ctx);
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                fs = evlog.close(h, ev);
                buffer.drop(h, wbuf);
                unbox_slice(h, dvb);
                unbox_slice(h, blob);
                unbox_slice(h, ixb);
                unbox_slice(h, arenab);
                unbox_slice(h, sgb);
            }
        }
        if status == 40 || status == 41 || status == 42 {
            borrow mut io as &!i in {
                if status == 40 {
                    say(i, "hooks: the events log has a segment in a format this version does not understand (it understands format 2); nothing was changed\n");
                } else if status == 41 {
                    say(i, "hooks: delivery.seg is in a format this version does not understand (it understands format 2); nothing was changed\n");
                } else {
                    say(i, "hooks: the events log has a hole or a break in its chain of segments (events.first, events-N.seg); nothing was changed\n");
                }
            }
        }
    }
    release(libc);
    release(stop);
    release(fs);
    release(net);
    release(clock);
    release(args);
    release(io);
    release(heap);
    return status;
}
