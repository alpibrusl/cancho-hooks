#!/usr/bin/env python3
"""The harness's backup step against a fake backup.sh (docs/soak.md, "Validating the harness"): a backup that takes too long is killed with everything it started, its `.partial`
directory is removed, the timeout scales with the size of the data directory, and only the newest backups are kept.

    python3 scripts/soak/tests/test_backup.py
"""
import os
import sys
import tempfile
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common  # noqa: E402
import workers  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


class Poster:
    max_id = 0


class FakeRun:
    def __init__(self, root):
        self.out = os.path.join(root, "out")
        self.datadir = os.path.join(root, "data")
        os.makedirs(self.datadir)
        open(os.path.join(self.datadir, "events.seg"), "wb").write(b"x" * 3 * 1048576)
        self.vlock = threading.RLock()
        self.poster = Poster()
        self.pg = {"db": "d", "host": "h", "port": 1, "user": "u"}
        self.counts, self.events, self.violations = Counter(), [], []

    def pg_env(self):
        return dict(os.environ)

    def log(self, kind, **kw):
        self.events.append((kind, kw))

    def violate(self, tag, **kw):
        self.violations.append((tag, kw))


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    root = tempfile.mkdtemp(prefix="backup-test-")
    scripts = os.path.join(root, "root", "scripts")
    os.makedirs(scripts)
    pidfile = os.path.join(root, "child.pid")
    # a backup that makes its partial directory, starts a child that outlives it (as `cp` and the checker did), and never finishes
    open(os.path.join(scripts, "backup.sh"), "w").write(f'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do case $1 in --out) out=$2; shift 2;; *) shift;; esac; done
mkdir -p "$out/.hooks-backup-x.partial"; head -c 1000 /dev/zero > "$out/.hooks-backup-x.partial/events.seg"
sleep 300 &
echo $! > {pidfile}
sleep 300
''')
    common.ROOT = os.path.join(root, "root")
    r = FakeRun(root)
    orig = workers.backup_timeout
    workers.backup_timeout = lambda d, n: (1.5, 3.0)
    t0 = time.time()
    res = workers.run_backup(r)
    took = time.time() - t0
    workers.backup_timeout = orig
    time.sleep(0.3)
    child = int(open(pidfile).read())
    check("a backup that overruns is stopped at its timeout", res is None and took < 5 and any(t == "K_backup" for t, _ in r.violations), f"{res} {took} {r.violations}")
    check("and the processes it started with it", not alive(child), f"pid {child} is still there")
    check("(the partial directory was there to be removed)", os.path.isdir(os.path.join(r.out, "backups")))
    left = [n for n in os.listdir(os.path.join(r.out, 'backups')) if n.endswith(".partial")]
    check("and the .partial directory is removed", not left, str(left))
    ev = [kw for k, kw in r.events if k == "backup"]
    check("the timeout and the size it was scaled from are logged", ev and ev[0].get("timeout_s") == 2 and ev[0].get("data_mb") == 3.0 and "checker" in ev[0], str(ev))
    if alive(child):
        os.kill(child, 9)

    # the timeout scales with the data directory
    small, mb_small = workers.backup_timeout(r.datadir, False)
    open(os.path.join(r.datadir, "events-1.seg"), "wb").write(b"x" * 200 * 1048576)
    big, mb_big = workers.backup_timeout(r.datadir, False)
    nat, _ = workers.backup_timeout(r.datadir, True)
    check("the timeout grows with the data directory and is shorter for the native checker", big > small + 100 and nat < big and mb_big > 190, f"{small} {big} {nat}")
    os.remove(os.path.join(r.datadir, "events-1.seg"))

    # a backup that succeeds: only the newest BACKUPS_KEPT are kept
    open(os.path.join(scripts, "backup.sh"), "w").write('''#!/bin/bash
out=""
while [ $# -gt 0 ]; do case $1 in --out) out=$2; shift 2;; *) shift;; esac; done
mkdir -p "$out/hooks-backup-$(python3 -c 'import time;print(time.time_ns())')"; sleep 0.05
''')
    r2 = FakeRun(tempfile.mkdtemp(prefix="backup-test2-"))
    for _ in range(5):
        workers.run_backup(r2)
    kept = [n for n in os.listdir(os.path.join(r2.out, 'backups')) if n.startswith("hooks-backup-")]
    check(f"only the newest {workers.BACKUPS_KEPT} backups are kept", len(kept) == workers.BACKUPS_KEPT and not r2.violations, f"{kept} {r2.violations}")

    print("backup: all checks passed" if all(RESULTS) else "backup: FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
