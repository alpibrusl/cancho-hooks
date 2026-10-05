edition 5;

module logguard;

import std.io;
import log;
import record;
import segment;
import state;
import store;

// `logguard` -- refuse corruption instead of repairing it silently (`docs/production.md` 0.5, `docs/design.md` section 34.5).
//
// `lexsys-log`'s `recover` keeps the longest valid prefix of a log and cuts the rest, whatever the rest is. That is exactly right for the one thing a crash
// leaves, an unfinished write at the end, and wrong for anything else: one flipped byte in the middle of `events.seg` made the next start cut every record after
// it, silently (measured: 500 events, one byte, 250 gone). `lexsys-log` is another repository, so this module does the looking *before* the cut, with the same
// reader (`segment.scan`, so the same length, checksum and increasing-id rules), and the cut itself is `recover`'s own three steps.
//
// **What is a torn tail and what is damage.** The scan stops at the first place that is not a whole record, `valid_end`. What follows it is
//   * a *torn tail* if nothing in it is a whole record and it does not look like two or more records that were whole once: the unfinished write of a crash, a
//     page of zeros that never reached the disk. It is cut, as before, and reported on stderr after `listening`;
//   * *damage* if a record that validates (length, checksum, structure) starts anywhere after the first bad place, or if the bytes there parse as two records of
//     plausible length one after the other. Cutting would throw away records that are intact, or ones that a crash does not produce. The start is refused (status 19),
//     with where the log is whole up to, how many bytes the cut would take and how many intact records are among them; `repair-logs 1` cuts anyway, after copying
//     the cut bytes to `<log>.cut-<offset>`, and says so.
//
// This is a heuristic with one known false positive, and it errs on the side of asking: a crash that persisted a later page of the unsynced tail but not an earlier one
// leaves intact records after a hole (the records are unacknowledged, since nothing after a flush's boundary was), and is refused like bit rot is. The cost is an
// operator step; the cost of the other mistake was records.
//
// **The pair.** `delivery.seg` names events by id. If it names one that `events.seg` does not hold (an older events log beside a newer delivery log, as a restore
// done by hand makes), the service would acknowledge new events under ids it believes delivered, and never deliver them (`docs/runbook.md` section 4.7, measured).
// `pair_check` finds the largest event id `delivery.seg` refers to; the start is refused (status 18) when it is above the last id in `events.seg`.
//
// **Nothing is changed until everything is judged.** `preflight` opens both logs read-only, inspects each, and checks the pair against what each would be *after* its cut; only then
// does the start open them for appending, and `recover_known` apply the cut that was judged (it does not read the log again). So a refusal, whichever it is, leaves the directory as it was,
// and `repair-logs` cannot leave a log cut and the start refused.
//
// The report of a log, `rep` (`rep_size()` integers):
//     0 what was found: 0 clean, 1 a torn tail (cut), 2 damage (refused), 3 damage (cut, `repair-logs`), 4 a torn tail that could not be cut
//     1 valid_end: the offset where the log stops being whole       2 records before it       3 the id of the last of them (-1 if none)
//     4 bytes from valid_end to the end of the file                  5 offset of the first intact record after valid_end, or -1
//     6 intact records from there on (contiguous)                    7 plausible-length records chained from valid_end (2 is "two or more")
//     8 the sequence number of the last whole record (-1 if none)    9 the size of the file when it was looked at
//    10 1 if the first record is the header of a log written since retention (it counts in 2, and is not said in the messages: "records" are events or outcomes)

pub fn rep_size() -> [] int {
    return 11;
}

// The whole report of a start: the events log's, the delivery log's, then the pair's two integers, then which log was refused for damage (1 events, 2 delivery), then the
// number of the events segment that the events log's report is about (the last one: the only one a crash can leave a torn tail in, `docs/retention.md` section 5).
pub fn report_size() -> [] int {
    return 2 * rep_size() + 4;
}

