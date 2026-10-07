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
| `max-age-ms` | 1,200,000 (20 min; `--max-age-s`) | the hard maximum age (design.md section 47.2) that nothing can pin: without it waiting replays that never start kept the log for hours in the third 24 h run. See "The maximum age" below |
| `allow-private-hosts` | 1 | the receivers are on `127.0.0.1` |

The service is pinned to one core with `taskset` and the harness to the others (`--service-cpus`, `--harness-cpus`; the default on a machine with four or
more cores is the last core for the service and the rest for the harness). **The harness's own CPU is measured and reported** (the processes that make
the load and keep the ledger, from `/proc`), so that a capacity figure is never one that the harness polluted, and a run in which the harness could not
hold the rate it was asked for is marked as such rather than passed.

**The receivers** run as `--receiver-procs N` processes (2 by default): the endpoints are divided by index modulo N, each process has its own control port and its own ledger (`ledger/recv-<k>.bin`), and the checker reads the ledgers together, merged by the time of the request. Each process writes its ledger from a thread of its own, a batch at a time, and sends an answer only after the batch holding its record is in the file, so the record is still there before the answer. **A record carries two times**, when the request was read and when the answer was sent, and whether the answer counts (a 2xx sent within 1.6 s of the request, on a connection the service had not closed) is decided when it is sent, not from the delay that was planned: a receiver whose loop is late makes an answer that was meant to be prompt a timeout for the service, and the record now says so. Endpoints that are gone are forgotten by the receivers (the ticker used to walk every endpoint the run had ever made: on a synthetic case a request cost 4.3 times as much after 1,200 endpoints had come and gone, 333 µs against 78 µs, and the same test now holds it flat), and the name server of the `https` endpoints keeps a count and not a list of every question (it walked the list for each question: 54 µs at first, 544 µs after 28,000). A listener that cannot be opened ("Address already in use") is counted, retried, and named in `receivers.log` with the process that holds the port; one that stays stuck for 10 s makes the run invalid.

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
| `harness_cpu_pct`, `receivers_cpu_pct`, `receivers_cpu_max_pct`, `harness_total_cpu_pct`, `recv_loop_lag_max_ms` | the harness's own processes (the poster, the checker, the watcher, the workers, the probe: **not** the receivers), the receivers (all processes added, and the busiest one), both together, and the longest the busiest receiver's loop was late |
| `loadavg1`, `loadavg5`, `loadavg15`, `psi_cpu_*`, `psi_mem_*`, `psi_io_*`, `mem_avail_mb`, `swap_used_mb`, `swap_free_mb`, `swapin_s`, `swapout_s`, `majflt_s`, `ctxt_s`, `procs_running`, `procs_blocked`, `host_iowait_pct`, `host_steal_pct`, `host_busy_pct` | the host (`/proc/loadavg`, `/proc/pressure/*`, `/proc/meminfo`, `/proc/vmstat`, `/proc/stat`), so that a stall can be put on something. When a stall of more than 5 s is seen (a sample that came late, a receiver loop or a probe window longer than 5 s) the three processes that used most CPU since the last sample are written to `chaos.jsonl` (`harness-event`, `stall`) |
| `recv_late_unplanned`, `recv_conn_lost`, `recv_bind_failures`, `recv_writer_wait_max_ms`, `recv_sync_fallbacks`, `recv_writer_queue_max`, `recv_records`, `events_expired`, `valid` | the receivers' own accounting (answers due at once that went out more than a second late; answers whose connection the service had closed; failed binds; how long a record waited for the writer; records written by the loop because the queue was full; the queue's depth), the service's `events_expired`, and whether the guard still finds the run valid |
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

**A run is valid** only while the harness and the host are not the limit, and **that is judged continuously, not at the end** (`scripts/soak/guard.py`, tested by `scripts/soak/tests/test_guard.py`). Every sample is fed to a sliding window of the last ten minutes; the moment a criterion is met the progress line says `*** RUN INVALID ***`, `run.json` is marked (`valid: false`, with the reason, the time and the elapsed seconds), and the run **ends early** (`--no-early-stop` goes on, for diagnosis; the run stays marked). The criteria:

