#!/usr/bin/env python3
"""The receivers of the soak (docs/soak.md): the endpoints' HTTP or TLS listeners in asyncio processes, which are also the load generator's far end and the
**receivers' ledger**: for every request that reaches a receiver one fixed-size record is written to `<dir>/ledger/recv-<shard>.bin` *before* the receiver answers, so that
whatever the service could have counted as delivered is already there when it counts it.

    receivers.py --dir DIR --seed S [--cpus 0,1,2] [--shard K --shards N]

With N processes (`--shards N`) the endpoints are divided by `idx % N`: process K serves the endpoints with `idx % N == K`, has its own control port (`DIR/ctl-K.port`)
and its own ledger; the checker reads the ledgers together. Each record is written by a thread of its own (a bounded queue, a batch at a time), and the answer is sent after
the batch holding its record has been written, so that the event loop never waits for the disk and the record is still there before the answer. The record says when the
request was read and when the answer was sent, and whether the answer counts (a 2xx within `EFF_MAX_DELAY` of the request **as it was sent**, on a connection that was
still open) is decided at that moment, not from the delay that was planned.

It reads `DIR/spec.json` (the endpoints, their ports, secrets and classes), listens, writes its control port to `DIR/ctl-K.port`, and takes commands as one line of
JSON each over that port: add, remove, secrets, mode, sick, fault, stats. Not a general HTTP server: it understands the one request the service makes, verifies its
Standard Webhooks signature itself (HMAC-SHA256 over `id.timestamp.body` under the endpoint's secrets, with the roles and validity the harness told it), and says what it
saw. It does not decide whether what it saw is right: the checker (ledger.py) does, from the records and from the poster's ledger.
"""
import argparse
import asyncio
import base64
import errno
import hashlib
import hmac
import json
import os
import queue
import random
import resource
import signal
import socket
import ssl
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CLASSES, F_BODY, F_EFF, F_NEW, F_OLD, F_RISK, F_SIG, F_TS, F_TWO, FLAP_CYCLE, REC, TYPE_CODE, UNKNOWN_TYPE, crc_pad, fail_count,  # noqa: E402
                    flap_window, poison, recv_ledger, secret_bytes)

EFF_MAX_DELAY = 1.6      # an answer later than this is past the service's deadline (2 s from the start of the attempt): not an acknowledgement
# the end-to-end latency of the first delivery is read only at endpoints that do not fail on purpose (a retry's is the schedule's delay, a replay's is its age)
LAT_CLASSES = ("oracle", "healthy", "healthy2", "filter", "https", "rate", "slow")
TS_TOLERANCE = 300       # Standard Webhooks: five minutes
STALL_RISK_S = 0.25      # a request read this long after the loop's last turn is marked as a risk
WRITE_QUEUE = 50000      # records waiting for the writer thread: past this the event loop writes the record itself (counted), which is slower but never loses the order "record, then answer"
WRITE_BATCH = 512


def port_holders(port):
    """Who has a socket on this port, for the message of a bind that fails: [(state, pid, command)] from /proc (Linux; empty elsewhere)."""
    inodes = {}
    states = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in open(f).read().splitlines()[1:]:
                x = line.split()
                if int(x[1].rsplit(":", 1)[1], 16) == port or int(x[2].rsplit(":", 1)[1], 16) == port:
                    inodes[x[9]] = states.get(x[3], x[3])
        except (OSError, ValueError, IndexError):
            pass
    out = []
    if not inodes:
        return out
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return out
    owned = set()
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                    cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")[:80]
                    out.append((inodes[tgt[8:-1]], int(pid), cmd))
                    owned.add(tgt[8:-1])
        except OSError:
            continue
    out += [(st, None, "no process (the kernel's: a connection closing)") for ino, st in inodes.items() if ino not in owned]
    return out


