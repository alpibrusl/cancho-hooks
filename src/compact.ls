edition 5;

// `compact` -- retention in the loop (`docs/retention.md`): which events may go, the snapshot that replaces the outcomes log, the steps the loop takes, and
// what the start does about a log that was cut at the front.
//
// This file declares no module, so that it can read the delivery state's layout (`off_cur`, `off_cells`, ... in `hooks.ls`) directly; the pure parts
// (the rule for dropping, the arithmetic) are in `retain.ls`, which has unit tests.

import store;
import retain;

// ---------------------------------------------------------------------
// The block of the delivery state that retention keeps (`dv[rt_at() ..]`)
// ---------------------------------------------------------------------

fn rt_at() -> [] int {
    return off_ex() + 8 + filter.max_type();
}

fn rt_size() -> [] int {
    return 32;
}

// Retention in ms (0: keep for ever), as set; and the age an event must reach before it may be dropped: the larger of that and the idempotency window.
fn r_retention_ms() -> [] int {
    return 0;
}

fn r_age_ms() -> [] int {
    return 1;
}

fn r_delivery_limit() -> [] int {
    return 2;
}

// The size of the outcomes log right after the last snapshot (0 if none has been made).
fn r_snap_bytes() -> [] int {
    return 3;
}

// The monotone ms before which maintenance does not look again (set after a failed step, or a lock that was held: a backoff; the decision itself is a few
// comparisons and is made every turn).
fn r_check_at() -> [] int {
    return 4;
}

fn r_snapshots() -> [] int {
    return 5;
}

fn r_stall_max() -> [] int {
    return 6;
}

fn r_stall_last() -> [] int {
    return 7;
}

fn r_lock_skips() -> [] int {
    return 8;
}

fn r_errors() -> [] int {
    return 9;
}

// What the loop says on stderr about the last step: a kind (0 nothing, 1 rolled, 2 dropped, 3 snapshot), and three numbers.
fn r_msg() -> [] int {
    return 10;
}

fn r_retention_days() -> [] int {
    return 14;
}

// The id of the last event dropped.
fn r_dropped_id() -> [] int {
    return 15;
}

fn rt_init[&d](dv: &!d [int], days: int, ms_knob: int, window_ms: int, delivery_limit: int) -> [] int {
    var retention = days * state.day_ms();
    if ms_knob > 0 {
        retention = ms_knob;
    }
    dv[rt_at() + r_retention_days()] = days;
    dv[rt_at() + r_retention_ms()] = retention;
    dv[rt_at() + r_age_ms()] = retain.age_needed(retention, window_ms);
    dv[rt_at() + r_delivery_limit()] = delivery_limit;
    return 0;
}

// ---------------------------------------------------------------------
// What may go
// ---------------------------------------------------------------------

// The floor: the largest id such that every event up to it is final at every endpoint of the table and has no replay waiting. The events log is cut at the
// front only, so a pin (an event not final somewhere, a replay, or an endpoint's cursor that is behind) keeps every later event too. With no endpoint
// every event is final.
fn rt_floor[&d, &l](dv: &d [int], lg: &l evlog.Ev) -> [] int {
    // Until the endpoints have been read from the database (`docs/design.md` section 37) the table is empty and "every event is final at every endpoint" is
    // vacuous: the cursors are unknown, and nothing may go.
    if !history.endpoints_known(dv[off_hq()..off_hq() + history.size()]) {
        return 0;
    }
    var f = evlog.last_id(lg);
    var i = 0;
    while i < dv[c_endpoints()] {
        let e = dv[off_table() + i * endpoints.stride()];
        if e >= 0 && dv[off_cur() + e] < f {
            f = dv[off_cur() + e];
        }
        i = i + 1;
    }
    var r = 0;
    while r < rp_cap() {
        let b = off_rp() + r * rp_stride();
        if dv[b] == 1 && dv[b + 2] - 1 < f {
            f = dv[b + 2] - 1;
        }
        r = r + 1;
    }
    return f;
}

// ---------------------------------------------------------------------
// The snapshot
// ---------------------------------------------------------------------

// The dead letters whose events are gone from the events log go too (`dead.ls`): every slot's table, from the first event that is left.
fn dead_expire_all[&l, &d](lg: &l evlog.Ev, dv: &!d [int]) -> [] int {
    var e = 0;
    var gone = 0;
    while e < state.max_endpoints() {
        gone = gone + dead.expire(dead_of_mut(dv), e, evlog.first_id(lg));
        e = e + 1;
    }
    return gone;
}

// One outcome record into `buf` at `at`, with the next sequence number. Answers where the next goes.
fn rt_put[&b, &v](buf: &!b [byte], at: int, dv: &!v [int], kind: int, e: int, id: int, tries: int, next_at: int) -> [] int {
    let n = state.put_outcome(buf, at, dv[c_seq()], kind, e, id, tries, next_at);
    dv[c_seq()] = dv[c_seq()] + 1;
    return at + n;
}

// The bytes the snapshot of the delivery state needs at most for one **chunk** of `rt_chunk()` slots: one record for the slot, one for each cell of its window, a
// few for its flags, and the replays (a little over 4.9 MB), and one for each dead letter the tables hold, 2,048 an endpoint, and the floor (9.8 MB more).
fn rt_snapshot_room() -> [] int {
    return 16777216;
}

