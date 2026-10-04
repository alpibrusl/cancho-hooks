#!/usr/bin/env python3
"""`src/sign.ls` against independent implementations: the reference Python library `standardwebhooks` for signatures, and
Python's `base64` for the encoding.

    pip install standardwebhooks
    python3 tests/sign_test.py build/sign_probe

Lengths are chosen around SHA-256's block boundaries (55, 56, 63, 64, 65 bytes of signed content) and HMAC's key boundary
(secrets that decode to under, exactly, and over 64 bytes, where the key is hashed first), and up to the largest event the
service accepts, where the old HMAC trapped (`docs/design.md` section 30).
"""
import base64
import os
import random
import subprocess
import sys

from standardwebhooks import Webhook

PROBE = sys.argv[1] if len(sys.argv) > 1 else "build/sign_probe"
rng = random.Random(3)


def run(*args):
    return subprocess.run([PROBE, *args], capture_output=True, timeout=20).stdout[:-1]


def main():
    fails = checks = 0

    def check(what, got, want):
        nonlocal fails, checks
        checks += 1
        if got != want:
            fails += 1
            if fails <= 10:
                print(f"FAIL {what}: got {got[:80]!r}, want {want[:80]!r}")

    # base64: every length from 0 to 40, random bytes (no NUL, which an argument cannot hold), both directions.
    for n in list(range(0, 41)) + [255, 256, 1000]:
        data = bytes(rng.choice([b for b in range(1, 256)]) for _ in range(n))
        enc = base64.b64encode(data)
        if n:
            check(f"encode {n}", run("b64", data), enc)
        check(f"decode {n}", run("unb64", enc), data if n else b"")
    for bad in [b"abc", b"ab=c", b"a===", b"====", b"ab!d", b"abcd=", b"YQ=a"]:
        check(f"reject {bad!r}", run("unb64", bad), b"error")

    # signatures, against the reference library
    for key_len in [1, 16, 24, 32, 63, 64, 65, 100, 128]:
        key = os.urandom(key_len)
        secret = "whsec_" + base64.b64encode(key).decode()
        wh = Webhook(secret)
        bare = base64.b64encode(key).decode()  # a secret without the prefix is accepted by the library too
        for payload_len in [0, 1, 7, 40, 41, 42, 55, 56, 63, 64, 65, 127, 128, 1000, 5000, 20000]:
            payload = "".join(chr(rng.randrange(32, 127)) for _ in range(payload_len))
            if payload_len > 3:
                payload = payload.replace(payload[1], "\n", 1)  # a newline and a quote in there
            for msg_id, ts in [("evt_1", 1700000000), ("msg_2KWPBgLlAfxdpx2AI54pPJ85f4W", 1674087231), ("x", 9)]:
                want = wh.sign(msg_id, __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc), payload).encode()
                got = run("sig", secret, msg_id, str(ts), payload)
                check(f"sig key={key_len} payload={payload_len} id={msg_id}", got, want)
        check(f"sig without prefix key={key_len}", run("sig", bare, "evt_1", "5", "{}"),
              wh.sign("evt_1", __import__("datetime").datetime.fromtimestamp(5, __import__("datetime").timezone.utc), "{}").encode())
    check("sig of a non-base64 secret", run("sig", "whsec_not base64!", "a", "1", "b"), b"error")

    # The largest events the service accepts (65,498 bytes unkeyed, docs/design.md section 30): with the old HMAC, signing
    # any payload from 65,446 bytes up trapped, because `crypto.sha256` copied the signed content into a 64 KiB arena.
    key = os.urandom(32)
    secret = "whsec_" + base64.b64encode(key).decode()
    wh = Webhook(secret)
    for payload_len in [65400, 65445, 65446, 65460, 65498, 70000]:
        payload = "".join(chr(rng.randrange(32, 127)) for _ in range(payload_len))
        for msg_id in ["evt_1", "evt_123456789"]:
            want = wh.sign(msg_id, __import__("datetime").datetime.fromtimestamp(1700000000, __import__("datetime").timezone.utc), payload).encode()
            check(f"sig payload={payload_len} id={msg_id}", run("sig", secret, msg_id, "1700000000", payload), want)
    print(f"{checks} checks, {fails} failures")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
