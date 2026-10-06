edition 6;

// `hooks-logcheck` -- read the two logs of a data directory without the service, and say whether they are a pair worth restoring (`docs/design.md` section 54).
//
//     hooks-logcheck check DIR [--kv]
//     hooks-logcheck trim FILE
//
// The same checks, the same words and the same exit status (0 consistent, 1 not, 2 usage) as `scripts/logcheck.py check`, which this replaces in `backup.sh` and `restore.sh`
// because it is about 350 times faster (lexsys-log's CRC-32C: 530 MB a second here, against Python's 1.5): restoring 400 MB of logs took four and a half minutes of CPU in the
// Python reader and takes a second or two in this one. `scripts/logcheck.py` stays: it is written apart from the service (`tests/corrupt_test.py` and `tests/chaos.py` use it as
// the independent reader), and `tests/logcheck_test.py` makes the two agree on every log it can build and damage. This one shares `segment`, `record` and the service's own rule
// for a torn tail and damage (`logguard.inspect`, the rule `logcheck.py` is the reference of), and nothing else of the service: it opens every file read-only.
//
// What is checked, in short: every segment of the events log, from the one `events.first` names, has a header of a format this code reads, begins where the one before it ends,
// holds dense ids, ends in whole records (a sealed one) or a torn tail (the last one) and holds only records of the four shapes the service writes; `delivery.seg` holds
// only outcomes of known kinds; and it does not refer to an event that the events log does not hold.

import std.bytes;
import std.io;
import record;
import segment;
import state;
import store;
import logguard;

fn max_len() -> [] int {
    return 65536;
}

fn window_size() -> [] int {
    return 1048576;
}

fn text_size() -> [] int {
    return 65536;
}

fn out_size() -> [] int {
    return 262144;
}

// The format of the events log that this version writes, and of the outcomes log (`state.format()` is its header's kind).
fn event_format() -> [] int {
    return 2;
}

// The counters, `st`:
//     0 length of the problems text    1 problems    2 length of the output
//     3 the events log is there        4 events      5 first event id (0: none seen)   6 last event id   7 segments   8 bytes   9 valid bytes   10 torn bytes (last segment)
//     11 typed events                  12 records of a shape the service does not write
//     13 where the next segment must begin (-1: no segment yet)   14 the id it must begin with   15 the last segment was a roll caught half-done and is ignored
//     20 delivery.seg is there   21 bytes   22 valid bytes   23 torn bytes   24 records   25 the largest event referred to   26 records that are not outcomes
//     30 + kind: how many records of each kind (1 to 19)         60 + reason: failed attempts by reason (0 to 26, 27 unknown)
fn st_size() -> [] int {
    return 96;
}

// ---------------------------------------------------------------------
// Text into a slice: the problems and the output. `st[idx]` is how much is in it.
// ---------------------------------------------------------------------

fn add[&b, &s, &t](buf: &!b [byte], st: &!s [int], idx: int, text: &t [byte]) -> [] int {
    var i = 0;
    while i < len(text) && st[idx] < len(buf) {
        buf[st[idx]] = text[i];
        st[idx] = st[idx] + 1;
        i = i + 1;
    }
    return 0;
}

fn addn[&b, &s](buf: &!b [byte], st: &!s [int], idx: int, n: int) -> [] int {
    if n < 0 {
        add(buf, st, idx, "-");
        return addn(buf, st, idx, 0 - n);
    }
    if n >= 10 {
        addn(buf, st, idx, n / 10);
    }
    if st[idx] < len(buf) {
        buf[st[idx]] = byte_of('0' + n % 10);
        st[idx] = st[idx] + 1;
    }
    return 0;
}

// One problem: its text goes to `pb`, a line each.
fn problem_begin[&p, &s](pb: &!p [byte], st: &!s [int]) -> [] int {
    st[1] = st[1] + 1;
    return 0;
}

fn problem_end[&p, &s](pb: &!p [byte], st: &!s [int]) -> [] int {
    add(pb, st, 0, "\n");
    return 0;
}

// ---------------------------------------------------------------------
// What a record is
// ---------------------------------------------------------------------

// The code of the key of a pair, for `shape_of`.
fn key_code[&b](buf: &b [byte], at: int, n: int) -> [] int {
    if n == 5 && int_of(buf[at]) == 'e' && int_of(buf[at + 1]) == 'v' && int_of(buf[at + 2]) == 'e' && int_of(buf[at + 3]) == 'n' && int_of(buf[at + 4]) == 't' {
        return 1;
    }
    if n == 3 && int_of(buf[at]) == 't' && int_of(buf[at + 1]) == 'y' && int_of(buf[at + 2]) == 'p' {
        return 2;
    }
    if n == 3 && int_of(buf[at]) == 'k' && int_of(buf[at + 1]) == 'e' && int_of(buf[at + 2]) == 'y' {
        return 3;
    }
    if n == 1 && int_of(buf[at]) == 't' {
        return 4;
    }
    return 9;
}

