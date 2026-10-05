#!/usr/bin/env python3
"""The database goes away and comes back (docs/design.md section 37): the service reconnects by itself, never waits for the database, and loses or repeats nothing.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/pgre_test.py build/hooks [stage ...]

PostgreSQL sits behind tests/pgproxy.py (cut, refuse, freeze, black-hole, restore) and `pg_terminate_backend` is aimed only at the proxy's own connections. Nothing
here restarts or stops the server.

  1. idle: cut, then restored: /readyz is 503 (database) and then 200 with no restart; the connections, losses, reconnects and failed attempts are counted; the
     routes about endpoints still answer from memory; an event after the outage is delivered and its row written
  2. events flowing through an outage (60 a second, 3 s away; then 100 a second, 5 s away): every event is delivered exactly once; every history row is in exactly one
     of written, failed, dropped or still queued; a short outage drops none (the queue is 256 rows), a long one drops the rest and counts them; what was written is in the
     table, and what waited is written after the outage
  3. a management request in flight when the connection goes (a trigger holds the insert): 504 with "may have been stored" at once, not five seconds later; with the
     database gone: 503 at once; after it is back: POST /endpoints and /schedules work again
  4. cron: a schedule due every second through an outage and through backends ended under it: no fire twice (the keys are unique), fires resume
  5. the start with the database away: it listens at once, takes events (202) and delivers none; /readyz and the routes about endpoints are 503; when the database
     comes the endpoints load, with no restart, and every event is delivered once; the same through a black hole (the SYN dropped); and with a log that has cursors,
     the events delivered before are not sent again; pg-start-wait-ms 0 waits for ever; a stop while waiting exits 0
  6. a login the server refuses after the service has run: it backs off (attempts are counted, not hundreds), burns no CPU, and recovers when the server accepts again
  7. the loop never stalls: the longest time a probe waited for /healthz while the database was black-holed, frozen, cut, killed and coming back
  8. pg-request-ms: a connection that goes silent with a request on it is given up, the request is answered 503, and a new one is made
  9. backends ended with pg_terminate_backend while idle: replaced at once
 10. the sleep bound of the loop includes the pool's: with waits of 2 ms the attempts to reconnect are made every few ms, not every 50 ms turn
 11. a table whose answer does not fit the pool's 1 MiB slab ends the start with status 20, `too large`, instead of asking again for ever
 12. the read of the table takes a while (a view that sleeps): in the meantime /readyz is 503 and the routes about endpoints are 503 although a connection is live;
     and a connection lost in the middle of the read is asked again, and the endpoints load
 13. (with HOOKS_PG_PASSWORD, i.e. a server that wants SCRAM-SHA-256) the logins of the reconnects are SCRAM: backends ended six times in a row, the
     longest wait of a probe on /healthz, no attempt failed; a wrong password at the start is refused at once, status 20, `cannot log in`
 15. retention (design 38) waits for the endpoints: with the database away and retention at 1 ms no segment and no event is dropped, every event is delivered
     once after the table is read and only then are segments dropped; `compact-now` with a database named reads the table first (status 20 and nothing changed if it cannot in pg-start-wait-ms)
 14. (with HOOKS_PG_STOP and HOOKS_PG_START, shell commands that stop and start the server under test) a server that really goes away and comes back: events
     flowing, every one delivered once, ready again with no restart, the history accounted. Never run against a server that others use
"""
import atexit
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
STAGES = sys.argv[2:]
check = L.Checks()
TOKEN = "pgre-admin-token-1"
AUTH = {"Authorization": "Bearer " + TOKEN}
CLEAN = []


@atexit.register
def _clean():
    for svc in CLEAN:
        try:
            svc.kill()
        except Exception:  # noqa: BLE001
            pass


def reset(endpoints=()):
    L.apply_schema()
    L.psql("truncate endpoints, attempts")
    L.psql("truncate schedules restart identity")
    for i, port in endpoints:
        L.psql(f"insert into endpoints values ({i}, '127.0.0.1', {port}, '{L.secret()}')")


def make(proxy, extra=(), d=None, token=True):
    d = d or L.free_dir("hooks-pgre-")
    args = ["--schedule", "200", "--deadline-ms", "800", *L.pg_flags(proxy.port if proxy else None)]
    if token:
        args += ["--admin-token", TOKEN]
    svc = L.Service(BIN, d, args + list(extra))
    CLEAN.append(svc)
    return svc


def readyz(svc):
    try:
        st, data = svc.get("/readyz", timeout=3)
        return st, json.loads(data)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def ready(svc):
    return readyz(svc)[0] == 200


def timed_readyz(svc, want, secs):
    """How long until /readyz says `want` (200 or 503), or None."""
    t = time.time()
    return round(time.time() - t, 2) if L.wait_for(lambda: readyz(svc)[0] == want, secs, 0.02) else None


def scount(table="attempts"):
    return int(L.psql(f"select count(*) from {table}")[0][0])


