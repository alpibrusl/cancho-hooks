# Delivery: how it works, retries, dead letters, pace

How an event travels from `POST /events` to a receiver, the retry schedule and its jitter, idempotency keys, dead letters with bulk replay and cancelling a replay, and the concurrency and rate of an endpoint.

## How it works

One thread, one poller. An accepted event is appended to a [`cancho-log`](https://github.com/alpibrusl/cancho-log) segment and its
request held; after the turn one `flush` covers every append and the held requests are answered. Delivery runs in the same loop without holding it: each attempt is a small state machine (connecting, sending, reading) whose connection is watched on the server's own poller, so up to 64 are in flight together and a slow, silent or unreachable endpoint costs the others almost nothing. Each endpoint has a cursor (every event up to it is delivered or dead) and a window of events above it that finished out of order or are waiting for a retry, so a failing event does not hold up the ones after it. What happened to each attempt goes to a second log, `delivery.seg`, which a restart replays; the time of the next attempt is a Unix time, so it survives too.
[`design.md`](design.md) has the semantics, the scenario fixed before the build, and what each step found.

## Idempotency

**Idempotency.** Send the same `Idempotency-Key` with the same event and you get the same answer and one event, however often you retry:

```
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":77}' localhost:8080/events
{"id":2}
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":77}' localhost:8080/events
{"id":2}
$ curl -XPOST -H 'Idempotency-Key: order-77' -d '{"type":"order.paid","id":78}' localhost:8080/events
{"error":"this Idempotency-Key was used for a different event"}
```

## Pace, dead letters and retries

**Pace.** Two limits keep one endpoint from taking more of the service, or of the receiver, than you mean to give it. They are settings for every endpoint (`endpoint-concurrency`, `endpoint-rate`) and each endpoint can have its own (`"concurrency"`, `"rate"` in `POST` and `PATCH /endpoints`, the columns and the words `concurrency=` and `rate=` of `endpoints.conf`; `0` or `null` follows the setting again; `GET /endpoints` shows the limit in force).

* `concurrency` is the most attempts the endpoint has in flight together: 1 to 8, the default 8. A receiver that can take two requests at a time is given two.
* `rate` is the most attempts the endpoint **starts** in a second: 1 to 100,000, `0` (the default) no limit. It is a token bucket worked on the clock the loop already has, a tenth of a second deep (so a limit of 5 allows one attempt at once and then one every 200 ms, and no second sees more than the rate plus one tenth of it). **An event held back by it waits; it does not fail.** No attempt is made, nothing is written to the log, the retry counter does not move, the event is not lost and the other endpoints are not slowed: the loop looks at an endpoint's window until its bucket is empty and goes on to the next, and it sleeps no longer than the time the soonest bucket needs. `/metrics` counts the attempts that were held back (`hooks_endpoint_throttled_total{endpoint}`). The bucket is memory: a restart starts every bucket full, so a limit can be exceeded by one bucket (a tenth of a second's worth) across a restart.

**Retry jitter.** The schedule is the Standard Webhooks one (5 s, 5 min, 30 min, ...). A receiver that was down for a minute fails every event that arrived in it at about the same moment, and the schedule would ask for all of them again at the same moment, and again at the next step. `retry-jitter` moves each delay up or down by up to that many percent of itself (0 to 50; the default **10**, so a 5-minute delay is 270 to 330 seconds). The move is a fixed function of the endpoint, the event and the attempt (no random source), and the time of the next attempt is written to the log with the outcome, so a restart keeps the time that was chosen, whatever the setting is by then. `0` is the schedule exactly. A replay's retries are moved the same way. The number of attempts before a dead letter is the schedule's, always.

**Dead letters.** An event is a *dead letter* at an endpoint when its last word there is `dead` (the schedule ran out, or the receiver answered `410`) and no replay of it has delivered it since. The service keeps the newest 2,048 of each endpoint in memory, built from `delivery.seg` at the start and kept while it runs:

```
$ curl localhost:8080/endpoints/3/dead?limit=2
{"endpoint":3,"order":"desc","held":5,"truncated":false,"complete_above":0,
 "dead":[{"event":5,"type":"user.created","attempts":10,"reason":"status_5xx","died_at":1791133036458,"replaying":false},
         {"event":4,"type":"invoice.paid","attempts":10,"reason":"connect_refused","died_at":1791133036457,"replaying":false}],
 "next":4}
$ curl -XPOST localhost:8080/endpoints/3/replay-dead -d '{"types":["user.*"],"limit":10}'
{"endpoint":3,"taken":3,"remaining":0,"waiting":3,"next":5}
```

* **The list** is newest first (`order=desc`, the default) or oldest first (`order=asc`); `limit` is 1 to 1,000 (100); `after=<event id>` goes on past that event in the order chosen, and `next` is the `after` for the page after this one (`null` at the end). The cursor is an event id, so a page is stable while dead letters are added or taken out. `died_at` is the Unix ms of the death (0 for a death recorded before the time was written); `reason` is one of the reasons of `GET /events/:id/attempts`; `replaying` says that a replay of it is waiting. `type` is `null` for an event that has none.
* **The bound.** An endpoint holds its 2,048 dead letters with the largest event ids. If it has more, `truncated` is true and `complete_above` says that every dead letter above that event id is in the list; the older ones are in the log, and they enter the list as soon as there is room (a replay that delivers frees an entry; the next look at the list completes it from the log, which reads `delivery.seg` once), until the log is replaced by a snapshot ([status.md](status.md), Limitations): a snapshot keeps the 2,048 an endpoint holds, and an older dead letter that was left out is still replayable by its event id (`POST /events/:id/replay/:endpoint`) while the event is kept. **A dead letter lasts as long as its event**: when retention deletes the event it leaves the list. The four routes answer `503` after a start until the endpoints have been read from the database.
* **Replay in bulk** sends the dead letters again, oldest first, as replays (the same `webhook-id`, the same schedule, `POST /events/:id/replay`). At most 32 replays wait at once, so one call takes as many as there is room for and no more than `limit` (an optional 1 to 2,048): `taken`, `remaining` (the dead letters that match and are not already replaying, and were not taken) and `waiting` (replays waiting in all) say what happened. Call again until `remaining` is 0. `taken` 0 with `remaining` above 0 means the table of 32 is full: wait for the replays to finish, or cancel some. `types` takes only the dead letters whose type matches one of the patterns (as an endpoint's subscription does; `[]` is every type), `after` only those with a larger event id (how a caller goes on past ones it has seen), and a member that is none of these is a `400`. `202` when something was taken, `200` when not. A dead letter that is replaying is not taken again, and one whose replay dies again is a dead letter again with its new attempts and time.
* **Cancel.** `DELETE /events/:id/replay/:endpoint` takes back one waiting replay and `DELETE /endpoints/:id/replays` all of an endpoint's: `{"cancelled":N,"busy":M}`. A replay with an attempt on the wire is not cancelled (`409` for one, counted in `busy` for all): ask again when it has ended. A cancellation is written to the log and flushed before the answer, so it survives a crash and a power cut; the event stays a dead letter and can be replayed again. `404` for a replay that is not waiting.