// The shape of the event record at `at`: 1 the pair `event`; 2 `event`, `typ`; 3 `event`, `key`, `t`; 4 `event`, `typ`, `key`, `t`; 0 anything else.
fn shape_of[&b](buf: &b [byte], at: int) -> [] int {
    let n = record.fields_of(buf, at);
    if n < 1 || n > 4 {
        return 0;
    }
    var p = record.first_pair(at);
    var packed = 0;
    var i = 0;
    while i < n {
        let q = record.pair_at(buf, p);
        packed = packed * 10 + key_code(buf, q.0, q.1);
        p = q.4;
        i = i + 1;
    }
    if packed == 1 {
        return 1;
    }
    if packed == 12 {
        return 2;
    }
    if packed == 134 {
        return 3;
    }
    if packed == 1234 {
        return 4;
    }
    return 0;
}

// Is the record at `at` the header of an events segment (id 0, a first pair named `format`)?
fn is_events_header[&b](buf: &b [byte], at: int) -> [] bool {
    if record.ms_of(buf, at) != 0 || record.fields_of(buf, at) < 1 {
        return false;
    }
    let q = record.pair_at(buf, record.first_pair(at));
    return q.1 == 6 && int_of(buf[q.0]) == 'f' && int_of(buf[q.0 + 1]) == 'o' && int_of(buf[q.0 + 2]) == 'r' && int_of(buf[q.0 + 3]) == 'm' && int_of(buf[q.0 + 4]) == 'a' && int_of(buf[q.0 + 5]) == 't';
}

// The start of an events segment, `got` bytes of it in `buf`: `(kind, header size, k, base, first id, version, why)`. kind: 0 no whole record (a cut header, or nothing),
// 1 a segment of the first format (no header), 2 a header, 3 a header that this code does not know (`why` says which: 1 not a header of three pairs, 2 an unreadable format).
fn head_of[&b](buf: &b [byte], got: int) -> [] (int, int, int, int, int, int, int) {
    let r = record.check(buf, 0, got, max_len());
    if r.0 != record.ok() {
        return (0, 0, 0, 0, 0, 0, 0);
    }
    if record.ms_of(buf, 0) >= 1 {
        return (1, 0, 0, 0, 1, 1, 0);
    }
    if record.ms_of(buf, 0) != 0 || record.fields_of(buf, 0) != 3 {
        return (3, 0, 0, 0, 0, 0, 1);
    }
    let a = record.pair_at(buf, record.first_pair(0));
    let b = record.pair_at(buf, a.4);
    if !is_events_header(buf, 0) || b.1 != 7 || b.3 != 32 || int_of(buf[b.0]) != 's' || int_of(buf[b.0 + 1]) != 'e' {
        return (3, 0, 0, 0, 0, 0, 1);
    }
    // `lexsys-hooks events <digits>`
    let prefix = "lexsys-hooks events ";
    if a.3 <= len(prefix) {
        return (3, 0, 0, 0, 0, 0, 2);
    }
    var i = 0;
    while i < len(prefix) {
        if int_of(buf[a.2 + i]) != int_of(prefix[i]) {
            return (3, 0, 0, 0, 0, 0, 2);
        }
        i = i + 1;
    }
    var version = 0;
    while i < a.3 {
        let c = int_of(buf[a.2 + i]);
        if c < '0' || c > '9' || version > 1000000 {
            return (3, 0, 0, 0, 0, 0, 2);
        }
        version = version * 10 + c - '0';
        i = i + 1;
    }
    return (2, r.1, record.get_u64(buf, b.2), record.get_u64(buf, b.2 + 8), record.get_u64(buf, b.2 + 16), version, 0);
}

// ---------------------------------------------------------------------
// One pass over a log: `segment.scan`'s loop, with a look at every record
// ---------------------------------------------------------------------

