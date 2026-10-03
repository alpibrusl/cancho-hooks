#!/bin/bash
# Build the service from source.
#
#   scripts/build.sh [out]            (default: build/hooks)
#
# The compiler is the only thing this needs beside this repository (and `git`, to fetch). Its two libraries are locked in
# `deps/`: `deps/log.lock` pins a commit of lexsys-log and `deps/server.lock` a commit of lex-sys (its `http-server`
# package), and `lex-sys vcs fetch` fetches each commit into a cache, re-checks every pin (re-parses, re-typechecks and
# re-hashes the source) and writes the sources to `build/deps/<hash>.ls`. See `docs/design.md` section 18, and
# `scripts/lock.sh` to move a pin.
#
#   LEX_SYS     the lex-sys compiler binary        (default: lex-sys on PATH)
#   LOG_STORE   a store directory of lexsys-log's `.lex-sys-vcs/log`, to build against a local checkout of it instead of
#   SERVER_STORE   the locked commit; likewise lex-sys's `packages/http-server/.lex-sys-vcs`
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
out=${1:-$here/build/hooks}
mkdir -p "$(dirname "$out")"
deps="$here/build/deps"
rm -rf "$deps"   # a fetched file is named by its hash, so a stale one from an older lock would be a second declaration
mkdir -p "$deps"
for pair in "log:${LOG_STORE:-}" "server:${SERVER_STORE:-}"; do
  name=${pair%%:*}
  store=${pair#*:}
  if [ -n "$store" ]; then
    "$LEX_SYS" vcs fetch --lock "$here/deps/$name.lock" --store "$store" -o "$deps" >/dev/null
  else
    "$LEX_SYS" vcs fetch --lock "$here/deps/$name.lock" -o "$deps" >/dev/null
  fi
done
"$LEX_SYS" build "$here/src/hooks.ls" "$here/src/attempt.ls" "$here/src/sign.ls" "$here/src/state.ls" "$here/src/endpoints.ls" \
  "$here/src/idem.ls" "$deps"/*.ls --std -o "$out"
gcc -shared -fPIC -O2 -o "$(dirname "$out")/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
# The probe that tests signing and base64 on their own (tests/sign_test.py).
"$LEX_SYS" build "$here/tests/sign_probe.ls" "$here/src/sign.ls" --std -o "$(dirname "$out")/sign_probe"
