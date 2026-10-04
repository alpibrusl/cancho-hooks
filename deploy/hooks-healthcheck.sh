#!/bin/bash
# The container's health check: `GET /healthz`, which only says the process is up and its loop turns (it reads nothing and writes
# nothing). It is not a readiness check: whether the logs are healthy, the database is reachable and receivers are answering is what
# /readyz and /metrics will say (docs/production.md 0.4, planned); until then a failing attempt is visible in /stats only.
#
# The port is `port` in the settings file (HOOKS_CONFIG, default /etc/hooks/hooks.conf); HOOKS_HEALTH_PORT overrides it, for a
# container started with `--port` on its command line. These two variables are read by this script only: the service reads none.
set -u
conf=${HOOKS_CONFIG:-/etc/hooks/hooks.conf}
port=${HOOKS_HEALTH_PORT:-}
if [ -z "$port" ] && [ -r "$conf" ]; then
  port=$(sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' "$conf" | tail -n 1)
fi
port=${port:-8080}
exec 3<>"/dev/tcp/127.0.0.1/$port" || exit 1
printf 'GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
IFS= read -r -t 3 status <&3 || exit 1
case $status in
  "HTTP/1.1 200 "*) exit 0 ;;
  *) exit 1 ;;
esac
