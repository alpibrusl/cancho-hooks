edition 5;

module idem;

// `idem` -- the index of idempotency keys (`docs/design.md` sections 4 and 17; `docs/retention.md` section 7).
//
// A client may send `Idempotency-Key`; a second `POST /events` with the same key and the same event, within the window,
// must answer what the first did and store nothing. So the service must know, for each key it has seen, which event it
// made, when, and enough about the event to tell a repeat from a different event sent under the same key.
//
// **The log is the truth; this is an index of it.** The key travels in the event's own record (a second pair, `key`, and a
// third, `t`, the Unix time in ms), so a crash that loses an event loses its key with it, and a restart rebuilds this index
// by reading the log. Nothing here is written anywhere.
//
// **It forgets, and only what it may.** A key is *fresh* while its window has not passed and its event has not been dropped
// by retention; a fresh key is never forgotten, and a full index refuses a new key (the caller answers 507) rather than
// forget one, because a key it forgot would double-store the event it exists to protect. A key that is not fresh is
// evicted, a few at a time, from the front of a ring that holds the keys in the order their events were appended, so the
// oldest are the first to go and no request pays for a sweep. (An expired key that has not been evicted yet is just
// absent: a repost overwrites it.)
//
// One index is: a ring of `cap` entries (event id, Unix ms, CRC-32C of the body, its length, where the key is and how
// long, the key's hash), a ring of bytes for the keys, and an open-addressing hash table of `2 * cap + 1` slots with
// linear probing and **backward-shift deletion**, so removing a key leaves no tombstone and lookups do not slow down with
// churn. The state is one `int` array, `ix`:
//
//     [0]  keys held (live entries)        [1]  the window, in ms       [2]  cap, the ring's capacity in entries
//     [3]  head: the number of the oldest entry in the ring            [4]  tail: the number the next entry will have
//     [5]  where the oldest key's bytes start in the arena             [6]  where the next key's bytes go
//     [7]  floor: events with an id at or below it have been dropped   [8]  the table's slots   [9]  the arena's size
//     [12 .. 12 + slots)          the table: 0 empty, else entry slot + 1 (an entry's slot is its number modulo cap)
//     [12 + slots ..)             the entries, seven integers each:
//                                 event id (-1 once removed), Unix ms of the event, CRC-32C of its body, its length,
//                                 key start, key length, key hash
//
// and the keys' bytes are `arena`. The service keeps two of them in one array each, one for the keys of clients and one for
// cron's (`cron:<id>:<second>`), which live longer.

fn header() -> [] int {
    return 12;
}

pub fn max_key() -> [] int {
    return 255;
}

// Bytes of arena per key of capacity: the keys of clients are short, and a ring that runs out of bytes first refuses a new key as a full one does.
pub fn arena_per() -> [] int {
    return 48;
}

fn slots_for(cap: int) -> [] int {
    return 2 * cap + 1;
}

// The integers an index of `cap` entries takes, and the bytes of its arena.
pub fn ix_size(cap: int) -> [] int {
    return header() + slots_for(cap) + 7 * cap;
}

pub fn arena_size(cap: int) -> [] int {
    return cap * arena_per();
}

// Make `ix` (zeroed, `ix_size(cap)` integers) an empty index of `cap` entries over an arena of `arena_len` bytes, with this window.
pub fn init[&i](ix: &!i [int], cap: int, arena_len: int, window: int) -> [] int {
    ix[0] = 0;
    ix[1] = window;
    ix[2] = cap;
    ix[3] = 0;
    ix[4] = 0;
    ix[5] = 0;
    ix[6] = 0;
    ix[7] = 0;
    ix[8] = slots_for(cap);
    ix[9] = arena_len;
    return 0;
}

pub fn count[&i](ix: &i [int]) -> [] int {
    return ix[0];
}

pub fn window_ms[&i](ix: &i [int]) -> [] int {
    return ix[1];
}

pub fn capacity[&i](ix: &i [int]) -> [] int {
    return ix[2];
}