// Where that segment number is.
pub fn segment_at() -> [] int {
    return 2 * rep_size() + 3;
}

// Where the pair's two integers are: the largest event the delivery log refers to, and the last event the events log would hold.
pub fn pair_at() -> [] int {
    return 2 * rep_size();
}

pub fn clean() -> [] int {
    return 0;
}

pub fn torn_cut() -> [] int {
    return 1;
}

pub fn damage_refused() -> [] int {
    return 2;
}

pub fn damage_cut() -> [] int {
    return 3;
}

// What `recover` answers instead of an errno when it refuses (the caller turns these into status 19): damage that `repair-logs` was not given for, and damage whose
// bytes could not be kept before the cut (so nothing was cut).
pub fn refused() -> [] int {
    return 2000;
}

pub fn not_kept() -> [] int {
    return 2001;
}

fn min_total() -> [] int {
    return 28;
}

// Whole-looking records chained from `from`, by their lengths alone (the checksum is not asked): at most 2.
fn chain[&f, &b](file: &!f File, size: int, from: int, buf: &!b [byte], max_len: int) -> [file_read] int {
    var pos = from;
    var n = 0;
    while n < 2 && pos + 4 <= size {
        match file_pread(file, pos, buf[0..4]) {
            Read::Got(k) => {
                if k < 4 {
                    return n;
                }
            }
            Read::End => {
                return n;
            }
            Read::Failed(e) => {
                return n;
            }
        }
        let length = record.get_u32(buf, 0);
        if length < record.min_len() || length > max_len || pos + 4 + length > size {
            return n;
        }
        n = n + 1;
        pos = pos + 4 + length;
    }
    return n;
}

// The offset of the first record after `from` (not at it) that validates, or -1. Every offset is a candidate: a record is found wherever its length is plausible and
// its checksum agrees. The window is read forward; a record that may run past the end of it is read again from where it starts.
fn find_valid[&f, &b](file: &!f File, size: int, from: int, buf: &!b [byte], max_len: int) -> [file_read] int {
    var pos = from + 1;
    while pos + min_total() <= size {
        var want = size - pos;
        if want > len(buf) {
            want = len(buf);
        }
        var got = 0;
        match file_pread(file, pos, buf[0..want]) {
            Read::Got(n) => {
                got = n;
            }
            Read::End => {
                return 0 - 1;
            }
            Read::Failed(e) => {
                return 0 - 1;
            }
        }
        var i = 0;
        var refill = false;
        while i + 4 <= got && !refill {
            let length = record.get_u32(buf, i);
            if length >= record.min_len() && length <= max_len {
                if i + 4 + length <= got {
                    if record.check(buf, i, got, max_len).0 == record.ok() {
                        return pos + i;
                    }
                } else if pos + got < size {
                    refill = true;
                }
            }
            if !refill {
                i = i + 1;
            }
        }
        if got < min_total() {
            return 0 - 1;
        }
        pos = pos + i;
    }
    return 0 - 1;
}

// How many whole records follow one another from `from`.
fn count_valid[&f, &b](file: &!f File, size: int, from: int, buf: &!b [byte], max_len: int) -> [file_read] int {
    var pos = from;
    var n = 0;
    var going = true;
    while going && pos < size {
        var want = size - pos;
        if want > len(buf) {
            want = len(buf);
        }
        var got = 0;
        match file_pread(file, pos, buf[0..want]) {
            Read::Got(k) => {
                got = k;
            }
            Read::End => {
                going = false;
            }
            Read::Failed(e) => {
                going = false;
            }
        }
        if going {
            let r = record.check(buf, 0, got, max_len);
            if r.0 == record.ok() {
                n = n + 1;
                pos = pos + r.1;
            } else {
                going = false;
            }
        }
    }
    return n;
}

