#!/bin/bash
# Restore a backup made by scripts/backup.sh into a data directory, and (optionally) its database tables.
#
#   scripts/restore.sh --backup /var/backups/hooks/hooks-backup-20261004T120000Z --dir /var/lib/hooks
#                      [--force]
#                      [--pg-database hooks [--pg-host H] [--pg-port P] [--pg-user U]]
#
# It checks everything before it changes anything: the checksums, the manifest, both logs record by record, and that
# delivery.seg does not refer to an event that the events log lacks (a pair like that makes a service acknowledge events it
# never delivers; scripts/logcheck.py explains). It refuses, and says why, rather than restore a pair it does not trust.
#
#   * The service must not be running (a log of --dir open in any process is a refusal; it looks in /proc: run it as root or
#     as the service's user).
#   * A --dir that already holds a log (events.seg, events-N.seg, delivery.seg) is a refusal, unless --force: then those files are MOVED to
#     <dir>/pre-restore-<UTC time>/, never deleted.
#   * With --pg-database the tables are restored with pg_restore --clean --if-exists in one transaction: the database must exist
#     (createdb hooks; the tables need not: the dump creates them, the sequence endpoint_ids keeps its value so that an endpoint
#     id is never given twice). A backup that holds a dump and a restore that names no database restores the files and says so.
#     A password: ~/.pgpass or PGPASSWORD, never an argument.
#
# The restored service starts as after a crash: it recovers both logs, and repeats the deliveries delivery.seg does not record
# (at least once, never zero). Check with GET /stats and GET /events/<last id> after the start.
#
# Exit status: 0 done; 2 usage; 3 refused (the service is running, --dir is not empty); 4 the backup does not verify; 5 pg_restore failed.
set -euo pipefail
umask 077

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
logcheck="$here/logcheck.py"

die() { local code=$1; shift; echo "restore: $*" >&2; exit "$code"; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2; exit 2; }

backup="" dir="" force=0
pg_host="" pg_port="" pg_user="" pg_db=""
while [ $# -gt 0 ]; do
  case $1 in
    --backup) backup=${2:?--backup needs a value}; shift 2 ;;
    --dir) dir=${2:?--dir needs a value}; shift 2 ;;
    --force) force=1; shift ;;
    --pg-host) pg_host=${2:?--pg-host needs a value}; shift 2 ;;
    --pg-port) pg_port=${2:?--pg-port needs a value}; shift 2 ;;
    --pg-user) pg_user=${2:?--pg-user needs a value}; shift 2 ;;
    --pg-database) pg_db=${2:?--pg-database needs a value}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "restore: unknown argument: $1" >&2; usage ;;
  esac
done
if [ -z "$backup" ] || [ -z "$dir" ]; then usage; fi
[ -d "$backup" ] || die 2 "$backup is not a directory"
backup=$(cd "$backup" && pwd)
command -v python3 >/dev/null || die 2 "python3 is needed (scripts/logcheck.py)"
command -v sha256sum >/dev/null || die 2 "sha256sum is needed"
if [ -n "$pg_db" ]; then command -v pg_restore >/dev/null || die 2 "pg_restore is needed for --pg-database"; fi

# 1. The backup itself.
if [ ! -f "$backup/MANIFEST" ] || [ ! -f "$backup/SHA256SUMS" ]; then die 4 "$backup has no MANIFEST or SHA256SUMS: not a backup of this script"; fi
# /1 is a backup of one events.seg; /2 lists the segments of the events log (retention: docs/retention.md) in `events_files=`.
if grep -qx 'format=lexsys-hooks-backup/2' "$backup/MANIFEST"; then
  events_files=$(sed -n 's/^events_files=//p' "$backup/MANIFEST")
  [ -n "$events_files" ] || die 4 "the MANIFEST lists no events files"
elif grep -qx 'format=lexsys-hooks-backup/1' "$backup/MANIFEST"; then
  events_files=events.seg
else
  die 4 "unknown backup format ($(grep '^format=' "$backup/MANIFEST" || echo none)): this script reads lexsys-hooks-backup/1 and /2"
