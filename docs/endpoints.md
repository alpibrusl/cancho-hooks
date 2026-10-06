# Endpoints and the database

Where endpoints come from (`endpoints.conf`, or the `endpoints` table of a PostgreSQL), how they are created, changed and deleted while the service runs, and what an endpoint can carry besides an address and a secret: event types, a secret that rotates with two signatures, custom headers.

## A database

With `--pg-host` the service uses PostgreSQL for two things (`design.md` section 24):

* **the endpoints**: they are the `endpoints` table, read once, as soon as the database answers. `endpoints.conf` is **not read** when a
  database is named. The start does not wait for the database: the service listens at once and takes events (`202`, stored in the log),
  but delivers nothing and answers `503` on the routes about endpoints, and `/readyz` says `database`, until it has read the table;
  then it delivers what it was given meanwhile. It does not guess: a stale or partial list would deliver to the wrong receivers. A login
  the server refuses for good (a wrong password, a role or a database that does not exist, a table that is not there) ends the start at
  once with status 20 and what failed; a database that cannot be reached or does not answer ends it with status 20 after `pg-start-wait-ms`
  (30 s; `0` waits for ever). Once the table has been read the service never ends because of the database.
* **the history**: a row for every delivery attempt that ends (the receiver's status, the outcome, the attempt's number, when and how
  long), read back by `GET /events/:id/attempts`. The log files stay the truth about delivery, so the history is **best effort**: the
  service delivers while the database is slow or gone, and counts the rows it could not write (`/stats`).

**A database that goes away comes back by itself.** The service keeps two connections to it, remakes one that is lost without waiting
(the waits between attempts start at `pg-backoff-min-ms` and double to `pg-backoff-max-ms`; a connection that was live for a second is
replaced at once), prepares its statements again, and `/readyz` is `200` again when one is live: no restart. While none is, the rows of
the history wait in a queue of 256 (the 257th is dropped and counted; a row that was on a connection when it went is counted `failed`:
whether it was written is not known), a change to an endpoint or a schedule is a `503` at once, one that was on the wire when the
connection went is a `504` "may have been stored", and the schedules wait (no event is made twice: the key is in the log). Only a name
for `pg-host` can stall the loop (the resolver is a call that waits): give an address.

```sh
createdb hooks && psql hooks -f sql/schema.sql
build/hooks --dir /var/lib/hooks --pg-host 127.0.0.1 --pg-user hooks_rw --import-endpoints 1   # copy endpoints.conf into the table, once
build/hooks --port 8080 --dir /var/lib/hooks --pg-host 127.0.0.1 --pg-user hooks_rw
psql hooks -c "select * from attempts where event = 41 order by endpoint, attempt"
```

The import is one transaction, leaves a row whose id is already there as it is, and refuses a file with a bad line without importing any
of it. The `endpoints` table holds the secrets in a form the service can sign with: give it the permissions of a secret.

## endpoints.conf

`endpoints.conf`, in the data directory, takes one endpoint a line (`#` comments and blank lines are
ignored; at most 1,024 endpoints, each with an id of up to six digits, written and never counted). It is its own file because it holds the secrets: give it the permissions secrets need, and keep it out of the settings.

```
# <id> <host> <port> <secret> [types=...] [headers=...] [old=...] [concurrency=...] [rate=...]   (the words after the secret: see below, and delivery.md for concurrency and rate)
0 127.0.0.1 9000 whsec_...
1 127.0.0.1 9001 whsec_...
```

## Per endpoint: event types, secret rotation and headers

Each endpoint can have three things besides an address and a secret (`design.md` section 35). They are set by `POST /endpoints` and `PATCH /endpoints/:id` (or by words on a line of `endpoints.conf`, below), kept in the `endpoints` table, and survive a restart.

**Event types.** `"types": ["invoice.paid", "user.*"]` sends the endpoint only the events whose type matches one of the patterns. No list (the default, or `[]`) is every event.

* **The type of an event** is the string `"type"` member of its JSON body, read once when the event is accepted and stored with it. A type that is empty, longer than 128 bytes or has a control character in it is stored as *no type*, and so is the type of an event stored by a version that did not keep types: an event with no type goes only to an endpoint with no list, or one that lists `*`.
* **A pattern** is 1 to 128 visible ASCII characters, no space and no comma; at most 16 patterns and 512 characters in all. `invoice.paid` matches exactly that type (byte for byte, case sensitive); `user.*` matches every type that begins `user.` (`user.created`, `user.address.changed`; not `user` and not `users.created`); `*` matches everything, an event with no type too. A `*` anywhere else is refused.
* **An event an endpoint does not want is final for it at once**: no attempt, no record, nothing in the history. Its cursor moves over it, so a stream of events nobody wants does not fill the window or hold anything back. `/stats` counts them (`filtered`). A change of `types` applies to the events the endpoint has not yet looked at; an event it passed over is not sent because the list grew (replay it). A restart decides again the events above an endpoint's recorded cursor, with the list then in force.
* **A replay** (`POST /events/:id/replay`) goes to the endpoints whose list wants the event; `/replay/:endpoint` goes to that endpoint whatever it lists.
* The check reads the stored type from the record: no JSON is parsed for an endpoint, and an endpoint with no list costs nothing.

