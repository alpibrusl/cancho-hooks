edition 5;

import std.test;
import epx;

// The extras of an endpoint (`src/epx.ls`, docs/design.md section 35): the row an endpoint has, set, read, shifted down by a delete, and cleared. Two
// rows fit one arena. (The request side, `epx.parse`, needs a heap and is tested through the service: `tests/headers_test.py`, `tests/filter_test.py`.)

fn test_a_row_starts_empty_and_wants_everything() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        test.assert_eq(epx.types_len(xt, 0), 0);
        test.assert_eq(epx.wire_len(xt, 0), 0);
        test.assert_eq(epx.old_len(xt, 0), 0);
        test.assert(epx.accepts(xt, 0, "anything"));
        test.assert(epx.accepts(xt, 0, ""));
        test.assert(!epx.old_active(xt, 0, 1));
    }
    return 0;
}

fn test_a_subscription_is_kept_per_row() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        epx.set_types(xt, 1, "user.*,ping");
        test.assert_eq(epx.types_len(xt, 1), 11);
        test.assert(epx.accepts(xt, 1, "user.created"));
        test.assert(epx.accepts(xt, 1, "ping"));
        test.assert(!epx.accepts(xt, 1, "order.created"));
        test.assert(!epx.accepts(xt, 1, ""));
        // row 0 is not touched
        test.assert(epx.accepts(xt, 0, "order.created"));
        // a shorter list replaces a longer one
        epx.set_types(xt, 1, "a");
        test.assert_eq(epx.types_len(xt, 1), 1);
        test.assert(epx.accepts(xt, 1, "a"));
        test.assert(!epx.accepts(xt, 1, "user.created"));
        // and the empty one is everything again
        epx.set_types(xt, 1, "");
        test.assert(epx.accepts(xt, 1, "user.created"));
    }
    return 0;
}

fn test_headers_are_kept_as_they_go_on_the_wire() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        test.assert_eq(epx.set_spec(xt, 0, "A:b%20c"), 0);
        // "A: b c\r\n"
        test.assert_eq(epx.wire_len(xt, 0), 8);
        test.assert_eq(epx.wire_byte(xt, 0, 0), int_of(byte_of('A')));
        test.assert_eq(epx.wire_byte(xt, 0, 3), int_of(byte_of('b')));
        test.assert_eq(epx.wire_byte(xt, 0, 4), int_of(byte_of(' ')));
        test.assert_eq(epx.wire_byte(xt, 0, 6), int_of(byte_of('\r')));
        test.assert_eq(epx.wire_byte(xt, 0, 7), int_of(byte_of('\n')));
        // a spec that is not good changes nothing
        test.assert_eq(epx.set_spec(xt, 0, "A:%0d%0a"), 0 - 1);
        test.assert_eq(epx.wire_len(xt, 0), 8);
        // the empty spec removes them
        test.assert_eq(epx.set_spec(xt, 0, ""), 0);
        test.assert_eq(epx.wire_len(xt, 0), 0);
    }
    return 0;
}

fn test_a_previous_secret_is_valid_until_its_time() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        epx.set_old(xt, 0, "0123456789abcdef", 5000);
        test.assert_eq(epx.old_len(xt, 0), 16);
        test.assert(epx.old_active(xt, 0, 4999));
        test.assert(!epx.old_active(xt, 0, 5000));
        test.assert(!epx.old_active(xt, 0, 6000));
        test.assert_eq(epx.old_byte(xt, 0, 0), int_of(byte_of('0')));
        test.assert_eq(epx.old_byte(xt, 0, 15), int_of(byte_of('f')));
        // the time can be moved while there is a key
        epx.set_old_until(xt, 0, 9000);
        test.assert(epx.old_active(xt, 0, 8999));
        test.assert(!epx.old_active(xt, 1, 1));
        epx.set_old_until(xt, 1, 9000);
        test.assert_eq(epx.old_len(xt, 1), 0);
        test.assert(!epx.old_active(xt, 1, 1));
        // no key, or no time, ends it, and the key's bytes are zeroed
        epx.set_old(xt, 0, "", 9000);
        test.assert(!epx.old_active(xt, 0, 1));
        test.assert_eq(epx.old_byte(xt, 0, 0), 0);
        epx.set_old(xt, 0, "k", 0);
        test.assert_eq(epx.old_len(xt, 0), 0);
    }
    return 0;
}

fn test_a_delete_shifts_the_rows_down_and_clears_the_last() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        epx.set_types(xt, 0, "first");
        epx.set_spec(xt, 0, "A:one");
        epx.set_types(xt, 1, "second");
        epx.set_old(xt, 1, "key", 77);
        epx.drop_row(xt, 2, 0);
        test.assert_eq(epx.types_len(xt, 0), 6);
        test.assert(epx.accepts(xt, 0, "second"));
        test.assert(!epx.accepts(xt, 0, "first"));
        test.assert_eq(epx.wire_len(xt, 0), 0);
        test.assert_eq(epx.old_len(xt, 0), 3);
        test.assert_eq(epx.old_until(xt, 0), 77);
        test.assert_eq(epx.types_len(xt, 1), 0);
        test.assert_eq(epx.old_len(xt, 1), 0);
        // the last row removed leaves the first alone
        epx.drop_row(xt, 2, 1);
        test.assert_eq(epx.types_len(xt, 0), 6);
    }
    return 0;
}

