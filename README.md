# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys) and built as one binary. You `POST` it an event; it stores the event durably and delivers it, signed with [Standard Webhooks](https://www.standardwebhooks.com) signatures, to every subscribed endpoint, **at least once**, over HTTP or `https`. Failures are retried on a schedule, what cannot be delivered is kept as a dead letter you can list and replay, and every outcome survives a crash. PostgreSQL is optional: it holds the endpoints and a history of attempts.

**Status: not for production yet.** What is left: a 24-hour soak test under chaos (the harness is built), the capacity figures it gives, and lex-sys's own TLS in place of OpenSSL (built as a second binary, `hooks-pure`, and not the default: [docs/pure-tls.md](docs/pure-tls.md)). See [docs/status.md](docs/status.md).

## What you get

* **Durable ingest.** `202` is sent only after the flush that covers the event; requests that arrive together share one flush.
* **Crash-safe delivery.** Every outcome, with the time of the next attempt, is logged; `kill -9` and power cuts lose no acknowledged event.
* **Retries and dead letters.** The Standard Webhooks schedule with jitter, then a dead letter; list them, replay them in bulk, cancel a waiting replay.
* **Isolation.** A slow, silent or dead endpoint does not stall the others; a circuit breaker pauses one that has failed for days; each endpoint can have its own concurrency and rate.
* **Per endpoint.** Event-type filters (`invoice.*`), secret rotation with two signatures, custom headers.
* **Cron.** Schedules that append ordinary events, exactly once, even across a crash.
* **`https` and names.** TLS with the certificate chain and host name verified; names resolved by the service, with the destination checked at every attempt.
* **Operable.** `/healthz`, `/readyz`, Prometheus `/metrics`, a graceful stop, a refusal to start on a corrupt log, backup and restore.
* **Bounded.** Retention drops old, finished events, a maximum age drops whatever is older, the history is pruned; scoped bearer tokens, and a `production` profile that refuses an unsafe configuration.
* **Privacy and audit.** Erase one event (`DELETE /events/:id`), encrypt bodies at rest, an audit log of who read and changed what ([docs/privacy.md](docs/privacy.md)).

## Quick start

You need `git`, Rust, `gcc`, OpenSSL 3 with its development files (`libssl-dev`), Python 3, and `curl`.

```sh
git clone https://github.com/alpibrusl/lex-sys                           # the compiler
git clone https://github.com/alpibrusl/lexsys-hooks && cd lexsys-hooks
REV=$(sed -n 's/^lex-sys *= *"\(.*\)"/\1/p' lex-sys.toml)                  # the compiler these sources need
(cd ../lex-sys && git fetch -q origin && git checkout "$REV" && cargo build --release -p lex-sys)
export LEX_SYS=$PWD/../lex-sys/target/release/lex-sys
scripts/build.sh                                                           # builds build/hooks
```

One endpoint (`<id> <host> <port> <secret>`), a receiver that verifies every signature with the reference library (`pip install standardwebhooks`) and prints what it is sent, and the service:

```sh
mkdir -p /tmp/hooks-data
echo "0 127.0.0.1 9100 whsec_$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(24)).decode())')" > /tmp/hooks-data/endpoints.conf

cat > verify.py <<'EOF'
import http.server, sys
from standardwebhooks import Webhook

wh = Webhook(sys.argv[1])                         # the endpoint's secret, whsec_...

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        wh.verify(body, dict(self.headers))       # raises if the signature or the timestamp is wrong
        extra = {k: v for k, v in self.headers.items() if k.lower().startswith("x-")}
        print("verified", self.headers["webhook-id"], extra, body.decode(), flush=True)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
EOF
python3 verify.py "$(cut -d' ' -f4 /tmp/hooks-data/endpoints.conf)" 9100 &
build/hooks --port 8080 --dir /tmp/hooks-data --allow-private-hosts 1 &     # the receiver is on 127.0.0.1
sleep 1                                                                    # until it listens
curl -s -XPOST -d '{"type":"user.created","id":7}' localhost:8080/events
```
```
{"id":1}
verified evt_1 {} {"type":"user.created","id":7}
```

`curl localhost:8080/events/1` returns `{"id":1,"event":{"type":"user.created","id":7}}`, and `/stats` counts the delivery. With nothing listening on 9100 the event is stored and retried (5 s, 5 min, ...). A subscriber gets three headers: `webhook-id`, `webhook-timestamp` and `webhook-signature` (`v1,` and a base64 HMAC-SHA256).

## Examples

These continue from the quick start (`verify.py` is its receiver); each output below is what the commands print, and ids and timestamps differ on every run.

**An endpoint with a filter and a custom header**, created while the service runs. This needs PostgreSQL and an admin token (`docs/endpoints.md`). Stop the quick-start service first:

