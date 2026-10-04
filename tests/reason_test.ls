edition 5;

import std.test;
import reason;

// Why an attempt failed (`src/reason.ls`, `docs/design.md` section 34.3): every code an attempt can end with has one reason; the reasons are numbered for ever
// (the numbers are on disk), named for the API and the metrics, and the history's `status` column keeps the coarse value it always held.

fn test_a_2xx_is_not_a_failure() -> [] int {
    test.assert_eq(reason.of(200), reason.none());
    test.assert_eq(reason.of(204), reason.none());
    test.assert_eq(reason.of(299), reason.none());
    return 0;
}

fn test_a_status_is_told_by_its_class_and_410_is_gone() -> [] int {
    test.assert_eq(reason.of(301), reason.status_3xx());
    test.assert_eq(reason.of(399), reason.status_3xx());
    test.assert_eq(reason.of(400), reason.status_4xx());
    test.assert_eq(reason.of(404), reason.status_4xx());
    test.assert_eq(reason.of(409), reason.status_4xx());
    test.assert_eq(reason.of(410), reason.gone());
    test.assert_eq(reason.of(411), reason.status_4xx());
    test.assert_eq(reason.of(499), reason.status_4xx());
    test.assert_eq(reason.of(500), reason.status_5xx());
    test.assert_eq(reason.of(503), reason.status_5xx());
    test.assert_eq(reason.of(599), reason.status_5xx());
    test.assert_eq(reason.of(100), reason.status_other());
    test.assert_eq(reason.of(199), reason.status_other());
    test.assert_eq(reason.of(600), reason.status_other());
    test.assert_eq(reason.of(999), reason.status_other());
    return 0;
}

// The negative codes of `attempt.ls`, each to its own reason.
fn test_each_way_an_attempt_ends_without_a_status_has_its_reason() -> [] int {
    test.assert_eq(reason.of(0 - 1), reason.connect_error());
    test.assert_eq(reason.of(0 - 2), reason.send_error());
    test.assert_eq(reason.of(0 - 3), reason.no_response());
    test.assert_eq(reason.of(0 - 4), reason.closed_early());
    test.assert_eq(reason.of(0 - 5), reason.connect_refused());
    test.assert_eq(reason.of(0 - 6), reason.connect_timeout());
    test.assert_eq(reason.of(0 - 7), reason.send_timeout());
    test.assert_eq(reason.of(0 - 8), reason.no_response());
    test.assert_eq(reason.of(0 - 9), reason.reset());
    test.assert_eq(reason.of(0 - 10), reason.closed_early());
    test.assert_eq(reason.of(0 - 11), reason.bad_response());
    test.assert_eq(reason.of(0 - 12), reason.busy());
    test.assert_eq(reason.of(0 - 13), reason.too_large());
    return 0;
}

// What the history's `status` held before reasons were recorded: -1 could not connect, -2 could not send, -3 timed out, -4 no answer.
fn test_the_history_status_keeps_its_coarse_values() -> [] int {
    test.assert_eq(reason.legacy_status(204), 204);
    test.assert_eq(reason.legacy_status(500), 500);
    test.assert_eq(reason.legacy_status(410), 410);
    test.assert_eq(reason.legacy_status(0 - 1), 0 - 1);
    test.assert_eq(reason.legacy_status(0 - 2), 0 - 2);
    test.assert_eq(reason.legacy_status(0 - 3), 0 - 3);
    test.assert_eq(reason.legacy_status(0 - 4), 0 - 4);
    // refused and a full table were -1 (could not connect)
    test.assert_eq(reason.legacy_status(0 - 5), 0 - 1);
    test.assert_eq(reason.legacy_status(0 - 12), 0 - 1);
    // every deadline was -3
    test.assert_eq(reason.legacy_status(0 - 6), 0 - 3);
    test.assert_eq(reason.legacy_status(0 - 7), 0 - 3);
    test.assert_eq(reason.legacy_status(0 - 8), 0 - 3);
    // a reset, a close and a bad status line were all -4 (no answer)
    test.assert_eq(reason.legacy_status(0 - 9), 0 - 4);
    test.assert_eq(reason.legacy_status(0 - 10), 0 - 4);
    test.assert_eq(reason.legacy_status(0 - 11), 0 - 4);
    // a request that did not fit was -2 (could not send)
    test.assert_eq(reason.legacy_status(0 - 13), 0 - 2);
    return 0;
}

// The numbers are written to disk: pin them. A reason is never renumbered or reused.
fn test_the_numbers_are_stable() -> [] int {
    test.assert_eq(reason.none(), 0);
    test.assert_eq(reason.connect_refused(), 1);
    test.assert_eq(reason.connect_timeout(), 2);
    test.assert_eq(reason.connect_error(), 3);
    test.assert_eq(reason.send_timeout(), 4);
    test.assert_eq(reason.send_error(), 5);
    test.assert_eq(reason.no_response(), 6);
    test.assert_eq(reason.reset(), 7);
    test.assert_eq(reason.closed_early(), 8);
    test.assert_eq(reason.bad_response(), 9);
    test.assert_eq(reason.status_3xx(), 10);
    test.assert_eq(reason.status_4xx(), 11);
    test.assert_eq(reason.status_5xx(), 12);
    test.assert_eq(reason.gone(), 13);
    test.assert_eq(reason.status_other(), 14);
    test.assert_eq(reason.busy(), 15);
    test.assert_eq(reason.too_large(), 16);
    test.assert_eq(reason.count(), 17);
    return 0;
}

// Every number below `count()` has a name of its own, and the names are what `/metrics` prints as a label value.
fn test_every_reason_has_its_own_name() -> [] int {
    var a = 0;
    while a < reason.count() {
        test.assert(len(reason.name(a)) > 0);
        test.assert(!bytes_same(reason.name(a), "unknown"));
        var b = a + 1;
        while b < reason.count() {
            test.assert(!bytes_same(reason.name(a), reason.name(b)));
            b = b + 1;
        }
        a = a + 1;
    }
    test.assert(bytes_same(reason.name(reason.count()), "unknown"));
    test.assert(bytes_same(reason.name(0 - 1), "unknown"));
    test.assert(bytes_same(reason.name(reason.connect_refused()), "connect_refused"));
    test.assert(bytes_same(reason.name(reason.no_response()), "no_response"));
    test.assert(bytes_same(reason.name(reason.reset()), "reset"));
    test.assert(bytes_same(reason.name(reason.status_5xx()), "status_5xx"));
    test.assert(bytes_same(reason.name(reason.gone()), "gone"));
    return 0;
}

fn bytes_same[&a, &b](x: &a [byte], y: &b [byte]) -> [] bool {
    if len(x) != len(y) {
        return false;
    }
    var i = 0;
    while i < len(x) {
        if x[i] != y[i] {
            return false;
        }
        i = i + 1;
    }
    return true;
}
