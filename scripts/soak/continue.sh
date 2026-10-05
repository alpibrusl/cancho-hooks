#!/bin/bash
# The soak run of docs/soak.md, started or continued: the one command to put under a supervisor (a systemd unit with Restart=on-failure, a loop, a person who comes back after the
# container was restarted).
#
#   scripts/soak/continue.sh soak-out --binary build/hooks --hours 24 --seed 1
#
# If soak-out holds a run that was interrupted (a run.json and no verdict.json) it is resumed and the other arguments are ignored (the run's own are in run.json); if it holds a
# finished run it says so and exits 0; otherwise a new run is begun with the arguments. Set HOOKS_PG (and HOOKS_PG_PASSWORD) as for the first start: the database must be the one
# the run used, with its contents (the endpoints of the run are rows in it). The exit status is soak.py's: 0 passed, 1 failed, 2 setup, 3 inconclusive.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if [ $# -lt 1 ]; then
    echo "usage: $0 OUT_DIR [soak.py arguments for a new run]" >&2
    exit 2
fi
out=$1
shift
if [ -f "$out/verdict.json" ]; then
    echo "$out holds a finished run (verdict.json); report: python3 $here/report.py $out"
    exit 0
elif [ -f "$out/run.json" ]; then
    exec python3 "$here/soak.py" --resume "$out"
elif [ -d "$out" ] && [ -n "$(ls -A "$out")" ]; then
    exec python3 "$here/soak.py" --out "$out" --force "$@"
else
    exec python3 "$here/soak.py" --out "$out" "$@"
fi
