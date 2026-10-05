edition 5;

module history;

import std.buffer;
import pg;
import pg.pool;
import queries;

// `history` -- the attempts that ended, written to PostgreSQL (`docs/design.md` section 24), and the service's one connection to the
// database (section 37).
//
// The delivery logs stay the truth about delivery. This is the record a person or the API reads, so it is **best effort**
// and it must never slow delivery down: an attempt that ended is *pushed* onto a ring in memory (no effect, no waiting), and
// once a turn `drain` turns what is in the ring into requests on the pool, whose connections are non-blocking and sit in the
// service's own poller. When the database is slow, the ring fills; when it is gone (the pool is reconnecting) the rows wait in the ring,
// and a ring that is full drops the new row, counted. Delivery does not notice either.
//
// The state is `size()` integers, which the caller owns (a slice of the delivery state):
//
//     [0] head: where the next row goes     [1] tail: where the next row to send is      [2] live connections
//     [3] enabled (a database was given)    [4] rows written   [5] rows the database refused or lost   [6] rows dropped
//     [7] requests submitted and not answered yet
//     [8] connections being made   [9] connections made again after a loss   [10] attempts to connect   [11] attempts that failed
//     [12] connections lost with a request on them     [13] the endpoints: 0 not read yet, 2 the read is asked for, 1 read
//     [14] why the last attempt failed (`pool.last_failure`)   [15] why the last connection was lost (`pool.last_loss`)
//     [16 .. 20] the settings of the connections: the first wait, the longest wait, the attempt's time, the request's time, the start's wait (ms)
//     [21] history-days (0: rows are kept for ever)   [22] when the next batch of old rows may be deleted (Unix ms; 0: not decided yet)   [23] 1 while a batch is
//     on the pool   [24] rows deleted since the start   [25] batches the database refused or lost (`docs/design.md` section 43)
//     [32 ...] the ring: `cap()` rows of nine integers: endpoint, event, replay, attempt, outcome, status, at (ms), latency (ms), reason (`reason.ls`)

pub fn cap() -> [] int {
    return 256;
}

pub fn size() -> [] int {
    return ring() + cap() * 9;
}

// Where the ring starts in the state.
fn ring() -> [] int {
    return 32;
}

pub fn enabled[&s](h: &s [int]) -> [] bool {
    return h[3] == 1;
}

pub fn live[&s](h: &s [int]) -> [] int {
    return h[2];
}

pub fn written[&s](h: &s [int]) -> [] int {
    return h[4];
}

pub fn failed[&s](h: &s [int]) -> [] int {
    return h[5];
}

pub fn dropped[&s](h: &s [int]) -> [] int {
    return h[6];
}

pub fn pending[&s](h: &s [int]) -> [] int {
    return h[0] - h[1];
}

// Say that a database was given: none of its connections is live yet and its endpoints are not read (section 37).
pub fn enable[&s](h: &!s [int]) -> [] int {
    h[3] = 1;
    h[2] = 0;
    h[13] = 0;
    return 0;
}

// The settings of the connections (`pg-backoff-min-ms` and the rest of `config.ls`), kept here so that `GET /config` and the loop can say them; set whether
// or not a database was named.
pub fn set_timing[&s](h: &!s [int], min_ms: int, max_ms: int, attempt_ms: int, request_ms: int, start_wait_ms: int) -> [] int {
    h[16] = min_ms;
    h[17] = max_ms;
    h[18] = attempt_ms;
    h[19] = request_ms;
    h[20] = start_wait_ms;
    return 0;
}

pub fn start_wait_ms[&s](h: &s [int]) -> [] int {
    return h[20];
}

// The settings of the connections as `enable` was given them: 0 the first wait, 1 the longest wait, 2 the attempt's time, 3 the request's time,
// 4 the start's wait (ms). Without a database named they are the defaults of `enable`'s caller (`GET /config` says them whether or not).
pub fn setting[&s](h: &s [int], which: int) -> [] int {
    return h[16 + which];
}

