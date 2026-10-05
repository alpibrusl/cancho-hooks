# The soak test: what is run, what is watched, what passes

A webhook service earns the word "production" by running for a long time, under fault, without anyone looking at it. This page is the method of that
test: what the harness does to the service, what it keeps watching, the invariants it checks (and how each one is checked), and the pass and fail
criteria. **The criteria below were written before any run and are the ones a run is judged by**; where a threshold was changed after measuring, the
change is listed at the end, in "Changes to the criteria", with the reason.

The harness is `scripts/soak/`. It is plain Python 3 (standard library only), starts the service itself, and needs a PostgreSQL it may use a database of.

```sh
# the real run: 24 hours, every chaos action on, the numbers kept in soak-out/
HOOKS_PG=127.0.0.1:5432:postgres:hooks_soak \
python3 scripts/soak/soak.py --binary build/hooks --hours 24 --seed 1 --out soak-out
python3 scripts/soak/report.py soak-out          # report.md and report.json, from the files of the run

python3 scripts/soak/soak.py --selftest          # the harness tested on a mutant: three minutes clean (must pass), three with a fault (must fail)
python3 scripts/soak/soak.py --resume soak-out   # continue a run after the machine or container was restarted (see "Resuming")
```

## 1. What is run

### The service

One `build/hooks` under the production-shaped settings of a service that is being exercised, with the three tokens, a database (through a proxy the harness
can break), `--cron-seconds 1`, and the logs shrunk so that a month of rolling and dropping happens in minutes:

| setting | value | why |
|---|---|---|
| `retention-ms` | 180,000 (3 min) | a segment is dropped three minutes after its last event is final, so segments roll and drop all day |
| `window-ms` | 120,000 | the idempotency window must be shorter than the retention for a segment to be droppable |
| `segment-bytes` | 1 MiB | a roll every few seconds at the default rate |
| `delivery-log-bytes` | 256 KiB | a snapshot of the outcomes log every few seconds |
| `schedule` | 500, 1000, 2000, 4000, 8000, 16000 ms | an event dies after about 31 s of failure: short enough that dead letters happen in the test, and the chaos that endpoints are put through (below) is shorter than that unless a dead letter is the point |
| `deadline-ms` | 2000 | the default |
| `allow-private-hosts` | 1 | the receivers are on `127.0.0.1` |

The service is pinned to one core with `taskset` and the harness to the others (`--service-cpus`, `--harness-cpus`; the default on a machine with four or
more cores is the last core for the service and the rest for the harness). **The harness's own CPU is measured and reported** (the processes that make
the load and keep the ledger, from `/proc`), so that a capacity figure is never one that the harness polluted, and a run in which the harness could not
hold the rate it was asked for is marked as such rather than passed.

### The workload

* **Events.** A steady stream at `--rate` events a second (the capacity page, `docs/capacity.md`, says how the rate is chosen: about 30 % of the
  capacity measured for the same endpoint mix), with a burst phase at `--burst-rate` (the measured capacity, about 100 %) for a minute every twenty
  minutes. Seven event types in fixed proportions (`user.created` 30 %, `user.updated` 25 %, `order.paid` 15 %, `order.refunded` 6 %, `invoice.paid` 10 %,
  `ping` 12 %, `rare.event` 2 %), bodies from 150 bytes to 50 KB (a long tail: 80 % under 400 bytes), nine in ten posted under an `Idempotency-Key`, and
  every event numbered by the poster (`n`) so that it can be recognised wherever it turns up.
* **Idempotency.** Three in a hundred keyed events are posted again at once with the same key (the answer must be the same id), and one in a hundred
  again with the same key and a *different* body (must be a `422`). A post that failed because the service was killed is retried with its key, so a
  kill never makes two events from one `n`.