// The snapshot is built and written a chunk of this many slots at a time, so that its buffer is 16 MiB however many slots there are (the largest state of 1,024
// endpoints would need 277 MB at once; `docs/design.md` section 41.4). It was the number of slots there were: 62 or fewer make one chunk, and the file is what it was.
fn rt_chunk() -> [] int {
    return 62;
}

// Does a slot of `state.first_wide()` or above have an endpoint (live, dormant or draining)? Then the log needs the marker that makes a build from before refuse it
// (`state.wide()`, `docs/design.md` section 41.5).
fn rt_uses_wide[&v](dv: &v [int]) -> [] bool {
    var e = state.first_wide();
    while e < state.max_endpoints() {
        if dv[off_slotid() + e] >= 0 {
            return true;
        }
        e = e + 1;
    }
    return false;
}

// The state as the shortest log that replays to it (`docs/retention.md` section 6), the slots `from` to `to - 1` of it: with `header`, the format header and, if a
// slot of 62 or above has an owner, the marker `wide`; then for each slot that has an endpoint (live, dormant or draining) `created`, the cells of its window, its
// flags and its waiting replays; `last` is the id of the newest event (a window past it is empty). Every kind is one the replay already knows (but `wide`, which it is
// only told). Answers `(bytes, the offset of a record boundary about half way)`.
fn rt_build[&b, &v](buf: &!b [byte], dv: &!v [int], now: int, last: int, from: int, to: int, header: bool) -> [] (int, int) {
    var at = 0;
    if header {
        at = rt_put(buf, 0, dv, state.format(), state.format_slot(), 2, 0, now);
        // what the new log holds is what `note_created` must know (hooks.ls `c_wide`)
        dv[c_wide()] = 0;
        if rt_uses_wide(dv) {
            at = rt_put(buf, at, dv, state.wide(), 0, 0, 0, 0);
            dv[c_wide()] = 1;
        }
    }
    var mid = 0;
    var e = from;
    while e < to {
        if e == from + (to - from) / 2 {
            mid = at;
        }
        let ident = dv[off_slotid() + e];
        if ident >= 0 {
            let c = dv[off_cur() + e];
            at = rt_put(buf, at, dv, state.created(), e, ident, c, 0);
            // (the cells of events that are not in the log are zero: an endpoint that is caught up has none to read, and a snapshot does not make its window resident)
            var id = c + 1;
            var upto = c + state.span();
            if last < upto {
                upto = last;
            }
            while id <= upto {
                let tries = state.attempts(dv[off_cells()..off_flight()], e, id);
                let next_at = state.next_at(dv[off_cells()..off_flight()], e, id);
                if state.is_final(dv[off_cells()..off_flight()], dv[off_cur()..off_cur() + state.max_endpoints()], e, id) {
                    // final is all the window needs; which of them are dead letters is the table's (below)
                    at = rt_put(buf, at, dv, state.delivered(), e, id, tries, next_at);
                } else if tries > 0 || next_at > 0 {
                    at = rt_put(buf, at, dv, state.failed(), e, id, tries, next_at);
                }
                id = id + 1;
            }
            // the dead letters the table holds, oldest first, and the floor it has (what was left out for room)
            var dk = 0;
            while dk < dead.count(dead_of(dv), e) {
                at = rt_put(buf, at, dv, state.dead_entry(), e, dead.id_at(dead_of(dv), e, dk), dead.attempts_at(dead_of(dv), e, dk) * 65536 + dead.reason_at(dead_of(dv), e, dk) + 1, dead.died_at(dead_of(dv), e, dk));
                dk = dk + 1;
            }
            if dead.floor(dead_of(dv), e) > 0 {
                at = rt_put(buf, at, dv, state.dead_entry(), e, dead.floor(dead_of(dv), e), 0, 0);
            }
            if is_paused(dv, e) {
                at = rt_put(buf, at, dv, state.paused(), e, 0, 0, 0);
            } else if is_disabled(dv, e) {
                at = rt_put(buf, at, dv, state.disabled(), e, 0, 0, 0);
            }
            if dv[off_streak() + e] > 0 {
                at = rt_put(buf, at, dv, state.streak(), e, 0, 0, dv[off_streak() + e]);
            }
            var r = 0;
            while r < rp_cap() {
                let b = off_rp() + r * rp_stride();
                if dv[b] == 1 && dv[b + 1] == e {
                    at = rt_put(buf, at, dv, state.replay(), e, dv[b + 2], 0, 0);
                    if dv[b + 3] > 0 || dv[b + 4] > 0 {
                        at = rt_put(buf, at, dv, state.replay_failed(), e, dv[b + 2], dv[b + 3], dv[b + 4]);
                    }
                }
                r = r + 1;
            }
        }
        e = e + 1;
    }
    return (at, mid);
}