pub fn floor[&i](ix: &i [int]) -> [] int {
    return ix[7];
}

// Events with an id at or below `f` have been dropped: their keys are no longer fresh.
pub fn set_floor[&i](ix: &!i [int], f: int) -> [] int {
    if f > ix[7] {
        ix[7] = f;
    }
    return 0;
}

// Where the second index of an array that holds two begins, in the integers and in the arena: right after the first.
pub fn second_at[&i](ix: &i [int]) -> [] int {
    return ix_size(ix[2]);
}

pub fn second_arena_at[&i](ix: &i [int]) -> [] int {
    return arena_size(ix[2]);
}

fn base[&i](ix: &i [int]) -> [] int {
    return header() + ix[8];
}

// FNV-1a over the bytes, kept to 32 bits.
fn hash[&k](key: &k [byte]) -> [] int {
    var h = 2166136261;
    var i = 0;
    while i < len(key) {
        h = (h ^ int_of(key[i])) * 16777619 & 0xffffffff;
        i = i + 1;
    }
    return h;
}

fn same[&a, &k](arena: &a [byte], start: int, n: int, key: &k [byte]) -> [] bool {
    if n != len(key) {
        return false;
    }
    var i = 0;
    while i < n {
        if int_of(arena[start + i]) != int_of(key[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Is `key` a key this service accepts: 1 to 255 bytes of visible ASCII (no spaces, no control characters)?
pub fn valid[&k](key: &k [byte]) -> [] bool {
    if len(key) < 1 || len(key) > max_key() {
        return false;
    }
    var i = 0;
    while i < len(key) {
        let c = int_of(key[i]);
        if c < 33 || c > 126 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// The entry slot holding `key`, or -1.
pub fn find[&i, &a, &k](ix: &i [int], arena: &a [byte], key: &k [byte]) -> [] int {
    let slots = ix[8];
    let h = hash(key);
    var s = h % slots;
    var probes = 0;
    while probes < slots {
        let held = ix[header() + s];
        if held == 0 {
            return 0 - 1;
        }
        let b = base(ix) + 7 * (held - 1);
        if ix[b + 6] == h && same(arena, ix[b + 4], ix[b + 5], key) {
            return held - 1;
        }
        s = (s + 1) % slots;
        probes = probes + 1;
    }
    return 0 - 1;
}

// Where in the arena a key of `n` bytes would go, or -1 if there is no room: after the last key, or at the front if the ring of bytes has
// wrapped (a key is never split).
fn place[&i](ix: &i [int], n: int) -> [] int {
    let size = ix[9];
    if ix[3] == ix[4] {
        if n <= size {
            return 0;
        }
        return 0 - 1;
    }
    let ahead = ix[5];
    let atail = ix[6];
    if atail > ahead {
        if size - atail >= n {
            return atail;
        }
        if n < ahead {
            return 0;
        }
        return 0 - 1;
    }
    if atail + n < ahead {
        return atail;
    }
    return 0 - 1;
}

// Is there room for one more key of `n` bytes? (A full ring is not room; eviction is how room is made.)
pub fn room[&i](ix: &i [int], n: int) -> [] bool {
    if ix[4] - ix[3] >= ix[2] {
        return false;
    }
    return place(ix, n) >= 0;
}

// Add `key` (which must not be held): answers its entry slot, or -1 if there is no room (nothing is changed). The entry is filled in with `set`.
pub fn add[&i, &a, &k](ix: &!i [int], arena: &!a [byte], key: &k [byte]) -> [] int {
    if ix[4] - ix[3] >= ix[2] {
        return 0 - 1;
    }
    let start = place(ix, len(key));
    if start < 0 {
        return 0 - 1;
    }
    var j = 0;
    while j < len(key) {
        arena[start + j] = key[j];
        j = j + 1;
    }
    let p = ix[4] % ix[2];
    let b = base(ix) + 7 * p;
    let h = hash(key);
    ix[b] = 0;
    ix[b + 1] = 0;
    ix[b + 2] = 0;
    ix[b + 3] = 0;
    ix[b + 4] = start;
    ix[b + 5] = len(key);
    ix[b + 6] = h;
    var s = h % ix[8];
    while ix[header() + s] != 0 {
        s = (s + 1) % ix[8];
    }
    ix[header() + s] = p + 1;
    ix[6] = start + len(key);
    ix[4] = ix[4] + 1;
    ix[0] = ix[0] + 1;
    return p;
}

// Record what entry `e` stands for: the event id, its Unix time in ms, the CRC-32C and the length of its body.
pub fn set[&i](ix: &!i [int], e: int, id: int, ms: int, crc: int, length: int) -> [] int {
    let b = base(ix) + 7 * e;
    ix[b] = id;
    ix[b + 1] = ms;
    ix[b + 2] = crc;
    ix[b + 3] = length;
    return 0;
}

pub fn id_of[&i](ix: &i [int], e: int) -> [] int {
    return ix[base(ix) + 7 * e];
}

pub fn ms_of[&i](ix: &i [int], e: int) -> [] int {
    return ix[base(ix) + 7 * e + 1];
}

// Is entry `e` still fresh at Unix time `now`: inside the window, and its event not dropped?
pub fn fresh[&i](ix: &i [int], e: int, now: int) -> [] bool {
    let b = base(ix) + 7 * e;
    return now - ix[b + 1] <= ix[1] && ix[b] > ix[7];
}

// Is the event with this CRC-32C and length the one entry `e` stands for?
pub fn matches[&i](ix: &i [int], e: int, crc: int, length: int) -> [] bool {
    let b = base(ix) + 7 * e;
    return ix[b + 2] == crc && ix[b + 3] == length;
}

// Take entry `e` out of the table and mark it gone; its place in the ring is given back when the ring's front reaches it. The table is
// repaired by moving back each later occupant of the run that could not be found otherwise (no tombstone is left).
pub fn remove[&i](ix: &!i [int], e: int) -> [] int {
    let slots = ix[8];
    let b = base(ix) + 7 * e;
    if ix[b] < 0 {
        return 0;
    }
    var i = ix[b + 6] % slots;
    var probes = 0;
    while ix[header() + i] != e + 1 && probes < slots {
        i = (i + 1) % slots;
        probes = probes + 1;
    }
    if ix[header() + i] != e + 1 {
        return 0 - 1;
    }
    var j = i;
    var going = true;
    while going {
        j = (j + 1) % slots;
        let v = ix[header() + j];
        if v == 0 {
            going = false;
        } else {
            let home = ix[base(ix) + 7 * (v - 1) + 6] % slots;
            var inside = false;
            if i <= j {
                inside = i < home && home <= j;
            } else {
                inside = i < home || home <= j;
            }
            if !inside {
                ix[header() + i] = v;
                i = j;
            }
        }
    }
    ix[header() + i] = 0;
    ix[b] = 0 - 1;
    ix[0] = ix[0] - 1;
    return 0;
}

// Give back the room of entries at the front of the ring that are gone or no longer fresh at `now`, at most `budget` of them. Answers how many.
pub fn evict[&i](ix: &!i [int], now: int, budget: int) -> [] int {
    var n = 0;
    var going = true;
    while going && n < budget && ix[3] < ix[4] {
        let p = ix[3] % ix[2];
        let b = base(ix) + 7 * p;
        if ix[b] < 0 {
            ix[3] = ix[3] + 1;
            n = n + 1;
        } else if now - ix[b + 1] > ix[1] || ix[b] <= ix[7] {
            remove(ix, p);
            ix[3] = ix[3] + 1;
            n = n + 1;
        } else {
            going = false;
        }
        if ix[3] == ix[4] {
            ix[5] = 0;
            ix[6] = 0;
        } else {
            ix[5] = ix[base(ix) + 7 * (ix[3] % ix[2]) + 4];
        }
    }
    return n;
}
