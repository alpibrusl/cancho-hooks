edition 5;

module attempt;

import std.conns;
import dns;
import destination;
import state;
import ossl;

// `attempt` -- delivery attempts that do not hold the loop (`docs/design.md` sections 16 and 40).
//
// An attempt is a small state machine. For an endpoint that is an IPv4 address it is what it always was: *connecting* (a connection started with
// `tcp_connect_start`, watched for writable), *sending* (the request, written as the kernel takes it), *reading* (the status line, as it arrives). Two phases
// come before those for the endpoints that are not that simple:
//
//     host is a name:       resolving (a TCP connection to the name server, the query, the answer; judged and pinned) -> connecting
//     scheme is https:      connecting -> handshake (TLS, over the same poller) -> sending -> reading
//
// Each state waits for the poller, so any number of attempts, up to `slots()`, are in flight together and none of them blocks the caller. The connections live
// in a `std.conns` table and are watched on the poller of the server that owns the loop, under tokens from `token0` up; the table's slot number is the
// attempt's slot. While a name is being resolved the slot's connection is the one to the name server; when the answer is in, `redial` closes it and puts the
// connection to the endpoint in the same slot, so that the token, the deadline and the slot's buffers mean the same thing for the whole attempt.
//
// **The rule that makes names safe** (`docs/design.md` section 26 and 40): the address a connection goes to is one the service looked at. A name is resolved by
// this module (never by `getaddrinfo`, which blocks the loop and would resolve a second time), **every** address of the answer must be public unless private
// hosts are allowed (one private address refuses the whole answer, `ssrf_refused()`), and `redial` connects to the first of them as a literal. Nothing resolves
// between the check and the connection, so there is nothing for a rebinding name to change. This happens at every attempt, because a name's address does.
//
// This module knows nothing about events, endpoints or retries: it dials, sends bytes, reads a status line, and answers what happened. What the answer means
// is `hooks.ls`'s.
//
// An attempt's per-slot numbers are `stride()` integers in one array, `at`, followed by the environment (`env_size()` integers: the TLS context, the name
// server, the policy, the saved sessions). The bytes of a slot are `slot_bytes()` in one byte array: the request (`req_max()`), the host name (`name_max()`),
// what is read from the socket (`ossl.net_max()`) and the ciphertext or the DNS message in flight (`ossl.out_max()`). The part of the response kept is
// `resp_max()` bytes per slot, enough for `HTTP/1.1 NNN`.

pub fn slots() -> [] int {
    return 64;
}

// The most bytes of request a slot holds: the largest event (65,487 bytes with the shortest type), the headers the delivery sets (about 300), a second
// signature (52) and the largest custom headers (2,048, `hdrs.max_wire()`), with room over.
pub fn req_max() -> [] int {
    return 70656;
}

pub fn resp_max() -> [] int {
    return 16;
}

fn stride() -> [] int {
    return 32;
}

// The bytes after the request in a slot: the name the certificate must carry, the scratch for what is read from the socket, the ciphertext or DNS buffer.
fn name_max() -> [] int {
    return 256;
}

fn name_at() -> [] int {
    return 70656;
}

fn net_at() -> [] int {
    return 70912;
}

fn io_at() -> [] int {
    return 75008;
}

pub fn slot_bytes() -> [] int {
    return 95488;
}

fn env_at() -> [] int {
    return 2048;
}

// The environment: 16 integers, then a saved session and the key it was saved for, for each endpoint slot there can be (`most_sessions()`: it was 64, and an endpoint
// in a slot above it was never resumed, without a word; `docs/design.md` section 41.4).
pub fn env_size() -> [] int {
    return 16 + 2 * most_sessions();
}

pub fn at_size() -> [] int {
    return 2048 + env_size();
}

