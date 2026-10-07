/* A shim that makes the status calls of the production profile fail.
 *
 * The production profile reads the permission bits of the data directory with cancho's `dir_own_mode` (`fstat` on the directory's
 * descriptor) and of its files with `dir_mode` (`fstatat` on one name), src/perm.cho. That the kernel refuses them cannot be made to happen
 * by a file's mode, so tests/production_test.py makes it happen here. Every other call, and every other value of STAT_SHIM, goes to the
 * real function:
 *
 *     STAT_SHIM=fail      fstatat on a name that ends in `.seg` (events.seg, delivery.seg): -1 and EACCES
 *     STAT_SHIM=failall   fstat and fstatat on anything: -1 and EACCES
 *
 *     gcc -shared -fPIC -O2 -o build/stat_shim.so tests/stat_shim.c -ldl
 *     LD_PRELOAD=build/stat_shim.so STAT_SHIM=fail build/hooks ...
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

typedef int (*fstatat_fn)(int, const char *, struct stat *, int);
typedef int (*fstat_fn)(int, struct stat *);

static int asked(const char *what) {
    const char *mode = getenv("STAT_SHIM");
    return mode && strcmp(mode, what) == 0;
}

int fstatat(int dirfd, const char *path, struct stat *buf, int flags) {
    size_t n = path ? strlen(path) : 0;
    int log = n > 4 && strcmp(path + n - 4, ".seg") == 0;
    if (asked("failall") || (asked("fail") && log)) {
        errno = EACCES;
        return -1;
    }
    fstatat_fn real = (fstatat_fn)dlsym(RTLD_NEXT, "fstatat");
    return real(dirfd, path, buf, flags);
}

int fstat(int fd, struct stat *buf) {
    if (asked("failall")) {
        errno = EACCES;
        return -1;
    }
    fstat_fn real = (fstat_fn)dlsym(RTLD_NEXT, "fstat");
    return real(fd, buf);
}