// Write the snapshot of the state to `delivery.seg.tmp` and make it durable (steps 13 to 16), a chunk of `rt_chunk()` slots at a time through one buffer (the
// steps are the same however many chunks there are: 13 when the file exists, 14 after the first half of the first chunk, 15 when the last is written). Answers
// the number of bytes, or `0 - 1` if a step failed (the file is then removed).
fn rt_write_snapshot[&h, &l, &v](heap: &!h Heap, lg: &l evlog.Ev, dv: &!v [int], now: int) -> [heap, fs_read(""), fs_write(""), file_write, poll] int {
    let fs = evlog.lend(lg);
    let dir = evlog.dir_of(lg);
    dead_expire_all(lg, dv);
    let buf = box_slice(heap, rt_snapshot_room(), byte_of(0));
    var total = 0;
    var rc = 0;
    region a {
        let tmp = alloc_slice[a](2112, byte_of(0));
        let tn = store.path_join(tmp, dir, "delivery.seg.tmp");
        var from = 0;
        while from < state.max_endpoints() && rc == 0 {
            var to = from + rt_chunk();
            if to > state.max_endpoints() {
                to = state.max_endpoints();
            }
            var bytes = 0;
            var mid = 0;
            borrow mut buf as &!bw in {
                let built = rt_build(contents(bw), dv, now, evlog.last_id(lg), from, to, from == 0);
                bytes = built.0;
                mid = built.1;
            }
            borrow buf as &bq in {
                if from == 0 {
                    rc = store.write_file(fs, tmp[0..tn], contents(bq)[0..0], false);
                    if rc == 0 {
                        evlog.step(lg, 13);
                        rc = store.append_bytes(fs, tmp[0..tn], contents(bq)[0..mid], false);
                    }
                    if rc == 0 {
                        evlog.step(lg, 14);
                        rc = store.append_bytes(fs, tmp[0..tn], contents(bq)[mid..bytes], false);
                    }
                } else if bytes > 0 {
                    rc = store.append_bytes(fs, tmp[0..tn], contents(bq)[0..bytes], false);
                }
            }
            total = total + bytes;
            from = to;
        }
        if rc == 0 {
            evlog.step(lg, 15);
            rc = store.sync_path(fs, tmp[0..tn]);
        }
        if rc == 0 {
            evlog.step(lg, 16);
        } else {
            store.remove(fs, tmp[0..tn]);
        }
    }
    unbox_slice(heap, buf);
    if rc != 0 {
        return 0 - 1;
    }
    return total;
}

// Replace the outcomes log by a snapshot of the state (`docs/retention.md` section 6), steps 12 to 19. `done` is the log in use, and the one to use
// afterwards: if anything fails before the rename it is still the old one, untouched. The new file is opened (as the log in use will be) before the
// rename, so its handles are the ones for `delivery.seg` once the rename has been made, and there is no moment at which the service holds a handle to
// a file that is not the log. The caller has taken the lock and nothing else is appending.
fn rt_snapshot[&h, &l, &v, &w](heap: &!h Heap, lg: &l evlog.Ev, done: log.Log, dv: &!v [int], window: &!w [byte], now: int) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] log.Log {
    var d = done;
    var flushed = 0;
    var before = 0;
    borrow mut d as &!dw in {
        flushed = log.flush(dw);
        before = log.size(dw);
    }
    if flushed != 0 {
        return d;
    }
    evlog.step(lg, 12);
    let fs = evlog.lend(lg);
    let dir = evlog.dir_of(lg);
    let total = rt_write_snapshot(heap, lg, dv, now);
    if total < 0 {
        dv[rt_at() + r_errors()] = dv[rt_at() + r_errors()] + 1;
        return d;
    }
    region a {
        let tmp = alloc_slice[a](2112, byte_of(0));
        let live = alloc_slice[a](2112, byte_of(0));
        d = rt_switch(lg, fs, dir, tmp, live, d, dv, window, total, before);
    }
    return d;
}

// Steps 17 to 19: the temporary file becomes the log. Answers the log in use afterwards: the new one, or `d` if the rename could not be made.
// (Not inside the region that holds the two paths: a region left by a `return` is not given back, `docs/lexsys-log-retention.md` gap 6.)
fn rt_switch[&l, &c, &v, &w, &t, &y](lg: &l evlog.Ev, fs: &c Fs(""), dir: &y [byte], tmp: &!t [byte], live: &!t [byte], d: log.Log, dv: &!v [int], window: &!w [byte], total: int, before: int) -> [fs_read(""), fs_write(""), file_read, file_write, poll] log.Log {
    let tn = store.path_join(tmp, dir, "delivery.seg.tmp");
    let ln = store.path_join(live, dir, "delivery.seg");
    match open_tmp_log(fs, tmp[0..tn], window) {
        Opening::Failed(e) => {
            store.remove(fs, tmp[0..tn]);
            dv[rt_at() + r_errors()] = dv[rt_at() + r_errors()] + 1;
            return d;
        }
        Opening::Ok(nl) => {
            if store.rename(fs, tmp[0..tn], live[0..ln]) != 0 {
                log.close(nl);
                store.remove(fs, tmp[0..tn]);
                dv[rt_at() + r_errors()] = dv[rt_at() + r_errors()] + 1;
                return d;
            }
            evlog.step(lg, 17);
            store.sync_path(fs, dir);
            evlog.step(lg, 18);
            log.close(d);
            evlog.step(lg, 19);
            dv[rt_at() + r_snapshots()] = dv[rt_at() + r_snapshots()] + 1;
            dv[rt_at() + r_snap_bytes()] = total;
            dv[rt_at() + r_msg()] = 3;
            dv[rt_at() + r_msg() + 1] = before;
            dv[rt_at() + r_msg() + 2] = total;
            return nl;
        }
    }
}

