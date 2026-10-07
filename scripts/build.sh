#!/bin/bash
# Build the service: `cancho build` (the programs and libraries are in cancho.toml), then the three small shims the tests preload: `fsync`
# for the crash tests (tests/fsync_shim.c), `fstat` and `fstatat` for the production profile's tests (tests/stat_shim.c) and `send`/`recv` for the partial-I/O tests (tests/io_shim.c).
#
#   CANCHO        the cancho compiler binary        (default: cancho on PATH)
#   CANCHO_PURE   the compiler for `--pure`          (default: CANCHO; the pure build pins a newer one than the default build, `pure/cancho.toml`)
#
#   scripts/build.sh --pure   build `pure/build/hooks-pure` instead: the service with cancho's own TLS and no OpenSSL (docs/pure-tls.md). Nothing is linked
#                             but libc, so it needs neither `libssl-dev` nor `scripts/cc-ssl.sh`. The shims are built beside it too, if `gcc` is there:
#                             the harnesses look for them beside the binary they are given, and a power cut without the fsync shim is not one.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
# The compiler may be named by a path relative to where this is run (CI does: `../cancho/target/release/cancho`); `--pure` runs it from `pure/`, so name it in full.
absolute() { case "$1" in */*) printf '%s/%s' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")" ;; *) printf '%s' "$1" ;; esac; }
CANCHO=${CANCHO:-cancho}
# The shims the tests preload, beside the binary in $1 (`retention_test.py`, `schedules_test.py` and `production_test.py` look for them there).
shims() {
  gcc -shared -fPIC -O2 -o "$1/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
  gcc -shared -fPIC -O2 -o "$1/stat_shim.so" "$here/tests/stat_shim.c" -ldl
  gcc -shared -fPIC -O2 -o "$1/io_shim.so" "$here/tests/io_shim.c" -ldl
}
if [ "${1:-}" = --pure ]; then
  CANCHO_PURE=$(absolute "${CANCHO_PURE:-$CANCHO}")
  python3 "$here/scripts/make_pure.py"
  (cd "$here/pure" && "$CANCHO_PURE" build)
  if command -v gcc >/dev/null; then
    shims "$here/pure/build"
  else
    echo "build.sh: no gcc, so no test shims in pure/build: the power-cut tests refuse to run against pure/build/hooks-pure without them" >&2
  fi
  exit 0
fi
# libssl and libcrypto are linked in by the C compiler driver, because the project file has no linking options (scripts/cc-ssl.sh).
(cd "$here" && CC="$here/scripts/cc-ssl.sh" "$CANCHO" build)
shims "$here/build"
