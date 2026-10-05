#!/usr/bin/env python3
"""Names, and the SSRF rule at every attempt (docs/design.md sections 26 and 40; production.md P1.7, slice T2).

    python3 tests/names_test.py build/hooks            (needs the `openssl` command for the https rows; no database)

An endpoint's host may be a name. The service resolves it itself, on the poller (it asks the name server `--dns-server` names, or the first of /etc/resolv.conf, over TCP), judges
**every** address of the answer by the ranges of section 26, and connects to the address it judged, as a literal, with nothing resolved between the check and the connection. The
rule is applied at every attempt, because a name's address is not a fact about the endpoint. A name that resolves to a private, loopback, link-local or reserved address is a failed
attempt with the reason `ssrf_refused`, retried like any other, and no connection is made. The name servers here are the test's own (`tests/tlskit.py`), so what each name resolves
to, and how many times it was asked, is known.

  1. the default policy: names that resolve to loopback, private, link-local, shared, reserved and multicast addresses are refused, each by its own attempt; a loopback receiver
     behind them is never connected to; a mixed answer (a public address beside a private one, in either order) is refused whole; `localhost` and `x.localhost` are loopback and
     are not asked of the name server
  2. `allow-private-hosts 1`: the same names are delivered to (plain http and https), with the name and the port as the Host header
  3. an address that is public is dialled (the attempt ends in a connection failure, not in `ssrf_refused`)
  4. rebinding: a name that answers a public address first and 127.0.0.1 second: the second attempt is refused, no connection is made to the loopback receiver, the name is asked
     once per attempt; and under `allow-private-hosts 1` a name that alternates between two loopback receivers is delivered to each in turn, to the address its own lookup gave
  5. a name that does not resolve is `dns_failed`: no such name, a server that fails or refuses, a name with no IPv4 address (IPv6 is not asked for), a name server that is not
     there, one that answers bytes that are not DNS
  6. a slow name server: the deadline passes while the name is being resolved (`dns_timeout`), 64 lookups wait together without holding the loop (the longest wait is
     measured), and 64 lookups of 300 ms take about 300 ms, not 64 times that
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opslib as L  # noqa: E402
import tlskit as K  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
check = L.Checks()
tick_gap = K.tick_gap


class Rig:
    """A service with `hosts` ({name: (scheme, port)}) as endpoints, one event posted, a name server of the test's."""

    def __init__(self, dns, hosts, private=False, deadline=1500, schedule="60000", args=(), dns_port=None):
        self.dns, self.secret, self.dir = dns, L.secret(), L.free_dir("hooks-names-")
        with open(os.path.join(self.dir, "endpoints.conf"), "w") as f:
            for i, (name, (scheme, port)) in enumerate(hosts.items()):
                f.write(f"{i + 1} {scheme}{name} {port} {self.secret}\n")
        self.n = len(hosts)
        server = f"127.0.0.1:{dns_port if dns_port else dns.port}"
        self.svc = L.Service(BIN, self.dir, ["--allow-private-hosts", "1" if private else "0", "--schedule", schedule, "--deadline-ms", str(deadline), "--dns-server", server,
                                             *args])

    def start(self):
        return self.svc.start()

    def reasons(self):
        m = self.svc.metrics()
        return {dict(k)["reason"]: int(v) for k, v in m.series("hooks_attempt_failures_total").items() if v}

    def last(self):
        m = self.svc.metrics()
        return {dict(k)["endpoint"]: dict(k)["reason"] for k in m.series("hooks_endpoint_last_failure")}

    def wait_attempts(self, n, secs=15):
        return L.wait_for(lambda: self.svc.stats()["attempts"] >= n, secs)

    def close(self):
        self.svc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def main():
    plain = {}

    # 1. the default policy
    unsafe = {"loopback": "127.0.0.1", "loopback2": "127.255.255.254", "ten": "10.0.0.1", "ten-end": "10.255.255.255", "private172": "172.16.0.1", "private172-end": "172.31.255.255",
              "private192": "192.168.1.1", "metadata": "169.254.169.254", "linklocal": "169.254.0.1", "shared": "100.64.0.1", "zero": "0.0.0.0", "thisnet": "0.1.2.3",
              "multicast": "224.0.0.1", "reserved": "240.0.0.1", "broadcast": "255.255.255.255", "doc1": "192.0.2.1", "doc2": "198.51.100.1", "doc3": "203.0.113.1",
              "bench": "198.18.0.1", "proto": "192.0.0.8", "relay": "192.88.99.1"}
    dns = K.DnsStub({f"{k}.test": [v] for k, v in unsafe.items()})
    loop = K.Sink("127.0.0.1")
    r = Rig(dns, {f"{k}.test": ("", loop.port) for k in unsafe})
    check("1. the service starts with %d endpoints whose names are fine and whose addresses are not" % len(unsafe), r.start(), r.svc.stderr())
    r.svc.post_event(1)
    check("1. every one is attempted", r.wait_attempts(len(unsafe)), str(r.svc.stats()))
    check("1. every attempt is `ssrf_refused`, and no other reason", r.reasons() == {"ssrf_refused": len(unsafe)}, str(r.reasons()))
    check("1. each endpoint's last failure is that reason", sorted(r.last().values()) == ["ssrf_refused"] * len(unsafe), str(r.last()))
    time.sleep(0.3)
    check("1. the receiver on loopback was never connected to (not once, by anything)", loop.connections == 0, str(loop.connections))
    check("1. every name was asked of the name server once", all(dns.questions(f"{k}.test") == 1 for k in unsafe), str(dns.asked))
    st = r.svc.stats()
    check("1. each is a failed attempt of the retry schedule (nothing is delivered, nothing is dead yet)", st["delivered"] == 0 and st["failed"] == len(unsafe) and st["dead"] == 0, str(st))
    r.close()
    dns.close()

    mixed = {"mixed1": ["192.0.1.1", "10.0.0.1"], "mixed2": ["10.0.0.1", "192.0.1.1"], "mixed3": ["192.0.1.1", "192.0.1.2", "169.254.169.254", "192.0.1.3"],
             "mixed4": ["8.8.8.8", "127.0.0.1"]}
    dns = K.DnsStub({f"{k}.test": v for k, v in mixed.items()})
    r = Rig(dns, {f"{k}.test": ("", loop.port) for k in mixed})
    r.start()
    r.svc.post_event(1)
    check("1. an answer with a public and a private address is refused whole, wherever the private one is", r.wait_attempts(len(mixed)) and r.reasons() == {"ssrf_refused": len(mixed)}, str(r.reasons()))
    r.close()
    dns.close()

    dns = K.DnsStub({})
    r = Rig(dns, {"localhost": ("", loop.port), "app.localhost": ("", loop.port), "LocalHost": ("", loop.port)})
    r.start()
    r.svc.post_event(1)
    check("1. localhost, app.localhost: loopback, refused", r.wait_attempts(3) and r.reasons() == {"ssrf_refused": 3}, str(r.reasons()))
    check("1. ... and not asked of the name server", dns.questions() == 0, str(dns.asked))
    r.close()
    dns.close()
    time.sleep(0.2)
    check("1. ... nor connected to", loop.connections == 0)
    loop.close()

    # 2. allow-private-hosts
    dns = K.DnsStub({f"{k}.test": ["127.0.0.1"] for k in ("alpha", "beta")})
    sink = K.Sink("127.0.0.1")
    r = Rig(dns, {"alpha.test": ("", sink.port), "beta.test": ("", sink.port), "localhost": ("", sink.port), "x.localhost": ("", sink.port)}, private=True)
    check("2. the service starts (allow-private-hosts 1)", r.start(), r.svc.stderr())
    r.svc.post_event(4)
    check("2. a name that resolves to 127.0.0.1 is delivered to, and so is localhost", L.wait_for(lambda: sink.count() == 4, 10) and r.reasons() == {}, f"{sink.count()} {r.reasons()}")
    hosts = sorted(dict(s["headers"])["Host"] for s in sink.seen)
    check("2. the Host header is the name and the port", hosts == sorted(f"{n}:{sink.port}" for n in ("alpha.test", "beta.test", "localhost", "x.localhost")), str(hosts))
    check("2. localhost is not asked of the name server; the others once each", dns.questions("localhost") == 0 and dns.questions("alpha.test") == 1 and dns.questions("beta.test") == 1, str(dns.asked))
    r.close()
    dns.close()
    sink.close()

    # 2b. an https endpoint behind a name, under allow-private-hosts
    pki = K.Pki()
    srv = K.TlsServer(*pki.leaf("hooks.test"))
    dns = K.DnsStub({"hooks.test": ["127.0.0.1"]})
    r = Rig(dns, {"hooks.test": ("https://", srv.port)}, private=True, args=["--tls-ca-file", pki.ca_pem])
    r.start()
    r.svc.post_event(6)
    check("2. an https endpoint behind a name that resolves to loopback is delivered to under allow-private-hosts", L.wait_for(lambda: srv.events() == [6], 10), f"{srv.events()} {r.reasons()}")
    r.close()
    # and refused under the default policy: the same name, the same receiver, no TLS handshake begun
    r = Rig(dns, {"hooks.test": ("https://", srv.port)}, private=False, args=["--tls-ca-file", pki.ca_pem])
    r.start()
    r.svc.post_event(7)
    check("2. ... and refused under the default policy: ssrf_refused, the receiver never connected to", r.wait_attempts(1) and r.reasons() == {"ssrf_refused": 1} and srv.accepted == 1, f"{r.reasons()} {srv.accepted}")
    r.close()
    srv.close()
    dns.close()

    # 2c. the lookup under partial and refused I/O (tests/io_shim.c): the query goes out in pieces, the answer comes in pieces, some calls are refused with EAGAIN
    dns = K.DnsStub({"alpha.test": ["127.0.0.1"]})
    sink = K.Sink("127.0.0.1")
    r = Rig(dns, {"alpha.test": ("", sink.port)}, private=True, deadline=20000)
    r.svc.extra_env = {"LD_PRELOAD": os.path.join(L.ROOT, "build", "io_shim.so"), "SHIM_PORTS": f"{dns.port}", "SHIM_SEND_MAX": "5", "SHIM_RECV_MAX": "3", "SHIM_EAGAIN_EVERY": "2"}
    r.start()
    r.svc.post_event(8)
    check("2. a lookup whose query goes out five bytes at a time and whose answer comes in three, with every second call refused: still resolved, and delivered", L.wait_for(lambda: sink.events() == [8], 30)
          and dns.questions("alpha.test") == 1, f"{sink.events()} {dns.asked} {r.reasons()}")
    r.close()
    dns.close()
    sink.close()

    # 3. a public address is dialled
    public = "192.0.1.1"
    dns = K.DnsStub({"public.test": [public]})
    r = Rig(dns, {"public.test": ("", 9)}, deadline=700)
    r.start()
    r.svc.post_event(1)
    ok = r.wait_attempts(1)
    got = r.reasons()
    check("3. an address that is public is dialled: the attempt fails in the connection, not in the policy", ok and len(got) == 1 and "ssrf_refused" not in got
          and list(got)[0] in ("connect_timeout", "connect_error", "connect_refused"), str(got))
    r.close()
    dns.close()

    # 4. rebinding
    loop = K.Sink("127.0.0.1")
    answers = {"rebind.test": lambda count: ["192.0.1.1"] if count == 0 else ["127.0.0.1"]}
    dns = K.DnsStub(answers)
    r = Rig(dns, {"rebind.test": ("", loop.port)}, deadline=500, schedule="300,300,300")
    r.start()
    r.svc.post_event(1)
    check("4. rebinding: four attempts, the first to the public address", L.wait_for(lambda: r.svc.stats()["attempts"] >= 4, 15), str(r.svc.stats()))
    got = r.reasons()
    first_kind = [k for k in got if k != "ssrf_refused"]
    check("4. the first attempt connected to the public answer (a connection failure), the three after it were refused", len(first_kind) == 1 and first_kind[0].startswith("connect") and got.get("ssrf_refused") == 3, str(got))
    check("4. the name was asked once per attempt, never between the check and the connection", dns.questions("rebind.test") == 4, str(dns.asked))
    time.sleep(0.3)
    check("4. the loopback receiver was never connected to", loop.connections == 0, str(loop.connections))
    r.close()
    dns.close()
    loop.close()

    a, b = K.Sink("127.0.0.1"), None
    b = K.Sink("127.0.0.2", port=a.port)
    dns = K.DnsStub({"flip.test": lambda count: ["127.0.0.1"] if count % 2 == 0 else ["127.0.0.2"]})
    r = Rig(dns, {"flip.test": ("", a.port)}, private=True)
    r.start()
    for n in range(1, 7):
        r.svc.post_event(n)
        L.wait_for(lambda n=n: a.count() + b.count() == n, 10)
    check("4. under allow-private-hosts a name that alternates is delivered to each address in turn: its own lookup's answer", a.events() == [1, 3, 5] and b.events() == [2, 4, 6], f"{a.events()} {b.events()}")
    check("4. one lookup per attempt: six deliveries, six questions", dns.questions("flip.test") == 6, str(dns.asked))
    r.close()
    dns.close()
    a.close()
    b.close()

    # 5. a name that does not resolve
    for label, dns_kw, names in (("no such name (NXDOMAIN)", {}, {}), ("a server that fails (SERVFAIL)", {"rcode": 2}, {"x.test": ["192.0.1.1"]}),
                                 ("a server that refuses (REFUSED)", {"rcode": 5}, {"x.test": ["192.0.1.1"]}), ("a name with no IPv4 address (IPv6 only is no address)", {}, {"x.test": []}),
                                 ("an answer that is not DNS", {"garbage": True}, {"x.test": ["192.0.1.1"]}),
                                 ("an answer to another question (the id is not the query's)", {"wrong_id": True}, {"x.test": ["192.0.1.1"]})):
        dns = K.DnsStub(names, **dns_kw)
        r = Rig(dns, {"x.test": ("", 9)}, deadline=1000)
        r.start()
        r.svc.post_event(1)
        check(f"5. {label}: dns_failed", r.wait_attempts(1) and r.reasons() == {"dns_failed": 1}, str(r.reasons()))
        r.close()
        dns.close()
    dns = K.DnsStub({"x.test": ["192.0.1.1"]})
    r = Rig(dns, {"x.test": ("", 9)}, deadline=1000, dns_port=L.closed_port())
    r.start()
    r.svc.post_event(1)
    check("5. no name server listening: dns_failed", r.wait_attempts(1) and r.reasons() == {"dns_failed": 1}, str(r.reasons()))
    r.close()
    dns.close()

    # 6. a slow name server
    dns = K.DnsStub({"slow.test": ["127.0.0.1"]}, delay=3.0)
    r = Rig(dns, {"slow.test": ("", 9)}, deadline=800, private=True)
    r.start()
    t0 = time.time()
    r.svc.post_event(1)
    ok = r.wait_attempts(1)
    took = time.time() - t0
    check("6. the deadline passes while the name is resolved: dns_timeout, at the deadline", ok and r.reasons() == {"dns_timeout": 1} and 0.7 <= took <= 3.0, f"{r.reasons()} {took:.2f}s")
    r.close()
    dns.close()

    dns = K.DnsStub({"slow.test": ["127.0.0.1"]}, delay=2.0)
    sink = K.Sink("127.0.0.1")
    r = Rig(dns, {"slow.test": ("", sink.port)} | {f"slow{i}.test": ("", sink.port) for i in range(7)}, private=True, deadline=5000)
    dns.names.update({f"slow{i}.test": ["127.0.0.1"] for i in range(7)})
    r.start()
    base = tick_gap(r.svc, 40)
    for n in range(1, 9):
        r.svc.post_event(n)
    check("6. 64 lookups are waiting for a name server that takes two seconds", L.wait_for(lambda: dns.questions() >= 64, 10), f"asked {dns.questions()}")
    loaded = tick_gap(r.svc, 100)
    check(f"6. the service answers a request while they wait: the longest wait {loaded * 1000:.1f} ms (idle: {base * 1000:.1f} ms), where a lookup that blocks would hold it for 2,000", loaded < 0.25, f"{loaded:.3f}")
    check("6. and they are delivered when the answers come", L.wait_for(lambda: sink.count() == 64, 20), str(sink.count()))
    r.close()
    dns.close()
    sink.close()

    dns = K.DnsStub({f"p{i}.test": ["127.0.0.1"] for i in range(8)}, delay=0.3)
    sink = K.Sink("127.0.0.1")
    r = Rig(dns, {f"p{i}.test": ("", sink.port) for i in range(8)}, private=True, deadline=5000)
    r.start()
    t0 = time.time()
    for n in range(1, 9):
        r.svc.post_event(n)
    done = L.wait_for(lambda: sink.count() == 64, 20)
    took = time.time() - t0
    check(f"6. 64 lookups of 300 ms are delivered in {took:.2f} s (together, not one after another: 19.2 s)", done and took < 4.0, f"{sink.count()} {took:.2f}")
    r.close()
    dns.close()
    sink.close()

    return check.finish("names")


if __name__ == "__main__":
    sys.exit(main())