// ---------------------------------------------------------------------
// The loop's step
// ---------------------------------------------------------------------

// Say on stderr what the last step did, once.
fn rt_report[&i, &d](out: &!i Io, dv: &!d [int]) -> [err_write] int {
    let kind = dv[rt_at() + r_msg()];
    if kind == 0 {
        return 0;
    }
    region a {
        let nb = alloc_slice[a](24, byte_of(0));
        let x = dv[rt_at() + r_msg() + 1];
        let y = dv[rt_at() + r_msg() + 2];
        let z = dv[rt_at() + r_msg() + 3];
        if kind == 1 {
            say(out, "hooks: events log: sealed a segment; the next is events-");
            say(out, nb[0..digits_of(x, nb)]);
            say(out, ".seg\n");
        } else if kind == 2 {
            say(out, "hooks: events log: dropped a segment of ");
            say(out, nb[0..digits_of(y, nb)]);
            say(out, " events (ids up to ");
            say(out, nb[0..digits_of(x, nb)]);
            say(out, "): final everywhere and past the retention\n");
        } else if kind == 3 {
            say(out, "hooks: delivery.seg: replaced by a snapshot, ");
            say(out, nb[0..digits_of(x, nb)]);
            say(out, " bytes to ");
            say(out, nb[0..digits_of(y, nb)]);
            say(out, "\n");
        }
    }
    dv[rt_at() + r_msg()] = 0;
    return 0;
}

fn lock_path[&o, &d](out: &!o [byte], dir: &d [byte]) -> [] int {
    return store.path_join(out, dir, "compact.lock");
}

// One step of retention, if one is due, and the idempotency indexes' eviction every time (a bounded number of keys, microseconds). At most one heavy
// step a call, and the lock (`compact.lock`) taken for the ones that remove something, so a backup that holds it is not disturbed. Answers the
// outcomes log, which a snapshot replaces.
fn rt_maintain[&h, &l, &v, &i, &a, &s, &k, &w](heap: &!h Heap, lg: &!l evlog.Ev, done: log.Log, dv: &!v [int], ix: &!i [int], arena: &!a [byte], sg: &s [int], clock: &k Clock, window: &!w [byte]) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll, clock] log.Log {
    let now = clock_unix_ms(clock);
    let mono = clock_ms(clock);
    idem.evict(ix, now, 256);
    let cat = idem.second_at(ix);
    idem.evict(ix[cat..len(ix)], now, 256);
    var d = done;
    if mono < dv[rt_at() + r_check_at()] || evlog.broken(lg) {
        return d;
    }
    let retention = dv[rt_at() + r_retention_ms()];
    let age = dv[rt_at() + r_age_ms()];
    let floor = rt_floor(dv, lg);
    let oldest = evlog.oldest_sealed(lg);
    // Before the endpoints are read (section 37) only sealing a segment is safe: a drop rests on cursors, and a snapshot is built from them, so with the table
    // empty it would replace the outcomes log by nothing.
    let known = history.endpoints_known(dv[off_hq()..off_hq() + history.size()]);
    var action = 0;
    if known && retain.may_drop(retention, oldest.0, oldest.1, floor, now, age) && sched.tick_state(sg) == 0 {
        action = 2;
    } else if retain.should_roll(evlog.active_bytes(lg), evlog.limit(lg), retention, evlog.active_created(lg), now, age) {
        action = 1;
    } else if known {
        var sz = 0;
        borrow d as &dr1 in {
            sz = log.size(dr1);
        }
        if retain.snapshot_due(sz, dv[rt_at() + r_delivery_limit()], dv[rt_at() + r_snap_bytes()]) {
            action = 3;
        }
    }
    if action == 0 {
        return d;
    }
    let t0 = clock_ms(clock);
    var rc = 0;
    var skipped = false;
    if action == 1 {
        // Sealing needs no lock: it only makes a file that a backup, which lists the segments after it has copied the outcomes, does not need.
        rc = evlog.flush(lg);
        if rc == 0 {
            rc = evlog.roll(lg, now);
        }
        if rc == 0 {
            dv[rt_at() + r_msg()] = 1;
            dv[rt_at() + r_msg() + 1] = evlog.active_k(lg);
        }
    } else {
        region a {
            let lp = alloc_slice[a](2112, byte_of(0));
            let ln = lock_path(lp, evlog.dir_of(lg));
            match store.try_lock(evlog.lend(lg), lp[0..ln]) {
                store.Locked::Busy => {
                    dv[rt_at() + r_lock_skips()] = dv[rt_at() + r_lock_skips()] + 1;
                    dv[rt_at() + r_check_at()] = mono + 1000;
                    skipped = true;
                }
                store.Locked::Failed(e) => {
                    dv[rt_at() + r_errors()] = dv[rt_at() + r_errors()] + 1;
                    dv[rt_at() + r_check_at()] = mono + 5000;
                    skipped = true;
                }
                store.Locked::Got(f) => {
                    if action == 2 {
                        // The cursors this decision rests on are made durable first.
                        borrow mut d as &!dw in {
                            rc = log.flush(dw);
                        }
                        let events = evlog.oldest_events(lg);
                        if rc == 0 {
                            rc = evlog.drop_oldest(lg);
                        }
                        if rc == 0 {
                            idem.set_floor(ix, oldest.0);
                            idem.set_floor(ix[cat..len(ix)], oldest.0);
                            dv[rt_at() + r_dropped_id()] = oldest.0;
                            dead_expire_all(lg, dv);
                            dv[rt_at() + r_msg()] = 2;
                            dv[rt_at() + r_msg() + 1] = oldest.0;
                            dv[rt_at() + r_msg() + 2] = events;
                        }
                    } else {
                        d = rt_snapshot(heap, lg, d, dv, window, now);
                    }
                    store.release_lock(f);
                }
            }
        }
    }
    if skipped {
        return d;
    }
    let spent = clock_ms(clock) - t0;
    dv[rt_at() + r_stall_last()] = spent;
    if spent > dv[rt_at() + r_stall_max()] {
        dv[rt_at() + r_stall_max()] = spent;
    }
    if rc != 0 {
        dv[rt_at() + r_errors()] = dv[rt_at() + r_errors()] + 1;
        dv[rt_at() + r_check_at()] = mono + 5000;
    }
    return d;
}

