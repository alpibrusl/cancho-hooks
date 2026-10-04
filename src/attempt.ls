edition 5;

module attempt;

import std.conns;

// `attempt` -- delivery attempts that do not hold the loop (`docs/design.md` section 16).
//
// An attempt is a small state machine: *connecting* (a connection started with `tcp_connect_start`, watched for writable),
// *sending* (the request, written as the kernel takes it), *reading* (the status line, as it arrives). Each state waits for
// the poller, so any number of attempts, up to `slots()`, are in flight together and none of them blocks the caller. The
// connections live in a `std.conns` table and are watched on the poller of the server that owns the loop, under tokens
// from `token0` up; the table's slot number is the attempt's slot.
//
// This module knows nothing about events, endpoints or retries: it dials, sends bytes, reads a status line, and answers
// what happened. What the answer means is `hooks.ls`'s.
//
// An attempt's per-slot numbers are `stride()` integers in one array, `at`: state, endpoint, event, deadline (`clock_ms`),
// bytes sent, response bytes held, request length. The request bytes are `req_max()` bytes per slot in one byte array; the
// part of the response kept is `resp_max()` bytes per slot, enough for `HTTP/1.1 NNN`.

pub fn slots() -> [] int {
    return 64;
}

pub fn req_max() -> [] int {
    return 66560;
}

pub fn resp_max() -> [] int {
    return 16;
}

fn stride() -> [] int {
    return 8;
}

pub fn at_size() -> [] int {
    return slots() * 8;
}

pub fn req_size() -> [] int {
    return slots() * 66560;
}

pub fn resp_size() -> [] int {
    return slots() * 16;
}

// The answers an attempt ends with: an HTTP status (100 and up), or one of these. The first four are the coarse reasons that `attempts.status`
// has always held; the others say more and are what `reason.ls` turns into the reason an attempt failed (`docs/design.md` section 34). Each of them
// has its coarse one (`reason.legacy_status`), so that the history table's `status` column means what it did.
pub fn pending() -> [] int {
    return 0 - 100;
}

pub fn no_connect() -> [] int {
    return 0 - 1;
}

pub fn no_send() -> [] int {
    return 0 - 2;
}

pub fn timed_out() -> [] int {
    return 0 - 3;
}

pub fn no_answer() -> [] int {
    return 0 - 4;
}

// The connection was refused (`ECONNREFUSED`): nothing listens there.
pub fn refused() -> [] int {
    return 0 - 5;
}

// The deadline passed while the connection was being made (a host that does not answer the SYN), or the kernel gave up (`ETIMEDOUT`).
pub fn connect_timed_out() -> [] int {
    return 0 - 6;
}

// The deadline passed while the request was being written: the receiver does not read.
pub fn send_timed_out() -> [] int {
    return 0 - 7;
}

// The deadline passed after the request was sent and before a status line came: the receiver is silent.
pub fn response_timed_out() -> [] int {
    return 0 - 8;
}

// The connection was reset (or failed) while the status line was awaited.
pub fn reset() -> [] int {
    return 0 - 9;
}

// The receiver closed the connection without a status line.
pub fn closed_early() -> [] int {
    return 0 - 10;
}

// Twelve bytes that are not `HTTP/1.x NNN`.
pub fn bad_response() -> [] int {
    return 0 - 11;
}

// All `slots()` connections were in use: the attempt was not made.
pub fn no_slot() -> [] int {
    return 0 - 12;
}

// The request does not fit the slot's buffer: the attempt was not made.
pub fn too_large() -> [] int {
    return 0 - 13;
}

// `ECONNREFUSED` and `ETIMEDOUT` on Linux and macOS alike are not the same number on both; the two programs this runs on say Linux.
fn econnrefused() -> [] int {
    return 111;
}

fn etimedout() -> [] int {
    return 110;
}

fn connecting() -> [] int {
    return 1;
}

fn sending() -> [] int {
    return 2;
}

fn reading() -> [] int {
    return 3;
}

pub fn busy[&a](at: &a [int], slot: int) -> [] bool {
    return slot >= 0 && slot < slots() && at[slot * stride()] != 0;
}

pub fn endpoint_of[&a](at: &a [int], slot: int) -> [] int {
    return at[slot * stride() + 1];
}

pub fn event_of[&a](at: &a [int], slot: int) -> [] int {
    return at[slot * stride() + 2];
}

// When the attempt in `slot` must end by (`clock_ms`).
pub fn deadline_of[&a](at: &a [int], slot: int) -> [] int {
    return at[slot * stride() + 3];
}

// Has the attempt in `slot` run past its deadline (`clock_ms`)?
pub fn expired[&a](at: &a [int], slot: int, now: int) -> [] bool {
    return busy(at, slot) && now >= at[slot * stride() + 3];
}

// The reason the attempt in `slot` ended at its deadline, by where it was when the deadline passed. (Asked before `finish`.)
pub fn timeout_of[&a](at: &a [int], slot: int) -> [] int {
    let state = at[slot * stride()];
    if state == connecting() {
        return connect_timed_out();
    }
    if state == sending() {
        return send_timed_out();
    }
    return response_timed_out();
}

