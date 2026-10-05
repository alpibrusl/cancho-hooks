"""The chaos schedule of the soak (docs/soak.md section 1): a set of independent processes of faults, each with its own mean interval and its own random generator seeded
from the run's seed and the fault's name, so that the n-th occurrence of a fault always has the same parameters whatever the others did. The instants depend on the
clock; the kinds and the parameters are a function of the seed. Every action is written to `chaos.jsonl` by `run.log`, and the ones that take the service or the database
away tell the checker.
"""
import os
import random
import subprocess
import threading
import time
import zlib

import workers

# name: (mean seconds between two, floor, restarts or breaks the service). The means are for a run of a day; `scale` shrinks them for shorter ones.
ACTIONS = {
    "kill9": (240, 20, True),
    "kill9-compaction": (600, 60, True),
    "sigterm": (720, 60, True),
    "pg": (180, 20, True),
    "receiver-vanish": (150, 15, False),
    "receiver-slow": (200, 20, False),
    "burst": (1200, 120, False),
    "sick": (900, 300, False),
    "rotate": (240, 30, False),
    "backup": (600, 60, False),
    "disk-full": (1800, 300, True),
}
PG_KINDS = ["cut", "freeze", "blackhole", "hold", "kill-backends"]      # and "restart" with --pg-restart-cmd
RESTARTS = ("kill9", "kill9-compaction", "sigterm", "disk-full")


