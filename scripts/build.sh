#!/bin/bash
# Build the service: `lex-sys build` (the programs and libraries are in lex-sys.toml), then the three small shims the tests preload: `fsync`
# for the crash tests (tests/fsync_shim.c), `statx` for the production profile's tests (tests/statx_shim.c) and `send`/`recv` for the partial-I/O tests (tests/io_shim.c).
#
#   LEX_SYS   the lex-sys compiler binary        (default: lex-sys on PATH)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
# libssl and libcrypto are linked in by the C compiler driver, because the project file has no linking options (scripts/cc-ssl.sh).
(cd "$here" && CC="$here/scripts/cc-ssl.sh" "$LEX_SYS" build)
gcc -shared -fPIC -O2 -o "$here/build/fsync_shim.so" "$here/tests/fsync_shim.c" -ldl
gcc -shared -fPIC -O2 -o "$here/build/statx_shim.so" "$here/tests/statx_shim.c" -ldl
gcc -shared -fPIC -O2 -o "$here/build/io_shim.so" "$here/tests/io_shim.c" -ldl
