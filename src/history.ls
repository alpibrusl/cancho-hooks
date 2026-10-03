edition 5;

module history;

import std.buffer;
import pg;
import pg.pool;
import queries;

// `history` -- the attempts that ended, written to PostgreSQL (`docs/design.md` section 24).
//
// The delivery logs stay the truth about delivery. This is the record a person or the API reads, so it is **best effort**
// and it must never slow delivery down: an attempt that ended is *pushed* onto a ring in memory (no effect, no waiting), and
// once a turn `drain` turns what is in the ring into requests on the pool, whose connections are non-blocking and sit in the
// service's own poller. When the database is slow, the ring fills; when it is gone, rows are counted and dropped. Delivery
// does not notice either.
//
// The state is `size()` integers, which the caller owns (a slice of the delivery state):
//
//     [0] head: where the next row goes     [1] tail: where the next row to send is      [2] live connections
//     [3] enabled (a database was given)    [4] rows written   [5] rows the database refused or lost   [6] rows dropped
//     [7] requests submitted and not answered yet
//     [16 ...] the ring: `cap()` rows of eight integers: endpoint, event, replay, attempt, outcome, status, at (ms), latency (ms)

pub fn cap() -> [] int {
    return 256;
}

pub fn size() -> [] int {
    return 16 + cap() * 8;
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

// Say that a database was given (`live` connections of it were opened).
pub fn enable[&s](h: &!s [int], live: int) -> [] int {
    h[3] = 1;
    h[2] = live;
    return 0;
}

// An attempt ended: remember it, to be written. Does nothing when no database was given. When the ring is full the row is
// dropped, and counted.
pub fn push[&s](h: &!s [int], endpoint: int, event: int, replay: int, attempt: int, outcome: int, status: int, at_ms: int, latency_ms: int) -> [] int {
    if h[3] != 1 {
        return 0;
    }
    if h[0] - h[1] >= cap() {
        h[6] = h[6] + 1;
        return 0;
    }
    let base = 16 + h[0] % cap() * 8;
    h[base] = endpoint;
    h[base + 1] = event;
    h[base + 2] = replay;
    h[base + 3] = attempt;
    h[base + 4] = outcome;
    h[base + 5] = status;
    h[base + 6] = at_ms;
    h[base + 7] = latency_ms;
    h[0] = h[0] + 1;
    return 1;
}

// Turn the rows in the ring into requests on `pl`, as many as it takes, at most `most` this turn. A row the pool has no room
// for stays in the ring for the next turn; a row it cannot take at all (no connection is live, or it is too large) is dropped,
// and counted. Nothing is sent until the pool's `flush`.
pub fn drain[&h, &q, &s](heap: &!h Heap, pl: &!q pool.Pool, hs: &!s [int], most: int) -> [heap] int {
    var sent = 0;
    var going = true;
    while going && sent < most && hs[0] > hs[1] {
        let base = 16 + hs[1] % cap() * 8;
        let request = queries.add_attempt_start(heap, hs[base], hs[base + 1], hs[base + 2], hs[base + 3], hs[base + 4], hs[base + 5], hs[base + 6], hs[base + 7]);
        var code = 0 - 1;
        borrow request as &rb in {
            code = pool.submit(pl, 1, buffer.bytes(rb));
        }
        buffer.drop(heap, request);
        if code == 0 {
            hs[7] = hs[7] + 1;
            hs[1] = hs[1] + 1;
            sent = sent + 1;
        } else if code == 0 - 1 {
            going = false;
        } else {
            hs[6] = hs[6] + 1;
            hs[1] = hs[1] + 1;
        }
    }
    return sent;
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

// Keep `live` as the pool says it is.
pub fn sync[&q, &s](pl: &q pool.Pool, hs: &!s [int]) -> [] int {
    hs[2] = pool.live(pl);
    return 0;
}

// An unpredictable client nonce for the SCRAM login: 18 bytes from the kernel, as base64.
fn fresh_nonce[&h, &f](heap: &!h Heap, fs: &f Fs("")) -> [heap, fs_read("")] buffer.Buffer {
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

// Log in on `conn` and prepare the queries: 0, or a nonzero status.
fn login[&h, &c, &u, &w, &d, &z](heap: &!h Heap, conn: &!c Conn, user: &u [byte], password: &w [byte], database: &d [byte], rng: &z Fs("")) -> [heap, conn_read, conn_write, fs_read("")] int {
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
    var bad = prepared;
    borrow refused as &fr in {
        if pg.failure(buffer.bytes(fr)) >= 0 {
            bad = 6;
        }
    }
    buffer.drop(heap, refused);
    return bad;
}

// Open `lanes` connections to `host:port`, log in and prepare, and put them in a pool. Answers the pool and how many of them
// are live (0 to `lanes`): a connection that could not be opened is not an error here, the service goes on without it.
pub fn open[&h, &n, &t, &u, &w, &d, &z](heap: &!h Heap, net: &n Net(""), host: &t [byte], port: int, user: &u [byte], password: &w [byte], database: &d [byte], lanes: int, rng: &z Fs("")) -> [heap, net_out(""), conn_read, conn_write, fs_read("")] (pool.Pool, int) {
    var pl = pool.empty(heap, lanes, 64, 131072, 131072);
    var added = 0;
    var k = 0;
    while k < lanes {
        match tcp_connect(net, host, port) {
            Dialed::Ok(dialed) => {
                var conn = dialed;
                var s = 5;
                borrow mut conn as &!ch in {
                    s = login(heap, ch, user, password, database, rng);
                }
                if s == 0 {
                    let (grown, slot) = pool.add(heap, pl, conn);
                    pl = grown;
                    if slot >= 0 {
                        added = added + 1;
                    }
                } else {
                    conn_close(conn);
                }
            }
            Dialed::Failed(e) => {
            }
        }
        k = k + 1;
    }
    return (pl, added);
}
