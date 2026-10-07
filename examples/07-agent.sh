#!/usr/bin/env bash
# Use case 7: let an agent look.
#
# hooks-mcp is an MCP server (JSON-RPC lines on standard input and output) that makes ordinary read requests to the service. Without --allow-write
# the tools that change anything are not even listed, and a call to one is refused. This script is the conversation an agent's client would have.
#
# Needs: bash, curl, python3 and build/hooks-mcp (HOOKS_MCP=path to use another). No database.
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

if [ ! -x "$HOOKS_MCP" ]; then
  echo "no hooks-mcp at $HOOKS_MCP: build it (scripts/build.sh) or set HOOKS_MCP=path/to/hooks-mcp" >&2
  exit 2
fi
say "a service with one endpoint nobody listens to, and one event that cannot be delivered"
echo "0 127.0.0.1 $(free_port) $(new_secret)" >"$DATA/endpoints.conf"
start_service --schedule 100 --retry-jitter 0
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"order.shipped\",\"order\":7}'"
dead() { [ "$(curl -s "$URL/stats" | grep -o '"dead":[0-9]*')" = '"dead":1' ]; }
wait_until 20 dead || { echo "the event did not die"; exit 1; }

say "the agent's client says hello, lists the tools, then asks about the event, the dead letters and tries to post an event"
cat >requests.jsonl <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hooks_get_event","arguments":{"event_id":1}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"hooks_list_dead_letters","arguments":{"endpoint_id":0}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"hooks_post_event","arguments":{"fields":{"order":8},"type":"order.shipped"}}}
JSON
hooks-mcp() { "$HOOKS_MCP" "$@"; }
run "hooks-mcp --url \$URL < requests.jsonl > replies.jsonl; echo \"\$(wc -l < replies.jsonl) replies\""
say "the tools it was offered (read-only: nothing here changes anything)"
run "grep '\"id\":2,' replies.jsonl | grep -o '\"name\":\"hooks_[a-z_]*\"'"
check "six tools, and no hooks_post_event among them" test "$(echo "$OUT" | wc -l | tr -d ' ')" = 6 -a "$(echo "$OUT" | grep -c post_event)" = 0
say "what it was told about the event and the dead letters, and the refusal of the write"
run "grep -E '\"id\":[345],' replies.jsonl"
check "the event, the dead letter, and an unknown-tool error for the post" test "$(echo "$OUT" | grep -c 'order.shipped')" -ge 2 -a "$(echo "$OUT" | grep -c '"error"')" = 1
run "curl -s -w ' [%{http_code}]\n' \$URL/events/2"
check "and no event 2 was made" test "${OUT##* }" = "[404]"
