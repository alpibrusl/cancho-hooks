#!/usr/bin/env python3
"""A full data directory does not make the service send the same events again and again (docs/soak.md, finding 2).

    python3 tests/fulldisk_test.py build/hooks        (root, or `sudo -n`, to mount a tmpfs of 8 MB as the data directory)

The soak found it: once the outcomes log could not take a write, an attempt could not be recorded, so it counted for nothing and was due again at once; one
event was sent 939 times in four seconds, 7,692 deliveries for 218 events. A broken log is cleared only by a restart (`/readyz` says so), so the service
starts no attempt while its outcomes log is broken. Here: 200 events delivered, the disk filled to the last byte, 200 more posted (refused or not), four
seconds; then no event may have been sent more than twice, and `/readyz` is `503`. Then the disk is freed and the service restarted: every acknowledged
event reaches the receiver, and none more than twice in all (once before the fault, once again for a delivery the log could not record).
"""
import collections
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


def main():
    root = tempfile.mkdtemp(prefix="hooks-fulldisk-")
    d = os.path.join(root, "data")
    os.makedirs(d)
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    subprocess.run(sudo + ["mount", "-t", "tmpfs", "-o", "size=8m,mode=1777", "tmpfs", d], check=True)
    svc = None
    try:
        got, lock = collections.Counter(), threading.Lock()
        peer = L.Peer("ok")

        def serve(c):
            try:
                g = peer._read_request(c)
                if g is None:
                    return
                with lock:
                    got[int(g[0][b"webhook-id"][4:])] += 1
                c.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

        peer._serve = serve
        open(os.path.join(d, "endpoints.conf"), "w").write(f"0 127.0.0.1 {peer.port} {L.secret()}\n")
        svc = L.Service(BIN, d, ["--schedule", "1000,1000,1000"])
        svc.start()
        acked = set()          # the ids the service answered 202 with
        for n in range(200):
            s, body = svc.post_event(n)
            if s == 202:
                acked.add(json.loads(body)["id"])
        check("before: 200 events taken and delivered once each", len(acked) == 200 and L.wait_for(lambda: len(got) >= 200, 30) and max(got.values()) == 1, f"{len(acked)} {len(got)}")
        time.sleep(0.5)
        ballast = os.path.join(d, "ballast")
        free = int(subprocess.run(["df", "--output=avail", "-B1", d], capture_output=True, text=True).stdout.split()[1])
        subprocess.run(["fallocate", "-l", str(free), ballast], check=False)
        taken = 0
        for n in range(200, 400):
            try:
                s, body = svc.post_event(n)
                if s == 202:
                    taken += 1
                    acked.add(json.loads(body)["id"])
            except OSError:
                pass
        time.sleep(4)
        with lock:
            most, total, seen = max(got.values()), sum(got.values()), len(got)
        check(f"disk full for 4 s ({taken} of 200 more events taken): no event sent more than twice ({seen} events, {total} deliveries, the most for one {most})", most <= 2, str(most))
        rs, rbody = svc.get("/readyz")
        check(f"... and /readyz is 503 and says which log is broken ({rbody[:60]!r})", rs == 503 and b"_log" in rbody, f"{rs} {rbody!r}")
        os.remove(ballast)
        svc.kill()
        svc.start()
        ok = L.wait_for(lambda: acked <= set(got), 60)
        time.sleep(1.0)
        with lock:
            most = max(got.values())
            missing = sorted(acked - set(got))
        check(f"after the disk is freed and a restart: every one of the {len(acked)} acknowledged events reaches the receiver, none more than twice in all (the most {most})",
              ok and most <= 2, f"missing {missing[:10]}, most {most}")
    finally:
        if svc is not None:
            try:
                svc.kill()
            except Exception:  # noqa: BLE001
                pass
        subprocess.run(sudo + ["umount", d], capture_output=True)
    return check.finish("fulldisk")


if __name__ == "__main__":
    sys.exit(main())
