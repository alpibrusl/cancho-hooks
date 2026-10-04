edition 5;

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
import history;
import view;
import queries;
import pg.pool;
import roster;
import record;
import attempt;
import crc;
import idem;
import std.conns;
import endpoints;
import config;
import sign;
import state;

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

// Open `<dir>/<name>` for appending and reading, recovering it first: the three-handle sequence `lexsys-log` describes.
fn open_log[&c, &d, &n, &w](fs: &c Fs(""), dir: &d [byte], name: &n [byte], window: &!w [byte]) -> [fs_read(""), fs_write(""), file_read, file_write] Opening {
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

// Is `body` a JSON object with a string `"type"`? Answers 0, or the reason as a static message (nonzero length).
fn invalid_event[&h, &b](heap: &!h Heap, body: &b [byte]) -> [heap] &static [byte] {
    var reason = "";
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
            }
        }
    }
    unbox_slice(heap, tape);
    return reason;
}

// The event with id `id`, as a JSON body `{"id":N,"event":<the stored object>}`, or an empty buffer if there is none.
// A linear scan: the ids are dense from 1 and the log keeps no index yet (`docs/design.md` section 4), which a service with
// a few thousand events can afford and a large one cannot; the index is the next thing the log needs.
fn find_event[&h, &l, &w](heap: &!h Heap, lg: &!l log.Log, window: &!w [byte], id: int) -> [heap, file_read] buffer.Buffer {
    var at = 0;
    var found = buffer.empty(heap, 0);
    var going = true;
    while going {
        let r = log.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else if record.ms_of(window, 0) == id {
            // The first pair's value is the stored body.
            let p = record.pair_at(window, record.first_pair(0));
            var w = json.writer(heap, r.1 + 32);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "id");
            w = json.put_int(heap, w, id);
            w = json.put_key(heap, w, "event");
            w = json.put_fragment(heap, w, window[p.2..p.2 + p.3]);
            w = json.end_object(heap, w);
            buffer.drop(heap, found);
            found = json.finish(w);
            going = false;
        } else {
            at = at + r.1;
        }
    }
    return found;
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

// Build the record of event `ms` in `scratch`: the body, and for a keyed event the key and the time `now` (Unix ms, eight bytes).
// Answers the record's length.
fn event_record[&c, &b, &k](scratch: &!c [byte], ms: int, body: &b [byte], key: &k [byte], keyed: bool, now: int) -> [] int {
    if !keyed {
        let p = record.begin(scratch, 0, ms, 0, 1);
        return record.seal(scratch, 0, record.put_pair(scratch, p, "event", body));
    }
    let p = record.begin(scratch, 0, ms, 0, 3);
    var end = record.put_pair(scratch, p, "event", body);
    end = record.put_pair(scratch, end, "key", key);
    region a {
        let stamp = alloc_slice[a](8, byte_of(0));
        record.put_u64(stamp, 0, now);
        end = record.put_pair(scratch, end, "t", stamp);
    }
    return record.seal(scratch, 0, end);
}

