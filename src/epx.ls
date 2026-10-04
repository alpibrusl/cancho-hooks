edition 5;

module epx;

import std.json;
import filter;
import hdrs;

// `epx` -- what an endpoint has besides an address and a secret (`docs/design.md` section 35): the event types it subscribes to, the custom
// headers every attempt carries, and the previous secret while a rotation overlaps. These are kept in the delivery state, one **row** per
// endpoint in the table's order (the same index as `endpoints.ls`'s table, shifted down by a delete the same way), of `stride()` integers, a
// byte to an integer like the admin token:
//
//     [0] types length   [1] header wire length   [2] old key length   [3] old key valid until (Unix ms; 0: none)
//     [4 .. 516)     the subscription, comma separated (`filter.ls`)
//     [516 .. 2564)  the headers as they go on the wire, `Name: value\r\n` each (`hdrs.ls`)
//     [2564 .. 2660) the previous secret's key, as `sign.secret_key` decodes it
//
// Fixed room for each, so nothing here ever needs to be compacted or can run out: a row is replaced in place.
//
// The change a `POST` or `PATCH` asks for waits for the database in the `xg` block (like `manage.ls`'s `mg`): which members it names, the
// subscription, the headers as a spec (`hdrs.ls`: the form the database holds), and the time the previous secret stays valid until.
//
//     [0] members named: 1 types, 2 headers, 4 keep   [1] types length   [2] spec length   [3] keep until (Unix ms; 0: end the overlap now)
//     [8 .. 520)     the subscription        [520 .. 4616)  the spec

pub fn stride() -> [] int {
    return 2660;
}

pub fn rows() -> [] int {
    return 62;
}

pub fn xt_size() -> [] int {
    return 62 * 2660;
}

pub fn xg_size() -> [] int {
    return 4616;
}

pub fn types_at() -> [] int {
    return 4;
}

pub fn wire_at() -> [] int {
    return 516;
}

pub fn old_at() -> [] int {
    return 2564;
}

pub fn xg_types_at() -> [] int {
    return 8;
}

pub fn xg_spec_at() -> [] int {
    return 520;
}

// The longest a previous secret can be kept: 30 days, in ms.
pub fn max_keep_ms() -> [] int {
    return 2592000000;
}

pub fn m_types() -> [] int {
    return 1;
}

pub fn m_headers() -> [] int {
    return 2;
}

pub fn m_keep() -> [] int {
    return 4;
}

// The refusals that are not the subscription's or the headers' own (those are theirs plus 100 and 200).
pub fn why(code: int) -> [] &static [byte] {
    if code > 100 && code < 200 {
        return filter.why(code - 100);
    }
    if code > 200 && code < 300 {
        return hdrs.why(code - 200);
    }
    if code == 301 {
        return "\"types\" must be an array of event type patterns (strings); an empty array means every event";
    }
    if code == 302 {
        return "\"headers\" must be an object of header names and string values; an empty object removes them all";
    }
    if code == 303 {
        return "\"keep_old_ms\" must be an integer from 0 to 2592000000 (30 days)";
    }
    if code == 304 {
        return "\"keep_old\" must be true or false, and cannot be given with \"keep_old_ms\"";
    }
    if code == 305 {
        return "there is no previous secret to keep: give a new \"secret\" (or \"rotate\") with \"keep_old_ms\"";
    }
    return "the request is not valid";
}

// ---------------------------------------------------------------------
// The rows
// ---------------------------------------------------------------------

fn base(i: int) -> [] int {
    return i * stride();
}

pub fn types_len[&x](xt: &x [int], i: int) -> [] int {
    return xt[base(i)];
}

pub fn wire_len[&x](xt: &x [int], i: int) -> [] int {
    return xt[base(i) + 1];
}

pub fn old_len[&x](xt: &x [int], i: int) -> [] int {
    return xt[base(i) + 2];
}

pub fn old_until[&x](xt: &x [int], i: int) -> [] int {
    return xt[base(i) + 3];
}

// The wire form's byte `k` of the headers of row `i`.
pub fn wire_byte[&x](xt: &x [int], i: int, k: int) -> [] int {
    return xt[base(i) + wire_at() + k];
}

pub fn old_byte[&x](xt: &x [int], i: int, k: int) -> [] int {
    return xt[base(i) + old_at() + k];
}

// Is the previous secret of row `i` still to be signed with at `now` (Unix ms)?
pub fn old_active[&x](xt: &x [int], i: int, now: int) -> [] bool {
    return xt[base(i) + 2] > 0 && now < xt[base(i) + 3];
}

// Does the subscription of row `i` want an event of type `typ` (the bytes of the record's `typ` pair; empty for an event with none)?
pub fn accepts[&x, &t](xt: &x [int], i: int, typ: &t [byte]) -> [] bool {
    let n = xt[base(i)];
    if n == 0 {
        return true;
    }
    return filter.accepts(xt[base(i) + types_at()..base(i) + types_at() + n], n, typ);
}