def req(svc, method, path, body=None, auth=True, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    t = time.time()
    st, out, _ = svc.request(method, path, data, dict(AUTH) if auth else {}, timeout)
    try:
        out = json.loads(out)
    except ValueError:
        pass
    return st, out, time.time() - t


def accounted(svc):
    """The history's accounting: every attempt that ended is a row, and a row is in exactly one place."""
    s = svc.stats()
    return s["attempts"], s["history_written"] + s["history_failed"] + s["history_dropped"] + s["history_queue"], s


class Sender(threading.Thread):
    """POST /events at `rate` a second until stopped; the ids the service acknowledged."""

    def __init__(self, svc, rate):
        super().__init__(daemon=True)
        self.svc, self.gap, self.ids, self.refused, self.go = svc, 1.0 / rate, [], 0, True

    def run(self):
        n = 0
        nxt = time.time()
        while self.go:
            n += 1
            try:
                st, data = self.svc.post_event(n, timeout=5)
                if st == 202:
                    self.ids.append(json.loads(data)["id"])
                else:
                    self.refused += 1
            except Exception:  # noqa: BLE001
                self.refused += 1
            nxt += self.gap
            time.sleep(max(0.0, nxt - time.time()))


class Probe(threading.Thread):
    """How long the loop made a caller wait: GET /healthz over and over (a new connection each time, so the accept path is in it), the longest answer."""

    def __init__(self, svc):
        super().__init__(daemon=True)
        self.svc, self.go, self.worst, self.slow, self.n, self.marks = svc, True, 0.0, 0, 0, {}
        self.window = 0.0

    def run(self):
        while self.go:
            t = time.time()
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.svc.port, timeout=5)
                c.request("GET", "/healthz")
                c.getresponse().read()
                c.close()
            except Exception:  # noqa: BLE001
                pass
            took = time.time() - t
            self.n += 1
            self.worst = max(self.worst, took)
            self.window = max(self.window, took)
            if took > 0.05:
                self.slow += 1
            time.sleep(0.002)

    def phase(self, name):
        """Close the current window under `name` and open the next."""
        self.marks[name] = round(self.window * 1000)
        self.window = 0.0


# ---- 1 ----------------------------------------------------------------------------------------------------------------

def stage1():
    print("== 1. idle: cut, then restored", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy)
    check("1. the service starts through the proxy and has read its endpoints", svc.start() and ready(svc), svc.stderr())
    pid = svc.proc.pid
    m = svc.metrics()
    check("1. two connections, none lost, the endpoints loaded", m.value("hooks_history_connections") == 2 and m.value("hooks_database_connection_losses_total") == 0
          and m.value("hooks_endpoints_loaded") == 1, str(m.series("hooks_history_connections")))
    check("1. it says so on stderr", svc.has_line("hooks: endpoints loaded: 1") and svc.has_line("hooks: the database: 2 of 2 connections live"), svc.stderr())
    t_cut = time.time()
    proxy.cut()
    went = timed_readyz(svc, 503, 10)
    st, body = readyz(svc)
    check("1. cut: /readyz is 503, check database, within a second or two (it is noticed with no traffic)", went is not None and went < 3 and body.get("check") == "database", str((went, st, body)))
    time.sleep(1.5)
    check("1. ... /healthz is 200 and the endpoints are known from memory (GET /endpoints 200)", svc.get("/healthz")[0] == 200 and svc.get("/endpoints")[0] == 200)
    s = svc.stats()
    check("1. ... the losses and the failed attempts to reconnect are counted", s["database_losses"] == 2 and s["database_failures"] >= 2 and s["history_live"] == 0, str(s))
    t_back = time.time()
    proxy.restore()
    back = timed_readyz(svc, 200, 15)
    check("1. restored: /readyz is 200 again with no restart (the process is the same)", back is not None and svc.alive() and svc.proc.pid == pid, str(back))
    print(f"INFO 1. the database was away {t_back - t_cut:.1f} s; /readyz was 200 again {back} s after it came back", flush=True)
    check("1. two connections live, two reconnects", L.wait_for(lambda: svc.stats()["history_live"] == 2, 5) and svc.stats()["database_reconnects"] == 2, str(svc.stats()))
    check("1. stderr says it was lost and found", svc.has_line("hooks: the database: 0 of 2 connections live") and svc.stderr().count("hooks: the database: 2 of 2 connections live") >= 2, svc.stderr())
    before = scount()
    status, _ = svc.post_event(1)
    check("1. an event after the outage is delivered and its row is written", status == 202 and L.wait_for(lambda: peer.distinct() == {1}, 10) and L.wait_for(lambda: scount() == before + 1, 10))
    check("1. nothing was restarted: one `endpoints loaded` line", svc.stderr().count("endpoints loaded") == 1)
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 2 ----------------------------------------------------------------------------------------------------------------

def flow(rate, away, label, expect_drops):
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy)
    svc.start()
    sender = Sender(svc, rate)
    sender.start()
    time.sleep(1.0)
    proxy.cut()
    time.sleep(away)
    mid = svc.stats()
    proxy.restore()
    check(f"2. {label}: /readyz is 200 again with no restart", timed_readyz(svc, 200, 20) is not None)
    time.sleep(1.5)
    sender.go = False
    sender.join(5)
    ids = list(sender.ids)
    check(f"2. {label}: every event the service acknowledged ({len(ids)}) is delivered", L.wait_for(lambda: peer.distinct() >= set(ids) and svc.stats()["delivered"] >= len(ids), 30),
          f"{len(ids)} {len(peer.distinct())} {svc.stats()}")
    time.sleep(1.0)
    ev = peer.events()
    check(f"2. {label}: ... each exactly once (no double delivery: {len(ev)} deliveries of {len(set(ev))} events)", len(ev) == len(set(ev)) == len(ids), f"{len(ev)} {len(set(ev))} {len(ids)}")
    check(f"2. {label}: none refused while the database was away (ingest does not need it)", sender.refused == 0, str(sender.refused))
    ok = L.wait_for(lambda: svc.stats()["history_queue"] == 0 and accounted(svc)[0] == accounted(svc)[1] and svc.stats()["history_live"] == 2, 20)
    attempts, placed, s = accounted(svc)
    check(f"2. {label}: every attempt's row is in exactly one of written, failed, dropped (queue empty): {attempts} = {s['history_written']} + {s['history_failed']} + {s['history_dropped']}",
          ok and attempts == len(ids) and placed == attempts, str(s))
    if expect_drops:
        check(f"2. {label}: the queue is bounded (256): the rows beyond it were dropped and counted ({s['history_dropped']})", s["history_dropped"] > 0 and mid["history_queue"] <= 256, str((mid, s)))
        check(f"2. {label}: ... and the queue was full while the database was away ({mid['history_queue']})", mid["history_queue"] >= 200, str(mid))
    else:
        check(f"2. {label}: nothing was dropped: the rows waited ({mid['history_queue']} in the queue at the end of the outage) and were written", s["history_dropped"] == 0 and mid["history_queue"] > 0, str((mid, s)))
    n = scount()
    check(f"2. {label}: the table has what was written ({n}): at least the written rows, at most those and the ones that were in flight when the connection went ({s['history_failed']})",
          s["history_written"] <= n <= s["history_written"] + s["history_failed"], str((n, s)))
    check(f"2. {label}: the failed rows are only those on the wire when it went (at most the pool's depth, 64 a connection)", s["history_failed"] <= 128, str(s))
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


