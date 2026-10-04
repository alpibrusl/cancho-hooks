#!/usr/bin/env python3
"""Read the two logs of a data directory without the service, and say whether they are a pair worth restoring.

    python3 scripts/logcheck.py check DIR [--kv]     # JSON report on stdout (or key=value lines); exit 0 if consistent, 1 if not
    python3 scripts/logcheck.py trim FILE            # cut FILE to its valid prefix (for a COPY: never run on a live file)

The format is lexsys-log's (its docs/design.md section 4): `len u32 | crc u32 | ms u64 | seq u64 | fields u32 | pairs...`, the CRC
being CRC-32C over everything after `len`. The reader is the one `tests/chaos.py` uses, written separately from the service.
`delivery.seg` records are the 40-byte outcomes of `src/state.ls` (`put_outcome`): kind, endpoint, event, attempts, next attempt.

What is checked, and why each one matters to a restore:

  * events.seg: every record's CRC, ids dense from 1 (the service numbers events 1, 2, 3 ... and `GET /events/:id` relies on it), and every
    record one of the four shapes the service writes and reads (docs/design.md section 35.1): the pairs `event`; `event`, `typ` (the event's
    type, when it has one); `event`, `key`, `t` (an idempotency key); or `event`, `typ`, `key`, `t`. A log from before event types has only
    the first and the third. Any other shape is one the service refuses to start on (status 16).
  * delivery.seg: every record is a well-formed outcome of a known kind (1 to 14).
  * **delivery.seg must not refer to an event that events.seg does not hold.** A service started on such a pair acknowledged new events
    under ids it already believed delivered, and never delivered them (measured: docs/runbook.md, "Backup"). The service refuses such a
    pair now too (status 18, docs/design.md section 34.5); this check is what keeps a backup from holding one.
  * a torn tail is reported, and is harmless: the service cuts it at start (and says so). Damage in the middle of the file is a
    problem: the service refuses to start on it (status 19) unless given `--repair-logs 1`. The rule is the service's (src/logguard.ls)
    and this file is its reference: after the last whole record, the bytes are damage if a record that validates starts anywhere in
    them, or if they parse as two records of plausible length one after the other; otherwise they are a torn tail (an unfinished
    write, a page of zeros). A last record that is whole but corrupt looks the same as a torn one: the service cuts it too, and so
    does this.

Standard library only. The CRC is table-driven Python: slow on a big log (the speed is measured in docs/runbook.md).
"""
import json
import os
import re
import struct
import sys

MAX_RECORD = 65536          # a record's `len` is at most the service's max_len + 4 (65,536 bounds a torn tail)
OUTCOME_KINDS = range(1, 15)         # 12 and 13 (a failure streak began, the circuit breaker paused an endpoint) are about an endpoint: `event` is not an event id
EVENT_KINDS = (1, 2, 3, 6, 7, 8, 9, 14)   # delivered, failed, dead, replay, replay failed / delivered / dead, why an attempt failed (14): `event` is an event id
REASONS = {0: "none", 1: "connect_refused", 2: "connect_timeout", 3: "connect_error", 4: "send_timeout", 5: "send_error", 6: "no_response",
           7: "reset", 8: "closed_early", 9: "bad_response", 10: "status_3xx", 11: "status_4xx", 12: "status_5xx", 13: "gone",
           14: "status_other", 15: "busy", 16: "too_large"}   # src/reason.ls; kind 14 holds one in its fifth field, plus 256 for a replay's attempt
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


def record_size(data, at):
    """The size in bytes of the whole, valid record at `at` (length in range, CRC agrees, the pairs fill it exactly), else 0."""
    if len(data) - at < 4:
        return 0
    (length,) = struct.unpack_from("<I", data, at)
    if length < 24 or length > MAX_RECORD or len(data) - at < 4 + length:
        return 0
    total = 4 + length
    (stored,) = struct.unpack_from("<I", data, at + 4)
    if stored != crc32c(data[at + 8: at + total]):
        return 0
    if data[at + 15] >= 128 or data[at + 23] >= 128:      # an id past 2^63 cannot be held in the service's `int`
        return 0
    fields = struct.unpack_from("<I", data, at + 24)[0]
    p, end = at + 28, at + total
    for _ in range(fields):
        if end - p < 4:
            return 0
        (kl,) = struct.unpack_from("<I", data, p)
        p += 4 + kl
        if end - p < 4:
            return 0
        (vl,) = struct.unpack_from("<I", data, p)
        p += 4 + vl
        if p > end:
            return 0
    return total if p == end else 0


def read_log(data):
    """The longest valid prefix: ([(id, [(key, value), ...])], where it ends)."""
    at, out, prev = 0, [], (-1, -1)
    while True:
        total = record_size(data, at)
        if not total:
            break
        ms, _seq, fields = struct.unpack_from("<QQI", data, at + 8)
        if (ms, _seq) <= prev:       # ids strictly increase within a log (lexsys-log `segment.scan`): a record that goes backwards is where the log stops
            break
        prev = (ms, _seq)
        p, pairs = at + 28, []
        for _ in range(fields):
            (kl,) = struct.unpack_from("<I", data, p)
            key = data[p + 4: p + 4 + kl]
            p += 4 + kl
            (vl,) = struct.unpack_from("<I", data, p)
            pairs.append((key, data[p + 4: p + 4 + vl]))
            p += 4 + vl
        out.append((ms, pairs))
        at += total
    return out, at


