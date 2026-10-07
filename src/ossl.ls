edition 5;

module ossl;

import std.conns;

// `ossl` -- the TLS client of an `https` endpoint: OpenSSL driven in steps, for one thread and one `Poller` (`docs/design.md` section 40).
//
// **This is foreign code.** Every function below that reaches OpenSSL names its library, `libssl` (`SSL_*`, `TLS_*`) or `libcrypto` (`BIO_*`, `ERR_*`,
// `X509_*`), and the authority report (`scripts/check-authority.sh`, `docs/authority.json`) lists the exact `library:symbol` pairs the service can call.
// That list is the whole of what the service may do with OpenSSL. The TLS of lex-sys itself (`packages/tls`, lex-sys epic #197) replaces this module
// when it is ready to verify certificates, and the symbols go with it. This module is the lex-sys spike's `examples/tls_nb/tls.ls` (lex-sys
// `docs/tls-nonblocking.md`), changed in the ways section 40.3 lists: the scopes are per library, there is no direct-socket transport and no
// `signal`, verification cannot be turned off, and the per-connection state lives in the caller's array.
//
// **No file descriptor reaches OpenSSL.** A connection is two memory BIOs: the bytes read from the socket go into one, the bytes OpenSSL wants sent come
// out of the other, and the socket stays a `Conn` in the attempt's `std.conns.Table`. So OpenSSL never writes to a socket itself and a peer that closed
// is an error code and not a `SIGPIPE`.
//
// **Every handle is an `int`.** An `SSL *` is a 64-bit pointer returned in a register; `c_ptr` cannot be named in an ordinary signature (lex-sys gap 1).
// A C `int` result is `c_int`, which sign-extends: `-1` read as `int` is 4294967295.
//
// The state of a connection is `fields()` integers in an array the caller owns, from index `b`:
//
//     [ssl, read BIO, write BIO, ciphertext sent of the pending chunk, ciphertext in the pending chunk, what the socket is watched for, stage, detail, 1 if the session was resumed]

// ---------------------------------------------------------------------
// libssl
// ---------------------------------------------------------------------
extern fn TLS_client_method[&f](ffi: &f Ffi("libssl")) -> [ffi("libssl")] int;

extern fn SSL_CTX_new[&f](ffi: &f Ffi("libssl"), method: int) -> [ffi("libssl")] int;

extern fn SSL_CTX_free[&f](ffi: &f Ffi("libssl"), ctx: int) -> [ffi("libssl")] int;

// `SSL_CTX_set_min_proto_version`, `SSL_CTX_set_mode` and `SSL_CTX_set_session_cache_mode` are macros over this; the last argument is a `void *`, NULL for all three.
extern fn SSL_CTX_ctrl[&f](ffi: &f Ffi("libssl"), ctx: int, cmd: int, larg: int, parg: int) -> [ffi("libssl")] int;

extern fn SSL_CTX_set_verify[&f](ffi: &f Ffi("libssl"), ctx: int, mode: int, callback: int) -> [ffi("libssl")] int;

extern fn SSL_CTX_set_default_verify_paths[&f](ffi: &f Ffi("libssl"), ctx: int) -> [ffi("libssl")] c_int;

// OpenSSL 3.0's one-string form of `SSL_CTX_load_verify_locations`, which has two strings and so cannot be declared (lex-sys gap 4). The path is NUL-terminated.
extern fn SSL_CTX_load_verify_file[&f, &p](ffi: &f Ffi("libssl"), ctx: int, path: &p [byte]) -> [ffi("libssl")] c_int;

extern fn SSL_new[&f](ffi: &f Ffi("libssl"), ctx: int) -> [ffi("libssl")] int;

extern fn SSL_free[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] int;

extern fn SSL_set_bio[&f](ffi: &f Ffi("libssl"), ssl: int, rbio: int, wbio: int) -> [ffi("libssl")] int;

extern fn SSL_set_connect_state[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] int;

