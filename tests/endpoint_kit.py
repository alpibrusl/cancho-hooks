"""What the tests of the per-endpoint extras share (docs/design.md section 35): the database, a receiver that keeps everything it is sent,
the service started and stopped, and the calls of the API.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...]      (default 127.0.0.1:5432:postgres:hooks, no password)

Not a test: `tests/filter_test.py`, `tests/rotation_test.py` and `tests/headers_test.py` import it.
"""
import base64
import http.client
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 256
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "correct-horse-battery-staple"
FAILS = []


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def finish(what):
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        sys.exit(1)
    print(f"all {what} checks passed")


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def reset_db():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("drop trigger if exists hooks_test_trigger on endpoints")
    psql("truncate endpoints, attempts")


def pg_flags():
    f = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


class Receiver:
    """Keeps every request: its event number and type, all its headers as sent (name, value), the body and the path. `status` answers the next
    request (a function of the request's index, 0-based) or is fixed."""

    def __init__(self, status=204, delay=0.0, port=0):
        self.seen, self.status, self.delay = [], status, delay
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    ev = json.loads(body)
                except ValueError:
                    ev = {}
                with outer.lock:
                    index = len(outer.seen)
                    outer.seen.append({"n": ev.get("n"), "type": ev.get("type"), "headers": list(self.headers.items()), "body": body, "path": self.path,
                                       "version": self.request_version})
                if outer.delay:
                    time.sleep(outer.delay)
                code = (outer.status(index, ev.get("n")) if outer.status.__code__.co_argcount >= 2 else outer.status(index)) if callable(outer.status) else outer.status
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def events(self):
        with self.lock:
            return [r["n"] for r in self.seen]

    def count(self):
        with self.lock:
            return len(self.seen)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Svc:
    pass


def start(d, extra=None, schedule="100", deadline="5000", private=True, env=None):
    port = chaos.free_port()
    flags = pg_flags() + ["--admin-token", TOKEN] if extra is None else extra
    priv = ["--allow-private-hosts", "1"] if private else []
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, *priv, "--schedule", schedule, "--deadline-ms", deadline, *flags], stderr=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, env=env)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    svc = Svc()
    svc.port, svc.proc, svc.lines, svc.exited, svc.dir = port, proc, lines, line != "listening", d
    return svc


def stop(svc):
    if not svc.exited and svc.proc.poll() is None:
        svc.proc.terminate()
    svc.proc.wait()


def kill9(svc):
    if svc.proc.poll() is None:
        svc.proc.kill()
    svc.proc.wait()


def tmp(prefix="hooks-kit-"):
    return tempfile.mkdtemp(prefix=prefix)


def req(svc, method, path, body=None, token=TOKEN, raw=False, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=15)
    h = {"Authorization": "Bearer " + token} if token is not None else {}
    h.update(headers or {})
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    if raw:
        return r.status, out
    try:
        return r.status, (json.loads(out) if out else None)
    except ValueError:
        return r.status, out


def get(svc, path):
    return req(svc, "GET", path, token=None)[1]


def post_event(svc, n, typ="t", extra=None, key=None, raw=None):
    """POST /events: `{"type": typ, "n": n}` (or the bytes `raw`), under an Idempotency-Key if one is given. Answers (status, body)."""
    ev = {"type": typ, "n": n}
    ev.update(extra or {})
    body = raw if raw is not None else json.dumps(ev).encode()
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    try:
        c.request("POST", "/events", body=body, headers={"Content-Type": "application/json", **({"Idempotency-Key": key} if key else {})})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def wait_for(cond, secs, step=0.02):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def endpoints(svc):
    return {e["id"]: e for e in get(svc, "/endpoints")}


def cursors(svc):
    return {i: e["cursor"] for i, e in endpoints(svc).items()}


def add_endpoint(ident, port, sec, types="", headers="", old="", until=0):
    psql(f"insert into endpoints (id, host, port, secret, types, headers, secret_old, secret_old_until) values ({ident}, '127.0.0.1', {port}, '{sec}', '{types}', '{headers}', '{old}', {until})")
