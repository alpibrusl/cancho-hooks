# lexsys-hooks: webhook delivery on lex-sys

Status: **design only.** Nothing is built. This project exists to be a realistic program that needs every component the lex-sys stack has, so that what is *missing* shows up as a failing test and not as an opinion. Section 9 lists what is predicted to be missing; it is a list of guesses, and the first job of the build is to find out which are wrong.

## 1. What this is, and what it is for

A customer's application posts **events** to an HTTP API. The service stores each event durably, then **delivers** it to every subscriber URL registered for that event's type: an HTTP `POST`, signed, retried with backoff on failure, with a record of every attempt. Svix and Hookdeck are products of this kind.

It is chosen because the properties that make it hard are exactly the properties the components claim, and each can be tested from outside:

| property | what has to be true | which component's claim it tests |
|---|---|---|
| an accepted event is never lost | a `2xx` from the API means the event survives `kill -9` at any instant | `lexsys-log` recovery (its design, section 6) |
| delivery is at least once | an event is delivered, or visibly dead-lettered, never silently dropped | consumer groups, the group log |
| a slow subscriber does not stall the others | one endpoint timing out for 30 s does not delay another's delivery | the event loop, outbound sockets, timers |
| the signature is checkable by anyone | an independent implementation verifies every delivery | `std.crypto` (plus HMAC, which is missing) |
| the authority is small and visible | the service touches one data directory, one listening port, and outbound network | `lex-sys authority`, `lex-os` grants |

The claim, in one sentence: **a single-node webhook service in lex-sys, with no `Ffi`, that loses no accepted event across arbitrary crashes, and whose authority report fits on one screen.** It is not a hosted product, it does not scale horizontally, and it has no multi-tenant isolation beyond API keys. A single node that does not lose events is a smaller claim than Svix's and a testable one.

## 2. What is built from what

| role | component | notes |
|---|---|---|
| HTTP API | `lexsys-web`, `lexsys-schema` | request validation by schema, `problem+json` errors |
| configuration data | Postgres via `lexsys-pg` (pool, and the pooler in front) | endpoints, subscriptions, attempt history |
| the queue | `lexsys-log`, Redis Streams front-end not needed here: the engine is used as a library | one stream, one consumer group of delivery workers |
| idempotency, rate limits, circuit state | `lexsys-cache` | *only* facts that can be lost without harm (section 4) |
| the audit trail | `lexsys-log` in trail mode, `lex-trail` events | one event per attempt |
| the authority check | `lex-sys authority`, `lex-os` grant | section 7 |
| signing | `std.crypto` SHA-256 plus an HMAC to be written | section 5 |

Where this needs something that does not exist, the project does not paper over it: it records the gap (section 9) and either works around it in the open or stops.

## 3. The data, and which store owns each fact

Two durable stores is the usual way a design like this loses events: the service writes an event to one, crashes, and the other never hears of it. The rule that prevents it is **one source of truth for each fact**, and nothing that must be consistent across two stores.

| fact | owner | why there |
|---|---|---|
| an event exists, and its payload | **the log** | accepted means "in the log and flushed" |
| what is pending, in flight, retried at time T, dead-lettered | **the log's consumer-group state** | it changes on every attempt; a log of changes is the cheap way to make it durable |
| endpoints, their secrets, which event types each wants | **Postgres** | rarely changed, queried by many, needs ordinary transactions |
| the history of attempts (status, latency, response body prefix) | **Postgres, written by the worker after the attempt** | for humans and the API; an `INSERT` with the attempt's id as a unique key, so a repeat is harmless |
| an event's idempotency key | **the log**, in the record, with an in-memory index rebuilt on open | a dedupe that forgets across a crash would double-send the event it was supposed to protect |
| per-endpoint failure counts, a circuit breaker's state, rate-limit counters | **the cache** | losing them costs a few extra attempts, never an event |

So an accepted event touches **one** durable store on the accept path. Postgres is on the accept path only for *reading* the subscriptions, and a Postgres outage degrades the service to "accepts events and queues them, cannot fan out until the subscriptions load", which is a stated mode and not a surprise. The attempt history can lag or be lost without losing a delivery.

## 4. Delivery semantics