extern fn SSL_do_handshake[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] c_int;

extern fn SSL_read[&f, &b](ffi: &f Ffi("libssl"), ssl: int, buf: &!b [byte]) -> [ffi("libssl")] c_int;

extern fn SSL_write[&f, &b](ffi: &f Ffi("libssl"), ssl: int, buf: &b [byte]) -> [ffi("libssl")] c_int;

extern fn SSL_shutdown[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] c_int;

extern fn SSL_get_error[&f](ffi: &f Ffi("libssl"), ssl: int, ret: int) -> [ffi("libssl")] c_int;

// `SSL_set_tlsext_host_name(ssl, name)` is a macro over this call: command 55, name type 0 (`TLSEXT_NAMETYPE_host_name`), and the NUL-terminated name as the `void *`.
extern fn SSL_ctrl[&f, &b](ffi: &f Ffi("libssl"), ssl: int, cmd: int, larg: int, parg: &b [byte]) -> [ffi("libssl")] int;

extern fn SSL_get0_param[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] int;

extern fn SSL_get_verify_result[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] int;

extern fn SSL_session_reused[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] c_int;

extern fn SSL_get1_session[&f](ffi: &f Ffi("libssl"), ssl: int) -> [ffi("libssl")] int;

extern fn SSL_set_session[&f](ffi: &f Ffi("libssl"), ssl: int, session: int) -> [ffi("libssl")] c_int;

extern fn SSL_SESSION_is_resumable[&f](ffi: &f Ffi("libssl"), session: int) -> [ffi("libssl")] c_int;

extern fn SSL_SESSION_free[&f](ffi: &f Ffi("libssl"), session: int) -> [ffi("libssl")] int;

// ---------------------------------------------------------------------
// libcrypto
// ---------------------------------------------------------------------
extern fn BIO_s_mem[&f](ffi: &f Ffi("libcrypto")) -> [ffi("libcrypto")] int;

extern fn BIO_new[&f](ffi: &f Ffi("libcrypto"), method: int) -> [ffi("libcrypto")] int;

extern fn BIO_read[&f, &b](ffi: &f Ffi("libcrypto"), bio: int, buf: &!b [byte]) -> [ffi("libcrypto")] c_int;

extern fn BIO_write[&f, &b](ffi: &f Ffi("libcrypto"), bio: int, buf: &b [byte]) -> [ffi("libcrypto")] c_int;

extern fn ERR_clear_error[&f](ffi: &f Ffi("libcrypto")) -> [ffi("libcrypto")] int;

extern fn ERR_get_error[&f](ffi: &f Ffi("libcrypto")) -> [ffi("libcrypto")] int;

// `X509_VERIFY_PARAM_set1_host(param, name, namelen)`: a pointer and a length, which is how a byte slice crosses, so no NUL is needed.
extern fn X509_VERIFY_PARAM_set1_host[&f, &b](ffi: &f Ffi("libcrypto"), param: int, name: &b [byte]) -> [ffi("libcrypto")] c_int;

extern fn X509_VERIFY_PARAM_set_hostflags[&f](ffi: &f Ffi("libcrypto"), param: int, flags: int) -> [ffi("libcrypto")] int;

// ---------------------------------------------------------------------
// The numbers
// ---------------------------------------------------------------------

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

// Bytes of ciphertext a connection can hold that the kernel has not taken yet: one maximal TLS record (16,384 bytes of plaintext plus up to 325 of header, MAC and
// padding) with room to spare.
pub fn out_max() -> [] int {
    return 20480;
}

// Bytes read from the socket in one go and handed to OpenSSL.
pub fn net_max() -> [] int {
    return 4096;
}

pub fn stage_of[&a](tt: &a [int], b: int) -> [] int {
    return tt[b + 6];
}