// One request, answered or noted for later. `note[0]` is set to the event's id if the request was an accepted
// `POST /events` (answer it after the flush, with `202`: a new event, or the one an earlier request with the same
// `Idempotency-Key` made), and to -1 otherwise (the answer in `out` goes out now). `now` is the Unix time in ms.
fn handle[&h, &r, &q, &t, &p, &b, &l, &w, &c, &n, &s, &x, &y, &z](heap: &!h Heap, router: &r route.Router, request: &q [byte], table: &t [int], params: &!p [int], body: &b [byte], lg: &!l log.Log, done: &!z log.Log, window: &!w [byte], scratch: &!c [byte], note: &!n [int], stats: &!s [int], ix: &!x [int], arena: &!y [byte], now: int, out: buffer.Buffer) -> [heap, file_read, file_write] buffer.Buffer {
    note[0] = 0 - 1;
    let keep = http.keeps_alive(table);
    let path = http.path(request, table);
    let id = route.find(router, http.method(request, table), path, params);
    if id == 1 {
        return server.reply(heap, out, 200, "{\"ok\":true}", keep);
    }
    if id == 2 {
        // POST /events
        let reason = invalid_event(heap, body);
        if len(reason) > 0 {
            return server.failure(heap, out, 422, reason, keep);
        }
        let kh = key_header(request, table);
        if kh == 0 - 2 {
            return server.failure(heap, out, 400, "more than one Idempotency-Key header", keep);
        }
        let keyed = kh >= 0;
        var key = request[0..0];
        var entry = 0 - 1;
        var sum = 0;
        if keyed {
            key = http.header_value(request, table, kh);
            if !idem.valid(key) {
                return server.failure(heap, out, 400, "the Idempotency-Key must be 1 to 255 visible ASCII characters", keep);
            }
            sum = crc.of(body);
            entry = idem.find(ix, arena, key);
            if entry >= 0 && idem.fresh(ix, entry, now) {
                if !idem.matches(ix, entry, sum, len(body)) {
                    return server.failure(heap, out, 422, "this Idempotency-Key was used for a different event", keep);
                }
                // The same event again: nothing is written, and the answer is the first one's, after the flush that covers it
                // (a broken log fails that flush, so the answer is then 503, as for any other request held).
                note[0] = idem.id_of(ix, entry);
                return out;
            }
            if entry < 0 && idem.count(ix) >= idem.capacity() {
                return server.failure(heap, out, 507, "too many Idempotency-Keys are held", keep);
            }
        }
        var ms = 1;
        if log.last_ms(lg) >= 1 {
            ms = log.last_ms(lg) + 1;
        }
        let total = event_record(scratch, ms, body, key, keyed, now);
        let code = log.append(lg, scratch[0..total], ms, 0);
        if code == log.too_long() {
            return server.failure(heap, out, 413, "the event is too large", keep);
        }
        if code != 0 {
            return server.failure(heap, out, 503, "the event could not be stored", keep);
        }
        if keyed {
            // Only now that the record is appended: the index never holds a key the log does not.
            if entry < 0 {
                entry = idem.add(ix, arena, key);
            }
            idem.set(ix, entry, ms, now, sum, len(body));
        }
        note[0] = ms;
        return out;
    }
    if id == 3 {
        // GET /events/:id
        let want = route.param_nat(path, params, 0);
        if want < 1 {
            return server.failure(heap, out, 400, "the id must be a positive number", keep);
        }
        let found = find_event(heap, lg, window, want);
        var answer = out;
        var empty = false;
        borrow found as &sz in {
            empty = buffer.size(sz) == 0;
        }
        if empty {
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
        var w = json.writer(heap, 320);
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
        w = json.put_key(heap, w, "history_live");
        w = json.put_int(heap, w, history.live(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_written");
        w = json.put_int(heap, w, history.written(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_failed");
        w = json.put_int(heap, w, history.failed(stats[off_hq()..off_hq() + history.size()]));
        w = json.put_key(heap, w, "history_dropped");
        w = json.put_int(heap, w, history.dropped(stats[off_hq()..off_hq() + history.size()]));
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
        if want < 0 || want >= state.max_endpoints() {
            return server.failure(heap, out, 400, "the id must be a number below 16", keep);
        }
        if index_of(stats, want) < 0 {
            return server.failure(heap, out, 404, "no such endpoint", keep);
        }
        if is_disabled(stats, want) {
            if note_endpoint(done, stats, state.enabled(), want) == 0 || log.flush(done) != 0 {
                return server.failure(heap, out, 503, "the change could not be stored", keep);
            }
            set_disabled(stats, want, false);
        }
        return server.reply(heap, out, 200, "{\"enabled\":true}", keep);
    }
    if id == 7 {
        // GET /endpoints: each endpoint's id, port, cursor (every event up to it is final) and whether it is disabled. Not the
        // host and not the secret.
        var w = json.writer(heap, 160);
        w = json.begin_array(heap, w);
        var i = 0;
        while i < stats[c_endpoints()] {
            let e = endpoints.id_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "id");
            w = json.put_int(heap, w, e);
            w = json.put_key(heap, w, "port");
            w = json.put_int(heap, w, endpoints.port_of(stats[off_table()..off_table() + endpoints.table_size()], i));
            w = json.put_key(heap, w, "cursor");
            w = json.put_int(heap, w, stats[off_cur() + e]);
            w = json.put_key(heap, w, "disabled");
            w = json.put_bool(heap, w, is_disabled(stats, e));
            w = json.end_object(heap, w);
            i = i + 1;
        }
        w = json.end_array(heap, w);
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
            if only < 0 || only >= state.max_endpoints() {
                return server.failure(heap, out, 400, "the endpoint must be a number below 16", keep);
            }
            if index_of(stats, only) < 0 {
                return server.failure(heap, out, 404, "no such endpoint", keep);
            }
        }
        let offset = find_offset(lg, window, want);
        if offset < 0 {
            return server.failure(heap, out, 404, "no such event", keep);
        }
        var needed = 0;
        var i = 0;
        while i < stats[c_endpoints()] {
            let e = endpoints.id_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if (only < 0 || e == only) && rp_find(stats, e, want) < 0 {
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
            let e = endpoints.id_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if only < 0 || e == only {
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
            let e = endpoints.id_of(stats[off_table()..off_table() + endpoints.table_size()], i);
            if only < 0 || e == only {
                rp_put(stats, e, want, offset);
                w = json.put_int(heap, w, e);
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
        if want > log.last_ms(lg) {
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
    return r;
}

// ---------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------

// The delivery state is one heap array of integers, `dv`, laid out as follows (`docs/design.md` section 15):
//
//     ctl      16    counters and the scan position (the `c_*` indices below)
//     table    96    the endpoints, six integers each (`endpoints.ls`)
//     cur      16    per endpoint id: every event up to this one is final
//     sched    17    the retry schedule: the number of delays, then the delays in ms
//     flying   16    per endpoint id: how many attempts are in flight
//     offs     1024  where in the events log each event of the window starts, by `id % window`
//     cells    ...   `state.cells(16)`: final / attempts / next attempt, per endpoint id and `id % window`

fn c_scanned() -> [] int {
    return 0;
}

fn c_offset() -> [] int {
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

// A bit per endpoint id (0 to 15): set while the endpoint is disabled and gets no new attempts (`docs/design.md` section 22).
fn c_disabled() -> [] int {
    return 10;
}

fn is_disabled[&d](dv: &d [int], e: int) -> [] bool {
    return dv[c_disabled()] >> e & 1 == 1;
}

fn set_disabled[&d](dv: &!d [int], e: int, on: bool) -> [] int {
    if on {
        dv[c_disabled()] = dv[c_disabled()] | 1 << e;
    } else {
        dv[c_disabled()] = dv[c_disabled()] & ~(1 << e);
    }
    return 0;
}

// Append an outcome record of any kind to `done`, not yet flushed. Answers 1 if it was appended, 0 if the log refused it.
fn note_outcome[&g, &d](done: &!g log.Log, dv: &!d [int], kind: int, e: int, id: int, attempts: int, next_at: int) -> [file_write] int {
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

// Append the record that endpoint `e` was disabled or enabled (`state.disabled()`, `state.enabled()`) to `done`, not yet
// flushed. Answers 1 if it was appended, 0 if the log refused it.
fn note_endpoint[&g, &d](done: &!g log.Log, dv: &!d [int], kind: int, e: int) -> [file_write] int {
    return note_outcome(done, dv, kind, e, 0, 0, 0);
}

fn off_table() -> [] int {
    return 16;
}

fn off_cur() -> [] int {
    return 112;
}

fn off_sched() -> [] int {
    return 128;
}

fn off_flying() -> [] int {
    return 145;
}

fn off_offs() -> [] int {
    return 161;
}

fn off_cells() -> [] int {
    return off_offs() + state.span();
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

fn dv_size() -> [] int {
    return off_hq() + history.size();
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

// The most attempts one endpoint has in flight together: a stalled endpoint holds this many slots and no more.
fn per_endpoint() -> [] int {
    return 8;
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

// The smallest cursor over the configured endpoints: events at or below it are final for all of them.
fn lowmark[&d](dv: &d [int]) -> [] int {
    var low = 0 - 1;
    var i = 0;
    while i < dv[c_endpoints()] {
        let e = dv[off_table() + i * endpoints.stride()];
        let c = dv[off_cur() + e];
        if low < 0 || c < low {
            low = c;
        }
        i = i + 1;
    }
    if low < 0 {
        return 0;
    }
    return low;
}

// The index in the table of the endpoint with id `e`, or -1.
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
// restart. Answers 0, or the number of records that were not outcomes (a log from something else), in which case the caller
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
            } else if o.0 >= state.replay() && o.0 <= state.replay_dead() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && index_of(dv, o.1) >= 0 {
                    recover_replay(dv, o.0, o.1, o.2, o.3, o.4);
                }
            } else if o.0 == state.disabled() || o.0 == state.enabled() {
                if o.1 >= 0 && o.1 < state.max_endpoints() && index_of(dv, o.1) >= 0 {
                    set_disabled(dv, o.1, o.0 == state.disabled());
                }
            } else if o.1 >= 0 && o.1 < state.max_endpoints() && index_of(dv, o.1) >= 0 {
                state.apply(dv[off_cells()..dv_size()], dv[off_cur()..off_cur() + state.max_endpoints()], o.1, o.0, o.2, o.3, o.4);
            }
            if record.ms_of(window, 0) >= dv[c_seq()] {
                dv[c_seq()] = record.ms_of(window, 0) + 1;
            }
            at = at + r.1;
        }
    }
    return odd;
}

// Read the events log forward until the window of the slowest endpoint is covered, noting where each event starts.
fn extend_scan[&l, &w, &d](lg: &!l log.Log, window: &!w [byte], dv: &!d [int]) -> [file_read] int {
    let limit = lowmark(dv) + state.span();
    var going = true;
    while going && dv[c_scanned()] < limit {
        let r = log.read_at(lg, dv[c_offset()], window);
        if r.0 != 0 {
            going = false;
        } else {
            let id = record.ms_of(window, 0);
            dv[off_offs() + id % state.span()] = dv[c_offset()];
            dv[c_scanned()] = id;
            dv[c_offset()] = dv[c_offset()] + r.1;
        }
    }
    return 0;
}

// The offset of the first record whose id is above `cursor`, or the end of what can be read.
fn seek_after[&l, &w](lg: &!l log.Log, window: &!w [byte], cursor: int) -> [file_read] int {
    var at = 0;
    var going = true;
    while going {
        let r = log.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else if record.ms_of(window, 0) > cursor {
            going = false;
        } else {
            at = at + r.1;
        }
    }
    return at;
}

// `POST /hook` with the event as the body and the three Standard Webhooks headers. `webhook-id` is `evt_<id>`, the same for
// every attempt at the event, so a receiver can drop a repeat.
fn request_for[&h, &b, &k](heap: &!h Heap, id: int, body: &b [byte], key: &k [byte], seconds: int) -> [heap] buffer.Buffer {
    region a {
        let msg_id = alloc_slice[a](24, byte_of(0));
        msg_id[0] = byte_of('e');
        msg_id[1] = byte_of('v');
        msg_id[2] = byte_of('t');
        msg_id[3] = byte_of('_');
        let id_len = 4 + sign.nat_text(id, msg_id[4..24]);
        let stamp = alloc_slice[a](24, byte_of(0));
        let stamp_len = sign.nat_text(seconds, stamp);
        let sig = alloc_slice[a](48, byte_of(0));
        sign.signature(heap, key, msg_id[0..id_len], stamp[0..stamp_len], body, sig);
        var q = buffer.append(heap, buffer.empty(heap, len(body) + 384), "POST /hook HTTP/1.1\r\nHost: receiver\r\nContent-Type: application/json\r\nwebhook-id: ");
        q = buffer.append(heap, q, msg_id[0..id_len]);
        q = buffer.append(heap, q, "\r\nwebhook-timestamp: ");
        q = buffer.append(heap, q, stamp[0..stamp_len]);
        q = buffer.append(heap, q, "\r\nwebhook-signature: ");
        q = buffer.append(heap, q, sig[0..47]);
        q = buffer.append(heap, q, "\r\nContent-Length: ");
        q = buffer.push_nat(heap, q, len(body));
        q = buffer.append(heap, q, "\r\nConnection: close\r\n\r\n");
        q = buffer.append(heap, q, body);
        return q;
    }
}

// An attempt at a replay ended (`docs/design.md` section 23): the same rules as a window's attempt (a `2xx` delivers, a `410` kills
// the event and disables the endpoint, the schedule running out kills it, anything else waits for the next delay), recorded
// under the kinds of a replay. Answers 1 if an outcome record was appended.
fn finish_replay[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, id: int, code: int, latency: int) -> [file_write, clock] int {
    dv[c_attempts()] = dv[c_attempts()] + 1;
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
    } else {
        next_at = clock_unix_ms(clock) + dv[off_sched() + tries];
    }
    var ok = 0;
    if note_outcome(done, dv, kind, e, id, tries, next_at) == 1 {
        ok = 1;
        if kind == state.replay_failed() {
            dv[b + 3] = tries;
            dv[b + 4] = next_at;
            dv[c_failed()] = dv[c_failed()] + 1;
        } else {
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
        history.push(dv[off_hq()..off_hq() + history.size()], e, id, 1, tries, outcome, code, clock_unix_ms(clock), latency);
    }
    if ok == 1 && code == 410 && !is_disabled(dv, e) {
        if note_endpoint(done, dv, state.disabled(), e) == 1 {
            set_disabled(dv, e, true);
        }
    }
    return ok;
}

// The offset in the events log of event `id`, or -1 if there is no such event.
fn find_offset[&l, &w](lg: &!l log.Log, window: &!w [byte], id: int) -> [file_read] int {
    var at = 0;
    var going = true;
    while going {
        let r = log.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else if record.ms_of(window, 0) == id {
            return at;
        } else if record.ms_of(window, 0) > id {
            going = false;
        } else {
            at = at + r.1;
        }
    }
    return 0 - 1;
}

// An attempt ended: `code` is what `attempt` answered (an HTTP status, or a negative reason). Count it, write its outcome to
// `done` (not yet flushed), apply it to the cells, and free the event to be tried again when its time comes. Answers 1 if an
// outcome record was appended, 0 if the log refused it (then the cells are left alone and a restart repeats the attempt).
fn finish_attempt[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, id: int, code: int, latency: int) -> [file_write, clock] int {
    if id >= replay_base() {
        return finish_replay(done, dv, clock, e, id - replay_base(), code, latency);
    }
    dv[c_attempts()] = dv[c_attempts()] + 1;
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
    } else {
        next_at = clock_unix_ms(clock) + dv[off_sched() + tries];
    }
    var ok = 0;
    region a {
        let rec = alloc_slice[a](128, byte_of(0));
        let total = state.put_outcome(rec, 0, dv[c_seq()], kind, e, id, tries, next_at);
        if log.append(done, rec[0..total], dv[c_seq()], 0) == 0 {
            dv[c_seq()] = dv[c_seq()] + 1;
            state.apply(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, kind, id, tries, next_at);
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
        history.push(dv[off_hq()..off_hq() + history.size()], e, id, 0, tries, kind, code, clock_unix_ms(clock), latency);
    }
    if ok == 1 && code == 410 && !is_disabled(dv, e) {
        if note_endpoint(done, dv, state.disabled(), e) == 1 {
            set_disabled(dv, e, true);
        }
    }
    return ok;
}

// The poller woke the attempt in `slot`: move it along, and if it ended, free its slot and record how. Answers 1 if an outcome
// was written.
fn settle[&g, &d, &k, &t, &p, &a, &r, &s](done: &!g log.Log, dv: &!d [int], clock: &k Clock, atab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], slot: int, token0: int) -> [file_write, clock, conn_read, conn_write, poll] int {
    let code = attempt.advance(atab, poller, at, req, resp, slot, token0);
    if code == attempt.pending() {
        return 0;
    }
    let e = attempt.endpoint_of(at, slot);
    let id = attempt.event_of(at, slot);
    let latency = clock_ms(clock) - (attempt.deadline_of(at, slot) - dv[c_deadline()]);
    attempt.finish(atab, at, slot);
    return finish_attempt(done, dv, clock, e, id, code, latency);
}

// End every attempt that has run past its deadline, as a timeout. Answers how many outcomes were written.
fn sweep[&g, &d, &k, &t, &a](done: &!g log.Log, dv: &!d [int], clock: &k Clock, atab: &!t conns.Table, at: &!a [int]) -> [file_write, clock] int {
    let now = clock_ms(clock);
    var written = 0;
    var slot = 0;
    while slot < attempt.slots() {
        if attempt.expired(at, slot, now) {
            let e = attempt.endpoint_of(at, slot);
            let id = attempt.event_of(at, slot);
            let latency = now - (attempt.deadline_of(at, slot) - dv[c_deadline()]);
            attempt.finish(atab, at, slot);
            written = written + finish_attempt(done, dv, clock, e, id, attempt.timed_out(), latency);
        }
        slot = slot + 1;
    }
    return written;
}

// Start an attempt at event `id` for the endpoint in table slot `i`. Answers the table and 1 if an outcome was written at once
// (the connection failed before it began), else 0.
fn start_one[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r](heap: &!h Heap, lg: &!l log.Log, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], atab: conns.Table, i: int, id: int, token0: int) -> [heap, file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
    let r0 = log.read_at(lg, dv[off_offs() + id % state.span()], window);
    if r0.0 != 0 || record.ms_of(window, 0) != id {
        return (atab, 0);
    }
    let p = record.pair_at(window, record.first_pair(0));
    let e = endpoints.id_of(dv[off_table()..off_table() + endpoints.table_size()], i);
    let request = request_for(heap, id, window[p.2..p.2 + p.3], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), clock_unix_ms(clock) / 1000);
    var table = atab;
    var started = 0 - 1;
    var code = attempt.no_connect();
    borrow request as &qb in {
        let (grown, slot, answer) = attempt.begin(heap, table, poller, net, endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i), buffer.bytes(qb), at, req, token0, e, id, clock_ms(clock) + dv[c_deadline()]);
        table = grown;
        started = slot;
        code = answer;
    }
    buffer.drop(heap, request);
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
fn start_replay[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &q](heap: &!h Heap, lg: &!l log.Log, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!q [byte], atab: conns.Table, i: int, r: int, token0: int) -> [heap, file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
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
    let r0 = log.read_at(lg, dv[base + 5], window);
    if r0.0 != 0 || record.ms_of(window, 0) != id {
        return (atab, finish_replay(done, dv, clock, e, id, attempt.no_connect(), 0));
    }
    let p = record.pair_at(window, record.first_pair(0));
    let request = request_for(heap, id, window[p.2..p.2 + p.3], endpoints.key_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), clock_unix_ms(clock) / 1000);
    var table = atab;
    var started = 0 - 1;
    var code = attempt.no_connect();
    borrow request as &qb in {
        let (grown, slot, answer) = attempt.begin(heap, table, poller, net, endpoints.host_of(dv[off_table()..off_table() + endpoints.table_size()], blob, i), endpoints.port_of(dv[off_table()..off_table() + endpoints.table_size()], i), buffer.bytes(qb), at, req, token0, e, id + replay_base(), clock_ms(clock) + dv[c_deadline()]);
        table = grown;
        started = slot;
        code = answer;
    }
    buffer.drop(heap, request);
    if started >= 0 {
        return (table, 0);
    }
    return (table, finish_replay(done, dv, clock, e, id, code, 0));
}

// Start attempts: for each endpoint in turn, starting from a different one each time, every event in its window that is not
// final, not in flight and whose time has come gets one, up to `most_starts()` in all and `per_endpoint()` in flight for each
// endpoint. Answers the table and how many outcomes were written at once.
fn start_attempts[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r](heap: &!h Heap, lg: &!l log.Log, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], atab: conns.Table, token0: int) -> [heap, file_read, file_write, net_out(""), poll, clock] (conns.Table, int) {
    let count = dv[c_endpoints()];
    extend_scan(lg, window, dv);
    var table = atab;
    var budget = most_starts();
    var written = 0;
    var turn = dv[c_turn()];
    dv[c_turn()] = turn + 1;
    var step = 0;
    while step < count && budget > 0 {
        let i = (turn + step) % count;
        let e = dv[off_table() + i * endpoints.stride()];
        let now = clock_unix_ms(clock);
        var id = dv[off_cur() + e] + 1;
        while budget > 0 && !is_disabled(dv, e) && id <= dv[c_scanned()] && state.in_window(dv[off_cur()..off_cur() + state.max_endpoints()], e, id) && dv[off_flying() + e] < per_endpoint() {
            if !state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) && dv[flight_at(e, id)] == 0 && state.next_at(dv[off_cells()..off_flight()], e, id) <= now {
                budget = budget - 1;
                let (grown, w) = start_one(heap, lg, done, window, dv, blob, net, clock, poller, at, req, table, i, id, token0);
                table = grown;
                written = written + w;
            }
            id = id + 1;
        }
        step = step + 1;
    }
    // Then the replays that are due, with what is left of the budget.
    let now = clock_unix_ms(clock);
    var rr = 0;
    while rr < rp_cap() && budget > 0 {
        let base = off_rp() + rr * rp_stride();
        if dv[base] == 1 && dv[base + 6] == 0 && dv[base + 4] <= now && !is_disabled(dv, dv[base + 1]) && dv[off_flying() + dv[base + 1]] < per_endpoint() {
            let i = index_of(dv, dv[base + 1]);
            if i >= 0 {
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
fn delivery_turn[&h, &l, &g, &w, &d, &b, &n, &k, &p, &a, &r, &s, &e](heap: &!h Heap, lg: &!l log.Log, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &b [byte], net: &n Net(""), clock: &k Clock, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], ev: &e [int], nev: int, token0: int, atab: conns.Table) -> [heap, file_read, file_write, net_out(""), conn_read, conn_write, poll, clock] (conns.Table, int) {
    var table = atab;
    var written = 0;
    var j = 0;
    while j < nev {
        let slot = ev[2 * j] - token0;
        if attempt.busy(at, slot) {
            borrow mut table as &!tw in {
                written = written + settle(done, dv, clock, tw, poller, at, req, resp, slot, token0);
            }
        }
        j = j + 1;
    }
    borrow mut table as &!tw in {
        written = written + sweep(done, dv, clock, tw, at);
    }
    let (grown, started) = start_attempts(heap, lg, done, window, dv, blob, net, clock, poller, at, req, table, token0);
    written = written + started;
    if written > 0 {
        // What a restart resumes from; a lost tail only means a repeat.
        log.flush(done);
    }
    return (grown, written);
}

// ---------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------

// Serve until killed. Each turn: `wait`, then every request that is ready. An accepted event is appended and its request
// *held*; after the turn's last request one `flush` covers every append of the turn, and then each held request is answered
// `202`. If the flush fails nothing is acknowledged: each gets a `503` and the log refuses everything after
// (`lexsys-log` design section 5).
fn run[&h, &r, &k, &l, &g, &d, &w, &n, &x, &v, &i, &a](heap: &!h Heap, router: &r route.Router, clock: &k Clock, listener: &!l Listener, lg: &!g log.Log, done: &!d log.Log, window: &!w [byte], net: &n Net(""), blob: &x [byte], dv: &!v [int], ix: &!i [int], arena: &!a [byte], pl0: pool.Pool) -> [heap, conn_accept, conn_read, conn_write, poll, clock, file_read, file_write, net_out("")] int {
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
            let note = box_slice(heap, 2, 0);
            // The requests for the history that wait for the database: per slot, the pool's tag (0 for a free slot), the ticket of the
            // held connection, whether to keep it alive, and when to give up.
            let pq = box_slice(heap, pq_cap() * 4, 0);
            var next_tag = history.query_base();
            // The delivery attempts in flight: their state, request and response bytes, the poller events that concern them, and
            // their connections.
            let at = box_slice(heap, attempt.at_size(), 0);
            let req = box_slice(heap, attempt.req_size(), byte_of(0));
            let resp = box_slice(heap, attempt.resp_size(), byte_of(0));
            let events = box_slice(heap, 256, 0);
            var atab = conns.empty(heap, attempt.slots());
            let tickets = box_slice(heap, most_held(), 0);
            let ids = box_slice(heap, most_held(), 0);
            let keeps = box_slice(heap, most_held(), 0);
            var out = buffer.empty(heap, 4096);
            while true {
                srv = server.wait(heap, srv, clock, listener, 50);
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
                        borrow srv as &sr in {
                            borrow mut params as &!pw in {
                                borrow mut scratch as &!cw in {
                                    borrow mut note as &!nw in {
                                        out = handle(heap, router, server.head(sr), server.parsed(sr), contents(pw), server.body(sr), lg, done, window, contents(cw), contents(nw), dv[0..dv_size()], ix, arena, clock_unix_ms(clock), out);
                                    }
                                }
                            }
                            if !http.keeps_alive(server.parsed(sr)) {
                                keep = 0;
                            }
                        }
                        var accepted = 0 - 1;
                        var asked = 0 - 1;
                        borrow note as &nr in {
                            accepted = contents(nr)[0];
                            if contents(nr)[0] == 0 - 2 {
                                asked = contents(nr)[1];
                            }
                        }
                        if asked >= 0 {
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
                                contents(kw)[held] = keep;
                            }
                            held = held + 1;
                        } else {
                            borrow mut srv as &!sw in {
                                borrow out as &ob in {
                                    server.respond(sw, buffer.bytes(ob));
                                }
                            }
                        }
                    }
                }
                if held > 0 {
                    // One flush for the turn, then the acknowledgements.
                    let stored = log.flush(lg);
                    var n = 0;
                    while n < held {
                        var ticket = 0;
                        var id = 0;
                        var keep_alive = true;
                        borrow tickets as &tr in {
                            ticket = contents(tr)[n];
                        }
                        borrow ids as &ir in {
                            id = contents(ir)[n];
                        }
                        borrow keeps as &kr in {
                            keep_alive = contents(kr)[n] == 1;
                        }
                        var resp = buffer.empty(heap, 256);
                        buffer.drop(heap, out);
                        if stored == 0 {
                            let body = id_body(heap, id);
                            borrow body as &bb in {
                                resp = server.reply(heap, resp, 202, buffer.bytes(bb), keep_alive);
                            }
                            buffer.drop(heap, body);
                        } else {
                            resp = server.failure(heap, resp, 503, "the event could not be stored", keep_alive);
                        }
                        borrow mut srv as &!sw in {
                            borrow resp as &ab in {
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
                if dv[c_endpoints()] > 0 {
                    borrow mut srv as &!sw in {
                        borrow mut at as &!aw in {
                            borrow mut req as &!qw in {
                                borrow mut resp as &!pw in {
                                    borrow events as &er in {
                                        let (grown, written) = delivery_turn(heap, lg, done, window, dv, blob, net, clock, server.poller(sw), contents(aw), contents(qw), contents(pw), contents(er), nev, token0, atab);
                                        atab = grown;
                                    }
                                }
                            }
                        }
                    }
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
                                    }
                                    j = j + 1;
                                }
                            }
                            // every request the pool has an answer for: an insert is counted, a request for the API is answered
                            var tag = pool.next_done(qw);
                            while tag >= 0 {
                                if tag < history.query_base() {
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
                                        server.answer(sw, ticket, buffer.bytes(lb));
                                    }
                                    buffer.drop(heap, late);
                                    borrow mut pq as &!pw in {
                                        contents(pw)[4 * q] = 0;
                                    }
                                }
                                q = q + 1;
                            }
                            history.drain(heap, qw, dv[off_hq()..off_hq() + history.size()], 64);
                            pool.flush(qw, server.poller(sw));
                        }
                    }
                }
            }
            pool.close(heap, pl);
            conns.drop(heap, atab);
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
            return 0;
        }
        Polling::Failed(e) => {
            pool.close(heap, pl0);
            return 4;
        }
    }
}

