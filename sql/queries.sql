-- The queries of hooks. `pgen` turns this file into src/queries.ls (see lexsys-pg's README):
--
--     pgen <host> <port> <user> <database> <password|-> sql/queries.sql > src/queries.ls

-- name: add_attempt endpoint event replay attempt outcome status at_ms latency_ms
insert into attempts (endpoint, event, replay, attempt, outcome, status, at_ms, latency_ms) values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict do nothing
