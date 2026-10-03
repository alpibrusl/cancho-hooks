# lexsys-hooks

A webhook delivery service written in [lex-sys](https://github.com/alpibrusl/lex-sys): the realistic program that uses every component of the stack (`lexsys-web`, `lexsys-schema`, `lexsys-pg`, `lexsys-cache`, `lexsys-log`) so that what is missing shows up as a failing test.

**Status: step H1c.** `POST /events` stores an event durably and answers `202` only after the flush that covers it. Events are delivered, signed (Standard Webhooks, checked against the reference library) and at least once, to the endpoints in `endpoints.conf`; a failed delivery is retried on the Standard Webhooks schedule, a delivery that runs out of retries is a dead letter, and every outcome, with the time of the next attempt, survives a crash. A slow or silent endpoint still costs the others (delivery shares the loop that serves ingest); `docs/design.md` section 15 has the measurements. The claim, the semantics and the scenario fixed before the build are in the design; sections 13 to 15 record what building each step showed.

```sh
scripts/build.sh                                  # needs lex-sys, lexsys-log (its append path) and gcc beside this checkout
build/hooks 8080 /var/lib/hooks &                 # port, data directory
curl -XPOST -d '{"type":"user.created"}' localhost:8080/events      # {"id":1}, after the flush
python3 tests/chaos.py build/hooks 3000 8 40      # kill -9 as a power cut, 150+ times: no acknowledged event may be lost
```

Licence: EUPL-1.2.
