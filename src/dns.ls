edition 5;

module dns;

// `dns` -- the part of a DNS client that is only bytes: build a query for an A record, read the answer (`docs/design.md` section 40).
//
// Pure, and total: every index is checked against the length of the message before it is used, so a hostile or damaged answer is an error code
// and never a trap (`tests/dns_test.ls`). It does not touch the network; `attempt.ls` makes the connection to the name server. Written for the
// lex-sys TLS spike (`docs/tls-nonblocking.md` there, `examples/tls_nb/dns.ls`) and used here as it was found, with the comments of this service.
//
// An answer is the addresses of every A record in it (a CNAME chain is followed by ignoring the names, which is sound for this purpose because the
// resolver we asked has already done the chasing and only the A records at the end hold an address), as packed integers
// `a * 2^24 + b * 2^16 + c * 2^8 + d` (the form `destination.address` uses), and the smallest TTL among them. AAAA records are neither asked for nor
// read: a name with only an IPv6 address has no address here.

pub fn max_addrs() -> [] int {
    return 8;
}

// The size of the `addrs` array `parse` writes: `max_addrs()` addresses, then the smallest TTL in seconds.
pub fn addrs_size() -> [] int {
    return 9;
}

// Answers of `parse` below zero.
pub fn malformed() -> [] int {
    return 0 - 1;
}

pub fn wrong_id() -> [] int {
    return 0 - 2;
}

pub fn not_a_response() -> [] int {
    return 0 - 3;
}

pub fn truncated() -> [] int {
    return 0 - 4;
}

// `0 - 100 - rcode` for a response code other than 0: NXDOMAIN (3) is -103, SERVFAIL (2) is -102, REFUSED (5) is -105.
pub fn rcode_error(rcode: int) -> [] int {
    return 0 - 100 - rcode;
}

// Write a query for the A record of `name` (`hooks.example.com`, an optional final dot) with the given `id` at `out[at..]`.
// Answers the number of bytes written, or -1 if `name` is not a name a query can carry: empty, a label empty or over 63
// bytes, the whole over 253, or `out` too small (a query needs `len(name) + 18` bytes at most).
pub fn build_query[&n, &o](name: &n [byte], id: int, out: &!o [byte], at: int) -> [] int {
    var nl = len(name);
    if nl > 0 && int_of(name[nl - 1]) == '.' {
        nl = nl - 1;
    }
    if nl == 0 || nl > 253 || at < 0 || len(out) < at + nl + 18 {
        return 0 - 1;
    }
    out[at] = byte_of(id / 256 % 256);
    out[at + 1] = byte_of(id % 256);
    // Flags: a query, recursion desired. One question, nothing else.
    out[at + 2] = byte_of(1);
    out[at + 3] = byte_of(0);
    out[at + 4] = byte_of(0);
    out[at + 5] = byte_of(1);
    var z = 6;
    while z < 12 {
        out[at + z] = byte_of(0);
        z = z + 1;
    }
    // The name as labels: each is a length byte and its bytes, ended by a zero.
    var p = at + 12;
    var label_at = p;
    p = p + 1;
    var label_len = 0;
    var i = 0;
    while i < nl {
        let c = int_of(name[i]);
        if c == '.' {
            if label_len == 0 || label_len > 63 {
                return 0 - 1;
            }
            out[label_at] = byte_of(label_len);
            label_at = p;
            p = p + 1;
            label_len = 0;
        } else {
            out[p] = name[i];
            p = p + 1;
            label_len = label_len + 1;
        }
        i = i + 1;
    }
    if label_len == 0 || label_len > 63 {
        return 0 - 1;
    }
    out[label_at] = byte_of(label_len);
    out[p] = byte_of(0);
    p = p + 1;
    // Type A (1), class IN (1).
    out[p] = byte_of(0);
    out[p + 1] = byte_of(1);
    out[p + 2] = byte_of(0);
    out[p + 3] = byte_of(1);
    return p + 4 - at;
}

fn byte_at[&m](msg: &m [byte], n: int, i: int) -> [] int {
    if i < 0 || i >= n || i >= len(msg) {
        return 0 - 1;
    }
    return int_of(msg[i]);
}

fn u16_at[&m](msg: &m [byte], n: int, i: int) -> [] int {
    let a = byte_at(msg, n, i);
    let b = byte_at(msg, n, i + 1);
    if a < 0 || b < 0 {
        return 0 - 1;
    }
    return a * 256 + b;
}