def stage2():
    print("== 2. events flowing through an outage", flush=True)
    flow(60, 3.0, "60 a second, away 3 s", False)
    flow(100, 5.0, "100 a second, away 5 s", True)


# ---- 3 ----------------------------------------------------------------------------------------------------------------

def stage3():
    print("== 3. a management request in flight", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy)
    svc.start()
    L.psql("create or replace function pgre_sleep() returns trigger as $$ begin perform pg_sleep(4); return new; end $$ language plpgsql")
    L.psql("create trigger pgre_sleep before insert on endpoints for each row execute function pgre_sleep()")
    L.psql("create trigger pgre_sleep before insert on schedules for each row execute function pgre_sleep()")
    try:
        for what, path, body in (("POST /endpoints", "/endpoints", {"host": "127.0.0.1", "port": peer.port}),
                                 ("POST /schedules", "/schedules", {"expr": "* * * * *", "type": "pgre.t"})):
            out = {}

            def ask(path=path, body=body, out=out):
                out["r"] = req(svc, "POST", path, body, timeout=15)

            th = threading.Thread(target=ask)
            th.start()
            time.sleep(0.8)
            t_cut = time.time()
            proxy.cut()
            th.join(15)
            st, msg, took = out["r"]
            text = json.dumps(msg)
            check(f"3. {what} in flight when the connection goes: 504 'may have been stored', within a second of the cut (not at the five-second deadline)",
                  st == 504 and "may have been stored" in text and time.time() - t_cut < 1.5, str((st, msg, round(time.time() - t_cut, 2))))
            check(f"3. ... and it is not the 503 of 'the database is unavailable'", st != 503)
            st2, msg2, took2 = req(svc, "POST", path, body)
            check(f"3. {what} with the database gone: 503 at once ('cannot be stored now'), nothing was sent", st2 == 503 and "cannot be stored now" in json.dumps(msg2) and took2 < 1.0, str((st2, msg2, took2)))
            proxy.restore()
            check(f"3. {what}: the database is back, /readyz 200", timed_readyz(svc, 200, 20) is not None)
            time.sleep(4.5)   # the backend that was in the trigger finishes and commits: the row may be there
        st, msg, _ = req(svc, "GET", "/schedules")
        check("3. GET /schedules works again (200)", st == 200, str((st, msg)))
        L.psql("drop trigger pgre_sleep on endpoints")
        L.psql("drop trigger pgre_sleep on schedules")
        st, msg, _ = req(svc, "POST", "/endpoints", {"host": "127.0.0.1", "port": peer.port})
        check("3. POST /endpoints works again (201), no restart", st == 201 and "secret" in msg, str((st, msg)))
        st, msg, _ = req(svc, "POST", "/schedules", {"expr": "* * * * *", "type": "pgre.t"})
        check("3. POST /schedules works again (201)", st == 201, str((st, msg)))
        in_table = scount("endpoints")
        known = svc.stats()["endpoints"]
        print(f"INFO 3. the table has {in_table} endpoints and the service knows {known}: a change whose outcome was unknown is in the table (the backend finished it) and "
              f"the running service does not know it until a restart", flush=True)
        check("3. the service knows the original and the new endpoint (the one of unknown outcome is not in memory)", known == 2 and in_table >= 2, str((known, in_table)))
    finally:
        for t in ("endpoints", "schedules"):
            try:
                L.psql(f"drop trigger if exists pgre_sleep on {t}")
            except RuntimeError:
                pass
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 4 ----------------------------------------------------------------------------------------------------------------

def fires(d):
    path = os.path.join(d, "events.seg")
    recs, _ = L.chaos.read_log(open(path, "rb").read()) if os.path.exists(path) else ([], 0)
    out = []
    for _ms, pairs in recs:
        kv = dict(pairs)
        ev = json.loads(kv[b"event"])
        if "scheduled_at" in ev:
            out.append((ev["schedule"], ev["scheduled_at"], kv.get(b"key", b"").decode()))
    return out


