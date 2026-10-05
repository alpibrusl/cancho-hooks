edition 5;

import std.test;
import bulk;
import std.bytes;

// What the requests about dead letters ask for (`src/bulk.ls`, docs/design.md section 39.1): the query of the list and the body of the bulk replay.

fn test_the_query_of_the_list_has_defaults_and_edges() -> [] int {
    let p = bulk.parse_page("");
    test.assert_eq(p.0, 0);
    test.assert_eq(p.1, 100);
    test.assert(p.2);
    test.assert_eq(p.3, 0 - 1);
    let q = bulk.parse_page("limit=7&order=asc&after=20");
    test.assert_eq(q.0, 0);
    test.assert_eq(q.1, 7);
    test.assert(!q.2);
    test.assert_eq(q.3, 20);
    test.assert_eq(bulk.parse_page("limit=1000").1, 1000);
    test.assert_eq(bulk.parse_page("limit=1").1, 1);
    test.assert_eq(bulk.parse_page("order=desc").0, 0);
    test.assert_eq(bulk.parse_page("after=0").3, 0);
    // the other members in the query do not matter, and the order of the members does not
    test.assert_eq(bulk.parse_page("x=1&after=5&limit=2").3, 5);
    return 0;
}

fn test_every_bad_query_has_its_code() -> [] int {
    test.assert_eq(bulk.parse_page("limit=0").0, 6);
    test.assert_eq(bulk.parse_page("limit=1001").0, 6);
    test.assert_eq(bulk.parse_page("limit=").0, 6);
    test.assert_eq(bulk.parse_page("limit=x").0, 6);
    test.assert_eq(bulk.parse_page("limit=-1").0, 6);
    test.assert_eq(bulk.parse_page("limit=1.5").0, 6);
    test.assert_eq(bulk.parse_page("order=up").0, 7);
    test.assert_eq(bulk.parse_page("order=").0, 7);
    test.assert_eq(bulk.parse_page("order=ascending").0, 7);
    test.assert_eq(bulk.parse_page("order=DESC").0, 7);
    test.assert_eq(bulk.parse_page("after=x").0, 8);
    test.assert_eq(bulk.parse_page("after=-1").0, 8);
    test.assert_eq(bulk.parse_page("after=").0, 8);
    test.assert_eq(bulk.parse_page("after=1234567890123").0, 8);
    return 0;
}

fn test_the_body_of_a_bulk_replay_may_be_empty_or_name_three_members[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let list = alloc_slice[a](520, 0);
        let none = bulk.parse_body(heap, "", list);
        test.assert_eq(none.0, 0);
        test.assert_eq(none.1, 0);
        test.assert_eq(none.2, 0);
        test.assert_eq(none.3, 0);
        let empty = bulk.parse_body(heap, "{}", list);
        test.assert_eq(empty.0, 0);
        test.assert_eq(empty.3, 0);
        let all = bulk.parse_body(heap, "{\"limit\":5,\"after\":9,\"types\":[\"user.*\",\"ping\"]}", list);
        test.assert_eq(all.0, 0);
        test.assert_eq(all.1, 5);
        test.assert_eq(all.2, 9);
        test.assert_eq(all.3, 11);
        test.assert_eq(list[0], int_of(byte_of('u')));
        test.assert_eq(list[6], int_of(byte_of(',')));
        test.assert_eq(list[10], int_of(byte_of('g')));
        // null is as if not named, and an empty list is every type
        let nulls = bulk.parse_body(heap, "{\"limit\":null,\"after\":null,\"types\":null}", list);
        test.assert_eq(nulls.0, 0);
        test.assert_eq(nulls.1, 0);
        test.assert_eq(nulls.3, 0);
        let emptylist = bulk.parse_body(heap, "{\"types\":[]}", list);
        test.assert_eq(emptylist.0, 0);
        test.assert_eq(emptylist.3, 0);
        // the edges of the limit
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":1}", list).1, 1);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":2048}", list).1, 2048);
        test.assert_eq(bulk.parse_body(heap, "{\"after\":0}", list).0, 0);
    }
    return 0;
}