// For a failed handshake: the `X509_V_ERR_*` number if verification failed (9 not yet valid, 10 expired, 18 self-signed, 19 self-signed in the chain,
// 20 unknown issuer, 62 host name mismatch, ...), otherwise the first OpenSSL error on the queue (a number of 16 million or more), or -1 if the peer closed.
// For a socket failure: -1000 minus the `errno` (so that it is not taken for a verification result).
pub fn detail_of[&a](tt: &a [int], b: int) -> [] int {
    return tt[b + 7];
}

pub fn live[&a](tt: &a [int], b: int) -> [] bool {
    return tt[b] != 0;
}

// Did this connection resume a session (an abbreviated handshake)? Known once the handshake is done.
pub fn resumed[&a](tt: &a [int], b: int) -> [] bool {
    return tt[b + 8] == 1;
}

fn fail[&a](tt: &!a [int], b: int, stage: int, detail: int) -> [] int {
    tt[b + 6] = stage;
    tt[b + 7] = detail;
    return failed();
}

// ---------------------------------------------------------------------
// The context: one per process, shared by every connection
// ---------------------------------------------------------------------

// Make the client context: certificates are verified (`SSL_VERIFY_PEER`; there is no way to turn it off), the minimum protocol is TLS 1.2, partial writes are
// enabled (`SSL_write` takes one record at a time and never waits for a retry with the same buffer), a connection's 16 KiB buffers are freed while it is idle, and
// the library's own cache of sessions is off (the service keeps one session per endpoint itself: `attempt.ls`). The trust store is the system's default
// locations if `cafile` is empty, and otherwise exactly the PEM file `cafile` (NUL-terminated), not the system's as well. Answers the context, or 0, and nothing
// is leaked.
pub fn context[&f, &c](ffi: &f Ffi("libssl"), cafile: &c [byte]) -> [ffi("libssl")] int {
    let method = TLS_client_method(ffi);
    if method == 0 {
        return 0;
    }
    let ctx = SSL_CTX_new(ffi, method);
    if ctx == 0 {
        return 0;
    }
    // SSL_CTRL_SET_MIN_PROTO_VERSION = 123, TLS1_2_VERSION = 0x0303.
    if SSL_CTX_ctrl(ffi, ctx, 123, 771, 0) != 1 {
        SSL_CTX_free(ffi, ctx);
        return 0;
    }
    // SSL_CTRL_MODE = 33; ENABLE_PARTIAL_WRITE 1, ACCEPT_MOVING_WRITE_BUFFER 2, RELEASE_BUFFERS 16.
    SSL_CTX_ctrl(ffi, ctx, 33, 19, 0);
    // SSL_CTRL_SET_SESS_CACHE_MODE = 44, SSL_SESS_CACHE_OFF = 0.
    SSL_CTX_ctrl(ffi, ctx, 44, 0, 0);
    SSL_CTX_set_verify(ffi, ctx, 1, 0);
    if len(cafile) > 0 {
        if SSL_CTX_load_verify_file(ffi, ctx, cafile) != 1 {
            SSL_CTX_free(ffi, ctx);
            return 0;
        }
    } else if SSL_CTX_set_default_verify_paths(ffi, ctx) != 1 {
        SSL_CTX_free(ffi, ctx);
        return 0;
    }
    return ctx;
}

pub fn free_context[&f](ffi: &f Ffi("libssl"), ctx: int) -> [ffi("libssl")] int {
    if ctx != 0 {
        SSL_CTX_free(ffi, ctx);
    }
    return 0;
}

// ---------------------------------------------------------------------
// One connection
// ---------------------------------------------------------------------