fi
(cd "$backup" && sha256sum --quiet -c SHA256SUMS) || die 4 "a checksum does not match: the backup is damaged"
for f in $events_files delivery.seg; do
  case $f in *[!A-Za-z0-9._-]*|"") die 4 "a file name in the MANIFEST is not one this script makes: $f" ;; esac
  grep -q " $f\$" "$backup/SHA256SUMS" || die 4 "$f is not listed in SHA256SUMS"
done
kv=$(python3 "$logcheck" check "$backup" --kv) || die 4 "the logs of the backup are not a consistent pair (see above); nothing was restored"
if echo "$kv" | grep -E '^(events|delivery)_torn_bytes=' | grep -qv '=0$'; then
  die 4 "a log in the backup ends in a partial record: backup.sh trims those, so this backup was altered"
fi

# 2. The destination.
mkdir -p "$dir"
dir=$(cd "$dir" && pwd)
holders=$(
  for fd in /proc/[0-9]*/fd/*; do
    target=$(readlink "$fd" 2>/dev/null) || continue
    case $target in
      "$dir"/events.seg|"$dir"/events-[0-9]*.seg|"$dir/delivery.seg") fd=${fd#/proc/}; echo "${fd%%/*}" ;;
    esac
  done | sort -u
)
[ -z "$holders" ] || die 3 "the service has $dir open (pid $(echo "$holders" | tr '\n' ' ')): stop it first"
aside=""
have_log=0
for f in "$dir"/events.seg "$dir"/events-[0-9]*.seg "$dir"/delivery.seg; do
  if [ -e "$f" ]; then have_log=1; fi
done
if [ "$have_log" = 1 ]; then
  [ "$force" = 1 ] || die 3 "$dir already has a log: restore into an empty directory, or --force to move the old files to $dir/pre-restore-<time>/"
  aside="$dir/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -m 0700 "$aside"
  for path in "$dir"/events.seg "$dir"/events-[0-9]*.seg "$dir"/events.first "$dir"/delivery.seg "$dir"/endpoints.conf; do
    if [ -e "$path" ]; then mv "$path" "$aside/$(basename "$path")"; fi
  done
  echo "restore: the files that were in $dir are in $aside" >&2
fi

# 3. The files: each to a temporary name, flushed, then renamed, so that a failure part-way leaves no half-written log.
# (the outcomes first, then the segments, then the manifest that names the first: the order of the backup)
for f in delivery.seg $events_files events.first endpoints.conf; do
  if [ -f "$backup/$f" ]; then
    cp "$backup/$f" "$dir/.$f.restoring"
    sync "$dir/.$f.restoring" 2>/dev/null || sync
    mv "$dir/.$f.restoring" "$dir/$f"
  fi
done
# An empty delivery.seg is what a service that never delivered has; the service creates the file itself.
sync "$dir" 2>/dev/null || sync
python3 "$logcheck" check "$dir" >/dev/null || die 4 "the restored files do not verify: $dir is not trustworthy (the old files, if any, are in ${aside:-nowhere: it was empty})"

# 4. The tables.
if [ -f "$backup/hooks.pgdump" ]; then
  if [ -z "$pg_db" ]; then
    echo "restore: the backup holds hooks.pgdump but no --pg-database was given: the tables were NOT restored" >&2
  else
    args=(--clean --if-exists --no-owner --no-privileges --single-transaction --exit-on-error -d "$pg_db")
    [ -z "$pg_host" ] || args+=(-h "$pg_host")
    [ -z "$pg_port" ] || args+=(-p "$pg_port")
    [ -z "$pg_user" ] || args+=(-U "$pg_user")
    pg_restore "${args[@]}" "$backup/hooks.pgdump" || die 5 "pg_restore failed (nothing was changed in the database: it is one transaction); the files are restored"
  fi
fi

echo "restore: $dir restored from $backup" >&2
sed -n 's/^\(events_last_id\|delivery_records\)=/restore:   \1 = /p' "$backup/MANIFEST" >&2
echo "restore: start the service; it will recover both logs as after a crash" >&2