**Secret rotation.** `PATCH /endpoints/:id` with a new `"secret"` (or `"rotate": true`, which makes one) replaces the secret at once. Add `"keep_old_ms": N` (0 to 2,592,000,000, 30 days) to keep the old secret valid for N ms, or `"keep_old": true` for the default period (`rotation-grace-ms`, a day). The period starts when the change is stored and answered, not when it was asked for: a change that waits for a slow database does not lose its overlap waiting. While it lasts **every delivery carries both signatures** in `webhook-signature`, the new one first: `v1,<new> v1,<old>`, as the Standard Webhooks specification has it for key rotation, so a receiver that knows either secret verifies the delivery (the reference library accepts the header whichever secret it holds). Afterwards only the new one. The answer says `"secret_old_until"` (Unix ms), and so does `GET /endpoints/:id` while the period lasts (0 otherwise). `{"keep_old_ms": 0}` alone ends the overlap now, `{"keep_old_ms": N}` alone moves its end (400 if there is no previous secret); a new secret without a period ends a running one, and a second rotation keeps only the secret it replaced. The previous secret is held in the row (`secret_old`, with `secret_old_until`) and read at start: the table is a secret, as before.

**Custom headers.** `"headers": {"Authorization": "Bearer ...", "X-Api-Key": "..."}` is sent on every attempt (and every replay) after the three `webhook-*` headers. `PATCH` replaces the whole set; `{}` or `null` removes it.

* At most 8 headers; a name of 1 to 64 characters from the HTTP token set; a value of 1 to 512 visible ASCII characters and spaces, not beginning or ending with a space; 2,048 bytes as sent in all; no name twice (any case).
* **Never allowed**, in any case, each refused with a reason: `webhook-id`, `webhook-timestamp`, `webhook-signature`, `host`, `content-length`, `content-type`, `connection`, `transfer-encoding`, and the other connection headers (`keep-alive`, `proxy-connection`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `upgrade`). A name or a value with a CR, an LF or a NUL, however written (a raw byte, a JSON escape, `%0d%0a` in the table), is refused, so a header cannot end the headers and start a request of its own.
* **The values are secrets** (an `Authorization` is what the receiver trusts): `GET` shows the names (`"headers": ["Authorization"]`) and never a value, and no answer or log line repeats one. The table holds them like the secret, so give it the permissions of a secret.

On the wire, with a rotation under way and two headers set:

```
POST /hook HTTP/1.1
Host: receiver
Content-Type: application/json
webhook-id: evt_41
webhook-timestamp: 1791028548
webhook-signature: v1,<signature under the new secret> v1,<signature under the old one>
Authorization: Bearer ...
X-Api-Key: ...
Content-Length: 53
Connection: close
```

`endpoints.conf` and the table's text take the same three as words after the secret (a line without them has none of the three):

```
# <id> <host> <port> <secret> [types=<patterns>] [headers=<spec>] [old=<secret>@<until>] [concurrency=<1 to 8>] [rate=<1 to 100000>]
0 127.0.0.1 9000 whsec_... types=invoice.paid,user.* headers=Authorization:Bearer%20abc,X-Api-Key:k
1 127.0.0.1 9001 whsec_... old=whsec_...@1791100000000 concurrency=2 rate=20
```

`types=` is the comma separated patterns; `headers=` is `Name:value` pairs separated by commas, each value percent-encoded (every byte but `A-Za-z0-9-._~` as `%XX`: a space is `%20`, a comma `%2C`, a percent `%25`), which is also how the `headers` column of the table holds them; `old=` is the previous secret and the Unix ms until which it is signed with too; `concurrency=` and `rate=` are the endpoint's own limits ([delivery.md](delivery.md)). A word that is none of these, or one twice, or one that is not good, is a bad line (status 13, naming the line); `--import-endpoints` copies the words into the table. The table is read as one text of at most 528 KiB (528 bytes for each of the 1,024 endpoints: 540,672), and the database's answer to the read is at most 1 MiB; over either, the start ends with status 20, `the table is too large`, and `POST` answers `507` before the text would not fit.
