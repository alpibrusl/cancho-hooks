edition 5;

module endpoints;

import destination;
import sign;
import state;

// `endpoints` -- the endpoints a service delivers to, read from a text file (`docs/design.md` sections 3 and 15).
//
// This is a stand-in for the Postgres table section 3 gives them: it is read once at start, one endpoint a line,
//
//     <id> <host> <port> <secret>
//
// with `#` comments and blank lines ignored. The id is a number of at most six digits and is the endpoint's identity, so it
// must not change when the file is reordered: it is written, not counted. The secret is a Standard
// Webhooks one (`whsec_` and base64); what is kept is the key it decodes to.
//
// `parse` fills a table of seven integers per endpoint, in the order the lines came:
//
//     [slot, port, host_start, host_len, key_start, key_len, id]
//
// where the starts index `blob`. The caller sizes both: `table` for `state.max_endpoints()` endpoints, `blob` for the file's
// own length (a host and a key are never longer than the line they came from). The **id** is the endpoint's identity (what the
// API and the history call it, 0 to 999999, never reused); the **slot** is its place in the delivery state's arrays, which
// `parse` cannot know: it writes the id there, and `hooks.ls` replaces it once it has read the log (`docs/design.md` section 25).

pub fn stride() -> [] int {
    return 7;
}

pub fn table_size() -> [] int {
    return 7 * state.max_endpoints();
}

// The most bytes of text (a file, or the database's table written as one) that `parse` is given.
pub fn text_limit() -> [] int {
    return 32768;
}

fn is_space(c: int) -> [] bool {
    return c == ' ' || c == '\t' || c == '\r';
}

// The next field of the line `[from, to)` of `text`: `(start, end)` with `start == end` if there is none.
pub fn field[&t](text: &t [byte], from: int, to: int) -> [] (int, int) {
    var s = from;
    while s < to && is_space(int_of(text[s])) {
        s = s + 1;
    }
    var e = s;
    while e < to && !is_space(int_of(text[e])) {
        e = e + 1;
    }
    return (s, e);
}

pub fn number[&t](text: &t [byte], from: int, to: int) -> [] int {
    if to == from || to - from > 6 {
        return 0 - 1;
    }
    var n = 0;
    var i = from;
    while i < to {
        let c = int_of(text[i]);
        if c < '0' || c > '9' {
            return 0 - 1;
        }
        n = n * 10 + (c - '0');
        i = i + 1;
    }
    return n;
}

// Parse the file. Answers the number of endpoints, or `0 - line` (the 1-based line number, negated) of the first line that is
// wrong: not four fields, an id that is not a number of at most six digits or is repeated, a port outside 1 to 65535, a host that is not a public IPv4 address (unless `open`: section 26), a secret that
// is not base64, a host that is not a public IPv4 address (unless `open`: `destination.ls`), or more than `state.max_endpoints()` endpoints.
pub fn parse[&t, &n, &b](text: &t [byte], table: &!n [int], blob: &!b [byte], open: bool) -> [] int {
    var count = 0;
    var line = 0;
    var at = 0;
    var used = 0;
    while at < len(text) {
        var end = at;
        while end < len(text) && int_of(text[end]) != '\n' {
            end = end + 1;
        }
        line = line + 1;
        let first = field(text, at, end);
        if first.0 < first.1 && int_of(text[first.0]) != '#' {
            let host = field(text, first.1, end);
            let port = field(text, host.1, end);
            let secret = field(text, port.1, end);
            let extra = field(text, secret.1, end);
            if host.0 == host.1 || port.0 == port.1 || secret.0 == secret.1 || extra.0 != extra.1 {
                return 0 - line;
            }
            let id = number(text, first.0, first.1);
            let p = number(text, port.0, port.1);
            if id < 0 || p < 1 || p > 65535 || count >= state.max_endpoints() {
                return 0 - line;
            }
            if !open && !destination.allowed(text[host.0..host.1]) {
                return 0 - line;
            }
            var i = 0;
            while i < count {
                if table[i * stride()] == id {
                    return 0 - line;
                }
                i = i + 1;
            }
            let base = count * stride();
            table[base] = id;
            table[base + 1] = p;
            table[base + 2] = used;
            table[base + 3] = host.1 - host.0;
            var k = 0;
            while k < host.1 - host.0 {
                blob[used + k] = text[host.0 + k];
                k = k + 1;
            }
            used = used + host.1 - host.0;
            let klen = sign.secret_key(text[secret.0..secret.1], blob[used..len(blob)]);
            if klen < 0 {
                return 0 - line;
            }
            table[base + 4] = used;
            table[base + 5] = klen;
            table[base + 6] = id;
            used = used + klen;
            count = count + 1;
        }
        at = end + 1;
    }
    return count;
}

// The `i`th endpoint's slot, id, port, host and key.
pub fn slot_of[&n](table: &n [int], i: int) -> [] int {
    return table[i * stride()];
}

pub fn ident_of[&n](table: &n [int], i: int) -> [] int {
    return table[i * stride() + 6];
}

pub fn set_slot[&n](table: &!n [int], i: int, slot: int) -> [] int {
    table[i * stride()] = slot;
    return 0;
}

pub fn port_of[&n](table: &n [int], i: int) -> [] int {
    return table[i * stride() + 1];
}

pub fn host_of[&n, &b](table: &n [int], blob: &b [byte], i: int) -> [] &b [byte] {
    return blob[table[i * stride() + 2]..table[i * stride() + 2] + table[i * stride() + 3]];
}

pub fn key_of[&n, &b](table: &n [int], blob: &b [byte], i: int) -> [] &b [byte] {
    return blob[table[i * stride() + 4]..table[i * stride() + 4] + table[i * stride() + 5]];
}

