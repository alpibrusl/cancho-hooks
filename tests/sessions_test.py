#!/usr/bin/env python3
"""TLS sessions, kept for resumption (docs/design.md section 40; production.md P1.7, slice T3).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/sessions_test.py build/hooks      (its tables `endpoints` and `attempts` are emptied)

A delivery to an `https` endpoint costs a handshake, because the service closes after each response (`Connection: close`). Resuming a session the endpoint gave on an earlier
delivery makes that handshake abbreviated (half the CPU, `scripts/bench/https_cost.py`). The service keeps one session per endpoint, in memory, and offers it only to the same
name and port. What is checked here is seen from the receiver's side, which counts the handshakes that resumed one:

  1. the first delivery to an endpoint is a full handshake and each one after it resumes (TLS 1.3 and TLS 1.2, and in slot 1000); `tls-resume 0` never resumes. The pure
     build (`pure/build/hooks-pure`, docs/pure-tls.md) resumes TLS 1.3 only, by lex-sys's design (its `docs/tls-resumption.md` §3 rule 7): its TLS 1.2 deliveries are six full
     handshakes
  2. a session is the endpoint's own: a second endpoint, behind the same receiver, starts with a full handshake of its own, and resumes its own after that
  3. the session is dropped when the endpoint changes: `PATCH` of the host, of the port, of the secret, and a `DELETE` followed by a new endpoint (which may have the old one's
     slot) each make the next delivery a full handshake, verified against the trust store again
  4. a receiver that no longer accepts the session (it was restarted: new ticket keys) is delivered to, with a full handshake and no failure
  5. no leaks, and memory that is bounded: after a thousand deliveries and a thousand failed handshakes the process holds the same descriptors and (within a few MiB: OpenSSL fills
     its tables once) the same memory as after the first hundred
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
# The build with lex-sys's own TLS (docs/pure-tls.md), which resumes TLS 1.3 only.
PURE = os.path.basename(BIN) == "hooks-pure"
TOKEN = "sessions-test-token"
check = L.Checks()


class Rig:
    def __init__(self, pki, dns, extra=(), private=True):
        self.pki, self.dns = pki, dns
        self.dir = L.free_dir("hooks-sessions-")
        args = ["--schedule", "60000", "--deadline-ms", "3000", "--dns-server", f"127.0.0.1:{dns.port}", "--tls-ca-file", pki.ca_pem, "--admin-token", TOKEN, *L.pg_flags(), *extra]
        self.svc = L.Service(BIN, self.dir, args)

    def start(self):
        return self.svc.start()

    def add(self, ident, host, port):
        L.psql(f"insert into endpoints (id, host, port, secret) values ({ident}, '{host}', {port}, '{L.secret()}')")

    def api(self, method, path, body=None):
        st, data, _ = self.svc.request(method, path, json.dumps(body).encode() if body is not None else None, {"Authorization": "Bearer " + TOKEN})
        return st, (json.loads(data) if data else None)

    def event(self, n):
        return self.svc.post_event(n)

    def delivered(self, count, secs=15):
        return L.wait_for(lambda: self.svc.stats()["delivered"] >= count, secs)

    def close(self):
        self.svc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def fresh_db():
    L.apply_schema()
    L.psql("truncate endpoints, attempts")


def main():
    pki = K.Pki()
    cert = pki.leaf("hooks.test,other.test")
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"], "other.test": ["127.0.0.1"], "third.test": ["127.0.0.1"]})

    # 1. one endpoint, six deliveries
    for label, kw in (("TLS 1.3", {}), ("TLS 1.2", {"tls12": True})):
        fresh_db()
        srv = K.TlsServer(*cert, **kw)
        r = Rig(pki, dns)
        r.add(1, "https://hooks.test", srv.port)
        check(f"1. {label}: the service starts", r.start(), r.svc.stderr())
        for n in range(1, 7):
            r.event(n)
            r.delivered(n)
        if kw and PURE:
            check(f"1. {label}, the pure build: six deliveries, six full handshakes (it resumes TLS 1.3 only)", srv.handshakes == 6 and srv.resumed == 0, f"{srv.handshakes} {srv.resumed}")
        else:
            check(f"1. {label}: six deliveries, six handshakes, the first full and the five after it resumed", srv.handshakes == 6 and srv.resumed == 5, f"{srv.handshakes} {srv.resumed}")
        check(f"1. {label}: the receiver saw the protocol it was meant to", {s["version"] for s in srv.seen} == {"TLSv1.3" if not kw else "TLSv1.2"}, str({s["version"] for s in srv.seen}))
        check(f"1. {label}: nothing failed", r.svc.stats()["delivered"] == 6 and r.svc.stats()["failed"] == 0)
        r.close()
        srv.close()
    fresh_db()
    srv = K.TlsServer(*cert)
    r = Rig(pki, dns, ["--tls-resume", "0"])
    r.add(1, "https://hooks.test", srv.port)
    r.start()
    for n in range(1, 7):
        r.event(n)
        r.delivered(n)
    check("1. tls-resume 0: six full handshakes, none resumed", srv.handshakes == 6 and srv.resumed == 0, f"{srv.handshakes} {srv.resumed}")
    r.close()
    srv.close()

    # 1b. an endpoint in a slot past the 64 there were sessions for before design section 41 (the id 1000 is its own slot) resumes as well
    fresh_db()
    srv = K.TlsServer(*cert)
    r = Rig(pki, dns)
    r.add(1000, "https://hooks.test", srv.port)
    r.start()
    for n in range(1, 7):
        r.event(n)
        r.delivered(n)
    check("1. an endpoint in slot 1000: six deliveries, six handshakes, the first full and the five after it resumed", srv.handshakes == 6 and srv.resumed == 5, f"{srv.handshakes} {srv.resumed}")
    r.close()
    srv.close()

    # 2. a session is the endpoint's own
    fresh_db()
    srv = K.TlsServer(*cert)
    r = Rig(pki, dns)
    r.add(1, "https://hooks.test", srv.port)
    r.add(2, "https://other.test", srv.port)
    r.start()
    r.event(1)
    r.delivered(2)
    check("2. two endpoints behind one receiver: both first deliveries are full handshakes (one's session is not offered to the other)", srv.handshakes == 2 and srv.resumed == 0
          and sorted(srv.sni) == ["hooks.test", "other.test"], f"{srv.handshakes} {srv.resumed} {srv.sni}")
    r.event(2)
    r.delivered(4)
    check("2. ... and the second deliveries each resume their own", srv.handshakes == 4 and srv.resumed == 2, f"{srv.handshakes} {srv.resumed}")
    # 3. changes drop the session
    n = 2
    delivered = 4

    def deliver_once():
        nonlocal n, delivered
        n += 1
        before = srv.resumed, srv.handshakes
        r.event(n)
        delivered += 2
        ok = r.delivered(delivered)
        return ok, srv.handshakes - before[1], srv.resumed - before[0]

    ok, hs, res = deliver_once()
    check("3. before any change a delivery to each endpoint resumes", ok and hs == 2 and res == 2, f"{hs} {res}")
    st, _ = r.api("PATCH", "/endpoints/1", {"host": "other.test"})
    check("3. PATCH of the host of endpoint 1 is accepted", st == 200, str(st))
    ok, hs, res = deliver_once()
    check("3. ... after it endpoint 1 makes a full handshake (to the new name) and endpoint 2 resumes", ok and hs == 2 and res == 1, f"{hs} {res}")
    ok, hs, res = deliver_once()
    check("3. ... and then both resume", ok and hs == 2 and res == 2, f"{hs} {res}")
    st, _ = r.api("PATCH", "/endpoints/2", {"secret": "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"})
    check("3. PATCH of the secret of endpoint 2 is accepted", st == 200, str(st))
    ok, hs, res = deliver_once()
    check("3. ... after it endpoint 2 makes a full handshake and endpoint 1 resumes", ok and hs == 2 and res == 1, f"{hs} {res}")
    srv2 = K.TlsServer(*cert)
    st, _ = r.api("PATCH", "/endpoints/1", {"port": srv2.port})
    check("3. PATCH of the port of endpoint 1 (another receiver) is accepted", st == 200, str(st))
    ok, hs, res = deliver_once()
    check("3. ... the delivery goes to the other receiver, as a full handshake", ok and srv2.handshakes == 1 and srv2.resumed == 0, f"{srv2.handshakes} {srv2.resumed}")
    ok, hs, res = deliver_once()
    check("3. ... and the one after it resumes there", ok and srv2.handshakes == 2 and srv2.resumed == 1, f"{srv2.handshakes} {srv2.resumed}")
    # a delete, then a new endpoint that may take the slot the old one had
    st, _ = r.api("DELETE", "/endpoints/2")
    check("3. DELETE of endpoint 2 is accepted", st == 200, str(st))
    st, made = r.api("POST", "/endpoints", {"url": f"https://other.test:{srv.port}"})
    check("3. a new endpoint is made behind the same name and port", st == 201, str((st, made)))
    before = srv.handshakes, srv.resumed
    delivered = r.svc.stats()["delivered"]
    n += 1
    r.event(n)
    ok = r.delivered(delivered + 2)
    check("3. its first delivery is a full handshake, whichever slot it has (the old endpoint's session is not inherited)", ok and srv.handshakes - before[0] == 1 and srv.resumed == before[1],
          f"{srv.handshakes - before[0]} {srv.resumed - before[1]}")
    r.close()
    srv.close()
    srv2.close()

    # 4. a receiver that no longer accepts the session
    fresh_db()
    srv = K.TlsServer(*cert)
    r = Rig(pki, dns)
    r.add(1, "https://hooks.test", srv.port)
    r.start()
    for k in (1, 2):
        r.event(k)
        r.delivered(k)
    check("4. a session is saved (the second delivery resumed)", srv.resumed == 1, f"{srv.handshakes} {srv.resumed}")
    srv.new_keys(*cert)
    r.event(3)
    check("4. the receiver restarted (new ticket keys): the offered session is refused, the handshake is a full one, and the delivery is made", r.delivered(3) and srv.resumed == 1
          and srv.handshakes == 3 and r.svc.stats()["failed"] == 0, f"{srv.handshakes} {srv.resumed} {r.svc.stats()}")
    r.event(4)
    check("4. ... and the session it gave then resumes", r.delivered(4) and srv.resumed == 2, f"{srv.handshakes} {srv.resumed}")
    r.close()
    srv.close()

    # 5. no leaks, memory bounded. The service keeps something for every event it takes (its log, its index, the cells of each endpoint: about 10 KB an event and endpoint, the same
    # with or without TLS), so memory is compared with a control that does everything but TLS. Stage one: one plain endpoint that delivers and three whose connection is refused, against
    # one https endpoint that delivers (resuming) and three whose handshake fails on the certificate. Stage two: only the endpoint that delivers, many more times (a session kept and
    # not freed, or a connection not freed, shows here). The TLS side may not grow faster than the control.
    def grow(kind, bad, first, more):
        fresh_db()
        sink = srv_good = srv_bad = None
        # a retry an hour away: every event is one attempt to each endpoint during the test, so when the count of attempts is reached nothing is in flight (and the endpoints
        # that fail hold every event in their windows: stage one stays under the 1,024 of a window)
        r = Rig(pki, dns, ["--deadline-ms", "3000", "--schedule", "3600000"])
        if kind == "tls":
            srv_good = K.TlsServer(*cert)
            r.add(1, "https://hooks.test", srv_good.port)
            if bad:
                srv_bad = K.TlsServer(*pki.selfsigned("hooks.test"))
                for i in range(2, 2 + bad):
                    r.add(i, "https://other.test", srv_bad.port)
        else:
            sink = K.Sink("127.0.0.1")
            r.add(1, "127.0.0.1", sink.port)
            for i in range(2, 2 + bad):
                r.add(i, "127.0.0.1", L.closed_port())
        r.start()

        def burst(count):
            base = r.svc.stats()["attempts"]
            for k in range(count):
                r.event(1000 + k)
            assert L.wait_for(lambda: r.svc.stats()["attempts"] >= base + count * (1 + bad), 240), f"a burst of {count} was not attempted: {r.svc.stats()}"
            time.sleep(0.3)

        # Three points, two equal steps after the first: a leak costs something in every step, a buffer that becomes resident once (a page of a lazily zeroed block, the heap
        # growing by its top pad) costs in one. The step that counts is the smaller of the two; what grew in each is kept, by mapping, for the message.
        burst(first)
        pid = r.svc.proc.pid
        a1, m1 = K.rss_kb(pid), K.mappings_kb(pid)
        burst(more)
        a2, m2 = K.rss_kb(pid), K.mappings_kb(pid)
        burst(more)
        a3, m3 = K.rss_kb(pid), K.mappings_kb(pid)
        st = r.svc.stats()
        handshakes = (srv_good.handshakes, srv_good.resumed) if srv_good else None
        r.close()
        for x in (sink, srv_good, srv_bad):
            if x:
                x.close()
        return (a1, a2, a3), st, handshakes, f"step 1: {K.grown(m1, m2)}; step 2: {K.grown(m2, m3)}"

    def step(points):
        (k1, _), (k2, _), (k3, _) = points
        return min(k2 - k1, k3 - k2), (k2 - k1, k3 - k2)

    tls_pts, tls_st, _, tls_where = grow("tls", 3, 200, 400)
    ctl_pts, ctl_st, _, ctl_where = grow("control", 3, 200, 400)
    check(f"5. TLS side: {tls_st['attempts']} attempts, a quarter delivered and the rest failed in the handshake; control: {ctl_st['attempts']}", tls_st["delivered"] == 1000 and tls_st["failed"] == 3000
          and ctl_st["delivered"] == 1000 and ctl_st["failed"] == 3000, f"{tls_st} {ctl_st}")
    check(f"5. no descriptor is left over: {tls_pts[0][1]}, {tls_pts[1][1]} and {tls_pts[2][1]} after 200, 600 and 1,000 events (the control: {ctl_pts[2][1]})",
          tls_pts[0][1] == tls_pts[1][1] == tls_pts[2][1] == ctl_pts[2][1], f"{tls_pts} {ctl_pts}")
    (d_tls, both_tls), (d_ctl, both_ctl) = step(tls_pts), step(ctl_pts)
    check(f"5. memory grows no faster with TLS than without it: 400 more events cost {d_tls} KiB against the control's {d_ctl} KiB (within 1.5 MiB; the smaller of two steps, {both_tls[0]} and {both_tls[1]} KiB, the control's {both_ctl[0]} and {both_ctl[1]})",
          d_tls <= d_ctl + 1536, f"TLS {tls_where} | control {ctl_where}")
    if max(both_tls) > d_ctl + 1536:
        print(f"     a step of the TLS side was over the bound and the other was not (resident once, not per event): {tls_where}", flush=True)
    tls_pts, tls_st, hs, tls_where = grow("tls", 0, 200, 1500)
    ctl_pts, ctl_st, _, ctl_where = grow("control", 0, 200, 1500)
    (d_tls, both_tls), (d_ctl, both_ctl) = step(tls_pts), step(ctl_pts)
    check(f"5. {tls_st['delivered']} deliveries over TLS, {hs[0]} handshakes of which {hs[1]} resumed; the control {ctl_st['delivered']}", tls_st["delivered"] == 3200 and ctl_st["delivered"] == 3200 and hs[1] >= 3100, f"{tls_st} {hs}")
    check(f"5. no descriptor is left over: {tls_pts[0][1]}, {tls_pts[1][1]} and {tls_pts[2][1]}", tls_pts[0][1] == tls_pts[1][1] == tls_pts[2][1] == ctl_pts[2][1], f"{tls_pts} {ctl_pts}")
    check(f"5. memory grows no faster with TLS than without it: 1,500 more deliveries cost {d_tls} KiB against the control's {d_ctl} KiB (within 1.5 MiB; the smaller of two steps, {both_tls[0]} and {both_tls[1]} KiB, the control's {both_ctl[0]} and {both_ctl[1]})",
          d_tls <= d_ctl + 1536, f"TLS {tls_where} | control {ctl_where}")
    if max(both_tls) > d_ctl + 1536:
        print(f"     a step of the TLS side was over the bound and the other was not (resident once, not per event): {tls_where}", flush=True)

    return check.finish("sessions")


if __name__ == "__main__":
    sys.exit(main())
