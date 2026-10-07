#!/usr/bin/env python3
"""Mutation testing on a COPY of the tree (docs/design.md section 40.10): the work tree is never touched.

    LEX_SYS=/path/to/lex-sys HOOKS_PG=host:port:user:database python3 scripts/mutate.py tests/mutants/https.py [id ...]
    PURE=1 LEX_SYS=/path/to/lex-sys python3 scripts/mutate.py tests/mutants/pure.py [id ...]     # the build with lex-sys's own TLS: `scripts/build.sh --pure`, tested as pure/build/hooks-pure

The tree (without .git) is copied to a temporary directory and built there. For each mutant of the file named (a list `MUTANTS` of `(id, file, old, new, [test sets])`): the text `old`, which
must occur exactly once in `file`, is replaced by `new`; the service is built; the test sets are run in order until one fails (the mutant is killed) or all pass (it survives); the file
is restored and compared byte for byte with what it was (`filecmp`, a mutant never stays on disk). A mutant that does not compile is reported as that (the checker killed it).
A mutants file may name its own build (`BUILD = [...]`, where `{LEX}` is the compiler): `tests/mutants/mcp.py` rebuilds only `hooks-mcp` and runs `tests/mcp_test.py` with the service that is already in `build/`.
Test sets: unit (`lex-sys test`), https, names, sessions, api, ssrf, reason, metrics, config, patch, slots, delete, ... (the `tests/*_test.py` of those names), many (all stages of
`tests/many_test.py`) and many:<stage> (one).
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) < 2:
    sys.exit(__doc__)
lex = os.environ.get("LEX_SYS", "lex-sys")
only = set(sys.argv[2:])
namespace = {}
exec(open(sys.argv[1]).read(), namespace)
MUTANTS = namespace["MUTANTS"]

copy = tempfile.mkdtemp(prefix="hooks-mutants-")
for name in os.listdir(here):
    if name != ".git":
        (shutil.copytree if os.path.isdir(os.path.join(here, name)) else shutil.copy2)(os.path.join(here, name), os.path.join(copy, name))
env = dict(os.environ, LEX_SYS=lex)
PURE = os.environ.get("PURE") == "1"
BUILD = ["scripts/build.sh", "--pure"] if PURE else ["scripts/build.sh"]
BUILD = [w.replace("{LEX}", lex) for w in namespace.get("BUILD", BUILD)]
BIN = "pure/build/hooks-pure" if PURE else "build/hooks"
TESTS = {"unit": [lex, "test"]}
for name in ("https", "names", "sessions", "api", "ssrf", "reason", "metrics", "config", "patch", "slots", "delete", "breaker", "retry", "scan", "replay", "gone", "layout", "dead", "limits", "pgre", "retention", "cancel", "isolation", "pure", "mcp", "keepalive"):
    TESTS[name] = ["python3", f"tests/{'https_api' if name == 'api' else name}_test.py", BIN]
if PURE:
    # `tests/https_test.py` always fails on the pure build (the check that SSL_CERT_FILE is honoured, which it is not), so a failure of it is no sign of a mutant killed.
    TESTS["https"] = ["python3", "scripts/https_both.py", "--check-pure", BIN]
# the first check that fails is the verdict (and the test says so at once, instead of finishing its minutes)
TESTS["mcp"] = ["env", "FAIL_FAST=1", "python3", "tests/mcp_test.py", BIN]
# the stages of tests/many_test.py (design section 41): "many:flags" runs only that stage, "many" all of them
TESTS["many"] = ["python3", "tests/many_test.py", BIN]
for stage in ("limit", "chaos", "flags", "replay", "quiet", "retention", "compactnow", "pool", "formats"):
    TESTS[f"many:{stage}"] = ["env", f"STAGES={stage}", "python3", "tests/many_test.py", BIN]


def run(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, env=env, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


rc, out = run(BUILD, 600)
if rc != 0:
    sys.exit("the unmutated tree does not build:\n" + out[-2000:])
results = []
for mid, path, old, new, tests in MUTANTS:
    if only and mid not in only:
        continue
    full = os.path.join(copy, path)
    saved = full + ".saved"
    shutil.copyfile(full, saved)
    src = open(full).read()
    if src.count(old) != 1:
        print(f"{mid}: the text to change occurs {src.count(old)} times in {path}", flush=True)
        os.remove(saved)
        results.append((mid, "bad"))
        continue
    open(full, "w").write(src.replace(old, new))
    t0 = time.time()
    detail = []
    try:
        rc, out = run(BUILD, 600)
        if rc != 0:
            status, detail = "does not compile", out.strip().splitlines()[-2:]
        else:
            status = "SURVIVED"
            for t in tests:
                rc, out = run(TESTS[t])
                if rc != 0:
                    status = f"killed by {t}"
                    detail = [l for l in out.splitlines() if l.startswith("FAIL") or "FAILED" in l][:2]
                    break
    finally:
        shutil.copyfile(saved, full)
        same = filecmp.cmp(saved, full, shallow=False)
        os.remove(saved)
    print(f"{mid}: {status} ({time.time() - t0:.0f}s) restored-identical={same} {' | '.join(d[:100] for d in detail)}", flush=True)
    results.append((mid, status))
shutil.rmtree(copy, ignore_errors=True)
print("killed", sum(1 for r in results if r[1].startswith("killed")), "| survived", [r[0] for r in results if r[1] == "SURVIVED"],
      "| did not compile", [r[0] for r in results if r[1] == "does not compile"], "| bad", [r[0] for r in results if r[1] == "bad"])
sys.exit(0 if all(r[1] != "SURVIVED" and r[1] != "bad" for r in results) else 1)