// The events segment `file` (length `size`), whose first event must be `first`. Counts the shapes into `st`. Answers `(verdict, valid end, events, dense, last id)`: the
// verdict is `segment.scan`'s (`clean`, `torn`, `damaged`, `unreadable`), `dense` is 1 if the ids run `first`, `first + 1`, ... and the last id is the last event's (0 if none).
fn scan_events[&f, &w, &s](file: &!f File, size: int, first: int, window: &!w [byte], st: &!s [int]) -> [file_read] (int, int, int, int, int) {
    var pos = 0;
    var events = 0;
    var dense = 1;
    var last_id = 0;
    var prev_ms = 0 - 1;
    var prev_seq = 0 - 1;
    var verdict = segment.clean();
    var going = true;
    while going && pos < size {
        var want = size - pos;
        if want > len(window) {
            want = len(window);
        }
        var got = 0;
        match file_pread(file, pos, window[0..want]) {
            Read::Got(n) => {
                got = n;
            }
            Read::End => {
                verdict = segment.torn();
                going = false;
            }
            Read::Failed(e) => {
                verdict = segment.unreadable();
                going = false;
            }
        }
        if going {
            var at = 0;
            var refill = false;
            while at < got && !refill && going {
                let r = record.check(window, at, got, max_len());
                if r.0 == record.ok() {
                    let ms = record.ms_of(window, at);
                    let seq = record.seq_of(window, at);
                    if ms < prev_ms || ms == prev_ms && seq <= prev_seq {
                        verdict = segment.damaged();
                        going = false;
                    } else {
                        prev_ms = ms;
                        prev_seq = seq;
                        if !is_events_header(window, at) {
                            if ms != first + events {
                                dense = 0;
                            }
                            events = events + 1;
                            last_id = ms;
                            let shape = shape_of(window, at);
                            if shape == 0 {
                                st[12] = st[12] + 1;
                            } else if shape == 2 || shape == 4 {
                                st[11] = st[11] + 1;
                            }
                        }
                        at = at + r.1;
                    }
                } else if r.0 == record.bad() {
                    verdict = segment.damaged();
                    going = false;
                } else if pos + got < size {
                    refill = true;
                } else {
                    verdict = segment.torn();
                    going = false;
                }
            }
            if going && at == 0 {
                verdict = segment.unreadable();
                going = false;
            }
            pos = pos + at;
        }
    }
    return (verdict, pos, events, dense, last_id);
}

// The reason of a failed attempt as `src/reason.ls` names it, by its number (`logcheck.py`'s REASONS); 27 is a number it does not know.
fn reason_name(code: int) -> [] &static [byte] {
    if code == 0 {
        return "none";
    }
    if code == 1 {
        return "connect_refused";
    }
    if code == 2 {
        return "connect_timeout";
    }
    if code == 3 {
        return "connect_error";
    }
    if code == 4 {
        return "send_timeout";
    }
    if code == 5 {
        return "send_error";
    }
    if code == 6 {
        return "no_response";
    }
    if code == 7 {
        return "reset";
    }
    if code == 8 {
        return "closed_early";
    }
    if code == 9 {
        return "bad_response";
    }
    if code == 10 {
        return "status_3xx";
    }
    if code == 11 {
        return "status_4xx";
    }
    if code == 12 {
        return "status_5xx";
    }
    if code == 13 {
        return "gone";
    }
    if code == 14 {
        return "status_other";
    }
    if code == 15 {
        return "busy";
    }
    if code == 16 {
        return "too_large";
    }
    if code == 17 {
        return "dns_failed";
    }
    if code == 18 {
        return "dns_timeout";
    }
    if code == 19 {
        return "ssrf_refused";
    }
    if code == 20 {
        return "tls_handshake";
    }
    if code == 21 {
        return "cert_untrusted";
    }
    if code == 22 {
        return "cert_expired";
    }
    if code == 23 {
        return "cert_hostname";
    }
    if code == 24 {
        return "cert_invalid";
    }
    if code == 25 {
        return "tls_timeout";
    }
    if code == 26 {
        return "tls_error";
    }
    return "unknown";
}

// Does a record of this kind name an event in its `event` field (rather than in `attempts`, or not at all)? `logcheck.py`'s EVENT_KINDS.
fn names_event(kind: int) -> [] bool {
    return kind == 1 || kind == 2 || kind == 3 || kind >= 6 && kind <= 9 || kind == 14 || kind == 16 || kind == 17 || kind == 19;
}

// The delivery log `file` (length `size`): the kinds, the reasons, the largest event it refers to, the records that are not outcomes. Answers `(verdict, valid end, records)`.
fn scan_delivery[&f, &w, &s, &p](file: &!f File, size: int, window: &!w [byte], st: &!s [int], pb: &!p [byte]) -> [file_read] (int, int, int) {
    var pos = 0;
    var total = 0;
    var formats = 0;
    var prev_ms = 0 - 1;
    var prev_seq = 0 - 1;
    var verdict = segment.clean();
    var going = true;
    while going && pos < size {
        var want = size - pos;
        if want > len(window) {
            want = len(window);
        }
        var got = 0;
        match file_pread(file, pos, window[0..want]) {
            Read::Got(n) => {
                got = n;
            }
            Read::End => {
                verdict = segment.torn();
                going = false;
            }
            Read::Failed(e) => {
                verdict = segment.unreadable();
                going = false;
            }
        }
        if going {
            var at = 0;
            var refill = false;
            while at < got && !refill && going {
                let r = record.check(window, at, got, max_len());
                if r.0 == record.ok() {
                    let ms = record.ms_of(window, at);
                    let seq = record.seq_of(window, at);
                    if ms < prev_ms || ms == prev_ms && seq <= prev_seq {
                        verdict = segment.damaged();
                        going = false;
                    } else {
                        prev_ms = ms;
                        prev_seq = seq;
                        let o = state.outcome_at(window, at);
                        let kind = o.0;
                        if kind < 1 || kind > 19 {
                            st[26] = st[26] + 1;
                        } else if kind == state.format() {
                            formats = formats + 1;
                            if total != 0 {
                                st[26] = st[26] + 1;
                            } else if o.2 != event_format() {
                                problem_begin(pb, st);
                                add(pb, st, 0, "delivery.seg: format ");
                                addn(pb, st, 0, o.2);
                                add(pb, st, 0, ", which this version does not read");
                                problem_end(pb, st);
                            }
                        } else {
                            st[30 + kind] = st[30 + kind] + 1;
                            if kind == 14 {
                                var code = o.4 % 256;
                                if code > 26 {
                                    code = 27;
                                }
                                st[60 + code] = st[60 + code] + 1;
                            }
                            if names_event(kind) {
                                if o.2 > st[25] {
                                    st[25] = o.2;
                                }
                            } else if kind == state.created() {
                                if o.3 > st[25] {
                                    st[25] = o.3;
                                }
                            }
                        }
                        total = total + 1;
                        at = at + r.1;
                    }
                } else if r.0 == record.bad() {
                    verdict = segment.damaged();
                    going = false;
                } else if pos + got < size {
                    refill = true;
                } else {
                    verdict = segment.torn();
                    going = false;
                }
            }
            if going && at == 0 {
                verdict = segment.unreadable();
                going = false;
            }
            pos = pos + at;
        }
    }
    return (verdict, pos, total - formats);
}

