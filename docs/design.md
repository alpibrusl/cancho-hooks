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

## 17. What building the fifth step showed

H1e is idempotency on the way in (section 4): a client may send `Idempotency-Key`, and a second `POST /events` with the same key and the same event, within a window, answers the first one's answer and writes nothing.

**Where the key lives.** In the log, as section 3 said, and nowhere else. A keyed event's record has three pairs, `event` (first, so delivery and `GET /events/:id`, which read the first pair, are unchanged), `key`, and `t` (the Unix time in ms, eight bytes); an unkeyed record is exactly what it was. `src/idem.ls` is only an index of that: an open-addressing table of 131,072 slots over entries of six integers (event id, time, CRC-32C of the body, its length, where the key's bytes are, how long) and one byte arena, 65,536 keys at most. At start the service reads the events log once and rebuilds it, the later record winning when a key appears twice, as it does while running. Nothing else is persisted, so there is no second file that could disagree with the log, and an event the log lost in a crash takes its key with it.

**The flow.** Validate the event (`422`); read the header (one only, else `400`; 1 to 255 visible ASCII bytes, else `400`). If the key is held and fresh: same body (CRC-32C and length) is a repeat, answered `202 {"id":N}` **after the turn's flush**, the same path a new event takes, so a repeat in the same turn as the original, or of an original not yet flushed, never answers before the record is durable; another body is `422`. If the key is not held, or has expired, append the record, and only then insert or overwrite the index entry, so the index never holds a key the log does not. A new key when 65,536 are held is `507`: the index refuses, it never forgets, because a forgotten key would store twice the event it exists to protect. The body is compared by CRC-32C and length, not stored, which makes a collision (two different events of the same length and checksum under one key) accepted as a repeat; at 2^-32 per pair of events that share a key and a length, that is the price of 16 bytes an entry instead of the body.

**The window** is the optional fifth argument in ms (default one day). It is measured on the Unix clock (`clock_unix_ms`), from the time stored in the record, so it survives restarts and a key that expired before a kill stays expired after it. An expired key is not removed; the next event with it overwrites the entry (the count of keys does not grow).

**Tested** (`tests/idem_test.ls`, six unit tests of the index; `tests/idempotency_test.py`, eight stages, all reading the log with the independent reader of `chaos.py`):

* the contract: a repeat is byte-identical to the first answer, writes nothing, the same key with another event (also one of the same length) is `422` and disturbs nothing, unkeyed identical events are two events, keys are case-sensitive and the header name is not, the record is `event, key, t`;
* refusals: empty, 256 bytes, space, tab, non-ASCII, two keys: `400` and nothing stored; a bad event under a key leaves no key; an event the log refuses as too large (`413`) leaves none either;
* twenty simultaneous requests with one key make one event; 300 keys with four requests each, shuffled over eight threads, make 300;
* power cuts: 50 acknowledged keys survive a kill and answer as before; a key expired before a restart stays expired; after a rebuild the latest of two records under a key answers;
* a repeat is not a second delivery (a receiver that fails an event once sees five requests with one key as one event);
* chaos: 600 keyed events from six threads that retry (and, a third of the time, repeat after an answer) while the service is killed at random as power cuts: exactly one record per key, and every id a client was told is the one in the log;
* a log that cannot be written (`RLIMIT_FSIZE`): `503` for everything, held keys included, and after a restart every acknowledged key answers as before;
* `FULL=1`: 65,536 keyed events (18.8 s on 64 connections), the 65,537th key `507`, and a restart that rebuilds all of them in 0.24 s.

**Mutants: nineteen, eighteen killed, one equivalent.** Killed: no body comparison; no CRC; no length (the unit test); the window boundary; the index updated before the append (first survived, because the "too large" test sent 70 KB, which the server's input limit closes before the handler runs; the test now sends an event the log refuses and the mutant leaves a key behind); a rebuild where the first record wins; a rebuild that loses the time; the key limit one too high; the duplicate-header check removed; no `507`; a repeat answering the wrong id; the time not stored; the key not in the record; the window argument ignored; a key compared by prefix; an expired key added instead of overwritten; a repeat that appends anyway; the stats count wrong (first survived because no test had a failed attempt beside it; the delivery stage now has one). **Equivalent:** a check that the log is not broken before answering a repeat. A broken log fails the flush that every held request waits for, so the answer is `503` with or without it; the check was removed, and the `RLIMIT_FSIZE` stage is the test of the behaviour.

**Found along the way.**

* **The size limit changes with a key.** The log's limit is on the record, so an event is refused at 65,499 bytes unkeyed and at 65,216 to 65,468 with a key, by 28 bytes and the length of the key. The scratch buffer for the record was 256 bytes more than the log's limit, and the server accepts requests up to 4,096 bytes more than that; an event in between would have overrun it. It is now 8,192 more, so such an event reaches `log.append` and gets `413`.
* **A request over the server's input limit (about 69.6 KB) is answered by closing the connection**, not with `413`: the client sees a reset. That is `http-server`'s behaviour, not this service's, and a client library retrying on resets will retry it.
* `202` is sent with the reason phrase `Unknown`: `std.http` has none for it. Harmless, and a lex-sys change.

**Not built:** the index does not shrink (65,536 keys, ever, until compaction of the events log exists); the rebuild reads the whole events log at start (0.24 s for 65,537 records, linear); a client that repeats a key with a different event after the window gets a new event, by design. Replay and `410 Gone` (the next steps), endpoints and attempt history in Postgres, jitter, TLS are as in section 16.

## 18. What moving the dependencies into locks showed

Until here the build needed three clones and two SHAs: the compiler, `lexsys-log` (its four `src/*.ls`, named by relative path) and lex-sys's `packages/http-server/server.ls` (read out of the compiler's own checkout). lex-sys's `docs/package-system.md` section 7 built what replaces that, and this is the first program to use it.

