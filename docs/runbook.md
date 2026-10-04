# Running lexsys-hooks: the runbook

*For whoever has the pager. Everything here was read from the source or run; where it was not, it says so. Parts that depend on work that is not built are marked **(planned)** and say which item of [`production.md`](production.md) builds them: do not assume they exist. The README still says "Not for production", and `production.md` says what has to be true before that line comes off.*

## 0. What this is, in one paragraph

One process, one thread. It takes `POST /events`, appends the event to `events.seg` and answers `202` only after the `fsync` that covers it; it delivers each event, signed, to every endpoint, and records every outcome in `delivery.seg`. The two files are the truth; PostgreSQL (optional) holds the endpoints and a best-effort history of attempts. **Everything the service knows after a restart it learned by reading those two files.** That is why a crash is a normal event here, and why the backup is two files and a `pg_dump`.

| | verified here | not verified here |
|---|---|---|
| backup, restore, the consistency check | `tests/backup_test.py` (section 4): total loss and restore, and (in a 6,000-event run) 22 power-cut kills with 25 online backups | a backup of a log above a few hundred MB (speed: section 4.6) |
| the container | built (`docker build`, 1 min 40 s), run, health check healthy, `docker stop` in 0.08 s, volume kept an event across a restart, non-root uid 10001 | CI does not build it; the base image is a tag, not a digest; no multi-arch |
| the systemd unit | `systemd-analyze verify`, `systemd-analyze security --offline` (1.3 OK), the syscalls the service makes are inside `@system-service` | **never run under a systemd**: the first start on a real host is the test |
| the release script | run, tarball and checksums checked (section 9) | signing, a stable URL, a real SBOM format |

## 1. Start, stop, health

### Start

```sh
# systemd (deploy/hooks.service; the header of the file lists the install steps)
systemctl enable --now hooks && journalctl -u hooks -f

# container
docker run -d --name hooks -p 8080:8080 -v hooks-data:/var/lib/hooks lexsys-hooks

# by hand
build/hooks --config /etc/hooks/hooks.conf
```

The service prints **`listening`** on stderr when it is ready and nothing else while it runs (section 3.1). With a database named, the start can take as long as the database takes to answer (it reads the endpoints before it listens, and the blocking client has no timeout), and it reads both logs whole (section 7: start time grows with the logs).

### Stop

`SIGTERM` (or `systemctl stop hooks`, `docker stop hooks`). **There is no graceful stop today**: the process ends at once. That is safe, by construction and by test: an acknowledgement is only sent after the flush that covers the event, an outcome is written after the receiver answered, so what a stop can cost is a *repeat*: an attempt that was on the wire is made again at the next start (at-least-once). `kill -9` is the same, and `tests/chaos.py` and `tests/delivery.py` exist to prove that. A drain that finishes what is on the wire and exits 0 is **(planned: 0.4)**.

In a container the service is PID 1 unless something else is, and **a PID 1 with no `SIGTERM` handler ignores `SIGTERM`** (measured with `unshare --pid`: it survives). The image therefore starts it under `tini`: `docker stop` takes 0.08 s, and 10.1 s and exit 137 without it. If you run the binary some other way in a container, use `docker run --init`.

### Health

| | |
|---|---|
| `GET /healthz` | `200 {"ok":true}` while the process is up and its loop turns. It reads nothing and writes nothing, and it is what the container's `HEALTHCHECK` calls (`deploy/hooks-healthcheck.sh`). **It does not look at the logs or the database**: with the disk full (section 6) it still answers `200` while every `POST /events` is a `503`. |
| `GET /readyz` | **(planned: 0.4)** logs open, database reachable if named |
| `GET /metrics` | **(planned: 0.4)** Prometheus text: ingest, flush, attempts by outcome, in flight, per-endpoint lag, retries waiting, dead letters, history queue, log sizes |
| `GET /stats` | what exists today (section 3.2) |

Until `/readyz` and `/metrics` exist, watch `/stats` and the logs' sizes (`ls -l /var/lib/hooks`), and alert on a `POST /events` canary that returns anything but `202`.

### Exit statuses

The service never ends by itself once it listens (bar status 4). A start that ends has a status (from the source):

