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
| `schedule` | 500, 1000, 2000, 4000, 8000, 16000, 16000, 16000 ms | an event dies after about 64 s of failure: short enough that dead letters happen in the test, and long enough that the chaos an endpoint is put through (below: an outage of up to 10 s, on top of a receiver that fails an attempt of its own, on top of a restart of the service) cannot add up to a death unless a death is the point |
| `deadline-ms` | 2000 | the default |
| `allow-private-hosts` | 1 | the receivers are on `127.0.0.1` |

The service is pinned to one core with `taskset` and the harness to the others (`--service-cpus`, `--harness-cpus`; the default on a machine with four or
more cores is the last core for the service and the rest for the harness). **The harness's own CPU is measured and reported** (the processes that make
the load and keep the ledger, from `/proc`), so that a capacity figure is never one that the harness polluted, and a run in which the harness could not
hold the rate it was asked for is marked as such rather than passed.

### The workload

* **Events.** A steady stream at `--rate` events a second (40 by default, a fifth of the 200 a second this endpoint mix sustains with no fault, `docs/capacity.md`; lower than the 30 % the plan
  first named, because every restart and every outage leaves a backlog to work off, section 8), with a burst phase at `--burst-rate` (3 times the rate by default) for a minute about every twenty minutes. Seven event types in fixed proportions (`user.created` 30 %, `user.updated` 25 %, `order.paid` 15 %, `order.refunded` 6 %, `invoice.paid` 10 %,
  `ping` 12 %, `rare.event` 2 %), bodies from 150 bytes to 50 KB (a long tail: 80 % under 400 bytes), nine in ten posted under an `Idempotency-Key`, and
  every event numbered by the poster (`n`) so that it can be recognised wherever it turns up.
* **Idempotency.** Three in a hundred keyed events are posted again at once with the same key (the answer must be the same id), and one in a hundred
  again with the same key and a *different* body (must be a `422`). A post that failed because the service was killed is retried with its key, so a
  kill never makes two events from one `n`.
* **Endpoints** (a mix, `--endpoints N`, default 12; N plus the `--churn` threads, default 3, at most `--endpoint-limit`, default 62 as in the build the test was designed on: the limit is a parameter, so a build that takes more is tested with more):

  | class | what the receiver does | what it subscribes to | what must happen |
  |---|---|---|---|
  | `oracle` | answers 204 at once | every type | every event, once |
  | `healthy` | 204 at once | every type | every event, once |
  | `filter` | 204 | `user.*`, `invoice.paid` | exactly those, once; nothing else, ever |
  | `slow` | answers after 50 to 400 ms | `order.*`, `ping` | every event wanted, once |
  | `flapping` | for 3 to 8 seconds in every 30 refuses connections, resets them, or closes without answering | `user.updated`, `order.*` | no event may die (the down time is shorter than the retry horizon): every event wanted, once |
  | `http5xx` | answers 500, 502 or 503 to the first attempt of one event in two | `order.paid`, `ping`, `invoice.paid` | every event wanted, eventually, with its failed attempts counted |
  | `gone` | answers `410` to the events of a fixed one per cent (a function of the seed and the event) and 204 to the others | `invoice.*`, `ping` | the one per cent are dead letters and never delivered; the rest once; the harness enables the endpoint again after each `410` |
  | `dead` | nothing listens on its port, for a window of 90 s about every fifteen minutes (the same cycle as `sick`, with a refused connection for the failure) | `rare.event` | what is acknowledged in the window dies; after it `replay-dead` brings it back: at the end every event wanted, delivered. (A port closed for good was tried and is not in the mix: the endpoint falls behind its 1,024-id window at once and the log cannot be bounded, section 8) |
  | `https` | TLS with a certificate from a throwaway CA the service is given (`tls-ca-file`), a name that resolves through a name server of the harness's | `user.*` | as `healthy` |
  | `rate` | 204 | `user.created` | an endpoint with `"rate"` set to two and a half times its steady share of the stream, which a burst exceeds: held back, never failed; every event wanted, once |
  | `sick` | in a window of 90 s about every fifteen minutes answers 503 to everything | `ping`, `order.paid` | events that fail for the whole of the schedule die; after the window the harness runs `replay-dead` until nothing remains: **at the end, every event wanted, delivered** |

