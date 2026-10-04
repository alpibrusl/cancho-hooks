#!/usr/bin/env python3
"""Refuse corruption instead of repairing it silently (docs/production.md 0.5; docs/design.md section 34.5; docs/runbook.md 4.7).

    python3 tests/corrupt_test.py build/hooks              (no database needed)

Every case starts the service on a copy of a real data directory (500 events, all delivered) that has been damaged in one way. What is checked is what the start
does and says, and what it leaves on the disk; the expectation of each case is worked out from the bytes (the independent reader of tests/chaos.py), not from the service.

  A. the pair. `delivery.seg` refers to an event `events.seg` does not hold (an older events log beside a newer delivery log: the restore done by hand that
     docs/runbook.md 4.7 measured, where the service answered 202 and never delivered): status 18 and a message, before it listens, with both files untouched. A `created` record
     whose starting cursor is beyond the log, a reason record, a missing events log: the same. A pair that agrees to the last event starts.
  B. damage in the middle. One flipped byte in the middle of `events.seg` (500 events became 250 and nothing was printed: docs/runbook.md 4.7), in `delivery.seg`, in a length field, a hole of zeros
     with records after it, the last three records spoiled, the second last: status 19, where the log is whole to, how many bytes the cut would take and how many intact records are among them; the files are untouched and nothing is
     written beside them.
  C. a torn tail is still recovered, and now said: an unfinished record, a few bytes of a header, a page of zeros, garbage, the shape a power cut makes (a spoiled record and a part of the next): the
     start cuts it, listens, and says so on stderr after `listening`; nothing before it is lost.
  D. `--repair-logs 1`: the damage is cut at the first bad byte after the cut bytes have been copied to `<log>.cut-<offset>` (byte for byte); the report says how much; the next start has nothing to say; it
     refuses when it cannot keep the bytes; it never lifts the refusal of the pair; given on a clean log it does nothing.
  E. one rule for the service and for scripts/logcheck.py: 153 (150 random, 3 directed) corruptions of a small log are classified by logcheck.classify_tail and then given to the service, which must do what was predicted.
"""
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import chaos  # noqa: E402
import logcheck  # noqa: E402
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()
N = 500


def conf(d, port):
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"0 127.0.0.1 {port} {L.secret()}\n")


def make_base(n):
    """A data directory with `n` events, all delivered to one endpoint, written by the service itself."""
    d = L.free_dir("hooks-corrupt-base-")
    peer = L.Peer("ok")
    conf(d, peer.port)
    svc = L.Service(BIN, d, ["--schedule", "200", "--deadline-ms", "800"])
    svc.start()
    for i in range(n):
        assert svc.post_event(i)[0] == 202
    assert L.wait_for(lambda: svc.stats()["delivered"] == n, 60)
    assert svc.stop() == 0
    peer.close()
    os.remove(os.path.join(d, "endpoints.conf"))
    return d


def copy_of(base):
    d = L.free_dir("hooks-corrupt-")
    for name in os.listdir(base):
        shutil.copy(os.path.join(base, name), os.path.join(d, name))
    return d


def read(d, name):
    return open(os.path.join(d, name), "rb").read()


def write(d, name, data):
    open(os.path.join(d, name), "wb").write(data)


def snapshot(d):
    return {n: read(d, n) for n in sorted(os.listdir(d))}