// ---------------------------------------------------------------------
// The checks
// ---------------------------------------------------------------------

// The words of damage in the middle of a log, `logcheck.py`'s `damage_text`.
fn damage_text[&p, &s, &n, &r](pb: &!p [byte], st: &!s [int], name: &n [byte], size: int, end: int, rep: &r [int]) -> [] int {
    add(pb, st, 0, name);
    add(pb, st, 0, ": damage in the middle: ");
    addn(pb, st, 0, size - end);
    add(pb, st, 0, " bytes after the last valid record (byte ");
    addn(pb, st, 0, end);
    add(pb, st, 0, ") are not a torn tail (");
    addn(pb, st, 0, rep[6]);
    add(pb, st, 0, " intact records start at byte ");
    addn(pb, st, 0, rep[5]);
    add(pb, st, 0, "): the service refuses to start on it");
    return 0;
}

// The number `events.first` holds, 0 if there is no such file.
fn first_segment[&c, &d](fs: &c Fs(""), dir: &d [byte]) -> [fs_read(""), file_read] int {
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
    }
    return k;
}

// How many segments there are from `first` up while the files exist.
fn count_segments[&c, &d](fs: &c Fs(""), dir: &d [byte], first: int) -> [fs_read(""), file_read] int {
    var n = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        var more = true;
        while more && n < 100000000 {
            let nm = store.seg_path(path, dir, first + n);
            if store.size_of(fs, path[0..nm]) >= 0 {
                n = n + 1;
            } else {
                more = false;
            }
        }
    }
    return n;
}

