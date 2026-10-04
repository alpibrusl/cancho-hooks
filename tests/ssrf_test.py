#!/usr/bin/env python3
"""Where a delivery may go (docs/design.md section 26).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/ssrf_test.py build/hooks

An endpoint is an address the service will POST to from inside the operator's network. Unless `allow-private-hosts` is 1 the host
must be an IPv4 literal outside the private, loopback, link-local and reserved ranges; a name, and every spelling of an address a
resolver accepts and a person does not expect, is refused.

  1. endpoints.conf: every private, reserved, malformed and named host is refused before the service listens (status 13) and the
     message says which line; every public one (the edges of the ranges included) is accepted and listed by GET /endpoints
  2. `allow-private-hosts 1`: the same hosts, names included, are accepted
  3. POST /endpoints: each refused host is a 400 that says why, nothing is stored (no row, no record in the log); a public host
     is a 201; with `allow-private-hosts 1` a loopback one is a 201 too
  4. a row put in the table by hand with a private host stops the service from starting (status 13); `--import-endpoints 1` refuses it
  5. a redirect is not followed: a receiver that answers 302 to another receiver is sent one request, the other none, and the
     attempt is a failure (only 2xx is delivered)
"""
import base64
import http.client
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import pgwait  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], int(PG[1]), PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
PSQL_ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "ssrf-test-token"
SECRET = "whsec_" + base64.b64encode(os.urandom(24)).decode()
FAILS = []

REFUSED = [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254",
    "169.254.0.1", "100.64.0.1", "100.127.255.255", "0.0.0.0", "0.1.2.3", "224.0.0.1", "239.255.255.255", "240.0.0.1", "255.255.255.255",
    "192.0.0.8", "192.0.2.1", "192.88.99.1", "198.18.0.1", "198.19.1.1", "198.51.100.1", "203.0.113.1",
    # not a literal in the sense of destination.ls: a name, a short or number form, a leading zero, IPv6, junk
    "localhost", "example.com", "metadata.google.internal", "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "127.0.0.01", "010.0.0.1",
    "1.2.3", "1.2.3.4.5", "256.1.1.1", "::1", "[::1]", "::ffff:127.0.0.1", "8.8.8.8.nip.io", "1.1.1.1.",
]
PUBLIC = [
    "1.1.1.1", "8.8.8.8", "9.255.255.255", "11.0.0.1", "100.63.255.255", "100.128.0.1", "126.255.255.255", "128.0.0.1", "169.253.255.255",
    "169.255.0.1", "172.15.255.255", "172.32.0.1", "192.0.1.1", "192.0.3.1", "192.167.255.255", "192.169.0.1", "198.17.255.255",
    "198.20.0.1", "198.51.99.1", "198.51.101.1", "203.0.112.1", "203.0.114.1", "223.255.255.254",
]


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def pg_flags():
    f = ["--pg-host", PG_HOST, "--pg-port", str(PG_PORT), "--pg-user", PG_USER, "--pg-database", PG_DB]
    if PG_PASSWORD:
        f += ["--pg-password", PG_PASSWORD]
    return f


def psql(sql):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=PSQL_ENV)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


class Svc:
    pass


def start(d, extra=()):
    port = chaos.free_port()
    proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--schedule", "100", "--deadline-ms", "800", *extra], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    lines = []
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line in ("listening", ""):
            break
    gone = line == "listening" and pgwait.after_listening(proc, lines, extra)
    svc = Svc()
    svc.port, svc.proc, svc.lines, svc.exited = port, proc, lines, line != "listening" or gone
    svc.status = proc.wait(timeout=10) if svc.exited else None
    return svc


def stop(svc):
    if not svc.exited:
        svc.proc.terminate()
        svc.proc.wait()


def req(svc, method, path, body=None, token=None):
    c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=15)
    h = {"Authorization": "Bearer " + token} if token else {}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (json.loads(out) if out else None)


def conf(d, hosts):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i, h in enumerate(hosts):
            f.write(f"{i} {h} 9 {SECRET}\n")


def tmp():
    return tempfile.mkdtemp(prefix="hooks-ssrf-")


