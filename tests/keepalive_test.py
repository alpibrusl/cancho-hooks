#!/usr/bin/env python3
"""Kept connections (docs/design.md section 53), against a receiver that keeps them too.

    python3 tests/keepalive_test.py build/hooks
    python3 tests/keepalive_test.py pure/build/hooks-pure

The receiver is HTTP/1.1, plain or TLS (the test's own authority, `--tls-ca-file`), serves many requests on one connection, and counts connections,
requests and TLS handshakes. Its `mode` makes it answer as each check needs. The endpoints are in `endpoints.conf` (no database). Each check has a control:
the same thing that should not keep a connection, or keep-alive turned off.

  1. plain HTTP, a `Content-Length` answer: twenty deliveries go on one connection (two at most), each delivered once
  2. TLS: the same, with one handshake (two at most)
  3. `Connection: close` is honoured: a connection a delivery, all delivered
  4. the framings of section 53.2: chunked (with a trailer) and `204` keep the connection; HTTP/1.0, a length and `chunked` together, a body over 64 KiB
     and two lengths close it; every delivery is made either way
  5. a body that stalls after the status line: the delivery counts as made, the connection is closed, the next delivery is on a new one, nothing failed
  6. a receiver that closes idle connections after 0.5 s (Node.js's shape): deliveries a second apart are all made and none failed
  7. the race of section 53.4: on every reused connection the receiver reads the request and closes without answering; every event is delivered, none failed
  8. `keep-alive 0`: one connection a delivery
  9. many endpoints, each delivered to and leaving an idle connection: more endpoints than slots, every delivery made (idle connections never refuse a slot)
 10. the idle bound: a connection left idle is closed by the service after 30 s
 11. a burst over TLS (all events posted at once): at most as many connections as attempts in flight (8); a session kept for resumption does not close
     them (a bug the first build had: replacing the saved session retired the endpoint's connections)
 12. a `PATCH` (of the secret: same host and port) and a `410` each close the endpoint's kept connection (section 53.5; the PATCH with the database)
 13. two endpoints behind one receiver (the same host and port): every delivery made once, each on its own endpoint's connections; the receiver fails one
     endpoint's requests (told by their signature), and the dead letters are that endpoint's, none the other's (an outcome is never put on the endpoint whose
     connection carried it)
 14. the retry of section 53.4 is once: a receiver that answers once and then closes on every request costs each later attempt at most two requests
 15. framings that break, closed at once (not at the deadline): a body longer than its length, a header block over 8 KiB; a body that stalls is closed at the
     deadline, not left open
 16. a chunked body whose last CRLF comes later is waited for, and the connection kept
 17. bytes nobody asked for on an idle connection close it; every delivery made
 18. over TLS, the service ends what it closes with close_notify (the idle bound), so a TLS library keeps the session resumable (section 53.11)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402
from keepkit import Receiver  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


class Rig:
    def __init__(self, receivers, scheme="", name="127.0.0.1", dns=None, ca=None, extra=(), secrets=None):
        self.dir = L.free_dir("hooks-keepalive-")
        self.secrets = list(secrets) if secrets else [L.secret() for _ in receivers]
        with open(os.path.join(self.dir, "endpoints.conf"), "w") as f:
            for i, r in enumerate(receivers):
                f.write(f"{i + 1} {scheme}{name} {r.port} {self.secrets[i]}\n")
        args = ["--schedule", "200,400", "--deadline-ms", "2000", *extra]
        if dns:
            args += ["--dns-server", f"127.0.0.1:{dns.port}"]
        if ca:
            args += ["--tls-ca-file", ca]
        self.svc = L.Service(BIN, self.dir, args)

    def run(self, n, gap=0.0, secs=30):
        ok = self.svc.start()
        for k in range(n):
            self.svc.post_event(k)
            if gap:
                time.sleep(gap)
        done = L.wait_for(lambda: self.svc.stats()["delivered"] >= n, secs)
        return ok and done

    def stats(self):
        return self.svc.stats()

    def close(self):
        self.svc.stop()


def one(label, receiver_kw, n, rig_kw=None, gap=0.0, want_conns=None, more_conns=None):
    r = Receiver(**receiver_kw)
    rig = Rig([r], **(rig_kw or {}))
    ok = rig.run(n, gap=gap)
    st = rig.stats()
    rig.close()
    r.close()
    check(f"{label}: all {n} delivered, none failed", ok and st["delivered"] == n and st["failed"] == 0, str(st))
    if want_conns is not None:
        check(f"{label}: at most {want_conns} connections for {n} deliveries ({r.connections})", r.connections <= want_conns, str(r.connections))
    if more_conns is not None:
        check(f"{label}: a connection a delivery ({r.connections} for {n})", r.connections >= more_conns, str(r.connections))
    return r, st


def main():
    pki = K.Pki()
    cert = pki.leaf("hooks.test")
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    tls = {"scheme": "https://", "name": "hooks.test", "dns": dns, "ca": pki.ca_pem}

    # 1. plain HTTP
    one("1. plain HTTP, Content-Length", {}, 20, gap=0.05, want_conns=2)
    # 2. TLS
    r, _ = one("2. TLS", {"tls_cert": cert}, 20, rig_kw=tls, gap=0.05, want_conns=2)
    check(f"2. TLS: at most two handshakes for twenty deliveries ({r.handshakes})", r.handshakes <= 2, str(r.handshakes))
    # 3. Connection: close
    one("3. Connection: close", {"mode": "close"}, 10, gap=0.05, more_conns=10)
    one("3. Connection: close over TLS", {"tls_cert": cert, "mode": "close"}, 6, rig_kw=tls, gap=0.05, more_conns=6)
    # 4. framings
    one("4. chunked, with an extension and a trailer: kept", {"mode": "chunked"}, 10, gap=0.05, want_conns=2)
    one("4. chunked over TLS: kept", {"tls_cert": cert, "mode": "chunked"}, 10, rig_kw=tls, gap=0.05, want_conns=2)
    one("4. a 204 with no length: kept", {"mode": "empty204"}, 10, gap=0.05, want_conns=2)
    for mode in ("http10", "both", "huge", "twolengths"):
        one(f"4. {mode}: not kept", {"mode": mode}, 5, gap=0.05, more_conns=5)
    # 5. a body that stalls
    r, st = one("5. a stalled body: the outcome stands", {"mode": "stall"}, 3, gap=0.1, more_conns=3)
    # 6. a receiver that closes idle connections after half a second
    one("6. idle connections closed by the receiver after 0.5 s", {"idle_close": 0.5}, 6, gap=1.0)
    one("6. ... over TLS", {"tls_cert": cert, "idle_close": 0.5}, 4, rig_kw=tls, gap=1.0)
    # 7. the race
    r, st = one("7. a reused connection closed under the request", {"mode": "racy"}, 10, gap=0.05)
    check(f"7. ... the receiver did drop requests on reused connections ({r.dropped}), and each was made again on a new one", r.dropped > 0, str(r.dropped))
    r, st = one("7. ... over TLS", {"tls_cert": cert, "mode": "racy"}, 6, rig_kw=tls, gap=0.05)
    check(f"7. ... over TLS, requests dropped ({r.dropped})", r.dropped > 0, str(r.dropped))
    # 8. keep-alive 0
    one("8. keep-alive 0", {}, 10, rig_kw={"extra": ["--keep-alive", "0"]}, gap=0.05, more_conns=10)
    # 9. more endpoints than slots
    receivers = [Receiver() for _ in range(70)]
    rig = Rig(receivers)
    ok = rig.svc.start()
    for k in range(140):
        rig.svc.post_event(k)
    done = L.wait_for(lambda: rig.svc.stats()["delivered"] >= 140 * 70, 120)
    st = rig.stats()
    rig.close()
    for x in receivers:
        x.close()
    check(f"9. 70 endpoints, 140 events each delivered to all of them: every delivery made, none failed ({st['delivered']}, {st['failed']})",
          ok and done and st["failed"] == 0, str(st))
    # 10. the idle bound
    r = Receiver()
    rig = Rig([r])
    ok = rig.run(1)
    time.sleep(1.0)
    before = r.closed_by_peer
    closed = L.wait_for(lambda: r.closed_by_peer > before, 40)
    rig.close()
    r.close()
    check("10. an idle connection is closed by the service within 30 s (and some)", ok and closed, f"{ok} {r.closed_by_peer}")

    # 11. a burst over TLS
    r = Receiver(tls_cert=cert)
    rig = Rig([r], **tls)
    ok = rig.svc.start()
    for k in range(200):
        rig.svc.post_event(k)
    done = L.wait_for(lambda: rig.svc.stats()["delivered"] >= 200, 60)
    st = rig.stats()
    rig.close()
    r.close()
    check(f"11. a burst of 200 over TLS: all delivered on at most 8 connections ({r.connections}), none closed by the service early ({r.closed_by_peer})",
          ok and done and st["failed"] == 0 and r.connections <= 8, f"{st} {r.connections}")

    # 12. a 410 closes the kept connection (the endpoint is disabled)
    r = Receiver(mode="gone")
    rig = Rig([r])
    ok = rig.svc.start()
    rig.svc.post_event(1)
    closed = L.wait_for(lambda: r.closed_by_peer >= 1, 10)
    rig.close()
    r.close()
    check("12. a 410: the endpoint's kept connection is closed by the service", ok and closed, f"{ok} {r.closed_by_peer}")
    # ... and a PATCH, with the database
    if os.environ.get("HOOKS_PG"):
        L.apply_schema()
        L.psql("truncate endpoints, attempts")
        r = Receiver()
        d = L.free_dir("hooks-keepalive-pg-")
        token = "keepalive-test-token"
        svc = L.Service(BIN, d, ["--schedule", "200,400", "--deadline-ms", "2000", "--admin-token", token, *L.pg_flags()])
        L.psql(f"insert into endpoints (id, host, port, secret) values (1, '127.0.0.1', {r.port}, '{L.secret()}')")
        ok = svc.start()
        for k in range(3):
            svc.post_event(k)
            L.wait_for(lambda k=k: svc.stats()["delivered"] >= k + 1, 10)
        before = r.connections
        st, _, _ = svc.request("PATCH", "/endpoints/1", b'{"secret": "whsec_QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"}', {"Authorization": "Bearer " + token})
        svc.post_event(9)
        L.wait_for(lambda: svc.stats()["delivered"] >= 4, 10)
        svc.stop()
        r.close()
        check(f"12. a PATCH: the next delivery is on a new connection ({before} before, {r.connections} after)", ok and st == 200 and before == 1 and r.connections == 2,
              f"{ok} {st} {before} {r.connections}")
    else:
        print("skip 12. the PATCH check needs the database (HOOKS_PG)")

    # 13. two endpoints, one receiver
    r = Receiver()
    rig = Rig([r, r])
    ok = rig.svc.start()
    for k in range(20):
        rig.svc.post_event(k)
        time.sleep(0.05)
    done = L.wait_for(lambda: rig.svc.stats()["delivered"] >= 40, 30)
    time.sleep(0.5)
    st = rig.stats()
    rig.close()
    r.close()
    check(f"13. two endpoints, one receiver: 40 deliveries, none failed, 40 requests ({st['delivered']}, {st['failed']}, {r.requests})",
          ok and done and st["delivered"] == 40 and st["failed"] == 0 and r.requests == 40, str(st))
    check("13. ... each event reached the receiver twice, once an endpoint", all(r.ids.count(i) == 2 for i in set(r.ids)) and len(set(r.ids)) == 20,
          str(sorted(r.ids)))
    secrets = [L.secret(), L.secret()]
    r = Receiver(fail_key=secrets[1])
    rig = Rig([r, r], secrets=secrets)
    ok = rig.svc.start()
    for k in range(10):
        rig.svc.post_event(k)
        time.sleep(0.05)
    done = L.wait_for(lambda: rig.svc.stats()["delivered"] >= 10 and rig.svc.stats()["dead"] >= 10, 30)
    st = rig.stats()
    dead = {dict(lb).get("endpoint"): v for (name, lb), v in rig.svc.metrics().items() if name == "hooks_endpoint_dead_letters"}
    rig.close()
    r.close()
    check(f"13. ... one endpoint failed by the receiver: 10 delivered, 10 dead ({st['delivered']}, {st['dead']}), all the dead the failed endpoint's ({dead})",
          ok and done and st["delivered"] == 10 and st["dead"] == 10 and sorted(dead.values()) == [0, 10], f"{st} {dead}")

    # 14. the retry is once
    r = Receiver(mode="sulk")
    rig = Rig([r])
    ok = rig.svc.start()
    rig.svc.post_event(1)
    L.wait_for(lambda: rig.svc.stats()["delivered"] >= 1, 10)
    rig.svc.post_event(2)
    dead = L.wait_for(lambda: rig.svc.stats()["dead"] >= 1, 20)
    st = rig.stats()
    rig.close()
    r.close()
    # attempt 1 of event 2 is on the kept connection: one request there and one on a new connection; attempts 2 and 3 are on new connections, one each
    check(f"14. a receiver that closes on every request: event 2 dies after its 3 attempts, with at most 4 requests dropped ({r.dropped})",
          ok and dead and st["delivered"] == 1 and r.dropped <= 4, f"{st} {r.dropped}")

    # 15. closed at once, or at the deadline
    for mode in ("overlong", "bighead"):
        r, _ = one(f"15. {mode}: not kept", {"mode": mode}, 4, gap=0.3, more_conns=4)
        late = [round(x, 2) for x in r.close_after if x > 1.0]
        check(f"15. {mode}: each connection closed by the service within 1 s of the response ({len(r.close_after)} closes, late {late})",
              len(r.close_after) >= 3 and not late, str(r.close_after))
    r, _ = one("15. stall: the outcome stands", {"mode": "stall"}, 1)
    check(f"15. stall: the service closes the stalled connection at the deadline (2 s), not later ({[round(x, 2) for x in r.close_after]})",
          len(r.close_after) == 1 and r.close_after[0] < 4.0, str(r.close_after))

    # 16. the last CRLF of a chunked body, late
    one("16. a chunked body whose end comes 0.2 s later: kept", {"mode": "chunk_split"}, 6, gap=0.5, want_conns=2)

    # 17. bytes on an idle connection
    one("17. bytes nobody asked for on an idle connection", {"mode": "chatty"}, 5, gap=0.4, more_conns=5)

    # 18. close_notify on what the service closes over TLS
    r = Receiver(tls_cert=cert)
    rig = Rig([r], **tls)
    ok = rig.run(1)
    closed = L.wait_for(lambda: r.closed_by_peer >= 1, 40)
    rig.close()
    r.close()
    check(f"18. over TLS, the idle bound closes the connection with close_notify (clean {r.clean}, unclean {r.unclean})", ok and closed and r.clean == 1 and r.unclean == 0,
          f"{ok} {r.clean} {r.unclean}")
    return check.finish("keep-alive")


if __name__ == "__main__":
    sys.exit(main())
