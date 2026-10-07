edition 5;

module bulk;

import std.http;
import std.json;
import filter;

// `bulk` -- what the requests about dead letters and waiting replays ask for (`docs/design.md` section 39.1 and 36.2): the page of
// `GET /endpoints/:id/dead` and the body of `POST /endpoints/:id/replay-dead`. Pure: it reads a query string or a body and says what is wanted, or what
// is wrong with it, and touches nothing.

// The most dead letters one page of the list holds, and its default.
pub fn max_page() -> [] int {
    return 1000;
}

pub fn default_page() -> [] int {
    return 100;
}

// The most dead letters one `replay-dead` is asked to take: a table holds 2,048 (`dead.cap()`), and the table of waiting replays 32 at once, which is
// the real bound (the answer says how many were taken).
pub fn max_take() -> [] int {
    return 2048;
}

// What was wrong with a request, by code.
pub fn why(code: int) -> [] &static [byte] {
    if code == 1 {
        return "the body must be a JSON object (or empty)";
    }
    if code == 2 {
        return "\"limit\" must be an integer from 1 to 2048";
    }
    if code == 3 {
        return "\"after\" must be an event id: an integer, 0 or more";
    }
    if code == 4 {
        return "\"types\" must be an array of event type patterns (strings); an empty array means every type";
    }
    if code == 5 {
        return "the members are \"limit\", \"types\" and \"after\" and no others";
    }
    if code == 6 {
        return "limit must be an integer from 1 to 1000";
    }
    if code == 7 {
        return "order must be desc (newest first, the default) or asc (oldest first)";
    }
    if code == 8 {
        return "after must be an event id: an integer, 0 or more";
    }
    if code == 9 {
        return "limit must be an integer from 1 to 256";
    }
    if code == 10 {
        return "offset must be an integer, 0 or more";
    }
    if code == 11 {
        return "page must be an integer, 0 or more";
    }
    if code > 100 && code < 200 {
        return filter.why(code - 100);
    }
    return "the request is not valid";
}

