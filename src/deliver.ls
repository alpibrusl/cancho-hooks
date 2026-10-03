edition 5;

module deliver;

// `deliver` -- one delivery attempt: connect, send a request, read the status line, give up after a deadline
// (`docs/design.md` sections 4 and 9).
//
// **What this blocks on, and what it does not.** The connect is `tcp_connect`, which blocks until the kernel answers: a
// receiver that does not answer the SYN stalls the whole service for as long as the kernel waits. That is the first
// predicted gap, kept deliberately so the scenario can measure it. The *read* does not block: the connection is made
// non-blocking and watched with a `Poller` for at most `timeout_ms`, so a receiver that accepts and then says nothing
// costs the deadline and no more.

// An attempt answers an HTTP status (100 and up) or one of these.
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

// Write all of `data`, or answer false.
fn send_all[&c, &d](conn: &!c Conn, data: &d [byte]) -> [conn_write] bool {
    var sent = 0;
    while sent < len(data) {
        match conn_write(conn, data[sent..len(data)]) {
            Sent::Wrote(n) => {
                sent = sent + n;
            }
            Sent::Again => {
                return false;
            }
            Sent::Failed(e) => {
                return false;
            }
        }
    }
    return true;
}

// Read until a status line is in `buf`, the peer closes, or `timeout_ms` has passed. `conn` is already non-blocking and
// registered in `poller` under token 1.
fn read_status[&c, &p, &k, &b, &e](conn: &!c Conn, poller: &!p Poller, clock: &k Clock, timeout_ms: int, buf: &!b [byte], events: &!e [int]) -> [conn_read, poll, clock] int {
    let deadline = clock_ms(clock) + timeout_ms;
    var have = 0;
    while true {
        let left = deadline - clock_ms(clock);
        if left <= 0 {
            return timed_out();
        }
        let ready = poller_wait(poller, events, left);
        if ready > 0 {
            match conn_read(conn, buf[have..len(buf)]) {
                Received::Data(n) => {
                    have = have + n;
                    let code = status_of(buf, have);
                    if code >= 100 {
                        return code;
                    }
                    if have >= 12 || have >= len(buf) {
                        return no_answer();
                    }
                }
                Received::End => {
                    return no_answer();
                }
                Received::Again => {
                    have = have;
                }
                Received::Failed(err) => {
                    return no_answer();
                }
            }
        }
    }
    return no_answer();
}

// One attempt: send `request` to `host:port` and answer the status of the reply, or a negative reason above. The
// connection is closed either way (`Connection: close`, so there is one request to one connection and no pool to go wrong).
pub fn attempt[&n, &h, &r, &k](net: &n Net(""), host: &h [byte], port: int, request: &r [byte], clock: &k Clock, timeout_ms: int) -> [net_out(""), conn_read, conn_write, poll, clock] int {
    var status = no_connect();
    match tcp_connect(net, host, port) {
        Dialed::Failed(e) => {
            return no_connect();
        }
        Dialed::Ok(c) => {
            var conn = c;
            match poller_new() {
                Polling::Failed(e) => {
                    conn_close(conn);
                    return no_connect();
                }
                Polling::Ok(p0) => {
                    var poller = p0;
                    borrow mut conn as &!ch in {
                        if !send_all(ch, request) {
                            status = no_send();
                        } else {
                            conn_nonblocking(ch);
                            borrow mut poller as &!pw in {
                                poller_add_conn(pw, ch, 1, 1);
                                region a {
                                    let buf = alloc_slice[a](256, byte_of(0));
                                    let events = alloc_slice[a](8, 0);
                                    status = read_status(ch, pw, clock, timeout_ms, buf, events);
                                }
                            }
                        }
                    }
                    poller_close(poller);
                    conn_close(conn);
                    return status;
                }
            }
        }
    }
}