// Row `i` empty: no subscription, no headers, no previous secret.
pub fn clear_row[&x](xt: &!x [int], i: int) -> [] int {
    var k = 0;
    while k < stride() {
        xt[base(i) + k] = 0;
        k = k + 1;
    }
    return 0;
}

// Take row `i` out of the first `count`: the rows after it move down one place, the last is cleared, and the removed row is cleared first (it held
// a credential).
pub fn drop_row[&x](xt: &!x [int], count: int, i: int) -> [] int {
    clear_row(xt, i);
    var j = i;
    while j < count - 1 {
        var k = 0;
        while k < stride() {
            xt[base(j) + k] = xt[base(j + 1) + k];
            k = k + 1;
        }
        j = j + 1;
    }
    if count > 0 {
        clear_row(xt, count - 1);
    }
    return 0;
}

// The subscription of row `i` set to `csv` (a good list; the caller has judged it with `filter.check_list`).
pub fn set_types[&x, &c](xt: &!x [int], i: int, csv: &c [byte]) -> [] int {
    var k = 0;
    while k < len(csv) {
        xt[base(i) + types_at() + k] = int_of(csv[k]);
        k = k + 1;
    }
    xt[base(i)] = len(csv);
    return 0;
}

// The headers of row `i` set from `spec` (a good spec, `hdrs.check_spec`). Answers 0, or -1 if the spec is not good (nothing is changed then).
pub fn set_spec[&x, &s](xt: &!x [int], i: int, spec: &s [byte]) -> [] int {
    if len(spec) == 0 {
        xt[base(i) + 1] = 0;
        return 0;
    }
    region a {
        let wire = alloc_slice[a](hdrs.max_wire(), 0);
        let n = hdrs.decode(spec, wire);
        if n < 0 {
            return 0 - 1;
        }
        var k = 0;
        while k < n {
            xt[base(i) + wire_at() + k] = wire[k];
            k = k + 1;
        }
        xt[base(i) + 1] = n;
    }
    return 0;
}

// The previous secret of row `i` set to `key` (the key bytes) valid until `until` (Unix ms); an empty key or an `until` of 0 ends the overlap.
pub fn set_old[&x, &k](xt: &!x [int], i: int, key: &k [byte], until: int) -> [] int {
    if len(key) == 0 || until <= 0 || len(key) > 96 {
        xt[base(i) + 2] = 0;
        xt[base(i) + 3] = 0;
        var z = 0;
        while z < 96 {
            xt[base(i) + old_at() + z] = 0;
            z = z + 1;
        }
        return 0;
    }
    var j = 0;
    while j < len(key) {
        xt[base(i) + old_at() + j] = int_of(key[j]);
        j = j + 1;
    }
    xt[base(i) + 2] = len(key);
    xt[base(i) + 3] = until;
    return 0;
}

// The previous secret of row `i`, which it has, is valid until `until` instead (Unix ms).
pub fn set_old_until[&x](xt: &!x [int], i: int, until: int) -> [] int {
    if xt[base(i) + 2] > 0 {
        xt[base(i) + 3] = until;
    }
    return 0;
}

// An estimate of the bytes the table's text takes for the extras of the first `count` rows (a subscription as it is, the headers as they may be
// written, a previous secret as text): what `POST /endpoints` adds to its check that the whole table still fits in what the service reads at start.
pub fn text_used[&x](xt: &x [int], count: int) -> [] int {
    var total = 0;
    var i = 0;
    while i < count {
        total = total + xt[base(i)] + 2 * xt[base(i) + 1] + 2 * xt[base(i) + 2] + 40;
        i = i + 1;
    }
    return total;
}