// `compact-now` (`docs/retention.md` section 3): seal the active segment if it holds anything, drop every segment that may go, replace the outcomes log
// by a snapshot. Takes the lock. Answers the outcomes log in use and a status: 0, or 3 if somebody holds the lock.
fn rt_compact_now[&h, &l, &v, &i, &a, &w](heap: &!h Heap, lg: &!l evlog.Ev, done: log.Log, dv: &!v [int], ix: &!i [int], arena: &!a [byte], window: &!w [byte], now: int) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll] (log.Log, int) {
    var d = done;
    var status = 0;
    region b {
        let lp = alloc_slice[b](2112, byte_of(0));
        let ln = lock_path(lp, evlog.dir_of(lg));
        match store.try_lock(evlog.lend(lg), lp[0..ln]) {
            store.Locked::Busy => {
                status = 3;
            }
            store.Locked::Failed(e) => {
                status = 3;
            }
            store.Locked::Got(f) => {
                var rc = evlog.flush(lg);
                borrow mut d as &!dw in {
                    if log.flush(dw) != 0 {
                        rc = 1;
                    }
                }
                if rc == 0 && evlog.active_bytes(lg) > 0 {
                    rc = evlog.roll(lg, now);
                }
                let cat = idem.second_at(ix);
                var more = rc == 0;
                while more {
                    let oldest = evlog.oldest_sealed(lg);
                    if retain.may_drop(dv[rt_at() + r_retention_ms()], oldest.0, oldest.1, rt_floor(dv, lg), now, dv[rt_at() + r_age_ms()]) {
                        if evlog.drop_oldest(lg) == 0 {
                            idem.set_floor(ix, oldest.0);
                            idem.set_floor(ix[cat..len(ix)], oldest.0);
                            dv[rt_at() + r_dropped_id()] = oldest.0;
                            dead_expire_all(lg, dv);
                        } else {
                            more = false;
                            rc = 1;
                        }
                    } else {
                        more = false;
                    }
                }
                if rc == 0 {
                    d = rt_snapshot(heap, lg, d, dv, window, now);
                } else {
                    status = 1;
                }
                store.release_lock(f);
            }
        }
    }
    return (d, status);
}

// ---------------------------------------------------------------------
// The start
// ---------------------------------------------------------------------

// The format of the outcomes log: a new log gets its header; an old one is format 1 (no header) and is read as it always was; a header of a number
// this version does not know is a refusal (status 41). Answers 0 or 41.
fn rt_check_format[&g, &w](done: &!g log.Log, window: &!w [byte], now: int) -> [file_read, file_write] int {
    if log.records(done) == 0 {
        var rc = 1;
        region a {
            let rec = alloc_slice[a](128, byte_of(0));
            let n = state.put_outcome(rec, 0, 1, state.format(), state.format_slot(), 2, 0, now);
            rc = log.append(done, rec[0..n], 1, 0);
        }
        if rc != 0 || log.flush(done) != 0 {
            return 12;
        }
        return 0;
    }
    let r = log.read_at(done, 0, window);
    if r.0 != 0 {
        return 0;
    }
    let o = state.outcome_at(window, 0);
    if o.0 == state.format() && o.2 != 2 {
        return 41;
    }
    return 0;
}

// Endpoints that are behind the oldest event the log still holds: a row that came back after its events were dropped. They start where the log does
// (what they missed and retention has dropped cannot be sent). Answers how many events were skipped in all.
fn rt_clamp[&l, &d](lg: &!l evlog.Ev, dv: &!d [int]) -> [] int {
    let lo = evlog.first_id(lg) - 1;
    var skipped = 0;
    var i = 0;
    while i < dv[c_endpoints()] {
        let e = dv[off_table() + i * endpoints.stride()];
        if e >= 0 && dv[off_cur() + e] < lo {
            skipped = skipped + lo - dv[off_cur() + e];
            reset_window(dv, e, lo);
        }
        i = i + 1;
    }
    evlog.note_clamped(lg, skipped);
    return skipped;
}