**At least once.** After a crash an event can be delivered twice; it cannot be delivered zero times unless it is dead-lettered, and a dead letter is a recorded outcome the API shows.

**Idempotency on the way in.** A client may send an `Idempotency-Key`. A second `POST` with the same key within the window answers the first one's response, byte for byte, and writes nothing. The window is configurable; the keys live in the log (section 3), so the guarantee survives a crash.

**Per-endpoint order.** Events accepted in sequence are *attempted* in sequence to one endpoint **when the endpoint is healthy**. After a failure the retry of event N and the first attempt of event N+1 are not ordered, because holding every later event behind a failing one is head-of-line blocking, which is the usual reason to want a queue in the first place. Strict ordering is a per-endpoint option that accepts the blocking, and is not in v1.

**Retries.** A delivery succeeds on any `2xx`. Anything else, a timeout, or a connection failure is a failure. The schedule is Standard Webhooks' (so a subscriber used to Svix is not surprised): 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h, 24 h after the first attempt, then dead-letter. (This paragraph first said `10 h, 10 h` from memory; the specification, fetched when signing was built, lists `10 h, 14 h, 20 h, 24 h`. No jitter is built yet.) `410 Gone` disables the endpoint. The delays are data in the group state, so a restart resumes them, and a restart does not retry everything at once.

**Dead letters** stay in the log and are visible by id; `POST /events/{id}/replay` puts one back.

