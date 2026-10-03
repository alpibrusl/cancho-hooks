# lexsys-hooks

A webhook delivery service written in [lex-sys](https://github.com/alpibrusl/lex-sys): the realistic program that uses every component of the stack (`lexsys-web`, `lexsys-schema`, `lexsys-pg`, `lexsys-cache`, `lexsys-log`) so that what is missing shows up as a failing test.

**Status: step H1b, ingest and delivery to one fixed receiver.** `POST /events` stores an event durably and answers `202` only after the flush that covers it (requests that arrive together share one flush); `GET /events/:id` and `GET /healthz`. Given a receiver (`hooks <port> <dir> <host> <port>`) it delivers the events in order, at least once, and remembers how far it got across crashes. One receiver, no signing, no retry schedule, and a slow receiver stalls ingest (section 14). `docs/design.md` has the claim, the delivery semantics, the scenario fixed before the build, and the gaps it predicts; sections 13 and 14 record what building each step showed.

```sh
scripts/build.sh                                  # needs lex-sys, lexsys-log (its append path) and gcc beside this checkout
build/hooks 8080 /var/lib/hooks &                 # port, data directory
curl -XPOST -d '{"type":"user.created"}' localhost:8080/events      # {"id":1}, after the flush
python3 tests/chaos.py build/hooks 3000 8 40      # kill -9 as a power cut, 150+ times: no acknowledged event may be lost
```

Licence: EUPL-1.2.
