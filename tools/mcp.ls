edition 6;

// `hooks-mcp` -- an MCP (Model Context Protocol) server for a running hooks service, over standard input and output (`docs/design.md` section 51, `docs/agents.md`).
//
//     hooks-mcp [--url http://HOST:PORT] [--token-file PATH] [--allow-write] [--allow-remote-plaintext] [--timeout-seconds N]
//
// One JSON-RPC 2.0 message to a line on standard input, one reply to a line on standard output, nothing else on standard output. It speaks to the service's HTTP API with
// a plain TCP client of lex-sys's own built-ins (`tcp_connect_start`, a `Poller` and the clock for the time limit; HTTP/1.1, `Connection: close`, no TLS) and answers `initialize`,
// `ping`, `tools/list` and `tools/call`; every other notification is ignored and every other request is `-32601`. The tools that only read are always listed; the ones that change
// something (post an event, replay, take a replay back) are not even listed unless `--allow-write` is given. Not exposed at all, by choice: erasing events, making, changing or
// removing endpoints (their secrets are in the answers), schedules, `/config` and `/metrics`.
//
// The bearer token is read from a file (never from the command line), is sent only in the `Authorization` header of a request to the configured host, and is never written
// anywhere else: not to standard output, not to standard error, not into an error text. A token goes to a host that is not this machine only with
// `--allow-remote-plaintext`, because the connection has no TLS.
//
// The program holds no foreign function and no `Ffi`: its authority is the console (`io_read`, `io_write`, `err_write`), the network (`net_out`, with the host not bounded: it is an
// argument), the clock and a poller (the time limit of a call), reading the token file (`fs_read`, the path likewise an argument), and the two that nothing here can do without
// (`heap`, `args`). `lex-sys authority` says so, and `scripts/check-authority.sh --mcp` pins it (`docs/authority-mcp.json`).

import std.buffer;
import std.bytes;
import std.http;
import std.io;
import std.json;

// ---------------------------------------------------------------------
// The limits
// ---------------------------------------------------------------------

// The longest message accepted, in bytes: a longer line is read through and answered `-32600` (it cannot be parsed, so its id is not known).
fn max_line() -> [] int {
    return 1048576;
}

// The largest answer read from the service: a larger one is a tool error, not a truncated answer.
fn max_response() -> [] int {
    return 4194304;
}

// The largest integer a JSON number can carry exactly (2^53 - 1): every id and offset is within it.
// The default time one call to the service may take, in all (connecting, sending, waiting for and reading the answer), in seconds; `--timeout-seconds` changes it.
fn default_timeout() -> [] int {
    return 30;
}

fn max_number() -> [] int {
    return 9007199254740991;
}

// Nesting of the `fields` of an event: the writer of `std.json` traps past 60, and a message must not be able to make the server trap.
fn max_nesting() -> [] int {
    return 16;
}

fn version() -> [] &static [byte] {
    return "0.1.0";
}

// ---------------------------------------------------------------------
// Bytes into a fixed slice: the request line and the headers that come from arguments
// ---------------------------------------------------------------------

fn put[&o, &r](out: &!o [byte], at: int, text: &r [byte]) -> [] int {
    if at < 0 || at + len(text) > len(out) {
        return 0 - 1;
    }
    var i = 0;
    while i < len(text) {
        out[at + i] = text[i];
        i = i + 1;
    }
    return at + len(text);
}

// A non-negative integer in decimal. The slices it writes into are far larger than twenty digits.
fn put_nat[&o](out: &!o [byte], at: int, n: int) -> [] int {
    var here = at;
    if n >= 10 {
        here = put_nat(out, here, n / 10);
    }
    if here < 0 || here >= len(out) {
        return 0 - 1;
    }
    out[here] = byte_of('0' + n % 10);
    return here + 1;
}

// ---------------------------------------------------------------------
// Standard input and standard output
// ---------------------------------------------------------------------

// One line of standard input into `line` (emptied first). Answers it and how it ended:
//     0 a line         1 the end of input, nothing read       2 a line longer than `max_line` (read through and dropped)
//     3 a line, then the end of input (no newline at the end)      4 a line too long, then the end of input
fn read_line[&h, &i](heap: &!h Heap, io: &!i Io, line: buffer.Buffer) -> [heap, io_read] (buffer.Buffer, int) {
    var b = line;
    borrow mut b as &!bb in {
        buffer.clear(bb);
    }
    var count = 0;
    var over = false;
    var status = 0 - 1;
    while status < 0 {
        let c = getchar(io);
        if c < 0 {
            if over {
                status = 4;
            } else if count > 0 {
                status = 3;
            } else {
                status = 1;
            }
        } else if c == 10 {
            if over {
                status = 2;
            } else {
                status = 0;
            }
        } else if count < max_line() {
            b = buffer.push(heap, b, byte_of(c));
            count = count + 1;
        } else {
            over = true;
        }
    }
    return (b, status);
}

// One reply and its newline, flushed: a client waits for it. 0, or -1 if standard output cannot be written (the client is gone).
fn emit[&i, &r](io: &!i Io, text: &r [byte]) -> [io_write] int {
    io.write_all(io, text);
    io.write_all(io, "\n");
    match flush_out(io) {
        Done::Ok(n) => {
            return 0;
        }
        Done::Failed(e) => {
            return 0 - 1;
        }
    }
}

fn say[&i, &r](io: &!i Io, text: &r [byte]) -> [err_write] int {
    return io.error_all(io, text);
}

// ---------------------------------------------------------------------
// Replies
// ---------------------------------------------------------------------

// The text of the id of a request, as it was written, for the reply: a string with its quotes or a number. `-1` (and an id that is not either) is `null`.
fn put_id[&h, &s, &t](heap: &!h Heap, out: buffer.Buffer, src: &s [byte], tape: &t [int], idn: int) -> [heap] buffer.Buffer {
    let k = json.kind(tape, idn);
    if k == 5 {
        return buffer.append(heap, out, src[tape[3 * idn + 1] - 1..tape[3 * idn + 2] + 1]);
    }
    if k == 3 || k == 4 {
        return buffer.append(heap, out, src[tape[3 * idn + 1]..tape[3 * idn + 2]]);
    }
    return buffer.append(heap, out, "null");
}

fn put_code[&h](heap: &!h Heap, out: buffer.Buffer, code: int) -> [heap] buffer.Buffer {
    if code < 0 {
        return buffer.push_nat(heap, buffer.push(heap, out, byte_of('-')), 0 - code);
    }
    return buffer.push_nat(heap, out, code);
}

// `{"jsonrpc":"2.0","id":ID,"result":RESULT}`; or, when `code` is not 0, the error with that code, and `result` is its message (made of this program's own words, argument names
// and numbers, never of what a client or the service sent; it is written as a JSON string all the same).
fn reply[&h, &s, &t](heap: &!h Heap, src: &s [byte], tape: &t [int], idn: int, result: buffer.Buffer, code: int) -> [heap] buffer.Buffer {
    var out = buffer.empty(heap, 256);
    out = buffer.append(heap, out, "{\"jsonrpc\":\"2.0\",\"id\":");
    out = put_id(heap, out, src, tape, idn);
    if code == 0 {
        out = buffer.append(heap, out, ",\"result\":");
        borrow result as &rr in {
            out = buffer.append(heap, out, buffer.bytes(rr));
        }
    } else {
        out = buffer.append(heap, out, ",\"error\":{\"code\":");
        out = put_code(heap, out, code);
        out = buffer.append(heap, out, ",\"message\":");
        var w = json.writer(heap, 128);
        borrow result as &rr in {
            w = json.put_string(heap, w, buffer.bytes(rr));
        }
        let quoted = json.finish(w);
        borrow quoted as &qq in {
            out = buffer.append(heap, out, buffer.bytes(qq));
        }
        buffer.drop(heap, quoted);
        out = buffer.append(heap, out, "}");
    }
    buffer.drop(heap, result);
    return buffer.append(heap, out, "}");
}

// A message (or a small result) made of a literal.
fn text_of[&h, &m](heap: &!h Heap, text: &m [byte]) -> [heap] buffer.Buffer {
    return buffer.append(heap, buffer.empty(heap, len(text) + 1), text);
}

// A reply with the id `null`, for a message that could not be read at all.
fn reply_null[&h](heap: &!h Heap, result: buffer.Buffer, code: int) -> [heap] buffer.Buffer {
    let t = box_slice(heap, 3, 0);
    var out = buffer.empty(heap, 1);
    borrow t as &tr in {
        buffer.drop(heap, out);
        out = reply(heap, "", contents(tr), 0 - 1, result, code);
    }
    unbox_slice(heap, t);
    return out;
}