```sh
createdb hooks && psql hooks -q -f sql/schema.sql
ADMIN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
cat > hooks.conf <<EOF
port = 8080
dir = /tmp/hooks-db
pg-host = 127.0.0.1
pg-user = postgres
pg-database = hooks
admin-token = $ADMIN
allow-private-hosts = 1
EOF
mkdir -p /tmp/hooks-db && build/hooks --config hooks.conf &
```
```
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints \
    -d '{"host":"127.0.0.1","port":9100,"types":["invoice.*"],"headers":{"X-Api-Key":"k123"}}'
{"id":0,"host":"127.0.0.1","scheme":"http","port":9100,"secret":"whsec_...","from":"now","cursor":0}
$ python3 verify.py whsec_... 9100 &       # the secret is in this answer and in no other
$ curl -s -XPOST -d '{"type":"user.created","id":1}' localhost:8080/events      # not wanted: never sent
{"id":1}
$ curl -s -XPOST -d '{"type":"invoice.paid","id":42}' localhost:8080/events
{"id":2}
verified evt_2 {'X-Api-Key': 'k123'} {"type":"invoice.paid","id":42}
```

**Dead letters and bulk replay** (`docs/delivery.md`). Restart the service with `--schedule 100,200` so that events die within a second, add an endpoint on port 9101 with nothing listening, send three events, list the dead letters, then start a receiver and replay them:

```
$ build/hooks --config hooks.conf --schedule 100,200 &
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints -d '{"host":"127.0.0.1","port":9101}'
$ for i in 1 2 3; do curl -s -XPOST -d "{\"type\":\"order.shipped\",\"id\":$i}" localhost:8080/events; done
$ curl -s 'localhost:8080/endpoints/1/dead?limit=2'
{"endpoint":1,"order":"desc","held":3,"truncated":false,"complete_above":0,"dead":[{"event":5,"type":"order.shipped","attempts":3,"reason":"connect_refused","died_at":1791180808831,"replaying":false},{"event":4,"type":"order.shipped","attempts":3,"reason":"connect_refused","died_at":1791180808729,"replaying":false}],"next":4}
$ python3 verify.py whsec_... 9101 &
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints/1/replay-dead
{"endpoint":1,"taken":3,"remaining":0,"waiting":3,"next":5}
verified evt_3 {} {"type":"order.shipped","id":1}
verified evt_4 {} {"type":"order.shipped","id":2}
verified evt_5 {} {"type":"order.shipped","id":3}
```

**A cron schedule** (UTC; `docs/cron.md`). Each fire is an ordinary event, signed, retried and replayable, made once even if the service is killed in the middle of it:

```
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/schedules \
    -d '{"expr":"30 4 * * 1","type":"report.due","body":{"report":"weekly"}}'
{"id":1,"expr":"30 4 * * 1","type":"report.due","body":{"report":"weekly"},"enabled":true,"created_at":1791180812,"last_fired":null,"next_fire":1791779400,"next_fire_at":"2026-10-12T04:30:00Z"}
```

**Scrape the metrics** (`docs/operating.md`; the read token, when one is set, goes in `Authorization: Bearer`):

```
$ curl -s localhost:8080/metrics | grep -E '^hooks_(attempts_total|endpoint_lag_events|ready )'
hooks_ready 1
hooks_attempts_total{outcome="delivered"} 3
hooks_attempts_total{outcome="failed"} 6
hooks_attempts_total{outcome="dead"} 3
hooks_endpoint_lag_events{endpoint="0"} 0
hooks_endpoint_lag_events{endpoint="1"} 0
```

## HTTP API at a glance

The scope says which bearer token a route needs when that token is configured (`ingest`, `read`, `admin`; `open` needs none). The full table, with every answer and error, is [docs/api.md](docs/api.md).

