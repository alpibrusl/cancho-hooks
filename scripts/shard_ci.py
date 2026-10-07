#!/usr/bin/env python3
"""Split the test job of .github/workflows/ci.yml into parallel shards, and keep the split right.

    scripts/shard_ci.py .github/workflows/ci.yml steps.tsv [--shards N]     split (the first time) or rebalance
    scripts/shard_ci.py .github/workflows/ci.yml --check                    fail if a step is in no shard, or in a shard that does not exist

steps.tsv has one line per step, `name TAB startedAt TAB completedAt`, from a recent run (every shard of it, or the one job of an unsharded
run):

    gh run view RUN --repo alpibrusl/cancho-hooks --json jobs \\
      --jq '.jobs[]|select(.name|startswith("test"))|.steps[]|[.name,.startedAt,.completedAt]|@tsv' > steps.tsv

Every step of the job that is a test (not setup) gets `if: matrix.shard == 'x'`, the shard chosen longest step first on the measured durations,
so that the shards take about the same time. Setup steps (the checkouts, the compiler, the build, the libraries the tests need) run in every
shard. The job is a matrix called test-shards, and a job called test that needs all of them keeps the name callers know.

A step added to the job needs a shard: run this to give it one. A step with none would run in every shard, which is correct and N times as slow,
and --check, which CI runs, refuses it.
"""
import csv
import datetime as dt
import re
import sys

SETUP_PREFIX = ("Read the compiler pin", "Cache the compiler", "Build cancho", "OpenSSL", "Install the libraries, build",
                "The reference library for Standard Webhooks")
SHARD_IF = re.compile(r"^        if: matrix\.shard == '(.)'\s*$")
JOB_NOTE = "  # One name for all of the shards, for whoever waits for `test`."


def split_blocks(steps):
    """The steps of the job: a block starts at `      - `, and the comments (6 spaces, then #) just before it belong to it."""
    blocks, cur, pending = [], None, []
    for line in steps:
        if re.match(r"^      - ", line):
            cur = pending + [line]
            pending = []
            blocks.append(cur)
        elif re.match(r"^      #", line):
            pending.append(line)
        elif pending:
            pending.append(line)
        elif cur is not None:
            cur.append(line)
        else:
            sys.exit("text before the first step: " + line)
    if pending:
        blocks[-1].extend(pending)
    return blocks


def name_of(block):
    for line in block:
        m = re.match(r'^      - name: "?(.*?)"?\s*$', line)
        if m:
            return m.group(1)
    return None


def shard_of(block):
    for line in block:
        m = SHARD_IF.match(line)
        if m:
            return m.group(1)
    return None


def has_own_if(block):
    return any(re.match(r"^        if: ", line) and not SHARD_IF.match(line) for line in block)


def kind_of(block):
    """setup: runs in every shard; test: divided; upload: the artifact of the binary, once."""
    n = name_of(block)
    if n is None:
        return "upload" if "actions/upload-artifact" in "\n".join(block) else "setup"
    if n.startswith(SETUP_PREFIX) or has_own_if(block):
        return "setup"
    if n.startswith("Keep the service binary"):
        return "upload"
    return "test"


def find_job(lines):
    sharded = "  test-shards:" in lines
    start = next(i for i, l in enumerate(lines) if l == ("  test-shards:" if sharded else "  test:"))
    end = next(i for i, l in enumerate(lines) if l == (JOB_NOTE if sharded else "  pure-tls:"))
    return sharded, start, end