def listening(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Run:
    """Start the service on `d` and see what it does: it ended (`code`) or listened. `lines` is what it said."""

    def __init__(self, d, extra=(), peer_port=None):
        if peer_port:
            conf(d, peer_port)
        self.svc = L.Service(BIN, d, ["--schedule", "200", "--deadline-ms", "800", *extra])
        self.up = self.svc.start()
        if self.up:
            # the cut report is printed before the loop starts: when it answers, everything the start had to say has been said
            assert L.wait_for(lambda: self.svc.get("/healthz")[0] == 200, 5)
            self.lines = list(self.svc.lines)
            self.code = None
        else:
            self.code = self.svc.wait_exit(10)
            time.sleep(0.05)
            self.lines = list(self.svc.lines)
        self.text = "\n".join(self.lines)

    def stop(self):
        if self.up:
            return self.svc.stop()


def flip(data, at):
    x = bytearray(data)
    x[at] ^= 0x01
    return bytes(x)


def records_end(data, k):
    """The offset where record number k (0-based) starts."""
    at = 0
    for _ in range(k):
        (n,) = struct.unpack_from("<I", data, at)
        at += 4 + n
    return at


def stage_a(base):
    print("== A. the pair", flush=True)
    ev = read(base, "events.seg")
    dl = read(base, "delivery.seg")
    d = copy_of(base)
    cut = records_end(ev, 300)
    write(d, "events.seg", ev[:cut])
    before = snapshot(d)
    r = Run(d)
    check("A1. an events.seg of 300 events beside a delivery.seg that refers to event 500: status 18", r.code == 18 and not r.up, str((r.code, r.text)))
    check("A1. ... it says which event, and where the events log ends", "delivery.seg refers to event 500" in r.text and "events.seg ends at event 300" in r.text, r.text)
    check("A1. ... and why it matters, and what to do", "never deliver" in r.text and "restore" in r.text.lower(), r.text)
    check("A1. ... it did not say `listening`, and nothing listens", "listening" not in r.lines and not listening(r.svc.port))
    check("A1. ... and the directory is exactly as it was", snapshot(d) == before)
    shutil.rmtree(d)

    d = copy_of(base)
    os.remove(os.path.join(d, "events.seg"))
    r = Run(d)
    check("A2. no events.seg beside a delivery.seg that refers to events: status 18", r.code == 18, str((r.code, r.text)))
    shutil.rmtree(d)

    d = copy_of(base)
    write(d, "delivery.seg", dl + L.put_record(5000, 10, 0, 0, 999, 0))        # a `created` record whose starting cursor is event 999
    before = snapshot(d)
    r = Run(d)
    check("A3. a created record whose cursor is beyond the events log: status 18", r.code == 18 and "refers to event 999" in r.text, str((r.code, r.text)))
    check("A3. ... files untouched", snapshot(d) == before)
    shutil.rmtree(d)

    d = copy_of(base)
    write(d, "delivery.seg", dl + L.put_record(5000, 14, 0, 501, 1, 12))       # the reason of an attempt at event 501
    r = Run(d)
    check("A4. the reason record of an attempt at an event that is not there: status 18", r.code == 18 and "refers to event 501" in r.text, str((r.code, r.text)))
    shutil.rmtree(d)

    d = copy_of(base)
    write(d, "delivery.seg", dl + L.put_record(5000, 14, 0, 500, 1, 12) + L.put_record(5001, 10, 0, 0, 500, 0))
    r = Run(d)
    check("A5. references up to exactly the last event are fine: the service starts, with nothing to say", r.up and r.lines == ["listening"], str((r.code, r.text)))
    r.stop()
    shutil.rmtree(d)

    # the hazard that was measured: the pair given to a service that does not check delivers nothing. Here it is refused; with --repair-logs it still is.
    d = copy_of(base)
    write(d, "events.seg", ev[:cut])
    r = Run(d, ["--repair-logs", "1"])
    check("A6. --repair-logs does not lift the refusal of the pair", r.code == 18, str((r.code, r.text)))
    shutil.rmtree(d)

    # and the exit statuses are told apart from the ones that were there
    d = copy_of(base)
    os.chmod(os.path.join(d, "events.seg"), 0)
    if os.geteuid() != 0:
        r = Run(d)
        check("A7. an events.seg that cannot be opened is still status 10, not 18 or 19", r.code == 10, str(r.code))
    os.chmod(os.path.join(d, "events.seg"), 0o644)
    shutil.rmtree(d)


def damage_cases(base):
    ev = read(base, "events.seg")
    dl = read(base, "delivery.seg")
    mid = records_end(ev, N // 2)
    dmid = records_end(dl, len(chaos.read_log(dl)[0]) // 2)
    cases = []
    cases.append(("a byte flipped in the middle of events.seg", "events.seg", flip(ev, mid + 12)))
    cases.append(("a byte flipped in a length field in the middle of events.seg", "events.seg", flip(ev, mid + 1)))
    cases.append(("a byte flipped in the middle of delivery.seg", "delivery.seg", flip(dl, dmid + 20)))
    zero = bytearray(ev)
    zero[mid:mid + 200] = bytes(200)
    cases.append(("a hole of 200 zeros in the middle of events.seg, with records after it", "events.seg", bytes(zero)))
    last3 = bytearray(ev)
    for k in (N - 3, N - 2, N - 1):
        last3[records_end(ev, k) + 16] ^= 0xFF
    cases.append(("the last three records of events.seg spoiled (three whole records that do not validate)", "events.seg", bytes(last3)))
    cases.append(("the second last record of events.seg spoiled (the last is intact)", "events.seg", flip(ev, records_end(ev, N - 2) + 14)))
    cases.append(("the first record of events.seg spoiled", "events.seg", flip(ev, 10)))
    return cases


def stage_b(base):
    print("== B. damage in the middle", flush=True)
    ev = read(base, "events.seg")
    for name, log, data in damage_cases(base):
        d = copy_of(base)
        write(d, log, data)
        before = snapshot(d)
        recs, end = chaos.read_log(data)
        r = Run(d)
        short = log.replace(".seg", "")
        check(f"B. {name}: status 19, before it listens", r.code == 19 and not r.up and "listening" not in r.lines, str((r.code, r.text)))
        check(f"B. ... it names the file and says it is damage in the middle, not a torn tail", f"hooks: {log}: damage in the middle of the log" in r.text, r.text)
        check(f"B. ... it says where the log is whole to ({len(recs)} records, byte {end}) and how many bytes the cut would take ({len(data) - end})",
              f"whole for {len(recs)} records (up to byte {end})" in r.text and f"after that {len(data) - end} bytes do not read as the log" in r.text and f"cut the log at byte {end}" in r.text, r.text)
        tail = logcheck.classify_tail(data, end)
        if tail["found_at"] >= 0:
            check(f"B. ... and how many intact records are among them ({tail['found_records']}, from byte {tail['found_at']})",
                  f"{tail['found_records']} intact record" in r.text and f"start at byte {tail['found_at']}" in r.text, r.text)
        check(f"B. ... and how to go on (--repair-logs 1, the cut kept in {log}.cut-{end})", "--repair-logs 1" in r.text and f"{log}.cut-{end}" in r.text, r.text)
        check("B. ... the directory is exactly as it was: nothing cut, nothing written", snapshot(d) == before)
        shutil.rmtree(d)
    # both statuses are the same on the two logs, and different from 18
    d = copy_of(base)
    write(d, "events.seg", flip(ev, records_end(ev, 10) + 12))
    write(d, "delivery.seg", flip(read(base, "delivery.seg"), 30))
    r = Run(d)
    check("B. damage in both logs: events.seg is named first (status 19)", r.code == 19 and "hooks: events.seg: damage" in r.text, r.text)
    shutil.rmtree(d)


def tail_cases(base):
    """Torn tails after the 500 events that delivery.seg knows: the 501st (and 502nd) event was being written, as a crash leaves it. (A tear of an event delivery.seg
    names is not a crash: that is a pair that disagrees, case A.)"""
    ev = read(base, "events.seg")
    n501 = L.put_event(N + 1, b'{"type":"x","n":501}')
    n502 = L.put_event(N + 2, b'{"type":"x","n":502}')
    r = random.Random(11)
    cases = []
    cases.append(("an unfinished last record (37 bytes of it)", ev + n501[:37]))
    cases.append(("an unfinished last record (all but one byte)", ev + n501[:-1]))
    cases.append(("three bytes of a header", ev + b"\x40\x00\x00"))
    cases.append(("a page of 4096 zeros", ev + bytes(4096)))
    cases.append(("garbage that is not a record, 90 bytes", ev + bytes(r.randrange(1, 256) for _ in range(90))))
    cases.append(("the shape a power cut makes: a spoiled record and a part of the next", ev + flip(n501, 20) + n502[:30]))
    both = bytearray(ev + n501)
    both[len(both) - 40:] = bytes(40)
    cases.append(("the last 40 bytes zeroed (a page that never reached the disk)", bytes(both)))
    cases.append(("the last record spoiled, whole (it looks like a torn one, and is cut as one)", ev + flip(n501, 12)))
    return cases


def stage_c(base):
    print("== C. a torn tail is recovered, and said", flush=True)
    for name, data in tail_cases(base):
        d = copy_of(base)
        write(d, "events.seg", data)
        recs, end = chaos.read_log(data)
        r = Run(d)
        check(f"C. {name}: the service starts", r.up and r.lines[0] == "listening", str((r.code, r.text)))
        if r.up:
            line = [ln for ln in r.lines if ln.startswith("hooks: events.seg: cut a torn tail")]
            want = f"hooks: events.seg: cut a torn tail of {len(data) - end} bytes at byte {end} (an unfinished write); {len(recs)} records are whole"
            check("C. ... it says what it cut, after `listening`, and how much is whole", line == [want], f"{line} / {want}")
            check("C. ... the file is the whole records and nothing else", len(read(d, "events.seg")) == end)
            st, body = r.svc.get(f"/events/{len(recs)}")
            check(f"C. ... event {len(recs)} (the last whole one) is served; the next id continues", st == 200 and r.svc.post_event(7)[0] == 202 and
                  json.loads(r.svc.get(f"/events/{len(recs) + 1}")[1])["id"] == len(recs) + 1)
            r.stop()
            r2 = Run(d)
            check("C. ... the start after that has nothing to cut and says nothing", r2.up and r2.lines == ["listening"], r2.text)
            r2.stop()
        shutil.rmtree(d)
    # the delivery log too
    dl = read(base, "delivery.seg")
    d = copy_of(base)
    write(d, "delivery.seg", dl + bytes(11))
    r = Run(d)
    check("C. eleven zero bytes after delivery.seg are cut and said", r.up and "hooks: delivery.seg: cut a torn tail of 11 bytes" in r.text, r.text)
    r.stop()
    shutil.rmtree(d)


def stage_d(base):
    print("== D. --repair-logs", flush=True)
    ev = read(base, "events.seg")
    dl = read(base, "delivery.seg")
    mid = records_end(ev, N // 2)
    bad = flip(ev, mid + 12)
    recs, end = chaos.read_log(bad)
    d = copy_of(base)
    write(d, "events.seg", bad)
    os.remove(os.path.join(d, "delivery.seg"))         # the pair must agree after the cut (D2); without delivery.seg every event is delivered again
    r = Run(d, ["--repair-logs", "1"])
    check("D1. with --repair-logs 1 the service starts on the damaged log", r.up and r.lines[0] == "listening", str((r.code, r.text)))
    want = f"hooks: --repair-logs: events.seg: cut at byte {end}: {len(bad) - end} bytes gone, among them {N - len(recs) - 1} intact records; {len(recs)} records are whole. The bytes are kept in events.seg.cut-{end}"
    check("D1. ... it says what it cut, how much, how many intact records were among it, and where the bytes are", want in r.lines, "\n".join(r.lines))
    check("D1. ... events.seg is the whole prefix, byte for byte", read(d, "events.seg") == bad[:end])
    check("D1. ... the bytes that were cut are kept, byte for byte, in events.seg.cut-%d" % end, read(d, f"events.seg.cut-{end}") == bad[end:])
    st, body = r.svc.get(f"/events/{len(recs)}")
    check(f"D1. ... event {len(recs)} is served, the next one is not there, and a new event takes its id", st == 200 and r.svc.get(f"/events/{len(recs) + 1}")[0] == 404 and
          json.loads(r.svc.post_event(1)[1])["id"] == len(recs) + 1)
    r.stop()
    r2 = Run(d)
    check("D1. ... the next start, without the flag, has nothing to cut and nothing to say", r2.up and r2.lines == ["listening"], r2.text)
    r2.stop()
    shutil.rmtree(d)

    # the pair after a repair: delivery.seg refers to events the shortened log would lack: refused, and nothing is cut (the cut is not made first)
    d = copy_of(base)
    write(d, "events.seg", bad)
    before = snapshot(d)
    r = Run(d, ["--repair-logs", "1"])
    check("D2. --repair-logs on events.seg while delivery.seg refers to events beyond the cut: status 18", r.code == 18 and not r.up, str((r.code, r.text)))
    check(f"D2. ... it says delivery.seg refers to event {N} and the log would end at event {len(recs)}", f"delivery.seg refers to event {N}" in r.text and f"events.seg ends at event {len(recs)}" in r.text, r.text)
    check("D2. ... and NOTHING was cut: the directory is exactly as it was (no half-repair)", snapshot(d) == before)
    shutil.rmtree(d)

    # refuses if it cannot keep the cut bytes
    d = copy_of(base)
    write(d, "events.seg", bad)
    os.remove(os.path.join(d, "delivery.seg"))
    write(d, f"events.seg.cut-{end}", b"somebody's file")
    before = snapshot(d)
    r = Run(d, ["--repair-logs", "1"])
    check("D3. when events.seg.cut-%d exists already nothing is cut: status 19, and it says the bytes could not be kept" % end, r.code == 19 and "nothing was cut" in r.text and not r.up, str((r.code, r.text)))
    check("D3. ... the directory is exactly as it was", snapshot(d) == before)
    shutil.rmtree(d)

    # delivery.seg: the damage is cut, and what was recorded after it is delivered again (at least once)
    bad_dl = flip(dl, records_end(dl, 250) + 20)
    drecs, dend = chaos.read_log(bad_dl)
    d = copy_of(base)
    write(d, "delivery.seg", bad_dl)
    peer = L.Peer("ok")
    r = Run(d, ["--repair-logs", "1"], peer_port=peer.port)
    check("D4. delivery.seg with damage in the middle: cut with the flag, reported with its file name",
          r.up and f"hooks: --repair-logs: delivery.seg: cut at byte {dend}" in r.text and read(d, f"delivery.seg.cut-{dend}") == bad_dl[dend:], r.text)
    expect_again = N - len(drecs)
    ok = L.wait_for(lambda: len(peer.distinct()) >= expect_again, 20)
    check(f"D4. ... what delivery.seg had recorded after the cut is delivered again: at least once, never zero ({expect_again} events)", ok, f"{len(peer.distinct())} of {expect_again}")
    r.stop()
    peer.close()
    shutil.rmtree(d)

    # a clean log with the flag: nothing happens
    d = copy_of(base)
    r = Run(d, ["--repair-logs", "1"])
    check("D5. --repair-logs on a clean log does nothing and says nothing", r.up and r.lines == ["listening"] and not [n for n in os.listdir(d) if ".cut-" in n], r.text)
    cfg = r.svc.get_json("/config")[1]
    check("D5. ... GET /config shows that it was given", cfg["repair-logs"] == 1 and cfg["stop-deadline-ms"] == 5000, str(cfg))
    r.stop()
    # a torn tail with the flag is cut as it would be without it: one line, no quarantine file
    write(d, "events.seg", ev + bytes(50))
    r = Run(d, ["--repair-logs", "1"])
    check("D5. a torn tail with the flag is the torn tail's report, not a repair, and keeps nothing", r.up and "cut a torn tail of 50 bytes" in r.text and not [n for n in os.listdir(d) if ".cut-" in n], r.text)
    r.stop()
    shutil.rmtree(d)

    # from a settings file
    d = copy_of(base)
    write(d, "events.seg", bad)
    os.remove(os.path.join(d, "delivery.seg"))
    cfgfile = os.path.join(d, "hooks.conf")
    port = chaos.free_port()
    open(cfgfile, "w").write(f"port = {port}\ndir = {d}\nrepair-logs = 1\nallow-private-hosts = 1\n")
    p = subprocess.Popen([BIN, "--config", cfgfile], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    first = p.stderr.readline().decode().strip()
    L.wait_for(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2).status == 200, 5)
    p.terminate()
    rest = p.stderr.read().decode()
    p.wait()
    check("D6. repair-logs = 1 in the settings file is the same setting", first == "listening" and "--repair-logs: events.seg: cut at byte" in rest, first + rest)
    shutil.rmtree(d)


def stage_e(base_small):
    print("== E. one rule for the service and for scripts/logcheck.py", flush=True)
    ev = read(base_small, "events.seg")
    rng = random.Random(2026)
    total = 0
    agree = {"clean": 0, "torn": 0, "damage": 0}
    bad = []
    # directed cases first: the corruptions a random one rarely makes (several whole spoiled records at the end, with nothing valid after them)
    directed = []
    for k in (2, 3, 5):
        y = bytearray(ev)
        for j in range(1, k + 1):
            y[records_end(ev, 55 - j) + 16] ^= 0xFF
        directed.append(("spoiled-%d" % k, bytes(y)))
    for i in range(150 + len(directed)):
        if i < len(directed):
            kind, forced = directed[i]
            data = forced
            recs, end = logcheck.read_log(data)
            want = logcheck.classify_tail(data, end)["kind"]
            d = L.free_dir("hooks-corrupt-e-")
            write(d, "events.seg", data)
            r = Run(d)
            ok = (r.code == 19 and read(d, "events.seg") == data) if want == "damage" else (r.up and len(read(d, "events.seg")) == end)
            agree[want] += 1
            if not ok or want != "damage":
                bad.append((i, kind, want, r.code, end, len(data), r.text[:150]))
            r.stop()
            shutil.rmtree(d)
            total += 1
            continue
        x = bytearray(ev)
        kind = rng.choice(["flip", "flip2", "truncate", "zero", "garbage", "hole", "dupe", "tailflip"])
        if kind == "flip":
            x[rng.randrange(len(x))] ^= 1 << rng.randrange(8)
        elif kind == "flip2":
            for _ in range(2):
                x[rng.randrange(len(x))] ^= 1 << rng.randrange(8)
        elif kind == "truncate":
            x = x[:rng.randrange(1, len(x))]
        elif kind == "zero":
            at = rng.randrange(len(x))
            x[at:at + rng.randrange(1, 200)] = bytes(min(len(x) - at, 199))
        elif kind == "garbage":
            x += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300)))
        elif kind == "hole":
            at = rng.randrange(len(x) // 2)
            del x[at:at + rng.randrange(1, 150)]
        elif kind == "dupe":
            at = records_end(ev, rng.randrange(1, 55))
            x = bytearray(ev[:at] + ev[records_end(ev, 3):records_end(ev, 5)] + ev[at:])
        else:
            x[len(x) - rng.randrange(1, 80)] ^= 0xFF
        data = bytes(x)
        recs, end = logcheck.read_log(data)
        want = logcheck.classify_tail(data, end)["kind"]
        d = L.free_dir("hooks-corrupt-e-")
        write(d, "events.seg", data)
        r = Run(d)
        if want == "damage":
            ok = r.code == 19 and read(d, "events.seg") == data
        else:
            ok = r.up and len(read(d, "events.seg")) == end
            if r.up:
                ok = ok and (want == "clean") == (r.lines == ["listening"])
        agree[want] += 1
        if not ok:
            bad.append((i, kind, want, r.code, end, len(data), r.text[:150]))
        r.stop()
        shutil.rmtree(d)
        total += 1
    check(f"E. {total} corruptions (3 directed, the rest random): the service did what logcheck.classify_tail predicted in every one ({agree})", not bad, str(bad[:4]))
    check("E. ... and the corpus has all three kinds (clean, torn, damage)", all(agree.values()), str(agree))


def main():
    base = make_base(N)
    small = make_base(55)
    try:
        stage_a(base)
        stage_b(base)
        stage_c(base)
        stage_d(base)
        stage_e(small)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(small, ignore_errors=True)
    return check.finish("corrupt")


if __name__ == "__main__":
    sys.exit(main())
