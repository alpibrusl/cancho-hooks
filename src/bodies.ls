edition 5;

module bodies;

import std.chacha20;
import std.crypto;
import record;

// `bodies` -- the bodies of events encrypted at rest (`docs/design.md` section 47.4).
//
// With `encryption-key-file` (32 random bytes, or 64 hexadecimal digits) each event's body is stored as `nonce (12) | ciphertext | tag (16)` sealed with
// ChaCha20-Poly1305 (RFC 8439, lex-sys's own `std.chacha20`: no foreign call), with the event's id as the associated data, so a sealed body cannot be moved to
// another record. The record says which key sealed it: a pair `x` of four bytes, the start of the key's SHA-256, so that a key can be rotated
// (`encryption-key-file-old` opens what the key before sealed, until retention has dropped it). The nonce is a prefix of four random bytes chosen at each start,
// then the id: a restore that hands out an id again after a restart does not reuse a nonce unless the prefix comes up again (one in 2^32).
//
// The state is `size()` integers, the caller's: [0] 1 when bodies are encrypted   [1] 1 when there is a key before it   [2] the key's fingerprint
// [3] the old key's   [4] the nonce prefix   [8 .. 40] the key, a byte to an integer   [40 .. 72] the old key   [72] bodies sealed   [73] bodies opened
// [74] bodies that would not open

pub fn size() -> [] int {
    return 80;
}

pub fn overhead() -> [] int {
    return 28;
}

pub fn on[&s](st: &s [int]) -> [] bool {
    return st[0] == 1;
}

pub fn fingerprint[&s](st: &s [int]) -> [] int {
    return st[2];
}

pub fn sealed_count[&s](st: &s [int]) -> [] int {
    return st[72];
}

pub fn opened_count[&s](st: &s [int]) -> [] int {
    return st[73];
}

pub fn refused_count[&s](st: &s [int]) -> [] int {
    return st[74];
}

