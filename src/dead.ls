edition 5;

module dead;

import state;

// `dead` -- the dead letters of each endpoint, kept in memory so that they can be listed and replayed in bulk (`docs/design.md` section 39.1).
//
// A *dead letter* is an event whose last word at an endpoint is `dead`: the schedule ran out, or the receiver answered `410`, and no replay of it has
// delivered it since. The delivery log (`delivery.seg`) is the truth, and holds each one as a record of kind 3 (or 9, the replay's own), with the
// attempts it took, the Unix time it died (the record's fifth field) and, in a record of kind 14 right behind it, why. Finding them there means
// reading the whole log, once for every request; this keeps the ones that matter in a bounded table that the same replay of the log builds at start
// and the same code keeps while the service runs.
//
// **The bound.** Each endpoint holds the `cap()` dead letters with the **largest event ids** (the newest): 2,048. A table that is full when another
// event dies drops the entry with the smallest id (the oldest); an event that dies with an id below every entry of a full table is not entered. Either
// way the largest id that was left out is the table's **floor**: every dead letter with an event id above the floor is in the table, and there may be
// others at or below it (the list says so: `truncated`, `complete_above`). The floor is what lets the table be completed from the log when room appears
// (a delivered replay takes an entry out): `wants_refold` says that the floor is above 0 and the table has room, and `hooks.ls` then reads the delivery log
// once for the events below the floor (`reset_floor` first; the fold sets it again to whatever it left out in turn). Nothing is lost in the log by any of
// this. A plain fold of the log from its start, as recovery does, is not enough: it drops the old ones when the table is full of newer ones that a
// later record then removes, and has no way to bring them back. Why 2,048:
// an endpoint can have only one window of 1,024 events not final (`state.span()`), and a window's worth dies per schedule (about 76 hours), so the
// circuit breaker (5 days by default) pauses an endpoint after roughly one and a half windows. The whole table is `state.max_endpoints()` x (4 + 2,048 x 4)
// integers (1,024 x 64 KiB, 67 MB), touched only where an endpoint has dead letters (`clear` writes only what a block held: it is all zero past its count).
//
// An entry is four integers: the event id, `attempts * 65536 + reason` (`reason.ls`; 0: not recorded), when it died (Unix ms; 0: a record from before
// the time was written), and where the event starts in the events log plus one (0: not known yet; the first listing that wants the event's type
// finds it, `hooks.ls`). A table is sorted by event id, so a page of it is found by a binary search and is stable while the table changes: the
// cursor of the API is an event id.
//
// The block for slot `e` is `[count, floor, settled, 0]` (in the dense headers) and then `cap()` entries. `settled` is 1 once a fold of the log for the events below the floor
// found nothing to add: the log has no more to give (a snapshot of it keeps only what the table held), so asking again would read it for nothing. A
// dead letter left out later clears it.
//
// A dead letter **expires with its event** (`expire`, `docs/retention.md`): once retention has dropped an event from the events log it is not listed
// and cannot be replayed (`GET /events/:id` is 404 for it), so its entry goes with it.

pub fn cap() -> [] int {
    return 2048;
}

fn width() -> [] int {
    return 4;
}

pub fn block() -> [] int {
    return 4 + cap() * width();
}

pub fn size() -> [] int {
    return state.max_endpoints() * block();
}

// The headers of the blocks are dense, `[count, floor, settled, 0]` for slot `e` at `4 e`, in the first `4 x rows` integers of the array (`rows` is its length over `block()`,
// which is `state.max_endpoints()` for the real one), and the entries follow in `cap()` for each slot: so reading a slot's count or floor never touches the 64 KiB of its
// entries, and nothing of a slot's entries is resident until it has dead letters (`docs/design.md` section 41.4).
fn hb(e: int) -> [] int {
    return e * 4;
}

fn entry[&d](d: &d [int], e: int, k: int) -> [] int {
    return len(d) / block() * 4 + e * cap() * width() + k * width();
}

pub fn count[&d](d: &d [int], e: int) -> [] int {
    return d[hb(e)];
}

// The largest event id that was left out of slot `e`'s table for room (0: none was; the table is all of the dead letters).
pub fn floor[&d](d: &d [int], e: int) -> [] int {
    return d[hb(e) + 1];
}

// Is the table short of the dead letters it could hold: some were left out, and it has room now?
pub fn wants_refold[&d](d: &d [int], e: int) -> [] bool {
    return d[hb(e) + 1] > 0 && d[hb(e)] < cap() && d[hb(e) + 2] == 0;
}

