#!/usr/bin/env python3
"""A hard maximum age (docs/design.md section 47.2): nothing keeps an event longer than `max-age-days`.

    python3 tests/maxage_test.py build/hooks

Retention drops an event only when it is final everywhere; an endpoint that never answers, or a waiting replay, keeps it for ever. Here the age is 3 s (the
tests' knob, `--max-age-ms`), retention is off (`retention-days 0`), segments are 256 KiB, and of two endpoints one delivers and one has nothing listening
(its events are never final: a retry an hour away).

  1. 2,000 events of 400 bytes, a replay of event 5 to the endpoint that is down (it waits behind the outcome of the first attempt): the segments past the age
     are sealed and dropped though they are pinned; the endpoint that is down has its cursor moved past them; /stats counts the expired events and segments;
     the waiting replay is ended; event 1 is a 410; the endpoint that delivers got every event once
  2. a restart: the cursors agree (the age keeps dropping while it runs: the endpoint that is down only moves forward), nothing is sent again, the dropped events are still 410
  3. max-age 0 (the default): the same pins keep every segment
"""
import collections
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()


def counting_peer():
    got, lock = collections.Counter(), threading.Lock()
    peer = L.Peer("ok")

    def serve(c):
        try:
            g = peer._read_request(c)
            if g is None:
                return
            with lock:
                got[int(g[0][b"webhook-id"][4:])] += 1
            c.sendall(b"HTTP/1.1 204 X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
    peer._serve = serve
    return peer, got


def segments(d):
    return sorted(n for n in os.listdir(d) if n.startswith("events") and n.endswith(".seg"))


def run(max_age_ms):
    d = tempfile.mkdtemp(prefix="hooks-maxage-")
    peer, got = counting_peer()
    open(os.path.join(d, "endpoints.conf"), "w").write(f"1 127.0.0.1 {peer.port} {L.secret()}\n2 127.0.0.1 {L.closed_port()} {L.secret()}\n")
    args = ["--retention-days", "0", "--segment-bytes", "262144", "--window-ms", "1000", "--schedule", "3600000"]
    if max_age_ms:
        args += ["--max-age-ms", str(max_age_ms)]
    svc = L.Service(BIN, d, args)
    svc.start(timeout=30)
    pad = "x" * 400
    # The replay is asked for after the first events, not after all 2000: on a slow machine posting them can take longer than the age, and by then
    # event 5's segment is already expired and the answer is a 410 (CI run 37530950131), which is not what this check is about.
    for n in range(10):
        svc.post_event(n, extra={"pad": pad})
    s, _ = svc.request("POST", "/events/5/replay/2", b"")[:2]
    for n in range(10, 2000):
        svc.post_event(n, extra={"pad": pad})
    return d, svc, peer, got, s


def cursors(svc):
    return {e["id"]: e["cursor"] for e in json.loads(svc.get("/endpoints")[1])}


def main():
    # 1.
    d, svc, peer, got, replay_status = run(3000)
    before = len(segments(d))
    gone = L.wait_for(lambda: svc.stats()["segments_dropped"] >= 1 and svc.stats()["segments_expired"] >= 1, 30)
    st = svc.stats()
    cur = cursors(svc)
    check(f"1. the segments past the age are dropped though an endpoint that is down pins them ({before} segments, {st['segments_dropped']} dropped, {st['segments_expired']} by the age)",
          gone and st["segments_dropped"] >= 1, str(st))
    check(f"1. the endpoint that is down has its cursor moved past them ({cur.get(2)} >= {st['events_first_id'] - 1})", cur.get(2, 0) >= st["events_first_id"] - 1, f"{cur} {st['events_first_id']}")
    check(f"1. /stats counts the expired events ({st['events_expired']})", st["events_expired"] >= st["events_first_id"] - 2, str(st["events_expired"]))
    check(f"1. the replay waiting for event 5 (asked: {replay_status}) is ended", replay_status == 202 and st["replays"] == 0, str(st["replays"]))
    code, body = svc.get("/events/1")
    check("1. event 1 is a 410", code == 410, f"{code} {body}")
    all_in = L.wait_for(lambda: len(got) >= 2000, 30)
    check(f"1. the endpoint that delivers got every event once ({len(got)}, the most {max(got.values()) if got else 0})", all_in and max(got.values()) == 1, str(len(got)))
    # 2.
    svc.stop(10)
    svc.start(timeout=30)
    time.sleep(2)
    cur2 = cursors(svc)
    first2 = svc.stats()["events_first_id"]
    check(f"2. after a restart the cursors agree ({cur} / {cur2}: the one that delivers where it was, the one that is down no lower, and past what the age has dropped since), nothing is sent again, event 1 is still 410",
          cur2[1] == cur[1] and cur2[2] >= cur[2] and cur2[2] >= first2 - 1 and max(got.values()) == 1 and svc.get("/events/1")[0] == 410, f"{cur} {cur2} {first2}")
    svc.kill()
    peer.close()
    # 3.
    d, svc, peer, got, _ = run(0)
    time.sleep(6)
    st = svc.stats()
    check(f"3. without a maximum age the same pins keep every segment ({st['segments_dropped']} dropped, {len(segments(d))} on disk)", st["segments_dropped"] == 0 and st["events_expired"] == 0, str(st))
    svc.kill()
    peer.close()
    return check.finish("maxage")


if __name__ == "__main__":
    sys.exit(main())
