#!/usr/bin/env python3
"""Erasure of one event (docs/design.md section 47.3): `DELETE /events/:id`.

    [STAGES=erase,active,kill] python3 tests/erase_test.py build/hooks

  erase   300 events, each with a word of its own in its body; one endpoint delivers, one refuses until it is let go (so its window holds events that wait for a
          retry). Event 150 is erased: 200; `GET` and a replay are 410; a second DELETE says it was already; the word of event 150 is in no file of the data
          directory (read as bytes), the words of 149 and 151 are; every segment is read whole by lexsys-log's reader in chaos.py, event 150's body is
          `{"erased":true}` and spaces to its old length; when the second endpoint is let go it is sent every event but 150; a restart agrees; logcheck passes
  active  the newest event (in the segment being written) is erased: the segment is sealed first and the word is gone
  kill    kill -9 at each step of the rewrite (compact-kill-at 34, 35, 36): a start finishes the erasure (the word gone, 410)
"""
import collections
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
STAGES = os.environ.get("STAGES", "erase,active,kill").split(",")
ADMIN = "erase-admin-token-1"
check = L.Checks()


class Receiver:
    """Counts deliveries by event id; refuses (500) every event while `hold` is set."""

    def __init__(self, hold=False):
        self.got, self.lock, self.hold = collections.Counter(), threading.Lock(), threading.Event()
        if hold:
            self.hold.set()
        self.peer = L.Peer("ok")
        self.peer._serve = self.serve
        self.port = self.peer.port

    def serve(self, c):
        try:
            g = self.peer._read_request(c)
            if g is None:
                return
            if self.hold.is_set():
                c.sendall(b"HTTP/1.1 500 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            with self.lock:
                self.got[int(g[0][b"webhook-id"][4:])] += 1
            c.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass


def word(n):
    return f"private-word-{n:05d}-zz"


def all_bytes(d):
    out = b""
    for p in glob.glob(os.path.join(d, "*")):
        if os.path.isfile(p):
            out += open(p, "rb").read()
    return out


def segment_events(d):
    """Every event of every segment, read by chaos.py's reader (lexsys-log records): {id: body}, and whether every file read whole."""
    events, whole = {}, True
    for p in sorted(glob.glob(os.path.join(d, "events*.seg"))):
        data = open(p, "rb").read()
        recs, end = chaos.read_log(data)
        whole = whole and end == len(data)
        for ms, pairs in recs:
            body = dict(pairs).get(b"event")
            if ms > 0 and body is not None:
                events[ms] = body
    return events, whole


def start(d, a, b, extra=()):
    open(os.path.join(d, "endpoints.conf"), "w").write(f"1 127.0.0.1 {a.port} {L.secret()}\n2 127.0.0.1 {b.port} {L.secret()}\n")
    svc = L.Service(BIN, d, ["--admin-token", ADMIN, "--schedule", "1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000,1000", "--segment-bytes", "262144", *extra])
    svc.start(timeout=30)
    return svc


def admin(svc, method, path):
    s, b, _ = svc.request(method, path, b"" if method != "GET" else None, {"Authorization": f"Bearer {ADMIN}"})
    return s, b


def post_all(svc, n):
    for k in range(1, n + 1):
        svc.post_event(k, extra={"w": word(k), "pad": "y" * 900})


def stage_erase():
    d = tempfile.mkdtemp(prefix="hooks-erase-")
    a, b = Receiver(), Receiver(hold=True)
    svc = start(d, a, b)
    post_all(svc, 300)
    check("erase: the endpoint that delivers has all 300", L.wait_for(lambda: len(a.got) >= 300, 30), str(len(a.got)))
    segs = len(glob.glob(os.path.join(d, "events*.seg")))
    s, body = admin(svc, "DELETE", "/events/150")
    check(f"erase: DELETE /events/150 is 200 ({body[:60]!r}; {segs} segments)", s == 200 and json.loads(body).get("erased") is True, f"{s} {body}")
    code, gb = svc.get("/events/150")
    check("erase: GET /events/150 is 410 and says erased", code == 410 and b"erased" in gb, f"{code} {gb}")
    s2, rb = admin(svc, "POST", "/events/150/replay")
    check("erase: a replay of it is 410", s2 == 410, f"{s2} {rb}")
    s3, b3 = admin(svc, "DELETE", "/events/150")
    check("erase: a second DELETE is 200 and says it was already", s3 == 200 and json.loads(b3).get("already") is True, f"{s3} {b3}")
    raw = all_bytes(d)
    check("erase: the word of event 150 is in no file of the data directory; those of 149 and 151 are",
          word(150).encode() not in raw and word(149).encode() in raw and word(151).encode() in raw, "")
    ev, whole = segment_events(d)
    b150 = ev.get(150, b"")
    check(f"erase: every segment is read whole by chaos.py's reader; event 150's body is the marker and spaces ({len(b150)} bytes)",
          whole and b150.startswith(b'{"erased":true}') and b150[15:].strip(b" ") == b"" and json.loads(ev.get(151, b"{}")).get("w") == word(151), f"{whole} {b150[:30]!r}")
    b.hold.clear()
    every = L.wait_for(lambda: len(b.got) >= 299, 60)
    check(f"erase: the endpoint that was waiting is sent every event but 150 ({len(b.got)}; 150 {'sent' if 150 in b.got else 'not sent'})", every and 150 not in b.got, str(len(b.got)))
    check(f"erase: /stats counts it ({svc.stats().get('events_erased')})", svc.stats().get("events_erased") == 1, "")
    svc.stop(10)
    svc.start(timeout=30)
    time.sleep(3)
    check("erase: after a restart it is still 410, and neither endpoint is sent it", svc.get("/events/150")[0] == 410 and 150 not in b.got and a.got[150] == 1, "")
    svc.stop(10)
    lc = subprocess.run(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "logcheck.py"), "check", d], capture_output=True, text=True)
    check("erase: logcheck reads the directory and finds it consistent", lc.returncode == 0, lc.stdout[-300:] + lc.stderr[-300:])
    a.peer.close()
    b.peer.close()


