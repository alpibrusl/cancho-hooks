#!/usr/bin/env python3
"""A report, not a gate (docs/design.md section 29): two endpoints, one dead, 3,000 events. Prints the cursors.

    gcc -O2 -o scripts/bench/loadgen scripts/bench/loadgen.c && gcc -O2 -o scripts/bench/sink scripts/bench/sink.c
    python3 scripts/bench/stall_probe.py          # HOOKS_BIN is not read: it runs build/hooks

Before the fix of docs/production.md item 0.1 the healthy endpoint stops at cursor 1,024; after it, at 3,000.
"""
import base64, os, subprocess, sys, tempfile, time, json, urllib.request, socket
HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(os.path.dirname(HERE)); sys.path.insert(0,HERE)
import run as R
d=tempfile.mkdtemp(prefix="stall-")
sp=R.free_port(); dead=R.free_port()   # nothing listens on `dead`
sink=subprocess.Popen([os.path.join(HERE,"sink"),str(sp)],stderr=subprocess.PIPE)
sec="whsec_"+base64.b64encode(os.urandom(24)).decode()
open(f"{d}/endpoints.conf","w").write(f"0 127.0.0.1 {sp} {sec}\n1 127.0.0.1 {dead} {sec}\n")
port=R.free_port()
p=subprocess.Popen([os.path.join(ROOT,"build","hooks"),"--port",str(port),"--dir",d,"--allow-private-hosts","1"],stderr=subprocess.PIPE,stdout=subprocess.DEVNULL)
assert p.stderr.readline().strip()==b"listening"
out=subprocess.run([os.path.join(HERE,"loadgen"),str(port),"16","3000","200"],capture_output=True,text=True,timeout=120)
time.sleep(4)
eps=json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/endpoints").read())
print("3000 events posted; healthy endpoint 0 and dead endpoint 1:", [(e["id"],e["cursor"]) for e in eps])
s=R.stats(port); print("delivered",s["delivered"],"attempts",s["attempts"],"failed",s["failed"])
p.terminate(); p.wait(); sink.terminate(); sink.wait()
