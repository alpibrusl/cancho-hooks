#!/usr/bin/env bash
# Use case 3: a customer's endpoint is down.
#
# The service retries, and when the schedule runs out the event is a dead letter of that endpoint: it is kept, listed, and sent again in bulk
# when the customer is back.
#
# Needs: bash, curl, python3. No database. The retry schedule is shortened for the demo (--schedule 200,300,400: four attempts in about a second;
# the default is nine retries over a day: 5 s, 5 min, 30 min, 2 h, ...). The service is $HOOKS (default build/hooks). Exits 0 only if every
# expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

CUSTOMER_PORT=$(free_port)
SECRET=$(new_secret)
echo "$SECRET" >"$TMP/customer.secret"
say "one endpoint, and nothing is listening on its port"
run "echo \"0 127.0.0.1 \$CUSTOMER_PORT \$SECRET\" > \$DATA/endpoints.conf"
echo "0 127.0.0.1 $CUSTOMER_PORT $SECRET" >"$DATA/endpoints.conf"
start_service --schedule 200,300,400 --retry-jitter 0

say "three events are accepted (202) all the same"
run "for i in 1 2 3; do curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d \"{\\\"type\\\":\\\"order.shipped\\\",\\\"order\\\":\$i}\"; done"
check "three accepted" test "$(echo "$OUT" | grep -c '\[202\]')" = 3
say "each is tried, retried and, when the schedule is spent, a dead letter of this endpoint"
dead_count() { [ "$(curl -s "$URL/stats" | grep -o '"dead":[0-9]*')" = '"dead":3' ]; }
wait_until 20 dead_count || { echo "the events did not die"; exit 1; }
run "curl -s \"\$URL/endpoints/0/dead?limit=2\""
# (on macOS the reason is connect_error: the service names ECONNREFUSED by its Linux number, src/attempt.cho)
check "three dead letters held, every one tried four times and refused by the connection" python3 -c '
import json, sys
d = json.loads(sys.argv[1])
assert d["held"] == 3 and all(x["attempts"] == 4 and x["reason"] in ("connect_refused", "connect_error") for x in d["dead"]), d' "$OUT"

say "the customer is back"
start_receiver customer "$CUSTOMER_PORT"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/endpoints/0/replay-dead -H \"Authorization: Bearer \$ADMIN\""
received customer 3 | sort | sed 's/^/  /'
check "all three arrived, signed, once each" test "$(grep -c 'signature ok' "$TMP/customer.out")" = 3
run "curl -s \"\$URL/endpoints/0/dead\""
check "no dead letters are left" python3 -c 'import json,sys; assert json.loads(sys.argv[1])["held"] == 0' "$OUT"
