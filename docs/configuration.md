# Configuration

Every setting, where settings come from (a file, flags, or both), and the exit status of a start that ends. The format of `endpoints.conf` is in `endpoints.md`.

## Settings

Settings come from a file (`--config`), from flags, or both (`design.md` section 20). `--port` and `--dir` are required; the rest have defaults:

| setting | default | what it is |
|---|---|---|
| `port` | (required) | the TCP port, 1 to 65535 |
| `dir` | (required) | the data directory: the logs, and `endpoints.conf` |
| `schedule` | `5000,300000,...` (nine delays, to a day) | retry delays in ms, comma separated; after the last, a dead letter |
| `deadline-ms` | `2000` | how long one delivery attempt may take |
| `window-ms` | `86400000` | how long an idempotency key is remembered |
| `pg-host` | (none) | a PostgreSQL: the endpoints are read from it and the attempt history written to it; without it the endpoints are `endpoints.conf` and there is no history (`design.md` section 24) |
| `pg-port`, `pg-user`, `pg-database`, `pg-password` | `5432`, `hooks`, `hooks`, none | how to reach it. Put a password in the settings file, not on the command line |
| `pg-backoff-min-ms`, `pg-backoff-max-ms` | `100`, `5000` | the wait after a failed attempt to connect starts at the first (1 to 600,000) and doubles up to the second (1 to 3,600,000, not below the first); per connection (`design.md` section 37) |
| `pg-attempt-ms` | `5000` | the longest one attempt (dial, login, prepare the statements) may take (1 to 600,000); a server that accepts and says nothing costs this much |
| `pg-request-ms` | `10000` | the longest a request may wait with no byte coming back before its connection is given up and remade (0 to 3,600,000; `0` never). Longer than the slowest query: the service itself answers a held request after 5 s |
| `pg-start-wait-ms` | `30000` | how long the service may run without having read its endpoints from the database before it ends with status 20 (0 to 86,400,000; `0` waits for ever) |
| `allow-private-hosts` | `0` | `1`: an endpoint's host may be an address in a private, loopback, link-local or reserved range, and a name may resolve to one. With `0` (the default) an address must be public in `endpoints.conf`, in the table and in `POST /endpoints`, and **a name must resolve to public addresses only, which is judged at every attempt** (`ssrf_refused`; design.md sections 26 and 40). A host that is neither an address nor a name is refused either way |
| `admin-token` | (none) | the bearer token of the **admin** scope: everything that changes configuration or state (create, change and delete endpoints, enable, replay, the schedules), and it also does what the other two do. 8 to 255 visible characters; without it `POST`, `PATCH` and `DELETE /endpoints` and the schedules are a `403`. Anyone who has it can choose where the service sends requests, within what `allow-private-hosts` allows (by default public addresses only), so keep it secret and put it in the settings file, not on the command line |
| `ingest-token` | (none) | the token of the **ingest** scope: `POST /events` and so its idempotent retries; same rule as `admin-token`. Without it `POST /events` is open |
| `read-token` | (none) | the token of the **read** scope: `GET /events/:id`, `/events/:id/attempts`, `/endpoints`, `/endpoints/:id`, `/stats`, `/config`, `/metrics`; same rule. Without it those are open (in production: they need the admin token) |
| `production` | `0` | `1`: refuse to start unless `admin-token` and `ingest-token` are set and different from each other and from `read-token`, `allow-private-hosts` is `0`, and the data directory and its files cannot be read or written by the group or by others. One exit status for each cause (30 to 35), and a line on stderr that names the setting or the path ("Securing it" below) |
| `breaker-days` | `5` | pause an endpoint whose every attempt has failed for this many days (0 to 36,500; `0` is off). It is disabled as a `410` disables it, and `GET /endpoints` says `"paused":true`; its events wait in the log and are sent when a person enables it. Counted from the first failed attempt after a delivery, checked when an attempt fails (`design.md` section 31) |
| `history-days` | `30` | delete the rows of the `attempts` table (the history, with a database) older than this many days, by the time of the attempt, ten thousand at a time: the first batch ten seconds after the database is there, the next a second after a full one, otherwise every ten minutes; `0` keeps them for ever (0 to 36,500). `GET /stats` says how many (`history_pruned`). Run `sql/schema.sql` first: it adds the index the batches use |
| `retention-days` | `30` | drop events that are **final at every endpoint** (delivered or dead-lettered) and older than this many days, a whole segment at a time; `0` keeps them for ever. An event a paused, disabled or dead endpoint, or a waiting replay, still needs is never dropped. After the drop `GET /events/:id` and a replay of it are a `404` and its `Idempotency-Key` is forgotten (retention.md, design.md section 38) |
| `segment-bytes`, `delivery-log-bytes` | `67108864`, `33554432` | the events log is sealed into a new segment at this size (256 KiB or more), and `delivery.seg` is replaced by a snapshot of the delivery state at this size (64 KiB or more) or four times the last snapshot |
| `idem-keys` | `262144` | how many idempotency keys the index holds (16 to 4,194,304; resident only as keys are held: 30 MB for 200,000); a key leaves when its `window-ms` has passed or its event is dropped |
| `compact-now` | `0` | `1`: do one maintenance pass (seal, drop what may be dropped, replace `delivery.seg` by a snapshot), print what was done and exit; for an operator, and it takes the same lock as the loop |
| `retry-jitter` | `10` | how far each retry delay is moved, up or down, in percent of itself (0 to 50; `0` is exactly the schedule). A function of the endpoint, the event and the attempt, fixed when the failure is recorded (`design.md` section 39.3) |
| `retention-ms`, `compact-kill-at` | `0`, `0` | **test knobs** (`tests/retention_test.py`): retention in ms instead of days; stop at a numbered step (1 to 64) of a compaction and wait there, writing `killpoint` in the data directory, so that a test can kill the service at that step. Not for use outside the tests |
| `endpoint-concurrency` | `8` | the most attempts one endpoint has in flight together (1 to 8); an endpoint's own `"concurrency"` replaces it (`design.md` section 39.4) |
| `endpoint-rate` | `0` | the most attempts one endpoint starts a second (0 to 100,000; `0` is no limit); an endpoint's own `"rate"` replaces it. A held-back event waits, it does not fail (`design.md` section 39.4) |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the database and exit (needs `pg-host`; no `port` needed) |
| `cron-catchup` | `1` | `1`: a schedule that missed fires while the service was stopped fires once for them; `0`: it skips them (`design.md` section 32) |
| `rotation-grace-ms` | `86400000` | how long the previous secret is still signed with after `PATCH /endpoints/:id` with `"keep_old": true` (1 to 2,592,000,000; `keep_old_ms` names another period for one change) |
| `dns-server` | the first IPv4 `nameserver` of `/etc/resolv.conf`, port 53 | `ip` or `ip:port`: the name server that resolves the names of endpoints, asked over TCP (`design.md` section 40) |
| `tls-ca-file` | (the system's trust store) | a PEM file of certificates; with it an `https` endpoint's chain must lead to one of **these** and not to the system's. A file that cannot be read or holds none is a refusal to start (status 21) |
| `tls-resume` | `1` | `1`: keep a TLS session for each `https` endpoint and resume it; `0`: a full handshake and a full verification every time |
| `cron-seconds` | `0` | `1`: a schedule's expression has a leading *seconds* field (six fields). A test mode, so that a test sees a fire in seconds instead of a minute; do not switch it with schedules in the table (a row of the other kind does not parse and is parked) |
| `stop-deadline-ms` | `5000` | how long the attempts on the wire may take to finish after `SIGTERM` or `SIGINT` (0 to 3,600,000; `0` does not wait). Keep systemd's `TimeoutStopSec` and `docker stop -t` above it (`design.md` section 34.4) |
| `repair-logs` | `0` | `1`: a log with damage in the middle is cut at the first bad byte, after the cut bytes are copied to `<log>.cut-<offset>`, instead of the start being refused (status 19). Give it once on the command line, after reading the refusal (`design.md` section 34.5) |

```sh
build/hooks --port 8080 --dir /tmp/hooks-data --schedule 100,200,400,800   # four retries, then a dead letter after the fifth attempt
build/hooks --port=8080 --dir=/tmp/hooks-data --deadline-ms=500 --window-ms=60000
build/hooks --config /etc/hooks/hooks.conf --window-ms 60000   # the file, then the flag over it
```

```
# hooks.conf: one `key = value` a line, `#` on a line of its own
port = 8080
dir = /var/lib/hooks
schedule = 1000,5000,30000
```

The **last source that names a setting wins**: the defaults, then the file, then the flags in the order written (`--config`
may stand anywhere among them; a second one replaces the first). Anything else is refused before the service listens or
writes: exit 2 and a line on stderr that names the argument, or the line of the file. `GET /config` says what is in force.
There are no environment variables and no positional arguments (`hooks <port> <dir> ...` is refused).

## Exit statuses

**Exit statuses of a start that ends** (the message is on stderr; the full list with what to do is in `runbook.md` section 1): `0` finished (`--import-endpoints`, or a drain); `2` a setting was refused; `5` `SIGINT` and `SIGTERM` could not be claimed; `10`/`12` `events.seg`/`delivery.seg` could not be opened; `11` the port is taken; `13` an endpoint line or row is invalid; `14` the retry schedule does not parse; `15`/`16` a log this version does not understand; `17` an endpoint could not be given a slot; **`18` `delivery.seg` refers to an event that `events.seg` does not hold** (an older events log beside a newer delivery log: the service would acknowledge new events and never deliver them), **`19` damage in the middle of a log** (not a torn tail: the message says where the log is whole to, how many bytes the cut would take and how many intact records are among them); `20` the database could not be read (also `--compact-now 1` with a database it cannot read the endpoints from within `pg-start-wait-ms`: nothing is changed); `21` the TLS trust store could not be loaded (`tls-ca-file`); `30` to `35` the production profile refused an unsafe setting or mode (the table in [security.md](security.md)); `40`/`41` a log in a format this version does not know (a newer one), `42` a hole or break in the chain of events segments, `43`/`44` `--compact-now` found the lock held, or a step failed. A refusal with 18 or 19 leaves both logs exactly as they were. A single torn record at the end, which is what a crash leaves, is still cut at start, and now said (after `listening`).
