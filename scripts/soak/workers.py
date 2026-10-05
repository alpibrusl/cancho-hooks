"""What the soak does besides posting events (docs/soak.md section 1): replays and their cancellation, bulk replay of dead letters, enabling an endpoint a 410 disabled,
creating, changing and deleting endpoints, rotating secrets, online backups and one restore. Each is a thread or a function of a `Run` (soak.py); none of them decides whether
the service was right: they tell the checker (ledger.py) what they asked for, **before** they ask, so that a delivery that follows is never taken for a surprise.
"""
import json
import os
import random
import re
import secrets as pysecrets
import subprocess
import threading
import time
import base64

import common
from common import CLASSES, TYPE_NAMES, wanted

MUST = ("oracle", "healthy", "healthy2", "filter", "slow", "flapping", "http5xx", "https", "rate")


def new_secret():
    return "whsec_" + base64.b64encode(pysecrets.token_bytes(24)).decode()


# ---- secrets -----------------------------------------------------------------------------------------------------------

def rotate_secret(run, label, keep_old_ms, rng=None):
    """Change an endpoint's secret with PATCH. The receiver is told first that either secret is valid; when the service has answered it is told when the old one stops
    being (3 s after a rotation without an overlap, 3 s after the overlap ends otherwise); the checker is told from when two signatures are due."""
    ep = run.eps[label]
    if ep.svc_id is None or not ep.active:
        return None
    old = ep.secrets[-1]["s"]
    new = new_secret()
    t0 = time.time()
    with run.vlock:
        run.verifier.note_rotation(label, t0, t0)       # the overlap of the rotation before this one ends here
    keep = [dict(x) for x in ep.secrets if x.get("to") is None or x["to"] > t0 - 60]          # an older secret is valid until its own end (an attempt may be in flight)
    run.recv({"op": "secrets", "label": label, "secrets": keep + [{"s": new, "from": t0 - 1.0, "to": None}]})
    body = {"secret": new}
    if keep_old_ms:
        body["keep_old_ms"] = keep_old_ms
    status, ans = run.admin("PATCH", f"/endpoints/{ep.svc_id}", body, timeout=10)
    t1 = time.time()
    if status == 200:
        until = t1 + (keep_old_ms / 1000.0 if keep_old_ms else 0.0) + 3.0
        # every secret before the new one ends now (the service has one of them or none: an earlier change that was not answered is settled by this one)
        ep.secrets = [dict(k, to=(k["to"] if k.get("to") is not None else until)) for k in keep] + [{"s": new, "from": t0 - 1.0, "to": None}]
        run.recv({"op": "secrets", "label": label, "secrets": ep.secrets})
        if keep_old_ms:
            with run.vlock:
                run.verifier.note_rotation(label, t1, t1 + keep_old_ms / 1000.0)
        run.log("rotate", label=label, keep_old_ms=keep_old_ms, status=status)
        run.save_state()
        return True
    # not known whether the service took it: both secrets stay valid at the receiver, and the checker is not told of an overlap
    ep.secrets = keep[:-1] + [dict(keep[-1], to=None), {"s": new, "from": t0 - 1.0, "to": None}]
    run.log("rotate", label=label, keep_old_ms=keep_old_ms, status=status, uncertain=True)
    return False


# ---- replays -----------------------------------------------------------------------------------------------------------

