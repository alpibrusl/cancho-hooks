#!/usr/bin/env python3
"""Stopping the service (docs/production.md 0.4; docs/design.md section 34.4): SIGTERM and SIGINT drain, a second signal ends the process at once.

    python3 tests/stop_test.py build/hooks              (no database needed)

Nothing here waits for the clock to say something happened: every step is taken when the service or a receiver has been seen to do the thing before it (a receiver
has the request, a line has appeared on stderr), and the only times that are checked are the ones the contract is about (how long a drain may take).

  1. an idle service: SIGTERM and SIGINT both end it with status 0, with the two lines it promises, and the logs are whole and the next start finds nothing to cut
  2. with attempts on the wire: the service stops taking requests (503, `Connection: close`) and says it is not ready (`/readyz` 503, `/metrics` stopping 1) but
     still answers GET /healthz; it starts no attempt (8 are on the wire, 192 events wait: when the receiver answers, no ninth request ever reaches it); the 8 finish, are
     recorded, and the process exits 0; the next start delivers the other 192 and repeats none of the 8
  3. the deadline: a receiver that never answers, `stop-deadline-ms 1000`: the process exits 0 after about a second, says how many attempts it left, and the next start repeats them
     (at least once) and only them
  4. a second signal: during a drain that would take a minute, the second SIGTERM (or SIGINT) ends the process at once, killed by the signal; the logs are whole
  5. under load: four clients post as fast as they can, two receivers (one slow) are being delivered to, SIGTERM; the exit status is 0 within the deadline; every event a client was told was stored is in
     the log; and across the stop and the next start every event reached each receiver exactly once
"""
import json
import os
import shutil
import signal
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


def conf(d, peers):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        for i, p in enumerate(peers):
            f.write(f"{i} 127.0.0.1 {p.port if hasattr(p, 'port') else p} {L.secret()}\n")


def delivered_pairs(d):
    return [(e, ev) for k, e, ev, *_ in L.outcomes(d) if k == 1]


def whole_logs(d):
    """Both logs end on a record boundary (nothing torn): what a clean stop leaves."""
    out = True
    for name in ("events.seg", "delivery.seg"):
        path = os.path.join(d, name)
        if os.path.exists(path):
            data = open(path, "rb").read()
            recs, end = chaos.read_log(data)
            out = out and end == len(data)
    return out


def stage1():
    print("== 1. an idle service", flush=True)
    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        d = L.free_dir()
        a = L.Peer("ok")
        conf(d, [a])
        svc = L.Service(BIN, d)
        svc.start()
        for n in range(5):
            svc.post_event(n)
        L.wait_for(lambda: len(a.distinct()) == 5 and svc.stats()["delivered"] == 5, 10)
        t0 = time.time()
        svc.proc.send_signal(sig)
        code = svc.wait_exit(10)
        took = time.time() - t0
        check(f"1. {name}: the process exits with status 0 within a second or two", code == 0 and took < 3, str((code, took)))
        err = svc.stderr()
        check(f"1. {name}: stderr says it was stopping on that signal, then that nothing was left on the wire",
              f"hooks: stopping on {name}" in err and "hooks: stopped: nothing was left on the wire" in err, err)
        check(f"1. {name}: both logs are whole and hold the 5 events and their 5 deliveries", whole_logs(d) and len(L.events_log(d)[0]) == 5 and len(delivered_pairs(d)) == 5)
        svc2 = L.Service(BIN, d)
        svc2.start()
        check(f"1. {name}: the next start cuts nothing (no line about a torn tail) and delivers nothing again",
              "cut a torn tail" not in svc2.stderr() and not [1 for ln in svc2.lines if "cut" in ln], svc2.stderr())
        time.sleep(0.2)
        check(f"1. {name}: ... nothing is sent again", a.count() == 5, str(a.count()))
        svc2.stop()
        shutil.rmtree(d)
        a.close()