| status | meaning | `systemd` restarts it? |
|---|---|---|
| 0 | `--import-endpoints 1` finished | n/a |
| 2 | a setting was refused (the message names the argument or the line of the file), or `--port`/`--dir` are missing | no |
| 3 | the value before the logs are opened; no path in the source leaves it, so you should not see it | yes |
| 4 | the event loop could not create its poller (printed `listening` first) | yes |
| 10 | `events.seg` could not be opened or recovered (permissions, a read error) | no |
| 11 | could not listen on the port (taken, or below 1024 without the right) | yes |
| 12 | `delivery.seg` could not be opened or recovered | no |
| 13 | `endpoints.conf` or the `endpoints` table has an invalid line or row (the message names it) | no |
| 14 | the retry schedule does not parse | no |
| 15 | `delivery.seg` has records this version does not understand | no |
| 16 | `events.seg` has a record this version does not understand, or more idempotency keys than the index holds (65,536) | no |
| 17 | a new endpoint could not be given a slot (the `created` record could not be written) | no |
| 20 | the database could not be read at start (message), or an import was refused | yes |

Statuses 10 to 17 print nothing: **the status is the whole message**. 15 and 16 are what a log from a *newer* version looks like to an older one (section 5).

## 2. Configuration

Settings come from the defaults, then `--config <file>`, then the flags in the order written: **the last source that names a setting wins**. Anything else is refused before the service listens or writes (exit 2, a line on stderr naming the argument or the line of the file). **There are no environment variables**: the service reads none. `GET /config` shows what is in force, never a secret. A commented sample: `deploy/hooks.conf.example`.

| key | default | what to know |
|---|---|---|
| `port` | required | 1 to 65535. Not below 1024 under the unit (no capability): put a reverse proxy in front |
| `dir` | required | the data directory: `events.seg`, `delivery.seg`, and (no database) `endpoints.conf` |
| `schedule` | `5000,300000,1800000,7200000,18000000,36000000,50400000,72000000,86400000` | retry delays in ms; after the last, the event is a dead letter for that endpoint. Nine delays is ten attempts over about 24 hours |
| `deadline-ms` | `2000` | how long one attempt may take, connect to status line |
| `window-ms` | `86400000` | how long an `Idempotency-Key` is remembered |
| `pg-host`, `pg-port`, `pg-user`, `pg-database`, `pg-password` | none, `5432`, `hooks`, `hooks`, none | with `pg-host`: endpoints come from the table, the history is written to it, and a database that cannot be read at start is a refusal (status 20). Put the password in the file, not on a command line |
| `allow-private-hosts` | `0` | `0`: an endpoint's host must be a public IPv4 literal. `1`: names and private, loopback, link-local addresses too. Names are resolved by a call that **blocks the whole loop** |
| `admin-token` | none | 8 to 255 visible characters. Without it `POST`, `PATCH`, `DELETE /endpoints` are `403`. Whoever has it chooses where the service sends requests |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the table and exit |

**Secrets.** The settings file holds `admin-token` and `pg-password`; `endpoints.conf` and the `endpoints` table hold every endpoint's signing secret **in the clear** (the service must sign with it). Give the file `root:hooks 0640`, the database role only what it needs, and treat a backup as a secret (section 4). **(planned: 0.3)** scoped tokens for ingest and read, a `production = 1` profile that refuses an unsafe configuration, and a statement on secrets at rest. Today `POST /events`, `GET /events/:id`, `GET /endpoints` and `POST /endpoints/:id/enable` need no credential at all: **bind the port to a trusted network or put an authenticating proxy in front**.

**PostgreSQL.** `psql -f sql/schema.sql` creates `attempts`, `endpoints`, `schedules` and the sequence `endpoint_ids`; it is idempotent and **must be re-run before starting a newer binary**, because every connection prepares every statement and a missing table or sequence is a refusal (status 20, "the query failed"). The image carries the file: `docker run --rm --entrypoint cat lexsys-hooks /usr/share/hooks/schema.sql | psql ...`.

## 3. Reading the service

### 3.1 What it writes to stderr (all of it)