class Replayer(threading.Thread):
    """Every few seconds: replay a recent event to every endpoint that wants it, or to one endpoint; sometimes take the replay back at once. And, now and then, replay the
    dead letters of the `dead` endpoint in bulk and cancel what waits."""

    def __init__(self, run):
        super().__init__(daemon=True, name="replayer")
        self.r = run
        self.rng = random.Random(run.seed * 31 + 5)
        self.n = 0

    def step(self):
        r = self.r
        rng = self.rng
        if r.quiet_replays:
            return
        cands = [i for i, (n, t) in list(r.poster.recent.items()) if time.time() - t > 2.0 and time.time() - t < 60.0 and i > 0]
        if not cands:
            return
        ev = rng.choice(cands)
        actives = [e for e in r.eps.values() if e.active and e.svc_id is not None and e.cls != "dead"]
        typ = r.type_of(ev)
        if typ is None:
            return
        if rng.random() < 0.6:
            labels = [e.label for e in actives if wanted(e.types, typ)]
            path = f"/events/{ev}/replay"
        else:
            e = rng.choice(actives)
            labels = [e.label]
            path = f"/events/{ev}/replay/{e.svc_id}"
        with r.vlock:
            for lb in labels:
                r.verifier.note_replay(lb, ev)
        status, ans = r.admin("POST", path, None, timeout=8)
        r.log("replay", path=path, labels=labels, status=status)
        if status not in (202, 0, 504):
            with r.vlock:
                for lb in labels:
                    r.verifier.note_replay(lb, ev, -1)
        r.counts["replays"] += status == 202
        if status == 202 and rng.random() < 0.3 and len(labels) == 1:
            e = r.eps[labels[0]]
            s2, _ = r.admin("DELETE", f"/events/{ev}/replay/{e.svc_id}", None, timeout=8)
            r.counts["replay_cancels"] += s2 == 200
        self.n += 1

    def run(self):
        while not self.r.stop.is_set():
            try:
                self.step()
            except Exception as ex:  # noqa: BLE001
                self.r.log("worker_error", who="replayer", error=repr(ex))
            self.r.stop.wait(self.rng.uniform(1.5, 5.0))


class Enabler(threading.Thread):
    """An endpoint that answered 410 is disabled until someone enables it: this is that someone, for the `gone` class. For any other class a disabled endpoint is a finding."""

    def __init__(self, run):
        super().__init__(daemon=True, name="enabler")
        self.r = run

    def run(self):
        r = self.r
        while not r.stop.is_set():
            r.stop.wait(1.5)
            status, eps = r.read("/endpoints")
            if status != 200:
                continue
            for e in eps:
                label = r.label_of.get(e["id"])
                if not label:
                    continue
                if e.get("disabled") and r.eps[label].cls == "gone":
                    s, _ = r.admin("POST", f"/endpoints/{e['id']}/enable", None, timeout=5)
                    r.counts["enables"] += s == 200


# ---- endpoint churn ----------------------------------------------------------------------------------------------------

