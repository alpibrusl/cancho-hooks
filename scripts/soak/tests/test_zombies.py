#!/usr/bin/env python3
"""The janitor of the harness, tested against a fake service and a fake table (docs/soak.md, "Validating the harness"): an endpoint the harness did not intend to be there is removed
(API and row), whatever way it got there; one it does intend is never touched; a create that was not answered is found by its port and removed, receiver and port included.

    python3 scripts/soak/tests/test_zombies.py
"""
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workers  # noqa: E402
from soak import EpInfo  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok   " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


class FakeSvc:
    inc = 1


class FakeRun:
    """What the janitor uses of a Run: a service (endpoints in memory) and a table (rows), which differ when a create was not answered."""

    def __init__(self):
        self.memory, self.table = {}, {}      # id -> port
        self.eps, self.label_of = {}, {}
        self.counts, self.events, self.violations = Counter(), [], []
        self.stop, self.stop_all = threading.Event(), threading.Event()
        self.vlock = threading.RLock()
        self.free_ports, self.recv_cmds, self.retired = [], [], []
        self.svc = FakeSvc()
        self.verifier = self
        self.delete_status = None
        self.seed = 1

    # service
    def read(self, path, timeout=3.0, raw=False):
        if path.startswith("/endpoints?"):
            return 200, [{"id": i, "port": p, "cursor": 0, "disabled": False} for i, p in sorted(self.memory.items())]
        i = int(path.rsplit("/", 1)[1])
        return (200, {"id": i}) if i in self.memory else (404, None)

    def admin(self, method, path, body=None, timeout=8.0):
        i = int(path.rsplit("/", 1)[1])
        if self.delete_status:
            return self.delete_status, None
        if i in self.memory:
            del self.memory[i]
            self.table.pop(i, None)
            return 200, {}
        return 404, None

    def psql_rows(self, sql):
        if sql.startswith("select id from endpoints where port"):
            port = int(sql.rsplit("=", 1)[1])
            return [(str(i),) for i, p in self.table.items() if p == port]
        if sql.startswith("select id from endpoints where id"):
            i = int(sql.rsplit("=", 1)[1])
            return [(str(i),)] if i in self.table else []
        if sql.startswith("delete from endpoints where id"):
            self.table.pop(int(sql.rsplit("=", 1)[1]), None)
            return []
        raise AssertionError(sql)

    # harness
    def log(self, kind, **kw):
        self.events.append((kind, kw))

    def violate(self, tag, **kw):
        self.violations.append((tag, kw))

    def recv(self, cmd, timeout=5.0):
        self.recv_cmds.append(cmd)

    def activate(self, label, c0, t, ambiguous=False):
        pass

    def retire(self, label, t):
        self.retired.append(label)

    def drop_endpoint(self, ep, verified_gone=True, quarantine=30.0, delay=12.0):
        import soak
        soak.Run.drop_endpoint(self, ep, verified_gone, quarantine, 0.0)

    def release_receiver(self, ep, verified_gone, quarantine=30.0, delay=12.0):
        import soak
        soak.Run.release_receiver(self, ep, verified_gone, quarantine, 0.0)

    def add_intended(self, sid, port, label):
        ep = EpInfo(label, sid, "churn", [], port)
        ep.svc_id = sid
        self.eps[label] = ep
        self.label_of[sid] = label
        self.memory[sid] = self.table[sid] = port
        return ep


def sweep(run, en, times=2):
    """The Enabler's own step, as it runs every 1.5 s; what it has seen for less than 5 s is waited for, so the first pass notes it and is back-dated."""
    for _ in range(times):
        en.step()
        for k in list(en.unknown):
            en.unknown[k] -= 10.0


def main():
    # 1. an endpoint nobody told the harness of is removed from the service and from the table; an intended one stays
    r = FakeRun()
    keep = r.add_intended(1, 24001, "healthy0")
    r.memory[7] = r.table[7] = 24007
    en = workers.Enabler(r)
    sweep(r, en)
    check("an unknown endpoint is removed from the service and the table", 7 not in r.memory and 7 not in r.table, str(r.memory))
    check("an intended endpoint is not touched", 1 in r.memory and 1 in r.table and keep.active)
    check("the removal is a harness event with its reason", any(k == "harness-event" and kw.get("what") == "endpoint-removed" and kw.get("svc_id") == 7 for k, kw in r.events), str(r.events))
    check("it is counted", r.counts["zombies_removed"] == 1)

    # 2. a retired endpoint that is back (the next start loaded the row): removed, its port not given back twice
    r = FakeRun()
    ep = r.add_intended(2, 24002, "churn0-1")
    ep.active, ep.retired_at, ep.released = False, time.time() - 60, True
    en = workers.Enabler(r)
    sweep(r, en)
    check("a retired endpoint that the service has again is removed", 2 not in r.memory and 2 not in r.table)
    check("and its port is not given back a second time", not r.free_ports, str(r.free_ports))

    # 3. an unanswered create whose row was made: found by its port, removed (the service never knew it: the API says 404, the row still goes), receiver removed, port reusable
    r = FakeRun()
    ep = EpInfo("churn1-1", 5, "churn", [], 24005)
    r.eps["churn1-1"] = ep
    r.table[55] = 24005                  # the row is there; the service's memory does not have it
    ch = workers.Churn(r, 1)
    orig_sleep = time.sleep
    workers.time.sleep = lambda s: None
    try:
        ch.reconcile(ep)
    finally:
        workers.time.sleep = orig_sleep
    check("an unanswered create is found by its port and its row is deleted", 55 not in r.table, str(r.table))
    check("its receiver is taken away", any(c.get("op") == "remove" and c.get("label") == "churn1-1" for c in r.recv_cmds), str(r.recv_cmds))
    check("and its port can be used again", [p for _, p in r.free_ports] == [24005], str(r.free_ports))
    check("it is never judged (ambiguous) and is retired", "churn1-1" in r.retired)

    # 4. a delete the service will not take: not hidden. A violation, and the port is NOT given back
    r = FakeRun()
    ep = r.add_intended(9, 24009, "churn2-1")
    r.delete_status = 503
    ok = workers.remove_endpoint(r, 9, 24009, label="churn2-1", wait_s=0.2)
    check("a delete that fails is a violation and not a success", not ok and any(t == "P_zombie_endpoint" for t, _ in r.violations), str(r.violations))

    print("zombies: all checks passed" if all(RESULTS) else "zombies: FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