def stage4():
    print("== 4. cron through an outage", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy, extra=["--cron-seconds", "1"])
    svc.start()
    st, msg, _ = req(svc, "POST", "/schedules", {"expr": "* * * * * *", "type": "pgre.cron"})
    check("4. a schedule due every second", st == 201, str((st, msg)))
    sid = msg["id"]
    time.sleep(3.0)
    n0 = len(fires(svc.dir))
    check("4. it fires (about one a second)", 2 <= n0 <= 5, str(n0))
    t_cut = int(time.time())
    proxy.cut()
    time.sleep(4.0)
    during = len(fires(svc.dir))
    check("4. while the database is away it fires nothing (it cannot ask what is due)", during - n0 <= 1, str((n0, during)))
    check("4. ... and it does not ask (no connection is live): the cycles it did not start are not errors", svc.stats()["cron_errors"] <= 1, str(svc.stats()))
    t_back = int(time.time())
    proxy.restore()
    check("4. the database is back: /readyz 200", timed_readyz(svc, 200, 20) is not None)
    time.sleep(3.0)
    after = fires(svc.dir)
    check("4. it fires again after the outage, with no restart", len(after) > during and max(s for _, s, _ in after) >= t_back, str((during, len(after), t_back)))
    # backends ended under it, repeatedly, for a while: updates are lost with their outcome unknown, the next cycle re-evaluates
    end = time.time() + 12
    killed = 0
    while time.time() < end:
        killed += proxy.kill_backends(L.psql)
        time.sleep(0.45)
    time.sleep(3.0)
    check("4. backends ended 12 s long (%d ended): the service is ready again" % killed, ready(svc) or L.wait_for(lambda: ready(svc), 15), str(readyz(svc)))
    time.sleep(3.0)
    allf = fires(svc.dir)
    keys = [k for _, _, k in allf]
    seconds = [(s, sec) for s, sec, _ in allf]
    check("4. no fire twice: every (schedule, second) once and every key once (%d fires)" % len(allf), len(set(seconds)) == len(seconds) and len(set(keys)) == len(keys) and all(k == f"cron:{s}:{sec}" for s, sec, k in allf), str(len(allf)))
    check("4. ... the events in the log are the fires (nothing else wrote one)", all(s == sid for s, _, _ in allf))
    last = int(L.psql("select last_fired from schedules where id = %d" % sid)[0][0])
    check("4. the table's last_fired is the last second fired, or the one before (an update may wait for the next cycle)", last >= max(sec for _, sec in seconds) - 3, str((last, max(sec for _, sec in seconds))))
    check("4. it still fires after all that", L.wait_for(lambda: len(fires(svc.dir)) > len(allf), 8))
    s = svc.stats()
    print(f"INFO 4. cron_fired {s['cron_fired']} cron_errors {s['cron_errors']} cron_skipped {s['cron_skipped']}; {len(allf)} fires in the log from t={t_cut}", flush=True)
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 5 ----------------------------------------------------------------------------------------------------------------

def stage5():
    print("== 5. the start with the database away", flush=True)
    # a. cut: the connection is refused
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    proxy.cut()
    svc = make(proxy, extra=["--pg-start-wait-ms", "60000"])
    t = time.time()
    up = svc.start(loaded=False)
    check("5a. the database refuses connections: the service listens at once (does not wait)", up and time.time() - t < 3, f"{up} {time.time() - t:.1f}")
    pid = svc.proc.pid
    time.sleep(0.6)
    st, body = readyz(svc)
    check("5a. ... /readyz is 503, check database", st == 503 and body.get("check") == "database", str((st, body)))
    st, data = svc.get("/endpoints")
    check("5a. ... GET /endpoints is 503: the endpoints are not loaded", st == 503 and b"not loaded" in data, str((st, data)))
    codes = [svc.request(m, p, b"{}", {**AUTH})[0] for m, p in (("POST", "/endpoints"), ("PATCH", "/endpoints/0"), ("DELETE", "/endpoints/0"), ("POST", "/endpoints/0/enable"), ("POST", "/events/1/replay"),
                                                                                                             ("GET", "/endpoints/0/dead"), ("POST", "/endpoints/0/replay-dead"), ("DELETE", "/events/1/replay/0"), ("DELETE", "/endpoints/0/replays"))]
    check("5a. ... so are the routes that change or use them, and the four of dead letters and cancelling a replay (design 39.1)", codes == [503] * 9, str(codes))
    ids = []
    for n in range(1, 6):
        s, data = svc.post_event(n)
        ids.append((s, json.loads(data)["id"]))
    check("5a. ... but an event is taken (202) and stored", all(s == 202 for s, _ in ids), str(ids))
    time.sleep(1.0)
    check("5a. ... nothing is delivered while the endpoints are unknown", peer.count() == 0 and svc.stats()["endpoints_loaded"] is False and svc.stats()["delivered"] == 0, str(svc.stats()))
    check("5a. ... GET /metrics: endpoints_loaded 0, ready 0; the failures are counted", svc.metrics().value("hooks_endpoints_loaded") == 0 and svc.metrics().value("hooks_ready") == 0
          and svc.stats()["database_failures"] >= 2, str(svc.stats()))
    proxy.restore()
    ok = L.wait_for(lambda: svc.has_line("endpoints loaded: 1"), 20)
    check("5a. the database comes: the endpoints load, with no restart (the same process)", ok and svc.proc.pid == pid and svc.alive(), svc.stderr())
    check("5a. ... /readyz is 200", L.wait_for(lambda: ready(svc), 5), str(readyz(svc)))
    check("5a. ... the five events are delivered, each once", L.wait_for(lambda: peer.distinct() == {i for _, i in ids}, 15) and peer.count() == 5, str((peer.events(), ids)))
    check("5a. ... GET /endpoints is 200 and shows the endpoint", svc.get("/endpoints")[0] == 200 and svc.stats()["endpoints"] == 1)
    svc.stop()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)

    # b. black hole at the start (the SYN is dropped), with a probe on the loop
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy.close()
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT), backlog=1)
    proxy.blackhole()
    svc = make(proxy, extra=["--pg-start-wait-ms", "60000", "--pg-attempt-ms", "1000"])
    t = time.time()
    up = svc.start(loaded=False)
    check("5b. a black hole (the SYN dropped): the service listens at once", up and time.time() - t < 3, f"{up} {time.time() - t:.1f}")
    probe = Probe(svc)
    probe.start()
    time.sleep(5.0)
    probe.phase("black hole at the start, 5 s of attempts that run out of time")
    s = svc.stats()
    check("5b. ... attempts are made and run out of time (not blocked): %d failed attempts in 5 s" % s["database_failures"], 3 <= s["database_failures"] <= 30, str(s))
    check("5b. ... the loop answered throughout: the longest wait for /healthz was %d ms (limit 250)" % round(probe.worst * 1000), probe.worst < 0.25 and probe.n > 100, str((probe.worst, probe.n)))
    proxy.restore()
    check("5b. restored: the endpoints load and /readyz is 200 (at most 'pg-attempt-ms + the wait' later)", L.wait_for(lambda: svc.has_line("endpoints loaded: 1") and ready(svc), 20))
    probe.phase("restored")
    probe.go = False
    probe.join(3)
    print(f"INFO 5b. longest wait for /healthz by phase (ms): {probe.marks}; {probe.n} probes, {probe.slow} over 50 ms", flush=True)
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)

    # c. a log with cursors: what was delivered is not sent again
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    d = L.free_dir("hooks-pgre-")
    svc = make(proxy, d=d)
    svc.start()
    first = [json.loads(svc.post_event(n)[1])["id"] for n in range(1, 4)]
    check("5c. three events delivered by the first run", L.wait_for(lambda: peer.distinct() == set(first), 10) and L.wait_for(lambda: svc.stats()["delivered"] == 3, 10))
    time.sleep(0.5)
    svc.stop()
    proxy.blackhole()
    svc = make(proxy, d=d, extra=["--pg-start-wait-ms", "60000", "--pg-attempt-ms", "1000"])
    svc.start(loaded=False)
    more = [json.loads(svc.post_event(n)[1])["id"] for n in range(4, 7)]
    time.sleep(1.0)
    check("5c. the second run starts with the database black-holed, takes three more events and delivers none", peer.count() == 3 and len(more) == 3, str(peer.events()))
    proxy.restore()
    check("5c. the database comes: the three new events are delivered once each, the first three are not sent again",
          L.wait_for(lambda: peer.distinct() == set(first + more), 20) and L.wait_for(lambda: peer.count() == 6, 5), str(peer.events()))
    time.sleep(1.0)
    check("5c. ... and still exactly six deliveries", peer.count() == 6 and svc.stats()["delivered"] == 3, str((peer.events(), svc.stats())))
    svc.stop()
    proxy.close()
    peer.close()

    # d. pg-start-wait-ms 0 waits for ever; a stop while waiting is a clean exit
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    proxy.cut()
    svc = make(proxy, extra=["--pg-start-wait-ms", "0"])
    svc.start(loaded=False)
    time.sleep(4.0)
    check("5d. pg-start-wait-ms 0: after four seconds without a database it is still there, still not ready", svc.alive() and readyz(svc)[0] == 503)
    t = time.time()
    code = svc.stop()
    check("5d. SIGTERM while it waits for the database: exit 0, promptly", code == 0 and time.time() - t < 3, f"{code} {time.time() - t:.1f}")
    proxy.close()
    # e. a login the server refuses for good: the end, at once, with 20
    t = time.time()
    svc = make(None, extra=["--pg-start-wait-ms", "60000", "--pg-user", "pgre_no_such_role"])
    svc.start(timeout=10)
    code = svc.wait_exit(10)
    check("5e. a role that does not exist is refused for good: status 20, `cannot log in`, long before pg-start-wait-ms", code == 20 and svc.has_line("cannot log in") and time.time() - t < 5, f"{code} {svc.stderr()}")
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 6 ----------------------------------------------------------------------------------------------------------------