// The events log: every segment, in order.
fn check_events[&c, &d, &w, &s, &p](fs: &c Fs(""), dir: &d [byte], window: &!w [byte], st: &!s [int], pb: &!p [byte]) -> [fs_read(""), file_read] int {
    let first_k = first_segment(fs, dir);
    let count = count_segments(fs, dir, first_k);
    if count == 0 {
        problem_begin(pb, st);
        add(pb, st, 0, "the events log is missing (no events.seg, and none of the segments events.first names)");
        problem_end(pb, st);
        return 0;
    }
    st[3] = 1;
    st[7] = count;
    st[13] = 0 - 1;
    st[14] = 0 - 1;
    var i = 0;
    while i < count {
        let k = first_k + i;
        let last = i == count - 1;
        region a {
            let path = alloc_slice[a](2112, byte_of(0));
            let name = alloc_slice[a](64, byte_of(0));
            let rep = alloc_slice[a](logguard.rep_size(), 0);
            let nn = store.seg_name(name, k);
            let pn = store.seg_path(path, dir, k);
            match open_read(fs, path[0..pn]) {
                Opened::Failed(e) => {
                    problem_begin(pb, st);
                    add(pb, st, 0, name[0..nn]);
                    add(pb, st, 0, ": cannot be opened");
                    problem_end(pb, st);
                }
                Opened::Ok(f0) => {
                    var f = f0;
                    borrow mut f as &!fh in {
                        var size = 0;
                        match file_size(fh) {
                            Done::Ok(n) => {
                                size = n;
                            }
                            Done::Failed(e) => {
                                size = 0;
                            }
                        }
                        st[8] = st[8] + size;
                        var got = 0;
                        var want = size;
                        if want > 4096 {
                            want = 4096;
                        }
                        if want > 0 {
                            match file_pread(fh, 0, window[0..want]) {
                                Read::Got(n) => {
                                    got = n;
                                }
                                Read::End => {
                                }
                                Read::Failed(e) => {
                                }
                            }
                        }
                        let head = head_of(window, got);
                        // head: 0 cut, 1 no header, 2 header, 3 unknown
                        if head.0 == 3 {
                            problem_begin(pb, st);
                            add(pb, st, 0, name[0..nn]);
                            if head.6 == 1 {
                                add(pb, st, 0, ": the first record is not a header this code knows");
                            } else {
                                add(pb, st, 0, ": an unreadable format");
                            }
                            problem_end(pb, st);
                        } else if head.0 == 0 {
                            if last && k > first_k {
                                // a roll caught half-done: no events in it, ignored as the service ignores it
                                st[15] = 1;
                            } else if !last || k != 0 || size > 0 {
                                problem_begin(pb, st);
                                add(pb, st, 0, name[0..nn]);
                                add(pb, st, 0, ": no whole record");
                                problem_end(pb, st);
                            }
                        } else {
                            var hdr = 0;
                            var base = 0;
                            var first = 1;
                            var usable = true;
                            if head.0 == 2 {
                                hdr = head.1;
                                base = head.3;
                                first = head.4;
                                if head.5 != event_format() {
                                    problem_begin(pb, st);
                                    add(pb, st, 0, name[0..nn]);
                                    add(pb, st, 0, ": format ");
                                    addn(pb, st, 0, head.5);
                                    add(pb, st, 0, ", which this version does not read");
                                    problem_end(pb, st);
                                    usable = false;
                                } else if head.2 != k {
                                    problem_begin(pb, st);
                                    add(pb, st, 0, name[0..nn]);
                                    add(pb, st, 0, ": its header says it is segment ");
                                    addn(pb, st, 0, head.2);
                                    problem_end(pb, st);
                                }
                            } else if k != 0 {
                                problem_begin(pb, st);
                                add(pb, st, 0, name[0..nn]);
                                add(pb, st, 0, ": no header (only events.seg may be a format 1 segment)");
                                problem_end(pb, st);
                            }
                            if usable {
                                if st[13] >= 0 && (base != st[13] || first != st[14]) {
                                    problem_begin(pb, st);
                                    add(pb, st, 0, name[0..nn]);
                                    add(pb, st, 0, ": begins at offset ");
                                    addn(pb, st, 0, base);
                                    add(pb, st, 0, ", event ");
                                    addn(pb, st, 0, first);
                                    add(pb, st, 0, ", but the segment before it ends at offset ");
                                    addn(pb, st, 0, st[13]);
                                    add(pb, st, 0, ", event ");
                                    addn(pb, st, 0, st[14] - 1);
                                    problem_end(pb, st);
                                }
                                let scan = scan_events(fh, size, first, window, st);
                                let end = scan.1;
                                if scan.3 == 0 {
                                    problem_begin(pb, st);
                                    add(pb, st, 0, name[0..nn]);
                                    add(pb, st, 0, ": ids are not dense from ");
                                    addn(pb, st, 0, first);
                                    problem_end(pb, st);
                                }
                                if end < size {
                                    // A tail: a torn one or damage, by the service's rule.
                                    let verdict = logguard.inspect(fh, size, window, max_len(), rep);
                                    if !last {
                                        problem_begin(pb, st);
                                        add(pb, st, 0, name[0..nn]);
                                        add(pb, st, 0, ": a sealed segment ends in ");
                                        addn(pb, st, 0, size - end);
                                        add(pb, st, 0, " bytes that are not whole records");
                                        problem_end(pb, st);
                                    } else if verdict.0 == logguard.damage_refused() {
                                        problem_begin(pb, st);
                                        damage_text(pb, st, name[0..nn], size, end, rep);
                                        problem_end(pb, st);
                                    }
                                }
                                if last {
                                    st[10] = size - end;
                                }
                                if scan.2 > 0 && st[5] == 0 {
                                    st[5] = first;
                                }
                                if scan.2 > 0 {
                                    st[6] = scan.4;
                                }
                                st[13] = base + (end - hdr);
                                st[14] = first + scan.2;
                                st[4] = st[4] + scan.2;
                                st[9] = st[9] + end;
                            }
                        }
                    }
                    file_close(f);
                }
            }
        }
        i = i + 1;
    }
    if st[12] > 0 {
        problem_begin(pb, st);
        add(pb, st, 0, "the events log: ");
        addn(pb, st, 0, st[12]);
        add(pb, st, 0, " record(s) are not of a shape this version writes (event, typ, key, t: the pairs `event`, then `typ` if typed, then `key` and `t` if keyed): the service refuses to start on it (status 16)");
        problem_end(pb, st);
    }
    // What the log holds, for the pair check and the report.
    if st[5] == 0 {
        if st[14] >= 0 {
            st[5] = st[14];
        } else {
            st[5] = 1;
        }
        st[6] = st[5] - 1;
    }
    return 0;
}

