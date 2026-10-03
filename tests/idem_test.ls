edition 5;

import std.test;
import idem;

// The idempotency-key index (`src/idem.ls`): lookup, collisions, overwrite, the window, the body check, the key
// alphabet, and a full index.

// `find` and `add` for the key number `n`, built in `kb`.
fn find_n[&i, &a, &b](ix: &i [int], arena: &a [byte], kb: &!b [byte], n: int) -> [] int {
    let m = key_into(kb, n);
    return idem.find(ix, arena, kb[0..m]);
}

fn add_n[&i, &a, &b](ix: &!i [int], arena: &!a [byte], kb: &!b [byte], n: int) -> [] int {
    let m = key_into(kb, n);
    return idem.add(ix, arena, kb[0..m]);
}

// "k" and the decimal digits of n, written to the front of `b`: answers the length (2 to 8 bytes), distinct keys for distinct n.
fn key_into[&b](b: &!b [byte], n: int) -> [] int {
    var digits = 0;
    var m = n;
    while m > 0 {
        digits = digits + 1;
        m = m / 10;
    }
    if digits == 0 {
        digits = 1;
    }
    b[0] = byte_of('k');
    m = n;
    var i = digits;
    while i > 0 {
        b[i] = byte_of('0' + m % 10);
        m = m / 10;
        i = i - 1;
    }
    return digits + 1;
}

fn a_key_is_found_after_it_is_added[&i, &a, &k](ix: &!i [int], arena: &!a [byte], kb: &!k [byte]) -> [] int {
    ix[1] = 1000;
    test.assert_eq(find_n(ix, arena, kb, 1), 0 - 1);
    let e = add_n(ix, arena, kb, 1);
    test.assert_eq(e, 0);
    idem.set(ix, e, 7, 5000, 99, 12);
    test.assert_eq(idem.count(ix), 1);
    test.assert_eq(find_n(ix, arena, kb, 1), 0);
    test.assert_eq(idem.id_of(ix, 0), 7);
    test.assert_eq(idem.ms_of(ix, 0), 5000);
    test.assert_eq(find_n(ix, arena, kb, 2), 0 - 1);
    let f = add_n(ix, arena, kb, 2);
    test.assert_eq(f, 1);
    idem.set(ix, f, 8, 6000, 98, 13);
    test.assert_eq(find_n(ix, arena, kb, 2), 1);
    test.assert_eq(find_n(ix, arena, kb, 1), 0);
    test.assert_eq(idem.id_of(ix, 1), 8);
    return 0;
}

fn a_prefix_is_not_the_key[&i, &a, &k](ix: &!i [int], arena: &!a [byte], kb: &!k [byte]) -> [] int {
    let e = add_n(ix, arena, kb, 12345);
    idem.set(ix, e, 1, 0, 0, 0);
    test.assert_eq(find_n(ix, arena, kb, 123), 0 - 1);
    let f = add_n(ix, arena, kb, 123);
    idem.set(ix, f, 2, 0, 0, 0);
    test.assert_eq(find_n(ix, arena, kb, 12345), 0);
    test.assert_eq(find_n(ix, arena, kb, 123), 1);
    return 0;
}

fn many_keys_all_found_and_no_others[&i, &a, &k](ix: &!i [int], arena: &!a [byte], kb: &!k [byte]) -> [] int {
    var n = 1;
    while n <= 20000 {
        test.assert_eq(find_n(ix, arena, kb, n), 0 - 1);
        let e = add_n(ix, arena, kb, n);
        test.assert_eq(e, n - 1);
        idem.set(ix, e, n, n * 10, n, n);
        n = n + 1;
    }
    test.assert_eq(idem.count(ix), 20000);
    n = 1;
    while n <= 20000 {
        let e = find_n(ix, arena, kb, n);
        test.assert_eq(e, n - 1);
        test.assert_eq(idem.id_of(ix, e), n);
        n = n + 1;
    }
    test.assert_eq(find_n(ix, arena, kb, 20001), 0 - 1);
    test.assert_eq(find_n(ix, arena, kb, 99999), 0 - 1);
    return 0;
}