* **Cron.** Two schedules (every second, and every fifth second) make `cron.tick` events; every endpoint that subscribes to everything receives them.
* **Replays.** Every few seconds the harness replays a recent event to every endpoint that wants it, or to one endpoint (whatever it subscribes to); a
  replay is cancelled again some of the time; the dead letters of `dead` are replayed in bulk and cancelled, those of `sick` are replayed in bulk until
  they are gone.
* **Endpoint churn.** Three threads in turn create an endpoint (a new receiver, a secret of the harness's choosing), `PATCH` it (`concurrency`, `rate`,
  a custom header, the secret with and without an overlap), let it run, wait until its cursor has reached the newest event, and `DELETE` it.
* **Secret rotation** of the long-lived endpoints, with an overlap (`keep_old_ms`: every delivery must carry two signatures while it lasts) and without
  (the old secret must stop signing at the next attempt).
* **Backups.** Every ten minutes `scripts/backup.sh --mode online` (never while the harness is killing the service: that is `tests/backup_test.py`'s); every fourth is restored into a scratch
  directory with `scripts/restore.sh` and checked, and once more at the end.

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
| `bursting`, `acked`, `phase` | whether a burst is on, events acknowledged so far, and 0 the run / 1 the drain at the end / 2 the service started once more after the clean stop (not judged) |

**The loop probe** is a separate process that asks `GET /healthz` ten times a second and, immediately after each, makes the same kind of request to a
responder of its own. The first measures the service's loop (the service has one thread and answers `/healthz` from it, so a held loop is a late
answer); the second measures the host. A stall is a window in which the service was late *and the host was not*: on a shared machine other work is part of the weather, and a delay the control request shared is counted as the host's
and reported as such, not as the service's. The responder of the control request runs on the core the service is pinned to (the harness's cores are not the service's), so that work that competes with the
service for its core is seen by both.

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
A repeat is **explained** when (1) the delivery before it was made at most 2 s before a kill, a power cut or a forced stop of the service, or up
to a second after it (the outcome is written after the receiver answers, and a flush later: what a power cut can take is the deliveries of the last turns; 2 s is far
more than a turn; and a request that was in the kernel's buffers when the service died is read by the receiver a moment later), (2) the harness asked for
a replay of that event to that endpoint (up to one repeat for each replay accepted; an endpoint created while a replay waits is sent it too and that is not a repeat),
or (3) the receiver took half a second or more to answer, so that the service's own deadline may have passed. Anything else is a violation, and the report counts
the repeats **per kill**: the number, the largest. A stop with `SIGTERM` explains only as many repeats as the service said were on the wire. A repeat that nothing
explains and that comes after a restart of the service, of a delivery made before it, is tagged `B_restart_repeat`, because it is one finding with one cause (section 8).

**C. Filters hold.** No delivery of an event whose type the endpoint did not subscribe to at the time (the receiver records the type of the body it
got; the harness compares it with the endpoint's list, which it keeps), and no delivery of an event older than the endpoint, other than a replay the
harness asked for.

**D. Signatures hold, and are what they should be.** Every delivery carries a `webhook-id` that matches the event, a timestamp within five minutes of the
receiver's clock, and a Standard Webhooks signature that the receiver checks itself (HMAC-SHA256 over `id.timestamp.body`, by the secret of the endpoint
at that time). While a rotation overlaps (more than 3 s after the change was answered and more than 3 s before it ends) the header must carry **two**
signatures, one of each secret; after an overlap, or a rotation without one, a delivery signed only by the old secret more than 3 s after the change
is a violation.

**E. Cursors never go backwards.** Sampled every second from `GET /endpoints`: within one incarnation, strictly. After a restart the service re-reads the events above the
cursor it recorded and works its cursor up again (section 8: at about 50 events a second), so the first cursors it reports are below the ones before the stop; the
criterion is that each endpoint's cursor is back at what it was within 120 s (at what it was 2 s before a kill, or at the last one read before a graceful stop), and the report
gives the time it took.

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
| B | an unexplained repeat (`B_repeat`, and `B_restart_repeat` after a restart) | **0** |
| C | a delivery against a filter, or of an event older than the endpoint | **0** |
| D | a delivery with a bad, missing or revoked signature, or one signature where two are due | **0** |
| E | a cursor that goes back within an incarnation; one that is not back at what it was 120 s after a restart | **0** |
| F | a dead letter at a class that must deliver everything; a `gone` dead letter that is not a `410` | **0** |
| G1 | memory: on the incarnation of the quiet tail, the fitted growth of resident memory over the last two thirds of that incarnation | at most **max(5 % of the resident memory of the first third, 4 MiB)**; and the final resident memory at most 110 % of the median of the first third plus 8 MiB |
| G2 | the same regression on every earlier incarnation of at least 20 minutes (the first 20 % of each is warm-up) | the same |
| G3 | descriptors: the largest count in the last two thirds of an incarnation against the largest in its first third; the fitted growth | at most 16 more; at most 8 over the window. Threads: the same count at every sample of an incarnation |
| G4 | disk: the largest `data_bytes` against the bound `1.5 x (bytes ingested in retention + window + 120 s) + 2 x segment-bytes + 4 x delivery-log-bytes + 4 MiB`; and no growth: the largest in the last half at most 1.3 times the largest of the second quarter | never above the bound |
| H1 | loop: the probe's largest answer time in any one-second window, outside the excused cases (the seconds around a kill, a stop or a start; the freeze at a numbered step of a compaction; a `replay-dead` call, which can read `delivery.seg`; a window the control request was late in too) | at most **500 ms** (the service's `maintenance_ms_max` at most **250 ms**) |
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
`tests/filter_test.py`'s); receivers in Python, so the rate the harness can sustain (about 200 events a second for this mix, `docs/capacity.md`) is below what
the service can (the harness measures its own share, and the capacity page uses C tools for the capacity figures); OpenSSL in the process is exercised by the
`https` endpoint and by nothing else; **faults do not overlap** except as the schedule happens to place them: a restart, a database fault and a disk fault never run
together (one lock), and an endpoint is not made to fail again within 45 s of the last time, because the retry schedule is meant to carry one outage and not four
(in a run compressed to 25 minutes four 5 s outages in 65 s killed an event that has nine attempts: a finding about the schedule and not about the service, and not
a thing a real day does often). A green 24 hours is evidence about these faults at this rate, not a proof about the others.

