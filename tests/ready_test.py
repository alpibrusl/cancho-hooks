#!/usr/bin/env python3
"""GET /readyz (docs/production.md 0.4; docs/design.md section 34.1): 200 only when the service can do its job, 503 with the reason when it cannot.

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/ready_test.py build/hooks

  1. ready: 200 {"ready":true}, no credential; /healthz is unchanged; POST /readyz is a 405
  2. the database. A proxy sits between the service and PostgreSQL. Cut it (the database goes away: every connection is closed and new ones are
     refused): /readyz is 503 with check "database" and a reason, /metrics says ready 0 and no connection, /healthz is still 200, and delivery is
     unaffected (an event posted now is stored and delivered). The proxy comes back: the service reconnects by itself (docs/design.md section 37), so /readyz is 200
     again with no restart, the history rows that waited are written, and /metrics says it reconnected.
  3. the log directory (a 256 KiB tmpfs, when this host lets the test mount one):
       a. the disk fills: the next event is refused with a 503 and the events log is broken (a failed write is never retried); /readyz is 503 with check "events_log"; /healthz stays
          200 (this is the case the plan names: "it answers 200 with the disk full"); a restart after space is freed finds every acknowledged event and is ready again
       b. the disk fills by something else (a file written until ENOSPC): within a second or two the probe notices, before any event is posted: 503 with check
          "data_dir"; when space is freed /readyz is 200 again without a restart (nothing had failed in the logs, so nothing is broken). (A read-only remount would be
          the same, but a tmpfs cannot be remounted read-only while the service holds its logs open for writing: EBUSY.)
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
from pgproxy import PgProxy  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


def readyz(svc):
    status, data = svc.get("/readyz")
    return status, json.loads(data)


def stage1():
    print("== 1. ready", flush=True)
    d = L.free_dir()
    svc = L.Service(BIN, d)
    svc.start()
    check("1. 200 {\"ready\":true}", readyz(svc) == (200, {"ready": True}))
    st, data, hdr = svc.request("GET", "/readyz")
    check("1. ... as JSON, with no credential asked", hdr.get("Content-Type", "").startswith("application/json") and "WWW-Authenticate" not in hdr)
    check("1. GET /healthz is still 200 {\"ok\":true}", svc.get("/healthz") == (200, b'{"ok":true}'))
    st, data, hdr = svc.request("POST", "/readyz", b"")
    check("1. POST /readyz is a 405 with Allow: GET", st == 405 and "GET" in hdr.get("Allow", ""), str((st, hdr)))
    check("1. /metrics agrees: ready 1", svc.metrics().value("hooks_ready") == 1)
    svc.stop()
    shutil.rmtree(d)


def stage2():
    print("== 2. the database", flush=True)
    L.apply_schema()
    L.psql("truncate endpoints, attempts")
    peer = L.Peer("ok")
    L.psql(f"insert into endpoints values (0, '127.0.0.1', {peer.port}, '{L.secret()}')")
    proxy = PgProxy(L.PG_HOST, int(L.PG_PORT))
    d = L.free_dir()
    args = ["--schedule", "200", "--deadline-ms", "800", *L.pg_flags(proxy.port)]
    svc = L.Service(BIN, d, args)
    check("2. the service starts through the proxy", svc.start(), svc.stderr())
    check("2. with the database reachable: 200", readyz(svc) == (200, {"ready": True}))
    check("2. ... two history connections", svc.metrics().value("hooks_history_connections") == 2)
    proxy.cut()
    ok = L.wait_for(lambda: readyz(svc)[0] == 503, 15)
    st, body = readyz(svc)
    check("2. the database goes away: /readyz becomes 503, check \"database\", with a reason", ok and body.get("check") == "database" and body.get("ready") is False and
          "no live connection to it" in body.get("reason", ""), str((st, body)))
    m = svc.metrics()
    check("2. ... /metrics: ready 0, no history connection", m.value("hooks_ready") == 0 and m.value("hooks_history_connections") == 0)
    check("2. ... /healthz is still 200", svc.get("/healthz")[0] == 200)
    status, data = svc.post_event(1)
    check("2. ... and delivery is unaffected: an event is stored and delivered", status == 202 and L.wait_for(lambda: peer.distinct() == {1}, 10), str((status, data)))
    time.sleep(0.2)
    check("2. ... the history rows wait in the queue (bounded: 256), they are not lost yet", svc.metrics().value("hooks_history_queue") >= 1)
    proxy.mode = "pass"
    check("2. the database is back: /readyz is 200 again, with no restart", L.wait_for(lambda: readyz(svc) == (200, {"ready": True}), 15), str(readyz(svc)))
    m = svc.metrics()
    check("2. ... /metrics: two connections again, and it counts what happened (losses, reconnects, failed attempts)",
          L.wait_for(lambda: svc.metrics().value("hooks_history_connections") == 2, 5) and m.value("hooks_database_connection_losses_total") >= 1
          and m.value("hooks_database_connect_failures_total") >= 1, str(m.value("hooks_database_reconnects_total")))
    check("2. ... and the row that waited is written", L.wait_for(lambda: svc.metrics().value("hooks_history_rows_total", result="written") >= 1 and svc.metrics().value("hooks_history_queue") == 0, 10))
    code = svc.stop()
    check("2. SIGTERM with the database gone: exit status 0", code == 0, str(code))
    svc2 = L.Service(BIN, d, args)
    svc2.start()
    check("2. and after a restart too: 200", L.wait_for(lambda: readyz(svc2) == (200, {"ready": True}), 10))
    svc2.stop()
    shutil.rmtree(d)
    proxy.close()
    peer.close()


def mount_tmpfs(path, size_kb):
    for cmd in (["mount", "-t", "tmpfs", "-o", f"size={size_kb}k", "tmpfs", path], ["sudo", "-n", "mount", "-t", "tmpfs", "-o", f"size={size_kb}k", "tmpfs", path]):
        try:
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return cmd[0] == "sudo"
        except FileNotFoundError:
            pass
    return None


def run_mount(sudo, *args):
    return subprocess.run((["sudo", "-n"] if sudo else []) + ["mount", *args], capture_output=True).returncode == 0


def stage3():
    print("== 3. the log directory", flush=True)
    d = L.free_dir()
    sudo = mount_tmpfs(d, 256)
    if sudo is None:
        print("(3 skipped: this host does not let the test mount a tmpfs; run it as root or with passwordless sudo)")
        shutil.rmtree(d)
        return
    if sudo:
        subprocess.run(["sudo", "-n", "chmod", "0777", d])
    try:
        svc = L.Service(BIN, d)
        svc.start()
        check("3a. on a 256 KiB tmpfs the service is ready", readyz(svc) == (200, {"ready": True}))
        acked, refused = {}, 0
        n = 0
        while refused < 3 and n < 60:
            status, data = svc.post_event(n, extra={"pad": "x" * 30000})
            if status == 202:
                acked[json.loads(data)["id"]] = n
            else:
                refused += 1
            n += 1
        check("3a. the disk fills: the events that fit are acknowledged, then every one is a 503", len(acked) >= 3 and refused == 3, f"{len(acked)} acked, {refused} refused")
        st, body = readyz(svc)
        check("3a. /readyz is 503 with check \"events_log\" and says to free the disk and restart", st == 503 and body.get("check") == "events_log" and "restart" in body.get("reason", ""), str((st, body)))
        check("3a. /healthz still says 200 (the very case the plan names)", svc.get("/healthz")[0] == 200)
        m = svc.metrics()
        check("3a. /metrics: ready 0, refused with 503 counted", m.value("hooks_ready") == 0 and m.value("hooks_ingest_refused_total", status="503") >= 3)
        code = svc.stop()
        check("3a. SIGTERM with the log broken: exit status 0", code == 0, str(code))
        # free some space (remove the broken events' tail by the service's own restart cut), then start again
        svc2 = L.Service(BIN, d)
        started = svc2.start()
        ev, size, valid = L.events_log(d)
        check("3a. a restart (the torn tail is cut) finds every acknowledged event, byte for byte", started and sorted(ev) == sorted(acked), f"{sorted(ev)} {sorted(acked)}")
        st, body = readyz(svc2)
        print(f"INFO 3a. after the restart on the same full tmpfs: /readyz {st} {body}", flush=True)
        svc2.stop()

        # b. the disk fills by something else: the probe sees it before any event is posted, and sees it clear
        for name in ("events.seg", "delivery.seg"):
            if os.path.exists(os.path.join(d, name)):
                os.remove(os.path.join(d, name))
        for name in os.listdir(d):
            if name.startswith("events.seg.") or name.startswith("."):
                os.remove(os.path.join(d, name))
        svc3 = L.Service(BIN, d)
        svc3.start()
        check("3b. with room again the service is ready", readyz(svc3) == (200, {"ready": True}))
        filler = os.path.join(d, "filler")
        fd = os.open(filler, os.O_WRONLY | os.O_CREAT)
        try:
            while True:
                os.write(fd, b"x" * 512)
        except OSError:
            pass
        os.close(fd)
        ok = L.wait_for(lambda: readyz(svc3)[0] == 503, 10)
        st, body = readyz(svc3)
        check("3b. the disk fills (another process): /readyz becomes 503 with check \"data_dir\", before any event is posted and before any log write failed", ok and body.get("check") == "data_dir", str((st, body)))
        check("3b. ... /healthz is still 200, and /metrics says ready 0", svc3.get("/healthz")[0] == 200 and svc3.metrics().value("hooks_ready") == 0)
        os.remove(filler)
        ok = L.wait_for(lambda: readyz(svc3) == (200, {"ready": True}), 10)
        check("3b. space is freed: 200 again, without a restart (no write had failed, so no log is broken)", ok, str(readyz(svc3)))
        status, data = svc3.post_event(1)
        check("3b. ... and an event is stored", status == 202, str((status, data)))
        svc3.stop()
        check("3b. a clean stop takes the probe files away", not [n for n in os.listdir(d) if n.startswith(".writable")], str(os.listdir(d)))
    finally:
        subprocess.run((["sudo", "-n"] if sudo else []) + ["umount", d], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


def main():
    stage1()
    stage2()
    stage3()
    return check.finish("ready")


if __name__ == "__main__":
    sys.exit(main())
