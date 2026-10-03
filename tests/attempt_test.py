#!/usr/bin/env python3
"""One delivery attempt, on its own (`src/deliver.ls`): what each kind of receiver makes `attempt` answer.

    python3 tests/attempt_test.py build/attempt_probe

The probe prints the HTTP status, or `-` and a reason: 1 connect failed, 2 send failed, 3 deadline passed, 4 no status
line. Each case is a one-connection Python receiver; the deadline is 1000 ms.
"""
import socket
import subprocess
import sys
import threading
import time

PROBE = sys.argv[1] if len(sys.argv) > 1 else "build/attempt_probe"


def receiver(mode):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(4)

    def serve():
        c, _ = s.accept()
        c.settimeout(3)
        try:
            c.recv(4096)
        except OSError:
            pass
        if mode == "ok":
            c.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")
        elif mode == "500":
            c.sendall(b"HTTP/1.1 500 Oops\r\n\r\n")
        elif mode == "stall":
            time.sleep(2.5)
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


def probe(port, ms=1000):
    t0 = time.time()
    out = subprocess.run([PROBE, "127.0.0.1", str(port), str(ms)], capture_output=True, text=True, timeout=20).stdout.strip()
    return out, (time.time() - t0) * 1000


def main():
    bad = 0
    for mode, want, lo, hi in [("ok", "204", 0, 500), ("500", "500", 0, 500), ("stall", "-3", 900, 1500), ("close", "-4", 0, 500),
                               ("junk", "-4", 0, 500), ("split", "200", 250, 900)]:
        got, ms = probe(receiver(mode))
        ok = got == want and lo <= ms <= hi
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {mode:6} answered {got:>4} in {ms:6.0f} ms (want {want}, {lo}..{hi} ms)")
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    got, ms = probe(dead)
    ok = got == "-1"
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} refused answered {got:>4} in {ms:6.0f} ms (want -1)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