// Look at the log `file` (length `size`) without changing it, and fill `rep`. Answers what `segment.scan` answers, except the verdict, which is `clean()`, `torn_cut()`
// (a tail that is safe to cut), `damage_refused()` or `segment.unreadable()`.
pub fn inspect[&f, &b, &r](file: &!f File, size: int, window: &!b [byte], max_len: int, rep: &!r [int]) -> [file_read] (int, int, int, int, int) {
    var i = 0;
    while i < rep_size() {
        rep[i] = 0;
        i = i + 1;
    }
    rep[3] = 0 - 1;
    rep[5] = 0 - 1;
    rep[8] = 0 - 1;
    rep[9] = size;
    let r = segment.scan(file, size, window, max_len, 0 - 1, 0 - 1);
    rep[1] = r.1;
    rep[2] = r.2;
    rep[3] = r.3;
    rep[8] = r.4;
    rep[4] = size - r.1;
    if r.0 == segment.unreadable() {
        return (segment.unreadable(), r.1, r.2, r.3, r.4);
    }
    if r.0 == segment.clean() {
        return (clean(), r.1, r.2, r.3, r.4);
    }
    // Something follows the last whole record: a torn tail, or damage.
    let found = find_valid(file, size, r.1, window, max_len);
    rep[5] = found;
    if found >= 0 {
        rep[6] = count_valid(file, size, found, window, max_len);
    }
    rep[7] = chain(file, size, r.1, window, max_len);
    if found >= 0 || rep[7] >= 2 {
        rep[0] = damage_refused();
        return (damage_refused(), r.1, r.2, r.3, r.4);
    }
    rep[0] = torn_cut();
    return (torn_cut(), r.1, r.2, r.3, r.4);
}

fn path_into[&d, &n, &w](out: &!w [byte], dir: &d [byte], name: &n [byte], offset: int) -> [] int {
    var i = 0;
    while i < len(dir) {
        out[i] = dir[i];
        i = i + 1;
    }
    out[i] = byte_of('/');
    i = i + 1;
    var j = 0;
    while j < len(name) {
        out[i + j] = name[j];
        j = j + 1;
    }
    i = i + len(name);
    if offset < 0 {
        return i;
    }
    let suffix = ".cut-";
    j = 0;
    while j < len(suffix) {
        out[i + j] = suffix[j];
        j = j + 1;
    }
    i = i + len(suffix);
    var width = 1;
    var t = offset;
    while t >= 10 {
        t = t / 10;
        width = width + 1;
    }
    var k = width;
    var m = offset;
    while k > 0 {
        out[i + k - 1] = byte_of('0' + m % 10);
        m = m / 10;
        k = k - 1;
    }
    return i + width;
}

// Copy `rw[from..size]` to a new file `<dir>/<name>.cut-<from>`, synced. 0 if it is there, 1 if it could not be made (it exists already, or the disk would not take it).
fn keep_cut[&c, &d, &n, &f, &b](fs: &c Fs(""), dir: &d [byte], name: &n [byte], rw: &!f File, from: int, size: int, buf: &!b [byte]) -> [fs_read(""), fs_write(""), file_read, file_write] int {
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        let path = path_buf[0..path_into(path_buf, dir, name, from)];
        match open_read(fs, path) {
            Opened::Ok(existing) => {
                file_close(existing);
                return 1;
            }
            Opened::Failed(e) => {
            }
        }
        match open_append(fs, path) {
            Opened::Failed(e) => {
                return 1;
            }
            Opened::Ok(out0) => {
                var out = out0;
                var ok = true;
                var pos = from;
                while ok && pos < size {
                    var want = size - pos;
                    if want > len(buf) {
                        want = len(buf);
                    }
                    var got = 0;
                    match file_pread(rw, pos, buf[0..want]) {
                        Read::Got(n) => {
                            got = n;
                        }
                        Read::End => {
                            ok = false;
                        }
                        Read::Failed(e) => {
                            ok = false;
                        }
                    }
                    var done = 0;
                    while ok && done < got {
                        borrow mut out as &!oh in {
                            match file_write(oh, buf[done..got]) {
                                Done::Ok(n) => {
                                    done = done + n;
                                }
                                Done::Failed(e) => {
                                    ok = false;
                                }
                            }
                        }
                    }
                    pos = pos + got;
                    if got == 0 {
                        ok = false;
                    }
                }
                if ok {
                    borrow mut out as &!oh in {
                        match file_sync(oh) {
                            Done::Ok(n) => {
                            }
                            Done::Failed(e) => {
                                ok = false;
                            }
                        }
                    }
                }
                file_close(out);
                if ok {
                    return 0;
                }
                return 1;
            }
        }
    }
}

