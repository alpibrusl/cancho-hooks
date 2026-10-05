#!/usr/bin/env python3
"""The soak test of lexsys-hooks (docs/soak.md): a long run of the real service under a steady stream, a mix of endpoints, and a seeded schedule of faults, with memory,
descriptors, disk and the loop watched, and every acknowledged event followed to every receiver that should have it by two ledgers the service has no hand in.

    python3 scripts/soak/soak.py --binary build/hooks --hours 24 --seed 1 --out soak-out [--rate 40 --burst-rate 120 --endpoints 12]
    python3 scripts/soak/soak.py --resume soak-out                  # continue after the machine or the container was restarted
    python3 scripts/soak/soak.py --selftest [--selftest-mutants all]  # the harness against itself: clean must pass, a faulty one must fail
    python3 scripts/soak/soak.py --calibrate --binary build/hooks    # the rate this endpoint mix sustains (what --rate is a fraction of)
    python3 scripts/soak/report.py soak-out                          # report.md and report.json from the files of a run

Set HOOKS_PG=host:port:user:database (and HOOKS_PG_PASSWORD) or give --pg. The database is **truncated** (endpoints, attempts, schedules): give it one of its own.
Exit status: 0 the run passed, 1 it failed (verdict.json says which invariant), 2 usage or setup, 3 inconclusive (the harness or the host was the limit).
Standard library only.
"""
import argparse
import atexit
import hashlib
import http.client
import json
import os
import platform
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402,F401  (puts tests/ on the path)
from common import ACK, CLASSES, EP_MAX, ROOT, plan_endpoints, share  # noqa: E402
import faults  # noqa: E402
import monitor  # noqa: E402
import series  # noqa: E402
import workers  # noqa: E402
from ledger import Verifier, Violations  # noqa: E402
from poster import Poster  # noqa: E402
from service import Svc, power_cut  # noqa: E402

ADMIN, INGEST, READ = "soak-admin-token-0001", "soak-ingest-token-0001", "soak-read-token-0001"
SERVICE_SETTINGS = ["--allow-private-hosts", "1", "--schedule", "500,1000,2000,4000,8000,16000,16000,16000", "--deadline-ms", "2000", "--retention-ms", "180000", "--window-ms", "120000",
                    "--segment-bytes", "1048576", "--delivery-log-bytes", "262144", "--cron-seconds", "1", "--retry-jitter", "10", "--stop-deadline-ms", "5000"]
PROBLEM_PREFIX = {"A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "F", "I": "I", "J": "J", "K": "K", "L": "L"}


class EpInfo:
    def __init__(self, label, idx, cls, types, port, tls=False, secrets=None, params=None):
        self.label, self.idx, self.cls, self.types, self.port, self.tls = label, idx, cls, list(types), port, tls
        self.secrets = secrets or []
        self.params = params or {}
        self.svc_id = None
        self.c0 = None
        self.active = True
        self.cursor = 0

    def spec(self):
        return {"idx": self.idx, "label": self.label, "cls": self.cls, "port": self.port, "tls": self.tls, "secrets": self.secrets, "params": self.params}

    def state(self):
        return {"idx": self.idx, "cls": self.cls, "types": self.types, "port": self.port, "tls": self.tls, "secrets": self.secrets, "svc_id": self.svc_id, "c0": self.c0,
                "active": self.active, "params": self.params}


def alloc_port(taken, lo=24000, hi=31999):
    """A port below the range the kernel hands out to outgoing connections, so that a receiver that closes its listener for a few seconds can have it back."""
    for p in range(lo, hi):
        if p in taken:
            continue
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))
        except OSError:
            continue
        finally:
            s.close()
        taken.add(p)
        return p
    raise RuntimeError("no free port")


