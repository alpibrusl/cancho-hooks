edition 5;

module ops;

import std.io;

// `ops` -- what an operator needs to watch the service and to stop it (`docs/design.md` section 34).
//
//   * the counters `/metrics` shows and `/stats` does not (ingest, group commits, why attempts failed), in one array of integers, `size()` of them, which
//     the caller keeps at the end of the delivery state;
//   * whether the service is ready (`GET /readyz`): the logs open and writable, the database reachable, not on its way out;
//   * how the program learns it has been asked to stop. lex-sys has no builtin for signals; libc has, and `Ffi("libc")` reaches it (the authority
//     report says so: four functions, listed below). A handler cannot do anything useful (a callback must have an empty effect row), so the program
//     does not install one: it **blocks** `SIGTERM` and `SIGINT` (`sigblock`), looks at the set of signals that are pending once a turn of the loop
//     (`sigpending`), and when it sees one it ignores and then restores the default disposition of both and unblocks them, which discards the
//     pending signal and makes the next one kill the process at once: the second signal of "a second signal exits at once". The set is read by a
//     system call a turn, which costs nothing measurable, and the latency of a stop is the loop's wait (50 ms at most).
//
// The numbers are counted since this start, like the ones in `/stats`.

// ---------------------------------------------------------------------
// The counters
// ---------------------------------------------------------------------

fn o_stop() -> [] int {
    return 0;
}

fn o_signal() -> [] int {
    return 1;
}

fn o_stop_at() -> [] int {
    return 2;
}

fn o_deadline() -> [] int {
    return 3;
}

fn o_started() -> [] int {
    return 4;
}

fn o_accepted() -> [] int {
    return 5;
}

fn o_duplicate() -> [] int {
    return 6;
}

fn o_refused() -> [] int {
    return 7;
}

// Refused POST /events by status: 400, 413, 422, 503, 507, then any other.
// The settings of this part that `GET /config` shows (the service sets them at start): the stop deadline in ms, and whether `repair-logs` was given.
fn o_stop_ms() -> [] int {
    return 22;
}

fn o_repair() -> [] int {
    return 23;
}

fn o_by_status() -> [] int {
    return 8;
}

pub fn statuses() -> [] int {
    return 6;
}

// Two integers: the bytes of the events log and of the delivery log that a flush last covered, as the loop last looked.
fn o_synced() -> [] int {
    return 16;
}

// Two integers: group commits of the events log and of the delivery log.
fn o_commits() -> [] int {
    return 18;
}

// 1: the data directory took a write at the last probe; 0: it did not; -1: no probe has run.
fn o_probe() -> [] int {
    return 20;
}

fn o_probe_at() -> [] int {
    return 21;
}

// How many probes have been made: its parity picks which of the two probe files the next one writes.
fn o_probe_round() -> [] int {
    return 41;
}

// A failed attempt for each reason (`reason.ls`): `reason.count()` integers.
fn o_reasons() -> [] int {
    return 24;
}

// The reason of the last failed attempt of the endpoint in each slot, 0 if its last attempt delivered or none has failed: 62 integers.
fn o_last() -> [] int {
    return 48;
}

fn slots() -> [] int {
    return 62;
}

pub fn size() -> [] int {
    return 112;
}

pub fn init[&o](o: &!o [int]) -> [] int {
    var i = 0;
    while i < size() {
        o[i] = 0;
        i = i + 1;
    }
    o[o_probe()] = 0 - 1;
    return 0;
}

// The loop is about to start: when (Unix ms), and how far each log is durable already, which is not a commit of this start.
pub fn begin[&o](o: &!o [int], now_ms: int, events_synced: int, delivery_synced: int) -> [] int {
    o[o_started()] = now_ms;
    o[o_synced()] = events_synced;
    o[o_synced() + 1] = delivery_synced;
    return 0;
}

pub fn set_settings[&o](o: &!o [int], stop_ms: int, repair: bool) -> [] int {
    o[o_stop_ms()] = stop_ms;
    o[o_repair()] = 0;
    if repair {
        o[o_repair()] = 1;
    }
    return 0;
}

pub fn stop_deadline[&o](o: &o [int]) -> [] int {
    return o[o_stop_ms()];
}

pub fn repair_flag[&o](o: &o [int]) -> [] int {
    return o[o_repair()];
}