// How much of `blob` the first `count` endpoints use: the end of the last host or key.
pub fn blob_used[&n](table: &n [int], count: int) -> [] int {
    var used = 0;
    var i = 0;
    while i < count {
        let host_end = table[i * stride() + 2] + table[i * stride() + 3];
        let key_end = table[i * stride() + 4] + table[i * stride() + 5];
        if host_end > used {
            used = host_end;
        }
        if key_end > used {
            used = key_end;
        }
        i = i + 1;
    }
    return used;
}

// Add an endpoint after the first `count`: its slot, id, port, host and the key its secret decodes to. Answers the new count, or -1 if the
// table or the blob is full (nothing is changed then).
pub fn append[&n, &b, &h, &k](table: &!n [int], blob: &!b [byte], count: int, slot: int, ident: int, port: int, host: &h [byte], key: &k [byte]) -> [] int {
    if count >= state.max_endpoints() {
        return 0 - 1;
    }
    let at = blob_used(table, count);
    if at + len(host) + len(key) > len(blob) {
        return 0 - 1;
    }
    var i = 0;
    while i < len(host) {
        blob[at + i] = host[i];
        i = i + 1;
    }
    i = 0;
    while i < len(key) {
        blob[at + len(host) + i] = key[i];
        i = i + 1;
    }
    let base = count * stride();
    table[base] = slot;
    table[base + 1] = port;
    table[base + 2] = at;
    table[base + 3] = len(host);
    table[base + 4] = at + len(host);
    table[base + 5] = len(key);
    table[base + 6] = ident;
    return count + 1;
}

// Move every host and key to the front of `blob`, in the order of the table, so that what `replace` left behind is room again. `scratch`
// is as long as `blob`. Answers the bytes in use.
pub fn compact[&n, &b, &s](table: &!n [int], blob: &!b [byte], count: int, scratch: &!s [byte]) -> [] int {
    var used = 0;
    var i = 0;
    while i < count {
        let base = i * stride();
        var k = 0;
        while k < table[base + 3] {
            scratch[used + k] = blob[table[base + 2] + k];
            k = k + 1;
        }
        table[base + 2] = used;
        used = used + table[base + 3];
        k = 0;
        while k < table[base + 5] {
            scratch[used + k] = blob[table[base + 4] + k];
            k = k + 1;
        }
        table[base + 4] = used;
        used = used + table[base + 5];
        i = i + 1;
    }
    var j = 0;
    while j < used {
        blob[j] = scratch[j];
        j = j + 1;
    }
    return used;
}

// Give the `i`th of the first `count` endpoints a new port, host and key (`docs/design.md` section 25.3). The new bytes go after the used
// ones, and the old ones are left behind until the blob is full, when `compact` takes them out (the endpoint's own old bytes too, if the new
// ones fit only without them). Answers 0, or -1 if there is no room even then (nothing is changed).
pub fn replace[&n, &b, &h, &k, &s](table: &!n [int], blob: &!b [byte], count: int, i: int, port: int, host: &h [byte], key: &k [byte], scratch: &!s [byte]) -> [] int {
    var at = blob_used(table, count);
    if at + len(host) + len(key) > len(blob) {
        compact(table, blob, count, scratch);
        at = blob_used(table, count);
        if at + len(host) + len(key) > len(blob) {
            // The endpoint's own old bytes are about to be replaced: if the new ones fit without them, they are taken out too.
            let own = i * stride();
            if at - table[own + 3] - table[own + 5] + len(host) + len(key) > len(blob) {
                return 0 - 1;
            }
            table[own + 3] = 0;
            table[own + 5] = 0;
            compact(table, blob, count, scratch);
            at = blob_used(table, count);
        }
    }
    var j = 0;
    while j < len(host) {
        blob[at + j] = host[j];
        j = j + 1;
    }
    j = 0;
    while j < len(key) {
        blob[at + len(host) + j] = key[j];
        j = j + 1;
    }
    let base = i * stride();
    table[base + 1] = port;
    table[base + 2] = at;
    table[base + 3] = len(host);
    table[base + 4] = at + len(host);
    table[base + 5] = len(key);
    return 0;
}

// Take the `i`th of the first `count` endpoints out (`docs/design.md` section 25.5): the entries after it move down one place, so the table is
// still the first `count - 1` endpoints in the order they had. Nothing that holds an index into the table across turns may survive this: the
// delivery state keys everything that lasts by *slot*, and the table is only ever read by index inside one turn. The bytes of the host and the
// key stay where they are (every other entry's `host_start` and `key_start` still point at its own), except that the removed endpoint's own are
// zeroed, so a deleted endpoint's signing key is not left in memory; `blob_used` then shrinks if they were the last, and what is left in the
// middle is room that `compact` gives back. Answers the new count, or -1 if `i` is not one of the first `count` (nothing is changed then).
pub fn remove[&n, &b](table: &!n [int], blob: &!b [byte], count: int, i: int) -> [] int {
    if i < 0 || i >= count {
        return 0 - 1;
    }
    let own = i * stride();
    var k = 0;
    while k < table[own + 3] {
        blob[table[own + 2] + k] = byte_of(0);
        k = k + 1;
    }
    k = 0;
    while k < table[own + 5] {
        blob[table[own + 4] + k] = byte_of(0);
        k = k + 1;
    }
    var j = i;
    while j < count - 1 {
        var f = 0;
        while f < stride() {
            table[j * stride() + f] = table[(j + 1) * stride() + f];
            f = f + 1;
        }
        j = j + 1;
    }
    var z = 0;
    while z < stride() {
        table[(count - 1) * stride() + z] = 0;
        z = z + 1;
    }
    return count - 1;
}
