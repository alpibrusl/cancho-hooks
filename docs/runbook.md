# Running lexsys-hooks: the runbook

*For whoever has the pager. Everything here was read from the source or run; where it was not, it says so. Parts that depend on work that is not built are marked **(planned)** and say which item of [`production.md`](production.md) builds them: do not assume they exist. The README still says "Not for production", and `production.md` says what has to be true before that line comes off.*

## 0. What this is, in one paragraph

One process, one thread. It takes `POST /events`, appends the event to the events log (`events.seg`, then `events-1.seg` ... as it rolls) and answers `202` only after the `fsync` that covers it; it delivers each event, signed, to every endpoint, and records every outcome in `delivery.seg`. The two files are the truth; PostgreSQL (optional) holds the endpoints and a best-effort history of attempts. **Everything the service knows after a restart it learned by reading those two files.** That is why a crash is a normal event here, and why the backup is two files and a `pg_dump`.

| | verified here | not verified here |
|---|---|---|
| backup, restore, the consistency check | `tests/backup_test.py` (section 4): total loss and restore, and (in a 6,000-event run) 22 power-cut kills with 25 online backups | a backup of a log above a few hundred MB (speed: section 4.6) |
| the container | built (`docker build`, 1 min 40 s), run, health check healthy, `docker stop` in 0.08 s, volume kept an event across a restart, non-root uid 10001 | CI does not build it; the base image is a tag, not a digest; no multi-arch |
| the systemd unit | `systemd-analyze verify`, `systemd-analyze security --offline` (1.3 OK), the syscalls the service makes are inside `@system-service` | **never run under a systemd**: the first start on a real host is the test |
| the release script | run, tarball and checksums checked (section 9) | signing, a stable URL, a real SBOM format |
| readiness, metrics, the reason an attempt failed, the stop, refusing corruption (sections 1, 3 and 4.7) | `tests/metrics_test.py`, `ready_test.py`, `reason_test.py`, `stop_test.py`, `corrupt_test.py`, `backup_test.py` (`docs/design.md` section 34) | the stop under a real systemd; `/readyz` with the database back (a lost connection is not reopened: restart); a log of hundreds of MB (the start reads `delivery.seg` once more, for the pair check) |

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

The service prints **`listening`** on stderr when it is ready and nothing else while it runs (section 3.1). With a database named, the start can take as long as the database takes to answer (it reads the endpoints before it listens, and the blocking client has no timeout), and it reads the retained events and the outcomes log (section 3.5: start time is bounded by retention, not by history).

### Stop

`SIGTERM` (`systemctl stop hooks`, `docker stop hooks`) or `SIGINT` (Ctrl-C): the service **drains** (`docs/design.md` section 34.4).

1. It says `hooks: stopping on SIGTERM: ...` on stderr. From then on it takes no request that writes and has passed the credential check (`POST`, `PATCH`, `DELETE`: a `503 {"error":"the service is stopping"}` and the connection closes), `GET /readyz` is `503` with `"check":"stopping"`, and it starts **no attempt**, new or retry or replay. Reads (`/healthz`, `/metrics`, `/stats`, `GET /events/:id`) still answer, so a drain can be watched (with the read token, where one is configured). The listening socket stays open until the process exits (the http server library owns it); a connection that arrives is answered `503` and closed.
2. The attempts that are on the wire go on. When the last one has ended (a status, a refusal, its own `deadline-ms`) and the history has been handed to the database, or when **`stop-deadline-ms`** (5000 unless set) has passed, whichever is first, it flushes both logs, closes them and exits **0**, saying `hooks: stopped: nothing was left on the wire` or `N attempts were still on the wire at the deadline; they are made again at the next start` (`1 attempt was ...; it is made again` for one).
3. A **second signal** (`SIGTERM` or `SIGINT`) ends the process at once, killed by that signal (status 143 or 130 from a shell). That is as safe as `kill -9`: an acknowledgement is only sent after the flush that covers the event, and an outcome is written after the receiver answered, so what it can cost is a *repeat* of an attempt that was on the wire (at-least-once).

