#!/usr/bin/env python3
"""One delivery attempt, through the service (src/attempt.cho): what each kind of receiver makes it record.

    python3 tests/attempt_test.py build/hooks

For each receiver the service runs with one endpoint, a retry schedule of 60 s (so only the first attempt happens) and an
attempt deadline of 800 ms; one event is posted, and the outcome written to `delivery.seg` is read with the independent reader:

    ok         answers 204                          delivered, soon
    500        answers 500                          failed
    stall      accepts, reads, never answers        failed, after about the deadline (not sooner, not much later)
    close      accepts, reads, closes               failed, soon
    junk       answers bytes that are not HTTP      failed, soon
    split      answers its status line in two parts delivered, after the pause between them
    refused    nothing listens                      failed, soon
    large      a 60,000-byte body to a receiver     delivered, and the receiver got every byte: the request needs
               with a 4 KiB receive buffer that     several writes, so a partial write is the case under test
               reads slowly
"""
import base64
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

DEADLINE = 0.8


def receiver(mode, got):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if mode == "large":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.bind(("127.0.0.1", 0))
    s.listen(4)

    def serve():
        c, _ = s.accept()
        c.settimeout(5)
        buf = b""
        try:
            if mode == "large":
                # Read slowly until the whole body has come: Content-Length says how much.
                while True:
                    time.sleep(0.01)
                    chunk = c.recv(2048)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\r\n\r\n" in buf:
                        head, _, body = buf.partition(b"\r\n\r\n")
                        n = int([l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0])
                        if len(body) >= n:
                            break
            else:
                buf = c.recv(65536)
        except OSError:
            pass
        got.append(buf)
        if mode in ("ok", "large"):
            c.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")
        elif mode == "500":
            c.sendall(b"HTTP/1.1 500 Oops\r\n\r\n")
        elif mode == "stall":
            time.sleep(3)
        elif mode == "junk":
            c.sendall(b"hello world, not http\r\n")
        elif mode == "split":
            c.sendall(b"HTT")
            time.sleep(0.3)
            c.sendall(b"P/1.1 200 OK\r\n\r\n")
        c.close()
        s.close()

    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1]


def outcomes(path):
    data = open(path, "rb").read() if os.path.exists(path) else b""
    records, _ = chaos.read_log(data)
    # The slot records (kinds 10 and 11, written when the endpoints are first seen) are not attempts.
    return [o for o in (struct.unpack("<5q", dict(pairs)[b"o"]) for _, pairs in records) if o[0] not in (10, 11)]


def run(mode):
    got = []
    if mode == "refused":
        port = chaos.free_port()      # nothing listens there, and no connection is given it as its own port
    else:
        port = receiver(mode, got)
    datadir = tempfile.mkdtemp(prefix="hooks-attempt-")
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {port} whsec_{base64.b64encode(os.urandom(24)).decode()}\n")
    svc = chaos.Service(chaos.free_port(), datadir, extra=("60000", str(int(DEADLINE * 1000))))
    svc.start()
    body = {"type": "t", "pad": "x" * (60000 if mode == "large" else 0)}
    payload = json.dumps(body).encode()
    t0 = time.time()
    chaos.post(svc.port, payload, timeout=5)
    seg = os.path.join(datadir, "delivery.seg")
    seen = None
    while time.time() < t0 + 6:
        o = outcomes(seg)
        if o:
            seen = (o[0], time.time() - t0)
            break
        time.sleep(0.01)
    svc.proc.terminate()
    svc.proc.wait()
    shutil.rmtree(datadir, ignore_errors=True)
    return seen, got, payload


def main():
    bad = 0
    cases = [("ok", 1, 0, 0.5), ("500", 2, 0, 0.5), ("stall", 2, DEADLINE - 0.05, DEADLINE + 0.6), ("close", 2, 0, 0.5),
             ("junk", 2, 0, 0.5), ("split", 1, 0.25, 0.9), ("refused", 2, 0, 0.5), ("large", 1, 0, 5)]
    for mode, kind, lo, hi in cases:
        seen, got, payload = run(mode)
        why = []
        if seen is None:
            why.append("no outcome was recorded")
            took = float("nan")
        else:
            (k, e, ev, attempts, next_at), took = seen
            if k != kind:
                why.append(f"outcome kind {k}, wanted {kind}")
            if attempts != 1:
                why.append(f"{attempts} attempts counted, wanted 1")
            if not (lo <= took <= hi):
                why.append(f"outcome after {took:.2f}s, wanted {lo:.2f}..{hi:.2f}")
        if mode == "large" and not why:
            head, _, body = got[0].partition(b"\r\n\r\n")
            if body != payload:
                why.append(f"the receiver got {len(body)} body bytes, wanted {len(payload)}, or they differed")
        bad += bool(why)
        print(f"{'ok  ' if not why else 'FAIL'} {mode:8} outcome {'?' if seen is None else seen[0][0]} after {took:5.2f}s"
              + ("" if not why else "   <- " + "; ".join(why)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
