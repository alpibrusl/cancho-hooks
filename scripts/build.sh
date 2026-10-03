#!/bin/bash
# Build the service from source.
#
#   scripts/build.sh [out]            (default: build/hooks)
#
# The sources come from three places, at the revisions CI builds and tests against:
#
#   LEX_SYS      the lex-sys compiler binary        (default: lex-sys on PATH)
#   LEX_SYS_DIR  a checkout of lex-sys              (default: ../lex-sys)   its packages/http-server is built in
#   LOG_DIR      a checkout of lexsys-log           (default: ../lexsys-log) its src/*.ls is built in
#
# `lex-sys` resolves a module by its name among the files it is given, so the packages are passed as files and no store
# is involved. That trades the hash check `vcs fetch` gives for one less moving part while the interfaces are settling;
# the compiler revision pinned in CI is what keeps the sources consistent.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
LEX_SYS_DIR=${LEX_SYS_DIR:-$here/../lex-sys}
LOG_DIR=${LOG_DIR:-$here/../lexsys-log}
out=${1:-$here/build/hooks}
mkdir -p "$(dirname "$out")"
"$LEX_SYS" build "$here/src/hooks.ls" "$here/src/attempt.ls" "$here/src/sign.ls" "$here/src/state.ls" "$here/src/endpoints.ls" "$here/src/idem.ls" \
  "$LEX_SYS_DIR/packages/http-server/server.ls" \
  "$LOG_DIR/src/log.ls" "$LOG_DIR/src/segment.ls" "$LOG_DIR/src/record.ls" "$LOG_DIR/src/crc.ls" \
  --std -o "$out"
gcc -shared -fPIC -O2 -o "$(dirname "$out")/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
# The probe that tests signing and base64 on their own (tests/sign_test.py).
"$LEX_SYS" build "$here/tests/sign_probe.ls" "$here/src/sign.ls" --std -o "$(dirname "$out")/sign_probe"
