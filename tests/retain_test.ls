edition 5;

import std.test;
import record;
import retain;
import evlog;
import store;

// The rules of retention (`src/retain.ls`), the header of a segment (`src/evlog.ls`) and the names of segments (`src/store.ls`):
// pure functions, each case at its edge.

fn test_the_age_an_event_must_reach_is_the_longer_of_retention_and_window() -> [] int {
    test.assert_eq(retain.age_needed(1000, 500), 1000);
    test.assert_eq(retain.age_needed(500, 1000), 1000);
    test.assert_eq(retain.age_needed(1000, 1000), 1000);
    // retention off: nothing is ever old enough, whatever the window
    test.assert_eq(retain.age_needed(0, 1000), 0);
    return 0;
}

// may_drop(retention, last_id, sealed_ms, floor, now, age)
fn test_a_segment_goes_only_when_sealed_final_everywhere_and_old_enough() -> [] int {
    test.assert(retain.may_drop(1000, 10, 5000, 10, 6000, 1000));
    // one millisecond short of the age
    test.assert(!retain.may_drop(1000, 10, 5000, 10, 5999, 1000));
    // an event of the segment is above the floor: not final somewhere
    test.assert(!retain.may_drop(1000, 11, 5000, 10, 6000, 1000));
    test.assert(retain.may_drop(1000, 10, 5000, 12, 6000, 1000));
    // retention off
    test.assert(!retain.may_drop(0, 10, 5000, 10, 600000, 0));
    // no sealed segment (the oldest is the active one)
    test.assert(!retain.may_drop(1000, 0 - 1, 0 - 1, 10, 600000, 1000));
    test.assert(!retain.may_drop(1000, 0, 5000, 10, 600000, 1000));
    // a floor of 0 (an endpoint has not delivered anything) keeps everything
    test.assert(!retain.may_drop(1000, 1, 5000, 0, 600000, 1000));
    return 0;
}

// should_roll(active bytes, limit, retention, created, now, age)
fn test_the_active_segment_is_sealed_at_its_size_or_when_it_has_been_open_as_long_as_an_event_must_be_kept() -> [] int {
    test.assert(retain.should_roll(1000, 1000, 5000, 100, 101, 5000));
    test.assert(!retain.should_roll(999, 1000, 5000, 100, 101, 5000));
    // old enough, and not empty
    test.assert(retain.should_roll(1, 1000, 5000, 100, 5100, 5000));
    test.assert(!retain.should_roll(1, 1000, 5000, 100, 5099, 5000));
    // empty: sealing nothing is not a roll
    test.assert(!retain.should_roll(0, 1000, 5000, 100, 600000, 5000));
    // retention off: only its size seals it
    test.assert(!retain.should_roll(1, 1000, 0, 100, 600000, 0));
    test.assert(retain.should_roll(5000, 1000, 0, 100, 101, 0));
    return 0;
}

fn test_the_outcomes_log_is_replaced_at_its_limit_or_four_times_the_last_snapshot() -> [] int {
    test.assert(retain.snapshot_due(1000, 1000, 0));
    test.assert(!retain.snapshot_due(999, 1000, 0));
    // a snapshot that was 400 bytes: four times is 1,600, which is more than the limit
    test.assert(!retain.snapshot_due(1599, 1000, 400));
    test.assert(retain.snapshot_due(1600, 1000, 400));
    // a small one: the limit rules
    test.assert(retain.snapshot_due(1000, 1000, 100));
    return 0;
}

fn test_a_segment_header_reads_back_as_written() -> [] int {
    region a {
        let buf = alloc_slice[a](512, byte_of(0));
        let n = evlog.put_header(buf, 7, 123456789, 4001, 1767225600000);
        let h = evlog.parse_head(buf, n);
        test.assert_eq(h.0, 0);
        test.assert_eq(h.1, n);
        test.assert_eq(h.2, 7);
        test.assert_eq(h.3, 123456789);
        test.assert_eq(h.4, 4001);
        test.assert_eq(h.5, 1767225600000);
        test.assert_eq(h.6, 2);
        // a header is the record with id (0, 0), and its three pairs are the shape the previous version refuses
        test.assert_eq(record.ms_of(buf, 0), 0);
        test.assert_eq(record.seq_of(buf, 0), 0);
        test.assert_eq(record.fields_of(buf, 0), 3);
        // cut anywhere short of its end, it is "nothing whole yet"
        var cut = 0;
        while cut < n {
            test.assert_eq(evlog.parse_head(buf, cut).0, 2);
            cut = cut + 1;
        }
    }
    return 0;
}

