edition 5;

module state;

import record;

// `state` -- what is known about each (endpoint, event) pair (`docs/design.md` sections 3, 4 and 15).
//
// **A cursor per endpoint, and a window of what is not final yet.** An event is *final* for an endpoint once it was delivered
// (a `2xx`) or dead-lettered (the schedule ran out). `cur[e]` is the largest id such that every event up to it is final for
// endpoint `e`. Events above it may be final too (a later event delivered while an earlier one waits for its retry), and
// those, with the attempts so far and the time of the next attempt of the ones that are not, live in a ring of
// `span()` cells per endpoint, indexed by `id % span()`. Ids above `cur[e] + span()` are not considered at all, which
// is the one place this design pushes back on the ingest: an endpoint more than `span()` events behind stops being served
// until it catches up (backpressure, stated in the design, not a silent drop).
//
// The cells are the only state; the log of outcomes (`put_outcome`) is how they survive a restart, by replaying every record
// through `apply`. `apply` is idempotent for ids at or below the cursor, so replaying a record twice, or after a compaction
// that kept some of the history, changes nothing.

pub fn span() -> [] int {
    return 1024;
}

// How many endpoints the service has at once: the slots of the arrays below (`docs/design.md` section 41: it was 62, the width of the one integer that held
// the disabled set; each flag is a word a slot now). This is the one place the number is written.
pub fn max_endpoints() -> [] int {
    return 1024;
}

// The slots below this are the ones every build has had (it was the limit, and the endpoint field of the format header is still written as it). A log that
// uses a slot at or above it carries a record of kind `wide()` first, so that a build from before refuses it (section 41.5).
pub fn first_wide() -> [] int {
    return 62;
}

// The kinds of outcome record. 0 is not one: it is what a record that does not decode answers.
pub fn delivered() -> [] int {
    return 1;
}

pub fn failed() -> [] int {
    return 2;
}

pub fn dead() -> [] int {
    return 3;
}

// Two records that are about an endpoint, not an event: it was disabled (a `410 Gone`, or a person), and it was enabled again.
// Their `id`, `attempts` and `next_at` are 0, and `apply` does nothing with them.
pub fn disabled() -> [] int {
    return 4;
}

pub fn enabled() -> [] int {
    return 5;
}

// Replay (`docs/design.md` section 23): an event is sent again to one endpoint although it is final there. A replay is asked
// for (`replay`, with the endpoint and event) and then has outcomes of its own, which are not the window's: the attempt failed
// (with its count and the time of the next), or ended delivered or dead.
pub fn replay() -> [] int {
    return 6;
}

pub fn replay_failed() -> [] int {
    return 7;
}

pub fn replay_delivered() -> [] int {
    return 8;
}

pub fn replay_dead() -> [] int {
    return 9;
}

// Two records about a *slot*, not an event (`docs/design.md` section 25): the endpoint with `id` was given the slot, its cursor
// starting at `attempts` (the record's fourth field), and the slot was freed. Everything before a `created` for a slot, and
// everything after a `removed` until the next `created`, is about an endpoint that is not the slot's now. `apply` does nothing
// with them: `reset` is what recovery does.
pub fn created() -> [] int {
    return 10;
}

pub fn removed() -> [] int {
    return 11;
}

// Two records about an endpoint's *health*, not an event (`docs/design.md` section 31): a streak of failures began (`next_at`, the record's
// fifth field, is its start in Unix ms; written once, by the first failed attempt after a delivery), and the circuit breaker paused the
// endpoint. A delivery ends a streak (no record: `delivered` is the record) and `enabled` ends a pause and a streak. `apply` does nothing
// with them.
pub fn streak() -> [] int {
    return 12;
}

pub fn paused() -> [] int {
    return 13;
}

// A record about *why* an attempt failed, not about its outcome (`docs/design.md` section 34.3): written right after the `failed`, `dead` or
// `replay_failed`/`replay_dead` record of the attempt it explains, in the same flush. `endpoint` and `event` are the attempt's, `attempts` is its
// number, and `next_at` (the fifth field) holds the reason (`reason.ls`), plus `reason_replay()` if the attempt was a replay's. `apply` does
// nothing with it; recovery keeps the last reason of each endpoint.
pub fn reason() -> [] int {
    return 14;
}

// Added to the reason in a kind-14 record when the attempt was made for a replay.
pub fn reason_replay() -> [] int {
    return 256;
}

// The header of a log written since there were formats (`docs/retention.md` section 4; kind 14 is the reason of a failed attempt): its `event` is the format's number, `next_at` the Unix ms
// it was written, and its endpoint is `format_slot()`, the number 62 that the limit was when it was designed and that is written still, so that a log is byte for byte
// what it was. Since the limit passed 62 that is a slot, so nothing may take a record of this kind for one of its slot's (`scan_slots`, `replay`). The
// previous version took it for a record that is not an outcome and refused the log.
pub fn format() -> [] int {
    return 15;
}

