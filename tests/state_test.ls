edition 5;

import std.test;
import record;
import state;

// The delivery window (`src/state.ls`): cursors, out-of-order finals, retries, the edge of the window, ring reuse, endpoints
// that do not see each other, and the outcome record. Two endpoints' cells (48 KiB) fit one arena.

fn test_in_order_deliveries_move_the_cursor() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        test.assert_eq(state.apply(w, c, 0, state.delivered(), 1, 1, 0), 0);
        test.assert_eq(c[0], 1);
        test.assert_eq(state.apply(w, c, 0, state.delivered(), 2, 1, 0), 0);
        test.assert_eq(c[0], 2);
        test.assert_eq(c[1], 0);
    }
    return 0;
}

fn test_a_later_final_waits_for_the_earlier_one() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        state.apply(w, c, 0, state.delivered(), 3, 1, 0);
        state.apply(w, c, 0, state.delivered(), 2, 1, 0);
        test.assert_eq(c[0], 0);
        test.assert(state.is_final(w, c, 0, 2));
        test.assert(state.is_final(w, c, 0, 3));
        test.assert(!state.is_final(w, c, 0, 1));
        // The gap filled: the cursor jumps over all three.
        state.apply(w, c, 0, state.dead(), 1, 10, 0);
        test.assert_eq(c[0], 3);
        test.assert(state.is_final(w, c, 0, 1));
    }
    return 0;
}

fn test_a_failure_records_attempts_and_the_next_time() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        state.apply(w, c, 0, state.failed(), 1, 1, 5000);
        test.assert_eq(state.attempts(w, 0, 1), 1);
        test.assert_eq(state.next_at(w, 0, 1), 5000);
        test.assert(!state.is_final(w, c, 0, 1));
        state.apply(w, c, 0, state.failed(), 1, 2, 9000);
        test.assert_eq(state.attempts(w, 0, 1), 2);
        test.assert_eq(state.next_at(w, 0, 1), 9000);
        test.assert_eq(c[0], 0);
        state.apply(w, c, 0, state.delivered(), 1, 3, 0);
        test.assert_eq(c[0], 1);
    }
    return 0;
}

fn test_replaying_a_record_changes_nothing() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        state.apply(w, c, 0, state.delivered(), 1, 1, 0);
        state.apply(w, c, 0, state.delivered(), 3, 1, 0);
        // The same two again, and a stale failure for an id already final.
        state.apply(w, c, 0, state.delivered(), 1, 1, 0);
        state.apply(w, c, 0, state.delivered(), 3, 1, 0);
        state.apply(w, c, 0, state.failed(), 1, 7, 99);
        test.assert_eq(c[0], 1);
        test.assert(state.is_final(w, c, 0, 3));
        test.assert(!state.is_final(w, c, 0, 2));
    }
    return 0;
}

fn test_the_window_has_an_edge() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        test.assert(state.in_window(c, 0, 1));
        test.assert(state.in_window(c, 0, state.span()));
        test.assert(!state.in_window(c, 0, state.span() + 1));
        test.assert(!state.in_window(c, 0, 0));
        test.assert_eq(state.apply(w, c, 0, state.delivered(), state.span() + 1, 1, 0), 0 - 1);
        test.assert_eq(c[0], 0);
        // One more event finishing moves the edge by one.
        state.apply(w, c, 0, state.delivered(), 1, 1, 0);
        test.assert(state.in_window(c, 0, state.span() + 1));
        test.assert_eq(state.apply(w, c, 0, state.delivered(), state.span() + 1, 1, 0), 0);
    }
    return 0;
}

// Ids wrap around the ring: after a full window has been finished, the same cells serve the next window, and nothing of the
// first window's attempts or times is left in them.
fn test_a_reused_cell_starts_clean() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        state.apply(w, c, 0, state.failed(), 5, 4, 12345);
        var id = 1;
        while id <= state.span() {
            state.apply(w, c, 0, state.delivered(), id, 1, 0);
            id = id + 1;
        }
        test.assert_eq(c[0], state.span());
        // 5 + window() maps to the cell id 5 used.
        let again = 5 + state.span();
        test.assert(state.in_window(c, 0, again));
        test.assert(!state.is_final(w, c, 0, again));
        test.assert_eq(state.attempts(w, 0, again), 0);
        test.assert_eq(state.next_at(w, 0, again), 0);
    }
    return 0;
}