fn hexval(c: int) -> [] int {
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

// The 32 bytes of a key file's contents `text` (32 raw bytes, or 64 hexadecimal digits and an optional newline) into `out`. Answers 0, or -1 if it is neither.
pub fn parse_key[&t, &o](text: &t [byte], out: &!o [byte]) -> [] int {
    var n = len(text);
    while n > 0 && (int_of(text[n - 1]) == '\n' || int_of(text[n - 1]) == '\r') {
        n = n - 1;
    }
    if n == 64 {
        var i = 0;
        while i < 32 {
            let hi = hexval(int_of(text[2 * i]));
            let lo = hexval(int_of(text[2 * i + 1]));
            if hi < 0 || lo < 0 {
                return 0 - 1;
            }
            out[i] = byte_of(hi * 16 + lo);
            i = i + 1;
        }
        return 0;
    }
    if len(text) == 32 {
        var i = 0;
        while i < 32 {
            out[i] = text[i];
            i = i + 1;
        }
        return 0;
    }
    return 0 - 1;
}

// The first four bytes of the key's SHA-256, as an integer: what a record names its key by.
fn print_of[&k](key: &k [byte]) -> [] int {
    var fp = 0;
    region a {
        let d = alloc_slice[a](32, byte_of(0));
        crypto.sha256(key, d);
        fp = int_of(d[0]) * 16777216 + int_of(d[1]) * 65536 + int_of(d[2]) * 256 + int_of(d[3]);
    }
    return fp;
}

// Keep the key (`which` 0) or the old key (1) in the state.
pub fn set_key[&s, &k](st: &!s [int], which: int, key: &k [byte], prefix: int) -> [] int {
    let at = 8 + 32 * which;
    var i = 0;
    while i < 32 {
        st[at + i] = int_of(key[i]);
        i = i + 1;
    }
    st[2 + which] = print_of(key);
    if which == 0 {
        st[0] = 1;
        st[4] = prefix;
    } else {
        st[1] = 1;
    }
    return 0;
}

fn key_into[&s, &o](st: &s [int], which: int, out: &!o [byte]) -> [] int {
    let at = 8 + 32 * which;
    var i = 0;
    while i < 32 {
        out[i] = byte_of(st[at + i]);
        i = i + 1;
    }
    return 0;
}

fn put_le[&o](out: &!o [byte], at: int, v: int, n: int) -> [] int {
    var x = v;
    var i = 0;
    while i < n {
        out[at + i] = byte_of(x % 256);
        x = x / 256;
        i = i + 1;
    }
    return 0;
}

// The fingerprint named by the record in `buf` at `at` (its pair `x`), or -1 if the record's body is not sealed.
pub fn sealed_by[&b](buf: &b [byte], at: int) -> [] int {
    var p = record.first_pair(at);
    var f = 0;
    while f < record.fields_of(buf, at) {
        let pr = record.pair_at(buf, p);
        if pr.1 == 1 && int_of(buf[pr.0]) == 'x' && pr.3 == 4 {
            return int_of(buf[pr.2]) * 16777216 + int_of(buf[pr.2 + 1]) * 65536 + int_of(buf[pr.2 + 2]) * 256 + int_of(buf[pr.2 + 3]);
        }
        p = pr.4;
        f = f + 1;
    }
    return 0 - 1;
}

// The four bytes a record names its key by, into `out`.
pub fn print_bytes[&s, &o](st: &s [int], out: &!o [byte]) -> [] int {
    let fp = st[2];
    out[0] = byte_of(fp / 16777216 % 256);
    out[1] = byte_of(fp / 65536 % 256);
    out[2] = byte_of(fp / 256 % 256);
    out[3] = byte_of(fp % 256);
    return 4;
}

// Seal `body` for event `id` into `out` (at least `len(body) + overhead()`): nonce, ciphertext, tag. Answers the length written, or -1.
pub fn seal[&s, &b, &o](st: &!s [int], id: int, body: &b [byte], out: &!o [byte]) -> [] int {
    var n = 0 - 1;
    region a {
        let key = alloc_slice[a](32, byte_of(0));
        let aad = alloc_slice[a](8, byte_of(0));
        let nonce = alloc_slice[a](12, byte_of(0));
        key_into(st, 0, key);
        put_le(nonce, 0, st[4], 4);
        put_le(nonce, 4, id, 8);
        put_le(aad, 0, id, 8);
        if chacha20.seal(key, nonce, aad, body, out[12..12 + len(body) + 16]) == chacha20.ok() {
            var k = 0;
            while k < 12 {
                out[k] = nonce[k];
                k = k + 1;
            }
            n = 12 + len(body) + 16;
            st[72] = st[72] + 1;
        }
        var i = 0;
        while i < 32 {
            key[i] = byte_of(0);
            i = i + 1;
        }
    }
    return n;
}

// Open the sealed body of event `id`, sealed by the key whose fingerprint is `fp`, into `out` (at least `len(sealed) - overhead()`). Answers the length of the
// body, or -1 if no key the service has is that one, or the tag does not match (the record was changed, or moved to another id).
pub fn open[&s, &b, &o](st: &!s [int], id: int, fp: int, sealed: &b [byte], out: &!o [byte]) -> [] int {
    var which = 0 - 1;
    if st[0] == 1 && st[2] == fp {
        which = 0;
    } else if st[1] == 1 && st[3] == fp {
        which = 1;
    }
    if which < 0 || len(sealed) < overhead() {
        st[74] = st[74] + 1;
        return 0 - 1;
    }
    var n = 0 - 1;
    region a {
        let key = alloc_slice[a](32, byte_of(0));
        let aad = alloc_slice[a](8, byte_of(0));
        key_into(st, which, key);
        put_le(aad, 0, id, 8);
        let plain = len(sealed) - overhead();
        if chacha20.open(key, sealed[0..12], aad, sealed[12..len(sealed)], out[0..plain]) == chacha20.ok() {
            n = plain;
            st[73] = st[73] + 1;
        } else {
            st[74] = st[74] + 1;
        }
        var i = 0;
        while i < 32 {
            key[i] = byte_of(0);
            i = i + 1;
        }
    }
    return n;
}
