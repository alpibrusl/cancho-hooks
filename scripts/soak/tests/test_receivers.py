#!/usr/bin/env python3
"""The receivers' own accounting, tested without a service (docs/soak.md, "Validating the harness"): the ledger is written off the event loop and before the answer, whether an answer
counts is decided when it is sent (not from the delay that was planned), a connection the service closed is not an acknowledgement, the endpoints are divided between the
processes, and a port that is in use is reported and retried, never ignored.

    python3 scripts/soak/tests/test_receivers.py        # exit 0 if every check passes (about 15 s)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOAK = os.path.dirname(HERE)
sys.path.insert(0, SOAK)
from common import F_EFF, F_RISK, REC, read_records, recv_ledger  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Rig:
    def __init__(self, n_eps=4, shards=1, bind_stuck_s=1.0, ports=None, extra=()):
        self.dir = tempfile.mkdtemp(prefix="recv-test-")
        os.makedirs(os.path.join(self.dir, "ledger"))
        self.shards = shards
        self.ports = ports or [free_port() for _ in range(n_eps)]
        spec = {"t0": time.time(), "endpoints": [{"idx": i, "label": f"healthy{i}", "cls": "healthy", "port": p, "tls": False, "secrets": [], "params": {}} for i, p in enumerate(self.ports)]}
        json.dump(spec, open(os.path.join(self.dir, "spec.json"), "w"))
        self.procs = [subprocess.Popen([sys.executable, os.path.join(SOAK, "receivers.py"), "--dir", self.dir, "--seed", "1", "--shard", str(k), "--shards", str(shards),
                                        "--bind-stuck-s", str(bind_stuck_s), *extra], stdout=open(os.path.join(self.dir, "receivers.log"), "a"), stderr=subprocess.STDOUT) for k in range(shards)]
        self.ctl = {}
        end = time.time() + 15
        for k in range(shards):
            f = os.path.join(self.dir, f"ctl-{k}.port")
            while not os.path.exists(f) and time.time() < end:
                time.sleep(0.05)
            self.ctl[k] = int(open(f).read())
        time.sleep(0.3)

    def cmd(self, shard, c):
        s = socket.create_connection(("127.0.0.1", self.ctl[shard]), timeout=10)
        s.sendall((json.dumps(c) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf)

    def records(self):
        out = []
        for k in range(self.shards):
            out += read_records(recv_ledger(self.dir, k), 0, REC)[0]
        return sorted(out, key=lambda r: r[0])

    def close(self):
        for p in self.procs:
            p.kill()
            p.wait()
        shutil.rmtree(self.dir, ignore_errors=True)


def request(port, ev, wait=True, close_after=None, timeout=10):
    """One request as the service makes it. Returns (status or None, seconds until the answer)."""
    body = json.dumps({"type": "user.created", "n": ev, "t": time.time()}).encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nwebhook-id: evt_%d\r\nwebhook-timestamp: %d\r\nwebhook-signature: v1,xx\r\nContent-Length: %d\r\n\r\n" % (ev, int(time.time()), len(body)) + body)
    t0 = time.time()
    if close_after is not None:
        time.sleep(close_after)
        s.close()
        return None, time.time() - t0
    try:
        data = s.recv(4096)
    except OSError:
        data = b""
    s.close()
    return (int(data.split()[1]) if data.startswith(b"HTTP/1.1") else None), time.time() - t0


def main():
    # 1. a prompt answer is an acknowledgement; the record is in the ledger, with both times
    r = Rig()
    try:
        st, dt = request(r.ports[0], 11)
        time.sleep(0.2)
        recs = r.records()
        rec = recs[0] if recs else None
        check("a prompt 2xx is a recorded acknowledgement", st == 204 and rec and rec[6] & F_EFF and rec[7] == 204, str(recs))
        check("the record has the time of the request and the time the answer was sent", rec and 0 <= rec[9] - rec[0] < 0.5, str(rec))
        stats = r.cmd(0, {"op": "stats"})
        check("the stats say how the writer did", stats["writer"]["batches"] >= 1 and stats["writer"]["sync_fallbacks"] == 0 and stats["records"] == 1, str(stats))

        # 2. a loop that is late: the answer was planned for 0.3 s, goes out about 2.2 s after the request: the service counted a timeout, and the record must not say it was acknowledged
        r.cmd(0, {"op": "mode", "label": "healthy1", "mode": "slow", "until": time.time() + 60, "delay": 0.3})
        out = {}

        import threading

        def go():
            out["r"] = request(r.ports[1], 12)

        t = threading.Thread(target=go)
        t.start()
        time.sleep(0.15)
        r.cmd(0, {"op": "stall", "seconds": 2.0})
        t.join()
        time.sleep(0.2)
        rec = [x for x in r.records() if x[2] == 12][0]
        stats = r.cmd(0, {"op": "stats"})
        check("an answer planned for 0.3 s that went out 2 s late is not an acknowledgement", out["r"][0] == 204 and not rec[6] & F_EFF and rec[9] - rec[0] > 1.8, f"{out} {rec}")
        check("and the receivers say their own loop was the cause", stats["late_unplanned"] == 1 and stats["late_max_ms"] > 1800, str(stats))

        # 3. an answer that is slow by plan, within the deadline, is an acknowledgement and is marked as a risk
        r.cmd(0, {"op": "mode", "label": "healthy1", "mode": "slow", "until": time.time() + 60, "delay": 0.8})
        st, dt = request(r.ports[1], 13)
        time.sleep(0.2)
        rec = [x for x in r.records() if x[2] == 13][0]
        check("a planned 0.8 s answer is an acknowledgement and a risk", st == 204 and rec[6] & F_EFF and rec[6] & F_RISK and 0.75 < rec[9] - rec[0] < 1.2, str(rec))

        # 4. a connection the service closed before the answer: the request was seen (a record), but nothing was acknowledged
        r.cmd(0, {"op": "mode", "label": "healthy1", "mode": "slow", "until": time.time() + 60, "delay": 1.0})
        request(r.ports[1], 14, close_after=0.3)
        time.sleep(1.4)
        rec = [x for x in r.records() if x[2] == 14]
        stats = r.cmd(0, {"op": "stats"})
        check("a request whose connection the service closed is recorded and not acknowledged", len(rec) == 1 and not rec[0][6] & F_EFF and stats["conn_lost"] >= 1, f"{rec} {stats.get('conn_lost')}")
    finally:
        r.close()

    # 5. the answer is not sent before the record is in the file: many requests at once, each answer read means its record can be read
    r = Rig(n_eps=2)
    try:
        import threading
        bad = []

        def one(i):
            st, _ = request(r.ports[i % 2], 1000 + i)
            have = {x[2] for x in r.records()}
            if st == 204 and (1000 + i) not in have:
                bad.append(i)

        ts = [threading.Thread(target=one, args=(i,)) for i in range(60)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("whenever an answer was received its record was already in the ledger (60 requests at once)", not bad, str(bad[:5]))
    finally:
        r.close()

    # 6. two processes: each serves the endpoints with its own idx modulo 2, writes its own ledger
    r = Rig(n_eps=6, shards=2)
    try:
        for i, p in enumerate(r.ports):
            request(p, 2000 + i)
        time.sleep(0.3)
        per = [{x[1] for x in read_records(recv_ledger(r.dir, k), 0, REC)[0]} for k in range(2)]
        check("each process records only the endpoints it serves", per[0] == {0, 2, 4} and per[1] == {1, 3, 5}, str(per))
        st = [r.cmd(k, {"op": "stats"})["endpoints"] for k in range(2)]
        check("and listens for no others", st == [3, 3], str(st))
    finally:
        r.close()

    # 7. a port that is in use: reported with who holds it, retried, and used as soon as it is free
    port = free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))        # no SO_REUSEADDR: the way a stranger holds a port
    holder.listen(1)
    r = Rig(n_eps=1, ports=[port], bind_stuck_s=1.0)
    try:
        time.sleep(2.0)
        stats = r.cmd(0, {"op": "stats"})
        log = open(os.path.join(r.dir, "receivers.log")).read()
        check("a port that is in use is counted, named in the log with its holder, and reported as stuck", stats["bind_failures"] > 0 and stats["bind_stuck"] == ["healthy0"] and "held by" in log and ("python" in log.lower() or not os.path.exists("/proc/net/tcp")),
              f"{stats.get('bind_failures')} {stats.get('bind_stuck')} {log[:300]}")
        holder.close()
        time.sleep(0.5)
        st, _ = request(port, 3000)
        stats = r.cmd(0, {"op": "stats"})
        check("it listens as soon as the port is free, and the stuck report ends", st == 204 and stats["bind_stuck"] == [], str(stats))
    finally:
        r.close()
        holder.close()

    # 8. the cost of a record does not grow with the endpoints the run has made and taken away (the ticker walks every endpoint it holds, 20 times a second)
    r = Rig(n_eps=1, extra=("--forget-s", "1"))
    try:
        def per_record(n=1500):
            c0 = r.cmd(0, {"op": "stats"})["cpu_s"]
            for i in range(n):
                request(r.ports[0], 5000 + i)
            c1 = r.cmd(0, {"op": "stats"})["cpu_s"]
            return (c1 - c0) / n * 1e6      # microseconds of CPU a request

        before = per_record()
        for i in range(1200):
            p = free_port()
            r.cmd(0, {"op": "add", "spec": {"idx": 100 + i * 2, "label": f"churn{i}", "cls": "churn", "port": p, "tls": False, "secrets": [], "params": {}}})
            if i % 100 == 99:
                time.sleep(0.2)
        for i in range(1200):
            r.cmd(0, {"op": "remove", "label": f"churn{i}"})
        time.sleep(3.0)
        n_left = r.cmd(0, {"op": "stats"})["endpoints"]
        after = per_record()
        check(f"after 1,200 endpoints came and went the receiver holds {n_left} (not 1,201) and a request costs {after:.0f} us against {before:.0f} us", n_left <= 3 and after < before * 1.5 + 50,
              f"{n_left} {before} {after}")
    finally:
        r.close()

    # 9. the name server of the https endpoints answers as fast after 30,000 questions as after none (it counted every question ever asked, in a walk under the lock: the cost of a
    # question grew with the run, and it shares the interpreter with the first receiver process)
    import struct
    import tlskit
    dns = tlskit.DnsStub({"hooks.test": ["127.0.0.1"]}, keep_asked=False)
    try:
        q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x05hooks\x04test\x00" + b"\x00\x01\x00\x01"
        c = socket.create_connection(("127.0.0.1", dns.port))

        def batch(n):
            t0 = time.time()
            for _ in range(n):
                c.sendall(struct.pack(">H", len(q)) + q)
                got = b""
                while len(got) < 2:
                    got += c.recv(2)
                ln = struct.unpack(">H", got[:2])[0]
                body = got[2:]
                while len(body) < ln:
                    body += c.recv(ln - len(body))
            return (time.time() - t0) / n * 1e6

        first = batch(3000)
        batch(25000)
        last = batch(3000)
        check(f"a question to the name server costs {last:.0f} us after 31,000 questions against {first:.0f} us for the first 3,000", last < first * 1.6 + 20 and not dns.asked, f"{first} {last}")
    finally:
        dns.close()

    print("receivers: all checks passed" if all(RESULTS) else "receivers: FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
