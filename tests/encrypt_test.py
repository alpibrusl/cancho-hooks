#!/usr/bin/env python3
"""Bodies encrypted at rest (docs/design.md section 47.4): `encryption-key-file`.

    python3 tests/encrypt_test.py build/hooks

  1. with a key: 100 events, each with a word of its own, are delivered with their bodies as they were posted (and signed over them); `GET /events/:id` gives the
     body; no word is in any file of the data directory; /stats counts 100 sealed
  2. a start without the key, or with another key, is refused (status 47) and changes nothing; a key file that holds no key is refused (status 46)
  3. rotation: a new key and the old one (`encryption-key-file-old`): the old events are read, a new event is sealed by the new key and delivered
  4. a log that began in the clear: events from before the key are read and delivered as they are, those after it are sealed
  5. an erasure of a sealed event: the body is replaced, `GET` is 410
"""
import collections
import glob
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
ADMIN = "encrypt-admin-token"
check = L.Checks()


class Receiver:
    def __init__(self):
        self.bodies, self.lock = {}, threading.Lock()
        self.peer = L.Peer("ok")
        self.peer._serve = self.serve
        self.port = self.peer.port

    def serve(self, c):
        try:
            g = self.peer._read_request(c)
            if g is None:
                return
            with self.lock:
                self.bodies[int(g[0][b"webhook-id"][4:])] = g[1]
            c.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass


def word(n):
    return f"secret-body-word-{n:05d}"


def key_file(text=None):
    fd, p = tempfile.mkstemp(prefix="hooks-key-")
    os.write(fd, (text if text is not None else secrets.token_hex(32) + "\n").encode())
    os.close(fd)
    os.chmod(p, 0o600)
    return p


def all_bytes(d):
    return b"".join(open(p, "rb").read() for p in glob.glob(os.path.join(d, "*")) if os.path.isfile(p))


def svc_for(d, rcv, keys=()):
    open(os.path.join(d, "endpoints.conf"), "w").write(f"1 127.0.0.1 {rcv.port} {L.secret()}\n")
    return L.Service(BIN, d, ["--admin-token", ADMIN, *keys])


def run_exit(d, keys):
    p = subprocess.run([BIN, "--port", str(L.chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", *keys], capture_output=True, text=True, timeout=30)
    return p.returncode, p.stderr


def main():
    d = tempfile.mkdtemp(prefix="hooks-encrypt-")
    rcv = Receiver()
    k1 = key_file()
    svc = svc_for(d, rcv, ["--encryption-key-file", k1])
    check("1. the service starts with a key", svc.start(timeout=30), svc.stderr()[-300:])
    for n in range(1, 101):
        svc.post_event(n, extra={"w": word(n)})
    got = L.wait_for(lambda: len(rcv.bodies) >= 100, 30)
    plain = all(json.loads(rcv.bodies[i]).get("w") == word(i) for i in rcv.bodies)
    check(f"1. the 100 events are delivered with their bodies as posted ({len(rcv.bodies)})", got and plain, "")
    code, body = svc.get("/events/42")
    check("1. GET /events/42 gives the body", code == 200 and json.loads(body)["event"].get("w") == word(42), f"{code} {body[:120]}")
    raw = all_bytes(d)
    leaked = [n for n in range(1, 101) if word(n).encode() in raw]
    check(f"1. no word is in any file of the data directory ({len(leaked)} found)", not leaked, str(leaked[:5]))
    check(f"1. /stats counts 100 sealed ({svc.stats().get('bodies_sealed')})", svc.stats().get("bodies_sealed") == 100, "")
    svc.stop(10)
    before = {p: open(p, "rb").read() for p in glob.glob(os.path.join(d, "*.seg"))}
    c1, e1 = run_exit(d, [])
    c2, e2 = run_exit(d, ["--encryption-key-file", key_file()])
    same = all(open(p, "rb").read() == b for p, b in before.items())
    check(f"2. a start without the key ({c1}) or with another key ({c2}) is refused with status 47, and nothing changes", c1 == 47 and c2 == 47 and same, e1[-200:] + e2[-200:])
    c3, e3 = run_exit(tempfile.mkdtemp(), ["--encryption-key-file", key_file("not a key")])
    check(f"2. a key file that holds no key is refused with status 46 ({c3})", c3 == 46, e3[-200:])
    # 3.
    k2 = key_file()
    svc = svc_for(d, rcv, ["--encryption-key-file", k2, "--encryption-key-file-old", k1])
    svc.start(timeout=30)
    code, body = svc.get("/events/7")
    check("3. with a new key and the old one, an old event is read", code == 200 and json.loads(body)["event"].get("w") == word(7), f"{code} {body[:120]}")
    svc.post_event(101, extra={"w": word(101)})
    ok = L.wait_for(lambda: 101 in rcv.bodies, 15)
    check("3. a new event is sealed by the new key and delivered", ok and json.loads(rcv.bodies[101]).get("w") == word(101) and word(101).encode() not in all_bytes(d), "")
    svc.stop(10)
    # 4.
    d2 = tempfile.mkdtemp(prefix="hooks-encrypt-")
    r2 = Receiver()
    svc = svc_for(d2, r2)
    svc.start(timeout=30)
    for n in range(1, 6):
        svc.post_event(n, extra={"w": word(n)})
    L.wait_for(lambda: len(r2.bodies) >= 5, 15)
    svc.stop(10)
    svc = svc_for(d2, r2, ["--encryption-key-file", key_file()])
    svc.start(timeout=30)
    for n in range(6, 11):
        svc.post_event(n, extra={"w": word(n)})
    ok = L.wait_for(lambda: len(r2.bodies) >= 10, 15)
    raw = all_bytes(d2)
    check("4. a log that began in the clear: the old events are read as they are, the new ones sealed and delivered",
          ok and json.loads(svc.get("/events/3")[1])["event"].get("w") == word(3) and json.loads(svc.get("/events/8")[1])["event"].get("w") == word(8)
          and word(3).encode() in raw and word(8).encode() not in raw, "")
    # 5.
    s, b, _ = svc.request("DELETE", "/events/8", b"", {"Authorization": f"Bearer {ADMIN}"})
    check("5. a sealed event is erased: 200, then GET is 410", s == 200 and svc.get("/events/8")[0] == 410, f"{s} {b}")
    svc.kill()
    rcv.peer.close()
    r2.peer.close()
    return check.finish("encrypt")


if __name__ == "__main__":
    sys.exit(main())
