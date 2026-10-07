#!/usr/bin/env python3
"""A webhook receiver for the examples: it VERIFIES every signature, says what it got, and can misbehave on purpose.

    python3 examples/receiver.py --port 9000 --name acme --secret whsec_... [--secret-file PATH] [--show-header X-Customer]
                                 [--fail N | --fail always] [--delay SECONDS]

It needs only python3. The signature is the Standard Webhooks one (https://www.standardwebhooks.com): the header `webhook-signature`
holds one or more `v1,<base64>` separated by spaces, each the HMAC-SHA256, under the decoded secret, of `<webhook-id>.<webhook-timestamp>.<body>`.
tests/examples_test.py checks this code against the reference library (`pip install standardwebhooks`).

  --secret        a secret (`whsec_` and base64) to verify with; give it more than once to accept several
  --secret-file   a file with one secret a line, read again for every request: change the file and the receiver has switched
  --fail N        answer 500 to the first N requests (then 204); `--fail always` answers 500 to every one
  --delay S       wait S seconds before answering (a slow receiver); the line is printed when it answers
  --show-header   print this request header's value too (repeatable)

For every request it prints one line to standard output (flushed), after answering:

    <name> evt_<id> signature ok (<n> signatures) <body>          verified, answered 204
    <name> evt_<id> signature ok (<n> signatures) duplicate       a webhook-id seen before: a receiver drops a repeat, and so does this one
    <name> evt_<id> refused on purpose (500)
    <name> evt_<id> SIGNATURE INVALID                             answered 401

A request that is too old or too new (more than five minutes from this clock) is refused, as the specification says.
"""
import argparse
import base64
import hashlib
import hmac
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOLERANCE_S = 300


def key_of(secret):
    """The HMAC key of a secret: `whsec_` and base64 (the prefix is optional)."""
    s = secret.strip()
    if s.startswith("whsec_"):
        s = s[len("whsec_"):]
    return base64.b64decode(s)


def sign(secret, msg_id, timestamp, body):
    """The `v1,` signature of a delivery: base64 of HMAC-SHA256 under the secret's key of `<id>.<timestamp>.<body>`."""
    content = msg_id.encode() + b"." + str(timestamp).encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(key_of(secret), content, hashlib.sha256).digest()).decode()


def verify(secrets, msg_id, timestamp, header, body, now=None):
    """(True, n) when one of the `n` signatures in the header verifies under one of the secrets; (False, reason) otherwise."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "bad timestamp"
    if abs((time.time() if now is None else now) - ts) > TOLERANCE_S:
        return False, "timestamp outside the tolerance"
    given = [p for p in (header or "").split(" ") if p]
    for secret in secrets:
        want = sign(secret, msg_id, ts, body)
        if any(hmac.compare_digest(want, g) for g in given):
            return True, len(given)
    return False, "no signature matches"


def main():
    ap = argparse.ArgumentParser(description="a webhook receiver that verifies signatures")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="receiver")
    ap.add_argument("--secret", action="append", default=[])
    ap.add_argument("--secret-file")
    ap.add_argument("--fail", default="0", help="N, or 'always'")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--show-header", action="append", default=[])
    a = ap.parse_args()
    if not a.secret and not a.secret_file:
        ap.error("give --secret or --secret-file")
    fail_always = a.fail == "always"
    fail_first = 0 if fail_always else int(a.fail)
    lock = threading.Lock()
    state = {"requests": 0, "seen": set()}

    def secrets_now():
        out = list(a.secret)
        if a.secret_file:
            try:
                with open(a.secret_file) as f:
                    out += [ln.strip() for ln in f if ln.strip()]
            except OSError:
                pass
        return out

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            msg_id = self.headers.get("webhook-id", "")
            with lock:
                state["requests"] += 1
                nth = state["requests"]
            if a.delay:
                time.sleep(a.delay)
            extra = "".join(" %s=%s" % (h, self.headers.get(h)) for h in a.show_header)
            if fail_always or nth <= fail_first:
                self._answer(500, "%s %s refused on purpose (500)%s" % (a.name, msg_id, extra))
                return
            ok, info = verify(secrets_now(), msg_id, self.headers.get("webhook-timestamp"), self.headers.get("webhook-signature"), body)
            if not ok:
                self._answer(401, "%s %s SIGNATURE INVALID (%s)%s" % (a.name, msg_id, info, extra))
                return
            with lock:
                dup = msg_id in state["seen"]
                state["seen"].add(msg_id)
            text = body.decode("utf-8", "replace")
            what = "duplicate" if dup else text
            self._answer(204, "%s %s signature ok (%d signature%s) %s%s" % (a.name, msg_id, info, "" if info == 1 else "s", what, extra))

        def _answer(self, status, line):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(line, flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.daemon_threads = True
    print("%s listening on 127.0.0.1:%d" % (a.name, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
