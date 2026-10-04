#!/usr/bin/env python3
"""Backup and restore (docs/production.md P2; docs/runbook.md "Backup and restore"): scripts/backup.sh, restore.sh, logcheck.py.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/backup_test.py build/hooks
                                                              (default 127.0.0.1:5432:postgres:hooks)
    EVENTS=1500 BACKUPS=24 MEAN_MS=250   # the defaults: sizes of stage B

The database must exist; the test applies sql/schema.sql and EMPTIES `endpoints` and `attempts` (and drops them in stage A).

The contract under test: after a backup, a total loss of the data directory and the tables, and a restore, **no acknowledged
event is lost, and no delivery is repeated beyond what at-least-once allows**. Every expectation is derived from the backup's own
files, read with the reader of tests/chaos.py (not the one in scripts/logcheck.py), never from what the test thinks happened.

  A. stopped, with PostgreSQL
     1. a backup of a running service in `--mode stopped` is refused (exit 3); `--stop-cmd` and `--start-cmd` are run
     2. 150 events; endpoint 0 delivered all of them, endpoint 1 only the first 50 (its receiver refuses the rest): the backup holds
        exactly that state, the tables and the sequence `endpoint_ids`
     3. total loss: the data directory is deleted and the three tables dropped; restore creates them again, equal row for row
        (the sequence keeps its value: an endpoint id is never given twice)
     4. the restored service: every acknowledged event is there byte for byte, endpoint 1 now gets 51 to 150 **once each**, endpoint 0
        gets **nothing it already had**, a new event is id 151 and is delivered to both
  B. online, under load and kill -9 as a power cut (the model is tests/chaos.py), backups taken all the time
     5. every backup exits 0 and verifies; every event acknowledged before the backup began is in it, byte for byte
     6. a sample of the backups (always the first, the last, and every one that raced a torn write) is restored and started: its
        events are all there, a **new event is delivered** (the restored service is not stalled), each event is delivered once to each
        endpoint that did not have it at the time of the backup and **never** to one that did, and the next id follows the last
     7. the live service itself lost nothing and delivered every acknowledged event to both endpoints
  C. refusals
     8. a backup with one flipped byte, and a pair with an events.seg older than its delivery.seg (checksums valid), are refused by
        restore.sh (exit 4) and nothing is written; a non-empty --dir is refused (exit 3) unless --force, which moves the old files
        aside; the online mode refuses --stop-cmd; a log that ends in a torn record (a copy caught mid-write, a power cut) is cut by
        backup.sh and restores; a log with damage in the middle is refused rather than silently shortened
     9. (information, not a gate) what a service does with the refused pair if it is given it anyway
"""
import base64
import http.server
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 128
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.environ.get("SCRIPTS", os.path.join(ROOT, "scripts"))   # a directory of (mutated) copies, to see that the test notices
BACKUP = os.path.join(SCRIPTS, "backup.sh")
RESTORE = os.path.join(SCRIPTS, "restore.sh")
LOGCHECK = os.path.join(SCRIPTS, "logcheck.py")
PG = os.environ.get("HOOKS_PG", "127.0.0.1:5432:postgres:hooks").split(":")
PG_HOST, PG_PORT, PG_USER, PG_DB = PG[0], PG[1], PG[2], PG[3]
PG_PASSWORD = os.environ.get("HOOKS_PG_PASSWORD", "")
ENV = dict(os.environ, PGPASSWORD=PG_PASSWORD) if PG_PASSWORD else dict(os.environ)
EVENTS = int(os.environ.get("EVENTS", "1500"))
BACKUPS = int(os.environ.get("BACKUPS", "24"))
MEAN_MS = int(os.environ.get("MEAN_MS", "250"))
FAILS = []
WORK = tempfile.mkdtemp(prefix="hooks-backup-test-")


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