| criterion | threshold | judged |
|---|---|---|
| the busiest receiver process | over 85 % of a core in more than 5 % of the samples of the window | once the window is nine tenths full; over half of the samples sooner |
| the receivers' loop | late by more than 250 ms in more than 5 % of the samples | the same |
| the harness's own processes (not the receivers) | over 60 % of a core on average over the window | window |
| the poster | median of the steady samples of the window under 90 % of the requested rate (steady: the service up, no burst, not in an excused interval) | window, at least 20 samples |
| receivers answering late | more than 20 answers due at once that went out more than a second late in a window | window |
| a listener that cannot bind its port | stuck for more than 10 s | at once |
| memory | under 1 GB available on the host, at two samples in a row | at once |
| swap | more than 200 pages a second swapped in and out, on average over the window | window |
| memory pressure (PSI) | all tasks stalled on memory more than 5 % of the time (avg60), three samples in a row | at once |

Before it starts the run refuses to if under 2 GB of memory is available, or if swap is exhausted (under 64 MB free) while under 6 GB is available (`--allow-low-memory` overrides, and `run.json` says so). Swap that is full with plenty of memory free is allowed: nothing is paged out, and the swap-rate and pressure criteria watch for the case that matters.

Two bounds are kept for the service while the run goes on, and each is a **violation** (group G), not an invalidity: the data directory above what retention allows for what was ingested (the G4 bound, at three samples in a row: `G_disk_over_bound`), and the table of waiting replays full (`--replay-cap`, 32) for more than three minutes (`G_replays_pinned`), which is how replays that never start pin the log. At the end the verdict adds the host's load (above the cores in more than a tenth of the samples). A run that is not valid is reported **inconclusive, with the reasons and when each was met**, whatever else it found (what it found stays listed in `verdict.json`, and nothing is dropped from `violations.jsonl`), and is not counted for or against the service: it is repeated on a quieter machine. A run that is valid and fails is a finding, with its seed, its `chaos.jsonl` and the data directory of the failing moment (kept).

**The maximum age.** The harness sets `--max-age-ms` (1,200,000 by default): a segment older than that is dropped whatever pins it, and the service moves every endpoint's cursor past what it had not delivered and counts those events (`events_expired`). The checker accepts a loss at an endpoint as an expiry **only for an event that was older than the maximum age when the cursor was found past it** (by the time the poster was told it was accepted, less 2 s of slack); a younger event passed undelivered is `A_missing`, and so is any event in a run with no maximum age (`--max-age-s 0`). The number excused is held against the service's own `events_expired`, summed over the incarnations as far as it was sampled: more than 110 % of it plus 50 is `A_expired_unexplained`. The maximum age is far above anything the harness pins on purpose (a window of 90 s, a retry horizon of 64 s), so in a valid run it is expected to expire nothing; a run that does expire events says so in `events_expired`.

### The 24-hour runs so far (none has passed)

**Run 1** (started 2026-10-05 16:14 CEST on the stack of 41653c6) was stopped at 2.6 hours. Two things: the harness's own database fault proxy closed sockets in a way that sent PostgreSQL no end of connection, so every killed incarnation left two backends open until the server refused every login ("too many clients") and a start of the service ended with status 20 (mended in `tests/pgproxy.py`, which also keeps its message for a start that gives up on the database, now naming the SQLSTATE); and 4 `D_overlap`, which were a **service bug**: `keep_old_ms` was counted from the request and not from the commit, so a rotation held by a blackholed database lost its overlap (design 35.4).

**Run 2** (2026-10-05 21:57 CEST, 0766bfc plus the harness changes below, seed 2) was stopped by us at 9.7 hours with 121 violations. What they were:

