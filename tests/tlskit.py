"""What the tests of `https` endpoints and of names share (docs/design.md section 40): a throwaway certificate authority and the certificates it signs, made with the
`openssl` command; a TLS receiver that fails in a chosen way and says what it saw (the name asked for in SNI, whether a session was resumed, the protocol);
and a name server (DNS over TCP, which is what the service speaks) that answers what the test says, counts the questions, and can be slow or change its answer.

Not a test. `tests/https_test.py` and `tests/names_test.py` import it. Nothing here is the thing under test; none of it is in the service.
"""
import base64
import os
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import warnings

# The tests ask for TLS 1.0 and 1.1 on purpose (a receiver that offers nothing newer): Python says so.
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*TLSVersion.*")

OPENSSL = os.environ.get("OPENSSL", "openssl")


def sh(*args, cwd=None):
    out = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {out.stderr}")
    return out.stdout


class Pki:
    """A certificate authority and the certificates it signed, in a directory.

        ca.pem                       the authority (what `tls-ca-file` names)
        leaf("hooks.test")           (cert path, key path): valid, names hooks.test
        leaf("other.test", ...)      a certificate that names other.test (several names: "a.test,b.test")
        expired("hooks.test")        names hooks.test, valid for one day in January 2020
        selfsigned("hooks.test")     names hooks.test, signed by itself, not by the authority
        other_ca / other_leaf        a second authority and a certificate it signed, for "the chain leads to a root that is not trusted"
    """

    def __init__(self, directory=None):
        self.dir = directory or tempfile.mkdtemp(prefix="hooks-pki-")
        self.ca_pem = os.path.join(self.dir, "ca.pem")
        self.ca_key = os.path.join(self.dir, "ca.key")
        self._made = {}
        with open(os.path.join(self.dir, "ca.cnf"), "w") as f:
            f.write("[ca]\ndefault_ca = CA_default\n[CA_default]\ndir = .\ndatabase = index.txt\nnew_certs_dir = newcerts\nserial = serial\ndefault_md = sha256\n"
                    "policy = pol\nunique_subject = no\ncopy_extensions = none\n[pol]\ncommonName = supplied\n")
        os.makedirs(os.path.join(self.dir, "newcerts"), exist_ok=True)
        open(os.path.join(self.dir, "index.txt"), "w").close()
        with open(os.path.join(self.dir, "serial"), "w") as f:
            f.write("1000\n")
        self._authority(self.ca_pem, self.ca_key, "hooks-test-ca")
        self.other_ca_pem = os.path.join(self.dir, "other-ca.pem")
        self.other_ca_key = os.path.join(self.dir, "other-ca.key")
        self._authority(self.other_ca_pem, self.other_ca_key, "another-test-ca")

    def _authority(self, pem, key, cn):
        sh(OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", pem, "-days", "3650",
           "-subj", f"/CN={cn}", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=self.dir)

    def _csr(self, name, cn):
        key = os.path.join(self.dir, f"{name}.key")
        csr = os.path.join(self.dir, f"{name}.csr")
        sh(OPENSSL, "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", csr, "-subj", f"/CN={cn}", cwd=self.dir)
        return key, csr

    def _ext(self, name, san):
        path = os.path.join(self.dir, f"{name}.ext")
        with open(path, "w") as f:
            f.write(f"subjectAltName={','.join('DNS:' + n for n in san.split(','))}\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature\n")
        return path

    def leaf(self, san="hooks.test", label=None, ca=None):
        """A certificate that names `san` (and is called `label` on disk), signed by the authority (or by `ca`: (pem, key))."""
        label = label or f"leaf-{san}"
        if label in self._made:
            return self._made[label]
        key, csr = self._csr(label, san.split(",")[0])
        pem = os.path.join(self.dir, f"{label}.pem")
        cert, cakey = ca or (self.ca_pem, self.ca_key)
        sh(OPENSSL, "x509", "-req", "-in", csr, "-CA", cert, "-CAkey", cakey, "-CAcreateserial", "-out", pem, "-days", "365", "-extfile", self._ext(label, san), cwd=self.dir)
        self._made[label] = (pem, key)
        return pem, key

    def damaged(self, san="hooks.test"):
        """The certificate `leaf(san)` makes with the last bit of its signature changed: the authority's key does not verify it, and the receiver's key still matches it, so the
        receiver can serve it."""
        label = f"damaged-{san}"
        if label in self._made:
            return self._made[label]
        pem, key = self.leaf(san)
        body = re.search(r"-----BEGIN CERTIFICATE-----\n(.*?)-----END CERTIFICATE-----", open(pem).read(), re.S).group(1)
        der = bytearray(base64.b64decode(body))
        der[-1] ^= 0x01
        out = os.path.join(self.dir, f"{label}.pem")
        with open(out, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(bytes(der)).decode() + "-----END CERTIFICATE-----\n")
        self._made[label] = (out, key)
        return out, key

    def other_leaf(self, san="hooks.test"):
        return self.leaf(san, label=f"other-{san}", ca=(self.other_ca_pem, self.other_ca_key))

    def expired(self, san="hooks.test"):
        label = f"expired-{san}"
        if label in self._made:
            return self._made[label]
        key, csr = self._csr(label, san)
        pem = os.path.join(self.dir, f"{label}.pem")
        sh(OPENSSL, "ca", "-config", "ca.cnf", "-cert", self.ca_pem, "-keyfile", self.ca_key, "-in", csr, "-out", pem, "-startdate", "20200101000000Z",
           "-enddate", "20200102000000Z", "-extfile", self._ext(label, san), "-batch", "-notext", cwd=self.dir)
        self._made[label] = (pem, key)
        return pem, key

    def not_yet_valid(self, san="hooks.test"):
        label = f"future-{san}"
        if label in self._made:
            return self._made[label]
        key, csr = self._csr(label, san)
        pem = os.path.join(self.dir, f"{label}.pem")
        sh(OPENSSL, "ca", "-config", "ca.cnf", "-cert", self.ca_pem, "-keyfile", self.ca_key, "-in", csr, "-out", pem, "-startdate", "20900101000000Z",
           "-enddate", "20910101000000Z", "-extfile", self._ext(label, san), "-batch", "-notext", cwd=self.dir)
        self._made[label] = (pem, key)
        return pem, key

    def wrong_purpose(self, san="hooks.test"):
        """Signed by the authority, names the host, and may be used to authenticate a client and not a server."""
        label = f"purpose-{san}"
        if label in self._made:
            return self._made[label]
        key, csr = self._csr(label, san)
        pem = os.path.join(self.dir, f"{label}.pem")
        ext = os.path.join(self.dir, f"{label}.ext")
        with open(ext, "w") as f:
            f.write(f"subjectAltName=DNS:{san}\nbasicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\n")
        sh(OPENSSL, "x509", "-req", "-in", csr, "-CA", self.ca_pem, "-CAkey", self.ca_key, "-CAcreateserial", "-out", pem, "-days", "365", "-extfile", ext, cwd=self.dir)
        self._made[label] = (pem, key)
        return pem, key

    def selfsigned(self, san="hooks.test"):
        label = f"self-{san}"
        if label in self._made:
            return self._made[label]
        pem = os.path.join(self.dir, f"{label}.pem")
        key = os.path.join(self.dir, f"{label}.key")
        sh(OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", pem, "-days", "365", "-subj", f"/CN={san}",
           "-addext", f"subjectAltName=DNS:{san}", cwd=self.dir)
        self._made[label] = (pem, key)
        return pem, key