_PLAUSIBLE = re.compile(rb"(?=(?:[\x18-\xff].|[\x00-\x17][\x01-\xff])\x00\x00|\x00\x00\x01\x00)", re.S)


def classify_tail(data, end):
    """What the bytes after the last valid record (`data[end:]`) are, by the service's rule (src/logguard.ls): a dict with
    `kind` "clean", "torn" (an unfinished write or a page of zeros: safe to cut) or "damage" (a record that validates starts after
    the first bad place, or the bytes parse as two records of plausible length in a row: cutting would throw away intact records, or
    ones a crash does not produce), and the numbers the service reports: `tail` bytes, `found_at` (the offset of the first intact
    record after `end`, or -1), `found_records` (intact records from there, in a row), `chain` (plausible-length records in a row from
    `end`, at most 2)."""
    out = {"kind": "clean", "tail": len(data) - end, "found_at": -1, "found_records": 0, "chain": 0}
    if end >= len(data):
        return out
    found = -1
    for m in _PLAUSIBLE.finditer(data, end + 1):
        if record_size(data, m.start()):
            found = m.start()
            break
    out["found_at"] = found
    if found >= 0:
        at, n = found, 0
        while at < len(data):
            size = record_size(data, at)
            if not size:
                break
            n += 1
            at += size
        out["found_records"] = n
    at, chain = end, 0
    while chain < 2 and len(data) - at >= 4:
        (length,) = struct.unpack_from("<I", data, at)
        if length < 24 or length > MAX_RECORD or at + 4 + length > len(data):
            break
        chain += 1
        at += 4 + length
    out["chain"] = chain
    out["kind"] = "damage" if found >= 0 or chain >= 2 else "torn"
    return out


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
        tail = classify_tail(ev, end)
        if tail["kind"] == "damage":
            problems.append(f"events.seg: damage in the middle: {torn} bytes after the last valid record (byte {end}) are not a torn tail"
                            f" ({tail['found_records']} intact records start at byte {tail['found_at']}): the service refuses to start on it")
        shapes = {"event": 0, "event,typ": 0, "event,key,t": 0, "event,typ,key,t": 0}
        odd = 0
        for _id, pairs in recs:
            shape = ",".join(k.decode("latin-1") for k, _v in pairs)
            if shape in shapes:
                shapes[shape] += 1
            else:
                odd += 1
        if odd:
            problems.append(f"events.seg: {odd} record(s) are not of a shape this version writes (event, typ, key, t: the pairs `event`, then `typ` if typed, "
                            "then `key` and `t` if keyed): the service refuses to start on it (status 16)")
        last_event = len(ids)
        report["events"] = {"bytes": len(ev), "valid_bytes": end, "torn_bytes": torn, "records": len(ids), "last_id": ids[-1] if ids else 0,
                            "typed": shapes["event,typ"] + shapes["event,typ,key,t"]}
    if dl is None:
        # a service that never delivered has no delivery.seg; one that has endpoints and events has one by its first turn
        report["delivery"] = {"bytes": 0, "valid_bytes": 0, "torn_bytes": 0, "records": 0, "max_event_ref": 0, "missing": True}
    else:
        recs, end = read_log(dl)
        torn = len(dl) - end
        max_ref, bad = 0, 0
        kinds, reasons = {}, {}
        for _seq, pairs in recs:
            if len(pairs) != 1 or pairs[0][0] != b"o" or len(pairs[0][1]) != 40:
                bad += 1
                continue
            kind, _endpoint, event, attempts, _next = struct.unpack("<5q", pairs[0][1])
            if kind not in OUTCOME_KINDS:
                bad += 1
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == 14:
                name = REASONS.get(_next & 255, "unknown")
                reasons[name] = reasons.get(name, 0) + 1
            if kind in EVENT_KINDS:
                max_ref = max(max_ref, event)
            elif kind == CREATED:
                max_ref = max(max_ref, attempts)
        if bad:
            problems.append(f"delivery.seg: {bad} record(s) are not outcomes this version writes")
        tail = classify_tail(dl, end)
        if tail["kind"] == "damage":
            problems.append(f"delivery.seg: damage in the middle: {torn} bytes after the last valid record (byte {end}) are not a torn tail"
                            f" ({tail['found_records']} intact records start at byte {tail['found_at']}): the service refuses to start on it")
        report["delivery"] = {"bytes": len(dl), "valid_bytes": end, "torn_bytes": torn, "records": len(recs), "max_event_ref": max_ref,
                              "kinds": {str(k): v for k, v in sorted(kinds.items())}, "reasons": reasons}
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
    if classify_tail(data, end)["kind"] == "damage":
        print(f"logcheck: {path}: {len(data) - end} bytes after the last valid record (byte {end}) are not a torn tail: damage in the middle, not trimming it", file=sys.stderr)
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
