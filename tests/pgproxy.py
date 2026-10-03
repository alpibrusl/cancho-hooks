"""A TCP forwarder in front of PostgreSQL that a test can break on purpose: `mode` is "pass" (forward), "hold" (accept and
read, forward nothing: a database that is up and never answers) or "cut" (close what is open and everything new)."""
import socket
import threading


class PgProxy:
    def __init__(self, host, port):
        self.target = (host, port)
        self.mode = "pass"
        self.conns = []
        self.lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.alive:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            if self.mode == "cut":
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
            threading.Thread(target=self._pipe, args=(c, u), daemon=True).start()
            threading.Thread(target=self._pipe, args=(u, c), daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
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

    def close(self):
        self.alive = False
        self.cut()
        try:
            self.srv.close()
        except OSError:
            pass
