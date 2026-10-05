#!/usr/bin/env python3
"""Credentials on every route (docs/design.md section 33): every route against every kind of token, in every way the tokens can be configured.

    python3 tests/authz_test.py build/hooks

The routes are not listed here from memory: they are read from the source. Every `route.add` of src/hooks.ls must have a row in ROUTES below
(its scope, the request that reaches it and the status of the handler behind the gate) and an entry in the table of src/authz.ls with the same
method, path and scope; a route added without either fails this test, and so does a row or an entry for a route that is not there.

  1. the routes of the source, the rows below and the table of src/authz.ls are the same set
  2. for each way of configuring the tokens (all three; only the admin token; none; the production profile, whose read scope falls back to
     the admin token; ingest and read without an admin token), each route is called with no token, a wrong one, the ingest, the read and the
     admin token, and the status is the one the scope's table says (a request the gate lets through reaches its handler and has the
     handler's status: the service here has no database and no endpoint, so the handlers answer 404, 503 and so on, which no gate does)
  3. what a refusal says: 401 with `WWW-Authenticate: Bearer`, 403 naming the scope the route needs, 403 "management is off" where there is
     no admin token; a path that is no route is a 404 and a wrong method a 405, for anyone
  4. no token appears in any answer, in the settings the service reports, or on its stderr, and a token that is refused as a setting is not
     repeated in the message that refuses it
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


# ---- the rows -----------------------------------------------------------------------------------------------------------------------------
# (method, path as registered) -> (scope, the request that reaches it, the handler's status when the gate lets it through, whether it answers
# 403 "management is off" when the service has no admin token). The service of this test has no database and no endpoint, and event 1 exists.
ROUTES = {
    ("GET", "/healthz"): ("open", "GET", "/healthz", None, 200, False),
    ("POST", "/events"): ("ingest", "POST", "/events", '{"type":"t"}', 202, False),
    ("GET", "/events/:id"): ("read", "GET", "/events/1", None, 200, False),
    ("GET", "/stats"): ("read", "GET", "/stats", None, 200, False),
    ("GET", "/config"): ("read", "GET", "/config", None, 200, False),
    ("POST", "/endpoints/:id/enable"): ("admin", "POST", "/endpoints/1/enable", None, 404, False),
    ("GET", "/endpoints"): ("read", "GET", "/endpoints", None, 200, False),
    ("POST", "/events/:id/replay"): ("admin", "POST", "/events/1/replay", None, 202, False),
    ("POST", "/events/:id/replay/:endpoint"): ("admin", "POST", "/events/1/replay/1", None, 404, False),
    ("GET", "/events/:id/attempts"): ("read", "GET", "/events/1/attempts", None, 503, False),
    ("POST", "/endpoints"): ("admin", "POST", "/endpoints", '{"host":"8.8.8.8","port":9}', 503, True),
    ("GET", "/endpoints/:id"): ("read", "GET", "/endpoints/1", None, 404, False),
    ("PATCH", "/endpoints/:id"): ("admin", "PATCH", "/endpoints/1", '{"port":9}', 503, True),
    ("DELETE", "/endpoints/:id"): ("admin", "DELETE", "/endpoints/1", None, 503, True),
    ("POST", "/schedules"): ("admin", "POST", "/schedules", '{"expr":"* * * * *","type":"t"}', 503, True),
    ("GET", "/schedules"): ("admin", "GET", "/schedules", None, 503, True),
    ("GET", "/schedules/:id"): ("admin", "GET", "/schedules/1", None, 503, True),
    ("PATCH", "/schedules/:id"): ("admin", "PATCH", "/schedules/1", '{"enabled":false}', 503, True),
    ("DELETE", "/schedules/:id"): ("admin", "DELETE", "/schedules/1", None, 503, True),
    ("GET", "/endpoints/:id/dead"): ("read", "GET", "/endpoints/1/dead", None, 404, False),
    ("POST", "/endpoints/:id/replay-dead"): ("admin", "POST", "/endpoints/1/replay-dead", None, 404, False),
    ("DELETE", "/events/:id/replay/:endpoint"): ("admin", "DELETE", "/events/1/replay/1", None, 404, False),
    ("DELETE", "/endpoints/:id/replays"): ("admin", "DELETE", "/endpoints/1/replays", None, 404, False),
    ("DELETE", "/events/:id"): ("admin", "DELETE", "/events/99", None, 404, False),
    ("GET", "/readyz"): ("open", "GET", "/readyz", None, 200, False),
    ("GET", "/metrics"): ("read", "GET", "/metrics", None, 200, False),
}

IDENTITIES = ["none", "wrong", "ingest", "read", "admin"]
OK = "ok"  # the gate lets the request through
# For each way of configuring the tokens: which tokens exist, and for each scope the outcome for each identity in IDENTITIES order. An identity
# whose token is not configured sends a token nobody was given, which is a wrong token.
CONFIGS = {
    "all three tokens": dict(
        tokens=("admin", "ingest", "read"), production=False, how="flags",
        ingest=[401, 401, OK, 403, OK],
        read=[401, 401, 403, OK, OK],
        admin=[401, 401, 403, 403, OK],
    ),
    "only the admin token": dict(
        tokens=("admin",), production=False, how="flags",
        ingest=[OK, OK, OK, OK, OK],
        read=[OK, OK, OK, OK, OK],
        admin=[401, 401, 401, 401, OK],
    ),
    "no token": dict(
        tokens=(), production=False, how="flags",
        ingest=[OK, OK, OK, OK, OK],
        read=[OK, OK, OK, OK, OK],
        admin=[OK, OK, OK, OK, OK],
    ),
    "the production profile (admin and ingest tokens, read falls back to admin)": dict(
        tokens=("admin", "ingest"), production=True, how="file",
        ingest=[401, 401, OK, 401, OK],
        read=[401, 401, 403, 401, OK],
        admin=[401, 401, 403, 401, OK],
    ),
    "ingest and read tokens, no admin token": dict(
        tokens=("ingest", "read"), production=False, how="file",
        ingest=[401, 401, OK, 403, 401],
        read=[401, 401, 403, OK, 401],
        admin=[OK, OK, OK, OK, OK],
    ),
}

HOOKS_SRC = open(os.path.join(ROOT, "src", "hooks.ls")).read()
AUTHZ_SRC = open(os.path.join(ROOT, "src", "authz.ls")).read()


# ---- 1: the source, the rows and the table are the same set -------------------------------------------------------------------------------------
def source_routes():
    return {(m, p): int(i) for m, p, i in re.findall(r'route\.add\(\s*heap,\s*r,\s*"([A-Z]+)",\s*"([^"]+)",\s*(\d+)\)', HOOKS_SRC)}


def authz_table():
    return {int(i): (s[2:], m, p) for i, s, m, p in re.findall(r"if id == (\d+) \{\s*return (s_\w+)\(\);\s*// ([A-Z]+) (\S+)", AUTHZ_SRC)}


def stage_sets():
    src = source_routes()
    check("1. the source registers routes (the pattern still finds them)", len(src) >= 19, f"{len(src)} found")
    ids = list(src.values())
    check("1. route ids are unique", len(ids) == len(set(ids)), str(sorted(ids)))
    for key in sorted(src):
        check(f"1. {key[0]} {key[1]} has a row in ROUTES (add it there, and to the table in src/authz.ls)", key in ROUTES)
    for key in sorted(ROUTES):
        check(f"1. the row {key[0]} {key[1]} is a route of the source", key in src)
    table = authz_table()
    for key, rid in sorted(src.items(), key=lambda kv: kv[1]):
        entry = table.get(rid)
        check(f"1. src/authz.ls has an entry for route {rid} ({key[0]} {key[1]}) naming that route", entry is not None and entry[1:] == key, str(entry))
        if key in ROUTES and entry is not None:
            check(f"1. route {rid} ({key[0]} {key[1]}) has the scope {ROUTES[key][0]} in src/authz.ls", entry[0] == ROUTES[key][0], entry[0])
    for rid in sorted(table):
        check(f"1. the entry of src/authz.ls for route {rid} is a route of the source", rid in src.values())
    check("1. every route has an entry in the table", len(table) == len(src), f"{len(table)} entries, {len(src)} routes")


# ---- the service --------------------------------------------------------------------------------------------------------------------------
class Svc:
    def __init__(self, proc, port, lines):
        self.proc, self.port, self.lines = proc, port, lines
        self.stderr = []

    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        self.reader.join(timeout=5)


def start(d, flags):
    port = chaos.free_port()
    old = os.umask(0o077)
    try:
        proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, *flags], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    finally:
        os.umask(old)
    svc = Svc(proc, port, [])

    def pump():
        for raw in proc.stderr:
            svc.stderr.append(raw.decode(errors="replace"))

    svc.reader = threading.Thread(target=pump, daemon=True)
    svc.reader.start()
    for _ in range(100):
        if any(line.strip() == "listening" for line in svc.stderr) or proc.poll() is not None:
            break
        time.sleep(0.05)
    return svc


def only_listening_and_the_stop(err):
    """stderr is `listening`, and then (the test ends the service with SIGTERM) the two lines of the drain (docs/design.md section 34.4)."""
    lines = err.strip().splitlines()
    return bool(lines) and lines[0] == "listening" and all(
        l.startswith("hooks: stopping on SIGTERM:") or l.startswith("hooks: stopped:") for l in lines[1:]) and len(lines) <= 3


ANSWERS = []  # every (status, headers, body) any request of this test got: stage 4 looks for the tokens in them


def call(port, method, path, token=None, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = "Bearer " + token
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read().decode(errors="replace")
    hdrs = dict((k.lower(), v) for k, v in r.getheaders())
    conn.close()
    ANSWERS.append((r.status, hdrs, data))
    return r.status, hdrs, data


def tokens_for(label):
    """Three tokens that look like nothing else, and one nobody is given."""
    return {
        "admin": f"adm-{label}-9f3a7c1e5b",
        "ingest": f"ing-{label}-4d82b6a0c7",
        "read": f"rea-{label}-e15f90d3a8",
        "wrong": f"unissued-{label}-0000",
    }


def settings_for(cfg, toks, d):
    pairs = [(f"{name}-token", toks[name]) for name in cfg["tokens"]]
    if cfg["production"]:
        pairs.append(("production", "1"))
    if cfg["how"] == "flags":
        flags = []
        for k, v in pairs:
            flags += [f"--{k}", v]
        return flags
    path = os.path.join(d + ".conf")
    with open(path, "w") as f:
        f.write("# the settings of this test\n" + "".join(f"{k} = {v}\n" for k, v in pairs))
    return ["--config", path]


def identity_token(ident, cfg, toks):
    if ident == "none":
        return None
    if ident == "wrong":
        return toks["wrong"]
    return toks[ident] if ident in cfg["tokens"] else toks["wrong"] + "-" + ident


def expect_for(cfg, route, outcome):
    scope, _m, _p, _b, handler, off = route
    if scope == "open":
        return handler
    if outcome == OK:
        return 403 if off and "admin" not in cfg["tokens"] else handler
    return outcome


def stage_matrix():
    for name, cfg in CONFIGS.items():
        label = re.sub(r"\W+", "", name)[:8]
        toks = tokens_for(label)
        d = tempfile.mkdtemp(prefix="authz-")
        os.chmod(d, 0o700)
        flags = settings_for(cfg, toks, d)
        svc = start(d, flags)
        try:
            check(f"2. [{name}] starts", svc.proc.poll() is None, "".join(svc.stderr))
            if svc.proc.poll() is not None:
                continue
            strongest = "admin" if "admin" in cfg["tokens"] else ("ingest" if "ingest" in cfg["tokens"] else None)
            # event 1 for the routes that read it; the strongest token the service knows posts it
            st, _, _ = call(svc.port, "POST", "/events", toks[strongest] if strongest else None, '{"type":"t"}')
            check(f"2. [{name}] event 1 is stored", st == 202, str(st))
            bad = []
            cells = 0
            for key, row in sorted(ROUTES.items()):
                scope, method, path, body, _handler, _off = row
                for i, ident in enumerate(IDENTITIES):
                    outcome = OK if scope == "open" else cfg[scope][i]
                    want = expect_for(cfg, row, outcome)
                    got, hdrs, text = call(svc.port, method, path, identity_token(ident, cfg, toks), body)
                    cells += 1
                    if got != want:
                        bad.append(f"{method} {path} as {ident}: {got}, expected {want} ({text[:80]})")
                        continue
                    # what a refusal says
                    if got == 401 and not (hdrs.get("www-authenticate") == "Bearer" and "bearer token is required" in text):
                        bad.append(f"{method} {path} as {ident}: a 401 without WWW-Authenticate or its reason: {hdrs} {text}")
                    if got == 403 and outcome != OK and f"needs the {scope} token" not in text:
                        bad.append(f"{method} {path} as {ident}: a 403 that does not name the scope: {text}")
                    if got == 403 and outcome == OK and "management is off" not in text:
                        bad.append(f"{method} {path} as {ident}: 403 without 'management is off': {text}")
            check(f"2. [{name}] {len(ROUTES)} routes x {len(IDENTITIES)} identities = {cells} requests, each with its status", not bad, "; ".join(bad[:6]) + f" ... ({len(bad)} wrong)")
            # 3. a path that is no route and a wrong method are for anyone
            for ident in IDENTITIES:
                tok = identity_token(ident, cfg, toks)
                st, _, _ = call(svc.port, "GET", "/no/such/route", tok)
                check(f"3. [{name}] an unknown path is a 404 for {ident}", st == 404, str(st))
                st, hdrs, _ = call(svc.port, "PUT", "/events", tok)
                check(f"3. [{name}] a wrong method is a 405 for {ident}", st == 405 and "POST" in hdrs.get("allow", ""), f"{st} {hdrs}")
            st, _, text = call(svc.port, "GET", "/config", toks["admin"] if "admin" in cfg["tokens"] else None)
            if st == 200:
                check(f"3. [{name}] /config says whether the production profile is on", json.loads(text).get("production") == (1 if cfg["production"] else 0), text)
            # a token with extra bytes, a missing scheme, two Authorization headers
            if "read" in cfg["tokens"] and "admin" in cfg["tokens"]:
                for what, headers in [
                    ("the scheme in other case", {"Authorization": "bEaReR " + toks["read"]}),
                ]:
                    st, _, _ = call(svc.port, "GET", "/stats", None, None, headers)
                    check(f"3. [{name}] {what}: accepted", st == 200, str(st))
                for what, headers in [
                    ("no scheme", {"Authorization": toks["read"]}),
                    ("the token with a byte more", {"Authorization": "Bearer " + toks["read"] + "x"}),
                    ("the token with a byte less", {"Authorization": "Bearer " + toks["read"][:-1]}),
                    ("another scheme", {"Authorization": "Basic " + toks["read"]}),
                ]:
                    st, _, _ = call(svc.port, "GET", "/stats", None, None, headers)
                    check(f"3. [{name}] {what}: refused", st == 401, str(st))
                conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
                conn.putrequest("GET", "/stats")
                conn.putheader("Authorization", "Bearer " + toks["read"])
                conn.putheader("Authorization", "Bearer " + toks["admin"])
                conn.endheaders()
                r = conn.getresponse()
                r.read()
                check(f"3. [{name}] two Authorization headers: refused", r.status == 401, str(r.status))
                conn.close()
            stage_leaks_for(name, svc, cfg, toks)
        finally:
            svc.stop()
            shutil.rmtree(d, ignore_errors=True)
            try:
                os.remove(d + ".conf")
            except OSError:
                pass
        err = "".join(svc.stderr)
        check(f"4. [{name}] stderr after the whole matrix is `listening` and nothing else, and holds no token",
              only_listening_and_the_stop(err) and not any(t in err for t in toks.values()), err[:200])


# ---- 4: nothing leaks ---------------------------------------------------------------------------------------------------------------------
SECRETS = []


def stage_leaks_for(name, svc, cfg, toks):
    """Called while the service is up: ask for everything a reader can ask for, as the strongest identity there is."""
    secret_values = [toks[t] for t in ("admin", "ingest", "read") if t in cfg["tokens"]]
    SECRETS.extend(secret_values)
    top = toks["admin"] if "admin" in cfg["tokens"] else None
    for method, path in [("GET", "/config"), ("GET", "/stats"), ("GET", "/endpoints"), ("GET", "/events/1"), ("GET", "/events/9"), ("GET", "/events/x"),
                         ("GET", "/endpoints/1"), ("GET", "/events/1/attempts"), ("GET", "/healthz"), ("GET", "/schedules")]:
        call(svc.port, method, path, top)
    for secret in secret_values:
        for ident_status, hdrs, text in ANSWERS:
            if secret in text or any(secret in v for v in hdrs.values()):
                check(f"4. [{name}] a token appears in an answer", False, text[:100])
                return
    check(f"4. [{name}] none of {len(secret_values)} tokens is in any of {len(ANSWERS)} answers so far", True)


def run_refusing(cmd):
    """Run a command that must refuse to start. If it does not (it listens), it is killed after a few seconds and the answer is a failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, -9, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                           "the service did not refuse: it started and kept running")