// delivery.seg.
fn check_delivery[&c, &d, &w, &s, &p](fs: &c Fs(""), dir: &d [byte], window: &!w [byte], st: &!s [int], pb: &!p [byte]) -> [fs_read(""), file_read] int {
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let rep = alloc_slice[a](logguard.rep_size(), 0);
        let pn = store.path_join(path, dir, "delivery.seg");
        match open_read(fs, path[0..pn]) {
            Opened::Failed(e) => {
                // A service that never delivered has no delivery.seg.
                st[20] = 0;
            }
            Opened::Ok(f0) => {
                st[20] = 1;
                var f = f0;
                borrow mut f as &!fh in {
                    var size = 0;
                    match file_size(fh) {
                        Done::Ok(n) => {
                            size = n;
                        }
                        Done::Failed(e) => {
                            size = 0;
                        }
                    }
                    st[21] = size;
                    let scan = scan_delivery(fh, size, window, st, pb);
                    st[22] = scan.1;
                    st[23] = size - scan.1;
                    st[24] = scan.2;
                    if st[26] > 0 {
                        problem_begin(pb, st);
                        add(pb, st, 0, "delivery.seg: ");
                        addn(pb, st, 0, st[26]);
                        add(pb, st, 0, " record(s) are not outcomes this version writes");
                        problem_end(pb, st);
                    }
                    if scan.1 < size {
                        let verdict = logguard.inspect(fh, size, window, max_len(), rep);
                        if verdict.0 == logguard.damage_refused() {
                            problem_begin(pb, st);
                            damage_text(pb, st, "delivery.seg", size, scan.1, rep);
                            problem_end(pb, st);
                        }
                    }
                }
                file_close(f);
            }
        }
    }
    if st[3] == 1 && st[25] > st[6] {
        problem_begin(pb, st);
        add(pb, st, 0, "delivery.seg refers to event ");
        addn(pb, st, 0, st[25]);
        add(pb, st, 0, " but the events log ends at event ");
        addn(pb, st, 0, st[6]);
        add(pb, st, 0, ": restoring this pair would acknowledge new events under ids already recorded as delivered, and never deliver them");
        problem_end(pb, st);
    }
    return 0;
}

// ---------------------------------------------------------------------
// The report
// ---------------------------------------------------------------------

fn kv_line[&b, &s, &k](ob: &!b [byte], st: &!s [int], key: &k [byte], value: int) -> [] int {
    add(ob, st, 2, key);
    add(ob, st, 2, "=");
    addn(ob, st, 2, value);
    add(ob, st, 2, "\n");
    return 0;
}

fn json_key[&b, &s, &k](ob: &!b [byte], st: &!s [int], key: &k [byte], value: int, comma: bool) -> [] int {
    add(ob, st, 2, "\"");
    add(ob, st, 2, key);
    add(ob, st, 2, "\": ");
    addn(ob, st, 2, value);
    if comma {
        add(ob, st, 2, ", ");
    }
    return 0;
}

// The problems, each a JSON string, separated by commas.
fn json_problems[&b, &s, &p](ob: &!b [byte], st: &!s [int], pb: &p [byte]) -> [] int {
    var i = 0;
    var open = false;
    var first = true;
    let n = st[0];
    while i < n {
        if !open {
            if !first {
                add(ob, st, 2, ", ");
            }
            add(ob, st, 2, "\"");
            open = true;
            first = false;
        }
        let c = int_of(pb[i]);
        if c == 10 {
            add(ob, st, 2, "\"");
            open = false;
        } else if c == '"' || c == 92 {
            add(ob, st, 2, "\\");
            ob[st[2]] = pb[i];
            st[2] = st[2] + 1;
        } else {
            ob[st[2]] = pb[i];
            st[2] = st[2] + 1;
        }
        i = i + 1;
    }
    return 0;
}

fn report_kv[&b, &s](ob: &!b [byte], st: &!s [int]) -> [] int {
    kv_line(ob, st, "events_records", st[4]);
    if st[3] == 0 {
        kv_line(ob, st, "events_first_id", 1);
    } else {
        kv_line(ob, st, "events_first_id", st[5]);
    }
    kv_line(ob, st, "events_last_id", st[6]);
    kv_line(ob, st, "events_segments", st[7] - st[15]);
    kv_line(ob, st, "events_bytes", st[9]);
    kv_line(ob, st, "events_torn_bytes", st[10]);
    kv_line(ob, st, "delivery_records", st[24]);
    kv_line(ob, st, "delivery_max_event_ref", st[25]);
    kv_line(ob, st, "delivery_bytes", st[22]);
    kv_line(ob, st, "delivery_torn_bytes", st[23]);
    return 0;
}

