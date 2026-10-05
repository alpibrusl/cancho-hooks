#!/usr/bin/env python3
"""Read the two logs of a data directory without the service, and say whether they are a pair worth restoring.

    python3 scripts/logcheck.py check DIR [--kv]     # JSON report on stdout (or key=value lines); exit 0 if consistent, 1 if not
    python3 scripts/logcheck.py trim FILE            # cut FILE to its valid prefix (for a COPY: never run on a live file)

The format is lexsys-log's (its docs/design.md section 4): `len u32 | crc u32 | ms u64 | seq u64 | fields u32 | pairs...`, the CRC
being CRC-32C over everything after `len`. The reader is the one `tests/chaos.py` uses, written separately from the service.
`delivery.seg` records are the 40-byte outcomes of `src/state.ls` (`put_outcome`): kind, endpoint, event, attempts, next attempt.

What is checked, and why each one matters to a restore:

  * the events log (`events.seg`, `events-1.seg`, ..., from the segment `events.first` names; docs/retention.md): every record's CRC, a header of
    a format this version reads (1: none, in `events.seg` only; 2: the one it writes) at the front of each segment, each segment beginning where
    the one before it ends, ids dense from the first retained event (the service numbers events 1, 2, 3 ... and `GET /events/:id` relies on it;
    retention may have dropped the first ones), and every record one of the four shapes the service writes and reads (docs/design.md section
    35.1): the pairs `event`; `event`, `typ` (the event's
    type, when it has one); `event`, `key`, `t` (an idempotency key); or `event`, `typ`, `key`, `t`. A log from before event types has only
    the first and the third. Any other shape is one the service refuses to start on (status 16).
  * delivery.seg: every record is a well-formed outcome of a known kind (1 to 19; 19 states a slot's cursor, 18 marks a log that uses a slot of 62 or above, 15 is the header of a log written since retention, 14 the reason of a failed attempt, 16 a replay cancelled, 17 a dead letter in a snapshot).
  * **delivery.seg must not refer to an event that events.seg does not hold.** A service started on such a pair acknowledged new events
    under ids it already believed delivered, and never delivered them (measured: docs/runbook.md, "Backup"). The service refuses such a
    pair now too (status 18, docs/design.md section 34.5); this check is what keeps a backup from holding one.
  * a torn tail in the LAST segment is reported, and is harmless: the service cuts it at start (and says so). A last segment that is empty or
    whose header is cut is a roll caught half-done: it holds no event and is ignored, as the service ignores it. A sealed segment must end
    exactly at a record. Damage in the middle of a file is a
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
OUTCOME_KINDS = range(1, 20)         # 19 states a slot's cursor (`event`, an event id: docs/design.md 42); 18 says the log uses a slot of 62 or above (docs/design.md 41.5); 12 and 13 (a failure streak began, the circuit breaker paused an endpoint) are about an endpoint: `event` is not an event id; 15 is the header
FORMAT = 15                           # the outcomes log's header: `event` is the format number
EVENT_FORMAT = 2                      # the format of the events log this version writes (1 has no header)
EVENT_KINDS = (1, 2, 3, 6, 7, 8, 9, 14, 16, 17, 19)   # a cursor (19); delivered, failed, dead, replay, replay failed / delivered / dead, why an attempt failed (14), a replay cancelled (16), a dead letter of a snapshot (17): `event` is an event id
REASONS = {0: "none", 1: "connect_refused", 2: "connect_timeout", 3: "connect_error", 4: "send_timeout", 5: "send_error", 6: "no_response",
           7: "reset", 8: "closed_early", 9: "bad_response", 10: "status_3xx", 11: "status_4xx", 12: "status_5xx", 13: "gone",
           14: "status_other", 15: "busy", 16: "too_large",
           17: "dns_failed", 18: "dns_timeout", 19: "ssrf_refused", 20: "tls_handshake", 21: "cert_untrusted", 22: "cert_expired", 23: "cert_hostname", 24: "cert_invalid",
           25: "tls_timeout", 26: "tls_error"}   # src/reason.ls; kind 14 holds one in its fifth field, plus 256 for a replay's attempt
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


def is_header(ms, pairs):
    """The header record a file of the format since retention begins with: an events segment's (id 0, a `format` pair) or the outcomes log's (kind 15)."""
    if ms == 0 and pairs and pairs[0][0] == b"format":
        return True
    return len(pairs) == 1 and pairs[0][0] == b"o" and len(pairs[0][1]) == 40 and struct.unpack_from("<q", pairs[0][1], 0)[0] == FORMAT


def read_log(data, headers=False):
    """The longest valid prefix: ([(id, [(key, value), ...])], where it ends). A header record is left out unless `headers`."""
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
        if headers or not is_header(ms, pairs):
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


def segment_name(k):
    return "events.seg" if k == 0 else f"events-{k}.seg"


def segment_numbers(directory):
    """The numbers of the segments of the events log in `directory`, from the one `events.first` names, running up while the files exist."""
    first = 0
    try:
        first = int(open(os.path.join(directory, "events.first")).read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    out, k = [], first
    while os.path.exists(os.path.join(directory, segment_name(k))):
        out.append(k)
        k += 1
    return first, out


def read_header(data):
    """('header', size, k, base, first_id, version) / ('none',) for a format 1 segment / ('cut',) for nothing whole / ('bad', why)."""
    recs, end = read_log(data, headers=True)
    if not recs:
        return ("cut",)
    ms, pairs = recs[0]
    if ms >= 1:
        return ("none",)
    if ms != 0 or len(pairs) != 3 or pairs[0][0] != b"format" or pairs[1][0] != b"segment" or len(pairs[1][1]) != 32:
        return ("bad", "the first record is not a header this code knows")
    text = pairs[0][1]
    prefix = b"lexsys-hooks events "
    if not text.startswith(prefix) or not text[len(prefix):].isdigit():
        return ("bad", "an unreadable format")
    version = int(text[len(prefix):])
    k, base, first, _created = struct.unpack("<4Q", pairs[1][1])
    size = 4 + struct.unpack_from("<I", data, 0)[0]
    return ("header", size, k, base, first, version)


def damage_text(name, data, end, tail):
    return (f"{name}: damage in the middle: {len(data) - end} bytes after the last valid record (byte {end}) are not a torn tail"
            f" ({tail['found_records']} intact records start at byte {tail['found_at']}): the service refuses to start on it")


def check(directory):
    problems = []
    report = {"dir": directory, "events": None, "delivery": None, "problems": problems}
    first_k, numbers = segment_numbers(directory)
    dl = load(os.path.join(directory, "delivery.seg"))
    last_event = 0
    if not numbers:
        problems.append("the events log is missing (no events.seg, and none of the segments events.first names)")
    else:
        ids, total_bytes, torn, valid = [], 0, 0, 0
        expect_base, expect_id = None, None
        ignored_last = False
        shapes = {"event": 0, "event,typ": 0, "event,key,t": 0, "event,typ,key,t": 0}
        odd = 0
        for i, k in enumerate(numbers):
            data = load(os.path.join(directory, segment_name(k)))
            total_bytes += len(data)
            last = i == len(numbers) - 1
            head = read_header(data)
            recs, end = read_log(data)
            if head[0] == "bad":
                problems.append(f"{segment_name(k)}: {head[1]}")
                continue
            if head[0] == "cut":
                if last and k > first_k:
                    ignored_last = True      # a roll caught half-done: no events in it, ignored as the service ignores it
                    continue
                if not last or k != 0 or len(data) > 0:
                    problems.append(f"{segment_name(k)}: no whole record")
                continue
            hdr, base, first, version = 0, 0, 1, 1
            if head[0] == "header":
                _t, hdr, hk, base, first, version = head
                if version != EVENT_FORMAT:
                    problems.append(f"{segment_name(k)}: format {version}, which this version does not read")
                    continue
                if hk != k:
                    problems.append(f"{segment_name(k)}: its header says it is segment {hk}")
            elif k != 0:
                problems.append(f"{segment_name(k)}: no header (only events.seg may be a format 1 segment)")
            if expect_base is not None and (base != expect_base or first != expect_id):
                problems.append(f"{segment_name(k)}: begins at offset {base}, event {first}, but the segment before it ends at offset {expect_base}, event {expect_id - 1}")
            ids_here = [r[0] for r in recs]
            if ids_here != list(range(first, first + len(ids_here))):
                problems.append(f"{segment_name(k)}: ids are not dense from {first}")
            tail = classify_tail(data, end)
            if not last and tail["kind"] != "clean":
                problems.append(f"{segment_name(k)}: a sealed segment ends in {len(data) - end} bytes that are not whole records")
            if last:
                if tail["kind"] == "damage":
                    problems.append(damage_text(segment_name(k), data, end, tail))
                torn = len(data) - end
            for _id, pairs in recs:
                shape = ",".join(kk.decode("latin-1") for kk, _v in pairs)
                if shape in shapes:
                    shapes[shape] += 1
                else:
                    odd += 1
            expect_base = base + (end - hdr)
            expect_id = first + len(ids_here)
            ids += ids_here
            valid += end
        if odd:
            problems.append(f"the events log: {odd} record(s) are not of a shape this version writes (event, typ, key, t: the pairs `event`, then `typ` if typed, "
                            "then `key` and `t` if keyed): the service refuses to start on it (status 16)")
        first_event = ids[0] if ids else (expect_id if expect_id is not None else 1)
        last_event = ids[-1] if ids else (expect_id - 1 if expect_id is not None else 0)
        report["events"] = {"bytes": total_bytes, "valid_bytes": valid, "torn_bytes": torn, "records": len(ids), "first_id": first_event, "last_id": last_event,
                            "segments": len(numbers) - (1 if ignored_last else 0), "typed": shapes["event,typ"] + shapes["event,typ,key,t"]}
    if dl is None:
        # a service that never delivered has no delivery.seg; one that has endpoints and events has one by its first turn
        report["delivery"] = {"bytes": 0, "valid_bytes": 0, "torn_bytes": 0, "records": 0, "max_event_ref": 0, "missing": True}
    else:
        recs, end = read_log(dl, headers=True)
        torn = len(dl) - end
        max_ref, bad = 0, 0
        kinds, reasons = {}, {}
        for n, (_seq, pairs) in enumerate(recs):
            if len(pairs) != 1 or pairs[0][0] != b"o" or len(pairs[0][1]) != 40:
                bad += 1
                continue
            kind, _endpoint, event, attempts, _next = struct.unpack("<5q", pairs[0][1])
            if kind not in OUTCOME_KINDS:
                bad += 1
                continue
            if kind == FORMAT:
                if n != 0:
                    bad += 1
                elif event != EVENT_FORMAT:
                    problems.append(f"delivery.seg: format {event}, which this version does not read")
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
        report["delivery"] = {"bytes": len(dl), "valid_bytes": end, "torn_bytes": torn, "records": len([1 for r in recs if not is_header(*r)]), "max_event_ref": max_ref,
                              "kinds": {str(k): v for k, v in sorted(kinds.items())}, "reasons": reasons}
        if report["events"] is not None and max_ref > last_event:
            problems.append(f"delivery.seg refers to event {max_ref} but the events log ends at event {last_event}: restoring this pair would "
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
            for k, v in (("events_records", ev.get("records", 0)), ("events_first_id", ev.get("first_id", 1)), ("events_last_id", ev.get("last_id", 0)),
                         ("events_segments", ev.get("segments", 0)), ("events_bytes", ev.get("valid_bytes", 0)),
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
