# The service with lex-sys's own TLS (`hooks-pure`)

**Status: built, and not the default.** `https` delivery has driven OpenSSL in the process (32 foreign functions, `docs/authority.json`). This is the same service with
lex-sys's own TLS client in its place (`packages/tls`, lex-sys epic #197): `pure/build/hooks-pure`, a second binary, with **no foreign function for TLS** and nothing
linked but libc. **It is not independently reviewed (lex-sys#209), and the default stays OpenSSL until that review is closed.** The design is lex-sys's `docs/tls-hooks.md` (#210); this page is what was
built, what was measured, and what is different.

## Build and run

```
LEX_SYS=<compiler of lex-sys.toml> scripts/build.sh                 # the default service, build/hooks (needs libssl-dev)
LEX_SYS_PURE=<compiler of pure/lex-sys.toml> scripts/build.sh --pure   # pure/build/hooks-pure (needs nothing but the compiler)
```

The two builds pin **different compilers**: the pure one needs `std.gcm`, `std.ecdh` and the packages, which are newer than the default's pin. It takes the same flags and
settings as the default one. (A first version added `tls` to the one project file; that collided, because a project's libraries are built into *every* program of it, and
lex-sys's `tls` package and this service's own OpenSSL module are both called `tls`. The pure build is a project of its own, `pure/lex-sys.toml`.)

## How it is made

`lex-sys` has no function values and no effect polymorphism (lex-sys `docs/effect-polymorphism.md`), so the 11 functions that carry an `Ffi("libssl")` row cannot be
written once for both backends. `scripts/make_pure.py` writes the other one, into `pure/build/src` (not committed, so no copy can drift):

- **`pure/src/tlsx.ls`**, the adapter, is the module that takes the place of `src/tls.ls`. It has the functions `attempt.ls` calls, in the same shape, over `packages/tls`'s
  engine: it moves bytes between the attempt's `Conn` and the engine as `src/tls.ls` does between the `Conn` and OpenSSL's memory BIOs (the loop of lex-sys's
  `tests/programs/tls_many.ls`, 64 connections on one poller). `tlsx.setup` seeds the engine from `/dev/urandom` and loads the trust store.
- **The engine takes the `Ffi`'s place**: in each of the 11 functions `ffi: &f Ffi("libssl")` is `engine: &!f tls.Engine`, in the same position, and the `ffi(...)` entries
  leave the rows. `attempt.advance` also takes the time, for the certificates' dates. `main` opens the engine once (it holds 64 slots), seeds it, loads the trust store and closes
  it at the end of its block.
- **Every change is an exact-match replacement with the number of places it must find**, and the script stops, saying which, if `src/` no longer has exactly that many:
  a change to the shape of these functions cannot be half applied. Line numbers are kept, so a compiler error in the generated source is at the line of `src/` that caused it.

Failures are the same reasons: `tlsx` sets `detail_of` to the `X509_V_ERR_*` number OpenSSL gives for the same fault (lex-sys `docs/tls-pure.md` §8), so `attempt.handshake_code`
is unchanged and the history means the same under either build.

## What is different

| | OpenSSL build | pure build |
|---|---|---|
| Session resumption | yes (`tls-resume`, one session per endpoint), TLS 1.3 and 1.2, a session reused by every connection that wants it | **TLS 1.3 only** (lex-sys `docs/tls-resumption.md`): one ticket per endpoint, **used once**, offered only for the same name, the same trust store, before the leaf expires and within an hour of the full handshake that verified the server. `tls-resume` and `/metrics` as for OpenSSL. *Corrected: this row said "none" before lex-sys#286 built it* |
| The trust store | the system's, `SSL_CERT_FILE` and `SSL_CERT_DIR` honoured, or exactly `tls-ca-file` | `tls-ca-file`, or else the first of `/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/cert.pem`, `/etc/ssl/ca-bundle.pem` that holds a certificate. **The environment is not read**: lex-sys reads no environment variable without a foreign call. A bundle that fills the 2 MiB buffer is refused, never truncated. If nothing loads the service does not start (status 21), as before |
| TLS 1.2 | the extended master secret optional | **required**: a receiver without it fails as `tls_handshake` (lex-sys `docs/tls-assurance.md` §4.1 lists this and five other differences on purpose) |
| Foreign functions | 32 (`libssl` and `libcrypto`) | **0** |
| Linked libraries | libc, libssl, libcrypto | libc (every program links it; nothing is called through `Ffi`) |

## Results

The machine for every number: a Linux VM (Ubuntu 24.04, aarch64, 6 vCPUs, under Virtualization.framework) on an Apple M4 Max. **It was not idle**: about twenty idle service
containers ran in the same VM, the load average was about 1.0 when each run started, and the host was in use. **Both services were built with lex-sys `7c1bd08`**: the default one with
`--ignore-compiler-rev`, because its own pin (`4c27593`) is older, so the comparison is of the two TLS backends and not of two compilers. The default service as it ships, with its own pinned compiler, was not measured here.

**Outcomes.** `python3 scripts/https_both.py` runs `tests/https_test.py` on both builds and compares every check: **97 checks on each, 96 with the same verdict, 1 different on
purpose** (the group 6 check that `SSL_CERT_FILE` is honoured: the pure build reads no environment variable), 0 unexpected. The checks name the outcome (delivered, or failed with which
reason, and nothing else), so the same verdict is the same outcome. That covers a good chain; every certificate failure (expired, not yet valid, another name, another authority,
self-signed, a signature changed by one bit, with and without `tls-ca-file`); a receiver that closes, resets, speaks garbage, offers TLS 1.1 or never answers; a 500, a redirect and a 410; the status line in two
records; a 60,000-byte event to a slow receiver and the same under partial and refused I/O; a certificate that is mended between attempts; `kill -9` in the middle of a handshake; and 64 handshakes
held at once (the longest wait of a request meanwhile 0.2 ms to 2.3 ms, on both).

**The rest of the harness.** Fifteen harnesses that do not need a database (`names`, `attempt`, `retry`, `reason`, `saturation`, `stop`, `ready`, `gone`, `replay`, `breaker`,
`layout`, `config`, `authz`, `isolation`, `scan`) were run against both builds: exit 0 and no failed check on each. The ones that need PostgreSQL were not run against the pure build here (CI has it).

**Authority** (`scripts/check-authority.sh --pure`, pinned in `docs/authority-pure.json`): the foreign symbols go from **32 to 0**, and the report is **`bounded: true`**: the service holds no `Ffi` capability at all. (When this page was written the count was 34 to 2: the two left were `libc:statx`, for the data directory's modes, and `libc:prctl`, for small pages. lex-sys's `dir_mode` and `dir_own_mode` (lex-sys#243) replaced the first, and the second went when the service stopped asking for small pages itself: design.md section 46.) The effects are otherwise the same, plus `dir_read`. The binary links no TLS library
(`ldd pure/build/hooks-pure` shows none).

**Cost** (`scripts/bench/https_cost.py`, the service pinned to one vCPU, the receivers on two others; 600 deliveries a row, 5 runs; CPU of the service per delivery, the median, with
the least and the most; ECDSA P-256 certificates, TLS 1.3). The CPU time is read from `/proc` in clock ticks of 10 ms, so a figure has a resolution of about 17 µs:

| row | endpoints | OpenSSL | pure |
|---|---|---|---|
| `http, name` (the floor: no TLS) | 1 / 10 | 133 (117 to 150) / 67 (33 to 67) µs | 117 (100 to 117) / 33 (33 to 50) µs |
| `https`, a full handshake | 1 / 10 | 700 (483 to 817) / 400 (367 to 400) µs | **2,950** (2,933 to 2,983) / **2,900** (2,867 to 2,933) µs |
| `https`, resumed | 1 / 10 | 250 (233 to 267) / 200 (183 to 200) µs | 3,000 / 2,900 µs before lex-sys#286; 1,900 (1,883 to 1,933) / 1,533 (1,483 to 1,567) µs after, with 401 and 528 of 600 resumed; **1,400 (1,333 to 1,583) / 1,350 (1,333 to 1,367) µs** with a pool of tickets an endpoint (lex-sys#310), 593 and 584 resumed (below) |
| `https`, full, a 50,000-byte event | 1 / 10 | 1,333 (1,200 to 1,367) / 800 (767 to 800) µs | 4,300 (4,267 to 4,400) / 4,067 (4,000 to 4,067) µs |

- **Resumption (lex-sys#286), measured** on the same VM and day as its own run (a second run of the table's rows: OpenSSL full 917 and 383 µs, resumed 250 and 217 µs; pure full
  3,000 and 2,967 µs): the pure build's resumed rows are 1,900 µs with one endpoint and 1,533 µs with ten, where 401 and 528 of the 600 deliveries resumed. Taking the full rows out,
  **a resumed delivery costs about 1,350 µs** (1,353 and 1,337 from the two rows), 2.2 times cheaper than a full one and about 740 a second a core; OpenSSL's resumed is 200 to 250 µs.
  **Why fewer resume:** `https_cost.py` posts every event at once, so several deliveries to one endpoint are in flight together. OpenSSL lends one session to all of them; the pure
  build's ticket is used once (RFC 8446 Appendix C.4), and the service keeps one per endpoint, so the others make full handshakes. The load average was 2.2 when this run started.
- **A pool of tickets an endpoint (lex-sys#310, its `docs/tls-resumption.md` §12).** An endpoint's saved session is now a pool of up to `epx.max_concurrency()` (8) tickets,
  its cap on deliveries in flight: each connection's ticket joins the pool (`tlsx.save_session`), and each new connection takes the newest. Measured again, the same way: **593
  of 600 deliveries resumed with one endpoint and 584 with ten**, at 1,400 and 1,350 µs a delivery, against 401 and 528 at 1,900 and 1,533 µs. `tests/sessions_test.py`'s
  bursts resume 3,192 of 3,200 deliveries, as the OpenSSL build does, against 1,943. Memory does not change: the table is still 1,024 tickets, shared by every endpoint.
- **A full handshake costs the pure build 4 to 7 times what OpenSSL's does** (2.9 ms against 0.4 to 0.7 ms), and **12 to 15 times a resumed one**. Without the floor that is about 2.8 ms
  against 0.3 to 0.6 ms for the handshake alone. A core does about **340** `https` deliveries a second with the pure build, against 1,430 to 2,500 with OpenSSL here
  (`docs/status.md` has about 970 for OpenSSL on a shared 4-core x86-64 VM: the machine is part of the result).
- **The records cost more too, by much less.** A 50,000-byte event adds 1,350 and 1,170 µs to the pure build's delivery and 630 and 400 µs to OpenSSL's: about 15 ns a byte more, about 70 MB/s for
  what the records and the larger event cost together. This is a difference of two rows, so it includes everything that grows with the event (the signature, the log), the same in both.
- **Memory** (`scripts/bench/tls_memory.py`, the resident set after the service has started and been idle, with 8 handshakes held by a receiver that never answers, and with 64):

| | idle | 8 held | 64 held | per held handshake |
|---|---|---|---|---|
| OpenSSL | 7,664 KiB | 8,420 KiB | 11,420 KiB | about 58 KiB |
| pure | 2,972 KiB | 4,176 KiB | 9,784 KiB | about 106 KiB |
| pure, with resumption (lex-sys#286; a table for 1,024 tickets) | 3,144 KiB | 4,356 KiB | 9,964 KiB | about 107 KiB |

  The ticket table is 2.4 MiB for 1,024 endpoints and costs 172 KiB at rest: its pages are not resident until tickets are stored in them.

  The pure build is smaller at the start (OpenSSL's libraries are resident, and the engine's 64 slots are not touched until a connection uses one) and about **twice as large for each connection**; at 64 it is
  still below OpenSSL's total, because of the libraries. The libraries' pages are shared with other processes, so the comparison of totals is not a saving a deployment will see; the per-connection figure is.

**What it means at the delivery rates this service documents.** `docs/capacity.md` has about 700 to 1,000 `https` deliveries a second a core with full handshakes, and that is what bounds the service
to `https` endpoints. The pure build bounds it at about 340 on this machine, a quarter to a seventh of what OpenSSL does here: **a service that delivers fewer than a few hundred `https` events a second needs no more cores with the pure build; one
that delivers more does**, and a deployment that leaned on resumption (1,460 a second on a core in `docs/status.md`, 4,000 to 5,000 on this machine) loses the most. Ingest is not changed. **Not measured:** the effect on the
latency of other requests: the service is one thread, so a handshake of about 2.8 ms of CPU holds the loop for that long where OpenSSL's holds it for 0.3 to 0.6 ms (inferred from the CPU per delivery, not
measured as latency). **Not measured either:** RSA certificate chains (the test authority is P-256), larger chains, and a full-length run.

## The tests of the pure build

```
python3 scripts/https_both.py                          # the https tests on both builds, the outcomes compared: every difference must be listed in the script
PURE=1 python3 scripts/mutate.py tests/mutants/pure.py   # the adapter's mutants (24 mutants of the adapter: 21 killed, 3 survive, below; P23 to P25, the pool, are killed by `sessions`)
python3 tests/pure_test.py pure/build/hooks-pure       # what only the pure build does: an oversize trust store, no environment, close_notify, resumption seen from the receiver
scripts/check-authority.sh --pure                      # the authority report is docs/authority-pure.json
```

CI's `pure-tls` job builds both compilers and both services, checks that the pure binary links no TLS library, that its source is formatted and its authority report is the committed one, and runs
`https_both.py`.

## The mutants

`tests/mutants/pure.py`: 21 changes to `pure/src/tlsx.ls`, each of which a test must catch (`scripts/mutate.py`, on a copy of the tree). **18 are killed** (14 before lex-sys#286's resumption, and the four of it: a ticket not saved, not offered, a resumption not counted, and resumption not advertised in the ClientHello, which needed a receiver that reads the raw ClientHello because Python's `ssl` sends tickets either way): the reason an expired, not yet
valid, wrong-name, unknown-authority or bad-signature certificate is recorded under; a handshake that never reports done; a clock of 0; a trust store that is ignored or never seeded;
partial and refused writes (`tests/https_test.py`'s group 4); a trust store that fills its buffer, and no `close_notify` (`tests/pure_test.py`). Writing them found two gaps in the tests, closed here:
a certificate whose signature is wrong was not tried (`tests/https_test.py` now has it, and it is one of the checks the two builds are compared on), and the oversize bundle was a bundle of
thousands of copies of one certificate, which the store of roots refuses for its own reason, so it did not show that a full buffer is refused.

**Three survive**, each guarding a state the service cannot reach:
- `P07`, the last line of the handshake's handling of a closed socket: by then the engine has failed with `tls-peer-closed`, and the branch before it returns that failure.
- `P10`, a `send` that takes nothing because the engine's output queue is full: `write` has just flushed that queue to the socket, so it is empty.
- `P17`, the engine taking fewer bytes than the socket gave: it takes everything but when its plaintext buffer is full, which needs more than a buffer of data the service has not read, and the
  service reads a status line of 12 bytes and finishes. The path is the engine's contract; lex-sys's own tests of the engine cover it there.

## Not done

- **Making it the default.** That is lex-sys#209's review, and a person's.
- **TLS 1.2 resumption.** lex-sys resumes TLS 1.3 only (its `docs/tls-resumption.md` §3 rule 7), so `tests/sessions_test.py` expects six full handshakes of the pure build's
  TLS 1.2 deliveries, and passes on it (with PostgreSQL). *Corrected:* this said the test failed two checks, the second a burst resuming 1,943 of 3,200; the pool of tickets
  (above) closed it.
- **The environment.** `SSL_CERT_FILE` and `SSL_CERT_DIR` are not read (above). A deployment that sets either names the file with `tls-ca-file`.
- **The other database tests on the pure build**, and the 24-hour soak.
- **RSA chains, and a measurement of the loop's latency under handshakes.**
