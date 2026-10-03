# lexsys-hooks

[![ci](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-hooks/actions/workflows/ci.yml)

A webhook delivery service, written in [lex-sys](https://github.com/alpibrusl/lex-sys): you `POST` it an event, it stores the
event durably, and it delivers the event, **signed**, to every subscribed endpoint, **at least once**, retrying on a schedule
and keeping what it could not deliver as a dead letter.

It is also the realistic program that uses the whole stack ([`lexsys-log`](https://github.com/alpibrusl/lexsys-log) today;
`lexsys-web`, `lexsys-schema`, `lexsys-pg` and `lexsys-cache` as it grows) so that what is missing shows up as a failing test
rather than a guess. No `Ffi`, no `unsafe`; the authority report names what the program can do.

## Status

**Step H1c.** Working: durable ingest (`202` only after the flush that covers the event; requests that arrive together share one
flush), delivery to several endpoints with [Standard Webhooks](https://www.standardwebhooks.com) signatures checked against the
reference library, retries on the Standard Webhooks schedule, dead letters, and every outcome (with the time of the next
attempt) surviving a crash. **Not met:** a slow or silent endpoint still costs the others, because delivery shares the loop that
serves ingest ([`docs/design.md`](docs/design.md) section 15 has the numbers). Not built: idempotency keys, endpoints and
attempt history in Postgres, `410 Gone` handling, jitter, replay, TLS (`https`) endpoints.

## Requirements

- The **lex-sys** compiler and a checkout of **lexsys-log**, both at the revisions this repository's CI builds with (below).
  The compiler needs `clock_unix_ms`, which the signing timestamp uses (lex-sys PR #190, merged).
- Rust, to build the compiler; `gcc`, to build the small `fsync` shim the crash tests use.
- To run the tests: `python3` and `pip install standardwebhooks` (the independent implementation signatures are checked against).

## Quick start

```sh
git clone https://github.com/alpibrusl/lex-sys
git clone https://github.com/alpibrusl/lexsys-log
git clone https://github.com/alpibrusl/lexsys-hooks && cd lexsys-hooks

REV=$(sed -n 's/^ *LEX_SYS_REV: *//p' .github/workflows/ci.yml)           # the revisions CI uses
LOG=$(sed -n 's/^ *LOG_REV: *//p' .github/workflows/ci.yml)
(cd ../lex-sys && git fetch -q origin && git checkout "$REV" && cargo build --release -p lex-sys)
(cd ../lexsys-log && git checkout "$LOG")
export LEX_SYS=$PWD/../lex-sys/target/release/lex-sys

scripts/build.sh                      # builds build/hooks (and the test probes and the fsync shim)

# one endpoint: <id> <host> <port> <secret>
mkdir -p /tmp/hooks-data
echo "0 127.0.0.1 9000 whsec_$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(24)).decode())')" \
  > /tmp/hooks-data/endpoints.conf

build/hooks 8080 /tmp/hooks-data &    # port, data directory
curl -XPOST -d '{"type":"user.created","id":7}' localhost:8080/events      # {"id":1}, after the flush
```

With nothing listening on port 9000 the event is stored and retried (5 s, 5 min, ...). To watch a delivery arrive, run the
receiver below first.

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
{"endpoints":1,"attempts":1,"delivered":1,"failed":0,"dead":0}
```

**Verify a delivery** the way a subscriber would, with the reference library (`pip install standardwebhooks`):

```python
from standardwebhooks import Webhook
Webhook("whsec_...").verify(body, {"webhook-id": "evt_1", "webhook-timestamp": "1791028548",
                                   "webhook-signature": "v1,5Zm7..."})   # raises if the signature or timestamp is wrong
```

**Several endpoints, and a faster schedule for trying things out.** `endpoints.conf` takes one endpoint a line (`#` comments and
blank lines are ignored); the optional third argument replaces the retry delays, in milliseconds:

```
# <id> <host> <port> <secret>
0 127.0.0.1 9000 whsec_...
1 127.0.0.1 9001 whsec_...
```

```sh
build/hooks 8080 /tmp/hooks-data 100,200,400,800    # four retries, then a dead letter after the fifth attempt
```

## HTTP API

| | |
|---|---|
| `POST /events` | a JSON object with a string `"type"`; answers `202 {"id":N}` after the flush, `422` for a body that is not one, `503` if the log is broken |
| `GET /events/:id` | the stored event, `404` if there is none |
| `GET /stats` | `{"endpoints","attempts","delivered","failed","dead"}` |
| `GET /healthz` | `{"ok":true}` |

A delivery is `POST /hook` to the endpoint, with the event as the body and three headers: `webhook-id` (`evt_<id>`, the same on
every attempt, so a receiver can drop a repeat), `webhook-timestamp` (Unix seconds) and `webhook-signature` (`v1,` and the base64
HMAC-SHA256 of `<id>.<timestamp>.<body>` under the decoded secret). Any `2xx` is a delivery. Anything else, a timeout or a
refused connection is a failure; the retries come 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h after the previous
attempt, and then the event is a dead letter for that endpoint.

## How it works

One thread, one poller. An accepted event is appended to a [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) segment and its
request held; after the turn one `flush` covers every append and the held requests are answered. Delivery then runs in the same
loop: each endpoint has a cursor (every event up to it is delivered or dead) and a window of events above it that finished out
of order or are waiting for a retry, so a failing event does not hold up the ones after it. What happened to each attempt goes to
a second log, `delivery.seg`, which a restart replays; the time of the next attempt is a Unix time, so it survives too.
[`docs/design.md`](docs/design.md) has the semantics, the scenario fixed before the build, and what each step found.

## Tests

```sh
$LEX_SYS test tests/state_test.ls src/state.ls ../lexsys-log/src/record.ls ../lexsys-log/src/crc.ls --std     # the delivery window
$LEX_SYS test tests/endpoints_test.ls src/endpoints.ls src/sign.ls src/state.ls ../lexsys-log/src/record.ls ../lexsys-log/src/crc.ls --std
python3 tests/sign_test.py build/sign_probe        # signatures and base64 against the reference library (536 checks)
python3 tests/attempt_test.py build/attempt_probe  # one delivery attempt against seven kinds of receiver
python3 tests/retry_test.py build/hooks            # the retry delays, also across restarts, and the dead letter
python3 tests/isolation_test.py build/hooks        # what a stalled or slow endpoint costs the others (reports; gates two cases)
python3 tests/chaos.py build/hooks 2000 8 50       # kill -9 as a power cut: no acknowledged event may be lost
python3 tests/delivery.py build/hooks 300 4 150    # three endpoints, signed, retried and dead-lettered, with the service killed
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` is a report, not a gate.

## Documentation

- [`docs/design.md`](docs/design.md): what this is for, which store owns which fact, the delivery semantics, the test scenario
  fixed before the build, the gaps predicted, and sections 13 to 15 on what building each step showed.

## Layout

```
src/hooks.ls       the service: routes, the loop, delivery
src/deliver.ls     one delivery attempt: connect, send, read a status line with a deadline
src/state.ls       the per-endpoint cursor and window, and the outcome record
src/endpoints.ls   the endpoints file
src/sign.ls        HMAC-SHA256, base64 and the Standard Webhooks signature
scripts/build.sh   builds the service, the probes and the fsync shim
tests/             unit tests (lex-sys) and harnesses (Python)
docs/design.md     the design and what building it found
```

## Limitations

One process, one thread; delivery blocks it (`connect` to an unanswered address for as long as the kernel waits). Endpoints come
from a file read at start, not a database; hostnames are resolved by a blocking call; no `https`. At most 16 endpoints, and an
endpoint more than 1,024 events behind is not served until it catches up. `delivery.seg` is never compacted. Not for production.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src`, the unit tests and the harnesses above. Design before code,
in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
