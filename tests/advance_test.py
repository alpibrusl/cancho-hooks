#!/usr/bin/env python3
"""A restart remembers where an endpoint was (docs/design.md section 42; docs/soak.md, findings 1 and 4).

    [STAGES=clean,kill,snapshot,plain] python3 tests/advance_test.py build/hooks        (HOOKS_BIN_MAIN=<a build from before> adds the stage `oldbuild`)

One endpoint with a list of types (`types=t.a`): half of 3,000 events are not wanted and are passed over with no record. The first wanted one is refused until it is
released, so the endpoint's cursor stays behind it while the window moves past the events after it; then it is let through and the cursor goes to the end. On the
build before, a restart then sent 987 of the 1,499 wanted events a second time (their records were more than a window above where a replay of the log left the
cursor) while the cursor climbed from 0 at about 50 ids a second.

  clean     SIGTERM and a start: no event is delivered again, and the cursor is at the end within a second of `listening`
  kill      the same with kill -9 as a power cut (the fsync shim) once everything was delivered and the log flushed: no event is delivered again
  snapshot  the outcomes log replaced by snapshots during the run (`delivery-log-bytes` 64 KiB), then kill -9, then SIGTERM: no event is delivered again
  plain     the same endpoint without a list: no `advanced` record is written (a log an older build reads)
  oldbuild  (HOOKS_BIN_MAIN) the build before refuses a log with kind 19 with status 15, and reads it again after `--compact-now`
"""
import collections
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
STAGES = os.environ.get("STAGES", "clean,kill,snapshot,plain,oldbuild").split(",")
N = 3000
check = L.Checks()