// Start TLS on a connection (the caller owns the socket): a new `SSL` over two memory BIOs, the server name for SNI and for the certificate check. `host` is a
// DNS name without a NUL; it is what the certificate must name, and it is independent of the address the socket was connected to. `session` is 0 or a session
// saved from an earlier connection to the same server. Answers 0, or `failed()` with the stage set to `stage_setup()` (nothing is left allocated).
pub fn open[&f, &a, &h](ffi: &f Ffi("libcrypto,libssl"), ctx: int, tt: &!a [int], b: int, host: &h [byte], session: int) -> [ffi("libcrypto"), ffi("libssl")] int {
    if tt[b] != 0 || len(host) == 0 {
        return fail(tt, b, stage_setup(), 1);
    }
    ERR_clear_error(ffi);
    let ssl = SSL_new(ffi, ctx);
    if ssl == 0 {
        return fail(tt, b, stage_setup(), 2);
    }
    let rbio = BIO_new(ffi, BIO_s_mem(ffi));
    let wbio = BIO_new(ffi, BIO_s_mem(ffi));
    if rbio == 0 || wbio == 0 {
        // A BIO that was made and not handed to the SSL is not freed by `SSL_free`; there is no `BIO_free` declared, and a failed `BIO_new` means the process is
        // out of memory, so this path is reported and not recovered.
        SSL_free(ffi, ssl);
        return fail(tt, b, stage_setup(), 3);
    }
    SSL_set_bio(ffi, ssl, rbio, wbio);
    SSL_set_connect_state(ffi, ssl);
    var ok = true;
    region r {
        let name = alloc_slice[r](len(host) + 1, byte_of(0));
        var i = 0;
        while i < len(host) {
            name[i] = host[i];
            i = i + 1;
        }
        // SSL_CTRL_SET_TLSEXT_HOSTNAME = 55, TLSEXT_NAMETYPE_host_name = 0.
        if SSL_ctrl(ffi, ssl, 55, 0, name) != 1 {
            ok = false;
        }
    }
    if !ok {
        SSL_free(ffi, ssl);
        return fail(tt, b, stage_setup(), 4);
    }
    let param = SSL_get0_param(ffi, ssl);
    // X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS = 4.
    X509_VERIFY_PARAM_set_hostflags(ffi, param, 4);
    if X509_VERIFY_PARAM_set1_host(ffi, param, host) != 1 {
        SSL_free(ffi, ssl);
        return fail(tt, b, stage_setup(), 5);
    }
    if session != 0 {
        // An abbreviated handshake if the server still accepts it; otherwise the full one, verified as always.
        SSL_set_session(ffi, ssl, session);
    }
    tt[b] = ssl;
    tt[b + 1] = rbio;
    tt[b + 2] = wbio;
    tt[b + 3] = 0;
    tt[b + 4] = 0;
    tt[b + 5] = 0;
    tt[b + 6] = 0;
    tt[b + 7] = 0;
    tt[b + 8] = 0;
    return 0;
}

