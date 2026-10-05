edition 5;

module evlog;

import record;
import log;
import std.bytes;
import store;
import logguard;

// `evlog` -- the events log as a chain of segments (`docs/retention.md` sections 4, 5 and 8).
//
// The log is `events.seg`, `events-1.seg`, ... The last is the **active** segment, appended to; the others are sealed. `events.first`
// names the oldest one that is still retained. An offset anywhere in this service is a **logical offset**: the number of event-record
// bytes since the beginning of the log, segment headers not counted, so it does not change when the front of the log is dropped.
//
// No file is held open. Appending copies a record into a staging buffer; `flush` writes what is staged and `fsync`s it; a read finds
// the segment from a small table and reads 256 KiB into a cache. (A resource cannot be replaced through a mutable reference, and a
// segment has to be able to appear and disappear: `docs/retention.md` section 13.)
//
// The table of segments, `segs`, has six integers a row, oldest first, the last row being the active segment:
//
//     k          the segment's number (0 is `events.seg`)
//     base       the logical offset of its first event
//     hdr        the size of its header record in bytes (0 for a format 1 segment, which has none)
//     first_id   the id of its first event
//     created    when it was created (Unix ms): no event in the segment before it is older than the segment before it was sealed
//     end        the logical offset where its events end (sealed segments only)

pub fn max_len() -> [] int {
    return 65536;
}

fn seg_max() -> [] int {
    return 4096;
}

fn stage_size() -> [] int {
    return 4194304;
}

fn cache_size() -> [] int {
    return 262144;
}

fn idx_cap() -> [] int {
    return 65536;
}

// One offset is kept for every `idx_every()` events, so finding an event is a walk of at most that many records.
fn idx_every() -> [] int {
    return 1024;
}

// The format this version writes. A header with any other number is refused.
pub fn version() -> [] int {
    return 2;
}

// What `append` answers besides 0; the first three are `lexsys-log`'s codes, so a caller comparing with `log.too_long()` is right.
pub fn id_not_after() -> [] int {
    return 1;
}

pub fn too_long() -> [] int {
    return 2;
}

pub fn is_broken() -> [] int {
    return 3;
}

pub fn full() -> [] int {
    return 4;
}

// The counters in `sc`.
fn s_rolls() -> [] int {
    return 0;
}

fn s_dropped_segments() -> [] int {
    return 1;
}

fn s_dropped_events() -> [] int {
    return 2;
}

fn s_orphans() -> [] int {
    return 3;
}

fn s_clamped() -> [] int {
    return 4;
}

pub res struct Ev {
    fs: Fs(""),
    dir: Box[[byte]],
    dlen: int,
    segs: Box[[int]],
    nseg: int,
    stage: Box[[byte]],
    slen: int,
    cache: Box[[byte]],
    cfrom: int,
    clen: int,
    idx: Box[[int]],
    sc: Box[[int]],
    first: int,
    tail: int,
    synced: int,
    last_id: int,
    limit: int,
    broken: bool,
    kill_at: int,
}

// An `Ev` over the data directory `dir`, holding the capability for the whole file system. Nothing is read or written until `open`.
// `limit` is the size at which the active segment is sealed; `kill_at` is the test knob (0 for none): the step number at which `step` stops.
pub fn new[&h, &d](heap: &!h Heap, fs: Fs(""), dir: &d [byte], limit: int, kill_at: int) -> [heap] Ev {
    let dirb = box_slice(heap, 2048, byte_of(0));
    var i = 0;
    borrow mut dirb as &!db in {
        while i < len(dir) {
            contents(db)[i] = dir[i];
            i = i + 1;
        }
    }
    return Ev { fs: fs, dir: dirb, dlen: len(dir), segs: box_slice(heap, 6 * seg_max(), 0), nseg: 0, stage: box_slice(heap, stage_size(), byte_of(0)), slen: 0, cache: box_slice(heap, cache_size(), byte_of(0)), cfrom: 0, clen: 0, idx: box_slice(heap, 2 * idx_cap(), 0), sc: box_slice(heap, 16, 0), first: 0, tail: 0, synced: 0, last_id: 0, limit: limit, broken: false, kill_at: kill_at };
}

// End the log; answers the capability it was holding.
pub fn close[&h](heap: &!h Heap, ev: Ev) -> [heap] Fs("") {
    let Ev { fs, dir, dlen, segs, nseg, stage, slen, cache, cfrom, clen, idx, sc, first, tail, synced, last_id, limit, broken, kill_at } = ev;
    unbox_slice(heap, dir);
    unbox_slice(heap, segs);
    unbox_slice(heap, stage);
    unbox_slice(heap, cache);
    unbox_slice(heap, idx);
    unbox_slice(heap, sc);
    return fs;
}

// The capability the log holds, lent: the rest of the program has no other.
pub fn lend[&e](ev: &e Ev) -> [] &e Fs("") {
    return ev.fs;
}

// ---------------------------------------------------------------------
// The header record
// ---------------------------------------------------------------------