class TlsServer:
    """A TLS receiver on 127.0.0.1 that records every request it gets, as a list of dicts (`seen`): the headers, the body, the name the client asked for in SNI, the
    protocol, and whether the client resumed a session. `mode`:

        ok              completes the handshake, reads the request, answers `status` (default 204)
        close           reads the client's hello and closes the connection without a word: an orderly end of the stream during the handshake
        reset           reads one byte of it and closes: the connection is reset during the handshake
        hold            accepts and says nothing (the handshake never completes) until `release()`
        silent          completes the handshake, reads the request and never answers (until `release()`)
        split           answers its status line in two TLS records, 300 ms apart
        tls11           offers TLS 1.0 and 1.1 only
        garbage         accepts and answers bytes that are not TLS
    """

    def __init__(self, cert=None, key=None, mode="ok", status=204, tls11=False, port=0, tls12=False, rcvbuf=None, read_delay=0.0):
        self.mode, self.status, self.read_delay = mode, status, read_delay
        self.seen, self.handshakes, self.resumed, self.failed, self.accepted = [], 0, 0, [], 0
        self.lock = threading.Lock()
        self.released = threading.Event()
        self.sni = []
        self.ctx = None
        if cert:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            if tls11 or mode == "tls11":
                self.ctx.minimum_version = ssl.TLSVersion.TLSv1
                self.ctx.maximum_version = ssl.TLSVersion.TLSv1_1
                self.ctx.set_ciphers("ALL:@SECLEVEL=0")
            if tls12:
                self.ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            self.ctx.load_cert_chain(cert, key)
            self.ctx.sni_callback = self._on_sni
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.bind(("127.0.0.1", port))
        s.listen(256)
        self.sock = s
        self.port = s.getsockname()[1]
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _on_sni(self, sslsock, name, ctx):
        with self.lock:
            self.sni.append(name)
        return None

    def _accept(self):
        while not self.closing:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.accepted += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def release(self):
        self.released.set()

    def new_keys(self, cert, key):
        """A new context, as a receiver that was restarted has: the tickets it gave before are no good."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        ctx.sni_callback = self._on_sni
        self.ctx = ctx

    def close(self):
        self.closing = True
        self.released.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self, c):
        c.settimeout(10)
        mode = self.mode
        try:
            if mode == "close":
                # reads what the client sent (its hello) and closes: an orderly end of the stream, a FIN, in the middle of the handshake
                try:
                    c.recv(65536)
                except OSError:
                    pass
                c.close()
                return
            if mode == "reset":
                # reads one byte of the hello and closes with the rest unread: the kernel resets the connection (an RST)
                try:
                    c.recv(1)
                except OSError:
                    pass
                c.close()
                return
            if mode == "hold":
                self.released.wait(30)
                c.close()
                return
            if mode == "garbage":
                try:
                    c.recv(4096)
                    c.sendall(b"HTTP/1.1 200 not tls at all\r\n\r\n")
                except OSError:
                    pass
                c.close()
                return
            try:
                t = self.ctx.wrap_socket(c, server_side=True)
            except (ssl.SSLError, OSError) as e:
                with self.lock:
                    self.failed.append(str(e))
                c.close()
                return
            with self.lock:
                self.handshakes += 1
                if t.session_reused:
                    self.resumed += 1
            buf = b""
            chunk_size = 2048 if self.read_delay else 65536
            try:
                while b"\r\n\r\n" not in buf:
                    chunk = t.recv(chunk_size)
                    if not chunk:
                        break
                    buf += chunk
                head, _, body = buf.partition(b"\r\n\r\n")
                lines = head.decode(errors="replace").split("\r\n")
                headers = [tuple(l.split(": ", 1)) for l in lines[1:] if ": " in l]
                need = int(dict((k.lower(), v) for k, v in headers).get("content-length", "0"))
                while len(body) < need:
                    if self.read_delay:
                        time.sleep(self.read_delay)
                    chunk = t.recv(chunk_size)
                    if not chunk:
                        break
                    body += chunk
                with self.lock:
                    self.seen.append({"request": lines[0], "headers": headers, "body": body, "sni": self.sni[-1] if self.sni else None, "reused": t.session_reused,
                                      "version": t.version(), "cipher": t.cipher()[0]})
                if mode == "silent":
                    self.released.wait(30)
                    t.close()
                    return
                code = self.status(len(self.seen) - 1) if callable(self.status) else self.status
                if mode == "split":
                    # the status line in two TLS records, with a pause between them
                    t.sendall(b"HTT")
                    time.sleep(0.3)
                    t.sendall(f"P/1.1 {code} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                    t.close()
                    return
                t.sendall(f"HTTP/1.1 {code} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                try:
                    t.unwrap()
                except (ssl.SSLError, OSError):
                    pass
            except (ssl.SSLError, OSError) as e:
                with self.lock:
                    self.failed.append(str(e))
            t.close()
        except Exception:  # noqa: BLE001
            try:
                c.close()
            except OSError:
                pass

    def count(self):
        with self.lock:
            return len(self.seen)

    def events(self):
        import json
        with self.lock:
            out = []
            for r in self.seen:
                try:
                    out.append(json.loads(r["body"]).get("n"))
                except ValueError:
                    out.append(None)
            return out


class DnsStub:
    """A name server that speaks DNS over TCP on 127.0.0.1. `names` maps a name to a list of addresses, or to a function of the number of questions about that name so far
    (0 for the first) that answers such a list; a name that is not there gets NXDOMAIN. `delay` (seconds) holds every answer back (a dict of name to seconds holds back only those names); `rcode` forces an error code
    (2 SERVFAIL, 5 REFUSED) for every question; `garbage` answers bytes that are not DNS; `wrong_id` answers with another query's id. `asked` lists the names asked, in order."""

    def __init__(self, names, delay=0.0, rcode=0, port=0, ttl=60, garbage=False, wrong_id=False):
        self.names, self.delay, self.rcode, self.ttl, self.garbage = {k.lower(): v for k, v in names.items()}, delay, rcode, ttl, garbage
        self.wrong_id = wrong_id
        self.asked, self.lock, self.connections = [], threading.Lock(), 0
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(256)
        self.sock, self.port, self.closing = s, s.getsockname()[1], False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closing = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept(self):
        while not self.closing:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _qname(msg):
        i, labels = 12, []
        while msg[i] != 0:
            labels.append(msg[i + 1:i + 1 + msg[i]].decode())
            i += 1 + msg[i]
        return ".".join(labels), i + 1 + 4

    def _serve(self, c):
        c.settimeout(10)
        try:
            while True:
                head = b""
                while len(head) < 2:
                    chunk = c.recv(2 - len(head))
                    if not chunk:
                        return
                    head += chunk
                n = struct.unpack(">H", head)[0]
                msg = b""
                while len(msg) < n:
                    chunk = c.recv(n - len(msg))
                    if not chunk:
                        return
                    msg += chunk
                name, end = self._qname(msg)
                key = name.lower()
                with self.lock:
                    count = sum(1 for a in self.asked if a == key)
                    self.asked.append(key)
                wait = self.delay.get(key, 0) if isinstance(self.delay, dict) else self.delay
                if wait:
                    time.sleep(wait)
                if self.garbage:
                    junk = b"\x00\x20" + bytes(range(32))
                    c.sendall(junk)
                    continue
                answer = self.names.get(key)
                if callable(answer):
                    answer = answer(count)
                rcode = self.rcode if self.rcode else (0 if answer is not None else 3)
                flags = 0x8180 | rcode
                records = b""
                for ip in (answer or []):
                    records += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, self.ttl, 4) + socket.inet_aton(ip)
                count_an = len(answer or []) if not self.rcode else 0
                if self.rcode:
                    records = b""
                ident = msg[:2] if not self.wrong_id else bytes([msg[0] ^ 0xFF, msg[1]])
                resp = ident + struct.pack(">HHHHH", flags, 1, count_an, 0, 0) + msg[12:end] + records
                c.sendall(struct.pack(">H", len(resp)) + resp)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def questions(self, name=None):
        with self.lock:
            if name is None:
                return len(self.asked)
            return sum(1 for a in self.asked if a == name.lower())


