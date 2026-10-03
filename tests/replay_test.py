#!/usr/bin/env python3
"""Replay: an event is sent again to one endpoint or to all, whatever happened to it there (docs/design.md section 23).

    python3 tests/replay_test.py build/hooks

Two endpoints, A (id 0) and B (id 1), each with a receiver this test controls. Schedule `150` (one retry, then a dead letter).

  1. event 1 is dead-lettered at A (it answered 500 twice) and delivered at B; A is fixed; `POST /events/1/replay/0` sends it to
     A once more with the same webhook-id, and B sees nothing
  2. `POST /events/1/replay` sends it to both
  3. a replay that fails is retried on the schedule and then dead-lettered, like any attempt
  4. the refusals: unknown event 404, unknown endpoint 404, event 0 400, endpoint 99 400, a GET 405
  5. a replay survives a restart: it is pending (failed once, waiting for its next attempt) when the service is killed, and is
     delivered once, not lost and not twice, after the restart
  6. a replay for a disabled endpoint waits, and is delivered when it is enabled
  7. at most 32 replays wait: the 33rd is a 507, and nothing was written for it
  8. an event far behind the cursor (more than a window of 1,024 events) can be replayed
  9. events posted while replays are pending are delivered as usual
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

http.server.HTTPServer.request_queue_size = 256
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class Receiver:
    def __init__(self):
        self.status = 204
        self.seen = []
        self.bodies = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.bodies.append(body)
                outer.seen.append((self.headers["webhook-id"], self.headers["webhook-signature"]))
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def ids(self):
        return [i for i, _ in self.seen]


def get(svc, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def request(svc, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{svc.port}{path}", method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
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


def start(schedule="150"):
    a, b = Receiver(), Receiver()
    datadir = tempfile.mkdtemp(prefix="hooks-replay-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {a.port} {secret}\n1 127.0.0.1 {b.port} {secret}\n")
    svc = chaos.Service(chaos.free_port(), datadir, extra=(schedule, 800))
    svc.start()
    return svc, a, b, datadir


def stop(svc, datadir):
    if svc.proc and svc.proc.poll() is None:
        svc.proc.terminate()
        svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)


def quiet(secs=0.6):
    time.sleep(secs)


def main():
    svc, a, b, d = start()

    # 1. replay to one endpoint
    a.status = 500
    post_event(svc, 1)
    check("1. event 1 is dead at A and delivered at B", wait_for(lambda: get(svc, "/stats")["dead"] == 1 and b.ids() == ["evt_1"], 5),
          f"{get(svc, '/stats')} {a.ids()} {b.ids()}")
    check("1. A saw it twice (the schedule's one retry)", a.ids() == ["evt_1", "evt_1"], str(a.ids()))
    a.status = 204
    code, body = request(svc, "POST", "/events/1/replay/0")
    check("1. replaying to A answers 202 and says where", code == 202 and body == {"event": 1, "endpoints": [0]}, f"{code} {body}")
    check("1. A gets it once more, with the same webhook-id", wait_for(lambda: a.ids() == ["evt_1"] * 3, 5), str(a.ids()))
    quiet()
    check("1. ... only once, and B sees nothing", a.ids() == ["evt_1"] * 3 and b.ids() == ["evt_1"], f"{a.ids()} {b.ids()}")
    check("1. the delivery counts as a delivery (delivered went from 1 to 2)", get(svc, "/stats")["delivered"] == 2, str(get(svc, "/stats")))
    post_event(svc, 2)
    check("1. event 2 delivered to both", wait_for(lambda: b.ids()[-1:] == ["evt_2"] and a.ids()[-1:] == ["evt_2"], 5), f"{a.ids()} {b.ids()}")
    n = len(a.seen)
    request(svc, "POST", "/events/2/replay/0")
    check("1. replaying event 2 (not the first record of the log) sends event 2's own body",
          wait_for(lambda: len(a.seen) == n + 1, 5) and a.ids()[-1] == "evt_2" and a.bodies[-1] == a.bodies[n - 1] and a.bodies[-1] != a.bodies[0],
          f"{a.ids()[n:]} {a.bodies[n:]} vs {a.bodies[n - 1:n]}")
    n_after_1 = len(a.seen)
    check("1. no replay is left waiting", get(svc, "/stats")["replays"] == 0, str(get(svc, "/stats")))

    # 2. replay to all
    na, nb = len(a.seen), len(b.seen)
    code, body = request(svc, "POST", "/events/1/replay")
    check("2. replaying to all answers 202 with both endpoints", code == 202 and body == {"event": 1, "endpoints": [0, 1]}, f"{code} {body}")
    check("2. both get it", wait_for(lambda: len(a.seen) == na + 1 and len(b.seen) == nb + 1, 5) and a.ids()[-1] == b.ids()[-1] == "evt_1", f"{a.ids()} {b.ids()}")

    # 3. a replay that fails is retried and dead-lettered
    a.status = 500
    dead0 = get(svc, "/stats")["dead"]
    n0 = len(a.seen)
    request(svc, "POST", "/events/1/replay/0")
    check("3. a failing replay is attempted twice (the schedule) and then is a dead letter",
          wait_for(lambda: get(svc, "/stats")["dead"] == dead0 + 1 and len(a.seen) == n0 + 2, 5), f"{get(svc, '/stats')} {len(a.seen) - n0}")
    quiet()
    check("3. ... and then silence, and nothing is left waiting", len(a.seen) == n0 + 2 and get(svc, "/stats")["replays"] == 0,
          f"{len(a.seen) - n0} {get(svc, '/stats')}")
    a.status = 410
    request(svc, "POST", "/events/1/replay/0")
    check("3. a replay answered 410 is a dead letter at once and disables the endpoint",
          wait_for(lambda: get(svc, "/endpoints")[0]["disabled"] and get(svc, "/stats")["replays"] == 0, 5), f"{get(svc, '/endpoints')} {get(svc, '/stats')}")
    a.status = 204
    request(svc, "POST", "/endpoints/0/enable")

    # 4. refusals
    check("4. an unknown event is a 404", request(svc, "POST", "/events/77/replay")[0] == 404)
    check("4. an unknown endpoint is a 404", request(svc, "POST", "/events/1/replay/5")[0] == 404)
    check("4. event 0 is a 400", request(svc, "POST", "/events/0/replay")[0] == 400)
    check("4. endpoint 99 is a 400", request(svc, "POST", "/events/1/replay/99")[0] == 400)
    check("4. a GET is a 405", request(svc, "GET", "/events/1/replay")[0] == 405)
    check("4. the refusals started nothing", get(svc, "/stats")["replays"] == 0)
    stop(svc, d)

    # 5. a restart with a replay pending
    svc, a, b, d = start("1500")
    post_event(svc, 1)
    check("5. event 1 delivered everywhere", wait_for(lambda: a.ids() == ["evt_1"] and b.ids() == ["evt_1"], 5), f"{a.ids()} {b.ids()}")
    a.status = 500
    request(svc, "POST", "/events/1/replay/0")
    check("5. the replay's first attempt fails", wait_for(lambda: len(a.seen) == 2, 5), str(a.ids()))
    request(svc, "POST", "/events/1/replay/0")
    check("5. asking again starts it over: an attempt at once, not after the 1.5 s it had been given",
          wait_for(lambda: len(a.seen) == 3, 0.9), str(a.ids()))
    time.sleep(0.2)
    svc.kill()
    a.status = 204
    svc.start()
    check("5. after the restart the replay is still pending", get(svc, "/stats")["replays"] == 1, str(get(svc, "/stats")))
    time.sleep(0.5)
    check("5. ... and waits for the time it was given (1.5 s), it is not tried at once", len(a.seen) == 3, str(a.ids()))
    check("5. ... and is delivered when its time comes", wait_for(lambda: len(a.seen) == 4, 6), str(a.ids()))
    quiet(2.0)
    check("5. ... once", len(a.seen) == 4 and get(svc, "/stats")["replays"] == 0, f"{len(a.seen)} {get(svc, '/stats')}")
    svc.kill()
    svc.start()
    quiet(1.0)
    check("5. a second restart repeats nothing", len(a.seen) == 4 and get(svc, "/stats")["replays"] == 0, f"{len(a.seen)} {get(svc, '/stats')}")

    # 9. new events flow while a replay is pending
    a.status = 500
    request(svc, "POST", "/events/1/replay/0")
    wait_for(lambda: len(a.seen) == 5, 5)
    post_event(svc, 2)
    check("9. an event posted while a replay waits is delivered to B at once", wait_for(lambda: "evt_2" in b.ids(), 5), str(b.ids()))
    a.status = 204
    stop(svc, d)

    # 6. a replay for a disabled endpoint
    svc, a, b, d = start()
    post_event(svc, 1)
    wait_for(lambda: a.ids() == ["evt_1"] and b.ids() == ["evt_1"], 5)
    a.status = 410
    post_event(svc, 2)
    check("6. A is disabled by a 410", wait_for(lambda: get(svc, "/endpoints")[0]["disabled"], 5), str(get(svc, "/endpoints")))
    a.status = 204
    n = len(a.seen)
    request(svc, "POST", "/events/1/replay/0")
    quiet(1.0)
    check("6. the replay waits while A is disabled", len(a.seen) == n and get(svc, "/stats")["replays"] == 1, f"{len(a.seen) - n} {get(svc, '/stats')}")
    request(svc, "POST", "/endpoints/0/enable")
    check("6. and is delivered once A is enabled", wait_for(lambda: "evt_1" in a.ids()[n:], 5), str(a.ids()[n:]))
    stop(svc, d)

    # 7. capacity
    svc, a, b, d = start()
    a.status = 410
    post_event(svc, 0)
    wait_for(lambda: get(svc, "/endpoints")[0]["disabled"], 5)
    for k in range(2, 40):
        post_event(svc, k)
    codes = [request(svc, "POST", f"/events/{k}/replay/0")[0] for k in range(1, 34)]
    check("7. 32 replays are taken and the 33rd is a 507", codes[:32] == [202] * 32 and codes[32] == 507, str(codes))
    check("7. 32 are waiting", get(svc, "/stats")["replays"] == 32, str(get(svc, "/stats")))
    code, _ = request(svc, "POST", "/events/1/replay/0")
    check("7. asking again for one that is waiting is not a new one", code == 202, str(code))
    stop(svc, d)

    # 8. far behind the cursor
    svc, a, b, d = start()
    for k in range(1, 1131):
        post_event(svc, k)
    check("8. 1,130 events delivered to both", wait_for(lambda: get(svc, "/stats")["delivered"] == 2260, 60), str(get(svc, "/stats")))
    n = len(b.seen)
    request(svc, "POST", "/events/1/replay/1")
    check("8. event 1, 1,129 behind the cursor, is replayed", wait_for(lambda: b.ids()[n:] == ["evt_1"], 5), str(b.ids()[n:]))
    stop(svc, d)

    print("FAILED: " + ", ".join(FAILS) if FAILS else "all replay checks passed")
    sys.exit(1 if FAILS else 0)


main()
