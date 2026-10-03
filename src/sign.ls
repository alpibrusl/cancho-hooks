edition 5;

module sign;

import std.crypto;

// `sign` -- Standard Webhooks signatures (`docs/design.md` section 4; https://www.standardwebhooks.com).
//
// The scheme, as the specification states it: three headers, `webhook-id`, `webhook-timestamp` (integer seconds since the
// epoch) and `webhook-signature`; the signed content is `<id>.<timestamp>.<payload>`; the signature is HMAC-SHA256 of it,
// base64-encoded, behind the version tag `v1,`; several signatures in one header are separated by spaces (key rotation); a
// secret is base64, prefixed `whsec_`. **The key is the base64-*decoded* secret**, which the specification's text does not
// say in so many words and every implementation does; the test (`tests/sign_test.py`) compares against the reference
// Python library for exactly that reason.
//
// Nothing here has a capability: it is arithmetic on slices. The one allocation, a copy of the message in front of the key
// pad, comes from a `Heap` the caller lends, because a payload can be 64 KiB and an arena is not.

fn alphabet() -> [] &static [byte] {
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
}

static decode_table: [int] {
    let table = alloc_slice[static](256, 0 - 1);
    let alpha = alphabet();
    var i = 0;
    while i < len(alpha) {
        table[int_of(alpha[i])] = i;
        i = i + 1;
    }
    return table;
}

// RFC 4648 section 4, with padding. `out` must hold `4 * ((len(data) + 2) / 3)` bytes; answers the length written.
pub fn b64_encode[&d, &o](data: &d [byte], out: &!o [byte]) -> [] int {
    let table = alphabet();
    var at = 0;
    var i = 0;
    while i + 3 <= len(data) {
        let bits = int_of(data[i]) << 16 | int_of(data[i + 1]) << 8 | int_of(data[i + 2]);
        out[at] = table[bits >> 18 & 0x3f];
        out[at + 1] = table[bits >> 12 & 0x3f];
        out[at + 2] = table[bits >> 6 & 0x3f];
        out[at + 3] = table[bits & 0x3f];
        at = at + 4;
        i = i + 3;
    }
    if len(data) - i == 1 {
        let bits = int_of(data[i]) << 16;
        out[at] = table[bits >> 18 & 0x3f];
        out[at + 1] = table[bits >> 12 & 0x3f];
        out[at + 2] = byte_of('=');
        out[at + 3] = byte_of('=');
        at = at + 4;
    } else if len(data) - i == 2 {
        let bits = int_of(data[i]) << 16 | int_of(data[i + 1]) << 8;
        out[at] = table[bits >> 18 & 0x3f];
        out[at + 1] = table[bits >> 12 & 0x3f];
        out[at + 2] = table[bits >> 6 & 0x3f];
        out[at + 3] = byte_of('=');
        at = at + 4;
    }
    return at;
}

// Strict decoding: a multiple of four characters, padding only at the end and only as the alphabet's last group allows.
// Answers the length written to `out`, or -1 for input that is not base64.
pub fn b64_decode[&t, &o](text: &t [byte], out: &!o [byte]) -> [] int {
    if len(text) % 4 != 0 {
        return 0 - 1;
    }
    var at = 0;
    var i = 0;
    while i < len(text) {
        let last = i + 4 == len(text);
        var pad = 0;
        if last && int_of(text[i + 3]) == '=' {
            pad = 1;
            if int_of(text[i + 2]) == '=' {
                pad = 2;
            }
        }
        var bits = 0;
        var k = 0;
        while k < 4 - pad {
            let v = decode_table[int_of(text[i + k])];
            if v < 0 {
                return 0 - 1;
            }
            bits = bits << 6 | v;
            k = k + 1;
        }
        bits = bits << 6 * pad;
        out[at] = byte_of(bits >> 16 & 0xff);
        at = at + 1;
        if pad < 2 {
            out[at] = byte_of(bits >> 8 & 0xff);
            at = at + 1;
        }
        if pad < 1 {
            out[at] = byte_of(bits & 0xff);
            at = at + 1;
        }
        i = i + 4;
    }
    return at;
}

