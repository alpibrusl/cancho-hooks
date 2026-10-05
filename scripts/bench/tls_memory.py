#!/usr/bin/env python3
"""What TLS costs the service in memory, on each backend (docs/pure-tls.md).

    python3 scripts/bench/tls_memory.py [build/hooks pure/build/hooks-pure]

For each binary: the service's resident set (`VmRSS`) and its peak (`VmHWM`), from /proc, after it has started and been idle, after one TLS handshake is held by
a receiver that never answers, and after 64 are (8 endpoints, 8 events: the 64 attempts the service allows at once). A held handshake is the most a connection
holds before it has data: the client has its hello out and its state allocated. The receiver is Python and is not counted. The difference between the first and
the last column is what 64 connections cost, and the first is what the service costs before any: the pure backend's engine allocates its 64 slots and its trust
store when it starts, where OpenSSL allocates a connection's memory when it is made. Reports the machine, as `https_cost.py` does.
"""
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402


def status_kib(pid, field):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith(field + ":"):
            return int(line.split()[1])
    return 0


def measure(binary):
    pki = K.Pki()
    good = pki.leaf("hooks.test")
    srv = K.TlsServer(*good, mode="hold")
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    d = L.free_dir("hooks-mem-")
    secret = L.secret()
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i in range(8):
            f.write(f"{i + 1} https://hooks.test {srv.port} {secret}\n")
    svc = L.Service(binary, d, ["--schedule", "60000", "--deadline-ms", "20000", "--dns-server", f"127.0.0.1:{dns.port}", "--tls-ca-file", pki.ca_pem])
    svc.start()
    pid = svc.proc.pid
    time.sleep(1.0)
    rows = [("idle", status_kib(pid, "VmRSS"), status_kib(pid, "VmHWM"))]
    svc.post_event(1)
    L.wait_for(lambda: srv.accepted >= 8, 15)
    time.sleep(0.5)
    rows.append(("8 held (one event, 8 endpoints)", status_kib(pid, "VmRSS"), status_kib(pid, "VmHWM")))
    for n in range(2, 9):
        svc.post_event(n)
    ok = L.wait_for(lambda: srv.accepted >= 64, 20)
    time.sleep(0.5)
    rows.append(("64 held" + ("" if ok else f" (only {srv.accepted} accepted)"), status_kib(pid, "VmRSS"), status_kib(pid, "VmHWM")))
    svc.stop()
    dns.close()
    srv.close()
    shutil.rmtree(d, ignore_errors=True)
    return rows


def main():
    cpu_model = ""
    for line in open("/proc/cpuinfo"):
        if line.startswith(("model name", "Model")):
            cpu_model = line.split(":", 1)[1].strip()
            break
    binaries = sys.argv[1:3] if len(sys.argv) >= 3 else ["build/hooks", "pure/build/hooks-pure"]
    print(f"{cpu_model or os.uname().machine}, {os.cpu_count()} cores")
    print(f"{'binary':24} {'state':36} {'VmRSS KiB':>10} {'VmHWM KiB':>10}")
    for b in binaries:
        for state, rss, hwm in measure(b):
            print(f"{os.path.basename(b):24} {state:36} {rss:10} {hwm:10}", flush=True)


if __name__ == "__main__":
    main()