// The event with id `id`, as a JSON body `{"id":N,"event":<the stored object>}`, or an empty buffer if there is none (never made, or dropped).
fn find_event[&h, &l, &w](heap: &!h Heap, lg: &!l evlog.Ev, window: &!w [byte], id: int) -> [heap, fs_read(""), file_read] buffer.Buffer {
    var at = evlog.seek(lg, id);
    var found = buffer.empty(heap, 0);
    var going = id >= evlog.first_id(lg);
    while going {
        let r = evlog.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else if record.ms_of(window, 0) == id {
            // The first pair's value is the stored body.
            let p = record.pair_at(window, record.first_pair(0));
            var w = json.writer(heap, r.1 + 32);
            w = json.begin_object(heap, w);
            w = json.put_key(heap, w, "id");
            w = json.put_int(heap, w, id);
            w = json.put_key(heap, w, "event");
            w = json.put_fragment(heap, w, window[p.2..p.2 + p.3]);
            w = json.end_object(heap, w);
            buffer.drop(heap, found);
            found = json.finish(w);
            going = false;
        } else if record.ms_of(window, 0) > id {
            going = false;
        } else {
            at = at + r.1;
        }
    }
    return found;
}

// The offset in the events log of event `id`, or -1 if there is no such event (or it was dropped).
fn find_offset[&l, &w](lg: &!l evlog.Ev, window: &!w [byte], id: int) -> [fs_read(""), file_read] int {
    if id < evlog.first_id(lg) {
        return 0 - 1;
    }
    var at = evlog.seek(lg, id);
    var going = true;
    while going {
        let r = evlog.read_at(lg, at, window);
        if r.0 != 0 {
            going = false;
        } else if record.ms_of(window, 0) == id {
            return at;
        } else if record.ms_of(window, 0) > id {
            going = false;
        } else {
            at = at + r.1;
        }
    }
    return 0 - 1;
}

// Where each endpoint of the table stands in the events log when the service starts (`docs/design.md` section 31): every one has looked at everything up
// to its own cursor, so the next record it reads is the first whose id is above it. Each is found from the sparse index in at most a thousand records, and
// an endpoint whose cursor is at or past the last record stands at the end of what can be read.
fn seek_slots[&l, &w, &d](lg: &!l evlog.Ev, window: &!w [byte], dv: &!d [int]) -> [fs_read(""), file_read] int {
    let n = dv[c_endpoints()];
    var i = 0;
    while i < n {
        let e = dv[off_table() + i * endpoints.stride()];
        if e >= 0 {
            dv[scan_id(e)] = dv[off_cur() + e];
            var at = evlog.seek(lg, dv[off_cur() + e] + 1);
            var going = true;
            while going {
                let p = evlog.peek(lg, at);
                if p.0 != 0 {
                    going = false;
                } else if record.ms_of(evlog.cache(lg), p.1) > dv[off_cur() + e] {
                    going = false;
                } else {
                    at = at + p.2;
                }
            }
            dv[scan_off(e)] = at;
        }
        i = i + 1;
    }
    return 0;
}

// Rebuild the idempotency indexes from the events log that is retained: every keyed record, in order, the later one of two with the same key winning (as it
// does when the service is running). Keys of cron's (`cron:` ...) go to the second index and are kept as long as their event; the others are kept while
// their window has not passed, so a key that has expired is not read in. Answers 0, or a status for `main` to exit with: the log has a record this code
// does not understand, ids that are not dense, or more keys that are fresh than the index holds (raise `idem-keys`).
fn rebuild[&g, &x, &y](lg: &!g evlog.Ev, ix: &!x [int], arena: &!y [byte], now: int) -> [fs_read(""), file_read] int {
    let cat = idem.second_at(ix);
    let aat = idem.second_arena_at(ix);
    var at = evlog.first_offset(lg);
    var expect = evlog.first_id(lg);
    idem.set_floor(ix, expect - 1);
    idem.set_floor(ix[cat..len(ix)], expect - 1);
    while true {
        let p = evlog.peek(lg, at);
        if p.0 == 1 {
            return 0;
        }
        if p.0 != 0 {
            return 16;
        }
        let id = record.ms_of(evlog.cache(lg), p.1);
        if id != expect {
            return 16;
        }
        evlog.note_scan(lg, id, at);
        let c = evlog.cache(lg);
        // The pairs are found by name: the body, then its type if it has one (`typ`, `docs/design.md` section 35), then for a keyed event `key` and `t`. A
        // log from before the type existed has records of one pair or of three, and reads as it always did.
        var after = 0;
        var left = record.fields_of(c, p.1) - 1;
        if left >= 1 {
            let ev0 = record.pair_at(c, record.first_pair(p.1));
            after = ev0.4;
            let ty = record.pair_at(c, after);
            if ty.1 == 3 && c[ty.0] == byte_of('t') && c[ty.0 + 1] == byte_of('y') && c[ty.0 + 2] == byte_of('p') {
                after = ty.4;
                left = left - 1;
            }
        }
        if left >= 2 {
            let ev = record.pair_at(c, record.first_pair(p.1));
            let k = record.pair_at(c, after);
            let t = record.pair_at(c, k.4);
            if k.1 != 3 || c[k.0] != byte_of('k') || t.1 != 1 || c[t.0] != byte_of('t') || t.3 != 8 {
                return 16;
            }
            let key = c[k.2..k.2 + k.3];
            let stamp = record.get_u64(c, t.2);
            let cron = bytes.starts_with(key, "cron:");
            if cron || now - stamp <= idem.window_ms(ix) {
                var entry = 0 - 1;
                var made = 0;
                if cron {
                    entry = idem.find(ix[cat..len(ix)], arena[aat..len(arena)], key);
                    if entry >= 0 {
                        idem.remove(ix[cat..len(ix)], entry);
                    }
                    if !idem.room(ix[cat..len(ix)], len(key)) {
                        idem.evict(ix[cat..len(ix)], now, 1073741824);
                    }
                    entry = idem.add(ix[cat..len(ix)], arena[aat..len(arena)], key);
                    if entry >= 0 {
                        idem.set(ix[cat..len(ix)], entry, id, stamp, crc.of(c[ev.2..ev.2 + ev.3]), ev.3);
                        made = 1;
                    }
                } else {
                    entry = idem.find(ix, arena, key);
                    if entry >= 0 {
                        idem.remove(ix, entry);
                    }
                    if !idem.room(ix, len(key)) {
                        idem.evict(ix, now, 1073741824);
                    }
                    entry = idem.add(ix, arena, key);
                    if entry >= 0 {
                        idem.set(ix, entry, id, stamp, crc.of(c[ev.2..ev.2 + ev.3]), ev.3);
                        made = 1;
                    }
                }
                if made == 0 {
                    return 16;
                }
            }
        }
        at = at + p.2;
        expect = id + 1;
    }
    return 0;
}