// Apply what `inspect` judged about the log `rw` (opened read-write): nothing for a clean log, the cut for a torn tail, and for damage the cut after the cut bytes were copied to
// `<log>.cut-<offset>` (only with `repair`; `preflight` has refused it otherwise). Answers what `log.recover` does -- `(status, valid_end, records, last_ms, last_seq)` -- where `status` is
// 0, an errno, 1000 for an unreadable file, or `not_kept()`. The log is not read again.
pub fn recover_known[&f, &b, &r, &c, &d, &n](rw: &!f File, rep: &!r [int], repair: bool, fs: &c Fs(""), dir: &d [byte], name: &n [byte], window: &!b [byte]) -> [fs_read(""), fs_write(""), file_read, file_write] (int, int, int, int, int) {
    let known = (rep[1], rep[2], rep[3], rep[8]);
    if rep[0] == segment.unreadable() {
        return (1000, known.0, known.1, known.2, known.3);
    }
    if rep[0] == clean() {
        return (0, known.0, known.1, known.2, known.3);
    }
    if rep[0] == damage_refused() {
        if !repair {
            return (refused(), known.0, known.1, known.2, known.3);
        }
        if keep_cut(fs, dir, name, rw, known.0, rep[9], window) != 0 {
            return (not_kept(), known.0, known.1, known.2, known.3);
        }
        rep[0] = damage_cut();
    }
    match file_truncate(rw, known.0) {
        Done::Ok(n) => {
            match file_sync(rw) {
                Done::Ok(m) => {
                }
                Done::Failed(e) => {
                    rep[0] = 4;
                    return (e, known.0, known.1, known.2, known.3);
                }
            }
        }
        Done::Failed(e) => {
            rep[0] = 4;
            return (e, known.0, known.1, known.2, known.3);
        }
    }
    return (0, known.0, known.1, known.2, known.3);
}

// The largest event id that the outcome records in `file[..valid_end]` (the delivery log) refer to. An outcome names an event in its `event` field (delivered, failed, dead, the replays',
// a failure's reason) or, for a `created` record, in the endpoint's starting cursor. Records that are not outcomes are skipped (`prepare` refuses the log for them later).
fn last_reference[&f, &w](file: &!f File, valid_end: int, window: &!w [byte], max_len: int) -> [file_read] int {
    var pos = 0;
    var most = 0;
    while pos < valid_end {
        var want = valid_end - pos;
        if want > len(window) {
            want = len(window);
        }
        var got = 0;
        match file_pread(file, pos, window[0..want]) {
            Read::Got(n) => {
                got = n;
            }
            Read::End => {
                return most;
            }
            Read::Failed(e) => {
                return most;
            }
        }
        var at = 0;
        var refill = false;
        while at < got && !refill {
            let r = record.check(window, at, got, max_len);
            if r.0 == record.ok() {
                let o = state.outcome_at(window, at);
                var ref = 0;
                if o.0 == state.delivered() || o.0 == state.failed() || o.0 == state.dead() || o.0 >= state.replay() && o.0 <= state.replay_dead() || o.0 == state.reason() {
                    ref = o.2;
                } else if o.0 == state.created() {
                    ref = o.3;
                } else if o.0 == state.advanced() || o.0 == state.erased() {
                    ref = o.2;
                }
                if ref > most {
                    most = ref;
                }
                at = at + r.1;
            } else if pos + got < valid_end {
                refill = true;
            } else {
                return most;
            }
        }
        if at == 0 {
            return most;
        }
        pos = pos + at;
    }
    return most;
}

