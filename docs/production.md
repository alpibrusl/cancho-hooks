# What "production" means for lexsys-hooks, and the plan to get there

*A plan with gates, written after measuring, not a promise. Effort figures are judgement and say so. Where a decision was taken on the owner's behalf so that work could start, it is marked **(default taken)** and can be reversed.*

## What the word means here

**Production** is a single-tenant, self-hosted service that an operator can run unattended for months: it does not lose an acknowledged event, one broken receiver cannot hurt delivery to another, it can be watched, backed up, upgraded and stopped safely, and it refuses to start in a configuration that is unsafe on the internet. A multi-tenant hosted service (customers, a portal, billing, SDKs, SOC 2) is a different and larger goal and is listed last, not folded in.

The README says "Not for production" today. That line comes off when the **gate of every P0 item is met and published**, not before.

## Where it stands, measured

| | |
|---|---|
| Durability | no acknowledged event lost under `kill -9` (2,000 events, `tests/chaos.py`); idempotency survives crashes |
| Capacity | about 90 to 140 µs of CPU per plain-HTTP delivery, so roughly 7 to 10k deliveries a second on one core; ingest 34 to 60k events a second over 64 connections (`scripts/bench/run.py`, a shared 4-core box, single runs) |
| Safety | destinations are public IPv4 literals unless `allow-private-hosts`; admin token on endpoint management; one bug of this kind found by measuring and fixed (section 28) |
| **Not production** | **one dead endpoint stops every endpoint after 1,024 events (section 29, measured)**; logs never shrink and the start reads them whole; `POST /events` and `GET /events/:id` need no credential; no `https`; no metrics, no readiness route (`GET /healthz` exists and says only that the process is up) and no graceful stop; a failed attempt records no reason |

## P0: blockers. Each has a gate that is a command.

**0.1 A dead endpoint must not stop the others.** Replace the shared ring of event offsets and the single scan position (`extend_scan`, `lowmark`, `c_scanned`, `c_offset`) with a **scan position per endpoint**, so each endpoint reads the events log forward from its own cursor and is bounded only by its own window. Decide and document what a long-dead endpoint does (a circuit breaker: after N days of failures the endpoint is paused, its events wait in the log and resume on re-enable; **default taken:** paused after 5 days of consecutive failures, as the larger services do, configurable, logged, shown in `GET /endpoints`).
*Gate:* `scripts/bench/stall_probe.py` ends with the healthy endpoint at 3,000 of 3,000; a dead endpoint revived later receives all 3,000 once each, in order within its window; the whole existing suite passes; a mutant that restores the global bound fails a new test; restart in the middle of it loses and repeats nothing.

**0.2 The logs must be bounded.** `events.seg` and `delivery.seg` are never compacted and the start reads both whole (the idempotency index is rebuilt from the events log; outcomes are replayed from the start). Design first (it may need multi-segment support in `lexsys-log`, a separate repository), then build: events are dropped when final at every endpoint and older than a retention period (**default taken:** 30 days, configurable), and the outcome log is compacted by a snapshot of the state it replays to.
*Gate:* with 10 million events through, disk use is bounded by the retention and not by history; start time is measured at 1M, 10M events and stated; a crash during compaction loses nothing (kill test at every step); `docs/design.md` says what is *not* recoverable after retention.

**0.3 Credentials on every route, and a profile that refuses to be unsafe.** Today `POST /events`, `GET /events/:id`, `GET /events/:id/attempts`, `GET /endpoints`, `POST /endpoints/:id/enable` and `/replay` need no credential. Add scoped tokens (ingest, read, admin), and `production = 1`, which **refuses to start** unless the admin and ingest tokens are set, private destinations are off, and the data directory is not world-readable. Defaults for development are unchanged. Secrets at rest are in the clear in the table (said so since section 24.1): decide on and document either encryption at rest with a key from outside the database or an explicit statement that the database is the trust boundary.
*Gate:* a matrix test of every route against every token (a route added later without a row fails the test); `production = 1` refuses each unsafe combination with a distinct message; constant-time comparison kept; nothing in logs or stats leaks a token or a secret.

