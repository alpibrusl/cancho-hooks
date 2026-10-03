edition 5;

// `hooks` -- a webhook delivery service (`docs/design.md`).
//
//     hooks <port> <data-dir> [<retry delays in ms, comma separated> [<attempt deadline in ms>]]
//
// This is step H1a: **ingest**. `POST /events` takes a JSON object with a string `"type"`, appends it to a durable log
// (`lexsys-log`'s `log.ls`), and answers `202` with its id **only after the flush that covers it**. Requests that arrive in
// the same turn of the loop share one flush (group commit), which is the whole reason the server holds a request and
// answers it later. `GET /events/:id` reads one back, and `GET /healthz` says the service is up.
//
// **Step H1b: delivery to one fixed receiver** (`hooks <port> <data-dir> <receiver-host> <receiver-port>`). After each turn the
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
import std.http;
import std.io;
import std.json;
import std.route;
import http.server;
import log;
import record;
import attempt;
import std.conns;
import endpoints;
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

// One request, answered or noted for later. `note[0]` is set to the new event's id if the request was an accepted
// `POST /events` (answer it after the flush, with `202`), and to -1 otherwise (the answer in `out` goes out now).
fn handle[&h, &r, &q, &t, &p, &b, &l, &w, &c, &n, &s](heap: &!h Heap, router: &r route.Router, request: &q [byte], table: &t [int], params: &!p [int], body: &b [byte], lg: &!l log.Log, window: &!w [byte], scratch: &!c [byte], note: &!n [int], stats: &s [int], out: buffer.Buffer) -> [heap, file_read, file_write] buffer.Buffer {
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
        var ms = 1;
        if log.last_ms(lg) >= 1 {
            ms = log.last_ms(lg) + 1;
        }
        let mut_pos = record.begin(scratch, 0, ms, 0, 1);
        let end = record.put_pair(scratch, mut_pos, "event", body);
        let total = record.seal(scratch, 0, end);
        let code = log.append(lg, scratch[0..total], ms, 0);
        if code == log.too_long() {
            return server.failure(heap, out, 413, "the event is too large", keep);
        }
        if code != 0 {
            return server.failure(heap, out, 503, "the event could not be stored", keep);
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
        var w = json.writer(heap, 128);
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
        w = json.end_object(heap, w);
        let body = json.finish(w);
        var answer = out;
        borrow body as &sb in {
            answer = server.reply(heap, answer, 200, buffer.bytes(sb), keep);
        }
        buffer.drop(heap, body);
        return answer;
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
    return 145 + state.span();
}

// One flag per cell: is an attempt at this (endpoint, event) in flight? (An event with one is not started again.)
fn off_flight() -> [] int {
    return off_cells() + state.cells(state.max_endpoints());
}

fn dv_size() -> [] int {
    return off_flight() + state.max_endpoints() * state.span();
}

fn flight_at(e: int, id: int) -> [] int {
    return off_flight() + e * state.span() + id % state.span();
}

// The most attempts one turn starts, so a long backlog does not starve the requests behind it.
fn most_starts() -> [] int {
    return 16;
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

// An attempt ended: `code` is what `attempt` answered (an HTTP status, or a negative reason). Count it, write its outcome to
// `done` (not yet flushed), apply it to the cells, and free the event to be tried again when its time comes. Answers 1 if an
// outcome record was appended, 0 if the log refused it (then the cells are left alone and a restart repeats the attempt).
fn finish_attempt[&g, &d, &k](done: &!g log.Log, dv: &!d [int], clock: &k Clock, e: int, id: int, code: int) -> [file_write, clock] int {
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
    } else if tries >= dv[off_sched()] + 1 {
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
    attempt.finish(atab, at, slot);
    return finish_attempt(done, dv, clock, e, id, code);
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
            attempt.finish(atab, at, slot);
            written = written + finish_attempt(done, dv, clock, e, id, attempt.timed_out());
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
    return (table, finish_attempt(done, dv, clock, e, id, code));
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
        while budget > 0 && id <= dv[c_scanned()] && state.in_window(dv[off_cur()..off_cur() + state.max_endpoints()], e, id) && dv[off_flying() + e] < per_endpoint() {
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
fn run[&h, &r, &k, &l, &g, &d, &w, &n, &x, &v](heap: &!h Heap, router: &r route.Router, clock: &k Clock, listener: &!l Listener, lg: &!g log.Log, done: &!d log.Log, window: &!w [byte], net: &n Net(""), blob: &x [byte], dv: &!v [int]) -> [heap, conn_accept, conn_read, conn_write, poll, clock, file_read, file_write, net_out("")] int {
    match poller_new() {
        Polling::Ok(p) => {
            var srv = server.open(heap, p, listener, max_len() + 4096, 0, 9);
            var widest = 1;
            if route.most_params(router) > 1 {
                widest = route.most_params(router);
            }
            let params = box_slice(heap, 2 * widest, 0);
            let scratch = box_slice(heap, max_len() + 256, byte_of(0));
            let note = box_slice(heap, 1, 0);
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
                                        out = handle(heap, router, server.head(sr), server.parsed(sr), contents(pw), server.body(sr), lg, window, contents(cw), contents(nw), dv[0..16], out);
                                    }
                                }
                            }
                            if !http.keeps_alive(server.parsed(sr)) {
                                keep = 0;
                            }
                        }
                        var accepted = 0 - 1;
                        borrow note as &nr in {
                            accepted = contents(nr)[0];
                        }
                        if accepted >= 0 && held < most_held() {
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
                if dv[c_endpoints()] > 0 {
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
            }
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
            unbox_slice(heap, tickets);
            unbox_slice(heap, ids);
            unbox_slice(heap, keeps);
            return 0;
        }
        Polling::Failed(e) => {
            return 4;
        }
    }
}

// Read `<dir>/endpoints.conf` into the delivery state. Answers the number of endpoints, 0 if there is no such file (the service
// then only ingests), or a negative number: `0 - line` for the first bad line, -1000 if the file cannot be read or is too large.
fn load_endpoints[&h, &c, &d, &v, &b](heap: &!h Heap, fs: &c Fs(""), dir: &d [byte], dv: &!v [int], blob: &!b [byte]) -> [heap, fs_read(""), file_read] int {
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
                let text = box_slice(heap, 16384, byte_of(0));
                var got = 0 - 1;
                borrow mut rd as &!rh in {
                    borrow mut text as &!tw in {
                        match file_pread(rh, 0, contents(tw)) {
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
                }
                file_close(rd);
                var result = 0 - 1000;
                if got >= 0 && got < 16384 {
                    borrow text as &tr in {
                        result = endpoints.parse(contents(tr)[0..got], dv[off_table()..off_table() + endpoints.table_size()], blob);
                    }
                }
                unbox_slice(heap, text);
                return result;
            }
        }
    }
}