def machine():
    cpu = ""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem = ""
    try:
        mem = [l for l in open("/proc/meminfo") if l.startswith("MemTotal")][0].split(":")[1].strip()
    except (OSError, IndexError):
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "memory": mem, "kernel": platform.release(), "python": platform.python_version(), "loadavg_at_start": os.getloadavg()}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Run:
    def __init__(self, a):
        self.args = a
        self.out = os.path.abspath(a.resume or a.out)
        self.datadir = os.path.join(self.out, "data")
        self.seed = a.seed
        self.rng = random.Random(a.seed)
        host, port, user, db = a.pg.split(":")
        self.pg = {"host": host, "port": int(port), "user": user, "db": db}
        self.stop, self.stop_all = threading.Event(), threading.Event()
        self.vlock = threading.RLock()
        self.fault_lock, self.backup_lock, self.logl, self.state_lock = threading.Lock(), threading.Lock(), threading.Lock(), threading.Lock()
        self.counts = Counter()
        self.excused = []
        self.eps, self.label_of = {}, {}
        self.next_idx = 0
        self.taken = set()
        self.bursting = False
        self.quiet_replays = False
        self.last_stats, self.last_stats_t = {}, 0.0
        self.recv_stats = {}
        self.t0 = time.time()
        self.elapsed_before = 0.0
        self.duration = a.duration_s or a.hours * 3600.0
        self.quiet_s = self.duration * a.quiet_fraction
        if self.duration >= 7200:
            self.quiet_s = max(self.quiet_s, 1800.0)
        self.receivers = self.probe = self.proxy = self.poster = self.checker = self.watcher = self.svc = None
        self.final = {}
        self.phase = 0               # 0 the run, 1 the drain, 2 after the clean stop
        self.service_cpus = self.harness_cpus = None
        self.threads = []
        self.tmpfs = None
        self.fatal = None
        self.early = False
        self.unexpected_exits = []
        self.chaos_log = None
        self.violations = Violations(sink=self.write_violation)
        self.verifier = Verifier(self.seed, violations=self.violations)
        self.viol_f = None
        self.t_first_violation = None
        self.resumed = False
        self.ctl_port = None
        self.dns = None
        self.cron_ids = {}
        self.saved_fault_counts = {}
        self.faults = None

    # ---- small things
    def elapsed(self):
        return self.elapsed_before + time.time() - self.t0

    @property
    def quiet(self):
        return self.elapsed() >= self.duration - self.quiet_s

    def path(self, *p):
        return os.path.join(self.out, *p)

    def log(self, _event, **kw):
        rec = {"t": round(time.time(), 3), "el": round(self.elapsed(), 1), "kind": _event, **kw}
        with self.logl:
            if self.chaos_log is None:
                self.chaos_log = open(self.path("chaos.jsonl"), "a", buffering=1)
            self.chaos_log.write(json.dumps(rec, default=str) + "\n")

    def write_violation(self, tag, detail):
        if self.viol_f is None:
            self.viol_f = open(self.path("violations.jsonl"), "a", buffering=1)
        if self.t_first_violation is None and not tag.startswith(("P_idem", "F_disabled")):
            self.t_first_violation = time.time()
        self.viol_f.write(json.dumps({"t": round(time.time(), 3), "tag": tag, **detail}, default=str) + "\n")

    def violate(self, tag, **kw):
        with self.vlock:
            self.violations.add(tag, **kw)

    def pg_env(self):
        env = dict(os.environ)
        if os.environ.get("HOOKS_PG_PASSWORD"):
            env["PGPASSWORD"] = os.environ["HOOKS_PG_PASSWORD"]
        return env

    def psql_cmd(self, *extra):
        return ["psql", "-h", self.pg["host"], "-p", str(self.pg["port"]), "-U", self.pg["user"], "-d", self.pg["db"], "-v", "ON_ERROR_STOP=1", *extra]

    def psql_rows(self, sql):
        out = subprocess.run(self.psql_cmd("-At", "-F", "|", "-c", sql), capture_output=True, text=True, env=self.pg_env(), timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr)
        return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]

    def http(self, method, path, body, token, timeout, raw=False):
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
            h = {"Authorization": "Bearer " + token}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            text = r.read().decode(errors="replace")
            c.close()
        except (OSError, http.client.HTTPException):
            return 0, None
        if raw:
            return r.status, text
        try:
            return r.status, (json.loads(text) if text else None)
        except ValueError:
            return r.status, text

    def read(self, path, timeout=3.0, raw=False):
        return self.http("GET", path, None, READ, timeout, raw)

    def admin(self, method, path, body=None, timeout=8.0):
        return self.http(method, path, body, ADMIN, timeout)

    def recv(self, cmd, timeout=5.0):
        try:
            s = socket.create_connection(("127.0.0.1", self.ctl_port), timeout=timeout)
            s.sendall((json.dumps(cmd) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            s.close()
            return json.loads(buf)
        except (OSError, ValueError):
            return {"error": "no answer"}

    def type_of(self, ev):
        from common import type_name
        with self.vlock:
            e = self.verifier.ev.get(ev)
        return type_name(e[0]) if e and e[0] != 255 else None

    # ---- endpoints
    def new_endpoint(self, label, cls, types, secrets, tls=False, params=None):
        with self.vlock:
            idx = self.next_idx
            self.next_idx += 1
        port = alloc_port(self.taken)
        ep = EpInfo(label, idx, cls, types, port, tls, secrets, params)
        self.eps[label] = ep
        with self.vlock:
            self.verifier.add_endpoint(idx, label, cls, types, c0=None, params=dict(CLASSES.get(cls, ([], {}))[1], **(params or {})))
        self.recv({"op": "add", "spec": ep.spec()})
        return ep

    def drop_endpoint(self, ep, keep_receiver=False):
        ep.active = False
        if not keep_receiver:
            self.recv({"op": "remove", "label": ep.label})
        with self.vlock:
            self.verifier.retire(ep.label, time.time())

    # ---- state
    def save_state(self):
        with self.state_lock:
            st = {"t": time.time(), "elapsed": self.elapsed(), "inc": self.svc.inc if self.svc else 0, "service_pid": self.svc.pid if self.svc else None,
                  "receivers_pid": self.receivers.pid if self.receivers else None, "probe_pid": self.probe.pid if self.probe else None, "n_next": self.poster.n_next if self.poster else 1,
                  "next_idx": self.next_idx, "port": self.port, "ctl_port": self.ctl_port, "cron": self.cron_ids, "dns": self.dns,
                  "eps": {k: v.state() for k, v in self.eps.items()}, "counts": dict(self.counts), "faults_count": dict(self.faults.count) if self.faults else {}, "taken": sorted(self.taken),
                  "settled": {k: self.verifier.by_label[k].settled for k in self.eps if k in self.verifier.by_label}, "kills": self.verifier.kills[-50:]}
            if self.poster:
                st["poster_counts"] = dict(self.poster.counts)
                st["bytes_ingested"] = self.poster.bytes_ingested
            st["excused"] = list(self.excused)
            st["unexpected_exits"] = list(self.unexpected_exits)
            st["unexpected"] = list(self.svc.unexpected[-200:]) if self.svc else []
            with self.vlock:
                st["notes"] = self.verifier.export_notes()
            tmp = self.path("state.json.tmp")
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self.path("state.json"))

    # ---- the service
    def service_flags(self):
        a = self.args
        f = list(SERVICE_SETTINGS) + ["--pg-host", self.pg["host"], "--pg-port", str(self.proxy.port), "--pg-user", self.pg["user"], "--pg-database", self.pg["db"],
                                      "--admin-token", ADMIN, "--ingest-token", INGEST, "--read-token", READ]
        if os.environ.get("HOOKS_PG_PASSWORD"):
            f += ["--pg-password", os.environ["HOOKS_PG_PASSWORD"]]
        if self.dns:
            f += ["--dns-server", f"127.0.0.1:{self.dns['port']}", "--tls-ca-file", self.dns["ca"]]
        return f + list(a.service_arg)

    def start_service(self, extra=(), timeout=90):
        ok, secs = self.svc.start(extra, timeout=timeout)
        self.log("service-start", inc=self.svc.inc, ok=ok, seconds=round(secs, 2), extra=list(extra))
        if not ok:
            self.fatal = f"the service did not start (exit {self.svc.proc.poll()}): " + " | ".join(self.svc.lines[-5:])
            self.violate("L_start_failed", exit=self.svc.proc.poll(), stderr=self.svc.lines[-5:])
            self.stop.set()
        return ok

    def restart_service(self, kind, extra=(), down_s=0.0, note=""):
        """Stop the service (`kill9`: SIGKILL and the power cut; `term`: the drain), tell the checker, start it again. Caller holds `fault_lock`."""
        svc, v = self.svc, self.verifier
        if kind == "kill9":
            t, lost = svc.kill9(cut=not self.args.no_power_cut)
            with self.vlock:
                v.note_kill(t)
                v.note_restart(svc.inc + 1, t, "kill")
            self.log("kill9", note=note, cut_bytes=lost)
            self.counts["kills"] += 1
        else:
            t, code, lines = svc.term(self.args.stop_deadline_s + 5)
            m = None
            for l in lines:
                m = re.search(r"hooks: stopped: (\d+) attempts? (?:was|were) still on the wire", l) or m
            on_wire = int(m.group(1)) if m else 0
            with self.vlock:
                v.note_stop(t, on_wire)
                v.note_restart(svc.inc + 1, t, "term")
            self.log("sigterm", exit=code, on_wire=on_wire, seconds=round(time.time() - t, 2), note=note)
            self.counts["stops"] += 1
            if code != 0:
                self.violate("J_stop_exit", exit=code, during="run", lines=lines[-3:])
        if self.args.keep_stop_copies:
            # for a person who wants to look at what a stop left: the data directory as it was between the stop and the start
            dst = self.path("stops", f"{svc.inc:04d}-{kind}")
            shutil.copytree(self.datadir, dst, ignore=shutil.ignore_patterns("*.synced", "ballast"))
        if down_s:
            self.stop.wait(down_s)
        ok = self.start_service(extra)
        t_up = time.time()
        with self.vlock:
            v.note_away(t, t_up, "service")
        self.excused.append((t - 0.5, t_up + 3.0))
        return ok

    def reap(self):
        """The service ended and nobody asked it to: that is a finding (invariant L); start it again and carry on."""
        svc = self.svc
        code = svc.proc.poll()
        t = time.time()
        self.unexpected_exits.append({"t": t, "exit": code, "stderr": svc.lines[-5:]})
        self.violate("L_exit", exit=code, stderr=svc.lines[-5:])
        with self.vlock:
            self.verifier.note_kill(t - 0.2)
            self.verifier.note_restart(svc.inc + 1, t - 0.2, "kill")
        self.log("unexpected-exit", exit=code, stderr=svc.lines[-5:])
        ok = self.start_service()
        with self.vlock:
            self.verifier.note_away(t, time.time(), "service")
        self.excused.append((t - 0.5, time.time() + 3.0))
        return ok

    # ---- setup
    def apply_schema(self):
        subprocess.run(self.psql_cmd("-q", "-f", os.path.join(ROOT, "sql", "schema.sql")), check=True, capture_output=True, env=self.pg_env())

    def start_receivers(self, spec):
        with open(self.path("spec.json"), "w") as f:
            json.dump(spec, f)
        try:
            os.remove(self.path("ctl.port"))
        except OSError:
            pass
        cpus = ",".join(str(c) for c in sorted(self.harness_cpus)) if self.harness_cpus else ""
        self.receivers = subprocess.Popen([sys.executable, os.path.join(HERE, "receivers.py"), "--dir", self.out, "--seed", str(self.seed), "--cpus", cpus],
                                          stdout=open(self.path("receivers.log"), "a"), stderr=subprocess.STDOUT)
        end = time.time() + 20
        while time.time() < end and not os.path.exists(self.path("ctl.port")):
            if self.receivers.poll() is not None:
                raise RuntimeError("the receivers did not start: see receivers.log")
            time.sleep(0.05)
        self.ctl_port = int(open(self.path("ctl.port")).read())

    def start_probe(self):
        cpus = ",".join(str(c) for c in sorted(self.harness_cpus)) if self.harness_cpus else ""
        self.probe = subprocess.Popen([sys.executable, os.path.join(HERE, "probe.py"), str(self.port), self.path("probe.jsonl"), "--cpus", cpus,
                                       "--ctl-cpus", ",".join(str(c) for c in sorted(self.service_cpus or []))], stderr=subprocess.DEVNULL)

    def pin(self):
        a = self.args
        n = os.cpu_count() or 1
        self.service_cpus = self.harness_cpus = None
        if a.no_pin or n < 2:
            return
        if a.service_cpus:
            self.service_cpus = {int(c) for c in a.service_cpus.split(",")}
        else:
            self.service_cpus = {n - 1}
        if a.harness_cpus:
            self.harness_cpus = {int(c) for c in a.harness_cpus.split(",")}
        else:
            self.harness_cpus = set(range(n)) - self.service_cpus
        try:
            os.sched_setaffinity(0, self.harness_cpus)
        except (OSError, AttributeError):
            pass

    def setup(self):
        a = self.args
        self.pin()
        if a.resume:
            return self.setup_resume()
        if os.path.exists(self.out) and os.listdir(self.out) and not a.force:
            raise SystemExit(f"{self.out} is not empty: give --force, or --resume to continue the run that is in it")
        for d in ("data", "ledger", "backups"):
            os.makedirs(self.path(d), exist_ok=True)
        if a.tmpfs_data:
            self.mount_tmpfs(a.tmpfs_data)
        self.apply_schema()
        subprocess.run(self.psql_cmd("-q", "-c", "truncate endpoints, attempts, schedules"), check=True, capture_output=True, env=self.pg_env())
        self.port = alloc_port(self.taken)
        plan = plan_endpoints(a.endpoints)
        spec = {"t0": time.time(), "endpoints": []}
        if any(c == "https" for c, _ in plan):
            import tlskit
            os.makedirs(self.path("pki"), exist_ok=True)
            pki = tlskit.Pki(self.path("pki"))
            cert, key = pki.leaf("hooks.test")
            self.dns = {"port": alloc_port(self.taken), "ca": pki.ca_pem, "name": "hooks.test"}
            spec["tls"] = {"cert": cert, "key": key}
            spec["dns"] = {"name": "hooks.test", "port": self.dns["port"]}
        eps = []
        for i, (cls, types) in enumerate(plan):
            label = f"{cls}{i}"
            params = {}
            ep = EpInfo(label, self.next_idx, cls, types, alloc_port(self.taken), tls=(cls == "https"), secrets=[{"s": workers.new_secret(), "from": None, "to": None}], params=params)
            self.next_idx += 1
            self.eps[label] = ep
            eps.append(ep)
            spec["endpoints"].append(ep.spec())
        self.start_receivers(spec)
        from pgproxy import PgProxy
        self.proxy = PgProxy(self.pg["host"], self.pg["port"])
        self.make_svc()
        for ep in eps:
            self.verifier.add_endpoint(ep.idx, ep.label, ep.cls, ep.types, c0=None, params=dict(CLASSES[ep.cls][1]))
        if not self.start_service():
            raise SystemExit("the service did not start: " + str(self.fatal))
        for ep in eps:
            body = {"host": "127.0.0.1", "port": ep.port, "secret": ep.secrets[0]["s"], "types": ep.types, "from": "now"}
            if ep.cls == "https":
                body = {"url": f"https://hooks.test:{ep.port}", "secret": ep.secrets[0]["s"], "types": ep.types, "from": "now"}
            if ep.cls == "rate":
                ep.params["rate"] = int(2.5 * share(ep.types) * a.rate) + 1
                body["rate"] = ep.params["rate"]
            s, ans = self.admin("POST", "/endpoints", body, timeout=15)
            if s != 201:
                raise SystemExit(f"POST /endpoints for {ep.label} answered {s}: {ans}")
            ep.svc_id, ep.c0 = int(ans["id"]), int(ans["cursor"])
            self.label_of[ep.svc_id] = ep.label
            self.verifier.activate(ep.label, ep.c0, time.time())
        for expr, period in (("* * * * * *", 1), ("*/5 * * * * *", 5))[:a.schedules]:
            s, ans = self.admin("POST", "/schedules", {"expr": expr, "type": "cron.tick"}, timeout=15)
            if s != 201:
                raise SystemExit(f"POST /schedules answered {s}: {ans}")
            self.cron_ids[str(ans["id"])] = period
            self.verifier.cron_period[int(ans["id"])] = period
        self.write_run_json()

    def make_svc(self):
        a = self.args
        shim = None
        if not a.no_power_cut:
            shim = a.shim or os.path.join(os.path.dirname(os.path.abspath(a.binary)), "fsync_shim.so")
            if a.fault == "liar":
                src = os.path.join(HERE, "liar_shim.c")
                shim = self.path("liar_shim.so")
                subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-o", shim, src, "-ldl"], check=True)
            if not os.path.exists(shim):
                print(f"soak: {shim} is not there, so a kill is a process kill and not a power cut (build it: scripts/build.sh)", file=sys.stderr)
                shim = None
        self.shim = shim
        self.svc = Svc(a.binary, self.datadir, self.port, self.service_flags(), self.path("service.log"), shim=shim, cpus=self.service_cpus, rng=random.Random(self.seed + 3))

    def write_run_json(self):
        a = self.args
        info = {"args": {k: v for k, v in vars(a).items()}, "machine": machine(), "binary_sha256": sha256(a.binary), "service_settings": SERVICE_SETTINGS,
                "started": time.time(), "duration_s": self.duration, "quiet_s": self.quiet_s, "pinned": {"service": sorted(self.service_cpus or []), "harness": sorted(self.harness_cpus or [])},
                "shim": self.shim, "ports": {"service": self.port}, "tmpfs": bool(self.tmpfs)}
        try:
            info["commit"] = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            info["pin"] = open(os.path.join(ROOT, "lex-sys.toml")).read().split('lex-sys = "')[1].split('"')[0]
        except (OSError, IndexError):
            pass
        with open(self.path("run.json"), "w") as f:
            json.dump(info, f, indent=1, default=str)

    def mount_tmpfs(self, mb):
        cmd = ["mount", "-t", "tmpfs", "-o", f"size={mb}m", "tmpfs", self.datadir]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"--tmpfs-data: cannot mount a tmpfs on {self.datadir}: {r.stderr.strip()}")
        self.tmpfs = True
        atexit.register(self.umount_tmpfs)

    def umount_tmpfs(self):
        if self.tmpfs:
            cmd = ["umount", self.datadir] if os.geteuid() == 0 else ["sudo", "-n", "umount", self.datadir]
            subprocess.run(cmd, capture_output=True)
            self.tmpfs = False

    def setup_resume(self):
        a = self.args
        st = json.load(open(self.path("state.json")))
        saved = json.load(open(self.path("run.json")))
        self.resumed = True
        was = saved.get("binary_sha256")
        if was and sha256(a.binary) != was and not a.force:
            print(f"the service binary is not the one this run began with (SHA-256 {sha256(a.binary)[:16]}... against {was[:16]}...): a run is of one build; --force to go on anyway", file=sys.stderr)
            raise SystemExit(2)
        self.elapsed_before = st["elapsed"]
        self.port = st["port"]
        self.next_idx = st["next_idx"]
        self.taken = set(st["taken"])
        self.cron_ids = st["cron"]
        self.dns = st.get("dns")
        self.counts.update(st.get("counts", {}))
        self.excused = [tuple(x) for x in st.get("excused", [])]
        self.poster_before = (st.get("poster_counts", {}), st.get("bytes_ingested", 0))
        self.unexpected_exits = list(st.get("unexpected_exits", []))
        # what the run had found before it was interrupted is part of its verdict
        try:
            for line in open(self.path("violations.jsonl")):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                tag = d.pop("tag", "?")
                d.pop("t", None)
                self.violations.count[tag] += 1
                if len(self.violations.examples[tag]) < self.violations.keep:
                    self.violations.examples[tag].append(d)
        except OSError:
            pass
        self.saved_fault_counts = st.get("faults_count", {})
        for pid_key, needle in (("service_pid", os.path.basename(a.binary)), ("receivers_pid", "receivers.py"), ("probe_pid", "probe.py")):
            pid = st.get(pid_key)
            if pid and os.path.exists(f"/proc/{pid}/cmdline"):
                cmd = open(f"/proc/{pid}/cmdline").read()
                if needle in cmd and (pid_key != "service_pid" or self.datadir in cmd):
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.2)
        if a.tmpfs_data and not os.path.ismount(self.datadir):
            os.makedirs(self.datadir, exist_ok=True)
            self.mount_tmpfs(a.tmpfs_data)
        spec = json.load(open(self.path("spec.json")))
        spec["endpoints"] = []
        for label, e in st["eps"].items():
            ep = EpInfo(label, e["idx"], e["cls"], e["types"], e["port"], e["tls"], e["secrets"], e["params"])
            ep.svc_id, ep.c0, ep.active = e["svc_id"], e["c0"], e["active"] and not label.startswith("churn")
            self.eps[label] = ep
            if ep.svc_id is not None:
                self.label_of[ep.svc_id] = label
            if ep.active:
                spec["endpoints"].append(ep.spec())
        self.start_receivers(spec)
        from pgproxy import PgProxy
        self.proxy = PgProxy(self.pg["host"], self.pg["port"])
        self.make_svc()
        self.svc.inc = st["inc"]
        self.svc.unexpected = [tuple(x) for x in st.get("unexpected", [])]
        # the old service, if it was running, was killed just now: the files are as a power cut leaves them
        # when did the old run end? The last heartbeat is up to five seconds before it, and a service that was left running (the harness alone was killed) went on delivering until it was stopped just
        # now: the latest write to either ledger is the best evidence of life
        t_dead = st["t"]
        for f in (os.path.join("ledger", "recv.bin"), "acked.bin"):
            try:
                t_dead = max(t_dead, os.path.getmtime(self.path(f)))
            except OSError:
                pass
        power_cut(self.datadir, random.Random(self.seed + 5)) if self.shim else None
        for label, ep in self.eps.items():
            self.verifier.add_endpoint(ep.idx, label, ep.cls, ep.types, c0=ep.c0, params=dict(CLASSES.get(ep.cls, ([], {}))[1], **ep.params))
            self.verifier.by_label[label].settled = st["settled"].get(label, ep.c0 or 0)
            if not ep.active:
                self.verifier.retire(label, t_dead)
        for k, p in self.cron_ids.items():
            self.verifier.cron_period[int(k)] = p
        self.verifier.import_notes(st.get("notes", {}))
        self.notes_since_heartbeat(st["t"])
        # rebuild the open window from the ledgers' tail, without counting again what was judged before
        self.checker = monitor.Checker(self)
        self.checker.seek_end_minus(self.verifier.keep_s + 60)
        keep = self.verifier.v
        self.verifier.v = Violations()
        with self.vlock:
            self.checker.drain()
        self.verifier.v = keep
        self.verifier.note_kill(t_dead)
        self.verifier.note_restart(self.svc.inc + 1, t_dead, "kill")
        self.log("resume", from_elapsed=st["elapsed"], dead_since=t_dead)
        self.counts["resumes"] += 1
        self.counts["kills"] += 1
        if not self.start_service():
            raise SystemExit("the service did not start on the resumed data directory: " + str(self.fatal))
        self.verifier.note_away(t_dead, time.time(), "service")
        self.excused.append((t_dead - 0.5, time.time() + 3.0))
        for label, ep in self.eps.items():
            if label.startswith("churn") and ep.svc_id is not None and st["eps"][label]["active"]:
                self.admin("DELETE", f"/endpoints/{ep.svc_id}", None)
        # the numbers (and so the idempotency keys) of events posted since the last heartbeat are in the poster's ledger, not in state.json: go past them
        recs, _ = common.read_records(self.path("acked.bin"), max(0, os.path.getsize(self.path("acked.bin")) - 200000 * ACK.size), ACK) if os.path.exists(self.path("acked.bin")) else ([], 0)
        self.poster_start_n = max([st["n_next"]] + [r[2] + 1 for r in recs]) + 100
        # a request the old poster had sent when it died, and whose answer it did not write down, may be an event the service has: its number is not "an event nobody posted"
        self.verifier.posted_n.update(range(max([r[2] for r in recs], default=0) + 1, self.poster_start_n))

    def notes_since_heartbeat(self, t_state):
        """What the harness did between the last write of `state.json` and the end of the previous harness is in `chaos.jsonl`: the replays it asked for (their deliveries are not repeats) and the
        windows in which it made an endpoint fail."""
        try:
            lines = open(self.path("chaos.jsonl")).read().splitlines()[-4000:]
        except OSError:
            return
        for l in lines:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("t", 0) < t_state - 1.0:
                continue
            if r.get("kind") == "replay" and r.get("status") in (202, 0, 504):
                m = re.match(r"/events/(\d+)/replay", r.get("path", ""))
                for lb in r.get("labels", []):
                    if m and lb in self.verifier.by_label:
                        self.verifier.note_replay(lb, int(m.group(1)))
            elif r.get("kind") == "sick" and r.get("label") in self.verifier.by_label:
                self.verifier.note_window(r["label"], r["t"], float(r["until"]))

    # ---- the run
    def start_workload(self):
        a = self.args
        self.poster = Poster(self.port, INGEST, self.seed, self.path("acked.bin"), self.violations, a.rate, workers=a.workers, start_n=getattr(self, "poster_start_n", 1))
        self.poster.counts.update(getattr(self, "poster_before", ({}, 0))[0])      # a resumed run goes on counting where the one before it stopped
        self.poster.bytes_ingested = getattr(self, "poster_before", ({}, 0))[1]
        if self.checker is None:
            self.checker = monitor.Checker(self)
        self.watcher = monitor.Watcher(self)
        self.start_probe()
        self.checker.start()
        self.watcher.start()
        self.poster.start()
        if a.fault:
            f = {"lose": {"lose": 1 / 120.0}, "dup": {"dup": 1 / 60.0}}.get(a.fault)
            if f:
                self.recv({"op": "fault", "faults": f})
                self.log("fault-injected", mutant=a.fault, faults=f)
        threading.Thread(target=self.heartbeat, daemon=True).start()
        if not a.calibrate:
            self.threads = [workers.Replayer(self), workers.Enabler(self)] + [workers.Churn(self, k) for k in range(a.churn)] + [self.make_faults()]
        else:
            self.threads = [workers.Enabler(self)]
        for t in self.threads:
            t.start()

    def make_faults(self):
        self.faults = faults.Faults(self)
        return self.faults

    def heartbeat(self):
        while not self.stop_all.wait(5.0):
            try:
                self.save_state()
            except Exception as ex:  # noqa: BLE001
                self.log("state_error", error=repr(ex))

    def run_loop(self):
        a = self.args
        last_print = 0.0
        while self.elapsed() < self.duration and not self.stop.is_set() and not self.early:
            time.sleep(0.5)
            if not self.fault_lock.locked() and not self.svc.alive() and not self.fatal:
                if self.fault_lock.acquire(blocking=False):
                    try:
                        if not self.svc.alive():
                            self.reap()
                    finally:
                        self.fault_lock.release()
            if a.stop_after_violation and self.t_first_violation and time.time() - self.t_first_violation > a.stop_after_violation:
                self.log("early-stop", reason="a violation was found and the run is a self-test")
                self.early = True
            if time.time() - last_print > a.progress_s:
                last_print = time.time()
                self.progress()

    def progress(self):
        rows = self.watcher.rows[-1:] if self.watcher and self.watcher.rows else []
        r = rows[0] if rows else {}
        print(f"[{self.elapsed():7.0f}s/{self.duration:.0f}s] inc {self.svc.inc} acked {self.poster.counts['acked']} rate {r.get('ingest_per_s', '')}/s lag_max {r.get('lag_max', '')} rss {r.get('rss_kb', '')} "
              f"fds {r.get('fds', '')} kills {self.counts['kills']} stops {self.counts['stops']} pg {self.counts['pg_faults']} violations {self.violations.total()} {dict(self.violations.count) if self.violations.total() else ''}",
              flush=True)

    # ---- the end
    def finish(self):
        a = self.args
        v = self.verifier
        self.log("finish", step="stop the workload")
        self.stop.set()
        self.poster.stop(timeout=120)
        for t in self.threads:
            t.join(timeout=100)
        self.proxy.restore()
        if self.fatal:
            # the service cannot be started on its own data directory: there is nothing to drain; what the directory holds is still checked against what was acknowledged
            self.log("finish", step="fatal", why=self.fatal)
            self.final = {"fatal": self.fatal}
            self.check_log_durability()
            self.stop_all.set()
            self.checker.join(5)
            self.watcher.join(5)
            for p in (self.probe, self.receivers):
                if p and p.poll() is None:
                    p.terminate()
            self.save_state()
            return
        for ep in self.eps.values():
            if ep.active:
                self.recv({"op": "mode", "label": ep.label, "mode": "normal", "until": 0})
                if ep.cls == "sick":
                    self.recv({"op": "sick", "label": ep.label, "until": 0})
        if not self.svc.alive():
            self.reap()
        # drain: every endpoint reaches the newest event
        self.log("finish", step="drain")
        self.phase = 1
        end = time.time() + a.drain_s
        last_id = 0
        caught = False
        while time.time() < end:
            s, st = self.read("/stats", timeout=5)
            if s == 200:
                last_id = st["events_last_id"]
            s, eps = self.read("/endpoints", timeout=5)
            if s == 200 and last_id:
                behind = []
                for e in eps:
                    label = self.label_of.get(e["id"])
                    if label is None or not self.eps[label].active:
                        continue
                    if e.get("disabled"):
                        self.admin("POST", f"/endpoints/{e['id']}/enable", None, timeout=5)
                    if e["cursor"] < last_id:
                        behind.append(label)
                for ep in self.eps.values():
                    if ep.cls in ("sick", "dead") and ep.active and ep.label in behind:
                        workers.sweep_dead(self, ep, rounds=3)
                if not behind:
                    caught = True
                    break
            if self.stop_all.wait(2.0):
                break
        self.log("finish", step="drained", caught=caught, last_id=last_id)
        time.sleep(3.0)      # the ledger is read once more after the last cursor
        for ep in self.eps.values():
            if ep.cls in ("sick", "dead") and ep.active:
                workers.sweep_dead(self, ep, rounds=10)
        time.sleep(2.0)
        with self.vlock:
            self.checker.drain()
        s, eps = self.read("/endpoints", timeout=10)
        s2, st = self.read("/stats", timeout=10)
        with self.vlock:
            self.checker.drain()
            cursors = {self.label_of[e["id"]]: e["cursor"] for e in (eps if s == 200 else []) if e["id"] in self.label_of}
            last_id = st["events_last_id"] if s2 == 200 else last_id
            v.finish(last_id, cursors, caught_up=caught or True)
            self.counts["final_last_id"] = last_id
        # the stop: SIGTERM, exit 0, logs that logcheck accepts, and a restart that repeats nothing recorded
        self.log("finish", step="clean stop")
        self.phase = 2
        before = self.violations.count["B_repeat"] + self.violations.count["B_restart_repeat"]
        t, code, lines = self.svc.term(a.stop_deadline_s + 10)
        self.excused.append((t - 0.5, t + 1e9))      # from here the service is stopped, started once to be watched, and stopped: nothing the probe sees counts
        m = None
        for l in lines:
            m = re.search(r"hooks: stopped: (\d+) attempts? (?:was|were) still on the wire", l) or m
        with self.vlock:
            v.note_stop(t, int(m.group(1)) if m else 0)
            v.note_restart(self.svc.inc + 1, t, "term")
        self.final = {"stop_exit": code, "stop_lines": lines[-3:]}
        if code != 0:
            self.violate("J_stop_exit", exit=code, during="end", lines=lines[-3:])
        q = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "logcheck.py"), "check", self.datadir], capture_output=True, text=True)
        self.final["logcheck"] = q.returncode
        if q.returncode != 0:
            self.violate("J_logcheck", out=q.stdout[-600:])
        self.check_log_durability()
        with self.backup_lock:
            workers.run_backup(self, restore=True)
        if self.violations.count["K_backup"] == 0 and self.violations.count["K_restore"] == 0:
            self.counts["final_restore_ok"] = 1
        self.log("finish", step="restart after a clean stop")
        self.svc.inc_before = self.svc.inc
        ok = self.start_service()
        if ok:
            self.stop_all.wait(a.after_stop_s)
            with self.vlock:
                self.checker.drain()
            after = self.violations.count["B_repeat"] + self.violations.count["B_restart_repeat"]
            if after > before:
                self.violate("J_repeat", why="a delivery recorded before the clean stop was made again after the restart", repeats=after - before)
            t, code, lines = self.svc.term(a.stop_deadline_s + 10)
            if code != 0:
                self.violate("J_stop_exit", exit=code, during="end, second stop", lines=lines[-3:])
        self.stop_all.set()
        self.checker.join(5)
        self.watcher.join(5)
        for p in (self.probe, self.receivers):
            if p and p.poll() is None:
                p.terminate()
        self.save_state()

    def check_log_durability(self):
        """Every acknowledged event of the window that retention has not dropped is in the events log, byte for byte what was posted (tests/chaos.py's reader)."""
        chaos = common.chaos_module()
        recs, torn = chaos.read_events(self.datadir)
        by_id = {i: dict(p).get(b"event") for i, p in recs}
        first = recs[0][0] if recs else 0
        missing = wrong = checked = 0
        with self.vlock:
            acked = [(i, e[1]) for i, e in self.verifier.ev.items() if e[4]]
        for i, n in acked:
            if i < first:
                continue
            checked += 1
            body = by_id.get(i)
            if body is None:
                missing += 1
                if missing <= 20:
                    self.violate("A_not_in_log", id=i, n=n)
                continue
            try:
                if json.loads(body).get("n") != n:
                    wrong += 1
                    if wrong <= 20:
                        self.violate("A_not_in_log", id=i, n=n, why="another event is in the log under this id")
            except ValueError:
                wrong += 1
        if missing > 20 or wrong > 20:
            self.violate("A_not_in_log", missing=missing, wrong=wrong)
        self.final["log_check"] = {"checked": checked, "missing": missing, "wrong": wrong, "first_id": first, "torn": torn}

    # ---- the verdict
    def verdict(self):
        a = self.args
        v = self.violations
        rows = []
        try:
            import csv
            rows = list(csv.DictReader(open(self.path("metrics.csv"))))
        except OSError:
            pass
        windows = []
        try:
            for line in open(self.path("probe.jsonl")):
                try:
                    windows.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        p = {"min_tail_s": a.min_tail_s if a.min_tail_s is not None else max(120.0, 0.5 * self.quiet_s), "min_inc_s": a.min_incarnation_s, "retention_s": 180.0, "window_s": 120.0,
             "segment_bytes": 1 << 20, "delivery_log_bytes": 1 << 18, "stall_ms": a.stall_ms, "p999_ms": a.p999_ms}
        results = series.check_all(rows, windows, self.excused, p)
        groups = {"A": "an acknowledged event missing, or an event that is not in the log", "B": "a repeat nothing explains", "C": "a delivery against a filter or of an event older than the endpoint",
                  "D": "a bad, stale or revoked signature, or one signature where two are due", "E": "a cursor that went back or ran ahead", "F": "a dead letter where none may be, or a disabled endpoint",
                  "I": "cron", "J": "the clean stop", "K": "backup and restore", "L": "the service ended or said something it should not", "P": "the poster's own checks",
                  "END": "an endpoint that had not caught up", "U": "a record of an endpoint that is not in the spec"}
        tally = {}
        for tag, n in v.count.items():
            key = "END" if tag.startswith("END") else tag[0]
            tally.setdefault(key, {})[tag] = n
        waived = set(x for x in a.waive.split(",") if x)
        for key, what in groups.items():
            tags = tally.get(key, {})
            left = {t: n for t, n in tags.items() if t not in waived}
            res = {"id": key, "ok": not left, "what": what, "tags": tags, "examples": {t: v.examples[t][:5] for t in tags}}
            if tags and not left:
                res["waived"] = sorted(set(tags) & waived)
            results.append(res)
        un = [x for x in self.svc.unexpected]
        results.append({"id": "L2", "ok": not un, "what": "nothing on standard error that is not in the runbook's list", "lines": un[:10]})
        # is the run valid?
        steady = [float(r["ingest_per_s"]) for r in rows if r.get("ingest_per_s") not in (None, "") and r.get("up") == "1" and r.get("bursting") == "0"
                  and not any(x - 1 <= float(r["t"]) <= y + 1 for x, y in self.excused)]
        ratio = series.median(steady) / a.rate if steady else 0.0
        hcpu = [float(r["harness_cpu_pct"]) for r in rows if r.get("harness_cpu_pct") not in (None, "")]
        load = [float(r["loadavg1"]) for r in rows if r.get("loadavg1") not in (None, "")]
        cores = os.cpu_count() or 1
        over = sum(1 for x in load if x > cores) / len(load) if load else 0.0
        reasons = []
        if ratio < 0.9:
            reasons.append(f"the poster held {ratio * 100:.0f} % of the requested rate (median of the steady windows)")
        if hcpu and sum(hcpu) / len(hcpu) > 60:
            reasons.append(f"the harness used {sum(hcpu) / len(hcpu):.0f} % of a core on average")
        if over > 0.1:
            reasons.append(f"the load average was above the {cores} cores in {over * 100:.0f} % of the samples")
        valid = {"valid": not reasons, "reasons": reasons, "poster_rate_ratio": round(ratio, 3), "harness_cpu_pct_mean": round(sum(hcpu) / len(hcpu), 1) if hcpu else None,
                 "loadavg_over_cores_fraction": round(over, 3), "cores": cores}
        failed = [r["id"] for r in results if r["ok"] is False]
        status = "FAIL" if failed else "PASS"
        if status == "PASS" and reasons and not a.lenient_validity:
            status = "INCONCLUSIVE"
        out = {"verdict": status, "failed": failed, "not_evaluated": [{"id": r["id"], "why": r.get("why")} for r in results if r["ok"] is None], "validity": valid,
               "checks": results, "violations": dict(v.count), "examples": {k: x[:20] for k, x in v.examples.items()}, "summary": self.verifier.summary(), "counts": dict(self.counts),
               "final": getattr(self, "final", {}), "elapsed_s": round(self.elapsed()), "duration_s": self.duration, "seed": self.seed, "unexpected_exits": self.unexpected_exits,
               "poster": dict(self.poster.counts), "resumed": self.resumed, "mutant": a.fault or None, "fatal": self.fatal, "waived": sorted(waived)}
        with open(self.path("verdict.json"), "w") as f:
            json.dump(out, f, indent=1, default=str)
        return out


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary", help="the service (build/hooks)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--duration-s", type=float, default=0.0, help="seconds, for a short run (replaces --hours)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--rate", type=float, default=40.0, help="events a second in the steady phases (what the service sustains between restarts is in docs/capacity.md; what it sustains across them, docs/soak.md section 8)")
    p.add_argument("--burst-rate", type=float, default=120.0, help="events a second in a burst (about the capacity of the mix: see --calibrate)")
    p.add_argument("--endpoints", type=int, default=12, help="long-lived endpoints, the classes of docs/soak.md in order (this and --churn together at most --endpoint-limit)")
    p.add_argument("--endpoint-limit", type=int, default=EP_MAX, help="the most endpoints the service takes (62 in the build the soak was designed on; give the limit of yours)")
    p.add_argument("--churn", type=int, default=3, help="threads that create, change and delete endpoints")
    p.add_argument("--schedules", type=int, default=2, choices=[0, 1, 2], help="cron schedules to make (every second; every fifth second)")
    p.add_argument("--workers", type=int, default=6, help="threads of the poster")
    p.add_argument("--pg", default=os.environ.get("HOOKS_PG", ""), help="host:port:user:database (default $HOOKS_PG); the database is truncated")
    p.add_argument("--out", default="soak-out")
    p.add_argument("--resume", help="continue the run in this directory")
    p.add_argument("--force", action="store_true", help="use an --out that is not empty")
    p.add_argument("--sample-s", type=float, default=10.0)
    p.add_argument("--progress-s", type=float, default=60.0)
    p.add_argument("--chaos", default="all", help="all, or a comma list of: " + ", ".join(faults.ACTIONS))
    p.add_argument("--no-chaos", default="", help="a comma list to leave out")
    p.add_argument("--chaos-scale", type=float, default=0.0, help="multiplies the mean times between faults (default: from the duration; 1 for a day)")
    p.add_argument("--restart-scale", type=float, default=0.0, help="like --chaos-scale, for the faults that restart the service (default: at least 0.5)")
    p.add_argument("--quiet-fraction", type=float, default=0.25, help="the end of the run with no restart of the service (at least 30 min of a run of two hours or more)")
    p.add_argument("--service-cpus", default="")
    p.add_argument("--harness-cpus", default="")
    p.add_argument("--no-pin", action="store_true")
    p.add_argument("--shim", default="", help="the fsync shim of the power cuts (default: fsync_shim.so beside the binary)")
    p.add_argument("--no-power-cut", action="store_true", help="a kill is a process kill only")
    p.add_argument("--pg-restart-cmd", default="", help="a command that stops and starts the PostgreSQL server and returns when it takes connections (adds the `restart` database fault; for a server that is the run's own)")
    p.add_argument("--tmpfs-data", type=int, default=0, metavar="MB", help="make the data directory a tmpfs of this size and add the disk-full fault (needs root or sudo -n)")
    p.add_argument("--service-arg", action="append", default=[], help="an extra argument for the service (repeat)")
    p.add_argument("--keep-stop-copies", action="store_true", help="keep a copy of the data directory after each stop of the service, before it is started again (for looking at a finding)")
    p.add_argument("--stop-deadline-s", type=float, default=5.0)
    p.add_argument("--drain-s", type=float, default=600.0)
    p.add_argument("--after-stop-s", type=float, default=15.0)
    p.add_argument("--min-incarnation-s", type=float, default=1200.0)
    p.add_argument("--min-tail-s", type=float, default=None)
    p.add_argument("--stall-ms", type=float, default=500.0)
    p.add_argument("--p999-ms", type=float, default=50.0)
    p.add_argument("--lenient-validity", action="store_true", help="a run that is not valid is still passed or failed on the invariants (the self-test)")
    p.add_argument("--fault", choices=["lose", "dup", "liar"], help="inject a fault into the harness's own world: the run must FAIL (the self-test)")
    p.add_argument("--waive", default="", help="a comma list of violation tags that do not fail the verdict (they are listed as waived): for looking past a known finding")
    p.add_argument("--stop-after-violation", type=float, default=0.0, help="end the run this many seconds after the first violation (the self-test)")
    p.add_argument("--calibrate", action="store_true", help="measure the rate the mix sustains (no chaos) and write calibration.json")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--selftest-mutants", default="lose", help="which faults the self-test injects: lose, dup, liar, all")
    p.add_argument("--selftest-strict", action="store_true", help="the self-test waives nothing (by default it waives B_restart_repeat, the known finding of docs/soak.md section 8)")
    p.add_argument("--selftest-ledger", action="store_true", help="only the offline test of the checker")
    a = p.parse_args(argv)
    return p, a


