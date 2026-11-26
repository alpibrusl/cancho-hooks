#!/bin/bash
# Build the service: `cancho build` (the programs and libraries are in cancho.toml), then the three small shims the tests preload: `fsync`
# for the crash tests (tests/fsync_shim.c), `fstat` and `fstatat` for the production profile's tests (tests/stat_shim.c) and `send`/`recv` for the partial-I/O tests (tests/io_shim.c).
#
#   CANCHO        the cancho compiler binary        (default: cancho on PATH)
#   CANCHO_PURE   the compiler for `--pure`          (default: CANCHO; the pure build pins a newer one than the default build, `pure/cancho.toml`)
#
#   scripts/build.sh --pure   build `pure/build/hooks-pure` instead: the service with cancho's own TLS and no OpenSSL (docs/pure-tls.md). Nothing is linked
#                             but libc, so it needs no `libssl-dev`. The shims are built beside it too, if `gcc` is there:
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
# libssl and libcrypto are linked in with `-l ssl -l crypto`. The standalone compiler reads no `CC` (docs/standalone-cancho.md) and the project mode takes no `-l`, so
# the `hooks` bin is built from its files, as `cancho build` would be given them: its sources, then the libraries `cancho install` writes to build/deps (the same
# list scripts/check-authority.sh reports on). `OPENSSL_LIB` names a directory to search (-L), for a system whose OpenSSL is not on the linker's path (Homebrew's).
cd "$here"
"$CANCHO" install
mkdir -p build
files=$(find src -name '*.cho' | LC_ALL=C sort; find build/deps -maxdepth 1 -name '*.cho' | LC_ALL=C sort)
# shellcheck disable=SC2086
"$CANCHO" build $files --std ${OPENSSL_LIB:+-L "$OPENSSL_LIB"} -l ssl -l crypto -o build/hooks
# The other programs of cancho.toml (the probes the tests run, the OpenAPI writer, the MCP server, the log checker) link nothing but libc, so the project mode
# builds them, one `--bin` each: `cancho build` alone would stop at `hooks`, which it cannot link. (Before the line above replaced `CC=scripts/cc-ssl.sh cancho build`,
# that one command built them all; without this loop build/sign_probe and build/cron_probe are not there and the tests that run them fail.)
awk '/^\[\[bin\]\]/ { in_bin = 1; next } in_bin && /^name *=/ { gsub(/^name *= *"|"$/, ""); if ($0 != "hooks") print; in_bin = 0 }' cancho.toml | while read -r bin; do
  "$CANCHO" build --bin "$bin"
done
shims "$here/build"