def stage2():
    print("== 2. attempts on the wire", flush=True)
    d = L.free_dir()
    slow, fast = L.Peer("slow", delay=120), L.Peer("ok")
    conf(d, [slow, fast])
    svc = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "60000"])
    svc.start()
    total = 200
    for n in range(total):
        assert svc.post_event(n)[0] == 202
    ok = L.wait_for(lambda: slow.count() == 8 and len(fast.distinct()) == total, 20)
    check("2. 8 attempts are on the wire at the slow receiver (the per-endpoint limit), the fast one has all 200", ok, f"{slow.count()} {len(fast.distinct())}")
    time.sleep(0.2)
    check("2. ... and the 8 are all it has: nothing more is started while they are out", slow.count() == 8)
    svc.term()
    seen = L.wait_for(lambda: svc.has_line("hooks: stopping on SIGTERM"), 10)
    check("2. the service says it is stopping", seen, svc.stderr())
    st, body = svc.post_event(9999)
    check("2. a POST /events during the drain is a 503 that says why", st == 503 and b"stopping" in body, str((st, body)))
    st, data, hdr = svc.request("POST", "/events", b'{"type":"x"}', {})
    check("2. ... and the connection is closed after it", st == 503 and hdr.get("Connection", "").lower() == "close", str((st, hdr)))
    st, data = svc.get("/readyz")
    check("2. GET /readyz is 503 with check \"stopping\"", st == 503 and json.loads(data)["check"] == "stopping" and json.loads(data)["ready"] is False, str((st, data)))
    m = svc.metrics()
    check("2. /metrics says stopping 1, ready 0, and 8 attempts in flight", m.value("hooks_stopping") == 1 and m.value("hooks_ready") == 0 and m.value("hooks_attempts_in_flight") == 8, str(m.value("hooks_attempts_in_flight")))
    check("2. GET /healthz is still 200 (the process is up)", svc.get("/healthz")[0] == 200)
    st, data, hdr = svc.request("POST", "/endpoints/0/enable", b"")
    check("2. any other write is refused the same way (POST /endpoints/0/enable)", st == 503, str((st, data)))
    check("2. nothing was stored by any of it: the events log still has the 200", len(L.events_log(d)[0]) == total)
    check("2. the drain is not over: the 8 attempts are still out and the process is running", svc.alive())
    slow.release()
    code = svc.wait_exit(15)
    check("2. when the 8 answered, the process exited with status 0", code == 0, str(code))
    check("2. ... without starting a ninth attempt: the slow receiver saw 8 requests in all", slow.count() == 8, str(slow.count()))
    done = [ev for e, ev in delivered_pairs(d) if e == 0]
    check("2. the 8 are recorded as delivered (they were finished, not abandoned)", sorted(done) == sorted(slow.distinct()) and len(done) == 8, str(done))
    check("2. the logs are whole", whole_logs(d))
    check("2. the service said what it did", "hooks: stopped: nothing was left on the wire" in svc.stderr(), svc.stderr())
    svc2 = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "5000"])
    slow.mode = "ok"
    svc2.start()
    ok = L.wait_for(lambda: len(slow.distinct()) == total, 30)
    time.sleep(0.3)
    per = {}
    for ev in slow.events():
        per[ev] = per.get(ev, 0) + 1
    check("2. the next start delivered the other 192 to the slow receiver and repeated none of the 8: every event reached it exactly once", ok and set(per.values()) == {1} and len(per) == total,
          f"{len(per)} {sorted(set(per.values()))}")
    svc2.stop()
    shutil.rmtree(d)
    slow.close()
    fast.close()


def stage3():
    print("== 3. the deadline", flush=True)
    d = L.free_dir()
    silent = L.Peer("silent")
    conf(d, [silent])
    args = ["--schedule", "60000", "--deadline-ms", "600000", "--stop-deadline-ms", "1000"]
    svc = L.Service(BIN, d, args)
    svc.start()
    for n in range(3):
        svc.post_event(n)
    check("3. 3 attempts are on the wire and the receiver is silent", L.wait_for(lambda: silent.count() == 3, 10))
    t0 = time.time()
    svc.term()
    code = svc.wait_exit(15)
    took = time.time() - t0
    check("3. the process exits with status 0", code == 0, str(code))
    check("3. ... after about the stop deadline (1 s): not at once, and not an attempt's 10 minutes", 0.8 <= took <= 6, f"{took:.2f}")
    err = svc.stderr()
    check("3. ... and it says 3 attempts were still on the wire, and that they are made again", "3 attempts were still on the wire at the deadline" in err and "made again" in err, err)
    check("3. the logs are whole; nothing was recorded for the 3 (no outcome)", whole_logs(d) and not [1 for k, *_ in L.outcomes(d) if k in (1, 2, 3)])
    svc2 = L.Service(BIN, d, args)
    svc2.start()
    again = L.wait_for(lambda: silent.count() == 6, 10)
    per = {}
    for ev in silent.events():
        per[ev] = per.get(ev, 0) + 1
    check("3. the next start made the 3 attempts again (at least once), and only those", again and sorted(per) == [1, 2, 3] and set(per.values()) == {2}, str(per))
    silent.release()
    svc2.stop()
    shutil.rmtree(d)
    silent.close()

    # a deadline of 0 is "do not wait": the process ends as soon as it has flushed, with the attempts on the wire left for the next start
    d = L.free_dir()
    silent = L.Peer("silent")
    conf(d, [silent])
    svc = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "600000", "--stop-deadline-ms", "0"])
    svc.start()
    svc.post_event(1)
    L.wait_for(lambda: silent.count() == 1, 10)
    t0 = time.time()
    svc.term()
    code = svc.wait_exit(15)
    check("3. --stop-deadline-ms 0: the process exits with status 0 at once, leaving the attempt for the next start", code == 0 and time.time() - t0 < 3 and "1 attempt was still on the wire at the deadline" in svc.stderr(),
          f"{code} {svc.stderr()}")
    silent.release()
    shutil.rmtree(d)
    silent.close()