fn the_window_and_the_body_check[&i, &a, &k](ix: &!i [int], arena: &!a [byte], kb: &!k [byte]) -> [] int {
    ix[1] = 1000;
    let e = add_n(ix, arena, kb, 5);
    idem.set(ix, e, 3, 10000, 4242, 17);
    test.assert(idem.fresh(ix, e, 10000));
    test.assert(idem.fresh(ix, e, 11000));
    test.assert(!idem.fresh(ix, e, 11001));
    test.assert(idem.matches(ix, e, 4242, 17));
    test.assert(!idem.matches(ix, e, 4243, 17));
    test.assert(!idem.matches(ix, e, 4242, 18));
    // An overwrite (a later event under an expired key) changes the entry, not the count.
    idem.set(ix, e, 9, 20000, 1, 2);
    test.assert_eq(idem.count(ix), 1);
    test.assert_eq(idem.id_of(ix, e), 9);
    test.assert(idem.fresh(ix, e, 20500));
    test.assert(idem.matches(ix, e, 1, 2));
    return 0;
}

fn the_key_alphabet[&b](big: &!b [byte]) -> [] int {
    test.assert(idem.valid("abc-DEF_123:x/y.z"));
    test.assert(idem.valid("a"));
    test.assert(idem.valid("~"));
    test.assert(idem.valid("!"));
    test.assert(!idem.valid(""));
    test.assert(!idem.valid("a b"));
    test.assert(!idem.valid(" "));
    test.assert(!idem.valid("tab\there"));
    var j = 0;
    while j < 300 {
        big[j] = byte_of('x');
        j = j + 1;
    }
    big[0] = byte_of(127);
    test.assert(!idem.valid(big[0..1]));
    big[0] = byte_of(233);
    test.assert(!idem.valid(big[0..3]));
    big[0] = byte_of(31);
    test.assert(!idem.valid(big[0..3]));
    big[0] = byte_of('x');
    test.assert(idem.valid(big[0..255]));
    test.assert(!idem.valid(big[0..256]));
    return 0;
}

fn a_full_index_refuses_and_keeps_what_it_has[&i, &a, &k](ix: &!i [int], arena: &!a [byte], kb: &!k [byte]) -> [] int {
    var n = 1;
    while n <= idem.capacity() {
        let e = add_n(ix, arena, kb, n);
        test.assert_eq(e, n - 1);
        idem.set(ix, e, n, 0, 0, 0);
        n = n + 1;
    }
    test.assert_eq(idem.count(ix), idem.capacity());
    test.assert_eq(add_n(ix, arena, kb, idem.capacity() + 1), 0 - 1);
    test.assert_eq(idem.count(ix), idem.capacity());
    test.assert_eq(find_n(ix, arena, kb, 1), 0);
    test.assert_eq(find_n(ix, arena, kb, idem.capacity()), idem.capacity() - 1);
    test.assert_eq(find_n(ix, arena, kb, idem.capacity() + 1), 0 - 1);
    return 0;
}

// Each test owns its index and runs one of the bodies above over it.
fn test_a_key_is_found_after_it_is_added[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 0);
}

fn test_a_prefix_is_not_the_key[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 1);
}

fn test_many_keys_all_found_and_no_others[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 2);
}

fn test_the_window_and_the_body_check[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 3);
}

fn test_a_full_index_refuses_and_keeps_what_it_has[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 4);
}

fn test_the_key_alphabet[&h](heap: &!h Heap) -> [heap] int {
    return over(heap, 5);
}

fn over[&h](heap: &!h Heap, which: int) -> [heap] int {
    var ixb = box_slice(heap, idem.ix_size(), 0);
    var arb = box_slice(heap, idem.arena_size(), byte_of(0));
    var kbb = box_slice(heap, 300, byte_of(0));
    var r = 0;
    borrow mut ixb as &!iw in {
        borrow mut arb as &!aw in {
            borrow mut kbb as &!kw in {
                if which == 0 {
                    r = a_key_is_found_after_it_is_added(contents(iw), contents(aw), contents(kw));
                } else if which == 1 {
                    r = a_prefix_is_not_the_key(contents(iw), contents(aw), contents(kw));
                } else if which == 2 {
                    r = many_keys_all_found_and_no_others(contents(iw), contents(aw), contents(kw));
                } else if which == 3 {
                    r = the_window_and_the_body_check(contents(iw), contents(aw), contents(kw));
                } else if which == 4 {
                    r = a_full_index_refuses_and_keeps_what_it_has(contents(iw), contents(aw), contents(kw));
                } else {
                    r = the_key_alphabet(contents(kw));
                }
            }
        }
    }
    unbox_slice(heap, ixb);
    unbox_slice(heap, arb);
    unbox_slice(heap, kbb);
    return r;
}
