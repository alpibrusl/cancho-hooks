#!/bin/bash
# Build the service: `lex-sys build` (the programs and libraries are in lex-sys.toml), then the two small shims the tests preload: `fsync`
# for the crash tests (tests/fsync_shim.c) and `statx` for the production profile's tests (tests/statx_shim.c).
#
#   LEX_SYS   the lex-sys compiler binary        (default: lex-sys on PATH)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
(cd "$here" && "$LEX_SYS" build)
gcc -shared -fPIC -O2 -o "$here/build/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
gcc -shared -fPIC -O2 -o "$here/build/statx_shim.so" "$here/tests/statx_shim.c" -ldl