class Churn(threading.Thread):
    """Creates an endpoint (a receiver of its own), changes it, lets it run, waits until its cursor has reached the newest event, deletes it, and does it again."""

    def __init__(self, run, k):
        super().__init__(daemon=True, name=f"churn{k}")
        self.r, self.k = run, k
        self.rng = random.Random(run.seed * 977 + k)
        self.cycle = 0

    def run(self):
        r = self.r
        time.sleep(self.rng.uniform(2, 8) + 3 * self.k)
        while not r.stop.is_set():
            try:
                self.lifecycle()
            except Exception as ex:  # noqa: BLE001
                r.log("worker_error", who=f"churn{self.k}", error=repr(ex))
            r.stop.wait(self.rng.uniform(1, 5))

    def lifecycle(self):
        r, rng = self.r, self.rng
        self.cycle += 1
        label = f"churn{self.k}-{self.cycle}"
        sec = new_secret()
        types = list(CLASSES["churn"][0])
        ep = r.new_endpoint(label, "churn", types, secrets=[{"s": sec, "from": None, "to": None}])
        body = {"host": "127.0.0.1", "port": ep.port, "secret": sec, "types": types, "from": "now"}
        status, ans = r.admin("POST", "/endpoints", body, timeout=10)
        if status != 201:
            r.log("churn", label=label, step="create", status=status)
            if status in (0, 504):
                self.reconcile(ep)      # the row may have been stored
            else:
                r.drop_endpoint(ep)
            return
        ep.svc_id, ep.c0 = int(ans["id"]), int(ans["cursor"])
        r.label_of[ep.svc_id] = label
        with r.vlock:
            r.verifier.activate(label, ep.c0, time.time())
        r.log("churn", label=label, step="created", svc_id=ep.svc_id, c0=ep.c0)
        r.save_state()
        life = rng.uniform(8, 40)
        end = time.time() + life
        patched = False
        while time.time() < end and not r.stop.is_set():
            r.stop.wait(rng.uniform(2, 6))
            if r.stop.is_set():
                break
            if not patched or rng.random() < 0.5:
                patched = True
                what = rng.choice(["limits", "headers", "rotate", "rotate-keep"])
                if what == "limits":
                    s, _ = r.admin("PATCH", f"/endpoints/{ep.svc_id}", {"rate": rng.choice([0, 200, 500]), "concurrency": rng.choice([0, 2, 4, 8])}, timeout=8)
                elif what == "headers":
                    s, _ = r.admin("PATCH", f"/endpoints/{ep.svc_id}", {"headers": {"X-Soak": str(rng.randint(1, 99))}}, timeout=8)
                else:
                    s = rotate_secret(r, label, 8000 if what == "rotate-keep" else 0)
                r.counts["churn_patches"] += 1
                r.log("churn", label=label, step="patch", what=what, status=s)
        self.retire(ep)

    def reconcile(self, ep):
        """A create that was not answered: the row may be there. Find it by its port, and take it away."""
        r = self.r
        time.sleep(1.0)
        try:
            rows = r.psql_rows(f"select id from endpoints where port = {ep.port}")
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            sid = int(rows[0][0])
            with r.vlock:
                r.verifier.activate(ep.label, 10 ** 12, time.time(), ambiguous=True)
            ep.svc_id = sid
            r.label_of[sid] = ep.label
            r.log("churn", label=ep.label, step="ambiguous", svc_id=sid)
            self.delete(ep)
        r.drop_endpoint(ep, keep_receiver=bool(rows))

    def retire(self, ep):
        r = self.r
        status, st = r.read("/stats")
        m = st.get("events_last_id") if status == 200 else None
        caught = False
        if m is not None:
            end = time.time() + 60
            while time.time() < end and not r.stop.is_set():
                s, e = r.read(f"/endpoints/{ep.svc_id}")
                if s == 200 and e.get("cursor", 0) >= m:
                    caught = True
                    break
                time.sleep(0.2)
        if caught:
            r.checker.sync_settle(ep.label)
        else:
            r.log("churn", label=ep.label, step="not-caught-up", m=m)
            if not r.stop.is_set() and m is not None:
                with r.vlock:
                    r.verifier.v.add("END_lag", ep=ep.label, why="a created endpoint did not catch up within 60 s", m=m)
        self.delete(ep)
        with r.vlock:
            r.verifier.retire(ep.label, time.time())
        ep.active = False
        r.counts["churn_done"] += 1
        threading.Timer(12.0, lambda: r.recv({"op": "remove", "label": ep.label})).start()

    def delete(self, ep):
        r = self.r
        for _ in range(5):
            s, _ = r.admin("DELETE", f"/endpoints/{ep.svc_id}", None, timeout=8)
            if s in (200, 404):
                break
            time.sleep(1.0)
        r.log("churn", label=ep.label, step="deleted", status=s)


# ---- the sick endpoint -------------------------------------------------------------------------------------------------

def sick_cycle(run, window_s=90.0):
    """The `sick` endpoint answers 503 to everything for a window and the `dead` one refuses connections for a window (longer than the retry horizon): whatever fails for the whole of the retry horizon is a
    dead letter. Then the dead letters are replayed in bulk until there are none."""
    eps = [e for e in run.eps.values() if e.cls in ("sick", "dead") and e.svc_id is not None and e.active]
    t0 = time.time()
    for e in eps:
        until = t0 + window_s
        if e.cls == "sick":
            run.recv({"op": "sick", "label": e.label, "until": until})
        else:
            run.recv({"op": "mode", "label": e.label, "mode": "vanish", "until": until})
        with run.vlock:
            run.verifier.note_window(e.label, t0, until)
        run.log("sick", label=e.label, cls=e.cls, until=until)
    if run.stop.wait(window_s + 10):       # an event that fails from its first attempt to the end of the window dies 63 s after it, so before the window ends
        return
    for e in eps:
        sweep_dead(run, e)


def sweep_dead(run, e, rounds=40):
    """POST replay-dead until the endpoint holds no dead letter (each call takes what the table of 32 waiting replays has room for)."""
    for _ in range(rounds):
        t_call = time.time()
        s, ans = run.admin("POST", f"/endpoints/{e.svc_id}/replay-dead", {}, timeout=10)
        # reading the dead letters beyond the table's 2,048 is one read of delivery.seg, a documented pause of the loop (docs/runbook.md 3.6): not counted against it
        run.excused.append((t_call - 0.2, time.time() + 1.0))
        run.log("replay-dead", label=e.label, status=s, seconds=round(time.time() - t_call, 2), answer=ans if s in (200, 202) else None)
        if s not in (200, 202):
            time.sleep(1.0)
            continue
        run.counts["replay_dead_calls"] += 1
        if ans.get("remaining", 1) == 0 and ans.get("waiting", 1) == 0:
            return True
        time.sleep(1.0)
    return False