fn report_json[&b, &s, &d, &p](ob: &!b [byte], st: &!s [int], dir: &d [byte], pb: &p [byte]) -> [] int {
    add(ob, st, 2, "{\n \"delivery\": {");
    if st[20] == 0 {
        json_key(ob, st, "bytes", 0, true);
        json_key(ob, st, "max_event_ref", 0, true);
        json_key(ob, st, "missing", 1, true);
        json_key(ob, st, "records", 0, true);
        json_key(ob, st, "torn_bytes", 0, true);
        json_key(ob, st, "valid_bytes", 0, false);
    } else {
        json_key(ob, st, "bytes", st[21], true);
        json_key(ob, st, "max_event_ref", st[25], true);
        add(ob, st, 2, "\"kinds\": {");
        var k = 1;
        var any = false;
        while k <= 19 {
            if st[30 + k] > 0 {
                if any {
                    add(ob, st, 2, ", ");
                }
                add(ob, st, 2, "\"");
                addn(ob, st, 2, k);
                add(ob, st, 2, "\": ");
                addn(ob, st, 2, st[30 + k]);
                any = true;
            }
            k = k + 1;
        }
        add(ob, st, 2, "}, \"reasons\": {");
        var r = 0;
        any = false;
        while r <= 27 {
            if st[60 + r] > 0 {
                if any {
                    add(ob, st, 2, ", ");
                }
                add(ob, st, 2, "\"");
                if r == 27 {
                    add(ob, st, 2, "unknown");
                } else {
                    add(ob, st, 2, reason_name(r));
                }
                add(ob, st, 2, "\": ");
                addn(ob, st, 2, st[60 + r]);
                any = true;
            }
            r = r + 1;
        }
        add(ob, st, 2, "}, ");
        json_key(ob, st, "records", st[24], true);
        json_key(ob, st, "torn_bytes", st[23], true);
        json_key(ob, st, "valid_bytes", st[22], false);
    }
    add(ob, st, 2, "},\n \"dir\": \"");
    add(ob, st, 2, dir);
    add(ob, st, 2, "\",\n \"events\": ");
    if st[3] == 0 {
        add(ob, st, 2, "null");
    } else {
        add(ob, st, 2, "{");
        json_key(ob, st, "bytes", st[8], true);
        json_key(ob, st, "first_id", st[5], true);
        json_key(ob, st, "last_id", st[6], true);
        json_key(ob, st, "records", st[4], true);
        json_key(ob, st, "segments", st[7] - st[15], true);
        json_key(ob, st, "torn_bytes", st[10], true);
        json_key(ob, st, "typed", st[11], true);
        json_key(ob, st, "valid_bytes", st[9], false);
        add(ob, st, 2, "}");
    }
    add(ob, st, 2, ",\n \"ok\": ");
    if st[1] == 0 {
        add(ob, st, 2, "true");
    } else {
        add(ob, st, 2, "false");
    }
    add(ob, st, 2, ",\n \"problems\": [");
    json_problems(ob, st, pb);
    add(ob, st, 2, "]\n}\n");
    return 0;
}

fn usage[&i](io: &!i Io) -> [err_write] int {
    io.error_all(io, "usage: hooks-logcheck check DIR [--kv]\n       hooks-logcheck trim FILE\n  check reads the logs of a data directory without changing them; exit 0 if they are a consistent pair, 1 if not, 2 for a usage error\n  trim cuts FILE, a COPY of a log (never a live file), to its valid prefix; it refuses damage in the middle (exit 1)\n");
    return 2;
}

// `trim FILE` (`logcheck.py`'s): cut a copy of a log to the end of its last whole record, if what follows is a torn tail; refuse if it is damage. Prints
// `{"file": ..., "valid_bytes": ..., "cut": ...}`. Answers the exit status.
fn trim[&c, &i, &w, &s, &p](fs: &c Fs(""), out: &!i Io, path: &p [byte], window: &!w [byte], st: &!s [int]) -> [fs_read(""), fs_write(""), file_read, file_write, err_write] int {
    var end = 0;
    var size = 0;
    var verdict = segment.unreadable();
    var found = false;
    var intact = 0;
    var intact_at = 0 - 1;
    region a {
        let rep = alloc_slice[a](logguard.rep_size(), 0);
        match open_read(fs, path) {
            Opened::Failed(e) => {
                found = false;
            }
            Opened::Ok(f0) => {
                found = true;
                var f = f0;
                borrow mut f as &!fh in {
                    match file_size(fh) {
                        Done::Ok(n) => {
                            size = n;
                            let v = logguard.inspect(fh, size, window, max_len(), rep);
                            verdict = v.0;
                            end = rep[1];
                            intact = rep[6];
                            intact_at = rep[5];
                        }
                        Done::Failed(e) => {
                            verdict = segment.unreadable();
                        }
                    }
                }
                file_close(f);
            }
        }
    }
    if !found {
        io.error_all(out, "logcheck: ");
        io.error_all(out, path);
        io.error_all(out, ": no such file\n");
        return 2;
    }
    if verdict == segment.unreadable() {
        io.error_all(out, "logcheck: ");
        io.error_all(out, path);
        io.error_all(out, ": cannot be read\n");
        return 2;
    }
    if verdict == logguard.damage_refused() {
        io.error_all(out, "logcheck: ");
        io.error_all(out, path);
        io.error_all(out, ": ");
        var tmp = 0;
        region b {
            let num = alloc_slice[b](24, byte_of(0));
            tmp = store.nat_text(num, 0, size - end);
            io.error_all(out, num[0..tmp]);
            io.error_all(out, " bytes after the last valid record (byte ");
            tmp = store.nat_text(num, 0, end);
            io.error_all(out, num[0..tmp]);
        }
        io.error_all(out, ") are not a torn tail: damage in the middle, not trimming it\n");
        return 1;
    }
    if end < size {
        var cut_ok = false;
        match open_rw(fs, path) {
            Opened::Failed(e) => {
                cut_ok = false;
            }
            Opened::Ok(g0) => {
                var g = g0;
                borrow mut g as &!gh in {
                    match file_truncate(gh, end) {
                        Done::Ok(n) => {
                            match file_sync(gh) {
                                Done::Ok(m) => {
                                    cut_ok = true;
                                }
                                Done::Failed(e) => {
                                    cut_ok = false;
                                }
                            }
                        }
                        Done::Failed(e) => {
                            cut_ok = false;
                        }
                    }
                }
                file_close(g);
            }
        }
        if !cut_ok {
            io.error_all(out, "logcheck: the file could not be cut\n");
            return 2;
        }
    }
    st[27] = end;
    st[28] = size - end;
    return 0;
}