* **Endpoints** (a mix, `--endpoints N`, default 12, at most 59 and the harness adds up to three that come and go):

  | class | what the receiver does | what it subscribes to | what must happen |
  |---|---|---|---|
  | `oracle` | answers 204 at once | every type | every event, once |
  | `healthy` | 204 at once | every type | every event, once |
  | `filter` | 204 | `user.*`, `invoice.paid` | exactly those, once; nothing else, ever |
  | `slow` | answers after 50 to 400 ms | `order.*`, `ping` | every event wanted, once |
  | `flapping` | for 3 to 8 seconds in every 30 refuses connections, resets them, or closes without answering | `user.updated`, `order.*` | no event may die (the down time is shorter than the retry horizon): every event wanted, once |
  | `http5xx` | answers 500, 502 or 503 to the first 0 to 3 attempts of each event | `order.paid`, `ping`, `invoice.paid` | every event wanted, eventually, with its failed attempts counted |
  | `gone` | answers `410` to the events of a fixed one per cent (a function of the seed and the event) and 204 to the others | `invoice.*`, `ping` | the one per cent are dead letters and never delivered; the rest once; the harness enables the endpoint again after each `410` |
  | `dead` | nothing listens on its port | `rare.event` | nothing delivered; every event a dead letter after the schedule |
  | `https` | TLS with a certificate from a throwaway CA the service is given (`tls-ca-file`), a name that resolves through a name server of the harness's | `user.*` | as `healthy` |
  | `rate` | 204 | `user.created` | an endpoint with `"rate"` set above its steady rate and below its burst rate: held back, never failed; every event wanted, once |
  | `sick` | in a window of 60 s every ten minutes answers 503 to everything | `ping`, `order.paid` | events that fail for the whole of the window die; after it the harness runs `replay-dead` until nothing remains: **at the end, every event wanted, delivered** |

* **Cron.** Two schedules (every second, and every fifth second) make `cron.tick` events; every endpoint that subscribes to everything receives them.
* **Replays.** Every few seconds the harness replays a recent event to every endpoint that wants it, or to one endpoint (whatever it subscribes to); a
  replay is cancelled again some of the time; the dead letters of `dead` are replayed in bulk and cancelled, those of `sick` are replayed in bulk until
  they are gone.
* **Endpoint churn.** Three threads in turn create an endpoint (a new receiver, a secret of the harness's choosing), `PATCH` it (`concurrency`, `rate`,
  a custom header, the secret with and without an overlap), let it run, wait until its cursor has reached the newest event, and `DELETE` it.
* **Secret rotation** of the long-lived endpoints, with an overlap (`keep_old_ms`: every delivery must carry two signatures while it lasts) and without
  (the old secret must stop signing at the next attempt).
* **Backups.** Every ten minutes `scripts/backup.sh --mode online`; every fourth is restored into a scratch directory with `scripts/restore.sh` and
  checked.

### The chaos

A schedule of faults drawn from a random generator seeded with `--seed`: the same seed gives the same sequence of kinds and parameters (the instants
depend on the clock and on how long the service takes to answer, so a run is reproduced in kind, not to the millisecond). **Every action is written
to `chaos.jsonl` with its time and its outcome** and the checker uses that file to decide what is excused.