def main():
    path = sys.argv[1]
    check = "--check" in sys.argv
    lines = open(path).read().split("\n")
    sharded, start, end = find_job(lines)
    job = lines[start:end]
    si = next(i for i, l in enumerate(job) if l == "    steps:")
    head, blocks = job[: si + 1], split_blocks(job[si + 1:])

    if check:
        if not sharded:
            sys.exit("shard_ci: the test job is not sharded")
        m = re.search(r"shard: \[(.*?)\]", "\n".join(head))
        known = set(re.findall(r"\w", m.group(1))) if m else set()
        problems = []
        for b in blocks:
            k, s, n = kind_of(b), shard_of(b), name_of(b) or "(a step without a name)"
            if k in ("test", "upload") and s is None:
                problems.append(f"in no shard: {n[:90]}")
            elif s is not None and s not in known:
                problems.append(f"in shard {s}, which is not in the matrix {sorted(known)}: {n[:90]}")
            elif k == "setup" and s is not None:
                problems.append(f"a setup step must run in every shard: {n[:90]}")
        if problems:
            print("\n".join(problems))
            sys.exit(f"shard_ci: {len(problems)} step(s) to give a shard (scripts/shard_ci.py, see its documentation)")
        print(f"shard_ci: every step is in a shard of {sorted(known)} or is setup")
        return

    tsv = sys.argv[2]
    n_shards = int(sys.argv[sys.argv.index("--shards") + 1]) if "--shards" in sys.argv else 4
    names = "abcdefgh"[:n_shards]
    dur = {}
    for row in csv.reader(open(tsv), delimiter="\t"):
        if len(row) == 3 and row[1] and row[2]:
            def t(x):
                return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
            dur[row[0]] = (t(row[2]) - t(row[1])).total_seconds()

    if sharded:                       # rebalancing: take the old conditions off first
        blocks = [[l for l in b if not SHARD_IF.match(l)] for b in blocks]

    tests = [b for b in blocks if kind_of(b) == "test"]
    uploads = [b for b in blocks if kind_of(b) == "upload"]
    bins = {s: 0.0 for s in names}
    assign = {}
    for b in sorted(tests, key=lambda b: -dur.get(name_of(b), 20.0)):
        s = min(bins, key=lambda k: bins[k])
        assign[id(b)] = s
        bins[s] += dur.get(name_of(b), 20.0)
    for b in uploads:                 # the artifact of the binary is uploaded once, by the first shard
        assign[id(b)] = names[0]
        bins[names[0]] += 2.0

    out_steps = []
    for b in blocks:
        if id(b) in assign:
            c = b[:]
            i = next(j for j, l in enumerate(c) if re.match(r"^      - ", l))
            c.insert(i + 1, f"        if: matrix.shard == '{assign[id(b)]}'")
            out_steps.extend(c)
        else:
            out_steps.extend(b)

    if not sharded:                   # the first split: rename the job and make it a matrix
        head[0] = "  test-shards:"
        ri = next(i for i, l in enumerate(head) if l.startswith("    runs-on:"))
        head[ri:ri] = [
            "    # The job is split in parallel shards (scripts/shard_ci.py made the split from the durations of a run, longest step first): each shard",
            "    # builds the service and runs its share of the steps, with a PostgreSQL of its own. The job `test` below needs them all.",
            "    name: test (${{ matrix.shard }})",
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
            f"        shard: [{', '.join(names)}]",
        ]
    new_job = head + out_steps
    while new_job and new_job[-1] == "":
        new_job.pop()
    new_job.append("")
    aggregate = [] if sharded else [
        JOB_NOTE,
        "  test:",
        "    if: always()",
        "    needs: test-shards",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - name: Every step is in a shard (scripts/shard_ci.py --check)",
        "        run: python3 scripts/shard_ci.py .github/workflows/ci.yml --check",
        "      - name: Every shard passed",
        "        run: test \"${{ needs.test-shards.result }}\" = success",
        "",
    ]
    lines[start:end] = new_job + aggregate
    open(path, "w").write("\n".join(lines))

    print("shards:", ", ".join(f"{s} {bins[s]:.0f}s" for s in names))
    print(f"{len(tests)} test steps divided, {len(uploads)} upload step(s) on shard {names[0]}, "
          f"{sum(1 for b in blocks if kind_of(b) == 'setup')} setup steps in every shard")


main()