// How the pool is doing, for `/stats` and `/metrics`: connections being made, connections remade after a loss, attempts, attempts that failed,
// connections lost with a request on them. All since the start; `sync` keeps them.
pub fn connecting[&s](h: &s [int]) -> [] int {
    return h[8];
}

pub fn reconnects[&s](h: &s [int]) -> [] int {
    return h[9];
}

pub fn attempts[&s](h: &s [int]) -> [] int {
    return h[10];
}

pub fn failures[&s](h: &s [int]) -> [] int {
    return h[11];
}

pub fn losses[&s](h: &s [int]) -> [] int {
    return h[12];
}

// Why the last attempt to connect failed (`pool.last_failure`), and why the last connection was lost (`pool.last_loss`); 0 if none has.
pub fn last_failure[&s](h: &s [int]) -> [] int {
    return h[14];
}

pub fn last_loss[&s](h: &s [int]) -> [] int {
    return h[15];
}

// The endpoints of the table, read once from the database (`docs/design.md` section 37.2). A service with no database named has nothing to read:
// it is always "known". Until they are, nothing is delivered and the routes that need them answer `503`.
pub fn endpoints_known[&s](h: &s [int]) -> [] bool {
    return h[3] != 1 || h[13] == 1;
}

// The state of the first read: 0 not asked for, 2 asked for and not answered, 1 read.
pub fn load_state[&s](h: &s [int]) -> [] int {
    return h[13];
}

pub fn set_load_state[&s](h: &!s [int], state: int) -> [] int {
    h[13] = state;
    return 0;
}

// What the loop may take as "the database is there": a connection is live **and** the endpoints have been read. `/readyz` says this.
pub fn serving[&s](h: &s [int]) -> [] int {
    if h[13] != 1 {
        return 0;
    }
    return h[2];
}

// An attempt ended: remember it, to be written. Does nothing when no database was given. When the ring is full the row is
// dropped, and counted.
pub fn push[&s](h: &!s [int], endpoint: int, event: int, replay: int, attempt: int, outcome: int, status: int, at_ms: int, latency_ms: int, why: int) -> [] int {
    if h[3] != 1 {
        return 0;
    }
    if h[0] - h[1] >= cap() {
        h[6] = h[6] + 1;
        return 0;
    }
    let base = ring() + h[0] % cap() * 9;
    h[base] = endpoint;
    h[base + 1] = event;
    h[base + 2] = replay;
    h[base + 3] = attempt;
    h[base + 4] = outcome;
    h[base + 5] = status;
    h[base + 6] = at_ms;
    h[base + 7] = latency_ms;
    h[base + 8] = why;
    h[0] = h[0] + 1;
    return 1;
}

// The pool the service makes for the database: its connections, and how many requests each may have in flight.
pub fn lanes() -> [] int {
    return 2;
}

pub fn depth() -> [] int {
    return 64;
}

// The places in flight the inserts leave free, for what a person or the schedules wait on (a change to an endpoint, a read, a tick): the inserts of a busy
// service would otherwise fill every place, and each change would be refused with a 503 while they last (found on a fast machine: every `PATCH` of 300 was).
pub fn reserved() -> [] int {
    return 16;
}

// Turn the rows in the ring into requests on `pl`, as many as it takes, at most `most` this turn. A row the pool has no room
// for stays in the ring for the next turn, and so does every row while no connection is live (the pool is making one: section 37.3); a row
// it cannot take at all (it is too large) is dropped, and counted. Nothing is sent until the pool's `flush`.
pub fn drain[&h, &q, &s](heap: &!h Heap, pl: &!q pool.Pool, hs: &!s [int], most: int) -> [heap] int {
    var sent = 0;
    var going = true;
    while going && sent < most && hs[0] > hs[1] && pool.in_flight(pl) < lanes() * depth() - reserved() {
        let base = ring() + hs[1] % cap() * 9;
        let request = queries.add_attempt_start(heap, hs[base], hs[base + 1], hs[base + 2], hs[base + 3], hs[base + 4], hs[base + 5], hs[base + 6], hs[base + 7], hs[base + 8]);
        var code = 0 - 1;
        borrow request as &rb in {
            code = pool.submit(pl, 1, buffer.bytes(rb));
        }
        buffer.drop(heap, request);
        if code == 0 {
            hs[7] = hs[7] + 1;
            hs[1] = hs[1] + 1;
            sent = sent + 1;
        } else if code == 0 - 1 || code == 0 - 3 {
            going = false;
        } else {
            hs[6] = hs[6] + 1;
            hs[1] = hs[1] + 1;
        }
    }
    return sent;
}

