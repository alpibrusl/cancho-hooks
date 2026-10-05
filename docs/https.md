# `https` endpoints and host names

Delivery over TLS with the certificate chain and the host name verified, host names resolved by the service with the destination checked at every attempt, TLS sessions, what it costs and what it needs.

An endpoint is delivered to over TLS when its host starts with `https://` as it is written in `endpoints.conf` and in the `endpoints` table (a name is needed: the certificate is checked against it, and
it is what SNI carries), and over the API as a `scheme` or a `url` (`design.md` section 40):

```
# <id> <host> <port> <secret> ...
7 https://hooks.example.com 443 whsec_...            # TLS 1.2 or 1.3, the chain and the name verified
8 receiver.internal 8080 whsec_...                    # a name, plain HTTP
curl -XPOST -H "Authorization: Bearer $ADMIN" -d '{"url":"https://hooks.example.com"}' localhost:8080/endpoints     # port 443; {"host":..,"port":..,"scheme":"https"} is the same
curl -XPATCH -H "Authorization: Bearer $ADMIN" -d '{"scheme":"http"}' localhost:8080/endpoints/7                       # only the scheme changes
```

* **Verification.** TLS 1.2 at least; the certificate chain must lead to a root in the **system's trust store** (OpenSSL's default locations, `SSL_CERT_FILE` and `SSL_CERT_DIR` honoured), or, with
  `tls-ca-file`, to **exactly** the PEM file named (not the system's as well: use it for a private authority, or for a test). The name in the endpoint is checked against the certificate and is sent as SNI.
  There is no setting that turns verification off. A trust store that cannot be loaded stops the start (status 21). Revocation is not checked; no client certificate.
* **Every failure has a reason of its own**, recorded like any failed attempt (retried on the schedule, dead after it; `GET /events/:id/attempts`, `GET /metrics`, the delivery log): `cert_untrusted`,
  `cert_expired` (also not yet valid), `cert_hostname`, `cert_invalid`, `tls_handshake` (the peer closed or spoke badly, or offers only TLS 1.1 or less), `tls_timeout`, `tls_error`; and for names `dns_failed`,
  `dns_timeout` and `ssrf_refused`.
* **Names** are resolved by the service itself, without blocking the loop, by asking **one name server over TCP** (`dns-server`, else the first IPv4 `nameserver` of `/etc/resolv.conf`). At the write a name only has
  to be a name; **at every attempt** every address it resolves to must be public (unless `allow-private-hosts 1`), and the connection goes to the address that was checked, with nothing resolved in between, so a name
  that is made to point at `127.0.0.1` or `169.254.169.254` is a failed attempt with the reason `ssrf_refused` and no connection (DNS rebinding changes nothing). No `/etc/hosts` (except `localhost`, which is the
  loopback), no search list, no IPv6 (A records only; a name with no IPv4 address is `dns_failed`), no UDP.
* **Sessions.** The service closes the connection after every delivery (`Connection: close`), so `https` costs a handshake a delivery. It keeps one TLS session per endpoint, in memory, and resumes it
  (an abbreviated handshake: about a third less CPU per delivery); it is dropped when the endpoint is changed or deleted, never offered to another name or port, and `tls-resume 0` turns it off. `/metrics` has
  `hooks_tls_handshakes_total{result="full|resumed"}`.
* **Cost** (one shared 4-core VM, the service pinned to a core, 10 endpoints, CPU of the service per delivery; `scripts/bench/https_cost.py`): an address over plain HTTP about 100 us, a name 170 us, `https` with a full
  handshake about 1,000 us, `https` resumed about 700 us. One core therefore does about 10,000 plain deliveries a second, 970 over `https` with full handshakes and 1,460 resumed (worked out from the CPU per delivery; `status.md` has the method). A lookup that is slow, or a handshake
  that never ends, holds nothing: the longest wait of a request to the service while 64 of either were pending was 1 ms (`tests/https_test.py`, `tests/names_test.py`).
* **Needs** OpenSSL 3.0 or later at run time (`libssl3`), its development files to build (`libssl-dev`), and `ca-certificates` for the system's store. The authority this adds is listed above and pinned in
  `authority.json`. The pure lex-sys TLS (lex-sys epic #197) replaces OpenSSL when it can verify chains.