def finalize_args(a, p):
    if a.selftest or a.selftest_ledger:
        return
    if not a.binary and not a.resume:
        p.error("--binary is required")
    if a.resume:
        saved = json.load(open(os.path.join(a.resume, "run.json")))["args"]
        keep = {"selftest", "selftest_ledger", "resume", "force", "progress_s"}
        if a.binary:
            keep.add("binary")      # the build may have moved: it is the same one only if its SHA-256 is (checked at the resume)
        if a.pg:
            keep.add("pg")          # the database may be somewhere else (HOOKS_PG is set again by whoever resumes)
        for k, v in saved.items():
            if k not in keep and not (k == "tmpfs_data" and a.tmpfs_data):
                setattr(a, k, v)
        a.resume = a.resume
    if not a.pg:
        p.error("--pg or HOOKS_PG=host:port:user:database is required")
    if a.endpoints < 1 or a.endpoints + a.churn > a.endpoint_limit:
        p.error(f"--endpoints ({a.endpoints}) plus --churn ({a.churn}) must be at least 1 and at most --endpoint-limit ({a.endpoint_limit})")
    a.binary = os.path.abspath(a.binary)
    dur = a.duration_s or a.hours * 3600.0
    if a.chaos_scale <= 0:
        a.chaos_scale = min(1.0, max(0.15, dur / 14400.0))


