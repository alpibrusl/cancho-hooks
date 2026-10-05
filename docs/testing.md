# Testing

How the tests are built, and the commands that run them.

## How the tests are built

The service is tested as a black box, with the real binary. A Python harness starts `build/hooks` in a temporary directory, drives it over HTTP, breaks things around it, and checks what happened from the outside; the Lex unit tests cover the pure parts. Nothing about the tests relies on the service's own account of itself.

* **Harnesses.** 51 programs (`tests/*_test.py`, `tests/chaos.py`, `tests/delivery.py`) each start the service and drive it with real clients and real receivers. They read the result from the receivers' side (in `delivery.py`, `sign_test.py`, `rotation_test.py`, `headers_test.py` and `https_test.py` a receiver verifies the signatures with the reference `standardwebhooks` library) and from the files: `tests/chaos.py` reads `events.seg` with a reader of its own, `scripts/logcheck.py` classifies a pair of logs apart from the service, and `tests/dead_test.py` folds the delivery log itself, so a check is made against the log and not against what the service says about it.
* **Crashes, at chosen points.** The service is stopped with `kill -9`, at random instants (`chaos.py`, `delivery.py`), when the receivers have seen a stated number of deliveries since the last start (so the kills follow progress, not the clock: `filter_test.py`, `dead_test.py`, `limits_test.py`, `scan_test.py`), or at a numbered step of its own compaction (`retention_test.py`: the service is told to stop at a numbered step, and the test kills it there).
* **Power cuts.** A plain `kill -9` cannot show a missing flush, because the kernel keeps every byte the process wrote. `tests/fsync_shim.c`, an `LD_PRELOAD` shim, records how long each log file was whenever `fsync` returned; before the restart the harness cuts each file to that length plus a random part of the rest, and may zero its last block: the data directory as a power cut could leave it.
* **The database.** `tests/pgproxy.py` sits between the service and PostgreSQL and can cut the connections, refuse new ones, freeze both directions without losing a byte (a cable pulled and put back), read and discard, black-hole new connections (they hang, as behind a firewall that drops packets) and end the backends of its own connections with `pg_terminate_backend`.
* **https and names.** `tests/tlskit.py` makes throwaway certificate authorities and certificates with the `openssl` command (expired, not yet valid, another name, another authority, self-signed, a purpose that does not fit), a TLS receiver that fails in a chosen way and reports what it saw (name, resumption, protocol), and a name server over TCP that answers what the test says, counts the questions and can be slow or change its answer.
* **Partial reads and writes.** `tests/io_shim.c`, another `LD_PRELOAD` shim, makes `send` take only so many bytes, `recv` return only so many, and every third call fail with `EAGAIN`, on the ports it is given: the branches a loopback socket never reaches. (The partial-write branch of a plain `http` attempt is still not reached by it; the shim is used on the TLS receiver and the name server.) `tests/stat_shim.c` makes the production profile's mode check fail in ways no file mode can.
* **Receivers that misbehave.** One that accepts and never answers, answers after 300 ms, closes without a word, resets in the middle of the response, answers garbage or a status line in two pieces, answers `500`, `404`, `301` or `410`, reads a 60,000-byte request a few KiB at a time, has a full accept queue, or sits behind a blackholed address (`attempt_test.py`, `reason_test.py`, `isolation_test.py`).
* **Mutation testing.** For each piece of new code, mutants (one edit each, on a copy of the tree) must be killed by a test; the design document lists, for each piece, how many were made, how many were killed, and every survivor with the argument for why it survives. `scripts/mutate.py` runs a list of mutants and checks that the file is restored byte for byte; the list for the `https` work is committed (`tests/mutants/https.py`: 49 mutants, 47 killed, one the compiler refuses and one that survives because it is redundant by design). The other lists were scratch work and are described in the design document, not kept. Mutation testing is run by hand, not in CI.
* **Unit tests in lex-sys.** `lex-sys test` runs `tests/*_test.ls`: **226 tests in 21 sets** for the pure parts (the cron calendar, the delivery state, the idempotency index, the settings, the destination rules, the DNS answers, the token bucket, the table of dead letters, the counters and the text of `/metrics`, and so on). lex-sys has no `examples {}` blocks, so a test is a function of assertions; the compiler's own test runner reports each by name.
* **CI** runs all of it on every push and pull request: the formatting check, the authority pin, the unit tests, the 51 harness programs and `shellcheck`. It does not run the mutation lists or the scripts under `scripts/bench/`, which are reports.

