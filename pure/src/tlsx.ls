edition 5;

module tlsx;

import std.conns;
import tls;
import tls_record;

// `tlsx` -- the TLS client of an `https` endpoint when the service is built with lex-sys's own TLS (`packages/tls`, lex-sys epic #197) and not OpenSSL
// (`docs/pure-tls.md`). It has the functions of `src/tls.ls` that `attempt.ls` calls, so the attempt's state machine is the same, and takes the engine where
// `src/tls.ls` takes the `Ffi`. It is not in `src/`: the pure build (`scripts/make_pure.py`) puts it beside the transformed copies of the other sources.
//
// **The authority is none.** Nothing here is foreign: the engine is lex-sys code, the socket is the `Conn` in the attempt's `std.conns.Table` as before, and the
// bytes move between the two here, as `tests/programs/tls_many.ls` of lex-sys does for 64 connections on one poller.
//
// The state of a connection is `fields()` integers in the caller's array, from index `b`, in the same places as `src/tls.ls` keeps them where they have the same
// meaning:
//
//     [live, ciphertext received and not yet taken: from, to (in the connection's `net` bytes), ciphertext sent of the pending chunk, ciphertext in the pending chunk,
//      what the socket is watched for, stage, detail, 1 if the session was resumed (never: there is no resumption)]

// Answers of a step.
pub fn done() -> [] int {
    return 0;
}

pub fn pending() -> [] int {
    return 1;
}

pub fn failed() -> [] int {
    return 2;
}

// Where a connection failed (`stage_of`).
pub fn stage_none() -> [] int {
    return 0;
}

pub fn stage_handshake() -> [] int {
    return 2;
}

pub fn stage_write() -> [] int {
    return 3;
}

pub fn stage_read() -> [] int {
    return 4;
}

pub fn stage_setup() -> [] int {
    return 5;
}

// How many integers a connection's state takes.
pub fn fields() -> [] int {
    return 9;
}

// Bytes of ciphertext a connection can hold that the kernel has not taken yet: one maximal TLS record with room to spare.
pub fn out_max() -> [] int {
    return 20480;
}

// Bytes read from the socket in one go and handed to the engine.
pub fn net_max() -> [] int {
    return 4096;
}

pub fn stage_of[&a](tt: &a [int], b: int) -> [] int {
    return tt[b + 6];
}

// For a failed connection, the number `attempt.handshake_code` reads, which is what `src/tls.ls` holds: the `X509_V_ERR_*` number for the certificate failures (the
// OpenSSL column of `docs/tls-pure.md` section 8 of lex-sys), -1 if the peer closed, 16,777,216 for any other protocol failure (OpenSSL's error numbers are that or
// more), and for a socket failure -1000 minus the `errno`.
pub fn detail_of[&a](tt: &a [int], b: int) -> [] int {
    return tt[b + 7];
}

pub fn live[&a](tt: &a [int], b: int) -> [] bool {
    return tt[b] != 0;
}

// There is no resumption (`docs/pure-tls.md`): a connection never resumes.
pub fn resumed[&a](tt: &a [int], b: int) -> [] bool {
    return tt[b + 8] == 1;
}

fn fail[&a](tt: &!a [int], b: int, stage: int, detail: int) -> [] int {
    tt[b + 6] = stage;
    tt[b + 7] = detail;
    return failed();
}

// The engine's refusal as the number `detail_of` holds.
fn detail_for(code: int) -> [] int {
    if code == tls_record.x509_expired() {
        return 10;
    }
    if code == tls_record.x509_not_yet_valid() {
        return 9;
    }
    if code == tls_record.x509_name_mismatch() {
        return 62;
    }
    if code == tls_record.x509_unknown_issuer() {
        return 20;
    }
    if code == tls_record.x509_decode() {
        return 6;
    }
    if code == tls_record.x509_bad_signature() {
        return 7;
    }
    if code == tls_record.x509_not_ca() {
        return 79;
    }
    if code == tls_record.x509_path_too_long() {
        return 25;
    }
    if code == tls_record.x509_name_constraint() {
        return 47;
    }
    if code == tls_record.x509_key_usage() {
        return 26;
    }
    if code == tls_record.x509_unsupported_algorithm() {
        return 76;
    }
    if code == tls_record.x509_key_size() {
        return 66;
    }
    if code == tls_record.x509_critical_extension() {
        return 34;
    }
    if code == tls_record.peer_closed() {
        return 0 - 1;
    }
    return 16777216;
}