// A fold for the events below the floor added nothing: do not ask again until something else is left out.
pub fn settle[&d](d: &!d [int], e: int) -> [] int {
    d[hb(e) + 2] = 1;
    return 0;
}

pub fn is_settled[&d](d: &d [int], e: int) -> [] bool {
    return d[hb(e) + 2] == 1;
}

// The largest event id left out of the table for room is at least `id`: what a snapshot says of the ones it did not keep.
pub fn raise_floor[&d](d: &!d [int], e: int, id: int) -> [] int {
    return left_out(d, e, id);
}

// The events with an id below `first` are gone from the events log (retention): their dead letters go too, and a floor below `first` is no floor. Answers
// how many entries went.
pub fn expire[&d](d: &!d [int], e: int, first: int) -> [] int {
    let gone = clear_below(d, e, first);
    if d[hb(e) + 1] < first {
        // (written only if it is not zero: this is called for every slot there is, whether it has dead letters or not)
        if d[hb(e) + 1] != 0 {
            d[hb(e) + 1] = 0;
        }
        if d[hb(e) + 2] != 0 {
            d[hb(e) + 2] = 0;
        }
    }
    return gone;
}

// Forget what was left out, before a fold of the log puts the entries below the old floor in and sets it again.
pub fn reset_floor[&d](d: &!d [int], e: int) -> [] int {
    d[hb(e) + 1] = 0;
    return 0;
}

fn left_out[&d](d: &!d [int], e: int, id: int) -> [] int {
    if id > d[hb(e) + 1] {
        d[hb(e) + 1] = id;
        d[hb(e) + 2] = 0;
    }
    return 0;
}

pub fn id_at[&d](d: &d [int], e: int, k: int) -> [] int {
    return d[entry(d, e, k)];
}

pub fn attempts_at[&d](d: &d [int], e: int, k: int) -> [] int {
    return d[entry(d, e, k) + 1] / 65536;
}

pub fn reason_at[&d](d: &d [int], e: int, k: int) -> [] int {
    return d[entry(d, e, k) + 1] % 65536;
}

pub fn died_at[&d](d: &d [int], e: int, k: int) -> [] int {
    return d[entry(d, e, k) + 2];
}

// Where the event starts in the events log, or -1 if that is not known yet.
pub fn offset_at[&d](d: &d [int], e: int, k: int) -> [] int {
    return d[entry(d, e, k) + 3] - 1;
}

pub fn set_offset[&d](d: &!d [int], e: int, k: int, offset: int) -> [] int {
    d[entry(d, e, k) + 3] = offset + 1;
    return 0;
}

// Forget slot `e`: no entries, nothing evicted. (A slot freed, or given to another endpoint.)
// A block is zero past its count (`put` fills the entry at the count, `remove` and `clear_below` zero the entries they vacate), so only the entries it holds are written:
// a start gives a slot to every endpoint, and a loop that stored zeros over 8,192 integers for each would make every block resident.
pub fn clear[&d](d: &!d [int], e: int) -> [] int {
    var k = 0;
    while k < d[hb(e)] * width() {
        d[entry(d, e, 0) + k] = 0;
        k = k + 1;
    }
    // (a header that is zero is not written either: a page that was never written is not resident)
    var h = 0;
    while h < 3 {
        if d[hb(e) + h] != 0 {
            d[hb(e) + h] = 0;
        }
        h = h + 1;
    }
    return 0;
}

