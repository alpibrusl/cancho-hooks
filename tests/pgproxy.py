"""A TCP forwarder in front of PostgreSQL that a test can break on purpose. `mode` is

  "pass"    forward
  "hold"    accept and read, forward nothing: a database that is up and never answers (what is read is thrown away)
  "cut"     close what is open and everything new (the database went away: a FIN, new connections refused after they are accepted)
  "refuse"  close everything new, leave what is open alone (a database that stopped taking connections)
  "freeze"  stop reading and forwarding in both directions, **losing nothing**: the bytes wait in the kernel's buffers and go on at "pass" (a cable pulled
            and put back: TCP keeps what it was sent)

and `blackhole()` / `restore()` make new connections hang as well: the proxy stops accepting and its queue is full, so a SYN is dropped and the caller's
connect waits for the kernel (what a firewall that drops packets does), while the connections that are open freeze. `kill_backends` ends the PostgreSQL
backends of **this proxy's connections only** with `pg_terminate_backend` (the server sends FATAL 57P01 and closes), never anybody else's.
"""
import socket
import threading
import time


class PgProxy:
    def __init__(self, host, port, backlog=64):
        self.target = (host, port)
        self.mode = "pass"
        self.conns = []
        self.upstream = []
        self.upstream_at = []
        self.accepted = 0
        self.lock = threading.Lock()
        self.accepting = threading.Event()
        self.accepting.set()
        self.dummies = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(backlog)
        self.srv.settimeout(0.05)
        self.backlog = backlog
        self.port = self.srv.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.alive:
            if not self.accepting.is_set():
                time.sleep(0.01)
                continue
            try:
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            if self.mode in ("cut", "refuse"):
                c.close()
                continue
            try:
                u = socket.create_connection(self.target, timeout=5)
                u.settimeout(None)   # the timeout is for connecting: an idle database connection is not an error
            except OSError:
                c.close()
                continue
            with self.lock:
                self.conns += [c, u]
                self.upstream.append(u)
                self.upstream_at.append(time.time())
            threading.Thread(target=self._pipe, args=(c, u), daemon=True).start()
            threading.Thread(target=self._pipe, args=(u, c), daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                while self.mode == "freeze" and self.alive:
                    time.sleep(0.005)
                if self.mode == "hold":
                    continue
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass

    def cut(self):
        self.mode = "cut"
        with self.lock:
            for s in self.conns:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass
            self.conns = []
            self.upstream = []
            self.upstream_at = []

    def restore(self):
        """Back to forwarding; and, after `blackhole`, accepting again."""
        self.unfill()
        self.mode = "pass"

    def blackhole(self):
        """Open connections freeze; new ones hang in connect (the accept queue is full, so the SYN is dropped)."""
        self.mode = "freeze"
        self.accepting.clear()
        time.sleep(0.12)
        for _ in range(self.backlog + 4):
            c = socket.socket()
            c.setblocking(False)
            try:
                c.connect(("127.0.0.1", self.port))
            except BlockingIOError:
                pass
            self.dummies.append(c)
        time.sleep(0.1)

    def unfill(self):
        for c in self.dummies:
            try:
                c.close()
            except OSError:
                pass
        self.dummies = []
        self.accepting.set()

    def upstream_ports(self):
        """The local ports of this proxy's connections to the database, which is how the server sees them (`client_port`)."""
        with self.lock:
            out = []
            for u in self.upstream:
                try:
                    out.append(u.getsockname()[1])
                except OSError:
                    pass
            return out

    def kill_backends(self, psql):
        """`pg_terminate_backend` for the backends this proxy's connections are on, and only those. `psql(sql)` runs SQL. Answers how many were ended.

        They are found by the source port the server sees (`client_port`). Behind something that makes new connections (a container's port mapping, a pooler)
        that port is not ours, so a backend is also taken if it is in this database, comes from the address the test's own psql comes from, is not that psql,
        and was started within a quarter of a second of a connection this proxy made: the test runs one psql at a time, so nothing else of its own is alive."""
        with self.lock:
            socks = [(u, t) for u, t in zip(self.upstream, self.upstream_at)]
        ports, times = [], []
        for u, t in socks:
            try:
                ports.append(u.getsockname()[1])
                times.append(t)
            except OSError:
                pass
        if not ports:
            return 0
        rows = psql("select count(pg_terminate_backend(pid)) from pg_stat_activity where pid <> pg_backend_pid() and datname = current_database() "
                    "and client_addr = inet_client_addr() and (client_port in (" + ",".join(str(p) for p in ports) + ") or exists (select 1 from unnest(array["
                    + ",".join(repr(t) for t in times) + "]::float8[]) as at(t) where abs(extract(epoch from backend_start) - at.t) < 0.25))")
        return int(rows[0][0])

    def close(self):
        self.alive = False
        self.unfill()
        self.cut()
        try:
            self.srv.close()
        except OSError:
            pass