// ---------------------------------------------------------------------
// One connection
// ---------------------------------------------------------------------

// Start TLS on a connection (the caller owns the socket) in the engine's slot `slot`: the server name for SNI and for the certificate check, and `now_ms` (Unix
// milliseconds) for the certificates' dates. `host` is a DNS name without a NUL. Answers 0, or `failed()` with the stage set to `stage_setup()` (nothing is left
// allocated).
pub fn open[&e, &a, &h](engine: &!e tls.Engine, slot: int, now_ms: int, tt: &!a [int], b: int, host: &h [byte]) -> [] int {
    if tt[b] != 0 || len(host) == 0 {
        return fail(tt, b, stage_setup(), 1);
    }
    let code = tls.start(engine, slot, host, now_ms);
    if code != 0 {
        return fail(tt, b, stage_setup(), detail_for(code));
    }
    tt[b] = 1;
    var k = 1;
    while k < fields() {
        tt[b + k] = 0;
        k = k + 1;
    }
    return 0;
}

// Free the slot, overwriting its secrets as far as the language allows. Safe on a connection that has none.
pub fn drop[&e, &a](engine: &!e tls.Engine, slot: int, tt: &!a [int], b: int) -> [] int {
    if tt[b] != 0 {
        tls.drop(engine, slot);
    }
    var k = 0;
    while k < fields() {
        tt[b + k] = 0;
        k = k + 1;
    }
    return 0;
}

// Watch the connection in `slot` for `events` (1 readable, 2 writable) if it is not already.
pub fn want[&t, &p, &a](tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, slot: int, token: int, events: int) -> [poll] int {
    if tt[b + 5] == events {
        return 0;
    }
    tt[b + 5] = events;
    return conns.rewatch(tab, poller, slot, token, events);
}

// Tell the connection what the socket is watched for, when the caller changed it without `want`.
pub fn watching[&a](tt: &!a [int], b: int, events: int) -> [] int {
    tt[b + 5] = events;
    return 0;
}

// Move ciphertext from the engine to the socket. 0: nothing is left; 1: the kernel would block (the rest is held); 2: the socket failed (detail = -1000 - errno).
fn flush[&e, &t, &a, &o](engine: &!e tls.Engine, tab: &!t conns.Table, tt: &!a [int], b: int, out: &!o [byte], slot: int) -> [conn_write] int {
    while true {
        if tt[b + 3] >= tt[b + 4] {
            let n = tls.take(engine, slot, out[0..out_max()]);
            if n <= 0 {
                tt[b + 3] = 0;
                tt[b + 4] = 0;
                return 0;
            }
            tt[b + 3] = 0;
            tt[b + 4] = n;
        }
        match conns.write(tab, slot, out[tt[b + 3]..tt[b + 4]]) {
            Sent::Wrote(k) => {
                tt[b + 3] = tt[b + 3] + k;
            }
            Sent::Again => {
                return 1;
            }
            Sent::Failed(e) => {
                tt[b + 7] = 0 - 1000 - e;
                return 2;
            }
        }
    }
    return 0;
}

// Give the engine ciphertext: what an earlier call read and it did not take, else a read of the socket. 1: some was given (or the engine refused it: its failure
// shows in `event`); 0: nothing to read yet; 2: the peer closed; 3: the socket failed (detail = -1000 - errno); 4: the engine took none of what it was offered, and
// has nothing to say, which a correct engine does not do (the caller fails the connection rather than loop).
fn pump[&e, &t, &a, &s](engine: &!e tls.Engine, tab: &!t conns.Table, tt: &!a [int], b: int, net: &!s [byte], slot: int) -> [conn_read] int {
    if tt[b + 1] < tt[b + 2] {
        let from = tt[b + 1];
        let to = tt[b + 2];
        let c = tls.feed(engine, slot, net[from..to]);
        if c < 0 {
            tt[b + 1] = 0;
            tt[b + 2] = 0;
            return 1;
        }
        if c == 0 {
            return 4;
        }
        if from + c >= to {
            tt[b + 1] = 0;
            tt[b + 2] = 0;
        } else {
            tt[b + 1] = from + c;
        }
        return 1;
    }
    match conns.read(tab, slot, net[0..net_max()]) {
        Received::Data(k) => {
            let c = tls.feed(engine, slot, net[0..k]);
            if c >= 0 && c < k {
                tt[b + 1] = c;
                tt[b + 2] = k;
            }
            return 1;
        }
        Received::Again => {
            return 0;
        }
        Received::End => {
            return 2;
        }
        Received::Failed(e) => {
            tt[b + 7] = 0 - 1000 - e;
            return 3;
        }
    }
}

