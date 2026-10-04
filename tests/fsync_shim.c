/* A shim that makes `kill -9` behave like a power cut.
 *
 * A process killed with SIGKILL loses nothing it had already `write`n: the kernel still has it. So a test that only kills
 * the process cannot tell an acknowledgement sent after a flush from one sent before it. This wraps `fsync` and
 * `fdatasync`: after the real call succeeds it records, in `<file>.synced` beside every `*.seg` file and `events.first`, how long the file
 * was when the flush returned. The harness then truncates each file to that length (plus whatever torn tail it chooses)
 * before restarting the service, which is the state a power cut could leave: everything flushed, and an arbitrary part of
 * what was not.
 *
 *     gcc -shared -fPIC -O2 -o build/fsync_shim.so tests/fsync_shim.c -ldl
 *     LD_PRELOAD=build/fsync_shim.so build/hooks ...
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* The files a power cut can leave short: the logs, the snapshot being made, and the manifest of the events log (`events.first`, and the
 * temporary file it is written as before the rename). */
static int tracked(const char *path, size_t n) {
    static const char *const ends[] = {".seg", ".seg.tmp", ".first", ".first.tmp"};
    for (size_t i = 0; i < sizeof ends / sizeof ends[0]; i++) {
        size_t m = strlen(ends[i]);
        if (n > m + 1 && strcmp(path + n - m, ends[i]) == 0) return 1;
    }
    return 0;
}

static void record(int fd) {
    char link[64], path[4096 + 8];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, 4096);
    if (n < 5) return;
    path[n] = 0;
    if (!tracked(path, (size_t)n)) return;
    struct stat st;
    if (fstat(fd, &st) != 0) return;
    strcat(path, ".synced");
    int side = open(path, O_WRONLY | O_CREAT, 0644);
    if (side < 0) return;
    int64_t size = st.st_size;
    ssize_t w = pwrite(side, &size, sizeof size, 0);
    (void)w;
    close(side);
}

/* A file renamed over another is the other: its sidecar goes with it, and the sidecar of the name it replaced is no longer true of anything
 * (a snapshot is fsynced as `delivery.seg.tmp` and renamed to `delivery.seg`; the old `delivery.seg.synced` would cut the new file back). */
int rename(const char *from, const char *to) {
    int (*real)(const char *, const char *) = dlsym(RTLD_NEXT, "rename");
    int r = real(from, to);
    if (r == 0) {
        char a[4096 + 8], b[4096 + 8];
        size_t lf = strlen(from), lt = strlen(to);
        if (lf < 4096 && lt < 4096) {
            snprintf(a, sizeof a, "%s.synced", from);
            snprintf(b, sizeof b, "%s.synced", to);
            if (tracked(to, lt)) {
                if (real(a, b) != 0) unlink(b);
            }
        }
    }
    return r;
}

int fsync(int fd) {
    int (*real)(int) = dlsym(RTLD_NEXT, "fsync");
    int r = real(fd);
    if (r == 0) record(fd);
    return r;
}

int fdatasync(int fd) {
    int (*real)(int) = dlsym(RTLD_NEXT, "fdatasync");
    int r = real(fd);
    if (r == 0) record(fd);
    return r;
}
