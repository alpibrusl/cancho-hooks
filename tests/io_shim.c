/* LD_PRELOAD shim for the partial-I/O tests (tests/https_test.py, tests/names_test.py): makes `send` and `recv` on the connections to chosen ports behave
 * the way a busy kernel does.
 *
 *   cc -O2 -shared -fPIC -o build/io_shim.so tests/io_shim.c -ldl
 *   SHIM_PORTS=9443,5353 SHIM_SEND_MAX=700 SHIM_RECV_MAX=300 SHIM_EAGAIN_EVERY=3 LD_PRELOAD=build/io_shim.so build/hooks ...
 *
 * On a socket whose peer port is in SHIM_PORTS, `send` takes at most SHIM_SEND_MAX bytes of what it is given, `recv` returns at most SHIM_RECV_MAX, and every
 * SHIM_EAGAIN_EVERY-th call of either answers -1 with EAGAIN without touching the socket. Sockets to other ports (the service's own clients, the database) are
 * left alone. Real sockets on loopback accept 60 KB in one call, so the branches that handle a write the kernel takes in pieces, a write that has to wait, a
 * TLS record or a DNS answer that arrives in pieces and a read with nothing yet are not reached by an ordinary run (design.md section 16 says the same of the
 * plain attempt's partial-write branch; the lex-sys TLS spike found it for its own).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

static long env(const char *k, long d) { const char *v = getenv(k); return v ? atol(v) : d; }
static long calls_send, calls_recv;

static int shimmed(int fd) {
    const char *ports = getenv("SHIM_PORTS");
    struct sockaddr_in a;
    socklen_t n = sizeof a;
    if (!ports || getpeername(fd, (struct sockaddr *)&a, &n) != 0 || a.sin_family != AF_INET) return 0;
    char list[256];
    strncpy(list, ports, sizeof list - 1);
    list[sizeof list - 1] = 0;
    for (char *p = strtok(list, ","); p; p = strtok(NULL, ","))
        if (atoi(p) == ntohs(a.sin_port)) return 1;
    return 0;
}

ssize_t send(int fd, const void *buf, size_t len, int flags) {
    static ssize_t (*real)(int, const void *, size_t, int);
    if (!real) real = dlsym(RTLD_NEXT, "send");
    if (shimmed(fd)) {
        long every = env("SHIM_EAGAIN_EVERY", 0), max = env("SHIM_SEND_MAX", 0);
        if (every && ++calls_send % every == 0) { errno = EAGAIN; return -1; }
        if (max && len > (size_t)max) len = max;
    }
    return real(fd, buf, len, flags);
}

ssize_t recv(int fd, void *buf, size_t len, int flags) {
    static ssize_t (*real)(int, void *, size_t, int);
    if (!real) real = dlsym(RTLD_NEXT, "recv");
    if (shimmed(fd)) {
        long every = env("SHIM_EAGAIN_EVERY", 0), max = env("SHIM_RECV_MAX", 0);
        if (every && ++calls_recv % every == 0) { errno = EAGAIN; return -1; }
        if (max && len > (size_t)max) len = max;
    }
    return real(fd, buf, len, flags);
}
