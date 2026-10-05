#!/usr/bin/env python3
"""The build with lex-sys's own TLS only (docs/pure-tls.md): what it does differently from the OpenSSL build, and what `tests/https_test.py` does not look at.

    python3 tests/pure_test.py pure/build/hooks-pure            (needs the `openssl` command and `pip install standardwebhooks`; no database)

`tests/https_test.py` runs on both builds (`scripts/https_both.py` compares them). These are the checks of the pure one alone:

  1. a trust store that fills the buffer it is read into (a `tls-ca-file` of 3 MiB; the buffer is 2 MiB) is refused, never truncated: the service does not start
     (status 21) and says why. The file is one certificate and then padding, so that it is the size and not the number of roots that is refused
  2. the environment is not read: with `SSL_CERT_FILE` naming the authority of the receiver's certificate and no `tls-ca-file`, the certificate is untrusted (the
     OpenSSL build honours the variable, and `https_test.py`'s group 6 expects that of it)
  3. the connection ends with `close_notify`: the receiver reads a clean end of the stream and not a truncation
  4. there is no resumption: two deliveries to one endpoint are two full handshakes, and the receiver saw no resumed session
"""
import os
import shutil
import socket
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "pure/build/hooks-pure"
check = L.Checks()


def service(server_port, pki, ca=True, env=None, endpoints=1):
    d = L.free_dir("hooks-pure-")
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    secret = L.secret()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(endpoints):
            f.write(f"{i + 1} https://hooks.test {server_port} {secret}\n")
    args = ["--schedule", "60000", "--deadline-ms", "5000", "--dns-server", f"127.0.0.1:{dns.port}"]
    if ca:
        args += ["--tls-ca-file", pki.ca_pem]
    return L.Service(BIN, d, args, env=env), d, dns


def reasons(svc):
    m = svc.metrics()
    return {dict(k)["reason"]: int(v) for k, v in m.series("hooks_attempt_failures_total").items() if v}


class CloseServer:
    """A receiver that answers one request and then says how the client ended the connection: `clean` (it read `close_notify`), `ragged` (the stream ended without
    one), or `timeout`."""

    def __init__(self, cert, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.ends = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        c.settimeout(5)
        try:
            t = self.ctx.wrap_socket(c, server_side=True, suppress_ragged_eofs=False)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += t.recv(65536)
            head, _, body = buf.partition(b"\r\n\r\n")
            need = 0
            for line in head.decode(errors="replace").split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    need = int(line.split(":", 1)[1])
            while len(body) < need:
                body += t.recv(65536)
            t.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                end = "clean" if t.recv(1) == b"" else "data"
            except ssl.SSLEOFError:
                end = "ragged"
            except socket.timeout:
                end = "timeout"
            self.ends.append(end)
            t.close()
        except (ssl.SSLError, OSError):
            self.ends.append("error")

    def close(self):
        self.sock.close()


def main():
    pki = K.Pki()
    good = pki.leaf("hooks.test")

    # 1. a trust store as large as the buffer is refused, not truncated
    big = os.path.join(pki.dir, "big.pem")
    # One certificate, the trust store's whole need, and then 3 MiB of text between blocks, which a bundle may hold: what fills the buffer is not certificates, so only
    # a refusal of a buffer that is full (and not an overflow of the store of roots) keeps a bundle that was cut off from being used.
    with open(big, "w") as f:
        f.write(open(pki.ca_pem).read())
        f.write("# padding\n" * (3 * 1024 * 1024 // len("# padding\n") + 1))
    d = L.free_dir("hooks-pure-")
    svc = L.Service(BIN, d, ["--tls-ca-file", big])
    started = svc.start()
    code = svc.wait_exit(10)
    L.wait_for(lambda: "trust store" in svc.stderr(), 3)
    check("1. a tls-ca-file of 3 MiB (the buffer is 2 MiB) is refused: the service does not start (status 21) and says why",
          not started and code == 21 and "trust store" in svc.stderr(), f"{code} {svc.stderr()}")
    shutil.rmtree(d, ignore_errors=True)
    os.remove(big)

    # 2. the environment is not read
    s = K.TlsServer(*good)
    svc, d, dns = service(s.port, pki, ca=False, env={"SSL_CERT_FILE": pki.ca_pem})
    check("2. SSL_CERT_FILE names the receiver's authority, there is no tls-ca-file: the service starts", svc.start(), svc.stderr())
    svc.post_event(1)
    L.wait_for(lambda: svc.stats()["attempts"] >= 1, 15)
    r = reasons(svc)
    check("2. ... and the certificate is untrusted: the variable is not read", svc.stats()["delivered"] == 0 and r == {"cert_untrusted": 1}, f"{svc.stats()} {r}")
    svc.stop()
    dns.close()
    s.close()
    shutil.rmtree(d, ignore_errors=True)

    # 3. close_notify
    cs = CloseServer(*good)
    svc, d, dns = service(cs.port, pki)
    check("3. the service starts", svc.start(), svc.stderr())
    svc.post_event(1)
    L.wait_for(lambda: svc.stats()["delivered"] >= 1, 15)
    L.wait_for(lambda: len(cs.ends) >= 1, 10)
    check("3. the delivery is made, and the receiver reads close_notify: a clean end of the stream", svc.stats()["delivered"] == 1 and cs.ends == ["clean"], f"{svc.stats()} {cs.ends}")
    svc.stop()
    dns.close()
    cs.close()
    shutil.rmtree(d, ignore_errors=True)

    # 4. no resumption
    s = K.TlsServer(*good)
    svc, d, dns = service(s.port, pki)
    check("4. the service starts (tls-resume is on, as it is by default)", svc.start(), svc.stderr())
    svc.post_event(1)
    L.wait_for(lambda: svc.stats()["delivered"] >= 1, 15)
    svc.post_event(2)
    L.wait_for(lambda: svc.stats()["delivered"] >= 2, 15)
    check("4. two deliveries to one endpoint are two full handshakes, and the receiver saw no resumed session", s.handshakes == 2 and s.resumed == 0, f"{s.handshakes} {s.resumed}")
    m = svc.metrics()
    series = {dict(k)["result"]: int(v) for k, v in m.series("hooks_tls_handshakes_total").items()}
    check("4. /metrics counts both as full", series.get("full") == 2 and not series.get("resumed"), str(series))
    svc.stop()
    dns.close()
    s.close()
    shutil.rmtree(d, ignore_errors=True)
    return check.finish("pure")


if __name__ == "__main__":
    sys.exit(main())
