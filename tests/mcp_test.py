#!/usr/bin/env python3
"""hooks-mcp, the MCP server for agents (docs/design.md section 51, docs/agents.md), against real services and against scripted ones.

    python3 tests/mcp_test.py build/hooks            (build/hooks-mcp beside it, or HOOKS_MCP=path)
    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/mcp_test.py build/hooks      (also stage 7, which needs a database and empties its `endpoints` and `attempts`)

  1. the protocol: the handshake (every version it knows, one it does not), `ping`, a notification gets no reply, ids of every type echoed exactly (integers, floats, strings with
     escapes), every error (-32700, -32600, -32601, -32602), batches, a reply from the client, blank lines and CRLF, a line at the limit and one past it, a message split across
     reads and several in one, the end of input with and without a last newline (exit 0), invalid UTF-8, nesting 100,000 deep, and 600 mutated messages: whatever is sent, the
     answer is nothing or a JSON-RPC line, never anything else on standard output, and the process stays up
  2. `tools/list`: read-only by default (six tools, no write tool even named), ten with `--allow-write`; every schema is well formed and closed; a write tool called without the flag is
     refused and nothing reaches the service
  3. the requests, read off a scripted service: for every tool the method, the path, the query, the headers and the body, byte for byte, with and without a token; nothing is sent for an
     argument that is not valid (about 90 of them: wrong types, out of range, injection into the path and into headers, nesting, unknown names), every one a -32602
  4. the answers, from a scripted service: Content-Length, chunked (split anywhere), until close, HTTP/1.0, truncated, too large, not HTTP, reset, closed, a redirect, a body that is not UTF-8
     or has control characters, a service that never answers (the time runs out), a port that is closed; each one an error that says so, not a hang and not a crash
  5. the real service, no token: every tool against direct HTTP calls (the text is the body, byte for byte; a 404, 410 or 503 is `isError` with its status), the write tools' effects
     (event stored, idempotent, replays), and the same through a connection that is split into single bytes
  6. the real service with tokens: the admin token, the read token (writes are refused by the service: an error result), a wrong token (401), none (401), a token file with a newline
     or CRLF; the token is never on standard output or standard error, in any run of this whole test; the remote plaintext refusal; every command line mistake
  7. with a database: endpoints, dead letters (pages, order, after), attempts, replay of a dead letter, the bulk replay, cancel of a waiting replay, each against the direct call
"""
import json
import os
import queue
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import http.client

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
MCP = os.environ.get("HOOKS_MCP") or os.path.join(os.path.dirname(BIN), "hooks-mcp")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chaos  # noqa: E402
HAVE_PG = bool(os.environ.get("HOOKS_PG"))
FAILS = []
TOKENS = []          # every token this test ever wrote to a file: none may be seen on standard output or standard error
CLIENTS = []
MAX_LINE = 1048576
LONG_TOKEN = "tok-" + "a1B2c3D4e5F6" * 3


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)
        if os.environ.get("FAIL_FAST"):
            os._exit(1)
    return ok


def free_port():
    return chaos.free_port()   # below the ephemeral range: a port bound to 0 and released can be given to another socket before the service has it


# ---- the client of hooks-mcp ---------------------------------------------------------------------------------------------------------

