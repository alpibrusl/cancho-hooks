# Privacy and audit

What the service holds, where and for how long; how a request about a person's data is answered with it; and what is the service's to do for GDPR and SOC 2 and what is the operator's. Compliance is a property of an organization, not of a program: this page is what the program offers, for the person who answers for it. It is not legal advice. The design is `design.md` section 47.

## What is held, where, for how long

| what | where | personal data? | how long |
|---|---|---|---|
| an event's **body** (what the client posted: it may hold anything) | the events log, `<dir>/events*.seg` | often | until it is final at every endpoint and older than `retention-days` (30); never longer than `max-age-days` if that is set; or until it is erased. Encrypted with `encryption-key-file` |
| an event's **type**, id, size, the time it came, its idempotency key | the events log | rarely (a key chosen by the client could be) | as the body |
| what happened to each attempt: endpoint, event id, status, time, reason | the outcomes log, `<dir>/delivery.seg` | no | replaced by snapshots of the state; an outcome of a dropped event goes with the next snapshot |
| the **history**: the same, one row an attempt | PostgreSQL, `attempts` | no | `history-days` (30) |
| **endpoints**: address, signing secret, custom headers (which may be credentials) | PostgreSQL `endpoints`, or `endpoints.conf` | not of the people in the events | until deleted |
| the **audit log**: who read or changed what (scope, method, path with ids, status, `X-Forwarded-For`) | `<dir>/audit.log` and up to `audit-log-files` older ones | the forwarded address can be | rotated at `audit-log-bytes`; ship it elsewhere to keep it longer |
| **backups** | wherever `scripts/backup.sh` writes them | as the logs | **the operator's**: a backup keeps what it copied, an erasure after it does not reach it |

The bodies also leave the service: **every receiver gets them**. Each endpoint is a recipient of the data.

## Answering a request about a person's data

* **Access (GDPR article 15).** Find the events that hold the person's data (the service does not index bodies: the client that posted them knows their ids, or the history and the client's own records do), and read each with `GET /events/:id` (read scope). The audit log writes down each read.
* **Erasure (article 17).** `DELETE /events/:id` (admin) for each event: its body is replaced in the events log at once (the segment rewritten in place), it is never sent, replayed or served again (`410`), and the audit log has the request and its outcome. What it does not reach: the receivers (who have their own copy: tell them), the backups taken before (expire them within your retention, or erase there too), and the event's type, idempotency key and id. `docs/retention.md` has the steps.
* **Storage limitation (article 5(1)(e)).** `retention-days` drops what is delivered everywhere; `max-age-days` drops whatever is older, even what an endpoint that is down still needs (those events are counted as expired); `history-days` prunes the history.

## Security of processing

* **Encryption at rest** (`encryption-key-file`): the bodies are sealed with ChaCha20-Poly1305; the key is a file the operator keeps apart from the data and its backups (`docs/security.md`). Disk encryption (LUKS, an encrypted volume) is the other way, and covers the outcomes log and the audit log too.
* **In transit**: delivery to `https` endpoints verifies the receiver's certificate; the service's own port has no TLS, so put a reverse proxy with TLS in front of it.
* **Access**: three bearer tokens (ingest, read, admin); `production = 1` refuses a configuration that leaves a scope open, and refuses `audit-log = 0`.
* **Audit trail**: `audit.log` (`docs/security.md`). It can be edited by whoever can write the data directory: ship it to a store of its own if it is evidence.

## SOC 2: what the service does for each criterion it touches

| criterion | what the service does | what stays the operator's |
|---|---|---|
| CC6.1 logical access | scoped tokens; `production = 1`; bodies encrypted at rest | who holds the tokens; rotating them (a restart); the key file |
| CC6.6, CC6.7 boundaries, transmission | the destination rule (no private addresses unless allowed); `https` verified | TLS in front of the service; the network |
| CC7.2 monitoring | `audit.log`; `/metrics`; `/readyz` says when the audit log cannot be written | collecting and alerting on them |
| CC7.3, CC7.4 incidents | the audit log of reads and changes; refused credentials are written | the response |
| CC8.1 change management | (the project's: CI, the authority pin, design records) | your deployment's reviews |
| A1.2 availability | backup and restore, tested; no acknowledged event lost under `kill -9`; the soak test | running the backups, keeping them, restoring |
| C1.2 disposal | retention, `max-age-days`, erasure, history pruning | backups; receivers |
| P (privacy criteria) | the above for erasure and retention | notices, consent, the legal basis |

## What is not done

No index of bodies by person (an access request needs the ids); no erasure in backups or at receivers; no signing or chaining of audit lines; no TLS on the service's own port; no key held in a KMS (a file only); the type, idempotency key and id of an event are never encrypted or erased.
