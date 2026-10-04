# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys): you `POST` it an event, it stores the
event durably, and it delivers the event, **signed**, to every subscribed endpoint, **at least once**, retrying on a schedule
and keeping what it could not deliver as a dead letter.

It keeps its two logs in [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and talks to PostgreSQL through
[`lexsys-pg`](https://github.com/alpibrusl/lexsys-pg). No `Ffi`, no `unsafe`: the authority report (`lex-sys authority`) names what
the program can do.

## Status

**Working:** durable ingest (`202` only after the flush that covers the event; requests that arrive together share one flush), delivery to several endpoints with [Standard Webhooks](https://www.standardwebhooks.com) signatures checked against the reference library, retries on the Standard Webhooks schedule, dead letters, and every outcome (with the time of the next attempt) surviving a crash. A slow, silent or unreachable endpoint costs the others almost nothing: delivery attempts do not hold the loop (up to 64 in flight, a state machine each), so ingest stays at a median of 2.3 ms and healthy endpoints see their deliveries within milliseconds ([`docs/design.md`](docs/design.md) section 16). That used to hold only until the unreachable endpoint was 1,024 events behind, when every endpoint stopped with it (measured: beside a dead endpoint a healthy one stopped at event 1,024 of 3,000, `docs/design.md` section 29). **Each endpoint now reads the events log from its own cursor and is bounded only by its own window** (same probe: 3,000 of 3,000; section 31), and a **circuit breaker** pauses an endpoint whose every attempt has failed for 5 days (`breaker-days`, 0 turns it off): its events wait in the log until `POST /endpoints/:id/enable`. A client may send an `Idempotency-Key` ([section 17](docs/design.md)); an event can be replayed to one endpoint or all (section 23); a `410 Gone` disables an endpoint (section 22); with PostgreSQL the endpoints are a table, every ended attempt is a row, and an endpoint can be created, changed and deleted without a restart (`POST`, `PATCH`, `DELETE /endpoints`) behind an admin token (sections 24 and 25). **Not built:** filtering by event type, two signatures during a secret rotation, jitter in the retry schedule, TLS (`https`) endpoints.

## Requirements

- The **lex-sys** compiler at the commit `lex-sys.toml` names (`[package] lex-sys`); `lex-sys build` refuses any other. It needs `clock_unix_ms` (the signing timestamp; lex-sys PR #190),
  `tcp_connect_start` (attempts that do not wait; #191), a lock with an origin (#192), the project file (#193), and `std.hmac`, which signs
  every delivery and replaced this repository's own HMAC (#229; [`docs/design.md`](docs/design.md) section 30).
- `git`: [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and lex-sys's `http-server` are not cloned by hand; they are dependencies in `lex-sys.toml`, pinned to a commit each, and `lex-sys build` fetches and checks them.
- Rust, to build the compiler; `gcc`, to build the small `fsync` shim the crash tests use.
- To run the tests: `python3` and `pip install standardwebhooks` (the independent implementation signatures are checked against).

## Quick start

```sh
git clone https://github.com/alpibrusl/lex-sys                          # the compiler, and nothing else to clone
git clone https://github.com/alpibrusl/lexsys-hooks && cd lexsys-hooks

REV=$(sed -n 's/^lex-sys *= *"\(.*\)"/\1/p' lex-sys.toml)                 # the compiler these sources were written for
(cd ../lex-sys && git fetch -q origin && git checkout "$REV" && cargo build --release -p lex-sys)
export LEX_SYS=$PWD/../lex-sys/target/release/lex-sys

lex-sys build                         # installs the two libraries in lex-sys.toml, then builds build/hooks and build/sign_probe
scripts/build.sh                      # the same, and the fsync shim the crash tests preload

# one endpoint: <id> <host> <port> <secret>
mkdir -p /tmp/hooks-data
echo "0 127.0.0.1 9000 whsec_$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(24)).decode())')" \
  > /tmp/hooks-data/endpoints.conf

build/hooks --port 8080 --dir /tmp/hooks-data --allow-private-hosts 1 &   # the receiver is on 127.0.0.1: see `allow-private-hosts`
curl -XPOST -d '{"type":"user.created","id":7}' localhost:8080/events      # {"id":1}, after the flush
```

With nothing listening on port 9000 the event is stored and retried (5 s, 5 min, ...). To watch a delivery arrive, run the
receiver below first.

## A database

With `--pg-host` the service uses PostgreSQL for two things (`docs/design.md` section 24):

* **the endpoints**: they are the `endpoints` table, read once at start. `endpoints.conf` is **not read** when a database is named,
  and a database that cannot be read is a refusal to start (status 20, with what failed), not a guess: a stale list would deliver to the
  wrong receivers. The start waits while the database is silent.
* **the history**: a row for every delivery attempt that ends (the receiver's status, the outcome, the attempt's number, when and how
  long), read back by `GET /events/:id/attempts`. The log files stay the truth about delivery, so the history is **best effort**: the
  service delivers while the database is slow or gone, and counts the rows it could not write (`/stats`). A connection that is lost is
  not reopened until the service is restarted.

```sh
createdb hooks && psql hooks -f sql/schema.sql
build/hooks --dir /var/lib/hooks --pg-host 127.0.0.1 --pg-user hooks_rw --import-endpoints 1   # copy endpoints.conf into the table, once
build/hooks --port 8080 --dir /var/lib/hooks --pg-host 127.0.0.1 --pg-user hooks_rw
psql hooks -c "select * from attempts where event = 41 order by endpoint, attempt"
```

The import is one transaction, leaves a row whose id is already there as it is, and refuses a file with a bad line without importing any
of it. The `endpoints` table holds the secrets in a form the service can sign with: give it the permissions of a secret.

## A prebuilt binary

Every CI run that passes keeps the service as an artifact of the run (Actions, the run, "Artifacts": `hooks-linux-x86_64-<commit>`,
a zip of `hooks-linux-x86_64` and its `.sha256`). It is built by the compiler this commit pins, for Linux x86-64 with the glibc of
`ubuntu-latest` or newer, and the zip loses the executable bit:

```sh
unzip hooks-linux-x86_64-*.zip && sha256sum -c hooks-linux-x86_64.sha256 && chmod +x hooks-linux-x86_64
./hooks-linux-x86_64 --port 8080 --dir /var/lib/hooks
```

A run's artifacts expire (90 days by default); a release with a stable URL is not built.

## Examples

**A receiver** (`receiver.py`): prints the three Standard Webhooks headers and the body of every delivery, and answers `204`.

```python
import http.server

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        print(self.headers["webhook-id"], self.headers["webhook-timestamp"],
              self.headers["webhook-signature"], body.decode(), flush=True)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

http.server.HTTPServer(("127.0.0.1", 9000), H).serve_forever()
```

```
$ python3 receiver.py &
$ curl -XPOST -d '{"type":"user.created","id":7}' localhost:8080/events
{"id":1}
evt_1 1791028548 v1,5Zm7wWgRWI1/Wv9pxlBJoAfhaZGx7dUmeUGdTj0hWd8= {"type":"user.created","id":7}
$ curl localhost:8080/events/1
{"id":1,"event":{"type":"user.created","id":7}}
$ curl localhost:8080/stats
{"endpoints":1,"attempts":1,"delivered":1,"failed":0,"dead":0,"keys":0}
```

**Verify a delivery** the way a subscriber would, with the reference library (`pip install standardwebhooks`):

```python
from standardwebhooks import Webhook
Webhook("whsec_...").verify(body, {"webhook-id": "evt_1", "webhook-timestamp": "1791028548",
                                   "webhook-signature": "v1,5Zm7..."})   # raises if the signature or timestamp is wrong
```

**Several endpoints.** `endpoints.conf`, in the data directory, takes one endpoint a line (`#` comments and blank lines are
ignored; at most 62 endpoints, each with an id of up to six digits, written and never counted). It is its own file because it holds the secrets: give it the permissions secrets need, and keep it out of the settings.

```
# <id> <host> <port> <secret>
0 127.0.0.1 9000 whsec_...
1 127.0.0.1 9001 whsec_...
```

**Settings**, from a file, from flags, or both (`docs/design.md` section 20). `--port` and `--dir` are required; the rest have defaults:

| setting | default | what it is |
|---|---|---|
| `port` | (required) | the TCP port, 1 to 65535 |
| `dir` | (required) | the data directory: the logs, and `endpoints.conf` |
| `schedule` | `5000,300000,...` (nine delays, to a day) | retry delays in ms, comma separated; after the last, a dead letter |
| `deadline-ms` | `2000` | how long one delivery attempt may take |
| `window-ms` | `86400000` | how long an idempotency key is remembered |
| `pg-host` | (none) | a PostgreSQL: the endpoints are read from it and the attempt history written to it; without it the endpoints are `endpoints.conf` and there is no history (design.md section 24) |
| `pg-port`, `pg-user`, `pg-database`, `pg-password` | `5432`, `hooks`, `hooks`, none | how to reach it. Put a password in the settings file, not on the command line |
| `allow-private-hosts` | `0` | `1`: an endpoint's host may be a name, or an address in a private, loopback, link-local or reserved range. With `0` (the default) it must be a public IPv4 literal, in `endpoints.conf`, in the table and in `POST /endpoints` (design.md section 26) |
| `admin-token` | (none) | the bearer token that lets a request create, change or delete endpoints, 8 to 255 visible characters; without it `POST`, `PATCH` and `DELETE /endpoints` are a `403`. Anyone who has it can choose where the service sends requests, within what `allow-private-hosts` allows (by default public addresses only), so keep it secret and put it in the settings file, not on the command line |
| `breaker-days` | `5` | pause an endpoint whose every attempt has failed for this many days (0 to 36,500; `0` is off). It is disabled as a `410` disables it, and `GET /endpoints` says `"paused":true`; its events wait in the log and are sent when a person enables it. Counted from the first failed attempt after a delivery, checked when an attempt fails (design.md section 31) |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the database and exit (needs `pg-host`; no `port` needed) |

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
There are no environment variables and no positional arguments (the old `hooks <port> <dir> ...` is refused).

**Idempotency.** Send the same `Idempotency-Key` with the same event and you get the same answer and one event, however often you retry:

```
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":77}' localhost:8080/events
{"id":2}
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":77}' localhost:8080/events
{"id":2}
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":78}' localhost:8080/events
{"error":"this Idempotency-Key was used for a different event"}
```

## HTTP API

| | |
|---|---|
| `POST /events` | a JSON object with a string `"type"`, and optionally an `Idempotency-Key` (1 to 255 visible ASCII characters); answers `202 {"id":N}` after the flush, `422` for a body that is not one or a key already used for a different event, `400` for a bad or doubled key, `413` for an event too large (over 65,499 bytes, less 28 and the key's length with a key), `507` for a new key when 65,536 are held, `503` if the log is broken |
| `GET /events/:id` | the stored event, `404` if there is none |
| `GET /stats` | `{"endpoints","attempts","delivered","failed","dead","keys","replays","draining","paused","breaker_trips","history_live","history_written","history_failed","history_dropped"}` (`paused`: endpoints the breaker has paused now; `breaker_trips`: pauses since the start) |
| `POST /events/:id/replay` | send the event again to every endpoint; `/replay/:endpoint` for one. `202 {"event","endpoints"}`, `404` for an unknown event or endpoint, `507` if 32 replays already wait. Same `webhook-id`, same schedule (design.md section 23) |
| `GET /events/:id/attempts` | the attempts of an event from the database, as `[{"endpoint","replay","attempt","outcome","status","at","latency_ms"}]`; `503` if no database is named or it cannot answer, `504` after five seconds |
| `GET /endpoints` | each endpoint's `{"id","port","cursor","disabled","paused","failing_since"}`: `paused` is true when the circuit breaker is why it is disabled, `failing_since` is the Unix time in ms at which its current run of failed attempts began (0 if none). Not the host, not the secret |
| `GET /endpoints/:id` | one endpoint, as `GET /endpoints` lists it; `404` for an unknown id, `400` for one that is not a number |
| `POST /endpoints` | create an endpoint (needs a database and an `admin-token`): `{"host","port"}` and optionally `"secret"` (`whsec_` and base64; the service makes one if it is left out) and `"from":"now"`. `201 {"id","host","port","secret","from","cursor"}`: **the secret is in this answer and in no other**. The endpoint gets the events from now on, not the log's past. `403` if the service has no `admin-token`, `401` without `Authorization: Bearer <token>`, `400` with the reason for a bad request, `409` if another change waits or 62 exist, `503` if no database is named or it refused, `504` after five seconds (the row may still have been stored: it is an endpoint at the next start) |
| `PATCH /endpoints/:id` | change an endpoint (needs a database and an `admin-token`): any of `"host"`, `"port"`, `"secret"` (`whsec_` and base64) and `"rotate": true` (the service makes a new secret; not with `"secret"`). `200 {"id","host","port"}`, and `"secret"` when the change brought or made one: **it is in this answer and in no other**. The next attempt uses the new address and secret, a retry of an event first tried under the old secret included; an attempt already on the wire finishes against the old address. There is one signature, so a receiver that has not been given the new secret refuses until it has (two at once is not built). `404` for an unknown id or a row that is gone, `409` while another change waits, `400` with a reason for a body that is not a change (the same host rule as `POST`), `503` if the database refuses (nothing changes) |
| `DELETE /endpoints/:id` | remove an endpoint (needs a database and an `admin-token`): the row is deleted first, and only when the database says commit does the service change. `200 {"id","deleted":true,"draining":bool}`. **No new attempt starts** for the endpoint from then on, and it is out of `GET /endpoints` at once; an attempt already on the wire finishes and is recorded (log and history, under the endpoint's id) as for any endpoint, and its slot cannot be reused until it has (`"draining":true`, `/stats` says how many); replays waiting for it are dropped; its rows in the history are kept; its id is never given again. A row that was already gone is removed from the service all the same (`200`, with `"row":"was already gone"`). `404` for an unknown id, `400` for one that is not a number, `403`/`401` as for `POST`, `409` while another change waits, or while all 62 slots are taken (one of them draining), `503` if the database refuses (nothing changes), `504` after five seconds (the row may still go: the next start reconciles) |
| `POST /endpoints/:id/enable` | enable an endpoint a `410` or the circuit breaker disabled (it also ends the run of failures); `200` whether or not it was, `404` for an unknown id |
| `GET /config` | the settings in force: `{"schedule":[ms,...],"deadline-ms","window-ms","allow-private-hosts","breaker-days"}` (not the endpoints, not their secrets) |
| `GET /healthz` | `{"ok":true}` |

A delivery is `POST /hook` to the endpoint, with the event as the body and three headers: `webhook-id` (`evt_<id>`, the same on
every attempt, so a receiver can drop a repeat), `webhook-timestamp` (Unix seconds) and `webhook-signature` (`v1,` and the base64
HMAC-SHA256 of `<id>.<timestamp>.<body>` under the decoded secret). Any `2xx` is a delivery. Anything else, a timeout or a
refused connection is a failure (a `410 Gone` is the exception: that event is a dead letter at once and the endpoint is **disabled**, no new attempts until `POST /endpoints/:id/enable`); the retries come 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h after the previous
attempt, and then the event is a dead letter for that endpoint.

## How it works

One thread, one poller. An accepted event is appended to a [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) segment and its
request held; after the turn one `flush` covers every append and the held requests are answered. Delivery runs in the same loop without holding it: each attempt is a small state machine (connecting, sending, reading) whose connection is watched on the server's own poller, so up to 64 are in flight together and a slow, silent or unreachable endpoint costs the others almost nothing. Each endpoint has a cursor (every event up to it is delivered or dead) and a window of events above it that finished out of order or are waiting for a retry, so a failing event does not hold up the ones after it. What happened to each attempt goes to a second log, `delivery.seg`, which a restart replays; the time of the next attempt is a Unix time, so it survives too.
[`docs/design.md`](docs/design.md) has the semantics, the scenario fixed before the build, and what each step found.

## Tests

```sh
$LEX_SYS test                                      # the unit-test sets of lex-sys.toml (state, endpoints, destination, idem, config)
python3 tests/sign_test.py build/sign_probe        # signatures and base64 against the reference library (536 checks)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/history_test.py build/hooks   # the history in PostgreSQL (needs one: see the file)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/roster_test.py build/hooks    # the endpoints in PostgreSQL: import, read at start, every refusal
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/manage_test.py build/hooks    # POST /endpoints and GET /endpoints/:id: the token, the request, from now
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/slots_test.py build/hooks     # endpoint ids and slots: the legacy log, ids above 15, dormant, reclaimed
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/patch_test.py build/hooks     # PATCH /endpoints/:id: address on the next attempt, secret rotation, a database that refuses, compaction
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/delete_test.py build/hooks    # DELETE /endpoints/:id: nothing new after it, an attempt on the wire finishes, replays dropped, the slot reused clean across a restart, 62 endpoints churned
python3 tests/saturation_test.py build/hooks                                       # ten endpoints beside 64 connections: no start beyond them is a failed attempt
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/ssrf_test.py build/hooks      # where a delivery may go: 41 refused hosts, 23 public ones, a redirect not followed
python3 tests/layout_test.py build/hooks           # the delivery state's regions do not overlap (2,300 events, two fail once)
python3 tests/replay_test.py build/hooks           # replay: one endpoint or all, restarts, capacity, an event far behind the cursor
python3 tests/gone_test.py build/hooks             # 410 Gone disables an endpoint, restarts keep it, enable undoes it
python3 tests/config_test.py build/hooks           # settings: a file, flags, which wins, and every refusal
python3 tests/attempt_test.py build/hooks          # one delivery attempt against eight kinds of receiver
python3 tests/retry_test.py build/hooks            # the retry delays, also across restarts, and the dead letter
python3 tests/isolation_test.py build/hooks        # what a silent, slow or unreachable endpoint costs the others (gated)
python3 tests/scan_test.py build/hooks             # a dead endpoint does not stop the others: 3,000 events, B revived, kill -9 twelve times in the middle
python3 tests/breaker_test.py build/hooks          # the circuit breaker: a run of failures, a pause after N days, its events wait, enable, restarts
python3 tests/chaos.py build/hooks 2000 8 50       # kill -9 as a power cut: no acknowledged event may be lost
python3 tests/delivery.py build/hooks 300 4 150    # three endpoints, signed, retried and dead-lettered, with the service killed
FULL=1 python3 tests/idempotency_test.py build/hooks   # idempotency keys: the contract, restarts, chaos, a broken log, a full index
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/backup_test.py build/hooks    # backup and restore: total loss, kill -9 under online backups, every refusal (docs/runbook.md section 4)
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` (what a bad receiver costs ingest) and `scripts/bench/stall_probe.py` (two endpoints, one dead, 3,000 events: the cursors) are reports, not gates.

## Documentation

- [`docs/runbook.md`](docs/runbook.md): running it: start and stop, the settings, what every log line and `/stats` field means, backup and restore (and whether the online variant is safe), upgrading, what to do when it goes wrong, the known limits. Parts that depend on work that is not built are marked planned.
- [`docs/production.md`](docs/production.md): what "production" means here, the plan to get there, and the status of each item.
- [`docs/design.md`](docs/design.md): what this is for, which store owns which fact, the delivery semantics, the test scenario
  fixed before the build, the gaps predicted, and sections 13 to 25 on what building each step showed. (Its first sections are the plan; where a later section says otherwise, the later one is what was built.)

## Layout

```
src/hooks.ls       the service: routes, the loop, delivery
src/attempt.ls     delivery attempts that do not hold the loop: connect, send, read a status line, each waiting for the poller
src/state.ls       the per-slot cursor and window, and the outcome record
src/idem.ls        the idempotency-key index (rebuilt from the log at start)
src/endpoints.ls   the endpoints (file or table) as the service holds them: id, slot, port, host, key
src/config.ls      the settings, from a file and from flags
src/history.ls     the attempts that ended, written to PostgreSQL, and the connections to it
src/roster.ls      the endpoints table: read at start, and `--import-endpoints`
src/manage.ls      `POST`, `PATCH` and `DELETE /endpoints`: who may call them, what a request may say, a secret for the endpoint
src/queries.ls     the SQL of `sql/queries.sql` as functions (generated by `pgen`)
src/view.ls        what the database says, as an HTTP answer
src/sign.ls        HMAC-SHA256, base64 and the Standard Webhooks signature
lex-sys.toml       the project file: the compiler, the two libraries (each pinned to a commit) and the programs
scripts/build.sh   `lex-sys build`, and the fsync shim the crash tests preload
scripts/backup.sh, restore.sh, logcheck.py   backup and restore of the two logs (and the tables), and the checker that refuses an inconsistent pair
scripts/release.sh a tarball, SHA256SUMS and an SBOM stub
Dockerfile, deploy/   the container image; the systemd unit, a settings sample and the container's health check
tests/             unit tests (lex-sys) and harnesses (Python)
docs/design.md     the design and what building it found
```

## Limitations

One process, one thread, one core: the loop does everything, and up to 64 delivery attempts are in flight at once (8 per endpoint). Endpoints come from a file read at start, or from the `endpoints` table (read at start, and added to with `POST /endpoints`); an endpoint the log has not seen (a row added by hand, a new line in the file) starts at the slowest cursor of the others, 0 if there are none, and one created with `POST /endpoints` starts from now; they can be changed with `PATCH /endpoints/:id` (host, port, secret) and removed with `DELETE /endpoints/:id` (an attempt of it that is on the wire finishes first, and until it has the endpoint's slot cannot be given to a new one: with 62 endpoints that is a `409` for up to the attempt's deadline); a host *name* is resolved by a blocking call that stalls the loop for as long as the resolver takes (use IP addresses); no `https`. By default only public IPv4 addresses are allowed as destinations (no names, no ports limited, no per-host allow-list: `docs/design.md` section 26). At most 62 endpoints. An endpoint is served at most 1,024 events past its own cursor (its window): an endpoint that is far behind waits there, the events beyond it wait in the log, and none of that holds up another endpoint (design.md section 31); left alone a dead endpoint dead-letters its events at the speed of its retry schedule, or is paused by the circuit breaker after `breaker-days` days of failures and waits for a person to enable it (no automatic resume). An event of 65,500 bytes or more is refused (`413`; 65,499 is the largest, and less with an `Idempotency-Key`). `delivery.seg` is never compacted. At most 65,536 idempotency keys; the index is rebuilt by reading the whole events log at start. Not for production.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src`, the unit tests and the harnesses above. Design before code,
in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
