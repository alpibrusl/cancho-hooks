edition 5;

// `hooks` -- a webhook delivery service (`docs/design.md`).
//
//     hooks <port> <data-dir>
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
import deliver;

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
fn handle[&h, &r, &q, &t, &p, &b, &l, &w, &c, &n](heap: &!h Heap, router: &r route.Router, request: &q [byte], table: &t [int], params: &!p [int], body: &b [byte], lg: &!l log.Log, window: &!w [byte], scratch: &!c [byte], note: &!n [int], out: buffer.Buffer) -> [heap, file_read, file_write] buffer.Buffer {
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
    return r;
}

// ---------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------

// The most events one turn delivers, so a long backlog does not starve the requests behind it.
fn most_per_turn() -> [] int {
    return 16;
}

// How long an attempt waits for the receiver's status line.
fn attempt_ms() -> [] int {
    return 2000;
}

// The pause after `fails` failures in a row: a placeholder (H1c is the real schedule).
fn pause_ms(fails: int) -> [] int {
    var p = 200 * fails;
    if p > 2000 {
        p = 2000;
    }
    return p;
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

// `POST /hook` with the event as the body. `webhook-id` is `evt_<id>`: the receiver's key for a repeat.
fn request_for[&h, &b](heap: &!h Heap, id: int, body: &b [byte]) -> [heap] buffer.Buffer {
    var q = buffer.append(heap, buffer.empty(heap, len(body) + 256), "POST /hook HTTP/1.1\r\nHost: receiver\r\nContent-Type: application/json\r\nwebhook-id: evt_");
    q = buffer.push_nat(heap, q, id);
    q = buffer.append(heap, q, "\r\nContent-Length: ");
    q = buffer.push_nat(heap, q, len(body));
    q = buffer.append(heap, q, "\r\nConnection: close\r\n\r\n");
    q = buffer.append(heap, q, body);
    return q;
}

// `st`: [0] the cursor (the last id delivered), [1] where in the events log the next undelivered record starts, [2] the
// time before which no attempt is made, [3] failures in a row, [4] attempts made, [5] deliveries recorded.
fn deliver_turn[&h, &l, &d, &w, &s, &n, &k, &x](heap: &!h Heap, lg: &!l log.Log, done: &!d log.Log, window: &!w [byte], st: &!s [int], net: &n Net(""), clock: &k Clock, host: &x [byte], port: int) -> [heap, file_read, file_write, net_out(""), conn_read, conn_write, poll, clock] int {
    var recorded = 0;
    var going = true;
    var turns = 0;
    region a {
        let scratch = alloc_slice[a](64, byte_of(0));
        while going && turns < most_per_turn() {
            turns = turns + 1;
            let now = clock_ms(clock);
            if now < st[2] {
                going = false;
            } else {
                let r = log.read_at(lg, st[1], window);
                if r.0 != 0 {
                    going = false;
                } else {
                    let id = record.ms_of(window, 0);
                    let p = record.pair_at(window, record.first_pair(0));
                    let request = request_for(heap, id, window[p.2..p.2 + p.3]);
                    var code = 0;
                    borrow request as &qb in {
                        code = deliver.attempt(net, host, port, buffer.bytes(qb), clock, attempt_ms());
                    }
                    buffer.drop(heap, request);
                    st[4] = st[4] + 1;
                    if code >= 200 && code < 300 {
                        let mark = record.begin(scratch, 0, id, 0, 1);
                        let end = record.put_pair(scratch, mark, "ok", "1");
                        let total = record.seal(scratch, 0, end);
                        if log.append(done, scratch[0..total], id, 0) == 0 {
                            st[0] = id;
                            st[1] = st[1] + r.1;
                            st[3] = 0;
                            st[5] = st[5] + 1;
                            recorded = recorded + 1;
                        } else {
                            going = false;
                        }
                    } else {
                        st[3] = st[3] + 1;
                        st[2] = clock_ms(clock) + pause_ms(st[3]);
                        going = false;
                    }
                }
            }
        }
    }
    if recorded > 0 {
        // The cursor is what a restart resumes from; a lost tail only means a repeat.
        log.flush(done);
    }
    return recorded;
}

// ---------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------

// Serve until killed. Each turn: `wait`, then every request that is ready. An accepted event is appended and its request
// *held*; after the turn's last request one `flush` covers every append of the turn, and then each held request is answered
// `202`. If the flush fails nothing is acknowledged: each gets a `503` and the log refuses everything after
// (`lexsys-log` design section 5).
fn run[&h, &r, &k, &l, &g, &d, &w, &n, &x](heap: &!h Heap, router: &r route.Router, clock: &k Clock, listener: &!l Listener, lg: &!g log.Log, done: &!d log.Log, window: &!w [byte], net: &n Net(""), host: &x [byte], port: int) -> [heap, conn_accept, conn_read, conn_write, poll, clock, file_read, file_write, net_out("")] int {
    match poller_new() {
        Polling::Ok(p) => {
            var srv = server.open(heap, p, listener, 16384, 0, 9);
            var widest = 1;
            if route.most_params(router) > 1 {
                widest = route.most_params(router);
            }
            let params = box_slice(heap, 2 * widest, 0);
            let scratch = box_slice(heap, 20480, byte_of(0));
            let note = box_slice(heap, 1, 0);
            // Delivery state (see `deliver_turn`).
            let state = box_slice(heap, 8, 0);
            borrow mut state as &!stw in {
                contents(stw)[0] = log.last_ms(done);
                if log.last_ms(done) < 0 {
                    contents(stw)[0] = 0;
                }
                contents(stw)[1] = seek_after(lg, window, contents(stw)[0]);
            }
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
                                        out = handle(heap, router, server.head(sr), server.parsed(sr), contents(pw), server.body(sr), lg, window, contents(cw), contents(nw), out);
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
                if port > 0 {
                    borrow mut state as &!stw in {
                        deliver_turn(heap, lg, done, window, contents(stw), net, clock, host, port);
                    }
                }
            }
            server.close(heap, srv);
            buffer.drop(heap, out);
            unbox_slice(heap, params);
            unbox_slice(heap, scratch);
            unbox_slice(heap, note);
            unbox_slice(heap, state);
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

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    var port = 0 - 1;
    var status = 2;
    var receiver_port = 0;
    region a {
        // The data directory, copied out of the argument borrow so it can be used after it.
        let dir_buf = alloc_slice[a](2048, byte_of(0));
        var dir_len = 0;
        // The receiver, if one is given: `<host> <port>` after the data directory.
        let host_buf = alloc_slice[a](256, byte_of(0));
        var host_len = 0;
        borrow args as &g in {
            if arg_count(g) > 1 {
                port = number_of(arg(g, 1));
            }
            if arg_count(g) > 4 {
                let hh = arg(g, 3);
                if len(hh) < 250 {
                    var k = 0;
                    while k < len(hh) {
                        host_buf[k] = hh[k];
                        k = k + 1;
                    }
                    host_len = len(hh);
                    receiver_port = number_of(arg(g, 4));
                }
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
        }
        if port > 0 && port < 65536 && dir_len > 0 {
            status = 3;
            borrow mut heap as &!h in {
                var wbuf = buffer.empty(h, max_len() + 4096);
                borrow mut wbuf as &!wb in {
                    borrow fs as &fsr in {
                        match open_log(fsr, dir_buf[0..dir_len], "events.seg", buffer.room(wb)) {
                            Opening::Failed(e) => {
                                status = 10;
                            }
                            Opening::Ok(lg0) => {
                                var lg = lg0;
                                match open_log(fsr, dir_buf[0..dir_len], "delivered.seg", buffer.room(wb)) {
                                    Opening::Failed(e) => {
                                        status = 12;
                                    }
                                    Opening::Ok(dl0) => {
                                        var dl = dl0;
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
                                                                borrow mut lg as &!lw in {
                                                                    borrow mut dl as &!dw in {
                                                                        status = run(h, r, c, lh, lw, dw, buffer.room(wb), nn, host_buf[0..host_len], receiver_port);
                                                                    }
                                                                }
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
                                        log.close(dl);
                                    }
                                }
                                log.close(lg);
                            }
                        }
                    }
                }
                buffer.drop(h, wbuf);
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