**0.4 It can be watched and stopped.** `GET /healthz` (process up; **built**, but it looks at nothing: it answers `200` with the disk full) and `GET /readyz` (logs open, database reachable if named); `GET /metrics` in the Prometheus text format (ingest, flush, attempts by outcome, in flight, per-endpoint lag and cursor, retries waiting, dead letters, history queue, log sizes); the **reason** a delivery attempt failed recorded with the outcome (connect refused, deadline, status, reset: today a spurious failure cannot be told from a refusal after the fact); `SIGTERM` stops accepting, finishes what is on the wire within a deadline, flushes, and exits 0.
*Gate:* each metric is checked against a known workload; `kill -TERM` under load loses and duplicates nothing beyond the at-least-once contract and exits 0 within the deadline; the failure reason is in the log and the history table.

**0.5 Refuse corruption instead of repairing it silently (found by the packaging slice, `docs/runbook.md` 4.7).** Two measured hazards in the service and in `lexsys-log`: (a) if `delivery.seg` refers to an event id above the last one in `events.seg` (an older events log restored beside a newer outcome log), the service starts, acknowledges new events with `202` and never delivers them; (b) one flipped byte in the middle of a log makes the next start silently truncate to the damage (500 events became 250, nothing printed). Fix: refuse to start with a distinct status for (a); make `recover` refuse to cut more than one torn tail record, and say what it cut; `/readyz` reports a broken log.
*Gate:* a test per hazard (the restored-pair and the flipped-byte cases of `tests/backup_test.py` become refusals); a torn tail of one record still recovers silently-but-logged.

## P1: what users expect, in this order

