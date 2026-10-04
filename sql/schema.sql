-- The tables `hooks` writes to when it is given a database (`--pg-host`). The service does not create them:
--
--     createdb hooks && psql hooks -f sql/schema.sql
--
-- The log files stay the truth about delivery (docs/design.md section 3); these tables are what a person or the API reads.

-- One row per delivery attempt that ended. `replay` is 1 for an attempt made for a replay. `outcome` is 1 delivered,
-- 2 failed (it will be tried again), 3 dead (it will not). `status` is the receiver's HTTP status, or a negative reason
-- (-1 could not connect, -2 could not send, -3 timed out, -4 no answer). `at_ms` is when it ended (Unix ms).
-- The key is the attempt's own identity, so a repeat is harmless.
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

-- A table made before endpoint ids could be above 32767 has the column as a smallint: widen it (a no-op on a new one).
alter table attempts alter column endpoint type bigint;

-- The endpoints the service delivers to, when it is given a database (docs/design.md section 24, C3c). `id` is the endpoint's
-- identity (what the API and the history call it), so it is written, never counted, and never reused: at most six digits; `secret` is the Standard
-- Webhooks one (`whsec_` and base64), exactly as it is given to the receiver. It has to be kept in a form the service can sign
-- with, so anyone who can read this table can sign as the service: give the table the permissions of a secret.
create table if not exists endpoints (
    id integer primary key check (id between 0 and 999999),
    host text not null,
    port int not null check (port between 1 and 65535),
    secret text not null
);

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
