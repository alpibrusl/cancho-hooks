#!/bin/bash
# Build the service: `lex-sys build` (the programs and libraries are in lex-sys.toml), then the three small shims the tests preload: `fsync`
# for the crash tests (tests/fsync_shim.c), `fstat` and `fstatat` for the production profile's tests (tests/stat_shim.c) and `send`/`recv` for the partial-I/O tests (tests/io_shim.c).
#
#   LEX_SYS        the lex-sys compiler binary        (default: lex-sys on PATH)
#   LEX_SYS_PURE   the compiler for `--pure`          (default: LEX_SYS; the pure build pins a newer one than the default build, `pure/lex-sys.toml`)
#
#   scripts/build.sh --pure   build `pure/build/hooks-pure` instead: the service with lex-sys's own TLS and no OpenSSL (docs/pure-tls.md). Nothing is linked
#                             but libc, so it needs neither `libssl-dev` nor `scripts/cc-ssl.sh`.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
# The compiler may be named by a path relative to where this is run (CI does: `../lex-sys/target/release/lex-sys`); `--pure` runs it from `pure/`, so name it in full.
absolute() { case "$1" in */*) printf '%s/%s' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")" ;; *) printf '%s' "$1" ;; esac; }
LEX_SYS=${LEX_SYS:-lex-sys}
if [ "${1:-}" = --pure ]; then
  LEX_SYS_PURE=$(absolute "${LEX_SYS_PURE:-$LEX_SYS}")
  python3 "$here/scripts/make_pure.py"
  (cd "$here/pure" && "$LEX_SYS_PURE" build)
  exit 0
fi
# libssl and libcrypto are linked in by the C compiler driver, because the project file has no linking options (scripts/cc-ssl.sh).
(cd "$here" && CC="$here/scripts/cc-ssl.sh" "$LEX_SYS" build)
gcc -shared -fPIC -O2 -o "$here/build/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
gcc -shared -fPIC -O2 -o "$here/build/stat_shim.so" "$here/tests/stat_shim.c" -ldl
gcc -shared -fPIC -O2 -o "$here/build/io_shim.so" "$here/tests/io_shim.c" -ldl
