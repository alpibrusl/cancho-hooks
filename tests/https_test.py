#!/usr/bin/env python3
"""Delivery over TLS (docs/design.md section 40; production.md P1.7, slice T1), against a TLS receiver made for each way it can go wrong.

    python3 tests/https_test.py build/hooks            (needs the `openssl` command and `pip install standardwebhooks`; no database)

An endpoint whose host starts with `https://` is delivered to over TLS: after the connection, a handshake on the same poller, SNI and the certificate checked against the
host name, TLS 1.2 at least, then the same request and the same status line. Every way it can fail is a reason of its own (`/metrics`, `hooks_attempt_failures_total`),
recorded like any failed attempt: retried on the schedule, dead at the end of it. The receiver (`tests/tlskit.py`) is Python's `ssl` behind a throwaway authority; the names
are resolved by a name server of the test's own (`--dns-server`), which answers 127.0.0.1 and runs with `allow-private-hosts 1`.

  1. a good chain: delivered; the request carries a signature the reference library verifies (as over http); SNI is the host name; the Host header is the name and the port;
     the protocol is TLS 1.2 or 1.3; the receiver saw the event once
  2. every certificate that is not good has its own reason, and nothing is delivered: expired, not yet valid, a name it does not carry, a chain to another authority, a
     self-signed one; with and without `tls-ca-file` (the file replaces the system's store: a self-signed certificate trusted by being the file is delivered to)
  3. the handshake: a receiver that closes, speaks garbage, offers TLS 1.1 only, or never answers (the deadline passes: not before it, and the loop is not held by it)
  4. after the handshake: a receiver that never answers (no_response), a 500, a 410 (dead, and the endpoint disabled)
  5. the retry schedule: a certificate that is mended between attempts is delivered by the retry; one that never is makes the event a dead letter after the schedule
  6. the trust store: the system's honours SSL_CERT_FILE; a `tls-ca-file` that cannot be read is a refusal to start (status 21), never an unverified client
  7. kill -9 in the middle of a handshake loses nothing: the event is delivered after the restart
  8. 64 handshakes that never complete hold nothing: the service answers a request while they wait (the longest wait is measured)
  9. retention: a dead letter of a reason above 16 is read by name by the log checker, kept by a snapshot of the outcomes log (`--compact-now 1`), and listed with its reason after it
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

try:
    from standardwebhooks import Webhook, WebhookVerificationError
except ImportError:
    print("this test needs the reference library: pip install standardwebhooks")
    sys.exit(2)

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()
tick_gap = K.tick_gap


def logcheck(d):
    """The report of scripts/logcheck.py (the restore's check of a data directory), with `consistent` from its exit status."""
    v = subprocess.run([sys.executable, os.path.join(L.ROOT, "scripts", "logcheck.py"), "check", d], capture_output=True, text=True)
    try:
        rep = json.loads(v.stdout)
    except ValueError:
        rep = {"stdout": v.stdout, "stderr": v.stderr}
    rep["consistent"] = v.returncode == 0
    return rep


class Case:
    """One service with `endpoints` https endpoints at one receiver, and a name server of its own."""

    def __init__(self, server, pki, name="hooks.test", dns=None, args=(), schedule="60000", deadline=1500, ca=True, endpoints=1, env=None, scheme="https://", shim=False):
        self.server, self.pki, self.name = server, pki, name
        self.dns = dns or K.DnsStub({name: ["127.0.0.1"]})
        if shim:
            env = dict(env or {}, LD_PRELOAD=os.path.join(L.ROOT, "build", "io_shim.so"), SHIM_PORTS=f"{server.port},{self.dns.port}", SHIM_SEND_MAX="700", SHIM_RECV_MAX="300",
                       SHIM_EAGAIN_EVERY="3")
        self.secret = L.secret()
        self.dir = L.free_dir("hooks-https-")
        with open(os.path.join(self.dir, "endpoints.conf"), "w") as f:
            for i in range(endpoints):
                f.write(f"{i + 1} {scheme}{name} {server.port} {self.secret}\n")
        a = ["--schedule", schedule, "--deadline-ms", str(deadline), "--dns-server", f"127.0.0.1:{self.dns.port}"]
        if ca:
            a += ["--tls-ca-file", ca if isinstance(ca, str) else pki.ca_pem]
        self.svc = L.Service(BIN, self.dir, a + list(args), env=env)
        self.t0 = None

    def start(self):
        ok = self.svc.start()
        return ok

    def post(self, n=1):
        self.t0 = time.time()
        return self.svc.post_event(n)

    def wait_attempts(self, n, secs=15):
        return L.wait_for(lambda: self.svc.stats()["attempts"] >= n, secs)

    def reasons(self):
        m = self.svc.metrics()
        return {dict(k)["reason"]: int(v) for k, v in m.series("hooks_attempt_failures_total").items() if v}

    def outcomes(self):
        s = self.svc.stats()
        return {k: s[k] for k in ("delivered", "failed", "dead")}

    def close(self):
        self.svc.stop()
        self.dns.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def one(label, server, pki, want_reason, **kw):
    """One event to one endpoint: the reason it failed with (or None if it was delivered), and how long the attempt took."""
    c = Case(server, pki, **kw)
    ok = c.start()
    check(f"{label}: the service starts", ok, c.svc.stderr())
    c.post()
    done = c.wait_attempts(1)
    took = time.time() - c.t0
    reasons = c.reasons()
    out = c.outcomes()
    if want_reason is None:
        check(f"{label}: delivered, and no failure is counted", done and out["delivered"] == 1 and not reasons, f"{out} {reasons}")
    else:
        check(f"{label}: failed with reason {want_reason} and nothing else", done and out["delivered"] == 0 and reasons == {want_reason: 1}, f"{out} {reasons}")
    c.close()
    return took


def main():
    pki = K.Pki()
    good = pki.leaf("hooks.test")

    # 1. a good chain
    srv = K.TlsServer(*good)
    c = Case(srv, pki, args=["--admin-token", "https-test-token"])
    check("1. the service starts with an https endpoint and a name server", c.start(), c.svc.stderr())
    status, body = c.post(7)
    check("1. the event is accepted", status == 202, str((status, body)))
    check("1. it is delivered over TLS", c.wait_attempts(1) and c.outcomes()["delivered"] == 1 and srv.count() == 1, f"{c.outcomes()} {srv.seen} {srv.failed}")
    rec = srv.seen[0]
    check("1. the receiver saw the event once, with its body", srv.events() == [7], str(srv.events()))
    check("1. SNI is the endpoint's host name", rec["sni"] == "hooks.test", str(rec["sni"]))
    check("1. the protocol is TLS 1.2 or 1.3", rec["version"] in ("TLSv1.2", "TLSv1.3"), str(rec["version"]))
    hdr = dict(rec["headers"])
    check("1. the Host header is the name and the port (not `receiver`)", hdr["Host"] == f"hooks.test:{srv.port}", str(hdr))
    check("1. the request is the one http carries: path, content type, ids, and no `Connection: close` (kept connections, design 53.2)", rec["request"] == "POST /hook HTTP/1.1"
          and hdr["Content-Type"] == "application/json" and hdr["webhook-id"] == "evt_1" and "Connection" not in hdr, str(rec))
    try:
        Webhook(c.secret).verify(rec["body"], hdr)
        verified = True
    except WebhookVerificationError as e:
        verified = False
    check("1. the signature verifies with the reference library, as over http", verified)
    check("1. the name was asked of the name server once", c.dns.questions("hooks.test") == 1, str(c.dns.asked))
    st, data, _ = c.svc.request("POST", "/events/1/replay/1", headers={"Authorization": "Bearer https-test-token"})
    check("1. a replay of the event to the endpoint is delivered over TLS as well, with the same signature headers (a replay is a new attempt of the same message)", st == 202 and L.wait_for(lambda: srv.count() == 2, 10)
          and srv.events() == [7, 7] and dict(srv.seen[1]["headers"])["webhook-id"] == "evt_1" and c.dns.questions("hooks.test") == 2, f"{st} {srv.events()} {c.dns.asked}")
    c.close()
    srv.close()

    # 2. certificates
    cases = [
        ("2. an expired certificate", pki.expired("hooks.test"), "cert_expired", {}),
        ("2. a certificate that is not valid yet", pki.not_yet_valid("hooks.test"), "cert_expired", {}),
        ("2. a certificate for another name", pki.leaf("other.test"), "cert_hostname", {}),
        ("2. a chain to another authority", pki.other_leaf("hooks.test"), "cert_untrusted", {}),
        ("2. a certificate whose signature was changed (one bit)", pki.damaged("hooks.test"), "cert_invalid", {}),
        ("2. a certificate that may be used to authenticate a client and not a server", pki.wrong_purpose("hooks.test"), "cert_invalid", {}),
        ("2. a self-signed certificate", pki.selfsigned("hooks.test"), "cert_untrusted", {}),
        ("2. a good chain, but no tls-ca-file: the system's store does not hold the test authority", good, "cert_untrusted", {"ca": False}),
        ("2. a self-signed certificate, no tls-ca-file", pki.selfsigned("hooks.test"), "cert_untrusted", {"ca": False}),
    ]
    for label, (cert, key), reason, kw in cases:
        s = K.TlsServer(cert, key)
        one(label, s, pki, reason, **kw)
        check(f"{label}: and the receiver saw no request", s.count() == 0, str(s.seen))
        s.close()
    # the controls: the same checks pass when they should (a failure above was the check, not the setup)
    s = K.TlsServer(*pki.selfsigned("hooks.test"))
    one("2. control: a self-signed certificate that is the tls-ca-file itself is trusted", s, pki, None, ca=pki.selfsigned("hooks.test")[0])
    s.close()
    s = K.TlsServer(*pki.leaf("other.test"))
    one("2. control: the same certificate for another name is delivered to when the endpoint has that name", s, pki, None, name="other.test")
    s.close()
    s = K.TlsServer(*good)
    one("2. control: tls-ca-file replaces the system's store (a different authority does not trust it)", s, pki, "cert_untrusted", ca=pki.other_ca_pem)
    s.close()

    # 3. the handshake
    s = K.TlsServer(*good, mode="close")
    one("3. a receiver that closes (an orderly end of the stream) during the handshake", s, pki, "tls_handshake")
    s.close()
    s = K.TlsServer(*good, mode="reset")
    one("3. a receiver that resets the connection during the handshake", s, pki, "tls_handshake")
    s.close()
    s = K.TlsServer(*good, mode="garbage")
    one("3. a receiver that answers something that is not TLS", s, pki, "tls_handshake")
    s.close()
    s = K.TlsServer(*good, mode="tls11")
    took = one("3. a receiver that offers TLS 1.1 at most", s, pki, "tls_handshake")
    # The system's OpenSSL configuration (Debian's and Ubuntu's) already refuses TLS 1.1, which would make the service's own minimum redundant here. With a configuration that
    # allows everything down to TLS 1.0 at security level 0 only the service's own minimum (src/ossl.cho: TLS 1.2) stands between it and this receiver.
    permissive = os.path.join(pki.dir, "permissive.cnf")
    with open(permissive, "w") as f:
        f.write("openssl_conf = default_conf\n[default_conf]\nssl_conf = ssl_sect\n[ssl_sect]\nsystem_default = system_default_sect\n[system_default_sect]\nMinProtocol = TLSv1\nCipherString = DEFAULT:@SECLEVEL=0\n")
    one("3. ... under an OpenSSL configuration that allows TLS 1.0 and security level 0: the service's own minimum refuses it", s, pki, "tls_handshake", env={"OPENSSL_CONF": permissive})
    check("3. ... and the receiver saw no request", s.count() == 0)
    # the control: that receiver does offer TLS 1.1 (a client that asks for it gets it), so the refusal above was the service's minimum and not the receiver's setup
    ctl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctl.minimum_version = ssl.TLSVersion.TLSv1
    ctl.maximum_version = ssl.TLSVersion.TLSv1_1
    ctl.set_ciphers("ALL:@SECLEVEL=0")
    ctl.check_hostname = False
    ctl.verify_mode = ssl.CERT_NONE
    got = None
    try:
        import socket as _socket
        with _socket.create_connection(("127.0.0.1", s.port), timeout=5) as raw, ctl.wrap_socket(raw, server_hostname="hooks.test") as tlsconn:
            got = tlsconn.version()
    except (ssl.SSLError, OSError) as e:
        got = str(e)
    check("3. ... control: a client that asks for TLS 1.1 gets it from the same receiver", got == "TLSv1.1", str(got))
    s.close()
    s = K.TlsServer(*good, mode="hold")
    took = one("3. a receiver that never answers the handshake", s, pki, "tls_timeout", deadline=900)
    check("3. ... the attempt took the deadline, not less and not much more", 0.8 <= took <= 3.0, f"{took:.2f}s")
    s.close()
    # a port nothing listens on: the connect fails before TLS is started
    class Gone:
        port = L.closed_port()

    c = Case(Gone(), pki)
    c.start()
    c.post()
    c.wait_attempts(1)
    check("3. nothing listening: connect_refused, not a TLS reason", c.reasons() == {"connect_refused": 1}, str(c.reasons()))
    c.close()

    # 4. after the handshake
    s = K.TlsServer(*good, mode="silent")
    took = one("4. a receiver that completes the handshake and never answers", s, pki, "no_response", deadline=900)
    check("4. ... at the deadline", 0.8 <= took <= 3.0, f"{took:.2f}s")
    s.close()
    s = K.TlsServer(*good, mode="split")
    c = Case(s, pki)
    c.start()
    c.post(3)
    ok = c.wait_attempts(1) and c.outcomes()["delivered"] == 1
    took = time.time() - c.t0
    check("4. a status line that arrives in two TLS records, 300 ms apart, is read whole: delivered, after the pause", ok and took >= 0.25 and s.events() == [3], f"{c.outcomes()} {took:.2f}")
    c.close()
    s.close()
    s = K.TlsServer(*good, rcvbuf=4096, read_delay=0.004)
    c = Case(s, pki, deadline=8000)
    c.start()
    status, body = c.svc.post_event(4, extra={"pad": "x" * 60000})
    ok = c.wait_attempts(1, 20) and c.outcomes()["delivered"] == 1
    sent = len(s.seen[0]["body"]) if s.seen else 0
    check("4. an event of 60,000 bytes to a receiver that reads slowly, a few KiB at a time (the request takes several records and several writes): delivered, every byte there", ok and status == 202
          and sent >= 60000 and json.loads(s.seen[0]["body"])["pad"] == "x" * 60000, f"{c.outcomes()} {sent}")
    c.close()
    s.close()
    # Partial and refused I/O (tests/io_shim.c): on the connections to the receiver and to the name server `send` takes at most 700 bytes, `recv` returns at most 300, and every
    # third call of either is refused with EAGAIN. A loopback socket takes 60 KB in one call, so without the shim the branches of the TLS phase and of the lookup that wait for the
    # kernel (a write in pieces, a write that waits, a record in pieces, a read with nothing yet) are not reached by any test.
    s = K.TlsServer(*good)
    c = Case(s, pki, deadline=20000, shim=True)
    c.start()
    status, body = c.svc.post_event(6, extra={"pad": "y" * 40000})
    ok = c.wait_attempts(1, 60) and c.outcomes()["delivered"] == 1
    check("4. under partial and refused I/O (the lookup, the handshake, a 40,000-byte request over many records and writes, the status line): delivered, every byte there", ok and status == 202
          and s.seen and json.loads(s.seen[0]["body"])["pad"] == "y" * 40000 and c.reasons() == {}, f"{c.outcomes()} {c.reasons()} {len(s.seen[0]['body']) if s.seen else 0}")
    c.close()
    s.close()
    s = K.TlsServer(*good, status=500)
    one("4. a 500", s, pki, "status_5xx")
    check("4. ... the receiver got the request once", s.count() == 1)
    s.close()
    s = K.TlsServer(*good, status=302)
    one("4. a redirect is not followed and is a failure", s, pki, "status_3xx")
    check("4. ... one request", s.count() == 1)
    s.close()
    s = K.TlsServer(*good, status=410)
    c = Case(s, pki)
    c.start()
    c.post()
    c.wait_attempts(1)
    st = c.outcomes()
    check("4. a 410: the event is dead at once", st["dead"] == 1 and c.reasons() == {"gone": 1}, f"{st} {c.reasons()}")
    status, endpoints = c.svc.get_json("/endpoints")
    check("4. ... the endpoint is disabled", endpoints[0]["disabled"] is True, str(endpoints))
    c.close()
    s.close()

    # 5. the retry schedule
    s = K.TlsServer(*pki.leaf("other.test"))
    c = Case(s, pki, schedule="400,400")
    c.start()
    c.post(5)
    check("5. the first attempt fails: a certificate for another name", c.wait_attempts(1) and c.reasons() == {"cert_hostname": 1}, str(c.reasons()))
    # mend it: the receiver now has a certificate for the name
    mended = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    mended.load_cert_chain(*good)
    s.ctx = mended
    check("5. the retry, 400 ms later, delivers", L.wait_for(lambda: c.outcomes()["delivered"] == 1, 10) and s.events() == [5], f"{c.outcomes()} {s.events()}")
    check("5. one failure was counted, not two", c.reasons() == {"cert_hostname": 1}, str(c.reasons()))
    c.close()
    s.close()
    s = K.TlsServer(*pki.selfsigned("hooks.test"))
    c = Case(s, pki, schedule="100,100")
    c.start()
    c.post()
    check("5. a certificate that is never mended: three attempts, then a dead letter", L.wait_for(lambda: c.outcomes()["dead"] == 1, 10) and c.svc.stats()["attempts"] == 3, f"{c.outcomes()} {c.svc.stats()}")
    check("5. ... three failures for the same reason", c.reasons() == {"cert_untrusted": 3}, str(c.reasons()))
    check("5. ... and the receiver saw nothing", s.count() == 0)
    c.close()
    s.close()

    # 6. the trust store
    s = K.TlsServer(*good)
    one("6. the system's store honours SSL_CERT_FILE (no tls-ca-file)", s, pki, None, ca=False, env={"SSL_CERT_FILE": pki.ca_pem})
    s.close()
    s = K.TlsServer(*good)
    one("6. ... and a tls-ca-file replaces it even then", s, pki, "cert_untrusted", ca=pki.other_ca_pem, env={"SSL_CERT_FILE": pki.ca_pem})
    s.close()
    for label, path in (("a file that is not there", "/nonexistent/ca.pem"), ("a file that holds no certificate", os.path.join(pki.dir, "ca.cnf"))):
        d = L.free_dir("hooks-https-")
        svc = L.Service(BIN, d, ["--tls-ca-file", path])
        started = svc.start()
        code = svc.wait_exit(5)
        L.wait_for(lambda: "trust store" in svc.stderr(), 3)    # stderr is read by a thread: the process may be gone before it has read the last line
        check(f"6. tls-ca-file is {label}: the service refuses to start (status 21) and says why", not started and code == 21 and "trust store" in svc.stderr(), f"{code} {svc.stderr()}")
        shutil.rmtree(d, ignore_errors=True)

    # 7. kill -9 in the middle of a handshake
    s = K.TlsServer(*good, mode="hold")
    c = Case(s, pki, schedule="200", deadline=20000)
    c.start()
    c.post(9)
    check("7. the handshake is under way (the receiver has the connection)", L.wait_for(lambda: s.accepted >= 1, 10))
    c.svc.kill()
    check("7. nothing was delivered or recorded yet", s.count() == 0)
    s.mode = "ok"
    s.release()
    c.svc = L.Service(BIN, c.dir, ["--schedule", "200", "--deadline-ms", "5000", "--dns-server", f"127.0.0.1:{c.dns.port}", "--tls-ca-file", pki.ca_pem])
    c.svc.start()
    check("7. after the restart the event is delivered, once", L.wait_for(lambda: s.events() == [9], 10), f"{s.events()} {s.failed}")
    c.close()
    s.close()

    # 8. the loop is not held
    s = K.TlsServer(*good, mode="hold")
    c = Case(s, pki, endpoints=8, schedule="60000", deadline=3000)
    c.start()
    base = tick_gap(c.svc, 40)
    for n in range(1, 9):
        c.svc.post_event(n)
    check("8. 64 handshakes are in flight, held by the receiver", L.wait_for(lambda: s.accepted >= 64, 15), f"accepted {s.accepted}")
    loaded = tick_gap(c.svc, 100)
    check(f"8. the service answers a request while they wait: the longest wait {loaded * 1000:.1f} ms (idle: {base * 1000:.1f} ms)", loaded < 0.25, f"{loaded:.3f}")
    ok = L.wait_for(lambda: c.svc.stats()["attempts"] >= 64, 15)
    check("8. all 64 end at the deadline as tls_timeout", ok and c.reasons().get("tls_timeout") == 64, f"{c.reasons()} {c.svc.stats()}")
    c.close()
    s.close()

    # 9. retention (docs/retention.md): the reasons of names and TLS are written in the outcomes log (kind 14, a reason above 16) and in a dead letter, and a snapshot of the log
    # (`--compact-now 1`) keeps the dead letter with the reason it died of; the log checker of the backup (scripts/logcheck.py) reads both and names the reason.
    s = K.TlsServer(*pki.selfsigned("hooks.test"))
    c = Case(s, pki, schedule="100,100")
    c.start()
    c.post(1)
    check("9. a certificate that is never mended is a dead letter", L.wait_for(lambda: c.outcomes()["dead"] == 1, 10), str(c.outcomes()))
    c.svc.stop()
    seen = logcheck(c.dir)
    check("9. the log checker reads the reasons of the failed attempts by name (three attempts: cert_untrusted, 21)", seen.get("consistent", False) and seen["delivery"]["reasons"].get("cert_untrusted") == 3, str(seen.get("delivery")))
    p = subprocess.run([BIN, "--port", str(c.svc.port), "--dir", c.dir, "--schedule", "100,100", "--compact-now", "1", "--dns-server", f"127.0.0.1:{c.dns.port}", "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=30)
    check("9. a snapshot of the outcomes log is made (compact-now exits 0)", p.returncode == 0 and "compacted" in p.stderr + p.stdout, f"{p.returncode} {p.stderr!r} {p.stdout!r}")
    after = logcheck(c.dir)
    check("9. the directory is still one the log checker accepts, and the snapshot holds the dead letter (kind 17)", after.get("consistent", False) and after["delivery"]["kinds"].get("17") == 1, str(after.get("delivery")))
    c.svc.start()
    status, dead = c.svc.get_json("/endpoints/1/dead")
    check("9. after the snapshot the dead letter still says why it died: cert_untrusted, three attempts", status == 200 and [(e["event"], e["attempts"], e["reason"]) for e in dead["dead"]] == [(1, 3, "cert_untrusted")], str((status, dead)))
    c.close()
    s.close()

    return check.finish("https")


if __name__ == "__main__":
    sys.exit(main())