def stage_stderr_and_refusals():
    """The service's stderr, with every token the matrix used, and the message that refuses a token."""
    # a service that ran with all three tokens, asked a few things, and stopped
    toks = tokens_for("stderr")
    d = tempfile.mkdtemp(prefix="authz-")
    os.chmod(d, 0o700)
    svc = start(d, ["--admin-token", toks["admin"], "--ingest-token", toks["ingest"], "--read-token", toks["read"]])
    try:
        for ident in IDENTITIES:
            for key, row in ROUTES.items():
                call(svc.port, row[1], row[2], identity_token(ident, CONFIGS["all three tokens"], toks), row[3])
        call(svc.port, "GET", "/stats", toks["wrong"])
    finally:
        svc.stop()
        shutil.rmtree(d, ignore_errors=True)
    err = "".join(svc.stderr)
    check("4. stderr of a service that was asked everything: only `listening`, and the drain's two lines at the stop", only_listening_and_the_stop(err), err[:200])
    for secret in (toks["admin"], toks["ingest"], toks["read"], toks["wrong"]):
        check("4. no token is on stderr", secret not in err)
    # a setting that is refused: the message names the argument and must not repeat a token
    for flag in ("admin-token", "ingest-token", "read-token", "pg-password"):
        for value in ("short", "has a space inside", "", "del\x7fdel1234", "caf\u00e9-caf\u00e9-1"):
            if flag == "pg-password" and value != "":
                continue
            d = tempfile.mkdtemp(prefix="authz-")
            p = run_refusing([BIN, "--port", "1", "--dir", d, f"--{flag}={value}"])
            shutil.rmtree(d, ignore_errors=True)
            shown = f"--{flag}"
            check(f"4. --{flag}={value!r} is refused (exit 2) naming the flag", p.returncode == 2 and f"`{shown}=<hidden>` has a value" in p.stderr, f"{p.returncode} {p.stderr}")
            check(f"4. --{flag}={value!r}: the refusal does not repeat the value", value == "" or value not in p.stderr, p.stderr)
        # the two-argument form names only the flag
        d = tempfile.mkdtemp(prefix="authz-")
        p = run_refusing([BIN, "--port", "1", "--dir", d, f"--{flag}", "tooshrt" if flag != "pg-password" else ""])
        shutil.rmtree(d, ignore_errors=True)
        check(f"4. --{flag} short (two arguments) is refused without the value", p.returncode == 2 and "tooshrt" not in p.stderr, p.stderr)
    # a settings file: a refused token is a line number, not the line
    d = tempfile.mkdtemp(prefix="authz-")
    conf = d + ".conf"
    with open(conf, "w") as f:
        f.write("port = 1\ndir = /tmp\nread-token = sh0rt\n")
    p = run_refusing([BIN, "--config", conf])
    os.remove(conf)
    shutil.rmtree(d, ignore_errors=True)
    check("4. a refused read-token in a settings file is a line number", p.returncode == 2 and "line 3" in p.stderr and "sh0rt" not in p.stderr, p.stderr)


def main():
    stage_sets()
    stage_matrix()
    stage_stderr_and_refusals()
    leaked = [(secret, status) for secret in SECRETS for status, hdrs, text in ANSWERS if secret in text or any(secret in v for v in hdrs.values())]
    check(f"4. in the end: none of {len(SECRETS)} tokens is in any of {len(ANSWERS)} answers (bodies and headers) of the whole run", not leaked, str(leaked[:3]))
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:10]))
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
