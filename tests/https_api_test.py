#!/usr/bin/env python3
"""The scheme of an endpoint: how it is written, shown, changed, stored and restored (docs/design.md section 40; production.md P1.7).

    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/https_api_test.py build/hooks      (its tables `endpoints` and `attempts` are emptied)

An endpoint is delivered to over TLS when its host starts with `https://` as it is stored (in `endpoints.conf` and in the `endpoints` table: nothing new in the schema, and a program
that does not know the scheme refuses the line instead of delivering over plain HTTP to port 443). Over the API it is a `scheme` member (`"http"` or `"https"`), or a `url`.

  1. POST /endpoints: `url` (http or https, with or without a port: the scheme's own), `host` and `port` with a `scheme`; the table holds the stored form; the answer and
     GET /endpoints and GET /endpoints/:id say `scheme`, and never the host
  2. every refusal is a 400 with its reason and stores nothing: another scheme, a `url` with a path or a user or a bad port, a `url` beside `host`, an address with https, the scheme
     as a prefix of `host`
  3. PATCH: `scheme` alone changes only the scheme; `host` alone keeps the scheme; `url` changes all three; an address under https is refused whatever order it is asked in, and
     the endpoint is left as it was; a change of scheme or host is delivered to at once (the receiver of the new scheme gets the next event)
  4. the file: an `https://` line is read, listed with its scheme and delivered to; an address under https stops the start (status 13) and the message says which line (in the table: which row, after the table is read); the
     `endpoints.conf` of an old line is what it was; `--import-endpoints` copies the scheme
  5. a backup and a restore keep the scheme: the restored service delivers over TLS again, verified
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
TOKEN = "https-api-test-token"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
check = L.Checks()


class Rig:
    def __init__(self, pki, dns, d=None, extra=(), private=True):
        self.dir = d or L.free_dir("hooks-httpsapi-")
        args = ["--schedule", "60000", "--deadline-ms", "3000", "--dns-server", f"127.0.0.1:{dns.port}", "--tls-ca-file", pki.ca_pem, "--admin-token", TOKEN, *L.pg_flags(), *extra]
        if not private:
            args += ["--allow-private-hosts", "0"]
        self.svc = L.Service(BIN, self.dir, args)

    def api(self, method, path, body=None):
        st, data, _ = self.svc.request(method, path, json.dumps(body).encode() if body is not None else None, {"Authorization": "Bearer " + TOKEN})
        try:
            return st, (json.loads(data) if data else None)
        except ValueError:
            return st, data

    def row(self, ident):
        rows = L.psql(f"select host, port from endpoints where id = {ident}")
        return rows[0] if rows else None

    def close(self):
        self.svc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def fresh_db():
    L.apply_schema()
    L.psql("truncate endpoints, attempts")


def main():
    pki = K.Pki()
    cert = pki.leaf("hooks.test,other.test")
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]})
    srv = K.TlsServer(*cert)
    plain = K.Sink("127.0.0.1")

    # 1. POST
    fresh_db()
    r = Rig(pki, dns, private=False)
    check("1. the service starts with a database and an admin token", r.svc.start(), r.svc.stderr())
    cases = [
        ("a url with a port", {"url": "https://hooks.test:8443"}, ("https", "hooks.test", 8443, "https://hooks.test")),
        ("a url without a port: 443", {"url": "https://hooks.test"}, ("https", "hooks.test", 443, "https://hooks.test")),
        ("a url, http, without a port: 80", {"url": "http://hooks.test"}, ("http", "hooks.test", 80, "hooks.test")),
        ("a url, http, with a port and a final slash", {"url": "http://hooks.test:8080/"}, ("http", "hooks.test", 8080, "hooks.test")),
        ("a host, a port and a scheme", {"host": "other.test", "port": 9443, "scheme": "https"}, ("https", "other.test", 9443, "https://other.test")),
        ("a host and a port: http, as it was", {"host": "8.8.8.8", "port": 9}, ("http", "8.8.8.8", 9, "8.8.8.8")),
        ("a host and a port, with scheme http", {"host": "other.test", "port": 9, "scheme": "http"}, ("http", "other.test", 9, "other.test")),
    ]
    ids = {}
    for label, body, (scheme, host, port, stored) in cases:
        st, out = r.api("POST", "/endpoints", body)
        ok = st == 201 and out["scheme"] == scheme and out["host"] == host and out["port"] == port
        check(f"1. POST {label}: 201, and the answer says {scheme} {host} {port}", ok, str((st, out)))
        if ok:
            ids[label] = out["id"]
            check(f"1. ... the table holds {stored!r} and {port}", r.row(out["id"]) == (stored, str(port)), str(r.row(out["id"])))
    st, listed = r.svc.get_json("/endpoints")
    by_id = {e["id"]: e for e in listed}
    check("1. GET /endpoints says the scheme of each, and no host", sorted(e["scheme"] for e in listed) == sorted(c[2][0] for c in cases) and all("host" not in e for e in listed), str(listed))
    first = ids["a url with a port"]
    st, one = r.svc.get_json(f"/endpoints/{first}")
    check("1. GET /endpoints/:id says https as well", st == 200 and one["scheme"] == "https" and one["port"] == 8443 and "host" not in one, str(one))

    # 2. refusals
    before = L.psql("select count(*) from endpoints")
    refusals = [
        ("another scheme", {"host": "hooks.test", "port": 9, "scheme": "ftp"}),
        ("a scheme that is not a string", {"host": "hooks.test", "port": 9, "scheme": 1}),
        ("an upper-case scheme", {"host": "hooks.test", "port": 9, "scheme": "HTTPS"}),
        ("an address with https", {"host": "8.8.8.8", "port": 443, "scheme": "https"}),
        ("a url with an address and https", {"url": "https://8.8.8.8"}),
        ("a url with a private address and http", {"url": "http://10.0.0.1:9"}),
        ("a url with a path", {"url": "https://hooks.test/hook"}),
        ("a url with a user", {"url": "https://user@hooks.test"}),
        ("a url with a query", {"url": "https://hooks.test?x=1"}),
        ("a url with port 0", {"url": "https://hooks.test:0"}),
        ("a url with port 65536", {"url": "https://hooks.test:65536"}),
        ("a url with a port that is not a number", {"url": "https://hooks.test:abc"}),
        ("a url with no scheme", {"url": "hooks.test"}),
        ("a url with another scheme", {"url": "ftp://hooks.test"}),
        ("a url that is not a string", {"url": 7}),
        ("a url and a host", {"url": "https://hooks.test", "host": "other.test"}),
        ("a url and a port", {"url": "https://hooks.test", "port": 443}),
        ("a url and a scheme", {"url": "https://hooks.test", "scheme": "https"}),
        ("a scheme as a prefix of the host", {"host": "https://hooks.test", "port": 443}),
        ("the other scheme as a prefix of the host", {"host": "http://hooks.test", "port": 80}),
        ("no host and no url", {"port": 443, "scheme": "https"}),
    ]
    bad = []
    for label, body in refusals:
        st, out = r.api("POST", "/endpoints", body)
        if st != 400 or len(json.dumps(out)) < 20:
            bad.append((label, st, out))
    check(f"2. all {len(refusals)} requests that are not an endpoint are a 400 with a reason", not bad, str(bad[:3]))
    check("2. ... and nothing was stored", L.psql("select count(*) from endpoints") == before)
    st, out = r.api("POST", "/endpoints", {"host": "8.8.8.8", "port": 443, "scheme": "https"})
    check("2. the reason of an address with https says that https needs a name", st == 400 and "https endpoint needs a host name" in json.dumps(out), str(out))
    st, out = r.api("POST", "/endpoints", {"url": "https://hooks.test", "host": "x.test"})
    check("2. the reason of a url beside a host says url", st == 400 and "url" in json.dumps(out), str(out))

    # 3. PATCH
    fresh_db()
    r.close()
    r = Rig(pki, dns)
    r.svc.start()
    st, made = r.api("POST", "/endpoints", {"url": f"https://hooks.test:{srv.port}"})
    ident = made["id"]
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"scheme": "http"})
    check("3. PATCH scheme http: 200, the answer says http, the host lost its prefix and the port is the same", st == 200 and out["scheme"] == "http" and out["host"] == "hooks.test"
          and r.row(ident) == ("hooks.test", str(srv.port)), str((st, out, r.row(ident))))
    st, one = r.svc.get_json(f"/endpoints/{ident}")
    check("3. ... GET says http", one["scheme"] == "http", str(one))
    r.svc.post_event(1)
    check("3. ... and the next event goes out in clear: the TLS receiver gets a request that is not a handshake", K_wait(lambda: srv.failed or srv.count(), 10) and srv.count() == 0, f"{srv.failed}")
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"scheme": "https"})
    check("3. PATCH scheme https: back to https://hooks.test", st == 200 and out["scheme"] == "https" and r.row(ident) == ("https://hooks.test", str(srv.port)), str((st, out, r.row(ident))))
    r.svc.post_event(2)
    check("3. ... and the next event is delivered over TLS", K_wait(lambda: 2 in srv.events(), 15), f"{srv.events()}")
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"host": "other.test"})
    check("3. PATCH host alone keeps the scheme", st == 200 and out["scheme"] == "https" and out["host"] == "other.test" and r.row(ident) == ("https://other.test", str(srv.port)), str((st, out, r.row(ident))))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"url": f"http://hooks.test:{plain.port}"})
    check("3. PATCH url changes host, port and scheme at once", st == 200 and out["scheme"] == "http" and r.row(ident) == ("hooks.test", str(plain.port)), str((st, out, r.row(ident))))
    r.svc.post_event(3)
    check("3. ... and the next event goes to the plain receiver, with the name and port as Host", K_wait(lambda: plain.count() == 1, 10) and dict(plain.seen[0]["headers"])["Host"] == f"hooks.test:{plain.port}", f"{plain.seen}")
    r.api("PATCH", f"/endpoints/{ident}", {"scheme": "https", "port": srv.port})
    row = r.row(ident)
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"host": "8.8.8.8"})
    check("3. PATCH host to an address on an https endpoint: 400, and the row is as it was", st == 400 and "https endpoint needs a host name" in json.dumps(out) and r.row(ident) == row, str((st, out, r.row(ident))))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"host": "8.8.8.8", "scheme": "https"})
    check("3. ... with the scheme given in the same request: the same", st == 400 and r.row(ident) == row, str((st, out, r.row(ident))))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"url": "https://8.8.8.8"})
    check("3. ... as a url: the same", st == 400 and r.row(ident) == row, str((st, out, r.row(ident))))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"host": "8.8.8.8", "scheme": "http"})
    check("3. an address with the scheme set to http in the same request is fine", st == 200 and out["scheme"] == "http" and r.row(ident) == ("8.8.8.8", row[1]), str((st, out, r.row(ident))))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"scheme": "https"})
    check("3. PATCH scheme https on an endpoint whose host is an address: 400, as it was", st == 400 and r.row(ident) == ("8.8.8.8", row[1]), str((st, out, r.row(ident))))
    for label, body in (("a scheme that is not one", {"scheme": "gopher"}), ("a url beside a host", {"url": "https://hooks.test", "host": "x.test"}), ("a url that is not a url", {"url": "hooks.test"})):
        st, out = r.api("PATCH", f"/endpoints/{ident}", body)
        check(f"3. PATCH {label}: 400, and the row is as it was", st == 400 and r.row(ident) == ("8.8.8.8", row[1]), str((st, out)))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"scheme": "http"})
    check("3. PATCH of the scheme it already has is a 200 and changes nothing", st == 200 and out["scheme"] == "http" and r.row(ident) == ("8.8.8.8", row[1]))
    st, out = r.api("PATCH", f"/endpoints/{ident}", {"types": ["a.b"]})
    check("3. a change that names neither host nor scheme leaves both (the stored form is rebuilt from the endpoint's own)", st == 200 and out["scheme"] == "http" and r.row(ident) == ("8.8.8.8", row[1]), str((st, out)))
    r.close()

    # 4. the file
    for label, lines, want in (
        ("an https line and a plain line", [f"1 https://hooks.test {srv.port} {L.secret()}", f"2 hooks.test {plain.port} {L.secret()}", f"3 127.0.0.1 {plain.port} {L.secret()}"], 0),
        ("an address under https", [f"1 https://8.8.8.8 443 {L.secret()}"], 13),
        ("a scheme that is not https", [f"1 http://hooks.test 80 {L.secret()}"], 13),
        ("a scheme and nothing else", [f"1 https:// 443 {L.secret()}"], 13),
        ("an upper-case scheme", [f"1 HTTPS://hooks.test 443 {L.secret()}"], 13),
    ):
        d = L.free_dir("hooks-httpsapi-")
        with open(os.path.join(d, "endpoints.conf"), "w") as f:
            f.write("\n".join(lines) + "\n")
        svc = L.Service(BIN, d, ["--dns-server", f"127.0.0.1:{dns.port}", "--tls-ca-file", pki.ca_pem, "--schedule", "60000", "--deadline-ms", "3000"])
        started = svc.start()
        if want == 0:
            st, listed = svc.get_json("/endpoints") if started else (0, [])
            check(f"4. endpoints.conf with {label}: starts, lists the schemes {[e['scheme'] for e in listed]}", started and [e["scheme"] for e in listed] == ["https", "http", "http"], str((svc.stderr(), listed)))
            svc.post_event(5)
            check("4. ... and the https line is delivered to over TLS, the others in clear", K_wait(lambda: 5 in srv.events() and plain.count() >= 2, 15), f"{srv.events()} {plain.count()}")
        else:
            code = svc.wait_exit(5)
            L.wait_for(lambda: "line 1" in svc.stderr(), 3)    # the reader of stderr is a thread: the process may be gone before it has read the last line
            check(f"4. endpoints.conf with {label}: the start is refused with status {want}, naming line 1", not started and code == want and "line 1" in svc.stderr(), f"{code} {svc.stderr()}")
        svc.stop()
        shutil.rmtree(d, ignore_errors=True)
    fresh_db()
    d = L.free_dir("hooks-httpsapi-")
    with open(os.path.join(d, "endpoints.conf"), "w") as f:
        f.write(f"1 https://hooks.test {srv.port} {L.secret()}\n2 plain.test 9 {L.secret()}\n")
    p = subprocess.run([BIN, "--dir", d, "--import-endpoints", "1", *L.pg_flags()], capture_output=True, text=True, timeout=15)
    check("4. --import-endpoints copies the scheme as it is stored", p.returncode == 0 and L.psql("select id, host from endpoints order by id") == [("1", "https://hooks.test"), ("2", "plain.test")], f"{p.returncode} {p.stderr}")
    shutil.rmtree(d, ignore_errors=True)
    d = L.free_dir("hooks-httpsapi-")
    L.psql("update endpoints set host = 'https://8.8.8.8' where id = 1")
    svc = L.Service(BIN, d, ["--dns-server", f"127.0.0.1:{dns.port}", *L.pg_flags()])
    # (since design section 37 the service listens first and reads the table after: the refusal is the exit status 13 that follows, and its message)
    svc.start()
    code = svc.wait_exit(10)
    check("4. a table row with an address under https stops the service (status 13), and the message says which row", code == 13 and "row 1 is not valid" in svc.stderr(), f"{code} {svc.stderr()}")
    shutil.rmtree(d, ignore_errors=True)

    # 5. backup and restore
    fresh_db()
    src = L.free_dir("hooks-httpsapi-")
    r = Rig(pki, dns, src)
    r.svc.start()
    st, made = r.api("POST", "/endpoints", {"url": f"https://hooks.test:{srv.port}"})
    r.svc.post_event(11)
    L.wait_for(lambda: 11 in srv.events(), 15)
    r.svc.stop()
    out = tempfile.mkdtemp(prefix="hooks-httpsapi-bk-")
    pg = ["--pg-database", L.PG_DB, "--pg-host", L.PG_HOST, "--pg-port", str(L.PG_PORT), "--pg-user", L.PG_USER]
    b = subprocess.run([os.path.join(ROOT, "scripts", "backup.sh"), "--dir", src, "--out", out, "--mode", "stopped", *pg], capture_output=True, text=True, env=L.pg_env())
    check("5. a backup of the data directory and the database", b.returncode == 0, b.stderr)
    made_dirs = sorted(os.listdir(out)) if b.returncode == 0 else []
    L.psql("truncate endpoints, attempts")
    dst = L.free_dir("hooks-httpsapi-")
    rs = subprocess.run([os.path.join(ROOT, "scripts", "restore.sh"), "--backup", os.path.join(out, made_dirs[0]), "--dir", dst, *pg], capture_output=True, text=True, env=L.pg_env()) if made_dirs else None
    check("5. the restore", rs is not None and rs.returncode == 0, rs.stderr if rs else "no backup")
    check("5. the restored table holds the stored form", L.psql("select host, port from endpoints") == [("https://hooks.test", str(srv.port))], str(L.psql("select host, port from endpoints")))
    r2 = Rig(pki, dns, dst)
    check("5. the restored service starts", r2.svc.start(), r2.svc.stderr())
    st, listed = r2.svc.get_json("/endpoints")
    check("5. ... it lists the endpoint as https", [e["scheme"] for e in listed] == ["https"], str(listed))
    r2.svc.post_event(12)
    check("5. ... and delivers over TLS again, verified (the restore repeated event 11 as well: at least once)", K_wait(lambda: 12 in srv.events(), 15), str(srv.events()))
    r2.close()
    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(src, ignore_errors=True)

    # 6. the history: the reason of an attempt that failed in the name or in TLS, in the API and in the table, with the coarse status the column always held
    def history(label, hosts, private, dns6, want):
        fresh_db()
        r = Rig(pki, dns6, private=private)
        r.svc.start()
        idents = {}
        for name, (host, port) in hosts.items():
            idents[name] = r.api("POST", "/endpoints", {"host": host, "port": port, "scheme": "https"})[1]["id"]
        r.svc.post_event(1)
        ok = L.wait_for(lambda: r.svc.stats()["history_written"] >= len(hosts), 20)
        st, data = r.svc.get("/events/1/attempts")
        got = {a["endpoint"]: (a["reason"], a["status"], a["outcome"]) for a in json.loads(data)}
        table = {int(e): (int(reason), int(status)) for e, reason, status in L.psql("select endpoint, reason, status from attempts where event = 1")}
        numbers = {"dns_failed": 17, "dns_timeout": 18, "ssrf_refused": 19, "tls_handshake": 20, "cert_untrusted": 21, "cert_expired": 22, "cert_hostname": 23}
        check(f"6. {label}: /events/1/attempts says {', '.join(f'{k}: {v[0]}' for k, v in want.items())}, with the coarse status and outcome failed", ok
              and all(got[idents[k]] == (v[0], v[1], "failed") for k, v in want.items()), f"{got}")
        check(f"6. {label}: the attempts table holds the reason's number and the same status", all(table[idents[k]] == (numbers[v[0]], v[1]) for k, v in want.items()), f"{table}")
        r.close()

    other = K.TlsServer(*pki.selfsigned("hooks.test"))
    expired = K.TlsServer(*pki.expired("hooks.test"))
    wrongname = K.TlsServer(*pki.leaf("elsewhere.test"))
    dns6 = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    history("certificates", {"self": ("hooks.test", other.port), "old": ("hooks.test", expired.port), "name": ("hooks.test", wrongname.port)}, True, dns6,
            {"self": ("cert_untrusted", -1), "old": ("cert_expired", -1), "name": ("cert_hostname", -1)})
    slow = K.DnsStub({"hooks.test": ["127.0.0.1"], "slow.test": ["127.0.0.1"]}, delay={"slow.test": 6.0})
    history("names, under the default policy", {"loop": ("hooks.test", 9), "gone": ("nonexistent.test", 9), "slow": ("slow.test", 9)}, False, slow,
            {"loop": ("ssrf_refused", -1), "gone": ("dns_failed", -1), "slow": ("dns_timeout", -3)})
    for x in (other, expired, wrongname, dns6, slow):
        x.close()

    return check.finish("https api")


def K_wait(cond, secs):
    return L.wait_for(cond, secs)


if __name__ == "__main__":
    sys.exit(main())
