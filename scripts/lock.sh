#!/bin/bash
# Move the pins in deps/ to other commits.
#
#   scripts/lock.sh <lexsys-log commit> <lex-sys commit>      (full 40-digit hashes; a branch name is refused)
#
# Rewrites deps/log.lock and deps/server.lock from scratch against those commits. `LEX_SYS` is the compiler, as in build.sh.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
LEX_SYS=${LEX_SYS:-lex-sys}
[ $# -eq 2 ] || { echo "usage: scripts/lock.sh <lexsys-log commit> <lex-sys commit>" >&2; exit 2; }
mkdir -p "$here/deps"
rm -f "$here/deps/log.lock" "$here/deps/server.lock"
"$LEX_SYS" vcs lock --git https://github.com/alpibrusl/lexsys-log --rev "$1" --path .lex-sys-vcs/log -o "$here/deps/log.lock" --all
"$LEX_SYS" vcs lock --git https://github.com/alpibrusl/lex-sys --rev "$2" --path packages/http-server/.lex-sys-vcs -o "$here/deps/server.lock" --all