**Signing.** The format is [Standard Webhooks](https://www.standardwebhooks.com): headers `webhook-id`, `webhook-timestamp` and `webhook-signature`, the signature being `v1,` plus the base64 of HMAC-SHA256 over `id.timestamp.payload` with the endpoint's secret. It is chosen because a subscriber then needs no library of ours, and because there are independent implementations to check against. The specification was fetched when signing was built (section 15): the three header names and the signing string above are as it states them. Two things it leaves out are stated in section 15: the key is the base64-*decoded* secret, and it publishes no test vectors, so the reference Python library is the oracle.

## 5. The API, minimally

| | |
|---|---|
| `POST /endpoints` | register a URL, the event types it wants; answers its id and signing secret once |
| `GET /endpoints/{id}`, `PATCH`, `DELETE` | ordinary |
| `POST /events` | `{type, payload}`, optional `Idempotency-Key`; **`202` with the event id, only after the flush** |
| `GET /events/{id}` | the event and, per endpoint, its delivery state |
| `GET /events/{id}/attempts` | the attempt history |
| `POST /events/{id}/replay` | re-deliver, including a dead letter |
| `GET /healthz`, `GET /metrics` | liveness; counters in the Prometheus text format |

Request bodies are validated with `lexsys-schema`, and the errors are `problem+json`, as in `lexsys-web`.

## 6. The test scenario, fixed before the build

A harness drives the service with a **receiver** it controls (it records every request and can be told to fail, stall or answer with a status) and a **chaos** process that `kill -9`s the service and restarts it.

1. **No acknowledged event is lost.** Send 10,000 events, killing the service at random instants at least 200 times. Every event that received a `202` is, at the end, delivered at least once or dead-lettered. The count of events the harness saw acknowledged and the count in the log after the last restart are compared by id.
2. **Retries follow the schedule.** A receiver that fails the first *k* attempts sees the documented delays within a stated tolerance (a scaled-down schedule for the test), then one success, then nothing more.
3. **A stalled receiver does not stall the others.** One endpoint that never answers; a hundred others healthy. The healthy ones' p99 delivery latency with the stalled one present is within a stated factor of without it.
4. **Idempotency survives a crash.** The same key sent before and after a kill gets the same answer and one delivery.
5. **Signatures verify** against an independent implementation (Python's `hmac` and `base64`), for every delivery in the run.
6. **The authority report** has no `ffi`, names one filesystem prefix, one bound port, and outbound network only. Pinned by a test.
7. **Reported, not gated:** events accepted per second with the flush policy `always`; p50 and p99 of time from `202` to first delivery; memory and descriptors at 1,000 endpoints. The comparison is a Python service (FastAPI, Redis Streams, one worker) built to the same specification; its numbers are measured before ours and the method is written down before either.

If a criterion cannot be met because a component is missing, the result says which component and what the failure looked like. That is a finding, and the purpose.

## 7. Running under a grant

The deployment target is a `lex-os` box. The grant the service needs is the point of the authority report: a data directory it may read and write, a port it may listen on, and outbound network to anything (a webhook service cannot know its subscribers in advance, which is the one place the authority is *wide* and the design says so).

**An expected tension.** `narrow` takes its prefix as a literal at compile time, so a data directory chosen at run time from a configuration file cannot be a narrowed `Fs`: it would be the unnarrowed root, and the report would say `fs_write("")`, which says nothing. Either the directory is a build-time constant (precise, awkward to deploy), or the report is wide and the `lex-os` grant carries the real limit. Which one is workable is something this project is placed to find out, and it is listed in section 9.

## 8. What is not in v1

Multiple nodes or any replication; per-tenant isolation beyond API keys; a dashboard; transformations of payloads; strict per-endpoint ordering; a retry schedule per endpoint; response-body storage beyond a prefix; webhook *receiving* helpers.

## 9. Missing pieces, predicted

Each is a prediction from reading the code, with how the project will find out.

| predicted gap | what in the scenario exposes it |
|---|---|
| **Outbound TLS.** Almost every real subscriber is `https`. lex-sys has a TLS client example over `Ffi` and OpenSSL and no native one. | any `https` receiver; the authority report gains `ffi` |
| **Non-blocking connect and name resolution.** `tcp_connect` blocks, and so does `getaddrinfo`. On one thread, one slow subscriber stalls every delivery. | criterion 3 |
| **Timers.** Backoff, delayed retry and request timeouts are time-ordered work; there is a clock and a poll timeout but no timer queue. | criterion 2 |
| **Signals and a graceful shutdown.** Nothing catches `SIGTERM` to stop accepting, drain in-flight attempts and flush. | a restart under load |
| **HMAC-SHA256.** Not in `std.crypto`; SHA-256 is. | criterion 5 |
| **Runtime-configured `Fs` narrowing** (section 7). | criterion 6 |
| **URL parsing**, a **JSON writer for larger bodies**, **configuration**, **structured logging**. | everywhere |
| **Several workers on one log.** Threads and forked heaps exist; sharing the log between them needs a communication primitive lex-sys does not have (`parallelism.md`, T5). | throughput past one core |
| **Process supervision** of a service and its workers. | probably `lex-os`'s job |

What this project does **not** test: horizontal scale, replication, a large number of tenants.

## 10. Order of work

1. **`lexsys-log` L0 to L2** (record format and recovery, the stream commands, consumer groups). Needed whatever else happens; the service can use a simpler in-process queue to start.
2. **The API and the store**, delivering to plain `http://` receivers, with the receiver and the chaos harness. Criteria 1, 4 and 6 can be tested here.
3. **Retries, backoff, dead-letter, signing.** Criteria 2 and 5.
4. **The first gap measured for real**, with the evidence from steps 2 and 3, decides which lex-sys work comes next (most likely non-blocking connect, then timers, then TLS).

## 11. Open questions

1. **Should the log be used as a library or over RESP?** As a library it is faster and has no wire protocol to keep compatible; over RESP the service tests the Streams front-end as a client would. The scenario favours the library; the Redis-compatibility claim favours RESP. This design assumes the library, and says so in section 2.
2. **A single process or a service and workers?** One process is simpler and shares the log; separate workers survive a service crash independently. The scenario's first criterion is easier to read with one process.
3. **Is the Python baseline worth building?** It costs about as much as a day of the project and is the only way the throughput numbers mean anything.

## 13. What building the first step showed

H1a is ingest only: `POST /events` appends to `lexsys-log`'s `log.ls` and answers `202` after a flush, `GET /events/:id`, `GET /healthz`, one process, one thread. Requests that arrive in one turn of the loop are **held** (`http.server`'s `hold`/`answer`), the turn's appends are covered by **one** `flush`, and then each is answered: group commit, as `lexsys-log` section 5 describes.

**The first criterion, tested (`tests/chaos.py`).** Eight threads post 3,000 events while the harness `kill -9`s the service at random instants and restarts it; a client whose request fails tries the same event again. The result on this machine: 155 kills, 3,001 records in the log, 3,000 acknowledged, every acknowledged event present and byte-identical, ids dense from 1.

**A `kill -9` is not a power cut, and a test that only kills the process cannot see a missing flush.** The kernel keeps every byte a killed process had written. The first version of this harness passed against a service that acknowledged *before* flushing, which is exactly the bug it exists to find. So `tests/fsync_shim.c` is an `LD_PRELOAD` shim that records, beside each `*.seg` file, how long it was when its last `fsync` returned; at every kill the harness truncates the file to that length plus a random part of the rest (and sometimes zeroes the last block of that part), which is a state a power cut can leave. With it:

| mutant of `src/hooks.ls` (a scratch copy, never committed) | result |
|---|---|
| acknowledge without flushing at all | **killed**: 228 acknowledged events missing |
| flush *after* sending the acknowledgements | **killed in 6 of 6 runs**, and by a *worse* symptom than loss: 6 to 8 acknowledged ids were **reused by a different event** after the cut, because the lost record's id was handed out again |
| append skipped but still acknowledged | **killed**: the event is missing |

(A fourth mutant, recovery skipped at open, would not compile: it removed the only use of `file_read` in that function and this language refuses a row that is not exact. That recovery is mutation-tested in `lexsys-log` instead.)

**Findings about lex-sys, from building it.**

* **`http.reason` has no `202`**, so the status line reads `HTTP/1.1 202 Unknown`. Cosmetic, in `std.http`, and a one-line fix.
* **`GET /events/:id` is a linear scan.** The log keeps no index yet. Fine for a few thousand events, not for a large log; the sparse index of `lexsys-log` section 4 is what it needs, and nothing else in this step does.
* **The data directory is an argument, so the service holds the unnarrowed `Fs("")`** and `lex-sys authority` says `fs_write("")`. This is the tension section 7 predicted, now seen in a real program.
* **A region's arena is 64 KiB**, so the 68 KiB scan window and the 20 KiB record scratch are heap boxes (`lexsys-log` section 12).
* **A `res` enum cannot be assigned over**, so a function that opens a log returns from inside the `match` instead of building an answer in a variable. Not a bug; it shaped the code.

**Not built in H1a:** endpoints, delivery, retries, signing, idempotency. H1b, delivery to one fixed receiver, is section 14.

## 14. What building the second step showed

H1b delivers to **one fixed receiver** given on the command line (`hooks <port> <dir> <host> <port>`). After each turn of the loop the service sends the events the log has flushed, in order, one at a time, to `POST /hook` with the event as the body and `webhook-id: evt_<id>`, and counts an event delivered on any `2xx`. `src/deliver.ls` (since replaced by `src/attempt.ls`, section 16) was one attempt: `tcp_connect`, send, then wait for a status line for at most 2 s with a `Poller` on the non-blocking connection.

**How delivery is remembered.** A second log, `delivered.seg`, gets one record per delivered event, and the record's id *is* the event's id. Because delivery is strictly in order, the last record is the cursor, and recovering that log is all a restart needs. The record is written **after** the receiver answered and flushed once per turn, so a crash between the answer and the flush redelivers (at most one turn's worth, 16): at least once, never zero.

**Tested (`tests/attempt_test.py`, `tests/delivery.py`).** The attempt alone, against seven receivers: `204`, `500`, a receiver that never answers (deadline at 1,003 ms), one that closes without answering, one that sends junk, one that splits the status line across two packets (`200` after 303 ms), and a port nothing listens on. All answer what was expected. The delivery run: a receiver answering `500` to 20% of requests and stalling past the deadline on 1%, a service killed as a power cut at least 100 times while 300 events are posted and drained, then **no more kills** and the backlog must drain within 90 s, then the service must go quiet, then one last power cut must leave the cursor where it was and a restart must deliver nothing. Result on this machine: 101 kills, 300 acknowledged events, all answered `2xx` by the receiver with the bytes the client sent, first deliveries in id order, 393 requests in all (93 repeats, of which 9 came after a `2xx` the receiver had given: a crash before the cursor flush, or a stall that answered after the deadline).

**Mutants, and what they said about the harness.** Five mutants of `src/hooks.ls` in scratch copies, never committed. All five are killed by the harness as it now stands, but **two of them survived its first version**, and that is the useful part:

| mutant | first version of the harness | now |
|---|---|---|
| treat any status as delivered (a `500` counts) | **survived**: it checked that the receiver *saw* each event, and a request that got a `500` is seen | killed: 64 acknowledged events never answered `2xx` |
| after a success, do not advance past the event | **survived**: every kill restarted the service, which rescans the log and so un-stuck it; it made one event of progress per restart | killed: the kill-free drain phase; the cursor stops at 36 of 300 |
| cursor record written but never flushed | passed every at-least-once check, as it should (a repeat is allowed) | killed only by the last stage: a power cut took the cursor from 300 back to 110 |
| restart scans the events log from offset 0 instead of after the cursor | killed (starvation: 293 events never delivered; the service redelivered events 1 to 7 on every start) | killed |
| restart ignores the cursor, starts at 0 | not run on the first version | killed: the cursor is at 6 of 300 after the kill-free phase |

The lesson is the same as in section 13: a property that tolerates a behaviour (repeats are allowed) cannot also detect it, so each such property needs a check of its own that does not tolerate it.

**The first predicted gap, measured (`tests/stall_probe.py`, a report and not a gate).** Delivery runs in the same loop as ingest, so a receiver that is slow is slow for everyone. `POST /events` latency while the receiver is:

| receiver | p50 | worst | |
|---|---|---|---|
| healthy | 1.8 ms | 89 ms | |
| accepts and never answers | 1.6 ms | 2,004 ms | 10 of 200 posts took over 500 ms; each is one attempt waiting out its 2 s deadline |
| accept queue full (the SYN is dropped) | 10,000 ms | 10,000 ms | 5 of 6 posts hit the probe's own 10 s limit; the real wait is the kernel's connect timeout, **not measured** and commonly over a minute |

So criterion 3 (a stalled receiver does not stall the others) **fails by construction**, as predicted, and the numbers say how: the read deadline bounds the damage at 2 s per attempt; the blocking `connect` does not bound it at all. Non-blocking connect (and name resolution, which this step does not meet because it dials an IP) is the next piece of lex-sys this project needs.

**Other findings.**

* **The cursor is stricter than section 4 allows.** One cursor means a failing event blocks every later one: head-of-line blocking, which section 4 says v1 avoids ("the retry of event N and the first attempt of event N+1 are not ordered"). With one receiver the two designs coincide; with several endpoints a per-endpoint state that is not a single number is needed, and `delivered.seg` becomes a set of (endpoint, event) records. This is H1c's first job, not a detail.
* **`poller_new` per attempt** costs an epoll descriptor each time. Fine at this rate; the poller belongs to the delivery loop once it is a loop with several attempts in flight.
* **A blocking write** after connect would stall on a receiver whose window is full; the bodies here fit the socket buffer, so it was not reached.
* **`log.read_at` reads only below the synced mark**, which is what stopped the delivery loop from sending an event whose `202` had not been given yet; nothing had to be added for that.

**Not verified here:** more than one receiver; signing; the retry schedule (the pause is a fixed placeholder, 200 ms times the failures in a row, capped at 2 s); the kernel's connect timeout; throughput of delivery; behaviour with a backlog larger than a turn (16) under kills.

## 15. What building the third step showed

H1c replaces the one fixed receiver with **several endpoints**, signs every delivery, and retries on the Standard Webhooks schedule, ending in a dead letter.

**Endpoints** are read from `<data-dir>/endpoints.conf`, one `<id> <host> <port> <secret>` a line (a stand-in for the Postgres table of section 3, read once at start). `GET /stats` answers the counters (`endpoints`, `attempts`, `delivered`, `failed`, `dead`). With no such file the service only ingests.

**State.** `src/state.ls` keeps, for each endpoint, a **cursor** (every event up to it is final: delivered or dead-lettered) and a **window of 1,024 cells** above it holding, for events that finished out of order or are waiting for a retry, the attempts so far and the time of the next attempt. This is what removes the head-of-line blocking section 14 found in the single cursor: a failing event waits in its cell while later ones go past it. The price is stated in the code and here: an endpoint more than 1,024 events behind is not served until it catches up (backpressure, not a drop), and the cursor assumes ids are dense, which they are because ingest hands out `last + 1`.

**The log of outcomes** (`delivery.seg`, which replaces `delivered.seg`) holds one record per attempt that ended in something: *delivered*, *failed* (with the attempts so far and the **Unix time** of the next attempt), or *dead*. Restart replays all of it, so the schedule, the counts and the delays survive a crash; nothing is flushed before an outcome is written, and one flush covers a turn's outcomes. It grows without bound (no compaction yet).

**Signing (`src/sign.ls`)**: HMAC-SHA256 and base64 written in lex-sys on `std.crypto`'s SHA-256, no capability. Checked against the **reference Python library** (`standardwebhooks`) in 536 comparisons: every payload length around SHA-256's block boundaries, keys under, at and over the 64-byte HMAC block, a secret with and without its `whsec_` prefix, strict base64 decoding with seven malformed inputs. Ten mutants of `sign.ls` (the two pads, the long-key rule and its boundary, the separator, the prefix, padding, the tail, the length, the decoder's length check) are all killed. What the specification does not say, and the library settled: **the HMAC key is the base64-decoded secret**; and it publishes no test vectors, which is why an independent implementation, not a vector, is the oracle.

**A gap found: lex-sys had no wall clock.** The timestamp header is Unix seconds and a receiver checks it, but `Clock` was monotonic only. Signing with that would have been refused as 50 years old, and the retry times persisted in the log need a clock that survives a restart. The fix is a builtin, `clock_unix_ms`, in lex-sys PR #190 (merged; this repository's CI builds against lex-sys `main` from that merge).

**Tested.**

| test | what it does | result |
|---|---|---|
| `tests/state_test.ls`, `tests/endpoints_test.ls` | the window (order, retries, edge, ring reuse, endpoints apart), the outcome record, the file and every kind of bad line | 9 + 2 pass |
| `tests/retry_test.py` | schedule 200/400/800/1600 ms: gaps of 203, 404, 806, 1,610 ms; 100/100/100 ms: 4 attempts then one dead letter; 500/1000/2000 ms with a kill after every failure: gaps of 520, 1,013, 2,017 ms | pass |
| `tests/delivery.py` | three endpoints (mostly healthy; flaky; poisoned), 100 kills as power cuts, 300 events, then a kill-free drain, quiet, a last cut and a restart | 300 acknowledged events (302 in the log); 18 poisoned events dead-lettered and none delivered; every request verified by the reference library; the next event overtook the poisoned one in 18 of 18 cases (the gate is 80%); 1,499 outcome records, 593 of them failed attempts |
| `tests/isolation_test.py` | a stalled, then a slow endpoint beside two healthy ones; a burst behind a slow one | next table |

**Mutants of the delivery logic: fourteen, all killed, and what it took.** Twelve changed the state, the schedule, the outcome log and the signing; two changed the bounds below. Four of them (ignore the next-attempt time; replay ignoring failed attempts; the next-attempt time not persisted; the attempt count not persisted) are killed **only** by `retry_test.py`, none by `delivery.py`, because under chaos a retry that happens too early or too late is still a delivery. The last of those survived the first version of the retry test too: it kills the service after every failure but only ever reached a third attempt, where a lost count changes nothing; a fourth attempt with stepped delays (500, 1,000, 2,000 ms) is what shows it. **One survived for a reason that was a bug in the harness, not the service**: "do not flush the outcome log" passed, because `chaos.py` still cut `delivered.seg`, the file's name in H1b, and so never cut the log under test. A mutant that survives a test written for exactly it is a reason to look at the test; this one found that the power-cut claim for the outcome log had not been exercised in the runs made between renaming the file and finding this. The H1b run (section 14) used the right name and is unaffected. The final run, with the right name, is the one in the table.

**The first predicted gap, again.** (*Section 16 removes the cause and re-measures; what follows is what H1c measured before that, and its numbers are not the service's now.*) Delivery still runs on the thread that serves ingest. Section 14 measured one stalled receiver; H1c made it worse before making it better: with a retry schedule every new event gets its own immediate first attempt, and with the same `most_per_turn` of 16 and a 2 s deadline a single silent endpoint held ingest for a median of **3,957 ms** a POST (it had been 1.6 ms). Two bounds fixed that: an attempt that **times out or cannot connect puts the endpoint to rest for 5 s**, and **a turn starts no new attempt after 250 ms**. Results (`tests/isolation_test.py`, 80 events at about 20 a second):

| endpoint beside two healthy ones | ingest p50 | ingest p99 | healthy endpoints' delivery p99 |
|---|---|---|---|
| none (baseline) | 2.0 ms | 50 ms | 2 ms |
| never answers | 2.1 ms | 1,956 ms (1% of POSTs over 500 ms) | 3 ms |
| answers after 300 ms | **255.9 ms** | 282 ms | 305 ms |
| burst of 40 queued behind the 300 ms one, then 25 probe POSTs | 506 ms | 605 ms (2,413 ms without the turn budget) | not measured |

So **criterion 3 is not met**, and the table says how. A stalled endpoint costs ingest one 2 s deadline per rest period, which the cool-down limits; a *slow but answering* endpoint costs every POST its full answer time, 255.9 ms against the 50 ms median that was fixed before the run (the test reports it and does not gate on it, since loosening it would hide the finding). A `connect` that is never answered (a full accept queue) still blocks the whole service for as long as the kernel waits; re-measured here at 10 s or more per POST, with the kernel's own limit still not measured. The remedy for all three is the same and is not a tuning: an attempt that does not hold the loop, which means a non-blocking connect and a way to have several attempts in flight.

**Not built:** idempotency keys; `410 Gone` disabling an endpoint; jitter; endpoints in Postgres, and the attempt history; the cache's circuit breaker; compaction of `delivery.seg`; `POST /events/{id}/replay`; the API of section 5 beyond `POST /events`, `GET /events/:id`, `GET /healthz` and `GET /stats`.

**Not verified:** more than three endpoints at once, or any with a name to resolve (`getaddrinfo` blocks, and the tests dial IP addresses); behaviour past the 1,024-event window under kills; the rest period under kills; throughput of delivery; DNS, TLS and `https` endpoints, which do not exist here yet.

## 16. What building the fourth step showed

H1d makes the attempt **not hold the loop**. The first predicted gap (section 9) was a blocking connect; sections 14 and 15 measured what it cost; this is the fix and the measurement of it.

**Two pieces of lex-sys.** `tcp_connect_start(net, host, port)` is `tcp_connect` with the socket made non-blocking first and `EINPROGRESS` counted as success; `conn_connect_status(&!Conn)` reads `SO_ERROR` once the poller says the connection is writable (`std.conns.connect_status` for one held in a table). Same authority as `tcp_connect`, same `net_out("host:port")` in the report. lex-sys PR #191 and `docs/native-sockets.md` section 10.6. A *name* is still resolved by a blocking `getaddrinfo`; an IP literal needs no lookup, and the tests dial IP literals.

**`src/attempt.ls`** is a state machine per attempt: *connecting* (watched for writable), *sending* (the request, as the kernel takes it), *reading* (the status line). Each state waits for the poller, so up to 64 attempts are in flight together and none of them blocks. Their connections sit in a `std.conns` table and are watched on **the server's own poller** under tokens above its connections' (`server.first_token`, `server.foreign`: the interface `lexsys-pg`'s pool already used). A turn of the loop is now: wait; handle requests; move along the attempts the poller woke; end those past their deadline; start new ones; flush the outcomes once. At most 8 attempts per endpoint are in flight and at most 16 are started a turn, so one bad endpoint can hold 8 of the 64 slots and no more. The 2 s deadline covers connect, send and the wait for the status line, and is the optional fourth argument (`hooks <port> <dir> [<delays> [<deadline-ms>]]`). The two bounds H1c added (a 5 s rest after a timeout, a 250 ms budget per turn) are gone: they were the workaround for the thing this removes.

**Measured (`tests/isolation_test.py`; 80 events at about 20 a second, one bad endpoint beside two healthy ones).** Before is section 15's table; after is this build.

| bad endpoint | ingest p50, before → after | ingest p99, before → after | healthy endpoints' delivery p99, before → after |
|---|---|---|---|
| silent (never answers) | 2.1 → 2.3 ms | 1,956 → 80 ms | 3 → 2 ms |
| slow (answers after 300 ms) | **255.9 → 2.3 ms** | 282 → 78 ms | 305 → 5 ms |
| blackholed (accept queue full) | **10,000 or more → 2.4 ms** | (a POST held 10 s or more) → 77 ms | not measured → 3 ms |
| burst of 100 events behind a silent endpoint | n/a | n/a | last event at the healthy endpoint 41 ms after the last POST (5.08 s with the per-endpoint cap removed) |
| burst of 40 events behind a slow endpoint, then 25 probe POSTs | n/a | probe p99 605 ms (2,413 ms without the turn budget H1c had) → 7 ms | n/a |

(The p99 of 77 to 80 ms is the baseline's own, 49 to 76 ms with no bad endpoint at all: it is one POST in 80 meeting a turn of the loop that was doing something else.) **The 50 ms median that section 15 fixed beforehand and the slow endpoint missed by a factor of five is now met by a factor of twenty, and the test gates on it.** Criterion 3 (a stalled receiver does not stall the others) is met for endpoints dialled by IP address.

**Tested.** The suites of H1c are unchanged and pass: the retry schedule across kills (gaps of 515, 1,045 and 2,004 ms for 500, 1,000 and 2,000), signatures (536 checks), the unit tests, ingest under power cuts (2,000 acknowledged events, none lost), and three endpoints under 101 kills (300 events, 18 poisoned events dead-lettered, the next event overtaking the poisoned one in 18 of 18 cases). New: `tests/attempt_test.py` drives the service with eight kinds of receiver (204; 500; accepts and never answers, recorded as failed after the 800 ms deadline, 0.89 s; closes without answering; junk; a status line split in two with a pause; nothing listening; a 60,000-byte body to a receiver reading slowly), and `tests/isolation_test.py` above.

**Mutants of the attempt machinery: eight, six killed.** No deadline sweep (killed by `attempt_test`); a deadline that never comes (the same test); a slot that is never closed (killed by the isolation test and by the delivery run, which runs out of slots); no per-endpoint cap (the burst behind a silent endpoint: 15 events missing, the last 5.08 s late); no in-flight flag, so an event is started again while one is in flight (killed by `retry_test`: 8 requests where 5 are expected, and by the delivery run); the in-flight count never decremented (the endpoint stops being served after 8 attempts). **Two survived, and what each is:** ignoring `conn_connect_status` is **equivalent** at what the tests can see (a refused connection goes on to the send, which fails, and an attempt that fails is recorded as failed either way; the status only changes which reason the code carries, and the outcome record does not keep it). **Assuming a write is whole is not equivalent and is not tested**: no test makes the kernel take part of a request. The 60,000-byte body to a slow reader did not do it, because this machine's send buffer takes 60 KB in one call; so the partial-write branch of `advance` has never run in a test, and is the one piece of this step that is unverified.

**Found along the way.**

* **A test receiver was dropping SYNs.** Python's `HTTPServer` listens with a backlog of 5; a burst of 8 connections lost one SYN and the sender waited TCP's one-second retransmit, which showed up as a *single* event arriving about 1,000 ms late in every run (938 to 958 ms in six). It looked like the service pacing itself. The arrival times told them apart (90 of 100 in 150 ms, one at 1.08 s); the receivers now listen with a backlog of 128.
* **Ingest refused any event over about 16 KiB** (`http.server`'s input buffer was 16,384 bytes, and the scratch buffer for the record 20 KiB), though the log accepts 65,536. Both now follow the log's limit; an event too large for the log answers `413` ("the event is too large"), not the generic `503`. A 60,004-byte body is accepted and delivered intact, a 65,504-byte one answers `413`.
* **The delivery test needed a bound.** `tests/delivery.py`'s healthy endpoint failed events at random, and five failures in a row (about 12% across 300 events) once dead-lettered a good event and failed the run; the endpoint now never fails one event more than three times, so a dead letter there can only be the service's fault.
* `release` is a builtin name in lex-sys and cannot be a function name (`attempt.finish` ends an attempt).

**Not built:** idempotency keys; `410 Gone`; jitter; endpoints and attempt history in Postgres; compaction of `delivery.seg`; replay; a resolver that does not block (a host *name* still stalls the loop for as long as `getaddrinfo` takes); TLS. **Not verified:** the partial-write branch (above); more than 64 attempts at once; name resolution under a slow resolver; Darwin.