class Mcp:
    """hooks-mcp on pipes. Everything it writes is kept: every line of standard output must be a JSON-RPC message (checked as it arrives), and standard error is kept whole."""

    def __init__(self, *args, url=None, token=None, write=False, timeout=None, extra=(), stdin=subprocess.PIPE, read_stdout=True):
        self.dir = tempfile.mkdtemp(prefix="hooks-mcp-test-")
        cmd = [MCP]
        if url:
            cmd += ["--url", url]
        if token is not None:
            path = os.path.join(self.dir, "token")
            with open(path, "wb") as f:
                f.write(token if isinstance(token, bytes) else token.encode())
            TOKENS.append(token.strip() if isinstance(token, str) else token.strip().decode())
            cmd += ["--token-file", path]
        if write:
            cmd.append("--allow-write")
        if timeout:
            cmd += ["--timeout-seconds", str(timeout)]
        cmd += list(args) + list(extra)
        self.cmd = cmd
        self.p = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.q = queue.Queue()
        self.raw = []
        self.err = bytearray()
        self.bad_lines = []
        if read_stdout:
            threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()
        CLIENTS.append(self)

    def _out(self):
        try:
            for line in iter(self.p.stdout.readline, b""):
                self.raw.append(line)
                self.q.put(line)
                self._protocol(line)
        except (ValueError, OSError):
            pass            # the test closed the pipe itself (standard output closed by the client)
        self.q.put(None)

    def _err(self):
        for chunk in iter(lambda: self.p.stderr.read(4096), b""):
            self.err += chunk

    def _protocol(self, line):
        """Standard output carries JSON-RPC 2.0 messages, one to a line, and nothing else."""
        try:
            if not line.endswith(b"\n") or b"\r" in line[:-1].replace(b"\\r", b""):
                raise ValueError("not a whole line")
            msg = json.loads(line)
            items = msg if isinstance(msg, list) else [msg]
            if not items:
                raise ValueError("empty batch")
            for m in items:
                if m.get("jsonrpc") != "2.0" or "id" not in m or ("result" in m) == ("error" in m):
                    raise ValueError("not a response")
                if "error" in m and not (isinstance(m["error"].get("code"), int) and isinstance(m["error"].get("message"), str)):
                    raise ValueError("bad error")
        except (ValueError, AttributeError) as e:
            self.bad_lines.append((line[:200], str(e)))

    def send(self, data):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.p.stdin.write(data + b"\n")
        self.p.stdin.flush()

    def write_raw(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def line(self, timeout=15.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return b"<timeout>"

    def recv(self, timeout=15.0):
        raw = self.line(timeout)
        if raw is None or raw == b"<timeout>":
            return None
        return json.loads(raw)

    def silent(self, wait=0.4):
        """Nothing is written for a while."""
        try:
            return self.q.get(timeout=wait) is None and False
        except queue.Empty:
            return True

    def rpc(self, method, params=None, id=1, timeout=15.0):
        msg = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.recv(timeout)

    def call(self, name, arguments=None, id=1, timeout=15.0, **kw):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.rpc("tools/call", params, id, timeout)

    def tool(self, name, arguments=None):
        """A result (not an error) of a tool call: (isError, text)."""
        r = self.call(name, arguments)
        assert r is not None and "result" in r, r
        res = r["result"]
        assert len(res["content"]) == 1 and res["content"][0]["type"] == "text", res
        return res["isError"], res["content"][0]["text"]

    def close(self, timeout=10.0):
        """End of input; the exit status."""
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            code = self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            code = None
        time.sleep(0.05)
        return code

    def stderr(self):
        return bytes(self.err)

    def stdout(self):
        return b"".join(self.raw)

    def initialize(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, id="init")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def done(self):
        if self.p.poll() is None:
            self.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def run_cli(*args, token=None, stdin=b"", url=None, write=False, timeout=15):
    """hooks-mcp with these arguments and this standard input, to its end: (exit status, stdout, stderr)."""
    m = Mcp(*args, url=url, token=token, write=write)
    try:
        m.p.stdin.write(stdin)
    except OSError:
        pass
    code = m.close(timeout)
    out = m.stdout()
    err = m.stderr()
    return code, out, err, m


# ---- a service that says what it is told to ------------------------------------------------------------------------------------------

HANG, RESET, CLOSE = object(), object(), object()


class Fake:
    """A TCP server that records every request (method, path, headers, body) and answers with what `respond(request)` returns: bytes, a list of bytes and delays, `HANG` (never answers), `RESET` or
    `CLOSE`."""

    def __init__(self, respond=None):
        self.requests = []
        self.respond = respond or (lambda r: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(16)
        self.port = self.s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.held = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = [tuple(x.split(": ", 1)) for x in lines[1:]]
            n = int(dict((k.lower(), v) for k, v in headers).get("content-length", 0))
            while len(rest) < n:
                d = c.recv(65536)
                if not d:
                    break
                rest += d
            req = {"method": method, "path": path, "headers": headers, "body": rest, "head": head}
            req["h"] = dict((k.lower(), v) for k, v in headers)
            with self.lock:
                self.requests.append(req)
            out = self.respond(req)
            if out is HANG:
                self.held.append(c)
                return
            if out is RESET:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.close()
                return
            if out is CLOSE:
                c.close()
                return
            for piece in ([out] if isinstance(out, bytes) else out):
                if isinstance(piece, float):
                    time.sleep(piece)
                else:
                    c.sendall(piece)
            c.close()
        except OSError:
            pass

    def last(self):
        with self.lock:
            return self.requests[-1]

    def count(self):
        with self.lock:
            return len(self.requests)

    def close(self):
        self.s.close()
        for c in self.held:
            c.close()


def http_ok(body, extra=""):
    b = body if isinstance(body, bytes) else body.encode()
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + extra.encode() + b"Content-Length: " + str(len(b)).encode() + b"\r\nConnection: close\r\n\r\n" + b


# ---- the real service ----------------------------------------------------------------------------------------------------------------

class Service:
    def __init__(self, *args, env=None):
        self.dir = tempfile.mkdtemp(prefix="hooks-mcp-svc-")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.lines = []
        self.proc = subprocess.Popen([BIN, "--port", str(self.port), "--dir", self.dir, "--allow-private-hosts", "1", *args], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     env=dict(os.environ, **(env or {})), preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        threading.Thread(target=self._read, daemon=True).start()
        end = time.time() + 20
        while "listening" not in self.lines and time.time() < end and self.proc.poll() is None:
            time.sleep(0.02)
        self.started = "listening" in self.lines

    def _read(self):
        for raw in self.proc.stderr:
            self.lines.append(raw.decode(errors="replace").strip())

    def request(self, method, path, body=None, token=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token:
            h["Authorization"] = "Bearer " + token
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out.decode("utf-8", "replace")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def expected(status, body):
    """What a tool call says of an answer: the body for a 2xx, `HTTP <status>: <body>` and an error otherwise."""
    return (False, body) if 200 <= status < 300 else (True, f"HTTP {status}: {body}")


# =============================================================================================================================================
# 1. the protocol
# =============================================================================================================================================

def stage1():
    print("== 1. the protocol", flush=True)
    dead = f"http://127.0.0.1:{free_port()}"
    m = Mcp(url=dead)
    r = m.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, id=1)
    res = r["result"]
    check("1. initialize: protocolVersion 2025-06-18, capabilities exactly {tools: {}}, serverInfo hooks-mcp with a version",
          res["protocolVersion"] == "2025-06-18" and res["capabilities"] == {"tools": {}} and res["serverInfo"]["name"] == "hooks-mcp" and res["serverInfo"]["version"], str(r))
    for asked, answered in (("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-06-18"), ("2024-10-07", "2025-06-18"), ("", "2025-06-18")):
        r = m.rpc("initialize", {"protocolVersion": asked, "capabilities": {}}, id=2)
        check(f"1. initialize asking for {asked!r} is answered {answered}", r["result"]["protocolVersion"] == answered, str(r))
    for bad in (None, [], "x", {}, {"protocolVersion": 5}, {"protocolVersion": None}):
        r = m.rpc("initialize", bad, id=3)
        check(f"1. initialize with params {bad!r} is -32602", r.get("error", {}).get("code") == -32602 and r["id"] == 3, str(r))
    m.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    m.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    m.send({"jsonrpc": "2.0", "method": "whatever/unknown", "params": [1]})
    m.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "hooks_health"}})
    m.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "hooks_nope", "arguments": 5}})
    m.send({"jsonrpc": "2.0", "method": "initialize"})
    m.send({"jsonrpc": "2.0", "method": "tools/list"})
    r = m.rpc("ping", id=42)
    check("1. notifications (known, unknown, a tools/call, a bad one) get no reply: the next line is the ping's", r == {"jsonrpc": "2.0", "id": 42, "result": {}}, str(r))
    m.send({"jsonrpc": "2.0", "method": 5})
    r = m.recv()
    check("1. a message with no id and no method is not a notification: -32600 with a null id", r["error"]["code"] == -32600 and r["id"] is None, str(r))

    # ids: the text of the id comes back as it was written
    ids = ["0", "-7", "9007199254740993", "1.50", "1e2", "-0", "123456789012345678901234567890", '"abc"', '""', '"\\u00e9\\n\\"q\\\\"', '"é \U0001f600"', '"a\\/b"']
    for raw in ids:
        m.write_raw(f'{{"jsonrpc":"2.0","id":{raw},"method":"ping"}}\n'.encode())
        line = m.line()
        check(f"1. id {raw} is echoed exactly", line == f'{{"jsonrpc":"2.0","id":{raw},"result":{{}}}}\n'.encode(), str(line))
    for raw in ("null", "{}", "[1]", "true", "false"):
        m.write_raw(f'{{"jsonrpc":"2.0","id":{raw},"method":"ping"}}\n'.encode())
        r = m.recv()
        check(f"1. id {raw} is not a string or a number: -32600 with the id null", r["error"]["code"] == -32600 and r["id"] is None, str(r))
    # invalid requests
    for text in ("42", '"x"', "true", "null", "{}", '{"jsonrpc":"2.0","id":1}', '{"jsonrpc":"2.0","id":1,"method":5}', '{"jsonrpc":"1.0","id":1,"method":"ping"}', '{"id":1,"method":"ping"}',
                 '{"jsonrpc":2,"id":1,"method":"ping"}', '{"jsonrpc":"2.0","id":1,"method":null}', '{"jsonrpc":"2.0","id":1,"method":["ping"]}'):
        m.send(text)
        r = m.recv()
        check(f"1. {text} is -32600", r is not None and r.get("error", {}).get("code") == -32600, str(r))
    r = m.rpc("no/such/method", {"a": 1}, id="zz")
    check("1. an unknown method is -32601 with its id", r["error"]["code"] == -32601 and r["id"] == "zz", str(r))
    r = m.rpc("tools/list", id=7)
    r2 = m.rpc("tools/list", {"cursor": "abc"}, id=8)
    check("1. tools/list ignores a cursor", r["result"] == r2["result"], "")
    r = m.rpc("resources/list", id=9)
    check("1. resources/list (a capability not offered) is -32601", r["error"]["code"] == -32601, str(r))
    # a reply of the client to a request the server never made
    m.send({"jsonrpc": "2.0", "id": 5, "result": {}})
    m.send({"jsonrpc": "2.0", "id": 6, "error": {"code": -1, "message": "no"}})
    r = m.rpc("ping", id=43)
    check("1. a response from the client is not answered", r["id"] == 43, str(r))
    # parse errors
    bad_texts = [b"garbage", b'{"jsonrpc":"2.0","id":1,"method":"ping"', b'{"a":1}}', b'{"jsonrpc":"2.0","id":1,"method":"ping"} x', b"\x00", b'{"a":"\xff"}', b'{"a":"\xc3"}',
                 b"[" * 100000, b"{" * 70, b'{"jsonrpc":"2.0","id":1,"method":"pi\x01ng"}', b"{'a':1}", b'{"a":01}', b"nul", b'{"a":1,}', b"\xef\xbb\xbf{}", b'{"jsonrpc":"2.0","id":1e,"method":"ping"}']
    for text in bad_texts:
        m.write_raw(text + b"\n")
        r = m.recv()
        check(f"1. {text[:40]!r} is -32700 with the id null", r is not None and r.get("error", {}).get("code") == -32700 and r["id"] is None, str(r))
    r = m.rpc("ping", id=44)
    check("1. still serving after all of those", r["id"] == 44 and "result" in r, str(r))

    # batches
    m.send([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": "s", "method": "ping"}, 5,
            {"jsonrpc": "2.0", "id": 9, "method": "nope"}])
    r = m.recv()
    check("1. a batch is answered with an array of the replies there are, in order (a notification none, a bad element -32600 with a null id)",
          isinstance(r, list) and [x["id"] for x in r] == [1, "s", None, 9] and r[2]["error"]["code"] == -32600 and r[3]["error"]["code"] == -32601, str(r))
    m.send([{"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "method": "x"}])
    r = m.rpc("ping", id=45)
    check("1. a batch of notifications is not answered at all", r["id"] == 45, str(r))
    m.send([])
    r = m.recv()
    check("1. an empty batch is -32600", r["error"]["code"] == -32600 and r["id"] is None, str(r))

    # blank lines, CRLF, several in one write, split reads
    m.write_raw(b"\n   \n\t\n")
    m.write_raw(b'{"jsonrpc":"2.0","id":50,"method":"ping"}\r\n')
    r = m.recv()
    check("1. blank lines are ignored and a CRLF line ending is read", r is not None and r["id"] == 50, str(r))
    m.write_raw(b'{"jsonrpc":"2.0","id":51,"method":"ping"}\n{"jsonrpc":"2.0","id":52,"method":"ping"}\n{"jsonrpc":"2.0","id":53,"method":"ping"}\n')
    got = [m.recv()["id"] for _ in range(3)]
    check("1. three messages in one write are three replies, in order", got == [51, 52, 53], str(got))
    text = '{"jsonrpc":"2.0","id":"café \U0001f600","method":"ping"}\n'.encode()
    for i in range(0, len(text)):
        m.write_raw(text[i:i + 1])
        if i % 9 == 0:
            time.sleep(0.01)
    r = m.recv()
    check("1. one message written a byte at a time (a multi-byte character split too) is one reply", r is not None and r["id"] == "café \U0001f600", str(r))
    for cut in (1, 10, len(text) - 2, len(text) - 1):
        m.write_raw(text[:cut])
        time.sleep(0.05)
        quiet = m.silent(0.1)
        m.write_raw(text[cut:])
        r = m.recv()
        check(f"1. a message cut at byte {cut} waits for the rest and answers once", quiet and r is not None and r["id"] == "café \U0001f600", str(r))

    # long lines
    pad = '{"jsonrpc":"2.0","id":60,"method":"ping","params":{"p":"%s"}}'
    body = pad % ("x" * (MAX_LINE - len(pad % "")))
    check("1. (the line made for the limit is exactly the limit)", len(body) == MAX_LINE, str(len(body)))
    m.write_raw(body.encode() + b"\n")
    r = m.recv(30)
    check("1. a line of exactly 1,048,576 bytes is read", r is not None and r.get("id") == 60 and "result" in r, str(r)[:200])
    body2 = pad % ("x" * (MAX_LINE - len(pad % "") + 1))
    m.write_raw(body2.encode() + b"\n")
    r = m.recv(30)
    check("1. a line of one byte more is -32600 with a null id", r is not None and r["error"]["code"] == -32600 and r["id"] is None, str(r)[:200])
    m.write_raw(b"y" * 5000000 + b"\n")
    r = m.recv(30)
    check("1. a line of 5 MB is -32600 too, and is not kept in memory", r is not None and r["error"]["code"] == -32600, str(r)[:200])
    r = m.rpc("ping", id=61)
    check("1. the next message is read after a line that was too long", r is not None and r["id"] == 61, str(r))

    # the end of input
    m.write_raw(b'{"jsonrpc":"2.0","id":70,"method":"ping"}')
    code = m.close()
    check("1. end of input after a last message without a newline: it is answered, exit 0, standard error empty", code == 0 and m.stdout().endswith(b'{"jsonrpc":"2.0","id":70,"result":{}}\n') and m.stderr() == b"", str((code, m.stderr(), m.stdout()[-80:])))
    check("1. standard output was only JSON-RPC lines for everything above", not m.bad_lines, str(m.bad_lines[:3]))
    m = Mcp(url=dead)
    check("1. end of input and nothing else: exit 0, nothing written", m.close() == 0 and m.stdout() == b"" and m.stderr() == b"", "")
    m = Mcp(url=dead)
    m.write_raw(b'{"jsonrpc":"2.0","id":71,')
    code = m.close()
    check("1. end of input in the middle of a message: -32700, exit 0", code == 0 and b'"code":-32700' in m.stdout(), str(m.stdout()))
    m = Mcp(url=dead)
    m.write_raw(b"x" * (MAX_LINE + 10))
    code = m.close()
    check("1. end of input in the middle of a line that is too long: -32600, exit 0", code == 0 and b'"code":-32600' in m.stdout(), str(m.stdout()[:100]))
    # the client goes away while the server has something to say. Nothing reads the pipe, so closing it closes it: a thread blocked in a read of it keeps the pipe open until the read
    # returns (the kernel holds the file for as long as the call lasts), and a server that wrote its fifty answers into a pipe that was still open was never told, and never ended
    m = Mcp(url=dead, read_stdout=False)
    m.p.stdout.close()
    for i in range(50):
        try:
            m.send({"jsonrpc": "2.0", "id": i, "method": "ping"})
        except OSError:
            break
    # it ends at its first write to the closed pipe; how soon is the machine's (0.5 s was not always enough with the processor shared), so it is waited for, and a server that does
    # not end is the one that spins
    end = time.time() + 15
    while m.p.poll() is None and time.time() < end:
        time.sleep(0.02)
    check("1. standard output closed by the client: the server ends (it does not spin)", m.p.poll() is not None, "")
    m.p.kill()


def fuzz_lines(rng, seeds):
    """Messages mutated every way a careless client could: cut, a byte changed or dropped, a value swapped for another type, lines glued together."""
    out = []
    swaps = ["null", "true", "[]", "{}", "-1", "0", "1.5", "1e400", '""', '"x"', '"\\ud800"', '"\\u0000"', "9" * 30, '"' + "a" * 3000 + '"', "[" * 40 + "]" * 40, '{"type":1}']
    for _ in range(600):
        base = rng.choice(seeds)
        kind = rng.randrange(6)
        b = bytearray(base.encode())
        if kind == 0:
            b = b[:rng.randrange(len(b) + 1)]
        elif kind == 1 and b:
            b[rng.randrange(len(b))] = rng.randrange(256)
        elif kind == 2 and b:
            del b[rng.randrange(len(b))]
        elif kind == 3:
            o = json.loads(base)
            def walk(x):
                if isinstance(x, dict):
                    for k in list(x):
                        if rng.random() < 0.3:
                            x[k] = json.loads(rng.choice(swaps))
                        else:
                            walk(x[k])
                elif isinstance(x, list):
                    for i in range(len(x)):
                        if rng.random() < 0.3:
                            x[i] = json.loads(rng.choice(swaps))
                        else:
                            walk(x[i])
            try:
                walk(o)
                b = bytearray(json.dumps(o).encode())
            except ValueError:
                pass
        elif kind == 4:
            b = bytearray(base.encode()) + bytearray(rng.choice(seeds).encode())
        else:
            b = bytearray(os.urandom(rng.randrange(1, 200)))
        out.append(bytes(b).replace(b"\n", b" "))
    return out


def stage1b(url):
    """The mutated messages, against a service that is there: nothing that is sent may make hooks-mcp write anything but a JSON-RPC line, or stop."""
    print("== 1b. mutated messages", flush=True)
    rng = random.Random(51)
    seeds = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}}) for i, (n, a) in enumerate([
        ("hooks_health", {}), ("hooks_stats", {}), ("hooks_get_event", {"event_id": 1}), ("hooks_list_endpoints", {"limit": 3, "offset": 0}),
        ("hooks_list_dead_letters", {"endpoint_id": 1, "limit": 5, "order": "asc", "after": 2}), ("hooks_get_attempts", {"event_id": 2}),
        ("hooks_post_event", {"type": "fz.t", "fields": {"a": [1, 2, {"b": None}], "c": "dé"}, "idempotency_key": "fz-1"}), ("hooks_replay_event", {"event_id": 1, "endpoint_id": 1}),
        ("hooks_replay_dead_letters", {"endpoint_id": 1, "limit": 4, "types": ["a", "b"], "after": 0}), ("hooks_cancel_replay", {"event_id": 1, "endpoint_id": 1})])]
    seeds += [json.dumps({"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}), json.dumps({"jsonrpc": "2.0", "id": 101, "method": "tools/list"}),
              json.dumps([{"jsonrpc": "2.0", "id": 102, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])]
    m = Mcp(url=url, write=True)
    lines = fuzz_lines(rng, seeds)
    answers = 0
    for line in lines:
        m.write_raw(line + b"\n")
    # a fence: the reply to this ping comes after the reply to every line before it
    m.send({"jsonrpc": "2.0", "id": "fence", "method": "ping"})
    seen_fence = False
    end = time.time() + 120
    while time.time() < end:
        raw = m.line(30)
        if raw is None or raw == b"<timeout>":
            break
        if b'"id":"fence"' in raw:
            seen_fence = True
            break
        answers += 1
    check("1. 600 mutated messages: every one answered or ignored, in order, and the process is up at the end", seen_fence and m.p.poll() is None, f"{answers} answers, fence {seen_fence}")
    check("1. ... and standard output held only JSON-RPC lines", not m.bad_lines, str(m.bad_lines[:3]))
    code = m.close()
    check("1. ... and the end of input is an exit 0 with nothing on standard error", code == 0 and m.stderr() == b"", str((code, m.stderr()[:200])))


# =============================================================================================================================================
# 2. tools/list
# =============================================================================================================================================

READ_TOOLS = ["hooks_health", "hooks_stats", "hooks_get_event", "hooks_list_endpoints", "hooks_list_dead_letters", "hooks_get_attempts"]
WRITE_TOOLS = ["hooks_post_event", "hooks_replay_event", "hooks_replay_dead_letters", "hooks_cancel_replay"]
NEVER = ["erase", "delete", "create_endpoint", "update_endpoint", "remove", "schedule", "config", "metrics", "secret", "enable"]


def schema_ok(t):
    s = t["inputSchema"]
    props = s.get("properties", {})
    return (s.get("type") == "object" and s.get("additionalProperties") is False and isinstance(props, dict) and all(r in props for r in s.get("required", []))
            and all("description" in p and "type" in p for p in props.values()) and len(t["description"]) > 40 and t["annotations"]["readOnlyHint"] in (True, False))


def stage2():
    print("== 2. tools/list", flush=True)
    fake = Fake()
    m = Mcp(url=fake.url)
    tools = m.rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    check("2. without --allow-write: exactly the six read-only tools", names == READ_TOOLS, str(names))
    check("2. ... none of them is called a write, and every name is in the namespace hooks_", all(n.startswith("hooks_") for n in names) and not any(w in " ".join(names) for w in WRITE_TOOLS + NEVER[:5]), str(names))
    check("2. ... each has a closed JSON schema, a description for the model and readOnlyHint true", all(schema_ok(t) and t["annotations"]["readOnlyHint"] is True for t in tools), str([t["name"] for t in tools if not schema_ok(t)]))
    text = json.dumps(tools)
    check("2. ... and the list does not so much as mention a write tool or an operation that is not exposed", not any(w in text for w in WRITE_TOOLS), "")
    mw = Mcp(url=fake.url, write=True)
    twrite = mw.rpc("tools/list")["result"]["tools"]
    wnames = [t["name"] for t in twrite]
    check("2. with --allow-write: the six and the four, in that order", wnames == READ_TOOLS + WRITE_TOOLS, str(wnames))
    check("2. ... the write tools have readOnlyHint false, destructiveHint false; every schema is closed", all(schema_ok(t) for t in twrite) and all(t["annotations"]["readOnlyHint"] is False and t["annotations"]["destructiveHint"] is False for t in twrite[6:]), "")
    check("2. ... and the tools the service has but an agent must not be handed (erase, endpoints' creation and change, schedules, /config, /metrics) are in neither list",
          not any(w in n for n in wnames for w in ("erase", "delete", "create", "update", "remove", "schedule", "config", "metrics", "enable", "secret")), str(wnames))
    for t in twrite:
        check(f"2. {t['name']}: every property is documented and a bounded integer has its minimum", all(("minimum" in p) for p in t["inputSchema"]["properties"].values() if p["type"] == "integer"), "")
    # the write tools without the flag
    for n in WRITE_TOOLS:
        r = m.call(n, {"event_id": 1, "endpoint_id": 1, "type": "x"})
        check(f"2. {n} called without --allow-write is -32602 unknown tool", r is not None and r.get("error", {}).get("code") == -32602 and "unknown tool" in r["error"]["message"], str(r))
    check("2. ... and nothing reached the service", fake.count() == 0, str(fake.count()))
    m.done()
    mw.done()
    fake.close()


# =============================================================================================================================================
# 3. the requests
# =============================================================================================================================================

def stage3():
    print("== 3. the requests", flush=True)
    for token in (None, LONG_TOKEN + "\n"):
        label = "with a token" if token else "without a token"
        fake = Fake(lambda r: http_ok('{"echo":1}'))
        m = Mcp(url=fake.url, token=token, write=True)
        tok = LONG_TOKEN

        def seen(name, args, method, path, body=None, idem=None, post=False):
            m.tool(name, args)
            r = fake.last()
            h = r["h"]
            host_ok = h.get("host") == f"127.0.0.1:{fake.port}"
            auth_ok = (h.get("authorization") == "Bearer " + tok) if token else ("authorization" not in h)
            idem_ok = (h.get("idempotency-key") == idem) if idem is not None else ("idempotency-key" not in h)
            if body is None:
                body_ok = (r["body"] == b"") and (h.get("content-length") == "0" if post else "content-length" not in h)
            else:
                body_ok = r["body"] == body if isinstance(body, bytes) else json.loads(r["body"]) == body
                body_ok = body_ok and h.get("content-length") == str(len(r["body"])) and h.get("content-type") == "application/json"
            ok = r["method"] == method and r["path"] == path and host_ok and auth_ok and idem_ok and body_ok and h.get("connection") == "close" and h.get("accept") == "application/json"
            check(f"3. {label}: {name} {json.dumps(args)} is {method} {path}" + (f" {body!r}" if body is not None else ""), ok,
                  str((r["method"], r["path"], r["headers"], r["body"])))

        seen("hooks_health", {}, "GET", "/readyz")
        seen("hooks_stats", None, "GET", "/stats")
        seen("hooks_get_event", {"event_id": 5}, "GET", "/events/5")
        seen("hooks_get_event", {"event_id": 9007199254740991}, "GET", "/events/9007199254740991")
        seen("hooks_list_endpoints", {}, "GET", "/endpoints")
        seen("hooks_list_endpoints", {"limit": 3, "offset": 2}, "GET", "/endpoints?limit=3&offset=2")
        seen("hooks_list_endpoints", {"offset": 0}, "GET", "/endpoints?offset=0")
        seen("hooks_list_endpoints", {"limit": 256}, "GET", "/endpoints?limit=256")
        seen("hooks_list_dead_letters", {"endpoint_id": 7}, "GET", "/endpoints/7/dead")
        seen("hooks_list_dead_letters", {"endpoint_id": 0}, "GET", "/endpoints/0/dead")
        seen("hooks_replay_event", {"event_id": 3, "endpoint_id": 0}, "POST", "/events/3/replay/0", None, post=True)
        seen("hooks_cancel_replay", {"event_id": 3, "endpoint_id": 0}, "DELETE", "/events/3/replay/0")
        seen("hooks_replay_dead_letters", {"endpoint_id": 0, "after": 0}, "POST", "/endpoints/0/replay-dead", {"after": 0})
        seen("hooks_list_dead_letters", {"endpoint_id": 7, "limit": 10, "order": "asc", "after": 0}, "GET", "/endpoints/7/dead?limit=10&order=asc&after=0")
        seen("hooks_list_dead_letters", {"endpoint_id": 7, "order": "desc"}, "GET", "/endpoints/7/dead?order=desc")
        seen("hooks_list_dead_letters", {"endpoint_id": 7, "after": 12}, "GET", "/endpoints/7/dead?after=12")
        seen("hooks_get_attempts", {"event_id": 9}, "GET", "/events/9/attempts")
        seen("hooks_post_event", {"type": "a.b"}, "POST", "/events", {"type": "a.b"})
        fields = {"s": "café \U0001f600 \"q\" \\ \n\t", "n": None, "t": True, "f": False, "i": -12, "big": 12345678901234567890, "x": 2.5e-3, "arr": [1, [2, [3]], {"k": []}], "o": {"a": {"b": {"c": {}}}}, "eé": 1}
        seen("hooks_post_event", {"type": "order.created", "fields": fields, "idempotency_key": "key-1_A.b~z"}, "POST", "/events", dict({"type": "order.created"}, **fields), idem="key-1_A.b~z")
        # a key with an escape and a string with every escape the parser reads, written by hand
        m.send('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hooks_post_event","arguments":{"type":"t\\u00e9\\/x","fields":{"k\\u0062":"\\ud83d\\ude00\\u0001","\\u00e9":1}}}}')
        m.recv()
        r = fake.last()
        check(f"3. {label}: escapes in a type, a key and a value are decoded and written again", json.loads(r["body"]) == {"type": "té/x", "kb": "\U0001f600\x01", "é": 1}, str(r["body"]))
        seen("hooks_replay_event", {"event_id": 3}, "POST", "/events/3/replay", None, post=True)
        seen("hooks_replay_event", {"event_id": 3, "endpoint_id": 4}, "POST", "/events/3/replay/4", None, post=True)
        seen("hooks_replay_dead_letters", {"endpoint_id": 2}, "POST", "/endpoints/2/replay-dead", None, post=True)
        seen("hooks_replay_dead_letters", {"endpoint_id": 2, "limit": 5, "types": ["a.b", "c"], "after": 9}, "POST", "/endpoints/2/replay-dead", {"limit": 5, "types": ["a.b", "c"], "after": 9})
        seen("hooks_replay_dead_letters", {"endpoint_id": 2, "types": ["x"]}, "POST", "/endpoints/2/replay-dead", {"types": ["x"]})
        seen("hooks_cancel_replay", {"event_id": 3, "endpoint_id": 4}, "DELETE", "/events/3/replay/4")
        m.done()
        fake.close()

    # nothing is sent for an argument that is not valid
    fake = Fake()
    m = Mcp(url=fake.url, token=LONG_TOKEN, write=True)
    big = 2 ** 53
    cases = []
    for t in ("hooks_get_event", "hooks_get_attempts"):
        for v in (None, "5", "5/../config", "1;x", 5.0, 1e2, True, [], {}, -1, 0, big, 99999999999999999999, "", "0x10", " 5"):
            cases.append((t, {"event_id": v}))
        cases.append((t, {}))
        cases.append((t, {"event_id": 1, "extra": 1}))
        cases.append((t, {"id": 1}))
    for v in (0, -1, 257, 1.5, "5", None, True, big):
        cases.append(("hooks_list_endpoints", {"limit": v}))
    for v in (-1, 1.5, "1", None, big, "0; DROP"):
        cases.append(("hooks_list_endpoints", {"offset": v}))
    cases.append(("hooks_list_endpoints", {"limit": 1, "ofset": 1}))
    for v in (None, -1, -2, "7", 7.5, big, "7/dead", True, [0]):
        cases.append(("hooks_list_dead_letters", {"endpoint_id": v}))
    cases.append(("hooks_list_dead_letters", {}))
    for k, vs in (("limit", (0, 1001, "5", -1, None)), ("order", ("ASC", "up", "", 1, None, "asc ", "asc&limit=1000")), ("after", (-1, "1", 1.5, None, big))):
        for v in vs:
            cases.append(("hooks_list_dead_letters", {"endpoint_id": 1, k: v}))
    for k, v in (("limit", 0), ("limit", 2049), ("after", -1), ("types", []), ("types", ["a"] * 33), ("types", "a"), ("types", [1]), ("types", [""]), ("types", ["x" * 201]), ("types", [None]), ("types", {"a": 1}), ("limit", "3")):
        cases.append(("hooks_replay_dead_letters", {"endpoint_id": 1, k: v}))
    cases.append(("hooks_replay_dead_letters", {"limit": 1}))
    cases.append(("hooks_replay_dead_letters", {"endpoint_id": 1, "sneaky": 1}))
    for v in (-1, None, "1", 1.5, big):
        cases.append(("hooks_replay_event", {"event_id": 1, "endpoint_id": v}))
    cases.append(("hooks_replay_event", {"endpoint_id": 1}))
    cases.append(("hooks_cancel_replay", {"event_id": 1}))
    cases.append(("hooks_cancel_replay", {"endpoint_id": 1}))
    cases.append(("hooks_cancel_replay", {"event_id": 1, "endpoint_id": -1}))
    deep = 1
    for _ in range(17):
        deep = [deep]
    deepo = {"a": 1}
    for _ in range(16):
        deepo = {"a": deepo}
    for args in ({}, {"type": None}, {"type": ""}, {"type": 5}, {"type": "x" * 201}, {"type": "a\u0001b"}, {"type": "a\nb"}, {"type": "a", "fields": []}, {"type": "a", "fields": "x"}, {"type": "a", "fields": None},
                 {"type": "a", "fields": {"type": "b"}}, {"type": "a", "fields": {"type": "b"}}, {"type": "a", "fields": deepo}, {"type": "a", "idempotency_key": ""}, {"type": "a", "idempotency_key": "x" * 256},
                 {"type": "a", "idempotency_key": "a b"}, {"type": "a", "idempotency_key": "a\r\nX-Evil: 1"}, {"type": "a", "idempotency_key": "café"}, {"type": "a", "idempotency_key": 5}, {"type": "a", "idempotency_key": None},
                 {"type": "a", "idempotency_key": "a\tb"}, {"type": "a", "idempotency_key": "a\u007fb"}, {"type": "a", "body": {}}, {"fields": {}}):
        cases.append(("hooks_post_event", args))
    for t in ("hooks_health", "hooks_stats"):
        cases.append((t, {"event_id": 1}))
        cases.append((t, {"": 1}))
    n_bad = 0
    n_bad_ok = 0
    for i, (t, a) in enumerate(cases):
        r = m.call(t, a, id=i)
        good = r is not None and r.get("error", {}).get("code") == -32602 and r["id"] == i and "Invalid params" in r["error"]["message"]
        n_bad += 1
        n_bad_ok += good
        if not good:
            check(f"3. {t} {json.dumps(a)[:100]} is refused with -32602", False, str(r)[:300])
    for text in ('{"endpoint_id":1,"types":["a\\u0062"]}', '{"endpoint_id":1,"types":["\\u0000"]}', '{"type":"a","fields":{"t\\u0079pe":1}}', '{"type":"a\\u0001"}', '{"type":"a","idempotency_key":"a\\u0020b"}', '{"type":"a","idempotency_key":"a\\r\\nx"}',
                 '{"event_id":1,"event_id":"x"}'):
        name = "hooks_post_event" if '"type"' in text else ("hooks_replay_dead_letters" if "types" in text else "hooks_get_event")
        m.send('{"jsonrpc":"2.0","id":"raw","method":"tools/call","params":{"name":"%s","arguments":%s}}' % (name, text))
        r = m.recv()
        good = r is not None and r.get("error", {}).get("code") == -32602
        n_bad += 1
        n_bad_ok += good
        if not good:
            check(f"3. raw arguments {text} are refused with -32602", False, str(r)[:300])
    check(f"3. {n_bad} calls with arguments that are not valid: every one is -32602 that names the argument", n_bad == n_bad_ok, f"{n_bad_ok} of {n_bad}")
    check("3. ... and not one request was made to the service", fake.count() == 0, str(fake.count()))
    # malformed calls
    for params in (None, [], "x", {}, {"name": 5}, {"name": None}, {"name": "hooks_nope"}, {"name": "HOOKS_HEALTH"}, {"name": "hooks_health ", "arguments": {}}, {"name": "hooks_health", "arguments": []},
                   {"name": "hooks_health", "arguments": None}, {"name": "hooks_health", "arguments": "x"}, {"name": "hooks_health", "other": 1}, {"name": ["hooks_health"]}):
        r = m.rpc("tools/call", params, id="p")
        check(f"3. tools/call with params {json.dumps(params)} is -32602", r is not None and r.get("error", {}).get("code") == -32602, str(r))
    check("3. ... still nothing sent", fake.count() == 0, str(fake.count()))
    check("3. the token is not in what was said to the client: standard output and standard error", LONG_TOKEN.encode() not in m.stdout() + m.stderr(), "")
    # `_meta` is allowed (MCP clients send a progress token there)
    r = m.rpc("tools/call", {"name": "hooks_health", "_meta": {"progressToken": 1}}, id="meta")
    check("3. `_meta` in the params of a call is accepted", r is not None and "result" in r, str(r))
    m.done()
    fake.close()


# =============================================================================================================================================
# 4. the answers
# =============================================================================================================================================

def chunked(body, sizes):
    out = b""
    i = 0
    for s in sizes:
        piece = body[i:i + s]
        if not piece:
            break
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
        i += s
    if i < len(body):
        rest = body[i:]
        out += f"{len(rest):x}\r\n".encode() + rest + b"\r\n"
    return out + b"0\r\n\r\n"


def stage4():
    print("== 4. the answers", flush=True)
    body = json.dumps({"id": 1, "event": {"type": "x", "s": "café “\U0001f600” \n tab\t \u0001"}})
    bb = body.encode()
    cases = []
    cases.append(("Content-Length", http_ok(body), (False, body)))
    cases.append(("Content-Length, header names in other cases", b"HTTP/1.1 200 OK\r\ncontent-LENGTH: " + str(len(bb)).encode() + b"\r\nCONNECTION: close\r\n\r\n" + bb, (False, body)))
    cases.append(("no Content-Length: until the connection closes", b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + bb, (False, body)))
    cases.append(("HTTP/1.0", b"HTTP/1.0 200 OK\r\n\r\n" + bb, (False, body)))
    cases.append(("a Content-Length shorter than what follows: the length is what counts", b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n" + bb, (False, body[:5])))
    cases.append(("chunked", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + chunked(bb, [7, 1, 20, 3]), (False, body)))
    cases.append(("chunked, header in other case", b"HTTP/1.1 200 OK\r\ntransfer-encoding: Chunked\r\n\r\n" + chunked(bb, [1000]), (False, body)))
    cases.append(("chunked, one byte to a chunk", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked(bb, [1] * len(bb)), (False, body)))
    cases.append(("an empty chunked body", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", (False, "")))
    cases.append(("202 with a body", b"HTTP/1.1 202 Accepted\r\nContent-Length: 8\r\n\r\n{\"id\":1}", (False, '{"id":1}')))
    cases.append(("204, no body", b"HTTP/1.1 204 No Content\r\n\r\n", (False, "")))
    cases.append(("404 is an error that says the status first", b"HTTP/1.1 404 Not Found\r\nContent-Length: 25\r\n\r\n{\"error\":\"no such event\"}", (True, 'HTTP 404: {"error":"no such event"}')))
    cases.append(("410", b"HTTP/1.1 410 Gone\r\nContent-Length: 2\r\n\r\n{}", (True, "HTTP 410: {}")))
    cases.append(("503", b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 2\r\n\r\n{}", (True, "HTTP 503: {}")))
    cases.append(("a redirect is an error, not followed", b"HTTP/1.1 301 Moved\r\nLocation: http://elsewhere.invalid/\r\nContent-Length: 0\r\n\r\n", (True, "HTTP 301: ")))
    cases.append(("a 500 with no body", b"HTTP/1.1 500 Oops\r\nContent-Length: 0\r\n\r\n", (True, "HTTP 500: ")))
    cases.append(("a body that is not UTF-8 comes out as valid JSON (U+FFFD)", http_ok(b'{"a":"\xff\xfe"}'), (False, '{"a":"��"}')))
    cases.append(("a body with every control character", http_ok(bytes(range(1, 32)) + b'"\\'), (False, bytes(range(1, 32)).decode() + '"\\')))
    big = b'{"d":"' + b"z" * 3000000 + b'"}'
    cases.append(("a 3 MB answer", http_ok(big), (False, big.decode())))
    for name, wire, want in cases:
        fake = Fake(lambda r, w=wire: w)
        m = Mcp(url=fake.url)
        r = m.call("hooks_stats", id=1, timeout=30)
        got = None
        if r and "result" in r:
            got = (r["result"]["isError"], r["result"]["content"][0]["text"])
        check(f"4. {name}", got == want, str(got)[:200])
        check(f"4. ... standard output is a protocol line (and the process lives)", not m.bad_lines and m.p.poll() is None, str(m.bad_lines[:1]))
        m.done()
        fake.close()

    # a split in every place of a response
    full = http_ok(body)
    for cut in sorted({1, 5, 12, 20, 40, full.index(b"\r\n\r\n") + 2, full.index(b"\r\n\r\n") + 4, full.index(b"\r\n\r\n") + 5, len(full) - 1}):
        fake = Fake(lambda r, c=cut: [full[:c], 0.05, full[c:]])
        m = Mcp(url=fake.url)
        got = m.tool("hooks_stats")
        check(f"4. an answer sent in two pieces, cut at byte {cut}", got == (False, body), str(got)[:100])
        m.done()
        fake.close()
    fake = Fake(lambda r: [bytes([b]) for b in http_ok(body)[:300]] + [http_ok(body)[300:]])
    m = Mcp(url=fake.url)
    check("4. an answer sent a byte at a time", m.tool("hooks_stats") == (False, body), "")
    m.done()
    fake.close()

    # what is wrong with an answer is an error result that says so
    bad = [
        ("an answer that is not HTTP", b"hello there\r\n\r\nworld", "did not answer with HTTP/1"),
        ("an answer with no head end", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n", "did not answer with HTTP/1"),
        ("nothing at all (closed at once)", CLOSE, "did not answer with HTTP/1"),
        ("a reset", RESET, None),
        ("a Content-Length that is not a number", b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n{}", "did not answer with HTTP/1"),
        ("two different Content-Lengths", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}", "did not answer with HTTP/1"),
        ("a Transfer-Encoding that is not chunked", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n{}", "did not answer with HTTP/1"),
        ("a status that is not a number", b"HTTP/1.1 2xx OK\r\nContent-Length: 2\r\n\r\n{}", "did not answer with HTTP/1"),
        ("a body shorter than its Content-Length", b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}", "cut short"),
        ("a chunked body that stops", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc", "cut short"),
        ("a chunk size that is not hex", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\nabc\r\n0\r\n\r\n", "cut short"),
        ("a chunk with an extension", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;x=1\r\n{}\r\n0\r\n\r\n", "cut short"),
        ("a head of 5 MB", b"HTTP/1.1 200 OK\r\nX: " + b"a" * 5000000 + b"\r\n\r\n{}", "larger than 4 MiB"),
        ("a body of 5 MB", b"HTTP/1.1 200 OK\r\n\r\n" + b"a" * 5000000, "larger than 4 MiB"),
    ]
    for name, wire, words in bad:
        fake = Fake(lambda r, w=wire: w)
        m = Mcp(url=fake.url)
        r = m.call("hooks_health", id=1, timeout=30)
        got = (r["result"]["isError"], r["result"]["content"][0]["text"]) if r and "result" in r else r
        good = isinstance(got, tuple) and got[0] is True and (words is None or words in got[1])
        check(f"4. {name}: an error result, not a hang or a crash", good, str(got)[:200])
        check("4. ... and the next call works", m.rpc("ping", id=2)["id"] == 2, "")
        m.done()
        fake.close()
    # a refused connection
    m = Mcp(url=f"http://127.0.0.1:{free_port()}")
    got = m.tool("hooks_health")
    check("4. a port nobody listens on: an error result that says the service could not be reached", got[0] is True and "could not connect" in got[1], str(got))
    m.done()
    # a service that never answers: the time runs out
    fake = Fake(lambda r: HANG)
    m = Mcp(url=fake.url, timeout=2)
    t0 = time.time()
    r = m.call("hooks_health", id=1, timeout=30)
    took = time.time() - t0
    got = (r["result"]["isError"], r["result"]["content"][0]["text"]) if r and "result" in r else r
    check("4. a service that takes the request and never answers: an error result after --timeout-seconds (2), saying so", got[0] is True and "in the time allowed" in got[1] and 1.5 < took < 6, f"{got} {took:.1f}s")
    check("4. ... and the next call is served", m.rpc("ping", id=2)["id"] == 2, "")
    m.done()
    fake.close()
    fake = Fake(lambda r: [b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{", 30.0])
    m = Mcp(url=fake.url, timeout=2)
    t0 = time.time()
    r = m.call("hooks_health", id=1, timeout=30)
    took = time.time() - t0
    check("4. a service that starts an answer and stops: the same, within the time", r and r["result"]["isError"] is True and took < 6, f"{r} {took:.1f}s")
    m.done()
    fake.close()
    fake = Fake(lambda r: [b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"] + [b"x" * 10, 0.4] * 40)
    m = Mcp(url=fake.url, timeout=3)
    t0 = time.time()
    r = m.call("hooks_health", id=1, timeout=30)
    took = time.time() - t0
    check("4. a service that drips bytes for ever: the call ends at the total time, not the idle time", r and r["result"]["isError"] is True and took < 7, f"{took:.1f}s")
    m.done()
    fake.close()


# =============================================================================================================================================
# 5. the real service, no token
# =============================================================================================================================================

def same(m, svc, tool, args, method, path, token=None, body=None, label=None):
    """The tool's result is exactly what the direct call says (the body for a 2xx, `HTTP status: body` and an error otherwise)."""
    st, text = svc.request(method, path, body, token)
    got = m.tool(tool, args)
    want = expected(st, text)
    return check(label or f"{tool} {json.dumps(args)} is the same as {method} {path} (HTTP {st})", got == want, f"{got!r} != {want!r}"), st


def stage5():
    print("== 5. the real service, no token", flush=True)
    svc = Service()
    if not svc.started:
        check("5. the service starts", False, "\n".join(svc.lines))
        return
    m = Mcp(url=svc.url, write=True)
    m.initialize()
    st, text = svc.request("GET", "/readyz")
    check("5. hooks_health is the body of GET /readyz", m.tool("hooks_health") == (False, text) and st == 200, text)
    got = json.loads(m.tool("hooks_stats")[1])
    direct = json.loads(svc.request("GET", "/stats")[1])
    stable = [k for k in direct if k not in ("turns", "endpoints_looked_at", "waits_skipped", "maintenance_ms_max")]
    check("5. hooks_stats is GET /stats (the same members, and the same counters that do not move on their own)", set(got) == set(direct) and all(got[k] == direct[k] for k in stable), str(got)[:200])
    # post through the tool, read through the tool and directly
    isb, text = m.tool("hooks_post_event", {"type": "mcp.test", "fields": {"n": 1, "name": "café", "nest": {"a": [1, 2, 3]}}})
    check("5. hooks_post_event: 202 and the id, the same body as a direct POST would give", not isb and json.loads(text) == {"id": 1}, text)
    st, direct_text = svc.request("GET", "/events/1")
    isb, text = m.tool("hooks_get_event", {"event_id": 1})
    check("5. hooks_get_event is GET /events/1, byte for byte", (isb, text) == (False, direct_text) and json.loads(text)["event"] == {"type": "mcp.test", "n": 1, "name": "café", "nest": {"a": [1, 2, 3]}}, text)
    isb, text = m.tool("hooks_post_event", {"type": "mcp.test", "idempotency_key": "k-1", "fields": {"n": 2}})
    isb2, text2 = m.tool("hooks_post_event", {"type": "mcp.test", "idempotency_key": "k-1", "fields": {"n": 2}})
    check("5. the same idempotency key twice: the same event id, one event stored", json.loads(text)["id"] == 2 and text == text2 and svc.request("GET", "/events/3")[0] == 404, f"{text} {text2}")
    isb, text = m.tool("hooks_post_event", {"type": "mcp.test", "idempotency_key": "k-1", "fields": {"n": 3}})
    st, direct_text = svc.request("POST", "/events", {"type": "mcp.test", "n": 3}, None, headers={"Idempotency-Key": "k-1"})
    check("5. the key used for another event: the service's 422, as an error result", isb is True and text == f"HTTP {st}: {direct_text}" and st == 422, text)
    same(m, svc, "hooks_get_event", {"event_id": 3}, "GET", "/events/3")
    same(m, svc, "hooks_get_event", {"event_id": 123456789}, "GET", "/events/123456789")
    same(m, svc, "hooks_list_endpoints", {}, "GET", "/endpoints")
    same(m, svc, "hooks_list_endpoints", {"limit": 5, "offset": 1}, "GET", "/endpoints?limit=5&offset=1")
    same(m, svc, "hooks_list_dead_letters", {"endpoint_id": 1}, "GET", "/endpoints/1/dead")
    same(m, svc, "hooks_get_attempts", {"event_id": 1}, "GET", "/events/1/attempts")
    # replay: no endpoint, an unknown endpoint, and the cancel of one that is not waiting
    st, text = svc.request("POST", "/events/1/replay", b"", None)
    got = m.tool("hooks_replay_event", {"event_id": 1})
    check("5. hooks_replay_event is POST /events/1/replay (202, no endpoint subscribes)", got == (False, text) and st == 202, f"{got} {st} {text}")
    same(m, svc, "hooks_replay_event", {"event_id": 1, "endpoint_id": 9}, "POST", "/events/1/replay/9", None, b"")
    same(m, svc, "hooks_replay_event", {"event_id": 99, "endpoint_id": 9}, "POST", "/events/99/replay/9", None, b"")
    same(m, svc, "hooks_replay_dead_letters", {"endpoint_id": 9}, "POST", "/endpoints/9/replay-dead", None, b"")
    same(m, svc, "hooks_replay_dead_letters", {"endpoint_id": 9, "limit": 5, "types": ["a"], "after": 1}, "POST", "/endpoints/9/replay-dead", None, {"limit": 5, "types": ["a"], "after": 1})
    same(m, svc, "hooks_cancel_replay", {"event_id": 1, "endpoint_id": 9}, "DELETE", "/events/1/replay/9")
    # the size of an event: the most the service takes for this type, and one byte more, which is refused here and not sent
    def event_of(n, ty="t"):
        return {"d": "x" * n}, len(json.dumps({"type": ty, **{"d": "x" * n}}, separators=(",", ":")))
    n = 65487 - len('{"type":"t","d":""}')
    fields, size = event_of(n)
    check("5. (the event made for the limit is exactly 65,487 bytes)", size == 65487, str(size))
    isb, text = m.tool("hooks_post_event", {"type": "t", "fields": fields})
    check("5. an event of the most the service takes (65,487 bytes with the type t) is posted: 202", isb is False and "id" in json.loads(text), text[:100])
    fields, size = event_of(n + 1)
    r = m.call("hooks_post_event", {"type": "t", "fields": fields})
    check("5. an event one byte larger is refused here with -32602 (the service would answer 413 and close on a body it has not read)", r["error"]["code"] == -32602 and "larger than the service takes" in r["error"]["message"], str(r)[:200])
    # erase through the service's own door: the tool then says 410
    # (needs an admin token: stage 6)
    # the same through a connection split into single bytes
    ms = Mcp(url=svc.url, write=True)
    line = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hooks_get_event", "arguments": {"event_id": 1}}}).encode() + b"\n"
    for i in range(len(line)):
        ms.write_raw(line[i:i + 1])
    r = ms.recv()
    check("5. a tool call written a byte at a time is answered as when written at once", r["result"]["content"][0]["text"] == svc.request("GET", "/events/1")[1], str(r)[:100])
    ms.done()
    # many in a row on one process (each a new connection)
    t0 = time.time()
    ok = True
    for i in range(300):
        r = m.call("hooks_health", id=i)
        ok = ok and r["id"] == i and r["result"]["content"][0]["text"] == '{"ready":true}'
    check("5. 300 calls in a row on one process", ok, "")
    print(f"     ({(time.time() - t0) / 300 * 1000:.2f} ms a call)", flush=True)
    r0 = rss(m.p.pid)
    for i in range(1500):
        m.call("hooks_health", id=i)
        m.call("hooks_post_event", {"type": "leak.t", "fields": {"i": i, "s": "x" * 200, "a": [1, 2, {"b": None}]}, "idempotency_key": f"leak-{i}"}, id=i)
        m.call("hooks_get_event", {"event_id": 9999999 + i}, id=i)
        m.call("hooks_get_event", {"event_id": 0}, id=i)
    # (each of those answers is read: the next call is made only when its answer has come, but the answers of the last ones may still be in the pipe: drain them)
    m.send({"jsonrpc": "2.0", "id": "end", "method": "ping"})
    while True:
        raw = m.line()
        if raw is None or b'"id":"end"' in raw:
            break
    r1 = rss(m.p.pid)
    check(f"5. 6,000 more calls (posts with fields, reads, refusals): the process grew by {r1 - r0} KB (at most 2,048) and uses {r1 // 1024} MB", r1 - r0 < 2048 and r1 < 60 * 1024, f"{r0} -> {r1}")
    check("5. the service is still ready, and standard output of hooks-mcp was only protocol lines", svc.request("GET", "/readyz")[0] == 200 and not m.bad_lines, str(m.bad_lines[:2]))
    check("5. the whole of it: exit 0, nothing on standard error", m.close() == 0 and m.stderr() == b"", str(m.stderr()))
    # the fuzz, against this service
    stage1b(svc.url)
    svc.stop()


def rss(pid):
    try:
        return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or 0)
    except ValueError:
        return 0


# =============================================================================================================================================
# 6. tokens
# =============================================================================================================================================

def stage6():
    print("== 6. tokens", flush=True)
    admin, ingest, read = "admin-tok-" + "Q" * 20, "ingest-tok-" + "W" * 20, "read-tok-" + "E" * 20
    svc = Service("--admin-token", admin, "--ingest-token", ingest, "--read-token", read)
    if not svc.started:
        check("6. the service starts with tokens", False, "\n".join(svc.lines))
        return
    st, _ = svc.request("GET", "/stats")
    check("6. (the service wants a token now)", st == 401, str(st))
    # no token
    m = Mcp(url=svc.url, write=True)
    got = m.tool("hooks_stats")
    check("6. no token: the service's 401, as an error result", got[0] is True and got[1].startswith("HTTP 401: "), str(got))
    got = m.tool("hooks_health")
    check("6. ... /readyz needs none", got == (False, '{"ready":true}'), str(got))
    m.done()
    # a wrong token
    m = Mcp(url=svc.url, token="wrong-token-" + "Z" * 20, write=True)
    got = m.tool("hooks_stats")
    check("6. a wrong token: 401 as an error result", got[0] is True and got[1].startswith("HTTP 401: "), str(got))
    check("6. ... and the wrong token is nowhere in what was written", b"wrong-token-" not in m.stdout() + m.stderr(), "")
    m.done()
    # the read token: reads, and the service refuses the writes
    for form, text in (("a newline at the end", read + "\n"), ("CRLF at the end", read + "\r\n"), ("trailing spaces", read + "  \n\n"), ("none at all", read)):
        m = Mcp(url=svc.url, token=text, write=True)
        got = m.tool("hooks_stats")
        check(f"6. the read token ({form}) opens a read tool", got[0] is False and "endpoints" in json.loads(got[1]), str(got)[:100])
        m.done()
    m = Mcp(url=svc.url, token=read, write=True)
    got = m.tool("hooks_post_event", {"type": "x"})
    check("6. the read token on a write tool: the service's 403 (or 401) as an error result", got[0] is True and (got[1].startswith("HTTP 403: ") or got[1].startswith("HTTP 401: ")), str(got))
    got = m.tool("hooks_get_event", {"event_id": 1})
    check("6. ... a read of an event that is not there is a 404", got[0] is True and got[1].startswith("HTTP 404: "), str(got))
    m.done()
    # the ingest token posts
    m = Mcp(url=svc.url, token=ingest, write=True)
    got = m.tool("hooks_post_event", {"type": "tok.test"})
    check("6. the ingest token posts an event", got[0] is False and json.loads(got[1]) == {"id": 1}, str(got))
    got = m.tool("hooks_stats")
    check("6. ... and may not read /stats", got[0] is True and got[1].startswith("HTTP 40"), str(got))
    m.done()
    # the admin token: every tool
    m = Mcp(url=svc.url, token=admin, write=True)
    got = m.tool("hooks_get_event", {"event_id": 1})
    st, text = svc.request("GET", "/events/1", token=admin)
    check("6. the admin token reads an event, as the direct call does", got == (False, text) and st == 200, str(got))
    same(m, svc, "hooks_list_endpoints", {}, "GET", "/endpoints", admin)
    same(m, svc, "hooks_replay_event", {"event_id": 1, "endpoint_id": 4}, "POST", "/events/1/replay/4", admin, b"")
    same(m, svc, "hooks_cancel_replay", {"event_id": 1, "endpoint_id": 4}, "DELETE", "/events/1/replay/4", admin)
    # erase the event with the service's own door, and the tool says gone
    st, text = svc.request("DELETE", "/events/1", token=admin)
    if st == 200:
        got = m.tool("hooks_get_event", {"event_id": 1})
        st2, text2 = svc.request("GET", "/events/1", token=admin)
        check("6. an erased event: 410 as an error result with the service's words", got == (True, f"HTTP {st2}: {text2}") and st2 == 410, str(got))
    else:
        print(f"     (this service has no DELETE /events/:id: {st}; the 410 is checked against the scripted service in stage 4)", flush=True)
    m.close()
    check("6. the admin run: nothing on standard error, no token on standard output", m.stderr() == b"" and admin.encode() not in m.stdout(), "")
    svc.stop()

    # the token reaches only the service it was given for, and only in the header: a scripted service sees it once per request
    fake = Fake(lambda r: http_ok("{}"))
    m = Mcp(url=fake.url, token=LONG_TOKEN)
    m.tool("hooks_stats")
    r = fake.last()
    check("6. the token is sent once, in the Authorization header, and in no other place of the request", r["head"].count(LONG_TOKEN.encode()) == 1 and r["h"]["authorization"] == "Bearer " + LONG_TOKEN and LONG_TOKEN.encode() not in r["head"].split(b"\r\n")[0], "")
    m.done()
    fake.close()
    # an answer that echoes the token (a hostile or buggy service) is the service's words: still not an error of this program, but check what is said of its own errors
    # command line
    tokfile_dir = tempfile.mkdtemp(prefix="hooks-mcp-tok-")

    def tfile(name, data):
        p = os.path.join(tokfile_dir, name)
        open(p, "wb").write(data)
        return p

    secret = "s3cr3t-" + "k" * 20
    TOKENS.append(secret)
    cases = [
        ("an unknown flag", ["--bogus"], b""),
        ("a flag that needs a value, without one", ["--url"], b""),
        ("a token-file flag without a value", ["--token-file"], b""),
        ("--timeout-seconds 0", ["--timeout-seconds", "0"], b""),
        ("--timeout-seconds 601", ["--timeout-seconds", "601"], b""),
        ("--timeout-seconds x", ["--timeout-seconds=x"], b""),
        ("an https URL", ["--url", "https://127.0.0.1:8443"], b""),
        ("a URL of another scheme", ["--url", "ftp://127.0.0.1"], b""),
        ("a URL with a path", ["--url", "http://127.0.0.1:8080/api"], b""),
        ("a URL with credentials", ["--url", "http://user:pw@127.0.0.1:8080"], b""),
        ("a URL with a query", ["--url", "http://127.0.0.1:8080/?a=1"], b""),
        ("a port that is 0", ["--url", "http://127.0.0.1:0"], b""),
        ("a port that is 65536", ["--url", "http://127.0.0.1:65536"], b""),
        ("a port that is not a number", ["--url", "http://127.0.0.1:x"], b""),
        ("no host", ["--url", "http://:8080"], b""),
        ("a host with a space or a CR", ["--url", "http://a b:80"], b""),
        ("a bare IPv6 address", ["--url", "http://::1:8080"], b""),
        ("a token file that is not there", ["--token-file", os.path.join(tokfile_dir, "nope")], b""),
        ("an empty token file", ["--token-file", tfile("empty", b"")], b""),
        ("a token file of only a newline", ["--token-file", tfile("nl", b"\n")], b""),
        ("a token file with a space inside", ["--token-file", tfile("sp", b"MARKER7 b\n")], b""),
        ("a token file with a space at the start", ["--token-file", tfile("lead", b" MARKER7\n")], b""),
        ("a token file with a control character inside", ["--token-file", tfile("ctl", b"MARKER7\x01def\n")], b""),
        ("a token file with a byte over 127", ["--token-file", tfile("hi", b"MARKER7\xc3\xa9\n")], b""),
        ("a token file with two lines", ["--token-file", tfile("two", secret.encode() + b"\nMARKER7\n")], b""),
        ("a token file of 5000 bytes", ["--token-file", tfile("big", b"MARKER7" + b"a" * 5000)], b""),
    ]
    for name, args, _ in cases:
        code, out, err, m = run_cli(*args)
        check(f"6. {name}: exit 2, a message on standard error, nothing on standard output", code == 2 and err and out == b"", str((code, out, err)))
        check(f"6. ... and the message does not say what the file held", secret.encode() not in err and b"MARKER7" not in err, str(err))
    # a token given on the command line by mistake is not echoed back
    code, out, err, m = run_cli("--token", secret)
    check("6. a token given as an argument by mistake: refused, and neither the flag's value nor the value is repeated", code == 2 and secret.encode() not in err and out == b"", str(err))
    code, out, err, m = run_cli(secret)
    check("6. an argument that is not a flag at all is not repeated either", code == 2 and secret.encode() not in err, str(err))
    code, out, err, m = run_cli(f"--token={secret}")
    check("6. --token=VALUE: the flag is named and the value is not repeated", code == 2 and secret.encode() not in err and b"--token" in err, str(err))
    # the secret that was in a file with two lines is not in any message
    code, out, err, m = run_cli("--token-file", tfile("two", secret.encode() + b"\nMARKER7\n"))
    check("6. a token file with two lines: refused, the content is not in the message", code == 2 and secret.encode() not in err and b"MARKER7" not in err, str(err))
    # the plaintext rule
    tf = tfile("good", (LONG_TOKEN + "\n").encode())
    for url in ("http://10.1.2.3:8080", "http://example.invalid", "http://203.0.113.5:80", "http://127.0.0.1.example.invalid:80", "http://localhost.example.invalid", "http://127.1:80", "http://0.0.0.0:80",
                "http://192.168.1.1", "http://127.0.0.1x:80", "http://128.0.0.1:80", "http://12.0.0.1:80", "http://2130706433"):
        code, out, err, m = run_cli("--url", url, "--token-file", tf)
        check(f"6. a token to {url}: refused (exit 2), the flag that allows it is named, and the token is not said", code == 2 and b"--allow-remote-plaintext" in err and LONG_TOKEN.encode() not in err and out == b"", str((code, err)))
    for url in ("http://10.1.2.3:8080", "http://example.invalid"):
        code, out, err, m = run_cli("--url", url, "--token-file", tf, "--allow-remote-plaintext")
        check(f"6. a token to {url} with --allow-remote-plaintext: it starts (exit 0 at the end of input)", code == 0 and err == b"", str((code, err)))
        code, out, err, m = run_cli("--url", url)
        check(f"6. {url} without a token: it starts (nothing secret crosses)", code == 0 and err == b"", str((code, err)))
    for url in ("http://127.0.0.1:1", "http://127.0.0.2:1", "http://127.255.255.254:1", "http://localhost:1", "http://LOCALHOST:1", "http://127.0.0.1", "http://127.0.0.1:1/"):
        code, out, err, m = run_cli("--url", url, "--token-file", tf)
        check(f"6. a token to {url}: allowed (this machine)", code == 0 and err == b"", str((code, err)))
    for url in ("http://[::1]:1", "http://[2001:db8::1]:80", "http://[::1"):
        code, out, err, m = run_cli("--url", url)
        check(f"6. {url}: refused, exit 2 (cancho's tcp_connect resolves IPv4 only: a loop that says so at the start, not an error in every call)", code == 2 and b"IPv6" in err and out == b"", str((code, err)))
    code, out, err, m = run_cli("--help")
    check("6. --help is not a flag it knows: exit 2 and the usage on standard error, nothing on standard output", code == 2 and b"usage:" in err and out == b"", str((code, err)))
    shutil.rmtree(tokfile_dir, ignore_errors=True)


# =============================================================================================================================================
# 7. with a database
# =============================================================================================================================================

def stage7():
    print("== 7. with a database", flush=True)
    sys.path.insert(0, HERE)
    import endpoint_kit as K
    K.reset_db()
    K.psql("alter sequence endpoint_ids restart with 0")      # a new database: the first endpoint is 0
    d = K.tmp()
    mode = {"code": 500}
    svc = K.start(d, schedule="40")
    bad = K.Receiver(status=lambda i, n: mode["code"])
    good = K.Receiver(status=204)

    def make(port, **extra):
        st, body = K.req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": port, **extra})
        assert st == 201, (st, body)
        return body["id"]
    a = make(bad.port)
    b = make(good.port, types=["only.this"])
    for n in range(1, 8):
        K.post_event(svc, n, "t")
    ok = K.wait_for(lambda: K.get(svc, "/stats")["dead"] == 7, 30)
    check("7. (seven events are dead letters of endpoint %d)" % a, ok, str(K.get(svc, "/stats")))
    tf = tempfile.mktemp(prefix="hooks-mcp-tok-")
    open(tf, "w").write(K.TOKEN + "\n")
    TOKENS.append(K.TOKEN)
    m = Mcp(url=f"http://127.0.0.1:{svc.port}", write=True, extra=["--token-file", tf])

    def direct(method, path, body=None):
        st, out = K.req(svc, method, path, body, raw=True)
        return st, out.decode()

    def same2(tool, args, method, path, body=None, label=None):
        st, text = direct(method, path, body)
        got = m.tool(tool, args)
        check(label or f"7. {tool} {json.dumps(args)} is {method} {path} (HTTP {st})", got == expected(st, text), f"{got!r} != {expected(st, text)!r}")
        return st, text
    st, text = same2("hooks_list_endpoints", {}, "GET", "/endpoints")
    eps = json.loads(text)
    check("7. (the first endpoint is 0, the second 1: the id 0 is a valid endpoint_id)", (a, b) == (0, 1), str((a, b)))
    check("7. the list of endpoints has both, with no host and no secret anywhere in it", [e["id"] for e in eps] == [a, b] and '"secret"' not in text and "whsec_" not in text and "127.0.0.1" not in text and '"host"' not in text, text[:200])
    same2("hooks_list_endpoints", {"limit": 1}, "GET", "/endpoints?limit=1")
    same2("hooks_list_endpoints", {"limit": 1, "offset": 1}, "GET", "/endpoints?limit=1&offset=1")
    st, text = same2("hooks_list_dead_letters", {"endpoint_id": a}, "GET", f"/endpoints/{a}/dead")
    check("7. the dead letters: seven of them", len(json.loads(text)["dead"]) == 7, text[:200])
    same2("hooks_list_dead_letters", {"endpoint_id": a, "limit": 3}, "GET", f"/endpoints/{a}/dead?limit=3")
    same2("hooks_list_dead_letters", {"endpoint_id": a, "limit": 3, "order": "asc"}, "GET", f"/endpoints/{a}/dead?limit=3&order=asc")
    page = json.loads(direct("GET", f"/endpoints/{a}/dead?limit=3&order=asc")[1])
    same2("hooks_list_dead_letters", {"endpoint_id": a, "limit": 3, "order": "asc", "after": page["next"]}, "GET", f"/endpoints/{a}/dead?limit=3&order=asc&after={page['next']}")
    same2("hooks_list_dead_letters", {"endpoint_id": b}, "GET", f"/endpoints/{b}/dead")
    same2("hooks_list_dead_letters", {"endpoint_id": 99}, "GET", "/endpoints/99/dead")
    # the pages put together are the whole list
    seen, after = [], None
    while True:
        args = {"endpoint_id": a, "limit": 2, "order": "asc"}
        if after is not None:
            args["after"] = after
        isb, text = m.tool("hooks_list_dead_letters", args)
        j = json.loads(text)
        seen += [e["event"] for e in j["dead"]]
        if j["next"] is None:
            break
        after = j["next"]
    check("7. paging with the tool (limit 2, next as after) walks the seven, once each, in order", seen == [1, 2, 3, 4, 5, 6, 7], str(seen))
    # attempts: the history rows are written a moment after
    K.wait_for(lambda: direct("GET", "/events/1/attempts")[0] == 200 and len(json.loads(direct("GET", "/events/1/attempts")[1])) >= 2, 10)
    st, text = same2("hooks_get_attempts", {"event_id": 1}, "GET", "/events/1/attempts")
    check("7. the attempts of event 1: the failed ones, with a reason", st == 200 and all(x["outcome"] != "delivered" for x in json.loads(text)[:1]) and len(json.loads(text)) >= 2, text[:200])
    same2("hooks_get_attempts", {"event_id": 123456}, "GET", "/events/123456/attempts")
    # replay one dead letter to the endpoint that is now mended
    mode["code"] = 204
    st, text = direct("POST", "/events/1/replay/%d" % b)
    isb, mtext = m.tool("hooks_replay_event", {"event_id": 1, "endpoint_id": b})
    check("7. hooks_replay_event to one endpoint is the same 202 as the direct call", (isb, json.loads(mtext)) == (False, json.loads(text)) and st == 202, f"{mtext} {text}")
    K.wait_for(lambda: good.count() >= 1, 10)
    check("7. ... and the receiver was sent the event again", good.count() >= 1 and good.events().count(1) >= 1, str(good.events()))
    # the bulk replay through the tool: everything dead at A, the receiver mended
    isb, text = m.tool("hooks_replay_dead_letters", {"endpoint_id": a, "limit": 3})
    j = json.loads(text)
    check("7. hooks_replay_dead_letters with a limit: 3 taken, 4 remaining", not isb and j["taken"] == 3 and j["remaining"] == 4, text)
    K.wait_for(lambda: bad.events().count(1) >= 2 or bad.count() >= 7 + 3, 15)
    isb, text = m.tool("hooks_replay_dead_letters", {"endpoint_id": a, "types": ["t"]})
    j = json.loads(text)
    check("7. ... and with a type filter: the rest", not isb and j["taken"] + 3 >= 4, text)
    isb, text = m.tool("hooks_replay_dead_letters", {"endpoint_id": a, "types": ["no.such.type"]})
    check("7. ... a type that none has takes none", not isb and json.loads(text)["taken"] == 0, text)
    ok = K.wait_for(lambda: K.get(svc, f"/endpoints/{a}/dead")["held"] == 0, 30)
    check("7. all seven were sent again and the list is empty", ok, str(K.get(svc, f"/endpoints/{a}/dead")))
    m.done()
    K.stop(svc)
    shutil.rmtree(d, ignore_errors=True)

    # cancel a waiting replay: a service whose retries are ten minutes apart
    d = K.tmp()
    svc = K.start(d, schedule="40,600000")
    bad = K.Receiver(status=500)
    a = make(bad.port)
    K.post_event(svc, 1, "t")
    K.wait_for(lambda: K.get(svc, "/stats")["replays"] == 0 and K.get(svc, "/stats")["delivered"] + K.get(svc, "/stats")["failed"] >= 1, 10)
    st, _ = K.req(svc, "POST", f"/events/1/replay/{a}", b"")
    # the replay's first attempt is the third request, and its own retry comes 40 ms later: wait until that has been made too (the fourth), so that
    # what is left is the ten-minute wait
    K.wait_for(lambda: K.get(svc, "/stats")["replays"] == 1 and bad.count() >= 4, 15)
    m = Mcp(url=f"http://127.0.0.1:{svc.port}", write=True, extra=["--token-file", tf])
    # An attempt that is still on the wire (the answer being read) makes the service say 409, "ask again when it has ended": a client does, and so
    # does this test (CI runs 37535955659 and 37535933162 asked in the instant of the 40 ms retry).
    for _ in range(50):
        isb, text = m.tool("hooks_cancel_replay", {"event_id": 1, "endpoint_id": a})
        if not (isb and text.startswith("HTTP 409")):
            break
        time.sleep(0.1)
    check("7. hooks_cancel_replay of a waiting replay: cancelled 1", not isb and json.loads(text) == {"event": 1, "endpoint": a, "cancelled": 1, "busy": 0}, text)
    check("7. ... the table of waiting replays is empty", K.get(svc, "/stats")["replays"] == 0, "")
    isb, text = m.tool("hooks_cancel_replay", {"event_id": 1, "endpoint_id": a})
    check("7. ... cancelling it again is the service's 404 as an error result", isb is True and text.startswith("HTTP 404: "), text)
    m.done()
    K.stop(svc)
    shutil.rmtree(d, ignore_errors=True)
    os.remove(tf)


# =============================================================================================================================================

def main():
    if not os.path.exists(MCP):
        print(f"no {MCP}: build it (cancho build)", file=sys.stderr)
        return 2
    stage1()
    stage2()
    stage3()
    stage4()
    stage5()
    stage6()
    if HAVE_PG:
        stage7()
    else:
        print("== 7. with a database: skipped (HOOKS_PG is not set)", flush=True)
    # the whole test: no token, in any run, anywhere
    leaks = [(i, t) for i, c in enumerate(CLIENTS) for t in set(TOKENS) if t.encode() in c.stdout() + c.stderr()]
    check("every run of hooks-mcp in this test (%d) wrote no token to standard output or standard error" % len(CLIENTS), not leaks, str([(i, t[:8]) for i, t in leaks]))
    check("every run's standard output was JSON-RPC lines and nothing else", not [c.cmd for c in CLIENTS if c.bad_lines], str([(c.cmd[-3:], c.bad_lines[:1]) for c in CLIENTS if c.bad_lines][:2]))
    for c in CLIENTS:
        if c.p.poll() is None:
            c.p.kill()
        shutil.rmtree(c.dir, ignore_errors=True)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS[:40]))
        return 1
    print("all hooks-mcp checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
