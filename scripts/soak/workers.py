"""What the soak does besides posting events (docs/soak.md section 1): replays and their cancellation, bulk replay of dead letters, enabling an endpoint a 410 disabled,
creating, changing and deleting endpoints, rotating secrets, online backups and one restore. Each is a thread or a function of a `Run` (soak.py); none of them decides whether
the service was right: they tell the checker (ledger.py) what they asked for, **before** they ask, so that a delivery that follows is never taken for a surprise.
"""
import json
import os
import random
import re
import secrets as pysecrets
import shutil
import signal
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
        actives = [e for e in r.eps.values() if e.active and e.svc_id is not None]
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


def find_rows_by_port(run, port):
    """The ids of the rows of the table that have this port (the harness's own database access; the running service may or may not know them)."""
    return [int(x[0]) for x in run.psql_rows(f"select id from endpoints where port = {int(port)}")]


def remove_endpoint(run, sid, port=None, why="", label=None, wait_s=180.0):
    """Take an endpoint away so that it is in neither the running service nor the table, and say so: DELETE through the API until the service says it is gone or has never heard of it, then
    delete the row (a service that never heard of it, because its create was not answered, is not told by the API, and the next start would load the row and deliver to it for ever, at
    the slowest cursor, with nobody listening: the zombies of the third 24 h run), and read the table to see it is gone. Returns True when it is."""
    end = time.time() + wait_s
    s = 0
    while time.time() < end and not run.stop_all.is_set():
        s, _ = run.admin("DELETE", f"/endpoints/{sid}", None, timeout=8)
        if s in (200, 404):
            break
        if s in (0, 504):
            g, _ = run.read(f"/endpoints/{sid}", timeout=5)
            if g == 404:
                s = 404
                break
        time.sleep(1.5)
    row_gone = None
    try:
        run.psql_rows(f"delete from endpoints where id = {int(sid)}")
        row_gone = not run.psql_rows(f"select id from endpoints where id = {int(sid)}")
    except Exception as ex:  # noqa: BLE001
        run.log("harness-event", what="zombie-row-delete-failed", svc_id=sid, error=repr(ex)[:200])
    ok = s in (200, 404) and row_gone is not False
    run.counts["zombies_removed" if why else "endpoints_removed"] += 1
    if why or not ok:
        run.log("harness-event", what="endpoint-removed", svc_id=sid, port=port, label=label, why=why, api_status=s, row_gone=row_gone)
    if not ok:
        run.violate("P_zombie_endpoint", ep=label, svc_id=sid, status=s, row_gone=row_gone, why=why)
    return ok


