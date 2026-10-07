edition 5;

module filter;

import record;

// `filter` -- which events an endpoint is sent (`docs/design.md` section 35, production.md P1.1).
//
// **The type of an event** is the string member `"type"` of its JSON body (`POST /events` requires one). It is read once, when the event is
// accepted, and stored in the event's record as a named pair `typ` beside the body, so that deciding whether an endpoint wants an event never parses
// JSON: `type_in` finds the pair in the record the delivery has just read, and `accepts` compares those bytes. A type that is longer than 128
// bytes, empty, or has a control character in it is stored as no type at all; so is the type of every event written before the pair existed. An
// event with no type is wanted only by an endpoint that subscribes to everything.
//
// **A subscription** is a list of patterns, comma separated (`invoice.paid,user.*`), at most 16 of them and 512 bytes in all; the empty list
// is "everything", the default. A pattern is 1 to 128 visible ASCII characters (no space, no comma) and is one of
//
//     *            every event, an event with no type too
//     prefix.*     every event whose type begins with `prefix.` (`user.*` is `user.created` and `user.address.changed`, not `user` and not `users.x`)
//     anything     exactly that type, byte for byte, case sensitive
//
// and a `*` anywhere else in a pattern is refused. The list is kept as integers, a byte to an integer, like the rest of the delivery state.

pub fn max_patterns() -> [] int {
    return 16;
}

pub fn max_pattern() -> [] int {
    return 128;
}

pub fn max_list() -> [] int {
    return 512;
}

pub fn max_type() -> [] int {
    return 128;
}

// The refusals of a list, by code (`why`).
pub fn empty_item() -> [] int {
    return 1;
}

pub fn long_item() -> [] int {
    return 2;
}

pub fn bad_byte() -> [] int {
    return 3;
}

pub fn bad_star() -> [] int {
    return 4;
}

pub fn too_many() -> [] int {
    return 5;
}

pub fn long_list() -> [] int {
    return 6;
}

pub fn why(code: int) -> [] &static [byte] {
    if code == 1 {
        return "an event type pattern cannot be empty";
    }
    if code == 2 {
        return "an event type pattern is at most 128 characters";
    }
    if code == 3 {
        return "an event type pattern is visible ASCII with no space and no comma";
    }
    if code == 4 {
        return "a * in an event type pattern must be the whole pattern or the end of a prefix.* pattern";
    }
    if code == 5 {
        return "at most 16 event type patterns";
    }
    return "the event type patterns are at most 512 characters in all";
}

// 0 if `p` is a good pattern, else the code of the refusal.
pub fn check_pattern[&t](p: &t [byte]) -> [] int {
    if len(p) == 0 {
        return empty_item();
    }
    if len(p) > max_pattern() {
        return long_item();
    }
    var star = 0 - 1;
    var i = 0;
    while i < len(p) {
        let c = int_of(p[i]);
        if c <= 32 || c >= 127 || c == ',' {
            return bad_byte();
        }
        if c == '*' {
            if star >= 0 {
                return bad_star();
            }
            star = i;
        }
        i = i + 1;
    }
    if star < 0 || len(p) == 1 {
        return 0;
    }
    if star == len(p) - 1 && star >= 2 && int_of(p[star - 1]) == '.' {
        return 0;
    }
    return bad_star();
}

// 0 if `list` (comma separated patterns; empty is good) is a good subscription, else the code of the first refusal.
pub fn check_list[&t](list: &t [byte]) -> [] int {
    if len(list) > max_list() {
        return long_list();
    }
    if len(list) == 0 {
        return 0;
    }
    var at = 0;
    var count = 0;
    while at <= len(list) {
        var end = at;
        while end < len(list) && int_of(list[end]) != ',' {
            end = end + 1;
        }
        let c = check_pattern(list[at..end]);
        if c != 0 {
            return c;
        }
        count = count + 1;
        if count > max_patterns() {
            return too_many();
        }
        at = end + 1;
    }
    return 0;
}

// The `typ` pair of the event record at offset 0 of `buf` (as `log.read_at` leaves it): `(start, length)` of the type's bytes in `buf`, or `(0, 0)`
// for an event with none. The pair, when there is one, is the second: right after the body.
pub fn type_in[&b](buf: &b [byte]) -> [] (int, int) {
    if record.fields_of(buf, 0) < 2 {
        return (0, 0);
    }
    let body = record.pair_at(buf, record.first_pair(0));
    let p = record.pair_at(buf, body.4);
    if p.1 == 3 && int_of(buf[p.0]) == 't' && int_of(buf[p.0 + 1]) == 'y' && int_of(buf[p.0 + 2]) == 'p' {
        return (p.2, p.3);
    }
    return (0, 0);
}

// Does the pattern `list[at..end]` (good, and without a comma) accept the type `typ`?
fn item_accepts[&l, &t](list: &l [int], at: int, end: int, typ: &t [byte]) -> [] bool {
    let m = end - at;
    if m == 1 && list[at] == '*' {
        return true;
    }
    if len(typ) == 0 {
        return false;
    }
    if m >= 3 && list[end - 1] == '*' && list[end - 2] == '.' {
        // prefix.*: the type begins with `prefix.`
        if len(typ) < m - 1 {
            return false;
        }
        var i = 0;
        while i < m - 1 {
            if list[at + i] != int_of(typ[i]) {
                return false;
            }
            i = i + 1;
        }
        return true;
    }
    if len(typ) != m {
        return false;
    }
    var i = 0;
    while i < m {
        if list[at + i] != int_of(typ[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Does the subscription `list[0..n]` (a good list, a byte to an integer) want an event of type `typ`? The empty subscription wants everything.
pub fn accepts[&l, &t](list: &l [int], n: int, typ: &t [byte]) -> [] bool {
    if n == 0 {
        return true;
    }
    var at = 0;
    while at <= n {
        var end = at;
        while end < n && list[end] != ',' {
            end = end + 1;
        }
        if item_accepts(list, at, end, typ) {
            return true;
        }
        at = end + 1;
    }
    return false;
}

// The type to store for an event whose decoded `"type"` is `typ`: its length if it is a type (1 to 128 bytes with no control character), else 0.
pub fn storable[&t](typ: &t [byte]) -> [] int {
    if len(typ) < 1 || len(typ) > max_type() {
        return 0;
    }
    var i = 0;
    while i < len(typ) {
        let c = int_of(typ[i]);
        if c < 32 || c == 127 {
            return 0;
        }
        i = i + 1;
    }
    return len(typ);
}