pub fn started[&o](o: &o [int]) -> [] int {
    return o[o_started()];
}

// ---- ingest

// An event was stored and acknowledged (after the flush), or an idempotent repeat was acknowledged, or a request to store one was refused.
pub fn accepted[&o](o: &!o [int]) -> [] int {
    o[o_accepted()] = o[o_accepted()] + 1;
    return 0;
}

pub fn duplicate[&o](o: &!o [int]) -> [] int {
    o[o_duplicate()] = o[o_duplicate()] + 1;
    return 0;
}

fn status_cell(status: int) -> [] int {
    if status == 400 {
        return 0;
    }
    if status == 413 {
        return 1;
    }
    if status == 422 {
        return 2;
    }
    if status == 503 {
        return 3;
    }
    if status == 507 {
        return 4;
    }
    return 5;
}

pub fn refused[&o](o: &!o [int], status: int) -> [] int {
    o[o_refused()] = o[o_refused()] + 1;
    let c = o_by_status() + status_cell(status);
    o[c] = o[c] + 1;
    return 0;
}

pub fn accepted_count[&o](o: &o [int]) -> [] int {
    return o[o_accepted()];
}

pub fn duplicate_count[&o](o: &o [int]) -> [] int {
    return o[o_duplicate()];
}

pub fn refused_count[&o](o: &o [int]) -> [] int {
    return o[o_refused()];
}

// The status of cell `i` of `refused_by`, and how many.
pub fn refused_status(i: int) -> [] int {
    if i == 0 {
        return 400;
    }
    if i == 1 {
        return 413;
    }
    if i == 2 {
        return 422;
    }
    if i == 3 {
        return 503;
    }
    if i == 4 {
        return 507;
    }
    return 0;
}

pub fn refused_by[&o](o: &o [int], i: int) -> [] int {
    return o[o_by_status() + i];
}

// ---- group commits

// The loop looks, once a turn, at how far each log is durable (`log.synced`): if it moved, a flush covered something this turn. `which` is 0 for
// the events log, 1 for the delivery log. A turn that flushed twice counts once: this counts commits, as the log's reader sees them.
pub fn look_at_log[&o](o: &!o [int], which: int, synced: int) -> [] int {
    if synced != o[o_synced() + which] {
        o[o_synced() + which] = synced;
        o[o_commits() + which] = o[o_commits() + which] + 1;
    }
    return 0;
}

pub fn commits[&o](o: &o [int], which: int) -> [] int {
    return o[o_commits() + which];
}

// ---- why attempts fail

// An attempt for endpoint slot `e` ended with reason `r` (0 for a delivery).
pub fn attempt_ended[&o](o: &!o [int], e: int, r: int) -> [] int {
    if r > 0 && r < 17 {
        o[o_reasons() + r] = o[o_reasons() + r] + 1;
    }
    if e >= 0 && e < slots() {
        o[o_last() + e] = r;
    }
    return 0;
}

// What recovery learned: the last reason recorded for the endpoint in slot `e`.
pub fn set_last_reason[&o](o: &!o [int], e: int, r: int) -> [] int {
    if e >= 0 && e < slots() {
        o[o_last() + e] = r;
    }
    return 0;
}

pub fn last_reason[&o](o: &o [int], e: int) -> [] int {
    return o[o_last() + e];
}

pub fn failures_for[&o](o: &o [int], r: int) -> [] int {
    return o[o_reasons() + r];
}

// ---------------------------------------------------------------------
// Readiness
// ---------------------------------------------------------------------

// Can the data directory be written? The loop asks once a second (`probe_due`) by writing a byte to a file there; a log whose handle is open
// keeps taking writes after the directory's permissions change, and only a read-only remount or a full disk shows, and then on the next write.
pub fn probe_due[&o](o: &o [int], now_ms: int) -> [] bool {
    return o[o_probe()] < 0 || now_ms - o[o_probe_at()] >= 1000 || now_ms < o[o_probe_at()];
}

pub fn probe_round[&o](o: &o [int]) -> [] int {
    return o[o_probe_round()];
}

pub fn probe_set[&o](o: &!o [int], ok: bool, now_ms: int) -> [] int {
    o[o_probe_at()] = now_ms;
    o[o_probe_round()] = o[o_probe_round()] + 1;
    o[o_probe()] = 0;
    if ok {
        o[o_probe()] = 1;
    }
    return 0;
}