// Look at one log without changing it: fill `rep`, and answer the verdict (`clean()`, `torn_cut()`, `damage_refused()`, `segment.unreadable()`). A log that is not there is a clean empty one;
// one that cannot be opened is left to the real open, which will say why (status 10 or 12). `refs` is the largest event id the log refers to if it is the delivery log (`outcomes`), else 0.
fn look[&c, &d, &n, &w, &r](fs: &c Fs(""), dir: &d [byte], name: &n [byte], window: &!w [byte], max_len: int, rep: &!r [int], outcomes: bool) -> [fs_read(""), file_read] (int, int) {
    var i = 0;
    while i < rep_size() {
        rep[i] = 0;
        i = i + 1;
    }
    rep[3] = 0 - 1;
    rep[5] = 0 - 1;
    rep[8] = 0 - 1;
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        let path = path_buf[0..path_into(path_buf, dir, name, 0 - 1)];
        match open_read(fs, path) {
            Opened::Failed(e) => {
                // No file (ENOENT) is a log with nothing in it. A file that is there and cannot be opened is not: it cannot be judged, and the
                // start that follows reports it as it always did (status 10 or 12), not as a log that is short.
                if e == 2 {
                    return (clean(), 0);
                }
                return (segment.unreadable(), 0);
            }
            Opened::Ok(f0) => {
                var f = f0;
                var size = 0;
                var verdict = segment.unreadable();
                var refs = 0;
                borrow mut f as &!fh in {
                    match file_size(fh) {
                        Done::Ok(n) => {
                            size = n;
                            verdict = inspect(fh, size, window, max_len, rep).0;
                            if verdict != segment.unreadable() && rep[2] > 0 {
                                var got = 0;
                                match file_pread(fh, 0, window[0..128]) {
                                    Read::Got(k) => {
                                        got = k;
                                    }
                                    Read::End => {
                                    }
                                    Read::Failed(e) => {
                                    }
                                }
                                if got >= 128 {
                                    if outcomes {
                                        if state.outcome_at(window, 0).0 == state.format() {
                                            rep[10] = 1;
                                        }
                                    } else if record.ms_of(window, 0) == 0 {
                                        rep[10] = 1;
                                    }
                                }
                            }
                            if outcomes && verdict != segment.unreadable() {
                                refs = last_reference(fh, rep[1], window, max_len);
                            }
                        }
                        Done::Failed(e) => {
                            rep[0] = segment.unreadable();
                        }
                    }
                }
                file_close(f);
                return (verdict, refs);
            }
        }
    }
}

// The number of the last segment of the events log: from the one `events.first` names (0 if there is no such file), up while the files exist. A directory from before retention has
// `events.seg` alone and answers 0.
fn last_segment[&c, &d](fs: &c Fs(""), dir: &d [byte]) -> [fs_read(""), file_read] int {
    var k = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let small = alloc_slice[a](32, byte_of(0));
        let pn = store.path_join(path, dir, "events.first");
        let got = store.read_range(fs, path[0..pn], 0, small);
        var i = 0;
        var n = 0;
        while got > 0 && i < got && int_of(small[i]) >= 48 && int_of(small[i]) <= 57 && n < 1000000000 {
            n = n * 10 + int_of(small[i]) - 48;
            i = i + 1;
        }
        if i > 0 {
            k = n;
        }
        var more = true;
        while more && k < n + 4096 + 1 {
            let nm = store.seg_path(path, dir, k + 1);
            if store.size_of(fs, path[0..nm]) >= 0 {
                k = k + 1;
            } else {
                more = false;
            }
        }
    }
    return k;
}