## The tests

To run them you need `python3` and `pip install standardwebhooks` (the independent implementation signatures are checked against), the `openssl` command (the tests of `https` make their own certificate authorities), and, for the tests of the database, a PostgreSQL and `psql`; `HOOKS_PG=host:port:user:database` names the database.

```sh
$LEX_SYS test                                      # the lex-sys unit tests: 223 in the 21 sets of lex-sys.toml (state, endpoints, destination, idem, cron, authz, filter, hdrs, epx, retain, config, dns, resolve, manage, reason, dbup, jitter, bulk, dead, lim, ops)
python3 tests/cron_test.py build/cron_probe 500      # cron expressions against an independent implementation (calendar, steps, day-of-month and day-of-week, leap days)
python3 tests/sign_test.py build/sign_probe        # signatures and base64 against the reference library (548 checks)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/history_test.py build/hooks   # the history in PostgreSQL (needs one: see the file)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/pgre_test.py build/hooks      # the database goes away and comes back: cut, frozen, black-holed, backends ended; no restart, nothing lost or repeated, the loop never stalls
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/roster_test.py build/hooks    # the endpoints in PostgreSQL: import, read at start, every refusal
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/manage_test.py build/hooks    # POST /endpoints and GET /endpoints/:id: the token, the request, from now
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/slots_test.py build/hooks     # endpoint ids and slots: the legacy log, ids above 15, dormant, reclaimed
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/patch_test.py build/hooks     # PATCH /endpoints/:id: address on the next attempt, secret rotation, a database that refuses, compaction
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/delete_test.py build/hooks    # DELETE /endpoints/:id: nothing new after it, an attempt on the wire finishes, replays dropped, the slot reused clean across a restart, 1,024 endpoints churned
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/many_test.py build/hooks       # 1,024 endpoints: the limit and the 1,025th, kill -9 over all of them, disabled and paused in every slot, replays and dead letters, the loop not looking at what has nothing to do, retention pins, a database away at the start, the logs an older build refuses
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
python3 tests/retention_test.py build/hooks      # retention: segments dropped, pins, the snapshot, formats, keys, kill -9 at all 19 steps of a compaction, the stall, a flat memory (retention.md)
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/dead_test.py build/hooks      # dead letters: the list and its pages against the log, kill -9, bulk replay under the bound of 32, the table of 2,048, a log from before the time of death
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/cancel_test.py build/hooks    # cancelling a waiting replay: one or all, kill -9, a power cut, an attempt on the wire, the bound of 32
python3 tests/jitter_test.py build/hooks           # retry jitter: bounds, mean and spread, 0 is the schedule, the same delay from the same endpoint and event, the recorded time kept across kill -9
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/limits_test.py build/hooks    # concurrency and rate per endpoint, observed at a receiver; held-back events are not failures; the table, endpoints.conf, an old schema
HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/backup_test.py build/hooks    # backup and restore: total loss, kill -9 under online backups, every refusal (runbook.md section 4)
FULL=1 HOOKS_PG=127.0.0.1:5432:postgres:hooks python3 tests/schedules_test.py build/hooks   # schedules: the token, every refusal, a fire is an ordinary event, missed fires, kill -9 between the event and the database, a real minute (needs a database of its own: it empties `schedules`)
```

The crash tests emulate a power cut with a small `LD_PRELOAD` shim (`tests/fsync_shim.c`): a plain `kill -9` cannot show a
missing flush, because the kernel keeps every byte the process wrote. `tests/stall_probe.py` (what a bad receiver costs ingest) and `scripts/bench/stall_probe.py` (two endpoints, one dead, 3,000 events: the cursors) are reports, not gates, and so is `scripts/bench/retention_bench.py` (`through`: millions of events through one endpoint with the disk and memory sampled; `start`: the start with a million events retained).
