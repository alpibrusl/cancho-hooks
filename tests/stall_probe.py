#!/usr/bin/env python3
"""How much a bad receiver costs the *ingest* path (design criterion 3's question, asked of H1b).

    python3 tests/stall_probe.py build/hooks <mode>

mode: healthy | silent (accepts, never answers) | blackhole (a listener whose accept queue is full, so the SYN is dropped
and `connect` waits for the kernel). Posts 40 events one after another and prints the latency of each POST: p50, p99, max.
A report, not a gate: this is the number that decides whether non-blocking connect is the next piece of lex-sys.
"""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

BIN, MODE = os.path.abspath(sys.argv[1]), sys.argv[2]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


socks = []
if MODE == "healthy":
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    rport = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
elif MODE == "silent":
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(64)
    rport = s.getsockname()[1]
    socks.append(s)

    def eat():
        while True:
            c, _ = s.accept()
            socks.append(c)

    threading.Thread(target=eat, daemon=True).start()
else:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(0)
    rport = s.getsockname()[1]
    socks.append(s)
    # Fill the accept queue: connections that complete the handshake and are never accepted.
    for _ in range(4):
        c = socket.socket()
        c.setblocking(False)
        try:
            c.connect(("127.0.0.1", rport))
        except BlockingIOError:
            pass
        socks.append(c)
    time.sleep(0.2)

datadir = tempfile.mkdtemp(prefix="hooks-stall-")
port = free_port()
p = subprocess.Popen([BIN, str(port), datadir, "127.0.0.1", str(rport)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
p.stderr.readline()
lat = []
for n in range(int(os.environ.get('N', '40'))):
    t0 = time.time()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("POST", "/events", body=b'{"type":"x"}', headers={"Content-Type": "application/json"})
        c.getresponse().read()
        lat.append((time.time() - t0) * 1000)
    except OSError:
        lat.append(10000.0)  # no answer within 10 s: counted as 10 s, which understates it
    c.close()
    time.sleep(0.05)
p.kill()
lat.sort()
slow = sum(1 for x in lat if x > 500)
print(f"{MODE:9} POST /events latency ms: p50 {lat[len(lat)//2]:.1f}  p99 {lat[int(len(lat)*0.99)]:.1f}  max {lat[-1]:.1f};"
      f" {slow} of {len(lat)} took over 500 ms")
