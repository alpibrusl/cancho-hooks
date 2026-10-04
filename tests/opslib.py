"""Helpers shared by the operations tests (tests/metrics_test.py, ready_test.py, reason_test.py, stop_test.py, corrupt_test.py): a service
whose stderr is kept, receivers that fail in a chosen way, an independent reader of the logs, and a strict parser of the Prometheus text
format. Nothing here is imported by the service's own tests, and nothing in it is the thing under test."""
import base64
import http.client
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import pgwait  # noqa: E402

PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], PG[1], PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "build", "fsync_shim.so")


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, name, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
        if not ok:
            self.fails.append(name)
        return ok

    def finish(self, label):
        if self.fails:
            print(f"FAILED {len(self.fails)}: " + "; ".join(self.fails))
            return 1
        print(f"all {label} checks passed")
        return 0


def wait_for(cond, secs, step=0.02):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


# ---- PostgreSQL -------------------------------------------------------------------------------------------------------

def pg_env():
    return dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)


def psql(sql, host=None, port=None):
    out = subprocess.run(["psql", "-h", host or PG_HOST, "-p", str(port or PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=pg_env())
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def apply_schema():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=pg_env())


def pg_flags(port=None):
    return ["--pg-host", PG_HOST, "--pg-port", str(port or PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB] + (["--pg-password", PG_PASSWORD] if PG_PASSWORD else [])


# ---- the service ------------------------------------------------------------------------------------------------------

class Service:
    """A running `hooks`, its stderr kept line by line. `start()` waits for `listening` (or for the process to end: `exited`)."""

    def __init__(self, bin_path, datadir, args=(), port=None, power_loss=False, env=None):
        self.bin, self.dir, self.args = os.path.abspath(bin_path), datadir, list(args)
        self.port = port or chaos.free_port()
        self.lines, self.proc, self.power_loss, self.extra_env = [], None, power_loss, env or {}
        self.lock = threading.Lock()
        self.reader = None

    def start(self, timeout=15.0, loaded=True):
        env = dict(os.environ, **self.extra_env)
        if self.power_loss and os.path.exists(SHIM):
            env["LD_PRELOAD"] = SHIM
        self.lines = []
        # A job started in the background by a non-interactive shell has SIGINT ignored, and a child inherits that: the service would then never see a SIGINT. The
        # test is of the service's handling of the signal, so it starts it with the default disposition, as systemd and a terminal do.
        self.proc = subprocess.Popen([self.bin, "--port", str(self.port), "--dir", self.dir, "--allow-private-hosts", "1", *self.args],
                                     stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self.reader.start()
        wait_for(lambda: "listening" in self.lines or self.proc.poll() is not None, timeout)
        if loaded and "listening" in self.lines and any(a == "--pg-host" for a in self.args):
            # the socket is open; the endpoints are read from the table a moment later (docs/design.md section 37)
            wait_for(lambda: self.has_line("endpoints loaded") or self.proc.poll() is not None, timeout)
        return "listening" in self.lines

    def _read(self, proc):
        for raw in proc.stderr:
            with self.lock:
                self.lines.append(raw.decode(errors="replace").rstrip("\n"))

    def stderr(self):
        with self.lock:
            return "\n".join(self.lines)

    def has_line(self, text):
        with self.lock:
            return any(text in line for line in self.lines)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_exit(self, secs):
        try:
            code = self.proc.wait(timeout=secs)
        except subprocess.TimeoutExpired:
            return None
        # The process has exited, so its end of the pipe is closed: let the thread that reads stderr take what is left in it before anyone looks at the lines.
        # (A service that stops in a fraction of a millisecond is gone before the thread has read what it said last.)
        if self.reader is not None:
            self.reader.join(5)
        return code

    def kill(self):
        if self.alive():
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def term(self):
        self.proc.send_signal(signal.SIGTERM)

    def stop(self, secs=10):
        """SIGTERM and wait; the exit status (None if it had to be killed)."""
        if not self.alive():
            return self.proc.returncode if self.proc else None
        self.term()
        code = self.wait_exit(secs)
        if code is None:
            self.kill()
        wait_for(lambda: not self.alive(), 2)
        return code

    # -- HTTP
    def request(self, method, path, body=None, headers=None, timeout=5.0):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def get(self, path, timeout=5.0):
        status, data, _ = self.request("GET", path, timeout=timeout)
        return status, data

    def get_json(self, path, timeout=5.0):
        status, data = self.get(path, timeout)
        return status, json.loads(data)

    def post_event(self, n, key=None, extra=None, timeout=5.0):
        body = json.dumps({"type": "ops.test", "n": n, **(extra or {})}).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        status, data, _ = self.request("POST", "/events", body, headers, timeout)
        return status, data

    def stats(self):
        return self.get_json("/stats")[1]

    def metrics(self):
        status, data = self.get("/metrics")
        assert status == 200, (status, data[:200])
        return parse_metrics(data.decode())


# ---- receivers that fail in a chosen way -----------------------------------------------------------------------------------

class Peer:
    """A receiver on a raw socket, so that it can fail the way a real one does. `mode` can be changed while it runs:

        ok          204                              status      the `status` attribute, with no body
        silent      reads the request, never answers (until `release()`)
        slow        answers 204 after `delay` seconds (or at `release()`)
        reset       sends the start of a status line, then resets the connection (RST)
        close       reads the request and closes without a word
        garbage     answers twelve bytes that are not HTTP
    """

    def __init__(self, mode="ok", status=204, delay=0.0):
        self.mode, self.status, self.delay = mode, status, delay
        self.hits, self.lock, self.released = [], threading.Lock(), threading.Event()
        self.open_conns = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(256)
        self.port = self.srv.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.alive:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _read_request(self, c):
        buf = b""
        c.settimeout(10)
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
        return headers, rest[:need]

    def _serve(self, c):
        try:
            got = self._read_request(c)
            if got is None:
                return
            headers, body = got
            eid = int(headers.get(b"webhook-id", b"evt_0")[4:])
            with self.lock:
                self.hits.append((eid, time.time(), self.mode))
                self.open_conns.append(c)
            mode = self.mode
            if mode == "ok" or mode == "status":
                code = 204 if mode == "ok" else self.status
                c.sendall(f"HTTP/1.1 {code} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            elif mode == "slow":
                self.released.wait(self.delay)
                c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            elif mode == "silent":
                self.released.wait(120)
            elif mode == "reset":
                c.sendall(b"HTTP/1.1 2")
                time.sleep(0.05)
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            elif mode == "close":
                pass
            elif mode == "garbage":
                c.sendall(b"HELLO, THIS IS NOT HTTP\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def release(self):
        self.released.set()

    def events(self):
        with self.lock:
            return [e for e, _, _ in self.hits]

    def count(self):
        with self.lock:
            return len(self.hits)

    def distinct(self):
        return set(self.events())

    def close(self):
        self.alive = False
        self.released.set()
        try:
            self.srv.close()
        except OSError:
            pass


def closed_port():
    """A port nothing listens on (bound and released a moment ago)."""
    return chaos.free_port()


# ---- the logs, read independently of the service ----------------------------------------------------------------------

def events_log(d):
    path = os.path.join(d, "events.seg")
    data = open(path, "rb").read() if os.path.exists(path) else b""
    recs, end = chaos.read_log(data)
    return {ms: dict(pairs).get(b"event") for ms, pairs in recs}, len(data), end


def outcomes(d):
    """[(kind, endpoint slot, event, attempts, next_at)] from delivery.seg."""
    path = os.path.join(d, "delivery.seg")
    data = open(path, "rb").read() if os.path.exists(path) else b""
    recs, _ = chaos.read_log(data)
    return [struct.unpack("<5q", pairs[0][1]) for _ms, pairs in recs]


def put_record(seq, kind, slot, event=0, attempts=0, next_at=0):
    """One outcome record as the service writes it (src/state.ls `put_outcome`): the log's record around five 8-byte integers."""
    value = struct.pack("<5Q", kind, slot, event, attempts, next_at)
    body = struct.pack("<QQI", seq, 0, 1) + struct.pack("<I", 1) + b"o" + struct.pack("<I", len(value)) + value
    return struct.pack("<I", 4 + len(body)) + struct.pack("<I", chaos.crc32c(body)) + body


def put_event(ms, body):
    """One event record as the service writes it."""
    rec = struct.pack("<QQI", ms, 0, 1) + struct.pack("<I", 5) + b"event" + struct.pack("<I", len(body)) + body
    return struct.pack("<I", 4 + len(rec)) + struct.pack("<I", chaos.crc32c(rec)) + rec


KINDS = {1: "delivered", 2: "failed", 3: "dead", 4: "disabled", 5: "enabled", 6: "replay", 7: "replay_failed", 8: "replay_delivered", 9: "replay_dead",
         10: "created", 11: "removed", 12: "streak", 13: "paused", 14: "reason"}
REASONS = {1: "connect_refused", 2: "connect_timeout", 3: "connect_error", 4: "send_timeout", 5: "send_error", 6: "no_response", 7: "reset",
           8: "closed_early", 9: "bad_response", 10: "status_3xx", 11: "status_4xx", 12: "status_5xx", 13: "gone", 14: "status_other", 15: "busy", 16: "too_large"}


def log_counts(d):
    """({kind name: n}, {reason name: n}) over delivery.seg."""
    kinds, reasons = {}, {}
    for kind, _e, _ev, _at, nxt in outcomes(d):
        kinds[KINDS[kind]] = kinds.get(KINDS[kind], 0) + 1
        if kind == 14:
            r = REASONS.get(nxt % 256, "unknown")
            reasons[r] = reasons.get(r, 0) + 1
    return kinds, reasons


# ---- the Prometheus text format, strictly ----------------------------------------------------------------------------

_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (-?[0-9]+(?:\.[0-9]+)?)$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"\\\n]*)"')


class Metrics(dict):
    """{(name, ((label, value), ...)): float}, with `.types` {family: type} and `.helps`."""
    types, helps = None, None

    def value(self, name, **labels):
        return self[(name, tuple(sorted(labels.items())))]

    def series(self, name):
        return {k[1]: v for k, v in self.items() if k[0] == name}

    def total(self, name):
        return sum(self.series(name).values())


def parse_metrics(text):
    """The text exposition format 0.0.4, strictly: every line is a HELP, a TYPE or a sample; a family's HELP and TYPE come once, before its samples, and its
    samples are together; no series twice; label values are not escaped here because the service writes none that need it."""
    out, types, helps = Metrics(), {}, {}
    seen_family, current, closed = set(), None, set()
    assert text.endswith("\n"), "the text does not end with a newline"
    for lineno, line in enumerate(text.split("\n")[:-1], 1):
        assert line, f"line {lineno}: empty line"
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in helps, f"line {lineno}: two HELP lines for {name}"
            helps[name] = line
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert name not in types, f"line {lineno}: two TYPE lines for {name}"
            assert kind in ("counter", "gauge", "histogram", "summary", "untyped"), f"line {lineno}: type {kind}"
            types[name] = kind
            continue
        assert not line.startswith("#"), f"line {lineno}: comment {line!r}"
        m = _SAMPLE.match(line)
        assert m, f"line {lineno}: not a sample: {line!r}"
        name, _, labels_text, value = m.groups()
        labels = tuple(sorted(_LABEL.findall(labels_text or "")))
        if labels_text:
            assert ",".join(f'{k}="{v}"' for k, v in _LABEL.findall(labels_text)) == labels_text, f"line {lineno}: labels not well formed: {labels_text!r}"
        assert name in types, f"line {lineno}: sample of {name} before its TYPE"
        if name != current:
            assert name not in closed, f"line {lineno}: samples of {name} are not together"
            if current:
                closed.add(current)
            current = name
        assert (name, labels) not in out, f"line {lineno}: series {name}{labels} twice"
        out[(name, labels)] = float(value)
    out.types, out.helps = types, helps
    for name in types:
        assert name in helps, f"{name} has a TYPE and no HELP"
        if types[name] == "counter":
            assert name.endswith("_total"), f"counter {name} does not end in _total"
    return out


def metrics_get(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
        return parse_metrics(r.read().decode())


def free_dir(prefix="hooks-ops-"):
    return tempfile.mkdtemp(prefix=prefix)
