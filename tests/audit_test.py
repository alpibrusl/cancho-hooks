#!/usr/bin/env python3
"""The audit log (docs/design.md section 47.1).

    HOOKS_PG=host:port:user:database python3 tests/audit_test.py build/hooks        (the endpoints table is truncated; stage `disk` needs root or `sudo -n`)

  1. what is written: a read of an event (scope read), a refused ingest (none), a path that matches nothing, a change held for the database written twice with
     the same sequence number (taken, status 0; then its outcome, 201), a refused change (bad token, 401), `X-Forwarded-For` as it was sent; what is not: an
     ingest that was taken, /healthz, /readyz, /metrics, /stats. No token, no header value and no body is in the file. The lines are on disk when the answer
     has arrived (a turn's lines are synced with its group commit).
  2. the rotation: audit-log-bytes 65536 and audit-log-files 3: after about 260 KB of lines there are audit.log, audit.log.1 and audit.log.2, none more, each
     at most the limit and a line, every line whole JSON, and the sequence numbers continue across the files
  3. `production = 1` with `audit-log = 0` ends the start with status 36; `audit-log = 0` writes no file
  4. (disk) on a full tmpfs a write that fails is counted (`/stats audit_failures`) and `/readyz` is 503; when there is room again it is 200 with no restart
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
STAGES = os.environ.get("STAGES", "what,rotation,production,disk").split(",")
ADMIN, READ, INGEST = "admin-secret-token-1", "read-secret-token-22", "ingest-secret-token-333"
check = L.Checks()


def call(port, method, path, body=None, token=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def lines_of(d, name="audit.log"):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p).read().splitlines() if x]


def svc_args(extra=()):
    return ["--admin-token", ADMIN, "--read-token", READ, "--ingest-token", INGEST, *extra]


def stage_what():
    L.apply_schema()
    L.psql("truncate endpoints")
    peer = L.Peer("ok")
    d = tempfile.mkdtemp(prefix="hooks-audit-")
    svc = L.Service(BIN, d, svc_args(L.pg_flags()))
    check("1. the service starts", svc.start(timeout=30), svc.stderr()[-300:])
    port = svc.port
    s, b = call(port, "POST", "/events", {"type": "t", "secret_body_word": "pineapple"}, INGEST)
    eid = json.loads(b)["id"] if s == 202 else 0
    s2, _ = call(port, "POST", "/events", {"type": "t"})
    s3, b3 = call(port, "GET", f"/events/{eid}", token=READ, headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    for p in ("/healthz", "/readyz", "/metrics", "/stats"):
        call(port, "GET", p, token=READ)
    s4, _ = call(port, "GET", "/no/such/route", token=READ)
    s5, b5 = call(port, "POST", "/endpoints", {"host": "127.0.0.1", "port": peer.port, "headers": {"X-Api-Key": "header-value-kiwi"}}, ADMIN)
    s6, _ = call(port, "PATCH", "/endpoints/1", {"rate": 5}, "not-a-token-at-all")
    on_disk = lines_of(d)          # read at once: the answers have come, so their lines are synced
    check(f"1. the calls answered as expected ({s} {s2} {s3} {s4} {s5} {s6})", (s, s2, s3, s4, s5, s6) == (202, 401, 200, 404, 201, 401), "")
    req = [x for x in on_disk if "path" in x]
    paths = [(x["method"], x["path"], x["status"], x["scope"]) for x in req]
    check("1. an ingest that was taken is not written; a refused one is (scope none, 401)", ("POST", "/events", 401, "none") in paths and not any(p[:3] == ("POST", "/events", 202) for p in paths), str(paths))
    check("1. a read of an event is written with the scope of the token presented (read) and X-Forwarded-For as it was sent",
          any(x["path"] == f"/events/{eid}" and x["scope"] == "read" and x["status"] == 200 and x.get("fwd") == "203.0.113.7, 10.0.0.1" for x in req), str(req))
    check("1. /healthz, /readyz, /metrics and /stats are not written", not any(x["path"] in ("/healthz", "/readyz", "/metrics", "/stats") for x in req), str(paths))
    check("1. a path that matches nothing is written", ("GET", "/no/such/route", 404, "read") in paths, str(paths))
    held = [x for x in req if x["path"] == "/endpoints" and x["method"] == "POST"]
    outcome = [x for x in on_disk if "path" not in x and held and x["seq"] == held[0]["seq"]]
    check("1. a change held for the database: taken (status 0, scope admin), then its outcome (201) under the same sequence number",
          len(held) == 1 and held[0]["status"] == 0 and held[0]["scope"] == "admin" and len(outcome) == 1 and outcome[0]["status"] == 201, f"{held} {outcome}")
    check("1. a refused change is written with scope bad and 401", ("PATCH", "/endpoints/1", 401, "bad") in paths, str(paths))
    text = open(os.path.join(d, "audit.log")).read()
    leaked = [w for w in (ADMIN, READ, INGEST, "not-a-token-at-all", "pineapple", "header-value-kiwi", "Bearer") if w in text]
    check("1. no token, no header value and no body is in the file", not leaked, str(leaked))
    seqs = [x["seq"] for x in req]
    check("1. the sequence numbers rise by one", seqs == list(range(seqs[0], seqs[0] + len(seqs))), str(seqs))
    st = json.loads(call(port, "GET", "/stats", token=READ)[1])
    check(f"1. /stats counts the lines ({st.get('audit_lines')}) and no failure", st.get("audit_lines") == len(on_disk) and st.get("audit_failures") == 0, str(st.get("audit_lines")))
    svc.kill()
    peer.close()


def stage_rotation():
    d = tempfile.mkdtemp(prefix="hooks-audit-")
    svc = L.Service(BIN, d, svc_args(["--audit-log-bytes", "65536", "--audit-log-files", "3"]))
    svc.start(timeout=30)
    s, b = call(svc.port, "POST", "/events", {"type": "t"}, INGEST)
    eid = json.loads(b)["id"]
    fwd = "198.51.100." + "9" * 200
    for _ in range(900):
        call(svc.port, "GET", f"/events/{eid}", token=READ, headers={"X-Forwarded-For": fwd})
    time.sleep(0.3)
    names = sorted(n for n in os.listdir(d) if n.startswith("audit.log"))
    sizes = {n: os.path.getsize(os.path.join(d, n)) for n in names}
    check(f"2. three files are kept and no more ({names})", names == ["audit.log", "audit.log.1", "audit.log.2"], str(names))
    check(f"2. each is at most the limit and a turn's lines ({sizes})", all(v <= 65536 + 8192 for v in sizes.values()), str(sizes))
    seqs = []
    for n in ("audit.log.2", "audit.log.1", "audit.log"):
        seqs += [x["seq"] for x in lines_of(d, n)]
    check("2. every line is whole JSON and the sequence numbers continue across the files", seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"{seqs[:3]} .. {seqs[-3:]}")
    svc.kill()


def stage_production():
    d = tempfile.mkdtemp(prefix="hooks-audit-")
    os.chmod(d, 0o700)
    p = subprocess.run([BIN, "--port", str(L.chaos.free_port()), "--dir", d, "--production", "1", "--audit-log", "0", *svc_args()], capture_output=True, text=True, timeout=30)
    check(f"3. production = 1 with audit-log = 0 ends the start with status 36 ({p.returncode})", p.returncode == 36 and "audit-log" in p.stderr, p.stderr[-300:])
    d2 = tempfile.mkdtemp(prefix="hooks-audit-")
    svc = L.Service(BIN, d2, svc_args(["--audit-log", "0"]))
    svc.start(timeout=30)
    call(svc.port, "GET", "/events/1", token=READ)
    call(svc.port, "GET", "/no/such", token=READ)
    time.sleep(0.3)
    check("3. audit-log = 0 writes no file", not os.path.exists(os.path.join(d2, "audit.log")), str(os.listdir(d2)))
    svc.kill()


def stage_disk():
    root = tempfile.mkdtemp(prefix="hooks-audit-disk-")
    d = os.path.join(root, "data")
    os.makedirs(d)
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    if subprocess.run(sudo + ["mount", "-t", "tmpfs", "-o", "size=4m,mode=1777", "tmpfs", d]).returncode != 0:
        print("skip disk: cannot mount a tmpfs")
        return
    svc = None
    try:
        svc = L.Service(BIN, d, svc_args())
        svc.start(timeout=30)
        s, b = call(svc.port, "POST", "/events", {"type": "t"}, INGEST)
        eid = json.loads(b)["id"]
        ballast = os.path.join(d, "ballast")
        free = int(subprocess.run(["df", "--output=avail", "-B1", d], capture_output=True, text=True).stdout.split()[1])
        subprocess.run(["fallocate", "-l", str(free), ballast], check=False)
        for _ in range(50):
            call(svc.port, "GET", f"/events/{eid}", token=READ, headers={"X-Forwarded-For": "x" * 250})
        time.sleep(0.5)
        st = json.loads(call(svc.port, "GET", "/stats", token=READ)[1])
        code, body = call(svc.port, "GET", "/readyz")
        check(f"4. on a full disk the failed writes are counted ({st.get('audit_failures')}) and /readyz is 503 ({body[:60]!r})", st.get("audit_failures", 0) >= 1 and code == 503, f"{st.get('audit_failures')} {code} {body}")
        os.remove(ballast)
        ok = L.wait_for(lambda: (call(svc.port, "GET", f"/events/{eid}", token=READ) and call(svc.port, "GET", "/readyz")[0] == 200), 15)
        check("4. with room again the next write succeeds and /readyz is 200, with no restart", ok, str(call(svc.port, "GET", "/readyz")))
    finally:
        if svc:
            svc.kill()
        subprocess.run(sudo + ["umount", d], capture_output=True)


def main():
    for name in STAGES:
        globals()[f"stage_{name}"]()
    return check.finish("audit")


if __name__ == "__main__":
    sys.exit(main())
