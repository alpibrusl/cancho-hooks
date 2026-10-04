#!/usr/bin/env python3
"""Read the two logs of a data directory without the service, and say whether they are a pair worth restoring.

    python3 scripts/logcheck.py check DIR [--kv]     # JSON report on stdout (or key=value lines); exit 0 if consistent, 1 if not
    python3 scripts/logcheck.py trim FILE            # cut FILE to its valid prefix (for a COPY: never run on a live file)

The format is lexsys-log's (its docs/design.md section 4): `len u32 | crc u32 | ms u64 | seq u64 | fields u32 | pairs...`, the CRC
being CRC-32C over everything after `len`. The reader is the one `tests/chaos.py` uses, written separately from the service.
`delivery.seg` records are the 40-byte outcomes of `src/state.ls` (`put_outcome`): kind, endpoint, event, attempts, next attempt.

What is checked, and why each one matters to a restore:

  * events.seg: every record's CRC, ids dense from 1 (the service numbers events 1, 2, 3 ... and `GET /events/:id` relies on it).
  * delivery.seg: every record is a well-formed outcome of a known kind (1 to 13).
  * **delivery.seg must not refer to an event that events.seg does not hold.** A service started on such a pair acknowledges
    new events under ids it already believes delivered, and never delivers them (measured: docs/runbook.md, "Backup"). The
    service has no guard against it, so this check is the guard.
  * a torn tail (at most one record that does not validate) is reported, and is harmless: the service cuts it at start. More
    than that after a bad record is damage in the middle of the file, which would silently shorten the log, and is a problem.
    (A last record that is whole but corrupt looks the same as a torn one: the service cuts it too, and so does this.)

Standard library only. The CRC is table-driven Python: slow on a big log (the speed is measured in docs/runbook.md).
"""
import json
import os
import struct
import sys

MAX_RECORD = 65536          # a record's `len` is at most the service's max_len + 4 (65,536 bounds a torn tail)
OUTCOME_KINDS = range(1, 14)         # 12 and 13 (a failure streak began, the circuit breaker paused an endpoint) are about an endpoint: `event` is not an event id
EVENT_KINDS = (1, 2, 3, 6, 7, 8, 9)   # delivered, failed, dead, replay, replay failed / delivered / dead: `event` is an event id
CREATED = 10                          # `attempts` is the endpoint's starting cursor: an event id (or 0)

try:  # a C implementation if one happens to be installed; the table below is the fallback and the same function
    from crc32c import crc32c as _crc32c  # type: ignore
except ImportError:
    _crc32c = None


def _table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t


_T = _table()


def crc32c(data):
    if _crc32c is not None:
        return _crc32c(bytes(data))
    c = 0xFFFFFFFF
    t = _T
    for b in data:
        c = (c >> 8) ^ t[(c ^ b) & 0xFF]
    return c ^ 0xFFFFFFFF


def read_log(data):
    """The longest valid prefix: ([(id, [(key, value), ...])], where it ends)."""
    at, out = 0, []
    while len(data) - at >= 4:
        (length,) = struct.unpack_from("<I", data, at)
        if length < 24 or length > MAX_RECORD or len(data) - at < 4 + length:
            break
        total = 4 + length
        (stored,) = struct.unpack_from("<I", data, at + 4)
        if stored != crc32c(data[at + 8: at + total]):
            break
        ms, _seq, fields = struct.unpack_from("<QQI", data, at + 8)
        p, pairs, end, ok = at + 28, [], at + total, True
        for _ in range(fields):
            if end - p < 4:
                ok = False
                break
            (kl,) = struct.unpack_from("<I", data, p)
            p += 4
            key = data[p: p + kl]
            p += kl
            if end - p < 4:
                ok = False
                break
            (vl,) = struct.unpack_from("<I", data, p)
            p += 4
            pairs.append((key, data[p: p + vl]))
            p += vl
            if p > end:
                ok = False
                break
        if not ok or p != end:
            break
        out.append((ms, pairs))
        at += total
    return out, at


def tail_kind(tail):
    """What the bytes after the last valid record are: "clean" (none), "torn" (at most one record's worth that does not validate: a
    write the power cut or the copy caught half-done, which the service cuts at start), or "damage" (more than one record's worth
    after a bad one, so something valid may follow it: corruption in the middle, which would silently shorten the log)."""
    if not tail:
        return "clean"
    if len(tail) < 4:
        return "torn"
    (length,) = struct.unpack_from("<I", tail, 0)
    if 24 <= length <= MAX_RECORD and len(tail) <= 4 + length:
        return "torn"
    if len(tail) <= MAX_RECORD + 4 and not any(tail):
        return "torn"      # zeros: a page that never reached the disk
    return "damage"


