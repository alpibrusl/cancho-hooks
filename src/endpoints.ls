edition 5;

module endpoints;

import sign;
import state;

// `endpoints` -- the endpoints a service delivers to, read from a text file (`docs/design.md` sections 3 and 15).
//
// This is a stand-in for the Postgres table section 3 gives them: it is read once at start, one endpoint a line,
//
//     <id> <host> <port> <secret>
//
// with `#` comments and blank lines ignored. The id is a number below `state.max_endpoints()` and is the endpoint's identity
// in the outcome log, so it must not change when the file is reordered: it is written, not counted. The secret is a Standard
// Webhooks one (`whsec_` and base64); what is kept is the key it decodes to.
//
// `parse` fills a table of six integers per endpoint, in the order the lines came:
//
//     [id, port, host_start, host_len, key_start, key_len]
//
// where the starts index `blob`. The caller sizes both: `table` for `state.max_endpoints()` endpoints, `blob` for the file's
// own length (a host and a key are never longer than the line they came from).

pub fn stride() -> [] int {
    return 6;
}

pub fn table_size() -> [] int {
    return 6 * state.max_endpoints();
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
// wrong: not four fields, an id that is not a number below the limit or is repeated, a port outside 1 to 65535, a secret that
// is not base64, or more than `state.max_endpoints()` endpoints.
pub fn parse[&t, &n, &b](text: &t [byte], table: &!n [int], blob: &!b [byte]) -> [] int {
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
            if id < 0 || id >= state.max_endpoints() || p < 1 || p > 65535 || count >= state.max_endpoints() {
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
            used = used + klen;
            count = count + 1;
        }
        at = end + 1;
    }
    return count;
}

// The `i`th endpoint's id, port, host and key.
pub fn id_of[&n](table: &n [int], i: int) -> [] int {
    return table[i * stride()];
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
