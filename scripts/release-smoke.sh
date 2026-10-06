#!/bin/bash
# Try a release tarball the way a person who downloaded it would: check it against SHA256SUMS, unpack it, start the service from it,
# post an event, read it back, and stop it.
#
#   scripts/release-smoke.sh dist/hooks-<version>-linux-<arch>/hooks-<version>-linux-<arch>.tar.gz
#
# It looks only at the tarball and the SHA256SUMS beside it, not at this checkout. Exit: 0 it works; 1 something did not; 2 usage.
set -euo pipefail

tarball=${1:?usage: release-smoke.sh path/to/hooks-<version>-linux-<arch>.tar.gz}
[ -f "$tarball" ] || { echo "release-smoke: $tarball is not a file" >&2; exit 2; }
dir=$(cd "$(dirname "$tarball")" && pwd)
base=$(basename "$tarball")
work=$(mktemp -d)
pid=""
cleanup() { if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi; rm -rf "$work"; }
trap cleanup EXIT
fail() { echo "release-smoke: FAIL: $*" >&2; exit 1; }

# 1. the checksum a downloader would check
(cd "$dir" && grep -F " $base" SHA256SUMS | sha256sum -c -) || fail "$base does not match SHA256SUMS"

# 2. the tarball holds what it says
tar -xzf "$tarball" -C "$work"
root=$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)
for f in bin/hooks deploy/hooks.service scripts/backup.sh scripts/restore.sh sql/schema.sql README.md LICENSE Dockerfile; do
  [ -e "$root/$f" ] || fail "the tarball has no $f"
done
[ -x "$root/bin/hooks" ] || fail "bin/hooks is not executable"

# 3. it runs: start, post, read back
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
mkdir "$work/data"
"$root/bin/hooks" --port "$port" --dir "$work/data" >"$work/out.log" 2>&1 &
pid=$!
ready=0
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$port/readyz" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$pid" 2>/dev/null || { cat "$work/out.log" >&2; fail "the service ended before it was ready"; }
  sleep 0.1
done
[ "$ready" = 1 ] || { cat "$work/out.log" >&2; fail "/readyz never answered"; }

code=$(curl -sS -o "$work/post.json" -w '%{http_code}' -X POST "http://127.0.0.1:$port/events" -d '{"type":"smoke.test","n":1}')
[ "$code" = 202 ] || fail "POST /events answered $code, not 202: $(cat "$work/post.json")"
grep -q '"id":1' "$work/post.json" || fail "the first event did not get id 1: $(cat "$work/post.json")"
code=$(curl -sS -o "$work/get.json" -w '%{http_code}' "http://127.0.0.1:$port/events/1")
[ "$code" = 200 ] || fail "GET /events/1 answered $code"
grep -q 'smoke.test' "$work/get.json" || fail "the event read back is not the one posted: $(cat "$work/get.json")"

# 4. and it stops when told
kill -TERM "$pid"
for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || { pid=""; break; }; sleep 0.1; done
[ -z "$pid" ] || fail "the service did not stop on SIGTERM within 10 s"

echo "release-smoke: ok: $base verifies, unpacks, starts, takes an event and stops"
