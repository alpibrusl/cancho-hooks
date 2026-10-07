#!/usr/bin/env bash
# Use case 4: a report every Monday, with no scheduler.
#
# A schedule is a cron expression; the service makes an ordinary event on it, and the event is signed, delivered, retried and can be replayed like
# any other. Part one shows the real expression, "0 6 * * 1" (06:00 UTC, Mondays); part two restarts the service with --cron-seconds 1 (a mode for
# demos and tests: the expression gets a leading seconds field) so that a fire can be seen in seconds instead of on Monday.
#
# Needs: bash, curl, python3, psql, and a PostgreSQL (the schedules are rows in it):
#   HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] examples/04-cron.sh
# It applies sql/schema.sql and EMPTIES the tables endpoints, attempts and schedules of that database: give it one made for this.
# The service is $HOOKS (default build/hooks). Exits 0 only if every expectation held.
# shellcheck source=examples/lib.sh
. "$(dirname "$0")/lib.sh"

use_postgres
REPORTS_PORT=$(free_port)
start_service

say "the endpoint that wants the report"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/endpoints -H \"Authorization: Bearer \$ADMIN\" -d '{\"host\":\"127.0.0.1\",\"port\":'\$REPORTS_PORT',\"types\":[\"report.due\"]}'"
jget "$OUT" secret >"$TMP/reports.secret"

say "part one: the real schedule, every Monday at 06:00 UTC"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/schedules -H \"Authorization: Bearer \$ADMIN\" -d '{\"expr\":\"0 6 * * 1\",\"type\":\"report.due\",\"body\":{\"report\":\"weekly\"}}'"
SCHEDULE=$(jget "$OUT" id)
check "the next fire is a Monday, 06:00:00 UTC" python3 -c '
import datetime, json, sys
t = sys.argv[1].rsplit(" [", 1)[0]
d = datetime.datetime.fromtimestamp(json.loads(t)["next_fire"], datetime.timezone.utc)
assert (d.weekday(), d.hour, d.minute, d.second) == (0, 6, 0, 0), d' "$OUT"
run "curl -s -w ' [%{http_code}]\n' -X DELETE \$URL/schedules/\$SCHEDULE -H \"Authorization: Bearer \$ADMIN\""
stop_service

say "part two: the same, with seconds, so that it can be watched (a fire every 2 seconds)"
start_service --cron-seconds 1
start_receiver reports "$REPORTS_PORT"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/schedules -H \"Authorization: Bearer \$ADMIN\" -d '{\"expr\":\"*/2 * * * * *\",\"type\":\"report.due\",\"body\":{\"report\":\"weekly\"}}'"
SCHEDULE=$(jget "$OUT" id)
say "nothing else sends events: these are the fires"
received reports 2 | sed 's/^/  /'
run "curl -s -w ' [%{http_code}]\n' -X DELETE \$URL/schedules/\$SCHEDULE -H \"Authorization: Bearer \$ADMIN\""
check "each fire carries the schedule's id, the second it was due and its body" grep -Eq "\"type\":\"report.due\",\"schedule\":$SCHEDULE,\"scheduled_at\":[0-9]+,\"body\":\{\"report\":\"weekly\"\}\}" "$TMP/reports.out"
sleep 2.5
run "curl -s \$URL/stats | grep -o '\"cron_fired\":[0-9]*'"
fired=${OUT##*:}
wait_until 10 delivered_at_least reports "$fired"
check "the fires the service counted are the verified deliveries the receiver has: none lost, none doubled" test "$(grep -c 'signature ok' "$TMP/reports.out")" = "$fired" -a "$(delivered reports)" = "$fired"
say "a fire is an event like any other: it can be sent again, with the same webhook-id"
run "curl -s -w ' [%{http_code}]\n' -X POST \$URL/events/1/replay -H \"Authorization: Bearer \$ADMIN\""
received reports $((fired + 1)) | tail -n 1 | sed 's/^/  /'
check "the replay of the first fire was recognised as a repeat" grep -q 'evt_1 signature ok .* duplicate' "$TMP/reports.out"