**What changed.** `deps/log.lock` pins a commit of `lexsys-log` and `deps/server.lock` a commit of lex-sys, each with `--all` of the store's declarations; `scripts/build.sh` runs `lex-sys vcs fetch` on each (the commit is fetched into `~/.cache/lex-sys/git/<commit>/` once, every pin is re-parsed, re-typechecked and re-hashed, and the sources land in `build/deps/<hash>.ls`) and passes them to `build`. `scripts/lock.sh <log commit> <lex-sys commit>` moves the pins. CI no longer checks out `lexsys-log` and drops `LOG_REV`; its one pin is the compiler's. `LOG_STORE` and `SERVER_STORE` point `build.sh` at a local store when a dependency is being changed.

**Checked.** Every suite of sections 13 to 17 passes on a binary built this way: the three unit suites, signatures (536 checks), the receiver kinds, the retry schedule, isolation, chaos (2,000 events under power cuts, none lost), delivery (300 events, three endpoints, 101 kills), and idempotency including the 65,536-key stage. The tests that used `../lexsys-log/src/record.ls` now use `build/deps/*.ls`.

**Found.**

* **A fetched file is named by its hash, so an old one is a second declaration.** Moving a pin leaves the previous commit's files in `build/deps` next to the new ones and `build` refuses the duplicates; `build.sh` clears the directory first. (lex-sys's own `fetch_net_dependencies` test helper says the same about two fetches of one package.)
* **The pin on `lexsys-log` had to move once, after the merge.** It was first the head of the pull request that added the published stores; a squash merge makes a new commit with the same tree, so `scripts/lock.sh` was run again with the merge commit (`6b4f46f`) and the diff of `deps/log.lock` was that one `rev` line, as predicted: the entries inside do not change, only where they are fetched from.
* **Still two hand-kept lists:** the source files `build.sh` names (`src/*.ls`), and the test commands. A project file (lex-sys section 7.5, step 3) would hold both.

## 19. What moving the dependencies into the project file showed

Section 18 put the two libraries in lock files and a script; lex-sys section 8 replaces both with `lex-sys.toml`, and this is the second program on it.

**What changed.** `lex-sys.toml` holds the compiler the sources were written for (`[package] lex-sys`, a commit), the two libraries (`[dependencies.log]` and `[dependencies.server]`: repository, a full commit hash, the store inside it) and the two programs to build (`hooks` from `src/`, `sign_probe` from `tests/sign_probe.ls` and `src/sign.ls`). `deps/` and `scripts/lock.sh` are gone. `scripts/build.sh` is `lex-sys build` and the `fsync` shim the crash tests preload. The CI workflow reads the compiler's commit out of `lex-sys.toml` to know which lex-sys to build, so there is one place to change it, and the compiler checks it again: `lex-sys build` refuses any other.

**Checked.** Every suite of sections 13 to 17 on a binary built this way, locally; the table is in the pull request. A cold `lex-sys build` (the two libraries fetched from GitHub, then compiled) took 5.7 s.

**Found.** The compiler pin cannot name the commit that contains the project file before that commit exists, so the first version of this file named a build of the branch, and it was moved to the merge commit (`1200968`) once there was one. A pin that moves is the cost of pinning what you also change.

## 20. Settings

*Unlike the earlier sections this was written down after the code, in the same change: the rule was decided first (in the conversation that asked for it), the gates below were written with the tests and not before them, and the mutants were run once the tests passed. Read the gates as a description of what the tests check, not as a pre-registration.*

**What asked for it.** The service takes `hooks <port> <dir> [<schedule> [<deadline-ms> [<window-ms>]]]`. Each new setting so far (sections 13 to 17) was one more positional argument: to set the idempotency window you must also give a schedule and a deadline, a person reading `build/hooks 8080 /d 100,200 500 60000` has to know the order, and nothing says which of them a deployment chose. The question was whether an app like this keeps its parameters in the command line or in a file. Both, and the point of this section is to say which wins.

**What is a setting, and what is not.** Five things are: `port`, `dir`, `schedule`, `deadline-ms`, `window-ms`. The **endpoints are not**: they are data (an id, a host, a port, a secret each), they will be rows in Postgres (section 3), and they hold secrets, so they stay in `endpoints.conf` in the data directory, a file with its own permissions that a settings file or a process listing never has to contain. Nothing in the settings is a secret, which is why `GET /config` can print them.

**The rule.** Three sources, **the last one that names a setting wins**: the defaults, then the file given by `--config <path>`, then the flags in the order written. The same function judges a value whichever source it came from, so a port is 1 to 65535 in a file and on the command line alike. `--config` can stand anywhere among the flags; a second one replaces the first (the files are not merged: a file is a complete statement of what it says, and merging two would make the result depend on a history nobody can read off a command line). There is **no default file**: reading `hooks.conf` from the working directory because it happens to be there would make the service's behaviour depend on where it was started.

**No environment variables, no positional arguments.** Environment variables are the third place a value can hide, and nothing here has asked for one; the day a container platform does, it is one more source between the file and the flags and the rule above does not change. Positional arguments are **refused**, not kept for compatibility: two ways to say the same thing is how the order comes back, and the only users are this repository's tests, which move in the same change.

**A refusal never starts the service.** An unknown setting, a value a setting does not take, a flag with no value, a file that cannot be read, is 16 KiB or more, or has a line that is not `key = value`: exit 2, one line on stderr that names the argument or the file and its line, before anything is listened on or written to the data directory. A typo in a setting that silently took its default is worse than a service that does not start.

