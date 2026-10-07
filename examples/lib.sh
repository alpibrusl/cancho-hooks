# shellcheck shell=bash
# shellcheck disable=SC2016,SC2119  # the commands that are printed show the variables' names, not their values; start_service takes flags, or none
# Helpers shared by the example scripts (sourced, never run). Needs bash, curl and python3; psql and a PostgreSQL where a script says so.
#
# Every script prints each command as `$ command` and then what it printed, and a line `ok: ...` for each expectation that held. It exits 0 only if
# every expectation held. It starts the service in a temporary directory on a free port and, on exit, ends only the processes it started.

set -euo pipefail

EXAMPLES_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$EXAMPLES_DIR/.." && pwd)
HOOKS=${HOOKS:-$ROOT/build/hooks}
HOOKS_MCP=${HOOKS_MCP:-$(dirname "$HOOKS")/hooks-mcp}

if [ ! -x "$HOOKS" ]; then
  echo "no service binary at $HOOKS: build it (scripts/build.sh) or set HOOKS=path/to/hooks" >&2
  exit 2
fi
command -v curl >/dev/null || { echo "needs curl" >&2; exit 2; }
command -v python3 >/dev/null || { echo "needs python3" >&2; exit 2; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/hooks-example.XXXXXX")
PIDS=()
DATA=$TMP/data
mkdir -p "$DATA"
cd "$TMP"   # the commands that are printed say hooks.conf and acme.secret, and these are files of this directory
ADMIN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
USE_PG=0
OUT=""

cleanup() {
  local rc=$?
  local p
  for p in ${PIDS[@]+"${PIDS[@]}"}; do
    kill "$p" 2>/dev/null || true
  done
  for p in ${PIDS[@]+"${PIDS[@]}"}; do
    wait "$p" 2>/dev/null || true
  done
  if [ "$rc" -ne 0 ]; then
    echo "FAILED (exit $rc)"
    if [ -f "$TMP/service.log" ]; then
      echo "--- the service's log (last lines)"
      tail -n 15 "$TMP/service.log" || true
    fi
  fi
  cd /
  rm -rf "$TMP"
  exit "$rc"
}
trap cleanup EXIT

# ---- printing -----------------------------------------------------------------------------------------------------------------------------

say() { printf '# %s\n' "$*"; }

# run 'command': print it as `$ command`, run it (variables in it are expanded when it runs), print what it printed. The output is in $OUT.
run() {
  printf '$ %s\n' "$1"
  OUT=$(eval "$1" 2>&1) || true
  if [ -n "$OUT" ]; then printf '%s\n' "$OUT"; fi
}

# check 'what is expected' command...: print `ok: ...` when the command succeeds, and end the script (status 1) when it does not.
check() {
  local what=$1
  shift
  if "$@"; then
    printf 'ok: %s\n' "$what"
  else
    printf 'EXPECTATION FAILED: %s\n' "$what"
    exit 1
  fi
}

# ---- small tools --------------------------------------------------------------------------------------------------------------------------

free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }
new_secret() { python3 -c 'import base64,os;print("whsec_"+base64.b64encode(os.urandom(24)).decode())'; }

# jget 'text' path: the field at a dotted path of the JSON in the text (a trailing ` [status]` from curl -w is ignored)
jget() {
  python3 -c '
import json, sys
t = sys.argv[1].strip()
if t.endswith("]") and " [" in t:
    t = t.rsplit(" [", 1)[0]
d = json.loads(t)
for k in sys.argv[2].split("."):
    d = d[int(k)] if isinstance(d, list) else d[k]
print(json.dumps(d) if isinstance(d, (dict, list, bool)) or d is None else d)' "$1" "$2"
}

# wait_until SECONDS command...: poll the command every 50 ms until it succeeds; fails after SECONDS
wait_until() {
  local limit=$1
  shift
  local end=$(($(now_ms) + limit * 1000))
  while ! "$@" 2>/dev/null; do
    if [ "$(now_ms)" -gt "$end" ]; then return 1; fi
    sleep 0.05
  done
}

# lines_in FILE PATTERN: how many lines of the file match (0 when there is no file)
lines_in() { if [ -f "$1" ]; then grep -c -- "$2" "$1" || true; else echo 0; fi; }

# ---- the service --------------------------------------------------------------------------------------------------------------------------