// The header of segment `k` into `out` at 0: a record with id (0, 0) and three pairs, `format`, `segment` (k, base, first_id, created: four
// 8-byte integers) and `created`. Three pairs because of their shape: a reader of the previous version takes a record of three pairs for a keyed
// event and refuses this one. Answers its size.
pub fn put_header[&o](out: &!o [byte], k: int, base: int, first_id: int, created: int) -> [] int {
    var size = 0;
    region a {
        let seg = alloc_slice[a](32, byte_of(0));
        record.put_u64(seg, 0, k);
        record.put_u64(seg, 8, base);
        record.put_u64(seg, 16, first_id);
        record.put_u64(seg, 24, created);
        let stamp = alloc_slice[a](8, byte_of(0));
        record.put_u64(stamp, 0, created);
        let p = record.begin(out, 0, 0, 0, 3);
        var end = record.put_pair(out, p, "format", "lexsys-hooks events 2");
        end = record.put_pair(out, end, "segment", seg);
        end = record.put_pair(out, end, "created", stamp);
        size = record.seal(out, 0, end);
    }
    return size;
}

// The start of a segment file, `n` bytes of it in `buf`: `(status, header size, k, base, first_id, created, version)`.
//   status 0   a header: the rest is what it says
//   status 1   no header, a first event: a format 1 segment (only `events.seg` may be one)
//   status 2   nothing whole yet: an empty file, or a record cut off (a roll that did not finish, or an empty log)
//   status 3   not a whole valid record: damaged, or cut and filled with zeros (a header that was cut by a power cut is this, and is small)
//   status 5   a whole valid record that is not a header of this program's: something else was put there
//   status 4   a header of a format this version does not know (the version is the last field)
pub fn parse_head[&b](buf: &b [byte], n: int) -> [] (int, int, int, int, int, int, int) {
    let r = record.check(buf, 0, n, max_len());
    if r.0 == record.incomplete() {
        return (2, 0, 0, 0, 0, 0, 0);
    }
    if r.0 != record.ok() {
        return (3, 0, 0, 0, 0, 0, 0);
    }
    if record.ms_of(buf, 0) >= 1 {
        return (1, 0, 0, 0, 1, 0, 1);
    }
    if record.ms_of(buf, 0) != 0 || record.seq_of(buf, 0) != 0 || record.fields_of(buf, 0) != 3 {
        return (5, 0, 0, 0, 0, 0, 0);
    }
    let f = record.pair_at(buf, record.first_pair(0));
    if !bytes.equal(buf[f.0..f.0 + f.1], "format") {
        return (5, 0, 0, 0, 0, 0, 0);
    }
    let text = buf[f.2..f.2 + f.3];
    let prefix = "lexsys-hooks events ";
    if !bytes.starts_with(text, prefix) || len(text) == len(prefix) {
        return (5, 0, 0, 0, 0, 0, 0);
    }
    var ver = 0;
    var i = len(prefix);
    while i < len(text) {
        let c = int_of(text[i]);
        if c < 48 || c > 57 || ver > 1000000 {
            return (5, 0, 0, 0, 0, 0, 0);
        }
        ver = ver * 10 + (c - 48);
        i = i + 1;
    }
    if ver != version() {
        return (4, 0, 0, 0, 0, 0, ver);
    }
    let s = record.pair_at(buf, f.4);
    if !bytes.equal(buf[s.0..s.0 + s.1], "segment") || s.3 != 32 {
        return (5, 0, 0, 0, 0, 0, 0);
    }
    return (0, r.1, record.get_u64(buf, s.2), record.get_u64(buf, s.2 + 8), record.get_u64(buf, s.2 + 16), record.get_u64(buf, s.2 + 24), ver);
}

// ---------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------

fn seg_k[&e](ev: &e Ev, j: int) -> [] int {
    return contents(ev.segs)[6 * j];
}

fn seg_base[&e](ev: &e Ev, j: int) -> [] int {
    return contents(ev.segs)[6 * j + 1];
}

fn seg_hdr[&e](ev: &e Ev, j: int) -> [] int {
    return contents(ev.segs)[6 * j + 2];
}

fn seg_first_id[&e](ev: &e Ev, j: int) -> [] int {
    return contents(ev.segs)[6 * j + 3];
}

fn seg_created[&e](ev: &e Ev, j: int) -> [] int {
    return contents(ev.segs)[6 * j + 4];
}

fn put_row[&e](ev: &!e Ev, j: int, k: int, base: int, hdr: int, first_id: int, created: int, end: int) -> [] int {
    let t = contents(ev.segs);
    t[6 * j] = k;
    t[6 * j + 1] = base;
    t[6 * j + 2] = hdr;
    t[6 * j + 3] = first_id;
    t[6 * j + 4] = created;
    t[6 * j + 5] = end;
    return 0;
}

