#!/usr/bin/env bash
# Use case 2: payments that must not be lost or doubled.
#
# An event is acknowledged (202) only after it is on disk. Send it twice with the same Idempotency-Key and there is one event; kill -9 the
# service after the 202 and before any delivery, and the event is delivered once the service is back; a repeat of a delivery carries the same
# webhook-id, so the receiver drops it.
#
# Needs: bash, curl, python3. No database. The retry delays are shortened for the demo (--schedule 500,...: the default first retry is 5 seconds).
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

SHOP_PORT=$(free_port)
SECRET=$(new_secret)
echo "$SECRET" >"$TMP/shop.secret"
say "one endpoint, the shop's server, which is not running yet"
run "echo \"0 127.0.0.1 \$SHOP_PORT \$SECRET\" > \$DATA/endpoints.conf"
echo "0 127.0.0.1 $SHOP_PORT $SECRET" >"$DATA/endpoints.conf"
start_service --schedule 500,500,500,500,500,500 --retry-jitter 0

say "the same payment is sent twice, with the same Idempotency-Key (a client that did not hear the first answer would do this)"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -H 'Idempotency-Key: order-1042' -d '{\"type\":\"payment.captured\",\"order\":1042,\"amount\":4900}'"
FIRST=$OUT
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -H 'Idempotency-Key: order-1042' -d '{\"type\":\"payment.captured\",\"order\":1042,\"amount\":4900}'"
check "the same id comes back: one event" test "$FIRST" = "$OUT"
say "the same key for a different payment is refused, not merged"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -H 'Idempotency-Key: order-1042' -d '{\"type\":\"payment.captured\",\"order\":1042,\"amount\":9900}'"
check "refused with 422" test "${OUT##* }" = "[422]"

say "the 202 came after the event was flushed to disk. Now the service dies, hard, before it has delivered anything"
# (not `run`: bash tells of a job that was killed, "Killed", at the moment it reaps it, which is in the shell's own turn when the service dies at once and in the wait when it is a
# little slower to die. The kill and the wait are one group with its error output discarded, so the report goes to the same place on a quiet machine and on a busy one)
# shellcheck disable=SC2016
printf '%s\n' '$ kill -9 $SVC_PID'
{ kill -9 "$SVC_PID"; wait "$SVC_PID"; } 2>/dev/null || true
start_service --schedule 500,500,500,500,500,500 --retry-jitter 0
say "the key was remembered too: asking again does not make a second event"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -H 'Idempotency-Key: order-1042' -d '{\"type\":\"payment.captured\",\"order\":1042,\"amount\":4900}'"
check "still the same id" test "$FIRST" = "$OUT"

say "the shop's server comes back"
start_receiver shop "$SHOP_PORT"
received shop 1 | sed 's/^/  /'
sleep 3
check "it arrived, once, although the service was killed and the shop was down" test "$(delivered shop)" = 1

say "a repeat of a delivery has the same webhook-id (evt_1): the receiver drops it. Here the service is asked to send the event again"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events/1/replay -H \"Authorization: Bearer \$ADMIN\""
received shop 2 | sed 's/^/  /'
check "the second delivery is the same webhook-id and the receiver said duplicate" test "$(grep -c 'evt_1 signature ok .* duplicate' "$TMP/shop.out")" = 1