// How many entries of slot `e` have an event id below `id`: the place `id` is, or would be, in the table.
pub fn below[&d](d: &d [int], e: int, id: int) -> [] int {
    var lo = 0;
    var hi = d[hb(e)];
    while lo < hi {
        let mid = (lo + hi) / 2;
        if d[entry(d, e, mid)] < id {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    return lo;
}

// How many have an event id of `id` or less.
pub fn upto[&d](d: &d [int], e: int, id: int) -> [] int {
    return below(d, e, id + 1);
}

// The index of the entry for event `id`, or -1.
pub fn find[&d](d: &d [int], e: int, id: int) -> [] int {
    let k = below(d, e, id);
    if k < d[hb(e)] && d[entry(d, e, k)] == id {
        return k;
    }
    return 0 - 1;
}

// What `put` answers.
pub fn put_added() -> [] int {
    return 0;
}

pub fn put_replaced() -> [] int {
    return 1;
}

// The table was full: the entry with the smallest id was dropped for this one.
pub fn put_pushed_out() -> [] int {
    return 2;
}

// The table was full and this event is older than every entry: it was not entered.
pub fn put_left_out() -> [] int {
    return 3;
}

// Event `id` is a dead letter of slot `e` after `attempts` attempts, the last failing for `why` (`reason.ls`), at `died` (Unix ms), and starts at
// `offset` in the events log (-1: not known). One that is there is overwritten (a replay of it died again); its offset is kept if `offset` is -1.
pub fn put[&d](d: &!d [int], e: int, id: int, attempts: int, why: int, died: int, offset: int) -> [] int {
    let k = below(d, e, id);
    let n = d[hb(e)];
    if k < n && d[entry(d, e, k)] == id {
        d[entry(d, e, k) + 1] = attempts * 65536 + why;
        d[entry(d, e, k) + 2] = died;
        if offset >= 0 {
            d[entry(d, e, k) + 3] = offset + 1;
        }
        return put_replaced();
    }
    var at = k;
    var verdict = put_added();
    var m = n;
    if n >= cap() {
        if k == 0 {
            left_out(d, e, id);
            return put_left_out();
        }
        // drop the smallest: the entries below the new one's place (1 to k - 1) move down one place, the ones above it stay where they are, and
        // the new entry takes the place at k - 1
        let dropped = d[entry(d, e, 0)];
        var j = 1;
        while j < k {
            var w = 0;
            while w < width() {
                d[entry(d, e, j - 1) + w] = d[entry(d, e, j) + w];
                w = w + 1;
            }
            j = j + 1;
        }
        left_out(d, e, dropped);
        at = k - 1;
        m = n - 1;
        verdict = put_pushed_out();
    } else {
        // make room: entries from k up move one place up
        var j = n;
        while j > k {
            var w = 0;
            while w < width() {
                d[entry(d, e, j) + w] = d[entry(d, e, j - 1) + w];
                w = w + 1;
            }
            j = j - 1;
        }
    }
    d[entry(d, e, at)] = id;
    d[entry(d, e, at) + 1] = attempts * 65536 + why;
    d[entry(d, e, at) + 2] = died;
    d[entry(d, e, at) + 3] = offset + 1;
    d[hb(e)] = m + 1;
    return verdict;
}

// Drop the entries of slot `e` with an event id below `id` (the table of an endpoint that is another's from here on: a fold of the log that goes back over a
// slot's earlier holders). `id` 0 drops nothing.
pub fn clear_below[&d](d: &!d [int], e: int, id: int) -> [] int {
    let cut = below(d, e, id);
    if cut == 0 {
        return 0;
    }
    let n = d[hb(e)];
    var j = cut;
    while j < n {
        var w = 0;
        while w < width() {
            d[entry(d, e, j - cut) + w] = d[entry(d, e, j) + w];
            w = w + 1;
        }
        j = j + 1;
    }
    j = n - cut;
    while j < n {
        var w = 0;
        while w < width() {
            d[entry(d, e, j) + w] = 0;
            w = w + 1;
        }
        j = j + 1;
    }
    d[hb(e)] = n - cut;
    return cut;
}

// Event `id` is not a dead letter of slot `e` any more (a replay of it was delivered). Answers 1 if it was in the table.
pub fn remove[&d](d: &!d [int], e: int, id: int) -> [] int {
    let k = find(d, e, id);
    if k < 0 {
        return 0;
    }
    let n = d[hb(e)];
    var j = k;
    while j < n - 1 {
        var w = 0;
        while w < width() {
            d[entry(d, e, j) + w] = d[entry(d, e, j + 1) + w];
            w = w + 1;
        }
        j = j + 1;
    }
    var w = 0;
    while w < width() {
        d[entry(d, e, n - 1) + w] = 0;
        w = w + 1;
    }
    d[hb(e)] = n - 1;
    return 1;
}

// The reason an attempt failed (the record of kind 14 behind the outcome) for event `id` of slot `e`, if the entry is for that attempt: `attempts`
// is the attempt's number. Answers 1 if it was set.
pub fn set_reason[&d](d: &!d [int], e: int, id: int, attempts: int, why: int) -> [] int {
    let k = find(d, e, id);
    if k < 0 || d[entry(d, e, k) + 1] / 65536 != attempts {
        return 0;
    }
    d[entry(d, e, k) + 1] = attempts * 65536 + why;
    return 1;
}
