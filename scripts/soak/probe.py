#!/usr/bin/env python3
"""The loop probe of the soak (docs/soak.md section 2): ten times a second it asks the service `GET /healthz` over a new connection and, right after, makes the same kind of
request to a responder of its own in this process. The first measures the service's one loop (it answers /healthz from that loop, so a loop that is held is a late
answer); the second measures the host (the scheduler, this process, the kernel's loopback): a delay both shared is the machine's, and the checker does not blame the service
for it. One line of JSON a second to the output file:

    {"t": <second>, "n": requests, "max": ms, "ctl": largest control time ms, "err": failed requests, "l": [ms of each request]}

    probe.py PORT OUTFILE [--cpus 0,1,2] [--interval-ms 100]
"""
import argparse
import json
import os
import socket
import sys
import threading
import time


def echo_server(cpus=None):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def serve():
        if cpus:
            try:
                os.sched_setaffinity(0, cpus)      # this thread only: the host's weather on the core the service has, not on the harness's
            except OSError:
                pass
        while True:
            c, _ = s.accept()
            try:
                c.recv(16)
                c.sendall(b"x")
            except OSError:
                pass
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1]


def ask(port, payload, want, timeout):
    t = time.perf_counter()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(payload)
        got = s.recv(256)
        s.close()
        ok = got.startswith(want)
    except OSError:
        ok = False
    return (time.perf_counter() - t) * 1000.0, ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("port", type=int)
    p.add_argument("out")
    p.add_argument("--cpus", default="")
    p.add_argument("--interval-ms", type=int, default=100)
    p.add_argument("--ctl-cpus", default="", help="the cores the control responder runs on (the service's)")
    a = p.parse_args()
    if a.cpus:
        try:
            os.sched_setaffinity(0, {int(c) for c in a.cpus.split(",")})
        except (OSError, AttributeError):
            pass
    ctl = echo_server({int(c) for c in a.ctl_cpus.split(",")} if a.ctl_cpus else None)
    req = b"GET /healthz HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n"
    out = open(a.out, "a", buffering=1)
    step = a.interval_ms / 1000.0
    nxt = time.monotonic()
    cur, win = None, None
    while True:
        nxt += step
        d = nxt - time.monotonic()
        if d > 0:
            time.sleep(d)
        elif d < -2:
            nxt = time.monotonic()
        sec = int(time.time())
        if cur != sec:
            if win is not None and win["n"]:
                win["l"] = [round(x, 1) for x in win["l"]]
                out.write(json.dumps(win) + "\n")
            cur, win = sec, {"t": sec, "n": 0, "max": 0.0, "ctl": 0.0, "err": 0, "l": []}
        ms, ok = ask(a.port, req, b"HTTP/1.1 200", 5.0)
        cms, _ = ask(ctl, b"x", b"x", 5.0)
        win["n"] += 1
        win["ctl"] = max(win["ctl"], round(cms, 1))
        if ok:
            win["max"] = max(win["max"], round(ms, 1))
            win["l"].append(ms)
        else:
            win["err"] += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