// Run the handshake as far as it goes. `done()` (the session is established), `pending()` (the poller will say when; the connection is already watched for the
// direction it needs) or `failed()`.
pub fn handshake[&e, &t, &p, &a, &o, &s](engine: &!e tls.Engine, tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], net: &!s [byte], slot: int, token: int) -> [conn_read, conn_write, poll] int {
    while true {
        let fl = flush(engine, tab, tt, b, out, slot);
        if fl == 2 {
            return fail(tt, b, stage_handshake(), tt[b + 7]);
        }
        if fl == 1 {
            want(tab, poller, tt, b, slot, token, 2);
            return pending();
        }
        let ev = tls.event(engine, slot);
        if ev == tls.event_established() {
            return done();
        }
        if ev == tls.event_failed() {
            return fail(tt, b, stage_handshake(), detail_for(tls.failure(engine, slot)));
        }
        if ev == tls.event_closed() {
            return fail(tt, b, stage_handshake(), 0 - 1);
        }
        let step = pump(engine, tab, tt, b, net, slot);
        if step == 0 {
            want(tab, poller, tt, b, slot, token, 1);
            return pending();
        }
        if step == 2 {
            tls.eof(engine, slot);
            let after = tls.event(engine, slot);
            if after == tls.event_failed() {
                return fail(tt, b, stage_handshake(), detail_for(tls.failure(engine, slot)));
            }
            return fail(tt, b, stage_handshake(), 0 - 1);
        }
        if step == 3 {
            return fail(tt, b, stage_handshake(), tt[b + 7]);
        }
        if step == 4 {
            return fail(tt, b, stage_handshake(), 16777216);
        }
    }
    return pending();
}

// Encrypt and send up to one record of `data`. Answers the number of bytes of `data` accepted (more than 0), or a negative: -1 nothing yet (the poller will say
// when), -2 failed.
pub fn write[&e, &t, &p, &a, &o, &d](engine: &!e tls.Engine, tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], slot: int, token: int, data: &d [byte]) -> [conn_write, poll] int {
    // One record at a time: the previous one must be on its way before the next is made.
    let fl = flush(engine, tab, tt, b, out, slot);
    if fl == 2 {
        fail(tt, b, stage_write(), tt[b + 7]);
        return 0 - 2;
    }
    if fl == 1 {
        want(tab, poller, tt, b, slot, token, 2);
        return 0 - 1;
    }
    let n = tls.send(engine, slot, data);
    if n > 0 {
        let fl2 = flush(engine, tab, tt, b, out, slot);
        if fl2 == 2 {
            fail(tt, b, stage_write(), tt[b + 7]);
            return 0 - 2;
        }
        if fl2 == 1 {
            want(tab, poller, tt, b, slot, token, 2);
        }
        return n;
    }
    if n == 0 {
        // The engine's output queue is full: wait for the socket to take it.
        want(tab, poller, tt, b, slot, token, 2);
        return 0 - 1;
    }
    fail(tt, b, stage_write(), detail_for(n));
    return 0 - 2;
}

// Decrypt up to `len(into)` bytes. Answers the count (more than 0); 0 for the peer's `close_notify`; -1 nothing yet (watched readable); -2 failed; -3 the peer
// closed the connection with no `close_notify` (a truncation, or a server that does not send one).
pub fn read[&e, &t, &p, &a, &o, &s, &i](engine: &!e tls.Engine, tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], net: &!s [byte], slot: int, token: int, into: &!i [byte]) -> [conn_read, conn_write, poll] int {
    while true {
        let n = tls.recv(engine, slot, into);
        if n > 0 {
            return n;
        }
        if n == 0 {
            return 0;
        }
        if n != tls.would_block() {
            if n == tls_record.peer_closed() {
                return 0 - 3;
            }
            fail(tt, b, stage_read(), detail_for(n));
            return 0 - 2;
        }
        let fl = flush(engine, tab, tt, b, out, slot);
        if fl == 2 {
            fail(tt, b, stage_read(), tt[b + 7]);
            return 0 - 2;
        }
        if fl == 1 {
            want(tab, poller, tt, b, slot, token, 2);
            return 0 - 1;
        }
        let step = pump(engine, tab, tt, b, net, slot);
        if step == 0 {
            want(tab, poller, tt, b, slot, token, 1);
            return 0 - 1;
        }
        if step == 2 {
            // The socket ended: a clean end if the peer's `close_notify` came first (the next `recv` says 0), otherwise a truncation.
            if tls.eof(engine, slot) != 0 {
                return 0 - 3;
            }
        }
        if step == 3 {
            fail(tt, b, stage_read(), tt[b + 7]);
            return 0 - 2;
        }
        if step == 4 {
            fail(tt, b, stage_read(), 16777216);
            return 0 - 2;
        }
    }
    return 0 - 2;
}

