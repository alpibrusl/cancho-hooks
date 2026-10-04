#!/bin/bash
# Back up a lexsys-hooks data directory: the two logs (events.seg, delivery.seg), endpoints.conf if there is one, and (with
# --pg-database) a pg_dump of the tables endpoints, attempts and the sequence endpoint_ids.
#
#   scripts/backup.sh --dir /var/lib/hooks --out /var/backups/hooks --mode stopped|online
#                     [--stop-cmd 'systemctl stop hooks' --start-cmd 'systemctl start hooks']      (stopped only)
#                     [--pg-database hooks [--pg-host H] [--pg-port P] [--pg-user U] [--skip-attempts]]
#
# The result is one directory, <out>/hooks-backup-<UTC time>/, written under a temporary name and renamed only after it
# verified: the files, SHA256SUMS, MANIFEST and (if a database was named) hooks.pgdump. It holds every endpoint secret in the
# clear (as the table and endpoints.conf do): the directory is created 0700, keep it where you keep secrets.
# A password for the database is not accepted on the command line: use ~/.pgpass or PGPASSWORD, as for any libpq tool.
#
# MODES. Both leave a backup that scripts/restore.sh checks again before it touches anything.
#
#   stopped   the service must not be running. The files are then exactly what a crash would leave (every acknowledgement was
#             sent after a flush), and the pair is consistent by construction. There is no graceful stop today (SIGTERM ends the
#             process at once; `docs/production.md` 0.4 plans one), so "stop" is SIGTERM and an attempt on the wire is repeated
#             after the restore: at least once. The script refuses if any process still has a log open (it looks in /proc: run
#             it as root or as the service's user, or it cannot see the process), and runs --stop-cmd first and --start-cmd last.
#   online    the service keeps running and no acknowledged event is held back. This is safe because (1) both logs are only
#             appended to while the service runs (recovery cuts a torn tail only at start, and nothing compacts or rewrites them:
#             if that ever changes, this mode must be withdrawn), so a copy is a valid prefix plus at most one torn record, and
#             (2) the copies are taken **delivery.seg first, events.seg second**, so events.seg is never older than what
#             delivery.seg says was delivered. The reverse order is a disaster, not an inconvenience: a restored service whose
#             events.seg is shorter than its delivery.seg acknowledges new events under ids it believes delivered and never
#             delivers them (measured; tests/backup_test.py and docs/runbook.md). The script copies in the safe order, trims the
#             copies to their valid prefix, and then checks the pair with scripts/logcheck.py: it fails rather than keep a pair that
#             refers to an event it does not hold. What a restore can repeat is the deliveries recorded after delivery.seg was
#             copied: at least once, never zero.
#
# Exit status: 0 done; 2 usage; 3 refused (the service is running in stopped mode, the output exists); 4 the copy did not verify;
# 5 pg_dump failed.
set -euo pipefail
umask 077

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
logcheck="$here/logcheck.py"

die() { local code=$1; shift; echo "backup: $*" >&2; exit "$code"; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2; exit 2; }

dir="" out="" mode="" stop_cmd="" start_cmd=""
pg_host="" pg_port="" pg_user="" pg_db="" skip_attempts=0
while [ $# -gt 0 ]; do
  case $1 in
    --dir) dir=${2:?--dir needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --mode) mode=${2:?--mode needs a value}; shift 2 ;;
    --stop-cmd) stop_cmd=${2:?--stop-cmd needs a value}; shift 2 ;;
    --start-cmd) start_cmd=${2:?--start-cmd needs a value}; shift 2 ;;
    --pg-host) pg_host=${2:?--pg-host needs a value}; shift 2 ;;
    --pg-port) pg_port=${2:?--pg-port needs a value}; shift 2 ;;
    --pg-user) pg_user=${2:?--pg-user needs a value}; shift 2 ;;
    --pg-database) pg_db=${2:?--pg-database needs a value}; shift 2 ;;
    --skip-attempts) skip_attempts=1; shift ;;
    -h|--help) usage ;;
    *) echo "backup: unknown argument: $1" >&2; usage ;;
  esac
done
if [ -z "$dir" ] || [ -z "$out" ]; then usage; fi
case $mode in stopped|online) ;; *) echo "backup: --mode is stopped or online" >&2; usage ;; esac
if [ "$mode" = online ] && { [ -n "$stop_cmd" ] || [ -n "$start_cmd" ]; }; then
  die 2 "--stop-cmd and --start-cmd belong to --mode stopped"
fi
[ -d "$dir" ] || die 2 "$dir is not a directory"
dir=$(cd "$dir" && pwd)
[ -f "$dir/events.seg" ] || die 2 "$dir/events.seg does not exist: is that the --dir the service runs with?"
command -v python3 >/dev/null || die 2 "python3 is needed (scripts/logcheck.py)"
command -v sha256sum >/dev/null || die 2 "sha256sum is needed"
if [ -n "$pg_db" ]; then command -v pg_dump >/dev/null || die 2 "pg_dump is needed for --pg-database"; fi

