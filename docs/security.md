# Securing it: tokens, the production profile, authority

The three bearer tokens, what `production = 1` refuses to start without, where the secrets live, and the authority the service holds (the foreign symbols pinned in `authority.json`).

## Tokens and the production profile

Out of the box the service is open: with no token configured anyone who can reach the port can post events, read them, and (bar the
routes that say `403` without an admin token) enable and replay. That is right for a laptop and wrong for anything else. Three bearer tokens,
one for each scope, close it; `production = 1` refuses to run without them.

```
# /etc/hooks/hooks.conf (deploy/hooks.conf.example has the whole sample)
port = 8080
dir = /var/lib/hooks
production = 1
admin-token  = <32 random characters>   # changes configuration or state; also does what the others do
ingest-token = <32 random characters>   # POST /events: give it to the services that send events
read-token   = <32 random characters>   # the GETs: give it to dashboards and monitors (optional: without it the reads need the admin token)
```

* **A token is `Authorization: Bearer <token>`.** A request with none, or one that is none of the configured tokens, is a `401` with
  `WWW-Authenticate: Bearer`. A token that is valid but of a scope that does not reach the route (the read token on `POST /events`) is a `403` that says which
  token the route needs. Where there is no admin token, the routes that have always said so are a `403` ("management is off"): `POST`, `PATCH` and `DELETE /endpoints`
  and every route of `/schedules`.
* **A scope with no token configured is open**: that is what keeps a development service free of ceremony, and it is why a
  half-configured service is not a secured one. `production = 1` is the check that nothing was left out.