# ---- backups -----------------------------------------------------------------------------------------------------------

def run_backup(run, restore=False):
    """An online backup of the live service (scripts/backup.sh), and, if asked, a restore of it into a scratch directory, checked by logcheck and against the poster's ledger."""
    from common import ROOT
    out = os.path.join(run.out, "backups")
    os.makedirs(out, exist_ok=True)
    with run.vlock:
        m0 = run.poster.max_id
    t0 = time.time()
    cmd = ["bash", os.path.join(ROOT, "scripts", "backup.sh"), "--dir", run.datadir, "--out", out, "--mode", "online", "--pg-database", run.pg["db"], "--pg-host", run.pg["host"],
           "--pg-port", str(run.pg["port"]), "--pg-user", run.pg["user"], "--skip-attempts"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=run.pg_env())
    except subprocess.TimeoutExpired:
        run.violate("K_backup", why="backup.sh did not finish in 180 s")
        return None
    run.counts["backups"] += 1
    run.log("backup", status=p.returncode, seconds=round(time.time() - t0, 2), stderr=p.stderr[-300:] if p.returncode else "")
    if p.returncode != 0:
        run.violate("K_backup", status=p.returncode, stderr=p.stderr[-500:])
        return None
    dirs = sorted(d for d in os.listdir(out) if d.startswith("hooks-backup-"))
    path = os.path.join(out, dirs[-1])
    ok = True
    if restore:
        ok = restore_check(run, path, m0)
    for d in dirs[:-1]:
        subprocess.run(["rm", "-rf", os.path.join(out, d)])
    return ok


def restore_check(run, backup, m0):
    from common import ROOT
    scratch = os.path.join(run.out, "restore-scratch")
    subprocess.run(["rm", "-rf", scratch])
    os.makedirs(scratch)
    p = subprocess.run(["bash", os.path.join(ROOT, "scripts", "restore.sh"), "--backup", backup, "--dir", scratch], capture_output=True, text=True, timeout=180)
    run.counts["restores"] += 1
    if p.returncode != 0:
        run.violate("K_restore", status=p.returncode, stderr=p.stderr[-500:])
        return False
    q = subprocess.run(["python3", os.path.join(ROOT, "scripts", "logcheck.py"), "check", scratch], capture_output=True, text=True, timeout=180)
    if q.returncode != 0:
        run.violate("K_restore", why="logcheck does not accept the restored directory", out=q.stdout[-500:])
        return False
    chaos = common.chaos_module()  # tests/chaos.py: an independent reader of the log
    recs, torn = chaos.read_events(scratch)
    ids = [i for i, _ in recs]
    bad = []
    if ids != list(range(ids[0], ids[0] + len(ids))) if ids else False:
        bad.append("ids not dense")
    if ids and ids[-1] < m0 and not (recs and ids[0] > m0):
        bad.append(f"newest restored event {ids[-1]} is older than the newest acknowledged before the backup began ({m0})")
    from common import crc_pad
    seen = 0
    with run.vlock:
        known = dict(run.poster.recent)
    for ident, pairs in recs:
        body = dict(pairs).get(b"event")
        try:
            d = json.loads(body)
        except (TypeError, ValueError):
            bad.append(f"event {ident} does not parse")
            continue
        if "c" in d and d["c"] != crc_pad(d.get("pad", "")):
            bad.append(f"event {ident} was altered")
        k = known.get(ident)
        if k is not None:
            seen += 1
            if k[0] != d.get("n"):
                bad.append(f"event {ident} is n={d.get('n')} in the backup and n={k[0]} as acknowledged")
    run.log("restore", events=len(recs), first=ids[0] if ids else None, last=ids[-1] if ids else None, compared=seen, problems=bad[:5])
    if bad:
        run.violate("K_restore", problems=bad[:10])
        return False
    subprocess.run(["rm", "-rf", scratch])
    return True