def cpu(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def stage6():
    print("== 6. a login the server refuses after the service has run", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    role = "pgre_rt"
    L.psql(f"drop owned by {role}") if L.psql(f"select 1 from pg_roles where rolname = '{role}'") else None
    L.psql(f"drop role if exists {role}")
    L.psql(f"create role {role} login" + (" password 'pgre-rt-pw'" if L.PG_PASSWORD else ""))
    L.psql(f"grant all on all tables in schema public to {role}")
    L.psql(f"grant all on all sequences in schema public to {role}")
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    args = ["--schedule", "200", "--deadline-ms", "800", "--pg-host", L.PG_HOST, "--pg-port", str(proxy.port), "--pg-user", role, "--pg-database", L.PG_DB,
            "--pg-start-wait-ms", "60000", "--admin-token", TOKEN] + (["--pg-password", "pgre-rt-pw"] if L.PG_PASSWORD else [])
    svc = L.Service(BIN, L.free_dir("hooks-pgre-"), args)
    CLEAN.append(svc)
    try:
        check("6. the service runs as a role of its own", svc.start() and ready(svc), svc.stderr())
        pid = svc.proc.pid
        L.psql(f"alter role {role} nologin")
        proxy.kill_backends(L.psql)
        check("6. the role may no longer log in and its connections are ended: /readyz is 503", timed_readyz(svc, 503, 10) is not None)
        f0, c0, t0 = svc.stats()["database_failures"], cpu(pid), time.time()
        time.sleep(6.0)
        s = svc.stats()
        n = s["database_failures"] - f0
        used = cpu(pid) - c0
        check("6. in six seconds it tried %d times (waits of 100 ms doubling to 5 s, two connections: about 14), it does not spin" % n, 4 <= n <= 24, str(s))
        check("6. ... and used %.3f s of CPU doing it (limit 0.3)" % used, used < 0.3)
        check("6. ... the service is alive, answers, and has not ended (a service that has run never gives up)", svc.alive() and svc.get("/healthz")[0] == 200 and svc.proc.pid == pid)
        L.psql(f"alter role {role} login")
        back = timed_readyz(svc, 200, 15)
        check("6. the role may log in again: /readyz is 200 within the longest wait (5 s) and an attempt, with no restart", back is not None and svc.proc.pid == pid, str(back))
    finally:
        svc.stop()
        L.psql(f"drop owned by {role}")
        L.psql(f"drop role if exists {role}")
        proxy.close()
        peer.close()
        shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 7 ----------------------------------------------------------------------------------------------------------------

def stage7():
    print("== 7. the loop never stalls", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT), backlog=1)
    svc = make(proxy, extra=["--pg-attempt-ms", "1500", "--pg-request-ms", "2000", "--pg-backoff-min-ms", "50", "--pg-backoff-max-ms", "400"])
    svc.start()
    sender = Sender(svc, 80)
    sender.start()
    probe = Probe(svc)
    probe.start()
    time.sleep(2.0)
    probe.phase("steady")
    proxy.blackhole()
    time.sleep(7.0)
    probe.phase("black hole 7 s (frozen connections, dials that hang, requests that time out)")
    proxy.restore()
    check("7. restored", timed_readyz(svc, 200, 20) is not None)
    time.sleep(1.0)
    probe.phase("restored")
    proxy.cut()
    time.sleep(4.0)
    probe.phase("cut 4 s (refused, backing off)")
    proxy.restore()
    timed_readyz(svc, 200, 20)
    for _ in range(6):
        proxy.kill_backends(L.psql)
        time.sleep(0.8)
    probe.phase("six backends ended in a row (reconnect at once, logins)")
    time.sleep(1.0)
    sender.go = False
    probe.go = False
    probe.join(3)
    sender.join(3)
    probe.phase("end")
    s = svc.stats()
    worst = max(probe.marks.values())
    print(f"INFO 7. the longest wait for /healthz by phase (ms): {probe.marks}; {probe.n} probes, {probe.slow} over 50 ms; load average {os.getloadavg()[0]:.1f}", flush=True)
    check("7. the loop never stalled: the longest wait for /healthz in any phase was %d ms (limit 250)" % worst, worst < 250 and probe.n > 500, str((probe.marks, probe.n)))
    check("7. ... with events flowing all the while: each acknowledged event delivered once", L.wait_for(lambda: peer.distinct() >= set(sender.ids), 20) and peer.count() == len(set(peer.events())),
          f"{len(sender.ids)} {peer.count()} {len(peer.distinct())} {s}")
    check("7. ... the pool counted what happened: losses %d, reconnects %d, failed attempts %d" % (s["database_losses"], s["database_reconnects"], s["database_failures"]),
          s["database_losses"] >= 6 and s["database_reconnects"] >= 6 and s["database_failures"] >= 10, str(s))
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 8 ----------------------------------------------------------------------------------------------------------------

def stage8():
    print("== 8. a connection that goes silent with a request on it", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy, extra=["--pg-request-ms", "1500", "--pg-attempt-ms", "1000"])
    svc.start()
    svc.post_event(1)
    L.wait_for(lambda: svc.stats()["history_written"] == 1, 10)
    proxy.mode = "freeze"
    t = time.time()
    st, data = svc.get("/events/1/attempts", timeout=12)
    took = time.time() - t
    check("8. a request on a connection that went silent: given up after pg-request-ms (1.5 s), answered 503, not at the 5 s deadline (504)", st == 503 and 1.2 <= took <= 3.5, f"{st} {took:.1f}")
    s = svc.stats()
    check("8. ... the connection is counted lost", s["database_losses"] >= 1, str(s))
    proxy.restore()
    check("8. restored: ready again, no restart, and the same request is answered", timed_readyz(svc, 200, 20) is not None and svc.get("/events/1/attempts")[0] == 200)
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 9 ----------------------------------------------------------------------------------------------------------------

def stage9():
    print("== 9. backends ended while idle", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy)
    svc.start()
    time.sleep(1.5)
    n = proxy.kill_backends(L.psql)
    t = time.time()
    check("9. the backends of the proxy's two connections ended (a backend the test's own psql left a moment ago may be among them)", n >= 2, str(n))
    went = L.wait_for(lambda: svc.stats()["database_losses"] == 2, 5)
    check("9. both losses are noticed (a FATAL message arrives with no traffic)", went, str(svc.stats()))
    back = L.wait_for(lambda: svc.stats()["history_live"] == 2, 10)
    took = time.time() - t
    check("9. both are replaced at once (a connection that was live for a second is retried at once): two live again in %.2f s" % took, back and took < 3, f"{took:.1f} {svc.stats()}")
    s = svc.stats()
    check("9. ... counted: 2 losses, 2 reconnects, and no failed attempt (a connection live for a second is replaced at once, and it works)",
          s["database_losses"] == 2 and s["database_reconnects"] == 2 and s["database_failures"] == 0 and svc.metrics().value("hooks_database_reconnects_total") == 2
          and svc.metrics().value("hooks_database_connection_losses_total") == 2 and svc.metrics().value("hooks_database_connect_failures_total") == 0, str(s))
    before = scount()
    svc.post_event(7)
    check("9. and the statements were prepared again (an insert works)", L.wait_for(lambda: scount() == before + 1, 10) and svc.stats()["history_failed"] == 0, str(svc.stats()))
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 10 ---------------------------------------------------------------------------------------------------------------

def stage10():
    print("== 10. the loop wakes for the pool", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy, extra=["--pg-backoff-min-ms", "2", "--pg-backoff-max-ms", "2"])
    svc.start()
    proxy.cut()
    L.wait_for(lambda: svc.stats()["history_live"] == 0, 5)
    f0, c0 = svc.stats()["database_failures"], cpu(svc.proc.pid)
    time.sleep(2.0)
    n = svc.stats()["database_failures"] - f0
    used = cpu(svc.proc.pid) - c0
    print(f"INFO 10. {n} failed attempts in 2 s with waits of 2 ms, {used:.2f} s of CPU", flush=True)
    check("10. with waits of 2 ms the attempts come every few ms (%d in 2 s), not once a 50 ms turn of the loop (80 at most)" % n, n >= 300, str(n))
    proxy.restore()
    check("10. and it connects when the database is back", timed_readyz(svc, 200, 10) is not None)
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 11 ---------------------------------------------------------------------------------------------------------------

def stage11():
    print("== 11. a table too large for the answer", flush=True)
    reset()
    secret = L.secret()
    L.psql(f"insert into endpoints (id, host, port, secret, types, headers) select g, repeat('a', 240) || '.example', 9, '{secret}', repeat('t.x,', 120) || 't.y', repeat('a', 1500) from generate_series(0, 1023) g")
    try:
        t = time.time()
        svc = make(None, extra=["--pg-start-wait-ms", "60000"])
        svc.start(timeout=10)
        code = svc.wait_exit(20)
        check("11. 1,024 rows with 250-byte hosts, long lists and headers (about 2.4 MB): status 20, `too large`, at once (the answer does not fit the pool's 1 MiB), not asked for again for ever",
              code == 20 and svc.has_line("the table is too large") and time.time() - t < 10, f"{code} {svc.stderr()} {time.time() - t:.1f}")
        shutil.rmtree(svc.dir, ignore_errors=True)
    finally:
        L.psql("truncate endpoints")


# ---- 12 ---------------------------------------------------------------------------------------------------------------

def stage12():
    print("== 12. the read of the table takes a while", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    L.psql("alter table endpoints rename to endpoints_real")
    L.psql("create or replace function pgre_slow() returns boolean language plpgsql as $$ begin perform pg_sleep(2); return true; end $$")
    L.psql("create view endpoints as select * from endpoints_real where pgre_slow()")
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy, extra=["--pg-start-wait-ms", "60000"])
    try:
        svc.start(loaded=False)
        check("12. connections are live (the read of the table is on the wire)", L.wait_for(lambda: svc.stats()["history_live"] >= 1, 5), str(svc.stats()))
        time.sleep(0.3)
        st, body = readyz(svc)
        check("12. a connection is live but the endpoints are not read: /readyz is 503 (database)", st == 503 and body.get("check") == "database", str((st, body)))
        st, data = svc.get("/endpoints")
        check("12. ... and GET /endpoints is 503, not an empty list", st == 503 and b"not loaded" in data, str((st, data)))
        check("12. ... /metrics: connections 2 or 1, endpoints_loaded 0", svc.metrics().value("hooks_endpoints_loaded") == 0 and svc.metrics().value("hooks_history_connections") >= 1)
        # the connection goes in the middle of the read
        proxy.cut()
        time.sleep(0.5)
        check("12. cut in the middle of the read: not loaded, still running", svc.alive() and not svc.has_line("endpoints loaded"), svc.stderr())
        ids = [json.loads(svc.post_event(n)[1])["id"] for n in range(1, 4)]
        proxy.restore()
        check("12. back: the read is asked again, and the endpoints load (once)", L.wait_for(lambda: svc.has_line("endpoints loaded: 1"), 20) and svc.stderr().count("endpoints loaded") == 1, svc.stderr())
        check("12. ... and the events taken meanwhile are delivered, once each", L.wait_for(lambda: peer.distinct() == set(ids), 15) and peer.count() == 3, str((peer.events(), ids)))
        check("12. ... ready", L.wait_for(lambda: ready(svc), 5))
    finally:
        svc.stop()
        L.psql("drop view if exists endpoints")
        L.psql("drop function if exists pgre_slow()")
        L.psql("alter table endpoints_real rename to endpoints")
        proxy.close()
        peer.close()
        shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 13 ---------------------------------------------------------------------------------------------------------------

def stage13():
    print("== 13. SCRAM-SHA-256 logins", flush=True)
    if not L.PG_PASSWORD:
        print("skipped 13: the server under test must want a password (HOOKS_PG_PASSWORD)")
        return
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    svc = make(proxy)
    check("13. the service logs in with SCRAM-SHA-256 and has read its endpoints", svc.start() and ready(svc), svc.stderr())
    sender = Sender(svc, 60)
    sender.start()
    probe = Probe(svc)
    probe.start()
    time.sleep(1.5)
    worst = []
    for _ in range(6):
        proxy.kill_backends(L.psql)
        probe.phase("quiet")
        time.sleep(1.2)
        worst.append(probe.marks["quiet"])
    sender.go = False
    probe.go = False
    probe.join(3)
    sender.join(3)
    s = svc.stats()
    print(f"INFO 13. the longest wait for /healthz in the 1.2 s after each of six backends ended (SCRAM logins), ms: {worst}; {probe.n} probes, load average {os.getloadavg()[0]:.1f}", flush=True)
    check("13. six times both backends ended: replaced each time by a SCRAM login, none failed (%d losses, %d reconnects, %d failed attempts)" % (s["database_losses"], s["database_reconnects"], s["database_failures"]),
          s["database_losses"] >= 12 and s["database_reconnects"] >= 12 and s["database_failures"] == 0 and ready(svc), str(s))
    check("13. ... the loop never stalled while keys were derived: the longest wait for /healthz was %d ms (limit 250)" % max(worst), max(worst) < 250, str(worst))
    check("13. ... and nothing was lost or repeated", L.wait_for(lambda: peer.distinct() >= set(sender.ids), 20) and peer.count() == len(set(peer.events())), str((len(sender.ids), peer.count())))
    svc.stop()
    proxy.close()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)
    t = time.time()
    bad = L.Service(BIN, L.free_dir("hooks-pgre-"), ["--schedule", "200", "--pg-host", L.PG_HOST, "--pg-port", str(L.PG_PORT), "--pg-user", L.PG_USER, "--pg-database", L.PG_DB,
                                                     "--pg-password", L.PG_PASSWORD + "-wrong", "--pg-start-wait-ms", "60000"])
    CLEAN.append(bad)
    bad.start(timeout=10)
    code = bad.wait_exit(10)
    check("13. a wrong password at the start: status 20, `cannot log in`, at once (not after pg-start-wait-ms)", code == 20 and bad.has_line("cannot log in") and time.time() - t < 5, f"{code} {bad.stderr()}")
    shutil.rmtree(bad.dir, ignore_errors=True)


