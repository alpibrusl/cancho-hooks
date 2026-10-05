# Cron: scheduled events

A schedule makes the service append an ordinary event on a cron expression, exactly once even if the service is killed in the middle of a fire. It needs a database and an admin token.

With a database and an `admin-token`, `POST /schedules` makes the service append an event on a schedule (`design.md` section 32):

```sh
psql hooks -f sql/schema.sql    # adds the `schedules` table; the service refuses to start on a database without it
curl -X POST localhost:8080/schedules -H "Authorization: Bearer $TOKEN" \
     -d '{"expr": "30 4 1,15 * 5", "type": "report.due", "body": {"report": "weekly"}}'
# 201 {"id":1,"expr":"30 4 1,15 * 5","type":"report.due","body":{"report":"weekly"},"enabled":true,"created_at":...,"last_fired":null,
#      "next_fire":1791...,"next_fire_at":"2026-10-15T04:30:00Z"}
```

* **The expression** has five fields, `minute hour day-of-month month day-of-week`, each a list of `*`, `a`, `a-b`, `*/n` and `a-b/n` (numbers only; no
  names, no `@daily`), **in UTC**. Sunday is `0` or `7`. As in Vixie cron, if both the day of month and the day of week are restricted a day
  matches when *either* does (`0 0 1 * 1` is the 1st and every Monday), and if one begins with `*` both must (`0 0 * * 1`: Mondays). An expression that cannot
  parse, or that never fires (`0 0 31 2 *`), is a `400` with the reason.
* **A fire is an ordinary event**, `{"type": <type>, "schedule": <id>, "scheduled_at": <Unix second>, "body": <body>}`, appended through the same code as
  `POST /events` with the idempotency key `cron:<id>:<scheduled second>`, so it is stored, delivered, signed, retried and replayed like any other. The key is
  what makes a fire happen **once**: the event is flushed *before* the database is told, and a restart that finds the database one behind finds the key
  and appends nothing.
* **After a stop**, a schedule fires **once** for the time it missed (not once per missed minute), for the last scheduled second more than 10 seconds old; the
  ones within 10 seconds of the restart fire each on its own. With `cron-catchup 0` the missed ones are skipped.
* **The routes** need the admin token, the reads too (`403` without a token configured, `401` without the right one, `503` without a database): `POST /schedules`,
  `GET /schedules` (the list, with each `next_fire`), `GET /schedules/:id`, `PATCH /schedules/:id` (any of `expr`, `type`, `body`, `enabled`; a changed expression
  or an enabled schedule counts from now), `DELETE /schedules/:id`. At most 64 schedules; a body is JSON of at most 1,024 bytes.
* **One service per database.** The schedules are rows, and a second service reading the same table would fire them too, into its own log.
