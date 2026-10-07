#!/usr/bin/env bash
# Use case 6: erase and expire personal data.
#
# Part one: erase one event (DELETE /events/:id): it answers 410 from then on, it is never delivered, and the bytes of its body are no longer in the
# data directory (grep says so). Part two: --max-age-days (here its seconds twin for tests, --max-age-ms) drops what is older, even events that an
# endpoint that is down would keep for ever.
#
# What is NOT erased is said at the end and in docs/privacy.md: the event's type, id, size and idempotency key, the rows of the history (they hold
# no body), backups taken before, and the copies the receivers already have.
#
# Needs: bash, curl, python3, grep. No database. The retry delay is long on purpose: the events wait, undelivered, for an endpoint that is down.
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

CUSTOMER_PORT=$(free_port)
new_secret >"$TMP/customer.secret"
say "part one. One endpoint that is down for now; two events wait for it, one of them about a person"
run "echo \"0 127.0.0.1 \$CUSTOMER_PORT \$(cat customer.secret)\" > \$DATA/endpoints.conf"
echo "0 127.0.0.1 $CUSTOMER_PORT $(cat "$TMP/customer.secret")" >"$DATA/endpoints.conf"
start_service --schedule 4000,4000,4000 --retry-jitter 0
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"customer.updated\",\"email\":\"ana@example.com\"}'"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"order.created\",\"email\":\"ben@example.com\"}'"
run "curl -s -w ' [%{http_code}]\n' \$URL/events/1"
run "grep -rl ana@example.com \$DATA | sed \"s|^\$DATA/||\""
check "the address is in a file of the data directory" test "$OUT" = "events.seg"

say "erasure"
run "curl -s -w ' [%{http_code}]\n' -X DELETE \$URL/events/1 -H \"Authorization: Bearer \$ADMIN\""
check "erased" test "${OUT##* }" = "[200]"
run "curl -s -w ' [%{http_code}]\n' \$URL/events/1"
check "it is gone: 410" test "${OUT##* }" = "[410]"
run "(grep -rl ana@example.com \$DATA || echo 'ana@example.com: in no file') | sed \"s|^\$DATA/||\""
check "the address is in no file of the data directory" test "$OUT" = "ana@example.com: in no file"
run "grep -rl ben@example.com \$DATA | sed \"s|^\$DATA/||\""
check "the other event is untouched" test "$OUT" = "events.seg"
say "what is not erased: the event's type (and its id, its size and its idempotency key if it had one)"
run "grep -rao customer.updated \$DATA | sed \"s|^\$DATA/||\""
check "the type of the erased event is still in the log" test "$OUT" = "events.seg:customer.updated"

say "the customer's server comes back: it gets the event that is left, and never the erased one"
start_receiver customer "$CUSTOMER_PORT"
received customer 1 | sed 's/^/  /'
sleep 1
check "only the event that was not erased arrived" test "$(delivered customer)" = 1 -a "$(grep -c 'evt_2 ' "$TMP/customer.out")" = 1
stop_receiver customer
stop_service

say "part two. Events that nothing may keep: an endpoint that is down pins them (retention only drops what is final everywhere)"
DATA=$TMP/data2
mkdir -p "$DATA"
run "echo \"0 127.0.0.1 \$CUSTOMER_PORT \$(cat customer.secret)\" > \$DATA/endpoints.conf"
echo "0 127.0.0.1 $CUSTOMER_PORT $(cat "$TMP/customer.secret")" >"$DATA/endpoints.conf"
start_service --segment-bytes 262144 --schedule 3600000
say "six events of 60 KB: more than a 256 KiB segment, so the first segment is sealed"
run "printf '{\"type\":\"export.ready\",\"pad\":\"%s\"}' \"\$(head -c 60000 /dev/zero | tr '\\0' x)\" > big.json"
printf '{"type":"export.ready","pad":"%s"}' "$(head -c 60000 /dev/zero | tr '\0' x)" >"$TMP/big.json"
run "for i in 1 2 3 4 5 6; do curl -s -w ' [%{http_code}]\n' -X POST \$URL/events --data-binary @big.json; done"
sleep 4
run "curl -s -o /dev/null -w '%{http_code}\n' \$URL/events/1"
check "no maximum age: event 1 is still there, held by the endpoint that is down" test "$OUT" = 200
stop_service

say "the same data, with a maximum age of 3 seconds (the real setting is --max-age-days; this is its knob for tests)"
start_service --segment-bytes 262144 --schedule 3600000 --max-age-ms 3000
gone() { [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/events/1")" = 410 ]; }
wait_until 30 gone || { echo "event 1 was not dropped"; exit 1; }
run "curl -s -o /dev/null -w '%{http_code}\n' \$URL/events/1"
run "curl -s \$URL/stats | grep -oE '\"(events_expired|segments_expired)\":[0-9]+'"
check "the expired events are counted, not hidden" test "$(echo "$OUT" | grep -c '":[1-9]')" = 2
