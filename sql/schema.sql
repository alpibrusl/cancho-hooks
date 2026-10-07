-- The tables `hooks` writes to when it is given a database (`--pg-host`). The service does not create them:
--
--     createdb hooks && psql hooks -f sql/schema.sql
--
-- The log files stay the truth about delivery (docs/design.md section 3); these tables are what a person or the API reads.

-- One row per delivery attempt that ended. `replay` is 1 for an attempt made for a replay. `outcome` is 1 delivered,
-- 2 failed (it will be tried again), 3 dead (it will not). `status` is the receiver's HTTP status, or a negative reason
-- (-1 could not connect, -2 could not send, -3 timed out, -4 no answer). `reason` says why an attempt failed, finer than that: the numbers of
-- `src/reason.cho` (0 none, 1 connect refused, 2 connect timeout, 3 other connect error, 4 send timeout, 5 send error, 6 no response before the
-- deadline, 7 reset, 8 closed early, 9 bad response, 10 to 12 status 3xx 4xx 5xx, 13 gone, 14 other status, 15 busy, 16 too large, 17 name did not resolve,
-- 18 name lookup timed out, 19 destination refused (the name resolves to a private address), 20 TLS handshake failed, 21 certificate untrusted, 22 certificate expired,
-- 23 certificate does not name the host, 24 certificate invalid, 25 handshake timed out, 26 TLS error).
-- `at_ms` is when it ended (Unix ms). The key is the attempt's own identity, so a repeat is harmless.
create table if not exists attempts (
    endpoint bigint not null,
    event bigint not null,
    replay smallint not null,
    attempt int not null,
    outcome smallint not null,
    status int not null,
    at_ms bigint not null,
    latency_ms int not null,
    primary key (endpoint, event, replay, attempt)
);

-- Asking "what happened to event 41" is the question this table is for.
create index if not exists attempts_event on attempts (event);
-- The rows older than `history-days` are deleted in batches by the service (docs/design.md section 43): by the time of the attempt.
create index if not exists attempts_at on attempts (at_ms);

-- A table made before endpoint ids could be above 32767 has the column as a smallint: widen it (a no-op on a new one).
alter table attempts alter column endpoint type bigint;

-- A table made before the reason of a failure was recorded (docs/design.md section 34.3) has no such column: add it. The rows already there
-- get 0 ("none"), which for a failed attempt means "not recorded". Run this file BEFORE starting a binary that has the column in its queries.
alter table attempts add column if not exists reason smallint not null default 0;

-- The endpoints the service delivers to, when it is given a database (docs/design.md section 24, C3c). `id` is the endpoint's
-- identity (what the API and the history call it), so it is written, never counted, and never reused: at most six digits; `secret` is the Standard
-- Webhooks one (`whsec_` and base64), exactly as it is given to the receiver. `host` is an IPv4 address or a host name, with `https://` in front of it for an endpoint
-- delivered to over TLS (docs/design.md section 40): the scheme is part of the stored host, so there is no column for it and nothing here changed for it. It has to be kept in a form the service can sign
-- with, so anyone who can read this table can sign as the service: give the table the permissions of a secret.
create table if not exists endpoints (
    id integer primary key check (id between 0 and 999999),
    host text not null,
    port int not null check (port between 1 and 65535),
    secret text not null,
    types text not null default '',
    headers text not null default '',
    secret_old text not null default '',
    secret_old_until bigint not null default 0
);

-- What a delivery needs beyond an address (docs/design.md section 35). `types` is the event types the endpoint subscribes to, comma separated
-- (`invoice.paid,user.*`; empty: all events). `headers` is the custom headers every attempt carries, `Name:value` pairs separated by commas, a
-- value percent-encoded (a space is `%20`, a comma `%2C`, a percent `%25`): the table holds credentials, so it is a secret like `secret`.
-- `secret_old` is the previous secret while a rotation overlaps (empty when none) and `secret_old_until` the Unix ms after which it is no longer
-- signed with (0 when none). A table made before these columns existed is given them, with their defaults, by the statements below.
alter table endpoints add column if not exists types text not null default '';
alter table endpoints add column if not exists headers text not null default '';
alter table endpoints add column if not exists secret_old text not null default '';
alter table endpoints add column if not exists secret_old_until bigint not null default 0;

-- How fast an endpoint is attempted (docs/design.md section 39.4). `concurrency` is the most attempts the endpoint has in flight together (1 to 8)
-- and `rate` the most it has *started* in a second (1 to 100000); 0 in either is "follow the service's endpoint-concurrency / endpoint-rate".
-- A table made before these columns existed is given them with 0, which is what it did: the service's settings. Run this file BEFORE starting a
-- binary that has the columns in its queries.
alter table endpoints add column if not exists concurrency int not null default 0 check (concurrency between 0 and 8);
alter table endpoints add column if not exists rate int not null default 0 check (rate between 0 and 100000);

-- The ids `POST /endpoints` gives (docs/design.md section 25.2): a sequence, so that an id is never given twice even after its endpoint is
-- deleted. An id somebody inserted by hand is skipped: the new id is the larger of the sequence and one more than the largest in the table.
create sequence if not exists endpoint_ids minvalue 0 start 0;

-- The schedules the service fires (docs/design.md section 32): a cron expression, the event it makes, and where it is. `expr` has five
-- fields (minute hour day-of-month month day-of-week; UTC), or six with a leading seconds field when the service runs with
-- `cron-seconds 1` (a test mode). `event_type` and `body` (a JSON value, compact) make the event each fire appends. `last_fired` is
-- the scheduled second (Unix seconds) of the last fire, 0 if there has been none. `base` is the second a schedule counts from when its next fire has to
-- be worked out: the second it was created, or last changed or enabled again, so that it does not fire for the time before. `next_fire` is the
-- scheduled second the tick fires next, and 0 when it has to be worked out from `base` (the service does that on its next tick). The service
-- moves `next_fire` and `last_fired` only with a compare-and-set on `base` and `next_fire`, and after the event is in the log.
create table if not exists schedules (
    id bigint generated always as identity primary key,
    expr text not null,
    event_type text not null,
    body text not null default '{}',
    enabled boolean not null default true,
    created_at bigint not null,
    base bigint not null,
    last_fired bigint not null default 0,
    next_fire bigint not null default 0
);

-- The tick asks for the schedules that are due, so that it reads the few rows that matter and not the table.
create index if not exists schedules_due on schedules (next_fire) where enabled;
