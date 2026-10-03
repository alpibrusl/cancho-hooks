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