class Enabler(threading.Thread):
    """An endpoint that answered 410 is disabled until someone enables it: this is that someone, for the `gone` class. For any other class a disabled endpoint is a finding.

    It is also the janitor: **the service may only hold the endpoints the harness means it to hold.** A create that was not answered (the database was frozen, or cut) may have been made
    after the harness gave up on it, and an endpoint that is in the service and that the harness does not intend (one it has not been told of for 5 s, or one it deleted and that came back
    with the next start, which loads the table) is a factory of dead letters at the slowest cursor and a pin on the log. Each is deleted through the API and its row removed, its receiver
    is taken away, and the port is given back, and each is a harness event in `chaos.jsonl` (`endpoint-removed`, with the reason) and counted (`zombies_removed`). What the harness does
    intend is never touched: an endpoint that really exists is judged like any other. After each start of the service the first list is logged (`reconcile`)."""

    def __init__(self, run):
        super().__init__(daemon=True, name="enabler")
        self.r = run
        self.unknown = {}
        self.inc_seen = None

    def run(self):
        r = self.r
        while not r.stop_all.is_set():
            r.stop_all.wait(1.5)
            self.step()

    def step(self):
        r = self.r
        if True:
            inc = r.svc.inc
            status, eps = r.read("/endpoints?limit=256")
            if status != 200 or not isinstance(eps, list):
                return
            now = time.time()
            seen, zombies = set(), []
            for e in eps:
                label = r.label_of.get(e["id"])
                ep = r.eps.get(label) if label else None
                if ep is not None and ep.active:
                    if e.get("disabled") and ep.cls == "gone" and not r.stop.is_set():
                        s, _ = r.admin("POST", f"/endpoints/{e['id']}/enable", None, timeout=5)
                        r.counts["enables"] += s == 200
                    continue
                pending = next((x for x in r.eps.values() if x.port == e.get("port") and x.svc_id is None and x.active), None)
                if pending is not None and now - pending.created_at < 90.0:
                    continue        # a create that is being waited on (Churn.reconcile): its own thread settles it
                if ep is not None and not ep.active and now - ep.retired_at < 15.0:
                    continue        # being deleted just now
                seen.add(e["id"])
                since = self.unknown.setdefault(e["id"], now)
                zombies.append((e, label, pending, now - since))
            if self.inc_seen != inc:
                self.inc_seen = inc
                r.log("harness-event", what="reconcile", inc=inc, listed=len(eps), intended=sum(1 for x in r.eps.values() if x.active and x.svc_id is not None),
                      unintended=[e["id"] for e, *_ in zombies])
            for e, label, pending, age in zombies:
                if age > 5.0:
                    self.stray(e, label, pending)
            for k in [k for k in self.unknown if k not in seen]:
                del self.unknown[k]

    def stray(self, e, label, pending):
        r = self.r
        sid, port = e["id"], e.get("port")
        ep = pending
        if ep is not None:
            ep.svc_id = sid
            r.label_of[sid] = ep.label
            with r.vlock:
                r.verifier.activate(ep.label, 0, time.time(), ambiguous=True)
            r.log("stray", svc_id=sid, label=ep.label, port=port)
        elif label is not None:
            ep = r.eps[label]
            r.log("stray", svc_id=sid, label=label, port=port, note="deleted before, and the service has it again (loaded from the table at a start)")
        else:
            r.log("stray", svc_id=sid, label=None, port=port, note="not one of the harness's")
        why = "unanswered create" if pending is not None else ("came back at a start" if label is not None else "unknown to the harness")
        if remove_endpoint(r, sid, port, why=why, label=label if label else (ep.label if ep else None)):
            r.counts["strays_deleted"] += 1
            self.unknown.pop(sid, None)
            if pending is not None:
                r.drop_endpoint(ep)      # one that was retired before has been dropped already: its receiver and its port were dealt with then


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
        """A create that was not answered: the row may be there, now or in a moment (the database was frozen or cut; the service answers 504 after five seconds and the row may still go).
        Look for it by its port in the table every 3 s for a minute; when it is found, take it away (API and row) and take the receiver away. When it is not found the receiver stays for
        another minute and the table is looked at once more; the janitor (Enabler) catches what is still made after that."""
        r = self.r
        end = time.time() + 60
        sid = None
        while time.time() < end and not r.stop_all.is_set():
            time.sleep(3.0)
            try:
                rows = find_rows_by_port(r, ep.port)
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                sid = rows[0]
                break
        if sid is None:
            threading.Timer(60.0, self.late_look, args=(ep,)).start()
            return
        with r.vlock:
            r.verifier.activate(ep.label, 10 ** 12, time.time(), ambiguous=True)
        ep.svc_id = sid
        r.label_of[sid] = ep.label
        r.log("churn", label=ep.label, step="ambiguous", svc_id=sid)
        ok = remove_endpoint(r, sid, ep.port, why="unanswered create", label=ep.label)
        r.drop_endpoint(ep, verified_gone=ok)

    def late_look(self, ep):
        r = self.r
        try:
            rows = find_rows_by_port(r, ep.port)
        except Exception:  # noqa: BLE001
            rows = None
        if rows:
            sid = rows[0]
            with r.vlock:
                r.verifier.activate(ep.label, 10 ** 12, time.time(), ambiguous=True)
            ep.svc_id = sid
            r.label_of[sid] = ep.label
            ok = remove_endpoint(r, sid, ep.port, why="unanswered create, found late", label=ep.label)
            r.drop_endpoint(ep, verified_gone=ok)
        else:
            # nothing: the create did not happen; a quarantine of ten minutes before the port is used again, as a create that is still somewhere in the service would send to it
            r.drop_endpoint(ep, verified_gone=rows == [], quarantine=600.0)

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
        gone = self.delete(ep)
        with r.vlock:
            r.verifier.retire(ep.label, time.time())
        ep.active = False
        ep.retired_at = time.time()
        r.counts["churn_done"] += 1
        r.release_receiver(ep, verified_gone=gone)

    def delete(self, ep):
        """Delete until the service says it is gone (or has never heard of it) and the row is gone from the table: a database that is cut or frozen refuses with a 503 or a 504 for as long as
        the fault lasts, so this waits that out: an endpoint that is left behind with no receiver is a dead letter factory and a pin on the log, and that is the harness's fault, not the
        service's. Returns whether it is gone."""
        r = self.r
        ok = remove_endpoint(r, ep.svc_id, ep.port, label=ep.label)
        r.log("churn", label=ep.label, step="deleted", ok=ok)
        return ok


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

BACKUPS_KEPT = 2          # the newest backup is what the restore check reads; the one before it is kept in case the newest is not good. Nothing older is.


def run_group(cmd, timeout, env=None):
    """Run a command in a process group of its own and, when it takes longer than `timeout`, kill the whole group: `subprocess.run(timeout=)` kills only the child (the `bash` of
    backup.sh), and the `cp` and the checker it started went on running, with the directory they were writing left behind. Returns (completed process or None on timeout, stdout, stderr)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        return None, "", ""


def checker_kind():
    """Which log checker backup.sh and restore.sh will use. They look for the native `hooks-logcheck` beside themselves, in `build/` or `bin/`, or on the PATH, and use scripts/logcheck.py
    (Python) when there is none; this looks in the same places, so that a backup's log says which one it was."""
    from common import ROOT
    for c in (os.path.join(ROOT, "scripts", "hooks-logcheck"), os.path.join(ROOT, "build", "hooks-logcheck"), os.path.join(ROOT, "bin", "hooks-logcheck"), shutil.which("hooks-logcheck")):
        if c and os.access(c, os.X_OK):
            return "native hooks-logcheck"
    return "python logcheck.py"


