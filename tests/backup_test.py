#!/usr/bin/env python3
"""Backup and restore (docs/production.md P2; docs/runbook.md "Backup and restore"): scripts/backup.sh, restore.sh, logcheck.py.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/backup_test.py build/hooks
                                                              (default 127.0.0.1:5432:postgres:hooks)
    EVENTS=1500 BACKUPS=24 MEAN_MS=250   # the defaults: sizes of stage B
    D_EVENTS=1800                        # stage D

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
     9. the two hazards restore.sh refuses, given to the service itself: it refuses both before it listens, with a status of its own (18 for the pair, 19 for damage in the
        middle), a message, and the files exactly as they were. (They were information here: the service silently stalled, or silently cut 500 events to 250.)
  D. retention (docs/retention.md): a service that rolls its events log in 256 KiB segments, drops what is final everywhere after 1.2 s and
     replaces its outcomes log at 64 KiB, under kill -9 as a power cut, with online backups taken all the time
     13. every backup exits 0; each holds, byte for byte, every event acknowledged before it began, or the event was dropped and was final at both
         endpoints in the backup's own delivery.seg; ids are dense from the first retained, no torn tail, the pair consistent
     14. a sample (the first, a middle one, the last, and some that had dropped segments) is restored and started with retention off: what is
         retained is served, what was dropped answers 404, a new event takes the next id (even when every event had been dropped), each retained
         event not final in the backup is delivered once to the endpoint that lacked it, none that was final is delivered again
  E. 15. compact.lock held by someone else (what a backup is): the service drops nothing and replaces nothing, counts the deferral, still takes
         events; a backup waits for the lock and finishes when it is let go; the service resumes
     16. a directory of the previous format (no headers, one events.seg) is backed up as it is, restored, and runs: the numbering continues
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
import urllib.error
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

def event_files(directory):
    """The events segments of a backup (the MANIFEST lists them) or of a data directory (events.first, then up while the files exist)."""
    mf = os.path.join(directory, "MANIFEST")
    if os.path.exists(mf):
        for line in open(mf).read().splitlines():
            if line.startswith("events_files="):
                return line.split("=", 1)[1].split()
    first = 0
    if os.path.exists(os.path.join(directory, "events.first")):
        first = int(open(os.path.join(directory, "events.first")).read().strip() or 0)
    names, k = [], first
    while os.path.exists(os.path.join(directory, "events.seg" if k == 0 else f"events-{k}.seg")):
        names.append("events.seg" if k == 0 else f"events-{k}.seg")
        k += 1
    return names or ["events.seg"]


def read_events(directory):
    """{event id: body} over every segment, and the bytes of torn tail after the last record of the last segment."""
    events, torn = {}, 0
    names = event_files(directory)
    for i, name in enumerate(names):
        data = open(os.path.join(directory, name), "rb").read()
        records, end = chaos.read_log(data)
        events.update({ms: dict(pairs)[b"event"] for ms, pairs in records})
        if i == len(names) - 1:
            torn = len(data) - end
    return events, torn


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
    names = [n for n in (*event_files(directory), "events.first", "delivery.seg", "MANIFEST", "endpoints.conf", "hooks.pgdump") if os.path.exists(os.path.join(directory, n))]
    out = run(["sha256sum", "--", *names], cwd=directory)
    open(os.path.join(directory, "SHA256SUMS"), "w").write(out.stdout)


def tree_files(d):
    return sorted(n for n in os.listdir(d) if not n.startswith("."))


# ---- stage A ---------------------------------------------------------------------------------------------------------------

def stage_a():
    print("== A. stopped, with PostgreSQL", flush=True)
    apply_schema()
    psql("truncate endpoints, attempts")
    psql("truncate schedules restart identity")
    # Two schedules, disabled so that they add no events to the counts below: the rows (and the identity sequence behind `id`) are
    # state worth restoring.
    psql("insert into schedules (expr, event_type, body, enabled, created_at, base, last_fired, next_fire) values "
         "('0 3 * * *', 'nightly', '{\"a\":1}', false, 1000, 1000, 0, 0), ('*/5 * * * *', 'tick', '{}', false, 2000, 2000, 4242, 4500)")
    r0, r1 = Receiver(), Receiver()
    s0, s1 = secret(), secret()
    psql(f"insert into endpoints values (0, '127.0.0.1', {r0.port}, '{s0}'), (1, '127.0.0.1', {r1.port}, '{s1}')")
    # the columns of design section 35 are state worth restoring too: a list that wants everything, a header, an expired previous secret
    psql(f"update endpoints set types = '*', headers = 'X-Trace:abc%20d', secret_old = '{secret()}', secret_old_until = 1 where id = 0")
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
    endpoints_before = psql("select id, host, port, secret, types, headers, secret_old, secret_old_until from endpoints order by id")
    schedules_before = psql("select id, expr, event_type, body, enabled, created_at, base, last_fired, next_fire from schedules order by id")
    check("2. there are schedules to restore", len(schedules_before) == 2, str(schedules_before))
    check("2. the history has rows to restore", attempts_before >= 200, str(attempts_before))
    svc_stats_before = None  # the service is stopped; its numbers are in the logs

    # 3. total loss
    shutil.rmtree(data)
    psql("drop table attempts, endpoints, schedules")
    psql("drop sequence endpoint_ids")
    os.makedirs(data)
    out = run([RESTORE, "--backup", bk, "--dir", data, *pg_flags()])
    check("3. restore succeeds into the empty directory and the empty database", out.returncode == 0, out.stderr)
    check("3. the logs are the backup's, byte for byte",
          all(open(os.path.join(data, n), "rb").read() == open(os.path.join(bk, n), "rb").read() for n in ("events.seg", "delivery.seg")))
    check("3. the endpoints table is back, row for row (secrets, event types, headers and the previous secret included)",
          psql("select id, host, port, secret, types, headers, secret_old, secret_old_until from endpoints order by id", check_ok=False) == endpoints_before)
    check("3. the schedules table is back, row for row, with where each one is",
          psql("select id, expr, event_type, body, enabled, created_at, base, last_fired, next_fire from schedules order by id", check_ok=False) == schedules_before)
    new_id = psql("with i as (insert into schedules (expr, event_type, created_at, base, enabled) values ('0 0 1 1 *', 'x', 3000, 3000, false) "
                  "returning id) select id from i", check_ok=False)
    check("3. the identity of schedules continues (the next id is not one already given)", new_id == [("3",)], str(new_id))
    psql("delete from schedules where id = 3", check_ok=False)
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
    # every event of this test has a type, so every record has the `typ` pair (design section 35.1): logcheck knows the shape and counts them
    last_report = json.loads(run(["python3", LOGCHECK, "check", backups[-1][0]]).stdout)["events"]
    check("5. logcheck reads the typed records of a backup: every record has its type, and none is a shape it does not know",
          last_report["records"] > 0 and last_report["typed"] == last_report["records"], str(last_report))
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
    # a power cut that zeroes the last 64 bytes of what it kept spoils the ends of two small records at once: still a torn tail (found by stage D)
    two = os.path.join(WORK, "c-torn2")
    os.makedirs(two)
    spoiled = bytearray(good_dl[-154:-77] + good_dl[-77:-57])      # one record and 20 bytes of the next: 97 bytes
    spoiled[-64:] = bytes(64)
    open(os.path.join(two, "events.seg"), "wb").write(good_ev)
    open(os.path.join(two, "delivery.seg"), "wb").write(good_dl + bytes(spoiled))
    out = run([BACKUP, "--dir", two, "--out", os.path.join(WORK, "c-torn2-out"), "--mode", "online"])
    tb2 = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
    check("8. two records spoiled by one zeroed 64 bytes are a torn tail too: cut (97 bytes), and the log up to them is the backup",
          out.returncode == 0 and open(os.path.join(tb2, "delivery.seg"), "rb").read() == good_dl and "torn_bytes_cut_delivery=97" in open(os.path.join(tb2, "MANIFEST")).read(),
          out.stderr)
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
    # 9. the hazards restore.sh refuses, given to the service anyway: it refuses them too (docs/production.md 0.5), each with its own status, before it listens
    #    and with the files exactly as they were. (They used to be information: "the service silently stalls", "250 of 500 events, nothing printed".)
    def service_on(directory, label):
        port = chaos.free_port()
        before = {n: open(os.path.join(directory, n), "rb").read() for n in sorted(os.listdir(directory))}
        p = subprocess.run([BIN, "--port", str(port), "--dir", directory, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=30)
        after = {n: open(os.path.join(directory, n), "rb").read() for n in sorted(os.listdir(directory))}
        return p, before == after

    d = os.path.join(WORK, "c-hazard")
    os.makedirs(d)
    for n in ("events.seg", "delivery.seg"):
        shutil.copy(os.path.join(mix, n), os.path.join(d, n))
    p, untouched = service_on(d, "pair")
    check("9. the refused pair (an events.seg older than its delivery.seg), given to the service: it refuses, status 18, says which event, never says listening, and changes nothing",
          p.returncode == 18 and "delivery.seg refers to event" in p.stderr and "listening" not in p.stderr and untouched, f"{p.returncode} {p.stderr!r}")
    d = os.path.join(WORK, "c-hazard2")
    shutil.copytree(dmg_dir, d)
    events_before = open(os.path.join(d, "events.seg"), "rb").read()
    p, untouched = service_on(d, "damage")
    check("9. a log with one flipped byte in the middle (the 500 events that became 250 with nothing printed), given to the service: status 19, and it says where, how many bytes and how many intact records; nothing is cut",
          p.returncode == 19 and "damage in the middle" in p.stderr and "intact record" in p.stderr and "listening" not in p.stderr and untouched and
          open(os.path.join(d, "events.seg"), "rb").read() == events_before, f"{p.returncode} {p.stderr!r}")


# ---- stage D: retention (docs/retention.md) ---------------------------------------------------------------------------------

RETAIN = ["--retention-ms", "1200", "--window-ms", "300", "--segment-bytes", "262144", "--delivery-log-bytes", "65536"]
D_EVENTS = int(os.environ.get("D_EVENTS", "1800"))


def manifest_of(bk):
    return dict(line.split("=", 1) for line in open(os.path.join(bk, "MANIFEST")).read().splitlines() if "=" in line)


def write_conf(d, receivers):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i, r in enumerate(receivers):
            f.write(f"{i} 127.0.0.1 {r.port} {secret()}\n")


def verify_restore_retained(bk, before, fin, ev):
    """Restore a backup of a service that drops what it has delivered, start it with retention off, and see: what is retained is served, what
    was dropped was final everywhere in the backup, a new event takes the next id, every retained event not final at an endpoint is
    delivered there once and none that was final is delivered again."""
    name = os.path.basename(bk)
    d = os.path.join(WORK, "restored-" + name)
    out = run([RESTORE, "--backup", bk, "--dir", d])
    if out.returncode != 0:
        check(f"14. {name}: restore", False, out.stderr)
        return
    m = manifest_of(bk)
    first, last = int(m["events_first_id"]), int(m["events_last_id"])
    r0, r1 = Receiver(), Receiver(flaky=True)
    write_conf(d, [r0, r1])
    svc = Svc(d, ["--schedule", "100,200,400", "--deadline-ms", "800", "--retention-days", "0"])
    try:
        svc.start()
        bad, gone = [], []
        for i, body in before.items():
            try:
                got = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{svc.port}/events/{i}", timeout=5).read())["event"]
                if got != json.loads(body):
                    bad.append(i)
            except urllib.error.HTTPError as e:
                if e.code == 404 and i < first:
                    gone.append(i)
                else:
                    bad.append((i, e.code))
        status, body = post_event(svc, 99999)
        new_id = json.loads(body)["id"] if status == 202 else None
        retained = set(range(first, last + 1))
        want = [(retained - fin[0]) | {last + 1}, (retained - fin[1]) | {last + 1}]
        ok = wait_for(lambda: set(r0.delivered()) >= want[0] and set(r1.delivered()) >= want[1], 20)
        time.sleep(0.5)
        g0, g1 = sorted(r0.delivered()), sorted(r1.delivered())
        check(f"14. {name} (events {first}..{last}, {len(gone)} acknowledged ones dropped): retained ones served intact, dropped ones 404, next id {last + 1}",
              not bad and new_id == last + 1, f"bad {bad[:3]} new_id {new_id}")
        check(f"14. {name}: the new event reaches both endpoints; each retained event not final in the backup exactly once, none that was final again",
              ok and g0 == sorted(want[0]) and g1 == sorted(want[1]),
              f"endpoint0 diff {sorted(set(g0) ^ want[0])[:6]} dup {len(g0) - len(set(g0))}; endpoint1 diff {sorted(set(g1) ^ want[1])[:6]} dup {len(g1) - len(set(g1))}")
    finally:
        svc.stop()
        r0.close()
        r1.close()
        shutil.rmtree(d, ignore_errors=True)


def stage_d():
    print("== D. retention: online backups while segments are dropped and the outcomes log is replaced, under kill -9 as a power cut", flush=True)
    r0, r1 = Receiver(), Receiver(flaky=True)
    data = os.path.join(WORK, "d-data")
    os.makedirs(data)
    write_conf(data, [r0, r1])
    svc = Svc(data, ["--schedule", "100,200,400", "--deadline-ms", "800", *RETAIN], power_loss=True)
    svc.start()
    acked, lock = {}, threading.Lock()
    done = threading.Event()
    kills = [0]
    rng = random.Random(29)
    backups, failures = [], []
    outdir = os.path.join(WORK, "d-backups")
    pad = "x" * 1400

    def chaos_loop():
        while not done.is_set():
            time.sleep(rng.expovariate(1000.0 / 400))
            if done.is_set():
                break
            svc.kill()
            kills[0] += 1
            time.sleep(rng.uniform(0.0, 0.05))
            svc.start()

    def worker(first, step):
        for n in range(first, D_EVENTS, step):
            body = json.dumps({"type": "backup.test", "n": n, "pad": pad}).encode()
            while True:
                try:
                    status, data_ = chaos.post(svc.port, body)
                except (OSError, chaos.http.client.HTTPException):
                    time.sleep(0.01)
                    continue
                if status == 202:
                    with lock:
                        acked[json.loads(data_)["id"]] = body
                    break
                time.sleep(0.01)
            time.sleep(0.004)

    def backup_loop():
        while not done.is_set():
            time.sleep(rng.uniform(0.0, 0.25))
            with lock:
                before = dict(acked)
            k0 = kills[0]
            out = run([BACKUP, "--dir", data, "--out", outdir, "--mode", "online"])
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
    # let the service drop what it can, then one more backup after the last acknowledgement, with nothing killing it
    time.sleep(3.0)
    with lock:
        before = dict(acked)
    out = run([BACKUP, "--dir", data, "--out", outdir, "--mode", "online"])
    if out.returncode == 0:
        backups.append((out.stdout.strip().splitlines()[-1], before, kills[0], kills[0]))
    else:
        failures.append(f"final backup exit {out.returncode}: {out.stderr.strip()[-300:]}")
    st = svc.get("/stats")
    print(f"   {D_EVENTS} events of 1.4 KB, {kills[0]} kills as power cuts, {svc.starts} starts, {len(backups)} online backups in {time.time() - started:.1f}s; "
          f"the live service: {st['segments_dropped']} segments dropped, {st['segments_sealed']} sealed, {st['snapshots']} snapshots, events {st['events_first_id']}..{st['events_last_id']}, "
          f"{st['maintenance_lock_skips']} steps deferred by the backup's lock", flush=True)
    check("13. retention was at work: segments dropped, the outcomes log replaced, while the backups ran", st["segments_dropped"] >= 2 and st["snapshots"] >= 1, str(st))
    check("13. every online backup exited 0", not failures and len(backups) >= 5, "; ".join(failures[:3]) + f" ({len(backups)} backups)")

    existing = [b for b in backups if os.path.isdir(b[0])]
    lost, wrong, dropped_seen, multi = [], [], 0, 0
    infos = {}
    for bk, before, k0, k1 in existing:
        ev, torn = read_events(bk)
        m = manifest_of(bk)
        fin = final_sets(bk)
        ids = sorted(ev)
        if ids and ids != list(range(ids[0], ids[-1] + 1)):
            wrong.append((bk, "ids not contiguous"))
        if torn:
            wrong.append((bk, "torn tail kept"))
        first = int(m["events_first_id"])
        multi += len(event_files(bk)) > 1
        for i, body in before.items():
            if i in ev:
                if ev[i] != body:
                    wrong.append((bk, i))
            elif i < first and i in fin[0] and i in fin[1]:
                dropped_seen += 1
            else:
                lost.append((bk, i))
        v = run(["python3", LOGCHECK, "check", bk, "--kv"])
        if v.returncode != 0:
            wrong.append((bk, "logcheck: " + v.stderr))
        infos[bk] = (fin, ev)
    check(f"13. all {len(existing)} backups: every event acknowledged before they began is there byte for byte, or was dropped after being final at both endpoints "
          f"({dropped_seen} such, {multi} backups of more than one segment); ids dense, no torn tail, the pair consistent",
          not lost and not wrong and dropped_seen > 0, f"lost {lost[:3]} wrong {wrong[:3]} dropped_seen {dropped_seen}")

    sample = [existing[0][0], existing[len(existing) // 2][0], existing[-1][0]]
    seen_dropped = [b for b, *_ in existing if int(manifest_of(b)["events_first_id"]) > 1]
    sample += seen_dropped[:: max(1, len(seen_dropped) // 2)][:3]
    for bk in dict.fromkeys(sample):
        before = next(b for p, b, *_ in existing if p == bk)
        fin, ev = infos[bk]
        verify_restore_retained(bk, before, fin, ev)
    svc.kill()
    r0.close()
    r1.close()
    return existing[-1][0]


def stage_e():
    print("== E. the lock, and a directory of the previous format", flush=True)
    import fcntl
    r0 = Receiver()
    d = os.path.join(WORK, "e-data")
    os.makedirs(d)
    write_conf(d, [r0])
    svc = Svc(d, ["--schedule", "100,200", "--deadline-ms", "800", "--retention-ms", "300", "--window-ms", "100", "--segment-bytes", "262144",
                  "--delivery-log-bytes", "65536"])
    svc.start()
    pad = "y" * 1400
    for n in range(300):
        chaos.post(svc.port, json.dumps({"type": "e", "n": n, "pad": pad}).encode())
    wait_for(lambda: svc.get("/stats")["delivered"] >= 300, 20)
    # a holder of compact.lock (what a backup is): the service defers its steps, and goes on serving
    fd = os.open(os.path.join(d, "compact.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    time.sleep(0.5)
    a = svc.get("/stats")
    for n in range(300, 1500):
        chaos.post(svc.port, json.dumps({"type": "e", "n": n, "pad": pad}).encode())
    time.sleep(2.5)
    b = svc.get("/stats")
    check("15. while compact.lock is held, the service drops no segment and replaces no log, and says it deferred",
          b["segments_dropped"] == a["segments_dropped"] and b["snapshots"] == a["snapshots"] and b["maintenance_lock_skips"] > a["maintenance_lock_skips"], f"{a} {b}")
    check("15. ... and still takes events (rolling only adds a file after the ones a backup lists)", b["events_last_id"] == a["events_last_id"] + 1200 and b["segments_sealed"] >= a["segments_sealed"], str((a, b)))
    t0 = time.time()
    bk_proc = subprocess.Popen([BACKUP, "--dir", d, "--out", os.path.join(WORK, "e-backups"), "--mode", "online"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
    time.sleep(1.0)
    check("15. a backup waits for the lock instead of copying under a step", bk_proc.poll() is None)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    out, err = bk_proc.communicate(timeout=60)
    check("15. ... and finishes when the lock is let go", bk_proc.returncode == 0, err)
    check("15. the service resumes its steps once the lock is free", wait_for(lambda: svc.get("/stats")["segments_dropped"] > b["segments_dropped"], 20), str(svc.get("/stats")))
    svc.stop()
    r0.close()

    # a directory of format 1 (no headers, one events.seg): backed up, restored, started
    d = os.path.join(WORK, "e-legacy")
    os.makedirs(d)
    body = lambda i: json.dumps({"type": "legacy", "n": i}).encode()   # noqa: E731
    rec = lambda ms, pairs, seq=0: (lambda b: struct.pack("<II", 4 + len(b), chaos.crc32c(b)) + b)(          # noqa: E731
        struct.pack("<QQI", ms, seq, len(pairs)) + b"".join(struct.pack("<I", len(k)) + k + struct.pack("<I", len(v)) + v for k, v in pairs))
    with open(os.path.join(d, "events.seg"), "wb") as f:
        for i in range(1, 9):
            f.write(rec(i, [(b"event", body(i))]))
    with open(os.path.join(d, "delivery.seg"), "wb") as f:
        f.write(rec(1, [(b"o", struct.pack("<5q", 10, 0, 0, 0, 0))], 1))
        for i in range(1, 6):
            f.write(rec(i + 1, [(b"o", struct.pack("<5q", 1, 0, i, 1, 0))], i + 1))
    out = run([BACKUP, "--dir", d, "--out", os.path.join(WORK, "e-legacy-out"), "--mode", "online"])
    bk = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
    check("16. a directory of the previous format (no headers) is backed up: one file, no header added, MANIFEST names it",
          out.returncode == 0 and "events_files=events.seg" in open(os.path.join(bk, "MANIFEST")).read() and
          open(os.path.join(bk, "events.seg"), "rb").read() == open(os.path.join(d, "events.seg"), "rb").read(), out.stderr)
    d2 = os.path.join(WORK, "e-legacy-restored")
    out = run([RESTORE, "--backup", bk, "--dir", d2]) if bk else out
    r = Receiver()
    write_conf(d2, [r])
    check("16. ... and restored", out.returncode == 0, out.stderr)
    s2 = Svc(d2, ["--schedule", "100,200", "--retention-days", "0"])
    s2.start()
    got = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{s2.port}/events/3", timeout=5).read())["event"]
    status, nb = post_event(s2, 77)
    ok = wait_for(lambda: set(r.delivered()) >= {6, 7, 8, 9}, 15)
    time.sleep(0.4)
    s2.stop()
    check("16. the restored previous-format directory runs: event 3 served, the next id is 9, events 6 to 9 delivered once, 1 to 5 not again",
          got == {"type": "legacy", "n": 3} and status == 202 and json.loads(nb)["id"] == 9 and ok and sorted(r.delivered()) == [6, 7, 8, 9],
          f"{got} {status} {nb} {sorted(r.delivered())}")
    r.close()


def main():
    try:
        a_bk, a_data = stage_a()
        backups = stage_b()
        stage_c(backups, a_bk, a_data)
        stage_d()
        stage_e()
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
