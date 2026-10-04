#!/usr/bin/env python3
"""Idempotency keys (docs/design.md sections 4 and 17).

    python3 tests/idempotency_test.py build/hooks        (FULL=1 adds stage 7)

A second `POST /events` with the same `Idempotency-Key` and the same event answers the first one's answer, byte for byte,
and writes nothing. Every check reads the log file with a reader written here (chaos.read_log), not through the service.

  1. the contract: the same key twice, different keys, the same key with another event, no key at all, key and header-name
     case, a key under a window that has passed
  2. what is refused: keys that are empty, too long, with spaces or control bytes, two keys in one request; a bad event
     under a key leaves no trace
  3. twenty requests at once with one key, and many keys with several requests each, interleaved
  4. restarts: a key survives `kill -9` as a power cut (the index is rebuilt from the log, and an expired key stays expired)
  5. a repeat makes no second delivery (a receiver counts)
  6. chaos: logical events with keys, retried by clients while the service is killed at random: exactly one record per key,
     and the id every client was told is the one in the log
  7. a log that cannot be written: 503 for everything, a repeat of a held key included, and a restart still finds every key
  8. (`FULL=1`) the index at its capacity: the key that does not fit is refused with 507, the others keep working, and a
     restart rebuilds all of them
"""
import base64
import http.client
import http.server
import json
import os
import random
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

http.server.HTTPServer.request_queue_size = 128

BIN = chaos.BIN
FULL = os.environ.get("FULL") == "1"
CAPACITY = 65536
failures = []


def check(cond, what):
    if not cond:
        failures.append(what)
        print("FAIL:", what)
    return cond


def raw(port, body, key=None, extra=()):
    """One request on its own connection; the whole answer as bytes."""
    head = [f"POST /events HTTP/1.1", "Host: x", f"Content-Length: {len(body)}", "Connection: close"]
    if key is not None:
        head.append(f"Idempotency-Key: {key}")
    head.extend(extra)
    data = ("\r\n".join(head) + "\r\n\r\n").encode() + body
    return exchange(port, data)