def main():
    # 1. endpoints.conf, default policy
    bad = []
    for h in REFUSED:
        d = tmp()
        conf(d, ["8.8.8.8", h])
        svc = start(d)
        if not (svc.exited and svc.status == 13):
            bad.append((h, svc.status, svc.lines))
        elif "line 2" not in " ".join(svc.lines) and "2" not in " ".join(svc.lines):
            bad.append((h, "no line number", svc.lines))
        stop(svc)
        shutil.rmtree(d)
    check(f"1. all {len(REFUSED)} refused hosts stop the start with status 13 and name the line", not bad, str(bad[:3]))
    # all the public ones in one file (23 endpoints), listed by GET /endpoints
    d = tmp()
    conf(d, PUBLIC)
    svc = start(d)
    ok = not svc.exited
    listed = []
    if ok:
        status, body = req(svc, "GET", "/endpoints")
        listed = body
    check(f"1. all {len(PUBLIC)} public hosts, the edges of every range, are accepted and listed", ok and len(listed) == len(PUBLIC), str((svc.lines, len(listed))))
    stop(svc)
    shutil.rmtree(d)

    # 2. allow-private-hosts 1
    d = tmp()
    some = ["127.0.0.1", "10.0.0.1", "169.254.169.254", "localhost", "example.com", "192.168.0.1"]
    conf(d, some)
    svc = start(d, ["--allow-private-hosts", "1"])
    ok = not svc.exited
    n = len(req(svc, "GET", "/endpoints")[1]) if ok else 0
    check("2. with allow-private-hosts 1 private addresses and names are accepted", ok and n == len(some), str((svc.lines, n)))
    stop(svc)
    shutil.rmtree(d)

    # 3. POST /endpoints
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(ROOT, "sql", "schema.sql")],
                   check=True, capture_output=True, env=PSQL_ENV)
    psql("truncate endpoints")
    d = tmp()
    svc = start(d, pg_flags() + ["--admin-token", TOKEN])
    refused_ok, detail = True, []
    for h in REFUSED:
        status, body = req(svc, "POST", "/endpoints", {"host": h, "port": 9}, TOKEN)
        # "printable, no space" refusals (code 2) are fine for a malformed host too: what matters is a 400 and nothing stored
        if status != 400:
            refused_ok = False
            detail.append((h, status, body))
    check(f"3. every one of the {len(REFUSED)} refused hosts is a 400 from POST /endpoints", refused_ok, str(detail[:3]))
    status, body = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": 9}, TOKEN)
    check("3. ... and the message says why (SSRF, public IPv4)", status == 400 and "public IPv4" in json.dumps(body), str((status, body)))
    log = os.path.join(d, "delivery.seg")
    check("3. ... nothing was stored: no row, and nothing in the outcome log (no `created` record)", psql("select count(*) from endpoints") == [("0",)] and
          (not os.path.exists(log) or chaos.read_log(open(log, "rb").read())[0] == []), str((psql("select count(*) from endpoints"), os.path.getsize(log))))
    status, body = req(svc, "POST", "/endpoints", {"host": "8.8.8.8", "port": 9}, TOKEN)
    check("3. a public host is a 201", status == 201 and body["host"] == "8.8.8.8", str((status, body)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)
    d = tmp()
    svc = start(d, pg_flags() + ["--admin-token", TOKEN, "--allow-private-hosts", "1"])
    status, body = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": 9}, TOKEN)
    check("3. with allow-private-hosts 1 a loopback host is a 201", status == 201, str((status, body)))
    stop(svc)
    psql("truncate endpoints")
    shutil.rmtree(d)

    # 4. a row by hand, and the import
    d = tmp()
    psql(f"insert into endpoints values (1, '10.1.2.3', 9, '{SECRET}')")
    svc = start(d, pg_flags())
    check("4. a table row with a private host stops the start (status 13) and the message names the setting",
          svc.exited and svc.status == 13 and "allow-private-hosts" in " ".join(svc.lines), str((svc.status, svc.lines)))
    stop(svc)
    psql("truncate endpoints")
    conf(d, ["192.168.0.5"])
    p = subprocess.run([BIN, "--dir", d, "--import-endpoints", "1", *pg_flags()], capture_output=True, text=True, timeout=10)
    check("4. --import-endpoints refuses a private host and imports nothing", p.returncode == 13 and psql("select count(*) from endpoints") == [("0",)], str((p.returncode, p.stderr)))
    shutil.rmtree(d)

    # 5. a redirect is not followed
    seen = {"first": 0, "second": 0}

    class Second(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen["second"] += 1
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *a):
            pass

    second = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Second)
    threading.Thread(target=second.serve_forever, daemon=True).start()

    class First(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen["first"] += 1
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{second.server_address[1]}/hook")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    first = http.server.ThreadingHTTPServer(("127.0.0.1", 0), First)
    threading.Thread(target=first.serve_forever, daemon=True).start()
    d = tmp()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {first.server_address[1]} {SECRET}\n")
    svc = start(d, ["--allow-private-hosts", "1", "--schedule", "100"])
    chaos.post(svc.port, json.dumps({"type": "t", "n": 1}).encode(), timeout=5.0)
    end = time.time() + 5
    while time.time() < end and seen["first"] < 2:
        time.sleep(0.05)
    time.sleep(0.4)
    stats = req(svc, "GET", "/stats")[1]
    check("5. a 302 is a failed attempt, retried like one: the first receiver is sent the event again", seen["first"] >= 2 and stats["delivered"] == 0, str((seen, stats)))
    check("5. ... and the receiver it pointed to is never called", seen["second"] == 0, str(seen))
    stop(svc)
    shutil.rmtree(d)

    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all ssrf checks passed")


main()