def run_main(a):
    run = Run(a)
    if a.calibrate:
        a.sample_s = 3600.0
    atexit.register(lambda: [p.kill() for p in (run.receivers, run.probe, run.svc.proc if run.svc else None) if p and p.poll() is None])
    signal.signal(signal.SIGTERM, lambda *_: run.stop.set())
    run.setup()
    run.start_workload()
    print(f"soak: {run.duration:.0f} s, seed {a.seed}, rate {a.rate}/s (burst {a.burst_rate}/s), {a.endpoints} endpoints, out {run.out}; service pinned to {sorted(run.service_cpus or [])}, "
          f"harness to {sorted(run.harness_cpus or [])}", flush=True)
    if a.calibrate:
        run.watcher.rows.clear()
        return calibrate(run)
    try:
        run.run_loop()
    except KeyboardInterrupt:
        run.log("interrupted")
    run.finish()
    out = run.verdict()
    print(f"\nverdict: {out['verdict']}" + (f"  failed: {', '.join(out['failed'])}" if out["failed"] else "") + (f"  (inconclusive: {'; '.join(out['validity']['reasons'])})" if out["verdict"] == "INCONCLUSIVE" else ""))
    for r in out["checks"]:
        mark = {True: "ok  ", False: "FAIL", None: "n/a "}[r["ok"]]
        print(f"  {mark} {r['id']:4} {r['what']}" + (f"   [{r.get('why')}]" if r["ok"] is None else ""))
    run.umount_tmpfs()
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[out["verdict"]]