# Is a log of $dir open in some process? Prints its pid. /proc only shows other users' processes to root.
holders() {
  local fd target
  for fd in /proc/[0-9]*/fd/*; do
    target=$(readlink "$fd" 2>/dev/null) || continue
    case $target in
      "$dir/events.seg"|"$dir/delivery.seg") fd=${fd#/proc/}; echo "${fd%%/*}" ;;
    esac
  done | sort -u
}

mkdir -p "$out"
out=$(cd "$out" && pwd)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name=hooks-backup-$stamp
n=1
while [ -e "$out/$name" ]; do n=$((n + 1)); name=hooks-backup-$stamp-$n; done   # two backups in one second
final="$out/$name"
work="$out/.$name.partial"
rm -rf "$work"
mkdir -m 0700 "$work"

started=0
cleanup() {
  local rc=$?
  if [ "$started" = 1 ] && [ -n "$start_cmd" ]; then
    echo "backup: running --start-cmd" >&2
    bash -c "$start_cmd" || { echo "backup: --start-cmd FAILED: the service may be down" >&2; [ "$rc" -ne 0 ] || rc=1; }
  fi
  if [ -d "$work" ]; then rm -rf "$work"; fi
  exit "$rc"
}
trap cleanup EXIT

if [ "$mode" = stopped ]; then
  if [ -n "$stop_cmd" ]; then
    started=1   # from here the service is down (or the stop failed part-way): --start-cmd must run either way
    echo "backup: running --stop-cmd" >&2
    bash -c "$stop_cmd" || die 3 "--stop-cmd failed"
  fi
  pids=$(holders)
  [ -z "$pids" ] || die 3 "the service still has $dir open (pid $(echo "$pids" | tr '\n' ' ')): stop it first, or use --mode online"
fi

# The database first, then the logs. A row the dump has and the logs lack makes the service start that endpoint at the
# slowest cursor (a repeat, never a loss); the other way round is also a repeat. Either order is safe; this one makes the logs
# the later (and so the more complete) of the two stores.
if [ -n "$pg_db" ]; then
  args=(-Fc --no-owner --no-privileges -t endpoints -t endpoint_ids)
  [ "$skip_attempts" = 1 ] || args+=(-t attempts)
  [ -z "$pg_host" ] || args+=(-h "$pg_host")
  [ -z "$pg_port" ] || args+=(-p "$pg_port")
  [ -z "$pg_user" ] || args+=(-U "$pg_user")
  pg_dump "${args[@]}" -f "$work/hooks.pgdump" "$pg_db" || die 5 "pg_dump failed"
fi

# The logs: delivery.seg FIRST. See MODES above.
if [ -f "$dir/delivery.seg" ]; then cp "$dir/delivery.seg" "$work/delivery.seg"; else : > "$work/delivery.seg"; fi   # a service that never delivered has none
cp "$dir/events.seg" "$work/events.seg"
if [ -f "$dir/endpoints.conf" ]; then cp "$dir/endpoints.conf" "$work/endpoints.conf"; fi

# A copy taken while the service ran may end in a torn record: cut it, as the service would at start.
cut_of() { sed -n 's/.*"cut": \([0-9]*\).*/\1/p'; }
cut_delivery=$(python3 "$logcheck" trim "$work/delivery.seg" | cut_of) || die 4 "delivery.seg: the copy is damaged"
cut_events=$(python3 "$logcheck" trim "$work/events.seg" | cut_of) || die 4 "events.seg: the copy is damaged"
kv=$(python3 "$logcheck" check "$work" --kv) || die 4 "the copied pair is not consistent (see above); nothing was kept"

{
  echo "format=lexsys-hooks-backup/1"
  echo "mode=$mode"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "source_dir=$dir"
  echo "pg_database=${pg_db:-}"
  echo "torn_bytes_cut_delivery=$cut_delivery"
  echo "torn_bytes_cut_events=$cut_events"
  echo "$kv"
} > "$work/MANIFEST"
listed=(events.seg delivery.seg MANIFEST)
[ ! -f "$work/endpoints.conf" ] || listed+=(endpoints.conf)
[ ! -f "$work/hooks.pgdump" ] || listed+=(hooks.pgdump)
(cd "$work" && sha256sum -- "${listed[@]}" > SHA256SUMS)
sync "$work"/* 2>/dev/null || sync
mv "$work" "$final"
sync "$out" 2>/dev/null || sync
trap - EXIT
if [ "$started" = 1 ] && [ -n "$start_cmd" ]; then
  echo "backup: running --start-cmd" >&2
  bash -c "$start_cmd" || die 3 "--start-cmd failed: the backup is complete at $final but the service may be down"
fi
echo "$final"
echo "backup: $mode backup of events up to $(echo "$kv" | sed -n 's/^events_last_id=//p'), $(echo "$kv" | sed -n 's/^delivery_records=//p') outcome records" >&2
