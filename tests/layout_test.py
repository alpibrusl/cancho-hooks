#!/usr/bin/env python3
"""The delivery state's regions do not overlap (docs/design.md section 15, and the bug section 25 records).

    python3 tests/layout_test.py build/hooks

The table of where each event of the window starts in the events log (`offs`, 1,024 entries) once overlapped the first 16 integers
of the cells, so that an event whose number was 1,008 to 1,023 modulo 1,024 lost its place in the log when the first events of the
window became final: a retry of it read the log at a wrong offset and the event was never delivered. The receiver here fails
event 1,011 once and event 2,035 once (the same place in the next window); both must be delivered later, with their own bodies,
along with every other event, once each (retries aside).
"""
import base64
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

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAIL_ONCE = {1011, 2035}
seen, failed = {}, set()
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        n = json.loads(body)["n"]
        status = 204
        if n in FAIL_ONCE and n not in failed:
            failed.add(n)
            status = 500
        else:
            seen.setdefault(n, []).append(body)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    http.server.HTTPServer.request_queue_size = 256
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="hooks-layout-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {srv.server_address[1]} {secret}\n")
    port = chaos.free_port()
    p = subprocess.Popen([BIN, "--port", str(port), "--dir", d, "--allow-private-hosts", "1", "--schedule", "300,300,300", "--deadline-ms", "800"],
                         stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    p.stderr.readline()
    total = 2300
    for n in range(1, total + 1):
        chaos.post(port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)
    end = time.time() + 30
    while time.time() < end and not all(n in seen for n in range(1, total + 1)):
        time.sleep(0.1)
    missing = [n for n in range(1, total + 1) if n not in seen]
    check("every one of 2,300 events is delivered, though 1,011 and 2,035 failed once", not missing, f"missing {missing[:10]}")
    check("the events that failed once are delivered with their own bodies",
          all(n in seen and all(json.loads(b)["n"] == n for b in seen[n]) for n in FAIL_ONCE), str({n: seen.get(n) for n in FAIL_ONCE}))
    check("both did fail first", failed == FAIL_ONCE, str(failed))
    p.terminate()
    p.wait()
    shutil.rmtree(d, ignore_errors=True)
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)
    print("all layout checks passed")


main()