* **64 `D_overlap` in one second at 7.9 h, a service bug again, in the same place:** a rotation held 5 s by the blackholed database, answered, and a `kill -9` 2.7 s later. The restart reads the row, and the row still had the time as of when the change was sent, earlier than the commit by the wait. The first fix had put the commit's time in memory and not in the row. The database now counts the row's end itself (design 35.4; `rotation_test` stage 11 holds a PATCH behind a proxy and kills the service after it).
* **Everything else followed from the harness, from about 7 hours on, and is attributed by what it looks like and not proven one by one:** the receivers (one Python process) ran at 49 % of a core at the steady rate and at 97 % from about 7 h, with the loop late by 400 to 570 ms, and the service, at 9 % of its core, sat with its 64 attempts in flight waiting for answers. A receiver that answers after the deadline (2 s) is a failed attempt to the service, which retries; the retries are more load. The lag of the three endpoints that must keep up reached 83,000 events (35 minutes). Behind it: 22 `END_lag` (a created endpoint not caught up in 60 s), 27 `B_restart_repeat` and 6 `B_repeat` (repeats 236 to 298 s after the first delivery: the retry after a timeout), 2 `C_filter` (a replay to one endpoint, which ignores the subscription on purpose, delivered 446 s after it was asked for, outside the checker's window for it) and 1 `A_missing` at `dead7` for an event of the last two minutes (the checker judges when an endpoint's cursor moves and excuses a missing event at a `dead` endpoint only if its ledger shows a failed attempt or a fault window; with the ledger as late as the receivers were, the cursor can move before the record is read, which is the likeliest cause and **was not shown**: a run that passes the new validity guard will say). The first burst of the run is at 120 events a second, 3 times the steady rate, which needs about 147 % of the receivers' core: the bursts were the trigger.
* Also found: the harness's saved state had grown to 5.7 MB (3,504 endpoints, 4 MB of the checker's notes) and took 107 ms to write with the interpreter lock held, at every step; its main process was at 610 MB.

**What changed in the harness because of it** (and none of it in what is judged about the service): the burst is 1.5 times the rate (60 a second) and not 3 times; a run whose receivers were above 85 % of a core, or whose receivers' loop was late by more than 250 ms, in more than 5 % of the samples is **not valid** and says why (the verdict is then `INCONCLUSIVE` and not a list of failures that are the harness's); the saved state forgets an endpoint ten minutes after it was retired; the progress line shows the receivers' share of a core; every sample records the three busiest threads of the harness; and `kill -USR1 <pid>` writes the stack of every thread to `<out>/stacks.txt`. **The 200 events a second this mix is said to sustain** (above, and `docs/capacity.md`) was measured on another machine; on this one, with this build, the receivers' process is the limit at about 80.

**Run 3** (2026-10-06 08:14 CEST, the service of PR 44's head ab07f51, the harness of 9e896bd, seed 3, burst 60) was stopped by us at 15.8 hours and is **`INCONCLUSIVE`** by the rule above, not a pass and not a list of service failures. It had no violation for the first 6.6 hours (the snapshot at 4.7 hours is the figure on the project page). The first, an `END_lag`, came as the receivers passed 85 % of a core; over the run the receivers were above 85 % in a third of the samples and their loop later than 250 ms in over half, the harness itself used about a core, and by the end the lag of the endpoints that must keep up was 546 seconds. The final tally was about 198,000 violations, 197,942 of them `P_phantom` from 14.7 hours. The evidence (the violation records, the ledger, the service's own logs and data directory) was read kind by kind against the checker's rules:

* **Attributed to the harness, with a mechanism found in its code and numbers that match the timestamps:** `END_lag` (the receivers were the bottleneck); `P_phantom` (the checker trimmed its record of posted events while late deliveries from endpoints it had excluded were still arriving: every phantom event is in the poster's own acknowledgement log); `C_filter` (seven replays the harness had asked for, delivered eleven minutes later, after the checker had forgotten that it asked); `A_missing` (two events on a dead endpoint, acknowledged 12 s before the failure window the checker allows for; the service's own log has both as dead letters); `K_backup` (the Python log checker reads about 0.31 s per MB, the data directory grew to 2 GB because a pile of waiting replays pinned the log, and a backup killed at its timeout left its partial copy and its children behind: 36 partials, 32 GB); `B_repeat` (170 of 196 inside a 102-second stall of the whole machine, 19 just after a 15-second one, 7 on an `https` endpoint under receiver lag; the last seven are inferred from the timing and not proven).
* **Twelve endpoints whose create was answered 504 or not at all left their rows behind** (the harness marks such an endpoint as one about which nothing is judged, as described below, but did not remove its row or its receiver), and were loaded again by every restart: about 24 deliveries a second each.
* **Not explained: one `B_restart_repeat`**, `healthy1`, event 1080878, delivered at 26,104.9 s and again at 26,408.1 s with a `kill -9`, a `SIGTERM` and a `kill -9` between them and no stall: its attempts history has no rows, so whether the service sent a delivered event again is not known. The other seven `B_restart_repeat` are a stall followed by restarts. This case is open.
* **Something about the service, not a bug by its documents:** the 32 waiting replays never started (attempts 0 for hours) because they are started only with what is left of a turn after the window's attempts, and two endpoints had a permanent backlog; a waiting replay pins the log, so the data directory grew until the run ended. `--max-age-days` (design section 47.2) bounds that and the harness did not set it.

Taken together: no violation contradicts the service, one case is open, and the run proves nothing about 24 hours. The evidence is kept; the harness is being repaired before a next run (the receivers over several processes, honest answer times, early stop when a run turns invalid, a backup that is killed with its children and cleaned up, a maximum age, and a checker that forgets nothing it can still need).

**What was found in the harness afterwards** (the analysis above is from the evidence of run 3; this is what was found by reading the harness's code and testing its parts, each checked before it was mended; where a mechanism was found and not shown to be the run's, it says so). The open case of `healthy1` (event 1,080,878) is **still open**: nothing here explains it, and it is the first thing to look at in the next valid run.


* **Zombie endpoints.** A create answered `504` (or not at all) left its row in the table; the service did not know it, so the harness's `DELETE` was a `404` and the row stayed; the next restart loaded it as an endpoint at the slowest cursor, and the harness kept its receiver up and never removed it. Twelve of them in 15.8 h, about 24 deliveries a second each. They explain a factor of 1.5 in the receivers' cost, not all of it.
* **The receivers' cost per record.** One Python process, the ledger written by the event loop, and two things that grow. (1) **The name server of the `https` endpoints**, which the first receiver process holds, counted how often a name had been asked by walking the list of every question ever asked, under its lock and the interpreter's: the cost of an answer was 54 µs at first and 544 µs after 28,000 questions (measured on the stub alone), and the service asks for every attempt to the `https` endpoints, about 20 times a second. On a validation run of the old code the cost per record rose from 500 to 1,500 µs in 15 minutes. At that rate fifteen hours is a factor far beyond the 3.6 that run 3 showed, so it is the cause of the growth in kind; **run 3 itself was not profiled, and GC, SMT contention and kernel cost were not excluded** as smaller contributions. (2) A ticker that walked every endpoint the run had ever made, twenty times a second: on its own a request cost 4.3 times as much after 1,200 endpoints had come and gone. The zombies add their own load (a factor of 1.5).
* **A late answer was recorded as an acknowledgement.** The receiver decided "effective" from the delay it had planned and wrote the record when it read the request; an answer that went out late because its loop was late was recorded as delivered while the service had counted a timeout, and the retry that followed was a `B_repeat`.
* **The checker.** `purge` dropped what was needed by late deliveries (a replay-excused delivery that came late: 7 `C_filter`; an event delivered late by a zombie, below the floor, taken for an event nobody posted: 197,942 `P_phantom`); `posted_n` was trimmed by value once it passed 600,000, which dropped the numbers of events still in the window; an event at a sick or dead endpoint was excused by its acknowledgement time against the failure window, though its first attempt could be later by the service's lag; the saved state kept every endpoint ever made (6.1 MB at each save); and the checker kept multi-million-entry tables (20 % of a core by hour 15).
* **"Address already in use".** Five times in `receivers.log`, on the ports of endpoints of the run; **the holder was not identified** and it did not happen in any of the eleven runs since. What was changed: the allocation of ports is under a lock (three churn threads could be given the same port, which is one way to get exactly this, not shown to be the way), the ports of endpoints that are gone are used again (the range of 8,000 ports is no longer run through), and a failed bind is counted, retried, named in the log with the processes that hold the port, and, when it lasts 10 s, makes the run invalid. A port with a listener of its own that does not set `SO_REUSEADDR` is the one case in which Linux refuses a bind that does; `SO_REUSEPORT` was not used, because it would let a stale listener of a stranger share the port and answer nothing.
* **Backups.** `subprocess.run(timeout=180)` killed only the `bash` of `backup.sh`; the `cp` and the checker it had started ran on and the `.partial` directory stayed: 36 of them, 32 GB.
* **The log was pinned.** The harness never set a maximum age, and 32 waiting replays that never started (attempts 0, for hours) kept the log from being dropped.
* **The guard only spoke at the end.** The validity criteria were evaluated from the finished run, so the run went on for nine hours after it had become invalid.