def pg_flags():
    """For the scripts: a password is never an argument to them (PGPASSWORD, which `run` passes in the environment)."""
    return ["--pg-host", PG_HOST, "--pg-port", PG_PORT, "--pg-user", PG_USER, "--pg-database", PG_DB]


def svc_pg_flags():
    """For the service."""
    return pg_flags() + (["--pg-password", PG_PASSWORD] if PG_PASSWORD else [])


def psql(sql, check_ok=True):
    out = subprocess.run(["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, env=ENV)
    if out.returncode != 0 and check_ok:
        raise RuntimeError(out.stderr)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def apply_schema():
    subprocess.run(["psql", "-q", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-f",
                    os.path.join(ROOT, "sql", "schema.sql")], check=True, capture_output=True, env=ENV)


def secret():
    return "whsec_" + base64.b64encode(os.urandom(24)).decode()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=ENV, **kw)


# ---- receivers -------------------------------------------------------------------------------------------------------------

class Receiver:
    """Records every request as (event id, status answered). `gate_open = False` answers 500 to everything; `flaky` answers the
    first request for each event with 500."""

    def __init__(self, flaky=False):
        self.flaky, self.gate_open = flaky, True
        self.hits, self.tries, self.lock = [], {}, threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                eid = int(self.headers["webhook-id"][4:])
                with outer.lock:
                    n = outer.tries[eid] = outer.tries.get(eid, 0) + 1
                    status = 204 if outer.gate_open and not (outer.flaky and n == 1) else 500
                    outer.hits.append((eid, status))
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def delivered(self, since=0):
        with self.lock:
            return [e for e, s in self.hits[since:] if s == 204]

    def mark(self):
        with self.lock:
            return len(self.hits)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---- the service -----------------------------------------------------------------------------------------------------------

class Svc(chaos.Service):
    def __init__(self, datadir, args, power_loss=False):
        super().__init__(chaos.free_port(), datadir)
        self.args, self.power_loss = list(args), power_loss

    def start(self):
        with self.lock:
            env = dict(ENV)
            if self.power_loss and chaos.POWER_LOSS:
                env["LD_PRELOAD"] = os.path.abspath(chaos.SHIM)
            self.proc = subprocess.Popen([BIN, "--port", str(self.port), "--dir", self.datadir, "--allow-private-hosts", "1", *self.args],
                                         stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env)
            self.starts += 1
            line = self.proc.stderr.readline().decode().strip()
            threading.Thread(target=self.proc.wait, daemon=True).start()   # reap: `kill -0` must see a stopped process as gone
            if line != "listening":
                raise RuntimeError("the service did not start: " + line)

    def kill(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(chaos.signal.SIGKILL)
                self.proc.wait()
            if self.power_loss and chaos.POWER_LOSS:
                self.power_cut()

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read())


def post_event(svc, n, timeout=5.0):
    status, data = chaos.post(svc.port, json.dumps({"type": "backup.test", "n": n}).encode(), timeout=timeout)
    return status, data


def wait_for(cond, secs, step=0.02):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


# ---- reading a backup, independently of scripts/logcheck.py ----------------------------------------------------------------

def read_events(directory):
    data = open(os.path.join(directory, "events.seg"), "rb").read()
    records, end = chaos.read_log(data)
    return {ms: dict(pairs)[b"event"] for ms, pairs in records}, len(data) - end


def final_sets(directory, endpoints=(0, 1)):
    """{endpoint: set of event ids that are final} from delivery.seg: delivered or dead, and everything below a `created` start."""
    path = os.path.join(directory, "delivery.seg")
    data = open(path, "rb").read() if os.path.exists(path) else b""
    records, _ = chaos.read_log(data)
    final = {e: set() for e in endpoints}
    for _seq, pairs in records:
        kind, e, event, attempts, _next = struct.unpack("<5q", pairs[0][1])
        if e not in final:
            continue
        if kind in (1, 3):
            final[e].add(event)
        elif kind == 10:
            final[e] = set(range(1, attempts + 1))
        elif kind == 11:
            final[e] = set()
    return final


def sha_resign(directory):
    names = [n for n in ("events.seg", "delivery.seg", "MANIFEST", "endpoints.conf", "hooks.pgdump") if os.path.exists(os.path.join(directory, n))]
    out = run(["sha256sum", "--", *names], cwd=directory)
    open(os.path.join(directory, "SHA256SUMS"), "w").write(out.stdout)


def tree_files(d):
    return sorted(n for n in os.listdir(d) if not n.startswith("."))


# ---- stage A ---------------------------------------------------------------------------------------------------------------

def stage_a():
    print("== A. stopped, with PostgreSQL", flush=True)
    apply_schema()
    psql("truncate endpoints, attempts")
    r0, r1 = Receiver(), Receiver()
    s0, s1 = secret(), secret()
    psql(f"insert into endpoints values (0, '127.0.0.1', {r0.port}, '{s0}'), (1, '127.0.0.1', {r1.port}, '{s1}')")
    for _ in range(3):
        psql("select nextval('endpoint_ids')")        # as three POST /endpoints would have: the sequence is state worth restoring
    seq_before = psql("select last_value, is_called from endpoint_ids")
    data = os.path.join(WORK, "a-data")
    os.makedirs(data)
    svc = Svc(data, ["--schedule", "1000,1000,1000,1000,1000,1000,1000,1000", "--deadline-ms", "800", *svc_pg_flags()])
    svc.start()
    acked = {}
    for n in range(150):
        if n == 50:
            assert wait_for(lambda: len(r1.delivered()) == 50, 10), "endpoint 1 did not get the first 50"
            r1.gate_open = False      # from event 51 on, endpoint 1 refuses: those stay pending, retried every second
        status, body = post_event(svc, n)
        assert status == 202, (status, body)
        acked[json.loads(body)["id"]] = json.dumps({"type": "backup.test", "n": n}).encode()
    assert wait_for(lambda: len(r0.delivered()) == 150, 20), "endpoint 0 did not get all 150"
    assert wait_for(lambda: svc.get("/stats")["delivered"] == 200, 20), "the outcomes were not all recorded"
    time.sleep(0.4)

    # 1. the service is running: stopped mode refuses
    out = run([BACKUP, "--dir", data, "--out", os.path.join(WORK, "a-refused"), "--mode", "stopped"])
    check("1. --mode stopped on a running service is refused (exit 3) and says so", out.returncode == 3 and "still has" in out.stderr, out.stderr)
    check("1. ... and kept nothing", not any(not n.startswith(".") for n in os.listdir(os.path.join(WORK, "a-refused"))) , str(os.listdir(os.path.join(WORK, "a-refused"))))
    out = run([BACKUP, "--dir", data, "--out", WORK, "--mode", "online", "--stop-cmd", "true"])
    check("8. --mode online refuses --stop-cmd (exit 2)", out.returncode == 2, out.stderr)

    # 2. stop it with --stop-cmd and back up
    marker = os.path.join(WORK, "start-cmd-ran")
    pid = svc.proc.pid
    stop = f"kill -TERM {pid}; for i in $(seq 200); do kill -0 {pid} 2>/dev/null || exit 0; sleep 0.05; done; exit 1"
    out = run([BACKUP, "--dir", data, "--out", os.path.join(WORK, "a-backups"), "--mode", "stopped", "--stop-cmd", stop,
               "--start-cmd", f"touch {marker}", *pg_flags()])
    check("2. the backup succeeds (stopped, --stop-cmd, pg_dump)", out.returncode == 0, out.stderr)
    check("2. --start-cmd ran, after the copy", os.path.exists(marker))
    bk = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
    check("2. the backup is a directory of events.seg delivery.seg MANIFEST SHA256SUMS hooks.pgdump",
          tree_files(bk) == ["MANIFEST", "SHA256SUMS", "delivery.seg", "events.seg", "hooks.pgdump"] if bk else False, str(tree_files(bk) if bk else ""))
    check("2. the backup directory is 0700 and holds no group or world bits", bk != "" and (os.stat(bk).st_mode & 0o077) == 0 and
          all((os.stat(os.path.join(bk, n)).st_mode & 0o077) == 0 for n in tree_files(bk)))
    bk_events, torn = read_events(bk)
    fin = final_sets(bk)
    check("2. the backup holds all 150 acknowledged events byte for byte", bk_events == acked and torn == 0, f"{len(bk_events)} events, torn {torn}")
    check("2. ... endpoint 0 final for 1..150, endpoint 1 final for 1..50 (the state the test built)",
          fin[0] == set(range(1, 151)) and fin[1] == set(range(1, 51)), f"{len(fin[0])} {len(fin[1])}")
    attempts_before = int(psql("select count(*) from attempts")[0][0])
    endpoints_before = psql("select id, host, port, secret from endpoints order by id")
    check("2. the history has rows to restore", attempts_before >= 200, str(attempts_before))
    svc_stats_before = None  # the service is stopped; its numbers are in the logs

    # 3. total loss
    shutil.rmtree(data)
    psql("drop table attempts, endpoints")
    psql("drop sequence endpoint_ids")
    os.makedirs(data)
    out = run([RESTORE, "--backup", bk, "--dir", data, *pg_flags()])
    check("3. restore succeeds into the empty directory and the empty database", out.returncode == 0, out.stderr)
    check("3. the logs are the backup's, byte for byte",
          all(open(os.path.join(data, n), "rb").read() == open(os.path.join(bk, n), "rb").read() for n in ("events.seg", "delivery.seg")))
    check("3. the endpoints table is back, row for row (secrets included)",
          psql("select id, host, port, secret from endpoints order by id", check_ok=False) == endpoints_before)
    check("3. the attempts table is back with every row", psql("select count(*) from attempts", check_ok=False) == [(str(attempts_before),)])
    seq_after = psql("select last_value, is_called from endpoint_ids", check_ok=False)       # [] if the sequence is not there
    check("3. the sequence endpoint_ids has its value: the next id is not one already given", seq_after == seq_before, str(seq_after))

    # 4. start it and watch
    r1.gate_open = True
    m0, m1 = r0.mark(), r1.mark()
    svc2 = Svc(data, ["--schedule", "100,200,400", "--deadline-ms", "800", *svc_pg_flags()])
    svc2.start()
    bad = [i for i, body in acked.items() if json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc2.port}/events/{i}", timeout=5).read())["event"] !=
           json.loads(body)]
    check("4. every acknowledged event is served back with the body it was sent", not bad, str(bad[:5]))
    status, body = post_event(svc2, 1000)
    check("4. a new event is id 151 (the numbering continues)", status == 202 and json.loads(body)["id"] == 151, str((status, body)))
    ok = wait_for(lambda: set(r1.delivered(m1)) >= set(range(51, 152)) and 151 in r0.delivered(m0), 20)
    time.sleep(0.6)       # a repeat would show by now
    got0, got1 = r0.delivered(m0), r1.delivered(m1)
    check("4. endpoint 1 gets 51 to 151 and nothing else", ok and sorted(got1) == list(range(51, 152)), f"{len(got1)} {sorted(set(got1) ^ set(range(51, 152)))[:8]}")
    check("4. endpoint 0 gets only the new event: nothing it already had is sent again", got0 == [151], str(got0[:10]))
    svc2.stop()
    r0.close()
    r1.close()
    return bk, data