| fault | what is done | what is excused afterwards |
|---|---|---|
| `kill9` | `SIGKILL`, then the files are cut as a power cut would leave them (the `fsync` shim of `tests/fsync_shim.c`: everything flushed stays, a random part of the rest goes, the last block may be zeros), then a restart | repeats of deliveries made in the 2 s before the kill (section 3, repeats); a cursor lower by what was delivered in those 2 s; a cron second missed while the service was down; the loop's probe while it was not there |
| `kill9-compaction` | the service is started with `--compact-kill-at n` (n from 1 to 19: a step of a roll, a drop or a snapshot, `docs/retention.md` section 10); when it stops at the step (it writes `killpoint`) the harness kills it and cuts the files as above | as `kill9` |
| `sigterm` | the graceful stop: it must exit 0 within `stop-deadline-ms` plus 5 s, then it is started again | repeats only of the attempts the service said it left on the wire |
| `pg-cut`, `pg-freeze`, `pg-blackhole`, `pg-hold`, `pg-kill-backends` | the proxy in front of PostgreSQL (`tests/pgproxy.py`) closes everything, stops forwarding, drops new connections, swallows what is sent, or the backends of the service's own connections are ended with `pg_terminate_backend`; for 2 to 20 s | the history rows it could not write; a change of an endpoint refused with a `503` or `504`; a cron second missed while it lasted. **Delivery is not excused: it must go on, and the loop must not stall** |
| `pg-restart` | with `--pg-restart-cmd`, a real stop and start of the server (off by default: it is somebody else's server on a shared host) | as the other database faults |
| `receiver-vanish` | the listener of a chosen endpoint is closed for 3 to 12 s (connection refused) | nothing: no event may die in so short a time |
| `receiver-slow` | the receivers answer after 1 to 1.7 s, or after 2.5 s (past the deadline: the service counts a failure, the receiver has the request, and so a repeat is not a violation, because that answer was not an acknowledgement), or accept and say nothing | nothing |
| `disk-full` | with `--tmpfs-data MB` the data directory is a `tmpfs` that is filled to the last byte for a few seconds (`ENOSPC` on every write), then freed, and the service is restarted as its readiness says it must be | refused events (`503`) are retried by the poster; nothing acknowledged is lost |
| `sick` window, `burst`, `enable`, `rotate`, `replay`, `backup` | the workload's own events, listed above | |

**The quiet tail.** The last quarter of the run (at least 30 minutes) has no kill and no stop: one incarnation of the service lives through it, so that
the growth of memory and descriptors is a regression on a process that was not restarted. The database and receiver faults, the replays and the churn go
on.

## 2. What is watched

Every `--sample-s` seconds (10 by default) one row of `metrics.csv` (and one line of `samples.jsonl` with the per-endpoint detail):

| column | from |
|---|---|
| `inc`, `pid`, `up` | the incarnation of the service (counted from 1) and whether it answered |
| `rss_kb`, `hwm_kb`, `threads`, `fds` | `/proc/<pid>/status` and `/proc/<pid>/fd` of the service |
| `cpu_s` | the service's user plus system time |
| `data_bytes`, `seg_files`, `files` | the data directory (the `.synced` files of the shim are not counted) |
| `events_first`, `events_last`, `segments`, `sealed`, `dropped`, `snapshots`, `maint_ms_max`, `maint_errors`, `lock_skips` | `GET /stats` |
| `delivered`, `failed`, `dead`, `filtered`, `in_flight`, `retries_waiting`, `replays_waiting` | `/stats` and `/metrics` |
| `lag_max`, `lag_sum`, `lag_over_1024` | `hooks_endpoint_lag_events`, over the endpoints (the per-endpoint values are in `samples.jsonl`) |
| `db_reconnects`, `db_failures`, `db_losses`, `hist_written`, `hist_failed`, `hist_dropped` | `/stats` |
| `probe_max_ms`, `probe_p99_ms`, `probe_errors`, `control_max_ms` | the probe (below) over the sample interval |
| `ingest_per_s`, `ingest_p50_ms`, `ingest_p99_ms`, `ingest_max_ms` | the poster's own timing of `POST /events` |
| `deliv_lat_p50_ms`, `deliv_lat_p99_ms`, `deliv_lat_max_ms` | the receivers' clock minus the time the poster sent the event, over the sample interval: the end-to-end latency of the first delivery |
| `harness_cpu_pct`, `receivers_cpu_pct`, `loadavg1`, `recv_loop_lag_max_ms` | the cost of the harness itself and the load of the host |

**The loop probe** is a separate process that asks `GET /healthz` ten times a second and, immediately after each, makes the same kind of request to a
responder of its own. The first measures the service's loop (the service has one thread and answers `/healthz` from it, so a held loop is a late
answer); the second measures the host. A stall is a window in which the service was late *and the host was not*: on a shared machine the other
agents' tests are a part of the weather, and a delay the control request shared is counted as the host's and reported as such, not as the service's.