# use_postgres: the example needs the endpoints API (a database). HOOKS_PG=host:port:user:database and HOOKS_PG_PASSWORD, as the tests read them. The
# schema is applied (sql/schema.sql, safe to repeat) and the tables endpoints, attempts and schedules are EMPTIED: give it a database made for this.
use_postgres() {
  if [ -z "${HOOKS_PG:-}" ]; then
    echo "this example needs PostgreSQL: set HOOKS_PG=host:port:user:database (and HOOKS_PG_PASSWORD), a database it may empty" >&2
    exit 2
  fi
  command -v psql >/dev/null || { echo "this example needs psql" >&2; exit 2; }
  IFS=: read -r PGH PGP PGU PGD <<<"$HOOKS_PG"
  export PGPASSWORD=${HOOKS_PG_PASSWORD:-}
  PGOPTIONS='-c client_min_messages=warning' psql -h "$PGH" -p "$PGP" -U "$PGU" -d "$PGD" -q -v ON_ERROR_STOP=1 -f "$ROOT/sql/schema.sql" >/dev/null
  PGOPTIONS='-c client_min_messages=warning' psql -h "$PGH" -p "$PGP" -U "$PGU" -d "$PGD" -q -v ON_ERROR_STOP=1 -c 'truncate endpoints, attempts, schedules restart identity; alter sequence endpoint_ids restart with 0' >/dev/null
  USE_PG=1
}

# configure_service: pick the port and write the settings file (the admin token and the database's password belong in a file, not on the command line)
configure_service() {
  PORT=$(free_port)
  URL=http://127.0.0.1:$PORT
  {
    echo "admin-token = $ADMIN"
    echo "allow-private-hosts = 1"
    if [ "$USE_PG" = 1 ]; then
      echo "pg-host = $PGH"
      echo "pg-port = $PGP"
      echo "pg-user = $PGU"
      echo "pg-database = $PGD"
      echo "pg-password = ${HOOKS_PG_PASSWORD:-}"
    fi
  } >"$TMP/hooks.conf"
  chmod 600 "$TMP/hooks.conf"
  say "hooks.conf: admin-token = <secret>, allow-private-hosts = 1 (the receivers are on this machine)$(if [ "$USE_PG" = 1 ]; then echo ', pg-* = a PostgreSQL'; fi)"
}

# start_service [flags...]: start the service on $PORT with the data directory $DATA, wait until it answers /readyz. The pid is in $SVC_PID.
start_service() {
  if [ -z "${PORT:-}" ]; then configure_service; fi
  printf '$ hooks --config hooks.conf --port $PORT --dir $DATA%s &\n' "$(if [ $# -gt 0 ]; then printf ' %s' "$*"; fi)"
  "$HOOKS" --config "$TMP/hooks.conf" --port "$PORT" --dir "$DATA" "$@" >>"$TMP/service.log" 2>&1 &
  SVC_PID=$!
  PIDS+=("$SVC_PID")
  wait_until 30 curl -sf "$URL/readyz" -o /dev/null || { echo "the service did not become ready"; exit 1; }
}

# stop_service: SIGTERM, wait for the drain
stop_service() {
  kill "$SVC_PID"
  wait "$SVC_PID" 2>/dev/null || true
}

# ---- a receiver ---------------------------------------------------------------------------------------------------------------------------

# start_receiver NAME PORT [receiver.py flags...]: start examples/receiver.py; what it prints goes to $TMP/NAME.out. Its secrets are the lines of $TMP/NAME.secret
start_receiver() {
  local name=$1 port=$2
  shift 2
  printf '$ python3 examples/receiver.py --name %s --port $%s_PORT --secret-file %s.secret%s &\n' "$name" "$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')" "$name" "$(if [ $# -gt 0 ]; then printf ' %s' "$*"; fi)"
  python3 "$EXAMPLES_DIR/receiver.py" --name "$name" --port "$port" --secret-file "$TMP/$name.secret" "$@" >"$TMP/$name.out" 2>&1 &
  eval "PID_$name=$!"
  PIDS+=("$!")
  wait_until 10 grep -q listening "$TMP/$name.out" || { echo "receiver $name did not start"; exit 1; }
}

stop_receiver() {
  eval "kill \$PID_$1"
  eval "wait \$PID_$1" 2>/dev/null || true
}

# delivered NAME: how many deliveries the receiver has printed
delivered() { lines_in "$TMP/$1.out" ' evt_'; }

delivered_at_least() { [ "$(delivered "$1")" -ge "$2" ]; }

# show_received NAME: print what the receiver printed since the last time (the lines about deliveries)
show_received() {
  local name=$1 shown=0
  if [ -f "$TMP/$name.shown" ]; then shown=$(cat "$TMP/$name.shown"); fi
  grep ' evt_' "$TMP/$name.out" | tail -n +$((shown + 1)) || true
  delivered "$name" >"$TMP/$name.shown"
}

# received NAME [count]: wait until the receiver has printed that many deliveries in all (1 by default), then show the new lines
received() {
  local name=$1 want=${2:-1}
  wait_until 20 delivered_at_least "$name" "$want" || {
    echo "$name did not receive $want"
    cat "$TMP/$name.out"
    curl -s "$URL/stats" -H "Authorization: Bearer $ADMIN" || true
    exit 1
  }
  show_received "$name"
}