// The row of the segment that holds the logical offset `at`: the last whose base is at or below it.
fn row_of[&e](ev: &e Ev, at: int) -> [] int {
    var lo = 0;
    var hi = ev.nseg - 1;
    while lo < hi {
        let mid = (lo + hi + 1) / 2;
        if seg_base(ev, mid) <= at {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    return lo;
}

// The path of segment `k` into `path`; answers its length.
fn path_for[&e, &p](ev: &e Ev, path: &!p [byte], k: int) -> [] int {
    return store.seg_path(path, contents(ev.dir)[0..ev.dlen], k);
}

fn manifest_path[&e, &p](ev: &e Ev, path: &!p [byte], name: &static [byte]) -> [] int {
    return store.path_join(path, contents(ev.dir)[0..ev.dlen], name);
}

// ---------------------------------------------------------------------
// Opening
// ---------------------------------------------------------------------

// A segment file this small cannot hold an event after its header (the header is 133 bytes and the smallest event record 53), so what is in it, if it is
// not a whole header, is a roll or an install that was cut.
fn cut_header_bytes() -> [] int {
    return 160;
}

// The status `open` answers besides 0: the files could not be opened or recovered (10, as before), a header of a format this version does not
// know (40), a hole or a break in the chain of segments (42).
pub fn bad_format() -> [] int {
    return 40;
}

pub fn bad_chain() -> [] int {
    return 42;
}

// A data directory with no events log: `events.seg` with its header.
fn fresh[&e](ev: &!e Ev, now_ms: int) -> [fs_read(""), fs_write(""), file_write] int {
    var rc = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let hb = alloc_slice[a](256, byte_of(0));
        let pn = path_for(ev, path, 0);
        let hn = put_header(hb, 0, 0, 1, now_ms);
        if store.write_file(ev.fs, path[0..pn], hb[0..hn], true) != 0 {
            rc = 10;
        } else if store.sync_path(ev.fs, contents(ev.dir)[0..ev.dlen]) != 0 {
            rc = 10;
        } else {
            put_row(ev, 0, 0, 0, hn, 1, now_ms, 0);
            ev.nseg = 1;
            ev.first = 0;
            ev.tail = 0;
            ev.synced = 0;
            ev.last_id = 0;
        }
    }
    return rc;
}

// The digits at the front of `text` as a number, or -1.
fn number_of[&t](text: &t [byte]) -> [] int {
    var n = 0;
    var i = 0;
    while i < len(text) && int_of(text[i]) >= 48 && int_of(text[i]) <= 57 && n < 1000000000 {
        n = n * 10 + (int_of(text[i]) - 48);
        i = i + 1;
    }
    if i == 0 {
        return 0 - 1;
    }
    return n;
}

// Read the data directory: find the segments, check their headers and their chain, recover the active one (cutting a torn tail, as
// `lexsys-log` does), and settle where the next event goes. Answers 0, or 10, 30 or 32 (above). Nothing is written but what repair needs: a segment
// that a roll left without a header is deleted, and so are the files below the oldest retained one.
pub fn open[&e, &r](ev: &!e Ev, now_ms: int, repair: bool, rep: &!r [int]) -> [fs_read(""), fs_write(""), file_read, file_write] int {
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let small = alloc_slice[a](32, byte_of(0));
        // The manifest: the number of the oldest retained segment.
        var k0 = 0;
        var manifest = false;
        let mn = manifest_path(ev, path, "events.first");
        let got = store.read_range(ev.fs, path[0..mn], 0, small);
        if got > 0 {
            k0 = number_of(small[0..got]);
            manifest = true;
            if k0 < 0 {
                return 42;
            }
        } else if got < 0 && got != 0 - 2 {
            return 10;
        }
        let tn = manifest_path(ev, path, "events.first.tmp");
        store.remove(ev.fs, path[0..tn]);
        let first_size = seg_size(ev, path, k0);
        if first_size < 0 {
            if first_size == 0 - 2 && !manifest && k0 == 0 {
                return fresh(ev, now_ms);
            }
            if first_size != 0 - 2 {
                // there, and not to be opened (permissions, a directory, a failing disk): the old refusal, not a claim about the chain
                return 10;
            }
            return 42;
        }
        // The last segment is the highest number that exists, running up from the first; a gap further on is a hole.
        var kl = k0;
        var more = true;
        while more && kl + 1 < k0 + seg_max() {
            if seg_size(ev, path, kl + 1) >= 0 {
                kl = kl + 1;
            } else {
                more = false;
            }
        }
        var g = 2;
        while g <= 9 {
            if seg_size(ev, path, kl + g) >= 0 {
                return 42;
            }
            g = g + 1;
        }
        // What is below the oldest is a drop that did not finish: delete it.
        g = 1;
        while g <= 8 {
            if k0 - g >= 0 {
                let pn = path_for(ev, path, k0 - g);
                if store.remove(ev.fs, path[0..pn]) == 0 {
                    contents(ev.sc)[s_orphans()] = contents(ev.sc)[s_orphans()] + 1;
                }
            }
            g = g + 1;
        }
        // The headers.
        let head = contents(ev.cache);
        var j = 0;
        var k = k0;
        var broken_last = false;
        while k <= kl && !broken_last {
            let pn = path_for(ev, path, k);
            var want = seg_size(ev, path, k);
            if want > len(head) {
                want = len(head);
            }
            var n = 0;
            if want > 0 {
                n = store.read_range(ev.fs, path[0..pn], 0, head[0..want]);
                if n < 0 {
                    return 10;
                }
            }
            var h = parse_head(head, n);
            if h.0 == 4 {
                return 40;
            }
            // A header that is not whole, or is zeros where a block never reached the disk, in a file too small to hold an event after it: a roll (or an
            // install) that was cut, whatever the cut left. (A header that is damaged in a file that holds events is not that, and is a refusal.)
            if h.0 == 3 && n <= cut_header_bytes() && k == kl {
                h = (2, 0, 0, 0, 0, 0, 0);
            }
            if h.0 == 0 {
                if h.2 != k {
                    return 42;
                }
                put_row(ev, j, k, h.3, h.1, h.4, h.5, 0);
            } else if h.0 == 1 || h.0 == 2 && k == 0 {
                // A segment from before there were headers: only the first can be one.
                if k != 0 {
                    return 42;
                }
                put_row(ev, j, 0, 0, 0, 1, 0, 0);
            } else if h.0 == 2 && k == kl && k > k0 {
                // A roll that did not get as far as a durable header: it holds no event.
                broken_last = true;
            } else {
                return 42;
            }
            if !broken_last {
                j = j + 1;
            }
            k = k + 1;
        }
        if broken_last {
            let pn = path_for(ev, path, kl);
            store.remove(ev.fs, path[0..pn]);
            kl = kl - 1;
        }
        ev.nseg = j;
        // The chain: each segment begins where the one before it ends.
        var r = 0;
        while r + 1 < ev.nseg {
            let pn = path_for(ev, path, seg_k(ev, r));
            let size = seg_size(ev, path, seg_k(ev, r));
            if size < 0 {
                return 10;
            }
            let end = seg_base(ev, r) + size - seg_hdr(ev, r);
            if end != seg_base(ev, r + 1) || seg_first_id(ev, r + 1) <= seg_first_id(ev, r) {
                return 42;
            }
            contents(ev.segs)[6 * r + 5] = end;
            r = r + 1;
        }
        // The active segment is recovered as `lexsys-log` does it.
        let act = ev.nseg - 1;
        let pn = path_for(ev, path, seg_k(ev, act));
        var rec = (0, 0, 0, 0 - 1, 0 - 1);
        match open_rw(ev.fs, path[0..pn]) {
            Opened::Failed(e) => {
                return 10;
            }
            Opened::Ok(rw0) => {
                var rw = rw0;
                borrow mut rw as &!x in {
                    if broken_last {
                        // The segment that was judged before the start (`logguard.preflight`) was a roll that did not finish and is gone: this one is sealed, and whole.
                        rec = log.recover(x, contents(ev.cache), max_len());
                    } else {
                        // The cut that was judged before anything was changed (`docs/design.md` section 34.5), applied: a torn tail goes, damage in the middle is refused.
                        let nm = alloc_slice[a](64, byte_of(0));
                        rec = logguard.recover_known(x, rep, repair, ev.fs, contents(ev.dir)[0..ev.dlen], nm[0..store.seg_name(nm, seg_k(ev, act))], contents(ev.cache));
                    }
                }
                file_close(rw);
            }
        }
        if rec.0 >= logguard.refused() {
            return rec.0;
        }
        if rec.0 != 0 {
            return 10;
        }
        if rec.1 < seg_hdr(ev, act) {
            return 42;
        }
        if rec.1 == 0 && act == 0 {
            // An empty `events.seg` (an install that stopped before its header was durable): start it properly.
            store.remove(ev.fs, path[0..pn]);
            ev.nseg = 0;
            return fresh(ev, now_ms);
        }
        ev.tail = seg_base(ev, act) + rec.1 - seg_hdr(ev, act);
        ev.synced = ev.tail;
        ev.first = seg_base(ev, 0);
        ev.last_id = seg_first_id(ev, act) - 1;
        if rec.3 >= seg_first_id(ev, act) {
            ev.last_id = rec.3;
        }
        ev.slen = 0;
        ev.clen = 0;
        return 0;
    }
}

