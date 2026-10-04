edition 5;

module hdrs;

// `hdrs` -- the custom headers of an endpoint (`docs/design.md` section 35, production.md P1.5): the rules that decide which are allowed, and the
// one form they are kept in.
//
// **The spec.** An endpoint's headers are kept (in the `headers` column, in `endpoints.conf`, in the roster's text) as `Name:value` pairs separated
// by commas, each value percent-encoded: every byte but `A-Z a-z 0-9 - . _ ~` is `%XX`, so the text has no space, no comma, no colon in a value
// that is not meant, and nothing a line-oriented format could mistake for its own syntax. The decoder is lenient about what it reads raw (any
// visible ASCII but `,` and `%`) and strict about what it produces: the value that comes out is judged, whatever way it got there, so `%0d%0a` is
// refused as a CR and an LF are.
//
// **The wire form.** `decode` turns a good spec into the bytes a request carries, `Name: value\r\n` for each header, into an array of integers
// (a byte to an integer: the delivery state is integers), and `wire_len` is how many bytes that is.
//
// **The rules** (README, "Custom headers"): at most 8 headers; a name of 1 to 64 token characters (RFC 9110: letters, digits and ``!#$%&'*+-.^_`|~``);
// a value of 1 to 512 visible ASCII characters and spaces, not beginning or ending with a space (nothing below 0x20 and not 0x7f: a CR, an LF or a NUL
// would end the header and begin another); the wire form at most 2,048 bytes and the spec at most 4,096; no name twice (any case); and **never** the
// headers the delivery sets itself (`webhook-id`, `webhook-timestamp`, `webhook-signature`, `host`, `content-length`, `content-type`) or that
// belong to the connection and not to the message (`connection`, `keep-alive`, `proxy-connection`, `proxy-authenticate`, `proxy-authorization`,
// `te`, `trailer`, `transfer-encoding`, `upgrade`).

pub fn max_headers() -> [] int {
    return 8;
}

pub fn max_name() -> [] int {
    return 64;
}

pub fn max_value() -> [] int {
    return 512;
}

pub fn max_wire() -> [] int {
    return 2048;
}

pub fn max_spec() -> [] int {
    return 4096;
}

// The refusals, by code (`why` says each in words).
pub fn too_many() -> [] int {
    return 1;
}

pub fn bad_name() -> [] int {
    return 2;
}

pub fn forbidden_name() -> [] int {
    return 3;
}

pub fn repeated_name() -> [] int {
    return 4;
}

pub fn bad_length() -> [] int {
    return 5;
}

pub fn bad_byte() -> [] int {
    return 6;
}

pub fn edge_space() -> [] int {
    return 7;
}

pub fn too_large() -> [] int {
    return 8;
}

pub fn malformed() -> [] int {
    return 9;
}

pub fn why(code: int) -> [] &static [byte] {
    if code == 1 {
        return "at most 8 custom headers";
    }
    if code == 2 {
        return "a header name is 1 to 64 characters from the HTTP token set (letters, digits and !#$%&'*+-.^_`|~)";
    }
    if code == 3 {
        return "that header cannot be set: the delivery sets webhook-id, webhook-timestamp, webhook-signature, host, content-length and content-type itself, and connection, keep-alive, proxy-connection, proxy-authenticate, proxy-authorization, te, trailer, transfer-encoding and upgrade belong to the connection";
    }
    if code == 4 {
        return "a header name is given twice";
    }
    if code == 5 {
        return "a header value is 1 to 512 characters";
    }
    if code == 6 {
        return "a header value is visible ASCII and spaces: no control character (CR, LF and NUL included) and nothing outside ASCII";
    }
    if code == 7 {
        return "a header value does not begin or end with a space";
    }
    if code == 8 {
        return "the headers are too large (2,048 bytes as sent, 4,096 as stored)";
    }
    return "the headers must be an object of header names and string values";
}

// Is `c` a character of an HTTP token (RFC 9110 5.6.2)?
fn token_byte(c: int) -> [] bool {
    if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' {
        return true;
    }
    return c == '!' || c == '#' || c == '$' || c == '%' || c == '&' || c == 39 || c == '*' || c == '+' || c == '-' || c == '.' || c == '^' || c == '_' || c == 96 || c == '|' || c == '~';
}