class Faults(threading.Thread):
    def __init__(self, run):
        super().__init__(daemon=True, name="faults")
        self.r = run
        a = run.args
        self.scale = a.chaos_scale
        self.enabled = set(a.chaos.split(",")) if a.chaos != "all" else set(ACTIONS) - ({"disk-full"} if not a.tmpfs_data else set())
        self.enabled = (self.enabled - {x for x in a.no_chaos.split(",") if x}) & set(ACTIONS)
        self.rngs = {n: random.Random(zlib.crc32(f"{run.seed}/{n}".encode())) for n in ACTIONS}
        self.next = {}
        self.running = set()
        self.last_outage = {}
        self.count = {n: run.saved_fault_counts.get(n, 0) for n in ACTIONS}
        now = time.time()
        for n in self.enabled:
            self.next[n] = now + self.interval(n) * 0.5 if n != "backup" else now + 60 * min(1.0, max(self.scale, 0.2))

    def interval(self, name):
        mean, floor, restarts = ACTIONS[name]
        s = self.scale
        if restarts and name != "pg":
            s = self.r.args.restart_scale or max(s, 0.5)      # a restart is costlier than a database fault: the service re-reads what is above its cursors (docs/soak.md section 8)
        return max(self.rngs[name].expovariate(1.0 / (mean * s)), floor * min(1.0, max(s, 0.5)))

    def run(self):
        r = self.r
        while not r.stop.is_set():
            if not self.next:
                return
            name = min(self.next, key=self.next.get)
            wait = self.next[name] - time.time()
            if wait > 0 and r.stop.wait(min(wait, 1.0)):
                return
            if wait > 1.0:
                continue
            self.next[name] = time.time() + self.interval(name)
            if r.stop.is_set():
                return
            if ACTIONS[name][2] and r.quiet and name != "pg":
                continue          # the quiet tail: nothing restarts the service
            if name in self.running:
                continue
            self.count[name] += 1
            k = self.count[name]
            try:
                if name in ("burst", "sick", "backup", "rotate"):
                    self.running.add(name)
                    threading.Thread(target=self.guard, args=(name, k), daemon=True).start()
                else:
                    self.act(name, k)
            except Exception as ex:  # noqa: BLE001
                r.log("fault_error", action=name, error=repr(ex))

    def guard(self, name, k):
        try:
            self.act(name, k)
        except Exception as ex:  # noqa: BLE001
            self.r.log("fault_error", action=name, error=repr(ex))
        finally:
            self.running.discard(name)

    # ---- one action
    def act(self, name, k):
        r = self.r
        rng = random.Random(zlib.crc32(f"{r.seed}/{name}/{k}".encode()))
        if name in RESTARTS or name == "pg":
            with r.fault_lock:
                if r.stop.is_set():
                    return
                if name == "kill9":
                    with r.backup_lock:
                        r.restart_service("kill9", down_s=rng.uniform(0.0, 3.0))
                elif name == "sigterm":
                    with r.backup_lock:
                        r.restart_service("term", down_s=rng.uniform(0.0, 2.0))
                elif name == "kill9-compaction":
                    self.kill_in_compaction(rng)
                elif name == "pg":
                    self.pg_fault(rng)
                elif name == "disk-full":
                    self.disk_full(rng)
        elif name == "receiver-vanish":
            d = rng.uniform(3.0, 10.0)
            labels = self.pick(rng, rng.randint(1, 3), d)
            for lb in labels:
                r.recv({"op": "mode", "label": lb, "mode": "vanish", "until": time.time() + d})
            r.log("receiver-vanish", labels=labels, seconds=round(d, 1))
        elif name == "receiver-slow":
            kind = rng.choice(["slow", "late", "hold"])
            d = rng.uniform(5.0, 10.0)
            labels = self.pick(rng, rng.randint(2, 5), d)
            until = time.time() + d
            for lb in labels:
                if kind == "slow":
                    r.recv({"op": "mode", "label": lb, "mode": "slow", "until": until, "delay": rng.uniform(0.6, 1.2)})
                elif kind == "late":
                    r.recv({"op": "mode", "label": lb, "mode": "slow", "until": until, "delay": 2.5})
                else:
                    r.recv({"op": "mode", "label": lb, "mode": "hold", "until": until})
            r.log("receiver-slow", labels=labels, which=kind, seconds=round(d, 1))
        elif name == "burst":
            dur = min(60.0, max(10.0, r.args.duration_s / 10.0)) if r.args.duration_s else 60.0
            r.log("burst", start=True, rate=r.args.burst_rate, seconds=dur)
            r.poster.rate = r.args.burst_rate
            r.bursting = True
            r.stop.wait(dur)
            r.poster.rate = r.args.rate
            r.bursting = False
            r.log("burst", start=False)
        elif name == "sick":
            workers.sick_cycle(r)
        elif name == "rotate":
            cands = [e.label for e in r.eps.values() if e.cls in workers.MUST and e.active and e.svc_id is not None]
            if cands:
                workers.rotate_secret(r, rng.choice(cands), rng.choice([0, 10000, 20000]))
        elif name == "backup":
            with r.backup_lock:
                workers.run_backup(r, restore=(k % 4 == 1))

    def pick(self, rng, k, seconds=10.0):
        """Endpoints to make fail for `seconds`: not one that is failing already (a flapping endpoint, or one that was made to fail in the last 45 s), so that the outages an endpoint is put
        through never add up to more than the retry schedule is meant to carry. (In a compressed run they would: four outages of 5 s in 65 s kill an event that has nine attempts.)"""
        now = time.time()
        cands = [e.label for e in self.r.eps.values() if e.active and e.cls in workers.MUST and e.cls != "flapping" and now - self.last_outage.get(e.label, 0) > 45.0]
        rng.shuffle(cands)
        out = cands[:k]
        for lb in out:
            self.last_outage[lb] = now + seconds
        return out

    def kill_in_compaction(self, rng):
        r = self.r
        step = rng.randint(1, 19)
        with r.backup_lock:
            r.restart_service("kill9", extra=["--compact-kill-at", str(step)], down_s=rng.uniform(0, 1.0), note="compact-kill-at %d" % step)
        kp = os.path.join(r.datadir, "killpoint")
        t_flag = time.time()
        reached = False
        while time.time() - t_flag < 90 and not r.stop.is_set():
            if os.path.exists(kp):
                reached = True
                break
            if not r.svc.alive():
                break
            time.sleep(0.05)
        frozen_at = os.path.getmtime(kp) if reached else time.time()
        with r.backup_lock:
            try:
                os.remove(kp)
            except OSError:
                pass
            r.excused.append((frozen_at - 0.5, time.time() + 6))
            r.restart_service("kill9", down_s=0.0, note=f"killpoint {step} reached={reached}")
        r.counts["compaction_kills"] += reached

    def pg_fault(self, rng):
        r = self.r
        kinds = PG_KINDS + (["restart"] if r.args.pg_restart_cmd else [])
        kind = rng.choice(kinds)
        d = rng.uniform(2.0, 20.0)
        px = r.proxy
        t0 = time.time()
        r.log("pg", which=kind, seconds=round(d, 1), start=True)
        if kind == "cut":
            px.cut()
        elif kind == "freeze":
            px.mode = "freeze"
        elif kind == "blackhole":
            px.blackhole()
        elif kind == "hold":
            px.mode = "hold"
        elif kind == "restart":
            # a real stop and start of the server, by the command the person gave: it returns when the server takes connections again
            c = subprocess.run(r.args.pg_restart_cmd, shell=True, capture_output=True, text=True, timeout=300)
            r.log("pg", which=kind, status=c.returncode, stderr=c.stderr[-200:])
            d = 0.0
        elif kind == "kill-backends":
            n = px.kill_backends(r.psql_rows)
            r.log("pg", which=kind, killed=n)
            d = min(d, 4.0)
        r.stop.wait(d)
        px.restore()
        r.log("pg", which=kind, start=False)
        with r.vlock:
            r.verifier.note_away(t0, time.time(), "pg")
        r.counts["pg_faults"] += 1
        r.counts["pg_" + kind] += 1

    def disk_full(self, rng):
        r = self.r
        if not r.tmpfs:
            return
        ballast = os.path.join(r.datadir, "ballast")
        r.log("disk-full", start=True)
        os.system(f"fallocate -l $(( $(df --output=avail -B1 {r.datadir} | tail -1) )) {ballast} 2>/dev/null || dd if=/dev/zero of={ballast} bs=1M 2>/dev/null")
        r.stop.wait(rng.uniform(3.0, 6.0))
        try:
            os.remove(ballast)
        except OSError:
            pass
        r.log("disk-full", start=False)
        status, _ = r.read("/readyz", raw=True)
        if status != 200:
            with r.backup_lock:
                r.restart_service("kill9", down_s=0.0, note="restart after a full disk")
        r.counts["disk_full"] += 1