// The size of segment `k`'s file, or `0 - errno`.
fn seg_size[&e, &p](ev: &e Ev, path: &!p [byte], k: int) -> [fs_read(""), file_read] int {
    let n = path_for(ev, path, k);
    return store.size_of(ev.fs, path[0..n]);
}

// ---------------------------------------------------------------------
// Appending
// ---------------------------------------------------------------------

// Stage the sealed record `rec` of event `ms`. No input or output happens: it is durable when `flush` has answered 0. Answers 0, or
// `id_not_after`, `too_long`, `is_broken`, or `full` (the turn has staged as much as it can).
pub fn append[&e, &r](ev: &!e Ev, rec: &r [byte], ms: int) -> [] int {
    if ev.broken {
        return 3;
    }
    if len(rec) - 4 > max_len() {
        return 2;
    }
    if ms <= ev.last_id {
        return 1;
    }
    if ev.slen + len(rec) > stage_size() {
        return 4;
    }
    let st = contents(ev.stage);
    var i = 0;
    while i < len(rec) {
        st[ev.slen + i] = rec[i];
        i = i + 1;
    }
    if (ms - 1) % idx_every() == 0 {
        let slot = (ms - 1) / idx_every() % idx_cap();
        let ix = contents(ev.idx);
        ix[2 * slot] = (ms - 1) / idx_every() + 1;
        ix[2 * slot + 1] = ev.tail;
    }
    ev.slen = ev.slen + len(rec);
    ev.tail = ev.tail + len(rec);
    ev.last_id = ms;
    return 0;
}

