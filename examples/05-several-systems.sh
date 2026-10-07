#!/usr/bin/env bash
# Use case 5: one event, several internal systems.
#
# Three internal receivers, each with the filter it needs and its own speed: billing (invoices) and audit (everything) answer at once, search
# (invoices and users) takes 1.5 seconds for each event and is given one request at a time. A slow receiver does not delay the others: the
# times are measured and printed.
#
# Needs: bash, curl, python3. No database (the endpoints are lines of endpoints.conf: types= and concurrency= are words after the secret).
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

BILLING_PORT=$(free_port)
AUDIT_PORT=$(free_port)
SEARCH_PORT=$(free_port)
for name in billing audit search; do new_secret >"$TMP/$name.secret"; done
say "three endpoints: billing wants invoices, audit wants everything, search wants invoices and users and takes one request at a time"
run "cat > \$DATA/endpoints.conf <<EOF
0 127.0.0.1 \$BILLING_PORT \$(cat billing.secret) types=invoice.*
1 127.0.0.1 \$AUDIT_PORT \$(cat audit.secret)
2 127.0.0.1 \$SEARCH_PORT \$(cat search.secret) types=invoice.*,user.* concurrency=1
EOF"
# shellcheck disable=SC2119  # no flags
start_service
start_receiver billing "$BILLING_PORT"
start_receiver audit "$AUDIT_PORT"
start_receiver search "$SEARCH_PORT" --delay 1.5

say "four events, one after the other"
t0=$(now_ms)
run "for t in invoice.paid user.created invoice.refunded invoice.paid; do curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d \"{\\\"type\\\":\\\"\$t\\\"}\"; done"
wait_until 30 delivered_at_least billing 3 || { echo "billing did not receive 3"; exit 1; }
t_billing=$(($(now_ms) - t0))
wait_until 30 delivered_at_least audit 4 || { echo "audit did not receive 4"; exit 1; }
t_audit=$(($(now_ms) - t0))
wait_until 30 delivered_at_least search 4 || { echo "search did not receive 4"; exit 1; }
t_search=$(($(now_ms) - t0))
say "what each receiver got, and when it had it all (from the first event posted)"
printf 'billing: %s deliveries (invoice.*), done after %s ms\n' "$(delivered billing)" "$t_billing"
printf 'audit:   %s deliveries (everything), done after %s ms\n' "$(delivered audit)" "$t_audit"
printf 'search:  %s deliveries (invoice.*, user.*; 1.5 s each, one at a time), done after %s ms\n' "$(delivered search)" "$t_search"
check "billing got the 3 invoice events, audit all 4, search all 4" test "$(delivered billing)" = 3 -a "$(delivered audit)" = 4 -a "$(delivered search)" = 4
check "every delivery verified" test "$(cat "$TMP/billing.out" "$TMP/audit.out" "$TMP/search.out" | grep -c 'signature ok')" = 11
check "the two fast receivers were done in less than half the time the slow one needed" test $((t_billing * 2)) -lt "$t_search" -a $((t_audit * 2)) -lt "$t_search"
check "the slow one took at least 4 x 1.5 s: it was held to one request at a time" test "$t_search" -ge 6000