def calibrate(run):
    """Steps of rising rate with no fault: for each, what was achieved, the worst lag of the endpoints that do not fail on purpose, the CPU of the service and of the harness, and the
    sender's and the delivery's latency. The mix sustains the highest rate at which 95 % of it was achieved, no steady endpoint was more than 500 ids behind, and the receivers' loop
    was never 100 ms late."""
    a = run.args
    steps = []
    rates = [r for r in (25, 50, 100, 200, 300, 400, 600, 800, 1200, 1600) if r >= a.rate / 2] if a.rate else [50, 100, 200, 400, 800]
    best = None
    steady = ("oracle", "healthy", "healthy2", "filter", "https", "slow")      # not `rate`: it is limited on purpose
    for rate in rates:
        run.poster.rate = rate
        time.sleep(6)
        run.poster.window()
        run.recv({"op": "stats"})                      # the window starts here: the receivers keep their percentiles from this call to the next
        s0 = run.poster.counts["acked"]
        c0, w0 = run.svc.cpu_s(), time.time()
        rc0 = run.recv({"op": "stats"}).get("cpu_s", 0)
        lag_max = 0
        t_end = time.time() + 20
        while time.time() < t_end:
            time.sleep(1.0)
            s, text = run.read("/metrics", timeout=4, raw=True)
            if s == 200:
                for sid, v in monitor.parse_series(text, "hooks_endpoint_lag_events").items():
                    if run.eps.get(run.label_of.get(sid, ""), None) is not None and run.eps[run.label_of[sid]].cls in steady:
                        lag_max = max(lag_max, v)
        dt = time.time() - w0
        acked = run.poster.counts["acked"] - s0
        rs = run.recv({"op": "stats"})
        w = run.poster.window()
        step = {"rate": rate, "achieved_per_s": round(acked / dt, 1), "lag_max_steady_endpoints": lag_max, "service_cpu_pct": round((run.svc.cpu_s() - c0) / dt * 100, 1),
                "receivers_cpu_pct": round((rs.get("cpu_s", 0) - rc0) / dt * 100, 1), "receivers_loop_lag_max_ms": round(rs.get("loop_lag_max_ms", 0), 1), "ingest_p50_ms": round(w["p50"], 2),
                "ingest_p99_ms": round(w["p99"], 2), "ingest_max_ms": round(w["max"], 1), "deliv_lat_p50_ms": rs.get("lat_p50"), "deliv_lat_p99_ms": rs.get("lat_p99"),
                "deliv_lat_max_ms": rs.get("lat_max"), "rss_kb": (run.svc.proc_info() or [0])[0], "loadavg1": round(os.getloadavg()[0], 2)}
        steps.append(step)
        print(json.dumps(step), flush=True)
        ok = step["achieved_per_s"] >= 0.95 * rate and lag_max < 500 and step["receivers_loop_lag_max_ms"] < 100
        if ok:
            best = step
        else:
            break
    run.stop.set()
    run.poster.stop(timeout=60)
    run.stop_all.set()
    run.svc.term(15)
    for p in (run.probe, run.receivers):
        if p and p.poll() is None:
            p.terminate()
    out = {"steps": steps, "sustained_events_per_s": best["rate"] if best else None, "limited_by": None, "endpoints": a.endpoints, "schedules": a.schedules, "machine": machine(),
           "binary_sha256": sha256(a.binary)}
    if steps:
        last = steps[-1]
        out["limited_by"] = ("the service (one core)" if last["service_cpu_pct"] >= 85 else
                             ("the harness's receivers" if last["receivers_cpu_pct"] >= 85 or last["receivers_loop_lag_max_ms"] >= 100 else
                              ("a lag among the steady endpoints (see the step)" if last["lag_max_steady_endpoints"] >= 500 else "neither is saturated")))
    with open(run.path("calibration.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("calibration:", json.dumps({k: out[k] for k in ("sustained_events_per_s", "limited_by")}))
    return 0


def selftest(a):
    """The harness against itself (docs/soak.md): the checker on mutated ledgers, then three minutes on the real service clean (must pass) and with a fault injected (must fail)."""
    import selftest_ledger
    results = []
    rc = selftest_ledger.main()
    results.append(("the checker catches every mutation of the ledgers", rc == 0, ""))
    if a.selftest_ledger:
        return 0 if rc == 0 else 1
    if not a.binary or not a.pg:
        print("--selftest needs --binary and --pg (or HOOKS_PG) for the runs on the service", file=sys.stderr)
        return 2
    base = os.path.abspath(a.out if a.out != "soak-out" else "soak-selftest")
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(base)
    secs = a.duration_s or 180.0
    common_args = ["--binary", a.binary, "--pg", a.pg, "--duration-s", str(secs), "--seed", str(a.seed or 11), "--rate", str(min(a.rate, 40.0)), "--burst-rate", str(min(a.burst_rate, 120.0)), "--restart-scale", "0.25",
                   "--endpoints", str(a.endpoints), "--lenient-validity", "--sample-s", "5", "--progress-s", "30", "--drain-s", "180", "--after-stop-s", "8", "--min-incarnation-s", "600"] + ([] if a.selftest_strict else ["--waive", "B_restart_repeat,J_repeat"]) + \
                  (["--no-pin"] if a.no_pin else []) + (["--shim", a.shim] if a.shim else [])
    plan = [("clean", [], None)]
    names = ["lose", "dup", "liar"] if a.selftest_mutants == "all" else [x for x in a.selftest_mutants.split(",") if x]
    expect = {"lose": ("A_missing",), "dup": ("B_repeat",), "liar": ("P_id_mismatch", "A_not_in_log", "A_missing", "A_unseen_event")}
    for m in names:
        extra = ["--fault", m, "--stop-after-violation", "25"]
        if m == "liar":
            extra += ["--chaos", "kill9", "--chaos-scale", "0.05"]
        plan.append((m, extra, expect[m]))
    for name, extra, tags in plan:
        out = os.path.join(base, name)
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *common_args, "--out", out, *extra], capture_output=True, text=True)
        try:
            v = json.load(open(os.path.join(out, "verdict.json")))
        except (OSError, ValueError):
            results.append((f"run {name}", False, f"no verdict: {r.stdout[-400:]} {r.stderr[-400:]}"))
            continue
        took = time.time() - t0
        if name == "clean":
            ok = v["verdict"] == "PASS"
            results.append(("a clean run passes", ok, f"{v['verdict']}, failed {v['failed']}, {took:.0f} s, violations {v['violations']}"))
        else:
            hit = [t for t in tags if v["violations"].get(t)]
            ok = v["verdict"] == "FAIL" and bool(hit)
            results.append((f"a run with the fault `{name}` fails, caught as {'/'.join(tags)}", ok, f"{v['verdict']}, caught as {hit or 'nothing'} ({took:.0f} s), violations {v['violations']}"))
    print()
    for name, ok, detail in results:
        print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""))
    allok = all(ok for _, ok, _ in results)
    print("self-test " + ("passed" if allok else "FAILED"))
    return 0 if allok else 1


def main(argv=None):
    p, a = parse(argv or sys.argv[1:])
    if a.selftest or a.selftest_ledger:
        if a.selftest_ledger and not a.selftest:
            import selftest_ledger
            return selftest_ledger.main()
        return selftest(a)
    finalize_args(a, p)
    return run_main(a)


if __name__ == "__main__":
    sys.exit(main())