def exchange(port, data, timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


def status_of(answer):
    return int(answer.split(b" ", 2)[1])


def id_of(answer):
    return json.loads(answer.split(b"\r\n\r\n", 1)[1])["id"]


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read()


def keys_held(port):
    return json.loads(get(port, "/stats")[1])["keys"]


def records(datadir):
    data = open(os.path.join(datadir, "events.seg"), "rb").read()
    recs, _ = chaos.read_log(data)
    return recs


def event(n, pad=0):
    return json.dumps({"type": "t", "n": n, "pad": "p" * pad}).encode()


class Run:
    """A service with a data directory of its own."""

    def __init__(self, window_ms=None, flags=()):
        self.datadir = tempfile.mkdtemp(prefix="hooks-idem-")
        self.port = chaos.free_port()
        extra = ("5000", 0, window_ms) if window_ms is not None else ()
        if flags:
            extra = ("5000", 0, 86400000 if window_ms is None else window_ms, *flags)
        self.svc = chaos.Service(self.port, self.datadir, extra=extra)
        self.svc.start()

    def restart(self, power_cut=True):
        self.svc.kill()
        self.svc.start()

    def close(self):
        self.svc.kill()
        shutil.rmtree(self.datadir, ignore_errors=True)


# ---- 1. the contract ----------------------------------------------------------------------------------------------

def stage_contract():
    r = Run()
    a1 = raw(r.port, event(1), "k-1")
    a2 = raw(r.port, event(1), "k-1")
    check(status_of(a1) == 202 and id_of(a1) == 1, "the first request with a key is stored: 202 and id 1")
    check(a1 == a2, "a repeat answers the first one's bytes exactly")
    check(len(records(r.datadir)) == 1, "a repeat writes nothing")
    check(keys_held(r.port) == 1, "one key is held")

    b = raw(r.port, event(2), "k-2")
    check(id_of(b) == 2 and len(records(r.datadir)) == 2, "another key is another event")

    d = raw(r.port, event(99), "k-1")
    check(status_of(d) == 422, "the same key with another event is refused (422)")
    check(len(event(1)) == len(event(2)) and status_of(raw(r.port, event(2), "k-1")) == 422,
          "also when the other event is the same length")
    check(len(records(r.datadir)) == 2, "and writes nothing")
    # The refusal must not have disturbed the original.
    check(raw(r.port, event(1), "k-1") == a1, "the original still answers after a refused reuse")

    u1, u2 = raw(r.port, event(5)), raw(r.port, event(5))
    check(id_of(u1) != id_of(u2), "without a key, two identical events are two events")

    check(id_of(raw(r.port, event(1), "K-1")) not in (1,), "keys are case-sensitive")
    check(raw(r.port, event(1), None, ["idempotency-key: k-1"]) == a1, "the header name is case-insensitive")
    check(raw(r.port, event(1), None, ["IDEMPOTENCY-KEY:   k-1  "]) == a1, "spaces around the value are not part of the key")

    # The record: event first (delivery and GET read the first pair), then the type (design section 35), then the key, then the time.
    recs = records(r.datadir)
    first = recs[0][1]
    check([k for k, _ in first] == [b"event", b"typ", b"key", b"t"], f"a keyed record is event, typ, key, t; got {[k for k, _ in first]}")
    check(dict(first)[b"key"] == b"k-1" and dict(first)[b"event"] == event(1), "the record holds the key and the event as sent")
    t = struct.unpack("<Q", dict(first)[b"t"])[0]
    check(abs(t - time.time() * 1000) < 60000, "the record's time is the Unix time in ms")
    unkeyed = [pairs for ms, pairs in recs if ms == id_of(u1)][0]
    check([k for k, _ in unkeyed] == [b"event", b"typ"], "an unkeyed record is the event and its type")
    st, body = get(r.port, "/events/1")
    check(st == 200 and json.loads(body)["event"]["n"] == 1, "GET /events/1 still reads a keyed event")
    r.close()

    # A window that passes: the same key and event then make a new event, and the new one is the one that answers.
    w = Run(window_ms=400)
    first = raw(w.port, event(1), "w")
    again = raw(w.port, event(1), "w")
    check(first == again, "inside the window a repeat answers the first")
    time.sleep(0.6)
    later = raw(w.port, event(1), "w")
    check(id_of(later) == 2, f"after the window the same key makes a new event (got id {id_of(later)})")
    check(raw(w.port, event(1), "w") == later, "and the new one answers from then on")
    check(keys_held(w.port) == 1, "an expired key is overwritten, not added")
    time.sleep(0.6)
    other = raw(w.port, event(7), "w")
    check(status_of(other) == 202 and id_of(other) == 3, "after the window the key may be used for a different event")
    check(len(records(w.datadir)) == 3, "three records, one per use that was not a repeat")
    w.close()


# ---- 2. what is refused -------------------------------------------------------------------------------------------

def stage_refusals():
    r = Run()
    bad = {
        "an empty key": raw(r.port, event(1), ""),
        "a key of 256 bytes": raw(r.port, event(1), "x" * 256),
        "a key with a space inside": raw(r.port, event(1), "a b"),
        "a key with a tab inside": raw(r.port, event(1), "a\tb"),
        "a key with a non-ASCII byte": raw(r.port, event(1), "café".encode("latin-1").decode("latin-1")),
        "two keys": raw(r.port, event(1), "one", ["Idempotency-Key: two"]),
    }
    for what, answer in bad.items():
        check(status_of(answer) == 400, f"{what} is refused with 400 (got {status_of(answer)})")
    check(status_of(raw(r.port, event(1), "x" * 255)) == 202, "a key of 255 bytes is accepted")
    check(status_of(raw(r.port, event(2), "!~:/.-_=+")) == 202, "punctuation is allowed in a key")
    n = len(records(r.datadir))
    check(n == 2, f"nothing was stored for the refused ones ({n} records)")

    bad_event = raw(r.port, b'{"no":"type"}', "good-key")
    check(status_of(bad_event) == 422, "an invalid event under a key is a 422")
    check(keys_held(r.port) == 2, "and leaves no key behind")
    ok = raw(r.port, event(3), "good-key")
    check(status_of(ok) == 202, "the key is still free after a refused event")

    # An event the log refuses as too large reaches the handler (the server's input limit is higher): 413, and no key.
    huge = json.dumps({"type": "t", "pad": "x" * 65600}).encode()
    check(status_of(raw(r.port, huge, "big")) == 413, "an event over the log's limit is refused with 413")
    check(keys_held(r.port) == 3, "and holds no key")
    check(status_of(raw(r.port, event(8), "big")) == 202, "the key is free afterwards")
    # Over the server's own input limit the connection may simply be closed.
    try:
        status_of(raw(r.port, json.dumps({"type": "t", "pad": "x" * 70000}).encode(), "huger"))
    except OSError:
        pass
    check(keys_held(r.port) == 4, "an event over the server's input limit holds no key either")
    # Near the limit: a keyed record is larger than an unkeyed one, so the limit is lower by the key and 40 bytes.
    near = json.dumps({"type": "t", "pad": "x" * 65300}).encode()
    a = raw(r.port, near, "k" * 200)
    check(status_of(a) in (202, 413), "a keyed event near the limit is answered, not crashed on")
    check(get(r.port, "/healthz")[0] == 200, "the service is up after it")
    r.close()


# ---- 3. concurrency -----------------------------------------------------------------------------------------------

def stage_concurrent():
    r = Run()
    answers = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def one():
        barrier.wait()
        a = raw(r.port, event(1), "storm")
        with lock:
            answers.append(a)

    ts = [threading.Thread(target=one) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(len(set(answers)) == 1 and status_of(answers[0]) == 202, "twenty simultaneous requests with one key get one answer")
    check(len(records(r.datadir)) == 1, "and make one event")

    # Many keys, several requests each, in one shuffled stream over eight threads.
    work = [(f"m-{k}", k) for k in range(300) for _ in range(4)]
    random.Random(3).shuffle(work)
    seen = {}

    def worker(chunk):
        for key, n in chunk:
            a = raw(r.port, event(n), key)
            with lock:
                seen.setdefault(key, set()).add(a)

    chunks = [work[i::8] for i in range(8)]
    ts = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(all(len(v) == 1 for v in seen.values()), "each key got one answer throughout")
    recs = records(r.datadir)
    check(len(recs) == 301, f"301 records: the storm and 300 keys ({len(recs)})")
    ids = {id_of(next(iter(v))) for v in seen.values()}
    check(len(ids) == 300, "300 distinct ids for 300 keys")
    check(keys_held(r.port) == 301, "301 keys are held")
    r.close()


# ---- 4. restarts --------------------------------------------------------------------------------------------------

def stage_restart():
    r = Run()
    acks = {k: raw(r.port, event(n), f"r-{k}") for n, k in enumerate(range(50))}
    r.restart()
    check(keys_held(r.port) == 50, "after a power cut and a restart every acknowledged key is held again")
    check(all(raw(r.port, event(n), f"r-{k}") == acks[k] for n, k in enumerate(range(50))),
          "and answers what it answered before")
    check(len(records(r.datadir)) == 50, "without writing anything")
    r.close()

    w = Run(window_ms=700)
    first = raw(w.port, event(1), "x")
    w.restart()
    check(raw(w.port, event(1), "x") == first, "inside the window a repeat after a restart is still the first")
    time.sleep(1.0)
    w.restart()
    later = raw(w.port, event(1), "x")
    check(id_of(later) == 2, f"a key expired before a restart stays expired after it (got id {id_of(later)})")
    w.close()

    # The later of two records under one key wins after a rebuild, as it did while running.
    w = Run(window_ms=300)
    raw(w.port, event(1), "y")
    time.sleep(0.5)
    second = raw(w.port, event(1), "y")
    w.svc.kill()
    w.svc.extra = ["5000", "0", "10000"]
    w.svc.start()
    check(raw(w.port, event(1), "y") == second, "after a rebuild a key answers with its latest event")
    check(len(records(w.datadir)) == 2, "and still writes nothing")
    w.close()


# ---- 5. a repeat is not a second delivery -------------------------------------------------------------------------

def stage_delivery():
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            got.append(self.headers["webhook-id"])
            self.send_response(500 if self.headers["webhook-id"] == "evt_2" and got.count("evt_2") == 1 else 204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    datadir = tempfile.mkdtemp(prefix="hooks-idem-")
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    with open(os.path.join(datadir, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {srv.server_address[1]} {secret}\n")
    svc = chaos.Service(chaos.free_port(), datadir, extra=("100,200",))
    svc.start()
    for _ in range(5):
        raw(svc.port, event(1), "once")
    raw(svc.port, event(2), "twice")
    deadline = time.time() + 5
    while time.time() < deadline and len(got) < 3:
        time.sleep(0.05)
    time.sleep(0.5)
    check(sorted(set(got)) == ["evt_1", "evt_2"] and got.count("evt_1") == 1,
          f"five requests with one key are one delivery (receiver saw {got})")
    stats = json.loads(get(svc.port, "/stats")[1])
    check(stats["delivered"] == 2 and stats["failed"] >= 1 and stats["keys"] == 2, f"stats agree: {stats}")
    svc.kill()
    shutil.rmtree(datadir, ignore_errors=True)
    srv.shutdown()


# ---- 6. chaos -----------------------------------------------------------------------------------------------------

def stage_chaos(events=600, threads=6, mean_ms=120):
    datadir = tempfile.mkdtemp(prefix="hooks-idem-chaos-")
    port = chaos.free_port()
    svc = chaos.Service(port, datadir, extra=("5000",))
    svc.start()
    told = {}                       # key -> set of ids clients were told
    lock = threading.Lock()
    stop = threading.Event()
    kills = [0]
    rng = random.Random(11)

    def killer():
        while not stop.is_set():
            time.sleep(rng.expovariate(1000.0 / mean_ms))
            if stop.is_set():
                break
            svc.kill()
            kills[0] += 1
            time.sleep(rng.uniform(0.0, 0.05))
            svc.start()

    def worker(first):
        r = random.Random(first)
        for n in range(first, events, threads):
            body = event(n, n % 40)
            key = f"chaos-{n}"
            tries = 0
            while True:
                tries += 1
                try:
                    a = raw(port, body, key)
                except (OSError, http.client.HTTPException):
                    time.sleep(0.01)
                    continue
                if not a.startswith(b"HTTP/1.1 202"):
                    time.sleep(0.01)
                    continue
                with lock:
                    told.setdefault(key, set()).add(id_of(a))
                # A client that is unsure whether it was heard sends it again, sometimes, even after an answer.
                if r.random() < 0.3 and tries < 3:
                    continue
                break

    k = threading.Thread(target=killer, daemon=True)
    k.start()
    started = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stop.set()
    k.join()
    svc.kill()
    elapsed = time.time() - started

    data = open(os.path.join(datadir, "events.seg"), "rb").read()
    recs, _ = chaos.read_log(data)
    by_key = {}
    for ms, pairs in recs:
        d = dict(pairs)
        by_key.setdefault(d.get(b"key"), []).append(ms)
    dup = {k: v for k, v in by_key.items() if len(v) > 1}
    check(not dup, f"{len(dup)} keys have more than one record, e.g. {list(dup.items())[:3]}")
    check(len(by_key) == events, f"{len(by_key)} keys in the log, expected {events}")
    wrong = [k for k, ids in told.items() if ids != set(by_key.get(k.encode(), []))]
    check(not wrong, f"{len(wrong)} keys were told an id that is not the one in the log, e.g. {wrong[:3]}")
    print(f"chaos: {events} keyed events, {threads} threads, {kills[0]} kills as power cuts, {svc.starts} starts, {elapsed:.1f}s;"
          f" {len(recs)} records for {len(by_key)} keys")
    shutil.rmtree(datadir, ignore_errors=True)


# ---- 8. a log that cannot be written ------------------------------------------------------------------------------

def stage_broken():
    """A failed write breaks the log (lexsys-log): everything is refused with 503 until a restart, a repeat of a held key
    included (whether its record reached the disk is no longer known), and a restart finds every acknowledged key."""
    import resource
    import signal
    import subprocess
    datadir = tempfile.mkdtemp(prefix="hooks-idem-")
    port = chaos.free_port()

    def limited():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (6000, 6000))

    proc = subprocess.Popen([BIN, *chaos.flags(port, datadir)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            preexec_fn=limited)
    proc.stderr.readline()
    acked, first_refusal = {}, None
    for n in range(200):
        a = raw(port, event(n, 20), f"b-{n}")
        if status_of(a) == 202:
            acked[n] = a
        else:
            first_refusal = (n, status_of(a))
            break
    check(first_refusal is not None and first_refusal[1] == 503, f"a write that fails is answered 503 ({first_refusal})")
    check(len(acked) > 5, "some events went in before the limit")
    held = max(acked)
    check(status_of(raw(port, event(held, 20), f"b-{held}")) == 503, "a repeat of a held key is refused too while the log is broken")
    check(status_of(raw(port, event(0, 20), "b-0")) == 503, "so is the first one")
    check(status_of(raw(port, event(1000), "b-new")) == 503, "so is a new key")
    check(get(port, "/healthz")[0] == 200 and get(port, "/events/1")[0] == 200, "reads still work")
    proc.kill()
    proc.wait()
    svc = chaos.Service(port, datadir)
    svc.start()
    check(all(raw(port, event(n, 20), f"b-{n}") == a for n, a in acked.items()), "after a restart every acknowledged key answers as before")
    check(len(records(datadir)) == len(acked), "and nothing was stored twice")
    svc.kill()
    shutil.rmtree(datadir, ignore_errors=True)


# ---- 7. the index at its capacity ---------------------------------------------------------------------------------

def stage_full():
    # The index holds `idem-keys` keys (a setting since retention; 262,144 by default, 65,536 was the only size it had): the limit is the same
    # mechanism at whatever size it is given, and 65,536 is the size that can be filled in seconds.
    r = Run(flags=("--idem-keys", str(CAPACITY)))
    done = [0]
    lock = threading.Lock()
    refused = []

    def worker(first, step, n):
        c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=30)
        for i in range(first, n, step):
            body = b'{"type":"t"}'
            c.request("POST", "/events", body=body, headers={"Idempotency-Key": f"full-{i}"})
            resp = c.getresponse()
            resp.read()
            with lock:
                done[0] += 1
                if resp.status != 202:
                    refused.append((i, resp.status))

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i, 64, CAPACITY)) for i in range(64)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(not refused, f"{len(refused)} of the first {CAPACITY} keys were refused: {refused[:3]}")
    check(keys_held(r.port) == CAPACITY, f"{CAPACITY} keys held")
    print(f"full: {CAPACITY} keyed events in {time.time() - t0:.1f}s")
    over = raw(r.port, b'{"type":"t"}', "one-too-many")
    check(status_of(over) == 507, f"the key that does not fit is refused with 507 (got {status_of(over)})")
    check(len(records(r.datadir)) == CAPACITY, "and is not stored")
    check(status_of(raw(r.port, b'{"type":"t"}')) == 202, "an event without a key still goes in")
    check(status_of(raw(r.port, b'{"type":"t"}', "full-7")) == 202, "a key already held still answers")
    t0 = time.time()
    r.restart()
    rebuild = time.time() - t0
    check(keys_held(r.port) == CAPACITY, "a restart rebuilds all of them")
    print(f"full: restart with {CAPACITY + 1} records took {rebuild:.2f}s")
    check(status_of(raw(r.port, b'{"type":"t"}', "one-too-many")) == 507, "still refused after the restart")
    r.close()


def main():
    stages = [("contract", stage_contract), ("refusals", stage_refusals), ("concurrent", stage_concurrent),
              ("restart", stage_restart), ("delivery", stage_delivery), ("chaos", stage_chaos), ("broken", stage_broken)]
    if FULL:
        stages.append(("full", stage_full))
    for name, fn in stages:
        before = len(failures)
        t0 = time.time()
        fn()
        print(f"{name}: {'ok' if len(failures) == before else 'FAILED'} ({time.time() - t0:.1f}s)")
    if failures:
        print(f"{len(failures)} checks failed")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