// A decimal number of 1 to 12 digits in `text[from..to]`, or -1.
pub fn number[&t](text: &t [byte], from: int, to: int) -> [] int {
    if to <= from || to - from > 12 {
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

// The query of `GET /endpoints/:id/dead`: `limit` (1 to 1000; 100), `order` (`desc` newest first, the default, or `asc`), `after` (an event id: the
// page goes on from the entry after it in that order; absent: from the first). Answers `(code, limit, newest_first, after)`; `after` is -1 when absent.
pub fn parse_page[&q](query: &q [byte]) -> [] (int, int, bool, int) {
    var limit = default_page();
    var newest = true;
    var after = 0 - 1;
    let l = http.query_value(query, "limit");
    if l.0 >= 0 {
        limit = number(query, l.0, l.1);
        if limit < 1 || limit > max_page() {
            return (6, 0, true, 0 - 1);
        }
    }
    let o = http.query_value(query, "order");
    if o.0 >= 0 {
        if o.1 - o.0 == 4 && int_of(query[o.0]) == 'd' && int_of(query[o.0 + 1]) == 'e' && int_of(query[o.0 + 2]) == 's' && int_of(query[o.0 + 3]) == 'c' {
            newest = true;
        } else if o.1 - o.0 == 3 && int_of(query[o.0]) == 'a' && int_of(query[o.0 + 1]) == 's' && int_of(query[o.0 + 2]) == 'c' {
            newest = false;
        } else {
            return (7, 0, true, 0 - 1);
        }
    }
    let a = http.query_value(query, "after");
    if a.0 >= 0 {
        after = number(query, a.0, a.1);
        if after < 0 {
            return (8, 0, true, 0 - 1);
        }
    }
    return (0, limit, newest, after);
}

// The page of `GET /endpoints` (`docs/design.md` section 41.4: an answer cannot be longer than the server's 64 KiB queue, and 1,024 endpoints are 250 KB): `limit` (1 to
// `max_listing()`, default `default_listing()`) endpoints from `offset` (default 0) in the table's order. Answers `(code, limit, offset)`.
pub fn max_listing() -> [] int {
    return 256;
}

pub fn default_listing() -> [] int {
    return 64;
}

pub fn parse_listing[&q](query: &q [byte]) -> [] (int, int, int) {
    var limit = default_listing();
    var offset = 0;
    let l = http.query_value(query, "limit");
    if l.0 >= 0 {
        limit = number(query, l.0, l.1);
        if limit < 1 || limit > max_listing() {
            return (9, 0, 0);
        }
    }
    let o = http.query_value(query, "offset");
    if o.0 >= 0 {
        offset = number(query, o.0, o.1);
        if offset < 0 {
            return (10, 0, 0);
        }
    }
    return (0, limit, offset);
}

// The page of `GET /metrics`: `page` (default 0). Answers `(code, page)`.
pub fn parse_metrics_page[&q](query: &q [byte]) -> [] (int, int) {
    let p = http.query_value(query, "page");
    if p.0 < 0 {
        return (0, 0);
    }
    let n = number(query, p.0, p.1);
    if n < 0 {
        return (11, 0);
    }
    return (0, n);
}

// The body of `POST /endpoints/:id/replay-dead`: empty, or an object with `limit` (how many to take at most; default: as many as the table of waiting
// replays has room for), `types` (event type patterns, as an endpoint's subscription: only dead letters of those types) and `after` (take dead letters with
// an event id above it: how a caller goes on past the ones it has already seen). The patterns go into `list`, a byte to an integer (`filter.accepts`
// reads them), and their length is the fourth answer. Answers `(code, limit, after, list_length)`; `limit` is 0 when absent.
pub fn parse_body[&h, &b, &l](heap: &!h Heap, body: &b [byte], list: &!l [int]) -> [heap] (int, int, int, int) {
    if len(body) == 0 {
        return (0, 0, 0, 0);
    }
    var code = 0;
    var limit = 0;
    var after = 0;
    var tn = 0;
    let tape = box_slice(heap, json.tape_len(body), 0);
    borrow mut tape as &!tw in {
        let t = contents(tw);
        if json.parse(body, t) < 0 || !json.is_object(t, 0) {
            code = 1;
        } else {
            // no member but these three: a misspelt "limt" would otherwise take every dead letter it was meant to bound
            var left = json.count(t, 0);
            var j = 1;
            while left > 0 && code == 0 {
                if !json.string_equals(body, t, j, "limit") && !json.string_equals(body, t, j, "types") && !json.string_equals(body, t, j, "after") {
                    code = 5;
                }
                j = json.skip(t, j + 1);
                left = left - 1;
            }
            let ln = json.get(body, t, 0, "limit");
            if code == 0 && ln >= 0 && !json.is_null(t, ln) {
                if !json.is_int(t, ln) || !json.fits_int(body, t, ln) {
                    code = 2;
                } else {
                    limit = json.to_int(body, t, ln);
                    if limit < 1 || limit > max_take() {
                        code = 2;
                    }
                }
            }
            let an = json.get(body, t, 0, "after");
            if code == 0 && an >= 0 && !json.is_null(t, an) {
                if !json.is_int(t, an) || !json.fits_int(body, t, an) {
                    code = 3;
                } else {
                    after = json.to_int(body, t, an);
                    if after < 0 {
                        code = 3;
                    }
                }
            }
            let ty = json.get(body, t, 0, "types");
            if code == 0 && ty >= 0 && !json.is_null(t, ty) {
                if !json.is_array(t, ty) {
                    code = 4;
                } else {
                    region a {
                        let item = alloc_slice[a](filter.max_pattern() + 8, byte_of(0));
                        let n = json.count(t, ty);
                        var e = 0;
                        while e < n && code == 0 {
                            let node = json.at(t, ty, e);
                            if !json.is_string(t, node) {
                                code = 4;
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
                                            list[tn] = ',';
                                            tn = tn + 1;
                                        }
                                        var k = 0;
                                        while k < m {
                                            list[tn + k] = int_of(item[k]);
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
                    }
                }
            }
        }
    }
    unbox_slice(heap, tape);
    if code != 0 {
        return (code, 0, 0, 0);
    }
    return (0, limit, after, tn);
}
