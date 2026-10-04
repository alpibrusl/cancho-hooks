#!/bin/bash
# Pin the authority report. `lex-sys authority` says what the service can do (the capabilities it performs, with their arguments, whether the
# report is bounded, the foreign symbols it calls); `docs/authority.json` is that report as of the last time a person approved it. This script
# regenerates it from the sources and the installed libraries and fails on any difference, so a new foreign call or a new capability is a red diff
# that is only made green by committing the new file, which is the approval. The report's `unbounded_by` lists every reachable foreign symbol as
# `scope:symbol`, one per line (`libc:statx` today), so a new symbol is exactly one added line (lex-sys docs/foreign-authority.md).
#
#   scripts/check-authority.sh             compare; exit 0 if the report is the committed one, 1 (and show the diff) if not
#   scripts/check-authority.sh --update    write docs/authority.json (after reading what changed)
#
#   LEX_SYS   the lex-sys compiler binary        (default: lex-sys on PATH; the commit lex-sys.toml pins)
#
# Which program: the `hooks` bin of lex-sys.toml (its sources, and the libraries that `lex-sys install` writes to build/deps), with the standard library.
# What is left out of the pinned file: the list of provably pure functions and the three counts (`folded_operators`, `folded_calls`, `functions`).
# They change with every function anyone adds, say nothing about authority, and would make every change red. Everything else the report has is pinned,
# including any field a later compiler adds.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
pinned=$here/docs/authority.json

mode=compare
case "${1:-}" in
  "") ;;
  --update) mode=update ;;
  *) echo "usage: $0 [--update]" >&2; exit 2 ;;
esac

cd "$here"
"$LEX_SYS" install >&2

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The files of the program, as `lex-sys build` would be given them: the bin's sources (a directory is its .ls files), then the libraries.
python3 - "$here" > "$work/files" <<'PY'
import glob, os, sys, tomllib
here = sys.argv[1]
project = tomllib.load(open(os.path.join(here, "lex-sys.toml"), "rb"))
bins = [b for b in project["bin"] if b["name"] == "hooks"]
if len(bins) != 1:
    sys.exit("lex-sys.toml has no [[bin]] named hooks")
files = []
for source in bins[0]["sources"]:
    path = os.path.join(here, source)
    if os.path.isdir(path):
        files += sorted(glob.glob(os.path.join(path, "**", "*.ls"), recursive=True))
    else:
        files.append(path)
files += sorted(glob.glob(os.path.join(here, "build", "deps", "*.ls")))
for f in files:
    print(os.path.relpath(f, here))
PY
mapfile -t files < "$work/files"
[ "${#files[@]}" -gt 0 ] || { echo "check-authority: no source files" >&2; exit 2; }

"$LEX_SYS" authority "${files[@]}" --std --output json > "$work/raw.json"

python3 - "$work/raw.json" > "$work/report.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
if "unbounded_by" not in report:
    sys.exit("check-authority: this compiler's report has no `unbounded_by` (lex-sys before foreign authority, #247); use the compiler lex-sys.toml pins")
for volatile in ("pure", "folded_operators", "folded_calls", "functions"):
    report.pop(volatile, None)
print(json.dumps(report, indent=2))
PY

if [ "$mode" = update ]; then
  cp "$work/report.json" "$pinned"
  echo "check-authority: wrote docs/authority.json" >&2
  exit 0
fi

if [ ! -f "$pinned" ]; then
  echo "check-authority: docs/authority.json does not exist; run scripts/check-authority.sh --update and commit it" >&2
  exit 1
fi
if diff -u "$pinned" "$work/report.json" > "$work/diff"; then
  echo "check-authority: the authority report is the committed one" >&2
  exit 0
fi
cat "$work/diff"
echo >&2
echo "check-authority: the authority report changed. If the new foreign call or capability is intended, read the diff above, run" >&2
echo "  scripts/check-authority.sh --update" >&2
echo "and commit docs/authority.json: that commit is the approval." >&2
exit 1
