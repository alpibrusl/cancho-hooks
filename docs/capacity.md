# Capacity: how it is measured, and what was measured

This page is the method for answering "how much will one service carry?" on your own hardware, and the figures it gave on the machine described below. It is a
measurement of this service, not a benchmark of anyone else's, and **the machine is part of every result**: the figures were taken on a shared virtual machine, where other
work was running at the same time (the load average is printed with each table), so they are what this build did there on that day, not a guarantee. Where a figure has
not been measured, the cell says so; none is estimated.

## 1. What there is to measure

The service is one process with one thread and one event loop (`docs/runbook.md` section 0): it takes `POST /events`, makes a group commit of the events that arrived together, and
delivers each event to each endpoint that wants it, with at most 64 attempts in flight and at most 8 to one endpoint. Capacity is therefore **per core**, and the question is what
one core costs per event and per delivery. More than one core of work is a second service with its own database (one service per database).

| quantity | how it is stated |
|---|---|
| ingest | events a second the service acknowledges (`202` is sent after the flush that covers the event), with 64 connections and with one (one flush for each event), and the time a sender waits (p50, p99) |
| delivery, plain HTTP | CPU microseconds per delivery, and so deliveries a second per core (`1,000,000 / cost`), with 1, 10 and 62 endpoints (the most there can be) |
| delivery, `https` | the same, for a full handshake each time and for a resumed session |
| latency of delivery | the time from the sender's request to the first delivery at a receiver (end to end), at a rate the service sustains |
| memory, descriptors | resident memory (`VmRSS`) and its high-water mark (`VmHWM`), descriptors, threads |
| disk | bytes of the data directory, which retention bounds (`docs/retention.md`) |

What bounds a service besides its core, and is not a cost per event:

* **The window of 1,024 ids.** Each endpoint has a cursor (every event up to it is final) and a window of 1,024 ids above it. An endpoint more than 1,024 ids behind is not served
  until it catches up, which is backpressure, not a loss. It follows that a receiver that fails for a time `T` while events arrive at `R` a second falls out of its window if `R x T`
  exceeds 1,024: at 150 events a second, in under 7 s. An endpoint that fails for the whole of the retry schedule (24 hours by default) holds its events, and so the log, for that time.
* **Attempts in flight**: 64 in all, 8 to an endpoint unless the endpoint's own `concurrency` says less. A receiver that takes `L` seconds to answer carries at most `8 / L` deliveries a
  second, whatever the service's core could do.
* **The rate limit** of an endpoint (`rate`), when set, is a ceiling by design.
* **A restart**: a service that is started works the cursors of its endpoints up again from the logs (section 5: about 50 ids a second for an endpoint with a list of event types that was far behind;
  within a second or two for one that was close). What a restart leaves behind is a backlog, and a backlog is delivered again in some cases (`docs/soak.md` section 8, finding 1): the rate a service
  sustains **across** restarts is below the rate it sustains between them.

## 2. The method

Everything is in `scripts/soak/capacity.py` and `scripts/soak/soak.py --calibrate`; the C tools are `scripts/bench/loadgen.c` and `sink.c`.

```sh
# gcc is needed; the tools are built into the output directory
python3 scripts/soak/capacity.py --binary build/hooks --out capacity-out          # ingest, delivery to 1, 10 and 62 endpoints, and https; capacity.md and capacity.json
python3 scripts/soak/soak.py --calibrate --binary build/hooks --out calib-out      # what the endpoint mix of the soak sustains, with the latencies; no fault
```

1. **A quiet machine, with the cores divided.** The service is pinned to one core with `taskset` (the last), the load generator and the receivers to the others. The load average is
   written down at the start and at the end; a run on a machine whose load is above its cores is repeated, or marked.
2. **Receivers that are not the limit.** The load generator (`loadgen`, in C, `epoll`, keep-alive connections, one request in flight on each) and the receiver (`sink`, in C, answers `204`
   and closes) are not what a figure measures. The `https` receiver is Python (`tests/tlskit.py`) and is the slower side: for `https` the figure is the service's own CPU per delivery,
   not the rate the pair reached.
3. **CPU is read from the service**: `utime + stime` from `/proc/<pid>/stat` before and after, over a number of events or deliveries large enough that the tick (10 ms) does not matter
   (20,000 events; 600 deliveries for `https`). The cost of a delivery is `(total CPU - events x the cost of an event measured with no endpoint) / deliveries`.
4. **Every row is run three times** and printed as the median with the least and the most. A row whose runs differ by more than a quarter was disturbed and is read as such.
5. **The sender's latency** is the loadgen's own, from the first byte sent to the last byte of the answer, for each of the requests (p50, p99).
6. **End-to-end latency of a delivery** needs a receiver that knows when the event was sent: the soak's receivers read it from the body (`t`) and keep the percentiles for each sample
   interval (`deliv_lat_*` in `metrics.csv`). `--calibrate` runs the endpoint mix of `docs/soak.md` at rising rates with no fault, 20 s each, and prints for each rate what was achieved, the
   worst lag of any endpoint, the CPU of the service and of the harness, and those percentiles. The rate the mix **sustains** is the highest at which at least 95 % of the requested rate
   was achieved, no endpoint was more than 500 events behind, and the receivers' loop was never more than 100 ms late.