fn nothing[&h](heap: &!h Heap) -> [heap] buffer.Buffer {
    return buffer.empty(heap, 1);
}

// ---------------------------------------------------------------------
// Looking at the arguments of a call
// ---------------------------------------------------------------------

// Whether every key of the object at `obj` is one of the comma separated names in `allowed`, and none is there twice. An object that is not one has no keys; a key with an escape is not any of them.
fn keys_ok[&s, &t, &a](src: &s [byte], tape: &t [int], obj: int, allowed: &a [byte]) -> [] bool {
    if json.kind(tape, obj) != 7 {
        return true;
    }
    var j = obj + 1;
    var left = json.count(tape, obj);
    while left > 0 {
        var found = false;
        if json.string_plain(tape, j) {
            let key = json.string_view(src, tape, j);
            var n = 1;
            let names = bytes.count_byte(allowed, ',') + 1;
            while n <= names {
                if bytes.equal(bytes.field(allowed, ',', n), key) {
                    found = true;
                }
                n = n + 1;
            }
        }
        if !found {
            return false;
        }
        // The same key twice is not an argument that can be read one way.
        var before = obj + 1;
        while before < j {
            if json.string_plain(tape, before) && bytes.equal(json.string_view(src, tape, before), json.string_view(src, tape, j)) {
                return false;
            }
            before = json.skip(tape, before + 1);
        }
        j = json.skip(tape, j + 1);
        left = left - 1;
    }
    return true;
}

// An integer argument. -1 if there is none, -2 if it is not an integer in `low..=high` (a float, a string, too large, too small), else the value.
fn int_arg[&s, &t, &k](src: &s [byte], tape: &t [int], args: int, key: &k [byte], low: int, high: int) -> [] int {
    let n = json.get(src, tape, args, key);
    if n < 0 {
        return 0 - 1;
    }
    if !json.fits_int(src, tape, n) {
        return 0 - 2;
    }
    let v = json.to_int(src, tape, n);
    if v < low || v > high {
        return 0 - 2;
    }
    return v;
}

// A string argument, decoded into `out`. -1 if there is none, -2 if it is not a string or decodes to more than `most` bytes (or than `out` holds), else its length.
fn str_arg[&s, &t, &k, &o](src: &s [byte], tape: &t [int], args: int, key: &k [byte], out: &!o [byte], most: int) -> [] int {
    let n = json.get(src, tape, args, key);
    if n < 0 {
        return 0 - 1;
    }
    if !json.is_string(tape, n) {
        return 0 - 2;
    }
    let size = json.string_length(src, tape, n);
    if size < 0 || size > most || size > len(out) {
        return 0 - 2;
    }
    return json.string_into(src, tape, n, out);
}

