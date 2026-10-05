#!/usr/bin/env python3
"""The history is pruned (docs/design.md section 43): rows of the attempts table older than `history-days` are deleted, a batch at a time, by the service.

    HOOKS_PG=host:port:user:database python3 tests/prune_test.py build/hooks        (the database's endpoints and attempts are truncated)

  1. `history-days 1`: 25,000 rows two days old and 500 an hour old are put in the table by hand; the service deletes the old ones in batches of 10,000 (the first
     batch ten seconds after the database is there, the next a second after a full one) and keeps the recent ones; `/stats` counts 25,000; events posted
     meanwhile are delivered and their rows written; `/config` says 1
  2. `history-days 0`: the same old rows stay, and nothing is counted
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
TOKEN = "prunepruneprune"
check = L.Checks()


def count(where=""):
    return int(L.psql(f"select count(*) from attempts {where}")[0][0])


def seed(now_ms):
    L.psql("truncate attempts")
    L.psql(f"insert into attempts (endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms, reason) "
           f"select 999, g, 0, 1, 1, 204, {now_ms - 2 * 86400000} + g, 1, 0 from generate_series(1, 25000) g")
    L.psql(f"insert into attempts (endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms, reason) "
           f"select 998, g, 0, 1, 1, 204, {now_ms - 3600000} + g, 1, 0 from generate_series(1, 500) g")


def run(days):
    L.psql("truncate endpoints")
    peer = L.Peer("ok")
    d = tempfile.mkdtemp(prefix="hooks-prune-")
    L.psql(f"insert into endpoints (id, host, port, secret) values (1, '127.0.0.1', {peer.port}, '{L.secret()}')")
    seed(int(time.time() * 1000))
    svc = L.Service(BIN, d, ["--admin-token", TOKEN, "--history-days", str(days), *L.pg_flags()])
    ok = svc.start(timeout=30)
    return svc, peer, ok


def main():
    L.apply_schema()
    # 1.
    svc, peer, ok = run(1)
    check("1. the service starts with history-days 1", ok, svc.stderr()[-300:])
    cfg = json.loads(svc.get("/config")[1])
    check("1. /config says history-days 1", cfg.get("history-days") == 1, str(cfg.get("history-days")))
    for n in range(50):
        svc.post_event(n)
    t0 = time.time()
    gone = L.wait_for(lambda: count("where endpoint = 999") == 0, 90)
    took = time.time() - t0
    st = svc.stats()
    check(f"1. the 25,000 rows two days old are deleted ({took:.1f} s after the start, in batches)", gone, str(count("where endpoint = 999")))
    check(f"1. the 500 rows an hour old are kept ({count('where endpoint = 998')})", count("where endpoint = 998") == 500)
    check(f"1. /stats counts the rows deleted ({st.get('history_pruned')})", st.get("history_pruned") == 25000, str(st.get("history_pruned")))
    delivered = L.wait_for(lambda: svc.stats()["delivered"] >= 50 and count("where endpoint = 1") >= 50, 30)
    check("1. the events posted meanwhile are delivered and their rows written", delivered, f"{svc.stats()['delivered']} {count('where endpoint = 1')}")
    svc.stop(10)
    peer.close()
    # 2.
    svc, peer, ok = run(0)
    time.sleep(15)
    st = svc.stats()
    check(f"2. history-days 0 keeps every row ({count()} of 25,500) and counts none ({st.get('history_pruned')})", ok and count() == 25500 and st.get("history_pruned") == 0,
          f"{count()} {st.get('history_pruned')}")
    svc.stop(10)
    peer.close()
    return check.finish("prune")


if __name__ == "__main__":
    sys.exit(main())