fn test_the_text_the_extras_take_is_estimated_generously() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        test.assert_eq(epx.text_used(xt, 2), 80);
        epx.set_types(xt, 0, "abcde");
        epx.set_spec(xt, 1, "A:b");
        test.assert(epx.text_used(xt, 2) > 80 + 5);
        test.assert_eq(epx.text_used(xt, 0), 0);
    }
    return 0;
}

// The limits of an endpoint (`lim.ls`, docs/design.md section 39.4) are two integers of its row: 0 is "follow the service". A delete shifts them like the rest.
fn test_the_limits_are_kept_per_row_and_shift_with_a_delete() -> [] int {
    region a {
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        test.assert_eq(epx.conc(xt, 0), 0);
        test.assert_eq(epx.rate(xt, 0), 0);
        epx.set_conc(xt, 1, 3);
        epx.set_rate(xt, 1, 250);
        test.assert_eq(epx.conc(xt, 1), 3);
        test.assert_eq(epx.rate(xt, 1), 250);
        test.assert_eq(epx.conc(xt, 0), 0);
        test.assert_eq(epx.rate(xt, 0), 0);
        // they sit beyond the previous secret's key, in no other member's place
        epx.set_old(xt, 1, "key", 77);
        epx.set_types(xt, 1, "a,b");
        epx.set_spec(xt, 1, "A:one");
        test.assert_eq(epx.conc(xt, 1), 3);
        test.assert_eq(epx.rate(xt, 1), 250);
        epx.drop_row(xt, 2, 0);
        test.assert_eq(epx.conc(xt, 0), 3);
        test.assert_eq(epx.rate(xt, 0), 250);
        test.assert_eq(epx.conc(xt, 1), 0);
        test.assert_eq(epx.rate(xt, 1), 0);
        epx.clear_row(xt, 0);
        test.assert_eq(epx.conc(xt, 0), 0);
        test.assert_eq(epx.rate(xt, 0), 0);
        test.assert_eq(epx.max_concurrency(), 8);
        test.assert_eq(epx.max_rate(), 100000);
    }
    return 0;
}

fn test_the_request_names_the_limits_or_refuses_them[&h](heap: &!h Heap) -> [heap] int {
    let xgb = box_slice(heap, epx.xg_size(), 0);
    borrow mut xgb as &!xw in {
        let xg = contents(xw);
        // both, in a POST and in a PATCH
        test.assert_eq(epx.parse(heap, "{\"host\":\"8.8.8.8\",\"concurrency\":3,\"rate\":250}", xg, false, 1000, 5), 0);
        test.assert_eq(epx.pending_mask(xg), epx.m_conc() | epx.m_rate());
        test.assert_eq(epx.pending_conc(xg), 3);
        test.assert_eq(epx.pending_rate(xg), 250);
        test.assert_eq(epx.parse(heap, "{\"rate\":7}", xg, true, 1000, 5), 0);
        test.assert_eq(epx.pending_mask(xg), epx.m_rate());
        test.assert_eq(epx.pending_rate(xg), 7);
        test.assert_eq(epx.pending_conc(xg), 0);
        // null and 0 follow the service: named, and 0
        test.assert_eq(epx.parse(heap, "{\"concurrency\":null,\"rate\":0}", xg, true, 1000, 5), 0);
        test.assert_eq(epx.pending_mask(xg), epx.m_conc() | epx.m_rate());
        test.assert_eq(epx.pending_conc(xg), 0);
        test.assert_eq(epx.pending_rate(xg), 0);
        // the edges
        test.assert_eq(epx.parse(heap, "{\"concurrency\":8,\"rate\":100000}", xg, true, 1000, 5), 0);
        test.assert_eq(epx.pending_conc(xg), 8);
        test.assert_eq(epx.pending_rate(xg), 100000);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":1,\"rate\":1}", xg, true, 1000, 5), 0);
        // not named: not in the mask
        test.assert_eq(epx.parse(heap, "{\"types\":[\"a\"]}", xg, true, 1000, 5), 0);
        test.assert_eq(epx.pending_mask(xg) & (epx.m_conc() | epx.m_rate()), 0);
        // refusals leave nothing pending
        test.assert_eq(epx.parse(heap, "{\"concurrency\":9}", xg, true, 1000, 5), 306);
        test.assert_eq(epx.pending_mask(xg), 0);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":-1}", xg, true, 1000, 5), 306);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":2.5}", xg, true, 1000, 5), 306);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":\"2\"}", xg, true, 1000, 5), 306);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":true}", xg, true, 1000, 5), 306);
        test.assert_eq(epx.parse(heap, "{\"rate\":100001}", xg, true, 1000, 5), 307);
        test.assert_eq(epx.parse(heap, "{\"rate\":-1}", xg, true, 1000, 5), 307);
        test.assert_eq(epx.parse(heap, "{\"rate\":\"x\"}", xg, true, 1000, 5), 307);
        test.assert_eq(epx.parse(heap, "{\"rate\":99999999999999999999}", xg, true, 1000, 5), 307);
        test.assert_eq(epx.parse(heap, "{\"concurrency\":3,\"rate\":[]}", xg, true, 1000, 5), 307);
        test.assert_eq(epx.pending_conc(xg), 0);
        test.assert_eq(epx.pending_rate(xg), 0);
    }
    unbox_slice(heap, xgb);
    return 0;
}
