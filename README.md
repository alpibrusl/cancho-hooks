# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys): you `POST` it an event, it stores the
event durably, and it delivers the event, **signed**, to every subscribed endpoint, **at least once**, retrying on a schedule
and keeping what it could not deliver as a dead letter.

It keeps its two logs in [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and talks to PostgreSQL through
[`lexsys-pg`](https://github.com/alpibrusl/lexsys-pg). No `unsafe`, and foreign authority only through `Ffi`, per library, for exactly the symbols
[`docs/authority.json`](docs/authority.json) lists, 33 in all (**the report is pinned in CI**: `scripts/check-authority.sh` regenerates it and fails on any difference, so a new foreign symbol is a red diff that only a commit of the new file turns green):

* **libc**, one function, `statx`, which reads the mode of the data directory for the production profile (`src/perm.ls`; lex-sys has no file-mode builtin: lex-sys#243).
  How the service learns it was asked to stop is not foreign: it claims `SIGINT` and `SIGTERM` through lex-sys's signals capability (`Signals("INT,TERM")`, `src/ops.ls`) and
  watches the claim in the same poller as its sockets, so a stop wakes the loop at once.
* **libssl and libcrypto (OpenSSL), 32 functions**, for the TLS client of an `https` endpoint (`src/tls.ls`, section 40): `libssl` `SSL_CTX_new`, `SSL_CTX_free`, `SSL_CTX_ctrl`,
  `SSL_CTX_set_verify`, `SSL_CTX_set_default_verify_paths`, `SSL_CTX_load_verify_file`, `TLS_client_method`, `SSL_new`, `SSL_free`, `SSL_set_bio`, `SSL_set_connect_state`,
  `SSL_do_handshake`, `SSL_read`, `SSL_write`, `SSL_shutdown`, `SSL_get_error`, `SSL_ctrl`, `SSL_get0_param`, `SSL_get_verify_result`, `SSL_session_reused`, `SSL_get1_session`,
  `SSL_set_session`, `SSL_SESSION_is_resumable`, `SSL_SESSION_free`; `libcrypto` `BIO_s_mem`, `BIO_new`, `BIO_read`, `BIO_write`, `ERR_clear_error`, `ERR_get_error`,
  `X509_VERIFY_PARAM_set1_host`, `X509_VERIFY_PARAM_set_hostflags`. No socket reaches OpenSSL (it works on two memory buffers, and the sockets stay lex-sys connections), and no
  function that does anything but TLS is declared. **OpenSSL is C code in the process, and the report cannot say what it does** with the bytes and the memory it is given. It is
  the choice made to have `https` now; the TLS of lex-sys itself (`packages/tls`, lex-sys epic #197) replaces it when it can verify certificate chains (lex-sys #206) and
  has been independently reviewed (#209), and the 32 symbols go with it.

The authority report (`lex-sys authority`) therefore says `bounded: false`, with the symbols above under `unbounded_by`, each with the library the program says it is in (a library
is not an authority domain: the labels bound everything except what those symbols do; `docs/foreign-authority.md` in lex-sys), and names the two signals. A file-mode builtin in lex-sys
would remove the libc part; the pure TLS would remove the rest.

## Status

**Working:** durable ingest (`202` only after the flush that covers the event; requests that arrive together share one flush), delivery to several endpoints with [Standard Webhooks](https://www.standardwebhooks.com) signatures checked against the reference library, retries on the Standard Webhooks schedule, dead letters, and every outcome (with the time of the next attempt) surviving a crash. A slow, silent or unreachable endpoint costs the others almost nothing: delivery attempts do not hold the loop (up to 64 in flight, a state machine each), so ingest stays at a median of 2.3 ms and healthy endpoints see their deliveries within milliseconds ([`docs/design.md`](docs/design.md) section 16). That used to hold only until the unreachable endpoint was 1,024 events behind, when every endpoint stopped with it (measured: beside a dead endpoint a healthy one stopped at event 1,024 of 3,000, `docs/design.md` section 29). **Each endpoint now reads the events log from its own cursor and is bounded only by its own window** (same probe: 3,000 of 3,000; section 31), and a **circuit breaker** pauses an endpoint whose every attempt has failed for 5 days (`breaker-days`, 0 turns it off): its events wait in the log until `POST /endpoints/:id/enable`. A client may send an `Idempotency-Key` ([section 17](docs/design.md)); an event can be replayed to one endpoint or all (section 23); a `410 Gone` disables an endpoint (section 22); with PostgreSQL the endpoints are a table, every ended attempt is a row, and an endpoint can be created, changed and deleted without a restart (`POST`, `PATCH`, `DELETE /endpoints`) behind an admin token (sections 24 and 25). **Cron:** with PostgreSQL a schedule (`POST /schedules`: a five-field cron expression, an event type and a body; UTC) appends an ordinary event at each scheduled second, exactly once even if the service is killed in the middle of a fire, and fires once for the time it was stopped (section 32). **Operating it:** `GET /readyz` (200 only when the logs are open and take a write, a named database has a live connection, and the service is not stopping; a 503 says why), `GET /metrics` in the Prometheus text format (behind the read token when one is configured; ingest, group commits, attempts by outcome and by the reason they failed, per-endpoint lag, retries waiting, the history, the logs' sizes), the reason an attempt failed in the log, in `GET /events/:id/attempts` and in the history table, and `SIGTERM` or `SIGINT` that drains and exits 0 within `stop-deadline-ms` (a second signal ends it at once). A log with damage in the middle, or a `delivery.seg` that is ahead of its `events.seg`, is **refused at start with a status of its own** instead of being cut or delivered into silence (design.md section 34.5; `--repair-logs 1` cuts the damage, keeping what it cut). **Credentials:** every route has a scope, and three bearer tokens (`ingest`, `read`, `admin`) say who may call what; `production = 1` makes the service refuse to start in a configuration that is unsafe on the internet (section 33, and "Securing it" below). **Per endpoint** you can choose which event types it is sent (`invoice.paid`, `user.*`), keep the previous secret valid for a stated time while a secret is rotated (every delivery then carries both signatures), and have it sent custom headers such as an `Authorization` (section 35; [below](#per-endpoint-event-types-secret-rotation-and-headers)). **Delivery to `https` endpoints** (TLS 1.2 or 1.3, the certificate chain and the host name verified, every failure its own reason, sessions resumed) **and to host names** (resolved by the service, on the poller, with the address checked at every attempt: SSRF; section 40; [below](#https-endpoints-and-names)). **The logs are bounded by retention, not by history** (`retention-days`, 30 by default): an event is dropped, a segment at a time, only when it is delivered or dead-lettered at every endpoint and old enough, and a paused, disabled or dead endpoint, or a waiting replay, keeps its events (section 38). **A database that goes away comes back by itself**, and the start does not wait for it (section 37). **Dead letters** can be listed and replayed in bulk, a waiting replay can be cancelled, retries are spread by `retry-jitter`, and each endpoint can have a concurrency and a rate of its own (section 39). **Backup and restore** of the two logs and the tables (`scripts/backup.sh`, `scripts/restore.sh`), a `Dockerfile` and a systemd unit (`deploy/`) are in the repository ([`docs/runbook.md`](docs/runbook.md)). **Not for production**; what is not built, and what is left before that line can be lifted, is in [What it is not (yet)](#what-it-is-not-yet).

## What it is not (yet)

Each item is from [`docs/production.md`](docs/production.md) and the design document, which also say how each was or was not verified.

* **Not for production.** The project's own list of what is left before that line is lifted: a **soak test of at least 24 hours under chaos** (kills, a dead endpoint, a slow database, with memory, descriptors and disk watched, and its numbers published), a **capacity page** that gives the method behind the figures under [Measured](#measured), and the endpoint limit below. Neither the soak nor the page is built: no run of 24 hours has been made, and the figures under Measured come from short runs on shared machines.
* **At most 62 endpoints.** <!-- endpoint-limit: the one place the limit is stated as an open item --> A larger limit is being worked on.
* **OpenSSL, until lex-sys has its own TLS.** Delivery to an `https` endpoint drives OpenSSL in the process (32 foreign functions, pinned in `docs/authority.json`; the authority report says `bounded: false` for them). It stays until lex-sys's own TLS can verify certificate chains and has been independently reviewed. Not built for `https`: revocation checks, client certificates, IPv6, keeping a connection open between deliveries (each delivery costs a handshake), `/etc/hosts`, UDP or a second name server, a per-name allow-list for private addresses, an `https` endpoint at an IP address. A real public certificate chain was not exercised, only throwaway authorities.
* **The database.** A host *name* for `pg-host` stalls the loop while it resolves (give an address). The `endpoints` table is read once: a change made behind the service's back, or one whose outcome it did not learn (a `504`), is seen at the next start. The history (`attempts`) is best effort and is not pruned.
* **Its own listener and its credentials.** No TLS for the service's own port (put a reverse proxy in front), no audit log, no rate limit on failed tokens, a token changes only with a restart, and signing secrets and header values are stored in the clear: the database is the trust boundary.
* **Operating it.** One process, one thread, one core. An endpoint paused by the circuit breaker is resumed only by a person. An event dropped by retention is gone (`404`, no tombstone). The `Dockerfile` was built and run by hand and is not built in CI; the systemd unit was checked with `systemd-analyze` and never run under a real systemd; the release script does not sign, has no stable URL and writes a stub where an SBOM would be; the only binary is the CI artifact, for Linux x86-64; macOS is not verified.

## Requirements

- The **lex-sys** compiler at the commit `lex-sys.toml` names (`[package] lex-sys`); `lex-sys build` refuses any other. It needs `clock_unix_ms` (the signing timestamp; lex-sys PR #190),
  `tcp_connect_start` (attempts that do not wait; #191), a lock with an origin (#192), the project file (#193), and `std.hmac`, which signs
  every delivery and replaced this repository's own HMAC (#229; [`docs/design.md`](docs/design.md) section 30).
- `git`: [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and lex-sys's `http-server` are not cloned by hand; they are dependencies in `lex-sys.toml`, pinned to a commit each, and `lex-sys build` fetches and checks them.
- Rust, to build the compiler; `gcc`, to build the three small shims the tests preload (`fsync`, for the crash tests; `statx`, for the production profile's; `send` and `recv`, for the partial-I/O tests).
- OpenSSL 3.0 or later: its development files (`libssl-dev`) to build, because the service is linked against `libssl` and `libcrypto` (`scripts/build.sh` does it; a plain `lex-sys build` stops at the link), and `libssl3` and `ca-certificates` to run it.
- To run the tests: `python3` and `pip install standardwebhooks` (the independent implementation signatures are checked against), the `openssl` command (the tests of `https` make their own certificate authorities), and, for the tests of the database, a PostgreSQL and `psql`.

## Quick start

```sh
git clone https://github.com/alpibrusl/lex-sys                          # the compiler, and nothing else to clone
git clone https://github.com/alpibrusl/lexsys-hooks && cd lexsys-hooks

REV=$(sed -n 's/^lex-sys *= *"\(.*\)"/\1/p' lex-sys.toml)                 # the compiler these sources were written for
(cd ../lex-sys && git fetch -q origin && git checkout "$REV" && cargo build --release -p lex-sys)
export LEX_SYS=$PWD/../lex-sys/target/release/lex-sys

scripts/build.sh                      # installs the libraries in lex-sys.toml, builds build/hooks (linked against OpenSSL) and the probes, and the shims the tests preload

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

* **the endpoints**: they are the `endpoints` table, read once, as soon as the database answers. `endpoints.conf` is **not read** when a
  database is named. The start does not wait for the database: the service listens at once and takes events (`202`, stored in the log),
  but delivers nothing and answers `503` on the routes about endpoints, and `/readyz` says `database`, until it has read the table;
  then it delivers what it was given meanwhile. It does not guess: a stale or partial list would deliver to the wrong receivers. A login
  the server refuses for good (a wrong password, a role or a database that does not exist, a table that is not there) ends the start at
  once with status 20 and what failed; a database that cannot be reached or does not answer ends it with status 20 after `pg-start-wait-ms`
  (30 s; `0` waits for ever). Once the table has been read the service never ends because of the database.
* **the history**: a row for every delivery attempt that ends (the receiver's status, the outcome, the attempt's number, when and how
  long), read back by `GET /events/:id/attempts`. The log files stay the truth about delivery, so the history is **best effort**: the
  service delivers while the database is slow or gone, and counts the rows it could not write (`/stats`).

**A database that goes away comes back by itself.** The service keeps two connections to it, remakes one that is lost without waiting
(the waits between attempts start at `pg-backoff-min-ms` and double to `pg-backoff-max-ms`; a connection that was live for a second is
replaced at once), prepares its statements again, and `/readyz` is `200` again when one is live: no restart. While none is, the rows of
the history wait in a queue of 256 (the 257th is dropped and counted; a row that was on a connection when it went is counted `failed`:
whether it was written is not known), a change to an endpoint or a schedule is a `503` at once, one that was on the wire when the
connection went is a `504` "may have been stored", and the schedules wait (no event is made twice: the key is in the log). Only a name
for `pg-host` can stall the loop (the resolver is a call that waits): give an address.

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
# <id> <host> <port> <secret> [types=<patterns>] [headers=<spec>] [old=<secret>@<until>] [concurrency=<1 to 8>] [rate=<1 to 100000>]
0 127.0.0.1 9000 whsec_... types=invoice.paid,user.* headers=Authorization:Bearer%20abc,X-Api-Key:k
1 127.0.0.1 9001 whsec_... old=whsec_...@1791100000000 concurrency=2 rate=20
```

`types=` is the comma separated patterns; `headers=` is `Name:value` pairs separated by commas, each value percent-encoded (every byte but `A-Za-z0-9-._~` as `%XX`: a space is `%20`, a comma `%2C`, a percent `%25`), which is also how the `headers` column of the table holds them; `old=` is the previous secret and the Unix ms until which it is signed with too; `concurrency=` and `rate=` are the endpoint's own limits ("Pace" below). A word that is none of these, or one twice, or one that is not good, is a bad line (status 13, naming the line); `--import-endpoints` copies the words into the table. The table is read as one text of at most 32 KiB, so 62 endpoints with large header sets do not all fit (`POST` answers `507` before the table would not start).

## Pace, dead letters and retries

**Pace.** Two limits keep one endpoint from taking more of the service, or of the receiver, than you mean to give it. They are settings for every endpoint (`endpoint-concurrency`, `endpoint-rate`) and each endpoint can have its own (`"concurrency"`, `"rate"` in `POST` and `PATCH /endpoints`, the columns and the words `concurrency=` and `rate=` of `endpoints.conf`; `0` or `null` follows the setting again; `GET /endpoints` shows the limit in force).

* `concurrency` is the most attempts the endpoint has in flight together: 1 to 8, the default 8 (it was a fixed 8). A receiver that can take two requests at a time is given two.
* `rate` is the most attempts the endpoint **starts** in a second: 1 to 100,000, `0` (the default) no limit. It is a token bucket worked on the clock the loop already has, a tenth of a second deep (so a limit of 5 allows one attempt at once and then one every 200 ms, and no second sees more than the rate plus one tenth of it). **An event held back by it waits; it does not fail.** No attempt is made, nothing is written to the log, the retry counter does not move, the event is not lost and the other endpoints are not slowed: the loop looks at an endpoint's window until its bucket is empty and goes on to the next, and it sleeps no longer than the time the soonest bucket needs. `/metrics` counts the attempts that were held back (`hooks_endpoint_throttled_total{endpoint}`). The bucket is memory: a restart starts every bucket full, so a limit can be exceeded by one bucket (a tenth of a second's worth) across a restart.

**Retry jitter.** The schedule is the Standard Webhooks one (5 s, 5 min, 30 min, ...). A receiver that was down for a minute fails every event that arrived in it at about the same moment, and the schedule would ask for all of them again at the same moment, and again at the next step. `retry-jitter` moves each delay up or down by up to that many percent of itself (0 to 50; the default **10**, so a 5-minute delay is 270 to 330 seconds). The move is a fixed function of the endpoint, the event and the attempt (no random source), and the time of the next attempt is written to the log with the outcome, so a restart keeps the time that was chosen, whatever the setting is by then. `0` is the schedule exactly. A replay's retries are moved the same way. The number of attempts before a dead letter is the schedule's, always.

**Dead letters.** An event is a *dead letter* at an endpoint when its last word there is `dead` (the schedule ran out, or the receiver answered `410`) and no replay of it has delivered it since. The service keeps the newest 2,048 of each endpoint in memory, built from `delivery.seg` at the start and kept while it runs:

```
$ curl localhost:8080/endpoints/3/dead?limit=2
{"endpoint":3,"order":"desc","held":5,"truncated":false,"complete_above":0,
 "dead":[{"event":5,"type":"user.created","attempts":10,"reason":"status_5xx","died_at":1791133036458,"replaying":false},
         {"event":4,"type":"invoice.paid","attempts":10,"reason":"connect_refused","died_at":1791133036457,"replaying":false}],
 "next":4}
$ curl -XPOST localhost:8080/endpoints/3/replay-dead -d '{"types":["user.*"],"limit":10}'
{"endpoint":3,"taken":3,"remaining":0,"waiting":3,"next":5}
```

* **The list** is newest first (`order=desc`, the default) or oldest first (`order=asc`); `limit` is 1 to 1,000 (100); `after=<event id>` goes on past that event in the order chosen, and `next` is the `after` for the page after this one (`null` at the end). The cursor is an event id, so a page is stable while dead letters are added or taken out. `died_at` is the Unix ms of the death (0 for a death recorded before the time was written); `reason` is one of the reasons of `GET /events/:id/attempts`; `replaying` says that a replay of it is waiting. `type` is `null` for an event that has none.
* **The bound.** An endpoint holds its 2,048 dead letters with the largest event ids. If it has more, `truncated` is true and `complete_above` says that every dead letter above that event id is in the list; the older ones are in the log, and they enter the list as soon as there is room (a replay that delivers frees an entry; the next look at the list completes it from the log, which reads `delivery.seg` once), until the log is replaced by a snapshot (retention, below): a snapshot keeps the 2,048 an endpoint holds, and an older dead letter that was left out is still replayable by its event id (`POST /events/:id/replay/:endpoint`) while the event is kept. **A dead letter lasts as long as its event**: when retention deletes the event it leaves the list. The four routes answer `503` after a start until the endpoints have been read from the database.
* **Replay in bulk** sends the dead letters again, oldest first, as replays (the same `webhook-id`, the same schedule, `POST /events/:id/replay`). At most 32 replays wait at once, so one call takes as many as there is room for and no more than `limit` (an optional 1 to 2,048): `taken`, `remaining` (the dead letters that match and are not already replaying, and were not taken) and `waiting` (replays waiting in all) say what happened. Call again until `remaining` is 0. `taken` 0 with `remaining` above 0 means the table of 32 is full: wait for the replays to finish, or cancel some. `types` takes only the dead letters whose type matches one of the patterns (as an endpoint's subscription does; `[]` is every type), `after` only those with a larger event id (how a caller goes on past ones it has seen), and a member that is none of these is a `400`. `202` when something was taken, `200` when not. A dead letter that is replaying is not taken again, and one whose replay dies again is a dead letter again with its new attempts and time.
* **Cancel.** `DELETE /events/:id/replay/:endpoint` takes back one waiting replay and `DELETE /endpoints/:id/replays` all of an endpoint's: `{"cancelled":N,"busy":M}`. A replay with an attempt on the wire is not cancelled (`409` for one, counted in `busy` for all): ask again when it has ended. A cancellation is written to the log and flushed before the answer, so it survives a crash and a power cut; the event stays a dead letter and can be replayed again. `404` for a replay that is not waiting.

## `https` endpoints and names

An endpoint is delivered to over TLS when its host starts with `https://` as it is written in `endpoints.conf` and in the `endpoints` table (a name is needed: the certificate is checked against it, and
it is what SNI carries), and over the API as a `scheme` or a `url` (`docs/design.md` section 40):

```
# <id> <host> <port> <secret> ...
7 https://hooks.example.com 443 whsec_...            # TLS 1.2 or 1.3, the chain and the name verified
8 receiver.internal 8080 whsec_...                    # a name, plain HTTP
curl -XPOST -H "Authorization: Bearer $ADMIN" -d '{"url":"https://hooks.example.com"}' localhost:8080/endpoints     # port 443; {"host":..,"port":..,"scheme":"https"} is the same
curl -XPATCH -H "Authorization: Bearer $ADMIN" -d '{"scheme":"http"}' localhost:8080/endpoints/7                       # only the scheme changes
```

* **Verification.** TLS 1.2 at least; the certificate chain must lead to a root in the **system's trust store** (OpenSSL's default locations, `SSL_CERT_FILE` and `SSL_CERT_DIR` honoured), or, with
  `tls-ca-file`, to **exactly** the PEM file named (not the system's as well: use it for a private authority, or for a test). The name in the endpoint is checked against the certificate and is sent as SNI.
  There is no setting that turns verification off. A trust store that cannot be loaded stops the start (status 21). Revocation is not checked; no client certificate.
* **Every failure has a reason of its own**, recorded like any failed attempt (retried on the schedule, dead after it; `GET /events/:id/attempts`, `GET /metrics`, the delivery log): `cert_untrusted`,
  `cert_expired` (also not yet valid), `cert_hostname`, `cert_invalid`, `tls_handshake` (the peer closed or spoke badly, or offers only TLS 1.1 or less), `tls_timeout`, `tls_error`; and for names `dns_failed`,
  `dns_timeout` and `ssrf_refused`.
* **Names** are resolved by the service itself, without blocking the loop, by asking **one name server over TCP** (`dns-server`, else the first IPv4 `nameserver` of `/etc/resolv.conf`). At the write a name only has
  to be a name; **at every attempt** every address it resolves to must be public (unless `allow-private-hosts 1`), and the connection goes to the address that was checked, with nothing resolved in between, so a name
  that is made to point at `127.0.0.1` or `169.254.169.254` is a failed attempt with the reason `ssrf_refused` and no connection (DNS rebinding changes nothing). No `/etc/hosts` (except `localhost`, which is the
  loopback), no search list, no IPv6 (A records only; a name with no IPv4 address is `dns_failed`), no UDP.
* **Sessions.** The service closes the connection after every delivery (`Connection: close`), so `https` costs a handshake a delivery. It keeps one TLS session per endpoint, in memory, and resumes it
  (an abbreviated handshake: about a third less CPU per delivery); it is dropped when the endpoint is changed or deleted, never offered to another name or port, and `tls-resume 0` turns it off. `/metrics` has
  `hooks_tls_handshakes_total{result="full|resumed"}`.
* **Cost** (one shared 4-core VM, the service pinned to a core, 10 endpoints, CPU of the service per delivery; `scripts/bench/https_cost.py`): an address over plain HTTP about 100 us, a name 170 us, `https` with a full
  handshake about 1,000 us, `https` resumed about 700 us. One core therefore does roughly 10,000 plain deliveries a second, 1,000 over `https` and 1,400 over `https` resumed. A lookup that is slow, or a handshake
  that never ends, holds nothing: the longest wait of a request to the service while 64 of either were pending was 1 ms (`tests/https_test.py`, `tests/names_test.py`).
* **Needs** OpenSSL 3.0 or later at run time (`libssl3`), its development files to build (`libssl-dev`), and `ca-certificates` for the system's store. The authority this adds is listed above and pinned in
  `docs/authority.json`. The pure lex-sys TLS (lex-sys epic #197) replaces OpenSSL when it can verify chains.

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
{"endpoints":1,"attempts":1,"delivered":1,"failed":0,"dead":0,"keys":0, ... }   # and the other counters: see `GET /stats` in the HTTP API table
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
# <id> <host> <port> <secret> [types=...] [headers=...] [old=...] [concurrency=...] [rate=...]   (the words after the secret: see "Per endpoint" and "Pace")
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
| `pg-backoff-min-ms`, `pg-backoff-max-ms` | `100`, `5000` | the wait after a failed attempt to connect starts at the first (1 to 600,000) and doubles up to the second (1 to 3,600,000, not below the first); per connection (design.md section 37) |
| `pg-attempt-ms` | `5000` | the longest one attempt (dial, login, prepare the statements) may take (1 to 600,000); a server that accepts and says nothing costs this much |
| `pg-request-ms` | `10000` | the longest a request may wait with no byte coming back before its connection is given up and remade (0 to 3,600,000; `0` never). Longer than the slowest query: the service itself answers a held request after 5 s |
| `pg-start-wait-ms` | `30000` | how long the service may run without having read its endpoints from the database before it ends with status 20 (0 to 86,400,000; `0` waits for ever) |
| `allow-private-hosts` | `0` | `1`: an endpoint's host may be an address in a private, loopback, link-local or reserved range, and a name may resolve to one. With `0` (the default) an address must be public in `endpoints.conf`, in the table and in `POST /endpoints`, and **a name must resolve to public addresses only, which is judged at every attempt** (`ssrf_refused`; design.md sections 26 and 40). A host that is neither an address nor a name is refused either way |
| `admin-token` | (none) | the bearer token of the **admin** scope: everything that changes configuration or state (create, change and delete endpoints, enable, replay, the schedules), and it also does what the other two do. 8 to 255 visible characters; without it `POST`, `PATCH` and `DELETE /endpoints` and the schedules are a `403`. Anyone who has it can choose where the service sends requests, within what `allow-private-hosts` allows (by default public addresses only), so keep it secret and put it in the settings file, not on the command line |
| `ingest-token` | (none) | the token of the **ingest** scope: `POST /events` and so its idempotent retries; same rule as `admin-token`. Without it `POST /events` is open |
| `read-token` | (none) | the token of the **read** scope: `GET /events/:id`, `/events/:id/attempts`, `/endpoints`, `/endpoints/:id`, `/stats`, `/config`, `/metrics`; same rule. Without it those are open (in production: they need the admin token) |
| `production` | `0` | `1`: refuse to start unless `admin-token` and `ingest-token` are set and different from each other and from `read-token`, `allow-private-hosts` is `0`, and the data directory and its files cannot be read or written by the group or by others. One exit status for each cause (30 to 35), and a line on stderr that names the setting or the path ("Securing it" below) |
| `breaker-days` | `5` | pause an endpoint whose every attempt has failed for this many days (0 to 36,500; `0` is off). It is disabled as a `410` disables it, and `GET /endpoints` says `"paused":true`; its events wait in the log and are sent when a person enables it. Counted from the first failed attempt after a delivery, checked when an attempt fails (design.md section 31) |
| `retention-days` | `30` | drop events that are **final at every endpoint** (delivered or dead-lettered) and older than this many days, a whole segment at a time; `0` keeps them for ever. An event a paused, disabled or dead endpoint, or a waiting replay, still needs is never dropped. After the drop `GET /events/:id` and a replay of it are a `404` and its `Idempotency-Key` is forgotten (docs/retention.md, design.md section 38) |
| `segment-bytes`, `delivery-log-bytes` | `67108864`, `33554432` | the events log is sealed into a new segment at this size (256 KiB or more), and `delivery.seg` is replaced by a snapshot of the delivery state at this size (64 KiB or more) or four times the last snapshot |
| `idem-keys` | `262144` | how many idempotency keys the index holds (16 to 4,194,304; resident only as keys are held: 30 MB for 200,000); a key leaves when its `window-ms` has passed or its event is dropped |
| `compact-now` | `0` | `1`: do one maintenance pass (seal, drop what may be dropped, replace `delivery.seg` by a snapshot), print what was done and exit; for an operator, and it takes the same lock as the loop |
| `retry-jitter` | `10` | how far each retry delay is moved, up or down, in percent of itself (0 to 50; `0` is exactly the schedule). A function of the endpoint, the event and the attempt, fixed when the failure is recorded (design.md section 39.3) |
| `retention-ms`, `compact-kill-at` | `0`, `0` | **test knobs** (`tests/retention_test.py`): retention in ms instead of days; stop at a numbered step (1 to 64) of a compaction and wait there, writing `killpoint` in the data directory, so that a test can kill the service at that step. Not for use outside the tests |
| `endpoint-concurrency` | `8` | the most attempts one endpoint has in flight together (1 to 8); an endpoint's own `"concurrency"` replaces it (section 39.4) |
| `endpoint-rate` | `0` | the most attempts one endpoint starts a second (0 to 100,000; `0` is no limit); an endpoint's own `"rate"` replaces it. A held-back event waits, it does not fail (section 39.4) |
| `import-endpoints` | `0` | `1`: copy `endpoints.conf` into the database and exit (needs `pg-host`; no `port` needed) |
| `cron-catchup` | `1` | `1`: a schedule that missed fires while the service was stopped fires once for them; `0`: it skips them (design.md section 32) |
| `rotation-grace-ms` | `86400000` | how long the previous secret is still signed with after `PATCH /endpoints/:id` with `"keep_old": true` (1 to 2,592,000,000; `keep_old_ms` names another period for one change) |
| `dns-server` | the first IPv4 `nameserver` of `/etc/resolv.conf`, port 53 | `ip` or `ip:port`: the name server that resolves the names of endpoints, asked over TCP (design.md section 40) |
| `tls-ca-file` | (the system's trust store) | a PEM file of certificates; with it an `https` endpoint's chain must lead to one of **these** and not to the system's. A file that cannot be read or holds none is a refusal to start (status 21) |
| `tls-resume` | `1` | `1`: keep a TLS session for each `https` endpoint and resume it; `0`: a full handshake and a full verification every time |
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
| `POST /events` | ingest | a JSON object with a string `"type"`, and optionally an `Idempotency-Key` (1 to 255 visible ASCII characters); answers `202 {"id":N}` after the flush, `422` for a body that is not one or a key already used for a different event, `400` for a bad or doubled key, `413` for an event too large (over 65,499 bytes, less 11 and the type's length for its stored type, less 28 and the key's length with a key), `507` for a new key when `idem-keys` (262,144 by default) are held and fresh, `503` if the log is broken |
| `GET /events/:id` | read | the stored event, `404` if there is none |
| `GET /stats` | read | `{"endpoints","attempts","delivered","failed","dead","keys","replays","draining","paused","breaker_trips","history_live","history_written","history_failed","history_dropped","history_queue","endpoints_loaded","database_reconnects","database_failures","database_losses","cron_fired","cron_errors","cron_skipped","filtered"}` (`endpoints_loaded`: the endpoints have been read from the database, always true without one; `database_*`: connections made again, attempts to connect that failed, connections lost, since the start; `paused`: endpoints the breaker has paused now; `breaker_trips`: pauses since the start; `filtered`: events passed over for an endpoint that does not subscribe to their type, since the start); and what retention did: `"cron_keys","events_first_id","events_last_id","events_segments","events_bytes","events_dropped","segments_dropped","segments_sealed","delivery_bytes","snapshots","maintenance_ms_max","maintenance_errors","maintenance_lock_skips","events_skipped"` (docs/runbook.md 3.2) |
| `POST /events/:id/replay` | admin | send the event again to every endpoint that subscribes to its type; `/replay/:endpoint` for one, whatever it subscribes to. `202 {"event","endpoints"}`, `404` for an unknown event or endpoint, `507` if 32 replays already wait. Same `webhook-id`, same schedule (design.md section 23) |
| `DELETE /events/:id/replay/:endpoint` | admin | take back the waiting replay of the event for the endpoint. `200 {"event","endpoint","cancelled":1,"busy":0}`; `409` while an attempt of it is on the wire; `404` if no such replay waits (an unknown endpoint too); `400` for an id or endpoint that is not a number. Durable: written to the log and flushed before the answer |
| `DELETE /endpoints/:id/replays` | admin | take back every waiting replay of the endpoint: `200 {"endpoint","cancelled":N,"busy":M}` (`busy`: replays with an attempt on the wire, which stay) |
| `GET /endpoints/:id/dead` | read | the endpoint's dead letters, a page of them: `?limit=1..1000&order=desc|asc&after=<event id>`. `200 {"endpoint","order","held","truncated","complete_above","dead":[{"event","type","attempts","reason","died_at","replaying"}],"next"}`; `400` for a bad parameter, `404` for an unknown endpoint. See "Pace, dead letters and retries" |
| `POST /endpoints/:id/replay-dead` | admin | send the endpoint's dead letters again, oldest first, as replays, as many as the 32 waiting replays have room for: an optional body `{"limit":1..2048,"types":[...],"after":<event id>}`. `202`/`200` `{"endpoint","taken","remaining","waiting","next"}`; call again until `remaining` is 0. `400` for a bad body, `404` for an unknown endpoint |
| `GET /events/:id/attempts` | read | the attempts of an event from the database, as `[{"endpoint","replay","attempt","outcome","status","reason","at","latency_ms"}]`; `reason` says why a failed attempt failed (`connect_refused`, `connect_timeout`, `no_response` (the deadline passed before a status line), `reset`, `closed_early`, `bad_response`, `status_3xx`, `status_4xx`, `status_5xx`, `gone`, and the rarer ones: design.md section 34.3), `none` for a delivery, `unrecorded` for a row written before reasons were; `status` is the HTTP status, or the coarse -1 could not connect, -2 could not send, -3 timed out, -4 no answer; `503` if no database is named or it cannot answer, `504` after five seconds |
| `GET /endpoints` | read | each endpoint's `{"id","port","scheme","cursor","disabled","paused","failing_since","types","headers","secret_old_until","concurrency","rate"}`: `concurrency` and `rate` are the limits in force (the endpoint's own, or the service's), `paused` is true when the circuit breaker is why it is disabled, `failing_since` is the Unix time in ms at which its current run of failed attempts began (0 if none), `types` its event type patterns (`[]`: every event), `headers` the **names** of its custom headers, `secret_old_until` the Unix ms until which the previous secret is signed with as well (0: none). `scheme` is `"http"` or `"https"`. Not the host, not a secret, not a header's value |
| `GET /endpoints/:id` | read | one endpoint, as `GET /endpoints` lists it; `404` for an unknown id, `400` for one that is not a number |
| `POST /endpoints` | admin | create an endpoint (needs a database and an `admin-token`): `{"host","port"}` (a name or an address) or `{"url":"https://host:port"}`, optionally with `"scheme":"https"`, and optionally `"secret"` (`whsec_` and base64; the service makes one if it is left out), `"types"` (event type patterns), `"headers"` (custom headers: see "Per endpoint"), `"concurrency"` and `"rate"` (its own limits: "Pace") and `"from":"now"`. `201 {"id","host","scheme","port","secret","from","cursor"}`: **the secret is in this answer and in no other**. The endpoint gets the events from now on, not the log's past. `403` if the service has no `admin-token`, `401` without `Authorization: Bearer <token>`, `400` with the reason for a bad request, `409` if another change waits or 62 exist, `503` if no database is named or it refused, `504` after five seconds (the row may still have been stored: it is an endpoint at the next start) |
| `PATCH /endpoints/:id` | admin | change an endpoint (needs a database and an `admin-token`): any of `"url"`, `"host"`, `"port"`, `"scheme"`, `"secret"` (`whsec_` and base64), `"rotate": true` (the service makes a new secret; not with `"secret"`), `"types"`, `"headers"`, `"concurrency"`, `"rate"`, and `"keep_old_ms"` or `"keep_old": true` (keep the secret this one replaces valid for that long, both signatures on every delivery meanwhile: see "Per endpoint"). `200 {"id","host","scheme","port"}`, and `"secret"` when the change brought or made one: **it is in this answer and in no other**; `"secret_old_until"` when a previous secret is kept. The next attempt uses the new address and secret, a retry of an event first tried under the old secret included; an attempt already on the wire finishes against the old address; a saved TLS session of the endpoint is dropped. Without a period there is one signature, so a receiver that has not been given the new secret refuses until it has. `404` for an unknown id or a row that is gone, `409` while another change waits, `400` with a reason for a body that is not a change (the same host rule as `POST`), `503` if the database refuses (nothing changes) |
| `DELETE /endpoints/:id` | admin | remove an endpoint (needs a database and an `admin-token`): the row is deleted first, and only when the database says commit does the service change. `200 {"id","deleted":true,"draining":bool}`. **No new attempt starts** for the endpoint from then on, and it is out of `GET /endpoints` at once; an attempt already on the wire finishes and is recorded (log and history, under the endpoint's id) as for any endpoint, and its slot cannot be reused until it has (`"draining":true`, `/stats` says how many); replays waiting for it are dropped; its rows in the history are kept; its id is never given again. A row that was already gone is removed from the service all the same (`200`, with `"row":"was already gone"`). `404` for an unknown id, `400` for one that is not a number, `403`/`401` as for `POST`, `409` while another change waits, or while all 62 slots are taken (one of them draining), `503` if the database refuses (nothing changes), `504` after five seconds (the row may still go: the next start reconciles) |
| `POST /endpoints/:id/enable` | admin | enable an endpoint a `410` or the circuit breaker disabled (it also ends the run of failures); `200` whether or not it was, `404` for an unknown id |
| `GET /config` | read | the settings in force: `{"schedule":[ms,...],"deadline-ms","window-ms","allow-private-hosts","breaker-days","production","cron-catchup","cron-seconds","stop-deadline-ms","repair-logs","rotation-grace-ms","retention-days","segment-bytes","delivery-log-bytes","idem-keys","pg-backoff-min-ms","pg-backoff-max-ms","pg-attempt-ms","pg-request-ms","pg-start-wait-ms","retry-jitter","endpoint-concurrency","endpoint-rate"}` (not the endpoints, not their secrets, never a token, and not yet `dns-server`, `tls-ca-file` or `tls-resume`) |
| `POST /schedules` | admin | create a schedule (needs a database and an `admin-token`): `{"expr","type"}` and optionally `"body"` (any JSON, at most 1,024 bytes, default `{}`) and `"enabled"` (default true). `201` with the schedule: `{"id","expr","type","body","enabled","created_at","last_fired","next_fire","next_fire_at"}`. A schedule counts from now: it does not fire for the time before it existed. `400` with the reason for a bad request, `409` at 64 schedules |
| `GET /schedules`<br>`GET /schedules/:id` | admin | the schedules, or one, as above; `next_fire` (Unix seconds) and `next_fire_at` (UTC text) are the next scheduled second after now, `null` if disabled. Admin token needed for these too (a body is a payload). `404` for an unknown id |
| `PATCH /schedules/:id` | admin | any of `"expr"`, `"type"`, `"body"`, `"enabled"`. A changed expression, or `"enabled": true` on a disabled schedule, counts from now (no fires for the time between). `200` with the schedule, `404`, `400` |
| `DELETE /schedules/:id` | admin | `200 {"id","deleted":true}`, `404` |
| `GET /healthz` | open | `{"ok":true}` while the process is up and its loop turns. It looks at nothing: it says `200` with the disk full. Open whatever is configured |
| `GET /readyz` | open | `200 {"ready":true}`, or `503 {"ready":false,"check":...,"reason":...}` with `check` one of `stopping`, `events_log` or `delivery_log` (a write or a flush failed: restart after freeing the disk), `data_dir` (the data directory does not take a write, checked once a second), `database` (a database is named and no connection to it is live, or its endpoints have not been read yet; it reconnects by itself and this is `200` again when it has). No credential, whatever is configured: this is the container's health check |
| `GET /metrics` | read | the Prometheus text exposition format 0.0.4: [what each series is](#operating-it); needs the read token when one is configured (the admin token also opens it) |

A delivery is `POST /hook` to the endpoint, with the event as the body and three headers: `webhook-id` (`evt_<id>`, the same on
every attempt, so a receiver can drop a repeat), `webhook-timestamp` (Unix seconds) and `webhook-signature` (`v1,` and the base64
HMAC-SHA256 of `<id>.<timestamp>.<body>` under the decoded secret; two, space separated, while a rotation overlaps), then the endpoint's custom headers, if it has any. Any `2xx` is a delivery. Anything else, a timeout or a
refused connection is a failure (a `410 Gone` is the exception: that event is a dead letter at once and the endpoint is **disabled**, no new attempts until `POST /endpoints/:id/enable`); the retries come 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h after the previous
attempt (each moved by up to `retry-jitter` percent, 10 by default), and then the event is a dead letter for that endpoint.

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
* **What it does not do:** no TLS for the service's own port (put a reverse proxy in front for `https` towards your own clients; delivery **to** `https` receivers is built: [below](#https-endpoints-and-names)), no
  per-token rotation without a restart, no rate limit on failed tokens, no audit log of who called what. A token is the whole of the authentication.

## Operating it

**Metrics.** `GET /metrics` answers in the Prometheus text format (counters are since this start; a restart is a reset, which `rate` and `increase` expect). About 70 series for the service and 9 for each endpoint (a tenth, its last failure, once it has failed), labelled by the endpoint's `id` and by nothing else that grows (62 endpoints at most):

| | |
|---|---|
| `hooks_ingest_events_total{result}` | `POST /events`: `accepted` (stored, flushed, `202`), `duplicate` (an `Idempotency-Key` repeat, `202`), `refused`; `hooks_ingest_refused_total{status}` splits the refusals (`400`, `413`, `422`, `503`, `507`, `other`) |
| `hooks_log_commits_total{log}`, `hooks_log_size_bytes{log}`, `hooks_log_synced_bytes{log}` | group commits (turns in which a flush made a log's new records durable) and the size of `events` and `delivery` |
| `hooks_attempts_total{outcome}`, `hooks_attempt_failures_total{reason}` | attempts that ended, `delivered`, `failed` (it will be tried again) or `dead` (a dead letter); and the failed and dead ones by the reason they failed |
| `hooks_attempts_in_flight`, `hooks_retries_waiting`, `hooks_replays_waiting`, `hooks_breaker_trips_total` | on the wire now; events that failed and wait for a retry; replays not finished; times the circuit breaker paused an endpoint |
| `hooks_endpoint_cursor`, `_lag_events`, `_disabled`, `_paused`, `_retries_waiting`, `_in_flight`, `_failing_since_ms`, `_last_failure{endpoint,reason}`, `_throttled_total`, `_dead_letters` | per endpoint: its cursor, the events behind (the newest event's id minus the cursor), disabled (a `410`, a person, or the breaker), paused (the breaker), the events waiting for a retry, attempts on the wire, when its run of failures began, and why its last attempt failed (kept across restarts), attempts held back by its rate limit since the start, and the dead letters it holds (at most 2,048) |
| `hooks_history_queue`, `_connections`, `_rows_total{result}` | the history of attempts: rows waiting (at most 256), live database connections (0 to 2), rows `written`, `failed` (refused, or on a connection that was lost) or `dropped` (the queue was full) |
| `hooks_database_connecting`, `hooks_database_reconnects_total`, `hooks_database_connect_failures_total`, `hooks_database_connection_losses_total`, `hooks_endpoints_loaded` | the connection to the database: being made now; made again after a loss; attempts that failed; live connections lost; and whether the endpoints have been read (`0` until the table has been) |
| `hooks_cron_fires_total`, `_errors_total`, `_skipped_total`; `hooks_events_last_id`, `hooks_idempotency_keys`, `hooks_endpoints`, `hooks_uptime_seconds`, `hooks_ready`, `hooks_stopping` | the schedules, and the rest |

`GET /metrics` is a **read**-scope route: when a `read-token` is configured (or, in production, the admin token), a scraper sends it as `Authorization: Bearer <token>`; without one the route is open, like the other reads ("Securing it" above). `GET /readyz` and `GET /healthz` are always open. What to alert on is in [`docs/runbook.md`](docs/runbook.md) section 1.

**Stopping.** `SIGTERM` (`systemctl stop`, `docker stop`) or `SIGINT`: the service stops taking requests (every write that passes the credential check is a `503` that closes the connection; `GET /readyz` says `stopping`; `GET /healthz`, `/metrics` and the other reads still answer), starts no attempt, lets the attempts on the wire finish for at most `stop-deadline-ms`, flushes both logs and exits **0**. Attempts still on the wire at the deadline are made again at the next start (at least once, as after a crash). A stop is noticed at once, not at the next turn of the loop (the poller waits on the signals too). A second signal ends the process at once, killed by that signal. Nothing is lost either way: an acknowledgement is only sent after the flush that covers the event.

**Exit statuses of a start that ends** (the message is on stderr; the full list with what to do is in `docs/runbook.md` section 1): `0` finished (`--import-endpoints`, or a drain); `2` a setting was refused; `5` `SIGINT` and `SIGTERM` could not be claimed; `10`/`12` `events.seg`/`delivery.seg` could not be opened; `11` the port is taken; `13` an endpoint line or row is invalid; `14` the retry schedule does not parse; `15`/`16` a log this version does not understand; `17` an endpoint could not be given a slot; **`18` `delivery.seg` refers to an event that `events.seg` does not hold** (an older events log beside a newer delivery log: the service would acknowledge new events and never deliver them), **`19` damage in the middle of a log** (not a torn tail: the message says where the log is whole to, how many bytes the cut would take and how many intact records are among them); `20` the database could not be read; `21` the TLS trust store could not be loaded (`tls-ca-file`); `30` to `35` the production profile refused an unsafe setting or mode (the table under "Securing it" above); `40`/`41` a log in a format this version does not know (a newer one), `42` a hole or break in the chain of events segments, `43`/`44` `--compact-now` found the lock held, or a step failed. A refusal with 18 or 19 leaves both logs exactly as they were. A single torn record at the end, which is what a crash leaves, is still cut at start, and now said (after `listening`).

## How it works

One thread, one poller. An accepted event is appended to a [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) segment and its
request held; after the turn one `flush` covers every append and the held requests are answered. Delivery runs in the same loop without holding it: each attempt is a small state machine (connecting, sending, reading) whose connection is watched on the server's own poller, so up to 64 are in flight together and a slow, silent or unreachable endpoint costs the others almost nothing. Each endpoint has a cursor (every event up to it is delivered or dead) and a window of events above it that finished out of order or are waiting for a retry, so a failing event does not hold up the ones after it. What happened to each attempt goes to a second log, `delivery.seg`, which a restart replays; the time of the next attempt is a Unix time, so it survives too.
[`docs/design.md`](docs/design.md) has the semantics, the scenario fixed before the build, and what each step found.

## Measured

These come from runs on a shared 4-core VM (Intel Xeon 2.10 GHz) that other work was also using, so each is a bound and a few percent is noise; they are measurements of this service, not a benchmark against any other. Where a range is given it is the spread of the runs. Each row names where its method is written down (design section) and the script under `scripts/bench/`. The capacity page that would give the method in one place is not built (see [What it is not (yet)](#what-it-is-not-yet)).

| what | figure | method |
|---|---|---|
| CPU of the service per delivery, plain HTTP to an address | **100 us** (67 to 117) with ten endpoints; 267 us (183 to 300) with one | `scripts/bench/https_cost.py`: the service pinned to one core, receivers on the others, 600 deliveries a run, median of the runs, user plus system CPU from `/proc`; the ingest of the event and the logs are in the figure (section 40.9) |
| the same through a name (one lookup a delivery) | 167 us (150 to 217) with ten endpoints; 283 us (267 to 317) with one | the same |
| `https`, full handshake every time (`tls-resume 0`) | **1,033 us** (983 to 1,317) with ten endpoints; 1,433 us (1,400 to 1,533) with one | the same; a full verified handshake is about 0.9 ms of the service's CPU |
| `https`, session resumed | **683 us** (650 to 817) with ten endpoints; 833 us (767 to 983) with one | the same; a resumed handshake is about 0.5 ms |
| deliveries a second one core does | about 10,000 plain, 970 over `https` with full handshakes, 1,460 resumed (ten endpoints) | worked out from the CPU per delivery above, not a throughput that was run (section 40.9) |
| deliveries a second, end to end | 9,977 with ten endpoints, 6,126 with one (plain HTTP) | `scripts/bench/run.py`: a load generator and a sink in C, 64 connections, four alternated rounds, medians; measured before retention and the reconnecting pool were put under it and not repeated (section 39.8; later runs of the delivery path, sections 37.8 and 40.9, show no change above the noise) |
| ingest | 11 us of CPU an event (10 to 12); 40,484 to 55,460 events a second over 64 keep-alive connections | `scripts/bench/run.py`, 50,000 events, no endpoint; the CPU figure is from section 39.8, and the rate is the least and the most of sections 31 (52,703 and 55,460) and 34.8 (40,484 and 40,793), which were run under different load |
| disk with retention | a peak of **95 MB** for 3,000,000 events (the history: about 1,002 MB); between 15 and 93 MB for 10,000,000 (the history: about 3,180 MB, 34 times more) | `scripts/bench/retention_bench.py through`: one endpoint, every event delivered, retention 20 s and segments of 32 MiB so that a month happens in seconds; 7,775 and 5,274 events a second end to end; the 10,000,000 run was on the build before the merge with the tokens, operating and filtering work, and the 3,000,000 run on the merged build, and neither has https (section 38.1). The bound is the retention, one segment, the events a paused, disabled or dead endpoint or a waiting replay still needs, and `delivery-log-bytes` (section 38.2) |
| memory | 2.5 MB resident after `listening` whatever `idem-keys` is; flat between 2.7 and 9.7 MB through 3,000,000 events and deliveries; 200,000 distinct idempotency keys in 30 MB; with OpenSSL linked and the system's trust store loaded the process is about 5.7 MB larger (9.7 MB against 4.0 MB after 100 deliveries) | `scripts/bench/retention_bench.py`, `tests/retention_test.py` stage `memory`, `scripts/bench/https_cost.py` (sections 38.1, 38.2, 40.9); all but the last are from builds before https |
| start | **0.89 to 0.95 s** with 1,000,000 events retained (257 MB, 8 segments) and no endpoint; 2.12 to 2.22 s with one endpoint that has been sent all of them and 128,460 outcome records to replay. 10,000,000 retained events: about 7.5 to 9.5 s plus the replay, **extrapolated, not measured** | `scripts/bench/retention_bench.py start`: from `exec` to `listening`, three runs, files in the page cache (section 38.1) |
| the loop held by maintenance | 34 to 70 ms for the largest snapshot there can be (62 endpoints with full windows, 4.9 MB; six runs, and 32 ms on the merged build); the longest step of the 3,000,000-event run was 30 ms and of the 10,000,000-event run 68 ms | `maintenance_ms_max` in `GET /stats` (sections 38.1, 38.5) |
| a database that comes back | `/readyz` is `200` again 1.66 s after a cut connection is restored; both connections replaced in 0.07 s after their backends were ended; ready 2.1 s after a real server that was stopped for four seconds started; the longest wait of a probe of `/healthz` in any phase 3 to 23 ms | `tests/pgre_test.py` (section 37.8) |
| a stop is noticed | median 0.24 ms, 90th percentile 0.54 ms, longest 3.2 ms from the signal to the service's first line about it (60 starts, idle service) | section 36 |
| `https` and names do not hold the loop | the longest wait of a request while 64 handshakes were pending 0.6 to 1.3 ms (idle: 0.9 to 3.2 ms); while 64 lookups waited on a name server that takes 2 s, 1.1 to 2.2 ms | `tests/https_test.py`, `tests/names_test.py` (section 40.9) |

## How the tests are built

The service is tested as a black box, with the real binary. A Python harness starts `build/hooks` in a temporary directory, drives it over HTTP, breaks things around it, and checks what happened from the outside; the Lex unit tests cover the pure parts. Nothing about the tests relies on the service's own account of itself.

* **Harnesses.** 44 programs (`tests/*_test.py`, `tests/chaos.py`, `tests/delivery.py`) each start the service and drive it with real clients and real receivers. They read the result from the receivers' side (in `delivery.py`, `sign_test.py`, `rotation_test.py`, `headers_test.py` and `https_test.py` a receiver verifies the signatures with the reference `standardwebhooks` library) and from the files: `tests/chaos.py` reads `events.seg` with a reader of its own, `scripts/logcheck.py` classifies a pair of logs apart from the service, and `tests/dead_test.py` folds the delivery log itself, so a check is made against the log and not against what the service says about it.
* **Crashes, at chosen points.** The service is stopped with `kill -9`, at random instants (`chaos.py`, `delivery.py`), when the receivers have seen a stated number of deliveries since the last start (so the kills follow progress, not the clock: `filter_test.py`, `dead_test.py`, `limits_test.py`, `scan_test.py`), or at a numbered step of its own compaction (`retention_test.py`: the service is told to stop at a numbered step, and the test kills it there).
* **Power cuts.** A plain `kill -9` cannot show a missing flush, because the kernel keeps every byte the process wrote. `tests/fsync_shim.c`, an `LD_PRELOAD` shim, records how long each log file was whenever `fsync` returned; before the restart the harness cuts each file to that length plus a random part of the rest, and may zero its last block: the data directory as a power cut could leave it.
* **The database.** `tests/pgproxy.py` sits between the service and PostgreSQL and can cut the connections, refuse new ones, freeze both directions without losing a byte (a cable pulled and put back), read and discard, black-hole new connections (they hang, as behind a firewall that drops packets) and end the backends of its own connections with `pg_terminate_backend`.
* **https and names.** `tests/tlskit.py` makes throwaway certificate authorities and certificates with the `openssl` command (expired, not yet valid, another name, another authority, self-signed, a purpose that does not fit), a TLS receiver that fails in a chosen way and reports what it saw (name, resumption, protocol), and a name server over TCP that answers what the test says, counts the questions and can be slow or change its answer.
* **Partial reads and writes.** `tests/io_shim.c`, another `LD_PRELOAD` shim, makes `send` take only so many bytes, `recv` return only so many, and every third call fail with `EAGAIN`, on the ports it is given: the branches a loopback socket never reaches. (The partial-write branch of a plain `http` attempt is still not reached by it; the shim is used on the TLS receiver and the name server.) `tests/statx_shim.c` makes the production profile's mode check fail in ways no file mode can.
* **Receivers that misbehave.** One that accepts and never answers, answers after 300 ms, closes without a word, resets in the middle of the response, answers garbage or a status line in two pieces, answers `500`, `404`, `301` or `410`, reads a 60,000-byte request a few KiB at a time, has a full accept queue, or sits behind a blackholed address (`attempt_test.py`, `reason_test.py`, `isolation_test.py`).
* **Mutation testing.** For each piece of new code, mutants (one edit each, on a copy of the tree) must be killed by a test; the design document lists, for each piece, how many were made, how many were killed, and every survivor with the argument for why it survives. `scripts/mutate.py` runs a list of mutants and checks that the file is restored byte for byte; the list for the `https` work is committed (`tests/mutants/https.py`: 49 mutants, 47 killed, one the compiler refuses and one that survives because it is redundant by design). The other lists were scratch work and are described in the design document, not kept. Mutation testing is run by hand, not in CI.
* **Unit tests in lex-sys.** `lex-sys test` runs `tests/*_test.ls`: **215 tests in 21 sets** for the pure parts (the cron calendar, the delivery state, the idempotency index, the settings, the destination rules, the DNS answers, the token bucket, the table of dead letters, the counters and the text of `/metrics`, and so on). lex-sys has no `examples {}` blocks, so a test is a function of assertions; the compiler's own test runner reports each by name.
* **CI** runs all of it on every push and pull request: the formatting check, the authority pin, the unit tests, the 44 harness programs and `shellcheck`. It does not run the mutation lists or the scripts under `scripts/bench/`, which are reports.

## Tests

```sh
$LEX_SYS test                                      # the lex-sys unit tests: 215 in the 21 sets of lex-sys.toml (state, endpoints, destination, idem, cron, authz, filter, hdrs, epx, retain, config, dns, resolve, manage, reason, dbup, jitter, bulk, dead, lim, ops)
python3 tests/cron_test.py build/cron_probe 500      # cron expressions against an independent implementation (calendar, steps, day-of-month and day-of-week, leap days)
python3 tests/sign_test.py build/sign_probe        # signatures and base64 against the reference library (548 checks)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/history_test.py build/hooks   # the history in PostgreSQL (needs one: see the file)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/pgre_test.py build/hooks      # the database goes away and comes back: cut, frozen, black-holed, backends ended; no restart, nothing lost or repeated, the loop never stalls
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/roster_test.py build/hooks    # the endpoints in PostgreSQL: import, read at start, every refusal
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/manage_test.py build/hooks    # POST /endpoints and GET /endpoints/:id: the token, the request, from now
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/slots_test.py build/hooks     # endpoint ids and slots: the legacy log, ids above 15, dormant, reclaimed
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/patch_test.py build/hooks     # PATCH /endpoints/:id: address on the next attempt, secret rotation, a database that refuses, compaction
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/delete_test.py build/hooks    # DELETE /endpoints/:id: nothing new after it, an attempt on the wire finishes, replays dropped, the slot reused clean across a restart, 62 endpoints churned
python3 tests/saturation_test.py build/hooks                                       # ten endpoints beside 64 connections: no start beyond them is a failed attempt
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/ssrf_test.py build/hooks      # where a delivery may go: 48 refused hosts, 23 public ones and 7 names accepted, a redirect not followed
python3 tests/names_test.py build/hooks            # names: the SSRF rule at every attempt (21 unsafe addresses, mixed answers, localhost, rebinding), DNS failures, a slow resolver
python3 tests/https_test.py build/hooks            # TLS: every certificate failure, the handshake, a stalled one, kill -9 in the middle of one, 64 held handshakes (needs `openssl`)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/https_api_test.py build/hooks    # the scheme: POST, PATCH, GET, the file, the table, a backup and a restore
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/sessions_test.py build/hooks     # TLS sessions: resumption seen from the receiver, dropped on change, no leaks
python3 scripts/bench/https_cost.py                 # what a delivery costs in CPU: an address, a name, https, https resumed
scripts/check-authority.sh                          # the authority report is docs/authority.json
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
scripts/check-authority.sh                         # the authority report is docs/authority.json: a new foreign call or capability is a diff to approve
python3 tests/stop_test.py build/hooks             # SIGTERM and SIGINT drain, the deadline, a second signal, under load nothing is lost or repeated
python3 tests/corrupt_test.py build/hooks          # a delivery log ahead of its events log, damage in the middle, a torn tail, --repair-logs, and the same rule as scripts/logcheck.py on 153 corruptions (150 random, 3 directed)
python3 tests/metrics_test.py build/hooks          # /metrics against a known workload: ingest, group commits, attempts by outcome and reason, lag, retries (with HOOKS_PG: the history)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/ready_test.py build/hooks     # /readyz: a database that goes away, a disk that fills (a tmpfs it mounts)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/reason_test.py build/hooks    # why an attempt failed: a receiver for each reason, in /attempts, /metrics, the table and the log
python3 tests/retention_test.py build/hooks      # retention: segments dropped, pins, the snapshot, formats, keys, kill -9 at all 19 steps of a compaction, the stall, a flat memory (docs/retention.md)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/dead_test.py build/hooks      # dead letters: the list and its pages against the log, kill -9, bulk replay under the bound of 32, the table of 2,048, a log from before the time of death
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/cancel_test.py build/hooks    # cancelling a waiting replay: one or all, kill -9, a power cut, an attempt on the wire, the bound of 32
python3 tests/jitter_test.py build/hooks           # retry jitter: bounds, mean and spread, 0 is the schedule, the same delay from the same endpoint and event, the recorded time kept across kill -9
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/limits_test.py build/hooks    # concurrency and rate per endpoint, observed at a receiver; held-back events are not failures; the table, endpoints.conf, an old schema
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/backup_test.py build/hooks    # backup and restore: total loss, kill -9 under online backups, every refusal (docs/runbook.md section 4)
FULL=1 HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/schedules_test.py build/hooks   # schedules: the token, every refusal, a fire is an ordinary event, missed fires, kill -9 between the event and the database, a real minute (needs a database of its own: it empties `schedules`)
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` (what a bad receiver costs ingest) and `scripts/bench/stall_probe.py` (two endpoints, one dead, 3,000 events: the cursors) are reports, not gates, and so is `scripts/bench/retention_bench.py` (`through`: millions of events through one endpoint with the disk and memory sampled; `start`: the start with a million events retained).

## Documentation

- [`docs/runbook.md`](docs/runbook.md): running it: start and stop, the settings, what every log line and `/stats` field means, backup and restore (and whether the online variant is safe), upgrading, what to do when it goes wrong, the known limits. Parts that depend on work that is not built are marked planned.
- [`docs/retention.md`](docs/retention.md): how the logs are bounded: the segments, the snapshot, the rules for what may be dropped, and what is not recoverable afterwards.
- [`docs/production.md`](docs/production.md): what "production" means here, the plan to get there, and the status of each item.
- [`docs/design.md`](docs/design.md): what this is for, which store owns which fact, the delivery semantics, the test scenario
  fixed before the build, the gaps predicted, and sections 13 to 25 on what building each step showed, section 32 on cron, section 33 on the tokens and the production profile, section 34 on operating it (readiness, metrics, the reason an attempt failed, stopping, refusing corruption), section 35 on event types, rotation and headers, section 36 on signals, section 37 on the database that comes back, section 38 on retention, section 39 on dead letters, cancelling a replay, retry jitter and the limits on an endpoint's pace, and section 40 on `https` endpoints, names and the destination rule at every attempt. (Its first sections are the plan; where a later section says otherwise, the later one is what was built.)

## Layout

```
src/hooks.ls       the service: routes, the loop, delivery
src/state.ls       the per-slot cursor and window, and the outcome record
src/idem.ls        the idempotency-key index (rebuilt from the log at start)
src/endpoints.ls   the endpoints (file or table) as the service holds them: id, slot, port, host, key
src/config.ls      the settings, from a file and from flags
src/history.ls     the attempts that ended, written to PostgreSQL, and the connections to it
src/dbup.ls        whether the start is going to succeed with the database as it is: refused for good, or only away and waited for up to `pg-start-wait-ms` (pure)
src/evlog.ls       the events log as a chain of segments (`events.seg`, `events-1.seg`, ...) and its manifest `events.first`
src/store.ls       the small file operations retention is made of: paths, writing and replacing files
src/compact.ls     retention in the loop: which events may go, the snapshot that replaces the outcomes log, the steps the loop takes
src/retain.ls      the rules of retention (pure): when a segment goes, when the active one is sealed, when the outcomes log is replaced
src/roster.ls      the endpoints table: read at start, and `--import-endpoints`
src/manage.ls      `POST`, `PATCH` and `DELETE /endpoints`: who may call them, what a request may say, a secret for the endpoint
src/authz.ls       who may call which route: the scope of every route, and the verdict for a request's token
src/perm.ls        the modes of the data directory and its files (`production = 1`): the `statx` call into libc
src/filter.ls      which events an endpoint is sent: the patterns, the match, the type in a record
src/hdrs.ls        an endpoint's custom headers: the rules, the form they are kept in, the wire form
src/epx.ls         what an endpoint has besides an address and a secret (types, headers, the previous secret), and the request that sets it
src/jitter.ls      the spread given to a retry delay: a function of endpoint, event and attempt
src/lim.ls         the limits on the pace of an endpoint: concurrency and rate (the token bucket), the settings and the endpoint's own
src/dead.ls        the table of each endpoint's dead letters: sorted by event id, bounded, completed from the log
src/bulk.ls        what the requests about dead letters ask for: the page of the list, the body of the bulk replay
src/wire.ls        the request of one attempt: the headers, one signature or two, the Host
src/attempt.ls     one delivery attempt as a state machine on the poller: resolving a name, connecting, the TLS handshake, sending, reading
src/tls.ls         the TLS client: OpenSSL driven in steps over memory BIOs (the foreign calls of libssl and libcrypto), the trust store, sessions
src/dns.ls         the DNS client's bytes: the query for an A record, the answer (pure, total on any bytes)
src/resolve.ls     which name server to ask: `dns-server`, or /etc/resolv.conf
src/destination.ls where a delivery may go: addresses, names, the ranges, the scheme in the host
src/cron.ls        cron expressions: parse, and the next and last fire (pure, no clock)
src/sched.ls       `/schedules`: the requests, what a due row means, and the state the tick keeps
src/ops.ls         what an operator needs: the counters, readiness, how the service learns it is to stop (a claim on `SIGINT` and `SIGTERM`, woken through the poller)
src/metrics.ls     `GET /metrics`: the Prometheus text, from two arrays of numbers
src/reason.ls      why an attempt failed: the reasons, their numbers (on disk) and names, and the coarse status the history keeps
src/logguard.ls    the start's look at the logs before it changes them: torn tail or damage, the pair of logs, `--repair-logs`
src/queries.ls     the SQL of `sql/queries.sql` as functions (generated by `pgen`)
src/view.ls        what the database says, as an HTTP answer
src/sign.ls        HMAC-SHA256, base64 and the Standard Webhooks signature
lex-sys.toml       the project file: the compiler, the two libraries (each pinned to a commit) and the programs
scripts/build.sh   `lex-sys build` (linked against OpenSSL: `scripts/cc-ssl.sh`), and the fsync shim the crash tests preload
scripts/check-authority.sh   regenerates the authority report and compares it with `docs/authority.json` (`--update` writes it)
scripts/backup.sh, restore.sh, logcheck.py   backup and restore of the two logs (and the tables), and the checker that refuses an inconsistent pair
scripts/release.sh a tarball, SHA256SUMS and an SBOM stub
Dockerfile, deploy/   the container image; the systemd unit, a settings sample (with the production profile) and the container's health check and entry point
tests/             unit tests (lex-sys, `tests/*_test.ls`) and harnesses (Python, `tests/*_test.py`, `chaos.py`, `delivery.py`), the shims they preload (`*.c`) and the mutants of the https work (`mutants/`)
scripts/bench/     the scripts behind the figures under "Measured" (`run.py`, `https_cost.py`, `retention_bench.py`, `stall_probe.py`)
docs/design.md     the design and what building it found
```

## Limitations

One process, one thread, one core: the loop does everything, and up to 64 delivery attempts are in flight at once (8 per endpoint at most; fewer with a concurrency limit). Endpoints come from a file read at start, or from the `endpoints` table (read at start, and added to with `POST /endpoints`); an endpoint the log has not seen (a row added by hand, a new line in the file) starts at the slowest cursor of the others, 0 if there are none, and one created with `POST /endpoints` starts from now; they can be changed with `PATCH /endpoints/:id` (host, port, secret, event types, headers, concurrency, rate) and removed with `DELETE /endpoints/:id` (an attempt of it that is on the wire finishes first, and until it has the endpoint's slot cannot be given to a new one: with 62 endpoints that is a `409` for up to the attempt's deadline); `https` endpoints and host names are supported (section 40): names are resolved by the service over TCP, with no `/etc/hosts`, no IPv6, no UDP and one name server; the connection is closed after every delivery, so `https` costs a handshake each (resumed when the receiver allows); revocation is not checked. By default only public IPv4 addresses are allowed as destinations, and a name must resolve to public ones (no ports limited, no per-host allow-list: `docs/design.md` sections 26 and 40). At most 62 endpoints. An endpoint is served at most 1,024 events past its own cursor (its window): an endpoint that is far behind waits there, the events beyond it wait in the log, and none of that holds up another endpoint (design.md section 31); left alone a dead endpoint dead-letters its events at the speed of its retry schedule, or is paused by the circuit breaker after `breaker-days` days of failures and waits for a person to enable it (no automatic resume). An event of 65,500 bytes or more is refused (`413`; 65,499 is the largest, less 11 and the length of its stored type, and less with an `Idempotency-Key`). **The logs are bounded by retention, not by history** (docs/retention.md, design.md section 38): the events log is a chain of segments and a segment is deleted once every event in it is final everywhere and older than `retention-days` (and than `window-ms`); `delivery.seg` is replaced by a snapshot of the state it replays to when it has grown to `delivery-log-bytes`, which holds the loop for 40 to 70 ms at the most (the largest possible state: 62 endpoints with full windows, 4.9 MB; a typical one, a few milliseconds); every file begins with a format version and an unknown version is refused. An event that is dropped is gone (`404`; its idempotency key is forgotten, so the same key posted again is a new event). The endpoints table is read once: a row changed in it behind the service's back is seen at the next start, and a change whose outcome the service does not know (a `504` "may have been stored") is not in the running service until then. A host *name* for `pg-host` is resolved inside every attempt to connect, by a call that waits: use an address. The stop waits for no more than `stop-deadline-ms` and leaves the listening socket open until it exits (new connections are answered `503` and closed). At most `idem-keys` idempotency keys (262,144 by default) that are fresh; the index is rebuilt at start from the retained events only, **and every fire of a schedule uses one**; cron's keys are forgotten with their events, so a schedule of every minute no longer fills the index. Schedules: UTC only, at most 64, one service per database, and a jump of the machine's clock is a stop or a restart as far as they can tell. A dead letter list holds the newest 2,048 of an endpoint (the rest are completed from the log as room appears); a rate limit is memory and can be exceeded by a tenth of a second's worth across a restart. Not for production: [What it is not (yet)](#what-it-is-not-yet) says what is left before that line can be lifted.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src`, the unit tests, the harnesses above and `scripts/check-authority.sh` (the authority report must be the committed `docs/authority.json`; a change to it is a change a person approves by committing the file). Design before code,
in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