| route | scope | what it does |
|---|---|---|
| `POST /events` | ingest | store an event (a JSON object with a string `"type"`; optional `Idempotency-Key`); `202 {"id":N}` after the flush |
| `GET /events/:id` | read | the stored event (`410` once it was dropped or erased) |
| `DELETE /events/:id` | admin | erase one event: its body is replaced in the log, and it is never sent or served again |
| `GET /events/:id/attempts` | read | the attempts of an event and why each failed (needs a database) |
| `POST /events/:id/replay[/:endpoint]` | admin | send an event again to every subscribed endpoint, or to one |
| `GET /endpoints`, `GET /endpoints/:id` | read | the endpoints (never the host, a secret or a header's value) |
| `POST`, `PATCH`, `DELETE /endpoints[/:id]` | admin | create, change, delete an endpoint without a restart (needs a database) |
| `GET /endpoints/:id/dead`, `POST /endpoints/:id/replay-dead` | read, admin | list and bulk-replay dead letters |
| `POST /endpoints/:id/enable` | admin | enable an endpoint a `410` or the circuit breaker disabled |
| `POST`, `GET`, `PATCH`, `DELETE /schedules[/:id]` | admin | cron schedules (needs a database) |
| `GET /stats`, `GET /config`, `GET /metrics` | read | counters, the settings in force, Prometheus metrics |
| `GET /healthz`, `GET /readyz` | open | liveness, readiness |

## Configuration at a glance

Settings come from a file (`--config hooks.conf`, `key = value` a line), from flags (`--port 8080`), or both; the last source wins; unknown ones are refused before the service listens. The settings people touch first (all of them, with exit statuses, are in [docs/configuration.md](docs/configuration.md)):

| setting | default | what it is |
|---|---|---|
| `port`, `dir` | required | the TCP port, and the data directory (the logs, and `endpoints.conf`) |
| `admin-token`, `ingest-token`, `read-token` | none | bearer tokens of the three scopes; a scope without one is open |
| `production` | `0` | `1`: refuse to start unless tokens are set, private hosts are off, the audit log is on and the data directory is closed to others |
| `pg-host` (`pg-user`, `pg-database`, `pg-password`) | none | a PostgreSQL for the endpoints and the attempt history |
| `schedule` | nine delays, 5 s to 24 h | retry delays in ms; after the last, a dead letter |
| `retry-jitter` | `10` | percent each retry delay is moved, up or down |
| `retention-days` | `30` | drop finished events older than this (`0` keeps them) |
| `max-age-days` | `0` | drop any event older than this, finished or not (`0`: none) |
| `history-days` | `30` | delete history rows older than this (`0` keeps them) |
| `encryption-key-file` | none | encrypt event bodies at rest with this key (32 bytes or 64 hex digits; keep it apart from backups) |
| `audit-log` | `1` | write `<dir>/audit.log`: who read and changed what |
| `allow-private-hosts` | `0` | `1`: endpoints may be on private, loopback or link-local addresses |

## Documentation

* [docs/api.md](docs/api.md): every route, scope and error. [docs/configuration.md](docs/configuration.md): every setting and exit status.
* [docs/endpoints.md](docs/endpoints.md): the database, `endpoints.conf`, event types, secret rotation, custom headers. [docs/cron.md](docs/cron.md): schedules.
* [docs/delivery.md](docs/delivery.md): how delivery works, retries, idempotency, dead letters, pace. [docs/https.md](docs/https.md): `https` and host names.
* [docs/security.md](docs/security.md): tokens, the production profile, authority, the audit log, encryption at rest. [docs/privacy.md](docs/privacy.md): what is held, for how long, erasure, and GDPR and SOC 2. [docs/operating.md](docs/operating.md): metrics, stopping, building, the prebuilt binary.
* [docs/runbook.md](docs/runbook.md): running it, log lines, backup and restore, what to do when it goes wrong.
* [docs/status.md](docs/status.md): what is built, what is not, measured costs, limits. [docs/production.md](docs/production.md): what "production" means here and the plan.
* [docs/testing.md](docs/testing.md): how the tests are built and run. [docs/layout.md](docs/layout.md): the source map.
* [docs/design.md](docs/design.md): the design and what building each step found. [docs/retention.md](docs/retention.md): how the logs are bounded. [docs/authority.json](docs/authority.json): the pinned authority report.
* [docs/index.html](docs/index.html): the project page. [docs/inbound-gateway.md](docs/inbound-gateway.md) and [docs/lexsys-log-retention.md](docs/lexsys-log-retention.md): proposals.

## Status and limits

* **Not for production.** No 24-hour soak test has been run: the harness and its criteria are built ([docs/soak.md](docs/soak.md)), and so is the method of capacity ([docs/capacity.md](docs/capacity.md)), but the long run that fills both is not made; the figures in [docs/status.md](docs/status.md) come from short runs on shared machines.
* **One process, one thread, one core**, at most 1,024 endpoints (a constant of the build; [status.md](docs/status.md)), 64 attempts in flight.
* **`https` uses OpenSSL in the process** by default; with lex-sys's own TLS it is a second build, `hooks-pure`, with no foreign function for TLS, no resumption and about 4 to 7 times the CPU a handshake ([docs/pure-tls.md](docs/pure-tls.md)); no revocation checks, client certificates or IPv6, and each delivery costs a handshake.
* **No TLS on the service's own port** (put a reverse proxy in front); signing secrets are stored in the clear in the database, which is the trust boundary.
* **The endpoints table is read once**: a change made behind the service's back is seen at the next start.
* **An event dropped by retention is gone** (`410`); delivery is at least once, and not strictly ordered.

## Contributing

Every change goes through what CI runs: `fmt --check src`, the unit tests, the harnesses and `scripts/check-authority.sh`. Design before code, in `docs/design.md`, with claims measured; a claim that turns out false is corrected in place. See [docs/testing.md](docs/testing.md) and [docs/layout.md](docs/layout.md).

## Licence

[EUPL-1.2](LICENSE).
