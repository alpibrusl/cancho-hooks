-- The queries of hooks. `pgen` turns this file into src/queries.ls (see lexsys-pg's README):
--
--     pgen <host> <port> <user> <database> <password|-> sql/queries.sql > src/queries.ls

-- name: add_attempt endpoint event replay attempt outcome status at_ms latency_ms reason
insert into attempts (endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms, reason) values ($1, $2, $3, $4, $5, $6, $7, $8, $9) on conflict do nothing

-- name: attempts_of event
select endpoint, replay, attempt, outcome, status, at_ms, latency_ms, reason from attempts where event = $1 order by endpoint, replay, attempt limit 200

-- name: endpoints_all
select id, host, port, secret, types, headers, secret_old, secret_old_until from endpoints order by id

-- name: add_endpoint id host port secret types headers secret_old secret_old_until
insert into endpoints (id, host, port, secret, types, headers, secret_old, secret_old_until) values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict do nothing

-- name: create_endpoint host port secret types headers
insert into endpoints (id, host, port, secret, types, headers) select greatest(nextval('endpoint_ids'), coalesce((select max(id) from endpoints), -1) + 1), $1::text, $2::int, $3::text, $4::text, $5::text returning id

-- name: patch_endpoint id host port secret? types? headers? keep_until?
update endpoints set host = $2::text, port = $3::int, secret_old = case when $4::text is not null then (case when coalesce($7::bigint, 0) > 0 then secret else '' end) when $7::bigint is not null then (case when $7::bigint > 0 then secret_old else '' end) else secret_old end, secret_old_until = case when $4::text is not null then coalesce($7::bigint, 0) when $7::bigint is not null then (case when $7::bigint > 0 and secret_old <> '' then $7::bigint else 0 end) else secret_old_until end, secret = coalesce($4::text, secret), types = coalesce($5::text, types), headers = coalesce($6::text, headers) where id = $1::int returning id

-- name: patch_address id host port
update endpoints set host = $2::text, port = $3::int where id = $1::int returning id

-- name: delete_endpoint id
with gone as (delete from endpoints where id = $1::int returning id) select id, setval('endpoint_ids', greatest(nextval('endpoint_ids'), id), true) from gone

-- name: schedules_due now
select id, expr, event_type, body, base, next_fire from schedules where enabled and next_fire <= $1::bigint order by next_fire, id limit 32

-- name: advance_schedule id was_base was_next fired next
update schedules set last_fired = case when $4::bigint > 0 then $4::bigint else last_fired end, next_fire = $5::bigint where id = $1::bigint and base = $2::bigint and next_fire = $3::bigint

-- name: create_schedule expr event_type body enabled now
insert into schedules (expr, event_type, body, enabled, created_at, base) select $1::text, $2::text, $3::text, $4::boolean, $5::bigint, $5::bigint where (select count(*) from schedules) < 64 returning id, expr, event_type, body, enabled, created_at, last_fired, next_fire

-- name: schedule_by_id id
select id, expr, event_type, body, enabled, created_at, last_fired, next_fire from schedules where id = $1::bigint

-- name: schedules_all
select id, expr, event_type, body, enabled, created_at, last_fired, next_fire from schedules order by id limit 64

-- name: patch_schedule id expr? event_type? body? enabled? now
update schedules set expr = coalesce($2::text, expr), event_type = coalesce($3::text, event_type), body = coalesce($4::text, body), enabled = coalesce($5::boolean, enabled), base = case when $2::text is not null or ($5::boolean and not enabled) then greatest(base, $6::bigint) else base end, next_fire = case when $2::text is not null or ($5::boolean and not enabled) then 0 else next_fire end where id = $1::bigint returning id, expr, event_type, body, enabled, created_at, last_fired, next_fire

-- name: delete_schedule id
delete from schedules where id = $1::bigint returning id
