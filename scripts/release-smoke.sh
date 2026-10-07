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
for f in bin/hooks bin/hooks-mcp bin/hooks-logcheck deploy/hooks.service scripts/backup.sh scripts/restore.sh sql/schema.sql README.md LICENSE Dockerfile; do
  [ -e "$root/$f" ] || fail "the tarball has no $f"
done
[ -x "$root/bin/hooks" ] || fail "bin/hooks is not executable"
[ -x "$root/bin/hooks-mcp" ] || fail "bin/hooks-mcp is not executable"
[ -x "$root/bin/hooks-logcheck" ] || fail "bin/hooks-logcheck is not executable"

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

# 3b. the MCP server answers a handshake against it, and offers only the read tools
mcp_in='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hooks_get_event","arguments":{"event_id":1}}}'
mcp_out=$(printf '%s\n' "$mcp_in" | "$root/bin/hooks-mcp" --url "http://127.0.0.1:$port") || fail "hooks-mcp did not exit 0 at the end of its input"
echo "$mcp_out" | grep -q '"protocolVersion"' || fail "hooks-mcp did not answer initialize: $mcp_out"
echo "$mcp_out" | grep -q 'hooks_get_event' || fail "hooks-mcp did not list its tools: $mcp_out"
if echo "$mcp_out" | grep -q 'hooks_post_event'; then fail "hooks-mcp lists a write tool without --allow-write"; fi
echo "$mcp_out" | grep -q 'smoke.test' || fail "hooks-mcp did not read back the event: $mcp_out"

# 4. and it stops when told
kill -TERM "$pid"
for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || { pid=""; break; }; sleep 0.1; done
[ -z "$pid" ] || fail "the service did not stop on SIGTERM within 10 s"

# 5. the log checker that backup.sh and restore.sh use finds the directory the service left whole
"$root/bin/hooks-logcheck" check "$work/data" >"$work/logcheck.out" 2>&1 || { cat "$work/logcheck.out" >&2; fail "hooks-logcheck says the directory the service left is not whole"; }

echo "release-smoke: ok: $base verifies, unpacks, starts, takes an event and stops; hooks-mcp reads it; hooks-logcheck finds the directory whole"
