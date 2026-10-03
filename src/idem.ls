edition 5;

module idem;

// `idem` -- the index of idempotency keys (`docs/design.md` sections 4 and 17).
//
// A client may send `Idempotency-Key`; a second `POST /events` with the same key and the same event, within the window,
// must answer what the first did and store nothing. So the service must know, for each key it has seen, which event it
// made, when, and enough about the event to tell a repeat from a different event sent under the same key.
//
// **The log is the truth; this is an index of it.** The key travels in the event's own record (a second pair, `key`, and a
// third, `t`, the Unix time in ms), so a crash that loses an event loses its key with it, and a restart rebuilds this index
// by reading the log. Nothing here is written anywhere.
//
// It is an open-addressing hash table over integers and one byte arena, with no deletion: an expired key is not removed, a
// later event with the same key overwrites its entry. It holds `capacity()` keys. A full index **refuses** a new key
// (the caller answers 507); it never forgets one, because a key it forgot would double-store the event it exists to protect.
//
// The state is one `int` array, `ix`:
//
//     [0]                    how many keys are held
//     [1]                    the window, in ms
//     [2 .. 2 + slots)       the table: 0 empty, else entry number + 1
//     [2 + slots ..)         the entries, six integers each:
//                            event id, Unix ms of the event, CRC-32C of its body, its length, key start, key length
//
// and the keys' bytes are `arena`, back to back.

pub fn capacity() -> [] int {
    return 65536;
}

fn slots() -> [] int {
    return 131072;
}

pub fn max_key() -> [] int {
    return 255;
}

pub fn ix_size() -> [] int {
    return 2 + 131072 + 6 * 65536;
}

pub fn arena_size() -> [] int {
    return 65536 * 255;
}

fn entry_base() -> [] int {
    return 2 + 131072;
}

pub fn count[&i](ix: &i [int]) -> [] int {
    return ix[0];
}

pub fn window_ms[&i](ix: &i [int]) -> [] int {
    return ix[1];
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

// The entry number holding `key`, or -1.
pub fn find[&i, &a, &k](ix: &i [int], arena: &a [byte], key: &k [byte]) -> [] int {
    var s = hash(key) % slots();
    var probes = 0;
    while probes < slots() {
        let held = ix[2 + s];
        if held == 0 {
            return 0 - 1;
        }
        let b = entry_base() + 6 * (held - 1);
        if same(arena, ix[b + 4], ix[b + 5], key) {
            return held - 1;
        }
        s = (s + 1) % slots();
        probes = probes + 1;
    }
    return 0 - 1;
}

// Add `key`: answers its entry number, or -1 if the index is full (nothing is changed). The entry is filled in with `set`.
// `key` must not be held already.
pub fn add[&i, &a, &k](ix: &!i [int], arena: &!a [byte], key: &k [byte]) -> [] int {
    let n = ix[0];
    if n >= capacity() {
        return 0 - 1;
    }
    var start = 0;
    if n > 0 {
        let last = entry_base() + 6 * (n - 1);
        start = ix[last + 4] + ix[last + 5];
    }
    var j = 0;
    while j < len(key) {
        arena[start + j] = key[j];
        j = j + 1;
    }
    let b = entry_base() + 6 * n;
    ix[b + 4] = start;
    ix[b + 5] = len(key);
    var s = hash(key) % slots();
    while ix[2 + s] != 0 {
        s = (s + 1) % slots();
    }
    ix[2 + s] = n + 1;
    ix[0] = n + 1;
    return n;
}

// Record what entry `e` stands for: the event id, its Unix time in ms, the CRC-32C and the length of its body.
pub fn set[&i](ix: &!i [int], e: int, id: int, ms: int, crc: int, length: int) -> [] int {
    let b = entry_base() + 6 * e;
    ix[b] = id;
    ix[b + 1] = ms;
    ix[b + 2] = crc;
    ix[b + 3] = length;
    return 0;
}

pub fn id_of[&i](ix: &i [int], e: int) -> [] int {
    return ix[entry_base() + 6 * e];
}

pub fn ms_of[&i](ix: &i [int], e: int) -> [] int {
    return ix[entry_base() + 6 * e + 1];
}

// Is entry `e` still inside the window at Unix time `now`?
pub fn fresh[&i](ix: &i [int], e: int, now: int) -> [] bool {
    return now - ix[entry_base() + 6 * e + 1] <= ix[1];
}

// Is the event with this CRC-32C and length the one entry `e` stands for?
pub fn matches[&i](ix: &i [int], e: int, crc: int, length: int) -> [] bool {
    let b = entry_base() + 6 * e;
    return ix[b + 2] == crc && ix[b + 3] == length;
}