// The status code from `HTTP/1.x NNN ...` in the first `n` bytes of `head`, or -1.
pub fn status_of[&h](head: &h [byte], n: int) -> [] int {
    if n < 12 || int_of(head[0]) != 'H' || int_of(head[1]) != 'T' || int_of(head[2]) != 'T' || int_of(head[3]) != 'P' || int_of(head[4]) != '/' || int_of(head[5]) != '1' || int_of(head[6]) != '.' || int_of(head[8]) != ' ' {
        return 0 - 1;
    }
    var code = 0;
    var i = 9;
    while i < 12 {
        let c = int_of(head[i]);
        if c < '0' || c > '9' {
            return 0 - 1;
        }
        code = code * 10 + (c - '0');
        i = i + 1;
    }
    return code;
}

// Start an attempt: copy `request` into the slot's buffer, dial `host:port` without waiting, and watch the connection for
// writable under token `token0 + slot`. Answers the table and `(slot, code)`: `slot` is where the attempt lives, or -1 if it
// could not be started, in which case `code` says why (`no_connect()` for a connection that failed at once or could not be
// watched, `no_send()` for a request too large for the slot; and nothing is left to clean up). With `slot >= 0` the code is
// `pending()`.
pub fn begin[&h, &n, &q, &r, &a, &p, &e](heap: &!h Heap, tab: conns.Table, poller: &!p Poller, net: &n Net(""), host: &q [byte], port: int, request: &e [byte], at: &!a [int], req: &!r [byte], token0: int, endpoint: int, id: int, deadline: int) -> [heap, net_out(""), poll] (conns.Table, int, int) {
    var held = 0;
    borrow tab as &tt in {
        held = conns.live(tt);
    }
    if held >= slots() {
        return (tab, 0 - 1, no_slot());
    }
    if len(request) > req_max() {
        return (tab, 0 - 1, too_large());
    }
    match tcp_connect_start(net, host, port) {
        Dialed::Failed(err) => {
            return (tab, 0 - 1, no_connect());
        }
        Dialed::Ok(c) => {
            let (grown, slot) = conns.put(heap, tab, c);
            var table = grown;
            if slot < 0 || slot >= slots() {
                // `put` could not ticket the connection (and closed it), or the table is larger than the arrays: leave.
                if slot >= 0 {
                    borrow mut table as &!ct in {
                        conns.close(ct, slot);
                    }
                }
                return (table, 0 - 1, no_connect());
            }
            var watched = 0 - 1;
            borrow mut table as &!ct in {
                watched = conns.watch(ct, poller, slot, token0 + slot, 2);
                if watched != 0 {
                    conns.close(ct, slot);
                }
            }
            if watched != 0 {
                return (table, 0 - 1, no_connect());
            }
            var i = 0;
            while i < len(request) {
                req[slot * req_max() + i] = request[i];
                i = i + 1;
            }
            let b = slot * stride();
            at[b] = connecting();
            at[b + 1] = endpoint;
            at[b + 2] = id;
            at[b + 3] = deadline;
            at[b + 4] = 0;
            at[b + 5] = 0;
            at[b + 6] = len(request);
            return (table, slot, pending());
        }
    }
}

// Move the attempt in `slot` along, after the poller reported its connection (`ready`: 1 readable, 2 writable). Answers
// `pending()` while it waits for more, otherwise how it ended: an HTTP status, or `no_connect()`, `no_send()`, `no_answer()`.
// The caller then finishes the slot.
pub fn advance[&t, &p, &a, &r, &s](tab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], slot: int, token0: int) -> [conn_read, conn_write, poll] int {
    let b = slot * stride();
    var progress = true;
    while progress {
        progress = false;
        if at[b] == connecting() {
            let failed = conns.connect_status(tab, slot);
            if failed == econnrefused() {
                return refused();
            }
            if failed == etimedout() {
                return connect_timed_out();
            }
            if failed != 0 {
                return no_connect();
            }
            at[b] = sending();
            progress = true;
        } else if at[b] == sending() {
            let total = at[b + 6];
            let base = slot * req_max();
            match conns.write(tab, slot, req[base + at[b + 4]..base + total]) {
                Sent::Wrote(k) => {
                    at[b + 4] = at[b + 4] + k;
                    if at[b + 4] >= total {
                        at[b] = reading();
                        if conns.rewatch(tab, poller, slot, token0 + slot, 1) != 0 {
                            return no_send();
                        }
                    }
                    progress = true;
                }
                Sent::Again => {
                    return pending();
                }
                Sent::Failed(err) => {
                    return no_send();
                }
            }
        } else if at[b] == reading() {
            let base = slot * resp_max();
            match conns.read(tab, slot, resp[base + at[b + 5]..base + resp_max()]) {
                Received::Data(k) => {
                    at[b + 5] = at[b + 5] + k;
                    let code = status_of(resp[base..base + resp_max()], at[b + 5]);
                    if code >= 100 {
                        return code;
                    }
                    if at[b + 5] >= 12 {
                        return bad_response();
                    }
                    progress = true;
                }
                Received::End => {
                    return closed_early();
                }
                Received::Again => {
                    return pending();
                }
                Received::Failed(err) => {
                    return reset();
                }
            }
        }
    }
    return pending();
}

// End the attempt in `slot`: close its connection and free the slot.
pub fn finish[&t, &a](tab: &!t conns.Table, at: &!a [int], slot: int) -> [] int {
    conns.close(tab, slot);
    at[slot * stride()] = 0;
    return 0;
}