def stage4():
    print("== 4. a second signal", flush=True)
    for first, second, name in ((signal.SIGTERM, signal.SIGTERM, "SIGTERM twice"), (signal.SIGINT, signal.SIGINT, "SIGINT twice"), (signal.SIGTERM, signal.SIGINT, "SIGTERM then SIGINT")):
        d = L.free_dir()
        silent = L.Peer("silent")
        conf(d, [silent])
        svc = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "600000", "--stop-deadline-ms", "600000"])
        svc.start()
        for n in range(2):
            svc.post_event(n)
        L.wait_for(lambda: silent.count() == 2, 10)
        svc.proc.send_signal(first)
        seen = L.wait_for(lambda: svc.has_line("hooks: stopping on"), 10)
        check(f"4. {name}: the first signal began a drain that would take ten minutes", seen and svc.alive(), svc.stderr())
        t0 = time.time()
        svc.proc.send_signal(second)
        code = svc.wait_exit(10)
        took = time.time() - t0
        sig = -code if code is not None and code < 0 else None
        check(f"4. {name}: the second ended the process at once, killed by the signal it was sent", code is not None and took < 2 and sig == second, str((code, took)))
        check(f"4. {name}: the logs are whole", whole_logs(d) and len(L.events_log(d)[0]) == 2)
        svc2 = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "800"])
        silent.mode = "ok"
        svc2.start()
        check(f"4. {name}: and it starts again, repeating what was on the wire", L.wait_for(lambda: len(silent.distinct()) == 2 and svc2.stats()["delivered"] == 2, 10))
        svc2.stop()
        silent.release()
        shutil.rmtree(d)
        silent.close()


def stage5():
    print("== 5. under load", flush=True)
    d = L.free_dir()
    a, b = L.Peer("ok"), L.Peer("slow", delay=0.25)
    conf(d, [a, b])
    svc = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "5000", "--stop-deadline-ms", "5000"])
    svc.start()
    acked, lock = {}, threading.Lock()
    stop_posting = threading.Event()
    refused = [0]

    def worker(first):
        n = first
        while not stop_posting.is_set():
            body = json.dumps({"type": "load", "n": n}).encode()
            try:
                status, data = chaos.post(svc.port, body, timeout=3)
            except Exception:  # noqa: BLE001
                return
            if status == 202:
                with lock:
                    acked[json.loads(data)["id"]] = body
            else:
                refused[0] += 1
                return
            n += 4

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(4)]
    for t in threads:
        t.start()
    loaded = L.wait_for(lambda: len(acked) >= 400 and b.count() >= 8 and svc.stats()["attempts"] > 100, 60)
    inflight = svc.stats()
    check("5. under way: 400 events stored, the slow receiver has requests on the wire", loaded, str(inflight))
    t0 = time.time()
    svc.term()
    code = svc.wait_exit(15)
    took = time.time() - t0
    stop_posting.set()
    for t in threads:
        t.join(5)
    check("5. SIGTERM under load: the exit status is 0, within the deadline (5 s)", code == 0 and took < 5.5, f"{code} {took:.2f}")
    ev, size, valid = L.events_log(d)
    with lock:
        got = dict(acked)
    check("5. every event a client was told was stored is in the log, byte for byte", all(ev.get(i) == body for i, body in got.items()) and size == valid,
          f"missing {[i for i in got if i not in ev][:5]}")
    check("5. the logs are whole (nothing torn: the stop was clean)", whole_logs(d))
    pairs = delivered_pairs(d)
    check("5. no (endpoint, event) is recorded delivered twice", len(pairs) == len(set(pairs)), str(len(pairs) - len(set(pairs))))
    err = svc.stderr()
    check("5. it left nothing on the wire", "nothing was left on the wire" in err, err)
    svc2 = L.Service(BIN, d, ["--schedule", "60000", "--deadline-ms", "5000"])
    b.mode = "ok"
    svc2.start()
    total = len(ev)
    ok = L.wait_for(lambda: len(a.distinct()) == total and len(b.distinct()) == total, 60)
    time.sleep(0.5)
    ra, rb = {}, {}
    for e in a.events():
        ra[e] = ra.get(e, 0) + 1
    for e in b.events():
        rb[e] = rb.get(e, 0) + 1
    check(f"5. across the stop and the next start every one of the {total} events reached each receiver", ok, f"{len(a.distinct())} {len(b.distinct())}")
    check("5. ... exactly once: nothing the drain finished was repeated, nothing was lost", set(ra.values()) == {1} and set(rb.values()) == {1}, f"{sorted(set(ra.values()))} {sorted(set(rb.values()))}")
    svc2.stop()
    shutil.rmtree(d)
    a.close()
    b.close()


def main():
    stage1()
    stage2()
    stage3()
    stage4()
    stage5()
    return check.finish("stop")


if __name__ == "__main__":
    sys.exit(main())
