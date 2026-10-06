#!/usr/bin/env python3
"""The log checker in lex-sys (`hooks-logcheck`, docs/design.md section 54) against the one in Python (`scripts/logcheck.py`).

    python3 tests/logcheck_test.py build/hooks        (build/hooks-logcheck beside it)

The two are written apart (the Python one is the independent reader the other tests lean on; the lex-sys one shares only lexsys-log's scan and the service's rule for a torn
tail), so the way to know the fast one is the same checker is to give both everything and see them agree.

  1. a data directory made by the real service: several segments of the events log, deliveries, failures and dead letters, a clean stop: both say consistent, print the same
     `--kv` lines, and the JSON reports hold the same numbers (the kinds, the reasons, the sizes)
  2. the same directory damaged in 200 ways (one flipped byte, a cut, a zeroed run, garbage appended or put in the middle, a segment removed, a segment swapped for another,
     `events.first` moved or removed, `delivery.seg` from another moment, a header spoiled, a whole record's shape changed): for each, the exit status, the lines of `--kv` and the
     problems said (the same words, in the same order) are the same
  3. what it does not do: it changes nothing (every file of the directory is byte for byte what it was); and the speed of each is printed, not judged (a slow machine is not a bug)
"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import logcheck as PYCHECK  # noqa: E402
import opslib as L  # noqa: E402
import struct  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
NATIVE = os.path.join(os.path.dirname(BIN), "hooks-logcheck")     # beside the service (the other test modules read argv[2] as a number of events)
PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "logcheck.py")
check = L.Checks()
ADMIN = "logcheck-admin-token-1"


def run_py(d, kv=True):
    p = subprocess.run([sys.executable, PY, "check", d] + (["--kv"] if kv else []), capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def run_native(d, kv=True):
    p = subprocess.run([NATIVE, "check", d] + (["--kv"] if kv else []), capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def digest(d):
    h = hashlib.sha256()
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            h.update(name.encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class Receiver:
    def __init__(self, status=204):
        self.status = status
        self.peer = L.Peer("ok")
        self.peer._serve = self.serve
        self.port = self.peer.port

    def serve(self, c):
        try:
            g = self.peer._read_request(c)
            if g is None:
                return
            c.sendall(f"HTTP/1.1 {self.status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass


def make_dir():
    """A data directory the real service made: three segments, an endpoint that delivers and one that never does (failed attempts, reasons, dead letters)."""
    d = tempfile.mkdtemp(prefix="logcheck-")
    a, b = Receiver(204), Receiver(500)
    open(os.path.join(d, "endpoints.conf"), "w").write(f"1 127.0.0.1 {a.port} {L.secret()}\n2 127.0.0.1 {b.port} {L.secret()}\n")
    svc = L.Service(BIN, d, ["--admin-token", ADMIN, "--schedule", "300,300", "--segment-bytes", "262144"])
    svc.start(timeout=30)
    for k in range(1, 901):
        svc.post_event(k, extra={"pad": "y" * 900})
    time.sleep(4)
    svc.stop(10)
    a.peer.close()
    b.peer.close()
    return d


def segments(d):
    return sorted(n for n in os.listdir(d) if n.startswith("events") and n.endswith(".seg"))


def same(name, d):
    """Both checkers on `d`: the exit status, the --kv lines and the problems must be the same. On a difference, answers the lines that differ."""
    rc1, out1, err1 = run_py(d)
    rc2, out2, err2 = run_native(d)
    ok = rc1 == rc2 and out1 == out2 and err1 == err2
    if ok:
        return True, ""
    lines1, lines2 = (out1 + err1).splitlines(), (out2 + err2).splitlines()
    only1 = [x for x in lines1 if x not in lines2]
    only2 = [x for x in lines2 if x not in lines1]
    return False, f"{name}: exit {rc1} vs {rc2}; python only: {only1}; lex-sys only: {only2}"


def damage(base, rng, k, kinds=None):
    """A copy of `base` damaged in one of many ways; answers (the directory, what was done)."""
    d = tempfile.mkdtemp(prefix="logcheck-bad-")
    shutil.rmtree(d)
    shutil.copytree(base, d)
    segs = segments(d)
    target = rng.choice(segs + ["delivery.seg", "delivery.seg"])
    path = os.path.join(d, target)
    data = bytearray(open(path, "rb").read())
    kind = rng.choice(kinds or ["flip", "flip", "cut", "zero", "append", "insert", "drop_segment", "swap_segments", "first_moved", "first_removed", "old_delivery", "header", "shape", "tail_zeros"])
    what = f"{kind} {target}"
    if kind == "flip" and data:
        i = rng.randrange(len(data))
        data[i] ^= 1 << rng.randrange(8)
        what += f" at {i}"
    elif kind == "cut" and data:
        i = rng.randrange(len(data))
        del data[i:]
        what += f" at {i}"
    elif kind == "zero" and len(data) > 64:
        i = rng.randrange(len(data) - 32)
        n = rng.randrange(1, 32)
        data[i:i + n] = bytes(n)
        what += f" {n} at {i}"
    elif kind == "append":
        data += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 90)))
    elif kind == "tail_zeros":
        data += bytes(rng.choice([4, 28, 200, 4096]))
    elif kind == "insert" and data:
        i = rng.randrange(len(data))
        data[i:i] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        what += f" at {i}"
    elif kind == "drop_segment" and len(segs) > 1:
        os.remove(path if target in segs else os.path.join(d, rng.choice(segs)))
    elif kind == "swap_segments" and len(segs) > 2:
        a, b = rng.sample(segs, 2)
        pa, pb = os.path.join(d, a), os.path.join(d, b)
        da, db = open(pa, "rb").read(), open(pb, "rb").read()
        open(pa, "wb").write(db)
        open(pb, "wb").write(da)
        what = f"swap {a} {b}"
    elif kind == "first_moved":
        open(os.path.join(d, "events.first"), "w").write(f"{rng.randrange(0, 4)}\n")
    elif kind == "first_removed":
        if os.path.exists(os.path.join(d, "events.first")):
            os.remove(os.path.join(d, "events.first"))
    elif kind == "old_delivery":
        # a delivery log from after the events log it is given: the events log is cut to its first segment
        for s in segs[1:]:
            os.remove(os.path.join(d, s))
    elif kind == "header" and target != "delivery.seg" and len(data) > 40:
        i = rng.randrange(16, 40)
        data[i] ^= 0x55
        what += f" header byte {i}"
    elif kind == "shape" and target != "delivery.seg":
        # a record that is whole (its CRC is right) but not a shape the service writes: the key `event` of one record becomes `evenX`, and its CRC is made again
        at, offsets = 0, []
        while True:
            size = PYCHECK.record_size(bytes(data), at)
            if not size:
                break
            offsets.append(at)
            at += size
        if len(offsets) > 3:
            at = rng.choice(offsets[2:])
            size = PYCHECK.record_size(bytes(data), at)
            key_at = at + 28 + 4
            if data[key_at:key_at + 5] == b"event":
                data[key_at + 4] = ord("X")
                struct.pack_into("<I", data, at + 4, PYCHECK.crc32c(bytes(data[at + 8:at + size])))
                what += f" record at {at}"
    if kind not in ("drop_segment", "swap_segments", "first_moved", "first_removed", "old_delivery"):
        open(path, "wb").write(bytes(data))
    return d, what


def main():
    base = make_dir()
    segs = segments(base)
    check(f"1. the service made a directory with {len(segs)} segments of the events log and a delivery log", len(segs) >= 3 and os.path.exists(os.path.join(base, "delivery.seg")), str(os.listdir(base)))
    before = digest(base)
    ok, why = same("the directory as the service left it", base)
    check("1. both say the same, and consistent: the exit status, the --kv lines, and no problem", ok and run_native(base)[0] == 0, str(why)[:300])
    # the JSON reports hold the same numbers
    rc1, j1, _ = run_py(base, kv=False)
    rc2, j2, _ = run_native(base, kv=False)
    p, n = json.loads(j1), json.loads(j2)
    for r in (p, n):
        r.pop("dir", None)
    check("1. the JSON reports are the same (the sizes, the counts, the kinds, the reasons)", rc1 == rc2 == 0 and p == n, f"{json.dumps(p, sort_keys=True)[:300]}\n{json.dumps(n, sort_keys=True)[:300]}")
    kinds = p["delivery"].get("kinds", {})
    check(f"1. the delivery log holds failures, dead letters and the reasons of failures ({kinds}, {p['delivery'].get('reasons')})", "2" in kinds and "3" in kinds and "14" in kinds, str(kinds))

    rng = random.Random(int(os.environ.get("LOGCHECK_SEED", "49")))
    bad, rejected, seen = [], 0, {}
    for k in range(200):
        d, what = damage(base, rng, k)
        ok, why = same(what, d)
        rc = run_native(d)[0]
        rejected += rc != 0
        seen[what.split()[0]] = seen.get(what.split()[0], 0) + 1
        if not ok:
            bad.append(why)
        shutil.rmtree(d, ignore_errors=True)
    check(f"2. 200 damaged copies ({rejected} of them not consistent; kinds: {dict(sorted(seen.items()))}): the exit status, the lines and the problems are the same in every one", not bad,
          "\n".join(str(x)[:600] for x in bad[:12]) + f"\n({len(bad)} differ)")
    check("2. the damage was real: most copies are not consistent, and some are (a torn tail, a cut at a record, a flip in a value that nothing reads is still caught by the CRC)", 60 <= rejected <= 199, str(rejected))
    check("3. the checks changed nothing: the directory is byte for byte what the service left", digest(base) == before, "")

    # 4. trim: a damaged file given to each on its own copy: the same exit status, the same words, the same bytes left
    bad, cut, refused = [], 0, 0
    for k in range(100):
        d, what = damage(base, rng, k, kinds=["cut", "cut", "append", "tail_zeros", "tail_zeros", "flip", "insert"])
        target = rng.choice(segments(d) + ["delivery.seg"])
        src = os.path.join(d, target)
        if not os.path.exists(src):
            shutil.rmtree(d, ignore_errors=True)
            continue
        t1, t2 = tempfile.mkdtemp(prefix="logcheck-t1-"), tempfile.mkdtemp(prefix="logcheck-t2-")
        f1, f2 = os.path.join(t1, "f.seg"), os.path.join(t2, "f.seg")
        shutil.copy(src, f1)
        shutil.copy(src, f2)
        p1 = subprocess.run([sys.executable, PY, "trim", f1], capture_output=True, text=True)
        p2 = subprocess.run([NATIVE, "trim", f2], capture_output=True, text=True)
        norm = lambda x, t: x.replace(t, "<dir>")
        same_bytes = open(f1, "rb").read() == open(f2, "rb").read()
        if p1.returncode == 0:
            cut += json.loads(p1.stdout)["cut"] > 0
        refused += p1.returncode == 1
        if not (p1.returncode == p2.returncode and norm(p1.stdout, t1) == norm(p2.stdout, t2) and norm(p1.stderr, t1) == norm(p2.stderr, t2) and same_bytes):
            bad.append(f"{what} / {target}: exit {p1.returncode} vs {p2.returncode}; {norm(p1.stdout, t1)!r} vs {norm(p2.stdout, t2)!r}; {norm(p1.stderr, t1)!r} vs {norm(p2.stderr, t2)!r}; bytes {'same' if same_bytes else 'differ'}")
        for x in (d, t1, t2):
            shutil.rmtree(x, ignore_errors=True)
    nofile = subprocess.run([NATIVE, "trim", "/nonexistent/f.seg"], capture_output=True, text=True)
    check(f"4. trim on 100 files with damaged tails and middles ({cut} cut, {refused} refused as damage in the middle): the exit status, the words and the bytes left are the same", not bad and nofile.returncode == 2, "\n".join(bad[:8]) + f"\n({len(bad)} differ; no file: {nofile.returncode})")

    # speed: printed, not judged (a slow machine is not a bug)
    t0 = time.time()
    run_native(base)
    t_native = time.time() - t0
    t0 = time.time()
    run_py(base)
    t_py = time.time() - t0
    size = sum(os.path.getsize(os.path.join(base, n)) for n in os.listdir(base) if n.endswith(".seg"))
    print(f"   {size / 1e6:.1f} MB of logs: lex-sys {t_native:.2f} s ({size / 1e6 / max(t_native, 1e-3):.0f} MB/s), Python {t_py:.2f} s ({size / 1e6 / max(t_py, 1e-3):.1f} MB/s)", flush=True)
    shutil.rmtree(base, ignore_errors=True)
    return check.finish("logcheck")


if __name__ == "__main__":
    sys.exit(main())