# ---- 14 ---------------------------------------------------------------------------------------------------------------

def stage14():
    print("== 14. a server that really goes away", flush=True)
    stop_cmd, start_cmd = os.environ.get("HOOKS_PG_STOP"), os.environ.get("HOOKS_PG_START")
    if not (stop_cmd and start_cmd):
        print("skipped 14: HOOKS_PG_STOP and HOOKS_PG_START (shell commands for a server that is the test's own) are not set")
        return
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    svc = make(None)
    svc.start()
    sender = Sender(svc, 60)
    sender.start()
    time.sleep(1.5)
    subprocess.run(stop_cmd, shell=True, check=True, capture_output=True)
    check("14. the server is stopped: /readyz is 503 within a second or two", timed_readyz(svc, 503, 10) is not None)
    time.sleep(4.0)
    check("14. ... still serving after four seconds without a database: /healthz 200, events taken", svc.get("/healthz")[0] == 200 and sender.refused == 0 and svc.alive())
    subprocess.run(start_cmd, shell=True, check=True, capture_output=True)
    back = timed_readyz(svc, 200, 40)
    check("14. the server is started again: /readyz is 200 with no restart of the service (%s s)" % back, back is not None)
    time.sleep(1.5)
    sender.go = False
    sender.join(5)
    ids = list(sender.ids)
    check("14. every event taken (%d) is delivered, once" % len(ids), L.wait_for(lambda: peer.distinct() >= set(ids), 20) and peer.count() == len(set(peer.events())) == len(ids), f"{len(ids)} {peer.count()}")
    ok = L.wait_for(lambda: svc.stats()["history_queue"] == 0 and accounted(svc)[0] == accounted(svc)[1], 20)
    attempts, placed, st = accounted(svc)
    check("14. the history is accounted: %d = %d written + %d failed + %d dropped" % (attempts, st["history_written"], st["history_failed"], st["history_dropped"]), ok and attempts == placed, str(st))
    svc.stop()
    peer.close()
    shutil.rmtree(svc.dir, ignore_errors=True)