The service logs at start and when it refuses to start. **Nothing is logged while it runs**, not a failed attempt, not a dead letter, not a lost database connection: those are in `/stats`, in `delivery.seg` and in the `attempts` table. (The reason an attempt failed is recorded only as the table's `status`; the log has none. **(planned: 0.4)** the reason in the log.)

| line | meaning | what to do |
|---|---|---|
| `listening` | recovered, endpoints read, database connected (or not, see below), port open | nothing |
| `hooks: the database: N of 2 connections opened; history is written over those` | the history's connections: with fewer than 2 the history is degraded, delivery is not | check the database and the role; restart to reconnect (a lost connection is **never** reopened) |
| `hooks: --config <file>: cannot be read` / `is 16 KiB or more` / `: line N names a setting there is none of` / `has a value that setting does not take` / `is not key = value` | the settings file is wrong, status 2 | fix line N |
| ``hooks: `<arg>` is not a setting`` / `has a value that setting does not take` / `is not a flag: settings are --key value or --key=value` / `needs a value` | the command line is wrong, status 2 | |
| `hooks: --port and --dir are required` | status 2 | |
| `hooks: endpoints.conf: line N is not valid (...)` / `hooks: the endpoints table: row N is not valid (...)` | an id of seven digits or more, a repeated id, a bad port, a host that is not a public IPv4 literal (unless `allow-private-hosts`), a secret that is not `whsec_` and base64, or more than 62 endpoints. Status 13 | fix that line or row |
| `hooks: the database's endpoints cannot be read: cannot connect` / `cannot log in` / `the query failed (is the endpoints table there? sql/schema.sql)` / `a row has an empty field or a byte that is not printable` / `the table is too large (the service reads at most 32 KiB of it)` | status 20; the endpoints may not be guessed, so it does not start | the database, the password, `sql/schema.sql` |
| `hooks: --import-endpoints needs --pg-host`, `no endpoints.conf to import in --dir`, `endpoints.conf: line N is not valid ...; nothing was imported`, `the database refused the import; nothing was imported`, `imported A of N endpoints; one already there is not changed` | the one-off import | |

### 3.2 `GET /stats`

All counters are **since this start** (they are zero after every restart; the history table is what survives).

| field | meaning |
|---|---|
| `endpoints` | endpoints the service delivers to |
| `attempts` | delivery attempts that ended |
| `delivered` | ended with a `2xx` |
| `failed` | ended in failure and will be tried again |
| `dead` | ended in failure with the schedule exhausted: a dead letter (or a `410`) |
| `keys` | idempotency keys held (limit 65,536; a new key beyond that is a `507`) |
| `replays` | replay requests waiting (limit 32) |
| `draining` | deleted endpoints with an attempt still on the wire (their slot is not free yet) |
| `history_live` | database connections for the history (0 to 2) |
| `history_written` | history rows the database accepted |
| `history_failed` | rows the database refused, or lost with the connection |
| `history_dropped` | rows dropped because the ring (256) was full or no connection was live |

`attempts = delivered + failed + dead` (replays count too). A healthy idle service has `failed` flat. A `failed` that climbs with `delivered` flat is a receiver that is down. `history_dropped` climbing is a slow or gone database (delivery is unaffected).

### 3.3 `GET /endpoints`

`{"id","port","cursor","disabled"}` for each endpoint. **`cursor`** is the largest event id such that every event up to it is delivered or dead for that endpoint: **lag = the newest event id (the `id` the last `POST /events` answered) minus the cursor**. An endpoint whose cursor does not move while events arrive is the one to look at. `disabled: true` means a `410 Gone` disabled it; `POST /endpoints/:id/enable` undoes it.

### 3.4 The files

`events.seg` and `delivery.seg` are [lexsys-log](https://github.com/alpibrusl/lexsys-log) segments: `len | crc32c | id | seq | fields | pairs`, append-only. `events.seg` ids are dense from 1 (`GET /events/:id`). A `delivery.seg` record is five integers: kind (1 delivered, 2 failed, 3 dead, 4 disabled, 5 enabled, 6 to 9 replay and its outcomes, 10 an endpoint was given a slot, 11 a slot was freed), endpoint, event, attempts, next attempt (Unix ms). `scripts/logcheck.py check <dir>` reads both without the service and says what is wrong with them.

## 4. Backup and restore

### 4.1 What there is to back up

| | where | needed? |
|---|---|---|
| `events.seg` | `dir` | **yes**: every accepted event |
| `delivery.seg` | `dir` | **yes**: what was delivered, retried, dead. Lose it and every event is delivered again |
| `endpoints.conf` | `dir` | only if no database is named |
| `endpoints` table and the sequence `endpoint_ids` | PostgreSQL | **yes** with a database: the endpoints and their secrets, and the ids never to be given twice |
| `schedules` table | PostgreSQL | **yes** with a database: the cron expressions, and where each stands (`last_fired`, `next_fire`). The dump is taken before the logs, so a restored table is never ahead of the restored events log: a fire the table forgot is found by its idempotency key in the log and not made twice |
| `attempts` table | PostgreSQL | no: best-effort history. `--skip-attempts` leaves it out |
| the settings file | `/etc/hooks/` | yes, from your configuration management; it is not in the data directory |

`scripts/backup.sh` writes one directory (`hooks-backup-<UTC time>/`) with the files, `hooks.pgdump`, `MANIFEST` and `SHA256SUMS`, 0700, under a temporary name that is renamed only after it verified. **It holds every secret in the clear.**

### 4.2 Take one

```sh
# stopped: consistent by construction, costs the downtime of a copy
scripts/backup.sh --dir /var/lib/hooks --out /var/backups/hooks --mode stopped \
    --stop-cmd 'systemctl stop hooks' --start-cmd 'systemctl start hooks' \
    --pg-database hooks --pg-host 127.0.0.1 --pg-user hooks_backup          # password: ~/.pgpass or PGPASSWORD

# online: the service keeps running (read 4.4 first)
scripts/backup.sh --dir /var/lib/hooks --out /var/backups/hooks --mode online --pg-database hooks
```

`--mode stopped` refuses (exit 3) if any process still has a log open: it looks in `/proc`, so run it as root or as the service's user. `--start-cmd` runs even if the backup fails. Exit statuses: 0 done; 2 usage; 3 refused; 4 the copy did not verify (nothing is kept); 5 `pg_dump` failed. Needs `python3` (standard library only), `sha256sum`, and `pg_dump` for a database.

### 4.3 Restore

```sh
systemctl stop hooks
scripts/restore.sh --backup /var/backups/hooks/hooks-backup-20261004T120000Z --dir /var/lib/hooks \
    --pg-database hooks --pg-host 127.0.0.1 --pg-user hooks_admin            # createdb hooks first; the tables need not exist
systemctl start hooks
curl -s localhost:8080/stats; curl -s localhost:8080/events/<events_last_id of the MANIFEST>
```

`restore.sh` checks before it changes anything: `SHA256SUMS`, the format, both logs record by record, and **that `delivery.seg` does not refer to an event `events.seg` lacks** (4.4). It refuses a `--dir` that already has a log (exit 3) unless `--force`, which **moves** the old files to `<dir>/pre-restore-<time>/`; it refuses while the service has the files open; the database part is one transaction (`pg_restore --single-transaction`), so a failure leaves the tables as they were. Exit 4: the backup does not verify.

**What the restored service repeats.** It starts as after a crash. Every event that `events.seg` holds is there; deliveries that `delivery.seg` records are not repeated; deliveries after the backup are made (again): **at least once, never zero**. Tested exactly, not approximately (4.5). With a database, an endpoint created after the backup is gone and one deleted after it is back; the restored `endpoint_ids` keeps an id from being reused.

### 4.4 Is the online variant safe? Yes, under conditions, and here is what they are

The honest answer first: this is an **argument from the format and the code, plus tests, not a proof**, and it stops being true if the code changes in the ways listed.

1. **Both files are only appended to while the service runs.** Recovery cuts a torn tail only at *start*; nothing compacts or rewrites a log (`delivery.seg` "is never compacted", `production.md` 0.2 is the item that will change that). So a copy of a live file is a valid prefix plus, at most, one torn record, which the service itself would cut at start and `backup.sh` trims (and records in the `MANIFEST`: `torn_bytes_cut_*`).
2. **Every acknowledgement follows a flush, and a delivery only concerns flushed events**, so a copy begun after an acknowledgement contains that event, and an outcome in `delivery.seg` never refers to an event that was not already durable in `events.seg`.
3. **The order of the copies is what makes the pair usable: `delivery.seg` first, `events.seg` second.** Then `events.seg` is never older than what `delivery.seg` says. The other order is not a small inconvenience: **a service started on an `events.seg` shorter than its `delivery.seg` acknowledges new events under ids it believes already delivered, and never delivers them.** Measured (`tests/backup_test.py`, "INFO 9"; and by hand): a service given 8 events beside outcomes for 10 acknowledged event 6 with `202` and sent nothing. It does not notice. `backup.sh` copies in the safe order and checks the result; `restore.sh` checks again, so a pair assembled by hand or by another tool is refused (exit 4, "refers to event N but events.seg ends at event M"). **The service itself has no such guard** (a change for the code, see the end of this section).
4. **The cost is repeats, not loss.** A restore of an online backup repeats the deliveries made between the copy of `delivery.seg` and the end of the run, and an endpoint's retry state goes back to what it was at the copy.
5. **The database and the logs are two stores and no instant covers both.** `pg_dump` runs first (one snapshot), the logs after. A row the dump has and the logs lack makes the service place that endpoint at the slowest cursor (a repeat of up to 1,024 events, never a loss: `design.md` 25.3); a row deleted after the dump comes back and is placed the same way.
6. **A file-system or volume snapshot is also fine for the logs** (it is a crash-consistent image of both files at one instant, and the ordering problem disappears), as long as `fsync` is honoured below it.

If any of 1 to 3 is no longer true, withdraw `--mode online` and use `--mode stopped`. The test would show it: a mutant that copies `events.seg` first is caught by the script's own check (the backup is refused), and one without the check is caught by the restore tests.

**Not safe, whatever the mode:** copying the files with a tool that does not read them in the order above and then starting the service on the result without `restore.sh`/`logcheck.py`; restoring `events.seg` from one backup and `delivery.seg` from another.

### 4.5 What the test does (`tests/backup_test.py`, 46 s with the defaults)

Every expectation is derived from the backup's own files with the independent reader of `tests/chaos.py`.

* **A. stopped, with PostgreSQL.** 150 events; endpoint 0 delivered all of them and endpoint 1 only the first 50 (its receiver refuses the rest). Backup with `--stop-cmd`/`--start-cmd`. Then **total loss**: the directory deleted, `attempts`, `endpoints` and the sequence dropped. Restore. The tables are back row for row (secrets included), the sequence has its value, every event is served back byte for byte, a new event is id 151, **endpoint 1 receives 51 to 151 once each and endpoint 0 receives only 151**: nothing it already had is sent again.
* **B. online, under load and `kill -9` as a power cut** (the `fsync` shim of `tests/chaos.py`): backups taken continuously while 1,500 events are posted and the service is killed about every 250 ms (11 kills, 12 starts, 11 backups, 7 overlapping a kill; one run cut 43 bytes of torn tail from copies taken mid-write). Every backup holds every event acknowledged before it began, byte for byte; a sample (the first, the last, every one with a torn tail, and spread between) is restored and started: events served back, **a new event is delivered** (the restored service is not stalled), each event is delivered once to each endpoint that did not have it at the time of the backup and never to one that did, and the next id follows the last.
* **C. refusals.** A flipped byte; an `events.seg` older than its `delivery.seg` with valid checksums; a non-empty `--dir`; `--force`; a service holding the files; a torn tail (cut, and the result restores); damage in the middle of a log (refused, not silently shortened); `--stop-cmd` with `--mode online`.
* **Mutants of the scripts that the test kills (7 of 7):** `events.seg` copied first (the backup is refused by the script's own check); the trim removed; the cross-check removed from `logcheck.py`; the "is it running" check removed; the checksum check removed from `restore.sh`; the sequence left out of the dump; the `--force` guard removed.

### 4.6 Speed, and what grows

`logcheck.py` reads a log at about 8.6 MB/s (table-driven CRC-32C in Python, measured on 20 MB), and a backup reads each log about twice and a restore twice: **a 1 GB `events.seg` is about four minutes of checking per backup**. The logs are never compacted today (`production.md` 0.2), so this only grows. `pip install crc32c` makes it much faster (the script uses it if present) but that was not measured here.

### 4.7 For the code (not done here: `src/` was not touched)

* **Refuse a pair like the one 4.4 describes**: at start, if `delivery.seg` refers to an event id above the last in `events.seg`, exit with a status of its own (18) and a message. Today it silently stalls the new events.
* **Refuse, don't cut, a log with damage in the middle.** lexsys-log's `recover` keeps the longest valid prefix and cuts the rest *at start*. A flipped byte in the middle of `events.seg` makes the next start silently truncate everything after it: measured here, 500 events, one byte flipped in the middle, **250 events gone after one start and nothing printed**. A torn tail is at most one record: a tail longer than that is damage and should stop the start. (`logcheck.py check` tells them apart; run it before starting after any trouble with the disk.)

## 5. Upgrading

1. **Back up, stopped** (4.2). The backup is also the rollback.
2. Read the release notes for a schema change. `psql -f sql/schema.sql` is idempotent: **run it before the new binary starts** (an older deployment that lacks `endpoint_ids` or a column is refused with status 20).
3. Stop, replace `/opt/hooks/bin/hooks` (or the image tag), start. Check `listening`, then `GET /stats` and a canary event.
4. **Rolling back** is not "start the old binary": the new binary may have written records the old one does not know, and the old one refuses a `delivery.seg` it does not understand (status 15; 16 for `events.seg`). If the new version only ran briefly and wrote nothing new, the old binary starts; if it refuses, **restore the backup from step 1** and accept losing what was accepted since, or keep the new binary.
5. **There is no format version in the logs today, and no refusal that says "this log is from a newer version"**; 15 and 16 are the nearest thing, and they fire on unknown *record shapes*, not on a version. A version header and a refusal that names it is **(planned: P2 in `production.md`, not built here: it needs a change to the service and to lexsys-log)**.

`docker`: pull or build the new tag, `docker stop`, `docker run` with the same volume. The volume is the data directory; nothing else is state.

## 6. When it goes wrong

| what you see | what it is | what to do |
|---|---|---|
| the process is gone (OOM, `kill -9`, a crash, a power cut) | normal | restart it (systemd does). It recovers both logs, cuts a torn tail, and repeats what was on the wire. Nothing acknowledged is lost (`tests/chaos.py`) |
| `POST /events` answers `503 {"error":"the event could not be stored"}` for every request, `/healthz` says `200` | the log is **broken**: a write or flush failed (the disk is full, an I/O error) and by design it is not retried, because after a failed write the file's contents are unknown. **Verified on a 96 KB tmpfs: 634 events accepted, then 503 for each of the next 866 requests** (the ones before it are safe: only acknowledged after a flush) | free space or fix the disk, then **restart**: it cut the torn tail (144 bytes there) and accepted event 635, with 634 intact. A restart is the only way back. **(planned: 0.4)** `/readyz` would say it |
| status 11 at start | the port is taken | find who has it |
| status 10 or 12 | a log cannot be opened | permissions on the data directory (`StateDirectory`, uid 10001 in the container), a read error. Run `scripts/logcheck.py check <dir>` |
| status 13 | an endpoint line or row is invalid | the message names it |
| status 20 | the database is not reachable, the password is wrong, a table or the sequence is missing | `psql` with the same settings; `psql -f sql/schema.sql`. It is restarted by systemd until the database is back |
| status 15 or 16 | the logs are from a newer version, or damaged | section 5 step 4; else `logcheck.py check` |
| events accepted, nothing delivered after a restore | `delivery.seg` is newer than `events.seg` (section 4.4) | `scripts/logcheck.py check <dir>` says so; restore a consistent backup. Events accepted since are in `events.seg` and would be delivered once the pair is right |
| an endpoint's cursor does not move, `failed` climbs | the receiver is down, slow or refusing | it is retried on the schedule (5 s, 5 min, 30 min, 2 h, ...) and then dead-lettered; its events are never lost, `POST /events/:id/replay/:endpoint` sends one again. A `410` disables it |
| **every** endpoint stops, one is dead | the known limit: an endpoint more than 1,024 events behind is not served until it catches up **and the window it holds is shared** (`production.md` 0.1, measured: one dead endpoint stops the others after 1,024 events) | bring the dead receiver back, or `DELETE /endpoints/:id` it (that removes its cursor from the shared bound; not tested here). **(planned: 0.1)** |
| `history_dropped`/`history_failed` climbs | the database is slow, gone or refuses | delivery is unaffected. Restart to reconnect (a lost connection is never reopened). The history has holes: that is its contract |
| `POST /events` answers `507` | 65,536 idempotency keys are held | wait for the window (`window-ms`) or restart with a shorter one; it is not a log problem |
| `POST /events` answers `413` | the event is 65,500 bytes or more (less with a key) | the client's |
| start is slow | the start reads both logs whole and rebuilds the idempotency index from `events.seg` | no fix until the logs are bounded (`production.md` 0.2). Measure it and write it down |
| the admin token leaked | whoever has it can aim the service at any public address (a private one only with `allow-private-hosts`) | change `admin-token`, restart; `GET /endpoints` and check where each points. The signing secrets are in the table: rotate them with `PATCH ... "rotate": true` if the database was readable too |
| the disk of the **backups** fills, or a backup fails | `backup.sh` keeps nothing it did not verify | nothing partial is left (`.hooks-backup-*.partial` is removed); fix and re-run. Alert on the age of the newest `hooks-backup-*` |
| you must move to another host | a restore | section 4.3 on the new host with the newest backup; nothing else is state |

## 7. Known limits (from `production.md` and the README; none is hidden)

One process, one thread, one core (roughly 7 to 10k deliveries a second measured). At most 62 endpoints. **One dead endpoint stops every endpoint after 1,024 events** (0.1). Logs are never compacted and the start reads them whole; disk and start time grow with history (0.2). `POST /events`, `GET /events/:id`, `GET /endpoints` and `enable`/`replay` need no credential (0.3). No `https` endpoints. A host *name* blocks the loop while it resolves (use IP literals). No event-type filtering, no two signatures during a rotation, no jitter in the retry schedule. A failed attempt records no reason in the log (0.4). A lost database connection is not reopened. At most 65,536 idempotency keys. An event of 65,500 bytes or more is refused. Secrets are in the clear in the table and in `endpoints.conf`. No graceful stop, no readiness, no metrics (0.4). No log format version (P2). No soak test of 24 hours has been run.

## 8. Hardening notes (`deploy/hooks.service`)

* `systemd-analyze security --offline`: exposure 1.3 (OK). The unit drops all capabilities, makes the system read-only except `StateDirectory`, hides `/home` and `/proc` of others, gives a private `/tmp` and `/dev`, allows only `AF_INET`, `AF_INET6` and `AF_UNIX`, denies write-and-execute memory, and filters system calls to `@system-service` less `@privileged` and `@resources`.
* **Measured, not guessed:** `strace -f` over a workload with PostgreSQL, a settings file, 50 events, `GET`s and a `POST /endpoints` showed 34 distinct system calls (one of them the `execve` that strace's own start causes), **all** inside `@system-service`; the only executable mapping is libc's own (file-backed), so `MemoryDenyWriteExecute` holds; the only files opened are libc, `ld.so.cache`, the settings file, `/dev/urandom` and the two logs.
* **Not covered by that measurement:** a host *name* as an endpoint (`allow-private-hosts 1`) goes through the resolver, which may open NSS libraries and a socket; `AF_UNIX` is left open for that. If a name fails to resolve under the unit, look at `journalctl` for `SIGSYS`/`EPERM` and relax `SystemCallFilter` first.
* `Restart=on-failure`, with `RestartPreventExitStatus` for the refusals a restart cannot fix (section 1), and `StartLimitBurst=10` in 5 minutes.

## 9. Releases

`LEX_SYS=/path/to/lex-sys scripts/release.sh [--version V]` builds with `lex-sys build` (which refuses any compiler but the commit `lex-sys.toml` pins), refuses a dirty tree, and writes `dist/hooks-<version>-linux-<arch>/`: the tarball (`bin/hooks`, `deploy/`, `scripts/`, `sql/`, `docs/`, `README.md`, `LICENSE`, `Dockerfile`, `SBOM.json`), `hooks-<version>-linux-<arch>.sbom.json` and `SHA256SUMS` (`sha256sum -c SHA256SUMS`). The tarball is deterministic for a given binary (sorted names, owner 0, mtime from the last commit, `gzip -n`).

**Is the binary reproducible? Measured: yes, after one normalization.** Four builds of the same sources (three directories on one host, one inside the `Dockerfile`, with a separately built compiler) gave four different files. They differ in exactly one place: a `FILE` symbol with a temporary name that holds a process id (`lex-sys-llvm-<pid>-0.ll`), and the build-id note derived from it. After `strip --strip-all --remove-section=.note.gnu.build-id` all four are the same `fd3c914f9cc8a815...`, 309,232 bytes. That is what `release.sh` ships (`--keep-symbols` ships what the compiler wrote) and what the image holds, so **the binary in the tarball and in the image have the same hash when the toolchain is the same Ubuntu 24.04 (clang 18.1.3, gcc 13.3, binutils 2.42)**. This is measured on one OS and architecture; it does not say a different clang gives the same bytes. For the compiler: the pid in a symbol name is a small thing to fix in lex-sys.

**The SBOM is a stub and says so** (`"complete": false`, and a `not_listed` list in the file): the compiler (the pin, what `lex-sys --version` reports, whether they match, and the `clang` and `cc` that shaped the binary), the std (compiled into the compiler: its version *is* the compiler commit; the `std.*` modules the sources import), the two libraries pinned by commit, and the dynamic libraries from `ldd` with hashes and, where `dpkg` knows, the package and version **on the build host** (the target must have this glibc or newer). It does not list the Rust toolchain and crates behind the compiler, LLVM, the base image's packages, or any signature. It is not CycloneDX or SPDX.

**Not done:** signing (anyone who can replace the tarball can replace `SHA256SUMS`), a stable download URL (CI keeps a binary as a run artifact for 90 days), a multi-architecture build.

## 10. The container

`docker build -t lexsys-hooks .` (about 1 min 40 s cold, 9 s with the compiler layer cached). Two stages build; the runtime stage holds the base image (87.6 MB for `ubuntu:24.04`), `tini` (1.4 MB), the binary (0.3 MB) and `sql/schema.sql`. It runs as uid 10001, with `/var/lib/hooks` as a volume, `EXPOSE 8080` and a `HEALTHCHECK` on `GET /healthz`. The compiler stage reads the pin from `lex-sys.toml`, builds the compiler from that commit's `Cargo.lock` with the toolchain its `rust-toolchain.toml` pins (rustup-init 1.28.2, checked against its checksum file from the same server), and `lex-sys build` fetches and checks the two libraries. Not pinned, and said so in the file: the base image (a tag; use `--build-arg BASE=ubuntu:24.04@sha256:...`) and the apt packages. **Built and run here by hand, not by CI**; the sandbox needed a proxy CA, which is not part of the file (`--build-arg BASE=` pointed at a base image that has it).

A bind mount for `/var/lib/hooks` must be owned by uid 10001. The image's settings file has only `port` and `dir`; mount your own over `/etc/hooks/hooks.conf` (mode 0600, owner 10001) rather than put a token or a password on the command line. If you change the port with a flag, tell the health check: `HOOKS_HEALTH_PORT`. These two variables are read by the health check only; the service reads none.

## 11. What depends on what is not built

| here | needs | status |
|---|---|---|
| `GET /readyz`, `/metrics`, alerting on lag and dead letters | 0.4 | **planned** |
| a drain on `SIGTERM`, `TimeoutStopSec` as its deadline | 0.4 | **planned** |
| the reason a delivery attempt failed, in the log | 0.4 | **planned** |
| tokens for ingest and read, `production = 1` | 0.3 | **planned** |
| a dead endpoint not stopping the others | 0.1 | **planned** |
| bounded logs, a snapshot of the outcome state, a bounded start time | 0.2 | **planned**; `--mode online` backup must be re-argued when a log can be rewritten |
| a log format version and a refusal that names it | P2 | **planned** (needs the service and lexsys-log) |
| refusing a `delivery.seg` that outruns its `events.seg`, and a log with damage in the middle (4.7) | the service and lexsys-log | **planned**; the scripts are the guard until then |
| a soak test of 24 hours, a capacity page | P2 | not done |