## 8. Results

### The 24-hour run

**Not yet run.** The harness is built and has been validated (below); the 24-hour run is pending and will be started on a machine of its own, with
`python3 scripts/soak/soak.py --binary build/hooks --hours 24 --seed 1 --out soak-out` (four cores, about 3 GB of disk for `soak-out`, 1 GB of memory for the harness; the service needs
one core and, today, memory that grows: see finding 3). Its report will be published here, next to the commit and the binary it ran on. The cells are filled in by that run
and by nothing else.

| | |
|---|---|
| binary, commit, seed | to be measured by the long run |
| duration, posted, delivered, repeats | to be measured by the long run |
| kills, stops, database faults | to be measured by the long run |
| criteria A to L | to be measured by the long run |
| resident memory (quiet tail): start, end, slope | to be measured by the long run |
| descriptors, disk (peak, bound) | to be measured by the long run |
| loop: probe maximum, 99.9th percentile | to be measured by the long run |

### The harness was tested (and what it was tested on)

* **The checker against mutated ledgers**: `soak.py --selftest-ledger`, 24 mutations of a synthetic run of a correct service (a delivery lost, a repeat outside a kill, a repeat after a
  restart, a delivery against a filter, a signature, a timestamp, a body, one signature while two are due, a cursor that goes back or ahead, a dead letter where none may be, a scheduled second twice,
  a gap in a schedule, an event nobody posted, an acknowledged event that no ledger knows, a dead letter that is never replayed, an endpoint that has not caught up, one id for two events) and the series (a leak, a step, descriptors
  that grow, a second thread, a disk that grows, a long step of the loop, a stall, a stall the host shared, failed probes, a slow 99.9th percentile): all caught under their tags, and the unmutated run passes. It runs
  in 3 s and is a step of `ci.yml`.
* **The harness on the real service** (`soak.py --selftest --selftest-mutants all`, 180 s each, the build of commit `45faec0`): the clean run passes (with the finding 1 below waived and said so); a receiver that
  answers `204` and does not write the delivery down (`lose`) is caught as `A_missing` in 63 s; one that writes a delivery down twice (`dup`) as `B_repeat` in 63 s; a service run under a shim whose `fsync` lies (`liar`: three
  flushes in four are not made durable, and a power cut follows each kill) is caught as `A_not_in_log` (the service no longer starts on its own data directory, and the events it had acknowledged are not in it).