// Free the `SSL` and both BIOs. Safe on a connection that has none.
pub fn drop[&f, &a](ffi: &f Ffi("libssl"), tt: &!a [int], b: int) -> [ffi("libssl")] int {
    if tt[b] != 0 {
        SSL_free(ffi, tt[b]);
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

// Move ciphertext from the write BIO to the socket. 0: nothing is left; 1: the kernel would block (the rest is held); 2: the socket failed (detail = errno).
fn flush[&f, &t, &a, &o](ffi: &f Ffi("libcrypto"), tab: &!t conns.Table, tt: &!a [int], b: int, out: &!o [byte], slot: int) -> [ffi("libcrypto"), conn_write] int {
    while true {
        if tt[b + 3] >= tt[b + 4] {
            let n = BIO_read(ffi, tt[b + 2], out[0..out_max()]);
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

// Move ciphertext from the socket to the read BIO. 1: some was fed; 0: nothing to read yet; 2: the peer closed; 3: the socket failed (detail = errno).
fn feed[&f, &t, &a, &s](ffi: &f Ffi("libcrypto"), tab: &!t conns.Table, tt: &!a [int], b: int, net: &!s [byte], slot: int) -> [ffi("libcrypto"), conn_read] int {
    match conns.read(tab, slot, net[0..net_max()]) {
        Received::Data(k) => {
            BIO_write(ffi, tt[b + 1], net[0..k]);
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

// The reason a handshake failed, as one number: the verification result if it is not OK, otherwise the first error on the queue.
fn why[&f, &a](ffi: &f Ffi("libcrypto,libssl"), tt: &!a [int], b: int) -> [ffi("libcrypto"), ffi("libssl")] int {
    let v = SSL_get_verify_result(ffi, tt[b]);
    let e = ERR_get_error(ffi);
    ERR_clear_error(ffi);
    if v != 0 {
        return v;
    }
    return e;
}

// What a failed `SSL_*` call means for the step that made it. `stage` is where we were.
fn ssl_failed[&f, &a](ffi: &f Ffi("libcrypto,libssl"), tt: &!a [int], b: int, stage: int, code: int) -> [ffi("libcrypto"), ffi("libssl")] int {
    if code == 5 {
        // SSL_ERROR_SYSCALL: with memory BIOs this is the peer closing without close_notify (the error queue is empty).
        ERR_clear_error(ffi);
        return fail(tt, b, stage, 0 - 1);
    }
    return fail(tt, b, stage, why(ffi, tt, b));
}

// Run the handshake as far as it goes. `done()` (the session is established), `pending()` (the poller will say when; the connection is already watched for the
// direction it needs) or `failed()`.
pub fn handshake[&f, &t, &p, &a, &o, &s](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], net: &!s [byte], slot: int, token: int) -> [ffi("libcrypto"), ffi("libssl"), conn_read, conn_write, poll] int {
    while true {
        let fl = flush(ffi, tab, tt, b, out, slot);
        if fl == 2 {
            return fail(tt, b, stage_handshake(), tt[b + 7]);
        }
        if fl == 1 {
            want(tab, poller, tt, b, slot, token, 2);
            return pending();
        }
        ERR_clear_error(ffi);
        let r = SSL_do_handshake(ffi, tt[b]);
        if r == 1 {
            flush(ffi, tab, tt, b, out, slot);
            if SSL_session_reused(ffi, tt[b]) == 1 {
                tt[b + 8] = 1;
            }
            return done();
        }
        let e = SSL_get_error(ffi, tt[b], r);
        if e == 2 || e == 3 {
            // OpenSSL wants input. What it has to say goes out first.
            let fl2 = flush(ffi, tab, tt, b, out, slot);
            if fl2 == 2 {
                return fail(tt, b, stage_handshake(), tt[b + 7]);
            }
            if fl2 == 1 {
                want(tab, poller, tt, b, slot, token, 2);
                return pending();
            }
            let fd = feed(ffi, tab, tt, b, net, slot);
            if fd == 0 {
                want(tab, poller, tt, b, slot, token, 1);
                return pending();
            }
            if fd == 2 {
                return fail(tt, b, stage_handshake(), 0 - 1);
            }
            if fd == 3 {
                return fail(tt, b, stage_handshake(), tt[b + 7]);
            }
        } else {
            // Whatever alert OpenSSL wants to send is flushed, best effort, before the connection is closed.
            let failure = ssl_failed(ffi, tt, b, stage_handshake(), e);
            flush(ffi, tab, tt, b, out, slot);
            return failure;
        }
    }
    return pending();
}

// Encrypt and send up to one record of `data`. Answers the number of bytes of `data` accepted (more than 0), or a negative: -1 nothing yet (the poller will say
// when), -2 failed.
pub fn write[&f, &t, &p, &a, &o, &d](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], slot: int, token: int, data: &d [byte]) -> [ffi("libcrypto"), ffi("libssl"), conn_write, poll] int {
    // One record at a time: the previous one must be on its way before the next is made.
    let fl = flush(ffi, tab, tt, b, out, slot);
    if fl == 2 {
        fail(tt, b, stage_write(), tt[b + 7]);
        return 0 - 2;
    }
    if fl == 1 {
        want(tab, poller, tt, b, slot, token, 2);
        return 0 - 1;
    }
    ERR_clear_error(ffi);
    let r = SSL_write(ffi, tt[b], data);
    if r > 0 {
        let fl2 = flush(ffi, tab, tt, b, out, slot);
        if fl2 == 2 {
            fail(tt, b, stage_write(), tt[b + 7]);
            return 0 - 2;
        }
        if fl2 == 1 {
            want(tab, poller, tt, b, slot, token, 2);
        }
        return r;
    }
    let e = SSL_get_error(ffi, tt[b], r);
    if e == 2 || e == 3 {
        // A renegotiation or a post-handshake message needs input first.
        want(tab, poller, tt, b, slot, token, 1);
        return 0 - 1;
    }
    ssl_failed(ffi, tt, b, stage_write(), e);
    return 0 - 2;
}

// Decrypt up to `len(into)` bytes. Answers the count (more than 0); 0 for the peer's `close_notify`; -1 nothing yet (watched readable); -2 failed; -3 the peer
// closed the connection with no `close_notify` (a truncation, or a server that does not send one).
pub fn read[&f, &t, &p, &a, &o, &s, &i](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, tt: &!a [int], b: int, out: &!o [byte], net: &!s [byte], slot: int, token: int, into: &!i [byte]) -> [ffi("libcrypto"), ffi("libssl"), conn_read, conn_write, poll] int {
    while true {
        ERR_clear_error(ffi);
        let r = SSL_read(ffi, tt[b], into);
        if r > 0 {
            return r;
        }
        let e = SSL_get_error(ffi, tt[b], r);
        if e == 6 {
            return 0;
        }
        if e == 2 || e == 3 {
            let fl = flush(ffi, tab, tt, b, out, slot);
            if fl == 2 {
                fail(tt, b, stage_read(), tt[b + 7]);
                return 0 - 2;
            }
            if fl == 1 {
                want(tab, poller, tt, b, slot, token, 2);
                return 0 - 1;
            }
            let fd = feed(ffi, tab, tt, b, net, slot);
            if fd == 0 {
                want(tab, poller, tt, b, slot, token, 1);
                return 0 - 1;
            }
            if fd == 2 {
                return 0 - 3;
            }
            if fd == 3 {
                fail(tt, b, stage_read(), tt[b + 7]);
                return 0 - 2;
            }
        } else {
            if e == 5 {
                ERR_clear_error(ffi);
                return 0 - 3;
            }
            ssl_failed(ffi, tt, b, stage_read(), e);
            return 0 - 2;
        }
    }
    return 0 - 2;
}

// Send `close_notify` and push it to the socket as far as the kernel takes it now; does not wait for the peer's. The caller closes the connection after.
pub fn shutdown[&f, &t, &a, &o](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, tt: &!a [int], b: int, out: &!o [byte], slot: int) -> [ffi("libcrypto"), ffi("libssl"), conn_write] int {
    ERR_clear_error(ffi);
    SSL_shutdown(ffi, tt[b]);
    ERR_clear_error(ffi);
    flush(ffi, tab, tt, b, out, slot);
    return 0;
}

// ---------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------

// A reference to the session of the connection, to give `open` for a later connection to the same server, or 0 if it cannot be resumed (with TLS 1.3 the ticket
// arrives after the handshake, so ask after the first read). The caller owns the reference: `free_session`.
pub fn save_session[&f, &a](ffi: &f Ffi("libssl"), tt: &a [int], b: int) -> [ffi("libssl")] int {
    let session = SSL_get1_session(ffi, tt[b]);
    if session == 0 {
        return 0;
    }
    if SSL_SESSION_is_resumable(ffi, session) != 1 {
        SSL_SESSION_free(ffi, session);
        return 0;
    }
    return session;
}

pub fn free_session[&f](ffi: &f Ffi("libssl"), session: int) -> [ffi("libssl")] int {
    if session != 0 {
        SSL_SESSION_free(ffi, session);
    }
    return 0;
}