class LedgerWriter(threading.Thread):
    """The receivers' ledger is written here, off the event loop: records go through a bounded queue and are written a batch at a time with one `write`; the answers that were waiting for
    their records are released on the loop afterwards. Nothing is answered before its record is in the file."""

    def __init__(self, fd, loop):
        super().__init__(daemon=True, name="ledger-writer")
        self.fd, self.loop = fd, loop
        self.q = queue.Queue(WRITE_QUEUE)
        self.sync_fallbacks = 0
        self.batches = 0
        self.q_max = 0
        self.wait_max = 0.0      # the longest an item waited between being queued and being released (seconds)
        self.error = None

    def submit(self, data, release):
        """`data`: the bytes of one or two records (may be empty: a mutant lost it); `release()` is called on the loop when they are in the file."""
        try:
            self.q.put_nowait((time.monotonic(), data, release))
            n = self.q.qsize()
            if n > self.q_max:
                self.q_max = n
        except queue.Full:
            self.sync_fallbacks += 1
            self.write_all(data)
            release()

    def write_all(self, data):
        mv = memoryview(data)
        while mv:
            n = os.write(self.fd, mv)
            mv = mv[n:]

    def run(self):
        while True:
            batch = [self.q.get()]
            while len(batch) < WRITE_BATCH:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            try:
                self.write_all(b"".join(b[1] for b in batch))
            except OSError as e:
                self.error = repr(e)        # the disk is full or gone: the answers are not sent, which the service sees as failed attempts, and the harness sees `error` in the stats
                print(f"receivers: the ledger cannot be written: {e!r}", file=sys.stderr, flush=True)
                continue
            self.batches += 1
            self.loop.call_soon_threadsafe(self.release, batch)

    def release(self, batch):
        now = time.monotonic()
        for t_q, _data, rel in batch:
            self.wait_max = max(self.wait_max, now - t_q)
            rel()