## 3. The invariants, and how each is checked

The point of the test. Two ledgers are kept **independently of the service**: the poster's (every `POST /events`, the answer, the id) and the
receivers' (every request that reaches a receiver: which endpoint, which event, the signature checked, what the receiver answered, when). The
service's own counters are read and compared, but nothing is believed because the service said it.

**A. No acknowledged event is lost, and every wanted delivery is made.** The poster's ledger holds every event that got a `202`; the receivers' holds
every delivery. For each endpoint the harness reads its **cursor** (the service's claim: every event up to it is delivered or dead for that endpoint)
*and then* reads the receivers' ledger (which is written before a receiver answers, so everything the service could have counted is already in it). Every
event above the endpoint's creation point, up to the cursor, whose type the endpoint subscribes to, must have a **delivery** in the receivers' ledger: one the
receiver answered with a 2xx in time (a request the receiver answered `500`, or after the deadline, is a failed attempt and does not count). The check
runs continuously, a few times a second, on a window of the newest events, and at the end on everything still open; an event that is the cause of
a dead letter on purpose (`gone`'s one per cent, `dead`'s all, `sick`'s until they are replayed) is excused by the rule of its class, which the checker
computes from the same seed as the receiver. An event that is not in the cursor's claim and not delivered at the end, once the harness has waited for
every endpoint to catch up, is also a loss.

**B. At least once, and once except where the service died during an attempt.** A second delivery of the same event to the same endpoint is a repeat.
A repeat is **explained** when (1) the delivery before it was made at most 2 s before a kill, a power cut or a forced stop of the service (the
outcome is written after the receiver answers, and a flush later: what a power cut can take is the deliveries of the last turns; 2 s is far more than
a turn, and the report prints how long before the kill each repeat was), or (2) the harness asked for a replay of that event to that endpoint (up
to one repeat for each replay accepted). Anything else is a violation, and the report counts the repeats **per kill**: the number, the largest, and the
rate against the deliveries made in the 2 s before. A stop with `SIGTERM` explains only as many repeats as the service said were on the wire.

**C. Filters hold.** No delivery of an event whose type the endpoint did not subscribe to at the time (the receiver records the type of the body it
got; the harness compares it with the endpoint's list, which it keeps), and no delivery of an event older than the endpoint, other than a replay the
harness asked for.

**D. Signatures hold, and are what they should be.** Every delivery carries a `webhook-id` that matches the event, a timestamp within five minutes of the
receiver's clock, and a Standard Webhooks signature that the receiver checks itself (HMAC-SHA256 over `id.timestamp.body`, by the secret of the endpoint
at that time). While a rotation overlaps (more than 3 s after the change was answered and more than 3 s before it ends) the header must carry **two**
signatures, one of each secret; after an overlap, or a rotation without one, a delivery signed only by the old secret more than 3 s after the change
is a violation.

**E. Cursors never go backwards.** Sampled every 2 s from `GET /endpoints`: within one incarnation, strictly; after a restart that followed a kill,
the first cursor read is at least the one read 2 s or more before the kill (what was delivered in the last 2 s may have been forgotten, and is
then a repeat: B); after a graceful stop, at least the last one read.

**F. Nothing dies that must not.** The dead letters of an endpoint of a class that must deliver everything (`hooks_endpoint_dead_letters`, and the
receivers' ledger) are zero at every sample after the first minute, and the dead letters of `gone` are exactly the events the receiver answered `410`.

**G. Memory, descriptors, threads and disk do not grow** (section 4 has the thresholds). Memory and descriptors are judged on incarnations of the
service: a kill resets both, so the regression is run per incarnation of at least 20 minutes, and on the one of the quiet tail in particular. The
disk is judged against what retention allows, computed from what was ingested.

**H. The loop does not stall.** The probe's window maximum stays under the bound (section 4) outside the documented cases (the first seconds after
a start, a `compact-kill-at` freeze, a delay the host shared), and the service's own `maintenance_ms_max` stays under its bound.

**I. Cron.** Per schedule, no scheduled second has two events; every gap longer than 2 s lies inside a time the service or the database was away (plus
5 s), and the first fire after such a gap is the one catch-up event.

**J. The stop is clean.** At the end the service gets `SIGTERM`, exits 0 within the deadline, `scripts/logcheck.py` accepts its data directory, and
a restart then makes **no repeat** of a delivery that was recorded (the receivers' ledger shows no second delivery of an event the service said it had delivered,
beyond the attempts the stop said it left on the wire).

**K. Backups.** Every online backup exits 0 and passes `logcheck`; a restored one passes `logcheck` and holds every event the poster had acknowledged
before the backup began (that retention had not dropped), byte for byte as the harness posted it.

**L. The service stays up.** It never exits except when the harness stopped it, and writes nothing to standard error except the lines `docs/runbook.md`
section 3.1 lists.

### Validating the checker

A checker that cannot fail proves nothing. `scripts/soak/selftest_ledger.py` builds synthetic ledgers of the two kinds and **mutates** them (a delivery
removed, one duplicated outside any kill, one to an endpoint that filters it out, a signature from a revoked secret, one delivery too few after a kill, a
cursor that goes backwards, a dead letter where none may be, a cron second twice, a growing series of RSS, a stall): every mutation must be caught
under its own tag, and the unmutated ledger must pass. It runs in seconds (`soak.py --selftest-ledger`). `soak.py --selftest` then runs the whole
harness on the real service three minutes clean (must pass) and again three minutes with a **fault injected** (must fail, with the tag of the
fault): a receiver that answers 204 and does not record the delivery (`lose`), one that records a delivery twice (`dup`), and a service built to
lie about `fsync` (`liar`: a power cut then takes acknowledged events, which the harness must find).

## 4. Pass and fail

A run **passes** when every item below holds. Any other result is a failure whose tag names the invariant; `verdict.json` lists each violation with up to
twenty examples.

| # | criterion | threshold |
|---|---|---|
| A | an acknowledged event missing at an endpoint that wants it | **0** |
| B | an unexplained repeat | **0** |
| C | a delivery against a filter, or of an event older than the endpoint | **0** |
| D | a delivery with a bad, missing or revoked signature, or one signature where two are due | **0** |
| E | a cursor that goes back, outside the excused case | **0** |
| F | a dead letter at a class that must deliver everything; a `gone` dead letter that is not a `410` | **0** |
| G1 | memory: on the incarnation of the quiet tail, the fitted growth of resident memory over the last two thirds of that incarnation | at most **max(5 % of the resident memory of the first third, 4 MiB)**; and the final resident memory at most 110 % of the median of the first third plus 8 MiB |
| G2 | the same regression on every earlier incarnation of at least 20 minutes (the first 20 % of each is warm-up) | the same |
| G3 | descriptors: the largest count in the last two thirds of an incarnation against the largest in its first third; the fitted growth | at most 16 more; at most 8 over the window. Threads: the same count at every sample of an incarnation |
| G4 | disk: the largest `data_bytes` against the bound `1.5 x (bytes ingested in retention + window + 120 s) + 2 x segment-bytes + 4 x delivery-log-bytes + 4 MiB`; and no growth: the largest in the last half at most 1.3 times the largest of the second quarter | never above the bound |
| H1 | loop: the probe's largest answer time in any one-second window, outside the excused cases | at most **500 ms** (the service's `maintenance_ms_max` at most **250 ms**) |
| H2 | loop: the 99.9th percentile of the probe over the run | at most **50 ms** |
| I | cron: a second twice; a gap not excused | **0** |
| J | the stop: not exit 0, `logcheck` not accepting, a repeat after the restart | **0** |
| K | a backup that fails to verify, a restore that does not hold what it must | **0** |
| L | an exit the harness did not cause; a line on standard error that is not in the list | **0** |
| end | every endpoint of a class that must deliver everything has caught up (its cursor at the newest event) within ten minutes of the end of the posting | yes |

**A run is valid** only if the poster held at least 90 % of the requested rate over the steady phases, the harness used less than 60 % of one core on
average, and the load average of the host did not stay above the number of cores for more than a tenth of the run. A run that is not valid is
reported **inconclusive, with the reason**, and is not counted for or against the service: it is repeated on a quieter machine. A run that is
valid and fails is a finding, with its seed, its `chaos.jsonl` and the data directory of the failing moment (kept).

## 5. What the report says

`report.py` writes `report.md` and `report.json` from the files of a run: the exact method (this page's parameters as run), the seed, the SHA-256 of
the binary, the commit, the machine (CPU model, cores, memory, kernel, whether it was shared: the load average is in the report), the duration, the
counts (events posted, acknowledged, deliveries by class, attempts, repeats and their explanation, kills by kind, database faults by kind, backups,
restores, endpoints created and deleted), the verdict table above with the measured value of each line, the percentiles of ingest latency and of delivery
latency, the series of memory, descriptors and disk (minimum, median, maximum, fitted slope, per incarnation), the probe's distribution and its worst
windows, and the harness's own CPU.

## 6. Resuming

The data directory, the two ledgers and `state.json` persist in `--out`. `soak.py --resume DIR` reads the parameters from `DIR/run.json` (the seed, the
rate, the endpoints, the ports and secrets) and then, in this order: stops a service of the previous harness still running (found by the pid in
`state.json`, and only if its command line is this binary on this data directory), treats the stop as a power cut (**a kill at the time of the last
heartbeat**, so repeats just after it are excused), starts the service on the same data directory, brings the receivers back on the same ports, reads the
ledger tail again to rebuild the open window, and runs for the time that was left. A resumed run says so in its report, and the report counts the
resume as a kill. The poster continues the numbering of `n`; the chaos generator is fast-forwarded by the number of actions already taken, so the sequence
is the one the seed gives.

## 7. What this test does not cover

Stated, so that a green run is read for what it is: one service on one host (not a fleet, not a real network: loopback never drops a packet or
delays a segment); a PostgreSQL that is stopped for real only if `--pg-restart-cmd` is given; a disk that is full only with `--tmpfs-data`; a clock
that does not jump (receivers that answer slowly are tested, a wall clock stepped by NTP is not); no hostile receivers (an oversized response, a
slow-loris, a redirect: `tests/attempt_test.py` has those); changes of an endpoint's `types` while events flow (the matching rules are
`tests/filter_test.py`'s); receivers in Python, so the rate the harness can sustain is below what the service can (the harness measures its own
share, and the capacity page uses C tools for the capacity figures); OpenSSL in the process is exercised by the `https` endpoint and by nothing
else. A green 24 hours is evidence about these faults at this rate, not a proof about the others.

## 8. Results

**Not yet run.** The harness is built and validated (its mutants are caught; a shakedown run of the build at the time is in
`docs/capacity.md`'s record of runs); the 24-hour run is pending. Its report will be published here, next to the commit and the binary it ran on. The
cells below are filled in by that run and by nothing else.

| | |
|---|---|
| binary, commit, seed | to be measured by the long run |
| duration, posted, delivered, repeats | to be measured by the long run |
| kills, stops, database faults | to be measured by the long run |
| criteria A to L | to be measured by the long run |
| resident memory (quiet tail): start, end, slope | to be measured by the long run |
| descriptors, disk (peak, bound) | to be measured by the long run |
| loop: probe maximum, 99.9th percentile | to be measured by the long run |

## Changes to the criteria

None yet.
