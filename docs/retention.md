# Retention: how the logs are bounded (`docs/production.md` 0.2)

*Written before the code, as the task required, and corrected after it (the corrections are listed in section 16). Measurements are in `docs/design.md` section 38. Where this file and the code disagree, the code is right and this file has a bug.*

**What was wrong.** `events.seg` and `delivery.seg` were append-only and never shrank: disk grew with history, the start read both whole, and the idempotency index (65,536 keys, never forgot one) was full after 45 days of a once-a-minute schedule. The service could not be run unattended for months.

**What it is now, in one paragraph.** The events log is a chain of **segments** (`events.seg`, `events-1.seg`, ...) and a segment is deleted whole once every event in it is final at every endpoint and older than the retention period. The outcomes log (`delivery.seg`) is **replaced by a snapshot** of the state it replays to, in one atomic rename, whenever it has grown past a limit. The idempotency index forgets a key when its event is dropped or its window has passed. Event ids never go back. Every file starts with a format version and an unknown version is refused. All of it runs inside the loop in small steps, and every step is crash-safe: a `kill -9` at any point loses nothing and repeats nothing beyond the at-least-once contract.

## 1. Words

* **Final** (for an event at an endpoint): delivered (a `2xx`) or dead-lettered (the schedule ran out, or a `410`). Exactly what the cursor of `state.cho` already means: an endpoint's **cursor** is the largest id such that every event up to it is final there.
* **Final everywhere**: final at every endpoint that is in the table now. The least cursor among them is the **floor**; events with an id at or below it are final everywhere.
* **Pinned** (never dropped, whatever their age): an event above the floor (not final somewhere: this includes every event waiting at a **paused** endpoint (the circuit breaker), a **disabled** endpoint (a `410`, or a person) and a **dead** receiver still working through its retry schedule, because their cursors do not move); an event with a **replay waiting** (not yet delivered or dead, including one on the wire); an event of a **cron fire in flight** (the tick has appended it and the database has not heard yet). The pin is the least of these ids, so a pin keeps its event and every later one: the log is only ever cut at the front.
* **Retention** (`retention-days`, default **30**, `0` = keep for ever): the age after which an event may be dropped. The effective age is `max(retention, window-ms)`, so a key that is still inside the idempotency window always finds its event.
* **Retained**: not dropped. The retained part is a suffix of the log: ids from `events_first_id` to the last.
* **A replay of a dormant endpoint** (an endpoint whose row is gone but whose slot the log still remembers) is the one pin that cannot hold: nothing in the table says it is waiting. It is kept in the snapshot, and when the event it names has been dropped the replay is skipped at the start (and counted in `events_skipped`).

## 2. What is recoverable after retention, and what is not

| after an event is dropped | |
|---|---|
| the event's body, `GET /events/:id` | **gone**: `410`, "the event was dropped by retention" (an id that was never given is a `404`) |
| `POST /events/:id/replay` of it | **gone**: `410` |
| a dead letter older than the retention | **gone with its event** (it was final) |
| its delivery state | nothing to recover: it is below every cursor, and the snapshot keeps the cursors |
| the idempotency key it carried | **forgotten**: the same key posted again is a **new event** with a new id (the contract of the window, now enforced by memory and not only by the clock) |
| a cron fire's key (`cron:<id>:<second>`) | forgotten with its event. A fire is therefore made twice only if the database was behind by that fire for longer than the retention (30 days by default) and the event was dropped meanwhile; a database that has been unable to record one fire for a month is a larger problem |
| the history of attempts, `GET /events/:id/attempts` | **kept**: it is PostgreSQL's and the service never deletes from it (it grows by a row per attempt: prune it yourself, `attempts` has an index on `at`; a pruned event answers `[]`) |
| endpoints, schedules | untouched |
| a backup taken before the drop | still holds the event; a restore does not delete anything the backup holds until the next maintenance step, which will drop it again |
| an event **not** final at some endpoint | **never dropped**, at any age |
| an endpoint that was away (its row gone, its slot dormant) and comes back after events were dropped | resumes **at the oldest retained event**: what it missed and what was dropped is not sent. The start says so on stderr |