* **A shakedown of 25 minutes** (`--duration-s 1500 --seed 21`, rate 40 and burst 120 events a second, 12 endpoints and 3 churn threads, every fault, the last quarter without a restart), on a shared 4-core machine
  (load average 1 to 3), service on core 3, harness on the others, the harness at 19 % of a core in the median. 84,431 events acknowledged, 654,061 deliveries recorded by the receivers; 18 kills (5 of them at a numbered
  step of a compaction, which the service reached), 2 `SIGTERM` stops, 52 database faults (blackhole 9, cut 15, freeze 8, hold 9, backends ended 11), 14 online backups and 5 restores (all verified), 128 endpoints created, changed and deleted, 254
  secret rotations, 448 replays, 111 bulk replays of dead letters. **Every criterion passed except two**: finding 1 (waived in the run, 6,412 repeats after restarts) and **G1, the growth of memory** (finding 3: the final
  incarnation went from 20.9 to 99.4 MB in 560 s). The loop probe: p50 0.30 ms, p99 4.4 ms, p99.9 15.2 ms, largest 70.6 ms; the service's own longest step of the loop 14 ms; the data directory between 0 and 28.9 MB (median 15.8) against a retention of three
  minutes; descriptors 10 to 73, one thread throughout; cursors back at what they were within 1 s of a restart in all 13 cases measured. Repeats per kill that the kill explains: 1,676 in all, the most after one kill 734 (deliveries of the last two seconds before it,
  at several hundred deliveries a second).
* **A second shakedown of 15 minutes without cron and without endpoint churn** (`--schedules 0 --churn 0 --seed 22`): **verdict PASS** with finding 1 waived (793 repeats after restarts, and the one after the clean stop at the end). 68,328 events, 377,247 deliveries, 18 kills (7 at a step of a
  compaction), 3 stops, 27 database faults; the final incarnation lived 350 s with resident memory at 17.6 to 37.8 MB (fitted growth 1.9 MB over the window against a limit of 4.1) and 10 to 55 descriptors; the probe's largest 48.8 ms; the service's own longest step of the loop 24 ms; the data directory 0 to 34.3 MB, at most 56 % of the bound; cursors back within 1 s (median) and 13 s (the most) of a restart, 30 cases.
  This is the run that shows finding 3 is in the database paths and not in delivery.

### What the harness found in the service

The service is the build of `origin/main` at `45faec0`. None of these was fixed here; each has a reproducer in `scripts/soak/`, a seed and the evidence in the run directory it was found in.

1. **A restart makes the service deliver, again, events it had recorded as delivered** (`scripts/soak/repro_restart_repeat.py`, exit status 1 on this build). One endpoint with a list of event types (so that half of the events are passed
   over for it and leave no record), whose cursor is held behind one event that its receiver refuses, while the events after it are delivered window by window: when the receiver lets the first event through, all 1,500 events are delivered once and the
   cursor goes to 3,000. After a `SIGTERM` stop (exit 0, nothing on the wire) and a start, the cursor is re-read from the beginning, 41, 91, 143 ... (about 50 ids a second), and when it reaches
   the ids that its window of 1,024 did not cover when the records of their delivery were read, **987 of the 1,500 events are delivered a second time**, about 30 s after the start. The soak sees it on endpoints with a list of event types that were behind their cursor, after every kind of restart (`SIGTERM`, `kill -9`), thousands of deliveries repeated after one restart; it reports them as `B_restart_repeat` and
   they are what `J_repeat` finds after the clean stop of the end. At-least-once is the contract, so this is not a loss; it is a delivery storm after a restart, and it is also why the soak waives `B_restart_repeat` in its self-test. **Whose fault: the service's.**
2. **With the data directory full, the service delivers the same events again and again, as fast as the receivers answer** (`scripts/soak/repro_full_disk.py`, a tmpfs of 8 MB, needs root or `sudo -n`). The disk is filled for 4 s with 218 events in the log: the receiver is sent
   17,788 deliveries, one event 2,211 times. `/readyz` says `events_log` is broken, which is right, and `POST /events` is a `503`, which is right; but the outcome of an attempt cannot be written, so the attempt never counts, and the retry is not spaced by the schedule.
   In the soak (`--tmpfs-data`) the first such fault was 19,255 repeats in five seconds. **Whose fault: the service's.**
