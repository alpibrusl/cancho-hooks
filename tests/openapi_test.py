#!/usr/bin/env python3
"""docs/openapi.json is the service's API, checked (docs/design.md sections 50 and 52).

    python3 tests/openapi_test.py build/hooks

The document is GENERATED from the declaration the service routes with (`src/api.cho`; `scripts/openapi.sh --check` keeps the committed file equal to
what that declaration prints). What this test adds is that it is also TRUE of the running service, and that the gate agrees with it.

  1. the document is a valid shape (OpenAPI 3.1, every $ref resolves, every operation has an id and answers) and describes exactly the
     routes of `src/authz.cho`'s table: the same methods, paths and scopes, and the security of each operation is its scope's
  2. the service answers what the document says: for a set of real requests (no token, no database), the status is one the operation
     declares and the body fits the schema declared for that status (type, required, properties, items, enum; `$ref`s followed)
  3. a path that is no route and a method that is not allowed answer in the `Error` shape the document gives
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
SPEC = json.load(open(os.path.join(ROOT, "docs", "openapi.json")))
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


# ---- 1. the document against the route table
SCOPES = {0: "open", 1: "ingest", 2: "read", 3: "admin"}


def scope_of_security(security):
    """The scope a `security` array stands for: [] open; ingest or read with the admin token as the other alternative; admin alone."""
    names = [list(alternative)[0] for alternative in security]
    if names == []:
        return "open"
    if names == ["admin"]:
        return "admin"
    if len(names) == 2 and names[1] == "admin" and names[0] in ("ingest", "read"):
        return names[0]
    return "?" + "+".join(names)


table = {}
for m in re.finditer(r"return s_(\w+)\(\);\s*//\s*(GET|POST|PATCH|DELETE|PUT) (\S+)", open(os.path.join(ROOT, "src", "authz.cho")).read()):
    scope, method, path = m.groups()
    table[(method, re.sub(r":(\w+)", r"{\1}", path))] = scope
# the table calls the second parameter `:endpoint` and the first `:id`; the document names them the same way
declared = {}
for path, ops in SPEC["paths"].items():
    for method, o in ops.items():
        if method == "parameters":
            continue  # the parameters of the path, shared by its operations (the generated document writes each path parameter once)
        declared[(method.upper(), path)] = o
check("1. the document has the same operations as the route table", set(declared) == set(table),
      f"only in the table: {sorted(set(table) - set(declared))}; only in the document: {sorted(set(declared) - set(table))}")
check("1. every operation's security is the scope the gate's table enforces for it",
      all(scope_of_security(declared[k].get("security", ["none"])) == table[k] for k in table if k in declared),
      str({k: (scope_of_security(declared[k].get("security", ["none"])), table[k]) for k in table if k in declared and scope_of_security(declared[k].get("security", ["none"])) != table[k]}))
ids = [o["operationId"] for o in declared.values()]
check("1. operation ids are unique", len(ids) == len(set(ids)))
check("1. the version is OpenAPI 3.1", SPEC["openapi"].startswith("3.1"))


def refs_ok(node):
    if isinstance(node, dict):
        if "$ref" in node:
            t = SPEC
            for part in node["$ref"][2:].split("/"):
                if part not in t:
                    return False
                t = t[part]
        return all(refs_ok(v) for v in node.values())
    if isinstance(node, list):
        return all(refs_ok(v) for v in node)
    return True


check("1. every $ref resolves", refs_ok(SPEC))
check("1. every operation declares at least one 2xx answer", all(any(c.startswith("2") for c in o["responses"]) for o in declared.values()))


# ---- the checker of a body against a schema
def resolve(s):
    while "$ref" in s:
        t = SPEC
        for part in s["$ref"][2:].split("/"):
            t = t[part]
        s = t
    return s


def resolve_response(r):
    t = SPEC
    for part in r["$ref"][2:].split("/"):
        t = t[part]
    return t


def fits(v, s, where="$"):
    s = resolve(s)
    t = s.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    py = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if types:
        ok = False
        for x in types:
            if x == "integer":
                ok = ok or (isinstance(v, int) and not isinstance(v, bool))
            elif x == "number":
                ok = ok or (isinstance(v, (int, float)) and not isinstance(v, bool))
            else:
                ok = ok or isinstance(v, py[x])
        if not ok:
            return f"{where}: {v!r} is not {types}"
    if "enum" in s and v not in s["enum"]:
        return f"{where}: {v!r} not in {s['enum']}"
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                return f"{where}: missing `{k}`"
        for k, sub in s.get("properties", {}).items():
            if k in v:
                r = fits(v[k], sub, f"{where}.{k}")
                if r:
                    return r
        ap = s.get("additionalProperties")
        if isinstance(ap, dict):
            for k, x in v.items():
                if k not in s.get("properties", {}):
                    r = fits(x, ap, f"{where}.{k}")
                    if r:
                        return r
    if isinstance(v, list) and "items" in s:
        for i, x in enumerate(v):
            r = fits(x, s["items"], f"{where}[{i}]")
            if r:
                return r
    return None


# ---- the service
def free_port():
    return chaos.free_port()   # below the ephemeral range: a port bound to 0 and released can be given to another socket before the service has it


work = tempfile.mkdtemp(prefix="openapi_test_")
port = free_port()
proc = subprocess.Popen([BIN, "--port", str(port), "--dir", work], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, r.getheader("Content-Type", ""), raw

    def conforms(label, method, path, spec_path, body=None, headers=None, expect=None):
        st, ctype, raw = call(method, path, body, headers)
        op = SPEC["paths"][spec_path][method.lower()]
        decl = op["responses"].get(str(st))
        if decl is not None and "$ref" in decl:
            # a shared response (`components.responses`): the answer of the status, whose body is then held to the schema it names
            decl = {**resolve_response(decl), **{k: v for k, v in decl.items() if k != "$ref"}}
        if decl is None:
            check(f"2. {label}: {st} is declared", False, f"declared: {sorted(op['responses'])}; body {raw[:120]!r}")
            return st, raw
        if expect is not None and st != expect:
            check(f"2. {label}: status {expect}", False, f"got {st} {raw[:120]!r}")
            return st, raw
        content = decl.get("content")
        if content:
            mt = next(iter(content))
            if mt == "application/json":
                try:
                    v = json.loads(raw)
                except ValueError:
                    check(f"2. {label}: {st} body is JSON", False, f"{raw[:120]!r}")
                    return st, raw
                bad = fits(v, content[mt]["schema"])
                check(f"2. {label}: {st} fits its schema", bad is None, bad or "")
            else:
                check(f"2. {label}: {st} is {mt}", ctype.startswith(mt), ctype)
        else:
            check(f"2. {label}: {st} is declared", True)
        return st, raw

    conforms("GET /healthz", "GET", "/healthz", "/healthz", expect=200)
    conforms("GET /readyz", "GET", "/readyz", "/readyz", expect=200)
    st, raw = conforms("POST /events", "POST", "/events", "/events", {"type": "order.paid", "n": 1}, expect=202)
    eid = json.loads(raw)["id"]
    conforms("POST /events with a key", "POST", "/events", "/events", {"type": "a"}, {"Idempotency-Key": "k1"}, expect=202)
    conforms("POST /events, the same key and event", "POST", "/events", "/events", {"type": "a"}, {"Idempotency-Key": "k1"}, expect=202)
    conforms("POST /events, the same key, another event", "POST", "/events", "/events", {"type": "b"}, {"Idempotency-Key": "k1"}, expect=422)
    conforms("POST /events, not an object", "POST", "/events", "/events", b"[1]", expect=422)
    conforms("POST /events, a bad key", "POST", "/events", "/events", {"type": "a"}, {"Idempotency-Key": "a b"}, expect=400)
    conforms("GET /events/:id", "GET", f"/events/{eid}", "/events/{id}", expect=200)
    conforms("GET /events/:id, never given", "GET", "/events/99999", "/events/{id}", expect=404)
    conforms("GET /stats", "GET", "/stats", "/stats", expect=200)
    conforms("GET /config", "GET", "/config", "/config", expect=200)
    conforms("GET /metrics", "GET", "/metrics", "/metrics", expect=200)
    conforms("GET /endpoints", "GET", "/endpoints", "/endpoints")
    conforms("GET /endpoints/:id", "GET", "/endpoints/0", "/endpoints/{id}")
    conforms("GET /endpoints/:id/dead", "GET", "/endpoints/0/dead", "/endpoints/{id}/dead")
    conforms("GET /events/:id/attempts", "GET", f"/events/{eid}/attempts", "/events/{id}/attempts")
    conforms("POST /events/:id/replay", "POST", f"/events/{eid}/replay", "/events/{id}/replay")
    conforms("POST /events/:id/replay/:endpoint", "POST", f"/events/{eid}/replay/0", "/events/{id}/replay/{endpoint}")
    conforms("DELETE /events/:id/replay/:endpoint", "DELETE", f"/events/{eid}/replay/0", "/events/{id}/replay/{endpoint}")
    conforms("DELETE /endpoints/:id/replays", "DELETE", "/endpoints/0/replays", "/endpoints/{id}/replays")
    conforms("POST /endpoints/:id/replay-dead", "POST", "/endpoints/0/replay-dead", "/endpoints/{id}/replay-dead")
    conforms("POST /endpoints/:id/enable", "POST", "/endpoints/0/enable", "/endpoints/{id}/enable")
    conforms("POST /endpoints", "POST", "/endpoints", "/endpoints", {"host": "127.0.0.1", "port": 1})
    conforms("PATCH /endpoints/:id", "PATCH", "/endpoints/0", "/endpoints/{id}", {"rotate": True})
    conforms("DELETE /endpoints/:id", "DELETE", "/endpoints/0", "/endpoints/{id}")
    conforms("GET /schedules", "GET", "/schedules", "/schedules")
    conforms("POST /schedules", "POST", "/schedules", "/schedules", {"expr": "* * * * *", "type": "tick"})
    conforms("GET /schedules/:id", "GET", "/schedules/1", "/schedules/{id}")
    conforms("PATCH /schedules/:id", "PATCH", "/schedules/1", "/schedules/{id}", {"enabled": False})
    conforms("DELETE /schedules/:id", "DELETE", "/schedules/1", "/schedules/{id}")
    conforms("DELETE /events/:id (erase)", "DELETE", f"/events/{eid}", "/events/{id}", expect=200)
    conforms("DELETE /events/:id again", "DELETE", f"/events/{eid}", "/events/{id}", expect=200)
    conforms("GET /events/:id, erased", "GET", f"/events/{eid}", "/events/{id}", expect=410)

    # ---- 3. what is no route
    err = SPEC["components"]["schemas"]["Error"]
    for label, method, path, want in [("a path that is no route", "GET", "/nothing", 404), ("a method that is not allowed", "PUT", "/events", 405)]:
        st, ctype, raw = call(method, path)
        try:
            bad = fits(json.loads(raw), err)
        except ValueError:
            bad = f"not JSON: {raw[:80]!r}"
        check(f"3. {label}: {want} in the Error shape", st == want and bad is None, f"{st} {bad}")
finally:
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(work, ignore_errors=True)

if FAILS:
    print(f"FAILED {len(FAILS)}: " + "; ".join(FAILS))
    sys.exit(1)
print("all openapi checks passed")
