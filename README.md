# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys): you `POST` it an event, it stores the
event durably, and it delivers the event, **signed**, to every subscribed endpoint, **at least once**, retrying on a schedule
and keeping what it could not deliver as a dead letter.

It is also the realistic program that uses the whole stack ([`lexsys-log`](https://github.com/alpibrusl/lexsys-log) today;
`lexsys-web`, `lexsys-schema`, `lexsys-pg` and `lexsys-cache` as it grows) so that what is missing shows up as a failing test
rather than a guess. No `Ffi`, no `unsafe`; the authority report names what the program can do.

## Status

**Step H1e.** Working: durable ingest (`202` only after the flush that covers the event; requests that arrive together share one
flush), delivery to several endpoints with [Standard Webhooks](https://www.standardwebhooks.com) signatures checked against the
reference library, retries on the Standard Webhooks schedule, dead letters, and every outcome (with the time of the next
attempt) surviving a crash. A slow, silent or unreachable endpoint costs the others almost nothing: delivery attempts do not hold the loop (up to 64 in flight, a state machine each), so ingest stays at a median of 2.3 ms and healthy endpoints see their deliveries within milliseconds ([`docs/design.md`](docs/design.md) section 16). Since H1e a client may send an `Idempotency-Key`: a repeat of the same event answers the first answer, byte for byte, and stores nothing, also across a crash ([`docs/design.md`](docs/design.md) section 17). Not built: endpoints and
attempt history in Postgres, jitter, TLS (`https`) endpoints.

## Requirements

- The **lex-sys** compiler at the commit `lex-sys.toml` names (`[package] lex-sys`); `lex-sys build` refuses any other. It needs `clock_unix_ms` (the signing timestamp; lex-sys PR #190),
  `tcp_connect_start` (attempts that do not wait; #191), a lock with an origin (#192) and the project file (#193).
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

build/hooks --port 8080 --dir /tmp/hooks-data &
curl -XPOST -d '{"type":"user.created","id":7}' localhost:8080/events      # {"id":1}, after the flush
```

With nothing listening on port 9000 the event is stored and retried (5 s, 5 min, ...). To watch a delivery arrive, run the
receiver below first.

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
ignored). It is its own file because it holds the secrets: give it the permissions secrets need, and keep it out of the settings.

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
| `GET /stats` | `{"endpoints","attempts","delivered","failed","dead","keys","replays"}` |
| `POST /events/:id/replay` | send the event again to every endpoint; `/replay/:endpoint` for one. `202 {"event","endpoints"}`, `404` for an unknown event or endpoint, `507` if 32 replays already wait. Same `webhook-id`, same schedule (design.md section 23) |
| `GET /endpoints` | each endpoint's `{"id","port","cursor","disabled"}` (not the host, not the secret) |
| `POST /endpoints/:id/enable` | enable an endpoint a `410` disabled; `200` whether or not it was, `404` for an unknown id |
| `GET /config` | the settings in force: `{"schedule":[ms,...],"deadline-ms","window-ms"}` (not the endpoints, not their secrets) |
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
$LEX_SYS test                                      # the four unit-test sets of lex-sys.toml (state, endpoints, idem, config)
python3 tests/sign_test.py build/sign_probe        # signatures and base64 against the reference library (536 checks)
python3 tests/replay_test.py build/hooks           # replay: one endpoint or all, restarts, capacity, an event far behind the cursor
python3 tests/gone_test.py build/hooks             # 410 Gone disables an endpoint, restarts keep it, enable undoes it
python3 tests/config_test.py build/hooks           # settings: a file, flags, which wins, and every refusal
python3 tests/attempt_test.py build/hooks          # one delivery attempt against eight kinds of receiver
python3 tests/retry_test.py build/hooks            # the retry delays, also across restarts, and the dead letter
python3 tests/isolation_test.py build/hooks        # what a silent, slow or unreachable endpoint costs the others (gated)
python3 tests/chaos.py build/hooks 2000 8 50       # kill -9 as a power cut: no acknowledged event may be lost
python3 tests/delivery.py build/hooks 300 4 150    # three endpoints, signed, retried and dead-lettered, with the service killed
FULL=1 python3 tests/idempotency_test.py build/hooks   # idempotency keys: the contract, restarts, chaos, a broken log, a full index
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` is a report, not a gate.

## Documentation

- [`docs/design.md`](docs/design.md): what this is for, which store owns which fact, the delivery semantics, the test scenario
  fixed before the build, the gaps predicted, and sections 13 to 17 on what building each step showed.

## Layout

```
src/hooks.ls       the service: routes, the loop, delivery
src/attempt.ls     delivery attempts that do not hold the loop: connect, send, read a status line, each waiting for the poller
src/state.ls       the per-endpoint cursor and window, and the outcome record
src/idem.ls        the idempotency-key index (rebuilt from the log at start)
src/endpoints.ls   the endpoints file
src/sign.ls        HMAC-SHA256, base64 and the Standard Webhooks signature
lex-sys.toml       the project file: the compiler, the two libraries (each pinned to a commit) and the programs
scripts/build.sh   `lex-sys build`, and the fsync shim the crash tests preload
tests/             unit tests (lex-sys) and harnesses (Python)
docs/design.md     the design and what building it found
```

## Limitations

One process, one thread, one core: the loop does everything, and up to 64 delivery attempts are in flight at once (8 per endpoint). Endpoints come from a file read at start, not a database; a host *name* is resolved by a blocking call that stalls the loop for as long as the resolver takes (use IP addresses); no `https`. At most 16 endpoints, and an endpoint more than 1,024 events behind is not served until it catches up. Events over 65,500 bytes are refused (`413`). `delivery.seg` is never compacted. At most 65,536 idempotency keys; the index is rebuilt by reading the whole events log at start. Not for production.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src`, the unit tests and the harnesses above. Design before code,
in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
