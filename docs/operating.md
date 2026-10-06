# Operating it: metrics, stopping, installing

The `/metrics` series, readiness and health, stopping the service, building it and running a prebuilt binary. `runbook.md` is the operator's guide: what each log line means and what to do when it goes wrong.

## Metrics

`GET /metrics` answers in the Prometheus text format (counters are since this start; a restart is a reset, which `rate` and `increase` expect). About 70 series for the service and 9 for each endpoint (a tenth, its last failure, once it has failed), labelled by the endpoint's `id` and by nothing else that grows, in pages of 64 endpoints (below):

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

`GET /metrics` is a **read**-scope route: when a `read-token` is configured (or, in production, the admin token), a scraper sends it as `Authorization: Bearer <token>`; without one the route is open, like the other reads ([security.md](security.md)). `GET /readyz` and `GET /healthz` are always open. What to alert on is in [`runbook.md`](runbook.md) section 1.

**Pages.** The server's queue for one answer is 64 KiB, so with more than 64 endpoints `GET /metrics` is read in pages: `?page=0` (the default) has the service's own series and the per-endpoint series of the first 64 endpoints of the table, `?page=k` the endpoints 64k to 64k + 63 and nothing of the service's (so scraping every page counts nothing twice), a page past the last is a `404`, and `hooks_metrics_pages` says how many there are. A Prometheus scrapes one job for each page (`params: {page: ["1"]}`); with 64 endpoints or fewer there is one page and nothing changes. The longest page (64 endpoints, seven-digit values) is under 56 KB.

## Stopping

`SIGTERM` (`systemctl stop`, `docker stop`) or `SIGINT`: the service stops taking requests (every write that passes the credential check is a `503` that closes the connection; `GET /readyz` says `stopping`; `GET /healthz`, `/metrics` and the other reads still answer), starts no attempt, lets the attempts on the wire finish for at most `stop-deadline-ms`, flushes both logs and exits **0**. Attempts still on the wire at the deadline are made again at the next start (at least once, as after a crash). A stop is noticed at once, not at the next turn of the loop (the poller waits on the signals too). A second signal ends the process at once, killed by that signal. Nothing is lost either way: an acknowledgement is only sent after the flush that covers the event.

## Requirements

- The **lex-sys** compiler at the commit `lex-sys.toml` names (`[package] lex-sys`); `lex-sys build` refuses any other. It needs `clock_unix_ms` (the signing timestamp; lex-sys PR #190),
  `tcp_connect_start` (attempts that do not wait; #191), a lock with an origin (#192), the project file (#193), and `std.hmac`, which signs
  every delivery and replaced this repository's own HMAC (#229; [`design.md`](design.md) section 30).
- `git`: [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and lex-sys's `http-server` are not cloned by hand; they are dependencies in `lex-sys.toml`, pinned to a commit each, and `lex-sys build` fetches and checks them.
- Rust, to build the compiler; `gcc`, to build the three small shims the tests preload (`fsync`, for the crash tests; `fstat` and `fstatat`, for the production profile's; `send` and `recv`, for the partial-I/O tests).
- OpenSSL 3.0 or later: its development files (`libssl-dev`) to build, because the service is linked against `libssl` and `libcrypto` (`scripts/build.sh` does it; a plain `lex-sys build` stops at the link), and `libssl3` and `ca-certificates` to run it.

## A prebuilt binary

Every CI run that passes keeps the service as an artifact of the run (Actions, the run, "Artifacts": `hooks-linux-x86_64-<commit>`,
a zip of `hooks-linux-x86_64` and its `.sha256`). It is built by the compiler this commit pins, for Linux x86-64 with the glibc of
`ubuntu-latest` or newer, and the zip loses the executable bit:

```sh
unzip hooks-linux-x86_64-*.zip && sha256sum -c hooks-linux-x86_64.sha256 && chmod +x hooks-linux-x86_64
./hooks-linux-x86_64 --port 8080 --dir /var/lib/hooks
```

A run's artifacts expire (90 days by default); a release with a stable URL is not built.
