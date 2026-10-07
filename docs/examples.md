# Examples: the mechanisms by hand

Seven use cases, as scripts you can run and the output they print, are on the [examples page](examples.html) (the scripts are in [`examples/`](../examples); `tests/examples_test.py` runs each and checks that the page says only what it printed). This page is the other half: the same mechanisms by hand, one at a time, against a service you start yourself.

Worked examples against a running service. The quick start in the [README](../README.md) builds and starts it.

These continue from the quick start (`verify.py` is its receiver); each output below is what the commands print, and ids and timestamps differ on every run.

**An endpoint with a filter and a custom header**, created while the service runs. This needs PostgreSQL and an admin token (`docs/endpoints.md`). Stop the quick-start service first:

```sh
createdb hooks && psql hooks -q -f sql/schema.sql
ADMIN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
cat > hooks.conf <<EOF
port = 8080
dir = /tmp/hooks-db
pg-host = 127.0.0.1
pg-user = postgres
pg-database = hooks
admin-token = $ADMIN
allow-private-hosts = 1
EOF
mkdir -p /tmp/hooks-db && build/hooks --config hooks.conf &
```
```
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints \
    -d '{"host":"127.0.0.1","port":9100,"types":["invoice.*"],"headers":{"X-Api-Key":"k123"}}'
{"id":0,"host":"127.0.0.1","scheme":"http","port":9100,"secret":"whsec_...","from":"now","cursor":0}
$ python3 verify.py whsec_... 9100 &       # the secret is in this answer and in no other
$ curl -s -XPOST -d '{"type":"user.created","id":1}' localhost:8080/events      # not wanted: never sent
{"id":1}
$ curl -s -XPOST -d '{"type":"invoice.paid","id":42}' localhost:8080/events
{"id":2}
verified evt_2 {'X-Api-Key': 'k123'} {"type":"invoice.paid","id":42}
```

**Dead letters and bulk replay** (`docs/delivery.md`). Restart the service with `--schedule 100,200` so that events die within a second, add an endpoint on port 9101 with nothing listening, send three events, list the dead letters, then start a receiver and replay them:

```
$ build/hooks --config hooks.conf --schedule 100,200 &
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints -d '{"host":"127.0.0.1","port":9101}'
$ for i in 1 2 3; do curl -s -XPOST -d "{\"type\":\"order.shipped\",\"id\":$i}" localhost:8080/events; done
$ curl -s 'localhost:8080/endpoints/1/dead?limit=2'
{"endpoint":1,"order":"desc","held":3,"truncated":false,"complete_above":0,"dead":[{"event":5,"type":"order.shipped","attempts":3,"reason":"connect_refused","died_at":1791180808831,"replaying":false},{"event":4,"type":"order.shipped","attempts":3,"reason":"connect_refused","died_at":1791180808729,"replaying":false}],"next":4}
$ python3 verify.py whsec_... 9101 &
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/endpoints/1/replay-dead
{"endpoint":1,"taken":3,"remaining":0,"waiting":3,"next":5}
verified evt_3 {} {"type":"order.shipped","id":1}
verified evt_4 {} {"type":"order.shipped","id":2}
verified evt_5 {} {"type":"order.shipped","id":3}
```

**A cron schedule** (UTC; `docs/cron.md`). Each fire is an ordinary event, signed, retried and replayable, made once even if the service is killed in the middle of it:

```
$ curl -s -XPOST -H "Authorization: Bearer $ADMIN" localhost:8080/schedules \
    -d '{"expr":"30 4 * * 1","type":"report.due","body":{"report":"weekly"}}'
{"id":1,"expr":"30 4 * * 1","type":"report.due","body":{"report":"weekly"},"enabled":true,"created_at":1791180812,"last_fired":null,"next_fire":1791779400,"next_fire_at":"2026-10-12T04:30:00Z"}
```

**Scrape the metrics** (`docs/operating.md`; the read token, when one is set, goes in `Authorization: Bearer`):

```
$ curl -s localhost:8080/metrics | grep -E '^hooks_(attempts_total|endpoint_lag_events|ready )'
hooks_ready 1
hooks_attempts_total{outcome="delivered"} 3
hooks_attempts_total{outcome="failed"} 6
hooks_attempts_total{outcome="dead"} 3
hooks_endpoint_lag_events{endpoint="0"} 0
hooks_endpoint_lag_events{endpoint="1"} 0
```