fn lower(c: int) -> [] int {
    if c >= 'A' && c <= 'Z' {
        return c + 32;
    }
    return c;
}

// Is `name` (any case) the lowercase `want`?
fn is_named[&a, &b](name: &a [byte], want: &b [byte]) -> [] bool {
    if len(name) != len(want) {
        return false;
    }
    var i = 0;
    while i < len(name) {
        if lower(int_of(name[i])) != int_of(want[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Is `name` one that may not be set?
pub fn forbidden[&n](name: &n [byte]) -> [] bool {
    return is_named(name, "webhook-id") || is_named(name, "webhook-timestamp") || is_named(name, "webhook-signature") || is_named(name, "host") || is_named(name, "content-length") || is_named(name, "content-type") || is_named(name, "connection") || is_named(name, "keep-alive") || is_named(name, "proxy-connection") || is_named(name, "proxy-authenticate") || is_named(name, "proxy-authorization") || is_named(name, "te") || is_named(name, "trailer") || is_named(name, "transfer-encoding") || is_named(name, "upgrade");
}

// 0 if `name` is a header name that may be set, else the code of the refusal.
pub fn check_name[&n](name: &n [byte]) -> [] int {
    if len(name) < 1 || len(name) > max_name() {
        return bad_name();
    }
    var i = 0;
    while i < len(name) {
        if !token_byte(int_of(name[i])) {
            return bad_name();
        }
        i = i + 1;
    }
    if forbidden(name) {
        return forbidden_name();
    }
    return 0;
}

// 0 if `value` is a header value that may be sent, else the code of the refusal.
pub fn check_value[&v](value: &v [byte]) -> [] int {
    if len(value) < 1 || len(value) > max_value() {
        return bad_length();
    }
    var i = 0;
    while i < len(value) {
        let c = int_of(value[i]);
        if c < 32 || c > 126 {
            return bad_byte();
        }
        i = i + 1;
    }
    if int_of(value[0]) == ' ' || int_of(value[len(value) - 1]) == ' ' {
        return edge_space();
    }
    return 0;
}

// Is `a` the same name as `b`, ignoring case?
pub fn same_name[&a, &b](a: &a [byte], b: &b [byte]) -> [] bool {
    if len(a) != len(b) {
        return false;
    }
    var i = 0;
    while i < len(a) {
        if lower(int_of(a[i])) != lower(int_of(b[i])) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

fn is_unreserved(c: int) -> [] bool {
    return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '.' || c == '_' || c == '~';
}

fn hex_digit(n: int) -> [] int {
    if n < 10 {
        return '0' + n;
    }
    return 'A' + n - 10;
}

// `value` percent-encoded onto `out` at `at`: every byte but the unreserved ones as `%XX`. Answers the new end, or -1 if it would pass `limit`.
pub fn encode_value[&v, &o](value: &v [byte], out: &!o [byte], at: int, limit: int) -> [] int {
    var n = at;
    var i = 0;
    while i < len(value) {
        let c = int_of(value[i]);
        if is_unreserved(c) {
            if n + 1 > limit {
                return 0 - 1;
            }
            out[n] = byte_of(c);
            n = n + 1;
        } else {
            if n + 3 > limit {
                return 0 - 1;
            }
            out[n] = byte_of('%');
            out[n + 1] = byte_of(hex_digit(c >> 4 & 15));
            out[n + 2] = byte_of(hex_digit(c & 15));
            n = n + 3;
        }
        i = i + 1;
    }
    return n;
}

fn hex_value(c: int) -> [] int {
    if c >= '0' && c <= '9' {
        return c - '0';
    }
    if c >= 'a' && c <= 'f' {
        return c - 'a' + 10;
    }
    if c >= 'A' && c <= 'F' {
        return c - 'A' + 10;
    }
    return 0 - 1;
}

// Walk the spec `spec`: judge every header, and with `write` put the wire form into `out` (from 0). Answers `(code, wire length, count)`: code 0 if the
// spec is good. `out` need not be long (a slice of no integers will do) when `write` is false.
fn walk[&s, &o](spec: &s [byte], out: &!o [int], write: bool) -> [] (int, int, int) {
    if len(spec) > max_spec() {
        return (too_large(), 0, 0);
    }
    var at = 0;
    var wire = 0;
    var count = 0;
    while at < len(spec) {
        var end = at;
        while end < len(spec) && int_of(spec[end]) != ',' {
            end = end + 1;
        }
        // the header is spec[at..end): name, ':', encoded value
        var colon = at;
        while colon < end && int_of(spec[colon]) != ':' {
            colon = colon + 1;
        }
        if colon >= end {
            return (malformed(), 0, count);
        }
        if count >= max_headers() {
            return (too_many(), 0, count);
        }
        let name = spec[at..colon];
        let nc = check_name(name);
        if nc != 0 {
            return (nc, 0, count);
        }
        // not given before: every earlier header's name is compared, walking the spec again from its start
        var p = 0;
        var k = 0;
        while k < count {
            var pe = p;
            while pe < len(spec) && int_of(spec[pe]) != ',' {
                pe = pe + 1;
            }
            var pc = p;
            while pc < pe && int_of(spec[pc]) != ':' {
                pc = pc + 1;
            }
            if same_name(spec[p..pc], name) {
                return (repeated_name(), 0, count);
            }
            p = pe + 1;
            k = k + 1;
        }
        // the value, decoded: `%XX`, or a visible ASCII byte raw
        let enc = spec[colon + 1..end];
        var len_v = 0;
        var last = 0;
        var bad = 0;
        var j = 0;
        let value_at = wire + len(name) + 2;
        while j < len(enc) && bad == 0 {
            let c = int_of(enc[j]);
            var b = 0 - 1;
            if c == '%' {
                if j + 2 >= len(enc) {
                    bad = malformed();
                } else {
                    let hi = hex_value(int_of(enc[j + 1]));
                    let lo = hex_value(int_of(enc[j + 2]));
                    if hi < 0 || lo < 0 {
                        bad = malformed();
                    } else {
                        b = hi * 16 + lo;
                        j = j + 3;
                    }
                }
            } else if c > 32 && c < 127 {
                b = c;
                j = j + 1;
            } else {
                bad = bad_byte();
            }
            if bad == 0 {
                if b < 32 || b > 126 {
                    bad = bad_byte();
                } else if len_v == 0 && b == ' ' {
                    bad = edge_space();
                } else if wire + len(name) + 4 + len_v + 1 > max_wire() {
                    bad = too_large();
                } else {
                    if write {
                        out[value_at + len_v] = b;
                    }
                    len_v = len_v + 1;
                    last = b;
                }
            }
        }
        if bad != 0 {
            return (bad, 0, count);
        }
        if len_v < 1 || len_v > max_value() {
            return (bad_length(), 0, count);
        }
        if last == ' ' {
            return (edge_space(), 0, count);
        }
        if write {
            var q = 0;
            while q < len(name) {
                out[wire + q] = int_of(name[q]);
                q = q + 1;
            }
            out[wire + len(name)] = ':';
            out[wire + len(name) + 1] = ' ';
            out[wire + len(name) + 2 + len_v] = '\r';
            out[wire + len(name) + 3 + len_v] = '\n';
        }
        wire = wire + len(name) + 4 + len_v;
        if wire > max_wire() {
            return (too_large(), 0, count);
        }
        count = count + 1;
        at = end + 1;
    }
    if len(spec) > 0 && int_of(spec[len(spec) - 1]) == ',' {
        return (malformed(), 0, count);
    }
    return (0, wire, count);
}

// 0 if `spec` is a good spec (the empty one is: no headers), else the code of the first refusal.
pub fn check_spec[&s](spec: &s [byte]) -> [] int {
    region a {
        let none = alloc_slice[a](1, 0);
        return walk(spec, none, false).0;
    }
}

// The wire form of the good spec `spec` into `out` as one byte to an integer, from 0: `Name: value\r\n` for each header. Answers the number of bytes,
// or -1 if the spec is not good or `out` is shorter than `max_wire()`.
pub fn decode[&s, &o](spec: &s [byte], out: &!o [int]) -> [] int {
    if len(out) < max_wire() {
        return 0 - 1;
    }
    let r = walk(spec, out, true);
    if r.0 != 0 {
        return 0 - 1;
    }
    return r.1;
}

// How many headers a good spec has.
pub fn count_of[&s](spec: &s [byte]) -> [] int {
    region a {
        let none = alloc_slice[a](1, 0);
        return walk(spec, none, false).2;
    }
}
