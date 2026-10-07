#!/usr/bin/env python3
"""What a delivery costs the service in CPU, by kind (docs/design.md section 40).

    python3 scripts/bench/https_cost.py [deliveries]         # default 600 per row; HOOKS_BIN=path/to/hooks to name the binary; REPS=5 rows are repeated; KINDS='http, address' only those rows
    EVENT_BYTES=50000 python3 scripts/bench/https_cost.py    # each event carries that many bytes more: the difference from a row without is the cost of the records (docs/pure-tls.md)
    PIN=1 python3 scripts/bench/https_cost.py                # the service on core 3 (taskset), the receivers and this script on cores 1 and 2: as the cancho spike measured

Each row starts a service with E endpoints, posts events until there have been `deliveries` deliveries, and reads the service's own CPU time (user + system, from /proc) before and
after: the difference over the deliveries, in microseconds (the median of REPS runs, with the least and the most). The receivers are Python and are not counted; they run on the other cores. The kinds:

    http, address     an IPv4 literal, plain HTTP: what a delivery cost before names and TLS
    http, name        the same through a name: one lookup (DNS over TCP, to a name server of the script's own) per delivery
    https, full       a name, TLS, a full handshake every time (`tls-resume 0`)
    https, resumed    a name, TLS, the endpoint's session resumed (after the first delivery of each endpoint)
    http, kept        a name, plain HTTP, on a connection kept from the endpoint's last delivery (docs/design.md section 53)
    https, kept       a name, TLS, the same: no handshake but the endpoint's first

at E = 1 and E = 10 endpoints behind one receiver. The cost of the ingest of an event, of the logs and of the history is in every row (the service is whole); the first column's number is
the floor. Not a benchmark of anyone else's service: a measurement of this one on whatever machine runs it, and the machine is part of the result (`nproc`, the CPU model, are printed).
"""
import os
import shutil
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
_args = sys.argv[1:]
sys.argv = sys.argv[:1]
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402
from keepkit import Receiver  # noqa: E402

BIN = os.environ.get("HOOKS_BIN") or os.path.join(ROOT, "build", "hooks")
DELIVERIES = int(_args[0]) if _args else 600
EVENT_BYTES = int(os.environ.get("EVENT_BYTES", "0"))
PAD = {"pad": "x" * EVENT_BYTES} if EVENT_BYTES else None
REPS = int(os.environ.get("REPS", "3"))
if os.environ.get("PIN") == "1":
    # The service gets core 3 to itself (a wrapper that execs it under taskset); this process, and the receivers' threads in it, cores 1 and 2.
    wrapper = os.path.join(tempfile.mkdtemp(prefix="hooks-pin-"), "hooks")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/sh\nexec taskset -c 3 "{os.path.abspath(BIN)}" "$@"\n')
    os.chmod(wrapper, 0o755)
    BIN = wrapper
    os.sched_setaffinity(0, {1, 2})


def cpu_seconds(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def row(kind, endpoints, pki, cert):
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    d = L.free_dir("hooks-cost-")
    secret = L.secret()
    tls = kind.startswith("https")
    kept = kind.endswith("kept")
    # The receivers of the other rows answer `Connection: close`; the `kept` rows' keep the connection.
    if kept:
        srv = Receiver(tls_cert=cert if tls else None)
        sink = None
    else:
        srv = K.TlsServer(*cert) if tls else None
        sink = None if tls else K.Sink("127.0.0.1")
    port = srv.port if srv else sink.port
    host = {"http, address": "127.0.0.1", "http, name": "hooks.test", "https, full": "https://hooks.test", "https, resumed": "https://hooks.test",
            "http, kept": "hooks.test", "https, kept": "https://hooks.test"}[kind]
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(endpoints):
            f.write(f"{i + 1} {host} {port} {secret}\n")
    args = ["--schedule", "60000", "--deadline-ms", "5000"]
    if not os.environ.get("BEFORE"):    # BEFORE=1: a binary from before names and TLS, which does not know these settings (only the `http, address` row)
        args += ["--dns-server", f"127.0.0.1:{dns.port}", "--tls-ca-file", pki.ca_pem]
    if kind == "https, full":
        args += ["--tls-resume", "0"]
    svc = L.Service(BIN, d, args)
    svc.start()
    events = DELIVERIES // endpoints
    # warm up: one event per endpoint (the first delivery of an endpoint is a full handshake even where sessions are kept), then count
    svc.post_event(0, extra=PAD)
    L.wait_for(lambda: svc.stats()["delivered"] >= endpoints, 30)
    time.sleep(0.2)
    before = cpu_seconds(svc.proc.pid)
    handshakes0 = (srv.handshakes, getattr(srv, "resumed", 0)) if tls else (0, 0)
    t0 = time.time()
    for n in range(1, events + 1):
        svc.post_event(n, extra=PAD)
    ok = L.wait_for(lambda: svc.stats()["delivered"] >= endpoints * (events + 1), 120)
    wall = time.time() - t0
    time.sleep(0.2)
    used = cpu_seconds(svc.proc.pid) - before
    n_done = endpoints * events
    res = None
    if tls:
        res = (srv.handshakes - handshakes0[0], getattr(srv, "resumed", 0) - handshakes0[1])
    svc.stop()
    dns.close()
    (srv or sink).close()
    shutil.rmtree(d, ignore_errors=True)
    return ok, used / n_done * 1e6, n_done, wall, res


def main():
    cpu_model = ""
    for line in open("/proc/cpuinfo"):
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    print(f"{cpu_model}, {os.cpu_count()} cores{', service pinned to core 3' if os.environ.get('PIN') == '1' else ''}; {DELIVERIES} deliveries a row, {REPS} runs, {EVENT_BYTES} bytes of padding an event; binary {os.environ.get('HOOKS_BIN') or 'build/hooks'}")
    pki = K.Pki()
    cert = pki.leaf("hooks.test")
    print(f"{'kind':16} {'endpoints':>9} {'deliveries':>10} {'CPU per delivery, median (least to most)':>42} {'wall':>8}  handshakes (resumed)")
    for endpoints in (1, 10):
        for kind in ("http, address", "http, name", "https, full", "https, resumed", "http, kept", "https, kept"):
            if os.environ.get("KINDS") and kind not in os.environ["KINDS"].split(";"):
                continue
            runs = [row(kind, endpoints, pki, cert) for _ in range(REPS)]
            us = sorted(r[1] for r in runs)
            ok = all(r[0] for r in runs)
            res = runs[-1][4]
            extra = f"{res[0]} ({res[1]})" if res else ""
            print(f"{kind:16} {endpoints:9} {runs[-1][2]:10} {statistics.median(us):17.0f} us ({us[0]:.0f} to {us[-1]:.0f}) {statistics.median(r[3] for r in runs):7.1f}s  {extra}{'' if ok else '   NOT ALL DELIVERED'}", flush=True)


if __name__ == "__main__":
    main()