// Pruning (`docs/design.md` section 43): the rows older than `history-days` are deleted a batch at a time, by the time of the attempt, through the pool like any
// other request and never waited for. A full batch is followed by the next a second later; a batch that was not full, or that failed, by a wait.
pub fn prune_tag() -> [] int {
    return 3;
}

pub fn prune_batch() -> [] int {
    return 10000;
}

fn prune_first_ms() -> [] int {
    return 10000;
}

fn prune_every_ms() -> [] int {
    return 600000;
}

pub fn set_prune_days[&s](h: &!s [int], days: int) -> [] int {
    h[21] = days;
    return 0;
}

pub fn prune_days[&s](h: &s [int]) -> [] int {
    return h[21];
}

pub fn pruned[&s](h: &s [int]) -> [] int {
    return h[24];
}

pub fn prune_failures[&s](h: &s [int]) -> [] int {
    return h[25];
}

// Put the next batch on the pool if one is due: there is a setting, a live connection and no batch on the pool. Answers 1 if one was put. Nothing is sent until
// the pool's `flush`.
pub fn prune_start[&h, &q, &s](heap: &!h Heap, pl: &!q pool.Pool, hs: &!s [int], now_ms: int) -> [heap] int {
    if hs[21] == 0 || hs[23] == 1 || pool.live(pl) == 0 {
        return 0;
    }
    if hs[22] == 0 {
        hs[22] = now_ms + prune_first_ms();
    }
    if now_ms < hs[22] {
        return 0;
    }
    let request = queries.prune_attempts_start(heap, now_ms - hs[21] * 86400000, prune_batch());
    var code = 0 - 1;
    borrow request as &rb in {
        code = pool.submit(pl, prune_tag(), buffer.bytes(rb));
    }
    buffer.drop(heap, request);
    if code == 0 {
        hs[23] = 1;
        return 1;
    }
    hs[22] = now_ms + 1000;
    return 0;
}

// The batch `pool.next_done` last answered: count the rows it deleted, and say when the next may go.
pub fn prune_done[&q, &s](pl: &q pool.Pool, hs: &!s [int], now_ms: int) -> [] int {
    hs[23] = 0;
    var bad = pool.status(pl) != 0;
    if !bad && pg.failure(pool.reply(pl)) >= 0 {
        bad = true;
    }
    if bad {
        hs[25] = hs[25] + 1;
        hs[22] = now_ms + prune_every_ms();
        return 0;
    }
    let n = pg.affected(pool.reply(pl));
    if n > 0 {
        hs[24] = hs[24] + n;
    }
    if n >= prune_batch() {
        hs[22] = now_ms + 1000;
    } else {
        hs[22] = now_ms + prune_every_ms();
    }
    return 0;
}

// The tags of the requests on the pool: an insert is 1, and a request for the API (a query somebody is waiting for) is
// `query_base()` and above, each its own.
pub fn query_base() -> [] int {
    return 100;
}

// The request `pool.next_done` last answered was an insert: count it as written, or as failed if the reply is not a success.
pub fn account[&q, &s](pl: &q pool.Pool, hs: &!s [int]) -> [] int {
    var bad = pool.status(pl) != 0;
    if !bad {
        if pg.failure(pool.reply(pl)) >= 0 {
            bad = true;
        }
    }
    if bad {
        hs[5] = hs[5] + 1;
    } else {
        hs[4] = hs[4] + 1;
    }
    if hs[7] > 0 {
        hs[7] = hs[7] - 1;
    }
    return 0;
}