fn test_every_bad_body_has_its_code[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let list = alloc_slice[a](520, 0);
        test.assert_eq(bulk.parse_body(heap, "[1]", list).0, 1);
        test.assert_eq(bulk.parse_body(heap, "5", list).0, 1);
        test.assert_eq(bulk.parse_body(heap, "{", list).0, 1);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":0}", list).0, 2);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":2049}", list).0, 2);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":\"x\"}", list).0, 2);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":1.5}", list).0, 2);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":true}", list).0, 2);
        test.assert_eq(bulk.parse_body(heap, "{\"after\":-1}", list).0, 3);
        test.assert_eq(bulk.parse_body(heap, "{\"after\":\"x\"}", list).0, 3);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":\"user.*\"}", list).0, 4);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[1]}", list).0, 4);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[null]}", list).0, 4);
        test.assert_eq(bulk.parse_body(heap, "{\"limt\":5}", list).0, 5);
        test.assert_eq(bulk.parse_body(heap, "{\"limit\":5,\"extra\":1}", list).0, 5);
        // a pattern is judged by the rules of a subscription (code 100 and the subscription's own)
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[\"\"]}", list).0, 101);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[\"a b\"]}", list).0, 103);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[\"a*b\"]}", list).0, 104);
        test.assert_eq(bulk.parse_body(heap, "{\"types\":[\"a\",\"b\",\"c\",\"d\",\"e\",\"f\",\"g\",\"h\",\"i\",\"j\",\"k\",\"l\",\"m\",\"n\",\"o\",\"p\",\"q\"]}", list).0, 105);
        // a refusal answers nothing else
        let bad = bulk.parse_body(heap, "{\"limit\":5,\"after\":3,\"types\":[\"a b\"]}", list);
        test.assert_eq(bad.1, 0);
        test.assert_eq(bad.2, 0);
        test.assert_eq(bad.3, 0);
    }
    return 0;
}

// The page of `GET /endpoints` (section 41.4): 64 by default, 256 at most, from an offset; and the page of `GET /metrics`.
fn test_the_page_of_the_endpoints_has_defaults_and_edges() -> [] int {
    let p = bulk.parse_listing("");
    test.assert_eq(p.0, 0);
    test.assert_eq(p.1, 64);
    test.assert_eq(p.2, 0);
    let q = bulk.parse_listing("offset=128&limit=256");
    test.assert_eq(q.0, 0);
    test.assert_eq(q.1, 256);
    test.assert_eq(q.2, 128);
    test.assert_eq(bulk.parse_listing("limit=1").1, 1);
    test.assert_eq(bulk.parse_listing("x=1&offset=5").2, 5);
    test.assert_eq(bulk.parse_listing("offset=0").0, 0);
    // every bad one has its code, and the code a message
    test.assert_eq(bulk.parse_listing("limit=0").0, 9);
    test.assert_eq(bulk.parse_listing("limit=257").0, 9);
    test.assert_eq(bulk.parse_listing("limit=x").0, 9);
    test.assert_eq(bulk.parse_listing("limit=").0, 9);
    test.assert_eq(bulk.parse_listing("offset=-1").0, 10);
    test.assert_eq(bulk.parse_listing("offset=x").0, 10);
    test.assert_eq(bulk.parse_listing("offset=1234567890123").0, 10);
    test.assert(bytes.starts_with(bulk.why(9), "limit must"));
    test.assert(bytes.starts_with(bulk.why(10), "offset must"));
    test.assert(bytes.starts_with(bulk.why(11), "page must"));
    return 0;
}

fn test_the_page_of_the_metrics_defaults_to_the_first() -> [] int {
    test.assert_eq(bulk.parse_metrics_page("").1, 0);
    test.assert_eq(bulk.parse_metrics_page("").0, 0);
    test.assert_eq(bulk.parse_metrics_page("page=0").1, 0);
    test.assert_eq(bulk.parse_metrics_page("page=15").1, 15);
    test.assert_eq(bulk.parse_metrics_page("a=b&page=3").1, 3);
    test.assert_eq(bulk.parse_metrics_page("page=x").0, 11);
    test.assert_eq(bulk.parse_metrics_page("page=").0, 11);
    test.assert_eq(bulk.parse_metrics_page("page=-1").0, 11);
    return 0;
}