7. **What is reported with a figure**: the CPU model and cores, the kernel, the load average, which core had what, the binary's SHA-256, and the date; `capacity.json` has them.

A service that is run for days has other costs (memory that grows, a log that is not bounded), which a rate does not show; those are the soak test's (`docs/soak.md`).

## 3. Figures from earlier work, with their sources

These are cited, not repeated as measured here.

| figure | source |
|---|---|
| about 90 to 140 microseconds of CPU for a plain-HTTP delivery, so roughly 7,000 to 10,000 a second on one core | `docs/production.md` ("Where it stands"), `docs/design.md` section 28; `scripts/bench/run.py`, a shared 4-core machine, single runs |
| ingest 34,000 to 60,000 events a second over 64 connections | the same |
| about 1.0 to 1.4 ms of CPU for an `https` delivery with a full handshake, about 0.7 ms resumed: roughly 1,000 a second on a core | `docs/design.md` section 40, `scripts/bench/https_cost.py` |
| 3,000,000 and 10,000,000 events through one endpoint with retention at 20 s: the data directory between 15 and 93 MB, resident memory flat, no step of the loop longer than 68 ms | `docs/production.md` P0.2, `docs/design.md` section 38, `scripts/bench/retention_bench.py` |

## 4. Measured here

Machine: Intel(R) Xeon(R) Processor @ 2.10GHz, 4 cores, kernel 6.18.44-fc-v70, a shared virtual machine: other work ran on it during every measurement below (the load average is given). Build of `origin/main` at `45faec0`, binary SHA-256 `48c3cc3779e3c6c8...` (the sources of the service are the same at `d503801`, which only changed the documents; the compiler's output is not byte for byte reproducible, two builds of the same tree have different hashes, so the hash names the binary that was measured). The service on core 3, the C load generator and receiver on cores 0 to 2, 20,000 events of 200 bytes a run (645 events for 62 endpoints, so that the deliveries are about 40,000), five runs of each row. Measured 5 October 2026.

**Ingest and delivery** (`capacity.py --only ingest,deliver --reps 5`; load average 2.4 at the start and 1.9 at the end). "events/s (ingest)" of the delivery rows is the rate of the sender while the deliveries were going on; the cost per delivery is the figure to read, and 1,000,000 divided by it is the deliveries a second a core can make:

| row | events/s (ingest) | p50 / p99 to the sender (ms) | CPU per event (us) | deliveries/s end to end | CPU per delivery (us) | deliveries/s per core | RSS (MiB) | VmHWM (MiB) | data dir (MB) |
|---|---|---|---|---|---|---|---|---|---|
| ingest, 64 connections | 59655 (50443 to 73642) | 1.03 / 2.98 | 11.5 (9.0 to 12.0) |  |  |  | 8.6 (8.6 to 8.6) | 8.6 (8.6 to 8.6) | 5.1 (5.1 to 5.1) |
| ingest, one connection | 5083 (4484 to 5276) | 0.17 / 0.61 | 68.0 (66.0 to 76.0) |  |  |  | 8.4 (8.3 to 8.4) | 8.4 (8.3 to 8.4) | 1.3 (1.3 to 1.3) |
| deliver, 1 endpoint | 40912 (37315 to 45446) | 1.45 / 3.46 | 16.0 (14.0 to 18.0) | 6720 (6005 to 7103) | 86 (80 to 96) | 11696 | 9.5 (9.5 to 9.6) | 9.5 (9.5 to 9.6) | 6.7 (6.7 to 6.7) |
| deliver, 10 endpoints | 15409 (13236 to 17015) | 4.00 / 6.45 | 47.5 (45.0 to 52.5) | 10993 (10609 to 11840) | 72 (67 to 73) | 13822 | 11.0 (11.0 to 11.1) | 11.0 (11.0 to 11.1) | 4.1 (4.1 to 4.1) |
| deliver, 62 endpoints | 15540 (14144 to 17470) | 3.88 / 5.78 | 50.0 (40.0 to 50.0) | 12172 (11458 to 12221) | 67 (66 to 71) | 14981 | 17.1 (17.1 to 17.1) | 17.1 (17.1 to 17.1) | 5.0 (5.0 to 5.0) |

Read with care: **a core makes about 12,000 to 15,000 plain-HTTP deliveries a second** (67 to 86 microseconds each) and **ingests about 60,000 events a second over 64 connections** (11.5 microseconds each, p50 1.0 ms and p99 3.0 ms to the sender). With **one** connection every event waits for its own flush: 5,083 a second here (0.17 ms p50). Delivery to **one** endpoint reaches 6,700 a second where the same core makes 11,000 to 12,000 to ten or more: one endpoint has at most 8 attempts in flight, and that bounds it before the core does. The first run of the same measurement, with the load average at 7 to 8 (other work was running on the machine), gave 12,156 events a second over 64 connections and **375 over one** (126 us an event: the flush waits for the disk), 1,068 deliveries a second to one endpoint and 6,445 (3,643 to 11,555) to ten: the same service, the same binary. The machine is the result.

**`https`** (`capacity.py --only https --reps 5`, `scripts/bench/https_cost.py`; load average 1.5 at the start and 1.7 at the end; the receiver is Python, so the figure is the service's CPU per delivery and the wall time is the receiver's):

```
Intel(R) Xeon(R) Processor @ 2.10GHz, 4 cores, service pinned to core 3; 600 deliveries a row, 5 runs; binary build/hooks
kind             endpoints deliveries   CPU per delivery, median (least to most)     wall  handshakes (resumed)
http, address            1        600               317 us (283 to 367)     0.6s  
http, name               1        600               400 us (383 to 433)     0.7s  
https, full              1        600              1500 us (1433 to 1683)     3.5s  600 (0)
https, resumed           1        600               900 us (800 to 1067)     1.0s  600 (600)
http, address           10        600               133 us (133 to 183)     0.2s  
http, name              10        600               233 us (217 to 283)     0.5s  
https, full             10        600              1200 us (1100 to 1233)     0.9s  600 (0)
https, resumed          10        600               733 us (650 to 767)     0.7s  600 (600)
```

A full handshake costs about 1.2 to 1.5 ms of CPU against 0.13 to 0.3 ms for a plain delivery to an address, a resumed one 0.7 to 0.9 ms, so **about 700 to 850 `https` deliveries a second a core with a full handshake each time and 1,100 to 1,400 resumed** (the cost of an event and of the logs is in every row). A name instead of an address adds about 70 to 100 microseconds (a lookup for every attempt).

**The soak's endpoint mix with no fault** (`soak.py --calibrate`; 12 endpoints of the classes of `docs/soak.md`, each event going to five endpoints on average, two cron schedules; receivers in Python on three cores, the service on one; 20 s a step after 6 s of settling):

| events/s asked | achieved | service CPU | receivers CPU | receivers' loop late by (ms, worst) | worst lag of a steady endpoint (ids) | sender p50 / p99 / max (ms) | first delivery p50 / p99 / max (ms) | RSS (MiB) | load average |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 50 | 9 % | 12 % | 12 | 6 | 0.92 / 2.88 / 9 | 2 / 7 / 27 | 17.2 | 1.35 |
| 100 | 100 | 18 % | 22 % | 6 | 13 | 0.79 / 2.81 / 22 | 2 / 7 / 22 | 36.2 | 1.48 |
| 200 | 200 | 37 % | 44 % | 6 | 22 | 0.72 / 4.54 / 15 | 2 / 9 / 42 | 39.5 | 1.37 |
| 300 | 300 | 47 % | 66 % | 112 | 75 | 0.79 / 8.21 / 451 | 2 / 3351 / 3973 | 44.9 | 1.62 |

The mix **sustains 200 events a second** (about 1,000 deliveries a second), and what limits it is the harness's receivers: at 300 the receivers' loop (Python) was 112 ms late and the service was at 47 % of its core. The delivery latency here is the time from the sender's request to the receiver having the request, for the endpoints that answer at once; with faults it includes the retries, and is in the soak's reports. The soak's steady rate, 40 events a second, is a fifth of this; its burst, 120, is above half.

## 5. What the method found besides the figures

* **A restart works its cursors up slowly when they are far behind.** After a start, `GET /endpoints` reports the cursor of an endpoint with a list of event types as 41, 91, 143 ... (about 50 ids a second), where it
  said 3,000 before the stop, until it has passed the ids it had before (`scripts/soak/repro_restart_repeat.py` prints it). In the soak, with cursors a few hundred ids behind at 40 events a second, they were back at
  what they had been within 1 s (the median) and 13 s (the most) of 43 restarts. With a backlog of thousands of ids it takes minutes; this is why the soak's rate is a fifth of what the mix sustains and not three fifths.
* **The events delivered while the cursor is being worked up are, in some cases, delivered again** (`docs/soak.md` section 8, finding 1): the same cause, and a reproducer.
* **A full data directory makes the service deliver the same events again and again** (`docs/soak.md` section 8, finding 2).
* **A receiver that is late costs 8 / latency**: the soak's `slow` endpoint (20 to 120 ms, 70 ms on average) carries about 115 deliveries a second at most with its 8 attempts; it is sent a fifth of the stream, so that it is at its limit at about 550 events a second, which the mix does not reach.

## 6. To be measured by the long run

| | |
|---|---|
| resident memory after 24 hours under the soak, and the fitted growth (the criterion is in `docs/soak.md` section 4) | to be measured by the long run |
| the size of the data directory over 24 hours, against the bound retention allows | to be measured by the long run |
| the 99.9th percentile of the loop probe over 24 hours, and its worst window | to be measured by the long run |
| deliveries a second per core for `https` with a receiver that is not Python | to be measured by the long run |
| the same tables on the machine the long run uses (the one the 24 hours are made on) | to be measured by the long run |
