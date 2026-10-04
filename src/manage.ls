edition 5;

module manage;

import destination;
import std.buffer;
import std.http;
import std.json;
import pg;

// `manage` -- the pieces of `POST /endpoints` that are not the delivery state (`docs/design.md` section 25.2): who may call it, what
// a request may say, and a secret for the endpoint when it does not bring one.
//
// The request waits for the database, so what it asked for is kept between the turn that read it and the turn that has the
// database's answer, in a block of integers of the delivery state (`mg`, one byte to an integer: the delivery state is integers):
//
//     [0] state: 0 nothing waits, 1 asked (the loop has not sent it yet), 2 sent and waiting for the database
//     [1] the pool's tag for it   [2] the ticket of the held connection   [3] keep the connection alive   [4] when to give up (ms)
//     [5] port   [6] host length   [7] secret length   [8] 1 if the service has to make the secret
//     [9 .. 265) the host   [265 .. 361) the secret, `whsec_` and base64

pub fn token_size() -> [] int {
    return 256;
}

pub fn mg_size() -> [] int {
    return 368;
}

pub fn mg_state() -> [] int {
    return 0;
}

pub fn mg_tag() -> [] int {
    return 1;
}

pub fn mg_ticket() -> [] int {
    return 2;
}

pub fn mg_keep() -> [] int {
    return 3;
}

pub fn mg_deadline() -> [] int {
    return 4;
}

pub fn mg_port() -> [] int {
    return 5;
}

pub fn mg_host_len() -> [] int {
    return 6;
}

pub fn mg_secret_len() -> [] int {
    return 7;
}

pub fn mg_make() -> [] int {
    return 8;
}

pub fn mg_host() -> [] int {
    return 9;
}

pub fn mg_secret() -> [] int {
    return 265;
}

