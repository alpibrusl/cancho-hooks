edition 5;

import std.test;
import dead;

// The table of dead letters (`src/dead.ls`, `docs/design.md` section 39.1): sorted by event id, bounded, one table a slot.

fn check_entries_are_kept_in_event_order_whatever_order_they_came[&d](d: &!d [int]) -> [] int {
    test.assert_eq(dead.put(d, 0, 30, 3, 12, 1000, 500), dead.put_added());
    test.assert_eq(dead.put(d, 0, 10, 4, 11, 2000, 100), dead.put_added());
    test.assert_eq(dead.put(d, 0, 20, 5, 6, 3000, 300), dead.put_added());
    test.assert_eq(dead.put(d, 0, 40, 6, 1, 4000, 0 - 1), dead.put_added());
    test.assert_eq(dead.count(d, 0), 4);
    test.assert_eq(dead.id_at(d, 0, 0), 10);
    test.assert_eq(dead.id_at(d, 0, 1), 20);
    test.assert_eq(dead.id_at(d, 0, 2), 30);
    test.assert_eq(dead.id_at(d, 0, 3), 40);
    // every field travels with its entry
    test.assert_eq(dead.attempts_at(d, 0, 0), 4);
    test.assert_eq(dead.reason_at(d, 0, 0), 11);
    test.assert_eq(dead.died_at(d, 0, 0), 2000);
    test.assert_eq(dead.offset_at(d, 0, 0), 100);
    test.assert_eq(dead.attempts_at(d, 0, 2), 3);
    test.assert_eq(dead.reason_at(d, 0, 2), 12);
    test.assert_eq(dead.offset_at(d, 0, 2), 500);
    // an offset that is not known reads as -1
    test.assert_eq(dead.offset_at(d, 0, 3), 0 - 1);
    // another slot is another table
    test.assert_eq(dead.count(d, 1), 0);
    return 0;
}