3. **Resident memory grows with every call that goes to the database for a change** (`scripts/soak/repro_memory_growth.py`, exit status 1): a `PATCH` of an endpoint 18 to 55 KB a call, the creation and deletion of one 27 KB, a bulk replay of dead letters 9 KB, and **a cron fire 13 to 15 KB** on an idle
   service (and, in the soak, roughly 70 KB a fire while events flow). The reads, a replay, an enable, delivery to 12 endpoints over TLS with names, retries, a list of types, big events, idempotency keys, and a history row for each of 260,000 deliveries leave it flat (10.1 MB) in
   the same harness. At the soak's two schedules (1.2 fires a second) the service takes 5 MB a minute and does not stop (a 13-minute run, no fault, no churn: 9.5 to 85.5 MB, the line straight from 3 minutes to the end); without the schedules and without churn the 15-minute run above is flat. A schedule of every second takes from 1 GB (idle) to about 6 GB (under load) a day. G1 is the criterion that catches it; the 24-hour run should be made with `--schedules 0 --churn 0` until it is mended, and its report will say so. **Whose fault: the service's, in the path of the database round trips that change something** (the history rows, which are inserts, do not do it).
4. **A restart works its cursors up slowly when they are far behind** (finding 1's reproducer prints it): after the start `GET /endpoints` reports 41, 91, 143 ... (about 50 ids a second for each endpoint) where it said 3,000 before the stop, until the ids that were delivered before are reached. In the soak, with cursors that were close
   behind (a few hundred ids at 40 events a second), they were back within 1 s (median) and 13 s (the most) of 43 cases; with a backlog of thousands of ids it takes minutes, and the deliveries it repeats are finding 1. This is why the soak's rate is 40 and why `docs/capacity.md` says what a core carries and what a restart costs. **Whose fault: the service's** (a design choice that costs capacity after a restart).
5. **The runbook's list of what the service writes to standard error is short** (`docs/runbook.md` 3.1: "Nothing else is logged while it runs"): the service says a line for every roll of the events log, every drop of a segment and every replacement of the outcomes log by a snapshot (`hooks: events log: sealed a segment ...`, `dropped a segment of N events`, `delivery.seg: replaced by a snapshot`,
   `removed ...`), many a minute with the soak's small segments, a few a day at the defaults. The harness's list includes them (invariant L). **Whose fault: the page's.**
6. **An observation, not a finding**: a replay of an event to all the endpoints that want it, asked for while one of them holds it up, is also sent to an endpoint created before it ends (the soak saw an old event delivered to a new endpoint 24 s after it was made, and accepts it for a replay that was waiting). And the 1,024-id window means that a receiver that fails for the whole
   retry schedule pins the log and falls behind without limit once `rate x schedule` is above 1,024 ids (150 events a second and a 24-hour schedule: at once): the harness first had an endpoint on a closed port for good and could not bound the data directory with it, which is why `dead` is a cycle like `sick`.

### What the harness found in itself, and the criteria that changed because of it

The first runs of the harness failed for causes in the harness (counted wrongly, a lock kept too long, a rotation that dropped a secret too soon, endpoints left behind by a create that was not answered). They are corrected, and the criteria that changed with them are listed next.

## Changes to the criteria

Written after the shakedowns, and the reason for each:

| change | from | to | why |
|---|---|---|---|
| retry schedule | 500 ... 16000 ms (6 delays, about 31 s) | 8 delays, about 64 s | an outage of 10 s, a receiver that fails the first attempt of its own and a restart add up to more than 31 s; at 64 s they do not unless four outages hit one endpoint |
| `http5xx` | fails the first 0 to 3 attempts | fails the first attempt of one event in two | same |
| `dead` and `sick` | an endpoint on a closed port for good; a 60 s window | a window of 90 s about every fifteen minutes, longer than the schedule so that events die in it | finding 6 (the log cannot be bounded with a receiver that is down for good) |
| E | a cursor is at least what it was after any restart | back at what it was within 120 s of a restart; strictly monotonic within an incarnation | finding 4 (the service works its cursors up again after a start, at about 50 ids a second when they are far behind) |
| B | one tag | `B_repeat` and `B_restart_repeat` | finding 1 has one cause |
| rate | 150 events a second | 40 (burst 120), a fifth of what the mix sustains | every restart and outage leaves a backlog to work off (finding 4); `capacity.md` has what a core carries |
| outages | any endpoint at any time | an endpoint is not made to fail again within 45 s | stacking, section 7 |
| H | every window | not the seconds of a `replay-dead` call | reading the dead letters beyond the 2,048 of the table is a read of `delivery.seg` and a documented pause of the loop (`docs/runbook.md` 3.6) |
| G | the last incarnation | the last incarnation before the clean stop at the end | the incarnation that is started to watch the restart lives seconds |
| the `.synced` files of the shim | counted in the data directory | taken away when their file is gone, and not counted | the shim leaves one for every segment it has seen |