class Rig:
    """A receiver that refuses the first wanted event until `release` is set, and counts every delivery of every event."""

    def __init__(self, types=True, extra=(), power_loss=False):
        self.d = tempfile.mkdtemp(prefix="hooks-advance-")
        self.got, self.first, self.lock, self.release = collections.Counter(), [None], threading.Lock(), threading.Event()
        self.peer = L.Peer("ok")
        self.peer._serve = self.serve
        words = " types=t.a" if types else ""
        open(os.path.join(self.d, "endpoints.conf"), "w").write(f"0 127.0.0.1 {self.peer.port} {L.secret()}{words}\n")
        # retries that start at half a second and reach three and a half minutes: the held-back event does not die while it is refused
        self.args = ["--schedule", "500,500,500,500,1000,1000,2000,2000,5000,5000,10000,10000,30000,30000,60000,60000", *extra]
        self.power_loss = power_loss
        self.svc = None

    def serve(self, c):
        try:
            g = self.peer._read_request(c)
            if g is None:
                return
            eid = int(g[0][b"webhook-id"][4:])
            with self.lock:
                if self.first[0] is None:
                    self.first[0] = eid
                refuse = eid == self.first[0] and not self.release.is_set()
                if not refuse:
                    self.got[eid] += 1
            c.sendall(b"HTTP/1.1 500 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" if refuse else b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def start(self):
        self.svc = L.Service(BIN, self.d, self.args, port=self.svc.port if self.svc else None, power_loss=self.power_loss)
        t0 = time.time()
        ok = self.svc.start(timeout=30)
        return ok, t0

    def cursor(self):
        return json.loads(self.svc.get("/endpoints")[1])[0]["cursor"]

    def run_up(self, types=True):
        """Post the events, let the window move behind the refused one, release it, and wait until every wanted event is delivered and final."""
        for n in range(N):
            self.svc.request("POST", "/events", json.dumps({"type": "t.a" if n % 2 == 0 or not types else "t.b", "n": n}).encode(), {"Content-Type": "application/json"})
        want = N // 2 if types else N
        # the window moved: everything wanted in the first window behind the refused event is delivered, and the cursor is still behind it
        held = L.wait_for(lambda: len(self.got) >= min(want - 1, 500) and self.cursor() < N // 2, 120)
        self.release.set()
        done = L.wait_for(lambda: self.cursor() >= N and len(self.got) >= want, 180)
        time.sleep(1.0)
        return held, done, want

    def repeats(self):
        with self.lock:
            return sorted(e for e, k in self.got.items() if k > 1)

    def close(self):
        if self.svc:
            self.svc.kill()
        self.peer.close()


def stage_clean():
    r = Rig()
    r.start()
    held, done, want = r.run_up()
    check(f"clean: the cursor held behind the refused event while the window moved, then every one of the {want} wanted events delivered", held and done, f"{held} {done} {len(r.got)} {r.cursor()}")
    adv = r.svc.stats().get("advanced", -1)
    check(f"clean: a handful of `advanced` records were written ({adv}), at most one for each window of progress", 1 <= adv <= N // 1024 + 3, str(adv))
    code = r.svc.stop(10)
    ok, t0 = r.start()
    at_once = L.wait_for(lambda: r.cursor() >= N, 1.0)
    check(f"clean: after SIGTERM (exit {code}) and a start the cursor is at the end within a second of listening ({r.cursor()})", ok and code == 0 and at_once, str(r.cursor()))
    time.sleep(12)
    check(f"clean: no event is delivered again after the restart ({len(r.repeats())} of {len(r.got)}; attempts after the start {r.svc.stats()['attempts']})",
          not r.repeats() and r.svc.stats()["attempts"] == 0, str(r.repeats()[:10]))
    r.close()


def stage_kill():
    r = Rig(power_loss=True)
    r.start()
    held, done, want = r.run_up()
    check(f"kill: every one of the {want} wanted events delivered before the kill", held and done, f"{held} {done} {len(r.got)}")
    r.svc.kill()
    ok, _ = r.start()
    back = L.wait_for(lambda: r.cursor() >= N, 30)
    time.sleep(12)
    check(f"kill: after kill -9 as a power cut and a start no event is delivered again ({len(r.repeats())} of {len(r.got)}), and the cursor is back at the end ({r.cursor()})",
          ok and back and not r.repeats(), str(r.repeats()[:10]))
    r.close()


def stage_snapshot():
    r = Rig(extra=["--delivery-log-bytes", "65536"], power_loss=True)
    r.start()
    held, done, want = r.run_up()
    snaps = r.svc.stats()["snapshots"]
    check(f"snapshot: every wanted event delivered while the outcomes log was replaced ({snaps} snapshots)", held and done and snaps >= 1, f"{held} {done} {snaps}")
    r.svc.kill()
    ok1, _ = r.start()
    back1 = L.wait_for(lambda: r.cursor() >= N, 30)
    time.sleep(8)
    code = r.svc.stop(10)
    ok2, _ = r.start()
    back2 = L.wait_for(lambda: r.cursor() >= N, 1.0)
    time.sleep(8)
    check(f"snapshot: after kill -9 and a start, then SIGTERM and a start, no event is delivered again ({len(r.repeats())} of {len(r.got)})",
          ok1 and back1 and ok2 and back2 and code == 0 and not r.repeats(), f"{back1} {back2} {code} {r.repeats()[:10]}")
    r.close()


def stage_plain():
    r = Rig(types=False)
    r.start()
    held, done, want = r.run_up(types=False)
    adv = r.svc.stats().get("advanced", -1)
    code = r.svc.stop(10)
    kinds = json.loads(subprocess.run(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "logcheck.py"), "check", r.d],
                                      capture_output=True, text=True).stdout or "{}")
    found = (kinds.get("delivery") or {}).get("kinds", {})
    check(f"plain: an endpoint without a list writes no `advanced` record ({adv}; kinds in delivery.seg {found})", done and adv == 0 and "19" not in found, f"{done} {adv} {found}")
    ok, _ = r.start()
    time.sleep(6)
    check("plain: and a restart delivers nothing again", ok and code == 0 and not r.repeats(), str(r.repeats()[:10]))
    r.close()


def stage_oldbuild():
    old = os.environ.get("HOOKS_BIN_MAIN")
    if not old:
        print("skip oldbuild: HOOKS_BIN_MAIN is not set")
        return
    r = Rig()
    r.start()
    r.run_up()
    r.svc.stop(10)
    p = subprocess.run([old, "--port", str(L.chaos.free_port()), "--dir", r.d, "--allow-private-hosts", "1"], capture_output=True, text=True, timeout=30)
    check(f"oldbuild: the build before refuses the log with status 15 ({p.returncode})", p.returncode == 15, p.stderr[-300:])
    c = subprocess.run([BIN, "--port", str(L.chaos.free_port()), "--dir", r.d, "--allow-private-hosts", "1", "--compact-now", "1"], capture_output=True, text=True, timeout=60)
    svc = L.Service(old, r.d, r.args)
    ok = svc.start(timeout=30)
    check(f"oldbuild: after --compact-now (exit {c.returncode}) the build before starts on it", c.returncode == 0 and ok, c.stderr[-300:] + svc.stderr()[-300:])
    svc.kill()
    r.close()


def main():
    for name in STAGES:
        globals()[f"stage_{name}"]()
    return check.finish("advance")


if __name__ == "__main__":
    sys.exit(main())