How it is known: the service **claims** `SIGTERM` and `SIGINT` when its loop starts (lex-sys's signals capability, `Signals("INT,TERM")`: the kernel queues them instead of acting, and no handler runs) and the claim is watched in the same poller as the sockets, so a stop wakes the loop at once (a fraction of a millisecond; it was up to 50 ms when the loop looked once a turn). On the first signal it gives both signals their default action back, which is what makes the second one kill. No foreign call is involved: `lex-sys authority` lists `signals("INT,TERM")` and, as foreign code, only the `statx` of the production profile (section 2.1; lex-sys has no file-mode builtin: lex-sys#243), which is why it still says `bounded: false` for this binary, with exactly one entry in `unbounded_by`, `libc:statx`; the report is pinned in CI (`docs/authority.json`). A signal that arrives *before* the loop starts (while the logs are being read) has its default action: the process ends at once, which is safe. If the two signals cannot be claimed (the kernel is out of file descriptors) the service prints `hooks: SIGINT and SIGTERM cannot be claimed (errno N)` and exits with status 5 rather than run unable to hear a request to stop. `TimeoutStopSec` (systemd) and `docker stop -t` must be longer than `stop-deadline-ms` plus a moment to flush: the unit uses 15 s for the 5 s default.

A process started with `SIGINT` ignored (a background job of a non-interactive shell, `nohup`) inherits that. The service still claims and reads it, so the first Ctrl-C starts a drain; afterwards the signal is ignored again, as it was inherited, so a second `SIGINT` does nothing and `SIGTERM` is what ends the process at once. (The tests start the service with the default disposition, so that the second `SIGINT` is the one they check.)

`kill -9` and a power cut are still safe, by construction and by test (`tests/chaos.py`, `tests/delivery.py`): what they can cost is a repeat. A drain just makes that repeat rare.

In a container the service is PID 1 unless something else is, and **a PID 1 with no `SIGTERM` handler ignores `SIGTERM`** (measured with `unshare --pid`: it survives). The image therefore starts it under `tini`: `docker stop` takes 0.08 s when idle, and 10.1 s and exit 137 without it. If you run the binary some other way in a container, use `docker run --init`.

### Health, readiness, metrics

| | |
|---|---|
| `GET /healthz` | open whatever tokens are configured. `200 {"ok":true}` while the process is up and its loop turns. It reads nothing and writes nothing: **with the disk full (section 6) it still answers `200`** while every `POST /events` is a `503`. Use it as a liveness probe that must not restart on a full disk |
| `GET /readyz` | `200 {"ready":true}` when the service can do its job; otherwise `503 {"ready":false,"check":"<name>","reason":"<sentence>"}`. The checks, in the order they are made: `stopping` (asked to stop), `events_log` and `delivery_log` (a write or a flush failed; **only a restart clears it**), `data_dir` (a 1-byte file could not be written in the data directory; checked once a second; clears itself when space is back), `database` (a database is named and none of its two connections is live; **a lost connection is never reopened**, so this clears only by a restart). No credential, whatever tokens are configured. It is the container's health check (`deploy/hooks-healthcheck.sh`), because every way it can be `503` is one a restart repairs or a stop in progress |
| `GET /metrics` | the Prometheus text format, below. A **read**-scope route: when a `read-token` is configured (with `production = 1`: the read token, or the admin token if there is no read token) the scraper must send `Authorization: Bearer <token>`; in Prometheus, `authorization: { credentials_file: /etc/prometheus/hooks-read-token }` in the scrape job. Without a read token it is open, like the other reads (section 2.1) |
| `GET /stats` | the counters as JSON (section 3.2) |

**Alert on** (names are `/metrics` series; the thresholds are yours, these are where I would start):

| alert | because |
|---|---|
| `hooks_ready == 0` for 2 minutes, or `/readyz` not `200` | `check` says what: `events_log`/`delivery_log`/`database` need a restart, `data_dir` needs space |
| `rate(hooks_ingest_events_total{result="refused"}[5m]) > 0` with `hooks_ingest_refused_total{status="503"}` climbing | events are being refused: the log is broken or the disk is full. `status="413"`, `"422"` and `"400"` are the clients' |
| `hooks_endpoint_lag_events > 1000` for 10 minutes, per `endpoint` | an endpoint is behind: its receiver is down or slow (1,024 is its window). `hooks_endpoint_last_failure{endpoint,reason}` says why it last failed, and `hooks_endpoint_failing_since_ms` since when |
| `increase(hooks_attempts_total{outcome="dead"}[1h]) > 0` | dead letters were made: events that will not be retried (replay them: `POST /events/:id/replay/:endpoint`) |
| `hooks_endpoint_paused == 1` or `increase(hooks_breaker_trips_total[1d]) > 0` | the circuit breaker stopped an endpoint after `breaker-days` of failures; it waits for `POST /endpoints/:id/enable`. `hooks_endpoint_disabled == 1` without `paused` is a `410` or a person |
| `increase(hooks_attempt_failures_total{reason="connect_error"}[10m])` or `reason="busy"` | not the receivers': `connect_error` covers unreachable networks and descriptors; `busy` is all 64 connections in use |
| `hooks_history_rows_total{result="dropped"}` or `{result="failed"}` climbing, `hooks_history_connections < 2` | the database is slow, gone or refusing; delivery is unaffected, the history has holes |
| `hooks_log_size_bytes` growing without bound; `hooks_idempotency_keys` near `idem-keys` | retention is off (`retention-days = 0`), or an endpoint is paused, disabled or dead and pins its events (section 3.5); a new key beyond `idem-keys` while all are fresh is a `507` |
| the age of the newest `hooks-backup-*` | section 4 |
| a restart: `hooks_uptime_seconds` falling | a crash, a stop, or a deploy |

The reason series (`hooks_attempt_failures_total{reason}`): `connect_refused` (nothing listens there), `connect_timeout` (the address does not answer within `deadline-ms`), `connect_error` (any other way the connection failed: unreachable, reset, no descriptor), `send_timeout` and `send_error` (the receiver does not read the request, or hung up while it was written), `no_response` (the request was sent and no status line came within `deadline-ms`), `reset` (reset while waiting for the status line), `closed_early` (closed without a status line), `bad_response` (twelve bytes that are not `HTTP/1.x NNN`), `status_3xx`, `status_4xx`, `status_5xx`, `gone` (`410`), `status_other`, `busy`, `too_large`. A redirect (`3xx`) is a failure; it is not followed. The same names are `reason` in `GET /events/:id/attempts`; the numbers are in `sql/schema.sql` and `src/reason.ls` and are never reused.

### Exit statuses

The service never ends by itself once it listens (bar status 4). A start that ends has a status (from the source):

| status | meaning | `systemd` restarts it? |
|---|---|---|
| 0 | `--import-endpoints 1` finished; or the service was asked to stop (`SIGTERM`/`SIGINT`) and drained | n/a |
| 2 | a setting was refused (the message names the argument or the line of the file), or `--port`/`--dir` are missing | no |
| 3 | the value before the logs are opened; no path in the source leaves it, so you should not see it | yes |
| 4 | the event loop could not create its poller (printed `listening` first) | yes |
| 5 | `SIGINT` and `SIGTERM` could not be claimed (printed `listening` first, then one line with the errno) | yes |
| 10 | the events log could not be opened or recovered (permissions, a read error) | no |
| 11 | could not listen on the port (taken, or below 1024 without the right) | yes |
| 12 | `delivery.seg` could not be opened or recovered | no |
| 13 | `endpoints.conf` or the `endpoints` table has an invalid line or row (the message names it) | no |
| 14 | the retry schedule does not parse | no |
| 15 | `delivery.seg` has records this version does not understand | no |
| 16 | the events log has a record this version does not understand (a damaged sealed segment reads this way too), or more fresh idempotency keys than the index holds (`idem-keys`: raise it) | no |
| 17 | a new endpoint could not be given a slot (the `created` record could not be written) | no |
| 18 | **`delivery.seg` refers to an event `events.seg` does not hold** (an older events log beside a newer delivery log: `events.seg` ends at event M, `delivery.seg` names event N > M). Nothing was changed. Section 4.7 | no |
| 19 | **damage in the middle of a log**: records that validate follow a place that does not, or the bytes there look like two or more records; a torn tail is not this. The message names the log, says where it is whole to, how many bytes cutting would take and how many intact records are among them. Nothing was changed. Section 4.7 | no |
| 20 | the database could not be read at start (message), or an import was refused | yes |
| 30 | `production = 1`: `admin-token` is not set | no |
| 31 | `production = 1`: `ingest-token` is not set | no |
| 32 | `production = 1`: `allow-private-hosts` is 1 | no |
| 33 | `production = 1`: the data directory, `events.seg`, `delivery.seg` or `endpoints.conf` can be read or written by its group or by others (the message names the path) | no |
| 34 | `production = 1`: two of `admin-token`, `ingest-token` and `read-token` are the same | no |
| 35 | `production = 1`: the mode of the data directory cannot be read (it is not there, or the call failed) | no |
| 40 | a segment of the events log is in a format this version does not understand (a header that says 3 or more, or one that is not this program's header at all) | no |
| 41 | `delivery.seg` is in a format this version does not understand | no |
| 42 | the events log has a hole or a break in its chain of segments (`events.first` names a missing file, a number is missing, a base does not follow the one before) | no |
| 43 | `--compact-now 1` and `compact.lock` is held by another process (a backup?); nothing was done | n/a |
| 44 | `--compact-now 1` and a step failed; what was done is safe, run it again | n/a |

Statuses 10 to 17 print nothing: **the status is the whole message**. 40 to 42 print a line and write nothing; 40 and 41 are what a log from a *newer* version looks like to this one (section 5). 15 and 16 are what a log from *this* version looks like to the one before retention, by construction (section 5). 15 and 16 are what a log from a *newer* version looks like to an older one (section 5). 18 and 19 print what they found (section 3.1). Statuses 30 to 35 print one line that begins `hooks: production = 1 refuses to start:` and names the setting or the path (section 2.1). A status that is not in this table (for example 143, or 130, or a negative number from a supervisor) is the process being killed by a signal.

## 2. Configuration

Settings come from the defaults, then `--config <file>`, then the flags in the order written: **the last source that names a setting wins**. Anything else is refused before the service listens or writes (exit 2, a line on stderr naming the argument or the line of the file). **There are no environment variables**: the service reads none. `GET /config` shows what is in force, never a secret. A commented sample: `deploy/hooks.conf.example`.

| key | default | what to know |
|---|---|---|
| `port` | required | 1 to 65535. Not below 1024 under the unit (no capability): put a reverse proxy in front |
| `dir` | required | the data directory: the events log (`events.seg`, `events-N.seg`, `events.first`), `delivery.seg`, `compact.lock`, and (no database) `endpoints.conf` |
| `schedule` | `5000,300000,1800000,7200000,18000000,36000000,50400000,72000000,86400000` | retry delays in ms; after the last, the event is a dead letter for that endpoint. Nine delays is ten attempts over about 24 hours |
| `deadline-ms` | `2000` | how long one attempt may take, connect to status line |
| `window-ms` | `86400000` | how long an `Idempotency-Key` is remembered |
| `pg-host`, `pg-port`, `pg-user`, `pg-database`, `pg-password` | none, `5432`, `hooks`, `hooks`, none | with `pg-host`: endpoints come from the table, the history is written to it, and a database that cannot be read at start is a refusal (status 20). Put the password in the file, not on a command line |
| `allow-private-hosts` | `0` | `0`: an endpoint's host must be a public IPv4 literal. `1`: names and private, loopback, link-local addresses too. Names are resolved by a call that **blocks the whole loop** |
| `admin-token` | none | the **admin** scope: everything that changes configuration or state, and what the other two do. 8 to 255 visible characters. Without it `POST`, `PATCH`, `DELETE /endpoints` and `/schedules` are `403`. Whoever has it chooses where the service sends requests |
| `ingest-token` | none | the **ingest** scope: `POST /events`. Same rule. Without it `POST /events` is open |
| `read-token` | none | the **read** scope: the `GET`s of events, attempts, endpoints, `/stats`, `/config` and `/metrics`. Same rule. Without it they are open (with `production = 1`, they need the admin token) |
| `production` | `0` | `1`: refuse to start in a configuration that is unsafe on the internet (section 2.1) |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the table and exit |
| `stop-deadline-ms` | `5000` | how long the attempts on the wire may take to finish after `SIGTERM`/`SIGINT` before the process exits anyway (0 to 3,600,000). Keep `TimeoutStopSec` above it (section 1) |
| `repair-logs` | `0` | `1`: damage in the middle of a log is cut at the first bad byte (the cut bytes are first copied to `<log>.cut-<offset>`) instead of the start being refused with status 19. Give it **once, on the command line**, after reading the refusal; it never lifts status 18. Section 4.7 |
| `rotation-grace-ms` | `86400000` | how long the previous secret is still signed with after a `PATCH` with `"keep_old": true` (1 to 2,592,000,000). A change can name its own period with `"keep_old_ms"`. See 3.5 |

**Secrets.** The settings file holds the three tokens and `pg-password`; `endpoints.conf` and the `endpoints` table hold every endpoint's signing secret **in the clear** (the service must sign with it; and, per endpoint, the previous secret while a rotation overlaps, and the values of its custom headers, which are credentials too). Give the file `root:hooks 0640`, the database role only what it needs, and treat a backup as a secret (section 4). Section 2.1 says what is protected by what.

### 2.1 Securing it: the tokens and `production = 1`

**What is open by default.** With no token configured, every route is open, as it always was: a scope whose token is not set leaves its routes open. The exceptions are the routes that have always needed the admin token (`POST`, `PATCH` and `DELETE /endpoints`, every route of `/schedules`), which answer `403` ("management is off") without one. That is right for a laptop. **For anything reachable by anyone else, set `production = 1`**, and the service will not start until it is not open.

| scope | token | routes |
|---|---|---|
| ingest | `ingest-token` | `POST /events` (so its idempotent retries) |
| read | `read-token` | `GET /events/:id`, `GET /events/:id/attempts`, `GET /endpoints`, `GET /endpoints/:id`, `GET /stats`, `GET /config`, `GET /metrics` |
| admin | `admin-token` | `POST`, `PATCH`, `DELETE /endpoints`, `POST /endpoints/:id/enable`, `POST /events/:id/replay[/:endpoint]`, every route of `/schedules` (the reads too: a body is a payload); and everything the other two do |
| open | | `GET /healthz` and `GET /readyz`, always: a container's health check and a load balancer carry no secret |

A request carries `Authorization: Bearer <token>`. No token or a token that is none of the configured ones: `401` with `WWW-Authenticate: Bearer`. A configured token of a scope that does not reach the route: `403` (`this route needs the admin token`). The exact set of routes and scopes is the table in `src/authz.ls`; `tests/authz_test.py` reads the routes from the source and fails if one is missing from it. **A route without an entry is treated as admin-only, and is refused outright (`403`) when there is no admin token**, so a route forgotten in the table is closed, not open.

**The production profile.** Add `production = 1` to the settings file. The service then refuses to start (and prints one line saying which setting or path) unless:

1. `admin-token` and `ingest-token` are set (statuses 30 and 31); `read-token` is optional, and without it the read routes need the admin token;
2. the three tokens are different from one another (34): a token that is the same as the admin token gives its holder the admin scope;
3. `allow-private-hosts` is 0 (32);
4. the data directory and `events.seg`, `delivery.seg` and `endpoints.conf` in it cannot be read or written by the group or by others (33; 35 if their mode cannot be read). The directory should be `0700` and the files `0600`.

When several things are wrong it says one at a time, always in that order. A database is not required: the profile is about who may call the service, and a service with a file of endpoints and no database is a legitimate small deployment (`POST /endpoints` and the schedules answer `503`).

**The umask trap.** The logs the service creates are `0666` less its umask. Under the usual umask of `022` a first start in an empty directory makes `events.seg` `0644`, and with `production = 1` that is refused (33) *after* the files were made; the next start refuses too, until you `chmod 600` them. Start the service with `umask 077`: the systemd unit has `UMask=0077` and `StateDirectoryMode=0700`, the container image starts the service through `deploy/hooks-entrypoint.sh` (`umask 077`) and makes `/var/lib/hooks` `0700`. A bind mount for the container must be `0700` and owned by uid 10001. `scripts/restore.sh` writes under `umask 077`.

**Changing a token** takes a restart (there is no reload): put the new one in the settings file, restart, and give it to its users. During the restart events are not accepted; senders retry (use `Idempotency-Key`).

**Secrets at rest.** The endpoints' signing secrets are in the clear in the `endpoints` table (or `endpoints.conf`) because the service signs with them. **The database is the trust boundary**: whoever can read the table, or a backup of it, can sign as the service. Give the service's role `select`, `insert`, `update` and `delete` on `endpoints`, `attempts` and `schedules` and `usage` on the sequence `endpoint_ids`, nothing else (not the superuser, as the tests use); keep the database off any network the receivers or the senders are on; encrypt the disk and the backups. There is no encryption in the service, on purpose: a key it can read is a key a reader of the same machine can read.

**What it does not do.** No TLS (put a reverse proxy in front for `https` from your own clients), no rate limit on failed tokens, no audit of who called what, no rotation without a restart, no per-endpoint or per-sender scope. A route that exists can be told from one that does not without a token (`405` against `404`).

**PostgreSQL.** `psql -f sql/schema.sql` creates `attempts`, `endpoints`, `schedules` and the sequence `endpoint_ids`; it is idempotent and **must be re-run before starting a newer binary**, because every connection prepares every statement and a missing table, column or sequence is a refusal (status 20, "the query failed"). This version added `attempts.reason` (`alter table attempts add column if not exists reason smallint not null default 0`, in the file): rows written before it have `0`, which `GET /events/:id/attempts` shows as `unrecorded` for a failed attempt. The image carries the file: `docker run --rm --entrypoint cat lexsys-hooks /usr/share/hooks/schema.sql | psql ...`.

## 3. Reading the service

### 3.1 What it writes to stderr (all of it)

The service logs at start, when it refuses to start, when the circuit breaker pauses an endpoint and when it stops. **Nothing else is logged while it runs**, not a failed attempt, not a dead letter, not a lost database connection: those are in `/metrics`, `/stats`, `delivery.seg` (a failed attempt's reason is a record of kind 14 next to its outcome) and the `attempts` table.

| line | meaning | what to do |
|---|---|---|
| `listening` | recovered, endpoints read, database connected (or not, see below), port open | nothing |
| `hooks: events.seg: cut a torn tail of N bytes at byte X (an unfinished write); M records are whole` (and the same for `delivery.seg`) | printed **after `listening`** at a start that found the unfinished record a crash leaves at the end of a log, and cut it. Nothing acknowledged is lost: only an unflushed record can be torn | nothing; if it appears without a crash, look at the disk |
| `hooks: --repair-logs: events.seg: cut at byte X: N bytes gone, among them M intact records; K records are whole. The bytes are kept in events.seg.cut-X` | the start was given `--repair-logs 1` and cut damage in the middle of the log | section 4.7: keep `events.seg.cut-X` until you are sure |
| `hooks: events.seg: damage in the middle of the log, not a torn tail. It is whole for K records (up to byte X); after that N bytes do not read as the log, and M intact records start at byte Y.` and `starting would cut the log at byte X ...` | status 19; nothing was changed | section 4.7 |
| `hooks: delivery.seg refers to event N but events.seg ends at event M ...` | status 18; nothing was changed | section 4.7 |
| `hooks: stopping on SIGTERM: nothing new is taken or started; attempts on the wire may finish for 5000 ms; a second signal ends the process at once` and `hooks: stopped: nothing was left on the wire` (or `N attempts were still on the wire at the deadline; they are made again at the next start` (`1 attempt was ...; it is made again` for one)) | the drain (section 1) | |
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
| `keys` | idempotency keys held (limit `idem-keys`, 262,144 by default; a new key beyond that while all are fresh is a `507`) |
| `replays` | replay requests waiting (limit 32) |
| `draining` | deleted endpoints with an attempt still on the wire (their slot is not free yet) |
| `filtered` | (endpoint, event) pairs passed over because the endpoint's list of event types does not want the event: final at once, no attempt, no record (3.5). Not in `attempts` |
| `history_live` | database connections for the history (0 to 2) |
| `history_written` | history rows the database accepted |
| `history_failed` | rows the database refused, or lost with the connection |
| `history_dropped` | rows dropped because the ring (256) was full or no connection was live |
| `cron_keys` | keys held by the schedule index (a quarter of `idem-keys`) |
| `events_first_id`, `events_last_id` | the oldest event retained and the newest; `first = last + 1` when everything has been dropped (the next id is `last + 1` all the same) |
| `events_segments`, `events_bytes` | segments of the events log (the last is the active one) and their size |
| `events_dropped`, `segments_dropped`, `segments_sealed` | since this start: events dropped by retention, segments deleted, segments sealed |
| `delivery_bytes`, `snapshots` | size of `delivery.seg` now; snapshots made since this start |
| `maintenance_ms_max` | the longest a retention step (a roll, a drop, a snapshot) held the loop since this start; **alert if it climbs well above 100** |
| `maintenance_errors`, `maintenance_lock_skips` | steps that failed (what was done is safe; it tries again), and steps deferred because a backup held `compact.lock` |
| `events_skipped` | events an endpoint skipped because they were dropped while it was away (a dormant endpoint resumed at the oldest retained) |

`attempts = delivered + failed + dead` (replays count too). A healthy idle service has `failed` flat. A `failed` that climbs with `delivered` flat is a receiver that is down. `history_dropped` climbing is a slow or gone database (delivery is unaffected).

### 3.3 `GET /endpoints`

`{"id","port","cursor","disabled","paused","failing_since","types","headers","secret_old_until"}` for each endpoint (`types`: its event type patterns, `[]` for every event; `headers`: the **names** of its custom headers, never a value; `secret_old_until`: Unix ms until which the previous secret is signed with as well, 0 for none). **`cursor`** is the largest event id such that every event up to it is delivered or dead for that endpoint: **lag = the newest event id (the `id` the last `POST /events` answered) minus the cursor**. An endpoint whose cursor does not move while events arrive is the one to look at. `disabled: true` means a `410 Gone` disabled it; `POST /endpoints/:id/enable` undoes it.

### 3.4 The files

`events.seg`, `events-N.seg` and `delivery.seg` are [lexsys-log](https://github.com/alpibrusl/lexsys-log) records: `len | crc32c | id | seq | fields | pairs`. The events log is a chain of segments (`events.seg`, `events-1.seg`, ...; `events.first` names the oldest that is kept; the last is the one being appended to); each begins with a header record that carries the **format version** (2; no header = format 1, the layout before retention) and the segment's number, its logical offset and the id of its first event. Event ids are dense from `events_first_id` (1 until something is dropped) and **never reused**, even when every event has been dropped (`GET /events/:id`: `404` for a dropped one); an event is the pair `event` (the body), then `typ` (its event type, when it has one), then for a keyed event `key` and `t`. A `delivery.seg` record is five integers: kind (1 delivered, 2 failed, 3 dead, 4 disabled, 5 enabled, 6 to 9 replay and its outcomes, 10 an endpoint was given a slot, 11 a slot was freed, 12 a run of failures began, 13 the circuit breaker paused an endpoint, **15 the header of the log** (retention: the format in the event field, no slot), **14 why an attempt failed**: the endpoint, the event, the attempt's number and, in the fifth field, the reason of `src/reason.ls`, plus 256 for a replay's attempt), endpoint, event, attempts, next attempt (Unix ms). A kind-14 record follows the `failed`/`dead`/`replay_failed`/`replay_dead` record it explains, in the same flush; `scripts/logcheck.py check <dir>` counts them by reason. `scripts/logcheck.py check <dir>` reads both without the service and says what is wrong with them.

### 3.5 Event types, secret rotation, custom headers (design section 35)

* **Event types.** An endpoint with a list (`types`) is sent only the events whose stored type matches (exact, `prefix.*`, or `*`; README, "Per endpoint"); an event it does not want is final for it at once and counted in `filtered`. A cursor that is far behind **and** is not moving is a receiver problem as before; a cursor that moves with `attempts` flat is an endpoint whose list wants nothing that arrives. An event written before the type was stored (every event in a log from an older version) has no type and goes only to endpoints with no list or `*`.
* **Rotating a secret.** `PATCH /endpoints/:id {"secret": "whsec_...", "keep_old_ms": 86400000}` (or `"rotate": true` to have one made). While the period lasts every delivery carries two signatures in `webhook-signature`, new first; give the receivers the new secret, then wait for the period to end (`secret_old_until` in `GET /endpoints/:id` goes to 0) or end it with `{"keep_old_ms": 0}`. If the old secret leaked, rotate **without** a period: the old one stops signing at the next attempt. The previous secret stays in the `secret_old` column after the period until the next `PATCH` of that endpoint; `{"keep_old_ms": 0}` clears it.
* **Custom headers.** Set with `"headers": {...}` (README); the values are in the `headers` column, percent-encoded. A row with a header the delivery owns, or a malformed one, is a refusal to start (status 13 or 20, naming the row).
* **The table is read as one text of at most 32 KiB** at start; the extras count. `POST /endpoints` answers `507` when its estimate says the table would not fit; a row added by hand that makes it too large is a refusal to start (status 20, "the table is too large").

### 3.5 Retention, and what it means for the disk and the start

`retention-days` (default 30; `0` keeps everything) bounds the events log: a segment is deleted, oldest first, when it is sealed, every event in it is **final at every endpoint** (delivered, or dead-lettered), and the next segment is older than the retention (and than `window-ms`). Events a **paused**, **disabled** or **dead** endpoint still needs, an event with a **replay** waiting, and an event of a cron fire not yet recorded are never dropped, however old: a paused endpoint keeps the disk from shrinking, and `GET /endpoints` shows which. Disk is therefore about `retention-days` of events plus one segment (`segment-bytes`, 64 MiB) plus the pinned events, plus `delivery.seg` (at most `delivery-log-bytes`, or four times the last snapshot, 32 MiB at the default). **After the drop the event is gone**: its body (`GET /events/:id` and a replay are `404`), and its `Idempotency-Key`: posting the same key again is a **new event** with a new id; keys inside `window-ms` still deduplicate. The attempt history in PostgreSQL is not touched (prune `attempts` yourself).

`delivery.seg` is replaced by a snapshot (the shortest log that replays to the same state) by writing `delivery.seg.tmp`, syncing it and renaming it over the log, one step of the loop. The loop is held for as long as the step takes: milliseconds for a few endpoints, 40 to 70 ms (measured) for the largest state there can be (62 endpoints with a full window of retries each: 63,488 records, 4.9 MB). `maintenance_ms_max` in `/stats` is the longest one seen.

**The start** reads the retained events (one pass, the checksums, the index and the idempotency keys of the fresh ones) and the outcomes log: its time depends on how many events are retained and how big `delivery.seg` is, not on how long the service has run (docs/design.md section 38 has the measurements). The start also finishes what a crash cut (a half-made segment, a drop whose file was left, a snapshot file left), says so on stderr, and in a directory from before retention (no headers) the first turns after the start roll, drop and replace `delivery.seg` as usual: **the first compaction of an old directory can be large** (a year of history in one snapshot step); run `--compact-now 1` once, stopped, to take it at a time of your choosing.

`hooks ... --compact-now 1` does one pass (seal the active segment, drop everything droppable, replace `delivery.seg`), prints what it did and exits 0; it takes `compact.lock` without waiting and exits 43 if a backup holds it. It is the way to see what retention will do, and the way to give back the disk of an old log before the loop would get to it.

## 4. Backup and restore

### 4.1 What there is to back up

| | where | needed? |
|---|---|---|
| the events log: `events.seg`, `events-N.seg`, `events.first` | `dir` | **yes**: every accepted event that retention has not dropped (`backup.sh` lists them) |
| `delivery.seg` | `dir` | **yes**: what was delivered, retried, dead. Lose it and every event is delivered again |
| `endpoints.conf` | `dir` | only if no database is named |
| `endpoints` table and the sequence `endpoint_ids` | PostgreSQL | **yes** with a database: the endpoints and their secrets, and the ids never to be given twice |
| `schedules` table | PostgreSQL | **yes** with a database: the cron expressions, and where each stands (`last_fired`, `next_fire`). The dump is taken before the logs, so a restored table is never ahead of the restored events log: a fire the table forgot is found by its idempotency key in the log and not made twice |
| `attempts` table | PostgreSQL | no: best-effort history. `--skip-attempts` leaves it out |
| the settings file | `/etc/hooks/` | yes, from your configuration management; it is not in the data directory |

`scripts/backup.sh` writes one directory (`hooks-backup-<UTC time>/`) with the files (`MANIFEST` format `lexsys-hooks-backup/2` lists the segments; a `/1` backup of the layout before retention restores as it was), `hooks.pgdump`, `MANIFEST` and `SHA256SUMS`, 0700, under a temporary name that is renamed only after it verified. **It holds every secret in the clear.**

### 4.2 Take one

```sh
# stopped: consistent by construction, costs the downtime of a copy
scripts/backup.sh --dir /var/lib/hooks --out /var/backups/hooks --mode stopped \
    --stop-cmd 'systemctl stop hooks' --start-cmd 'systemctl start hooks' \
    --pg-database hooks --pg-host 127.0.0.1 --pg-user hooks_backup          # password: ~/.pgpass or PGPASSWORD

# online: the service keeps running (read 4.4 first)
scripts/backup.sh --dir /var/lib/hooks --out /var/backups/hooks --mode online --pg-database hooks
```

`--mode stopped` refuses (exit 3) if any process still has a log open: it looks in `/proc`, so run it as root or as the service's user. `--start-cmd` runs even if the backup fails. Exit statuses: 0 done; 2 usage; 3 refused; 4 the copy did not verify (nothing is kept); 5 `pg_dump` failed. Needs `python3` (standard library only), `sha256sum`, `flock` (util-linux) and `pg_dump` for a database. The online mode holds `compact.lock` for the whole copy (the service skips its drops and snapshots meanwhile and tries again); it waits up to 600 s for it, and exits 3 if it cannot get it.

### 4.3 Restore

```sh
systemctl stop hooks
scripts/restore.sh --backup /var/backups/hooks/hooks-backup-20261004T120000Z --dir /var/lib/hooks \
    --pg-database hooks --pg-host 127.0.0.1 --pg-user hooks_admin            # createdb hooks first; the tables need not exist
systemctl start hooks
curl -s localhost:8080/stats; curl -s localhost:8080/events/<events_last_id of the MANIFEST>
```

`restore.sh` checks before it changes anything: `SHA256SUMS`, the format, both logs record by record (the segments in order, each beginning where the one before ends), and **that `delivery.seg` does not refer to an event the events log lacks** (4.4). It refuses a `--dir` that already has a log (exit 3) unless `--force`, which **moves** the old files to `<dir>/pre-restore-<time>/`; it refuses while the service has the files open; the database part is one transaction (`pg_restore --single-transaction`), so a failure leaves the tables as they were. Exit 4: the backup does not verify.

**What the restored service repeats.** It starts as after a crash. Every event that the events log holds is there; deliveries that `delivery.seg` records are not repeated; deliveries after the backup are made (again): **at least once, never zero**. Tested exactly, not approximately (4.5). With a database, an endpoint created after the backup is gone and one deleted after it is back; the restored `endpoint_ids` keeps an id from being reused.

### 4.4 Is the online variant safe? Yes, under conditions, and here is what they are

The honest answer first: this is an **argument from the format and the code, plus tests, not a proof**, and it stops being true if the code changes in the ways listed.

1. **While the backup holds `compact.lock`, nothing is deleted or replaced.** Before retention both files were only appended to; now a segment is deleted and `delivery.seg` is replaced, and **that was the reason to re-argue this section** (docs/retention.md section 11). The service takes a non-blocking exclusive `flock` on `compact.lock` for every drop and every replacement and, if it cannot, skips the step and tries again; the backup holds the lock for the whole copy. Rolling (a new segment after the last) is allowed meanwhile: it only adds a file after the ones the backup lists. So a copy of a live file is a valid prefix plus, at most, one torn record (or, for the last segment, a roll caught before its header: left out), which the service itself would cut at start and `backup.sh` trims (and records in the `MANIFEST`: `torn_bytes_cut_*`). If the backup is killed the kernel releases the lock. A volume snapshot, which is crash-consistent, needs none of this.
2. **Every acknowledgement follows a flush, and a delivery only concerns flushed events**, so a copy begun after an acknowledgement contains that event, and an outcome in `delivery.seg` never refers to an event that was not already durable in `events.seg`.
3. **The order of the copies is what makes the pair usable: `delivery.seg` first, the events segments second.** Then the events log is never older than what `delivery.seg` says. The other order is not a small inconvenience: **a service started on an `events.seg` shorter than its `delivery.seg` acknowledges new events under ids it believes already delivered, and never delivers them.** Measured (`tests/backup_test.py`, "INFO 9"; and by hand): a service given 8 events beside outcomes for 10 acknowledged event 6 with `202` and sent nothing. It does not notice. `backup.sh` copies in the safe order and checks the result; `restore.sh` checks again, so a pair assembled by hand or by another tool is refused (exit 4, "refers to event N but events.seg ends at event M"). **The service has the guard too, since 0.5**: it refuses to start on such a pair with status 18, before it listens and without changing either file (section 4.7).
4. **The cost is repeats, not loss.** A restore of an online backup repeats the deliveries made between the copy of `delivery.seg` and the end of the run, and an endpoint's retry state goes back to what it was at the copy.
5. **The database and the logs are two stores and no instant covers both.** `pg_dump` runs first (one snapshot), the logs after. A row the dump has and the logs lack makes the service place that endpoint at the slowest cursor (a repeat of up to 1,024 events, never a loss: `design.md` 25.3); a row deleted after the dump comes back and is placed the same way.
6. **A file-system or volume snapshot is also fine for the logs** (it is a crash-consistent image of both files at one instant, and the ordering problem disappears), as long as `fsync` is honoured below it.

If any of 1 to 3 is no longer true, withdraw `--mode online` and use `--mode stopped`. The test would show it: a mutant that copies `events.seg` first is caught by the script's own check (the backup is refused), and one without the check is caught by the restore tests.

**Not safe, whatever the mode:** copying the files with a tool that does not read them in the order above and then starting the service on the result without `restore.sh`/`logcheck.py`; restoring `events.seg` from one backup and `delivery.seg` from another.

### 4.5 What the test does (`tests/backup_test.py`, 46 s with the defaults)

Every expectation is derived from the backup's own files with the independent reader of `tests/chaos.py`.

* **A. stopped, with PostgreSQL.** 150 events; endpoint 0 delivered all of them and endpoint 1 only the first 50 (its receiver refuses the rest). Backup with `--stop-cmd`/`--start-cmd`. Then **total loss**: the directory deleted, `attempts`, `endpoints` and the sequence dropped. Restore. The tables are back row for row (secrets included), the sequence has its value, every event is served back byte for byte, a new event is id 151, **endpoint 1 receives 51 to 151 once each and endpoint 0 receives only 151**: nothing it already had is sent again.
* **B. online, under load and `kill -9` as a power cut** (the `fsync` shim of `tests/chaos.py`): backups taken continuously while 1,500 events are posted and the service is killed about every 250 ms (11 kills, 12 starts, 11 backups, 7 overlapping a kill; one run cut 43 bytes of torn tail from copies taken mid-write). Every backup holds every event acknowledged before it began, byte for byte; a sample (the first, the last, every one with a torn tail, and spread between) is restored and started: events served back, **a new event is delivered** (the restored service is not stalled), each event is delivered once to each endpoint that did not have it at the time of the backup and never to one that did, and the next id follows the last.
* **C. refusals.** A flipped byte; an `events.seg` older than its `delivery.seg` with valid checksums; a non-empty `--dir`; `--force`; a service holding the files; a torn tail (cut, and the result restores); damage in the middle of a log (refused, not silently shortened); `--stop-cmd` with `--mode online`; and the two hazards given **to the service itself**: it refuses the pair with status 18 and the damaged log with status 19, never says `listening`, and leaves the files as they were (they were "information" in the first version of this test).
* **D. retention (new).** A service that rolls every 256 KiB, drops what is final after 1.2 s and replaces `delivery.seg` at 64 KiB, killed as a power cut about every 400 ms, with 1,800 events of 1.4 KB posted and online backups taken all the time. Each backup holds every event acknowledged before it began byte for byte, **or** the event was dropped and was final at both endpoints in that backup's own `delivery.seg`; ids dense from the first retained; no torn tail; the pair consistent. A sample (including backups taken after segments were dropped, one after **all** of them had been) is restored and started with retention off: retained events served, dropped ones `404`, a new event takes the next id, and each retained event not final in the backup is delivered once to the endpoint that lacked it and never again to one that had it. This stage found two bugs in the scripts on its first runs: `backup.sh` read `events.first` before taking the lock (a drop between the two left it with no first segment), and `logcheck.py` called a tail damage when a power cut had spoiled the ends of two small records at once.
* **E. the lock and the previous format (new).** With `compact.lock` held by the test the service drops nothing and replaces nothing, counts the deferral in `maintenance_lock_skips` and still takes events; a backup waits for the lock and finishes when it is let go; the service then resumes. A directory with no headers (the layout before retention) is backed up as it is, restored, and runs: the numbering continues.
* **Mutants of the scripts that the test kills (7 of 7):** `events.seg` copied first (the backup is refused by the script's own check); the trim removed; the cross-check removed from `logcheck.py`; the "is it running" check removed; the checksum check removed from `restore.sh`; the sequence left out of the dump; the `--force` guard removed.

### 4.6 Speed, and what grows

`logcheck.py` reads a log at about 8.6 MB/s (table-driven CRC-32C in Python, measured on 20 MB), and a backup reads each log about twice and a restore twice: **a 1 GB `events.seg` is about four minutes of checking per backup**. The logs are bounded by retention now (section 3.5), so the checking time is bounded too: a backup of a directory at its defaults reads at most the retention's events plus about 100 MB. `pip install crc32c` makes it much faster (the script uses it if present) but that was not measured here.

### 4.7 Refusing corruption: what the start does, and what to do (`docs/design.md` section 34.5)

Two hazards were found by the backup work, one in the service and one in `lexsys-log`: the service started on a `delivery.seg` that outruns its `events.seg` and **acknowledged new events and never delivered them**, and one flipped byte in the middle of `events.seg` made the next start **silently cut every record after it** (500 events, one byte, 250 gone, nothing printed). The start now looks at both logs, read only, **before it changes anything**, and judges them; either hazard stops the start with a status of its own, a message, and both files exactly as they were.

**Status 18, the pair.** `delivery.seg` names an event that `events.seg` would not hold (judged after any cut the start would make: an `events.seg` that `--repair-logs` would shorten counts as shortened). Starting would give the next events ids that `delivery.seg` already records as delivered. What to do: **restore a matching pair** (`scripts/restore.sh` checks this and `scripts/logcheck.py check <dir>` says it); if there is no backup and you accept every event being delivered again (at least once), move `delivery.seg` aside (`mv delivery.seg delivery.seg.old`) and start. `--repair-logs` does not lift 18.

**Status 19, damage in the middle.** The scan stops at the first place that is not a whole record (the rules are `lexsys-log`'s: a length in range, the CRC-32C, the pairs filling the record, ids that increase). What follows is judged:

| what follows | verdict | the start |
|---|---|---|
| nothing | clean | starts |
| bytes in which no record validates, and which do not parse as two records of plausible length one after the other: an unfinished write, a page of zeros, a spoiled last record and a part of the next (what a power cut makes) | **torn tail** | cuts it, starts, and says so after `listening` |
| a record that validates, anywhere after the first bad place; or two or more records of plausible length in a row | **damage** | **refuses** (19) |

The message says how far the log is whole (`whole for K records (up to byte X)`), how many bytes cutting would take, and how many intact records start among them. The log that holds the damage is the first one named (`events.seg` before `delivery.seg`). What to do, in this order:

1. **Look at the disk** (`dmesg`, `smartctl`); a flipped byte is a sign.
2. **Restore a backup** (section 4.3) if the damage is in the part of the log you cannot lose: it is where acknowledged events are.
3. If you accept the loss, start once with `--repair-logs 1`: the bytes after the damage are first copied to `<segment>.cut-X` (`events.seg.cut-X`, or `events-N.seg.cut-X` for the last segment of a chain; X is the byte the message named; if that file exists the start refuses again, "nothing was cut"), then the log is cut at X and the start goes on. It prints `hooks: --repair-logs: events.seg: cut at byte X: N bytes gone, among them M intact records; K records are whole. The bytes are kept in events.seg.cut-X`. The next start has nothing to say. **Cutting `events.seg` loses acknowledged events, and then `delivery.seg` refers to events that are gone: status 18** (and nothing is cut). So repairing `events.seg` is for the case where you also set `delivery.seg` aside: every event that is left is delivered again. Cutting `delivery.seg` forgets outcomes after the damage: those deliveries are made again (at least once, never zero).
4. Do not leave `repair-logs = 1` in the settings file.

**A known false positive, and why it errs this way.** A crash can persist a later page of an unsynced tail and not an earlier one, leaving intact records after a hole. Nothing in a record says it was unsynced, so the start cannot tell that from bit rot and refuses (19); the records are unacknowledged (nothing after a flush's boundary was), and `--repair-logs 1` is then the right answer and loses nothing acknowledged. The tests' power cuts (`tests/chaos.py`'s shim, which truncates and zeroes the unsynced tail) never leave records after a bad one, and none was refused in the 105 + 101 + 13 + 35 kills of one run of the suite. The opposite mistake (cutting intact records silently) is the one that cost 250 events.

**`scripts/logcheck.py check <dir>`** is the reference implementation of the same rule (and a differential test, `tests/corrupt_test.py` stage E, gives 153 corruptions (150 random, 3 directed) to both and asks them to agree); it reads the logs without the service and says what is wrong.

**What this does not cover.** Only the **last** segment of the events log is judged this way (it is the only one a crash can leave a torn tail in); a damaged *sealed* segment is found by the scan that rebuilds the idempotency index, which refuses the start with status 16 and changes nothing (`logcheck.py check` names the segment). A log that is whole but wrong (a record with a valid CRC and wrong content) is not detected. A flipped byte inside the **last** record is indistinguishable from a torn one and is cut (and said). `lexsys-log`'s `recover` is unchanged and still cuts whatever it is given: the service no longer gives it anything but a judged log.

## 5. Upgrading

1. **Back up, stopped** (4.2). The backup is also the rollback.
2. Read the release notes for a schema change. `psql -f sql/schema.sql` is idempotent: **run it before the new binary starts** (an older deployment that lacks `endpoint_ids` or a column is refused with status 20). **The version with event types, rotation and custom headers adds four columns to `endpoints`** (`types`, `headers`, `secret_old` and `secret_old_until`, every one `not null` with a default, so every row keeps meaning what it meant): `psql -f sql/schema.sql` adds them (`alter table endpoints add column if not exists ...`), a database that has them is unchanged, and a new binary started before it is refused with status 20 (`the query failed`). Nothing else needs doing: an endpoint with the defaults has no list (every event), no previous secret and no headers.
3. Stop, replace `/opt/hooks/bin/hooks` (or the image tag), start. Check `listening`, then `GET /stats` and a canary event.
4. **Rolling back** is not "start the old binary": the new binary may have written records the old one does not know (since event types: every event it accepted has a `typ` pair, and an older binary refuses an `events.seg` with one, status 16, nothing lost; the extra columns are ignored by an older binary, but a row that uses them would then lose its list, headers and previous secret), and the old one refuses a `delivery.seg` it does not understand (status 15; 16 for `events.seg`). **A `delivery.seg` written by this version holds records of kind 14 (why an attempt failed) as soon as one attempt has failed, and the previous version refuses it (status 15).** The database needs `attempts.reason` before this version starts (section 2), and the previous version is happy with the extra column. If the new version only ran briefly and wrote nothing new, the old binary starts; if it refuses, **restore the backup from step 1** and accept losing what was accepted since, or keep the new binary.
5. **The logs carry a format version since retention** (docs/retention.md section 4): the first record of every events segment and of `delivery.seg` says it (now 2; a file with no header is format 1, the layout before). A version this build does not know is a refusal that names the file: status 40 (events), 41 (`delivery.seg`), 42 (a hole in the chain), and nothing is written. **Upgrading from a build before retention is automatic and one way**: format 1 files are read as they are and the first roll or snapshot writes headers. **The build before retention cannot start a directory this build has written**: it refuses it (status 16 for the events log, 15 for `delivery.seg` once there are endpoints) because the header is a record it does not understand. That is by design and not by luck, but it means step 4 is not optional: **keep the backup of step 1 until you are sure**. A version header in `lexsys-log` itself is still a proposal (`docs/lexsys-log-retention.md`).

`docker`: pull or build the new tag, `docker stop`, `docker run` with the same volume. The volume is the data directory; nothing else is state.

## 6. When it goes wrong

| what you see | what it is | what to do |
|---|---|---|
| the process is gone (OOM, `kill -9`, a crash, a power cut) | normal | restart it (systemd does). It recovers both logs, cuts a torn tail, and repeats what was on the wire. Nothing acknowledged is lost (`tests/chaos.py`) |
| `POST /events` answers `503 {"error":"the event could not be stored"}` for every request, `/healthz` says `200` | the log is **broken**: a write or flush failed (the disk is full, an I/O error) and by design it is not retried, because after a failed write the file's contents are unknown. **Verified on a 96 KB tmpfs: 634 events accepted, then 503 for each of the next 866 requests** (the ones before it are safe: only acknowledged after a flush) | free space or fix the disk, then **restart**: it cut the torn tail (144 bytes there; it now says so after `listening`) and accepted event 635, with 634 intact. A restart is the only way back. `GET /readyz` says it (`"check":"events_log"`) and `hooks_ready` is 0, so a load balancer takes the instance out before the clients see the 503s (`tests/ready_test.py`, on a 256 KiB tmpfs) |
| `GET /readyz` is `503` with `"check":"data_dir"` | the data directory did not take a 1-byte write at the last probe (once a second): the disk is full, or read-only. No log is broken yet, because none has been written to | free space; it is `200` again within a second or two, **without a restart** (tested: another process fills the disk, then frees it) |
| `GET /readyz` is `503` with `"check":"database"` | a database is named and neither of the service's connections to it is live | delivery is unaffected; the history is dropped meanwhile. Fix the database, then **restart**: a lost connection is never reopened (a reopening needs a login that blocks the loop; `docs/design.md` section 34.1). The container's health check restarts it |
| status 11 at start | the port is taken | find who has it |
| status 10 or 12 | a log cannot be opened | permissions on the data directory (`StateDirectory`, uid 10001 in the container), a read error. Run `scripts/logcheck.py check <dir>` |
| status 13 | an endpoint line or row is invalid | the message names it |
| status 20 | the database is not reachable, the password is wrong, a table or the sequence is missing | `psql` with the same settings; `psql -f sql/schema.sql`. It is restarted by systemd until the database is back |
| status 15 or 16 | the logs are from a newer version, or damaged | section 5 step 4; else `logcheck.py check` |
| **status 18** | `delivery.seg` refers to an event `events.seg` does not hold: an older events log beside a newer delivery log (a restore done by hand), or an `events.seg` that lost its tail | restore a matching pair (section 4.7). It used to start anyway and acknowledge events it never delivered |
| **status 19** | damage in the middle of a log; the message says where, how many bytes and how many intact records | section 4.7: look at the disk, restore a backup, or `--repair-logs 1` once. It used to cut silently |
| the line `cut a torn tail of N bytes` after `listening` | a crash left an unfinished write; it was cut | normal after a crash or a `kill -9`. Nothing acknowledged is in it |
| the process ended with status 143 or 130 (or killed by `SIGTERM`/`SIGINT`) | a second signal during a drain, or a signal before the loop started | harmless: section 1 |
| an endpoint's cursor does not move, `failed` climbs | the receiver is down, slow or refusing | it is retried on the schedule (5 s, 5 min, 30 min, 2 h, ...) and then dead-lettered; its events are never lost, `POST /events/:id/replay/:endpoint` sends one again. A `410` disables it |
| an endpoint is far behind or dead | each endpoint has its own window and scan position (`production.md` 0.1, done): a dead one holds nobody else back | bring the receiver back; or let the circuit breaker pause it (`breaker-days`) and `POST /endpoints/:id/enable` later; or `DELETE /endpoints/:id` |
| `hooks_history_rows_total{result="dropped"}` / `"failed"` climbs (`history_dropped`/`history_failed` in `/stats`) | the database is slow, gone or refuses | delivery is unaffected. Restart to reconnect (a lost connection is never reopened). The history has holes: that is its contract |
| `POST /events` answers `507` | `idem-keys` idempotency keys are held and all are fresh (inside `window-ms`) | raise `idem-keys` (memory: resident only as keys are held, 30 MB for 200,000), or shorten `window-ms`; it is not a log problem |
| `POST /events` answers `413` | the event is 65,500 bytes or more (less 11 and the length of its stored type, and less with a key) | the client's |
| start is slow | the start reads the retained events (and rebuilds the idempotency index from them) and replays `delivery.seg` | the time is bounded by the retained events and `delivery-log-bytes` (section 3.5, design.md section 38); shorten `retention-days` or lower `delivery-log-bytes` if it is too long |
| the disk does not shrink | an endpoint is paused, disabled or dead (its events are pinned), or a replay is waiting, or the segment is younger than the retention (`max(retention-days, window-ms)`) | `GET /endpoints` and `/stats`: `events_first_id` against the lowest `cursor`; section 3.5 |
| status 40, 41 or 42 | a log in a format this version does not know, or a hole in the chain of segments (a file removed by hand?) | nothing was changed. Use the version that wrote it, or restore a backup; `logcheck.py check <dir>` names the segment |
| status 30 to 35 at start | `production = 1` and something unsafe (section 2.1); the line on stderr says which setting or path | fix the setting or `chmod 700` the directory and `chmod 600` its files, and start with `umask 077`. systemd does not restart these |
| a client gets `401` or `403` | a missing, wrong or too-narrow token (the body says which: `a valid bearer token is required`, `this route needs the read token (or the admin token)`) | give it the token of the scope it needs; `403` with `management is off` means the service has no `admin-token` |
| the admin token leaked | whoever has it can aim the service at any public address (a private one only with `allow-private-hosts`) | change `admin-token`, restart; `GET /endpoints` and check where each points. The signing secrets are in the table: rotate them with `PATCH ... "rotate": true` if the database was readable too |
| the disk of the **backups** fills, or a backup fails | `backup.sh` keeps nothing it did not verify | nothing partial is left (`.hooks-backup-*.partial` is removed); fix and re-run. Alert on the age of the newest `hooks-backup-*` |
| you must move to another host | a restore | section 4.3 on the new host with the newest backup; nothing else is state |

## 7. Known limits (from `production.md` and the README; none is hidden)

One process, one thread, one core (roughly 7 to 10k deliveries a second measured). At most 62 endpoints. The logs are bounded by retention (0.2, built): disk and start time follow `retention-days`, not history, but an event a paused, disabled or dead endpoint still needs is never dropped, a snapshot of the largest state holds the loop for 40 to 70 ms, and the first compaction of a directory from before retention can be large (3.5). (The start reads `delivery.seg` once more than before, for the pair check of section 4.7.) Every route can be given a token, but until the three tokens are set the routes of a scope without one are open (`/healthz` and `/readyz` always are); `production = 1` is what insists on them (section 2.1); no TLS, no audit log, a token changes only with a restart (0.3). No `https` endpoints. A host *name* blocks the loop while it resolves (use IP literals). No jitter in the retry schedule. **A lost database connection is not reopened**, so `/readyz` stays `503` (`database`) until a restart. The stop leaves the listening socket open until the process exits, and a drain longer than `TimeoutStopSec` is a `SIGKILL` (safe, a repeat of what was on the wire). The counters of `/metrics` are since the start. A crash that leaves intact records after a hole in a log's unsynced tail is refused as damage (4.7). At most `idem-keys` (262,144 by default) fresh idempotency keys. An event of 65,500 bytes or more is refused. Secrets (and custom header values, and the previous secret of a rotation) are in the clear in the table and in `endpoints.conf`: the database is the trust boundary (section 2.1). The authority report says `bounded: false` for one foreign symbol, libc's `statx` (the production profile's mode check; lex-sys#243), and CI pins it (`docs/authority.json`). No log format version (P2). No soak test of 24 hours has been run.

## 8. Hardening notes (`deploy/hooks.service`)

* `systemd-analyze security --offline`: exposure 1.3 (OK). The unit drops all capabilities, makes the system read-only except `StateDirectory`, hides `/home` and `/proc` of others, gives a private `/tmp` and `/dev`, allows only `AF_INET`, `AF_INET6` and `AF_UNIX`, denies write-and-execute memory, and filters system calls to `@system-service` less `@privileged` and `@resources`.
* **Measured, not guessed:** `strace -f` over a workload with PostgreSQL, a settings file, 50 events, `GET`s and a `POST /endpoints` showed 34 distinct system calls (one of them the `execve` that strace's own start causes), **all** inside `@system-service`; the only executable mapping is libc's own (file-backed), so `MemoryDenyWriteExecute` holds; the only files opened are libc, `ld.so.cache`, the settings file, `/dev/urandom` and the two logs. The stop adds no call of its own beyond `rt_sigprocmask` and `signalfd4` once at the start (the claim on `SIGINT` and `SIGTERM`; no `rt_sigaction`, so no disposition is touched and no handler exists), a `read` of the signal descriptor a turn, and one more `epoll_ctl`; `signalfd4` is in `@signal`, which `@system-service` allows.
* **Not covered by that measurement:** a host *name* as an endpoint (`allow-private-hosts 1`) goes through the resolver, which may open NSS libraries and a socket; `AF_UNIX` is left open for that. If a name fails to resolve under the unit, look at `journalctl` for `SIGSYS`/`EPERM` and relax `SystemCallFilter` first.
* `Restart=on-failure`, with `RestartPreventExitStatus` for the refusals a restart cannot fix (section 1, the production profile's included), and `StartLimitBurst=10` in 5 minutes.
* `StateDirectoryMode=0700` and `UMask=0077`: what `production = 1` insists on (section 2.1). The service calls `statx` (read-only, on the data directory and its three files) to read their modes, which the unit's filter allows (`@system-service`).

## 9. Releases

`LEX_SYS=/path/to/lex-sys scripts/release.sh [--version V]` builds with `lex-sys build` (which refuses any compiler but the commit `lex-sys.toml` pins), refuses a dirty tree, and writes `dist/hooks-<version>-linux-<arch>/`: the tarball (`bin/hooks`, `deploy/`, `scripts/`, `sql/`, `docs/`, `README.md`, `LICENSE`, `Dockerfile`, `SBOM.json`), `hooks-<version>-linux-<arch>.sbom.json` and `SHA256SUMS` (`sha256sum -c SHA256SUMS`). The tarball is deterministic for a given binary (sorted names, owner 0, mtime from the last commit, `gzip -n`).

**Is the binary reproducible? Measured: yes, after one normalization.** Four builds of the same sources (three directories on one host, one inside the `Dockerfile`, with a separately built compiler) gave four different files. They differ in exactly one place: a `FILE` symbol with a temporary name that holds a process id (`lex-sys-llvm-<pid>-0.ll`), and the build-id note derived from it. After `strip --strip-all --remove-section=.note.gnu.build-id` all four are the same `fd3c914f9cc8a815...`, 309,232 bytes. That is what `release.sh` ships (`--keep-symbols` ships what the compiler wrote) and what the image holds, so **the binary in the tarball and in the image have the same hash when the toolchain is the same Ubuntu 24.04 (clang 18.1.3, gcc 13.3, binutils 2.42)**. This is measured on one OS and architecture; it does not say a different clang gives the same bytes. For the compiler: the pid in a symbol name is a small thing to fix in lex-sys.

**The SBOM is a stub and says so** (`"complete": false`, and a `not_listed` list in the file): the compiler (the pin, what `lex-sys --version` reports, whether they match, and the `clang` and `cc` that shaped the binary), the std (compiled into the compiler: its version *is* the compiler commit; the `std.*` modules the sources import), the two libraries pinned by commit, and the dynamic libraries from `ldd` with hashes and, where `dpkg` knows, the package and version **on the build host** (the target must have this glibc or newer). It does not list the Rust toolchain and crates behind the compiler, LLVM, the base image's packages, or any signature. It is not CycloneDX or SPDX.

**Not done:** signing (anyone who can replace the tarball can replace `SHA256SUMS`), a stable download URL (CI keeps a binary as a run artifact for 90 days), a multi-architecture build.

## 10. The container

`docker build -t lexsys-hooks .` (about 1 min 40 s cold, 9 s with the compiler layer cached). Two stages build; the runtime stage holds the base image (87.6 MB for `ubuntu:24.04`), `tini` (1.4 MB), the binary (0.3 MB) and `sql/schema.sql`. It runs as uid 10001, with `/var/lib/hooks` as a volume, `EXPOSE 8080` and a `HEALTHCHECK` on **`GET /readyz`** (it was `/healthz` until the readiness route existed: a full disk or a lost database connection now makes the container unhealthy, and both are repaired by a restart; the health check was changed in the source and **the image was not rebuilt or run here**). The compiler stage reads the pin from `lex-sys.toml`, builds the compiler from that commit's `Cargo.lock` with the toolchain its `rust-toolchain.toml` pins (rustup-init 1.28.2, checked against its checksum file from the same server), and `lex-sys build` fetches and checks the two libraries. Not pinned, and said so in the file: the base image (a tag; use `--build-arg BASE=ubuntu:24.04@sha256:...`) and the apt packages. **Built and run here by hand, not by CI**; the sandbox needed a proxy CA, which is not part of the file (`--build-arg BASE=` pointed at a base image that has it).

The entry point is `deploy/hooks-entrypoint.sh`: a `umask 077`, then the service (`exec`, so `tini` signals the service itself); `/var/lib/hooks` is `0700`. A bind mount for `/var/lib/hooks` must be owned by uid 10001, and `0700` if the service is to run with `production = 1` (section 2.1). **This image has not been built since that change** (no Docker in the sandbox the change was made in). The image's settings file has only `port` and `dir`; mount your own over `/etc/hooks/hooks.conf` (mode 0600, owner 10001) rather than put a token or a password on the command line. If you change the port with a flag, tell the health check: `HOOKS_HEALTH_PORT`. These two variables are read by the health check only; the service reads none.

## 11. What depends on what is not built

| here | needs | status |
|---|---|---|
| `GET /readyz`, `/metrics`, alerting on lag and dead letters | 0.4 | **built** (section 1; `tests/ready_test.py`, `metrics_test.py`) |
| a drain on `SIGTERM`, `TimeoutStopSec` as its deadline | 0.4 | **built** (section 1; `tests/stop_test.py`); not run under a real systemd |
| the reason a delivery attempt failed, in the log | 0.4 | **built** (kind 14, `attempts.reason`, `/metrics`; `tests/reason_test.py`) |
| refusing a `delivery.seg` that outruns its `events.seg`, and a log with damage in the middle (4.7) | 0.5 | **built** (statuses 18 and 19, `--repair-logs`; `tests/corrupt_test.py`, `backup_test.py`). The scripts are still the guard for a backup |
| tokens for ingest and read, `production = 1` | 0.3 | **built** (section 2.1); open: no TLS, no audit, no rotation without a restart |
| a dead endpoint not stopping the others | 0.1 | **built** (design.md section 31) |
| bounded logs, a snapshot of the outcome state, a bounded start time | 0.2 | **built** (docs/retention.md, design.md section 38); `--mode online` backup re-argued (4.4) and tested under `kill -9` |
| reopening a lost database connection | (new) | **not built**: needs a login that does not block the loop (`lexsys-pg`); design.md section 34.9 |
| a log format version and a refusal that names it | P2 | **built** for this service's two logs (status 40 to 42); in `lexsys-log` itself a proposal (`docs/lexsys-log-retention.md`) |
| a soak test of 24 hours, a capacity page | P2 | not done |
