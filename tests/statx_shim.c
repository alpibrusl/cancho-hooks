/* A shim that makes `statx` fail, or answer without a mode, for the logs.
 *
 * The production profile reads the mode of the data directory and of its files with one call into libc, `statx` (src/perm.ls). The
 * ways that call can go wrong (the kernel refuses; it answers and says it did not fill in the mode) cannot be made to happen by a
 * file's mode, so tests/production_test.py makes them happen here. For a path that ends in `.seg` (events.seg, delivery.seg) the answer is
 * what STATX_SHIM says, and every other path, and every other value of STATX_SHIM, goes to the real `statx`:
 *
 *     STATX_SHIM=fail     -1 and EACCES
 *     STATX_SHIM=nomask   0, with the answer's mask (its first four bytes) zero: nothing was filled in
 *     STATX_SHIM=failall  -1 and EACCES for every path
 *
 *     gcc -shared -fPIC -O2 -o build/statx_shim.so tests/statx_shim.c -ldl
 *     LD_PRELOAD=build/statx_shim.so STATX_SHIM=fail build/hooks ...
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

typedef int (*statx_fn)(int, const char *, int, unsigned int, void *);

int statx(int dirfd, const char *path, int flags, unsigned int mask, void *buf) {
    const char *mode = getenv("STATX_SHIM");
    size_t n = path ? strlen(path) : 0;
    int logs = n > 4 && strcmp(path + n - 4, ".seg") == 0;
    if (mode && strcmp(mode, "failall") == 0) {
        errno = EACCES;
        return -1;
    }
    if (mode && logs && strcmp(mode, "fail") == 0) {
        errno = EACCES;
        return -1;
    }
    if (mode && logs && strcmp(mode, "nomask") == 0) {
        memset(buf, 0, 256);
        return 0;
    }
    statx_fn real = (statx_fn)dlsym(RTLD_NEXT, "statx");
    return real(dirfd, path, flags, mask, buf);
}
