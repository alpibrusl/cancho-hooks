"""What the tests of many endpoints share (docs/design.md section 41): one receiver that stands for every endpoint, the table filled in one statement,
the listing read page by page, and the outcome log read apart from the service.

Not a test: `tests/many_test.py` and `scripts/bench/many_bench.py` import it.

**One receiver, one address each.** Every address of 127.0.0.0/8 is this machine, so an endpoint `k` is `127.0.<k // 250 + 1>.<k % 250 + 1>` and all of them are
the same port of one socket bound to 0.0.0.0. The receiver tells which endpoint a request is for by the address the connection came *to* (`getsockname`), and
checks the signature with that endpoint's own key: a delivery sent with another endpoint's secret is a failure of the test, whatever address it was sent to.
"""
import base64
import hashlib
import hmac
import json
import os
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import opslib  # noqa: E402
from opslib import Checks, Service, wait_for  # noqa: E402,F401


def addr(k):
    """The address of endpoint number `k` (0 to 1,023 and beyond): none of them 127.0.0.1, which the service itself may use."""
    return f"127.0.{k // 250 + 1}.{k % 250 + 1}"


def key_of(secret):
    return base64.b64decode(secret[len("whsec_"):])


def sign(secret, msg_id, ts, body):
    mac = hmac.new(key_of(secret), f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


class Fan:
    """The receiver for every endpoint. `endpoints` is {address: (id, secret)}; `policy(address_index, event, attempt)` answers what to do with the
    `attempt`th request (1-based) of that endpoint for that event: 204 (the default), another status number, "close" (no answer at all), "reset" or
    "hang" (the connection is held, no answer, until `release()`)."""

    def __init__(self, endpoints, port=0, policy=None):
        self.endpoints = {}
        self.index = {}
        self.add(endpoints)
        self.policy = policy or (lambda k, n, attempt: 204)
        self.lock = threading.Lock()
        self.hits = {}          # (address, event id) -> number of requests
        self.order = []         # (address, event id) in the order they came
        self.bad = []           # what was wrong: a signature that is not the endpoint's, an address nobody has
        self.held = []
        self.alive = True
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", port))
        self.srv.listen(4096)
        self.srv.setblocking(False)
        self.port = self.srv.getsockname()[1]
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.srv, selectors.EVENT_READ, None)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def add(self, endpoints):
        """More endpoints, {address: (id, secret)}: the receiver numbers them by their address, as `addr(k)` does."""
        self.endpoints.update(endpoints)
        self.index = {a: k for k, a in enumerate(sorted(self.endpoints, key=lambda a: tuple(int(x) for x in a.split("."))))}

    def _loop(self):
        while self.alive:
            try:
                ready = self.sel.select(0.05)
            except (OSError, ValueError):
                return
            for key, _ in ready:
                if key.data is None:
                    self._accept()
                else:
                    self._read(key.fileobj, key.data)

    def _accept(self):
        for _ in range(512):
            try:
                c, _ = self.srv.accept()
            except (BlockingIOError, OSError):
                return
            c.setblocking(False)
            self.sel.register(c, selectors.EVENT_READ, {"buf": b"", "to": c.getsockname()[0]})

    def _drop(self, c):
        try:
            self.sel.unregister(c)
        except (KeyError, ValueError):
            pass
        try:
            c.close()
        except OSError:
            pass

    def _read(self, c, st):
        try:
            data = c.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(c)
            return
        if not data:
            self._drop(c)
            return
        st["buf"] += data
        head, sep, rest = st["buf"].partition(b"\r\n\r\n")
        if not sep:
            return
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        need = int(headers.get(b"content-length", b"0"))
        if len(rest) < need:
            return
        body = rest[:need]
        self._handle(c, st["to"], headers, body)

    def _handle(self, c, to, headers, body):
        who = self.endpoints.get(to)
        wid = headers.get(b"webhook-id", b"").decode()
        try:
            n = int(wid.split("_", 1)[1])
        except (IndexError, ValueError):
            n = -1
        if who is None:
            with self.lock:
                self.bad.append(("no endpoint at", to))
            action = 204
        else:
            ident, secret = who
            ts = headers.get(b"webhook-timestamp", b"").decode()
            want = sign(secret, wid, ts, body)
            sigs = headers.get(b"webhook-signature", b"").decode().split(" ")
            with self.lock:
                if want not in sigs:
                    self.bad.append(("signature not the endpoint's", to, ident, n))
                key = (to, n)
                self.hits[key] = self.hits.get(key, 0) + 1
                attempt = self.hits[key]
                self.order.append(key)
            action = self.policy(self.index[to], n, attempt)
        if action == "hang":
            with self.lock:
                self.held.append(c)
            try:
                self.sel.unregister(c)
            except (KeyError, ValueError):
                pass
            return
        if action == "reset":
            try:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            self._drop(c)
            return
        if action != "close":
            try:
                c.sendall(f"HTTP/1.1 {action} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            except OSError:
                pass
        self._drop(c)

    def release(self):
        with self.lock:
            held, self.held = self.held, []
        for c in held:
            try:
                c.close()
            except OSError:
                pass

    def count(self):
        with self.lock:
            return len(self.order)

    def reached(self, events, addresses=None):
        """How many of the (address, event) pairs of `events` x `addresses` (default: every endpoint) have had a request."""
        with self.lock:
            return sum(1 for a in (addresses or self.endpoints) for n in events if (a, n) in self.hits)

    def missing(self, events, addresses=None):
        with self.lock:
            return [(a, n) for a in (addresses or self.endpoints) for n in events if (a, n) not in self.hits]

    def close(self):
        self.alive = False
        self.release()
        try:
            self.sel.close()
            self.srv.close()
        except OSError:
            pass


# ---- the table ---------------------------------------------------------------------------------------------------------------------

def reset_db():
    opslib.apply_schema()
    opslib.psql("truncate endpoints, attempts")
    try:
        opslib.psql("truncate schedules")
    except RuntimeError:
        pass


def insert_endpoints(count, port, first_id=0, ids=None, types=None, host=None):
    """`count` endpoints in the table in one statement: ids `ids` (default `first_id`...), endpoint number `k` at `addr(k)` on `port` (or all at `host`), each with a secret of
    its own. `types` is None or a function of the number giving the subscription ('' for none). Answers {address: (id, secret)}."""
    rows, out = [], {}
    for k in range(count):
        ident = ids[k] if ids else first_id + k
        a = host or addr(k)
        s = opslib.secret()
        t = types(k) if types else ""
        rows.append(f"({ident}, '{a}', {port}, '{s}', '{t}')")
        out[a] = (ident, s)
    batch, size = [], 0
    for row in rows + [None]:
        if row is None or size + len(row) > 60000 or len(batch) >= 256:
            if batch:
                opslib.psql("insert into endpoints (id, host, port, secret, types) values " + ",".join(batch))
            batch, size = [], 0
        if row is not None:
            batch.append(row)
            size += len(row)
    return out


def table_ids():
    return sorted(int(r[0]) for r in opslib.psql("select id from endpoints"))


# ---- the service's answers -----------------------------------------------------------------------------------------------------------

def listing(svc, limit=None):
    """GET /endpoints page by page, as a client that follows `X-Next-Offset` does: (the endpoints, the number of pages, the largest answer in bytes, X-Total-Count)."""
    out, pages, biggest, offset, total = [], 0, 0, 0, None
    while True:
        path = f"/endpoints?offset={offset}" + (f"&limit={limit}" if limit else "")
        status, data, headers = svc.request("GET", path)
        assert status == 200, (status, data[:200])
        h = {k.lower(): v for k, v in headers.items()}
        out += json.loads(data)
        pages += 1
        biggest = max(biggest, len(data))
        total = int(h["x-total-count"])
        if "x-next-offset" not in h:
            return out, pages, biggest, total
        offset = int(h["x-next-offset"])
        assert pages < 5000, "the listing does not end"


def metrics_page(svc, page=None):
    path = "/metrics" + ("" if page is None else f"?page={page}")
    status, data, _ = svc.request("GET", path, timeout=15)
    return status, data


def delivery_records(d):
    """delivery.seg as [(kind, slot, event, attempts, next_at)] with its header record first, read apart from the service."""
    path = os.path.join(d, "delivery.seg")
    data = open(path, "rb").read() if os.path.exists(path) else b""
    recs, _ = chaos.read_log(data, headers=True)
    return [struct.unpack("<5q", pairs[0][1]) for _ms, pairs in recs]


def kinds_of(d, kind):
    return [r for r in delivery_records(d) if r[0] == kind]


def wait_started(svc, timeout=60):
    return wait_for(lambda: svc.has_line("endpoints loaded") or not svc.alive(), timeout)


def rss_kb(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def cpu_seconds(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def fd_limit(n=8192):
    """Raise the soft limit of descriptors: the receiver holds a socket for each attempt on the wire (64) and the test one for each request, but chaos can leave many."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, n), hard), hard))
    except (ImportError, ValueError, OSError):
        pass
