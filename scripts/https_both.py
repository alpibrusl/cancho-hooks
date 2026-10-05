#!/usr/bin/env python3
"""The `https` delivery tests on both TLS backends, and the outcomes compared (docs/pure-tls.md).

    python3 scripts/https_both.py [build/hooks pure/build/hooks-pure]
    python3 scripts/https_both.py --check-pure pure/build/hooks-pure       # only the pure build: exit 1 if a check fails that is not listed in EXPECTED (what `scripts/mutate.py` runs)

Runs `tests/https_test.py` once against the OpenSSL build and once against the pure build. Each of its checks says `ok` or `FAIL` and what it checked, and
the checks name the outcome (delivered, or failed with which reason, and nothing else). So **the outcomes are equal when every check has the same verdict on
both**, and this prints each check whose verdict differs.

`EXPECTED` lists the checks that differ on purpose, with the reason. A difference that is not listed fails the run, and so does a listed one that has gone
away: the list is the whole of how the two backends may disagree here. Exit status 1 on either, and on a check one build ran and the other did not.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# check -> (verdict on OpenSSL, verdict on the pure build, why)
EXPECTED = {
    "6. the system's store honours SSL_CERT_FILE (no tls-ca-file): delivered, and no failure is counted":
        ("ok", "FAIL", "the pure build reads no environment variable (lex-sys has no access to one without a foreign call), so SSL_CERT_FILE names nothing to it; "
                       "the trust store is tls-ca-file, or the system's bundle at a usual path"),
}


def run(binary):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "https_test.py"), binary], capture_output=True, text=True, cwd=ROOT)
    checks = {}
    for line in out.stdout.splitlines():
        m = re.match(r"^(ok|FAIL)\s+(.*)$", line)
        if m:
            # A check's name may carry a measurement ("the longest wait 1.6 ms"): the verdict is what is compared, so the number is not part of the name.
            name = re.sub(r"\d+(?:\.\d+)? ms", "<n> ms", m.group(2).split("  {")[0].rstrip())
            checks[name] = m.group(1)
    if not checks:
        raise SystemExit(f"{binary}: no check ran:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    return checks


def check_pure(binary):
    """The pure build alone: its failed checks are all expected ones. (`tests/https_test.py` exits 1 on the pure build, always, because of the check that
    `SSL_CERT_FILE` is honoured, so its exit status cannot say whether a change broke anything: a mutation test would call every mutant killed.)"""
    bad = 0
    for name, verdict in sorted(run(binary).items()):
        if verdict == "FAIL" and not (name in EXPECTED and EXPECTED[name][1] == "FAIL"):
            print(f"FAIL {name}")
            bad += 1
    print(f"{bad} unexpected failures")
    sys.exit(1 if bad else 0)


def main():
    if sys.argv[1:2] == ["--check-pure"]:
        check_pure(sys.argv[2] if len(sys.argv) > 2 else "pure/build/hooks-pure")
    openssl, pure = (sys.argv[1:3] if len(sys.argv) >= 3 else ["build/hooks", "pure/build/hooks-pure"])
    a, b = run(openssl), run(pure)
    bad = 0
    for name in sorted(set(a) | set(b)):
        va, vb = a.get(name, "(not run)"), b.get(name, "(not run)")
        if va == vb and name not in EXPECTED:
            continue
        if name in EXPECTED and (va, vb) == EXPECTED[name][:2]:
            print(f"expected   {name}\n           OpenSSL {va}, pure {vb}: {EXPECTED[name][2]}")
            continue
        bad += 1
        print(f"DIFFERENT  {name}\n           OpenSSL {va}, pure {vb}")
    agree = sum(1 for n in a if n in b and a[n] == b[n] and n not in EXPECTED)
    print(f"{len(a)} checks on OpenSSL, {len(b)} on the pure build; {agree} agree, {len(EXPECTED)} differ on purpose, {bad} unexpected")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