* **`GET /healthz` and `GET /readyz` are always open**: they say only that the process is up and whether it is ready (a container's health check and a load balancer carry no secret). `GET /metrics` is not: it is a read route. Whoever can reach the port can also learn that a route exists (a `405` names its methods).
* **Tokens are 8 to 255 visible characters**, compared in constant time (every byte of the longest token there can be is looked at whichever one differs first), and
  appear in no answer, in `GET /config`, in `GET /stats` or on stderr; a token that is refused as a setting is not repeated in the message that refuses it. Put them in the settings file,
  not on the command line (a command line is visible to every user of the host). There are no environment variables.
* **`production = 1` refuses to start**, with an exit status for each cause and a line on stderr that names the setting or the path:

| status | the service refuses because |
|---|---|
| 30 | `admin-token` is not set |
| 31 | `ingest-token` is not set |
| 32 | `allow-private-hosts` is `1` |
| 33 | the data directory, `events.seg`, `delivery.seg` or `endpoints.conf` can be read or written by its group or by others (the directory should be `0700`, the files `0600`; start the service with `umask 077`, which the systemd unit and the container image do, because the logs it makes itself are `0666` less the umask) |
| 34 | two of `admin-token`, `ingest-token` and `read-token` are the same |
| 35 | the mode of the data directory cannot be read (it is not there, or the system call failed) |

  Nothing is opened or created before these are judged, except that a first start in an empty directory makes the logs and judges them
  afterwards. A database is not required in production: without one the endpoints are the file `endpoints.conf`, and `POST /endpoints` and the schedules
  answer `503`. A mode of `0710` (a directory the group may search but not read) is allowed.
* **The database is the trust boundary for secrets.** The endpoints' signing secrets are stored in the clear, in the `endpoints` table (or `endpoints.conf`),
  because the service signs with them; there is no encryption at rest. Anyone who can read the table, or a backup of it, can sign as the service. Give the service's
  database role only what it needs (`select`, `insert`, `update` and `delete` on `endpoints`, `attempts` and `schedules`, and `usage` on the sequence), keep the database off
  any network the receivers can reach, and treat a backup like the secrets it holds (`runbook.md` section 2).
* **What it does not do:** no TLS for the service's own port (put a reverse proxy in front for `https` towards your own clients; delivery **to** `https` receivers is built: [https.md](https.md)), no
  per-token rotation without a restart, no rate limit on failed tokens. A token is the whole of the authentication.

## Authority

The service keeps its two logs in [`lexsys-log`](https://github.com/alpibrusl/lexsys-log) and talks to PostgreSQL through
[`lexsys-pg`](https://github.com/alpibrusl/lexsys-pg). No `unsafe`, and foreign authority only through `Ffi`, per library, for exactly the symbols
[`authority.json`](authority.json) lists, 33 in all (**the report is pinned in CI**: `scripts/check-authority.sh` regenerates it and fails on any difference, so a new foreign symbol is a red diff that only a commit of the new file turns green):

* **libc**, two functions: `statx`, which reads the mode of the data directory for the production profile (`src/perm.ls`; lex-sys has no file-mode builtin: lex-sys#243), and `prctl`, called once at the start to ask the kernel for small pages for the process (`src/thp.ls`, design.md section 46).
  How the service learns it was asked to stop is not foreign: it claims `SIGINT` and `SIGTERM` through lex-sys's signals capability (`Signals("INT,TERM")`, `src/ops.ls`) and
  watches the claim in the same poller as its sockets, so a stop wakes the loop at once.
* **libssl and libcrypto (OpenSSL), 32 functions**, for the TLS client of an `https` endpoint (`src/tls.ls`, section 40): `libssl` `SSL_CTX_new`, `SSL_CTX_free`, `SSL_CTX_ctrl`,
  `SSL_CTX_set_verify`, `SSL_CTX_set_default_verify_paths`, `SSL_CTX_load_verify_file`, `TLS_client_method`, `SSL_new`, `SSL_free`, `SSL_set_bio`, `SSL_set_connect_state`,
  `SSL_do_handshake`, `SSL_read`, `SSL_write`, `SSL_shutdown`, `SSL_get_error`, `SSL_ctrl`, `SSL_get0_param`, `SSL_get_verify_result`, `SSL_session_reused`, `SSL_get1_session`,
  `SSL_set_session`, `SSL_SESSION_is_resumable`, `SSL_SESSION_free`; `libcrypto` `BIO_s_mem`, `BIO_new`, `BIO_read`, `BIO_write`, `ERR_clear_error`, `ERR_get_error`,
  `X509_VERIFY_PARAM_set1_host`, `X509_VERIFY_PARAM_set_hostflags`. No socket reaches OpenSSL (it works on two memory buffers, and the sockets stay lex-sys connections), and no
  function that does anything but TLS is declared. **OpenSSL is C code in the process, and the report cannot say what it does** with the bytes and the memory it is given. It is
  the choice made to have `https` today; the TLS of lex-sys itself (`packages/tls`, lex-sys epic #197) replaces it when it can verify certificate chains (lex-sys #206) and
  has been independently reviewed (#209), and the 32 symbols go with it.

The authority report (`lex-sys authority`) therefore says `bounded: false`, with the symbols above under `unbounded_by`, each with the library the program says it is in (a library
is not an authority domain: the labels bound everything except what those symbols do; `docs/foreign-authority.md` in lex-sys), and names the two signals. A file-mode builtin in lex-sys
would remove the libc part; the pure TLS would remove the rest.

## The audit log

`<dir>/audit.log` (on by default; `production = 1` refuses `audit-log = 0`, status 36) has one line of JSON for each request that reads an event's data, changes anything, or is refused for its credentials: `{"t","seq","scope","method","path","status","fwd"}`. `scope` is the token the request presented (`admin`, `read`, `ingest`, `none`, or `bad` for one that is none of them), `path` carries the ids, `fwd` is `X-Forwarded-For` as it was sent (not trusted: the service does not see the client's address). A change held for the database is written when it is taken (`status` 0) and again, `{"t","seq","status"}`, with its outcome. **No body, no header value and no token is ever written.** Not written: an event that was taken (the events log is its record), `/healthz`, `/readyz`, `/metrics`, `/stats`. The lines of a turn are synced with its group commit, so a line is on disk before the answers of the next turn go out. At `audit-log-bytes` (64 MiB) the file becomes `audit.log.1`; `audit-log-files` (8) are kept. A write that fails is counted (`/stats audit_failures`) and `/readyz` says `audit_log` until one succeeds. The lines can be edited by anyone who can write the data directory: ship them to a store of their own (a log collector reading the file) if they are evidence. `docs/design.md` section 47.1.

## Bodies encrypted at rest

With `encryption-key-file` (32 random bytes, or 64 hexadecimal digits: `openssl rand -hex 32 > hooks.key; chmod 600 hooks.key`) every event's body is stored sealed with ChaCha20-Poly1305 (RFC 8439, lex-sys's own implementation: no foreign call), with the event's id as associated data, so a sealed body cannot be moved to another record; it is opened only to be delivered or read with `GET /events/:id`. The record names the key by the start of its SHA-256, so a key can be **rotated**: start with the new key as `encryption-key-file` and the old one as `encryption-key-file-old` until retention (or `max-age-days`) has dropped the events the old one sealed. A start whose newest event is sealed by a key it was not given ends with status 47 and changes nothing. A log that began in the clear stays readable: events from before the key are read as they are.

**The key is the only way back to the bodies.** Keep it apart from the data directory and from its backups (a backup that holds the key protects nothing), and keep a copy of it as carefully as the backups themselves. Not encrypted: the event's type, its idempotency key and its id, the outcomes log, the history and the endpoints table (whose signing secrets the service needs in the clear). `docs/design.md` section 47.4.