// An event type or any other name: visible characters and the space, no control character (the parser has already seen to the encoding).
fn plain_text[&r](text: &r [byte]) -> [] bool {
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if c < 32 || c == 127 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// An idempotency key: 1 to 255 visible ASCII characters, which is what the service takes and also all that can safely be a header's value.
fn key_text[&r](text: &r [byte]) -> [] bool {
    if len(text) < 1 || len(text) > 255 {
        return false;
    }
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if c < 33 || c > 126 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// How deeply the value at node `i` is nested: 0 for a scalar.
fn depth_of[&t](tape: &t [int], i: int) -> [] int {
    let k = json.kind(tape, i);
    if k != 6 && k != 7 {
        return 0;
    }
    var deepest = 0;
    var j = i + 1;
    var left = json.count(tape, i);
    while left > 0 {
        if k == 7 {
            j = j + 1;
        }
        let d = depth_of(tape, j);
        if d > deepest {
            deepest = d;
        }
        j = json.skip(tape, j);
        left = left - 1;
    }
    return deepest + 1;
}

// An object key into the writer, decoded.
fn put_key_of[&h, &s, &t](heap: &!h Heap, w: json.Writer, src: &s [byte], tape: &t [int], i: int) -> [heap] json.Writer {
    var out = w;
    if json.string_plain(tape, i) {
        out = json.put_key(heap, out, json.string_view(src, tape, i));
    } else {
        let room = box_slice(heap, json.string_length(src, tape, i) + 1, byte_of(0));
        borrow mut room as &!rw in {
            let r = contents(rw);
            let n = json.string_into(src, tape, i, r);
            out = json.put_key(heap, out, r[0..n]);
        }
        unbox_slice(heap, room);
    }
    return out;
}

// The value at node `i` into the writer, as it was written. (A number or a string is a fragment of the source, which the writer checks again.)
fn copy_value[&h, &s, &t](heap: &!h Heap, w: json.Writer, src: &s [byte], tape: &t [int], i: int) -> [heap] json.Writer {
    var out = w;
    let k = json.kind(tape, i);
    if k == 0 {
        return json.put_null(heap, out);
    }
    if k == 1 {
        return json.put_bool(heap, out, false);
    }
    if k == 2 {
        return json.put_bool(heap, out, true);
    }
    if k == 3 || k == 4 {
        return json.put_fragment(heap, out, src[tape[3 * i + 1]..tape[3 * i + 2]]);
    }
    if k == 5 {
        return json.put_fragment(heap, out, src[tape[3 * i + 1] - 1..tape[3 * i + 2] + 1]);
    }
    var j = i + 1;
    var left = json.count(tape, i);
    if k == 6 {
        out = json.begin_array(heap, out);
        while left > 0 {
            out = copy_value(heap, out, src, tape, j);
            j = json.skip(tape, j);
            left = left - 1;
        }
        return json.end_array(heap, out);
    }
    out = json.begin_object(heap, out);
    while left > 0 {
        out = put_key_of(heap, out, src, tape, j);
        out = copy_value(heap, out, src, tape, j + 1);
        j = json.skip(tape, j + 1);
        left = left - 1;
    }
    return json.end_object(heap, out);
}

// Whether the object at node `i` has a key `type`, however it is written.
fn has_type_key[&s, &t](src: &s [byte], tape: &t [int], i: int) -> [] bool {
    var j = i + 1;
    var left = json.count(tape, i);
    while left > 0 {
        if json.string_equals(src, tape, j, "type") {
            return true;
        }
        j = json.skip(tape, j + 1);
        left = left - 1;
    }
    return false;
}

// ---------------------------------------------------------------------
// The tools
// ---------------------------------------------------------------------
//
//     1 hooks_health   2 hooks_stats   3 hooks_get_event   4 hooks_list_endpoints   5 hooks_list_dead_letters   6 hooks_get_attempts          (read only: always listed)
//     7 hooks_post_event   8 hooks_replay_event   9 hooks_replay_dead_letters   10 hooks_cancel_replay                                       (only with `--allow-write`)

fn last_read_tool() -> [] int {
    return 6;
}

fn last_tool() -> [] int {
    return 10;
}

fn tool_name(tool: int) -> [] &static [byte] {
    if tool == 1 {
        return "hooks_health";
    }
    if tool == 2 {
        return "hooks_stats";
    }
    if tool == 3 {
        return "hooks_get_event";
    }
    if tool == 4 {
        return "hooks_list_endpoints";
    }
    if tool == 5 {
        return "hooks_list_dead_letters";
    }
    if tool == 6 {
        return "hooks_get_attempts";
    }
    if tool == 7 {
        return "hooks_post_event";
    }
    if tool == 8 {
        return "hooks_replay_event";
    }
    if tool == 9 {
        return "hooks_replay_dead_letters";
    }
    return "hooks_cancel_replay";
}

// The tool the string at node `n` names, or 0.
fn tool_of[&s, &t](src: &s [byte], tape: &t [int], n: int) -> [] int {
    var tool = 1;
    while tool <= last_tool() {
        if json.string_equals(src, tape, n, tool_name(tool)) {
            return tool;
        }
        tool = tool + 1;
    }
    return 0;
}

// The tool's definition, as `tools/list` gives it (the schema is JSON Schema, and `additionalProperties` is false: a call that names anything else is refused).
fn tool_json(tool: int) -> [] &static [byte] {
    if tool == 1 {
        return "{\"name\":\"hooks_health\",\"description\":\"Check whether the hooks service is ready to take and deliver events (GET /readyz). Answers {\\\"ready\\\":true}; when it is not ready the call is an error that names the failing check (events_log, delivery_log, data_dir, database or stopping).\",\"inputSchema\":{\"type\":\"object\",\"properties\":{},\"required\":[],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 2 {
        return "{\"name\":\"hooks_stats\",\"description\":\"Counters of the running hooks service since it started (GET /stats): endpoints, attempts, delivered, failed, dead letters, replays waiting, circuit breaker trips, history and retention figures.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{},\"required\":[],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 3 {
        return "{\"name\":\"hooks_get_event\",\"description\":\"Read one stored event (GET /events/{id}): the JSON that was posted. Error 404 means that id was never given; 410 means retention dropped it or it was erased.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"event_id\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"The event id (a positive integer).\",\"maximum\":9007199254740991}},\"required\":[\"event_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 4 {
        return "{\"name\":\"hooks_list_endpoints\",\"description\":\"List the delivery endpoints (GET /endpoints): id, port, scheme, cursor (the next event to send), disabled and paused flags, event type patterns, header names and limits. Never a host, a secret or a header value. Paged: limit 1 to 256 (64 by default) from offset; the answer is the page.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"limit\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"Endpoints per page, 1 to 256 (default 64).\",\"maximum\":256},\"offset\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"Where the page starts, in table order (default 0).\",\"maximum\":9007199254740991}},\"required\":[],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 5 {
        return "{\"name\":\"hooks_list_dead_letters\",\"description\":\"List the dead letters of one endpoint (GET /endpoints/{id}/dead): events whose retries ran out. Each has event id, type, attempts, reason, time of death and whether a replay is waiting. Paged: pass the answer's next as after to continue.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"endpoint_id\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"The endpoint id (a non-negative integer: the first endpoint is 0), as listed by hooks_list_endpoints.\",\"maximum\":9007199254740991},\"limit\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"Dead letters per page, 1 to 1000.\",\"maximum\":1000},\"order\":{\"type\":\"string\",\"enum\":[\"asc\",\"desc\"],\"description\":\"desc (the default) is newest first, asc oldest first.\"},\"after\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"An event id: the page starts after it (use the previous answer's next).\",\"maximum\":9007199254740991}},\"required\":[\"endpoint_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 6 {
        return "{\"name\":\"hooks_get_attempts\",\"description\":\"The delivery attempts of one event (GET /events/{id}/attempts), from the database: endpoint, replay, attempt number, outcome, HTTP status, why a failed attempt failed, time and latency in ms. Error 503 when the service has no database.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"event_id\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"The event id (a positive integer).\",\"maximum\":9007199254740991}},\"required\":[\"event_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":true,\"openWorldHint\":false}}";
    }
    if tool == 7 {
        return "{\"name\":\"hooks_post_event\",\"description\":\"Post a new event (POST /events). It is delivered to every endpoint that subscribes to its type, so this has effects outside the service. The event is {\\\"type\\\": type} plus the members of fields. Answers 202 and the new event's id. With idempotency_key a second post of the same key does not make a second event (422 if it is for a different event).\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"type\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":200,\"description\":\"The event type, for example order.created.\"},\"fields\":{\"type\":\"object\",\"description\":\"The other members of the event: any JSON object, without a type member.\"},\"idempotency_key\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":255,\"description\":\"1 to 255 visible ASCII characters, no spaces.\"}},\"required\":[\"type\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":false,\"openWorldHint\":false,\"destructiveHint\":false}}";
    }
    if tool == 8 {
        return "{\"name\":\"hooks_replay_event\",\"description\":\"Send a stored event again (POST /events/{id}/replay): to every endpoint that subscribes to its type, or only to endpoint_id, whatever that endpoint subscribes to. The receiver sees the same webhook-id. Answers 202; error 507 when 32 replays already wait.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"event_id\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"The event id (a positive integer).\",\"maximum\":9007199254740991},\"endpoint_id\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"The endpoint id (a non-negative integer: the first endpoint is 0), as listed by hooks_list_endpoints.\",\"maximum\":9007199254740991}},\"required\":[\"event_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":false,\"openWorldHint\":false,\"destructiveHint\":false}}";
    }
    if tool == 9 {
        return "{\"name\":\"hooks_replay_dead_letters\",\"description\":\"Send an endpoint's dead letters again, oldest first (POST /endpoints/{id}/replay-dead), as many as the 32 waiting replays have room for. Answers taken, remaining and next; call again until remaining is 0.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"endpoint_id\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"The endpoint id (a non-negative integer: the first endpoint is 0), as listed by hooks_list_endpoints.\",\"maximum\":9007199254740991},\"limit\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"At most this many, 1 to 2048.\",\"maximum\":2048},\"types\":{\"type\":\"array\",\"items\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":200},\"minItems\":1,\"maxItems\":32,\"description\":\"Only dead letters of these event types.\"},\"after\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"An event id: only dead letters after it.\",\"maximum\":9007199254740991}},\"required\":[\"endpoint_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":false,\"openWorldHint\":false,\"destructiveHint\":false}}";
    }
    if tool == 10 {
        return "{\"name\":\"hooks_cancel_replay\",\"description\":\"Take back a replay of an event for an endpoint that has not been sent yet (DELETE /events/{id}/replay/{endpoint}). Error 409 while an attempt of it is on the wire, 404 when none waits.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"event_id\":{\"type\":\"integer\",\"minimum\":1,\"description\":\"The event id (a positive integer).\",\"maximum\":9007199254740991},\"endpoint_id\":{\"type\":\"integer\",\"minimum\":0,\"description\":\"The endpoint id (a non-negative integer: the first endpoint is 0), as listed by hooks_list_endpoints.\",\"maximum\":9007199254740991}},\"required\":[\"event_id\",\"endpoint_id\"],\"additionalProperties\":false},\"annotations\":{\"readOnlyHint\":false,\"openWorldHint\":false,\"destructiveHint\":false}}";
    }
    return "";
}

// The names an argument object may have, comma separated, for each tool (`"\n"`, which no key can be, for none).
fn tool_args(tool: int) -> [] &static [byte] {
    if tool == 3 || tool == 6 {
        return "event_id";
    }
    if tool == 4 {
        return "limit,offset";
    }
    if tool == 5 {
        return "endpoint_id,limit,order,after";
    }
    if tool == 7 {
        return "type,fields,idempotency_key";
    }
    if tool == 8 || tool == 10 {
        return "event_id,endpoint_id";
    }
    if tool == 9 {
        return "endpoint_id,limit,types,after";
    }
    return "\n";
}

fn method_of(tool: int) -> [] &static [byte] {
    if tool <= 6 {
        return "GET";
    }
    if tool == 10 {
        return "DELETE";
    }
    return "POST";
}

// ---------------------------------------------------------------------
// Validating a call and making the path, the header and the body of the request from it
// ---------------------------------------------------------------------
//
// Nothing is sent before every argument has been judged, and the path is made from integers that were judged and from literals: no text of a client is ever put into it. A refusal
// is a message written into `path` and its length answered (the error is `-32602`).

// `Invalid params: NAME PROBLEM`, into `out`; its length.
fn fail[&o, &n, &p](out: &!o [byte], name: &n [byte], problem: &p [byte]) -> [] int {
    var at = put(out, 0, "Invalid params: ");
    at = put(out, at, name);
    return put(out, at, problem);
}

// An integer argument in `low..=high`: (its value or -1 if there is none, the length of a refusal written into `out` or 0). A required one that is missing is a refusal.
fn int_or_fail[&s, &t, &k, &o](src: &s [byte], tape: &t [int], args: int, key: &k [byte], low: int, high: int, required: bool, out: &!o [byte]) -> [] (int, int) {
    let v = int_arg(src, tape, args, key, low, high);
    if v == 0 - 1 {
        if required {
            return (0 - 1, fail(out, key, " is required"));
        }
        return (0 - 1, 0);
    }
    if v == 0 - 2 {
        var at = fail(out, key, " must be an integer from ");
        at = put_nat(out, at, low);
        at = put(out, at, " to ");
        at = put_nat(out, at, high);
        return (0 - 1, at);
    }
    return (v, 0);
}

fn has_query[&o](out: &!o [byte], at: int) -> [] bool {
    var i = 0;
    while i < at {
        if int_of(out[i]) == '?' {
            return true;
        }
        i = i + 1;
    }
    return false;
}

// The `?` that starts a query, or the `&` that goes between two.
fn add_sep[&o](out: &!o [byte], at: int) -> [] int {
    if has_query(out, at) {
        return put(out, at, "&");
    }
    return put(out, at, "?");
}

// `name=value` in the query, when there is a value.
fn add_nat[&o, &n](out: &!o [byte], at: int, name: &n [byte], v: int) -> [] int {
    if v < 0 {
        return at;
    }
    var here = add_sep(out, at);
    here = put(out, here, name);
    here = put(out, here, "=");
    return put_nat(out, here, v);
}

fn refused[&h](heap: &!h Heap, n: int) -> [heap] (int, int, buffer.Buffer, int) {
    return (n, 0, nothing(heap), 0 - 32602);
}

fn planned[&h](heap: &!h Heap, path_len: int) -> [heap] (int, int, buffer.Buffer, int) {
    return (path_len, 0, nothing(heap), 0);
}

// What the call asks of the service: the path (and query) into `path`, the value of an `Idempotency-Key` header into `idem`, and a body. Answers (the path's length, or a refusal's;
// the key's length; the body, empty when there is none; 0 or -32602).
fn plan[&h, &s, &t, &p, &q](heap: &!h Heap, tool: int, src: &s [byte], tape: &t [int], args: int, path: &!p [byte], idem: &!q [byte]) -> [heap] (int, int, buffer.Buffer, int) {
    if !keys_ok(src, tape, args, tool_args(tool)) {
        var at = put(path, 0, "Invalid params: an argument is unknown or given twice; this tool takes ");
        if tool <= 2 {
            at = put(path, at, "none");
        } else {
            at = put(path, at, tool_args(tool));
        }
        return refused(heap, at);
    }
    if tool == 1 {
        return planned(heap, put(path, 0, "/readyz"));
    }
    if tool == 2 {
        return planned(heap, put(path, 0, "/stats"));
    }
    if tool == 3 || tool == 6 {
        let (e, m) = int_or_fail(src, tape, args, "event_id", 1, max_number(), true, path);
        if m > 0 {
            return refused(heap, m);
        }
        var at = put_nat(path, put(path, 0, "/events/"), e);
        if tool == 6 {
            at = put(path, at, "/attempts");
        }
        return planned(heap, at);
    }
    if tool == 4 {
        let (limit, m1) = int_or_fail(src, tape, args, "limit", 1, 256, false, path);
        if m1 > 0 {
            return refused(heap, m1);
        }
        let (offset, m2) = int_or_fail(src, tape, args, "offset", 0, max_number(), false, path);
        if m2 > 0 {
            return refused(heap, m2);
        }
        var at = put(path, 0, "/endpoints");
        at = add_nat(path, at, "limit", limit);
        at = add_nat(path, at, "offset", offset);
        return planned(heap, at);
    }
    if tool == 5 {
        let (ep, m1) = int_or_fail(src, tape, args, "endpoint_id", 0, max_number(), true, path);
        if m1 > 0 {
            return refused(heap, m1);
        }
        let (limit, m2) = int_or_fail(src, tape, args, "limit", 1, 1000, false, path);
        if m2 > 0 {
            return refused(heap, m2);
        }
        let (after, m3) = int_or_fail(src, tape, args, "after", 0, max_number(), false, path);
        if m3 > 0 {
            return refused(heap, m3);
        }
        let on = json.get(src, tape, args, "order");
        var order = 0;
        if on >= 0 {
            if json.string_equals(src, tape, on, "asc") {
                order = 1;
            } else if json.string_equals(src, tape, on, "desc") {
                order = 2;
            } else {
                return refused(heap, fail(path, "order", " must be asc or desc"));
            }
        }
        var at = put_nat(path, put(path, 0, "/endpoints/"), ep);
        at = put(path, at, "/dead");
        at = add_nat(path, at, "limit", limit);
        if order == 1 {
            at = put(path, add_sep(path, at), "order=asc");
        }
        if order == 2 {
            at = put(path, add_sep(path, at), "order=desc");
        }
        at = add_nat(path, at, "after", after);
        return planned(heap, at);
    }
    if tool == 8 || tool == 10 {
        let (e, m1) = int_or_fail(src, tape, args, "event_id", 1, max_number(), true, path);
        if m1 > 0 {
            return refused(heap, m1);
        }
        let (ep, m2) = int_or_fail(src, tape, args, "endpoint_id", 0, max_number(), tool == 10, path);
        if m2 > 0 {
            return refused(heap, m2);
        }
        var at = put(path, put_nat(path, put(path, 0, "/events/"), e), "/replay");
        if ep >= 0 {
            at = put_nat(path, put(path, at, "/"), ep);
        }
        return planned(heap, at);
    }
    if tool == 9 {
        return plan_replay_dead(heap, src, tape, args, path);
    }
    return plan_post(heap, src, tape, args, path, idem);
}

fn plan_replay_dead[&h, &s, &t, &p](heap: &!h Heap, src: &s [byte], tape: &t [int], args: int, path: &!p [byte]) -> [heap] (int, int, buffer.Buffer, int) {
    let (ep, m1) = int_or_fail(src, tape, args, "endpoint_id", 0, max_number(), true, path);
    if m1 > 0 {
        return refused(heap, m1);
    }
    let (limit, m2) = int_or_fail(src, tape, args, "limit", 1, 2048, false, path);
    if m2 > 0 {
        return refused(heap, m2);
    }
    let (after, m3) = int_or_fail(src, tape, args, "after", 0, max_number(), false, path);
    if m3 > 0 {
        return refused(heap, m3);
    }
    let types = json.get(src, tape, args, "types");
    if types >= 0 {
        var ok = json.is_array(tape, types) && json.count(tape, types) >= 1 && json.count(tape, types) <= 32;
        var j = types + 1;
        var left = json.count(tape, types);
        while ok && left > 0 {
            // A plain string (no escape) of 1 to 200 bytes: what is written between the quotes is what the service sees.
            if !json.string_plain(tape, j) || json.string_length(src, tape, j) < 1 || json.string_length(src, tape, j) > 200 {
                ok = false;
            }
            j = json.skip(tape, j);
            left = left - 1;
        }
        if !ok {
            return refused(heap, fail(path, "types", " must be an array of 1 to 32 strings of 1 to 200 characters, without escapes"));
        }
    }
    let at = put_nat(path, put(path, 0, "/endpoints/"), ep);
    let end = put(path, at, "/replay-dead");
    if limit < 0 && after < 0 && types < 0 {
        return planned(heap, end);
    }
    var w = json.writer(heap, 128);
    w = json.begin_object(heap, w);
    if limit >= 0 {
        w = json.put_key(heap, w, "limit");
        w = json.put_int(heap, w, limit);
    }
    if types >= 0 {
        w = json.put_key(heap, w, "types");
        w = copy_value(heap, w, src, tape, types);
    }
    if after >= 0 {
        w = json.put_key(heap, w, "after");
        w = json.put_int(heap, w, after);
    }
    w = json.end_object(heap, w);
    return (end, 0, json.finish(w), 0);
}

fn plan_post[&h, &s, &t, &p, &q](heap: &!h Heap, src: &s [byte], tape: &t [int], args: int, path: &!p [byte], idem: &!q [byte]) -> [heap] (int, int, buffer.Buffer, int) {
    let tb = box_slice(heap, 256, byte_of(0));
    var refusal = 0;
    var key_len = 0;
    var body = nothing(heap);
    borrow mut tb as &!tw in {
        let ty = contents(tw);
        let tn = str_arg(src, tape, args, "type", ty, 200);
        key_len = str_arg(src, tape, args, "idempotency_key", idem, 255);
        let fnode = json.get(src, tape, args, "fields");
        if tn == 0 - 1 {
            refusal = fail(path, "type", " is required");
        } else if tn < 1 || !plain_text(ty[0..tn]) {
            refusal = fail(path, "type", " must be a string of 1 to 200 characters without control characters");
        } else if key_len == 0 - 2 || key_len == 0 && json.get(src, tape, args, "idempotency_key") >= 0 || key_len > 0 && !key_text(idem[0..key_len]) {
            refusal = fail(path, "idempotency_key", " must be 1 to 255 visible ASCII characters, without spaces");
        } else if fnode >= 0 && (!json.is_object(tape, fnode) || depth_of(tape, fnode) > max_nesting()) {
            refusal = fail(path, "fields", " must be a JSON object nested at most 16 deep");
        } else if fnode >= 0 && has_type_key(src, tape, fnode) {
            refusal = fail(path, "fields", " must not have a type member: the type is the argument type");
        } else {
            var w = json.writer(heap, 256);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "type");
            w = json.put_string(heap, w, ty[0..tn]);
            if fnode >= 0 {
                var j = fnode + 1;
                var left = json.count(tape, fnode);
                while left > 0 {
                    w = put_key_of(heap, w, src, tape, j);
                    w = copy_value(heap, w, src, tape, j + 1);
                    j = json.skip(tape, j + 1);
                    left = left - 1;
                }
            }
            w = json.end_object(heap, w);
            buffer.drop(heap, body);
            body = json.finish(w);
            // The service refuses an event over 65,499 bytes, less 11 and the length of the type, less 28 and the length of the key when there is one (docs/api.md). Said here, because a
            // service that answers 413 to a body it has not read closes the connection on it and the answer can be lost to the reset.
            var most = 65499 - 11 - tn;
            if key_len > 0 {
                most = most - 28 - key_len;
            }
            var made = 0;
            borrow body as &bb in {
                made = buffer.size(bb);
            }
            if made > most {
                refusal = fail(path, "fields", " make an event larger than the service takes (65,499 bytes less the type and the key)");
            }
        }
    }
    unbox_slice(heap, tb);
    if refusal > 0 {
        buffer.drop(heap, body);
        return refused(heap, refusal);
    }
    if key_len < 0 {
        key_len = 0;
    }
    return (put(path, 0, "/events"), key_len, body, 0);
}

// ---------------------------------------------------------------------
// The HTTP request and its answer
// ---------------------------------------------------------------------

// The request, with its body. The only header that carries anything of a client is `Idempotency-Key`, whose value was judged to be visible ASCII; the token goes into `Authorization`
// and nowhere else.
fn compose[&h, &m, &p, &i, &b, &a, &k](heap: &!h Heap, method: &m [byte], path: &p [byte], idem: &i [byte], body: &b [byte], post: bool, hosthdr: &a [byte], token: &k [byte]) -> [heap] buffer.Buffer {
    var r = buffer.empty(heap, 512 + len(body));
    r = buffer.append(heap, r, method);
    r = buffer.append(heap, r, " ");
    r = buffer.append(heap, r, path);
    r = buffer.append(heap, r, " HTTP/1.1\r\nHost: ");
    r = buffer.append(heap, r, hosthdr);
    r = buffer.append(heap, r, "\r\nUser-Agent: hooks-mcp/0.1\r\nAccept: application/json\r\nConnection: close\r\n");
    if len(token) > 0 {
        r = buffer.append(heap, r, "Authorization: Bearer ");
        r = buffer.append(heap, r, token);
        r = buffer.append(heap, r, "\r\n");
    }
    if len(idem) > 0 {
        r = buffer.append(heap, r, "Idempotency-Key: ");
        r = buffer.append(heap, r, idem);
        r = buffer.append(heap, r, "\r\n");
    }
    if post {
        r = buffer.append(heap, r, "Content-Type: application/json\r\nContent-Length: ");
        r = buffer.push_nat(heap, r, len(body));
        r = buffer.append(heap, r, "\r\n");
    }
    r = buffer.append(heap, r, "\r\n");
    return buffer.append(heap, r, body);
}

// Wait until the connection is ready for `events` (1 readable, 2 writable): the poller's answer (more than 0 if it is, 0 if the time ran out, less than 0 if the wait failed). The wait is
// for whatever is left of the call's time, which `deadline` (a time of `clock_ms`) is the end of.
fn await_conn[&p, &c, &e, &k](poller: &!p Poller, conn: &c Conn, evs: &!e [int], events: int, clock: &k Clock, deadline: int) -> [poll, clock] int {
    let left = deadline - clock_ms(clock);
    if left <= 0 {
        return 0;
    }
    if poller_modify(poller, conn, 1, events) != 0 {
        return 0 - 1;
    }
    return poller_wait(poller, evs, left);
}

// One exchange: connect, send `request`, read to the end of the connection into `resp`, all within `request_ms` and without ever blocking (a service that does not answer is an
// error, not a program that waits for ever). Answers the buffer and 0, or: -1 could not connect, -2 could not send, -3 the connection failed while reading, -5 the answer is larger than
// `max_response`, -6 the time ran out.
fn round[&q, &h, &n, &a, &r, &k](heap: &!h Heap, net: &n Net(""), clock: &k Clock, host: &a [byte], port: int, request: &r [byte], resp: buffer.Buffer, wait_ms: int) -> [heap, net_out(""), conn_read, conn_write, poll, clock] (buffer.Buffer, int) {
    var out = resp;
    var code = 0;
    let deadline = clock_ms(clock) + wait_ms;
    match tcp_connect_start(net, host, port) {
        Dialed::Ok(dialed) => {
            var conn = dialed;
            match poller_new() {
                Polling::Ok(made) => {
                    var poller = made;
                    let evbox = box_slice(heap, 16, 0);
                    borrow mut evbox as &!evw in {
                        borrow mut conn as &!ch in {
                            borrow mut poller as &!pw in {
                                let evs = contents(evw);
                                if poller_add_conn(pw, ch, 1, 2) != 0 {
                                    code = 0 - 1;
                                } else if await_conn(pw, ch, evs, 2, clock, deadline) <= 0 {
                                    code = 0 - 1;
                                } else if conn_connect_status(ch) != 0 {
                                    code = 0 - 1;
                                }
                                var at = 0;
                                while at < len(request) && code == 0 {
                                    match conn_write(ch, request[at..len(request)]) {
                                        Sent::Wrote(k) => {
                                            at = at + k;
                                        }
                                        Sent::Again => {
                                            if await_conn(pw, ch, evs, 2, clock, deadline) <= 0 {
                                                code = 0 - 6;
                                            }
                                        }
                                        Sent::Failed(e) => {
                                            code = 0 - 2;
                                        }
                                    }
                                }
                                var more = code == 0;
                                while more {
                                    out = buffer.reserve(heap, out, 4096);
                                    var total = 0;
                                    borrow out as &ob in {
                                        total = buffer.size(ob);
                                    }
                                    if total > max_response() {
                                        code = 0 - 5;
                                        more = false;
                                    } else {
                                        borrow mut out as &!ob in {
                                            match conn_read(ch, buffer.room(ob)) {
                                                Received::Data(k) => {
                                                    buffer.filled(ob, k);
                                                }
                                                Received::End => {
                                                    more = false;
                                                }
                                                Received::Again => {
                                                    if await_conn(pw, ch, evs, 1, clock, deadline) <= 0 {
                                                        code = 0 - 6;
                                                        more = false;
                                                    }
                                                }
                                                Received::Failed(e) => {
                                                    // A connection that is reset after it has said something (a service that answers without reading a request to the end)
                                                    // leaves what was sent; the framing of the answer says whether it is whole.
                                                    if total == 0 {
                                                        code = 0 - 3;
                                                    }
                                                    more = false;
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    unbox_slice(heap, evbox);
                    poller_close(poller);
                }
                Polling::Failed(e) => {
                    code = 0 - 1;
                }
            }
            conn_close(conn);
        }
        Dialed::Failed(e) => {
            code = 0 - 1;
        }
    }
    return (out, code);
}

// Whether `text` is, ignoring case, the lower case `lit`.
fn iequals[&a, &b](text: &a [byte], lit: &b [byte]) -> [] bool {
    if len(text) != len(lit) {
        return false;
    }
    var i = 0;
    while i < len(text) {
        if bytes.to_lower(int_of(text[i])) != int_of(lit[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// The head of an answer: (the status, where the body starts, the Content-Length or -1, 1 if it is chunked). A status of -1 when this is not an HTTP/1 answer with a whole head and
// headers that can be read (a Content-Length that is not a number or that is given twice differently, a Transfer-Encoding that is not exactly `chunked`).
fn head_of[&r](b: &r [byte]) -> [] (int, int, int, int) {
    let he = bytes.find(b, "\r\n\r\n");
    if he < 12 || !bytes.starts_with(b, "HTTP/1.") {
        return (0 - 1, 0, 0 - 1, 0);
    }
    if !bytes.is_digit(int_of(b[9])) || !bytes.is_digit(int_of(b[10])) || !bytes.is_digit(int_of(b[11])) || int_of(b[8]) != ' ' {
        return (0 - 1, 0, 0 - 1, 0);
    }
    let status = bytes.digit_of(int_of(b[9])) * 100 + bytes.digit_of(int_of(b[10])) * 10 + bytes.digit_of(int_of(b[11]));
    var clen = 0 - 1;
    var chunked = 0;
    var bad = false;
    var p = bytes.find(b, "\r\n") + 2;
    while p <= he && !bad {
        let le = p + bytes.find(b[p..he + 2], "\r\n");
        let line = b[p..le];
        let colon = bytes.find(line, ":");
        if colon < 1 {
            bad = true;
        } else {
            let name = line[0..colon];
            let value = bytes.trim(line[colon + 1..len(line)]);
            if iequals(name, "content-length") {
                if len(value) < 1 || len(value) > 10 {
                    bad = true;
                } else {
                    var v = 0;
                    var i = 0;
                    while i < len(value) {
                        if !bytes.is_digit(int_of(value[i])) {
                            bad = true;
                        } else {
                            v = v * 10 + bytes.digit_of(int_of(value[i]));
                        }
                        i = i + 1;
                    }
                    if clen >= 0 && clen != v {
                        bad = true;
                    }
                    clen = v;
                }
            } else if iequals(name, "transfer-encoding") {
                if iequals(value, "chunked") {
                    chunked = 1;
                } else {
                    bad = true;
                }
            }
        }
        p = le + 2;
    }
    if bad {
        return (0 - 1, 0, 0 - 1, 0);
    }
    return (status, he + 4, clen, chunked);
}

// The body of the answer `b` that starts at `at`, in a new buffer, or -4 (it is not as long as its head says, or it is chunked wrongly).
fn body_of[&h, &r](heap: &!h Heap, b: &r [byte], at: int, clen: int, chunked: int) -> [heap] (buffer.Buffer, int) {
    var out = buffer.empty(heap, len(b) - at + 1);
    var code = 0;
    if chunked == 1 {
        let room = box_slice(heap, len(b) - at + 1, byte_of(0));
        borrow mut room as &!rw in {
            let decoded = contents(rw);
            let (used, wrote) = http.dechunk(b[at..len(b)], decoded);
            if used < 0 {
                code = 0 - 4;
            } else {
                out = buffer.append(heap, out, decoded[0..wrote]);
            }
        }
        unbox_slice(heap, room);
    } else if clen >= 0 {
        if len(b) - at < clen {
            code = 0 - 4;
        } else {
            out = buffer.append(heap, out, b[at..at + clen]);
        }
    } else {
        out = buffer.append(heap, out, b[at..len(b)]);
    }
    return (out, code);
}

// The result of a tool call: the text, as MCP gives it, and whether it is an error.
fn tool_result[&h, &r](heap: &!h Heap, text: &r [byte], failed: bool) -> [heap] buffer.Buffer {
    var w = json.writer(heap, len(text) + 128);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "content");
    w = json.begin_array(heap, w);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "type");
    w = json.put_string(heap, w, "text");
    w = json.put_key(heap, w, "text");
    w = json.put_string(heap, w, text);
    w = json.end_object(heap, w);
    w = json.end_array(heap, w);
    w = json.put_key(heap, w, "isError");
    w = json.put_bool(heap, w, failed);
    w = json.end_object(heap, w);
    return json.finish(w);
}

// What a call comes to once the request has been made: the answer's body as the text of the result (a status that is not 2xx is an error, saying the status first), or what went wrong.
fn answer[&h, &r](heap: &!h Heap, resp: &r [byte], code: int) -> [heap] buffer.Buffer {
    if code == 0 - 1 {
        return tool_result(heap, "could not connect to the hooks service at the configured address", true);
    }
    if code == 0 - 2 {
        return tool_result(heap, "could not send the request to the hooks service", true);
    }
    if code == 0 - 3 {
        return tool_result(heap, "the connection to the hooks service failed while its answer was being read", true);
    }
    if code == 0 - 5 {
        return tool_result(heap, "the answer of the hooks service is larger than 4 MiB", true);
    }
    if code == 0 - 6 {
        return tool_result(heap, "the hooks service did not answer in the time allowed (--timeout-seconds)", true);
    }
    let (status, at, clen, chunked) = head_of(resp);
    if status < 0 {
        return tool_result(heap, "the hooks service did not answer with HTTP/1 that this client can read", true);
    }
    let (body, bc) = body_of(heap, resp, at, clen, chunked);
    var result = nothing(heap);
    if bc < 0 {
        buffer.drop(heap, result);
        result = tool_result(heap, "the answer of the hooks service is cut short or badly framed", true);
    } else if status >= 200 && status < 300 {
        borrow body as &bb in {
            buffer.drop(heap, result);
            result = tool_result(heap, buffer.bytes(bb), false);
        }
    } else {
        var text = buffer.append(heap, buffer.empty(heap, 64), "HTTP ");
        text = buffer.push_nat(heap, text, status);
        text = buffer.append(heap, text, ": ");
        borrow body as &bb in {
            text = buffer.append(heap, text, buffer.bytes(bb));
        }
        borrow text as &tb in {
            buffer.drop(heap, result);
            result = tool_result(heap, buffer.bytes(tb), true);
        }
        buffer.drop(heap, text);
    }
    buffer.drop(heap, body);
    return result;
}

// ---------------------------------------------------------------------
// tools/call
// ---------------------------------------------------------------------

// Answers (the result, 0) or (the message, -32602).
fn call_tool[&q, &h, &n, &s, &t, &a, &b, &k](heap: &!h Heap, net: &n Net(""), clock: &q Clock, src: &s [byte], tape: &t [int], node: int, host: &a [byte], hosthdr: &b [byte], port: int, token: &k [byte], allow_write: bool, wait_ms: int) -> [heap, net_out(""), conn_read, conn_write, poll, clock] (buffer.Buffer, int) {
    let params = json.get(src, tape, node, "params");
    if !json.is_object(tape, params) {
        return (text_of(heap, "Invalid params: params must be an object with the name of a tool"), 0 - 32602);
    }
    if !keys_ok(src, tape, params, "name,arguments,_meta") {
        return (text_of(heap, "Invalid params: unknown member of params"), 0 - 32602);
    }
    let nm = json.get(src, tape, params, "name");
    if !json.is_string(tape, nm) {
        return (text_of(heap, "Invalid params: name must be a string, the name of a tool"), 0 - 32602);
    }
    let tool = tool_of(src, tape, nm);
    if tool == 0 || tool > last_read_tool() && !allow_write {
        return (text_of(heap, "Invalid params: unknown tool"), 0 - 32602);
    }
    let args = json.get(src, tape, params, "arguments");
    if args >= 0 && !json.is_object(tape, args) {
        return (text_of(heap, "Invalid params: arguments must be an object"), 0 - 32602);
    }
    let pathb = box_slice(heap, 512, byte_of(0));
    let idemb = box_slice(heap, 320, byte_of(0));
    var result = nothing(heap);
    var code = 0;
    borrow mut pathb as &!pw in {
        borrow mut idemb as &!iw in {
            let path = contents(pw);
            let idem = contents(iw);
            let (pl, il, body, pc) = plan(heap, tool, src, tape, args, path, idem);
            if pc != 0 {
                buffer.drop(heap, result);
                result = text_of(heap, path[0..pl]);
                code = pc;
            } else {
                var req = buffer.empty(heap, 1);
                borrow body as &bb in {
                    buffer.drop(heap, req);
                    req = compose(heap, method_of(tool), path[0..pl], idem[0..il], buffer.bytes(bb), tool >= 7 && tool <= 9, hosthdr, token);
                }
                var resp = buffer.empty(heap, 8192);
                borrow req as &rq in {
                    let (got, rc) = round(heap, net, clock, host, port, buffer.bytes(rq), resp, wait_ms);
                    resp = got;
                    borrow resp as &rr in {
                        buffer.drop(heap, result);
                        result = answer(heap, buffer.bytes(rr), rc);
                    }
                }
                buffer.drop(heap, req);
                buffer.drop(heap, resp);
            }
            buffer.drop(heap, body);
        }
    }
    unbox_slice(heap, pathb);
    unbox_slice(heap, idemb);
    return (result, code);
}

// ---------------------------------------------------------------------
// The methods
// ---------------------------------------------------------------------

fn supported_version[&s, &t](src: &s [byte], tape: &t [int], n: int) -> [] bool {
    return json.string_equals(src, tape, n, "2025-06-18") || json.string_equals(src, tape, n, "2025-03-26") || json.string_equals(src, tape, n, "2024-11-05");
}

fn initialize[&h, &s, &t](heap: &!h Heap, src: &s [byte], tape: &t [int], node: int) -> [heap] (buffer.Buffer, int) {
    let params = json.get(src, tape, node, "params");
    let wanted = json.get(src, tape, params, "protocolVersion");
    if !json.is_object(tape, params) || !json.is_string(tape, wanted) {
        return (text_of(heap, "Invalid params: initialize needs params with a protocolVersion string"), 0 - 32602);
    }
    var w = json.writer(heap, 512);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "protocolVersion");
    if supported_version(src, tape, wanted) {
        w = json.put_string(heap, w, json.string_view(src, tape, wanted));
    } else {
        w = json.put_string(heap, w, "2025-06-18");
    }
    w = json.put_key(heap, w, "capabilities");
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "tools");
    w = json.begin_object(heap, w);
    w = json.end_object(heap, w);
    w = json.end_object(heap, w);
    w = json.put_key(heap, w, "serverInfo");
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "name");
    w = json.put_string(heap, w, "hooks-mcp");
    w = json.put_key(heap, w, "version");
    w = json.put_string(heap, w, version());
    w = json.end_object(heap, w);
    w = json.put_key(heap, w, "instructions");
    w = json.put_string(heap, w, "Inspect a running hooks webhook delivery service: its health, counters, events, endpoints, dead letters and delivery attempts. Tools that change something are present only when the server was started with --allow-write.");
    w = json.end_object(heap, w);
    return (json.finish(w), 0);
}

fn list_tools[&h](heap: &!h Heap, allow_write: bool) -> [heap] (buffer.Buffer, int) {
    var w = json.writer(heap, 8192);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "tools");
    w = json.begin_array(heap, w);
    var tool = 1;
    while tool <= last_tool() {
        if tool <= last_read_tool() || allow_write {
            w = json.put_fragment(heap, w, tool_json(tool));
        }
        tool = tool + 1;
    }
    w = json.end_array(heap, w);
    w = json.end_object(heap, w);
    return (json.finish(w), 0);
}

// One message (the object at `node`): its reply, which is empty for a notification and for the answer of a client to a request this server never made.
fn process[&q, &h, &n, &s, &t, &a, &b, &k](heap: &!h Heap, net: &n Net(""), clock: &q Clock, src: &s [byte], tape: &t [int], node: int, host: &a [byte], hosthdr: &b [byte], port: int, token: &k [byte], allow_write: bool, wait_ms: int) -> [heap, net_out(""), conn_read, conn_write, poll, clock] buffer.Buffer {
    if !json.is_object(tape, node) {
        return reply(heap, src, tape, 0 - 1, text_of(heap, "Invalid Request: a message is a JSON object"), 0 - 32600);
    }
    let idn = json.get(src, tape, node, "id");
    let method = json.get(src, tape, node, "method");
    let version_ok = json.string_equals(src, tape, json.get(src, tape, node, "jsonrpc"), "2.0");
    // A reply to something this server never asked is not answered.
    if method < 0 && (json.get(src, tape, node, "result") >= 0 || json.get(src, tape, node, "error") >= 0) {
        return buffer.empty(heap, 1);
    }
    var id_ok = idn < 0 || json.is_string(tape, idn) || json.is_number(tape, idn);
    if !version_ok || !json.is_string(tape, method) || !id_ok {
        var shown = idn;
        if !id_ok {
            shown = 0 - 1;
        }
        return reply(heap, src, tape, shown, text_of(heap, "Invalid Request: a request has jsonrpc 2.0, a string method and a string or number id"), 0 - 32600);
    }
    // A notification is never answered, and never acted on: there is nowhere to say what came of it.
    if idn < 0 {
        return buffer.empty(heap, 1);
    }
    var result = nothing(heap);
    var code = 0;
    buffer.drop(heap, result);
    if json.string_equals(src, tape, method, "initialize") {
        let (r, c) = initialize(heap, src, tape, node);
        result = r;
        code = c;
    } else if json.string_equals(src, tape, method, "ping") {
        result = text_of(heap, "{}");
    } else if json.string_equals(src, tape, method, "tools/list") {
        let (r, c) = list_tools(heap, allow_write);
        result = r;
        code = c;
    } else if json.string_equals(src, tape, method, "tools/call") {
        let (r, c) = call_tool(heap, net, clock, src, tape, node, host, hosthdr, port, token, allow_write, wait_ms);
        result = r;
        code = c;
    } else {
        result = text_of(heap, "Method not found");
        code = 0 - 32601;
    }
    return reply(heap, src, tape, idn, result, code);
}

// A line: its reply (empty if there is none to give). A batch (an array of messages) is answered with an array of the replies there are, and with nothing when there are none.
fn handle_line[&q, &h, &n, &s, &a, &b, &k](heap: &!h Heap, net: &n Net(""), clock: &q Clock, src: &s [byte], host: &a [byte], hosthdr: &b [byte], port: int, token: &k [byte], allow_write: bool, wait_ms: int) -> [heap, net_out(""), conn_read, conn_write, poll, clock] buffer.Buffer {
    let tape = box_slice(heap, json.tape_len(src), 0);
    var nodes = 0;
    borrow mut tape as &!tw in {
        nodes = json.parse(src, contents(tw));
    }
    var out = buffer.empty(heap, 1);
    borrow tape as &tr in {
        let tp = contents(tr);
        buffer.drop(heap, out);
        if nodes < 0 {
            out = reply(heap, src, tp, 0 - 1, text_of(heap, "Parse error"), 0 - 32700);
        } else if json.is_array(tp, 0) {
            if json.count(tp, 0) == 0 {
                out = reply(heap, src, tp, 0 - 1, text_of(heap, "Invalid Request: a batch has at least one message"), 0 - 32600);
            } else {
                var joined = buffer.push(heap, buffer.empty(heap, 256), byte_of('['));
                var any = false;
                var j = 1;
                var left = json.count(tp, 0);
                while left > 0 {
                    let one = process(heap, net, clock, src, tp, j, host, hosthdr, port, token, allow_write, wait_ms);
                    borrow one as &ob in {
                        if buffer.size(ob) > 0 {
                            if any {
                                joined = buffer.push(heap, joined, byte_of(','));
                            }
                            joined = buffer.append(heap, joined, buffer.bytes(ob));
                            any = true;
                        }
                    }
                    buffer.drop(heap, one);
                    j = json.skip(tp, j);
                    left = left - 1;
                }
                if any {
                    out = buffer.push(heap, joined, byte_of(']'));
                } else {
                    buffer.drop(heap, joined);
                    out = buffer.empty(heap, 1);
                }
            }
        } else {
            out = process(heap, net, clock, src, tp, 0, host, hosthdr, port, token, allow_write, wait_ms);
        }
    }
    unbox_slice(heap, tape);
    return out;
}

// Whether the line is only blanks.
fn blank[&r](line: &r [byte]) -> [] bool {
    var i = 0;
    while i < len(line) {
        if !bytes.is_blank(int_of(line[i])) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// ---------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------

// Messages until the end of standard input: 0, or 1 if standard output could not be written.
fn serve[&q, &h, &i, &n, &a, &b, &k](heap: &!h Heap, io: &!i Io, net: &n Net(""), clock: &q Clock, host: &a [byte], hosthdr: &b [byte], port: int, token: &k [byte], allow_write: bool, wait_ms: int) -> [heap, io_read, io_write, net_out(""), conn_read, conn_write, poll, clock] int {
    var line = buffer.empty(heap, 4096);
    var status = 0;
    var running = true;
    while running {
        let (got, how) = read_line(heap, io, line);
        line = got;
        var out = buffer.empty(heap, 1);
        if how == 2 || how == 4 {
            buffer.drop(heap, out);
            out = reply_null(heap, text_of(heap, "Invalid Request: the message is longer than 1048576 bytes"), 0 - 32600);
        } else if how == 0 || how == 3 {
            borrow line as &lr in {
                let text = buffer.bytes(lr);
                if !blank(text) {
                    buffer.drop(heap, out);
                    out = handle_line(heap, net, clock, text, host, hosthdr, port, token, allow_write, wait_ms);
                }
            }
        }
        borrow out as &ob in {
            if buffer.size(ob) > 0 {
                if emit(io, buffer.bytes(ob)) != 0 {
                    status = 1;
                    running = false;
                }
            }
        }
        buffer.drop(heap, out);
        if how == 1 || how >= 3 {
            running = false;
        }
    }
    buffer.drop(heap, line);
    return status;
}

// ---------------------------------------------------------------------
// The command line
// ---------------------------------------------------------------------

// A TCP port in decimal, 1 to 65535, or -1.
fn port_of[&r](text: &r [byte]) -> [] int {
    if len(text) < 1 || len(text) > 5 {
        return 0 - 1;
    }
    var v = 0;
    var i = 0;
    while i < len(text) {
        if !bytes.is_digit(int_of(text[i])) {
            return 0 - 1;
        }
        v = v * 10 + bytes.digit_of(int_of(text[i]));
        i = i + 1;
    }
    if v < 1 || v > 65535 {
        return 0 - 1;
    }
    return v;
}

// `--timeout-seconds`: 1 to 600, or -1.
fn timeout_of[&r](text: &r [byte]) -> [] int {
    if len(text) < 1 || len(text) > 3 {
        return 0 - 1;
    }
    var v = 0;
    var i = 0;
    while i < len(text) {
        if !bytes.is_digit(int_of(text[i])) {
            return 0 - 1;
        }
        v = v * 10 + bytes.digit_of(int_of(text[i]));
        i = i + 1;
    }
    if v < 1 || v > 600 {
        return 0 - 1;
    }
    return v;
}

fn host_byte(c: int) -> [] bool {
    return bytes.is_alpha(c) || bytes.is_digit(c) || c == '.' || c == '-';
}

// The address in `http://HOST[:PORT]` (a name or an IPv4 address; no path, query or credentials): (0, where the host starts, where it ends, the port, where the authority ends), or
// (1, ...) for a scheme that is not `http` (there is no TLS here), (3, ...) for an IPv6 address (`tcp_connect` resolves IPv4 only) and (2, ...) for an address that is not one.
fn parse_url[&r](url: &r [byte]) -> [] (int, int, int, int, int) {
    if !bytes.starts_with(url, "http://") {
        var code = 2;
        if bytes.starts_with(url, "https://") {
            code = 1;
        }
        return (code, 0, 0, 0, 0);
    }
    var end = len(url);
    if end > 7 && int_of(url[end - 1]) == '/' {
        end = end - 1;
    }
    let hs = 7;
    if hs < end && int_of(url[hs]) == '[' {
        return (3, 0, 0, 0, 0);
    }
    var he = end;
    var port = 80;
    var colon = 0 - 1;
    var i = hs;
    while i < end {
        if int_of(url[i]) == ':' {
            if colon >= 0 {
                return (2, 0, 0, 0, 0);
            }
            colon = i;
        }
        i = i + 1;
    }
    if colon >= 0 {
        he = colon;
        port = port_of(url[colon + 1..end]);
    }
    if he <= hs || port < 1 {
        return (2, 0, 0, 0, 0);
    }
    i = hs;
    while i < he {
        if !host_byte(int_of(url[i])) {
            return (2, 0, 0, 0, 0);
        }
        i = i + 1;
    }
    return (0, hs, he, port, end);
}

// Whether the host is this machine: `localhost`, or an IPv4 address of 127.0.0.0/8 written as four decimal numbers. A name that merely begins with `127.` is not.
fn is_loopback[&r](host: &r [byte]) -> [] bool {
    if iequals(host, "localhost") {
        return true;
    }
    if !bytes.starts_with(host, "127.") || bytes.count_byte(host, '.') != 3 {
        return false;
    }
    var i = 0;
    while i < len(host) {
        if !bytes.is_digit(int_of(host[i])) && int_of(host[i]) != '.' {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// The bearer token in the file at `path`, into `into`, without the whitespace at its end. Its length, or: -1 the file cannot be read, -2 it is longer than 4096 bytes, -3 it is empty,
// -4 it has a byte that cannot be in a bearer token (a space or a control character in the middle, or non-ASCII). What it holds is never reported.
fn read_token[&f, &p, &b](fs: &f Fs(""), path: &p [byte], into: &!b [byte]) -> [fs_read("")] int {
    let n = fs_read(fs, path, into);
    if n < 0 {
        return 0 - 1;
    }
    if n > 4096 {
        return 0 - 2;
    }
    var end = n;
    while end > 0 && int_of(into[end - 1]) <= 32 {
        end = end - 1;
    }
    if end == 0 {
        return 0 - 3;
    }
    var i = 0;
    while i < end {
        let c = int_of(into[i]);
        if c < 33 || c > 126 {
            return 0 - 4;
        }
        i = i + 1;
    }
    return end;
}

fn usage[&i](io: &!i Io) -> [err_write] int {
    say(io, "usage: hooks-mcp [--url http://HOST:PORT] [--token-file PATH] [--allow-write] [--allow-remote-plaintext] [--timeout-seconds N]\n");
    say(io, "  --url                      the hooks service (default http://127.0.0.1:8080); plain HTTP only\n");
    say(io, "  --token-file               a file holding a bearer token for the service (never given on the command line)\n");
    say(io, "  --allow-write              also offer the tools that post events, replay and cancel replays\n");
    say(io, "  --allow-remote-plaintext   send the token to a host that is not this machine, over plain HTTP\n");
    say(io, "  --timeout-seconds          the most one call to the service may take (default 30, at most 600)\n");
    return 0;
}

// Say that an argument is not understood, naming a flag and never anything else (a value given to a flag by mistake could be a secret).
fn say_bad_flag[&i, &r](io: &!i Io, a: &r [byte]) -> [err_write] int {
    say(io, "hooks-mcp: ");
    if bytes.starts_with(a, "--") {
        var end = 0;
        while end < len(a) && int_of(a[end]) != '=' {
            end = end + 1;
        }
        say(io, "`");
        say(io, a[0..end]);
        say(io, "` is not understood or is missing its value\n");
    } else {
        say(io, "an argument that is not a flag was given, and is not understood\n");
    }
    return usage(io);
}

fn run[&q, &h, &i, &f, &n, &g](heap: &!h Heap, io: &!i Io, fs: &f Fs(""), net: &n Net(""), clock: &q Clock, args: &g Args) -> [heap, io_read, io_write, err_write, fs_read(""), net_out(""), conn_read, conn_write, poll, clock, args] int {
    var url = "http://127.0.0.1:8080";
    var tokfile = "";
    var allow_write = false;
    var allow_remote = false;
    var seconds = default_timeout();
    var at = 1;
    var bad = 0;
    while at < arg_count(args) && bad == 0 {
        let a = arg(args, at);
        if bytes.equal(a, "--allow-write") {
            allow_write = true;
            at = at + 1;
        } else if bytes.equal(a, "--allow-remote-plaintext") {
            allow_remote = true;
            at = at + 1;
        } else if bytes.equal(a, "--url") && at + 1 < arg_count(args) {
            url = arg(args, at + 1);
            at = at + 2;
        } else if bytes.starts_with(a, "--url=") {
            url = a[6..len(a)];
            at = at + 1;
        } else if bytes.equal(a, "--timeout-seconds") && at + 1 < arg_count(args) {
            seconds = timeout_of(arg(args, at + 1));
            at = at + 2;
        } else if bytes.starts_with(a, "--timeout-seconds=") {
            seconds = timeout_of(a[18..len(a)]);
            at = at + 1;
        } else if bytes.equal(a, "--token-file") && at + 1 < arg_count(args) {
            tokfile = arg(args, at + 1);
            at = at + 2;
        } else if bytes.starts_with(a, "--token-file=") {
            tokfile = a[13..len(a)];
            at = at + 1;
        } else {
            bad = at;
        }
    }
    if bad > 0 {
        say_bad_flag(io, arg(args, bad));
        return 2;
    }
    if seconds < 1 {
        say(io, "hooks-mcp: --timeout-seconds must be a whole number from 1 to 600\n");
        return 2;
    }
    let (ucode, hs, he, port, auth_end) = parse_url(url);
    if ucode == 1 {
        say(io, "hooks-mcp: --url is https: this client speaks plain HTTP only (put it behind a local plain-HTTP address)\n");
        return 2;
    }
    if ucode == 3 {
        say(io, "hooks-mcp: --url names an IPv6 address, which this client cannot connect to (lex-sys's tcp_connect resolves IPv4 only); use a name or an IPv4 address\n");
        return 2;
    }
    if ucode != 0 {
        say(io, "hooks-mcp: --url must be http://HOST or http://HOST:PORT (a name or an IPv4 address; no path)\n");
        return 2;
    }
    let host = url[hs..he];
    let hosthdr = url[7..auth_end];
    let tokbox = box_slice(heap, 4097, byte_of(0));
    var status = 0;
    var tlen = 0;
    if len(tokfile) > 0 {
        borrow mut tokbox as &!tw in {
            tlen = read_token(fs, tokfile, contents(tw));
        }
        if tlen == 0 - 1 {
            say(io, "hooks-mcp: the token file cannot be read\n");
        } else if tlen == 0 - 2 {
            say(io, "hooks-mcp: the token file is longer than 4096 bytes\n");
        } else if tlen == 0 - 3 {
            say(io, "hooks-mcp: the token file is empty\n");
        } else if tlen == 0 - 4 {
            say(io, "hooks-mcp: the token file has a byte that cannot be in a bearer token (one token, ASCII, no spaces)\n");
        }
        if tlen < 0 {
            status = 2;
            tlen = 0;
        }
    }
    if status == 0 && tlen > 0 && !is_loopback(host) && !allow_remote {
        say(io, "hooks-mcp: refusing to send a bearer token over plain HTTP to a host that is not this machine (use --allow-remote-plaintext if the network between is trusted)\n");
        status = 2;
    }
    if status == 0 {
        borrow tokbox as &tr in {
            status = serve(heap, io, net, clock, host, hosthdr, port, contents(tr)[0..tlen], allow_write, seconds * 1000);
        }
    }
    unbox_slice(heap, tokbox);
    return status;
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock, signals } = split(world);
    release(ffi);
    release(signals);
    var status = 2;
    var h = heap;
    var o = io;
    borrow mut h as &!hp in {
        borrow mut o as &!op in {
            borrow fs as &fr in {
                borrow net as &nr in {
                    borrow clock as &cr in {
                        borrow args as &ar in {
                            status = run(hp, op, fr, nr, cr, ar);
                        }
                    }
                }
            }
        }
    }
    release(fs);
    release(net);
    release(clock);
    release(args);
    release(o);
    release(h);
    return status;
}