// HMAC-SHA256 (RFC 2104, block size 64) of `msg` under `key`, 32 bytes into `out`. Answers 0.
pub fn hmac_sha256[&h, &k, &m, &o](heap: &!h Heap, key: &k [byte], msg: &m [byte], out: &!o [byte]) -> [heap] int {
    region a {
        let block = alloc_slice[a](64, byte_of(0));
        if len(key) > 64 {
            crypto.sha256(key, block[0..32]);
        } else {
            var i = 0;
            while i < len(key) {
                block[i] = key[i];
                i = i + 1;
            }
        }
        // inner = (key ^ ipad) || msg
        let inner = box_slice(heap, 64 + len(msg), byte_of(0));
        let digest = alloc_slice[a](32, byte_of(0));
        borrow mut inner as &!iw in {
            let s = contents(iw);
            var i = 0;
            while i < 64 {
                s[i] = byte_of(int_of(block[i]) ^ 0x36);
                i = i + 1;
            }
            var j = 0;
            while j < len(msg) {
                s[64 + j] = msg[j];
                j = j + 1;
            }
            crypto.sha256(s[0..64 + len(msg)], digest);
        }
        unbox_slice(heap, inner);
        // outer = (key ^ opad) || inner digest
        let outer = alloc_slice[a](96, byte_of(0));
        var i = 0;
        while i < 64 {
            outer[i] = byte_of(int_of(block[i]) ^ 0x5c);
            i = i + 1;
        }
        var j = 0;
        while j < 32 {
            outer[64 + j] = digest[j];
            j = j + 1;
        }
        crypto.sha256(outer, out);
    }
    return 0;
}

// The decimal text of `n` (not negative) into `out`; answers its length. At most 19 bytes.
pub fn nat_text[&o](n: int, out: &!o [byte]) -> [] int {
    var digits = 1;
    var t = n / 10;
    while t > 0 {
        digits = digits + 1;
        t = t / 10;
    }
    var v = n;
    var i = digits;
    while i > 0 {
        i = i - 1;
        out[i] = byte_of(48 + v % 10);
        v = v / 10;
    }
    return digits;
}

// The key a secret stands for: `whsec_` is dropped if present and the rest decoded from base64, into `out`. Answers the key's
// length, or -1 if the secret is not base64.
pub fn secret_key[&s, &o](secret: &s [byte], out: &!o [byte]) -> [] int {
    var from = 0;
    if len(secret) >= 6 && int_of(secret[0]) == 'w' && int_of(secret[1]) == 'h' && int_of(secret[2]) == 's' && int_of(secret[3]) == 'e' && int_of(secret[4]) == 'c' && int_of(secret[5]) == '_' {
        from = 6;
    }
    return b64_decode(secret[from..len(secret)], out);
}

// The `webhook-signature` value for a message: `v1,` and the base64 of HMAC-SHA256 over `<id>.<timestamp>.<payload>` under
// `key`, into `out` (at least 47 bytes). `timestamp` is its decimal text. Answers the length written, 47.
pub fn signature[&h, &k, &i, &t, &p, &o](heap: &!h Heap, key: &k [byte], id: &i [byte], timestamp: &t [byte], payload: &p [byte], out: &!o [byte]) -> [heap] int {
    let signed = box_slice(heap, len(id) + len(timestamp) + len(payload) + 2, byte_of(0));
    var at = 0;
    region a {
        let mac = alloc_slice[a](32, byte_of(0));
        borrow mut signed as &!sw in {
            let s = contents(sw);
            var j = 0;
            while j < len(id) {
                s[at] = id[j];
                at = at + 1;
                j = j + 1;
            }
            s[at] = byte_of('.');
            at = at + 1;
            j = 0;
            while j < len(timestamp) {
                s[at] = timestamp[j];
                at = at + 1;
                j = j + 1;
            }
            s[at] = byte_of('.');
            at = at + 1;
            j = 0;
            while j < len(payload) {
                s[at] = payload[j];
                at = at + 1;
                j = j + 1;
            }
            hmac_sha256(heap, key, s[0..at], mac);
        }
        out[0] = byte_of('v');
        out[1] = byte_of('1');
        out[2] = byte_of(',');
        b64_encode(mac, out[3..47]);
    }
    unbox_slice(heap, signed);
    return 47;
}