// The names of the headers of row `i`, as a JSON array of strings, and the subscription as an array, onto `w`: the members `types`, `headers` and
// `secret_old_until` of `GET /endpoints`. Never a header's value. `secret_old_until` is the time (Unix ms) the previous secret is still signed with,
// 0 if it is not.
pub fn put_members[&h, &x](heap: &!h Heap, w: json.Writer, xt: &x [int], i: int, now: int) -> [heap] json.Writer {
    var out = json.put_key(heap, w, "types");
    out = json.begin_array(heap, out);
    let n = xt[base(i)];
    region a {
        let item = alloc_slice[a](filter.max_pattern() + 1, byte_of(0));
        var at = 0;
        while at < n {
            var end = at;
            while end < n && xt[base(i) + types_at() + end] != ',' {
                end = end + 1;
            }
            var k = 0;
            while k < end - at {
                item[k] = byte_of(xt[base(i) + types_at() + at + k]);
                k = k + 1;
            }
            out = json.put_string(heap, out, item[0..end - at]);
            at = end + 1;
        }
    }
    out = json.end_array(heap, out);
    out = json.put_key(heap, out, "headers");
    out = json.begin_array(heap, out);
    let wn = xt[base(i) + 1];
    region b {
        let name = alloc_slice[b](hdrs.max_name() + 1, byte_of(0));
        var p = 0;
        while p < wn {
            var colon = p;
            while colon < wn && xt[base(i) + wire_at() + colon] != ':' {
                colon = colon + 1;
            }
            var k = 0;
            while k < colon - p && k < hdrs.max_name() {
                name[k] = byte_of(xt[base(i) + wire_at() + p + k]);
                k = k + 1;
            }
            out = json.put_string(heap, out, name[0..k]);
            // to the end of this line
            var eol = colon;
            while eol + 1 < wn && !(xt[base(i) + wire_at() + eol] == '\r' && xt[base(i) + wire_at() + eol + 1] == '\n') {
                eol = eol + 1;
            }
            p = eol + 2;
        }
    }
    out = json.end_array(heap, out);
    out = json.put_key(heap, out, "secret_old_until");
    if old_active(xt, i, now) {
        out = json.put_int(heap, out, xt[base(i) + 3]);
    } else {
        out = json.put_int(heap, out, 0);
    }
    return out;
}

// ---------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------

