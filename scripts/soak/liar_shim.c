/* A mutant of tests/fsync_shim.c, for the soak's self-test (docs/soak.md, "Validating the checker"): an `fsync` that says it worked and does not always have.
 *
 * The shim of the crash tests records, after every fsync that succeeds, how long the file was; a "power cut" then cuts the file back to that length. This one records
 * only one fsync in four. The service therefore believes a flush made its events durable, acknowledges them, and the cut that follows `kill -9` takes some of them:
 * what a disk that lies about its write cache does. The harness must find the acknowledged events that are gone.
 *
 *     gcc -shared -fPIC -O2 -o liar_shim.so scripts/soak/liar_shim.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static unsigned long calls;

static int tracked(const char *path, size_t n) {
    static const char *const ends[] = {".seg", ".seg.tmp", ".first", ".first.tmp"};
    for (size_t i = 0; i < sizeof ends / sizeof ends[0]; i++) {
        size_t m = strlen(ends[i]);
        if (n > m + 1 && strcmp(path + n - m, ends[i]) == 0) return 1;
    }
    return 0;
}

static void record(int fd) {
    if (calls++ % 4 != 0) return;            /* the lie: three flushes in four are not recorded as durable */
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