// The JSON line `trim` prints.
fn trim_json[&b, &s, &p](ob: &!b [byte], st: &!s [int], path: &p [byte], valid: int, cut: int) -> [] int {
    add(ob, st, 2, "{\"file\": \"");
    add(ob, st, 2, path);
    add(ob, st, 2, "\", \"valid_bytes\": ");
    addn(ob, st, 2, valid);
    add(ob, st, 2, ", \"cut\": ");
    addn(ob, st, 2, cut);
    add(ob, st, 2, "}\n");
    return 0;
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock, signals } = split(world);
    release(ffi);
    release(net);
    release(clock);
    release(signals);
    var status = 2;
    var h = heap;
    var o = io;
    borrow args as &a in {
        let n = arg_count(a);
        var kv = false;
        // 1 check, 2 trim, 0 a usage error
        var mode = 0;
        if (n == 3 || n == 4) && bytes.equal(arg(a, 1), "check") {
            mode = 1;
            if n == 4 {
                if bytes.equal(arg(a, 3), "--kv") {
                    kv = true;
                } else {
                    mode = 0;
                }
            }
        } else if n == 3 && bytes.equal(arg(a, 1), "trim") {
            mode = 2;
        }
        if mode == 0 {
            borrow mut o as &!oo in {
                status = usage(oo);
            }
        } else {
            borrow fs as &fh in {
                borrow mut h as &!hp in {
                    let window = box_slice(hp, window_size(), byte_of(0));
                    let pbox = box_slice(hp, text_size(), byte_of(0));
                    let obox = box_slice(hp, out_size(), byte_of(0));
                    let stbox = box_slice(hp, st_size(), 0);
                    borrow mut window as &!ww in {
                        borrow mut pbox as &!pw in {
                            borrow mut obox as &!ow in {
                                borrow mut stbox as &!sw in {
                                    let st = contents(sw);
                                    let wn = contents(ww);
                                    let pb = contents(pw);
                                    let ob = contents(ow);
                                    if mode == 2 {
                                        borrow mut o as &!oo in {
                                            status = trim(fh, oo, arg(a, 2), wn, st);
                                            if status == 0 {
                                                trim_json(ob, st, arg(a, 2), st[27], st[28]);
                                                io.write_all(oo, ob[0..st[2]]);
                                            }
                                        }
                                    } else {
                                        check_events(fh, arg(a, 2), wn, st, pb);
                                        check_delivery(fh, arg(a, 2), wn, st, pb);
                                        if kv {
                                            report_kv(ob, st);
                                        } else {
                                            report_json(ob, st, arg(a, 2), pb);
                                        }
                                        borrow mut o as &!oo in {
                                            io.write_all(oo, ob[0..st[2]]);
                                            if kv && st[0] > 0 {
                                                // each problem on stderr, a line starting `problem: `
                                                var i = 0;
                                                var from = 0;
                                                while i < st[0] {
                                                    if int_of(pb[i]) == 10 {
                                                        io.error_all(oo, "problem: ");
                                                        io.error_all(oo, pb[from..i + 1]);
                                                        from = i + 1;
                                                    }
                                                    i = i + 1;
                                                }
                                            }
                                        }
                                        if st[1] == 0 {
                                            status = 0;
                                        } else {
                                            status = 1;
                                        }
                                    }
                                }
                            }
                        }
                    }
                    unbox_slice(hp, window);
                    unbox_slice(hp, pbox);
                    unbox_slice(hp, obox);
                    unbox_slice(hp, stbox);
                }
            }
        }
    }
    release(args);
    release(fs);
    release(o);
    release(h);
    return status;
}