// Judge both logs before anything is changed (see the head of this file). `rep` is `2 * rep_size() + 4` integers: the events log's report, the delivery log's, then the pair's
// (the largest event the delivery log refers to, the last event the events log would hold). Answers 0 to go on; 19 for damage that `repair` does not allow cutting (the events log
// is judged first; `which` tells which: `rep[2 * rep_size() + 2]`); 18 for a pair that disagrees *after* the cuts that would be made.
pub fn preflight[&c, &d, &w, &r](fs: &c Fs(""), dir: &d [byte], window: &!w [byte], max_len: int, repair: bool, rep: &!r [int]) -> [fs_read(""), file_read] int {
    let n = rep_size();
    // The events log is a chain of segments (`docs/retention.md`): the last is the one that can end in a torn tail, and the one this judges. (The others are sealed: the start
    // checks that each ends where the next begins.)
    let lastk = last_segment(fs, dir);
    rep[2 * n + 3] = lastk;
    var events = (clean(), 0);
    region a {
        let name = alloc_slice[a](64, byte_of(0));
        events = look(fs, dir, name[0..store.seg_name(name, lastk)], window, max_len, rep[0..n], false);
    }
    let delivery = look(fs, dir, "delivery.seg", window, max_len, rep[n..2 * n], true);
    rep[2 * n + 2] = 0;
    if events.0 == segment.unreadable() || delivery.0 == segment.unreadable() {
        // A log that cannot be read cannot be judged: go on, and the open that follows says why (status 10 or 12).
        return 0;
    }
    if events.0 == damage_refused() && !repair {
        rep[2 * n + 2] = 1;
        return 19;
    }
    if delivery.0 == damage_refused() && !repair {
        rep[2 * n + 2] = 2;
        return 19;
    }
    var last = rep[3];
    if last < 0 {
        last = 0;
    }
    rep[2 * n] = delivery.1;
    rep[2 * n + 1] = last;
    // A last segment that holds no event (just rolled, or everything dropped) says nothing about the last event: the start judges the pair again once the log is open.
    if last >= 1 && delivery.1 > last {
        return 18;
    }
    return 0;
}

// ---------------------------------------------------------------------
// Saying so
// ---------------------------------------------------------------------

fn say[&i, &t](out: &!i Io, text: &t [byte]) -> [err_write] int {
    return io.error_all(out, text);
}

fn num[&i](out: &!i Io, n: int) -> [err_write] int {
    region a {
        let nb = alloc_slice[a](24, byte_of(0));
        var width = 1;
        var t = n;
        while t >= 10 {
            t = t / 10;
            width = width + 1;
        }
        var k = width;
        var m = n;
        while k > 0 {
            nb[k - 1] = byte_of('0' + m % 10);
            m = m / 10;
            k = k - 1;
        }
        say(out, nb[0..width]);
    }
    return 0;
}

// Why a log was refused (status 19), from its report, on stderr.
pub fn say_damage[&i, &n, &r](out: &!i Io, name: &n [byte], rep: &r [int], why: int) -> [err_write] int {
    say(out, "hooks: ");
    say(out, name);
    say(out, ": damage in the middle of the log, not a torn tail. It is whole for ");
    num(out, rep[2] - rep[10]);
    say(out, " records (up to byte ");
    num(out, rep[1]);
    say(out, "); after that ");
    num(out, rep[4]);
    say(out, " bytes do not read as the log");
    if rep[5] >= 0 {
        say(out, ", and ");
        num(out, rep[6]);
        say(out, " intact record");
        if rep[6] != 1 {
            say(out, "s");
        }
        say(out, " start at byte ");
        num(out, rep[5]);
    }
    say(out, ".\nhooks: ");
    if why == not_kept() {
        say(out, "nothing was cut: the bytes after the damage could not be copied to ");
        say(out, name);
        say(out, ".cut-");
        num(out, rep[1]);
        say(out, " first (is it there already, or is the disk full?).\n");
    } else {
        say(out, "starting would cut the log at byte ");
        num(out, rep[1]);
        say(out, " and lose those ");
        num(out, rep[4]);
        say(out, " bytes. Check the disk, or restore a backup (scripts/logcheck.py check <dir> says the same); or start once with --repair-logs 1 to cut it there, keeping the cut bytes in ");
        say(out, name);
        say(out, ".cut-");
        num(out, rep[1]);
        say(out, "\n");
        if name_is_events(name) {
            say(out, "hooks: (a shortened events.seg must agree with delivery.seg: if delivery.seg refers to events beyond the cut, the start is refused again, status 18, and nothing is cut; then restore a backup pair, or move delivery.seg aside to have every event delivered again)\n");
        }
    }
    return 0;
}