// Open `delivery.seg` for the start: a snapshot that a crash left half made (`delivery.seg.tmp`) is not part of any log and goes first.
fn open_delivery[&e, &d, &w, &q](ev: &!e evlog.Ev, dir: &d [byte], window: &!w [byte], repair: bool, report: &!q [int], gate: int) -> [fs_read(""), fs_write(""), file_read, file_write] Opening {
    region a {
        let tmp = alloc_slice[a](2112, byte_of(0));
        let tn = store.path_join(tmp, dir, "delivery.seg.tmp");
        store.remove(evlog.lend(ev), tmp[0..tn]);
    }
    return open_log(evlog.lend(ev), dir, "delivery.seg", window, repair, report, gate);
}

// `compact-now` with a database (`docs/design.md` section 41.8): the endpoints of the table are what says which events are final, so the run reads them, **as the service
// does** (the same pool, the same request, `load_late` for what follows it), and waits for that as long as `pg-start-wait-ms` allows (`dbup.verdict`: a refusal for good
// ends it at once, a database that cannot be reached or that does not answer ends it after the wait; 0 waits for ever). There is no listener and no attempt: a poller of its own
// for the pool's connections and nothing else. The pool is closed before it answers. Answers the status: 0 (the endpoints are in the state, and `history.endpoints_known`), 20
// (the database could not give them: the message is said), 4 (no poller), or what `load_late` says (13 a bad row, 15 or 17 the log); a line on stderr for each but 0 and 4.
fn compact_read_table[&h, &l, &g, &w, &d, &b, &n, &k, &o, &e](heap: &!h Heap, lg: &!l evlog.Ev, done: &!g log.Log, window: &!w [byte], dv: &!d [int], blob: &!b [byte], net: &n Net(""), clock: &k Clock, io: &!o Io, pl0: pool.Pool, dbhost: &e [byte], dbport: int) -> [heap, conn_read, conn_write, poll, clock, file_read, file_write, fs_read(""), net_out(""), err_write] int {
    var result = 4;
    var pl = pl0;
    match poller_new() {
        Polling::Ok(p0) => {
            var poller = p0;
            borrow mut pl as &!qw in {
                borrow mut poller as &!pw in {
                    pool.start(qw, pw, 100);
                }
            }
            let events = box_slice(heap, 64, 0);
            let began = clock_ms(clock);
            var going = true;
            result = 0;
            while going {
                borrow mut poller as &!pw in {
                    pl = pool.revive(heap, pl, net, dbhost, dbport, pw, clock_ms(clock));
                }
                var nap = 50;
                borrow mut pl as &!qw in {
                    borrow mut poller as &!pw in {
                        var tag = pool.next_done(qw);
                        while tag >= 0 && going {
                            if tag == dbup.load_tag() {
                                if pool.status(qw) == 8 {
                                    // the answer does not fit the pool's input slab (1 MiB)
                                    say_unreadable(io, 20, 6);
                                    result = 20;
                                    going = false;
                                } else if pool.status(qw) != 0 {
                                    // the connection went with the request on it: asked again when one is live
                                    history.set_load_state(dv[off_hq()..off_hq() + history.size()], 0);
                                } else {
                                    let (st, dt) = load_late(heap, lg, done, window, dv, blob, pool.reply(qw));
                                    if st == 0 {
                                        history.set_load_state(dv[off_hq()..off_hq() + history.size()], 1);
                                        say(io, "hooks: endpoints loaded: ");
                                        ops.say_number(io, dt);
                                        say(io, "\n");
                                    } else {
                                        say_unreadable(io, st, dt);
                                        result = st;
                                    }
                                    going = false;
                                }
                            }
                            tag = pool.next_done(qw);
                        }
                        if going && history.load_state(dv[off_hq()..off_hq() + history.size()]) == 0 && pool.live(qw) > 0 {
                            let ask = queries.endpoints_all_start(heap);
                            var asked_it = 0 - 1;
                            borrow ask as &ab in {
                                asked_it = pool.submit(qw, dbup.load_tag(), buffer.bytes(ab));
                            }
                            buffer.drop(heap, ask);
                            if asked_it == 0 {
                                history.set_load_state(dv[off_hq()..off_hq() + history.size()], 2);
                            }
                        }
                        if going {
                            region ra {
                                let state5 = alloc_slice[ra](5, byte_of(0));
                                let known = pool.sqlstate(qw, state5);
                                let why = dbup.verdict(pool.last_failure(qw), state5[0..known], clock_ms(clock) - began, history.start_wait_ms(dv[off_hq()..off_hq() + history.size()]));
                                if why != 0 {
                                    say_unreadable(io, 20, why);
                                    result = 20;
                                    going = false;
                                }
                            }
                        }
                        pool.flush(qw, pw);
                    }
                    let due = pool.next_wake(qw, clock_ms(clock));
                    if due >= 0 && due < nap {
                        nap = due;
                    }
                }
                if going {
                    var ready = 0 - 1;
                    borrow mut poller as &!pw in {
                        borrow mut events as &!ew in {
                            ready = poller_wait(pw, contents(ew), nap);
                        }
                    }
                    var j = 0;
                    while j < ready {
                        var token = 0 - 1;
                        var how = 0;
                        borrow events as &er in {
                            token = contents(er)[2 * j];
                            how = contents(er)[2 * j + 1];
                        }
                        borrow mut pl as &!qw in {
                            if pool.owns(qw, token) {
                                borrow mut poller as &!pw in {
                                    pool.pump(qw, pw, token, how);
                                }
                            }
                        }
                        j = j + 1;
                    }
                }
            }
            unbox_slice(heap, events);
            pool.close(heap, pl);
            poller_close(poller);
        }
        Polling::Failed(e) => {
            pool.close(heap, pl);
        }
    }
    return result;
}

