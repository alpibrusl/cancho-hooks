edition 5;

module perm;

// `perm` -- who else on the machine can read or write the data directory and its files (`production = 1`, `docs/design.md` section 33).
//
// lex-sys has no way to ask for a file's mode: `fs_stat` is not a function, and what a program can learn about a path is only that it opens,
// that it is a directory, and its size (`docs/agent-toolbox.md` in lex-sys, A.2; a reproducer is in `docs/design.md` section 33.4). Opening
// cannot answer the question either: the owner can always open its own file, whatever the group and others may do. So this is the one
// place the service calls into libc, with `Ffi("libc")`, and it calls exactly one function, `statx`, read-only, on a path the settings named.
// That makes the authority report of the service `UNBOUNDED` (it says so about any program that calls foreign code); the day lex-sys has a
// stat builtin this file is a few lines and the report is bounded again.
//
// `statx` and not `stat`: the layout of `struct stat` differs between x86-64 and aarch64, that of `struct statx` does not. The mode is the
// 16-bit field at offset 28, in the byte order of the machine (little-endian on both); `stx_mask`, the first four bytes, says which fields
// the kernel filled in, and a mode it did not fill in is a refusal, not a guess.

// A slice crosses a foreign call as a pointer *and* a length, two arguments. `statx(dirfd, path, flags, mask, buf)` has no length after the
// path, so the length lands in `flags` and the arguments after it are one place early; the declaration is written for that: `mask` is
// where `flags` is in C's list, and the path is passed as an empty slice at the start of its buffer, so the length that lands in `flags`
// is 0 (follow symbolic links, ask for the mode as `stat` would) and the pointer is the start of the NUL-terminated path.
extern fn statx[&f, &p, &b](ffi: &f Ffi("libc"), dirfd: int, path: &p [byte], mask: int, buf: &!b [byte]) -> [ffi("libc")] c_int;

// `AT_FDCWD`.
fn here() -> [] int {
    return 0 - 100;
}

// `STATX_MODE`: ask for the type and permission bits only.
fn want_mode() -> [] int {
    return 2;
}

// The bits that matter: read or write by the group (`0o060`) or by others (`0o006`). Execute (search, for a directory) alone lets nobody
// read or write anything by itself, and the files in it are checked on their own.
pub fn exposed_bits() -> [] int {
    return 54;
}

// The result codes of `check`.
pub fn ok() -> [] int {
    return 0;
}

pub fn absent() -> [] int {
    return 1;
}

pub fn exposed() -> [] int {
    return 2;
}

pub fn unknown() -> [] int {
    return 3;
}

// The permission bits of `path` (NUL-terminated by the caller in `buf`, which is at least 256 bytes), or -1 if the kernel did not say.
fn mode_of[&f, &p, &b](ffi: &f Ffi("libc"), path: &p [byte], buf: &!b [byte]) -> [ffi("libc")] int {
    var i = 0;
    while i < 256 {
        buf[i] = byte_of(0);
        i = i + 1;
    }
    if statx(ffi, here(), path[0..0], want_mode(), buf) != 0 {
        return 0 - 1;
    }
    // stx_mask: bit 1 (STATX_MODE) must be set in the first four bytes.
    if int_of(buf[0]) & want_mode() == 0 {
        return 0 - 1;
    }
    return int_of(buf[28]) + 256 * int_of(buf[29]);
}

// Is the mode one that nobody but the owner may read or write?
pub fn private(mode: int) -> [] bool {
    return mode >= 0 && mode & exposed_bits() == 0;
}

// `<dir>` (when `name` is empty) or `<dir>/<name>` into `out` with a NUL after it; answers the length without the NUL, or -1 if it does not fit.
fn path_to[&o, &d, &n](out: &!o [byte], dir: &d [byte], name: &n [byte]) -> [] int {
    let need = len(dir) + len(name) + 2;
    if need > len(out) {
        return 0 - 1;
    }
    var at = 0;
    while at < len(dir) {
        out[at] = dir[at];
        at = at + 1;
    }
    if len(name) > 0 {
        out[at] = byte_of('/');
        at = at + 1;
        var j = 0;
        while j < len(name) {
            out[at + j] = name[j];
            j = j + 1;
        }
        at = at + len(name);
    }
    out[at] = byte_of(0);
    return at;
}

// Is the path `<dir>/<name>` private? `scratch` is at least 2,300 bytes (a path of up to 2,000 characters and the answer of `statx`).
// Answers `ok()`, `exposed()` (a bit for the group or for others is set), `unknown()` (the kernel did not say, and the path can be opened:
// refuse), or `absent()` (the path does not exist or cannot be opened; for the data directory the caller treats that as a refusal, for a
// file it means there is nothing to check).
pub fn check[&f, &c, &d, &n, &s](ffi: &f Ffi("libc"), fs: &c Fs(""), dir: &d [byte], name: &n [byte], scratch: &!s [byte]) -> [ffi("libc"), fs_read("")] int {
    let n_path = path_to(scratch[0..2048], dir, name);
    if n_path < 0 {
        return unknown();
    }
    let mode = mode_of(ffi, scratch[0..n_path + 1], scratch[2048..2304]);
    if mode >= 0 {
        if private(mode) {
            return ok();
        }
        return exposed();
    }
    // The kernel said nothing: either there is no such path, or something is wrong. If it opens, something is wrong.
    var opens = false;
    match open_read(fs, scratch[0..n_path]) {
        Opened::Failed(e) => {
            opens = false;
        }
        Opened::Ok(rd) => {
            file_close(rd);
            opens = true;
        }
    }
    if opens {
        return unknown();
    }
    return absent();
}

// The names whose modes `files` judges: the directory itself, then the three files the service keeps in it.
pub fn name_of(k: int) -> [] &static [byte] {
    if k == 1 {
        return "events.seg";
    }
    if k == 2 {
        return "delivery.seg";
    }
    if k == 3 {
        return "endpoints.conf";
    }
    return "";
}

// The production profile's judgement of the data directory `dir`: `(0, 0)` if it and every one of its files that exists is private, else
// `(33, k)` if `name_of(k)` can be read or written by the group or by others, or `(35, k)` if its mode could not be read (the exit statuses of
// `docs/design.md` section 33). The directory must exist. `scratch` is at least 2,304 bytes.
pub fn files[&f, &c, &d, &s](ffi: &f Ffi("libc"), fs: &c Fs(""), dir: &d [byte], scratch: &!s [byte]) -> [ffi("libc"), fs_read("")] (int, int) {
    var k = 0;
    while k < 4 {
        let r = check(ffi, fs, dir, name_of(k), scratch);
        if r == exposed() {
            return (33, k);
        }
        if r == unknown() || r == absent() && k == 0 {
            return (35, k);
        }
        k = k + 1;
    }
    return (0, 0);
}
