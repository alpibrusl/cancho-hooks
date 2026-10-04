-- The queries of hooks. `pgen` turns this file into src/queries.ls (see lexsys-pg's README):
--
--     pgen <host> <port> <user> <database> <password|-> sql/queries.sql > src/queries.ls

-- name: add_attempt endpoint event replay attempt outcome status at_ms latency_ms
insert into attempts (endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms) values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict do nothing

-- name: attempts_of event
select endpoint, replay, attempt, outcome, status, at_ms, latency_ms from attempts where event = $1 order by endpoint, replay, attempt limit 200

-- name: endpoints_all
select id, host, port, secret from endpoints order by id

-- name: add_endpoint id host port secret
insert into endpoints (id, host, port, secret) values ($1, $2, $3, $4) on conflict do nothing

-- name: create_endpoint host port secret
insert into endpoints (id, host, port, secret) select greatest(nextval('endpoint_ids'), coalesce((select max(id) from endpoints), -1) + 1), $1::text, $2::int, $3::text returning id