def stage_active():
    d = tempfile.mkdtemp(prefix="hooks-erase-")
    a, b = Receiver(), Receiver()
    svc = start(d, a, b)
    post_all(svc, 20)
    L.wait_for(lambda: len(a.got) >= 20, 20)
    before = len(glob.glob(os.path.join(d, "events*.seg")))
    s, body = admin(svc, "DELETE", "/events/20")
    after = len(glob.glob(os.path.join(d, "events*.seg")))
    check(f"active: the newest event is erased (200): its segment was sealed first ({before} then {after} segments) and its word is gone",
          s == 200 and after == before + 1 and word(20).encode() not in all_bytes(d) and word(19).encode() in all_bytes(d), f"{s} {body}")
    svc.kill()
    a.peer.close()
    b.peer.close()


def stage_kill():
    for step in (34, 35, 36):
        d = tempfile.mkdtemp(prefix="hooks-erase-")
        a, b = Receiver(), Receiver()
        svc = start(d, a, b, ["--compact-kill-at", str(step)])
        post_all(svc, 300)
        L.wait_for(lambda: len(a.got) >= 300, 30)

        def call():
            try:
                admin(svc, "DELETE", "/events/100")
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=call, daemon=True).start()
        reached = L.wait_for(lambda: os.path.exists(os.path.join(d, "killpoint")), 20)
        svc.kill()
        os.remove(os.path.join(d, "killpoint")) if os.path.exists(os.path.join(d, "killpoint")) else None
        svc2 = start(d, a, b)
        gone = word(100).encode() not in all_bytes(d)
        ev, whole = segment_events(d)
        check(f"kill: kill -9 at step {step} of the rewrite (reached: {reached}); the start finishes it: the word is gone, every segment whole, GET is 410",
              reached and gone and whole and svc2.get("/events/100")[0] == 410 and not glob.glob(os.path.join(d, "*.tmp")), f"{gone} {whole} {glob.glob(os.path.join(d, '*.tmp'))}")
        svc2.kill()
        a.peer.close()
        b.peer.close()


def main():
    for name in STAGES:
        globals()[f"stage_{name}"]()
    return check.finish("erase")


if __name__ == "__main__":
    sys.exit(main())