pub fn probe_ok[&o](o: &o [int]) -> [] bool {
    return o[o_probe()] != 0;
}

fn path_into[&d, &n, &w](out: &!w [byte], dir: &d [byte], name: &n [byte]) -> [] int {
    var i = 0;
    while i < len(dir) {
        out[i] = dir[i];
        i = i + 1;
    }
    out[i] = byte_of('/');
    var j = 0;
    while j < len(name) {
        out[i + 1 + j] = name[j];
        j = j + 1;
    }
    return i + 1 + len(name);
}

// Write one byte to a new file in the data directory, `<dir>/.writable-0` or `-1` by turns, and then remove the other one. True if the byte was taken. Two files, because a
// file that is rewritten in place gives back the block it needs when it is truncated, and a full disk would then take the probe: the new file needs a block of its own while
// the old one still holds its.
pub fn probe_write[&f, &d](fs: &f Fs(""), dir: &d [byte], round: int) -> [fs_write("")] bool {
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        let other_buf = alloc_slice[a](4096, byte_of(0));
        let path = path_buf[0..path_into(path_buf, dir, probe_name(round % 2))];
        let other = other_buf[0..path_into(other_buf, dir, probe_name(1 - round % 2))];
        let one = alloc_slice[a](1, byte_of('1'));
        // (Compared directly, `fs_write(..) == 1` makes the LLVM backend fail with "cannot determine the scalar kind"; see docs/design.md section 34.9.)
        let wrote = fs_write(fs, path, one);
        if wrote != 1 {
            return false;
        }
        match fs_remove(fs, other) {
            Done::Ok(n) => {
            }
            Done::Failed(e) => {
            }
        }
        return true;
    }
}

fn probe_name(which: int) -> [] &static [byte] {
    if which == 0 {
        return ".writable-0";
    }
    return ".writable-1";
}

// Take both probe files away (at a clean stop).
pub fn probe_clean[&f, &d](fs: &f Fs(""), dir: &d [byte]) -> [fs_write("")] int {
    region a {
        let path_buf = alloc_slice[a](4096, byte_of(0));
        var which = 0;
        while which < 2 {
            let path = path_buf[0..path_into(path_buf, dir, probe_name(which))];
            match fs_remove(fs, path) {
                Done::Ok(n) => {
                }
                Done::Failed(e) => {
                }
            }
            which = which + 1;
        }
    }
    return 0;
}

// Why the service is not ready, as a number: 0 if it is.
//   1 it was asked to stop      2 the events log is broken      3 the delivery log is broken
//   4 the data directory does not take a write      5 a database was named and no connection to it is live
pub fn not_ready[&o](o: &o [int], events_broken: bool, delivery_broken: bool, database: bool, database_live: int) -> [] int {
    if o[o_stop()] != 0 {
        return 1;
    }
    if events_broken {
        return 2;
    }
    if delivery_broken {
        return 3;
    }
    if o[o_probe()] == 0 {
        return 4;
    }
    if database && database_live < 1 {
        return 5;
    }
    return 0;
}

// The short name of the check that failed, for a machine to read.
pub fn check_name(code: int) -> [] &static [byte] {
    if code == 1 {
        return "stopping";
    }
    if code == 2 {
        return "events_log";
    }
    if code == 3 {
        return "delivery_log";
    }
    if code == 4 {
        return "data_dir";
    }
    if code == 5 {
        return "database";
    }
    return "";
}

// The scope `GET /metrics` needs: 0 is open. Production item 0.3 (scoped tokens) changes this one number to its read scope, and the handler
// of the route (`id == 41` in `hooks.ls`) is where that scope is checked.
pub fn scope_metrics() -> [] int {
    return 0;
}

pub fn why_not(code: int) -> [] &static [byte] {
    if code == 1 {
        return "the service is stopping";
    }
    if code == 2 {
        return "the events log is broken: a write or a flush failed, and it is not retried; free the disk and restart the service";
    }
    if code == 3 {
        return "the delivery log is broken: a write or a flush failed, and it is not retried; free the disk and restart the service";
    }
    if code == 4 {
        return "the data directory does not take a write (read-only, or the disk is full)";
    }
    if code == 5 {
        return "the database is named and no connection to it is live; a lost connection is not reopened, restart the service";
    }
    return "";
}