Nothing is dropped that a receiver is still owed. What retention takes away is the ability to *look at* or *resend* something that every receiver already has, after the period you chose.

## 3. Settings

| key | default | |
|---|---|---|
| `retention-days` | `30` | `0` keeps events for ever (the events log then grows without bound; segments and the snapshot still work) |
| `segment-bytes` | `67108864` (64 MiB) | an events segment is sealed at this size (256 KiB or more) |
| `delivery-log-bytes` | `33554432` (32 MiB) | `delivery.seg` is replaced by a snapshot at this size, or at four times the size of the last snapshot if that is more (64 KiB or more) |
| `idem-keys` | `262144` | how many idempotency keys the index holds (16 to 4,194,304); a schedule index of a quarter of this (at least 16) holds cron's. About 150 bytes a key, resident only as used (section 7) |
| `compact-now` | `0` | `1`: seal the active segment, drop everything droppable, replace the outcomes log by a snapshot, print what was done and **exit 0** (an operator's one-off; it takes the same lock as the loop). With a database named it first reads the `endpoints` table, as the service does and waiting at most `pg-start-wait-ms`, because the cursors say which events are final: if it cannot, it exits `20` and changes nothing |
| `retention-ms` | `0` | **test knob.** If not 0, the retention in milliseconds, instead of `retention-days`. Real deployments do not set it |
| `compact-kill-at` | `0` | **test knob.** The service stops dead (it writes `killpoint` in the data directory and then waits to be killed) at the n-th compaction step it reaches, so a test can `kill -9` at a named step without relying on timing. Section 10 lists the steps. Real deployments do not set it |

Segment and log sizes are settings so a test can make a day of rolling happen in a second; an operator should rarely change them.

## 4. On disk

```
events.seg            segment 0 (the name an existing deployment already has)
events-1.seg ...      segments 1, 2, ...  (the last is the active one)
events.first          the number of the oldest retained segment, "1\n"; absent means 0
delivery.seg          the outcomes: a snapshot, then the records after it
compact.lock          the lock (section 11); empty
```

**Format versions.** Each segment of the events log begins with a **header record** and `delivery.seg` begins with a **header** too. The current format is **2**. A file with no header at all is format **1**: what every deployment has today; it is read as it always was and nothing is rewritten at start. A header with a number this version does not know (3 or more, or unreadable) is a **refusal**: the start prints which file and which version and exits with status `40` (events) or `41` (outcomes), writing nothing. Upgrading is automatic and one way: format 1 files are read as they are, and a segment or a snapshot written by this version carries the header. **A log written by this version cannot be started by the previous one**, by construction and not by luck: the header's shape is one that the previous version's start refuses (`events.seg` status 16, `delivery.seg` status 15 whenever there are endpoints), so a downgrade fails loudly. (A downgrade is not a supported operation; restore the backup taken before the upgrade, `docs/runbook.md` section 5.)

* **Events segment header** (a record of `events-N.seg` with id `(0, 0)`, three pairs): `format` = `lexsys-hooks events 2` (the product's name before it was renamed to cancho-hooks: a format identifier is data that existing logs already carry, so it keeps the old name, and so does the prefix the service, `hooks-logcheck` and `scripts/logcheck.py` recognise); `segment` = four 8-byte little-endian integers, the segment's number, its **base**, the id of its first event, and the Unix ms it was created; `created`, the same time again (a reader that knows the first two pairs ignores it; it is there so that the record has the shape that makes the previous version refuse). Event ids start at 1, so the header is never an event.
* **Outcomes header** (an outcome record of kind 15, `format`; kind 14 is the reason of a failed attempt): endpoint 62 (not a slot), `event` = the format number, `next_at` = the time it was written. The previous version counts it as "a record that is not an outcome" and stops with status 15.
* An old file that is read as format 1 and then rolled or compacted ends up with a header from then on.

**Logical offsets.** Every place in the events log the service remembers (the ring of offsets per endpoint, the scan position, a waiting replay) is a **logical offset**: the number of event-record bytes before it in the whole log since its beginning, headers not counted. A segment's base is the logical offset of its first event; the next segment's base is the previous base plus the previous segment's size less its header. A logical offset never changes when the front of the log is dropped, which is why dropping needs no rewriting of anything. A read at a logical offset finds its segment from a small table (one row a segment), then the bytes.

## 5. The events log: segments, rolling, dropping

**Appending.** `append` copies the record into a staging buffer (4 MiB) and does no input or output; `flush` writes what is staged to the active segment, `fsync`s it, and only then do the reads (which look only at what a flush covered) and the acknowledgements follow. This is the group commit of `cancho-log`, with the buffer moved from the library's file handle to ours. A write or a `fsync` that fails marks the log **broken**: it refuses everything until the process is restarted and the file recovered, as `cancho-log` does. A turn that has staged 4 MiB answers `503` for the rest of it; the clients try again. At most 4,096 segments are retained (256 GiB at the default size): beyond that a roll is refused and counted in `maintenance_errors`.

**Rolling** (sealing the active segment and starting the next) happens when the active segment has reached `segment-bytes`, **or** when its first event is older than the retention and it holds any (a quiet service must not keep an old event in a segment that is never sealed). The steps, each durable before the next:

1. flush: nothing is staged;
2. create `events-<k+1>.seg` (exclusively: if it exists, the roll is refused);
3. write its header (`base` = the logical offset where the next event goes, `first_id` = the last id + 1) and `fsync` it;
4. `fsync` the directory;
5. switch: new events go to the new segment.

**Dropping** removes the **oldest** sealed segment (one a step), when all of: it is sealed; its last id is at or below the floor and below every pin; it is older than `max(retention, window-ms)` (a segment's age is measured from the time the *next* segment was created, which is no earlier than any of its events); the retention is not `0`; no cron tick is in flight; the lock is free. The steps:

1. flush the outcomes log (the cursors the decision rests on are durable);
2. write `events.first` = the next number, to a temporary file, `fsync`, rename over `events.first`, `fsync` the directory;
3. delete the segment's file; `fsync` the directory.

The manifest moves **before** the file goes, so a crash between leaves an orphan below the oldest retained number, which the next start deletes; the reverse order would leave a manifest naming a file that may be missing.

**What a start does** (`evlog.open`): reads `events.first`; the file it names must exist and every number from it up to the last that exists must exist (a gap, or a file two or more past the last, is a refusal: status `42`, "the events log has a hole"); reads each segment's header (the chain must be consistent: each base is the previous base plus the previous payload); recovers the **last** segment as `cancho-log` always did (a torn tail is cut). A last segment whose header is missing or torn is a roll that did not finish (step 3): it holds no event, is deleted, and the one before it is the active one. Orphans below the oldest retained number are deleted.

**Reading.** `read_at(offset)` finds the segment, reads 256 KiB from the file into a cache when the record is not in it, and hands back the one record. Open, `pread`, close per refill: no file is kept open, which is what lets a segment appear and disappear without touching any handle. A sparse index, one offset per 1,024 events (65,536 entries, a ring), makes "the record with id N" a walk of at most 1,024 records instead of a scan from the start; it is rebuilt by the scan at start and kept by `append`.

## 6. The outcomes log: a snapshot, by rename

`delivery.seg` is the log of everything that happened to every (endpoint, event) pair, and the state is what replaying it yields (section 15 of `docs/design.md`). Compacting it is **writing the state down as the shortest log that replays to it**, and replacing the file.

**The snapshot** is a header record, then for every slot that has an endpoint (live, dormant or draining), in this order: `created(slot, id, cursor)`; for each cell of the window above the cursor, `delivered` if the event is final there and `failed(attempts, next attempt)` if it is waiting for a retry; then, for each dead letter the endpoint's table holds (`docs/design.md` section 39.1), `dead_entry` (kind 17: the event, its attempts and reason, and when it died) and, if the table left some out for room, one more that carries its floor; `paused` or `disabled` if it is; `streak` if a run of failures is under way; then for each waiting replay of the slot, `replay` and, if it has been tried, `replay_failed`. Every record is one the replay knows (kind 17 is the one a snapshot adds to what the log holds otherwise, and a replay that was cancelled (kind 16) is not written at all: it is not in the table), so **the recovery of the state is the replay of the log**: that is the equivalence argument, and `tests/retention_test.py` checks it by comparing what the service reports (`/endpoints`, `/stats`, the next attempts) before and after a replacement. The records of a free slot, a stale outcome for a slot that was freed and everything below a cursor are not written: they replay to nothing. A final cell is written as `delivered`, whichever it was, because the cell does not record which and a window needs only that it is final; which of them are dead letters is the table's, written as `dead_entry` records, so a snapshot neither loses a dead letter nor makes a delivered event one (`tests/dead_test.py` stage 9, `tests/cancel_test.py` stage 10). The size is at most 62 slots x (1,030 records + 2,049 dead letters) x 77 bytes, about 15 MB (the buffer is 16 MiB); typically a few hundred bytes.

**The replacement** is one turn of the loop and nothing else happens in it:

1. flush the outcomes log;
2. create `delivery.seg.tmp` (truncating any leftover) and write the snapshot to it;
3. `fsync` it;
4. rename it over `delivery.seg`;
5. `fsync` the directory;
6. open the new file as the service's outcomes log (the old handles are closed and the sequence numbers go on from where they were).

A leftover `delivery.seg.tmp` is deleted at start and never read. The name `delivery.seg` is always a complete log, old or new, because a rename is atomic and the new file was durable before it. After a power cut that loses the rename the old file is what is there, and it is a complete, consistent, older log: the service replays it and repeats at most what the records since then said (at-least-once).

**When:** when `delivery.seg` has reached `max(delivery-log-bytes, 4 x the size of the last snapshot)`, and at the start if it is over that already (a log that grew for a year before the upgrade is compacted once, in the first turns of the loop after the start has replayed it; `--compact-now 1` does it at a time of the operator's choosing). Replay time at the next start is bounded by the same limit: about 400,000 records at the default.

## 7. The idempotency index

The index is an index of the events log and nothing else is persisted (design section 17); that does not change. What changes is that it is **bounded by time and by retention**, and that its size is a setting.

Two indexes, one structure: keys of the form `cron:<schedule>:<second>` live in their own, with their own capacity (a quarter of `idem-keys`), because their lifetime is different. Each is a **ring in the order the events were appended** (a FIFO) with an open-addressing hash table over it:

* a key is **fresh** while `now - t <= window-ms`, as before, and while its event is retained (cron keys: only the second condition, plus `fire_cron`'s rule of not looking at the age);
* **expiry is by eviction from the front of the ring**, a bounded number of keys a turn (256), so the cost never lands on one request; removing a key from the table is a backward shift (no tombstones), so lookups never slow down with churn;
* a key that is **expired but not yet evicted** is treated as absent: a repost overwrites it (removes the old entry, adds a new one);
* a **full** index (the ring or the key bytes) is a `507` for a new key, as before: it still never forgets a key that is fresh. It first evicts what it can.

**At start** the index is rebuilt by the same scan that finds the positions of the endpoints, over the **retained part only**: expired non-cron keys are skipped. If the keys that are still fresh do not fit `idem-keys`, the start is refused with status 16 as before, and the message says to raise it.

**Memory** (measured, `docs/design.md` section 38.2): the index is address space until it is used. On the compiler at the pin (4c27593, whose large `box_slice` is zeroed lazily) the service is **2.5 MB resident after `listening` for any `idem-keys`** from 16 to 4,194,304, and holds 200,000 distinct keys in 30 MB. (On the compiler before it the array was resident when made: the default of 262,144 keys cost 38 MB, a million 153 MB; `docs/cancho-log-retention.md` gap 4.) The previous index was 65,536 keys and never forgot one.

## 8. Event ids are never reused

The active segment is never dropped and its header says what the first id it holds is, which is always the last id of the log plus one at the moment it was created. So even when every event has been dropped, the next event is the next number. A new segment's header is durable before its first event is appended; a roll that did not finish leaves a segment with no events (deleted at start) and the previous one still tells the last id.

## 9. Start-up time

`T_start = T_scan x R + T_replay x D + c`, where **R** is the number of retained events, **D** the number of records in `delivery.seg` (at most about 400,000 at the default, plus the snapshot) and **c** a few milliseconds of probing. The scan checks every retained record's checksum and rebuilds the index and the sparse index in one pass. Measured values are in `docs/design.md` section 38: at 1,000,000 retained events (257 MB) the start takes 0.9 s with no endpoint, and 2.1 to 2.2 s with one that has been sent all of them and 128,000 records of the outcomes log to replay (about 0.9 microseconds an event and 9 a record, on a machine others were using; before the merge with the work that stores the event's type, 241 MB, 0.7 s and 1.5 to 2.1 s); the figure for 10,000,000 retained events (about 7.5 s plus the replay) is extrapolated and says so. **Bounded**: R is bounded by the retention and the rate; D by `delivery-log-bytes`; there is no term for history.

## 10. Crash safety: every step, and what a kill leaves

A kill at any instant is a power cut at worst (the tests use the `fsync` shim, which cuts every `*.seg` file back to what its last `fsync` covered). The steps are numbered; `compact-kill-at = n` stops the service at the n-th step reached, after the step has been done and before the next, so the tests do not depend on timing.

| n | step | what is on disk if killed here | what the next start does |
|---|---|---|---|
| 1 | roll: flushed | the old segment is complete | nothing to do; rolls again when due |
| 2 | roll: new file created | an empty `events-<k+1>.seg` | no header: deletes it |
| 3 | roll: header written (not synced) | a header that may be torn or lost | torn or empty: deletes it; whole: it is a valid empty segment |
| 4 | roll: header synced | a complete empty segment | uses it as the active segment |
| 5 | roll: directory synced | the same, durable | the same |
| 6 | roll: switched | new events may follow | the same |
| 7 | drop: manifest written | `events.first.tmp`, not yet `events.first` | ignores the temporary file |
| 8 | drop: manifest renamed | `events.first` names the next segment; the old file still exists | deletes the orphan |
| 9 | drop: manifest durable | the same | the same |
| 10 | drop: segment deleted | the file is gone | nothing |
| 11 | drop: directory synced | the same, durable | nothing |
| 12 | snapshot: outcomes flushed | the old `delivery.seg`, complete | nothing |
| 13 | snapshot: temporary file created | an empty `delivery.seg.tmp` | deletes it |
| 14 | snapshot: half written | a partial `delivery.seg.tmp` | deletes it |
| 15 | snapshot: written (not synced) | a complete or partial `.tmp` | deletes it |
| 16 | snapshot: synced | a complete `.tmp`, not yet the log | deletes it; replays the old log |
| 17 | snapshot: renamed | `delivery.seg` is the snapshot (or, after a power cut, the old log) | replays whichever it is |
| 18 | snapshot: directory synced | the snapshot, durable | replays it |
| 19 | snapshot: switched | the snapshot is the outcomes log in use, with records after it | replays it |

The same invariants after each: every acknowledged event not legitimately dropped is there, byte for byte; ids go on from the last (never reused); no event that was final before the kill is sent again, and no event is lost; a second start changes nothing. A roll cannot be mistaken for a drop and a drop cannot start unless the outcomes it relies on were flushed in the same step.

## 11. Backup, restore, `logcheck.py`

`backup.sh --mode online` used to rest on "both files are only appended to while the service runs". That is **no longer true**: a segment is deleted and `delivery.seg` is replaced. The decision, and why:

* **The service defers its destructive steps while a backup runs.** `compact.lock` is a lock file; the service takes a non-blocking exclusive `flock` on it for the duration of a drop or a replacement and, if somebody else holds it, skips the step and tries again in a moment. The online backup holds the lock (`flock`) for the whole copy. A held lock delays compaction by the length of the backup and costs nothing else. If the backup process dies, the kernel releases the lock.
* **Rolling is allowed during a backup.** It only creates a file after the ones the backup lists, and the copy of the previous active segment is a prefix of the sealed one. The backup lists the segments **after** it has copied `delivery.seg` (the same rule as before: events never older than outcomes), so the pair stays consistent: a segment created later is simply not in it.
* **The copy is `delivery.seg` first, then the manifest and the segments in order**, under the lock, so neither the manifest nor the snapshot can change in between; the last segment may end in a torn record, which `logcheck.py trim` cuts as before; a last segment whose header is torn or missing is a roll caught half-done and is left out.
* A volume or filesystem snapshot (the other supported way) is a crash-consistent image of everything at one instant, which every step above survives.
* **`--mode stopped`** needs nothing new.

`logcheck.py` reads the new layout (the manifest, the segments, their headers and chain, ids dense across them from the first retained id, the outcomes header) and the old one; `restore.sh` restores the segments and writes the manifest; `MANIFEST` of a backup is format `lexsys-hooks-backup/2`, and a `/1` backup restores as before. The tests (`tests/backup_test.py`, including the online variant under `kill -9`) run against the new layout and against a format 1 directory.

## 12. Stalls

Nothing in the loop waits for more than one of these in a turn, and the loop does not do two heavy things in a turn. **Bounds, as designed** (measured in section 38 of `design.md`): idempotency eviction, 256 keys a turn (microseconds); a roll, two `fsync`s and a file create; a drop, two `fsync`s, a rename and an unlink; a snapshot, a scan of the cells (62 x 1,024), a write of at most 5 MB, two `fsync`s and a rename. The dominant term in every one is the `fsync` of the machine, which the ingest already pays on every turn that has an event. The design does not slice the snapshot: it is one turn, and the measured worst case (62 endpoints with a full window of retries: 63,488 records, 4.9 MB) is the stated bound: 40 to 70 ms measured, about a microsecond a record (it was 7 microseconds, 0.5 s idle and 0.96 s once on a loaded machine, until `state.put_outcome` stopped losing the memory of its region on every call: `docs/cancho-log-retention.md`, gap 6; section 38 of `design.md`); if it ever had to be sliced, the snapshot would be built from a frozen copy of the state and the records appended meanwhile copied after it, which the replay already allows.

## 13. What `cancho-log` would need, and the compiler gaps

Everything above was built without changing `cancho-log`: its record and recovery functions are used as they are, and the segment chain, the staging, the logical offsets, the manifest and the replacement are this repository's (`src/evlog.cho`, `src/store.cho`, `src/compact.cho`). What a library change would make **simpler** (not possible otherwise) is in [`cancho-log-retention.md`](cancho-log-retention.md): a log object that is a chain of segments and can drop its front, a snapshot-and-replace operation, a header and a version in the file, a `read_chunk`, and an `fsync` of the directory. Two **cancho** gaps cost this repository an awkward structure and each has a reproducer there: a resource cannot be replaced through a mutable reference (so no object that holds a file can switch files while it is borrowed, which is why the events log holds none and the outcomes log is owned by the loop), and a capability cannot be shared (one `Fs` per program, so only one object can own it).

## 14. What is not done

* The snapshot is not sliced; its stall is measured and stated.
* One service per directory: no lock stops a second service from starting on the same files (as before). `compact.lock` protects against a backup, not against a second service.
* `attempts` (PostgreSQL) is pruned by the service on its own setting, `history-days` (docs/design.md section 43), not by retention: a row says what happened to an attempt, and may be wanted longer or shorter than the event.
* The sparse index and the scan are not parallel; the start is single threaded.
* `GET /events/:id` of a dropped event is a `410` and of an id never given a `404` (docs/design.md section 44): the ids are dense and never reused, so the oldest id the log keeps is the whole tombstone. What the event was is not kept.

## 15. The gate, and where each part is tested

`tests/retention_test.py` (the stages are named after this file's sections) and `scripts/bench/retention_bench.py`: ten million events through, disk bounded by retention and not by history; the start at 1,000,000 retained events; `kill -9` at every one of the 19 steps for both logs; an event not final at a paused endpoint, a disabled endpoint, or with a waiting replay survives; keys expire as designed, a key inside the window deduplicates across a compaction and a restart, a repost of an expired key is a new event; ids are not reused after everything was dropped; the existing suite. Results: `docs/design.md` section 38.

## 16. What building it changed in this design

* The staging buffer is 4 MiB, not 8; `idem-keys` defaults to 262,144, not 1,048,576 (the memory of a million keys is 291 MB and most deployments need a fraction of it).
* A directory from before retention is compacted in the first turns after the start, not at the start itself: the start only replays.
* A dormant endpoint's waiting replay cannot pin its event (section 1), and the replay's guards (`slot_known`) were needed so that a snapshot, which writes only slots that have an endpoint, replays to the same state as the log it replaced.
* `delivery.seg` has a header only after its first snapshot or when it is new; an old one is format 1 until then.
* Two bugs the backup test found in the scripts, not in the design: `backup.sh` read `events.first` before taking the lock, and `logcheck.py` called a tail "damage" when a power cut had spoiled the ends of two small records at once (a tail is now damage only if a valid record follows a bad byte).
* The stall of a snapshot was designed as "a few milliseconds plus the `fsync`s"; measured first at 0.5 s for the largest one, which was a leak in a function every delivery used (section 38.5 of `design.md`), and then at 40 to 70 ms. Its bound is stated in section 12.

## 17. Open

See `docs/design.md` section 38.6 and `docs/production.md` 0.2 for what is not verified and what is still open.

## The hard maximum age (`max-age-days`)

Retention keeps an event that is not final somewhere for as long as that lasts: a paused, disabled or dead endpoint, or a waiting replay, can keep the whole log from that event on. `max-age-days` (default 0: none) is the bound nothing can hold back: the oldest segment, once it is older than that, is dropped whatever pins it. Before it goes, in the same flush, every endpoint whose cursor is below its last event is moved past it with an `advanced` record (`docs/design.md` section 42), and a replay waiting for one of its events is ended (`replay_cancelled`); the events that were not final are counted (`/stats events_expired`, `segments_expired`) and are a `410` like any dropped event. The active segment is sealed when it is that old, so that a quiet service drops its events too. `docs/design.md` section 47.2.

## Erasing one event (`DELETE /events/:id`)

An event can be erased before retention would drop it (a request under GDPR article 17). In this order, each step durable before the next: a record of kind 20 (`erased`) in `delivery.seg`, flushed (from then on the event is final at every endpoint, never sent, replayed or served: `410`); its waiting replays ended and its dead letters gone; the segment that holds it sealed if it is the one being written; the segment rewritten with the event's body replaced by `{"erased":true}` and spaces to the same length (so every offset the service holds stays right), the record sealed again, the copy synced and renamed over the segment, the directory synced. A start that finds an `erased` record whose body is still in its segment finishes the rewrite. The rewrite reads and writes the whole segment in the loop (at most `segment-bytes`, 64 MiB by default): a request that takes a moment, which is the price of an append-only log. Not erased: the event's type, its idempotency key, its id and size, the history rows, and **backups taken before** (the operator expires those: `docs/privacy.md`). `docs/design.md` section 47.3.

