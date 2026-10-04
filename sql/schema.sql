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
    endpoint smallint not null,
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

-- The endpoints the service delivers to, when it is given a database (docs/design.md section 24, C3c). `id` is the endpoint's
-- identity in the outcome log, so it is written, never counted, and below the service's limit of 16; `secret` is the Standard
-- Webhooks one (`whsec_` and base64), exactly as it is given to the receiver. It has to be kept in a form the service can sign
-- with, so anyone who can read this table can sign as the service: give the table the permissions of a secret.
create table if not exists endpoints (
    id smallint primary key check (id >= 0),
    host text not null,
    port int not null check (port between 1 and 65535),
    secret text not null
);
