#!/usr/bin/env python3
"""A reproducer of the second thing the soak found (docs/soak.md section 8): **with the data directory full, the service delivers the same events again and again, as fast as the receivers answer.**

    python3 scripts/soak/repro_full_disk.py build/hooks              # needs root or `sudo -n` to mount a tmpfs; exit 0: no storm; exit 1: an event was delivered more than 5 times

A tmpfs of 8 MB is the data directory; one endpoint, one receiver that counts the deliveries of each event. 200 events are posted and delivered, the filesystem is filled to the last byte
for 4 seconds, and 200 more events are posted (the service answers them with a `503`, which is right: nothing is acknowledged that was not flushed). The events that were delivered just
before the disk filled, and every event that is retried while it is full, are delivered over and over: the outcome of an attempt cannot be written, so the attempt never counts, and the
retry is not spaced by the schedule (a `deliver` that is not recorded is not a failure with a time for the next attempt). The receiver sees the same webhook-id hundreds of times a second.
`/readyz` says `delivery_log` (or `events_log`): the readiness is right; the loop that sends is not told.
"""
import collections
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
binary = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(HERE)), "build", "hooks")
sys.argv = sys.argv[:1]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests"))
import opslib as L  # noqa: E402

root = tempfile.mkdtemp(prefix="repro-full-disk-")
d = os.path.join(root, "data")
os.makedirs(d)
sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
subprocess.run(sudo + ["mount", "-t", "tmpfs", "-o", "size=8m", "tmpfs", d], check=True)
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
    svc = L.Service(binary, d, ["--schedule", "1000,1000,1000"])
    svc.start()
    for n in range(200):
        svc.post_event(n)
    L.wait_for(lambda: len(got) >= 200, 20)
    time.sleep(0.5)
    print(f"before: {len(got)} events delivered, {sum(got.values())} deliveries")
    ballast = os.path.join(d, "ballast")
    subprocess.run(["fallocate", "-l", str(int(subprocess.run(["df", "--output=avail", "-B1", d], capture_output=True, text=True).stdout.split()[1])), ballast], check=False)
    refused = 0
    for n in range(200, 400):
        try:
            s, _ = svc.post_event(n)
            refused += s != 202
        except OSError:
            refused += 1
    time.sleep(4)
    print(f"disk full for 4 s: {refused} of 200 new events refused; /readyz: {svc.get('/readyz')}; {len(got)} events seen by the receiver, {sum(got.values())} deliveries, the most for one event: {max(got.values())}")
    storm = max(got.values()) > 5
    os.remove(ballast)
    svc.kill()
    sys.exit(1 if storm else 0)
finally:
    subprocess.run(sudo + ["umount", d], capture_output=True)
