#!/bin/bash
# `docs/openapi.json` is GENERATED: the service's routes and the document are one declaration (`src/api.ls`, docs/design.md section 52). This builds
# the program that prints the document (`hooks-openapi`, tools/openapi.ls: it has no network, clock, filesystem or foreign code, only the console),
# runs it, and either writes the file or compares it with the committed one.
#
#   scripts/openapi.sh            write docs/openapi.json
#   scripts/openapi.sh --check    regenerate into a temporary file and fail, showing the diff, if docs/openapi.json is not what the declaration says
#
#   LEX_SYS   the lex-sys compiler binary        (default: lex-sys on PATH; the commit lex-sys.toml pins)
#
# The program prints compact JSON; the committed file is the same document indented by two spaces (`python3`'s json module, member order kept), so that a
# change to the API is a readable diff. The order of the members is the order of the declaration; nothing is sorted.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
(cd "$here" && "$LEX_SYS" install >&2 && "$LEX_SYS" build --bin hooks-openapi >&2)
"$here/build/hooks-openapi" > "$work/compact.json"
python3 - "$work/compact.json" > "$work/openapi.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1])), indent=2, ensure_ascii=False))
PY
if [ "${1:-}" = --check ]; then
  if ! diff -u "$here/docs/openapi.json" "$work/openapi.json"; then
    echo "openapi: docs/openapi.json is not what src/api.ls declares; run scripts/openapi.sh and commit the result" >&2
    exit 1
  fi
  echo "docs/openapi.json is what src/api.ls declares"
else
  cp "$work/openapi.json" "$here/docs/openapi.json"
  echo "wrote docs/openapi.json"
fi