class Ep:
    def __init__(self, spec, seed, t0):
        self.idx, self.label, self.cls, self.port = spec["idx"], spec["label"], spec["cls"], spec["port"]
        self.tls = bool(spec.get("tls"))
        self.params = dict(CLASSES.get(self.cls, ([], {}))[1])
        self.params.update(spec.get("params", {}))
        self.seed, self.t0 = seed, t0
        self.set_secrets(spec.get("secrets", []))
        self.sock = None
        self.server = None
        self.removed = False
        self.removed_at = 0.0
        self.bind_failed_at = 0.0
        self.bind_reported = False
        self.vanish_until = 0.0
        self.hold_until = 0.0
        self.slow_until, self.slow_delay = 0.0, 0.0
        self.sick_until = 0.0
        self.down_mode = None            # a flapping endpoint's current down kind: reset or close (refuse closes the listener)
        self.attempts = {}               # event -> (attempts so far, last time): for the class that fails the first few
        self.n_req = self.n_eff = self.n_bad = 0

    def set_secrets(self, items):
        # oldest first; the last is the new one
        self.secrets = [(secret_bytes(it["s"]), it.get("from"), it.get("to")) for it in items]

    def flap_mode(self, now):
        cycle = int((now - self.t0) // FLAP_CYCLE)
        start, length, mode = flap_window(self.seed, self.label, cycle)
        off = (now - self.t0) - cycle * FLAP_CYCLE
        return mode if start <= off < start + length else None

    def want_listen(self, now):
        if self.removed:
            return False
        if self.vanish_until > now:
            return False
        if self.cls == "flapping" and self.flap_mode(now) == "refuse":
            return False
        return True


class Receivers:
    def __init__(self, a):
        self.dir = a.dir
        self.seed = a.seed
        self.shard, self.shards = a.shard, a.shards
        self.loop = None
        with open(os.path.join(a.dir, "spec.json")) as f:
            self.spec = json.load(f)
        self.t0 = self.spec.get("t0", time.time())
        self.eps = {}
        self.fd = os.open(recv_ledger(a.dir, a.shard), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.writer = None
        self.lat = []
        self.lag_max = 0.0
        self.records = 0
        self.faults = {}
        self.rng = random.Random(a.seed ^ 0x5eed ^ (a.shard * 7919))
        self.ssl = None
        cert = self.spec.get("tls")
        if cert:
            self.ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl.load_cert_chain(cert["cert"], cert["key"])
        self.accepted = 0
        self.bind_stuck_s = a.bind_stuck_s
        self.forget_s = a.forget_s
        self.bind_failures = 0       # a bind that failed with "Address already in use" (each is retried; none is ignored: `bind_stuck` in the stats lists the ones that last)
        self.late_unplanned = 0      # answers that were due within half a second and went out more than a second late: the receiver's own loop was the cause
        self.late_max = 0.0
        self.stall_end = -1e9
        self.last_tick = time.monotonic()    # the ticker's last turn: a request read long after it was a request that waited for a stalled loop
        self.conn_lost = 0           # answers that could not be sent because the service had closed the connection

    # ---- the ledger
    def write(self, ep, rec_args, release):
        """One record (two with the `dup` mutant, none with `lose`), queued for the writer; `release` sends the answer once it is in the file."""
        data = REC.pack(*rec_args)
        f = self.faults
        if f.get("lose") and self.rng.random() < f["lose"]:
            data = b""      # a mutant: the delivery happened and the ledger does not know
        else:
            self.records += 1
            if f.get("dup") and self.rng.random() < f["dup"]:
                data += data
                self.records += 1
        self.writer.submit(data, release)

    # ---- listeners
    def open_ep(self, ep):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setblocking(False)
        try:
            s.bind(("127.0.0.1", ep.port))
        except OSError as e:
            s.close()
            if e.errno == errno.EADDRINUSE:
                self.bind_failures += 1
                if not ep.bind_failed_at:
                    ep.bind_failed_at = time.time()
                    print(f"receivers: {ep.label}: port {ep.port} is in use: held by {port_holders(ep.port)}", file=sys.stderr, flush=True)
            raise
        ep.bind_failed_at = 0.0
        s.listen(1024)
        ep.sock = s
        kw = {"ssl": self.ssl, "ssl_handshake_timeout": 5} if ep.tls else {}
        ep.server = asyncio.ensure_future(self.loop.create_server(lambda: Conn(self, ep), sock=s, **kw))
        ep.server.add_done_callback(lambda fut: fut.exception() and print(f"receivers: {ep.label}: cannot serve: {fut.exception()!r}", file=sys.stderr, flush=True))
        return ep.server

    def close_ep(self, ep):
        srv, ep.server = ep.server, None
        if srv is not None:
            def done(fut):
                try:
                    fut.result().close()
                except Exception:  # noqa: BLE001
                    pass
            srv.add_done_callback(done) if not srv.done() else done(srv)
        if ep.sock is not None:
            try:
                ep.sock.close()
            except OSError:
                pass
            ep.sock = None

    def add(self, spec):
        ep = Ep(spec, self.seed, self.t0)
        old = self.eps.get(ep.idx)
        if old:
            self.close_ep(old)
        self.eps[ep.idx] = ep
        return ep

    def by_label(self, label):
        for ep in self.eps.values():
            if ep.label == label:
                return ep
        return None

    async def ticker(self):
        """Opens and closes listeners as the class and the harness say, and measures how late the loop itself runs."""
        period = 0.05
        last = time.monotonic()
        last_prune = time.time()
        while True:
            await asyncio.sleep(period)
            m = time.monotonic()
            if m - self.last_tick - period > STALL_RISK_S:
                self.stall_end = m          # requests read in the next half second waited in the kernel's buffer for this stall
            self.last_tick = m
            self.lag_max = max(self.lag_max, m - last - period)
            last = m
            now = time.time()
            for ep in self.eps.values():
                want = ep.want_listen(now)
                if want and ep.sock is None:
                    try:
                        self.open_ep(ep)
                    except OSError as e:
                        if ep.bind_failed_at and now - ep.bind_failed_at > 30 and not ep.bind_reported:
                            ep.bind_reported = True
                            print(f"receivers: {ep.label}: cannot listen on {ep.port} for 30 s: {e}", file=sys.stderr, flush=True)
                elif not want and ep.sock is not None:
                    self.close_ep(ep)
                ep.down_mode = None
                if ep.cls == "flapping":
                    fm = ep.flap_mode(now)
                    ep.down_mode = fm if fm in ("reset", "close") else None
            if now - last_prune > max(1.0, min(30.0, self.forget_s / 2)):
                last_prune = now
                for ep in list(self.eps.values()):
                    if len(ep.attempts) > 2000:
                        ep.attempts = {k: v for k, v in ep.attempts.items() if now - v[1] < 120}
                    # an endpoint that was taken away and has been closed for a minute is forgotten: the loop does not walk (and the stats do not list) every endpoint the run has ever made
                    if ep.removed and ep.sock is None and ep.server is None and now - ep.removed_at > self.forget_s:
                        del self.eps[ep.idx]

    # ---- control
    async def control(self, reader, writer):
        try:
            line = await reader.readline()
            cmd = json.loads(line)
            out = self.command(cmd)
        except Exception as e:  # noqa: BLE001
            out = {"error": repr(e)}
        writer.write((json.dumps(out) + "\n").encode())
        await writer.drain()
        writer.close()

    def command(self, c):
        op = c["op"]
        if op == "ping":
            return {"ok": True}
        if op == "profile":
            # a diagnostic for the question "what grows in the receivers' CPU per record": {"op":"profile","start":true} profiles the loop thread (cProfile), {"start":false,"path":P} stops and writes
            # the statistics (pstats) to P. Costs a few times the CPU while it is on: for a window of minutes, not a run.
            import cProfile
            if c.get("start"):
                self.prof = cProfile.Profile()
                self.prof.enable()
                return {"ok": True}
            prof = getattr(self, "prof", None)
            if prof is None:
                return {"error": "not profiling"}
            prof.disable()
            prof.dump_stats(c["path"])
            self.prof = None
            return {"ok": True, "path": c["path"]}
        if op == "stall":
            # a test hook, like `fault`: the loop does nothing for this long, as a loop that is starved of its core does not (the answers that were due meanwhile go out late)
            time.sleep(c["seconds"])
            return {"ok": True}
        if op == "add":
            ep = self.add(c["spec"])
            return {"ok": True, "idx": ep.idx}
        if op == "remove":
            ep = self.by_label(c["label"])
            if ep:
                ep.removed, ep.removed_at = True, time.time()
            return {"ok": ep is not None}
        if op == "secrets":
            ep = self.by_label(c["label"])
            ep.set_secrets(c["secrets"])
            return {"ok": True}
        if op == "mode":
            ep = self.by_label(c["label"])
            until = c["until"]
            if c["mode"] == "vanish":
                ep.vanish_until = until
            elif c["mode"] == "slow":
                ep.slow_until, ep.slow_delay = until, c["delay"]
            elif c["mode"] == "hold":
                ep.hold_until = until
            elif c["mode"] == "normal":
                ep.vanish_until = ep.slow_until = ep.hold_until = 0.0
            return {"ok": True}
        if op == "sick":
            ep = self.by_label(c["label"])
            ep.sick_until = c["until"]
            return {"ok": True}
        if op == "params":
            ep = self.by_label(c["label"])
            ep.params.update(c["params"])
            return {"ok": True}
        if op == "fault":
            self.faults = c.get("faults", {})
            return {"ok": True, "faults": self.faults}
        if op == "stats":
            lat, self.lat = self.lat, []
            lag, self.lag_max = self.lag_max, 0.0
            lat.sort()
            w = self.writer

            def pct(p):
                return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0
            ru = resource.getrusage(resource.RUSAGE_SELF)
            stuck = sorted(ep.label for ep in self.eps.values() if ep.bind_failed_at and time.time() - ep.bind_failed_at > self.bind_stuck_s)
            late, self.late_max = self.late_max, 0.0
            out = {"shard": self.shard, "pid": os.getpid(), "records": self.records, "accepted": self.accepted, "lat_n": len(lat), "lat_p50": pct(0.5), "lat_p99": pct(0.99),
                   "lat_max": lat[-1] if lat else 0, "loop_lag_max_ms": lag * 1000, "cpu_s": ru.ru_utime + ru.ru_stime, "rss_kb": ru.ru_maxrss, "endpoints": len(self.eps),
                   "writer": {"batches": w.batches, "queue_max": w.q_max, "sync_fallbacks": w.sync_fallbacks, "wait_max_ms": round(w.wait_max * 1000, 2), "error": w.error},
                   "bind_failures": self.bind_failures, "bind_stuck": stuck, "late_unplanned": self.late_unplanned, "late_max_ms": round(late * 1000, 1), "conn_lost": self.conn_lost}
            w.q_max, w.wait_max = 0, 0.0
            if c.get("eps"):
                out["eps"] = {ep.label: [ep.n_req, ep.n_eff, ep.n_bad] for ep in self.eps.values()}
            return out
        return {"error": "unknown op"}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.writer = LedgerWriter(self.fd, self.loop)
        self.writer.start()
        for s in self.spec["endpoints"]:
            if s["idx"] % self.shards != self.shard:
                continue
            ep = self.add(s)
            if ep.want_listen(time.time()):
                try:
                    self.open_ep(ep)
                except OSError:
                    pass        # counted and named in the log by open_ep; the ticker tries again
        srv = await asyncio.start_server(self.control, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        tmp = os.path.join(self.dir, f"ctl-{self.shard}.port.tmp")
        with open(tmp, "w") as f:
            f.write(str(port))
        os.replace(tmp, os.path.join(self.dir, f"ctl-{self.shard}.port"))
        asyncio.ensure_future(self.ticker())
        await asyncio.Event().wait()


class Conn(asyncio.Protocol):
    __slots__ = ("r", "ep", "tr", "buf", "done")

    def __init__(self, r, ep):
        self.r, self.ep, self.tr, self.buf, self.done = r, ep, None, bytearray(), False

    def connection_made(self, tr):
        self.tr = tr
        self.r.accepted += 1
        ep = self.ep
        dm = ep.down_mode
        if dm == "close":
            self.done = True
            tr.close()
            return
        if dm == "reset":
            self.done = True
            sock = tr.get_extra_info("socket")
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            tr.abort()
            return
        now = time.time()
        if ep.hold_until > now:
            self.done = True
            self.r.loop.call_later(ep.hold_until - now, tr.close)

    def data_received(self, data):
        if self.done:
            return
        self.buf += data
        i = self.buf.find(b"\r\n\r\n")
        if i < 0:
            if len(self.buf) > 70000:
                self.tr.close()
            return
        head = bytes(self.buf[:i]).split(b"\r\n")
        headers = {}
        for line in head[1:]:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        try:
            need = int(headers.get(b"content-length", b"0"))
        except ValueError:
            need = 0
        if len(self.buf) < i + 4 + need:
            return
        self.done = True
        body = bytes(self.buf[i + 4:i + 4 + need])
        try:
            self.handle(head[0], headers, body)
        except Exception as e:  # noqa: BLE001
            print(f"receivers: {self.ep.label}: {e!r}", file=sys.stderr, flush=True)
            self.tr.close()

    def handle(self, reqline, headers, body):
        r, ep = self.r, self.ep
        now = time.time()
        ep.n_req += 1
        wid = headers.get(b"webhook-id", b"")
        ev = int(wid[4:]) if wid.startswith(b"evt_") and wid[4:].isdigit() else 0
        ts = headers.get(b"webhook-timestamp", b"")
        sig = headers.get(b"webhook-signature", b"")
        flags = 0
        try:
            if abs(int(ts) - now) <= TS_TOLERANCE:
                flags |= F_TS
        except ValueError:
            pass
        tokens = [t[3:] for t in sig.split() if t.startswith(b"v1,")]
        if len(tokens) >= 2:
            flags |= F_TWO
        msg = wid + b"." + ts + b"." + body
        last = len(ep.secrets) - 1
        for k, (key, frm, to) in enumerate(ep.secrets):
            want = base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest())
            if want in tokens:
                valid = (frm is None or now >= frm) and (to is None or now <= to)
                if valid:
                    flags |= F_SIG
                    flags |= F_NEW if k == last else F_OLD
        typ_code, n, src, t_send, body_ok = UNKNOWN_TYPE, 0, 0, None, False
        try:
            d = json.loads(body)
            typ = d.get("type")
            if isinstance(typ, str):
                typ_code = TYPE_CODE.get(typ, 254)
            n = int(d.get("n", d.get("scheduled_at", 0)))
            src = int(d.get("schedule", 0))
            t_send = d.get("t")
            body_ok = isinstance(typ, str) and (("c" not in d) or (d["c"] == crc_pad(d.get("pad", ""))))
        except (ValueError, TypeError, AttributeError):
            pass
        if body_ok:
            flags |= F_BODY
        e2e = 0
        if t_send:
            e2e = max(0, min(4294967295, int((now - t_send) * 1000)))
            if ep.cls in LAT_CLASSES:
                r.lat.append(e2e)
        status, delay = self.decide(ep, now, ev)
        if not (flags & F_SIG):
            ep.n_bad += 1
        # Whether the answer counts is decided when it is sent, not now: `delay` is what was planned, and a loop that is late (or a service that gave up) makes an answer that was planned as
        # prompt a late one, which the service counts as a timeout. The record carries both times; it is in the file before the answer goes out.
        # A request that is read just after the loop was stalled was in the kernel's buffer for as long as the stall: the service's clock had been running all that time, and an answer that
        # is prompt by this receiver's clock may be a timeout by the service's. Such a record is marked as a risk (F_RISK), as a slow answer is, so that the repeat that follows is
        # attributed to the receivers' stall (which the guard reports) and not to the service.
        mono = time.monotonic()
        if mono - r.last_tick - 0.05 > STALL_RISK_S or mono - r.stall_end < 0.5:
            flags |= F_RISK
        if delay > 0:
            r.loop.call_later(delay, self.finish, now, flags, status, delay, ev, n, src, typ_code, e2e)
        else:
            self.finish(now, flags, status, delay, ev, n, src, typ_code, e2e)

    def finish(self, t_acc, flags, status, planned, ev, n, src, typ_code, e2e):
        r, ep = self.r, self.ep
        t_send = time.time()
        d = t_send - t_acc
        open_ = self.tr is not None and not self.tr.is_closing()
        if not open_:
            r.conn_lost += 1
        if planned <= 0.5 and d > 1.0:
            r.late_unplanned += 1
            r.late_max = max(r.late_max, d)
        eff = 200 <= status < 300 and d <= EFF_MAX_DELAY and open_
        if eff:
            flags |= F_EFF
            ep.n_eff += 1
            if d > 0.5:
                flags |= F_RISK
        r.write(ep, (t_acc, ep.idx, ev, n, src, typ_code, flags, status, e2e, t_send), lambda: self.answer(status))

    def decide(self, ep, now, ev):
        status, delay = 204, 0.0
        cls = ep.cls
        if cls == "slow":
            lo, hi = ep.params["delay"]
            delay = lo + (hi - lo) * self.r.rng.random()
        elif cls == "http5xx":
            seen = ep.attempts.get(ev, (0, 0))[0]
            ep.attempts[ev] = (seen + 1, now)
            if seen < fail_count(ep.seed, ep.label, ev):
                status = (500, 502, 503)[self.r.rng.randrange(3)]
        elif cls == "gone":
            if poison(ep.seed, ep.label, ev, ep.params.get("poison", 0.01)):
                status = 410
        elif cls == "sick":
            if ep.sick_until > now:
                status = 503
        if ep.slow_until > now:
            delay = max(delay, ep.slow_delay)
        return status, delay

    def answer(self, status):
        tr = self.tr
        if tr is None or tr.is_closing():
            return
        tr.write(f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        tr.close()

    def connection_lost(self, exc):
        self.tr = None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--cpus", default="")
    p.add_argument("--bind-stuck-s", type=float, default=10.0, help="a listener that cannot be opened for this long is reported in the stats (`bind_stuck`)")
    p.add_argument("--forget-s", type=float, default=60.0, help="a removed endpoint is forgotten (no longer walked by the ticker nor listed) this long after its listener closed")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    a = p.parse_args()
    if a.cpus:
        try:
            os.sched_setaffinity(0, {int(c) for c in a.cpus.split(",")})
        except (OSError, AttributeError):
            pass
    os.makedirs(os.path.join(a.dir, "ledger"), exist_ok=True)
    r = Receivers(a)
    # the name server of the https endpoints (tlskit's, which speaks DNS over TCP, the way the service asks)
    if r.spec.get("dns") and a.shard == 0:
        import tlskit
        tlskit.DnsStub({r.spec["dns"]["name"]: ["127.0.0.1"]}, port=r.spec["dns"]["port"], keep_asked=False)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        asyncio.run(r.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