// Everything delivery needs before the loop starts: the schedule, the endpoints, the outcomes of earlier runs replayed, and the
// scan of the events log positioned at the slowest endpoint. Answers 0, or a status for `main` to exit with.
fn prepare[&h, &c, &d, &g, &l, &w, &v, &b, &t](heap: &!h Heap, fs: &c Fs(""), dir: &d [byte], lg: &!g log.Log, done: &!l log.Log, window: &!w [byte], dv: &!v [int], blob: &!b [byte], schedule: &t [byte], deadline_ms: int) -> [heap, fs_read(""), file_read] int {
    default_schedule(dv[off_sched()..off_sched() + 17]);
    dv[c_deadline()] = default_deadline_ms();
    if deadline_ms > 0 {
        dv[c_deadline()] = deadline_ms;
    }
    if len(schedule) > 0 && parse_schedule(schedule, dv[off_sched()..off_sched() + 17]) < 0 {
        return 14;
    }
    let n = load_endpoints(heap, fs, dir, dv, blob);
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

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    var port = 0 - 1;
    var status = 2;
    region a {
        // The data directory and the retry schedule, copied out of the argument borrow so they can be used after it.
        let dir_buf = alloc_slice[a](2048, byte_of(0));
        var dir_len = 0;
        let sched_buf = alloc_slice[a](256, byte_of(0));
        var sched_len = 0;
        var deadline_ms = 0;
        borrow args as &g in {
            if arg_count(g) > 1 {
                port = number_of(arg(g, 1));
            }
            if arg_count(g) > 2 {
                let d = arg(g, 2);
                if len(d) < 2000 {
                    var k = 0;
                    while k < len(d) {
                        dir_buf[k] = d[k];
                        k = k + 1;
                    }
                    dir_len = len(d);
                }
            }
            if arg_count(g) > 3 {
                let sc = arg(g, 3);
                if len(sc) < 250 {
                    var k = 0;
                    while k < len(sc) {
                        sched_buf[k] = sc[k];
                        k = k + 1;
                    }
                    sched_len = len(sc);
                }
            }
            if arg_count(g) > 4 {
                deadline_ms = number_of(arg(g, 4));
            }
        }
        if port > 0 && port < 65536 && dir_len > 0 {
            status = 3;
            borrow mut heap as &!h in {
                var wbuf = buffer.empty(h, max_len() + 4096);
                let dvb = box_slice(h, dv_size(), 0);
                let blob = box_slice(h, 16384, byte_of(0));
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
                                                borrow mut lg as &!lw in {
                                                    borrow mut dl as &!dw in {
                                                        status = prepare(h, fsr, dir_buf[0..dir_len], lw, dw, buffer.room(wb), contents(dvw), contents(bw), sched_buf[0..sched_len], deadline_ms);
                                                        if status == 0 {
                                                            borrow net as &nn in {
                                                                match tcp_listen(nn, port, 1024, 0) {
                                                                    Listening::Ok(l) => {
                                                                        var listener = l;
                                                                        borrow mut listener as &!lh in {
                                                                            listener_nonblocking(lh);
                                                                            let router = routes(h);
                                                                            borrow mut io as &!i in {
                                                                                io.error_all(i, "listening\n");
                                                                            }
                                                                            borrow router as &r in {
                                                                                borrow clock as &c in {
                                                                                    status = run(h, r, c, lh, lw, dw, buffer.room(wb), nn, contents(bw), contents(dvw));
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