pub fn format_slot() -> [] int {
    return 62;
}

// A waiting replay was cancelled by a person (`docs/design.md` section 39.2): `endpoint` and `event` are the replay's; the other fields are 0. Recovery ends
// the replay without an outcome, as `replay_dead` does, and the event stays what it was (a dead letter stays one). `apply` does nothing with it.
pub fn replay_cancelled() -> [] int {
    return 16;
}

// A dead letter, in a snapshot of the state (`docs/design.md` section 39.1): the snapshot has no `dead` record for an event that is final (it writes it as
// `delivered`, which is all the window needs), so what the table of dead letters holds is written in these. `endpoint` and `event` are the dead
// letter's; `attempts` is its attempts times 65536 plus the reason it died of (`reason.ls`), plus one; `next_at` is when it died (Unix ms). With
// `attempts` 0 the record is the table's *floor* instead: `event` is the largest event id that was left out of the table for room. `apply` does nothing
// with it.
pub fn dead_entry() -> [] int {
    return 17;
}

// A record that says the log uses a slot of `first_wide()` or above (`docs/design.md` section 41.5): written, flushed, before the first record about such a slot,
// and by a snapshot while one has an owner. Its fields are 0. A build that does not know it reads it as "not an outcome" and refuses the log (status 15),
// where it would otherwise have ignored the records of those slots without a word. `apply` does nothing with it.
pub fn wide() -> [] int {
    return 18;
}

// Where an endpoint's cursor was (`docs/design.md` section 42): `endpoint` is the slot, `event` a cursor (every event up to it is final there), the others 0.
// Written when the endpoint has passed over events that leave no record (an unwanted type), before a window outcome that a replay could not otherwise place,
// and at a clean stop. Replayed, it moves the cursor up to it (`advance`). A build that does not know it refuses the log (status 15).
pub fn advanced() -> [] int {
    return 19;
}

// An event was erased (`docs/design.md` section 47.3): `event` is its id, the other fields 0. From then on it is final at every endpoint and never sent, replayed
// or served; its body in the events log is replaced (`evlog.redact`). Replayed, it makes the event final in every window that holds it. A build that does not know
// it refuses the log (status 15).
pub fn erased() -> [] int {
    return 20;
}

// How long a day is, in ms. The breaker counts days of failure in these and not in calendar days: "five days" is 432,000,000 ms.
pub fn day_ms() -> [] int {
    return 86400000;
}

// The circuit breaker's rule: an endpoint whose every attempt has failed since `since` (Unix ms; 0 if none has failed, or one has been
// delivered since) is paused when `now` is `days` days or more past it. `days` 0 turns the breaker off. A clock that went backwards
// (`now` before `since`) never trips it.
pub fn breaker_trips(days: int, since: int, now: int) -> [] bool {
    if days <= 0 || since <= 0 {
        return false;
    }
    return now - since >= days * day_ms();
}

// The size of the cell array for `n` endpoints: three ints (final, attempts, next attempt) per cell.
pub fn cells(n: int) -> [] int {
    return n * span() * 3;
}

fn cell(e: int, id: int) -> [] int {
    return (e * span() + id % span()) * 3;
}

pub fn is_final[&w, &c](w: &w [int], cur: &c [int], e: int, id: int) -> [] bool {
    if id <= cur[e] {
        return true;
    }
    if id > cur[e] + span() {
        return false;
    }
    return w[cell(e, id)] == 1;
}

// Is `id` inside the window of endpoint `e`: above the cursor and no more than `span()` above it.
pub fn in_window[&c](cur: &c [int], e: int, id: int) -> [] bool {
    return id > cur[e] && id <= cur[e] + span();
}

// Attempts made so far on `id` for `e`, which must be in the window.
pub fn attempts[&w](w: &w [int], e: int, id: int) -> [] int {
    return w[cell(e, id) + 1];
}

// The time (Unix ms) before which `id` is not attempted again for `e`, which must be in the window; 0 is "now".
pub fn next_at[&w](w: &w [int], e: int, id: int) -> [] int {
    return w[cell(e, id) + 2];
}

// Apply one outcome. `delivered` and `dead` make `id` final; `failed` records `attempts` and `next_at`; any other kind (the
// endpoint records) changes nothing. Answers 0, or -1 if
// `id` is above the window (nothing is changed). An id at or below the cursor is already final and is ignored (answers 0).
pub fn apply[&w, &c](w: &!w [int], cur: &!c [int], e: int, kind: int, id: int, attempts: int, next_at: int) -> [] int {
    if kind != failed() && kind != delivered() && kind != dead() {
        return 0;
    }
    if id <= cur[e] {
        return 0;
    }
    if id > cur[e] + span() {
        return 0 - 1;
    }
    let at = cell(e, id);
    if kind == failed() {
        w[at + 1] = attempts;
        w[at + 2] = next_at;
        return 0;
    }
    w[at] = 1;
    w[at + 1] = attempts;
    w[at + 2] = next_at;
    // Move the cursor over every final cell at its front, freeing each for the id one window above it.
    var going = true;
    while going {
        let front = cell(e, cur[e] + 1);
        if w[front] == 1 {
            w[front] = 0;
            w[front + 1] = 0;
            w[front + 2] = 0;
            cur[e] = cur[e] + 1;
        } else {
            going = false;
        }
    }
    return 0;
}