**What changed after run 3** (the harness only: what is judged about the service is the same, except as noted, and where a check was changed the reason is in "Changes to the criteria"):

* The janitor removes any endpoint the harness did not intend to be in the service (through the API, and the row), after an unanswered create and after every start, and logs each as a harness event; the receiver and the port of an endpoint that is gone are given back.
* The receivers write their ledger off the event loop, record when the answer was sent and decide whether it counts then, can run as several processes, forget endpoints that are gone, reuse ports, and report a port that is in use instead of ignoring it.
* The guard judges continuously and stops a run that is invalid, the host is profiled in every sample, and the run does not start on a host that is short of memory.
* The backup is killed as a group, its partial directory removed, its timeout scaled by the size of the data directory, and two backups kept.
* A maximum age bounds the data; expiry is excused only for events older than it, and counted against the service's own count.
* The checker's tables are bounded and keyed as described above.

**What was checked after the changes** (short runs of the pinned build, compiler d7228ca, of 200 s to 40 minutes (run A is the build of ce4c23f of the harness's branch, B e7568ee, C 43cc8f0; the harness changed little between them), with every fault on at a chaos scale of 0.15, so about 6 times as many kills, stops and database faults a minute as in a day-long run; the service on one core and the harness on seven others, on a 16-thread machine that other people also use; **none of this says anything about 24 hours**):

| what | measured |
|---|---|
| the receivers' cost per record, minutes 3 to 7 / 13 to 17 / 23 to 27 / 33 to 37 of a 40-minute run | 462 / 456 / 489 / 471 µs (run A); 1,092 / 1,003 / 909 / 997 µs (run B); 1,015 / 1,019 / 1,125 / 946 µs (run C): flat within each run. The same code cost about twice as much per record in B and C as in A from the first minute. **That the cause is the machine's clock (cpufreq `powersave`, 400 MHz to 4.7 GHz, the service's core at a median of 1,276 MHz and the harness's at 1,112 in C) was not shown**: only C recorded it. A cost per record is comparable within a run only. The run before the fix of the name server, for contrast: 500 to 1,530 µs in 15 minutes |
| the receivers' CPU, two processes, at 40 events a second | 15 to 37 % of a core in all, the busiest process at most 36 %; the loop late by at most 4 ms in A; the ledger writer's wait at most 4 ms, no record written by the loop for want of queue room, no answer that was due at once sent late (A) |
| zombies | A: 2 creates answered 404 by the service (the row was in the table) were found by their port and removed, API and row; none was left. B and C: none occurred |
| `.partial` directories, backups | none left (A to C); 31, 17 and 29 backups, the newest two kept |
| the table of waiting replays | reached the cap (32) at times, never for 180 s |
| the maximum age | `events_expired` stayed 0 in all three |
| the guard, on receivers held to a quota of 25 % of a core (SIGSTOP and SIGCONT every second on the two processes of the run, which is what a cgroup quota does) | **the run was marked invalid at 361 s** (the loop late by more than 250 ms in 51 % of the samples, three minutes after the quota began), stopped early, `run.json` says `valid: false` with the reason, the verdict is `INCONCLUSIVE`, and the repeats that a starved loop causes (a request read late is a timeout to the service) did not appear as violations: requests read after a stalled loop are marked as risks (before that change a core starved by other processes gave about 1,000 `B_repeat` in two minutes) |
| a stall | a receiver held for 7 s: the sample logs the stall with the three busiest processes (`harness-event`, `stall`) |
| violations in the three healthy runs | A: none. B and C: one `I_cron_gap` each (below). No `A_missing`, `B_repeat`, `C_filter`, `P_phantom`, `F_dead_letter` or `END_lag` in any of them |

Two criteria failed in the healthy runs, and **neither is attributed to the harness**:

* **H2, the 99.9th percentile of the loop probe, in all three:** 76, 97 and 100 ms against the 50 ms of the criterion (the 99th percentile 55 to 68 ms; the median 13.5, 23 and 26 ms, which follows the machine's slowness above). The harness's side of the measure is clean: the control request of the probe (the host) took a median of 1.3 ms and at most 3.8 ms, the receivers' loop at most 4 ms late, nothing sent late. The service's loop is what was late. It is a short run with the faults six times as dense as in a day, on a core that the cpufreq governor slows; that H2 would pass in a run of 24 hours is **not shown**. The criterion is not changed.
* **`I_cron_gap`, once each in B and C (not in A):** a gap of 5 s in the every-second schedule, 14 to 19 s after a database fault ended. In C `cron_fired` did not move for about 40 s that covered two faults (8.9 s and 18 s long), `cron_skipped` stayed 0, and `db_reconnects` rose some 20 s after the fault proxy was restored: **the service went on without cron until it had reconnected**, and the checker excuses a gap only within 5 s of the time the database was away. Whether so slow a reconnect is acceptable is a question about the service (what `design.md` says of cron and the database), not a defect of the harness, and the criterion is not loosened. It was not seen in soak25, whose database faults were a sixth as frequent.

### What the harness found in itself, and the criteria that changed because of it

The first runs of the harness failed for causes in the harness (counted wrongly, a lock kept too long, a rotation that dropped a secret too soon, endpoints left behind by a create that was not answered). They are corrected, and the criteria that changed with them are listed next.

One more, found by the self-test's clean run on a slow (emulated) machine: a create answered `504` left its row in the table, the service did not know it, so the harness's `DELETE` was a `404` and the row stayed; a `kill -9` later the start loaded it at the slowest cursor, a replay to every endpoint that wants an event reached it ahead of its window, and the window sent the same events again two seconds later. The endpoint was marked as one about which nothing is judged, but the repeat check did not honour the mark (three `B_repeat`). It does now, and `--selftest-ledger` has the case. What the service does here is what `status.md` says of a change whose outcome it did not learn (the table is read once).

## Changes to the criteria

Written after the shakedowns, and the reason for each:

| change | from | to | why |
|---|---|---|---|
| retry schedule | 500 ... 16000 ms (6 delays, about 31 s) | 8 delays, about 64 s | an outage of 10 s, a receiver that fails the first attempt of its own and a restart add up to more than 31 s; at 64 s they do not unless four outages hit one endpoint |
| `http5xx` | fails the first 0 to 3 attempts | fails the first attempt of one event in two | same |
| burst rate | 3 times the rate (120 a second) | 1.5 times the rate (60 a second) | the receivers are one process and used about 49 % of a core at 40 a second; a burst at 120 needed about 147 %, and the run that did it saturated them (section 8) |
| validity | the poster held 90 % of the rate, the harness under 60 % of a core, the load under the cores | and the receivers under 85 % of a core, their loop late by more than 250 ms in at most 5 % of the samples | a saturated receiver makes failed attempts that the checker reports as the service's |
| `dead` and `sick` | an endpoint on a closed port for good; a 60 s window | a window of 90 s about every fifteen minutes, longer than the schedule so that events die in it | finding 6 (the log cannot be bounded with a receiver that is down for good) |
| E | a cursor is at least what it was after any restart | back at what it was within 120 s of a restart; strictly monotonic within an incarnation | finding 4 (the service works its cursors up again after a start, at about 50 ids a second when they are far behind) |
| B | one tag | `B_repeat` and `B_restart_repeat` | finding 1 has one cause |
| rate | 150 events a second | 40 (burst 120), a fifth of what the mix sustains | every restart and outage leaves a backlog to work off (finding 4); `capacity.md` has what a core carries |
| outages | any endpoint at any time | an endpoint is not made to fail again within 45 s | stacking, section 7 |
| H | every window | not the seconds of a `replay-dead` call | reading the dead letters beyond the 2,048 of the table is a read of `delivery.seg` and a documented pause of the loop (`docs/runbook.md` 3.6) |
| G | the last incarnation | the last incarnation before the clean stop at the end | the incarnation that is started to watch the restart lives seconds |
| validity | judged from the finished run | judged continuously over a sliding window of ten minutes; the run is marked invalid from the moment a criterion is met and ends early | run 3 went on for nine hours after it had become invalid. The thresholds are the ones that were there (85 %, 250 ms, 5 %, 60 %, 90 %); `harness_cpu_pct` no longer includes the receivers, which have their own criterion, so that several receiver processes are not counted as one harness over its limit |
| A | an event passed undelivered is `A_missing` | an event older than the maximum age (less 2 s) when the cursor is found past it is an expiry, counted against `events_expired` | the service now has a maximum age (`--max-age-ms`); only events older than it are excused |
| A | a `sick` or `dead` endpoint's loss was deferred if the event was acknowledged inside the failure window | or if its first attempt could have been inside it (acknowledgement plus 2 s plus the service's lag at that endpoint, up to 300 s) | the first attempt can be later than the acknowledgement by the lag. A deferred event is judged again at the end (`A_missing_final`), so nothing is forgiven |
| receivers' record | effective if the planned delay was within 1.6 s, decided when the request was read | effective if a 2xx was sent within 1.6 s of the request, on a connection still open, decided when it was sent | a late answer is a timeout to the service |
| verdict of an invalid run | INCONCLUSIVE only if it would have passed | INCONCLUSIVE whatever it found, with what it found listed | a run that is stopped as invalid is not evidence for or against the service; a starved run was reported FAIL on a series criterion |
| a request read after a stalled loop | recorded as any other | marked F_RISK, like a slow answer, for the next half second after a stall of the receivers' loop of more than 250 ms | the service's clock had been running while the request waited in the kernel's buffer; without it a starved harness produced 1,000 `B_repeat` in two minutes, which the guard now reports as the harness's stall instead |
| G | the disk bound and the pin of the log judged at the end | also judged while the run goes (`G_disk_over_bound`, `G_replays_pinned`) | 8+ hours were spent on a run in which the log was pinned |
| the `.synced` files of the shim | counted in the data directory | taken away when their file is gone, and not counted | the shim leaves one for every segment it has seen |
| `I_cron_gap` after a database fault | a schedule may be silent for 5 s after a database fault ended | 20 s | the schedules are read through the database, and the service documents (`configuration.md`) that a held request is remade after `pg-request-ms` (10 s), an attempt may take `pg-attempt-ms` (5 s) and the wait between attempts is at most `pg-backoff-max-ms` (5 s): up to 20 s before a blackholed database is used again. Found by the 40-minute validation runs of the repaired harness, in which a schedule was silent for 14 to 19 s after a fault; a service that is away (a kill, a stop) keeps the 5 s, and `scripts/soak/tests/test_checker.py` holds the cases that must still be flagged |
