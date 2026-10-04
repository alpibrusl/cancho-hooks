# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys): you `POST` it an event, it stores the
event durably, and it delivers the event, **signed**, to every subscribed endpoint, **at least once**, retrying on a schedule
and keeping what it could not deliver as a dead letter.

It keeps its two logs in [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and talks to PostgreSQL through
[`lexsys-pg`](https://github.com/alpibrusl/lexsys-pg). No `unsafe`, and one foreign authority: `Ffi("libc")`, held for five functions: four signal functions
(`sigblock`, `sigsetmask`, `sigpending`, `signal`), which is how a lex-sys program learns it was asked to stop (`src/ops.ls`, [`docs/design.md`](docs/design.md)
section 34.4), and `statx`, which reads the mode of the data directory for the production profile (`src/perm.ls`, section 33.5; lex-sys has no way to ask for a
file's mode). The authority report (`lex-sys authority`) names what the program can do, but because it calls foreign code it opens with `UNBOUNDED` (a library is
not an authority domain: the labels below that line do not bound what the program can reach, as for any such program; `docs/under-a-grant.md` in lex-sys), and lists the five symbols. Without
those calls it would name every capability, as it did before. A signal builtin and a file-mode builtin in lex-sys would remove the `Ffi` and the caveat with it.

## Status

**Working:** durable ingest (`202` only after the flush that covers the event; requests that arrive together share one flush), delivery to several endpoints with [Standard Webhooks](https://www.standardwebhooks.com) signatures checked against the reference library, retries on the Standard Webhooks schedule, dead letters, and every outcome (with the time of the next attempt) surviving a crash. A slow, silent or unreachable endpoint costs the others almost nothing: delivery attempts do not hold the loop (up to 64 in flight, a state machine each), so ingest stays at a median of 2.3 ms and healthy endpoints see their deliveries within milliseconds ([`docs/design.md`](docs/design.md) section 16). That used to hold only until the unreachable endpoint was 1,024 events behind, when every endpoint stopped with it (measured: beside a dead endpoint a healthy one stopped at event 1,024 of 3,000, `docs/design.md` section 29). **Each endpoint now reads the events log from its own cursor and is bounded only by its own window** (same probe: 3,000 of 3,000; section 31), and a **circuit breaker** pauses an endpoint whose every attempt has failed for 5 days (`breaker-days`, 0 turns it off): its events wait in the log until `POST /endpoints/:id/enable`. A client may send an `Idempotency-Key` ([section 17](docs/design.md)); an event can be replayed to one endpoint or all (section 23); a `410 Gone` disables an endpoint (section 22); with PostgreSQL the endpoints are a table, every ended attempt is a row, and an endpoint can be created, changed and deleted without a restart (`POST`, `PATCH`, `DELETE /endpoints`) behind an admin token (sections 24 and 25). **Cron:** with PostgreSQL a schedule (`POST /schedules`: a five-field cron expression, an event type and a body; UTC) appends an ordinary event at each scheduled second, exactly once even if the service is killed in the middle of a fire, and fires once for the time it was stopped (section 32). **Operating it:** `GET /readyz` (200 only when the logs are open and take a write, a named database has a live connection, and the service is not stopping; a 503 says why), `GET /metrics` in the Prometheus text format (behind the read token when one is configured; ingest, group commits, attempts by outcome and by the reason they failed, per-endpoint lag, retries waiting, the history, the logs' sizes), the reason an attempt failed in the log, in `GET /events/:id/attempts` and in the history table, and `SIGTERM` or `SIGINT` that drains and exits 0 within `stop-deadline-ms` (a second signal ends it at once). A log with damage in the middle, or a `delivery.seg` that is ahead of its `events.seg`, is **refused at start with a status of its own** instead of being cut or delivered into silence (design.md section 34.5; `--repair-logs 1` cuts the damage, keeping what it cut). **Credentials:** every route has a scope, and three bearer tokens (`ingest`, `read`, `admin`) say who may call what; `production = 1` makes the service refuse to start in a configuration that is unsafe on the internet (section 33, and "Securing it" below). **Per endpoint** you can choose which event types it is sent (`invoice.paid`, `user.*`), keep the previous secret valid for a stated time while a secret is rotated (every delivery then carries both signatures), and have it sent custom headers such as an `Authorization` (section 35; [below](#per-endpoint-event-types-secret-rotation-and-headers)). **Not built:** jitter in the retry schedule, TLS (`https`) endpoints.

## Requirements

- The **lex-sys** compiler at the commit `lex-sys.toml` names (`[package] lex-sys`); `lex-sys build` refuses any other. It needs `clock_unix_ms` (the signing timestamp; lex-sys PR #190),
  `tcp_connect_start` (attempts that do not wait; #191), a lock with an origin (#192), the project file (#193), and `std.hmac`, which signs
  every delivery and replaced this repository's own HMAC (#229; [`docs/design.md`](docs/design.md) section 30).
- `git`: [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and lex-sys's `http-server` are not cloned by hand; they are dependencies in `lex-sys.toml`, pinned to a commit each, and `lex-sys build` fetches and checks them.
- Rust, to build the compiler; `gcc`, to build the two small shims the tests preload (`fsync`, for the crash tests; `statx`, for the production profile's).
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

## Cron: scheduled events

With a database and an `admin-token`, `POST /schedules` makes the service append an event on a schedule (`docs/design.md` section 32):

```sh
psql hooks -f sql/schema.sql    # adds the `schedules` table; the service refuses to start on a database without it
curl -X POST localhost:8080/schedules -H "Authorization: Bearer $TOKEN" \
     -d '{"expr": "30 4 1,15 * 5", "type": "report.due", "body": {"report": "weekly"}}'
# 201 {"id":1,"expr":"30 4 1,15 * 5","type":"report.due","body":{"report":"weekly"},"enabled":true,"created_at":...,"last_fired":null,
#      "next_fire":1791...,"next_fire_at":"2026-10-15T04:30:00Z"}
```

* **The expression** has five fields, `minute hour day-of-month month day-of-week`, each a list of `*`, `a`, `a-b`, `*/n` and `a-b/n` (numbers only; no
  names, no `@daily`), **in UTC**. Sunday is `0` or `7`. As in Vixie cron, if both the day of month and the day of week are restricted a day
  matches when *either* does (`0 0 1 * 1` is the 1st and every Monday), and if one begins with `*` both must (`0 0 * * 1`: Mondays). An expression that cannot
  parse, or that never fires (`0 0 31 2 *`), is a `400` with the reason.
* **A fire is an ordinary event**, `{"type": <type>, "schedule": <id>, "scheduled_at": <Unix second>, "body": <body>}`, appended through the same code as
  `POST /events` with the idempotency key `cron:<id>:<scheduled second>`, so it is stored, delivered, signed, retried and replayed like any other. The key is
  what makes a fire happen **once**: the event is flushed *before* the database is told, and a restart that finds the database one behind finds the key
  and appends nothing.
* **After a stop**, a schedule fires **once** for the time it missed (not once per missed minute), for the last scheduled second more than 10 seconds old; the
  ones within 10 seconds of the restart fire each on its own. With `cron-catchup 0` the missed ones are skipped.
* **The routes** need the admin token, the reads too (`403` without a token configured, `401` without the right one, `503` without a database): `POST /schedules`,
  `GET /schedules` (the list, with each `next_fire`), `GET /schedules/:id`, `PATCH /schedules/:id` (any of `expr`, `type`, `body`, `enabled`; a changed expression
  or an enabled schedule counts from now), `DELETE /schedules/:id`. At most 64 schedules; a body is JSON of at most 1,024 bytes.
* **One service per database.** The schedules are rows, and a second service reading the same table would fire them too, into its own log.

## Per endpoint: event types, secret rotation and headers

Each endpoint can have three things besides an address and a secret (`docs/design.md` section 35). They are set by `POST /endpoints` and `PATCH /endpoints/:id` (or by words on a line of `endpoints.conf`, below), kept in the `endpoints` table, and survive a restart.

**Event types.** `"types": ["invoice.paid", "user.*"]` sends the endpoint only the events whose type matches one of the patterns. No list (the default, or `[]`) is every event, as before.

* **The type of an event** is the string `"type"` member of its JSON body, read once when the event is accepted and stored with it. A type that is empty, longer than 128 bytes or has a control character in it is stored as *no type*, and so is the type of every event stored before this existed: an event with no type goes only to an endpoint with no list, or one that lists `*`.
* **A pattern** is 1 to 128 visible ASCII characters, no space and no comma; at most 16 patterns and 512 characters in all. `invoice.paid` matches exactly that type (byte for byte, case sensitive); `user.*` matches every type that begins `user.` (`user.created`, `user.address.changed`; not `user` and not `users.created`); `*` matches everything, an event with no type too. A `*` anywhere else is refused.
* **An event an endpoint does not want is final for it at once**: no attempt, no record, nothing in the history. Its cursor moves over it, so a stream of events nobody wants does not fill the window or hold anything back. `/stats` counts them (`filtered`). A change of `types` applies to the events the endpoint has not yet looked at; an event it passed over is not sent because the list grew (replay it). A restart decides again the events above an endpoint's recorded cursor, with the list then in force.
* **A replay** (`POST /events/:id/replay`) goes to the endpoints whose list wants the event; `/replay/:endpoint` goes to that endpoint whatever it lists.
* The check reads the stored type from the record: no JSON is parsed for an endpoint, and an endpoint with no list costs nothing.

**Secret rotation.** `PATCH /endpoints/:id` with a new `"secret"` (or `"rotate": true`, which makes one) replaces the secret at once, as before. Add `"keep_old_ms": N` (0 to 2,592,000,000, 30 days) to keep the old secret valid for N ms, or `"keep_old": true` for the default period (`rotation-grace-ms`, a day). While it lasts **every delivery carries both signatures** in `webhook-signature`, the new one first: `v1,<new> v1,<old>`, as the Standard Webhooks specification has it for key rotation, so a receiver that knows either secret verifies the delivery (the reference library accepts the header whichever secret it holds). Afterwards only the new one. The answer says `"secret_old_until"` (Unix ms), and so does `GET /endpoints/:id` while the period lasts (0 otherwise). `{"keep_old_ms": 0}` alone ends the overlap now, `{"keep_old_ms": N}` alone moves its end (400 if there is no previous secret); a new secret without a period ends a running one, and a second rotation keeps only the secret it replaced. The previous secret is held in the row (`secret_old`, with `secret_old_until`) and read at start: the table is a secret, as before.

**Custom headers.** `"headers": {"Authorization": "Bearer ...", "X-Api-Key": "..."}` is sent on every attempt (and every replay) after the three `webhook-*` headers. `PATCH` replaces the whole set; `{}` or `null` removes it.

* At most 8 headers; a name of 1 to 64 characters from the HTTP token set; a value of 1 to 512 visible ASCII characters and spaces, not beginning or ending with a space; 2,048 bytes as sent in all; no name twice (any case).
* **Never allowed**, in any case, each refused with a reason: `webhook-id`, `webhook-timestamp`, `webhook-signature`, `host`, `content-length`, `content-type`, `connection`, `transfer-encoding`, and the other connection headers (`keep-alive`, `proxy-connection`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `upgrade`). A name or a value with a CR, an LF or a NUL, however written (a raw byte, a JSON escape, `%0d%0a` in the table), is refused, so a header cannot end the headers and start a request of its own.
* **The values are secrets** (an `Authorization` is what the receiver trusts): `GET` shows the names (`"headers": ["Authorization"]`) and never a value, and no answer or log line repeats one. The table holds them like the secret, so give it the permissions of a secret.

On the wire, with a rotation under way and two headers set:

```
POST /hook HTTP/1.1
Host: receiver
Content-Type: application/json
webhook-id: evt_41
webhook-timestamp: 1791028548
webhook-signature: v1,<signature under the new secret> v1,<signature under the old one>
Authorization: Bearer ...
X-Api-Key: ...
Content-Length: 53
Connection: close
```

`endpoints.conf` and the table's text take the same three as words after the secret (a line without them is what it always was):

```
# <id> <host> <port> <secret> [types=<patterns>] [headers=<spec>] [old=<secret>@<until>]
0 127.0.0.1 9000 whsec_... types=invoice.paid,user.* headers=Authorization:Bearer%20abc,X-Api-Key:k
1 127.0.0.1 9001 whsec_... old=whsec_...@1791100000000
```

`types=` is the comma separated patterns; `headers=` is `Name:value` pairs separated by commas, each value percent-encoded (every byte but `A-Za-z0-9-._~` as `%XX`: a space is `%20`, a comma `%2C`, a percent `%25`), which is also how the `headers` column of the table holds them; `old=` is the previous secret and the Unix ms until which it is signed with too. A word that is none of these, or one twice, or one that is not good, is a bad line (status 13, naming the line); `--import-endpoints` copies the words into the table. The table is read as one text of at most 32 KiB, so 62 endpoints with large header sets do not all fit (`POST` answers `507` before the table would not start).

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
# <id> <host> <port> <secret> [types=...] [headers=...] [old=...]   (the words after the secret: see "Per endpoint" above)
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
| `admin-token` | (none) | the bearer token of the **admin** scope: everything that changes configuration or state (create, change and delete endpoints, enable, replay, the schedules), and it also does what the other two do. 8 to 255 visible characters; without it `POST`, `PATCH` and `DELETE /endpoints` and the schedules are a `403`. Anyone who has it can choose where the service sends requests, within what `allow-private-hosts` allows (by default public addresses only), so keep it secret and put it in the settings file, not on the command line |
| `ingest-token` | (none) | the token of the **ingest** scope: `POST /events` and so its idempotent retries; same rule as `admin-token`. Without it `POST /events` is open |
| `read-token` | (none) | the token of the **read** scope: `GET /events/:id`, `/events/:id/attempts`, `/endpoints`, `/endpoints/:id`, `/stats`, `/config`, `/metrics`; same rule. Without it those are open (in production: they need the admin token) |
| `production` | `0` | `1`: refuse to start unless `admin-token` and `ingest-token` are set and different from each other and from `read-token`, `allow-private-hosts` is `0`, and the data directory and its files cannot be read or written by the group or by others. One exit status for each cause (30 to 35), and a line on stderr that names the setting or the path ("Securing it" below) |
| `breaker-days` | `5` | pause an endpoint whose every attempt has failed for this many days (0 to 36,500; `0` is off). It is disabled as a `410` disables it, and `GET /endpoints` says `"paused":true`; its events wait in the log and are sent when a person enables it. Counted from the first failed attempt after a delivery, checked when an attempt fails (design.md section 31) |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the database and exit (needs `pg-host`; no `port` needed) |
| `cron-catchup` | `1` | `1`: a schedule that missed fires while the service was stopped fires once for them; `0`: it skips them (design.md section 32) |
| `rotation-grace-ms` | `86400000` | how long the previous secret is still signed with after `PATCH /endpoints/:id` with `"keep_old": true` (1 to 2,592,000,000; `keep_old_ms` names another period for one change) |
| `cron-seconds` | `0` | `1`: a schedule's expression has a leading *seconds* field (six fields). A test mode, so that a test sees a fire in seconds instead of a minute; do not switch it with schedules in the table (a row of the other kind does not parse and is parked) |
| `stop-deadline-ms` | `5000` | how long the attempts on the wire may take to finish after `SIGTERM` or `SIGINT` (0 to 3,600,000; `0` does not wait). Keep systemd's `TimeoutStopSec` and `docker stop -t` above it (design.md section 34.4) |
| `repair-logs` | `0` | `1`: a log with damage in the middle is cut at the first bad byte, after the cut bytes are copied to `<log>.cut-<offset>`, instead of the start being refused (status 19). Give it once on the command line, after reading the refusal (design.md section 34.5) |

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

The **scope** column says which token a route needs, when that token is configured ("Securing it" below): `ingest`, `read` or `admin`; the admin token also does what the other two do. `open`: no token, ever (`/healthz`, `/readyz`).

| route | scope | what it does |
|---|---|---|
| `POST /events` | ingest | a JSON object with a string `"type"`, and optionally an `Idempotency-Key` (1 to 255 visible ASCII characters); answers `202 {"id":N}` after the flush, `422` for a body that is not one or a key already used for a different event, `400` for a bad or doubled key, `413` for an event too large (over 65,499 bytes, less 11 and the type's length for its stored type, less 28 and the key's length with a key), `507` for a new key when 65,536 are held, `503` if the log is broken |
| `GET /events/:id` | read | the stored event, `404` if there is none |
| `GET /stats` | read | `{"endpoints","attempts","delivered","failed","dead","keys","replays","draining","paused","breaker_trips","history_live","history_written","history_failed","history_dropped","cron_fired","cron_errors","cron_skipped","filtered"}` (`paused`: endpoints the breaker has paused now; `breaker_trips`: pauses since the start; `filtered`: events passed over for an endpoint that does not subscribe to their type, since the start) |
| `POST /events/:id/replay` | admin | send the event again to every endpoint that subscribes to its type; `/replay/:endpoint` for one, whatever it subscribes to. `202 {"event","endpoints"}`, `404` for an unknown event or endpoint, `507` if 32 replays already wait. Same `webhook-id`, same schedule (design.md section 23) |
| `GET /events/:id/attempts` | read | the attempts of an event from the database, as `[{"endpoint","replay","attempt","outcome","status","reason","at","latency_ms"}]`; `reason` says why a failed attempt failed (`connect_refused`, `connect_timeout`, `no_response` (the deadline passed before a status line), `reset`, `closed_early`, `bad_response`, `status_3xx`, `status_4xx`, `status_5xx`, `gone`, and the rarer ones: design.md section 34.3), `none` for a delivery, `unrecorded` for a row written before reasons were; `status` is the HTTP status, or the coarse -1 could not connect, -2 could not send, -3 timed out, -4 no answer; `503` if no database is named or it cannot answer, `504` after five seconds |
| `GET /endpoints` | read | each endpoint's `{"id","port","cursor","disabled","paused","failing_since","types","headers","secret_old_until"}`: `paused` is true when the circuit breaker is why it is disabled, `failing_since` is the Unix time in ms at which its current run of failed attempts began (0 if none), `types` its event type patterns (`[]`: every event), `headers` the **names** of its custom headers, `secret_old_until` the Unix ms until which the previous secret is signed with as well (0: none). Not the host, not a secret, not a header's value |
| `GET /endpoints/:id` | read | one endpoint, as `GET /endpoints` lists it; `404` for an unknown id, `400` for one that is not a number |
| `POST /endpoints` | admin | create an endpoint (needs a database and an `admin-token`): `{"host","port"}` and optionally `"secret"` (`whsec_` and base64; the service makes one if it is left out), `"types"` (event type patterns) and `"headers"` (custom headers: see "Per endpoint") and `"from":"now"`. `201 {"id","host","port","secret","from","cursor"}`: **the secret is in this answer and in no other**. The endpoint gets the events from now on, not the log's past. `403` if the service has no `admin-token`, `401` without `Authorization: Bearer <token>`, `400` with the reason for a bad request, `409` if another change waits or 62 exist, `503` if no database is named or it refused, `504` after five seconds (the row may still have been stored: it is an endpoint at the next start) |
| `PATCH /endpoints/:id` | admin | change an endpoint (needs a database and an `admin-token`): any of `"host"`, `"port"`, `"secret"` (`whsec_` and base64), `"rotate": true` (the service makes a new secret; not with `"secret"`), `"types"`, `"headers"`, and `"keep_old_ms"` or `"keep_old": true` (keep the secret this one replaces valid for that long, both signatures on every delivery meanwhile: see "Per endpoint"). `200 {"id","host","port"}`, and `"secret"` when the change brought or made one: **it is in this answer and in no other**; `"secret_old_until"` when a previous secret is kept. The next attempt uses the new address and secret, a retry of an event first tried under the old secret included; an attempt already on the wire finishes against the old address. Without a period there is one signature, so a receiver that has not been given the new secret refuses until it has. `404` for an unknown id or a row that is gone, `409` while another change waits, `400` with a reason for a body that is not a change (the same host rule as `POST`), `503` if the database refuses (nothing changes) |
| `DELETE /endpoints/:id` | admin | remove an endpoint (needs a database and an `admin-token`): the row is deleted first, and only when the database says commit does the service change. `200 {"id","deleted":true,"draining":bool}`. **No new attempt starts** for the endpoint from then on, and it is out of `GET /endpoints` at once; an attempt already on the wire finishes and is recorded (log and history, under the endpoint's id) as for any endpoint, and its slot cannot be reused until it has (`"draining":true`, `/stats` says how many); replays waiting for it are dropped; its rows in the history are kept; its id is never given again. A row that was already gone is removed from the service all the same (`200`, with `"row":"was already gone"`). `404` for an unknown id, `400` for one that is not a number, `403`/`401` as for `POST`, `409` while another change waits, or while all 62 slots are taken (one of them draining), `503` if the database refuses (nothing changes), `504` after five seconds (the row may still go: the next start reconciles) |
| `POST /endpoints/:id/enable` | admin | enable an endpoint a `410` or the circuit breaker disabled (it also ends the run of failures); `200` whether or not it was, `404` for an unknown id |
| `GET /config` | read | the settings in force: `{"schedule":[ms,...],"deadline-ms","window-ms","allow-private-hosts","breaker-days","production","cron-catchup","cron-seconds","stop-deadline-ms","repair-logs","rotation-grace-ms"}` (not the endpoints, not their secrets, and never a token) |
| `POST /schedules` | admin | create a schedule (needs a database and an `admin-token`): `{"expr","type"}` and optionally `"body"` (any JSON, at most 1,024 bytes, default `{}`) and `"enabled"` (default true). `201` with the schedule: `{"id","expr","type","body","enabled","created_at","last_fired","next_fire","next_fire_at"}`. A schedule counts from now: it does not fire for the time before it existed. `400` with the reason for a bad request, `409` at 64 schedules |
| `GET /schedules`<br>`GET /schedules/:id` | admin | the schedules, or one, as above; `next_fire` (Unix seconds) and `next_fire_at` (UTC text) are the next scheduled second after now, `null` if disabled. Admin token needed for these too (a body is a payload). `404` for an unknown id |
| `PATCH /schedules/:id` | admin | any of `"expr"`, `"type"`, `"body"`, `"enabled"`. A changed expression, or `"enabled": true` on a disabled schedule, counts from now (no fires for the time between). `200` with the schedule, `404`, `400` |
| `DELETE /schedules/:id` | admin | `200 {"id","deleted":true}`, `404` |
| `GET /healthz` | open | `{"ok":true}` while the process is up and its loop turns. It looks at nothing: it says `200` with the disk full. Open whatever is configured |
| `GET /readyz` | open | `200 {"ready":true}`, or `503 {"ready":false,"check":...,"reason":...}` with `check` one of `stopping`, `events_log` or `delivery_log` (a write or a flush failed: restart after freeing the disk), `data_dir` (the data directory does not take a write, checked once a second), `database` (a database is named and no connection to it is live). No credential, whatever is configured: this is the container's health check |
| `GET /metrics` | read | the Prometheus text exposition format 0.0.4: [what each series is](#operating-it); needs the read token when one is configured (the admin token also opens it) |

A delivery is `POST /hook` to the endpoint, with the event as the body and three headers: `webhook-id` (`evt_<id>`, the same on
every attempt, so a receiver can drop a repeat), `webhook-timestamp` (Unix seconds) and `webhook-signature` (`v1,` and the base64
HMAC-SHA256 of `<id>.<timestamp>.<body>` under the decoded secret; two, space separated, while a rotation overlaps), then the endpoint's custom headers, if it has any. Any `2xx` is a delivery. Anything else, a timeout or a
refused connection is a failure (a `410 Gone` is the exception: that event is a dead letter at once and the endpoint is **disabled**, no new attempts until `POST /endpoints/:id/enable`); the retries come 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h after the previous
attempt, and then the event is a dead letter for that endpoint.

## Securing it

Out of the box the service is open: with no token configured anyone who can reach the port can post events, read them, and (bar the
routes that say `403` without an admin token) enable and replay. That is right for a laptop and wrong for anything else. Three bearer tokens,
one for each scope, close it; `production = 1` refuses to run without them.

```
# /etc/hooks/hooks.conf (deploy/hooks.conf.example has the whole sample)
port = 8080
dir = /var/lib/hooks
production = 1
admin-token  = <32 random characters>   # changes configuration or state; also does what the others do
ingest-token = <32 random characters>   # POST /events: give it to the services that send events
read-token   = <32 random characters>   # the GETs: give it to dashboards and monitors (optional: without it the reads need the admin token)
```

* **A token is `Authorization: Bearer <token>`.** A request with none, or one that is none of the configured tokens, is a `401` with
  `WWW-Authenticate: Bearer`. A token that is valid but of a scope that does not reach the route (the read token on `POST /events`) is a `403` that says which
  token the route needs. Where there is no admin token, the routes that have always said so are a `403` ("management is off"): `POST`, `PATCH` and `DELETE /endpoints`
  and every route of `/schedules`.
* **A scope with no token configured is open**, as it was before there were scopes: that is what keeps a development service free of ceremony, and it is why a
  half-configured service is not a secured one. `production = 1` is the check that nothing was left out.
* **`GET /healthz` and `GET /readyz` are always open**: they say only that the process is up and whether it is ready (a container's health check and a load balancer carry no secret). `GET /metrics` is not: it is a read route. Whoever can reach the port can also learn that a route exists (a `405` names its methods).
* **Tokens are 8 to 255 visible characters**, compared in constant time (every byte of the longest token there can be is looked at whichever one differs first), and
  appear in no answer, in `GET /config`, in `GET /stats` or on stderr; a token that is refused as a setting is not repeated in the message that refuses it. Put them in the settings file,
  not on the command line (a command line is visible to every user of the host). There are no environment variables.
* **`production = 1` refuses to start**, with an exit status for each cause and a line on stderr that names the setting or the path:

| status | the service refuses because |
|---|---|
| 30 | `admin-token` is not set |
| 31 | `ingest-token` is not set |
| 32 | `allow-private-hosts` is `1` |
| 33 | the data directory, `events.seg`, `delivery.seg` or `endpoints.conf` can be read or written by its group or by others (the directory should be `0700`, the files `0600`; start the service with `umask 077`, which the systemd unit and the container image do, because the logs it makes itself are `0666` less the umask) |
| 34 | two of `admin-token`, `ingest-token` and `read-token` are the same |
| 35 | the mode of the data directory cannot be read (it is not there, or the system call failed) |

  Nothing is opened or created before these are judged, except that a first start in an empty directory makes the logs and judges them
  afterwards. A database is not required in production: without one the endpoints are the file `endpoints.conf`, and `POST /endpoints` and the schedules
  answer `503`. A mode of `0710` (a directory the group may search but not read) is allowed.
* **The database is the trust boundary for secrets.** The endpoints' signing secrets are stored in the clear, in the `endpoints` table (or `endpoints.conf`),
  because the service signs with them; there is no encryption at rest. Anyone who can read the table, or a backup of it, can sign as the service. Give the service's
  database role only what it needs (`select`, `insert`, `update` and `delete` on `endpoints`, `attempts` and `schedules`, and `usage` on the sequence), keep the database off
  any network the receivers can reach, and treat a backup like the secrets it holds (`docs/runbook.md` section 2).
* **What it does not do:** no TLS (put a reverse proxy in front for `https` towards your own clients; delivery to `https` receivers is not built), no
  per-token rotation without a restart, no rate limit on failed tokens, no audit log of who called what. A token is the whole of the authentication.

## Operating it

**Metrics.** `GET /metrics` answers in the Prometheus text format (counters are since this start; a restart is a reset, which `rate` and `increase` expect). About 100 series for the service and 7 for each endpoint, labelled by the endpoint's `id` and by nothing else that grows (62 endpoints at most):

| | |
|---|---|
| `hooks_ingest_events_total{result}` | `POST /events`: `accepted` (stored, flushed, `202`), `duplicate` (an `Idempotency-Key` repeat, `202`), `refused`; `hooks_ingest_refused_total{status}` splits the refusals (`400`, `413`, `422`, `503`, `507`, `other`) |
| `hooks_log_commits_total{log}`, `hooks_log_size_bytes{log}`, `hooks_log_synced_bytes{log}` | group commits (turns in which a flush made a log's new records durable) and the size of `events` and `delivery` |
| `hooks_attempts_total{outcome}`, `hooks_attempt_failures_total{reason}` | attempts that ended, `delivered`, `failed` (it will be tried again) or `dead` (a dead letter); and the failed and dead ones by the reason they failed |
| `hooks_attempts_in_flight`, `hooks_retries_waiting`, `hooks_replays_waiting`, `hooks_breaker_trips_total` | on the wire now; events that failed and wait for a retry; replays not finished; times the circuit breaker paused an endpoint |
| `hooks_endpoint_cursor`, `_lag_events`, `_disabled`, `_paused`, `_retries_waiting`, `_in_flight`, `_failing_since_ms`, `_last_failure{endpoint,reason}` | per endpoint: its cursor, the events behind (the newest event's id minus the cursor), disabled (a `410`, a person, or the breaker), paused (the breaker), the events waiting for a retry, attempts on the wire, when its run of failures began, and why its last attempt failed (kept across restarts) |
| `hooks_history_queue`, `_connections`, `_rows_total{result}` | the history of attempts: rows waiting, live database connections (0 to 2), rows `written`, `failed` or `dropped` |
| `hooks_cron_fires_total`, `_errors_total`, `_skipped_total`; `hooks_events_last_id`, `hooks_idempotency_keys`, `hooks_endpoints`, `hooks_uptime_seconds`, `hooks_ready`, `hooks_stopping` | the schedules, and the rest |

`GET /metrics` is a **read**-scope route: when a `read-token` is configured (or, in production, the admin token), a scraper sends it as `Authorization: Bearer <token>`; without one the route is open, like the other reads ("Securing it" above). `GET /readyz` and `GET /healthz` are always open. What to alert on is in [`docs/runbook.md`](docs/runbook.md) section 1.

**Stopping.** `SIGTERM` (`systemctl stop`, `docker stop`) or `SIGINT`: the service stops taking requests (every write that passes the credential check is a `503` that closes the connection; `GET /readyz` says `stopping`; `GET /healthz`, `/metrics` and the other reads still answer), starts no attempt, lets the attempts on the wire finish for at most `stop-deadline-ms`, flushes both logs and exits **0**. Attempts still on the wire at the deadline are made again at the next start (at least once, as after a crash). A second signal ends the process at once, killed by that signal. Nothing is lost either way: an acknowledgement is only sent after the flush that covers the event.

**Exit statuses of a start that ends** (the message is on stderr; the full list with what to do is in `docs/runbook.md` section 1): `0` finished (`--import-endpoints`, or a drain); `2` a setting was refused; `10`/`12` `events.seg`/`delivery.seg` could not be opened; `11` the port is taken; `13` an endpoint line or row is invalid; `14` the retry schedule does not parse; `15`/`16` a log this version does not understand; `17` an endpoint could not be given a slot; **`18` `delivery.seg` refers to an event that `events.seg` does not hold** (an older events log beside a newer delivery log: the service would acknowledge new events and never deliver them), **`19` damage in the middle of a log** (not a torn tail: the message says where the log is whole to, how many bytes the cut would take and how many intact records are among them); `20` the database could not be read; `30` to `35` the production profile refused an unsafe setting or mode (the table under "Securing it" above). A refusal with 18 or 19 leaves both logs exactly as they were. A single torn record at the end, which is what a crash leaves, is still cut at start, and now said (after `listening`).

## How it works

One thread, one poller. An accepted event is appended to a [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) segment and its
request held; after the turn one `flush` covers every append and the held requests are answered. Delivery runs in the same loop without holding it: each attempt is a small state machine (connecting, sending, reading) whose connection is watched on the server's own poller, so up to 64 are in flight together and a slow, silent or unreachable endpoint costs the others almost nothing. Each endpoint has a cursor (every event up to it is delivered or dead) and a window of events above it that finished out of order or are waiting for a retry, so a failing event does not hold up the ones after it. What happened to each attempt goes to a second log, `delivery.seg`, which a restart replays; the time of the next attempt is a Unix time, so it survives too.
[`docs/design.md`](docs/design.md) has the semantics, the scenario fixed before the build, and what each step found.

## Tests

```sh
$LEX_SYS test                                      # the unit-test sets of lex-sys.toml (state, endpoints, destination, idem, cron, authz, filter, hdrs, epx, config, reason, ops: the counters, readiness and the text of /metrics)
python3 tests/cron_test.py build/cron_probe 500      # cron expressions against an independent implementation (calendar, steps, day-of-month and day-of-week, leap days)
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
python3 tests/authz_test.py build/hooks            # credentials: every route (read from the source) against no token, a wrong one and each scope's, in five ways of configuring them; no token leaks
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/production_test.py build/hooks   # production = 1: each unsafe setting or mode is a refusal of its own; all safe starts
python3 tests/attempt_test.py build/hooks          # one delivery attempt against eight kinds of receiver
python3 tests/retry_test.py build/hooks            # the retry delays, also across restarts, and the dead letter
python3 tests/isolation_test.py build/hooks        # what a silent, slow or unreachable endpoint costs the others (gated)
python3 tests/scan_test.py build/hooks             # a dead endpoint does not stop the others: 3,000 events, B revived, kill -9 twelve times in the middle
python3 tests/breaker_test.py build/hooks          # the circuit breaker: a run of failures, a pause after N days, its events wait, enable, restarts
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/filter_test.py build/hooks    # event types: the matching rules against a model, 3,000 events nobody wants, a dead endpoint, kill -9, a log from before the type, replay
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/rotation_test.py build/hooks  # two signatures during a rotation, checked with the reference library: both secrets, only the new one, restarts, every refusal
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/headers_test.py build/hooks   # custom headers: on the wire, never in a read, every forbidden name, CRLF injection, the limits, the largest event
python3 tests/chaos.py build/hooks 2000 8 50       # kill -9 as a power cut: no acknowledged event may be lost
python3 tests/delivery.py build/hooks 300 4 150    # three endpoints, signed, retried and dead-lettered, with the service killed
FULL=1 python3 tests/idempotency_test.py build/hooks   # idempotency keys: the contract, restarts, chaos, a broken log, a full index
python3 tests/stop_test.py build/hooks             # SIGTERM and SIGINT drain, the deadline, a second signal, under load nothing is lost or repeated
python3 tests/corrupt_test.py build/hooks          # a delivery log ahead of its events log, damage in the middle, a torn tail, --repair-logs, and the same rule as scripts/logcheck.py on 153 corruptions (150 random, 3 directed)
python3 tests/metrics_test.py build/hooks          # /metrics against a known workload: ingest, group commits, attempts by outcome and reason, lag, retries (with HOOKS_PG: the history)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/ready_test.py build/hooks     # /readyz: a database that goes away, a disk that fills (a tmpfs it mounts)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/reason_test.py build/hooks    # why an attempt failed: a receiver for each reason, in /attempts, /metrics, the table and the log
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/backup_test.py build/hooks    # backup and restore: total loss, kill -9 under online backups, every refusal (docs/runbook.md section 4)
FULL=1 HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/schedules_test.py build/hooks   # schedules: the token, every refusal, a fire is an ordinary event, missed fires, kill -9 between the event and the database, a real minute (needs a database of its own: it empties `schedules`)
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` (what a bad receiver costs ingest) and `scripts/bench/stall_probe.py` (two endpoints, one dead, 3,000 events: the cursors) are reports, not gates.

## Documentation

- [`docs/runbook.md`](docs/runbook.md): running it: start and stop, the settings, what every log line and `/stats` field means, backup and restore (and whether the online variant is safe), upgrading, what to do when it goes wrong, the known limits. Parts that depend on work that is not built are marked planned.
- [`docs/production.md`](docs/production.md): what "production" means here, the plan to get there, and the status of each item.
- [`docs/design.md`](docs/design.md): what this is for, which store owns which fact, the delivery semantics, the test scenario
  fixed before the build, the gaps predicted, and sections 13 to 25 on what building each step showed, section 32 on cron, section 33 on the tokens and the production profile, and section 34 on operating it (readiness, metrics, the reason an attempt failed, stopping, refusing corruption). (Its first sections are the plan; where a later section says otherwise, the later one is what was built.)

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
src/authz.ls       who may call which route: the scope of every route, and the verdict for a request's token
src/perm.ls        the modes of the data directory and its files (`production = 1`): the `statx` call into libc
src/filter.ls      which events an endpoint is sent: the patterns, the match, the type in a record
src/hdrs.ls        an endpoint's custom headers: the rules, the form they are kept in, the wire form
src/epx.ls         what an endpoint has besides an address and a secret (types, headers, the previous secret), and the request that sets it
src/wire.ls        the request of one attempt: the headers, one signature or two
src/cron.ls        cron expressions: parse, and the next and last fire (pure, no clock)
src/sched.ls       `/schedules`: the requests, what a due row means, and the state the tick keeps
src/ops.ls         what an operator needs: the counters, readiness, how the service learns it is to stop (four libc signal functions)
src/metrics.ls     `GET /metrics`: the Prometheus text, from two arrays of numbers
src/reason.ls      why an attempt failed: the reasons, their numbers (on disk) and names, and the coarse status the history keeps
src/logguard.ls    the start's look at the logs before it changes them: torn tail or damage, the pair of logs, `--repair-logs`
src/queries.ls     the SQL of `sql/queries.sql` as functions (generated by `pgen`)
src/view.ls        what the database says, as an HTTP answer
src/sign.ls        HMAC-SHA256, base64 and the Standard Webhooks signature
lex-sys.toml       the project file: the compiler, the two libraries (each pinned to a commit) and the programs
scripts/build.sh   `lex-sys build`, and the fsync shim the crash tests preload
scripts/backup.sh, restore.sh, logcheck.py   backup and restore of the two logs (and the tables), and the checker that refuses an inconsistent pair
scripts/release.sh a tarball, SHA256SUMS and an SBOM stub
Dockerfile, deploy/   the container image; the systemd unit, a settings sample (with the production profile) and the container's health check and entry point
tests/             unit tests (lex-sys) and harnesses (Python)
docs/design.md     the design and what building it found
```

## Limitations

One process, one thread, one core: the loop does everything, and up to 64 delivery attempts are in flight at once (8 per endpoint). Endpoints come from a file read at start, or from the `endpoints` table (read at start, and added to with `POST /endpoints`); an endpoint the log has not seen (a row added by hand, a new line in the file) starts at the slowest cursor of the others, 0 if there are none, and one created with `POST /endpoints` starts from now; they can be changed with `PATCH /endpoints/:id` (host, port, secret, event types, headers) and removed with `DELETE /endpoints/:id` (an attempt of it that is on the wire finishes first, and until it has the endpoint's slot cannot be given to a new one: with 62 endpoints that is a `409` for up to the attempt's deadline); a host *name* is resolved by a blocking call that stalls the loop for as long as the resolver takes (use IP addresses); no `https`. By default only public IPv4 addresses are allowed as destinations (no names, no ports limited, no per-host allow-list: `docs/design.md` section 26). At most 62 endpoints. An endpoint is served at most 1,024 events past its own cursor (its window): an endpoint that is far behind waits there, the events beyond it wait in the log, and none of that holds up another endpoint (design.md section 31); left alone a dead endpoint dead-letters its events at the speed of its retry schedule, or is paused by the circuit breaker after `breaker-days` days of failures and waits for a person to enable it (no automatic resume). An event of 65,500 bytes or more is refused (`413`; 65,499 is the largest, less 11 and the length of its stored type, and less with an `Idempotency-Key`). `delivery.seg` is never compacted. A lost database connection is never reopened (`/readyz` says `database` until the service is restarted). The stop waits for no more than `stop-deadline-ms` and leaves the listening socket open until it exits (new connections are answered `503` and closed). At most 65,536 idempotency keys; the index is rebuilt by reading the whole events log at start, **and every fire of a schedule uses one**: a schedule of every minute uses them all in 45 days, after which fires are refused (counted in `cron_errors`) rather than made without their key. Schedules: UTC only, at most 64, one service per database, and a jump of the machine's clock is a stop or a restart as far as they can tell. Not for production.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src`, the unit tests and the harnesses above. Design before code,
in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
