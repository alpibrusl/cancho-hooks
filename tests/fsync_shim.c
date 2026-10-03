/* A shim that makes `kill -9` behave like a power cut.
 *
 * A process killed with SIGKILL loses nothing it had already `write`n: the kernel still has it. So a test that only kills
 * the process cannot tell an acknowledgement sent after a flush from one sent before it. This wraps `fsync` and
 * `fdatasync`: after the real call succeeds it records, in `<file>.synced` beside every `*.seg` file, how long the file
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

static void record(int fd) {
    char link[64], path[4096 + 8];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, 4096);
    if (n < 5) return;
    path[n] = 0;
    if (strcmp(path + n - 4, ".seg") != 0) return;
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
