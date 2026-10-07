"""A receiver that keeps HTTP/1.1 connections, plain or TLS, and counts them (docs/design.md section 53): `tests/keepalive_test.py` and
`scripts/bench/https_cost.py`'s `kept` rows."""
import base64
import hashlib
import hmac
import socket
import ssl
import threading
import time


class Receiver:
    """An HTTP/1.1 receiver that keeps connections. `mode`:
    length (default), close, chunked, empty204, http10, both, huge, twolengths, gone (a 410), stall, idle (closes idle connections after `idle_close` s),
    racy (closes a reused connection after reading its request, without answering), sulk (answers the first request it ever reads, and closes on every later
    one without answering), overlong (a body longer than its Content-Length), chunk_split (a chunked body whose last CRLF comes 0.2 s later), chatty (bytes
    nobody asked for 0.1 s after each response), bighead (a header block over 8 KiB). With `fail_key`, a request signed with that secret is answered
    with a 500 (whatever the mode), so two endpoints behind one receiver are told apart.
    `close_after`: for each connection the service closed, the seconds from the last response to the close; `clean`/`unclean`: TLS connections the service
    ended with and without close_notify."""

    def __init__(self, tls_cert=None, mode="length", idle_close=None, fail_key=None):
        self.mode, self.idle_close, self.fail_key = mode, idle_close, fail_key
        self.failed_for_key = 0
        self.lock = threading.Lock()
        self.connections = self.requests = self.handshakes = self.dropped = 0
        self.ids = []
        self.ctx = None
        if tls_cert:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*tls_cert)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.closed_by_peer = 0
        self.close_after = []
        self.clean = self.unclean = 0
        self.answered_once = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _read_request(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            data = c.recv(65536)
            if not data:
                return None
            buf += data
        head, _, rest = buf.partition(b"\r\n\r\n")
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        need = int(headers.get(b"content-length", b"0"))
        while len(rest) < need:
            data = c.recv(65536)
            if not data:
                return None
            rest += data
        headers[b":body"] = rest[:need]
        return headers

    def _signed_with_fail_key(self, headers):
        """Is the request signed with `fail_key` (an endpoint's secret, `whsec_...`)? The receiver answers those with a 500."""
        if not self.fail_key:
            return False
        mid, ts, sig = (headers.get(k, b"").decode() for k in (b"webhook-id", b"webhook-timestamp", b"webhook-signature"))
        want = base64.b64encode(hmac.new(base64.b64decode(self.fail_key[6:]), f"{mid}.{ts}.".encode() + headers[b":body"], hashlib.sha256).digest()).decode()
        return "v1," + want in sig.split()

    def _answer(self):
        m = self.mode
        if m == "close":
            return b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        if m == "chunked":
            return b"HTTP/1.1 200 X\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n3;ext=1\r\nabc\r\n0\r\nX-Trailer: t\r\n\r\n"
        if m == "empty204":
            return b"HTTP/1.1 204 X\r\n\r\n"
        if m == "http10":
            return b"HTTP/1.0 200 X\r\nContent-Length: 2\r\n\r\nok"
        if m == "both":
            return b"HTTP/1.1 200 X\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        if m == "huge":
            return b"HTTP/1.1 200 X\r\nContent-Length: 70000\r\n\r\n" + b"x" * 70000
        if m == "gone":
            return b"HTTP/1.1 410 X\r\nContent-Length: 0\r\n\r\n"
        if m == "overlong":
            return b"HTTP/1.1 200 X\r\nContent-Length: 2\r\n\r\nokxyz"
        if m == "bighead":
            return b"HTTP/1.1 200 X\r\nX-Big: " + b"b" * 9000 + b"\r\nContent-Length: 2\r\n\r\nok"
        if m == "twolengths":
            return b"HTTP/1.1 200 X\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok"
        return b"HTTP/1.1 200 X\r\nContent-Length: 2\r\n\r\nok"

    def _answer_chunked(self):
        return b"HTTP/1.1 200 X\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"

    def _serve(self, raw):
        c = raw
        try:
            if self.ctx:
                c = self.ctx.wrap_socket(raw, server_side=True, suppress_ragged_eofs=False)
                with self.lock:
                    self.handshakes += 1
            with self.lock:
                self.connections += 1
            served = 0
            sent_at = None
            while True:
                c.settimeout(self.idle_close if self.idle_close else 60)
                try:
                    headers = self._read_request(c)
                except socket.timeout:
                    return
                except ConnectionResetError:
                    # the service closed with bytes of the response unread (a reset)
                    with self.lock:
                        self.closed_by_peer += 1
                        if sent_at is not None:
                            self.close_after.append(time.time() - sent_at)
                    return
                except ssl.SSLEOFError:
                    # the service closed without close_notify
                    with self.lock:
                        self.closed_by_peer += 1
                        self.unclean += 1
                        if sent_at is not None:
                            self.close_after.append(time.time() - sent_at)
                    return
                if headers is None:
                    with self.lock:
                        self.closed_by_peer += 1
                        if self.ctx:
                            self.clean += 1
                        if sent_at is not None:
                            self.close_after.append(time.time() - sent_at)
                    return
                with self.lock:
                    self.requests += 1
                    self.ids.append(headers.get(b"webhook-id", b"?").decode())
                if self.mode == "racy" and served > 0:
                    with self.lock:
                        self.dropped += 1
                    return
                if self.mode == "sulk":
                    with self.lock:
                        first = not self.answered_once
                        self.answered_once = True
                        if not first:
                            self.dropped += 1
                    if not first:
                        return
                if self.mode == "stall":
                    c.sendall(b"HTTP/1.1 200 X\r\nContent-Length: 100\r\n\r\npartial")
                    sent_at = time.time()
                    # wait (at most 8 s) for the service to give up on the body and close
                    c.settimeout(8)
                    try:
                        while c.recv(1024):
                            pass
                        with self.lock:
                            self.close_after.append(time.time() - sent_at)
                    except (OSError, ssl.SSLError):
                        pass
                    return
                if self.mode == "chunk_split":
                    a = self._answer_chunked()
                    c.sendall(a[:-2])
                    time.sleep(0.2)
                    c.sendall(a[-2:])
                elif self._signed_with_fail_key(headers):
                    with self.lock:
                        self.failed_for_key += 1
                    c.sendall(b"HTTP/1.1 500 X\r\nContent-Length: 0\r\n\r\n")
                else:
                    c.sendall(self._answer())
                sent_at = time.time()
                if self.mode == "chatty":
                    time.sleep(0.1)
                    c.sendall(b"junk")
                served += 1
                if self.mode in ("close", "http10", "both", "huge", "twolengths"):
                    # the service closes these; give it a moment to, then go
                    c.settimeout(5)
                    try:
                        while c.recv(1024):
                            pass
                    except (OSError, ssl.SSLError):
                        pass
                    return
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def close(self):
        self.sock.close()