// Read `<dir>/endpoints.conf` into `out` (16 KiB). Answers the number of bytes, 0 if there is no such file (the service then only
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
                if got >= 0 && got < 16384 {
                    return got;
                }
                return 0 - 1000;
            }
        }
    }
}

// Read `<dir>/endpoints.conf` into the delivery state. Answers the number of endpoints, 0 if there is no such file (the service
// then only ingests), or a negative number: `0 - line` for the first bad line, -1000 if the file cannot be read or is too large.
fn load_endpoints[&h, &c, &d, &v, &b](heap: &!h Heap, fs: &c Fs(""), dir: &d [byte], dv: &!v [int], blob: &!b [byte]) -> [heap, fs_read(""), file_read] int {
    let text = box_slice(heap, 16384, byte_of(0));
    var result = 0 - 1000;
    borrow mut text as &!tw in {
        let got = read_endpoints_file(fs, dir, contents(tw));
        if got >= 0 {
            result = endpoints.parse(contents(tw)[0..got], dv[off_table()..off_table() + endpoints.table_size()], blob);
        }
    }
    unbox_slice(heap, text);
    return result;
}

// Rebuild the idempotency index from the events log: every keyed record, in order, the later one of two with the same key
// winning (as it does when the service is running). Answers 0, or a status for `main` to exit with: the log has a record this
// code does not understand, or more keys than the index holds.
fn rebuild[&g, &w, &x, &y](lg: &!g log.Log, window: &!w [byte], ix: &!x [int], arena: &!y [byte]) -> [file_read] int {
    var at = 0;
    while true {
        let r = log.read_at(lg, at, window);
        if r.0 == 1 {
            return 0;
        }
        if r.0 != 0 {
            return 16;
        }
        if record.fields_of(window, 0) >= 3 {
            let ev = record.pair_at(window, record.first_pair(0));
            let k = record.pair_at(window, ev.4);
            let t = record.pair_at(window, k.4);
            if k.1 != 3 || window[k.0] != byte_of('k') || t.1 != 1 || window[t.0] != byte_of('t') || t.3 != 8 {
                return 16;
            }
            let key = window[k.2..k.2 + k.3];
            var entry = idem.find(ix, arena, key);
            if entry < 0 {
                entry = idem.add(ix, arena, key);
            }
            if entry < 0 {
                return 16;
            }
            idem.set(ix, entry, record.ms_of(window, 0), record.get_u64(window, t.2), crc.of(window[ev.2..ev.2 + ev.3]), ev.3);
        }
        at = at + r.1;
    }
    return 0;
}