def load(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def check(directory):
    problems = []
    report = {"dir": directory, "events": None, "delivery": None, "problems": problems}
    ev = load(os.path.join(directory, "events.seg"))
    dl = load(os.path.join(directory, "delivery.seg"))
    last_event = 0
    if ev is None:
        problems.append("events.seg is missing")
    else:
        recs, end = read_log(ev)
        ids = [r[0] for r in recs]
        torn = len(ev) - end
        if ids != list(range(1, len(ids) + 1)):
            problems.append("events.seg: ids are not dense from 1")
        if tail_kind(ev[end:]) == "damage":
            problems.append(f"events.seg: {torn} bytes after the last valid record that are more than one torn record: damage in the middle")
        last_event = len(ids)
        report["events"] = {"bytes": len(ev), "valid_bytes": end, "torn_bytes": torn, "records": len(ids), "last_id": ids[-1] if ids else 0}
    if dl is None:
        # a service that never delivered has no delivery.seg; one that has endpoints and events has one by its first turn
        report["delivery"] = {"bytes": 0, "valid_bytes": 0, "torn_bytes": 0, "records": 0, "max_event_ref": 0, "missing": True}
    else:
        recs, end = read_log(dl)
        torn = len(dl) - end
        max_ref, bad = 0, 0
        kinds = {}
        for _seq, pairs in recs:
            if len(pairs) != 1 or pairs[0][0] != b"o" or len(pairs[0][1]) != 40:
                bad += 1
                continue
            kind, _endpoint, event, attempts, _next = struct.unpack("<5q", pairs[0][1])
            if kind not in OUTCOME_KINDS:
                bad += 1
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind in EVENT_KINDS:
                max_ref = max(max_ref, event)
            elif kind == CREATED:
                max_ref = max(max_ref, attempts)
        if bad:
            problems.append(f"delivery.seg: {bad} record(s) are not outcomes this version writes")
        if tail_kind(dl[end:]) == "damage":
            problems.append(f"delivery.seg: {torn} bytes after the last valid record that are more than one torn record: damage in the middle")
        report["delivery"] = {"bytes": len(dl), "valid_bytes": end, "torn_bytes": torn, "records": len(recs), "max_event_ref": max_ref,
                              "kinds": {str(k): v for k, v in sorted(kinds.items())}}
        if ev is not None and max_ref > last_event:
            problems.append(f"delivery.seg refers to event {max_ref} but events.seg ends at event {last_event}: restoring this pair would "
                            "acknowledge new events under ids already recorded as delivered, and never deliver them")
    report["ok"] = not problems
    return report


def trim(path):
    data = load(path)
    if data is None:
        print(f"logcheck: {path}: no such file", file=sys.stderr)
        return 2
    _recs, end = read_log(data)
    if tail_kind(data[end:]) == "damage":
        print(f"logcheck: {path}: {len(data) - end} bytes after the last valid record are more than a torn record: damage in the middle, not trimming it", file=sys.stderr)
        return 1
    if end < len(data):
        with open(path, "r+b") as f:
            f.truncate(end)
            f.flush()
            os.fsync(f.fileno())
    print(json.dumps({"file": path, "valid_bytes": end, "cut": len(data) - end}))
    return 0


def main(argv):
    if len(argv) in (3, 4) and argv[1] == "check" and (len(argv) == 3 or argv[3] == "--kv"):
        r = check(argv[2])
        if len(argv) == 4:
            ev, dl = r["events"] or {}, r["delivery"] or {}
            for k, v in (("events_records", ev.get("records", 0)), ("events_last_id", ev.get("last_id", 0)), ("events_bytes", ev.get("valid_bytes", 0)),
                         ("events_torn_bytes", ev.get("torn_bytes", 0)), ("delivery_records", dl.get("records", 0)),
                         ("delivery_max_event_ref", dl.get("max_event_ref", 0)), ("delivery_bytes", dl.get("valid_bytes", 0)),
                         ("delivery_torn_bytes", dl.get("torn_bytes", 0))):
                print(f"{k}={v}")
            for problem in r["problems"]:
                print(f"problem: {problem}", file=sys.stderr)
        else:
            print(json.dumps(r, indent=1, sort_keys=True))
        return 0 if r["ok"] else 1
    if len(argv) == 3 and argv[1] == "trim":
        return trim(argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