// ---------------------------------------------------------------------
// Stopping
// ---------------------------------------------------------------------

pub fn sigint() -> [] int {
    return 2;
}

pub fn sigterm() -> [] int {
    return 15;
}

// libc's `int sigblock(int mask)`: block the signals whose bits (signal - 1) are set; answers the old mask. `signal(2, handler)` with a handler of 0
// (`SIG_DFL`) or 1 (`SIG_IGN`) takes an integer where C takes a pointer, which is the same width on both of the targets. `sigpending` fills a set;
// its length crosses as a second argument that C does not read. `sigsetmask(0)` unblocks everything.
extern fn sigblock[&f](ffi: &f Ffi("libc"), mask: int) -> [ffi("libc")] int;

extern fn sigsetmask[&f](ffi: &f Ffi("libc"), mask: int) -> [ffi("libc")] int;

extern fn sigpending[&f, &b](ffi: &f Ffi("libc"), set: &!b [byte]) -> [ffi("libc")] int;

extern fn signal[&f](ffi: &f Ffi("libc"), signum: int, handler: int) -> [ffi("libc")] int;

// Hold `SIGINT` and `SIGTERM` (bits 1 and 14) until `pending_signal` looks.
pub fn hold_signals[&f](libc: &f Ffi("libc")) -> [ffi("libc")] int {
    return sigblock(libc, 16386);
}

// The signal that is waiting, 15 or 2, or 0. `set` is at least 128 bytes (`sigset_t` is that big on Linux; the mask is the first eight bytes).
pub fn pending_signal[&f, &b](libc: &f Ffi("libc"), set: &!b [byte]) -> [ffi("libc")] int {
    if sigpending(libc, set) != 0 {
        return 0;
    }
    if int_of(set[1]) & 64 != 0 {
        return sigterm();
    }
    if int_of(set[0]) & 2 != 0 {
        return sigint();
    }
    return 0;
}

// The first signal has been seen. Ignore both signals (which throws away the one that is pending), then give both back their default action and
// unblock them: the next `SIGTERM` or `SIGINT` ends the process at once.
pub fn second_signal_kills[&f](libc: &f Ffi("libc")) -> [ffi("libc")] int {
    signal(libc, 15, 1);
    signal(libc, 2, 1);
    signal(libc, 15, 0);
    signal(libc, 2, 0);
    sigsetmask(libc, 0);
    return 0;
}

pub fn stopping[&o](o: &o [int]) -> [] bool {
    return o[o_stop()] != 0;
}

// Begin the drain: the signal that asked, when (monotonic ms), and by when the attempts on the wire must have ended.
pub fn begin_stop[&o](o: &!o [int], sig: int, now_ms: int, deadline_ms: int) -> [] int {
    o[o_stop()] = 1;
    o[o_signal()] = sig;
    o[o_stop_at()] = now_ms;
    o[o_deadline()] = now_ms + deadline_ms;
    return 0;
}

pub fn stop_signal[&o](o: &o [int]) -> [] int {
    return o[o_signal()];
}

// Has the drain run out of time?
pub fn deadline_passed[&o](o: &o [int], now_ms: int) -> [] bool {
    return o[o_stop()] != 0 && now_ms >= o[o_deadline()];
}

// ---------------------------------------------------------------------
// Saying so
// ---------------------------------------------------------------------

pub fn say[&i, &t](out: &!i Io, text: &t [byte]) -> [err_write] int {
    return io.error_all(out, text);
}

fn digits[&b](n: int, buf: &!b [byte]) -> [] int {
    var width = 1;
    var t = n;
    while t >= 10 {
        t = t / 10;
        width = width + 1;
    }
    var k = width;
    var m = n;
    while k > 0 {
        buf[k - 1] = byte_of('0' + m % 10);
        m = m / 10;
        k = k - 1;
    }
    return width;
}

pub fn say_number[&i](out: &!i Io, n: int) -> [err_write] int {
    region a {
        let nb = alloc_slice[a](24, byte_of(0));
        if n < 0 {
            say(out, "-");
            say(out, nb[0..digits(0 - n, nb)]);
        } else {
            say(out, nb[0..digits(n, nb)]);
        }
    }
    return 0;
}

// The request body for a refused request while the service is stopping.
pub fn stopping_message() -> [] &static [byte] {
    return "the service is stopping";
}