// Write what is staged to the active segment and `fsync` it. Answers 0, or an errno, after which the log is broken and refuses everything
// (the file's contents are unknown; a restart recovers it). Does nothing when nothing is staged.
pub fn flush[&e](ev: &!e Ev) -> [fs_write(""), file_write] int {
    if ev.broken {
        return 3;
    }
    if ev.slen == 0 {
        return 0;
    }
    var rc = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let pn = path_for(ev, path, seg_k(ev, ev.nseg - 1));
        rc = store.append_bytes(ev.fs, path[0..pn], contents(ev.stage)[0..ev.slen], true);
    }
    if rc != 0 {
        ev.broken = true;
        return rc;
    }
    ev.synced = ev.tail;
    ev.slen = 0;
    return 0;
}

// ---------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------

// Load the cache with the bytes from the logical offset `at` on (up to what the segment holds, and in the active one what a flush
// covered). Answers 0, or an errno.
fn refill[&e](ev: &!e Ev, at: int) -> [fs_read(""), file_read] int {
    let j = row_of(ev, at);
    var upto = ev.synced;
    if j + 1 < ev.nseg {
        upto = contents(ev.segs)[6 * j + 5];
    }
    var want = upto - at;
    if want > cache_size() {
        want = cache_size();
    }
    if want <= 0 {
        return 5;
    }
    var got = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let pn = path_for(ev, path, seg_k(ev, j));
        let phys = seg_hdr(ev, j) + at - seg_base(ev, j);
        got = store.read_range(ev.fs, path[0..pn], phys, contents(ev.cache)[0..want]);
    }
    if got <= 0 {
        ev.clen = 0;
        return 5;
    }
    ev.cfrom = at;
    ev.clen = got;
    return 0;
}

// Make the record at the logical offset `at` readable in the cache. Answers `(status, position in the cache, size)`: status 0, the record is
// whole and valid; 1, `at` is at or past what a flush covered; 2, there is no whole record there (before the retained part, or damaged).
pub fn peek[&e](ev: &!e Ev, at: int) -> [fs_read(""), file_read] (int, int, int) {
    if at >= ev.synced {
        return (1, 0, 0);
    }
    if at < ev.first {
        return (2, 0, 0);
    }
    var attempt = 0;
    while attempt < 2 {
        if ev.clen > 0 && at >= ev.cfrom && at < ev.cfrom + ev.clen {
            let pos = at - ev.cfrom;
            let r = record.check(contents(ev.cache), pos, ev.clen, max_len());
            if r.0 == record.ok() {
                return (0, pos, r.1);
            }
            if r.0 == record.bad() {
                return (2, 0, 0);
            }
        }
        if refill(ev, at) != 0 {
            return (2, 0, 0);
        }
        attempt = attempt + 1;
    }
    return (2, 0, 0);
}

// The body an erased event's record holds in place of its own (`docs/design.md` section 47.3), padded with spaces to the length the body had, so that every offset
// stays where it was; `{}` and spaces for a body shorter than it (an event always has a "type", so a body that is `{}` and spaces is no event's).
pub fn erased_marker() -> [] &static [byte] {
    return "{\"erased\":true}";
}

// Is `value` (the `event` pair of a record) an erased body?
pub fn is_erased[&v](value: &v [byte]) -> [] bool {
    let m = erased_marker();
    var head = 0;
    if len(value) >= len(m) && bytes_equal(value[0..len(m)], m) {
        head = len(m);
    } else if len(value) >= 2 && int_of(value[0]) == '{' && int_of(value[1]) == '}' {
        head = 2;
    } else {
        return false;
    }
    var i = head;
    while i < len(value) {
        if int_of(value[i]) != ' ' {
            return false;
        }
        i = i + 1;
    }
    return head == len(m) || len(value) > 2;
}