// Keep `live` and the pool's counters as the pool says they are.
pub fn sync[&q, &s](pl: &q pool.Pool, hs: &!s [int]) -> [] int {
    hs[2] = pool.live(pl);
    hs[8] = pool.connecting(pl);
    hs[9] = pool.reconnects(pl);
    hs[10] = pool.attempts(pl);
    hs[11] = pool.failures(pl);
    hs[12] = pool.losses(pl);
    hs[14] = pool.last_failure(pl);
    hs[15] = pool.last_loss(pl);
    return 0;
}

// An unpredictable client nonce for the SCRAM login: 18 bytes from the kernel, as base64.
pub fn fresh_nonce[&h, &f](heap: &!h Heap, fs: &f Fs("")) -> [heap, fs_read("")] buffer.Buffer {
    var nonce = buffer.empty(heap, 1);
    region a {
        let raw = alloc_slice[a](18, byte_of(0));
        let got = fs_read(fs, "/dev/urandom", raw);
        if got == 18 {
            buffer.drop(heap, nonce);
            nonce = pg.base64_encode(heap, raw);
        }
    }
    return nonce;
}

// What `login` answers when the login worked and a query could not be prepared (a table is missing): above every status of `pg.login`.
pub fn prepare_failed() -> [] int {
    return 16;
}

// Make the pool keep its connections (`pool.reconnect`): the login, the statements of `queries` prepared again on every connection, the waits
// between attempts, the time an attempt may take and the time a request may wait. The seed for the login's nonces is 32 bytes read from the
// kernel here; if the kernel gives fewer the seed is empty, which works for a server that trusts the connection or asks for a cleartext password
// and refuses a SCRAM-SHA-256 server (the pool's status 7, which `dbup.verdict` ends the start for). Answers the pool and 0, or -1 (a setting the pool
// refuses: the pool is as it was).
pub fn configure[&h, &f, &u, &w, &d](heap: &!h Heap, fs: &f Fs(""), pl: pool.Pool, user: &u [byte], password: &w [byte], database: &d [byte], min_ms: int, max_ms: int, attempt_ms: int, request_ms: int) -> [heap, fs_read("")] (pool.Pool, int) {
    let (script, count) = queries.prepare_script(heap);
    var made = pl;
    var code = 0;
    region a {
        let seed = alloc_slice[a](32, byte_of(0));
        let got = fs_read(fs, "/dev/urandom", seed);
        var kept = 32;
        if got != 32 {
            kept = 0;
        }
        borrow script as &sr in {
            let (grown, rc) = pool.reconnect(heap, made, user, password, database, seed[0..kept], buffer.bytes(sr), count, min_ms, max_ms, attempt_ms, request_ms);
            made = grown;
            code = rc;
        }
    }
    buffer.drop(heap, script);
    return (made, code);
}

// Log in on `conn` and prepare the queries: 0, or a nonzero status.
pub fn login[&h, &c, &u, &w, &d, &z](heap: &!h Heap, conn: &!c Conn, user: &u [byte], password: &w [byte], database: &d [byte], rng: &z Fs("")) -> [heap, conn_read, conn_write, fs_read("")] int {
    let nonce = fresh_nonce(heap, rng);
    var status = 0;
    borrow nonce as &nr in {
        let (reply, st) = pg.login(heap, conn, user, password, database, buffer.bytes(nr));
        buffer.drop(heap, reply);
        status = st;
    }
    buffer.drop(heap, nonce);
    if status != 0 {
        return status;
    }
    let (refused, prepared) = queries.prepare_all(heap, conn);
    var bad = 0;
    if prepared != 0 {
        bad = prepare_failed();
    }
    borrow refused as &fr in {
        if pg.failure(buffer.bytes(fr)) >= 0 {
            bad = prepare_failed();
        }
    }
    buffer.drop(heap, refused);
    return bad;
}
