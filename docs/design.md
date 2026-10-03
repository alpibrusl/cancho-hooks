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

**Retries.** A delivery succeeds on any `2xx`. Anything else, a timeout, or a connection failure is a failure. The schedule is exponential with jitter: 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 10 h (the values Standard Webhooks suggests, so a subscriber used to Svix is not surprised), then dead-letter. `410 Gone` disables the endpoint. The delays are data in the group state, so a restart resumes them, and a restart does not retry everything at once.

**Dead letters** stay in the log and are visible by id; `POST /events/{id}/replay` puts one back.

**Signing.** The format is [Standard Webhooks](https://www.standardwebhooks.com): headers `webhook-id`, `webhook-timestamp` and `webhook-signature`, the signature being `v1,` plus the base64 of HMAC-SHA256 over `id.timestamp.payload` with the endpoint's secret. It is chosen because a subscriber then needs no library of ours, and because there are independent implementations to check against. **I have not fetched the specification in this session**, and the header names and signing string above are from memory; the first step of the signing work is to read the specification and correct this paragraph where it is wrong.

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

H1b delivers to **one fixed receiver** given on the command line (`hooks <port> <dir> <host> <port>`). After each turn of the loop the service sends the events the log has flushed, in order, one at a time, to `POST /hook` with the event as the body and `webhook-id: evt_<id>`, and counts an event delivered on any `2xx`. `src/deliver.ls` is one attempt: `tcp_connect`, send, then wait for a status line for at most 2 s with a `Poller` on the non-blocking connection.

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