fn test_a_header_of_another_format_is_refused_and_a_damaged_one_is_not_believed() -> [] int {
    region a {
        let buf = alloc_slice[a](512, byte_of(0));
        let n = evlog.put_header(buf, 1, 0, 1, 5);
        // the version is the last byte of the "format" pair's value ("... events 2"): make it 3, and seal the record again so that only the version is wrong
        let p = record.pair_at(buf, record.first_pair(0));
        let at = p.2 + p.3 - 1;
        test.assert_eq(int_of(buf[at]), '2');
        buf[at] = byte_of('3');
        record.seal(buf, 0, n);
        let h = evlog.parse_head(buf, n);
        test.assert_eq(h.0, 4);
        test.assert_eq(h.6, 3);
        // a flipped byte anywhere in a whole record is damage
        buf[at] = byte_of('2');
        record.seal(buf, 0, n);
        test.assert_eq(evlog.parse_head(buf, n).0, 0);
        buf[n - 1] = byte_of(int_of(buf[n - 1]) ^ 1);
        test.assert_eq(evlog.parse_head(buf, n).0, 3);
    }
    return 0;
}

fn test_a_first_event_is_a_format_1_segment() -> [] int {
    region a {
        let buf = alloc_slice[a](512, byte_of(0));
        let p = record.begin(buf, 0, 1, 0, 1);
        let n = record.seal(buf, 0, record.put_pair(buf, p, "event", "{\"type\":\"t\"}"));
        let h = evlog.parse_head(buf, n);
        test.assert_eq(h.0, 1);
        test.assert_eq(h.1, 0);
        test.assert_eq(h.4, 1);
        // an empty file is nothing whole
        test.assert_eq(evlog.parse_head(buf, 0).0, 2);
        // a record with id 0 that is not a header is not this program's
        let q = record.begin(buf, 0, 0, 0, 1);
        let m = record.seal(buf, 0, record.put_pair(buf, q, "event", "{}"));
        test.assert_eq(evlog.parse_head(buf, m).0, 5);
        // a header with the wrong number of pairs
        let r = record.begin(buf, 0, 0, 0, 2);
        var e2 = record.put_pair(buf, r, "format", "lexsys-hooks events 2");
        e2 = record.put_pair(buf, e2, "segment", "x");
        test.assert_eq(evlog.parse_head(buf, record.seal(buf, 0, e2)).0, 5);
    }
    return 0;
}

fn test_segments_are_named_events_seg_then_events_dash_k_seg() -> [] int {
    region a {
        let buf = alloc_slice[a](64, byte_of(0));
        var n = store.seg_name(buf, 0);
        test.assert(bytes_are(buf[0..n], "events.seg"));
        n = store.seg_name(buf, 1);
        test.assert(bytes_are(buf[0..n], "events-1.seg"));
        n = store.seg_name(buf, 123456);
        test.assert(bytes_are(buf[0..n], "events-123456.seg"));
        n = store.path_join(buf, "/var/lib/hooks", "events.first");
        test.assert(bytes_are(buf[0..n], "/var/lib/hooks/events.first"));
        n = store.nat_text(buf, 3, 90210);
        test.assert_eq(n, 5);
        test.assert_eq(int_of(buf[3]), '9');
        test.assert_eq(int_of(buf[7]), '0');
        n = store.nat_text(buf, 0, 0);
        test.assert_eq(n, 1);
        test.assert_eq(int_of(buf[0]), '0');
    }
    return 0;
}

fn bytes_are[&a, &b](x: &a [byte], want: &b [byte]) -> [] bool {
    if len(x) != len(want) {
        return false;
    }
    var i = 0;
    while i < len(x) {
        if int_of(x[i]) != int_of(want[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}