// Fill `xg` with the members of the body `body` of a `POST /endpoints` (`patch` false) or `PATCH /endpoints/:id` (`patch` true) that this module
// owns: `types`, `headers`, and for a patch `keep_old_ms` or `keep_old` (the previous secret stays valid that long; `keep_old: true` is `grace_ms`).
// `now` is the Unix time in ms. Answers 0, or the code of the refusal (`why`). What the body does not name is not in `xg`'s mask. The body has been
// judged to be a JSON object by the caller.
pub fn parse[&h, &b, &g](heap: &!h Heap, body: &b [byte], xg: &!g [int], patch: bool, grace_ms: int, now: int) -> [heap] int {
    var code = 0;
    var mask = 0;
    var tn = 0;
    var sn = 0;
    var keep_until = 0;
    let tape = box_slice(heap, json.tape_len(body), 0);
    borrow mut tape as &!tw in {
        let t = contents(tw);
        if json.parse(body, t) < 0 || !json.is_object(t, 0) {
            code = 1;
        } else {
            region a {
                let list = alloc_slice[a](filter.max_list() + 8, byte_of(0));
                let spec = alloc_slice[a](hdrs.max_spec() + 8, byte_of(0));
                let item = alloc_slice[a](filter.max_pattern() + 8, byte_of(0));
                let ty = json.get(body, t, 0, "types");
                if ty >= 0 && !json.is_null(t, ty) {
                    mask = mask | m_types();
                    if !json.is_array(t, ty) {
                        code = 301;
                    } else {
                        var n = json.count(t, ty);
                        var e = 0;
                        while e < n && code == 0 {
                            let node = json.at(t, ty, e);
                            if !json.is_string(t, node) {
                                code = 301;
                            } else {
                                let m = json.string_into(body, t, node, item);
                                if m < 0 {
                                    code = 100 + filter.long_item();
                                } else if tn + m + 1 > filter.max_list() {
                                    code = 100 + filter.long_list();
                                } else {
                                    let c = filter.check_pattern(item[0..m]);
                                    if c != 0 {
                                        code = 100 + c;
                                    } else {
                                        if e > 0 {
                                            list[tn] = byte_of(',');
                                            tn = tn + 1;
                                        }
                                        var k = 0;
                                        while k < m {
                                            list[tn + k] = item[k];
                                            k = k + 1;
                                        }
                                        tn = tn + m;
                                    }
                                }
                            }
                            e = e + 1;
                        }
                        if code == 0 && n > filter.max_patterns() {
                            code = 100 + filter.too_many();
                        }
                        if code == 0 {
                            let c = filter.check_list(list[0..tn]);
                            if c != 0 {
                                code = 100 + c;
                            }
                        }
                    }
                } else if ty >= 0 {
                    mask = mask | m_types();
                }
                if code == 0 {
                    let hd = json.get(body, t, 0, "headers");
                    if hd >= 0 && !json.is_null(t, hd) {
                        mask = mask | m_headers();
                        if !json.is_object(t, hd) {
                            code = 302;
                        } else {
                            var left = json.count(t, hd);
                            var j = hd + 1;
                            var names = 0;
                            while left > 0 && code == 0 {
                                let knode = j;
                                let vnode = j + 1;
                                if !json.is_string(t, vnode) {
                                    code = 302;
                                } else if names >= hdrs.max_headers() {
                                    code = 200 + hdrs.too_many();
                                } else {
                                    let nl = json.string_into(body, t, knode, item);
                                    if nl < 0 {
                                        code = 200 + hdrs.bad_name();
                                    } else {
                                        let nc = hdrs.check_name(item[0..nl]);
                                        if nc != 0 {
                                            code = 200 + nc;
                                        } else {
                                            // the value is decoded to the tail of `spec`'s room, then encoded at the head
                                            region b {
                                                let raw = alloc_slice[b](hdrs.max_value() + 8, byte_of(0));
                                                let vl = json.string_into(body, t, vnode, raw);
                                                if vl < 0 {
                                                    // a value longer than a header value can be (or one that cannot be decoded)
                                                    code = 200 + hdrs.bad_length();
                                                } else {
                                                    let vc = hdrs.check_value(raw[0..vl]);
                                                    if vc != 0 {
                                                        code = 200 + vc;
                                                    } else {
                                                        var at = sn;
                                                        if names > 0 {
                                                            spec[at] = byte_of(',');
                                                            at = at + 1;
                                                        }
                                                        if at + nl + 1 > hdrs.max_spec() {
                                                            code = 200 + hdrs.too_large();
                                                        } else {
                                                            var q = 0;
                                                            while q < nl {
                                                                spec[at + q] = item[q];
                                                                q = q + 1;
                                                            }
                                                            spec[at + nl] = byte_of(':');
                                                            let end = hdrs.encode_value(raw[0..vl], spec, at + nl + 1, hdrs.max_spec());
                                                            if end < 0 {
                                                                code = 200 + hdrs.too_large();
                                                            } else {
                                                                sn = end;
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                            names = names + 1;
                                        }
                                    }
                                }
                                j = json.skip(t, vnode);
                                left = left - 1;
                            }
                            if code == 0 {
                                let c = hdrs.check_spec(spec[0..sn]);
                                if c != 0 {
                                    code = 200 + c;
                                }
                            }
                        }
                    } else if hd >= 0 {
                        mask = mask | m_headers();
                    }
                }
                if code == 0 && patch {
                    let km = json.get(body, t, 0, "keep_old_ms");
                    let kb = json.get(body, t, 0, "keep_old");
                    if km >= 0 && kb >= 0 {
                        code = 304;
                    } else if km >= 0 {
                        if !json.is_int(t, km) || !json.fits_int(body, t, km) {
                            code = 303;
                        } else {
                            let ms = json.to_int(body, t, km);
                            if ms < 0 || ms > max_keep_ms() {
                                code = 303;
                            } else {
                                mask = mask | m_keep();
                                if ms > 0 {
                                    keep_until = now + ms;
                                }
                            }
                        }
                    } else if kb >= 0 {
                        if !json.is_bool(t, kb) {
                            code = 304;
                        } else {
                            mask = mask | m_keep();
                            if json.to_bool(t, kb) {
                                keep_until = now + grace_ms;
                            }
                        }
                    }
                }
                if code == 0 {
                    xg[0] = mask;
                    xg[1] = tn;
                    xg[2] = sn;
                    xg[3] = keep_until;
                    var k = 0;
                    while k < tn {
                        xg[xg_types_at() + k] = int_of(list[k]);
                        k = k + 1;
                    }
                    k = 0;
                    while k < sn {
                        xg[xg_spec_at() + k] = int_of(spec[k]);
                        k = k + 1;
                    }
                }
            }
        }
    }
    unbox_slice(heap, tape);
    if code == 0 {
        return 0;
    }
    xg[0] = 0;
    xg[1] = 0;
    xg[2] = 0;
    xg[3] = 0;
    return code;
}

// A change with nothing named (the state when no request waits).
pub fn clear_pending[&g](xg: &!g [int]) -> [] int {
    xg[0] = 0;
    xg[1] = 0;
    xg[2] = 0;
    xg[3] = 0;
    return 0;
}

pub fn pending_mask[&g](xg: &g [int]) -> [] int {
    return xg[0];
}

pub fn pending_keep_until[&g](xg: &g [int]) -> [] int {
    return xg[3];
}

pub fn pending_types_len[&g](xg: &g [int]) -> [] int {
    return xg[1];
}

pub fn pending_spec_len[&g](xg: &g [int]) -> [] int {
    return xg[2];
}

// The pending subscription and spec as bytes, into `out` (at least as long as each); answers the length.
pub fn pending_types_into[&g, &o](xg: &g [int], out: &!o [byte]) -> [] int {
    var k = 0;
    while k < xg[1] {
        out[k] = byte_of(xg[xg_types_at() + k]);
        k = k + 1;
    }
    return xg[1];
}

pub fn pending_spec_into[&g, &o](xg: &g [int], out: &!o [byte]) -> [] int {
    var k = 0;
    while k < xg[2] {
        out[k] = byte_of(xg[xg_spec_at() + k]);
        k = k + 1;
    }
    return xg[2];
}