fn name_is_events[&n](name: &n [byte]) -> [] bool {
    return len(name) >= 10 && name[0] == byte_of('e');
}

// Status 19 from the whole report: the log that was refused is named in its last cell (or, when nothing was cut because the bytes could not be kept, the one whose verdict says so).
pub fn say_damage_of[&i, &r](out: &!i Io, report: &r [int], why: int) -> [err_write] int {
    var delivery = report[2 * rep_size() + 2] == 2;
    if why == not_kept() {
        delivery = report[rep_size()] == damage_refused() && report[0] != damage_refused();
    }
    if delivery {
        say_damage(out, "delivery.seg", report[rep_size()..2 * rep_size()], why);
    } else {
        region a {
            let name = alloc_slice[a](64, byte_of(0));
            say_damage(out, name[0..store.seg_name(name, report[segment_at()])], report[0..rep_size()], why);
        }
    }
    return 0;
}

// What a start did to a log, after `listening`: nothing for a clean one; for a torn tail that was cut, or damage that `repair-logs` cut.
pub fn say_cut[&i, &n, &r](out: &!i Io, name: &n [byte], rep: &r [int]) -> [err_write] int {
    if rep[0] == torn_cut() {
        say(out, "hooks: ");
        say(out, name);
        say(out, ": cut a torn tail of ");
        num(out, rep[4]);
        say(out, " bytes at byte ");
        num(out, rep[1]);
        say(out, " (an unfinished write); ");
        num(out, rep[2] - rep[10]);
        say(out, " records are whole\n");
    }
    if rep[0] == damage_cut() {
        say(out, "hooks: --repair-logs: ");
        say(out, name);
        say(out, ": cut at byte ");
        num(out, rep[1]);
        say(out, ": ");
        num(out, rep[4]);
        say(out, " bytes gone, among them ");
        num(out, rep[6]);
        say(out, " intact records; ");
        num(out, rep[2] - rep[10]);
        say(out, " records are whole. The bytes are kept in ");
        say(out, name);
        say(out, ".cut-");
        num(out, rep[1]);
        say(out, "\n");
    }
    return 0;
}

// What a start did to the events log (its last segment), after `listening`.
pub fn say_cut_events[&i, &r](out: &!i Io, report: &r [int]) -> [err_write] int {
    region a {
        let name = alloc_slice[a](64, byte_of(0));
        say_cut(out, name[0..store.seg_name(name, report[segment_at()])], report[0..rep_size()]);
    }
    return 0;
}

// Why the pair was refused (status 18).
pub fn say_pair[&i, &r](out: &!i Io, rep: &r [int]) -> [err_write] int {
    say(out, "hooks: delivery.seg refers to event ");
    num(out, rep[0]);
    say(out, " but events.seg ends at event ");
    num(out, rep[1]);
    say(out, ": an older events.seg beside a newer delivery.seg (or a events.seg that --repair-logs would cut shorter than delivery.seg knows). Starting would acknowledge new events under ids already recorded as delivered and never deliver them. Nothing was changed. Restore a matching pair (scripts/restore.sh checks this), or move delivery.seg aside to have every event delivered again\n");
    return 0;
}