fn test_endpoints_do_not_see_each_other() -> [] int {
    region a {
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        state.apply(w, c, 0, state.delivered(), 1, 1, 0);
        state.apply(w, c, 1, state.failed(), 1, 3, 777);
        test.assert_eq(c[0], 1);
        test.assert_eq(c[1], 0);
        test.assert(!state.is_final(w, c, 1, 1));
        test.assert_eq(state.attempts(w, 1, 1), 3);
        test.assert_eq(state.next_at(w, 1, 1), 777);
    }
    return 0;
}

fn test_an_outcome_reads_back() -> [] int {
    region a {
        let buf = alloc_slice[a](128, byte_of(0));
        let total = state.put_outcome(buf, 0, 42, state.failed(), 3, 99, 4, 1700000123456);
        let r = record.check(buf, 0, total, 1048576);
        test.assert_eq(r.0, record.ok());
        test.assert_eq(record.ms_of(buf, 0), 42);
        let o = state.outcome_at(buf, 0);
        test.assert_eq(o.0, state.failed());
        test.assert_eq(o.1, 3);
        test.assert_eq(o.2, 99);
        test.assert_eq(o.3, 4);
        test.assert_eq(o.4, 1700000123456);
    }
    return 0;
}

// A well-formed record that is not an outcome is kind 0, never a guess.
fn test_a_foreign_record_is_not_an_outcome() -> [] int {
    region a {
        let buf = alloc_slice[a](128, byte_of(0));
        var p = record.begin(buf, 0, 1, 0, 1);
        p = record.put_pair(buf, p, "event", "{}");
        record.seal(buf, 0, p);
        test.assert_eq(state.outcome_at(buf, 0).0, 0);
        // The right key and length, an unknown kind.
        let again = state.put_outcome(buf, 0, 2, state.delivered(), 0, 1, 1, 0);
        record.put_u64(buf, record.first_pair(0) + 4 + 1 + 4, 99);
        test.assert_eq(state.outcome_at(buf, 0).0, 0);
    }
    return 0;
}

// The endpoint records (disabled, enabled) read back as themselves, and `apply` does nothing with them: an id of 0 is at or
// below every cursor, but a record with an id in the window must not make that id final either.
fn test_endpoint_records_read_back_and_change_no_cell() -> [] int {
    region a {
        let buf = alloc_slice[a](128, byte_of(0));
        state.put_outcome(buf, 0, 7, state.disabled(), 2, 0, 0, 0);
        let o = state.outcome_at(buf, 0);
        test.assert_eq(o.0, state.disabled());
        test.assert_eq(o.1, 2);
        state.put_outcome(buf, 0, 8, state.enabled(), 2, 0, 0, 0);
        test.assert_eq(state.outcome_at(buf, 0).0, state.enabled());
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        test.assert_eq(state.apply(w, c, 0, state.disabled(), 1, 1, 0), 0);
        test.assert_eq(state.apply(w, c, 0, state.enabled(), 1, 1, 0), 0);
        test.assert_eq(c[0], 0);
        test.assert(!state.is_final(w, c, 0, 1));
        test.assert_eq(state.attempts(w, 0, 1), 0);
    }
    return 0;
}

// The replay records read back as themselves, with their attempts and next time, and change no cell either.
fn test_replay_records_read_back_and_change_no_cell() -> [] int {
    region a {
        let buf = alloc_slice[a](128, byte_of(0));
        state.put_outcome(buf, 0, 9, state.replay(), 1, 42, 0, 0);
        let o = state.outcome_at(buf, 0);
        test.assert_eq(o.0, state.replay());
        test.assert_eq(o.1, 1);
        test.assert_eq(o.2, 42);
        state.put_outcome(buf, 0, 10, state.replay_failed(), 1, 42, 3, 1700000000000);
        let f = state.outcome_at(buf, 0);
        test.assert_eq(f.0, state.replay_failed());
        test.assert_eq(f.3, 3);
        test.assert_eq(f.4, 1700000000000);
        state.put_outcome(buf, 0, 11, state.replay_delivered(), 1, 42, 4, 0);
        test.assert_eq(state.outcome_at(buf, 0).0, state.replay_delivered());
        state.put_outcome(buf, 0, 12, state.replay_dead(), 1, 42, 9, 0);
        test.assert_eq(state.outcome_at(buf, 0).0, state.replay_dead());
        let w = alloc_slice[a](state.cells(2), 0);
        let c = alloc_slice[a](2, 0);
        test.assert_eq(state.apply(w, c, 0, state.replay_delivered(), 1, 1, 0), 0);
        test.assert_eq(state.apply(w, c, 0, state.replay_dead(), 1, 1, 0), 0);
        test.assert_eq(c[0], 0);
        test.assert(!state.is_final(w, c, 0, 1));
    }
    return 0;
}