fn test_entries_are_kept_in_event_order_whatever_order_they_came[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_entries_are_kept_in_event_order_whatever_order_they_came(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

fn check_find_and_the_places_of_an_id_in_the_table[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 1, 1, 1, 0);
    dead.put(d, 0, 20, 1, 1, 1, 0);
    dead.put(d, 0, 30, 1, 1, 1, 0);
    test.assert_eq(dead.find(d, 0, 20), 1);
    test.assert_eq(dead.find(d, 0, 10), 0);
    test.assert_eq(dead.find(d, 0, 30), 2);
    test.assert_eq(dead.find(d, 0, 25), 0 - 1);
    test.assert_eq(dead.find(d, 0, 5), 0 - 1);
    test.assert_eq(dead.find(d, 0, 35), 0 - 1);
    test.assert_eq(dead.find(d, 1, 20), 0 - 1);
    // below: entries with a smaller id; upto: entries with that id or a smaller one
    test.assert_eq(dead.below(d, 0, 10), 0);
    test.assert_eq(dead.below(d, 0, 11), 1);
    test.assert_eq(dead.below(d, 0, 30), 2);
    test.assert_eq(dead.below(d, 0, 31), 3);
    test.assert_eq(dead.upto(d, 0, 10), 1);
    test.assert_eq(dead.upto(d, 0, 9), 0);
    test.assert_eq(dead.upto(d, 0, 30), 3);
    test.assert_eq(dead.upto(d, 0, 1000), 3);
    return 0;
}

fn test_find_and_the_places_of_an_id_in_the_table[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_find_and_the_places_of_an_id_in_the_table(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

fn check_an_event_that_dies_again_replaces_its_entry_and_keeps_its_offset[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 4, 11, 2000, 700);
    test.assert_eq(dead.put(d, 0, 10, 10, 12, 9000, 0 - 1), dead.put_replaced());
    test.assert_eq(dead.count(d, 0), 1);
    test.assert_eq(dead.attempts_at(d, 0, 0), 10);
    test.assert_eq(dead.reason_at(d, 0, 0), 12);
    test.assert_eq(dead.died_at(d, 0, 0), 9000);
    test.assert_eq(dead.offset_at(d, 0, 0), 700);
    // a known offset replaces
    dead.put(d, 0, 10, 10, 12, 9000, 800);
    test.assert_eq(dead.offset_at(d, 0, 0), 800);
    return 0;
}

fn test_an_event_that_dies_again_replaces_its_entry_and_keeps_its_offset[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_an_event_that_dies_again_replaces_its_entry_and_keeps_its_offset(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

fn check_remove_closes_the_gap_and_a_missing_event_is_nothing[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 1, 1, 1, 0);
    dead.put(d, 0, 20, 2, 1, 1, 0);
    dead.put(d, 0, 30, 3, 1, 1, 0);
    test.assert_eq(dead.remove(d, 0, 20), 1);
    test.assert_eq(dead.count(d, 0), 2);
    test.assert_eq(dead.id_at(d, 0, 0), 10);
    test.assert_eq(dead.id_at(d, 0, 1), 30);
    test.assert_eq(dead.attempts_at(d, 0, 1), 3);
    test.assert_eq(dead.remove(d, 0, 20), 0);
    test.assert_eq(dead.remove(d, 1, 10), 0);
    test.assert_eq(dead.remove(d, 0, 30), 1);
    test.assert_eq(dead.remove(d, 0, 10), 1);
    test.assert_eq(dead.count(d, 0), 0);
    // the freed place is clean
    test.assert_eq(dead.id_at(d, 0, 0), 0);
    test.assert_eq(dead.died_at(d, 0, 0), 0);
    return 0;
}

fn test_remove_closes_the_gap_and_a_missing_event_is_nothing[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_remove_closes_the_gap_and_a_missing_event_is_nothing(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

fn check_the_reason_is_set_only_for_the_attempt_it_explains[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 4, 0, 1, 0);
    test.assert_eq(dead.set_reason(d, 0, 10, 3, 12), 0);
    test.assert_eq(dead.reason_at(d, 0, 0), 0);
    test.assert_eq(dead.set_reason(d, 0, 10, 4, 12), 1);
    test.assert_eq(dead.reason_at(d, 0, 0), 12);
    test.assert_eq(dead.attempts_at(d, 0, 0), 4);
    test.assert_eq(dead.set_reason(d, 0, 11, 4, 12), 0);
    return 0;
}

fn test_the_reason_is_set_only_for_the_attempt_it_explains[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_the_reason_is_set_only_for_the_attempt_it_explains(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

// Full: the newest `cap()` are kept, the oldest go, and the count of what went is exact.
fn check_a_full_table_keeps_the_newest_and_counts_what_it_dropped[&d](d: &!d [int]) -> [] int {
    var id = 1;
    while id <= dead.cap() {
        test.assert_eq(dead.put(d, 0, id, 1, 1, 1, 0), dead.put_added());
        id = id + 1;
    }
    test.assert_eq(dead.count(d, 0), dead.cap());
    test.assert_eq(dead.floor(d, 0), 0);
    // fifty more, newer: the fifty oldest go
    while id <= dead.cap() + 50 {
        test.assert_eq(dead.put(d, 0, id, 1, 1, 1, 0), dead.put_pushed_out());
        id = id + 1;
    }
    test.assert_eq(dead.count(d, 0), dead.cap());
    test.assert_eq(dead.floor(d, 0), 50);
    test.assert_eq(dead.id_at(d, 0, 0), 51);
    test.assert_eq(dead.id_at(d, 0, dead.cap() - 1), dead.cap() + 50);
    test.assert_eq(dead.find(d, 0, 50), 0 - 1);
    test.assert_eq(dead.find(d, 0, 51), 0);
    // one older than all of them is not entered
    test.assert_eq(dead.put(d, 0, 7, 1, 1, 1, 0), dead.put_left_out());
    test.assert_eq(dead.count(d, 0), dead.cap());
    test.assert_eq(dead.floor(d, 0), 50);
    // a full table does not want completing, whatever was left out: it would read the log for nothing at every look
    test.assert(!dead.wants_refold(d, 0));
    // a hole in it: room, and something was left out, so it wants completing from the log; resetting the floor says it has been
    test.assert_eq(dead.remove(d, 0, 1000), 1);
    test.assert(dead.wants_refold(d, 0));
    dead.reset_floor(d, 0);
    test.assert(!dead.wants_refold(d, 0));
    test.assert_eq(dead.floor(d, 0), 0);
    test.assert_eq(dead.put(d, 0, 1000, 5, 1, 1, 0), dead.put_added());
    // one in the middle pushes the smallest out
    test.assert_eq(dead.remove(d, 0, 1000), 1);
    test.assert_eq(dead.put(d, 0, 1000, 5, 1, 1, 0), dead.put_added());
    test.assert_eq(dead.put(d, 0, 100000, 5, 1, 1, 0), dead.put_pushed_out());
    test.assert_eq(dead.id_at(d, 0, 0), 52);
    // still sorted, all the way
    var k = 1;
    while k < dead.count(d, 0) {
        test.assert(dead.id_at(d, 0, k - 1) < dead.id_at(d, 0, k));
        k = k + 1;
    }
    // the other slot is untouched
    test.assert_eq(dead.count(d, 1), 0);
    test.assert_eq(dead.floor(d, 1), 0);
    return 0;
}

fn test_a_full_table_keeps_the_newest_and_counts_what_it_dropped[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_a_full_table_keeps_the_newest_and_counts_what_it_dropped(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

fn check_clear_forgets_one_slot_only[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 1, 1, 1, 0);
    dead.put(d, 1, 10, 1, 1, 1, 0);
    dead.put(d, 1, 11, 1, 1, 1, 0);
    dead.clear(d, 1);
    test.assert_eq(dead.count(d, 0), 1);
    test.assert_eq(dead.count(d, 1), 0);
    test.assert_eq(dead.find(d, 1, 10), 0 - 1);
    test.assert_eq(dead.id_at(d, 1, 0), 0);
    return 0;
}

fn test_clear_forgets_one_slot_only[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_clear_forgets_one_slot_only(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

// `clear` leaves not one word of what the entries held (the invariant the dense layout depends on: everything past a count is zero), the last word of an entry included.
fn test_clear_leaves_no_word_of_an_entry_behind[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        let d = contents(tw);
        dead.put(d, 0, 10, 3, 5, 77, 0);
        dead.put(d, 0, 20, 4, 6, 88, 0);
        dead.set_offset(d, 0, 0, 123);
        dead.set_offset(d, 0, 1, 456);
        dead.clear(d, 0);
        test.assert_eq(dead.count(d, 0), 0);
        // the entries of slot 0 start after the two dense headers of four words
        var k = 8;
        var clean = true;
        while k < 8 + 2 * 4 {
            if d[k] != 0 {
                clean = false;
            }
            k = k + 1;
        }
        test.assert(clean);
    }
    unbox_slice(heap, tb);
    return 0;
}

// Every put into a full table, wherever the new id falls, leaves it sorted, with each id once, holding the largest `cap()` ids seen.
fn check_full_table_against_a_model[&d](d: &!d [int]) -> [] int {
    // ids 10, 20, ... 20480 fill it; then ids that fall everywhere in between and beyond, each checked
    var id = 1;
    while id <= dead.cap() {
        dead.put(d, 0, id * 10, 1, 1, 1, 0);
        id = id + 1;
    }
    var probe = 7;
    var round = 0;
    while round < 300 {
        // 5, 15, 25, ... : between the entries; every 7th one is above all of them
        var newid = probe * 10 + 5;
        if round % 7 == 0 {
            newid = 30000 + round;
        }
        let before = dead.count(d, 0);
        let low = dead.id_at(d, 0, 0);
        let verdict = dead.put(d, 0, newid, 2, 2, 2, 0);
        test.assert_eq(dead.count(d, 0), before);
        if newid < low {
            test.assert_eq(verdict, dead.put_left_out());
            test.assert_eq(dead.find(d, 0, newid), 0 - 1);
        } else {
            test.assert_eq(verdict, dead.put_pushed_out());
            test.assert(dead.find(d, 0, newid) >= 0);
            test.assert_eq(dead.find(d, 0, low), 0 - 1);
        }
        probe = probe + 11;
        round = round + 1;
    }
    var k = 1;
    while k < dead.count(d, 0) {
        test.assert(dead.id_at(d, 0, k - 1) < dead.id_at(d, 0, k));
        k = k + 1;
    }
    return 0;
}

fn test_a_full_table_stays_sorted_and_free_of_duplicates_whatever_falls_into_it[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_full_table_against_a_model(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

// A fold of the log that goes back over a slot's earlier holders drops what the table has from before below its limit: those entries and no others, the one at the limit
// itself staying (an id below `id`, not `id` and below).
fn check_clear_below[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 1, 1, 1, 0);
    dead.put(d, 0, 20, 2, 2, 2, 0);
    dead.put(d, 0, 30, 3, 3, 3, 0);
    dead.put(d, 0, 40, 4, 4, 4, 0);
    test.assert_eq(dead.clear_below(d, 0, 30), 2);
    test.assert_eq(dead.count(d, 0), 2);
    test.assert_eq(dead.id_at(d, 0, 0), 30);
    test.assert_eq(dead.attempts_at(d, 0, 0), 3);
    test.assert_eq(dead.id_at(d, 0, 1), 40);
    test.assert_eq(dead.attempts_at(d, 0, 1), 4);
    // the places behind are clean
    test.assert_eq(dead.id_at(d, 0, 2), 0);
    test.assert_eq(dead.id_at(d, 0, 3), 0);
    // nothing below: nothing happens
    test.assert_eq(dead.clear_below(d, 0, 30), 0);
    test.assert_eq(dead.count(d, 0), 2);
    // everything below
    test.assert_eq(dead.clear_below(d, 0, 41), 2);
    test.assert_eq(dead.count(d, 0), 0);
    return 0;
}

fn test_clear_below_drops_the_entries_below_an_id_and_keeps_the_one_at_it[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_clear_below(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

// Expiry, the floor a snapshot carries and the settled flag (`src/dead.ls`, `docs/design.md` section 39.1).
fn check_expire_and_the_floor[&d](d: &!d [int]) -> [] int {
    dead.put(d, 0, 10, 4, 1, 1000, 0 - 1);
    dead.put(d, 0, 20, 4, 1, 1000, 0 - 1);
    dead.put(d, 0, 30, 4, 1, 1000, 0 - 1);
    dead.put(d, 1, 5, 4, 1, 1000, 0 - 1);
    // a floor from a snapshot, and a fold that found nothing: settled, so not asked for again
    dead.raise_floor(d, 0, 15);
    test.assert_eq(dead.floor(d, 0), 15);
    test.assert(dead.wants_refold(d, 0));
    dead.settle(d, 0);
    test.assert(dead.is_settled(d, 0));
    test.assert(!dead.wants_refold(d, 0));
    // the floor only rises, and a rise clears the flag
    dead.raise_floor(d, 0, 12);
    test.assert_eq(dead.floor(d, 0), 15);
    test.assert(dead.is_settled(d, 0));
    dead.raise_floor(d, 0, 18);
    test.assert_eq(dead.floor(d, 0), 18);
    test.assert(!dead.is_settled(d, 0));
    // events below 16 are gone: the entry at 10 goes, the floor (18) is still above the first event that is left
    test.assert_eq(dead.expire(d, 0, 16), 1);
    test.assert_eq(dead.count(d, 0), 2);
    test.assert_eq(dead.id_at(d, 0, 0), 20);
    test.assert_eq(dead.floor(d, 0), 18);
    // events below 21 are gone: the entry at 20 goes and the floor, which is below 21, is no floor any more
    dead.settle(d, 0);
    test.assert_eq(dead.expire(d, 0, 21), 1);
    test.assert_eq(dead.count(d, 0), 1);
    test.assert_eq(dead.floor(d, 0), 0);
    test.assert(!dead.is_settled(d, 0));
    // nothing to expire is nothing, and another slot is not touched
    test.assert_eq(dead.expire(d, 0, 21), 0);
    test.assert_eq(dead.count(d, 1), 1);
    // clear forgets the flag as well
    dead.raise_floor(d, 0, 40);
    dead.settle(d, 0);
    dead.clear(d, 0);
    test.assert_eq(dead.floor(d, 0), 0);
    test.assert(!dead.is_settled(d, 0));
    return 0;
}

fn test_expire_drops_what_retention_dropped_and_the_floor_with_it[&h](heap: &!h Heap) -> [heap] int {
    let tb = box_slice(heap, 2 * dead.block(), 0);
    borrow mut tb as &!tw in {
        check_expire_and_the_floor(contents(tw));
    }
    unbox_slice(heap, tb);
    return 0;
}

// The real array holds a block for each of the 1,024 slots (section 41.4): the last slot's table is its own, in no other's place, and `clear` leaves nothing of it.
fn test_the_last_slot_of_the_real_array_has_a_table_of_its_own[&h](heap: &!h Heap) -> [heap] int {
    test.assert_eq(dead.size(), 1024 * dead.block());
    let tb = box_slice(heap, dead.size(), 0);
    borrow mut tb as &!tw in {
        let d = contents(tw);
        test.assert_eq(dead.put(d, 1023, 7, 3, 12, 1000, 55), dead.put_added());
        test.assert_eq(dead.put(d, 1023, 5, 4, 11, 2000, 0 - 1), dead.put_added());
        test.assert_eq(dead.count(d, 1023), 2);
        test.assert_eq(dead.count(d, 1022), 0);
        test.assert_eq(dead.count(d, 0), 0);
        test.assert_eq(dead.id_at(d, 1023, 0), 5);
        test.assert_eq(dead.id_at(d, 1023, 1), 7);
        test.assert_eq(dead.offset_at(d, 1023, 1), 55);
        test.assert_eq(dead.find(d, 1022, 7), 0 - 1);
        test.assert_eq(dead.put(d, 0, 9, 1, 1, 1, 1), dead.put_added());
        test.assert_eq(dead.count(d, 0), 1);
        test.assert_eq(dead.count(d, 1023), 2);
        dead.clear(d, 1023);
        test.assert_eq(dead.count(d, 1023), 0);
        test.assert_eq(dead.id_at(d, 1023, 0), 0);
        test.assert_eq(dead.id_at(d, 1023, 1), 0);
        test.assert_eq(dead.floor(d, 1023), 0);
        test.assert_eq(dead.count(d, 0), 1);
    }
    unbox_slice(heap, tb);
    return 0;
}