class Sink:
    """A plain http receiver on `host` (127.0.0.1 unless said), `port` (any unless said), that counts **connections**, even ones that send nothing (`connections`), and keeps every
    request it is sent (`seen`: the request line, the headers, the body). Answers `status`."""

    def __init__(self, host="127.0.0.1", port=0, status=204):
        self.status, self.connections, self.seen, self.lock, self.closing = status, 0, [], threading.Lock(), False
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(256)
        self.sock, self.port, self.host = s, s.getsockname()[1], host
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closing:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        import json
        c.settimeout(10)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.decode(errors="replace").split("\r\n")
            headers = [tuple(l.split(": ", 1)) for l in lines[1:] if ": " in l]
            need = int(dict((k.lower(), v) for k, v in headers).get("content-length", "0"))
            while len(body) < need:
                chunk = c.recv(65536)
                if not chunk:
                    break
                body += chunk
            try:
                n = json.loads(body).get("n")
            except ValueError:
                n = None
            with self.lock:
                self.seen.append({"request": lines[0], "headers": headers, "body": body, "n": n})
            c.sendall(f"HTTP/1.1 {self.status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def count(self):
        with self.lock:
            return len(self.seen)

    def events(self):
        with self.lock:
            return [r["n"] for r in self.seen]

    def close(self):
        self.closing = True
        try:
            self.sock.close()
        except OSError:
            pass


def tick_gap(svc, n):
    """The longest time one request to the service took, over `n` in a row: a service whose loop is held by anything cannot answer sooner than the hold."""
    worst = 0.0
    for _ in range(n):
        t = time.time()
        svc.get("/stats", timeout=10)
        worst = max(worst, time.time() - t)
        time.sleep(0.005)
    return worst


def rss_kb(pid):
    """The resident set of a process in KiB, and the number of open descriptors."""
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
    return kb, len(os.listdir(f"/proc/{pid}/fd"))


def mappings_kb(pid):
    """The resident KiB of each mapping of a process ({"start-end name": KiB}), to say where a jump of the resident set went."""
    out, key = {}, None
    with open(f"/proc/{pid}/smaps") as f:
        for line in f:
            head = line.split()
            if head and "-" in head[0] and not head[0].endswith(":"):
                key = head[0] + (" " + head[5] if len(head) > 5 else " [anon]")
            elif head and head[0] == "Rss:" and key:
                out[key] = int(head[1])
    return out


def grown(before, after, top=5):
    """The mappings whose resident size grew most between two `mappings_kb`, as text."""
    d = sorted(((after.get(k, 0) - before.get(k, 0), k) for k in set(before) | set(after)), reverse=True)
    return "; ".join(f"{k} +{n} KiB" for n, k in d[:top] if n > 0) or "no mapping grew"