# ---- stage B ---------------------------------------------------------------------------------------------------------------

def stage_b():
    print("== B. online, under load and kill -9 as a power cut", flush=True)
    apply_schema()
    psql("truncate endpoints, attempts")
    b0, b1 = Receiver(), Receiver(flaky=True)
    psql(f"insert into endpoints values (0, '127.0.0.1', {b0.port}, '{secret()}'), (1, '127.0.0.1', {b1.port}, '{secret()}')")
    data = os.path.join(WORK, "b-data")
    os.makedirs(data)
    svc = Svc(data, ["--schedule", "100,200,400", "--deadline-ms", "800", *svc_pg_flags()], power_loss=True)
    svc.start()
    acked, lock = {}, threading.Lock()
    done = threading.Event()
    kills = [0]
    rng = random.Random(13)
    backups = []          # (dir, acked before, kills before, kills after)
    failures = []
    outdir = os.path.join(WORK, "b-backups")

    def chaos_loop():
        while not done.is_set():
            time.sleep(rng.expovariate(1000.0 / MEAN_MS))
            if done.is_set():
                break
            svc.kill()
            kills[0] += 1
            time.sleep(rng.uniform(0.0, 0.05))
            svc.start()

    def worker(first, step):
        for n in range(first, EVENTS, step):
            body = json.dumps({"type": "backup.test", "n": n}).encode()
            while True:
                try:
                    status, data_ = chaos.post(svc.port, body)
                except (OSError, chaos.http.client.HTTPException):
                    time.sleep(0.01)
                    continue
                if status == 202:
                    with lock:
                        acked[json.loads(data_)["id"]] = json.dumps({"type": "backup.test", "n": n}).encode()
                    break
                time.sleep(0.01)

    def backup_loop():
        while not done.is_set() and len(backups) < BACKUPS:
            time.sleep(rng.uniform(0.0, 0.15))
            with lock:
                before = dict(acked)
            k0 = kills[0]
            out = run([BACKUP, "--dir", data, "--out", outdir, "--mode", "online", *pg_flags()])
            if out.returncode != 0:
                failures.append(f"backup exit {out.returncode}: {out.stderr.strip()[-300:]}")
                continue
            backups.append((out.stdout.strip().splitlines()[-1], before, k0, kills[0]))

    chaos_t = threading.Thread(target=chaos_loop, daemon=True)
    workers = [threading.Thread(target=worker, args=(i, 4)) for i in range(4)]
    backer = threading.Thread(target=backup_loop)
    started = time.time()
    chaos_t.start()
    backer.start()
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    done.set()
    chaos_t.join()
    backer.join()
    if not svc.alive():
        svc.start()
    # one more backup with the service up and nothing killing it, after the last acknowledgement
    with lock:
        before = dict(acked)
    out = run([BACKUP, "--dir", data, "--out", outdir, "--mode", "online", *pg_flags()])
    if out.returncode == 0:
        backups.append((out.stdout.strip().splitlines()[-1], before, kills[0], kills[0]))
    else:
        failures.append(f"final backup exit {out.returncode}: {out.stderr.strip()[-300:]}")
    print(f"   {EVENTS} events, {kills[0]} kills as power cuts, {svc.starts} starts, {len(backups)} online backups in {time.time() - started:.1f}s", flush=True)

    # 5.
    check("5. every online backup exited 0", not failures and len(backups) >= 5, "; ".join(failures[:3]) + f" ({len(backups)} backups)")
    lost, wrong, torn_cut, overlapped, ahead = [], [], 0, 0, 0
    for bk, before, k0, k1 in backups:
        ev, torn = read_events(bk)
        manifest = dict(line.split("=", 1) for line in open(os.path.join(bk, "MANIFEST")).read().splitlines() if "=" in line)
        torn_cut += int(manifest["torn_bytes_cut_delivery"]) + int(manifest["torn_bytes_cut_events"])
        overlapped += k1 > k0
        lost += [(bk, i) for i in before if i not in ev]
        wrong += [(bk, i) for i, body in before.items() if i in ev and ev[i] != body]
        if torn:
            wrong.append((bk, "torn tail kept"))
        v = run(["python3", LOGCHECK, "check", bk, "--kv"])
        if v.returncode != 0:
            wrong.append((bk, "logcheck: " + v.stderr))
        if int(manifest["delivery_max_event_ref"]) > int(manifest["events_last_id"]):
            ahead += 1
    check(f"5. all {len(backups)} backups hold every event acknowledged before they began, byte for byte, and no torn tail",
          not lost and not wrong, f"lost {lost[:3]} wrong {wrong[:3]}")
    check("5. in none of them does delivery.seg refer to an event events.seg lacks", ahead == 0, str(ahead))
    print(f"   {overlapped} backups overlapped a kill; {torn_cut} bytes of torn tail were cut from copies taken mid-write", flush=True)

    # 6. restore a sample and start it
    sample = {backups[0][0], backups[-1][0]}
    for bk, _b, _k0, _k1 in backups:
        m = dict(line.split("=", 1) for line in open(os.path.join(bk, "MANIFEST")).read().splitlines() if "=" in line)
        if int(m["torn_bytes_cut_delivery"]) + int(m["torn_bytes_cut_events"]) > 0:
            sample.add(bk)
    rest = [b for b, *_ in backups if b not in sample]
    sample |= set(rest[:: max(1, len(rest) // 5)][:5])
    ordered = [b for b, *_ in backups if b in sample]
    for bk in ordered:
        before = next(b for p, b, *_ in backups if p == bk)
        verify_restore(bk, before)

    # 7. the live service
    ids_ok = wait_for(lambda: len(set(b0.delivered())) >= len(acked) and len(set(b1.delivered())) >= len(acked), 30)
    missing0 = sorted(set(acked) - set(b0.delivered()))[:5]
    missing1 = sorted(set(acked) - set(b1.delivered()))[:5]
    ev, _ = read_events(data)
    check("7. the live service: every acknowledged event is in its log, and delivered to both endpoints at least once",
          ids_ok and all(ev.get(i) == body for i, body in acked.items()), f"missing {missing0} {missing1}")
    svc.kill()
    b0.close()
    b1.close()
    return backups


def verify_restore(bk, before):
    name = os.path.basename(bk)
    d = os.path.join(WORK, "restored-" + name)
    out = run([RESTORE, "--backup", bk, "--dir", d])
    if out.returncode != 0:
        check(f"6. {name}: restore", False, out.stderr)
        return
    events, _ = read_events(bk)
    fin = final_sets(bk)
    last = len(events)
    r0, r1 = Receiver(), Receiver(flaky=True)
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {r0.port} {secret()}\n1 127.0.0.1 {r1.port} {secret()}\n")
    svc = Svc(d, ["--schedule", "100,200,400", "--deadline-ms", "800"])
    try:
        svc.start()
        bad = []
        for i, body in before.items():
            got = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/{i}", timeout=5).read())["event"]
            if got != json.loads(body):
                bad.append(i)
        status, body = post_event(svc, 99999)
        new_id = json.loads(body)["id"] if status == 202 else None
        want = [set(range(1, last + 1)) - fin[0] | {last + 1}, set(range(1, last + 1)) - fin[1] | {last + 1}]
        ok = wait_for(lambda: set(r0.delivered()) >= want[0] and set(r1.delivered()) >= want[1], 20)
        time.sleep(0.5)
        g0, g1 = sorted(r0.delivered()), sorted(r1.delivered())
        check(f"6. {name} ({last} events, final {len(fin[0])}/{len(fin[1])}): events served back intact, next id {last + 1}",
              not bad and new_id == last + 1, f"bad {bad[:3]} new_id {new_id}")
        check(f"6. {name}: the new event is delivered to both endpoints; each other event exactly once to the endpoints that lacked it, never to one that had it",
              ok and g0 == sorted(want[0]) and g1 == sorted(want[1]),
              f"endpoint0 diff {sorted(set(g0) ^ want[0])[:6]} dup {len(g0) - len(set(g0))}; endpoint1 diff {sorted(set(g1) ^ want[1])[:6]} dup {len(g1) - len(set(g1))}")
    finally:
        svc.stop()
        r0.close()
        r1.close()
        shutil.rmtree(d, ignore_errors=True)


# ---- stage C ---------------------------------------------------------------------------------------------------------------

def stage_c(backups, a_backup, a_data):
    print("== C. refusals", flush=True)
    last = backups[-1][0]
    # 8a. a flipped byte
    bad = os.path.join(WORK, "c-flipped")
    shutil.copytree(last, bad)
    p = os.path.join(bad, "events.seg")
    raw = bytearray(open(p, "rb").read())
    raw[len(raw) // 2] ^= 1
    open(p, "wb").write(raw)
    d = os.path.join(WORK, "c-dir1")
    out = run([RESTORE, "--backup", bad, "--dir", d])
    check("8. a backup with one flipped byte is refused (exit 4, a checksum) and writes nothing",
          out.returncode == 4 and "checksum" in out.stderr and not os.path.exists(os.path.join(d, "events.seg")), f"{out.returncode} {out.stderr}")
    # 8b. an events.seg older than the delivery.seg: valid checksums, so only the cross-check can see it
    first_ev, _ = read_events(backups[0][0])
    last_ev, _ = read_events(last)
    mix = os.path.join(WORK, "c-mixed")
    shutil.copytree(last, mix)
    src = os.path.join(backups[0][0], "events.seg")
    cut = len(first_ev)
    ref = int(dict(line.split("=", 1) for line in open(os.path.join(last, "MANIFEST")).read().splitlines() if "=" in line)["delivery_max_event_ref"])
    if cut >= ref:       # nothing was delivered between the first and the last backup: cut at a record boundary instead
        cut = max(1, ref // 2)
        data = open(os.path.join(last, "events.seg"), "rb").read()
        at = 0
        for _ in range(cut):
            (n,) = struct.unpack_from("<I", data, at)
            at += 4 + n
        open(os.path.join(mix, "events.seg"), "wb").write(data[:at])
    else:
        shutil.copy(src, os.path.join(mix, "events.seg"))
    sha_resign(mix)
    d = os.path.join(WORK, "c-dir2")
    out = run([RESTORE, "--backup", mix, "--dir", d])
    check(f"8. an events.seg of {cut} events beside a delivery.seg that refers to event {ref} is refused (exit 4) and writes nothing",
          out.returncode == 4 and "refers to event" in out.stderr and not os.path.exists(os.path.join(d, "events.seg")), f"{out.returncode} {out.stderr}")
    # 8c. the destination
    d = os.path.join(WORK, "c-dir3")
    os.makedirs(d)
    open(os.path.join(d, "events.seg"), "wb").write(b"old")
    out = run([RESTORE, "--backup", last, "--dir", d])
    check("8. a --dir that already has a log is refused (exit 3) and left alone", out.returncode == 3 and open(os.path.join(d, "events.seg"), "rb").read() == b"old", out.stderr)
    out = run([RESTORE, "--backup", last, "--dir", d, "--force"])
    aside = [n for n in os.listdir(d) if n.startswith("pre-restore-")]
    check("8. --force moves the old files aside instead of deleting them",
          out.returncode == 0 and len(aside) == 1 and open(os.path.join(d, aside[0], "events.seg"), "rb").read() == b"old" and
          open(os.path.join(d, "events.seg"), "rb").read() == open(os.path.join(last, "events.seg"), "rb").read(), out.stderr)
    # 8e. a torn tail, and damage in the middle
    torn_dir = os.path.join(WORK, "c-torn")
    os.makedirs(torn_dir)
    good_ev = open(os.path.join(last, "events.seg"), "rb").read()
    good_dl = open(os.path.join(last, "delivery.seg"), "rb").read()
    open(os.path.join(torn_dir, "events.seg"), "wb").write(good_ev + good_ev[:37])       # half of the first record, as a copy mid-write has
    open(os.path.join(torn_dir, "delivery.seg"), "wb").write(good_dl + bytes(11))      # zeros: a page that never reached the disk
    out = run([BACKUP, "--dir", torn_dir, "--out", os.path.join(WORK, "c-torn-out"), "--mode", "online"])
    tb = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
    check("8. a torn tail on each log is cut from the copy (37 and 11 bytes), and what is left is the log up to it",
          out.returncode == 0 and open(os.path.join(tb, "events.seg"), "rb").read() == good_ev and open(os.path.join(tb, "delivery.seg"), "rb").read() == good_dl
          and "torn_bytes_cut_events=37" in open(os.path.join(tb, "MANIFEST")).read() and "torn_bytes_cut_delivery=11" in open(os.path.join(tb, "MANIFEST")).read(),
          out.stderr)
    out = run([RESTORE, "--backup", tb, "--dir", os.path.join(WORK, "c-torn-restored")]) if tb else out
    check("8. ... and that backup restores", out.returncode == 0, out.stderr)
    dmg = bytearray(good_ev)
    dmg[len(dmg) // 2] ^= 0xFF
    dmg_dir = os.path.join(WORK, "c-damaged")
    os.makedirs(dmg_dir)
    open(os.path.join(dmg_dir, "events.seg"), "wb").write(dmg)
    shutil.copy(os.path.join(last, "delivery.seg"), os.path.join(dmg_dir, "delivery.seg"))
    out = run([BACKUP, "--dir", dmg_dir, "--out", os.path.join(WORK, "c-damaged-out"), "--mode", "online"])
    kept = [n for n in os.listdir(os.path.join(WORK, "c-damaged-out")) if not n.startswith(".")]
    check("8. a log with a byte flipped in the middle is refused (exit 4) and no backup is kept: it is not shortened silently",
          out.returncode == 4 and not kept and "damage" in out.stderr, f"{out.returncode} {kept} {out.stderr}")
    # 8d. a service that has the log open
    live = Svc(os.path.join(WORK, "c-live"), [])
    os.makedirs(live.datadir)
    live.start()
    out = run([RESTORE, "--backup", last, "--dir", live.datadir, "--force"])
    check("8. a --dir the service has open is refused (exit 3)", out.returncode == 3 and "open" in out.stderr, f"{out.returncode} {out.stderr}")
    live.stop()
    # 9. information: the refused pair, given to a service anyway
    r0 = Receiver()
    d = os.path.join(WORK, "c-hazard")
    os.makedirs(d)
    for n in ("events.seg", "delivery.seg"):
        shutil.copy(os.path.join(mix, n), os.path.join(d, n))
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {r0.port} {secret()}\n")
    hz = Svc(d, ["--schedule", "100,200"])
    try:
        hz.start()
        status, body = post_event(hz, 424242)
        new_id = json.loads(body)["id"] if status == 202 else None
        got = wait_for(lambda: new_id in r0.delivered(), 3)
        print(f"INFO 9. the refused pair given to a service anyway: it acknowledged event {new_id} and "
              f"{'DELIVERED it' if got else 'NEVER DELIVERED it (the hazard restore.sh refuses to create)'}", flush=True)
    except RuntimeError as e:
        print(f"INFO 9. the refused pair given to a service anyway: it refused to start ({e}): the service has a guard now", flush=True)
    finally:
        hz.stop()
        r0.close()


def main():
    try:
        a_bk, a_data = stage_a()
        backups = stage_b()
        stage_c(backups, a_bk, a_data)
    finally:
        keep = os.environ.get("KEEP")
        if keep or FAILS:
            print(f"(kept {WORK})")
        else:
            shutil.rmtree(WORK, ignore_errors=True)
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        for f in FAILS:
            print("  FAIL:", f)
        return 1
    print("PASS: no acknowledged event lost across backup, total loss and restore; no delivery repeated beyond at-least-once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