// Send `close_notify` and push it to the socket as far as the kernel takes it now; does not wait for the peer's. The caller closes the connection after.
pub fn shutdown[&e, &t, &a, &o](engine: &!e tls.Engine, tab: &!t conns.Table, tt: &!a [int], b: int, out: &!o [byte], slot: int) -> [conn_write] int {
    tls.finish(engine, slot);
    flush(engine, tab, tt, b, out, slot);
    return 0;
}

// ---------------------------------------------------------------------
// Sessions: there are none (`docs/pure-tls.md`)
// ---------------------------------------------------------------------

pub fn save_session[&e, &a](engine: &!e tls.Engine, tt: &a [int], b: int) -> [] int {
    return 0;
}

pub fn free_session[&e](engine: &!e tls.Engine, session: int) -> [] int {
    return 0;
}

// ---------------------------------------------------------------------
// The engine's entropy and trust store, once, before the first attempt
// ---------------------------------------------------------------------

// The most a bundle of certificates may take, in bytes of PEM. A file that fills this is refused, never truncated: `fs_read` fills as much as the file has, so a
// bundle that fills the buffer may have more.
fn bundle_max() -> [] int {
    return 2097152;
}

// Load the roots of the PEM bundle at `path`: how many, or 0 (the file cannot be read, is too large, or holds no certificate).
fn load[&e, &f, &p, &o](engine: &!e tls.Engine, fs: &f Fs(""), path: &p [byte], into: &!o [byte]) -> [fs_read("")] int {
    let n = fs_read(fs, path, into);
    if n < 1 || n >= len(into) {
        return 0;
    }
    let roots = tls.trust(engine, into[0..n]);
    if roots < 1 {
        return 0;
    }
    return roots;
}

// Seed the engine from `/dev/urandom` and load the trust store: exactly the file `cafile` if it is not empty, otherwise the first of the usual places that holds a
// certificate (Debian and Ubuntu, Red Hat and Fedora, Alpine and macOS, openSUSE). **The environment is not read** (`SSL_CERT_FILE` and `SSL_CERT_DIR` are not
// honoured: lex-sys reads no environment variable without a foreign call), so a trust store other than the system's is named by `tls-ca-file`. Answers the number
// of roots, or 0 if the engine could not be seeded or no store was found: the service does not start (`status 21`), and never runs a client that does not verify.
pub fn setup[&e, &h, &f, &c](engine: &!e tls.Engine, heap: &!h Heap, fs: &f Fs(""), cafile: &c [byte]) -> [heap, fs_read("")] int {
    var seeded = false;
    region r {
        let entropy = alloc_slice[r](32, byte_of(0));
        // (bound first: an `fs_read` used directly as an operand is a code generation failure of the LLVM backend)
        let got = fs_read(fs, "/dev/urandom", entropy);
        if got == 32 {
            if tls.seed(engine, entropy) == 0 {
                seeded = true;
            }
        }
        var k = 0;
        while k < 32 {
            entropy[k] = byte_of(0);
            k = k + 1;
        }
    }
    if !seeded {
        return 0;
    }
    let pem = box_slice(heap, bundle_max(), byte_of(0));
    var roots = 0;
    borrow mut pem as &!pw in {
        let buf = contents(pw);
        if len(cafile) > 0 {
            roots = load(engine, fs, cafile, buf);
        } else {
            roots = load(engine, fs, "/etc/ssl/certs/ca-certificates.crt", buf);
            if roots == 0 {
                roots = load(engine, fs, "/etc/pki/tls/certs/ca-bundle.crt", buf);
            }
            if roots == 0 {
                roots = load(engine, fs, "/etc/ssl/cert.pem", buf);
            }
            if roots == 0 {
                roots = load(engine, fs, "/etc/ssl/ca-bundle.pem", buf);
            }
        }
    }
    unbox_slice(heap, pem);
    return roots;
}
