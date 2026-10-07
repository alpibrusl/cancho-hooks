#!/usr/bin/env bash
# Use case 1: a SaaS sends its customers webhooks.
#
# Two customers, each with an endpoint of its own: its own secret, its own filter, a header it asked for, its own pace. One event reaches only
# who subscribes, every delivery is signed and the receiver verifies it, and a secret is rotated with both signatures valid for a while, so the
# receiver switches without a single failed delivery.
#
# Needs: bash, curl, python3, psql, and a PostgreSQL (the endpoints API keeps the endpoints in it):
#   HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] examples/01-saas-customers.sh
# It applies sql/schema.sql and EMPTIES the tables endpoints, attempts and schedules of that database: give it one made for this.
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

use_postgres
ACME_PORT=$(free_port)
GLOBEX_PORT=$(free_port)
# shellcheck disable=SC2119  # no flags
start_service

say "two customers: acme wants invoices, at most 2 requests at once and 10 a second; globex wants everything"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/endpoints -H \"Authorization: Bearer \$ADMIN\" -d '{\"host\":\"127.0.0.1\",\"port\":'\$ACME_PORT',\"types\":[\"invoice.*\"],\"headers\":{\"X-Customer\":\"acme\"},\"concurrency\":2,\"rate\":10}'"
ACME_SECRET=$(jget "$OUT" secret)
# shellcheck disable=SC2034  # used by the commands that are printed and run
ACME_ID=$(jget "$OUT" id)
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/endpoints -H \"Authorization: Bearer \$ADMIN\" -d '{\"host\":\"127.0.0.1\",\"port\":'\$GLOBEX_PORT',\"headers\":{\"X-Customer\":\"globex\"}}'"
GLOBEX_SECRET=$(jget "$OUT" secret)
say "each secret is in the answer that made the endpoint and in no other; it goes to the customer, who keeps it"
echo "$ACME_SECRET" >"$TMP/acme.secret"
echo "$GLOBEX_SECRET" >"$TMP/globex.secret"
run "curl -s \$URL/endpoints -H \"Authorization: Bearer \$ADMIN\""
check "the list shows each endpoint's filter, header names and limits, and no secret" python3 -c '
import json, sys
a, g = json.loads(sys.argv[1])
assert a["types"] == ["invoice.*"] and a["headers"] == ["X-Customer"] and a["concurrency"] == 2 and a["rate"] == 10, a
assert g["types"] == [] and g["headers"] == ["X-Customer"], g
assert "whsec_" not in sys.argv[1]' "$OUT"

start_receiver acme "$ACME_PORT" --show-header X-Customer
start_receiver globex "$GLOBEX_PORT" --show-header X-Customer

say "an invoice goes to both; a new user goes to globex only"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"invoice.paid\",\"invoice\":\"INV-1001\",\"amount\":4900}'"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"user.created\",\"user\":\"u_77\"}'"
say "what the receivers print (each verified the signature with the secret it was given)"
received acme 1 | sed 's/^/  /'
received globex 2 | sed 's/^/  /'
check "acme got the invoice only, globex got both" test "$(delivered acme)" = 1 -a "$(delivered globex)" = 2
check "every delivery verified, and carried its customer's own header" test "$(cat "$TMP/acme.out" "$TMP/globex.out" | grep -c 'signature ok')" = 3 -a "$(grep -c 'X-Customer=acme' "$TMP/acme.out")" = 1
run "curl -s \$URL/stats -H \"Authorization: Bearer \$ADMIN\" | grep -oE '\"(delivered|failed|filtered)\":[0-9]+'"
check "the service counted one event it did not send to acme, and no failure" test "$(echo "$OUT" | tr -d '\n')" = '"delivered":3"failed":0"filtered":1'

say "acme rotates its secret: deliveries are signed with the new one and, for a day (keep_old), with the old one too"
run "curl -s -w ' [%{http_code}]\n' -X PATCH \$URL/endpoints/\$ACME_ID -H \"Authorization: Bearer \$ADMIN\" -d '{\"rotate\":true,\"keep_old\":true}'"
ACME_NEW=$(jget "$OUT" secret)
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"invoice.paid\",\"invoice\":\"INV-1002\",\"amount\":1500}'"
say "acme's receiver still knows only the old secret, and verifies it: the delivery carries two signatures"
received acme 2 | sed 's/^/  /'
say "acme switches its receiver to the new secret, at its own time"
run "echo \"\$ACME_NEW\" > acme.secret"
echo "$ACME_NEW" >"$TMP/acme.secret"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"invoice.paid\",\"invoice\":\"INV-1003\",\"amount\":700}'"
received acme 3 | sed 's/^/  /'
say "when acme is ready it ends the overlap, and only the new signature is sent"
run "curl -s -w ' [%{http_code}]\n' -X PATCH \$URL/endpoints/\$ACME_ID -H \"Authorization: Bearer \$ADMIN\" -d '{\"keep_old_ms\":0}'"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events -d '{\"type\":\"invoice.paid\",\"invoice\":\"INV-1004\",\"amount\":300}'"
received acme 4 | sed 's/^/  /'
received globex 5 >/dev/null
check "the three deliveries after the rotation began carried two signatures, two, then one, and every one verified" test "$(grep -c 'signature ok' "$TMP/acme.out")" = 4 -a "$(grep -c 'signature ok (2 signatures)' "$TMP/acme.out")" = 2
run "curl -s \$URL/stats -H \"Authorization: Bearer \$ADMIN\" | grep -oE '\"(delivered|failed)\":[0-9]+'"
check "no delivery failed, rotation included" test "$(echo "$OUT" | tr -d '\n')" = '"delivered":9"failed":0'