def backup_timeout(datadir, native):
    """Seconds a backup may take: the copy and the check of the logs scale with their size (the Python checker reads about 1.5 MB a second, the native one far faster), so a fixed
    180 s was a verdict on the size of the directory."""
    mb = dir_mb(datadir)
    return min(3600.0, (90.0 + 0.1 * mb) if native else (120.0 + 1.2 * mb)), mb


def dir_mb(d):
    from service import dir_usage
    return dir_usage(d)[0] / 1048576.0


def remove_partials(out):
    """The `.partial` directories a backup that was killed leaves in `out` (backup.sh writes under that name and renames when the copy verified). Returns how many were left that could not be removed."""
    left = 0
    try:
        names = [n for n in os.listdir(out) if n.endswith(".partial")]
    except OSError:
        return 0
    for n in names:
        shutil.rmtree(os.path.join(out, n), ignore_errors=True)
        if os.path.exists(os.path.join(out, n)):
            left += 1
    return left


def run_backup(run, restore=False):
    """An online backup of the live service (scripts/backup.sh), and, if asked, a restore of it into a scratch directory, checked by logcheck and against the poster's ledger."""
    from common import ROOT
    out = os.path.join(run.out, "backups")
    os.makedirs(out, exist_ok=True)
    remove_partials(out)        # what an earlier backup left (a run that was resumed after a crash)
    with run.vlock:
        m0 = run.poster.max_id
    t0 = time.time()
    kind = checker_kind()
    timeout, mb = backup_timeout(run.datadir, kind.startswith("native"))
    cmd = ["bash", os.path.join(ROOT, "scripts", "backup.sh"), "--dir", run.datadir, "--out", out, "--mode", "online", "--pg-database", run.pg["db"], "--pg-host", run.pg["host"],
           "--pg-port", str(run.pg["port"]), "--pg-user", run.pg["user"], "--skip-attempts"]
    p, so, se = run_group(cmd, timeout, env=run.pg_env())
    if p is None:
        left = remove_partials(out)
        run.log("backup", status="timeout", seconds=round(time.time() - t0, 2), timeout_s=round(timeout), data_mb=round(mb, 1), checker=kind, partials_left=left)
        run.violate("K_backup", why=f"backup.sh did not finish in {timeout:.0f} s (the data directory is {mb:.0f} MB; checker: {kind})")
        return None
    run.counts["backups"] += 1
    run.log("backup", status=p.returncode, seconds=round(time.time() - t0, 2), timeout_s=round(timeout), data_mb=round(mb, 1), checker=kind, stderr=se[-300:] if p.returncode else "")
    if p.returncode != 0:
        remove_partials(out)
        run.violate("K_backup", status=p.returncode, stderr=se[-500:])
        return None
    dirs = sorted(d for d in os.listdir(out) if d.startswith("hooks-backup-") and not d.endswith(".partial"))
    path = os.path.join(out, dirs[-1])
    ok = True
    if restore:
        ok = restore_check(run, path, m0)
    for d in dirs[:-BACKUPS_KEPT]:
        shutil.rmtree(os.path.join(out, d), ignore_errors=True)
    left = remove_partials(out)
    if left:
        run.violate("K_backup", why=f"{left} .partial directories could not be removed from {out}")
    return ok


RESTORE_TIMEOUT_S = 1200.0


def restore_check(run, backup, m0):
    from common import ROOT
    scratch = os.path.join(run.out, "restore-scratch")
    subprocess.run(["rm", "-rf", scratch])
    os.makedirs(scratch)
    # `restore.sh` checks the pair of logs with `logcheck.py`, which is Python: about 1.5 MB a second, so 400 MB of logs takes four and a half minutes of CPU on an idle
    # core. The second 24 h run died here, in a timeout of 180 s, and left no verdict. A timeout is a finding, never a crash.
    native = checker_kind().startswith("native")
    tmo = max(RESTORE_TIMEOUT_S if not native else 300.0, backup_timeout(run.datadir, native)[0])
    p, so, se = run_group(["bash", os.path.join(ROOT, "scripts", "restore.sh"), "--backup", backup, "--dir", scratch], tmo)
    if p is None:
        run.violate("K_restore", why=f"restore.sh did not finish in {tmo:.0f} s")
        return False
    run.counts["restores"] += 1
    if p.returncode != 0:
        run.violate("K_restore", status=p.returncode, stderr=se[-500:])
        return False
    q, qo, qe = run_group(["python3", os.path.join(ROOT, "scripts", "logcheck.py"), "check", scratch], tmo)
    if q is None:
        run.violate("K_restore", why=f"logcheck.py did not finish in {tmo:.0f} s")
        return False
    if q.returncode != 0:
        run.violate("K_restore", why="logcheck does not accept the restored directory", out=qo[-500:])
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