// The offset just past the name that starts at `i`: labels, ended by a zero byte or by a two-byte compression pointer. -1 if it
// runs off the message, a label's length byte is one of the reserved forms (the top bits `01` or `10`), or there are more
// labels than a name can have (which a loop of pointers would otherwise keep us in).
fn skip_name[&m](msg: &m [byte], n: int, from: int) -> [] int {
    var i = from;
    var steps = 0;
    while steps < 130 {
        let b = byte_at(msg, n, i);
        if b < 0 {
            return 0 - 1;
        }
        if b == 0 {
            return i + 1;
        }
        if b >= 192 {
            if byte_at(msg, n, i + 1) < 0 {
                return 0 - 1;
            }
            return i + 2;
        }
        if b >= 64 {
            return 0 - 1;
        }
        i = i + 1 + b;
        steps = steps + 1;
    }
    return 0 - 1;
}

// Read the answer in `resp[0..n]` to the query with `id`. Writes the addresses of its A records into `addrs[0..max_addrs()]` and
// the smallest TTL into `addrs[max_addrs()]`, and answers how many addresses it wrote (0 is a valid answer: a name with no A
// record), or a negative: `malformed()`, `wrong_id()`, `not_a_response()`, `truncated()` (the TC bit: ask again over TCP, or
// do not use the answer), or `rcode_error(rcode)`.
pub fn parse[&r, &a](resp: &r [byte], n: int, id: int, addrs: &!a [int]) -> [] int {
    if len(addrs) < addrs_size() {
        return malformed();
    }
    var z = 0;
    while z < addrs_size() {
        addrs[z] = 0;
        z = z + 1;
    }
    if n < 12 {
        return malformed();
    }
    let rid = u16_at(resp, n, 0);
    let flags = u16_at(resp, n, 2);
    let qd = u16_at(resp, n, 4);
    let an = u16_at(resp, n, 6);
    if rid < 0 || flags < 0 || qd < 0 || an < 0 {
        return malformed();
    }
    if rid != id % 65536 {
        return wrong_id();
    }
    if flags / 32768 % 2 != 1 {
        return not_a_response();
    }
    if flags / 512 % 2 == 1 {
        return truncated();
    }
    let rcode = flags % 16;
    if rcode != 0 {
        return rcode_error(rcode);
    }
    var at = 12;
    var q = 0;
    while q < qd {
        at = skip_name(resp, n, at);
        if at < 0 || at + 4 > n {
            return malformed();
        }
        at = at + 4;
        q = q + 1;
    }
    var found = 0;
    var min_ttl = 0 - 1;
    var rr = 0;
    while rr < an {
        at = skip_name(resp, n, at);
        if at < 0 || at + 10 > n {
            return malformed();
        }
        let rtype = u16_at(resp, n, at);
        let rclass = u16_at(resp, n, at + 2);
        let ttl = u16_at(resp, n, at + 4) * 65536 + u16_at(resp, n, at + 6);
        let rdlen = u16_at(resp, n, at + 8);
        at = at + 10;
        if rdlen < 0 || at + rdlen > n {
            return malformed();
        }
        if rtype == 1 && rclass == 1 && rdlen == 4 && found < max_addrs() {
            addrs[found] = int_of(resp[at]) * 16777216 + int_of(resp[at + 1]) * 65536 + int_of(resp[at + 2]) * 256 + int_of(resp[at + 3]);
            found = found + 1;
            if min_ttl < 0 || ttl < min_ttl {
                min_ttl = ttl;
            }
        }
        at = at + rdlen;
        rr = rr + 1;
    }
    if min_ttl >= 0 {
        addrs[max_addrs()] = min_ttl;
    }
    return found;
}

// A packed address as dotted decimal into `out[at..]`; answers the number of bytes written (7 to 15). `out` must have 15 free.
pub fn put_dotted[&o](out: &!o [byte], at: int, a: int) -> [] int {
    var p = at;
    var shift = 16777216;
    var k = 0;
    while k < 4 {
        let octet = a / shift % 256;
        if octet >= 100 {
            out[p] = byte_of('0' + octet / 100);
            p = p + 1;
        }
        if octet >= 10 {
            out[p] = byte_of('0' + octet / 10 % 10);
            p = p + 1;
        }
        out[p] = byte_of('0' + octet % 10);
        p = p + 1;
        if k < 3 {
            out[p] = byte_of('.');
            p = p + 1;
        }
        shift = shift / 256;
        k = k + 1;
    }
    return p - at;
}