# ---- 15 ---------------------------------------------------------------------------------------------------------------

def segments(d):
    return sorted(n for n in os.listdir(d) if n.startswith("events") and n.endswith(".seg"))


def stage15():
    print("== 15. retention waits for the endpoints", flush=True)
    peer = L.Peer("ok")
    reset([(0, peer.port)])
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    proxy.cut()
    knobs = ["--retention-ms", "1", "--window-ms", "1", "--segment-bytes", "262144", "--pg-start-wait-ms", "120000"]
    svc = make(proxy, extra=knobs)
    svc.start(loaded=False)
    ids = []
    for n in range(1, 9):
        ids.append(json.loads(svc.post_event(n)[1])["id"])
        time.sleep(0.7)
    time.sleep(1.5)
    s = svc.stats()
    first = segments(svc.dir)
    check("15. the database is away, retention is 1 ms and a segment is sealed every turn of its clock: segments were sealed (%d files), none was dropped, no event was dropped" % len(first),
          len(first) >= 3 and s["segments_dropped"] == 0 and s["events_dropped"] == 0 and s["events_first_id"] == 1, str((first, s)))
    proxy.restore()
    check("15. the database comes: the endpoints load", L.wait_for(lambda: svc.has_line("endpoints loaded: 1"), 20), svc.stderr())
    check("15. ... every event that was taken is delivered, once (nothing was dropped before the cursors were known)", L.wait_for(lambda: peer.distinct() == set(ids), 20) and peer.count() == len(ids), str((peer.events(), ids)))
    check("15. ... and then retention does its work: segments are dropped", L.wait_for(lambda: svc.stats()["segments_dropped"] >= 1, 20), str(svc.stats()))
    svc.stop()
    shutil.rmtree(svc.dir, ignore_errors=True)
    # compact-now with a database named reads the table first, through the same pool, and waits for it as long as pg-start-wait-ms allows (design section 41.8):
    # with the database away it ends with status 20 after that time and changes nothing; with the database there it reads the table and does its pass
    d = L.free_dir("hooks-pgre-")
    proxy.cut()
    sv2 = make(proxy, extra=knobs, d=d)
    sv2.start(loaded=False)
    for n in range(1, 5):
        sv2.post_event(n)
        time.sleep(0.5)
    sv2.stop()
    before = {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d) if n.endswith((".seg", ".first"))}
    t = time.time()
    proc = subprocess.run([BIN, "--port", str(L.chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", "--retention-ms", "1", "--window-ms", "1", "--pg-start-wait-ms", "2000", *L.pg_flags(proxy.port)], capture_output=True, text=True, timeout=60)
    took = time.time() - t
    after = {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d) if n.endswith((".seg", ".first"))}
    check("15. compact-now with the database away: status 20 after pg-start-wait-ms (2 s: it took %.1f), says it could not read the endpoints, and every file is as it was" % took,
          proc.returncode == 20 and "cannot be read" in proc.stderr and 1.5 < took < 20 and before == after, f"{proc.returncode} {proc.stderr!r} {took}")
    check("15. ... and it is not the status of a lock held (43) or of a step that failed (44)", proc.returncode not in (43, 44))
    proxy.restore()
    proc = subprocess.run([BIN, "--port", str(L.chaos.free_port()), "--dir", d, "--allow-private-hosts", "1", "--compact-now", "1", "--retention-ms", "1", "--window-ms", "1", "--pg-start-wait-ms", "20000", *L.pg_flags(proxy.port)], capture_output=True, text=True, timeout=60)
    after2 = {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d) if n.endswith((".seg", ".first"))}
    check("15. compact-now with the database there: it reads the endpoints (1), does its pass and exits 0", proc.returncode == 0 and "endpoints loaded: 1" in proc.stderr and "compacted" in proc.stderr, f"{proc.returncode} {proc.stderr!r}")
    check("15. ... and no segment of the events log was dropped: the endpoint it read has cursor 0, so every event is owed (what held them was the cursors, not the refusal)",
          all(n in after2 for n in before if n.startswith("events")), str(sorted(after2)))
    proxy.close()
    peer.close()
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    stages = {"1": stage1, "2": stage2, "3": stage3, "4": stage4, "5": stage5, "6": stage6, "7": stage7, "8": stage8, "9": stage9, "10": stage10, "11": stage11, "12": stage12, "13": stage13, "14": stage14, "15": stage15}
    for name, fn in stages.items():
        if not STAGES or name in STAGES:
            fn()
    sys.exit(check.finish("pgre"))