pub fn req_size() -> [] int {
    return 64 * 95488;
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

// The name has been resolved and judged: the caller calls `redial` with the slot. Not an answer an attempt ends with.
pub fn resolved() -> [] int {
    return 0 - 101;
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

// The name did not resolve: no name server is known or it cannot be reached, it answered an error (no such name, server failure, refused), garbage, or a name with no
// IPv4 address (IPv6 is not asked for).
pub fn dns_failed() -> [] int {
    return 0 - 14;
}

// The deadline passed while the name was being resolved.
pub fn dns_timed_out() -> [] int {
    return 0 - 15;
}

// The destination is not allowed: the name (or the address) resolves to a private, loopback, link-local or reserved address (`destination.is_public`) and private
// hosts are not allowed. No connection was made to it.
pub fn ssrf_refused() -> [] int {
    return 0 - 16;
}

// The TLS handshake failed for a reason that is not a certificate's: the peer closed, spoke badly, offered nothing we accept (TLS before 1.2), or sent an alert.
pub fn tls_failed() -> [] int {
    return 0 - 17;
}

// The certificate chain does not lead to a trusted root (unknown issuer, self-signed, a chain that does not verify).
pub fn cert_untrusted() -> [] int {
    return 0 - 18;
}

// The certificate has expired or is not yet valid.
pub fn cert_expired() -> [] int {
    return 0 - 19;
}

// The certificate does not name the host.
pub fn cert_hostname() -> [] int {
    return 0 - 20;
}

// Any other reason a certificate was refused (a bad signature, an unusable CA, a purpose that does not fit).
pub fn cert_invalid() -> [] int {
    return 0 - 21;
}

// The deadline passed during the handshake: the peer does not answer it.
pub fn tls_timed_out() -> [] int {
    return 0 - 22;
}

// TLS failed after the handshake, or could not be started (no memory for a session).
pub fn tls_error() -> [] int {
    return 0 - 23;
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

fn dns_sending() -> [] int {
    return 4;
}

fn dns_reading() -> [] int {
    return 5;
}

fn handshaking() -> [] int {
    return 6;
}

fn redialing() -> [] int {
    return 7;
}

// A kept connection (`docs/design.md` section 53): *draining*, the rest of a response after its status line (the attempt has ended and its outcome is
// recorded; what follows only decides whether the connection is kept), *idle*, in the pool for the endpoint's next attempt, and *doomed*, to be closed by the
// next `sweep_parked` (its endpoint changed, or the slots are wanted). None of them is an attempt: `busy` is false for them and `parked` true.
fn draining() -> [] int {
    return 8;
}

fn idle() -> [] int {
    return 9;
}

fn doomed() -> [] int {
    return 10;
}

// The bounds of section 53.3 and 53.2.
fn idle_ms() -> [] int {
    return 30000;
}

fn life_ms() -> [] int {
    return 300000;
}

fn most_uses() -> [] int {
    return 1000;
}

fn head_max() -> [] int {
    return 8192;
}

fn body_max() -> [] int {
    return 65536;
}

// The slots kept free for a turn's starts (`hooks.most_starts`): idle connections never take them.
fn starts_room() -> [] int {
    return 16;
}

// The per-slot numbers: state, endpoint, event, deadline, request bytes sent, response bytes held, request length, flags (1 TLS, 2 resolving), the address the
// connection goes to (packed, 0 until known), the port, the length of the host name, DNS bytes sent, DNS bytes received, the DNS query's id, its length,
// then `ossl.fields()` integers of TLS state, then the kept connection's (section 53): whether this attempt's connection came from the pool, the body's framing
// (`m_*`), the body bytes still to come, the chunk parser's state, the body bytes so far, when the connection was made (`clock_ms`, 0 until the next
// `sweep_parked` sees it), how many requests it has carried, and the key it is pooled under.
fn f_reused() -> [] int {
    return 24;
}

fn f_mode() -> [] int {
    return 25;
}

fn f_left() -> [] int {
    return 26;
}

fn f_chunk() -> [] int {
    return 27;
}

fn f_body() -> [] int {
    return 28;
}

fn f_born() -> [] int {
    return 29;
}

fn f_uses() -> [] int {
    return 30;
}

fn f_key() -> [] int {
    return 31;
}

// The framings of a response body (section 53.2): not known yet, a `Content-Length`, `chunked`, none (a `204` or `304`).
fn m_unknown() -> [] int {
    return 0;
}

fn m_length() -> [] int {
    return 1;
}

fn m_chunked() -> [] int {
    return 2;
}

fn m_none() -> [] int {
    return 3;
}

fn f_flags() -> [] int {
    return 7;
}

fn f_addr() -> [] int {
    return 8;
}

fn f_port() -> [] int {
    return 9;
}

fn f_name() -> [] int {
    return 10;
}

fn f_dsent() -> [] int {
    return 11;
}

fn f_dgot() -> [] int {
    return 12;
}

fn f_did() -> [] int {
    return 13;
}

fn f_dlen() -> [] int {
    return 14;
}

fn f_tls() -> [] int {
    return 15;
}

// The environment: [TLS context, name server (packed, 0 none), its port, private hosts allowed, query counter, sessions on, handshakes, resumed, ...
// 8 more spare, then a saved session for each endpoint slot, then the key (name and port) it was saved for].
fn e_ctx() -> [] int {
    return 0;
}

fn e_ns() -> [] int {
    return 1;
}

fn e_ns_port() -> [] int {
    return 2;
}

fn e_private() -> [] int {
    return 3;
}

fn e_counter() -> [] int {
    return 4;
}

fn e_resume() -> [] int {
    return 5;
}

fn e_handshakes() -> [] int {
    return 6;
}

fn e_resumed() -> [] int {
    return 7;
}

// 1 when connections are kept (`keep-alive`, section 53), the connections dialled, and the attempts that went on a kept one.
fn e_keep() -> [] int {
    return 8;
}

fn e_dialled() -> [] int {
    return 9;
}

fn e_kept() -> [] int {
    return 10;
}

fn e_sessions() -> [] int {
    return 16;
}

fn e_keys() -> [] int {
    return 16 + most_sessions();
}

fn most_sessions() -> [] int {
    return state.max_endpoints();
}

// Set what the attempts share: the TLS context (`ossl.context`, 0 if there is none), the name server's address (packed, 0: names cannot be resolved) and port,
// whether private addresses are allowed, and whether sessions are kept for resumption. Called once, before the first attempt.
pub fn configure[&a](at: &!a [int], ctx: int, ns: int, ns_port: int, private: bool, resume: bool, keep: bool) -> [] int {
    at[env_at() + e_keep()] = 0;
    if keep {
        at[env_at() + e_keep()] = 1;
    }
    at[env_at() + e_ctx()] = ctx;
    at[env_at() + e_ns()] = ns;
    at[env_at() + e_ns_port()] = ns_port;
    if private {
        at[env_at() + e_private()] = 1;
    } else {
        at[env_at() + e_private()] = 0;
    }
    if resume {
        at[env_at() + e_resume()] = 1;
    } else {
        at[env_at() + e_resume()] = 0;
    }
    return 0;
}

// How many TLS handshakes have been made, and how many of them resumed a session.
pub fn handshakes[&a](at: &a [int]) -> [] int {
    return at[env_at() + e_handshakes()];
}

pub fn resumed[&a](at: &a [int]) -> [] int {
    return at[env_at() + e_resumed()];
}

// How many connections have been dialled, and how many attempts went on a kept one (section 53).
pub fn dialled[&a](at: &a [int]) -> [] int {
    return at[env_at() + e_dialled()];
}

pub fn kept[&a](at: &a [int]) -> [] int {
    return at[env_at() + e_kept()];
}

// Forget the saved session of endpoint slot `e` (its host, port, scheme or secret changed, or it was deleted): the next attempt makes a full handshake.
// (Not its kept connections: `keep_session` calls this to replace a session with the newer one. A change of the endpoint calls `retire` as well.)
pub fn drop_session[&f, &a](ffi: &f Ffi("libssl"), at: &!a [int], e: int) -> [ffi("libssl")] int {
    if e < 0 || e >= most_sessions() {
        return 0;
    }
    ossl.free_session(ffi, at[env_at() + e_sessions() + e]);
    at[env_at() + e_sessions() + e] = 0;
    at[env_at() + e_keys() + e] = 0;
    return 0;
}

// The kept connections of endpoint slot `e` are not offered again and are closed by the next `sweep_parked` (a change, a delete, a `410`: section 53.5).
pub fn retire[&a](at: &!a [int], e: int) -> [] int {
    var slot = 0;
    while slot < slots() {
        let b = slot * stride();
        if at[b] >= draining() && at[b + 1] == e {
            at[b] = doomed();
        }
        slot = slot + 1;
    }
    return 0;
}

// Free every saved session and the context, at the end of the process.
pub fn close_tls[&f, &a](ffi: &f Ffi("libssl"), at: &!a [int]) -> [ffi("libssl")] int {
    var e = 0;
    while e < most_sessions() {
        drop_session(ffi, at, e);
        e = e + 1;
    }
    ossl.free_context(ffi, at[env_at() + e_ctx()]);
    at[env_at() + e_ctx()] = 0;
    return 0;
}

pub fn busy[&a](at: &a [int], slot: int) -> [] bool {
    return slot >= 0 && slot < slots() && at[slot * stride()] != 0 && at[slot * stride()] < draining();
}

// Is the slot a kept connection (draining, idle or doomed) rather than an attempt (section 53)?
pub fn parked[&a](at: &a [int], slot: int) -> [] bool {
    return slot >= 0 && slot < slots() && at[slot * stride()] >= draining();
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

// Is the attempt in `slot` waiting to be redialed (its name has been resolved)?
pub fn redial_due[&a](at: &a [int], slot: int) -> [] bool {
    return busy(at, slot) && at[slot * stride()] == redialing();
}

// The reason the attempt in `slot` ended at its deadline, by where it was when the deadline passed. (Asked before `finish`.)
pub fn timeout_of[&a](at: &a [int], slot: int) -> [] int {
    let state = at[slot * stride()];
    if state == dns_sending() || state == dns_reading() || state == redialing() {
        return dns_timed_out();
    }
    if state == connecting() {
        return connect_timed_out();
    }
    if state == handshaking() {
        return tls_timed_out();
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

// What a failed handshake means: the `X509_V_ERR_*` number `ossl.detail_of` holds, or another number when verification was not what failed.
pub fn handshake_code(detail: int) -> [] int {
    if detail == 9 || detail == 10 {
        return cert_expired();
    }
    if detail == 62 || detail == 63 || detail == 64 {
        return cert_hostname();
    }
    if detail == 2 || detail == 18 || detail == 19 || detail == 20 || detail == 21 || detail == 27 {
        return cert_untrusted();
    }
    if detail > 0 && detail < 256 {
        return cert_invalid();
    }
    return tls_failed();
}

// A number that is the same for the same host name and port: what a saved session is filed under, so that a session is never offered to another server.
fn name_key[&n](name: &n [byte], port: int) -> [] int {
    var h = 1469598103 + port;
    var i = 0;
    while i < len(name) {
        h = (h * 16777619 + int_of(name[i]) + 1) % 4294967291;
        i = i + 1;
    }
    return h + 1;
}

// The packed address `a` as dotted decimal into `out`; answers the length.
fn dotted[&o](out: &!o [byte], a: int) -> [] int {
    return dns.put_dotted(out, 0, a);
}

// Start an attempt: copy `request` into the slot's buffer and begin. `host` is the endpoint's host as stored (`destination.ls`: an optional `https://`, then an IPv4
// address or a name). For an address (or `localhost`) the destination is judged and the connection dialled at once; for a name the connection to the name server is
// dialled and the query is made ready. In both cases the connection is watched for writable under token `token0 + slot`. Answers the table and `(slot, code)`:
// `slot` is where the attempt lives, or -1 if it could not be started, in which case `code` says why (`no_connect()` for a connection that failed at once or could
// not be watched, `ssrf_refused()` for a destination that is not allowed, `dns_failed()` for a name with no name server to ask, `no_send()` for a request too large
// for the slot; and nothing is left to clean up). With `slot >= 0` the code is `pending()`.
pub fn begin[&h, &n, &q, &r, &a, &p, &e](heap: &!h Heap, tab0: conns.Table, poller: &!p Poller, net: &n Net(""), host: &q [byte], port: int, request: &e [byte], at: &!a [int], req: &!r [byte], token0: int, endpoint: int, id: int, deadline: int) -> [heap, net_out(""), poll] (conns.Table, int, int) {
    let (tab, kept_slot, kept_code) = reuse(tab0, poller, host, port, request, at, req, token0, endpoint, id, deadline);
    if kept_slot >= 0 {
        return (tab, kept_slot, kept_code);
    }
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
    let secure = destination.is_https(host);
    let name = destination.bare(host);
    if len(name) < 1 || len(name) > name_max() - 1 {
        return (tab, 0 - 1, no_connect());
    }
    var addr = destination.address(name);
    var resolving = false;
    if addr < 0 && destination.is_localhost(name) {
        addr = 2130706433;
    }
    if addr < 0 {
        resolving = true;
    } else if at[env_at() + e_private()] == 0 && !destination.is_public(addr) {
        return (tab, 0 - 1, ssrf_refused());
    }
    if resolving && at[env_at() + e_ns()] == 0 {
        return (tab, 0 - 1, dns_failed());
    }
    if secure && at[env_at() + e_ctx()] == 0 {
        return (tab, 0 - 1, tls_error());
    }
    var dial = addr;
    var dial_port = port;
    if resolving {
        dial = at[env_at() + e_ns()];
        dial_port = at[env_at() + e_ns_port()];
    }
    var dialed = false;
    var conn_slot = 0 - 1;
    var table = tab;
    region text {
        let literal = alloc_slice[text](16, byte_of(0));
        let n_text = dotted(literal, dial);
        match tcp_connect_start(net, literal[0..n_text], dial_port) {
            Dialed::Failed(err) => {
            }
            Dialed::Ok(c) => {
                let (grown, put_slot) = conns.put(heap, table, c);
                table = grown;
                conn_slot = put_slot;
                dialed = true;
            }
        }
    }
    if !dialed {
        return (table, 0 - 1, no_connect());
    }
    let slot = conn_slot;
    if slot < 0 || slot >= slots() {
        // `put` could not ticket the connection (and closed it), or the table is larger than the arrays: leave.
        if slot >= 0 {
            borrow mut table as &!ct in {
                conns.close(ct, slot);
            }
        }
        return (table, 0 - 1, no_connect());
    }
    let b = slot * stride();
    let base = slot * slot_bytes();
    var qlen = 0;
    if resolving {
        // TCP DNS: a two-byte length, then the message. The id is the counter mixed with the clock's low bits by the caller's `deadline`, which differs per attempt.
        let counter = at[env_at() + e_counter()] + 1;
        at[env_at() + e_counter()] = counter;
        let query_id = (counter * 40503 + deadline) % 65536;
        qlen = dns.build_query(name, query_id, req[base + io_at()..base + io_at() + ossl.out_max()], 2);
        if qlen < 0 {
            borrow mut table as &!ct in {
                conns.close(ct, slot);
            }
            return (table, 0 - 1, dns_failed());
        }
        req[base + io_at()] = byte_of(qlen / 256 % 256);
        req[base + io_at() + 1] = byte_of(qlen % 256);
        at[b + f_did()] = query_id;
        at[b + f_dlen()] = qlen + 2;
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
        req[base + i] = request[i];
        i = i + 1;
    }
    i = 0;
    while i < len(name) {
        req[base + name_at() + i] = name[i];
        i = i + 1;
    }
    var flags = 0;
    if secure {
        flags = 1;
    }
    if resolving {
        flags = flags + 2;
    }
    at[b] = connecting();
    at[b + 1] = endpoint;
    at[b + 2] = id;
    at[b + 3] = deadline;
    at[b + 4] = 0;
    at[b + 5] = 0;
    at[b + 6] = len(request);
    at[b + f_flags()] = flags;
    at[b + f_addr()] = addr;
    at[b + f_port()] = port;
    at[b + f_name()] = len(name);
    at[b + f_dsent()] = 0;
    at[b + f_dgot()] = 0;
    var k = 0;
    while k < ossl.fields() {
        at[b + f_tls() + k] = 0;
        k = k + 1;
    }
    at[b + f_reused()] = 0;
    at[b + f_born()] = 0;
    at[b + f_uses()] = 1;
    at[b + f_key()] = pool_key(name, port, secure);
    at[env_at() + e_dialled()] = at[env_at() + e_dialled()] + 1;
    return (table, slot, pending());
}

// The key a connection is pooled under: the name (or address) and port it was made to, and whether it is TLS.
fn pool_key[&n](name: &n [byte], port: int, secure: bool) -> [] int {
    var k = name_key(name, port) * 2;
    if secure {
        k = k + 1;
    }
    return k;
}

// `begin` on a kept connection (section 53): the most recently idle one of this endpoint, made to this host and port in the same scheme, if there is one.
// The request is copied in and the attempt starts at *sending*, its connection (and TLS session) as it was. Answers the table and `(slot, pending())`, or
// `(-1, 0)` when there is none to take.
fn reuse[&p, &q, &e, &a, &r](tab: conns.Table, poller: &!p Poller, host: &q [byte], port: int, request: &e [byte], at: &!a [int], req: &!r [byte], token0: int, endpoint: int, id: int, deadline: int) -> [poll] (conns.Table, int, int) {
    if at[env_at() + e_keep()] != 1 || len(request) > req_max() {
        return (tab, 0 - 1, 0);
    }
    let secure = destination.is_https(host);
    let key = pool_key(destination.bare(host), port, secure);
    var found = 0 - 1;
    var slot = 0;
    while slot < slots() {
        let b = slot * stride();
        // The newest idle one: its deadline (when it would be closed for idleness) is the latest.
        if at[b] == idle() && at[b + 1] == endpoint && at[b + f_key()] == key && at[b + f_uses()] < most_uses() {
            if found < 0 || at[b + 3] > at[found * stride() + 3] {
                found = slot;
            }
        }
        slot = slot + 1;
    }
    if found < 0 {
        return (tab, 0 - 1, 0);
    }
    var table = tab;
    let b = found * stride();
    let base = found * slot_bytes();
    var watched = 0 - 1;
    borrow mut table as &!ct in {
        if secure {
            ossl.watching(at, b + f_tls(), 0);
            watched = ossl.want(ct, poller, at, b + f_tls(), found, token0 + found, 2);
        } else {
            watched = conns.rewatch(ct, poller, found, token0 + found, 2);
        }
    }
    if watched != 0 {
        at[b] = doomed();
        return (table, 0 - 1, 0);
    }
    var i = 0;
    while i < len(request) {
        req[base + i] = request[i];
        i = i + 1;
    }
    at[b] = sending();
    at[b + 2] = id;
    at[b + 3] = deadline;
    at[b + 4] = 0;
    at[b + 5] = 0;
    at[b + 6] = len(request);
    at[b + f_reused()] = 1;
    at[b + f_uses()] = at[b + f_uses()] + 1;
    at[env_at() + e_kept()] = at[env_at() + e_kept()] + 1;
    return (table, found, pending());
}

// The name in `slot` has been resolved and judged (`advance` answered `resolved()`): close the connection to the name server and connect to the address that was
// judged, in the same slot. Answers the table and `pending()`, or a code the attempt ends with (`no_connect()`); the caller finishes the slot then. There is no
// lookup here: the address is the one in the slot, and that is the whole point.
pub fn redial[&h, &n, &p, &a](heap: &!h Heap, tab: conns.Table, poller: &!p Poller, net: &n Net(""), at: &!a [int], slot: int, token0: int) -> [heap, net_out(""), poll] (conns.Table, int) {
    let b = slot * stride();
    var table = tab;
    borrow mut table as &!ct in {
        conns.close(ct, slot);
    }
    var connected = false;
    var put_slot = 0 - 1;
    region text {
        let literal = alloc_slice[text](16, byte_of(0));
        let n_text = dotted(literal, at[b + f_addr()]);
        match tcp_connect_start(net, literal[0..n_text], at[b + f_port()]) {
            Dialed::Failed(err) => {
            }
            Dialed::Ok(c) => {
                let (grown, s) = conns.put(heap, table, c);
                table = grown;
                put_slot = s;
                connected = true;
            }
        }
    }
    if !connected {
        return (table, no_connect());
    }
    if put_slot != slot {
        // The slot the connection was given is not the one the attempt lives in: not reachable while the table frees newest first and nothing else puts in between, and not
        // something to carry on from.
        if put_slot >= 0 {
            borrow mut table as &!ct in {
                conns.close(ct, put_slot);
            }
        }
        return (table, no_connect());
    }
    var watched = 0 - 1;
    borrow mut table as &!ct in {
        watched = conns.watch(ct, poller, slot, token0 + slot, 2);
        if watched != 0 {
            conns.close(ct, slot);
        }
    }
    if watched != 0 {
        return (table, no_connect());
    }
    at[b + f_flags()] = at[b + f_flags()] % 2;
    at[b] = connecting();
    return (table, pending());
}

// Judge the answer of the name server in the slot's buffer (`want` bytes at `answer`): every address must be public unless private hosts are allowed. Answers
// `resolved()` (the first address is in the slot), `ssrf_refused()` or `dns_failed()`.
fn judge[&a, &r](at: &!a [int], req: &!r [byte], slot: int, answer: int, want: int) -> [] int {
    let b = slot * stride();
    var result = dns_failed();
    region scratch {
        let found = alloc_slice[scratch](dns.addrs_size(), 0);
        let n = dns.parse(req[answer..answer + want], want, at[b + f_did()], found);
        if n > 0 {
            var refused = false;
            var k = 0;
            while k < n {
                if at[env_at() + e_private()] == 0 && !destination.is_public(found[k]) {
                    refused = true;
                }
                k = k + 1;
            }
            if refused {
                result = ssrf_refused();
            } else {
                at[b + f_addr()] = found[0];
                result = resolved();
            }
        }
    }
    return result;
}

// Move the attempt in `slot` along, after the poller reported its connection (`ready`: 1 readable, 2 writable). Answers `pending()` while it waits for more,
// `resolved()` when a name has been resolved and judged (the caller calls `redial`, which can fail like a start), otherwise how it ended: an HTTP status, or one of
// the negative codes. The caller then finishes the slot.
pub fn advance[&f, &t, &p, &a, &r, &s](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], resp: &!s [byte], slot: int, token0: int) -> [ffi("libcrypto"), ffi("libssl"), conn_read, conn_write, poll] int {
    let b = slot * stride();
    let base = slot * slot_bytes();
    let tb = b + f_tls();
    let secure = at[b + f_flags()] % 2 == 1;
    var progress = true;
    while progress {
        progress = false;
        if at[b] == connecting() {
            let failed = conns.connect_status(tab, slot);
            if failed != 0 && at[b + f_flags()] / 2 == 1 {
                // The connection that failed is the one to the name server: the name did not resolve, whatever the server's address did.
                if failed == etimedout() {
                    return dns_timed_out();
                }
                return dns_failed();
            }
            if failed == econnrefused() {
                return refused();
            }
            if failed == etimedout() {
                return connect_timed_out();
            }
            if failed != 0 {
                return no_connect();
            }
            if at[b + f_flags()] / 2 == 1 {
                at[b] = dns_sending();
            } else if secure {
                // The connection is made: start TLS on it. A session saved for this endpoint is offered if it was saved for this very name and port.
                let e = at[b + 1];
                var session = 0;
                if at[env_at() + e_resume()] == 1 && e >= 0 && e < most_sessions() {
                    let key = name_key(req[base + name_at()..base + name_at() + at[b + f_name()]], at[b + f_port()]);
                    if at[env_at() + e_sessions() + e] != 0 && at[env_at() + e_keys() + e] == key {
                        session = at[env_at() + e_sessions() + e];
                    }
                }
                if ossl.open(ffi, at[env_at() + e_ctx()], at, tb, req[base + name_at()..base + name_at() + at[b + f_name()]], session) != 0 {
                    return tls_error();
                }
                ossl.watching(at, tb, 2);
                at[b] = handshaking();
            } else {
                at[b] = sending();
            }
            progress = true;
        } else if at[b] == dns_sending() {
            match conns.write(tab, slot, req[base + io_at() + at[b + f_dsent()]..base + io_at() + at[b + f_dlen()]]) {
                Sent::Wrote(k) => {
                    at[b + f_dsent()] = at[b + f_dsent()] + k;
                    if at[b + f_dsent()] >= at[b + f_dlen()] {
                        at[b] = dns_reading();
                        if conns.rewatch(tab, poller, slot, token0 + slot, 1) != 0 {
                            return dns_failed();
                        }
                    }
                    progress = true;
                }
                Sent::Again => {
                    return pending();
                }
                Sent::Failed(err) => {
                    return dns_failed();
                }
            }
        } else if at[b] == dns_reading() {
            // The answer is a two-byte length then that many bytes, kept at 1,024 bytes into the slot's ciphertext buffer (the query is at its start).
            let answer = base + io_at() + 1024;
            match conns.read(tab, slot, req[answer + at[b + f_dgot()]..answer + 4098]) {
                Received::Data(k) => {
                    at[b + f_dgot()] = at[b + f_dgot()] + k;
                    if at[b + f_dgot()] >= 2 {
                        let want = int_of(req[answer]) * 256 + int_of(req[answer + 1]);
                        if want < 12 || want > 4096 {
                            return dns_failed();
                        }
                        if at[b + f_dgot()] >= want + 2 {
                            let verdict = judge(at, req, slot, answer + 2, want);
                            if verdict == resolved() {
                                at[b] = redialing();
                            }
                            return verdict;
                        }
                    }
                    progress = true;
                }
                Received::End => {
                    return dns_failed();
                }
                Received::Again => {
                    return pending();
                }
                Received::Failed(err) => {
                    return dns_failed();
                }
            }
        } else if at[b] == handshaking() {
            let hs = ossl.handshake(ffi, tab, poller, at, tb, req[base + io_at()..base + io_at() + ossl.out_max()], req[base + net_at()..base + net_at() + ossl.net_max()], slot, token0 + slot);
            if hs == ossl.pending() {
                return pending();
            }
            if hs == ossl.failed() {
                return handshake_code(ossl.detail_of(at, tb));
            }
            at[env_at() + e_handshakes()] = at[env_at() + e_handshakes()] + 1;
            if ossl.resumed(at, tb) {
                at[env_at() + e_resumed()] = at[env_at() + e_resumed()] + 1;
            }
            at[b] = sending();
            progress = true;
        } else if at[b] == sending() {
            let total = at[b + 6];
            if secure {
                let k = ossl.write(ffi, tab, poller, at, tb, req[base + io_at()..base + io_at() + ossl.out_max()], slot, token0 + slot, req[base + at[b + 4]..base + total]);
                if k > 0 {
                    at[b + 4] = at[b + 4] + k;
                    if at[b + 4] >= total {
                        at[b] = reading();
                        ossl.want(tab, poller, at, tb, slot, token0 + slot, 1);
                    }
                    progress = true;
                } else if k == 0 - 1 {
                    return pending();
                } else {
                    return gone(ffi, at, slot, no_send());
                }
            } else {
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
                        return gone(ffi, at, slot, no_send());
                    }
                }
            }
        } else if at[b] == reading() {
            // The response is read into the slot's request buffer, which is spent once the request is sent: the status line, and on a kept connection the
            // rest of the head and the body after it (section 53.2).
            let held = at[b + 5];
            var k = 0;
            if secure {
                k = ossl.read(ffi, tab, poller, at, tb, req[base + io_at()..base + io_at() + ossl.out_max()], req[base + net_at()..base + net_at() + ossl.net_max()], slot, token0 + slot, req[base + held..base + head_max()]);
            } else {
                match conns.read(tab, slot, req[base + held..base + head_max()]) {
                    Received::Data(n) => {
                        k = n;
                    }
                    Received::End => {
                        k = 0 - 3;
                    }
                    Received::Again => {
                        k = 0 - 1;
                    }
                    Received::Failed(err) => {
                        k = 0 - 2;
                    }
                }
            }
            if k > 0 {
                at[b + 5] = held + k;
                let code = status_of(req[base..base + head_max()], at[b + 5]);
                if code >= 100 {
                    if secure {
                        keep_session(ffi, at, req, slot);
                    }
                    if at[env_at() + e_keep()] == 1 {
                        // The outcome is decided; the rest of the response decides whether the connection is kept.
                        at[b] = draining();
                        at[b + f_mode()] = m_unknown();
                        at[b + f_left()] = 0;
                        at[b + f_chunk()] = 0;
                        at[b + f_body()] = 0;
                        drain(ffi, tab, poller, at, req, slot, token0);
                        if at[b] == doomed() {
                            // Not a connection to keep (its framing, or `Connection: close`): end it now, as one that is not kept always was, with
                            // `close_notify` (OpenSSL will not resume a session whose connection was freed without it: `sessions_test`).
                            at[b] = reading();
                            if secure {
                                ossl.shutdown(ffi, tab, at, tb, req[base + io_at()..base + io_at() + ossl.out_max()], slot);
                            }
                        }
                    } else if secure {
                        ossl.shutdown(ffi, tab, at, tb, req[base + io_at()..base + io_at() + ossl.out_max()], slot);
                    }
                    return code;
                }
                if at[b + 5] >= 12 {
                    return bad_response();
                }
                progress = true;
            } else if k == 0 - 1 {
                return pending();
            } else if k == 0 - 2 {
                if at[b + 5] == 0 {
                    return gone(ffi, at, slot, reset());
                }
                return reset();
            } else {
                if at[b + 5] == 0 {
                    return gone(ffi, at, slot, closed_early());
                }
                return closed_early();
            }
        }
    }
    return pending();
}

// The connection of `slot` has answered: keep its session for the endpoint's next attempt (the newest replaces the one before), if sessions are kept. With
// TLS 1.3 the ticket comes after the handshake and has been read by now, with the status line.
fn keep_session[&f, &a, &r](ffi: &f Ffi("libssl"), at: &!a [int], req: &!r [byte], slot: int) -> [ffi("libssl")] int {
    let b = slot * stride();
    let e = at[b + 1];
    if at[env_at() + e_resume()] != 1 || e < 0 || e >= most_sessions() {
        return 0;
    }
    let saved = ossl.save_session(ffi, at, b + f_tls());
    if saved == 0 {
        return 0;
    }
    drop_session(ffi, at, e);
    let base = slot * slot_bytes();
    at[env_at() + e_sessions() + e] = saved;
    at[env_at() + e_keys() + e] = name_key(req[base + name_at()..base + name_at() + at[b + f_name()]], at[b + f_port()]);
    return 0;
}

// End the attempt in `slot`. A connection that is being drained to be kept stays (section 53); otherwise its TLS state is freed, the connection closed
// and the slot freed.
pub fn finish[&f, &t, &a](ffi: &f Ffi("libssl"), tab: &!t conns.Table, at: &!a [int], slot: int) -> [ffi("libssl")] int {
    if at[slot * stride()] == draining() || at[slot * stride()] == idle() {
        return 0;
    }
    return close(ffi, tab, at, slot);
}

fn close[&f, &t, &a](ffi: &f Ffi("libssl"), tab: &!t conns.Table, at: &!a [int], slot: int) -> [ffi("libssl")] int {
    drop_tls(ffi, at, slot);
    conns.close(tab, slot);
    at[slot * stride()] = 0;
    return 0;
}

fn drop_tls[&f, &a](ffi: &f Ffi("libssl"), at: &!a [int], slot: int) -> [ffi("libssl")] int {
    ossl.drop(ffi, at, slot * stride() + f_tls());
    return 0;
}

// ---------------------------------------------------------------------
// Kept connections (`docs/design.md` section 53)
// ---------------------------------------------------------------------

// The race of section 53.4: an attempt on a kept connection failed before a byte of a response came. The receiver closed it while it was idle, so the attempt
// is not ended: it is made again at once on a new connection to the same address (`redial`, as after a name is resolved), once. On a new connection the
// failure is `code`, as it always was.
fn gone[&f, &a](ffi: &f Ffi("libssl"), at: &!a [int], slot: int, code: int) -> [ffi("libssl")] int {
    let b = slot * stride();
    if at[b + f_reused()] != 1 {
        return code;
    }
    drop_tls(ffi, at, slot);
    var k = 0;
    while k < ossl.fields() {
        at[b + f_tls() + k] = 0;
        k = k + 1;
    }
    at[b + 4] = 0;
    at[b + 5] = 0;
    at[b + f_reused()] = 0;
    at[b + f_born()] = 0;
    at[b + f_uses()] = 1;
    at[b] = redialing();
    at[env_at() + e_dialled()] = at[env_at() + e_dialled()] + 1;
    return resolved();
}

// The poller woke a kept connection: move a draining one along, and close an idle one that the receiver closed or wrote to (an idle connection has nothing
// to read). Answers nothing the caller acts on: a kept connection has no outcome.
pub fn tend[&f, &t, &p, &a, &r](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], slot: int, token0: int) -> [ffi("libcrypto"), ffi("libssl"), conn_read, conn_write, poll] int {
    let b = slot * stride();
    if at[b] == draining() {
        return drain(ffi, tab, poller, at, req, slot, token0);
    }
    if at[b] == idle() {
        let base = slot * slot_bytes();
        var k = 0;
        if at[b + f_flags()] % 2 == 1 {
            // Not read: an end the TLS library would read as a truncation could make it refuse to resume the session (OpenSSL does). Whatever woke an
            // idle connection, it is not kept.
            k = 0 - 3;
        } else {
            match conns.read(tab, slot, req[base..base + 16]) {
                Received::Data(n) => {
                    k = n;
                }
                Received::End => {
                    k = 0 - 3;
                }
                Received::Again => {
                    k = 0 - 1;
                }
                Received::Failed(err) => {
                    k = 0 - 2;
                }
            }
        }
        if k != 0 - 1 {
            at[b] = doomed();
        }
    }
    return 0;
}

// Read and discard the rest of the response on a draining connection, as far as it has come, by the framing its head gives (section 53.2). A response whose
// framing is not one this keeps, or that is longer than the bounds, or more than one response, dooms the connection; a complete one makes it idle, watched
// for readable.
fn drain[&f, &t, &p, &a, &r](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, poller: &!p Poller, at: &!a [int], req: &!r [byte], slot: int, token0: int) -> [ffi("libcrypto"), ffi("libssl"), conn_read, conn_write, poll] int {
    let b = slot * stride();
    let base = slot * slot_bytes();
    let secure = at[b + f_flags()] % 2 == 1;
    var going = true;
    while going && at[b] == draining() {
        if at[b + f_mode()] == m_unknown() {
            let end = head_end(req[base..base + head_max()], at[b + 5]);
            if end > 0 {
                let mode = framing(req[base..base + end], at, b);
                if mode < 0 {
                    at[b] = doomed();
                    return 0;
                }
                at[b + f_mode()] = mode;
                // What came after the head is the first of the body.
                if !feed_body(at, b, req[base + end..base + at[b + 5]]) {
                    at[b] = doomed();
                    return 0;
                }
            } else if at[b + 5] >= head_max() {
                at[b] = doomed();
                return 0;
            }
        }
        if at[b] == draining() && at[b + f_mode()] != m_unknown() && body_done(at, b) {
            at[b] = idle();
            // Idle from now: `sweep_parked` gives it its idle deadline at the next turn.
            at[b + 3] = 0;
            var watched = 0;
            if secure {
                // Registered again whatever the TLS module last noted: an idle connection woken for writable would read as the receiver's doing.
                ossl.watching(at, b + f_tls(), 0);
                watched = ossl.want(tab, poller, at, b + f_tls(), slot, token0 + slot, 1);
            } else {
                watched = conns.rewatch(tab, poller, slot, token0 + slot, 1);
            }
            if watched != 0 {
                at[b] = doomed();
            }
            return 0;
        }
        // More to read: the head into the buffer after what is held, the body into the scratch after the head's room.
        let into_head = at[b + f_mode()] == m_unknown();
        var k = 0;
        if into_head {
            if secure {
                k = ossl.read(ffi, tab, poller, at, b + f_tls(), req[base + io_at()..base + io_at() + ossl.out_max()], req[base + net_at()..base + net_at() + ossl.net_max()], slot, token0 + slot, req[base + at[b + 5]..base + head_max()]);
            } else {
                match conns.read(tab, slot, req[base + at[b + 5]..base + head_max()]) {
                    Received::Data(n) => {
                        k = n;
                    }
                    Received::End => {
                        k = 0 - 3;
                    }
                    Received::Again => {
                        k = 0 - 1;
                    }
                    Received::Failed(err) => {
                        k = 0 - 2;
                    }
                }
            }
            if k > 0 {
                at[b + 5] = at[b + 5] + k;
            }
        } else {
            if secure {
                k = ossl.read(ffi, tab, poller, at, b + f_tls(), req[base + io_at()..base + io_at() + ossl.out_max()], req[base + net_at()..base + net_at() + ossl.net_max()], slot, token0 + slot, req[base + head_max()..base + head_max() + 8192]);
            } else {
                match conns.read(tab, slot, req[base + head_max()..base + head_max() + 8192]) {
                    Received::Data(n) => {
                        k = n;
                    }
                    Received::End => {
                        k = 0 - 3;
                    }
                    Received::Again => {
                        k = 0 - 1;
                    }
                    Received::Failed(err) => {
                        k = 0 - 2;
                    }
                }
            }
            if k > 0 && !feed_body(at, b, req[base + head_max()..base + head_max() + k]) {
                at[b] = doomed();
                return 0;
            }
        }
        if k == 0 - 1 {
            going = false;
        } else if k <= 0 {
            // The receiver closed, or the connection failed: nothing to keep.
            at[b] = doomed();
            return 0;
        }
    }
    return 0;
}

// Where the head of `h` ends (just after the blank line), or 0 if it has not all come.
fn head_end[&h](h: &h [byte], n: int) -> [] int {
    var i = 3;
    while i < n {
        if int_of(h[i - 3]) == 13 && int_of(h[i - 2]) == 10 && int_of(h[i - 1]) == 13 && int_of(h[i]) == 10 {
            return i + 1;
        }
        i = i + 1;
    }
    return 0;
}

fn lower(c: int) -> [] int {
    if c >= 'A' && c <= 'Z' {
        return c + 32;
    }
    return c;
}

// Does the header line `h[at..end]` have the name `name` (lowercase, without the colon)? Answers where its value starts, past the colon and spaces, or -1.
fn header_value[&h, &n](h: &h [byte], at: int, end: int, name: &n [byte]) -> [] int {
    let k = len(name);
    if end - at < k + 1 || int_of(h[at + k]) != ':' {
        return 0 - 1;
    }
    var i = 0;
    while i < k {
        if lower(int_of(h[at + i])) != int_of(name[i]) {
            return 0 - 1;
        }
        i = i + 1;
    }
    var v = at + k + 1;
    while v < end && (int_of(h[v]) == ' ' || int_of(h[v]) == 9) {
        v = v + 1;
    }
    return v;
}

// Is `h[v..end]`, without trailing spaces, `word` (lowercase), compared without case?
fn value_is[&h, &w](h: &h [byte], v: int, end: int, word: &w [byte]) -> [] bool {
    var e = end;
    while e > v && (int_of(h[e - 1]) == ' ' || int_of(h[e - 1]) == 9) {
        e = e - 1;
    }
    if e - v != len(word) {
        return false;
    }
    var i = 0;
    while i < len(word) {
        if lower(int_of(h[v + i])) != int_of(word[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// The framing of a response whose head is `h` (status line and headers, ending in the blank line), or -1 for one this does not keep (section 53.2): not
// `HTTP/1.1`, a `1xx`, `Connection: close` (or any `Connection` naming close), a `Transfer-Encoding` that is not exactly `chunked`, a length with it, two
// `Content-Length`s, one that is not all digits or is over `body_max()`, or a body that only the end of the connection would end. Sets the length to come.
fn framing[&h, &a](h: &h [byte], at: &!a [int], b: int) -> [] int {
    if int_of(h[7]) != '1' {
        return 0 - 1;
    }
    let code = status_of(h, len(h));
    if code < 200 {
        return 0 - 1;
    }
    var length = 0 - 1;
    var chunked = false;
    var line = 0;
    while line < len(h) && int_of(h[line]) != 10 {
        line = line + 1;
    }
    line = line + 1;
    while line < len(h) - 2 {
        var end = line;
        while end < len(h) && int_of(h[end]) != 13 {
            end = end + 1;
        }
        let cl = header_value(h, line, end, "content-length");
        let te = header_value(h, line, end, "transfer-encoding");
        let cn = header_value(h, line, end, "connection");
        if cl >= 0 {
            var n = 0;
            var digits = 0;
            var v = cl;
            while v < end && int_of(h[v]) >= '0' && int_of(h[v]) <= '9' && n <= body_max() {
                n = n * 10 + (int_of(h[v]) - '0');
                digits = digits + 1;
                v = v + 1;
            }
            while v < end && (int_of(h[v]) == ' ' || int_of(h[v]) == 9) {
                v = v + 1;
            }
            if digits == 0 || v != end || n > body_max() || length >= 0 {
                return 0 - 1;
            }
            length = n;
        }
        if te >= 0 {
            if !value_is(h, te, end, "chunked") || chunked {
                return 0 - 1;
            }
            chunked = true;
        }
        if cn >= 0 {
            // A list of tokens: any `close` in it closes.
            var v = cn;
            while v + 5 <= end {
                if lower(int_of(h[v])) == 'c' && lower(int_of(h[v + 1])) == 'l' && lower(int_of(h[v + 2])) == 'o' && lower(int_of(h[v + 3])) == 's' && lower(int_of(h[v + 4])) == 'e' {
                    return 0 - 1;
                }
                v = v + 1;
            }
        }
        line = end + 2;
    }
    if code == 204 || code == 304 {
        if length > 0 || chunked {
            return 0 - 1;
        }
        return m_none();
    }
    if chunked && length >= 0 {
        return 0 - 1;
    }
    if chunked {
        return m_chunked();
    }
    if length >= 0 {
        at[b + f_left()] = length;
        return m_length();
    }
    return 0 - 1;
}

// Has the body all come?
fn body_done[&a](at: &a [int], b: int) -> [] bool {
    let mode = at[b + f_mode()];
    if mode == m_none() {
        return true;
    }
    if mode == m_length() {
        return at[b + f_left()] == 0;
    }
    return at[b + f_chunk()] == 9;
}

// Take `d`, the next bytes of the body, by the framing: false if it breaks it (more bytes than a length said, bytes after the end, a chunk size that is not
// hexadecimal, a body over `body_max()`). A chunked body is parsed a byte at a time; its state is `f_chunk`:
//     0 the size's digits   1 an extension, skipped to its CR   2 the LF after the size   3 the data   4 the CR after the data   5 its LF
//     6 a trailer line's start   7 a trailer line   8 the LF ending a trailer line, or the last one   9 the end
fn feed_body[&a, &d](at: &!a [int], b: int, d: &d [byte]) -> [] bool {
    let mode = at[b + f_mode()];
    if len(d) == 0 {
        return true;
    }
    if mode == m_none() {
        return false;
    }
    if mode == m_length() {
        if len(d) > at[b + f_left()] {
            return false;
        }
        at[b + f_left()] = at[b + f_left()] - len(d);
        return true;
    }
    var i = 0;
    while i < len(d) {
        let c = int_of(d[i]);
        let st = at[b + f_chunk()];
        if st == 0 {
            var digit = 0 - 1;
            if c >= '0' && c <= '9' {
                digit = c - '0';
            } else if lower(c) >= 'a' && lower(c) <= 'f' {
                digit = lower(c) - 'a' + 10;
            }
            if digit >= 0 {
                at[b + f_left()] = at[b + f_left()] * 16 + digit;
                if at[b + f_body()] + at[b + f_left()] > body_max() {
                    return false;
                }
            } else if c == ';' {
                at[b + f_chunk()] = 1;
            } else if c == 13 {
                at[b + f_chunk()] = 2;
            } else {
                return false;
            }
            i = i + 1;
        } else if st == 1 {
            if c == 13 {
                at[b + f_chunk()] = 2;
            }
            i = i + 1;
        } else if st == 2 {
            if c != 10 {
                return false;
            }
            if at[b + f_left()] == 0 {
                at[b + f_chunk()] = 6;
            } else {
                at[b + f_body()] = at[b + f_body()] + at[b + f_left()];
                at[b + f_chunk()] = 3;
            }
            i = i + 1;
        } else if st == 3 {
            var take = len(d) - i;
            if take > at[b + f_left()] {
                take = at[b + f_left()];
            }
            at[b + f_left()] = at[b + f_left()] - take;
            i = i + take;
            if at[b + f_left()] == 0 {
                at[b + f_chunk()] = 4;
            }
        } else if st == 4 {
            if c != 13 {
                return false;
            }
            at[b + f_chunk()] = 5;
            i = i + 1;
        } else if st == 5 {
            if c != 10 {
                return false;
            }
            at[b + f_chunk()] = 0;
            i = i + 1;
        } else if st == 6 {
            if c == 13 {
                at[b + f_chunk()] = 8;
                at[b + f_left()] = 1;
            } else {
                at[b + f_chunk()] = 7;
            }
            i = i + 1;
        } else if st == 7 {
            if c == 13 {
                at[b + f_chunk()] = 8;
                at[b + f_left()] = 0;
            }
            i = i + 1;
        } else if st == 8 {
            if c != 10 {
                return false;
            }
            // `f_left` 1: the blank line that ends the trailers, so the body has ended; 0: a trailer line, and another may follow.
            if at[b + f_left()] == 1 {
                at[b + f_chunk()] = 9;
            } else {
                at[b + f_chunk()] = 6;
            }
            i = i + 1;
        } else {
            // Bytes after the end: a second response nobody asked for.
            return false;
        }
    }
    return true;
}

// Each turn, with the clock: give a kept connection its idle deadline and its birth time when it has none, close the ones that are doomed, past their idle
// deadline, older than `life_ms()`, worn out (`most_uses()`) or draining past the attempt's deadline, and close the least recently idle while fewer than
// `starts_room()` slots are free, so that the pool never takes a slot from an attempt (section 53.3). Answers how many it closed.
pub fn sweep_parked[&f, &t, &a, &r](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, at: &!a [int], req: &!r [byte], now: int) -> [ffi("libcrypto"), ffi("libssl"), conn_write] int {
    var closed = 0;
    var held = 0;
    var slot = 0;
    while slot < slots() {
        let b = slot * stride();
        if at[b] != 0 {
            if at[b + f_born()] == 0 {
                at[b + f_born()] = now;
            }
            if at[b] == idle() && at[b + 3] == 0 {
                at[b + 3] = now + idle_ms();
            }
            var close_it = at[b] == doomed();
            if at[b] == idle() && (now >= at[b + 3] || now - at[b + f_born()] >= life_ms() || at[b + f_uses()] >= most_uses()) {
                close_it = true;
            }
            if at[b] == draining() && now >= at[b + 3] {
                close_it = true;
            }
            if close_it {
                quit(ffi, tab, at, req, slot);
                closed = closed + 1;
            } else {
                held = held + 1;
            }
        }
        slot = slot + 1;
    }
    while slots() - held < starts_room() {
        var oldest = 0 - 1;
        slot = 0;
        while slot < slots() {
            let b = slot * stride();
            if at[b] == idle() && (oldest < 0 || at[b + 3] < at[oldest * stride() + 3]) {
                oldest = slot;
            }
            slot = slot + 1;
        }
        if oldest < 0 {
            return closed;
        }
        quit(ffi, tab, at, req, oldest);
        closed = closed + 1;
        held = held - 1;
    }
    return closed;
}

// Close a kept connection. Over TLS, `close_notify` first (it is not waited for): a TLS library may refuse to resume a session whose connection ended
// without one (OpenSSL does: `sessions_test` found it), and it is the clean end of a connection whose last response was read whole.
fn quit[&f, &t, &a, &r](ffi: &f Ffi("libcrypto,libssl"), tab: &!t conns.Table, at: &!a [int], req: &!r [byte], slot: int) -> [ffi("libcrypto"), ffi("libssl"), conn_write] int {
    let b = slot * stride();
    if at[b + f_flags()] % 2 == 1 && at[b] != draining() {
        let base = slot * slot_bytes();
        ossl.shutdown(ffi, tab, at, b + f_tls(), req[base + io_at()..base + io_at() + ossl.out_max()], slot);
    }
    return close(ffi, tab, at, slot);
}