fn lower_equal[&a, &b](name: &a [byte], want: &b [byte]) -> [] bool {
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

// May this request change endpoints? 0 yes; 1 no: the service was not given a token, so management is off; 2 no: the request has no
// bearer token, more than one `Authorization` header, or a token that is not the one. `token` is the configured token, its length
// and then its bytes. The comparison looks at every byte of the longest token there can be, whichever one differs first.
pub fn authorize[&q, &t, &k](request: &q [byte], table: &t [int], token: &k [int]) -> [] int {
    if token[0] == 0 {
        return 1;
    }
    let first = http.find_header(request, table, "authorization");
    if first < 0 {
        return 2;
    }
    var i = first + 1;
    while i < http.header_count(table) {
        if lower_equal(http.header_name(request, table, i), "authorization") {
            return 2;
        }
        i = i + 1;
    }
    let value = http.header_value(request, table, first);
    if len(value) < 8 || !lower_equal(value[0..7], "bearer ") {
        return 2;
    }
    let given = value[7..len(value)];
    if len(given) > 255 {
        return 2;
    }
    var diff = len(given) ^ token[0];
    var j = 0;
    while j < 255 {
        var a = 0;
        if j < len(given) {
            a = int_of(given[j]);
        }
        var b = 0;
        if j < token[0] {
            b = token[1 + j];
        }
        diff = diff | a ^ b;
        j = j + 1;
    }
    if diff == 0 {
        return 0;
    }
    return 2;
}

// What was wrong with a request to create an endpoint, by the code `parse_create` answers.
pub fn why(code: int) -> [] &static [byte] {
    if code == 1 {
        return "the body must be a JSON object";
    }
    if code == 2 {
        return "the endpoint needs a string \"host\" of 1 to 253 printable characters with no space";
    }
    if code == 3 {
        return "the endpoint needs an integer \"port\" from 1 to 65535";
    }
    if code == 4 {
        return "the \"secret\" must be whsec_ and base64 (or left out, and the service makes one)";
    }
    if code == 5 {
        return "a new endpoint starts from now: \"from\" may only be \"now\"";
    }
    if code == 6 {
        return "the host must be a public IPv4 address (four numbers, no name; loopback, private, link-local and reserved ranges are refused: SSRF)";
    }
    return "the request is not valid";
}

fn printable[&t](text: &t [byte]) -> [] bool {
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if c <= 32 || c >= 127 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Read `{"host": "...", "port": N, "secret": "whsec_..." (optional), "from": "now" (optional)}` and keep it in `mg` (from [5] up).
// Answers `(code, port, host length, secret length)`: code 0 if the request is good, else what `why` says. Other members are ignored.
// `scratch` is at least 400 bytes. The secret is judged by the rule that judges a line of `endpoints.conf`: it must decode as base64
// (`sign.secret_key` is the caller's check; here only its length and characters are).
pub fn parse_create[&h, &b, &c, &m](heap: &!h Heap, body: &b [byte], scratch: &!c [byte], mg: &!m [int], open: bool) -> [heap] (int, int, int, int) {
    var code = 0;
    var port = 0;
    var host_len = 0;
    var secret_len = 0;
    let tape = box_slice(heap, json.tape_len(body), 0);
    borrow mut tape as &!tw in {
        let t = contents(tw);
        if json.parse(body, t) < 0 || !json.is_object(t, 0) {
            code = 1;
        } else {
            let host = json.get(body, t, 0, "host");
            if host < 0 || !json.is_string(t, host) {
                code = 2;
            } else {
                host_len = json.string_into(body, t, host, scratch[0..253]);
                if host_len < 1 || !printable(scratch[0..host_len]) {
                    code = 2;
                } else if !open && !destination.allowed(scratch[0..host_len]) {
                    code = 6;
                }
            }
            if code == 0 {
                let p = json.get(body, t, 0, "port");
                if p < 0 || !json.is_int(t, p) || !json.fits_int(body, t, p) {
                    code = 3;
                } else {
                    port = json.to_int(body, t, p);
                    if port < 1 || port > 65535 {
                        code = 3;
                    }
                }
            }
            if code == 0 {
                let s = json.get(body, t, 0, "secret");
                if s >= 0 {
                    if !json.is_string(t, s) {
                        code = 4;
                    } else {
                        secret_len = json.string_into(body, t, s, scratch[256..352]);
                        if secret_len < 7 || !printable(scratch[256..256 + secret_len]) {
                            code = 4;
                        }
                    }
                }
            }
            if code == 0 {
                let f = json.get(body, t, 0, "from");
                if f >= 0 && !(json.is_string(t, f) && json.string_equals(body, t, f, "now")) {
                    code = 5;
                }
            }
        }
    }
    unbox_slice(heap, tape);
    if code != 0 {
        return (code, 0, 0, 0);
    }
    var i = 0;
    while i < host_len {
        mg[mg_host() + i] = int_of(scratch[i]);
        i = i + 1;
    }
    i = 0;
    while i < secret_len {
        mg[mg_secret() + i] = int_of(scratch[256 + i]);
        i = i + 1;
    }
    mg[mg_port()] = port;
    mg[mg_host_len()] = host_len;
    mg[mg_secret_len()] = secret_len;
    if secret_len == 0 {
        mg[mg_make()] = 1;
    } else {
        mg[mg_make()] = 0;
    }
    return (0, port, host_len, secret_len);
}

// Make a secret for the endpoint in `mg`: `whsec_` and the base64 of 24 bytes from the kernel. Answers 0, or -1 if the kernel did not
// give them (and then the request is refused: a secret that is not unpredictable is worse than none).
pub fn make_secret[&h, &f, &m](heap: &!h Heap, fs: &f Fs(""), mg: &!m [int]) -> [heap, fs_read("")] int {
    region a {
        let raw = alloc_slice[a](24, byte_of(0));
        let got = fs_read(fs, "/dev/urandom", raw);
        if got != 24 {
            return 0 - 1;
        }
        let text = pg.base64_encode(heap, raw);
        var n = 0;
        let prefix = "whsec_";
        while n < len(prefix) {
            mg[mg_secret() + n] = int_of(prefix[n]);
            n = n + 1;
        }
        borrow text as &tr in {
            let b = buffer.bytes(tr);
            var i = 0;
            while i < len(b) {
                mg[mg_secret() + n + i] = int_of(b[i]);
                i = i + 1;
            }
            mg[mg_secret_len()] = n + len(b);
        }
        buffer.drop(heap, text);
        return 0;
    }
}

// The bytes of `len` integers of `mg` from `at`, written into `out`.
pub fn bytes_of[&m, &o](mg: &m [int], at: int, count: int, out: &!o [byte]) -> [] int {
    var i = 0;
    while i < count {
        out[i] = byte_of(mg[at + i]);
        i = i + 1;
    }
    return count;
}