// Everything delivery needs before the loop starts: the schedule, the endpoints, the outcomes of earlier runs replayed, and the
// scan of the events log positioned at the slowest endpoint. Answers 0, or a status for `main` to exit with.
fn prepare[&h, &c, &d, &g, &l, &w, &v, &b, &t, &x, &y, &e](heap: &!h Heap, fs: &c Fs(""), dir: &d [byte], lg: &!g log.Log, done: &!l log.Log, window: &!w [byte], dv: &!v [int], blob: &!b [byte], schedule: &t [byte], deadline_ms: int, ix: &!x [int], arena: &!y [byte], etext: &e [byte], from_db: bool) -> [heap, fs_read(""), file_read] int {
    default_schedule(dv[off_sched()..off_sched() + 17]);
    dv[c_deadline()] = default_deadline_ms();
    if deadline_ms > 0 {
        dv[c_deadline()] = deadline_ms;
    }
    if len(schedule) > 0 && parse_schedule(schedule, dv[off_sched()..off_sched() + 17]) < 0 {
        return 14;
    }
    let rebuilt = rebuild(lg, window, ix, arena);
    if rebuilt != 0 {
        return rebuilt;
    }
    // The endpoints: the database's, as `roster.fetch` wrote them, or `endpoints.conf`.
    var n = 0;
    if from_db {
        n = endpoints.parse(etext, dv[off_table()..off_table() + endpoints.table_size()], blob);
    } else {
        n = load_endpoints(heap, fs, dir, dv, blob);
    }
    if n < 0 {
        return 13;
    }
    dv[c_endpoints()] = n;
    if n > 0 {
        if replay(done, window, dv) > 0 {
            return 15;
        }
        if dv[c_seq()] < 1 {
            dv[c_seq()] = 1;
        }
        let low = lowmark(dv);
        dv[c_scanned()] = low;
        dv[c_offset()] = seek_after(lg, window, low);
    }
    return 0;
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

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    var port = 0 - 1;
    var status = 2;
    region a {
        // The settings (`src/config.ls`): the defaults, then the file named by `--config`, then the flags.
        let cfg = alloc_slice[a](config.size(), 0);
        let cblob = alloc_slice[a](config.blob_size(), byte_of(0));
        let cpath = alloc_slice[a](4096, byte_of(0));
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
                    say(i, arg(g, code / 8));
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
        let etext = alloc_slice[a](16384, byte_of(0));
        var etext_n = 0;
        var from_db = false;
        var go = bad == 0 && dir_len > 0;
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
                let scratch = alloc_slice[a](endpoints.table_size(), 0);
                let sblob = alloc_slice[a](16384, byte_of(0));
                var text_len = 0;
                borrow fs as &fs0 in {
                    text_len = read_endpoints_file(fs0, cblob[0..dir_len], etext);
                }
                if text_len <= 0 {
                    borrow mut io as &!i in {
                        say(i, "hooks: no endpoints.conf to import in --dir\n");
                    }
                } else {
                    let count = endpoints.parse(etext[0..text_len], scratch, sblob);
                    if count < 0 {
                        borrow mut io as &!i in {
                            let nb = alloc_slice[a](12, byte_of(0));
                            say(i, "hooks: endpoints.conf: line ");
                            say(i, nb[0..digits_of(0 - count, nb)]);
                            say(i, " is not valid; nothing was imported\n");
                        }
                        status = 13;
                    } else {
                        var added = 0 - 1;
                        borrow mut heap as &!h0 in {
                            borrow net as &nn0 in {
                                borrow fs as &fs0 in {
                                    added = roster.copy_in(h0, nn0, cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg), cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], fs0, etext[0..text_len]);
                                }
                            }
                        }
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
        }
        if go && config.pg_host_len(cfg) > 0 {
            var got = 0 - 1;
            borrow mut heap as &!h0 in {
                borrow net as &nn0 in {
                    borrow fs as &fs0 in {
                        got = roster.fetch(h0, nn0, cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg), cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], fs0, etext);
                    }
                }
            }
            if got < 0 {
                go = false;
                status = 20;
                borrow mut io as &!i in {
                    say(i, "hooks: the database's endpoints cannot be read: ");
                    if got == 0 - 1 {
                        say(i, "cannot connect\n");
                    } else if got == 0 - 2 {
                        say(i, "cannot log in\n");
                    } else if got == 0 - 3 {
                        say(i, "the query failed (is the endpoints table there? sql/schema.sql)\n");
                    } else if got == 0 - 4 {
                        say(i, "a row has an empty field or a byte that is not printable\n");
                    } else {
                        say(i, "the table is too large (the service reads at most 16 KiB of it)\n");
                    }
                }
            } else {
                etext_n = got;
                from_db = true;
                let scratch = alloc_slice[a](endpoints.table_size(), 0);
                let sblob = alloc_slice[a](16384, byte_of(0));
                let count = endpoints.parse(etext[0..etext_n], scratch, sblob);
                if count < 0 {
                    go = false;
                    status = 13;
                    borrow mut io as &!i in {
                        let nb = alloc_slice[a](12, byte_of(0));
                        say(i, "hooks: the endpoints table: row ");
                        say(i, nb[0..digits_of(0 - count, nb)]);
                        say(i, " is not valid (an id of 16 or more, a repeated id, a port, or a secret that is not whsec_ and base64)\n");
                    }
                }
            }
        }
        if go && port > 0 && port < 65536 && dir_len > 0 {
            status = 3;
            borrow mut heap as &!h in {
                var wbuf = buffer.empty(h, max_len() + 4096);
                let dvb = box_slice(h, dv_size(), 0);
                let blob = box_slice(h, 16384, byte_of(0));
                let ixb = box_slice(h, idem.ix_size(), 0);
                let arenab = box_slice(h, idem.arena_size(), byte_of(0));
                borrow mut wbuf as &!wb in {
                    borrow fs as &fsr in {
                        match open_log(fsr, dir_buf[0..dir_len], "events.seg", buffer.room(wb)) {
                            Opening::Failed(e) => {
                                status = 10;
                            }
                            Opening::Ok(lg0) => {
                                var lg = lg0;
                                match open_log(fsr, dir_buf[0..dir_len], "delivery.seg", buffer.room(wb)) {
                                    Opening::Failed(e) => {
                                        status = 12;
                                    }
                                    Opening::Ok(dl0) => {
                                        var dl = dl0;
                                        borrow mut dvb as &!dvw in {
                                            borrow mut blob as &!bw in {
                                                borrow mut ixb as &!ixw in {
                                                    borrow mut arenab as &!arw in {
                                                        borrow mut lg as &!lw in {
                                                            borrow mut dl as &!dw in {
                                                                contents(ixw)[1] = window_ms;
                                                                status = prepare(h, fsr, dir_buf[0..dir_len], lw, dw, buffer.room(wb), contents(dvw), contents(bw), sched_buf[0..sched_len], deadline_ms, contents(ixw), contents(arw), etext[0..etext_n], from_db);
                                                                if status == 0 {
                                                                    borrow net as &nn in {
                                                                        match tcp_listen(nn, port, 1024, 0) {
                                                                            Listening::Ok(l) => {
                                                                                var listener = l;
                                                                                borrow mut listener as &!lh in {
                                                                                    listener_nonblocking(lh);
                                                                                    let router = routes(h);
                                                                                    // The database for the history, if one was named: connect and log in here, before the loop, and go on
                                                                                    // without it if it is not there (delivery does not depend on it; `docs/design.md` section 24).
                                                                                    var hpool = pool.empty(h, 1, 1, 4096, 4096);
                                                                                    if config.pg_host_len(cfg) > 0 {
                                                                                        let (opened, lanes) = history.open(h, nn, cblob[config.pg_host_at()..config.pg_host_at() + config.pg_host_len(cfg)], config.pg_port(cfg), cblob[config.pg_user_at()..config.pg_user_at() + config.pg_user_len(cfg)], cblob[config.pg_password_at()..config.pg_password_at() + config.pg_password_len(cfg)], cblob[config.pg_database_at()..config.pg_database_at() + config.pg_database_len(cfg)], 2, fsr);
                                                                                        pool.close(h, hpool);
                                                                                        hpool = opened;
                                                                                        history.enable(contents(dvw)[off_hq()..off_hq() + history.size()], lanes);
                                                                                        if lanes < 2 {
                                                                                            borrow mut io as &!i in {
                                                                                                let nb = alloc_slice[a](12, byte_of(0));
                                                                                                say(i, "hooks: the database: ");
                                                                                                say(i, nb[0..digits_of(lanes, nb)]);
                                                                                                say(i, " of 2 connections opened; history is written over those\n");
                                                                                            }
                                                                                        }
                                                                                    }
                                                                                    borrow mut io as &!i in {
                                                                                        io.error_all(i, "listening\n");
                                                                                    }
                                                                                    borrow router as &r in {
                                                                                        borrow clock as &c in {
                                                                                            status = run(h, r, c, lh, lw, dw, buffer.room(wb), nn, contents(bw), contents(dvw), contents(ixw), contents(arw), hpool);
                                                                                        }
                                                                                    }
                                                                                    route.drop(h, router);
                                                                                }
                                                                                listener_close(listener);
                                                                            }
                                                                            Listening::Failed(e) => {
                                                                                status = 11;
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
                                        log.close(dl);
                                    }
                                }
                                log.close(lg);
                            }
                        }
                    }
                }
                buffer.drop(h, wbuf);
                unbox_slice(h, dvb);
                unbox_slice(h, blob);
                unbox_slice(h, ixb);
                unbox_slice(h, arenab);
            }
        }
    }
    release(fs);
    release(net);
    release(clock);
    release(args);
    release(io);
    release(heap);
    return status;
}