// Move the cursor of `e` up to `to` (an `advanced` record): every event up to it is final, so the cells it passes are cleared; then over the final cells at
// its front, as a delivery does. A `to` at or below the cursor changes nothing. Only a cell that is not zero is written.
pub fn advance[&w, &c](w: &!w [int], cur: &!c [int], e: int, to: int) -> [] int {
    if to <= cur[e] {
        return 0;
    }
    var id = cur[e] + 1;
    var last = to;
    if last > cur[e] + span() {
        last = cur[e] + span();
    }
    while id <= last {
        let at = cell(e, id);
        if w[at] != 0 || w[at + 1] != 0 || w[at + 2] != 0 {
            w[at] = 0;
            w[at + 1] = 0;
            w[at + 2] = 0;
        }
        id = id + 1;
    }
    cur[e] = to;
    var going = true;
    while going {
        let front = cell(e, cur[e] + 1);
        if w[front] == 1 {
            w[front] = 0;
            w[front + 1] = 0;
            w[front + 2] = 0;
            cur[e] = cur[e] + 1;
        } else {
            going = false;
        }
    }
    return 0;
}

// Forget everything about slot `e`: its window is empty and its cursor is `start`. What a `created` or a `removed` record does.
// Only a cell that is not zero is written: the arrays are zero-filled when they are made, and a start gives a slot to every endpoint, so writing a zero over each
// cell would make resident the 24 KiB of every slot there is, used or not (`docs/design.md` section 41.2). Reading a page that was never written is free.
pub fn reset[&w, &c](w: &!w [int], cur: &!c [int], e: int, start: int) -> [] int {
    var i = 0;
    while i < span() * 3 {
        if w[e * span() * 3 + i] != 0 {
            w[e * span() * 3 + i] = 0;
        }
        i = i + 1;
    }
    cur[e] = start;
    return 0;
}

// An outcome as a log record at `at` in `out`, with sequence number `seq` as its id: one pair, `o`, whose value is five
// 8-byte integers: kind, endpoint, event, attempts, next attempt. Answers the record's size.
pub fn put_outcome[&o](out: &!o [byte], at: int, seq: int, kind: int, e: int, id: int, attempts: int, next_at: int) -> [] int {
    // The region is left by falling out of it, not by `return`: a region left by a `return` is not given back, and this runs once for every
    // delivery (about 3.5 KB lost each time, measured: docs/lexsys-log-retention.md gap 6).
    var size = 0;
    region a {
        let value = alloc_slice[a](40, byte_of(0));
        record.put_u64(value, 0, kind);
        record.put_u64(value, 8, e);
        record.put_u64(value, 16, id);
        record.put_u64(value, 24, attempts);
        record.put_u64(value, 32, next_at);
        let p = record.begin(out, at, seq, 0, 1);
        let end = record.put_pair(out, p, "o", value);
        size = record.seal(out, at, end);
    }
    return size;
}

// The outcome in the record at `at` in `buf`: `(kind, endpoint, event, attempts, next_at)`, or kind 0 if the record is not
// one this module wrote (the wrong shape): the caller refuses the log rather than guess.
pub fn outcome_at[&b](buf: &b [byte], at: int) -> [] (int, int, int, int, int) {
    if record.fields_of(buf, at) != 1 {
        return (0, 0, 0, 0, 0);
    }
    let p = record.pair_at(buf, record.first_pair(at));
    if p.1 != 1 || int_of(buf[p.0]) != 'o' || p.3 != 40 {
        return (0, 0, 0, 0, 0);
    }
    let kind = record.get_u64(buf, p.2);
    if kind < 1 || kind > 20 {
        return (0, 0, 0, 0, 0);
    }
    return (kind, record.get_u64(buf, p.2 + 8), record.get_u64(buf, p.2 + 16), record.get_u64(buf, p.2 + 24), record.get_u64(buf, p.2 + 32));
}

// How many attempts one turn may start: at most `most`, and no more than the `slots - held` connections that are free, never a negative
// number. A start beyond the free connections is answered "no connection" by `attempt.begin`, which was recorded as a failed attempt
// (`docs/design.md` section 28).
pub fn starts_allowed(held: int, slots: int, most: int) -> [] int {
    var free = slots - held;
    if free < 0 {
        free = 0;
    }
    if free < most {
        return free;
    }
    return most;
}