// `compact-now`: do it, say what was done, close the outcomes log. Answers the status to exit with: 0, 43 if somebody holds the lock, 44 if a step failed.
fn compact_once[&h, &l, &v, &i, &a, &w, &o](heap: &!h Heap, lg: &!l evlog.Ev, done: log.Log, dv: &!v [int], ix: &!i [int], arena: &!a [byte], window: &!w [byte], now: int, out: &!o Io) -> [heap, fs_read(""), fs_write(""), file_read, file_write, poll, err_write] int {
    if !history.endpoints_known(dv[off_hq()..off_hq() + history.size()]) {
        // Without the endpoints it knows no cursor: it would drop what a slow endpoint still needs and replace the outcomes log by an empty snapshot. `main` reads them
        // first (`compact_read_table`) and does not come here if it could not; this is the check that a path which did not read them cannot get past. Refused, nothing changed.
        log.close(done);
        say(out, "hooks: compact-now: the endpoints of the database have not been read, so it does not know which events are final; nothing was done\n");
        return 44;
    }
    let segments = evlog.dropped_segments(lg);
    let events = evlog.dropped_events(lg);
    var was = 0;
    borrow done as &dr2 in {
        was = log.size(dr2);
    }
    let (d2, st) = rt_compact_now(heap, lg, done, dv, ix, arena, window, now);
    var now_size = 0;
    borrow d2 as &dr3 in {
        now_size = log.size(dr3);
    }
    log.close(d2);
    region b {
        let nb = alloc_slice[b](24, byte_of(0));
        if st == 3 {
            say(out, "hooks: compact-now: compact.lock is held by another process (a backup?); nothing was done\n");
        } else if st != 0 {
            say(out, "hooks: compact-now: a step failed; what was done is safe, run it again\n");
        } else {
            say(out, "hooks: compacted: dropped ");
            say(out, nb[0..digits_of(evlog.dropped_segments(lg) - segments, nb)]);
            say(out, " segments (");
            say(out, nb[0..digits_of(evlog.dropped_events(lg) - events, nb)]);
            say(out, " events); delivery.seg ");
            say(out, nb[0..digits_of(was, nb)]);
            say(out, " bytes to ");
            say(out, nb[0..digits_of(now_size, nb)]);
            say(out, "\n");
        }
    }
    if st == 3 {
        return 43;
    }
    if st != 0 {
        return 44;
    }
    return 0;
}

// What the start found and repaired, said once: files a cut drop had left, and endpoints that were away while events were dropped.
fn rt_say_start[&i, &l](out: &!i Io, lg: &l evlog.Ev) -> [err_write] int {
    region a {
        let nb = alloc_slice[a](24, byte_of(0));
        if evlog.orphans(lg) > 0 {
            say(out, "hooks: events log: removed ");
            say(out, nb[0..digits_of(evlog.orphans(lg), nb)]);
            say(out, " segment file(s) that a drop which was cut short had left\n");
        }
        if evlog.clamped(lg) > 0 {
            say(out, "hooks: events log: an endpoint that was away while events were dropped starts at the oldest that is left; ");
            say(out, nb[0..digits_of(evlog.clamped(lg), nb)]);
            say(out, " events it would have been sent are gone (retention)\n");
        }
    }
    return 0;
}

// Does the log give slot `e` to an endpoint, one that is in the table or a dormant one (a row that is gone for now)? What the log says about such a slot is
// kept in the state like any other (`replay`), so that a snapshot of the state keeps it.
fn slot_known[&d](dv: &d [int], e: int) -> [] bool {
    return dv[off_slotid() + e] >= 0;
}