1. **Event-type filtering** per endpoint (the column that section 25 left out): only the events an endpoint subscribes to are sent, decided cheaply from the stored record (the gateway design, `docs/inbound-gateway.md` D10 and D11, has the mechanism).
2. **Retry jitter**, and a per-endpoint **concurrency and rate limit**.
3. **Secret rotation with two signatures** (Standard Webhooks allows `v1,a v1,b`): the old key stays valid for a stated period; a test with the reference library's multi-signature verification.
4. **Dead letters you can list and replay in bulk**: `GET /endpoints/:id/dead`, `POST /endpoints/:id/replay-dead`.
5. **Custom headers** per endpoint (never the signature or hop-by-hop ones).
6. **More than 62 endpoints** (the limit comes from the disabled bit-set and the per-slot window: 62 x 1,024 x 3 integers). Raise it to a stated number with the memory cost measured; the layout is a chain of computed offsets, so this is bounded work.
7. **`https` delivery.** The spike (`lex-sys` `docs/tls-nonblocking.md`) measured about 0.65 ms of CPU per verified handshake on one core, a resolver that does not block the loop, and certificate failures each with their own code. Three slices (T1 the TLS phase, T2 names and the SSRF rule moving to every attempt, T3 sessions). **(default taken)** OpenSSL in-process, and the gap this exposes in lex-sys (the `Ffi` scope is only a label, so the authority report turns `UNBOUNDED`) is fixed in lex-sys; the pure-lex-sys TLS epic (lex-sys#197) replaces OpenSSL later and removes the problem.
8. **Cron**: a schedule fires an event. **(default taken)** A `schedules` table in PostgreSQL (id, cron expression of five fields, event type and body, time zone UTC), a tick in the loop, and **exactly-once firing by construction**: each fire is appended as an ordinary event with the idempotency key `cron:<id>:<scheduled-second>`, so a crash and a restart cannot fire a second copy. Missed fires after a stop: fire once for the missed window and skip the rest (configurable). CRUD under the admin token, `GET /schedules/:id` showing the next fire. Effort: small, because the engine already exists (a few days).

*Gate for each:* its own test file, mutants, the README and the page.

## P2: operating it

A `Dockerfile` and a `systemd` unit; a tested **backup and restore** (the two logs and the database, consistent: stop-the-world and online variants); an upgrade procedure with **log format versions** and a refusal, not a guess, on an unknown version; a runbook (what each log line and metric means, what to do); a release with a stable URL, checksums and an SBOM; a **soak test** of at least 24 hours under chaos (kills, a dead endpoint, a slow database) with memory, descriptors and disk watched, its numbers published; a capacity page with the method.

**Status (packaging slice, built without touching `src/`):**

| item | state | what was verified, and what was not |
|---|---|---|
| `Dockerfile`, `.dockerignore` | **done, built by hand** | built (1 min 40 s cold), run, healthy, non-root (uid 10001), a volume that kept an event across a restart, `docker stop` in 0.08 s under `tini` (10.1 s and exit 137 without it: a PID 1 ignores `SIGTERM`). **Not verified:** a CI build, the base image by digest (a tag today), a second architecture. The sandbox needed a proxy CA, supplied by pointing `--build-arg BASE=` at a base image that has it |
| `deploy/hooks.service`, `deploy/hooks.conf.example` | **done, never run under systemd** | `systemd-analyze verify` and `security --offline` (exposure 1.3); the 34 system calls of a real workload are all in `@system-service`; no executable anonymous mapping. **Not verified:** a start under a real systemd (the sandbox has none) |
| backup and restore (`scripts/backup.sh`, `restore.sh`, `logcheck.py`, `tests/backup_test.py`) | **done, tested** | stopped and online variants; total loss and restore with PostgreSQL; no acknowledged event lost and no repeat beyond at-least-once, under `kill -9` as a power cut; 7 mutants of the scripts killed. **Online is safe under stated conditions, not proven**: `docs/runbook.md` section 4.4 lists them and what would withdraw it. Two things found that the *service* should fix (4.7): it silently stalls new events when `delivery.seg` outruns `events.seg`, and it silently truncates a log at a flipped byte in the middle. **Not verified:** a log of hundreds of MB (the checker reads 8.6 MB/s), the password path of PostgreSQL in the test (CI's SCRAM database; it is passed to the service as in the other tests), a restore across PostgreSQL major versions |
| runbook (`docs/runbook.md`) | **done** | every log line and `/stats` field read from the source; the disk-full and corrupted-log scenarios run. The items that depend on 0.1 to 0.4 are marked planned |
| release (`scripts/release.sh`) | **done** | tarball, `SHA256SUMS`, SBOM stub; the binary is bit-reproducible after stripping the pid-bearing symbol and the build-id (four builds, one hash). **Not done:** signing, a stable URL, a real SBOM format (CycloneDX or SPDX) |
| log format versions and a refusal on an unknown version | **not built** | needs a change to the service and to lexsys-log |
| soak test (24 h), capacity page | **not built** | |

## P3: a hosted, multi-tenant service (not before P0 to P2)

Tenants and their tokens, a customer portal, usage metering and billing, SDKs, per-tenant limits, an audit log, SOC 2 or ISO 27001. See the business sketch in the issue tracker; none of it is started.

## Order and what runs in parallel

Wave 1 (independent areas): **0.1**, **cron**, and **P2 packaging** (no change to the hot path). Wave 2: **0.3 and 0.4**, **P1.1 to P1.6**, **0.2** (design first). Then **https** (T1 to T3). Merges are one at a time into `main`; each change carries its own tests and mutants and goes through the same suite.

Effort, as judgement: 0.1 about a week; 0.3 and 0.4 about a week together; 0.2 two to three weeks including the design; the P1 list about three weeks; https about two to three weeks; P2 about a week. About three months of focused work to move the line from "not for production" to a published, measured claim, if nothing unexpected turns up, and things have turned up every time something was measured.
