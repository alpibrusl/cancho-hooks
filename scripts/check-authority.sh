#!/bin/bash
# Pin the authority report. `lex-sys authority` says what the service can do (the capabilities it performs, with their arguments, whether the
# report is bounded, the foreign symbols it calls); `docs/authority.json` is that report as of the last time a person approved it. This script
# regenerates it from the sources and the installed libraries and fails on any difference, so a new foreign call or a new capability is a red diff
# that is only made green by committing the new file, which is the approval. The report's `unbounded_by` lists every reachable foreign symbol as
# `scope:symbol`, one per line (`libssl:SSL_read` today), so a new symbol is exactly one added line (lex-sys docs/foreign-authority.md).
#
#   scripts/check-authority.sh             compare; exit 0 if the report is the committed one, 1 (and show the diff) if not
#   scripts/check-authority.sh --update    write docs/authority.json (after reading what changed)
#   scripts/check-authority.sh --pure      the same for the build with lex-sys's own TLS: docs/authority-pure.json (`--update` writes it)
#
#   LEX_SYS   the lex-sys compiler binary        (default: lex-sys on PATH; the commit lex-sys.toml pins)
#
# Which program: the `hooks` bin of lex-sys.toml (its sources, and the libraries that `lex-sys install` writes to build/deps), with the standard library.
# What is left out of the pinned file: the list of provably pure functions and the three counts (`folded_operators`, `folded_calls`, `functions`).
# They change with every function anyone adds, say nothing about authority, and would make every change red. Everything else the report has is pinned,
# including any field a later compiler adds.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
# The compiler may be named by a path relative to where this is run (CI does: `../lex-sys/target/release/lex-sys`); `--pure` runs it from `pure/`, so name it in full.
absolute() { case "$1" in */*) printf '%s/%s' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")" ;; *) printf '%s' "$1" ;; esac; }
LEX_SYS=$(absolute "${LEX_SYS:-lex-sys}")

mode=compare
variant=default
for arg in "$@"; do
  case "$arg" in
    --update) mode=update ;;
    --pure) variant=pure ;;
    *) echo "usage: $0 [--update] [--pure]" >&2; exit 2 ;;
  esac
done

# The default build (OpenSSL), or with `--pure` the build with lex-sys's own TLS (`pure/`, docs/pure-tls.md): its own project, bin and pinned report.
if [ "$variant" = pure ]; then
  project=$here/pure
  bin=hooks-pure
  pinned=$here/docs/authority-pure.json
  python3 "$here/scripts/make_pure.py" >&2
else
  project=$here
  bin=hooks
  pinned=$here/docs/authority.json
fi

cd "$project"
"$LEX_SYS" install >&2

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The files of the program, as `lex-sys build` would be given them: the bin's sources (a directory is its .ls files), then the libraries.
python3 - "$project" "$bin" > "$work/files" <<'PY'
import glob, os, sys, tomllib
here = sys.argv[1]
project = tomllib.load(open(os.path.join(here, "lex-sys.toml"), "rb"))
bins = [b for b in project["bin"] if b["name"] == sys.argv[2]]
if len(bins) != 1:
    sys.exit(f"lex-sys.toml has no [[bin]] named {sys.argv[2]}")
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
  echo "check-authority: wrote ${pinned#"$here"/}" >&2
  exit 0
fi

if [ ! -f "$pinned" ]; then
  echo "check-authority: ${pinned#"$here"/} does not exist; run scripts/check-authority.sh --update${variant:+ }$([ "$variant" = pure ] && echo --pure) and commit it" >&2
  exit 1
fi
if diff -u "$pinned" "$work/report.json" > "$work/diff"; then
  echo "check-authority: the authority report is the committed one" >&2
  exit 0
fi
cat "$work/diff"
echo >&2
echo "check-authority: the authority report changed. If the new foreign call or capability is intended, read the diff above, run" >&2
echo "  scripts/check-authority.sh --update$([ "$variant" = pure ] && echo " --pure")" >&2
echo "and commit ${pinned#"$here"/}: that commit is the approval." >&2
exit 1