fn bytes_equal[&a, &b](x: &a [byte], y: &b [byte]) -> [] bool {
    if len(x) != len(y) {
        return false;
    }
    var i = 0;
    while i < len(x) {
        if int_of(x[i]) != int_of(y[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Where the `event` pair of the whole record at `at` in `buf` has its value: `(start, length)`, or `(-1, 0)` if it has none.
pub fn event_value[&b](buf: &b [byte], at: int) -> [] (int, int) {
    var p = record.first_pair(at);
    var f = 0;
    while f < record.fields_of(buf, at) {
        let pr = record.pair_at(buf, p);
        if pr.1 == 5 && bytes_equal(buf[pr.0..pr.0 + 5], "event") {
            return (pr.2, pr.3);
        }
        p = pr.4;
        f = f + 1;
    }
    return (0 - 1, 0);
}

// Erase the body of the event whose record is at the logical offset `at` (`docs/design.md` section 47.3). Its segment, which must be sealed, is read whole,
// the record's `event` value replaced by `erased_marker()` and spaces to the same length and the record sealed again; the copy is written to
// `<segment>.tmp` and synced, renamed over the segment, and the directory synced (steps 34 to 36 of `compact-kill-at`). The cache is forgotten. Answers 0; 1 if
// the record is in the segment being written (seal it first); 2 if there is no whole record there; 3 if the copy could not be made or put in place (the
// segment is then as it was, or wholly the new one); 4 if it was erased already.
pub fn redact[&h, &e](heap: &!h Heap, ev: &!e Ev, at: int) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] int {
    if at < ev.first || at >= ev.synced {
        return 2;
    }
    let j = row_of(ev, at);
    if j == ev.nseg - 1 {
        return 1;
    }
    var rc = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let tmp = alloc_slice[a](2112, byte_of(0));
        let pn = path_for(ev, path, seg_k(ev, j));
        var tn = 0;
        while tn < pn {
            tmp[tn] = path[tn];
            tn = tn + 1;
        }
        let ext = ".tmp";
        var x = 0;
        while x < len(ext) {
            tmp[tn + x] = ext[x];
            x = x + 1;
        }
        tn = tn + len(ext);
        let size = store.size_of(ev.fs, path[0..pn]);
        if size <= 0 {
            rc = 3;
        } else {
            let whole = box_slice(heap, size, byte_of(0));
            borrow mut whole as &!w in {
                let buf = contents(w);
                let got = store.read_range(ev.fs, path[0..pn], 0, buf);
                let pos = seg_hdr(ev, j) + at - seg_base(ev, j);
                if got != size {
                    rc = 3;
                } else {
                    let r = record.check(buf, pos, size, max_len());
                    if r.0 != record.ok() {
                        rc = 2;
                    } else {
                        let v = event_value(buf, pos);
                        if v.0 < 0 {
                            rc = 2;
                        } else if is_erased(buf[v.0..v.0 + v.1]) {
                            rc = 4;
                        } else {
                            let m = erased_marker();
                            var i = 0;
                            while i < v.1 {
                                buf[v.0 + i] = byte_of(' ');
                                i = i + 1;
                            }
                            if v.1 >= len(m) {
                                i = 0;
                                while i < len(m) {
                                    buf[v.0 + i] = m[i];
                                    i = i + 1;
                                }
                            } else {
                                buf[v.0] = byte_of('{');
                                buf[v.0 + 1] = byte_of('}');
                            }
                            record.seal(buf, pos, pos + r.1);
                            if store.write_file(ev.fs, tmp[0..tn], buf, true) != 0 {
                                store.remove(ev.fs, tmp[0..tn]);
                                rc = 3;
                            }
                        }
                    }
                }
            }
            unbox_slice(heap, whole);
            if rc == 0 {
                step(ev, 34);
                if store.rename(ev.fs, tmp[0..tn], path[0..pn]) != 0 {
                    store.remove(ev.fs, tmp[0..tn]);
                    rc = 3;
                } else {
                    step(ev, 35);
                    store.sync_path(ev.fs, contents(ev.dir)[0..ev.dlen]);
                    step(ev, 36);
                }
            }
        }
    }
    ev.clen = 0;
    return rc;
}

// The cache `peek` reads into, lent. A position `peek` answered is good until the next `peek`.
pub fn cache[&e](ev: &e Ev) -> [] &e [byte] {
    return contents(ev.cache);
}

// The record at `at`, copied to the front of `buf` (`lexsys-log`'s `read_at`, over the whole chain). Answers `(status, size)`: 0 and the record's
// size; 1 at the end of what a flush covered; 2 if there is no whole record there.
pub fn read_at[&e, &b](ev: &!e Ev, at: int, buf: &!b [byte]) -> [fs_read(""), file_read] (int, int) {
    let p = peek(ev, at);
    if p.0 != 0 {
        return (p.0, 0);
    }
    let c = contents(ev.cache);
    var i = 0;
    while i < p.2 {
        buf[i] = c[p.1 + i];
        i = i + 1;
    }
    return (0, p.2);
}

// Where to start walking to find event `id`: the logical offset of an event at or before it (the segment's first, or the nearest of the
// sparse index). Never before the first retained event.
pub fn seek[&e](ev: &e Ev, id: int) -> [] int {
    var j = 0;
    var lo = 0;
    var hi = ev.nseg - 1;
    while lo < hi {
        let mid = (lo + hi + 1) / 2;
        if seg_first_id(ev, mid) <= id {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    j = lo;
    var start = seg_base(ev, j);
    if id >= 1 {
        let tag = (id - 1) / idx_every();
        let slot = tag % idx_cap();
        let ix = contents(ev.idx);
        let off = ix[2 * slot + 1];
        if ix[2 * slot] == tag + 1 && off > start && off < ev.tail {
            start = off;
        }
    }
    if start < ev.first {
        start = ev.first;
    }
    return start;
}

// While scanning the whole log (the start), note where event `id` begins so that `seek` can use it.
pub fn note_scan[&e](ev: &!e Ev, id: int, at: int) -> [] int {
    if (id - 1) % idx_every() == 0 {
        let slot = (id - 1) / idx_every() % idx_cap();
        let ix = contents(ev.idx);
        ix[2 * slot] = (id - 1) / idx_every() + 1;
        ix[2 * slot + 1] = at;
    }
    return 0;
}

// ---------------------------------------------------------------------
// What the log is
// ---------------------------------------------------------------------

pub fn first_offset[&e](ev: &e Ev) -> [] int {
    return ev.first;
}

pub fn first_id[&e](ev: &e Ev) -> [] int {
    return seg_first_id(ev, 0);
}

pub fn last_id[&e](ev: &e Ev) -> [] int {
    return ev.last_id;
}

// Where the next record will start (staged records counted).
pub fn tail[&e](ev: &e Ev) -> [] int {
    return ev.tail;
}

pub fn synced[&e](ev: &e Ev) -> [] int {
    return ev.synced;
}

pub fn broken[&e](ev: &e Ev) -> [] bool {
    return ev.broken;
}

pub fn segments[&e](ev: &e Ev) -> [] int {
    return ev.nseg;
}

pub fn limit[&e](ev: &e Ev) -> [] int {
    return ev.limit;
}

// The bytes of events the log holds (what disk the retained events take, less headers).
pub fn retained_bytes[&e](ev: &e Ev) -> [] int {
    return ev.tail - ev.first;
}

// The bytes the events log occupies on disk: the events, and a header in each segment. What `/metrics` calls its size, and the size of the file when there is one segment.
pub fn disk_bytes[&e](ev: &e Ev) -> [] int {
    var total = ev.tail - ev.first;
    var j = 0;
    while j < ev.nseg {
        total = total + seg_hdr(ev, j);
        j = j + 1;
    }
    return total;
}

// Of those, the bytes a flush has made durable (what is staged is not).
pub fn disk_synced[&e](ev: &e Ev) -> [] int {
    return disk_bytes(ev) - (ev.tail - ev.synced);
}

// The size of the active segment's events.
pub fn active_bytes[&e](ev: &e Ev) -> [] int {
    return ev.tail - seg_base(ev, ev.nseg - 1);
}

// The number of the active segment (0 is `events.seg`).
pub fn active_k[&e](ev: &e Ev) -> [] int {
    return seg_k(ev, ev.nseg - 1);
}

// When the active segment was created (Unix ms).
pub fn active_created[&e](ev: &e Ev) -> [] int {
    return seg_created(ev, ev.nseg - 1);
}

// For the oldest segment, when it is sealed: the id of its last event and the time the segment after it was created (no event in it is
// younger). (-1, -1) if there is only the active segment.
pub fn oldest_sealed[&e](ev: &e Ev) -> [] (int, int) {
    if ev.nseg < 2 {
        return (0 - 1, 0 - 1);
    }
    return (seg_first_id(ev, 1) - 1, seg_created(ev, 1));
}

// How many events the oldest segment holds, and its number.
pub fn oldest_events[&e](ev: &e Ev) -> [] int {
    if ev.nseg < 2 {
        return 0;
    }
    return seg_first_id(ev, 1) - seg_first_id(ev, 0);
}

pub fn count_of[&e](ev: &e Ev, which: int) -> [] int {
    return contents(ev.sc)[which];
}

pub fn rolls[&e](ev: &e Ev) -> [] int {
    return contents(ev.sc)[s_rolls()];
}

pub fn dropped_segments[&e](ev: &e Ev) -> [] int {
    return contents(ev.sc)[s_dropped_segments()];
}

pub fn dropped_events[&e](ev: &e Ev) -> [] int {
    return contents(ev.sc)[s_dropped_events()];
}

pub fn orphans[&e](ev: &e Ev) -> [] int {
    return contents(ev.sc)[s_orphans()];
}

pub fn note_clamped[&e](ev: &!e Ev, n: int) -> [] int {
    contents(ev.sc)[s_clamped()] = contents(ev.sc)[s_clamped()] + n;
    return 0;
}

pub fn clamped[&e](ev: &e Ev) -> [] int {
    return contents(ev.sc)[s_clamped()];
}

pub fn dir_of[&e](ev: &e Ev) -> [] &e [byte] {
    return contents(ev.dir)[0..ev.dlen];
}

// ---------------------------------------------------------------------
// The steps of a compaction
// ---------------------------------------------------------------------

// A step number that `compact-kill-at` can name has been reached. If it is the one named, say so (a file `killpoint` in the data directory,
// holding the number) and wait to be killed: the test does the killing, when it has seen the file, so nothing depends on timing. Answers 0
// otherwise.
pub fn step[&e](ev: &e Ev, n: int) -> [fs_write(""), file_write, poll] int {
    if ev.kill_at != n {
        return 0;
    }
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let text = alloc_slice[a](24, byte_of(0));
        let pn = manifest_path(ev, path, "killpoint");
        let tn = store.nat_text(text, 0, n);
        store.write_file(ev.fs, path[0..pn], text[0..tn], true);
    }
    region b {
        let waits = alloc_slice[b](16, 0);
        match poller_new() {
            Polling::Ok(p0) => {
                var p = p0;
                borrow mut p as &!pw in {
                    while true {
                        poller_wait(pw, waits, 1000);
                    }
                }
                poller_close(p);
                return 0;
            }
            Polling::Failed(e) => {
                while true {
                }
                return 0;
            }
        }
    }
}

// Seal the active segment and begin the next (`docs/retention.md` section 5). The caller has flushed: nothing is staged. Steps 1 to 6 of the
// table in section 10. Answers 0, or an errno (the roll did not happen, and what it made is removed).
pub fn roll[&e](ev: &!e Ev, now_ms: int) -> [fs_read(""), fs_write(""), file_write, poll] int {
    if ev.broken || ev.slen != 0 {
        return 3;
    }
    if ev.nseg >= seg_max() {
        return 28;
    }
    step(ev, 1);
    var out = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let hb = alloc_slice[a](256, byte_of(0));
        let act = ev.nseg - 1;
        let k = seg_k(ev, act) + 1;
        let pn = path_for(ev, path, k);
        var rc = store.create_new(ev.fs, path[0..pn]);
        if rc != 0 {
            out = rc;
        } else {
            step(ev, 2);
            let hn = put_header(hb, k, ev.tail, ev.last_id + 1, now_ms);
            rc = store.append_bytes(ev.fs, path[0..pn], hb[0..hn], false);
            if rc == 0 {
                step(ev, 3);
                rc = store.sync_path(ev.fs, path[0..pn]);
            }
            if rc == 0 {
                step(ev, 4);
                rc = store.sync_path(ev.fs, contents(ev.dir)[0..ev.dlen]);
            }
            if rc != 0 {
                store.remove(ev.fs, path[0..pn]);
                out = rc;
            } else {
                step(ev, 5);
                contents(ev.segs)[6 * act + 5] = ev.tail;
                put_row(ev, act + 1, k, ev.tail, hn, ev.last_id + 1, now_ms, 0);
                ev.nseg = ev.nseg + 1;
                contents(ev.sc)[s_rolls()] = contents(ev.sc)[s_rolls()] + 1;
                step(ev, 6);
            }
        }
    }
    return out;
}

// Drop the oldest segment, which must be sealed (the caller has decided that it may go: `docs/retention.md` section 5). Steps 7 to 11. The
// manifest moves first, so a crash leaves an orphan file and never a manifest that names a missing one. Answers 0, or an errno (nothing in memory
// has changed; a retry does the same steps again).
pub fn drop_oldest[&e](ev: &!e Ev) -> [fs_read(""), fs_write(""), file_write, poll] int {
    if ev.nseg < 2 || ev.broken {
        return 3;
    }
    var rc = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let tmp = alloc_slice[a](2112, byte_of(0));
        let text = alloc_slice[a](24, byte_of(0));
        let tn = store.nat_text(text, 0, seg_k(ev, 1));
        text[tn] = byte_of('\n');
        let mp = manifest_path(ev, path, "events.first");
        let tp = manifest_path(ev, tmp, "events.first.tmp");
        rc = store.write_file(ev.fs, tmp[0..tp], text[0..tn + 1], true);
        if rc == 0 {
            step(ev, 7);
            rc = store.rename(ev.fs, tmp[0..tp], path[0..mp]);
        }
        if rc == 0 {
            step(ev, 8);
            rc = store.sync_path(ev.fs, contents(ev.dir)[0..ev.dlen]);
        }
        if rc == 0 {
            step(ev, 9);
            let sp = path_for(ev, path, seg_k(ev, 0));
            rc = store.remove(ev.fs, path[0..sp]);
            if rc == 2 {
                rc = 0;
            }
        }
        if rc == 0 {
            step(ev, 10);
            rc = store.sync_path(ev.fs, contents(ev.dir)[0..ev.dlen]);
        }
        if rc == 0 {
            step(ev, 11);
        }
    }
    if rc != 0 {
        return rc;
    }
    contents(ev.sc)[s_dropped_events()] = contents(ev.sc)[s_dropped_events()] + seg_first_id(ev, 1) - seg_first_id(ev, 0);
    contents(ev.sc)[s_dropped_segments()] = contents(ev.sc)[s_dropped_segments()] + 1;
    var r = 0;
    while r + 1 < ev.nseg {
        var c = 0;
        while c < 6 {
            contents(ev.segs)[6 * r + c] = contents(ev.segs)[6 * (r + 1) + c];
            c = c + 1;
        }
        r = r + 1;
    }
    ev.nseg = ev.nseg - 1;
    ev.first = seg_base(ev, 0);
    ev.clen = 0;
    return 0;
}
