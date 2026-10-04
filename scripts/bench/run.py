#!/usr/bin/env python3
"""Capacity of one core (docs/design.md section 28): a load generator and a sink, both in C, drive the service and the script reads
the service's own CPU time from /proc.

    gcc -O2 -o scripts/bench/loadgen scripts/bench/loadgen.c && gcc -O2 -o scripts/bench/sink scripts/bench/sink.c
    python3 scripts/bench/run.py [scenario ...]          # HOOKS_BIN=path/to/hooks to name the binary

Scenarios: ingest64 (50,000 events, 64 keep-alive connections, no endpoint), ingest1 (20,000 events, one connection: one flush each),
one (1 endpoint, 20,000 events, 64 connections), ten (10 endpoints, 5,000 events: 50,000 deliveries). The sink answers every POST
with 204 and closes. The retry schedule is 100 ms so that a failed attempt does not hide in the wall time. Not a benchmark of
anyone else's service: a measurement of this one, on whatever machine runs it, and the machine is part of the result.
"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, socket
HERE=os.path.dirname(os.path.abspath(__file__))
BIN=os.environ.get("HOOKS_BIN") or os.path.join(os.path.dirname(os.path.dirname(HERE)), "build", "hooks")
def free_port():
    s=socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); return p
def cpu(pid):
    f=open(f"/proc/{pid}/stat").read().rsplit(")",1)[1].split(); return (int(f[11])+int(f[12]))/os.sysconf("SC_CLK_TCK")
def stats(port):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats",timeout=5).read())
def run(endpoints, events, conns, label, body=200):
    d=tempfile.mkdtemp(prefix="bench-")
    sink_port=free_port()
    sink=subprocess.Popen([f"{HERE}/sink",str(sink_port)],stderr=subprocess.PIPE)
    secret="whsec_"+base64.b64encode(os.urandom(24)).decode()
    with open(f"{d}/endpoints.conf","w") as f:
        for i in range(endpoints): f.write(f"{i} 127.0.0.1 {sink_port} {secret}\n")
    port=free_port()
    p=subprocess.Popen([BIN,"--port",str(port),"--dir",d,"--allow-private-hosts","1","--schedule","100,100,100"],stderr=subprocess.PIPE,stdout=subprocess.DEVNULL)
    assert p.stderr.readline().strip()==b"listening"
    c0=cpu(p.pid); t0=time.time()
    out=subprocess.run([f"{HERE}/loadgen",str(port),str(conns),str(events),str(body)],capture_output=True,text=True,timeout=300)
    t_ingest=time.time()-t0; c_ingest=cpu(p.pid)-c0
    want=events*endpoints; t_del=None
    if endpoints:
        end=time.time()+120
        while time.time()<end:
            s=stats(port)
            if s["delivered"]>=want: t_del=time.time()-t0; break
            time.sleep(0.05)
        s=stats(port)
    c_all=cpu(p.pid)-c0
    print(f"{label}: ingest: {out.stdout.strip()}")
    print(f"   hooks CPU for ingest {c_ingest:.2f}s ({c_ingest/events*1e6:.0f} us/event)", end="")
    if endpoints:
        print(f"; all {want} deliveries done after {t_del:.2f}s ({want/t_del:.0f} deliveries/s end to end), hooks CPU total {c_all:.2f}s = {(c_all-c_ingest)/want*1e6:.0f} us/delivery; stats {s}")
    else: print()
    p.terminate(); p.wait(); sink.terminate(); sink.wait(); print('   ', sink.stderr.read().decode().strip()); shutil.rmtree(d,ignore_errors=True)
SCENARIOS={
    "ingest64": lambda: run(0, 50000, 64, "ingest only, 64 conns"),
    "ingest1": lambda: run(0, 20000, 1, "ingest only, 1 conn"),
    "one": lambda: run(1, 20000, 64, "1 endpoint"),
    "ten": lambda: run(10, 5000, 64, "10 endpoints"),
}
if __name__=="__main__":
    for name in (sys.argv[1:] or SCENARIOS):
        SCENARIOS[name]()
