<p align="center"><img src="docs/assets/cancho-hooks-logo.jpg" alt="cancho-hooks" width="220"></p>

# cancho-hooks

[![ci](https://github.com/alpibrusl/cancho-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/cancho-hooks/actions/workflows/ci.yml)

**Webhooks that survive failure.** A self-hosted delivery service: your events stay on your infrastructure. Post an event once: it is on disk before you get the answer, then signed ([Standard Webhooks](https://www.standardwebhooks.com)) and delivered to every subscribed endpoint, **at least once**, through crashes, outages and slow receivers. One small binary, written in [cancho](https://github.com/alpibrusl/cancho). The [project page](https://alpibrusl.github.io/cancho-hooks/) has the pictures.

**Status: alpha, not for production yet.** The API may still change. No 24-hour chaos soak has yet completed as a valid run (the third ended inconclusive at 15.8 hours when the test harness failed, not the service as far as is known: [docs/soak.md](docs/soak.md)), so the capacity figures it would give are not published; `https` uses OpenSSL until cancho's own TLS has been independently reviewed (a second build, `hooks-pure`, has no foreign function at all: [docs/pure-tls.md](docs/pure-tls.md)). The whole list, with what each item was measured as: [docs/status.md](docs/status.md).

## What you get

* **Durable ingest.** `202` only after the flush that covers the event; `kill -9` and power cuts lose no acknowledged event.
* **Retries, dead letters, replay.** Nine retries over about three days with jitter, then a dead letter you can list and replay, one event or in bulk.
* **Isolation.** A slow or dead endpoint does not stall the others; each has its own rate and concurrency, and a circuit breaker pauses one that has failed for days.
* **Per endpoint.** Event-type filters (`invoice.*`), custom headers, secret rotation with both signatures valid while receivers switch.
* **Cron.** Schedules that fire exactly once, even if the service is killed mid-fire.
* **Operable.** `/readyz`, Prometheus `/metrics`, graceful stop, backup and restore, retention that bounds the logs.
* **Hard to run unsafe.** Scoped tokens, and a `production` profile that refuses to start open; deliveries go only to public addresses, checked at every attempt.
* **`https` and PostgreSQL**, both optional: TLS with the chain and name verified; endpoints and an attempt history in a table.

## Install

A prebuilt binary for Linux (x86-64 and arm64), from the [releases](https://github.com/alpibrusl/cancho-hooks/releases) (alpha: see the status above). Each tarball comes with a checksum, and a build attestation that says this repository's release workflow made it:

```sh
VERSION=0.1.0-alpha.1; ARCH=$(uname -m)                      # ARCH is x86_64 or aarch64
BASE=https://github.com/alpibrusl/cancho-hooks/releases/download/v$VERSION
curl -fLO "$BASE/hooks-$VERSION-linux-$ARCH.tar.gz" -O "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing                     # it is the file that was published
gh attestation verify "hooks-$VERSION-linux-$ARCH.tar.gz" --repo alpibrusl/cancho-hooks    # and this repository built it
tar -xzf "hooks-$VERSION-linux-$ARCH.tar.gz" && cd "hooks-$VERSION-linux-$ARCH"
mkdir data && echo "0 127.0.0.1 9100 whsec_$(head -c24 /dev/urandom | base64)" > data/endpoints.conf
bin/hooks --port 8080 --dir data --allow-private-hosts 1 &
sleep 1                                                      # until it listens
curl -d '{"type":"user.created"}' localhost:8080/events      # {"id":1}
```

Or as a container (x86-64): `docker run -d -p 8080:8080 -v hooks-data:/var/lib/hooks ghcr.io/alpibrusl/cancho-hooks:0.1.0-alpha.1`.

The binary uses the system's OpenSSL for `https` delivery (`libssl3`: `apt install libssl3` on Debian and Ubuntu) and a glibc as new as Ubuntu 24.04's. The tarball also holds `bin/hooks-mcp` ([for agents](docs/agents.md)), `bin/hooks-logcheck`, the backup and restore scripts, a systemd unit, and the documents. The [runbook](docs/runbook.md) says how to run it for real; the endpoint above only shows it works (the quick start below has a receiver that verifies the signature).

## Quick start (building from source)

You need `git`, Rust, `gcc`, OpenSSL 3 with its development files (`libssl-dev`), Python 3, and `curl`.

```sh
git clone https://github.com/alpibrusl/cancho                           # the compiler
git clone https://github.com/alpibrusl/cancho-hooks && cd cancho-hooks
REV=$(sed -n 's/^cancho *= *"\(.*\)"/\1/p' cancho.toml)                  # the compiler these sources need
(cd ../cancho && git fetch -q origin && git checkout "$REV" && cargo build --release -p cancho)
export CANCHO=$PWD/../cancho/target/release/cancho
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

## Learn more

| | |
|---|---|
| [docs/api.md](docs/api.md), [docs/configuration.md](docs/configuration.md) | every route and setting |
| [docs/examples.md](docs/examples.md) | worked examples |
| [docs/delivery.md](docs/delivery.md), [docs/endpoints.md](docs/endpoints.md), [docs/cron.md](docs/cron.md), [docs/https.md](docs/https.md) | how it behaves |
| [docs/security.md](docs/security.md), [docs/privacy.md](docs/privacy.md), [docs/runbook.md](docs/runbook.md), [docs/operating.md](docs/operating.md) | running it safely, and what it holds about people |
| [docs/status.md](docs/status.md), [docs/production.md](docs/production.md), [docs/soak.md](docs/soak.md), [docs/capacity.md](docs/capacity.md) | what is built, what is left, what was measured |
| [docs/testing.md](docs/testing.md), [docs/design.md](docs/design.md) | how it is tested, and what building each step found |

## Contributing

Every change goes through what CI runs: `fmt --check src`, the unit tests, the harnesses and `scripts/check-authority.sh`. Design before code, in `docs/design.md`, with claims measured; a claim that turns out false is corrected in place. See [docs/testing.md](docs/testing.md) and [docs/layout.md](docs/layout.md).

## Licence

[EUPL-1.2](LICENSE). Open source, provided as is, without warranty: you run it at your own responsibility. What it holds about people, and what it does and does not do for data-protection duties, is in [docs/privacy.md](docs/privacy.md); using it does not by itself make a deployment compliant with anything.