**What it does not do.** No reload on a signal: a changed file takes a restart, which the service is built to survive (section 4). No `include`, no sections, no quoting (a directory with a space in it needs a flag with a space in it; the file's value is the rest of the line). No secrets from files or the environment (the secret in `endpoints.conf` is what exists today).

**What the tests check.** (1) each of the five settings from a file and from flags ends up as the service reports it, and the directory is the one named; (2) precedence: a flag beats the file, a setting the flags do not name keeps the file's, `--config` anywhere gives the same result, a later flag or line beats an earlier one, a second `--config` replaces the first; (3) what is not set is the default, and the endpoints file's secret is not in `GET /config`; (4) every refusal above exits 2, says which argument or line, does not listen and leaves the data directory empty; (5) every suite of sections 13 to 17 passes unchanged but for how it starts the service; (6) each of a list of mutants of the new code is killed by a test.

**What building it showed.** `src/config.ls` is 227 lines, `main` in `src/hooks.ls` went from a hand-copy of five positional arguments to the three sources, and `GET /config` is new (it is how the tests read back what is in force, which an exit code and a directory listing cannot say). 7 unit tests (`tests/config_test.ls`) and 25 end-to-end checks (`tests/config_test.py`): all five settings from a file and from flags, the five precedence cases, the defaults, no secret in `/config`, and 13 refusals. Every other suite passes with the service started by flags: the retry schedule (gaps 0.203, 0.404, 0.805 and 1.608 s for 0.2, 0.4, 0.8 and 1.6), the isolation test, 600 events under 111 kills, three endpoints under 100 kills, idempotency with `FULL=1`, the 536 signature checks, and the other unit tests.

**Mutants of the new code: fourteen, fourteen killed.** In `config.ls`: the port ceiling off by one; thirteen digits allowed; `#` not a comment; `=` not a separator; the value's last byte lost; `window-ms` written to the deadline; `--key=value` not recognised. In `main`: flags never applied; the file never read; the file read after the flags (the precedence reversed); the check for a missing `--port`/`--dir` removed; an error naming the value, not the flag, which it does when the index is taken after the value is consumed (`--prot 1` said ``1` is not a setting``, and the first version of the code did exactly that: found by running it by hand, then by the test); the file size limit off by one; `--config`'s own value not read in pass 0. Six of them are killed by the unit tests as well, eight only by the end-to-end test: the wiring in `main` has no unit test, and cannot have one without a way to call `main`.

**Found along the way.**

* **`config.port(...)` did not compile inside `main`, where `port` is a local**: the checker took `config.port` for a field of the local. Not a bug in lex-sys (a local does shadow a module qualifier); the accessor is `port_of`.
* **The handler is given a 16-cell slice of the delivery state**, which is why the first `GET /config` trapped on an index past it: the schedule lives at 128. The slice is wider now.
* **The first test of "a bad window" expected the flag's name and got the whole argument** (`--window-ms=-1`): the message quotes what was typed, which is the better behaviour; the test changed, not the message.
* **Not tested:** the effect of a file whose value is a number larger than 12 digits only through the unit test; `--dir` with a space; a directory that does not exist (refused later, by the log, with the existing status 10, not as a setting).

## 21. What `lex-sys test` in a project showed

The four unit-test commands (CI and the README spelled them out, with `build/deps/*.ls` to name the libraries) are four `[[test]]` sets in `lex-sys.toml`, and the compiler pin moves to the merge commit of lex-sys #194, which has them. `lex-sys test` runs all four against the installed libraries (9, 2, 6 and 7 tests) and `lex-sys test --test idem` one. Run here with `build/deps` removed first, so the project, not a left-over directory, supplies the libraries. The CI step is one line. Nothing else changed.

## 22. Disabling an endpoint

*Written with the code, like section 20: the rule was decided first, the checks were written with the tests.*

**What asked for it.** Section 4 says `410 Gone` disables the endpoint. Until now a `410` was a failure like any other: the event was retried nine times over a day against a receiver that had said, in the clearest status there is, that it was never coming back.

**The rule.**

* A `410` from an endpoint makes **that event a dead letter at once** (it will not be delivered there, and retrying cannot change that) and **disables the endpoint**: it gets no new attempts. Attempts already in flight finish and are recorded as usual.
* A disabled endpoint's later events are not failed and not dead: they wait, uncounted, exactly where they are, so its cursor stays behind them. The window (section 15) is what bounds this: more than 1,024 events behind, the endpoint is not served at all, as for any endpoint that far behind. Nothing is dropped.
* **A person enables it again**: `POST /endpoints/:id/enable`. The events that waited are then delivered, in the window's order. The event that got the `410` stays dead; putting it back is replay (not built). Enabling an endpoint that is enabled answers `200` and writes nothing.
* No other status disables anything: a `404` or a `301` is a failure, with the schedule. Only `410`.
* `GET /endpoints` lists each endpoint's id, port, cursor and `disabled`. It does not list the host, and never the secret.

**How it survives a restart.** Two new kinds of record in `delivery.seg`, `disabled` and `enabled` (4 and 5), with the endpoint's id and nothing else. Recovery replays them in order into one bit per endpoint; `state.apply` ignores them (it used to treat any kind that was not `failed` as final, so it now says which kinds it means). A crash between the `dead` record and the `disabled` record leaves the event dead and the endpoint enabled: the next event is tried, gets its `410`, and the endpoint is disabled then (reasoned, not tested: no test kills the service in that gap). Enabling flushes before it answers; a failed flush is a `503` and the endpoint stays disabled (also not tested: no test breaks the delivery log).

**What it does not do.** No automatic re-enabling, no disabling by a person (only by the receiver), no per-endpoint reason or time recorded. Endpoints still come from `endpoints.conf`, which a restart re-reads: removing a line removes the endpoint, and its records are then ignored.

**What the tests check.** `tests/gone_test.py`: a `404` retries and disables nothing; a `410` on a retry disables, dead-letters the event at once, and leaves the other endpoint alone; later events reach only the other endpoint, and the disabled receiver sees nothing for longer than an attempt takes; a restart keeps it disabled; `enable` delivers the events that waited and not the dead one, and a restart keeps it enabled with nothing repeated; the refusals (`404` unknown, `400` out of range, `405` on `GET`); the endpoints are told apart (a non-zero id, both disabled, enabling one of two leaves the other, across a restart); an enable of an enabled endpoint writes no record. `tests/state_test.ls`: the two kinds read back and change no cell.

**Mutants: fifteen, fifteen killed.** The 410 rule removed; the disable not written; attempts not skipped for a disabled endpoint; recovery ignoring the records, or reading them inverted; enable not clearing the bit, not writing its record, writing one when there is nothing to enable, accepting an unknown endpoint; `/endpoints` always saying enabled; the bit always bit 0; the clear clearing every endpoint; the disable bit set without the record; `apply` treating the new kinds as final; the new kinds not read back. **Three survived the first tests, and each was a test that checked less than it said:** an enable of an enabled endpoint was claimed to write nothing and nothing looked; every disabled endpoint in the test was id 0, so a bit mask that only ever used bit 0 passed; and clearing every endpoint's bit when enabling one passed because the other endpoint, cleared, was disabled again a few milliseconds later by the `410` it still answered (the receivers now answer `204` before the enable, so a wrongly enabled endpoint stays enabled and shows).

## 23. Replay

*Written with the code, like sections 20 and 22.*

**What asked for it.** Section 4: "Dead letters stay in the log and are visible by id; `POST /events/{id}/replay` puts one back." A receiver was down for a day, the schedule ran out, the event is a dead letter; the receiver is fixed; someone needs to send it again.

**Why it is not "reopen the cell".** An event that is final at an endpoint is below that endpoint's cursor, and the cursor is the only record of it (section 15): the cells above it are a ring of 1,024, indexed by `id % 1024`, and the cell of an old event is the cell of the one 1,024 above it, which may be in use. Lowering the cursor to reopen an event would either redeliver everything between it and the old cursor or collide with live cells. A replay is therefore **not** a state change of the window. It is a separate, small thing next to it.

**The rule.**

* `POST /events/:id/replay` sends event `:id` again to **every** endpoint; `POST /events/:id/replay/:endpoint` to one. Whatever happened to the event there before (delivered, dead, never reached) does not matter. `404` for an event or an endpoint that does not exist, `400` for an id that is not a number (or an endpoint of 16 or more), `202 {"event":N,"endpoints":[...]}` otherwise, **after** the replay is stored.
* A replay is an attempt like any other, from the same machinery: the same signature scheme with a fresh timestamp, **the same `webhook-id` (`evt_<id>`)**, the same deadline, the same retry schedule (a failure waits for the next delay, and when the schedule runs out the replay is a dead letter), a `2xx` delivers, a `410` kills it and disables the endpoint (section 22). A receiver that remembers `webhook-id`s will drop a replay of an event it already processed: that is what the id is for, and it is why a replay may be sent to an endpoint that already has the event.
* **At most 32 replays are waiting** (not yet delivered or dead). The request that would exceed it is a `507` and stores nothing. Asking for a replay that is already waiting is not a new one: it starts that one over (zero attempts, due now).
* A replay for a disabled endpoint waits, and is attempted when the endpoint is enabled. It shares the endpoint's cap of 8 attempts in flight with the window's attempts and takes from the same per-turn budget, after the window's.
* Replays and ordinary events do not wait for each other.
* The outcomes count in `/stats` (`attempts`, `delivered`, `failed`, `dead`), and `replays` says how many are waiting.

**How it survives a restart.** Four more kinds of record in `delivery.seg`: a replay was asked for (endpoint, event), and its attempts ended failed (with the count and the time of the next), delivered, or dead (kinds 6 to 9). Recovery replays them in order into the table of waiting replays; `state.apply` ignores them. The event's place in the events log is **not** stored: it is looked up when the replay is first attempted (a scan from the start of the log) and kept in memory. Records for a replay are appended and flushed before the request is answered, and the table is changed only after the flush.

**What it does not do.** No replay of a range, no replay by a filter (all dead letters), no list of dead letters (`GET /events/:id` still shows the event, not where it stands), no way to cancel a waiting replay. The events log is never compacted, so every event can be found; when it is, the look-up must say "gone".

**What the tests check** (`tests/replay_test.py`): a dead letter at A is replayed to A alone and arrives with the same `webhook-id` while B sees nothing; a replay to all; a later event's replay carries that event's own body (not the first record's); a failing replay is tried on the schedule and then dead; a replay answered `410` is dead and disables the endpoint; the refusals and that they start nothing; a replay pending across a kill is delivered once, after the time it was given and not before, and two restarts after it repeat nothing; asking again starts a waiting one over; a replay waits for a disabled endpoint and goes when it is enabled; 32 are taken and the 33rd is a `507`; an event 1,129 behind the cursor is replayed; events posted while replays wait are delivered at once. `tests/state_test.ls`: the four kinds read back and change no cell.

**Mutants: 23 run, 21 killed, 2 survive, both defensive branches.** (One more was written as a no-op by mistake and is not counted.) Killed, among others: the replay outcome not routed to the replay path; the attempt's id without the replay marker; the replay loop never run; ignoring a disabled endpoint, and ignoring the time of the next attempt (in two ways); no capacity check; the request record not written, and recovery not applying it, or not restoring the count and the next time, or not freeing a finished one; the schedule never running out; no disabling on `410`; the endpoint filter ignored (at two places); the delivered counter; `/stats` always saying 0 replays; the log look-up returning offset 0 (it survived until a test replayed a later event); the reset of a waiting one (survived until a test asked twice). **Survivors:** the defensive check that the event read at the stored offset is the one asked for, and the branch for an event whose look-up found nothing when the first attempt starts. Both guard a log that changed under the service; no test corrupts the log, so both are untested, and said so here.

## 24. PostgreSQL

*Written with the first slice, not before it; the slices after it are a plan, and each will add its own results here.*

**What asked for it.** Section 3 puts three kinds of fact in three places: events in the log, delivery state in the log of outcomes, and **endpoints and the history of attempts in PostgreSQL**. Until now the endpoints were a file and the history did not exist, which is why a human could not ask "what happened to event 41 at endpoint 3, and when". The rule of section 3 stands and decides every choice below: **the log files are the truth about delivery and the database is for people and the API.** Delivery must never wait for it, and must not stop when it is gone.

**The slices.**

* **C3a, the history, written (this section).** Every attempt that ends is a row in `attempts`.
* **C3b, the history, read (built, below):** `GET /events/:id/attempts`, answered from the database through the same pool, the request held until PostgreSQL answers.
* **C3c, endpoints from the database (built, below):** `endpoints` is a table; with a database, the service loads it at start instead of `endpoints.conf` (the file stays the way to run without a database), and a command imports the file.
* **C3d, endpoint management:** `POST /endpoints` and the rest, taking effect without a restart. This is the one that changes the delivery state's fixed tables and needs the most care: it comes last.

**C3a: the rule.**

* Settings (section 20): `pg-host` (without it there is **no history and nothing changes**), `pg-port` (5432), `pg-user` and `pg-database` (both `hooks`), `pg-password` (none). A password on a command line is visible to every user of the machine: put it in the settings file, with the permissions of a secret.
* The service opens **two** connections at start, logs in (SCRAM-SHA-256, as `lexsys-pg` does), prepares its one statement, and puts them in a `pg.pool` whose connections sit in the service's own poller, under tokens above the delivery attempts'. If it cannot, it **starts anyway** and says so on stderr (`the database: N of 2 connections opened`): delivery is the product. (Since C3c that holds only for the *history*: the endpoints are read from the same database before this, and a database that cannot be read then is a refusal to start.) The start blocks on the connect and the login, so a host that does not answer at all delays the start by the kernel's connect timeout.
* An attempt that ends **pushes a row onto a ring in memory** (256 rows): no effect, no waiting, in the same call that records the outcome in the log. Once a turn the ring is turned into requests on the pool (at most 64), and one write goes out per connection. A row that the pool has no room for waits in the ring; a row the pool cannot take at all (no live connection) is dropped; a full ring drops the new row. `/stats` says `history_live`, `history_written`, `history_failed` (the database answered with an error, or the connection was lost with the request on it) and `history_dropped`.
* **A lost connection is not reopened.** `lexsys-pg`'s pool has no reconnect, and a reconnect needs a login that does not block the loop. A service that lost its database goes on delivering and drops rows until it is restarted.
* **The history is best effort and can have holes**: rows in the ring or on the wire when the service is killed are lost, and the log does not have what the database has (the receiver's status, the latency). It is never *wrong*: a row is keyed by (endpoint, event, replay, attempt) and written with `on conflict do nothing`, so a repeat after a crash changes nothing.
* The row: the endpoint, the event, whether it was a replay, the attempt's number, its outcome (`1` delivered, `2` failed and will be tried again, `3` dead), the receiver's HTTP status or a negative reason (`-1` could not connect, `-2` could not send, `-3` timed out, `-4` no answer), when it ended (Unix ms) and how long it took. The table is `sql/schema.sql`, which the service does not apply; the query is `sql/queries.sql`, and `src/queries.ls` is what `pgen` makes of it (committed, as in `lexsys-web`).

**What the tests check** (`tests/history_test.py`, against a real PostgreSQL; SCRAM when `HOOKS_PG_PASSWORD` is set): four endpoints (one answering 500, one 204, one not listening, one that never answers): every attempt is a row, with the right outcome and status, the right reasons (`-1`, `-3` after about the deadline), a latency that is small for a local receiver and about 800 ms for the stalled one, a time that is the clock's, and **as many rows as the service counted attempts**; both connections live; a replay's attempts are their own rows; a database that is not there at start (the service starts and says so, delivers, counts the rows as dropped); a wrong password is refused; **a database that is up and never answers** (a proxy that reads and forwards nothing): 600 more events are all delivered at the speed of the first 100 (within four times per event), the ring fills and rows are dropped and counted; a database that is **cut** in the middle: delivery goes on and the rows are counted; no database named: no counter moves; an SQL error (the table renamed away): ten rows counted as failed, delivery unaffected, writing resumes when the table is back on the same connections; the defaults (user and database `hooks`, seen from the server's side in `pg_stat_activity`).

**Mutants of the new code: 19 run, 16 killed, 3 survive.** Killed, among others: the ring never overflowing, a row it cannot take not counted as dropped, an SQL error not counted as a failure, `live` not kept, the latency, the replay flag, the attempt number, the pool's flush, its answers, its events, and the defaults of the user and the database. Three survive, and none is a gap in the tests: starting the pool's tokens at the attempts' own, and passing `live` as 0 at start, are behaviourally equivalent here (a spurious `pump` or `advance` on a non-blocking connection finds nothing to do; `settle` sets `live` every turn); and the default password `-` survives under a server that trusts the connection, since it accepts any (it is exercised only by the password test, which sends one). (A first mutant of the guard on `enabled` was written wrongly and is not counted; it was redone as removing the guard and killed.)

**Found along the way.**

* **`listening` was printed before the database was tried**, so a harness that waits for it could ask `/stats` before the connections existed. It is printed after now, which is also the better meaning of the word.
* **`state.apply` and `outcome_at` know nine kinds of record now**, and a test that used 9 as "a kind that does not exist" had to learn that (it uses 99).

**C3b: reading the history back.**

* `GET /events/:id/attempts` answers the rows of one event as a JSON array, in order of endpoint, replay (a replay's after the first run's) and attempt: `{"endpoint","replay","attempt","outcome":"delivered"|"failed"|"dead","status","at","latency_ms"}`. At most 200 rows (the query's limit; an event cannot reach it: 16 endpoints, ten attempts, and the replays are capped at 32).
* **The request does not wait in the loop.** The handler says only that a query is wanted; `run` queues it on the pool under a tag of its own (100 and up; an insert is tag 1), **holds** the connection (`server.hold`), and answers it when the pool has the reply (`server.answer`). Up to 64 requests wait at once; the 65th is a `503` at once, as is a request when the pool is full or has no live connection. A request the database has not answered in **five seconds** is a `504` and its slot is given back: a late answer finds no slot and is dropped (a tag is never reused, so it cannot be mistaken for a newer request's).
* `404` for an event that does not exist (its id above the last in the log: ids are dense from 1, so no scan), `400` for an id that is not a positive number, `503` with a reason when no database is named, when the pool cannot take it, and when the database answers with an error or the connection it was on is lost. An event that exists and has no rows (no endpoints, or not tried yet) is `[]`.
* The event's own existence is read from the log, the rows from the database: what the database lacks (a row lost with the service) is simply not in the list, which is the same hole section C3a states.

**What the tests check** (the same file, stages 10 and 11): the exact rows of an event tried by four endpoints and replayed to one (order, outcomes, statuses, a time and a latency on each, the stalled one's latency about the deadline), equal row for row to what `psql` reads; event 2's answer holds event 2's rows only; two requests on one kept-alive connection; **50 requests at once**, each answered with the rows of its own event (two events interleaved); 150 requests one after another (slots are given back); `Connection: close` honoured and keep-alive kept; 404, 400, `[]`, and the 503 that says no database is named; a database that **never answers**: 64 requests wait and are a 504 after about five seconds and the other 6 are a 503 at once, and the next request is **accepted again** (the slots were given back); a database that was cut, and an SQL error: a 503 each, and the same connections answer again when the table is back.

**Mutants of this slice: 13 run, 12 killed, 1 survives and is equivalent.** Killed: the 404 guard moved out of reach, the "no database" guard removed, the slot limit (3 instead of 64), the slot not freed after a timeout, a tag that is never advanced (answers go to the wrong request), the keep-alive flag always true, the deadline never reached or one second, the replay flag always false, a database error not turned into a 503, the outcome names confused, an answer for a request with a query tag treated as an insert. The one that survives frees a slot by zeroing its *deadline* instead of its *tag*: the next sweep then times it out and frees it (the held connection's ticket was answered already, so that second answer is refused and harmless), which is the same observable behaviour one turn later. The "150 in a row" test was written because I expected a mutant that leaks slots to survive; the "no database" check first asserted only the status code, which the pool's refusal also gives, and now reads the message.

**Found along the way.** My test proxy gave the connection to the database a five-second timeout meant for connecting, so after five idle seconds it hung up on the service, which then showed as a lost connection (`status 1`, `history_live 1`) and an instant 503 for the next request. Not the service's fault, and a useful accident: it is the test of the pool's answer to a connection lost with requests in flight, which the pool turned into a 503 with nothing lost or repeated. The proxy now only times out the connect.

## C3c: the endpoints in the database

*Written after the code, from what it does and what the tests found.*

**The rule.** With `pg-host` the endpoints are the rows of `endpoints (id, host, port, secret)` (`sql/schema.sql`), read **once, at start, before the logs are opened and before the service listens**. `endpoints.conf` is not read: two lists would make "who gets this event" depend on which one a person edited last, and the point of the table is that there is one. Without `pg-host` nothing changed.

**It is not best effort, and so it does not start without it.** The history may have holes; the endpoints may not, because an event for an endpoint the service does not know is simply not delivered to it, and `endpoints.conf`'s old list is as wrong as no list. A database that cannot be reached, a login that fails, a table that is not there, a query that fails when it runs, or a row the service cannot use is a **refusal to start** with a message and a status: `20` (the database: cannot connect, cannot log in, the query failed, a row with an empty field or a byte that is not printable, or a table over the 16 KiB it is read into) or `13` (a row that `endpoints.parse` refuses: an id of 16 or more, a secret that is not `whsec_` and base64; the message names the row). An **empty table is not a refusal**: the service starts with no endpoints and keeps the events it is given, as it always did with no `endpoints.conf`. The start **waits while the database is silent**: the blocking client has no timeout, so a database that accepts and never answers holds the start until it is killed (a test says so). That is the cost of reading the list before listening; a timeout is a change to `lexsys-pg`.

**One parser.** The table is written into a buffer as the same text `endpoints.conf` is (`id host port secret`, a line a row) and given to `endpoints.parse`, so a row is judged by the rule that judges a line of the file: nothing about "what is a valid endpoint" exists twice. Between the database and the parser each field must be non-empty and printable (a byte of 33 to 126), because a newline in a host would otherwise be a second line, and so a second endpoint.

**Importing.** `--import-endpoints 1` (a setting, so also a line in the settings file) reads `endpoints.conf` of `--dir`, checks it with `endpoints.parse`, inserts every line in one transaction (`on conflict do nothing`) and exits, saying `imported A of N endpoints`. A line whose id is in the table already is **left as it is, not updated**: a command that silently rewrote a receiver's host or secret would be the worse failure, and changing a row is C3d's. A file with a bad line imports none of it (status 13); a database that refuses a row part-way adds nothing (status 20). It needs no `--port`.

**The secret is in the table, in the clear.** The service signs with the key a `whsec_...` secret decodes to, so it has to be able to read it back; a hash would not do. Anyone who can read the table can sign as the service, and the schema file says so. This is the same exposure as `endpoints.conf` has, moved to the database's permissions.

**What the tests check** (`tests/roster_test.py`, against a real PostgreSQL, SCRAM when `HOOKS_PG_PASSWORD` is set; 33 checks): the import (every row as written, a second import adds none, a changed line changes nothing, a bad line imports none of the file, no file, no host, no database, a trigger that refuses the second line leaves the table empty and the same file imports whole without it, `0` or `1` only, no port needed); the service delivering to the table's endpoints and **not** to a different one in the file, each signed with its **own secret from the table** (the receiver verifies the Standard Webhooks signature itself); a row removed and a restart (it is no longer delivered to); an empty table; a secret that is not base64, an id of 20, a host with a space, an empty host and 201 rows of 250-byte hosts (each refused with its own message, the last not a crash); no table, a view that prepares and then fails when read, no database, a wrong password; the file as before without `pg-host`; and a database that is silent. The history suite changed with it: a database that is not there at start is now a refusal (it was "starts, drops rows"), and its helper fills the table instead of the file.

**Mutants of the new code: 19 run, 15 killed, 4 survive.** Killed, among others: a space or an empty field allowed through, no overflow guard on the buffer, every row counted as added, no transaction around the import, a missing table reported as a failed login, a failed query read as an empty table, the length of the text lost, the table never used, the row number wrong or the status of a bad row, no import without a port, the import flag never set. Four survive and none is a gap in the tests: the import's own check of the SQL error in the reply is redundant with the status the client already gives (a failed statement answers a nonzero status), a `commit` after a failed statement is a rollback in PostgreSQL, so never sending `rollback` changes nothing, the default password `-` is exercised only under a server that trusts the connection (as in section C3a), and accepting a seven-digit id changes nothing because the parser then refuses any id of 16 or more.

**Found along the way.**

* **`history.login` answered 6 for a table that was not there, and `roster` called it "cannot log in".** Preparing the statements is part of the login there, so a missing `endpoints` table looked like a wrong password. It answers a status of its own now (`prepare_failed`, 16) and the message says the query failed. A side effect: **every** user of the database now needs both tables, `attempts` and `endpoints`, even one that only wants the history, because all four statements are prepared on every connection; `sql/schema.sql` makes both.
* **`import` is a keyword in lex-sys**, and so is `connect` (a builtin): the module's functions are `copy_in` and `open_db`.
* **My first test of "a query that fails when it runs" did not test that.** It used a view that raises when selected, which could not be inserted into, so the *prepare* of `add_endpoint` failed and the service refused for that reason; the mutant that ignores a failed reply survived. An updatable view whose filter raises prepares and then fails, and kills it.
* **A test raced itself**: the stage with no database reused a directory that held an event from the stage before, so the file's endpoint was sent two events and `wait_for` returned at the first. It uses a fresh directory.
* **The service now needs the database before it listens**, which changes what `listening` on stderr means for a harness: it still means the service is up, but the start can take as long as the database takes to answer.

**What is next.** C3d: `POST /endpoints` and the rest, taking effect without a restart. That is the slice that has to change a delivery state of fixed tables while it runs (the table is six integers an endpoint, sized at start), and it needs a design of its own before any code.

## 25. C3d: managing endpoints without a restart (design, written before any code)

*Nothing in this section is built. It is the design, the gates and the questions, so that they are fixed before the code and the code can be held to them. Where a decision is marked **to confirm** it changes behaviour a person may rely on.*

**What is wanted.** `POST /endpoints` (register a URL and the event types it wants; the answer carries the secret, once), `GET /endpoints/:id`, `PATCH /endpoints/:id`, `DELETE /endpoints/:id`, taking effect **without a restart**, with the database as the owner of the configuration (section 3) and the log as the owner of delivery state. (Event types: section 5 says an endpoint "wants" some; nothing in the service filters by type yet, so **filtering is not in this slice** and the column is not added.)

**What the code does today that this has to be built on** (read from the source, not assumed):

1. **State is keyed by the endpoint's number**, 0 to 15: the cursor `cur[e]`, a window of 1,024 cells for each number, `flying[e]`, one bit in `c_disabled`. The outcome log records the number, and the history table's key is `(endpoint, event, replay, attempt)`. The number is both the endpoint's *identity* and its *slot*.
2. **The table of endpoints** (host, port, key) is positional, six integers an endpoint, with the host and the key in one 16 KiB blob that is filled once at start. There is no way to remove or grow an entry.
3. **A new endpoint starts at event 0**: its cursor is 0, so it is sent every event still in the log (and an endpoint more than 1,024 events behind is not served until it catches up). That is right for the file, where "the endpoint was always there", and wrong for an endpoint created today.
4. **`request_for` takes the key when an attempt starts**, so a changed key is used by the next attempt and not by one already on the wire.
5. **Nothing in the service is authenticated.** Every route can be called by anyone who can reach the port.

**Decisions.**

**D1. Identity and slot are different things (to confirm).** Reusing a number after a delete makes three things wrong at once: the old endpoint's outcome records in the log replay into the new one's state, its rows in `attempts` collide with the new one's (`on conflict do nothing` would silently drop the new one's), and a late attempt of the old endpoint could be recorded against the new one. So: an endpoint has an **id** (a database sequence, a `bigint`, never reused, what the API and the history use) and a **slot** (0 to `max_endpoints() - 1`, what the state arrays use, reused). The mapping slot → id is **in the log**: a record `endpoint created (slot, id, start cursor)` and `endpoint removed (slot)`, replayed in order at start, so a slot's outcome records belong to the endpoint created before them. `max_endpoints()` rises from 16 to **62** (the disabled set is one integer's bits; the cells for 62 slots are 62 × 1,024 × 3 × 8 bytes = 1.5 MB). The history's `endpoint` column becomes the id (`bigint`); the rows already written are of ids 0 to 15, which is what the importer will make the first ids, so nothing is rewritten.

**D2. Where a new endpoint starts (to confirm).** An endpoint created through the API starts **from now**: its start cursor is the last event id when it is created, carried in the `created` record, so it gets events from then on and not the log's past. The request may say `"from": "start"` to be sent the log's backlog (limited by the window: it is served as it catches up, as today). *An endpoint that is in the table at start with no `created` record* (the rows C3c imports, or a row somebody inserted with `psql`) **keeps today's behaviour and starts at 0**, and a record is written for it, so that the next start does not do it again. That keeps C3c's meaning and makes the new behaviour opt-out only for the API, which is the one place a person has said "create".

**D3. The two stores, and the order that makes a crash harmless.** A change is: (1) the database transaction (insert, update or delete the row), then (2) on its commit, the log record and the change to the tables in memory, answered after the record is flushed. A crash between 1 and 2 leaves the row without a record, which is exactly the state D2 describes for a row from `psql`: it is found at the next start and handled by the same rule, **there is no second recovery path**. For a delete the order is the same and the crash leaves a slot with a `created` record whose row is gone: at start, a slot whose id is not in the table is **removed** (a `removed` record is written), again by a rule that already has to exist. The database is the owner; the log never invents an endpoint.

**D4. One change at a time.** A change holds the connection (`server.hold`, as `GET /events/:id/attempts` does) while its transaction runs on the pool; a second change while one is waiting is `409`. A change the database does not answer in five seconds is a `504` and the transaction is **not** assumed to have failed: the next start reconciles (D3) and `GET /endpoints/:id` says what the service believes. The service never applies a change to memory before the database has said commit.

**D5. What a change does to what is in flight.**
* *Create*: nothing to wait for. The slot is zeroed (`cur` set to the start cursor, its cells cleared), the table entry appended, `c_endpoints` raised.
* *Patch of host or port*: the next attempt uses it; an attempt on the wire finishes against the old address. The blob entry is rewritten in place if it fits, else appended; the blob is compacted when it is full (the entries are short and few, so this is a copy, done between turns).
* *Patch of the secret*: replaces it, and the next attempt, **including a retry of an event first attempted under the old secret**, is signed with the new one. A receiver that has not been given the new secret refuses until it has. Standard Webhooks lets a message carry **two** signatures for exactly this rotation (`v1,a v1,b`); sending both for a fixed period is the better design and is **not in this slice** (it needs the old key kept, a time, and a test against the reference library's multi-signature verification). The API says so rather than hiding it.
* *Delete*: **no new attempt** is started for the slot from the moment it is applied; attempts on the wire are allowed to finish and their outcome is recorded (and written to the history) as for any endpoint; waiting replays for the slot are dropped, each with a record; the cells and the cursor are discarded when `flying[slot]` is 0 and the slot is only then reusable (a `removed` record is written at that moment, not at the request). Rows in `attempts` are kept: they are for people.

**D6. The secret.** `POST /endpoints` generates it (`whsec_` and the base64 of 24 bytes from `/dev/urandom`, the nonce's source) unless the request names one; it is in the `201` once and **no read returns it again** (`GET` answers the id, the address, whether it is disabled and the cursor, never the secret). It is stored in the clear in the table, for the reason C3c gave.

**D7. Authority (to confirm, and the part most likely to be wrong).** An endpoint is an address the service will `POST` to, chosen by whoever can call this API: with no authentication that is a request-forgery tool for anything the service can reach, and secrets are returned on create. So **every route that changes an endpoint requires a bearer token, `--admin-token` (a setting, so a line in the settings file, not a flag by habit), and with no token set they answer `403`**: a service that was not given one is read-only, which is the safe default, and an existing deployment is not changed by this slice. The comparison is constant-time. This does not make `POST /events` or the existing `POST /endpoints/:id/enable` authenticated: that is a separate decision and is listed below. Not in this slice: an allow-list or deny-list of destination hosts (private ranges, the metadata address); it is the real defence against request forgery from a stolen token and is the first thing to add after it, and the doc for the endpoint says that the token is the only barrier until then.

**D8. Validation** is the file's (`endpoints.parse`: a host with no whitespace, a port 1 to 65535, a secret that is `whsec_` and base64) plus a body limit and a count limit (`409` at 62 live endpoints). Errors are `problem+json`, as `lexsys-web` does; `lexsys-schema` is not used here because the three fields are checked by the parser that already judges the file, and a second description of what is valid is the thing C3c removed.

**The slices, in order, each with its own tests and mutants:**

1. **State and recovery, no API.** The slot/id split, `max_endpoints()` 62, the `created`/`removed` records, the start-time reconcile of D2 and D3, the history's `bigint` id. Gate: every older suite unchanged; a log written by the current binary (with no `created` records) is read by the new one and yields the same state; a kill between the database and the log in each direction, by hand-made tables and logs.
2. **`POST /endpoints` and `GET /endpoints/:id`, with the token.** Gate: an endpoint created while 1,000 events are in the log gets none of them, and gets event 1,001; `"from":"start"` gets the backlog; the secret is in one answer and no other; no token is `403`; a wrong token is `401`; the transaction fails (a trigger) and nothing changed in memory or the log; a kill after the commit and before the record, and the next start has the endpoint.
3. **`PATCH`.** Gate: a host change reaches the next attempt and not the one on the wire; a secret change signs the next attempt and the receiver verifies it with the **new** secret and refuses the old; a patch that the database refuses changes nothing.
4. **`DELETE`.** Gate: no attempt starts after it; one on the wire finishes and is recorded; replays for it are dropped; the slot is reused by a new endpoint that gets **none** of the old one's cursor, window or outcome records, across a restart (the case D1 exists for); the history keeps the old rows and the new endpoint's rows do not collide with them.

**Open questions, for a person.**

* **D1 and D2 are behaviour.** Is "an endpoint created through the API starts from now" what you want, with `"from":"start"` for the backlog? And is it right that a row that appears in the table without the API keeps starting at 0?
* **D7.** Is a bearer token, read-only without one, the authority you want for the management routes? Should `POST /events` and `/endpoints/:id/enable` be put behind a token too (that would break every client that does not send one, which is why it is not assumed)?
* **62 endpoints** is a limit of this design (the disabled set is an integer). Is it enough?
* **Secret rotation** with two signatures is deferred: is a PATCH that replaces the secret acceptable for a first version, with the receiver refusing in between?

**A bug found while planning slice 1, and fixed first (it is older than this section).** The delivery state's offsets table (`offs`: where each event of the window starts in the events log, 1,024 entries) began at 161 and the cells at 145 + 1,024 = 1,169, **16 integers inside it**. An event whose number is 1,008 to 1,023 modulo 1,024 had its offset overwritten when the first cells of endpoint 0 were written (they sit at 1,169 and up), so a *retry* of such an event read the log at a wrong offset and the event was not delivered; and as it could not become final, the cursor stayed below it and **the endpoint stopped being served once it was a window (1,024 events) behind**. Reproduced on the binary of the previous merge with a receiver that fails event 1,011 once: 1,011 is never delivered and, with 2,300 events, 2,035 and everything after it is not either. The first window is the one a test that posts fewer than about 1,000 events never leaves, which is why nothing earlier saw it, and the failure needs an event to fail *and* be retried in those 16 places, which is why production would see it rarely and badly. The fix is `off_cells() = off_offs() + span`; `tests/layout_test.py` posts 2,300 events, fails 1,011 and 2,035 once each and requires every event delivered with its own body (it fails on the previous binary and passes on this one). Slice 1 below rewrites the layout as a chain of computed offsets so that a region cannot be placed by hand again.
