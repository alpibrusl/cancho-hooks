#!/usr/bin/env python3
"""Capacity of one core (docs/design.md section 28): a load generator and a sink, both in C, drive the service and the script reads
the service's own CPU time from /proc.

    gcc -O2 -o scripts/bench/loadgen scripts/bench/loadgen.c && gcc -O2 -o scripts/bench/sink scripts/bench/sink.c
    python3 scripts/bench/run.py [scenario ...]          # HOOKS_BIN=path/to/hooks to name the binary

Scenarios (ten_wanted and ten_unwanted need the version with event types): ingest64 (50,000 events, 64 keep-alive connections, no endpoint), ingest1 (20,000 events, one connection: one flush each),
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
def pg_args():
    """HOOKS_PG=host:port:user:database (and HOOKS_PG_PASSWORD): the flags, and the endpoints table emptied for the scenario to fill."""
    h,pt,u,db=os.environ["HOOKS_PG"].split(":")
    a=["--pg-host",h,"--pg-port",pt,"--pg-user",u,"--pg-database",db]
    env=dict(os.environ,PGPASSWORD=os.environ.get("HOOKS_PG_PASSWORD",""))
    if os.environ.get("HOOKS_PG_PASSWORD"): a+=["--pg-password",os.environ["HOOKS_PG_PASSWORD"]]
    def psql(sql): subprocess.run(["psql","-q","-h",h,"-p",pt,"-U",u,"-d",db,"-c",sql],check=True,env=env,capture_output=True)
    return a,psql
def run(endpoints, events, conns, label, body=200, types=None, pg=False):
    d=tempfile.mkdtemp(prefix="bench-")
    sink_port=free_port()
    sink=subprocess.Popen([f"{HERE}/sink",str(sink_port)],stderr=subprocess.PIPE)
    secret="whsec_"+base64.b64encode(os.urandom(24)).decode()
    extra=[]
    with open(f"{d}/endpoints.conf","w") as f:
        for i in range(endpoints): f.write(f"{i} 127.0.0.1 {sink_port} {secret}" + (f" types={types}" if types else "") + "\n")
    if pg:
        # the endpoints are the table's, and every attempt that ends is a row of the history (design section 24)
        extra,psql=pg_args()
        psql("truncate endpoints, attempts")
        for i in range(endpoints): psql(f"insert into endpoints (id, host, port, secret) values ({i}, '127.0.0.1', {sink_port}, '{secret}')")
    port=free_port()
    p=subprocess.Popen([BIN,"--port",str(port),"--dir",d,"--allow-private-hosts","1","--schedule","100,100,100",*extra],stderr=subprocess.PIPE,stdout=subprocess.DEVNULL)
    assert p.stderr.readline().strip()==b"listening"
    if pg:
        # the start does not wait for the database (design section 37): wait for /readyz, as the old start did by not listening until then
        end=time.time()+30
        while time.time()<end:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz",timeout=2); break
            except Exception: time.sleep(0.05)
    c0=cpu(p.pid); t0=time.time()
    out=subprocess.run([f"{HERE}/loadgen",str(port),str(conns),str(events),str(body)],capture_output=True,text=True,timeout=300)
    t_ingest=time.time()-t0; c_ingest=cpu(p.pid)-c0
    want=events*endpoints; t_del=None
    if types and types != "bench":
        want=0   # nothing is wanted: the endpoints pass over every event (design section 35)
    if endpoints:
        end=time.time()+120
        while time.time()<end:
            s=stats(port)
            if types and types != "bench":
                if s.get("filtered",0)>=events*endpoints: t_del=time.time()-t0; break
            elif s["delivered"]>=want: t_del=time.time()-t0; break
            time.sleep(0.05)
        s=stats(port)
        if types and types != "bench": want=events*endpoints
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
    # design section 35: ten endpoints with a list. "wanted" lists the event's type (every delivery as in "ten", plus the check of the list);
    # "unwanted" lists another (50,000 passes-over and no delivery: the time and CPU are per (endpoint, event) pair, not per delivery).
    # design section 41: many endpoints, the same sink. "sixtytwo" is the most main could have; "many" is the new limit (events x endpoints deliveries each).
    "sixtytwo": lambda: run(62, 1000, 64, "62 endpoints"),
    "many": lambda: run(1024, 100, 64, "1,024 endpoints"),
    "many_idle": lambda: run(1024, 20000, 64, "1,024 endpoints that want nothing (filtered by type)", types="other.*"),
    "ten_pg": lambda: run(10, 5000, 64, "10 endpoints from the table, every attempt a row of the history", pg=True),
    "ingest64_pg": lambda: run(0, 50000, 64, "ingest only, 64 conns, a database named (the pool idle)", pg=True),
    "ten_wanted": lambda: run(10, 5000, 64, "10 endpoints, each with a list that wants the type", types="bench"),
    "ten_unwanted": lambda: run(10, 5000, 64, "10 endpoints, each with a list that does not want the type", types="other.*"),
}
if __name__=="__main__":
    for name in (sys.argv[1:] or SCENARIOS):
        SCENARIOS[name]()
