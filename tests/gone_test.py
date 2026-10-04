#!/usr/bin/env python3
"""`410 Gone` disables an endpoint, and a person enables it again (docs/design.md section 22).

    python3 tests/gone_test.py build/hooks

Two endpoints, A (id 0) and B (id 1), each with a receiver this test controls.

  1. A answers 404 to event 1: it is a failure, not a disablement (still enabled, retried)
  2. A answers 410 to a retry of that event: the event is a dead letter at once, A is disabled, and `GET /endpoints` says so
     while B is not
  3. events posted after that reach B and never A (A's receiver sees nothing more, for longer than an attempt takes)
  4. the service killed and restarted: A is still disabled and still gets nothing
  5. `POST /endpoints/0/enable`: A now gets the events it missed (2 and 3), and not event 1, which is dead; a restart keeps it
     enabled and repeats nothing
  6. enabling an endpoint that is enabled is a 200; an unknown endpoint is 404, a bad id 400, a GET 405
  7. the endpoints are told apart: B (id 1) disabled while A is not, both at once, enabling one leaves the other, across a restart;
     enabling an enabled endpoint writes no record
"""
import base64
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 128
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class Receiver:
    """Records the webhook-id of every request; answers with whatever `status` is."""

    def __init__(self):
        self.status = 204
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(self.headers["webhook-id"])
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def get(svc, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def request(svc, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{svc.port}{path}", method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_event(svc, n):
    chaos.post(svc.port, json.dumps({"type": "t", "n": n}).encode(), timeout=5.0)


def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def disabled(svc):
    return {e["id"]: e["disabled"] for e in get(svc, "/endpoints")}


def main():
    a, b = Receiver(), Receiver()
    datadir = tempfile.mkdtemp(prefix="hooks-gone-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {a.port} {secret}\n1 127.0.0.1 {b.port} {secret}\n")
    svc = chaos.Service(chaos.free_port(), datadir, extra=("300,300,300", 800))
    svc.start()

    # 1. a 404 is a failure
    a.status = 404
    post_event(svc, 1)
    check("1. a 404 is retried", wait_for(lambda: len(a.seen) >= 2, 5), str(a.seen))
    check("1. ... and does not disable the endpoint", disabled(svc) == {0: False, 1: False}, str(disabled(svc)))

    # 2. a 410 on a retry
    a.status = 410
    check("2. the next attempt gets the 410", wait_for(lambda: a.status == 410 and disabled(svc).get(0), 5), str(disabled(svc)))
    st = get(svc, "/stats")
    check("2. A is disabled and B is not", disabled(svc) == {0: True, 1: False}, str(disabled(svc)))
    check("2. the event is a dead letter at once (dead 1), not after the schedule", st["dead"] == 1, str(st))
    seen_a = list(a.seen)

    # 3. later events reach B only
    for n in (2, 3):
        post_event(svc, n)
    check("3. B gets all three events", wait_for(lambda: len(set(b.seen)) == 3, 5), str(b.seen))
    time.sleep(1.2)
    check("3. A saw nothing more", a.seen == seen_a, f"{a.seen} vs {seen_a}")
    ep = {e["id"]: e for e in get(svc, "/endpoints")}
    check("3. A's cursor stays behind (event 1 dead, 2 and 3 waiting: cursor 1), B's moves", ep[0]["cursor"] == 1 and ep[1]["cursor"] == 3, str(ep))

    # 4. a restart
    svc.kill()
    svc.start()
    check("4. after a restart A is still disabled", disabled(svc) == {0: True, 1: False}, str(disabled(svc)))
    time.sleep(1.2)
    check("4. ... and still gets nothing", a.seen == seen_a, f"{a.seen} vs {seen_a}")

    # 5. enable
    a.status = 204
    code, body = request(svc, "POST", "/endpoints/0/enable")
    check("5. enabling answers 200", code == 200 and json.loads(body) == {"enabled": True}, f"{code} {body}")
    check("5. A now gets events 2 and 3", wait_for(lambda: {"evt_2", "evt_3"} <= set(a.seen[len(seen_a):]), 5), str(a.seen))
    check("5. ... and not event 1 again, which is dead", "evt_1" not in a.seen[len(seen_a):], str(a.seen[len(seen_a):]))
    check("5. A is enabled", disabled(svc) == {0: False, 1: False}, str(disabled(svc)))
    time.sleep(0.6)
    n_after = len(a.seen)
    svc.kill()
    svc.start()
    time.sleep(1.2)
    check("5. after a restart A is still enabled and nothing is repeated", disabled(svc)[0] is False and len(a.seen) == n_after,
          f"{disabled(svc)} {len(a.seen)} vs {n_after}")

    # 6. the edges
    check("6. enabling an enabled endpoint is a 200", request(svc, "POST", "/endpoints/1/enable")[0] == 200)
    check("6. an unknown endpoint is a 404", request(svc, "POST", "/endpoints/7/enable")[0] == 404)
    check("6. an id that is not a number is a 400 and one that is not there a 404", request(svc, "POST", "/endpoints/x/enable")[0] == 400 and request(svc, "POST", "/endpoints/99/enable")[0] == 404)
    check("6. a GET on the enable path is a 405", request(svc, "GET", "/endpoints/0/enable")[0] == 405)
    check("6. no secret or host in /endpoints", secret not in json.dumps(get(svc, "/endpoints")) and "127.0.0.1" not in json.dumps(get(svc, "/endpoints")))

    # 7. the endpoints are told apart: a non-zero id, both at once, and enabling one of two
    def records():
        path = os.path.join(datadir, "delivery.seg")
        return len(chaos.read_log(open(path, "rb").read())[0]) if os.path.exists(path) else 0

    before = records()
    request(svc, "POST", "/endpoints/0/enable")
    time.sleep(0.2)
    check("7. enabling an enabled endpoint writes no record", records() == before, f"{before} -> {records()}")

    b.status = 410
    post_event(svc, 4)
    check("7. B (id 1) answers 410: B is disabled and A is not", wait_for(lambda: disabled(svc) == {0: False, 1: True}, 5), str(disabled(svc)))
    a.status = 410
    post_event(svc, 5)
    check("7. then A: both are disabled", wait_for(lambda: disabled(svc) == {0: True, 1: True}, 5), str(disabled(svc)))
    # Both receivers would take events now, so an endpoint wrongly enabled stays enabled and the check below sees it.
    a.status = b.status = 204
    request(svc, "POST", "/endpoints/0/enable")
    check("7. enabling A leaves B disabled", disabled(svc) == {0: False, 1: True}, str(disabled(svc)))
    svc.kill()
    svc.start()
    check("7. ... also after a restart", disabled(svc) == {0: False, 1: True}, str(disabled(svc)))

    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all gone checks passed")
    sys.exit(1 if FAILS else 0)


main()
