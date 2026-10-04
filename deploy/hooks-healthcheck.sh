#!/bin/bash
# The container's health check: `GET /readyz` (docs/design.md section 34.1): 200 when the logs are open and writable, the database (if one is named)
# has a live connection and the service has not been asked to stop. It is the right question for a container because every way it can be 503 is one that
# a restart fixes (a broken log is only cleared by a restart; a lost database connection is never reopened) or is a stop in progress. Whether the receivers
# are answering is not asked: a dead receiver is not the service's fault and restarting it does not help (/metrics says it).
# `GET /healthz` (the process is up and its loop turns, reading and writing nothing) is the other probe, for a supervisor that must not restart on a full disk.
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
printf 'GET /readyz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
IFS= read -r -t 3 status <&3 || exit 1
case $status in
  "HTTP/1.1 200 "*) exit 0 ;;
  *) exit 1 ;;
esac
