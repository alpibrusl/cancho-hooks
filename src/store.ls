edition 5;

module store;

// `store` -- the small file operations the retention code is made of (`docs/retention.md`).
//
// No file is held between calls: each of these opens the path, does one thing, and closes it. That is the design, not an
// economy: an object that holds a file cannot switch to another while it is borrowed (a resource cannot be replaced through a
// mutable reference), and a segment of the events log has to be able to appear and disappear. An answer of 0 is success; any
// other is an errno (positive), or, where it says so, a negative errno.

// `<dir>/<name>` into `out`; answers its length.
pub fn path_join[&o, &d, &n](out: &!o [byte], dir: &d [byte], name: &n [byte]) -> [] int {
    var i = 0;
    while i < len(dir) {
        out[i] = dir[i];
        i = i + 1;
    }
    out[i] = byte_of('/');
    var j = 0;
    while j < len(name) {
        out[i + 1 + j] = name[j];
        j = j + 1;
    }
    return i + 1 + len(name);
}

// The decimal digits of `n` (not negative) at `out[at..]`; answers how many.
pub fn nat_text[&o](out: &!o [byte], at: int, n: int) -> [] int {
    var width = 1;
    var t = n;
    while t >= 10 {
        t = t / 10;
        width = width + 1;
    }
    var k = width;
    var m = n;
    while k > 0 {
        out[at + k - 1] = byte_of('0' + m % 10);
        m = m / 10;
        k = k - 1;
    }
    return width;
}

// The name of segment `k` of the events log: `events.seg` for 0 (the name an existing deployment has), `events-<k>.seg` after it.
pub fn seg_name[&o](out: &!o [byte], k: int) -> [] int {
    var n = 0;
    let stem = "events";
    while n < len(stem) {
        out[n] = stem[n];
        n = n + 1;
    }
    if k > 0 {
        out[n] = byte_of('-');
        n = n + 1;
        n = n + nat_text(out, n, k);
    }
    let tail = ".seg";
    var i = 0;
    while i < len(tail) {
        out[n + i] = tail[i];
        i = i + 1;
    }
    return n + len(tail);
}

// `<dir>/<name of segment k>` into `out`.
pub fn seg_path[&o, &d](out: &!o [byte], dir: &d [byte], k: int) -> [] int {
    // No `return` inside the region: a region left by a `return` is not given back (docs/lexsys-log-retention.md, gap 6).
    var n = 0;
    region a {
        let name = alloc_slice[a](40, byte_of(0));
        let m = seg_name(name, k);
        n = path_join(out, dir, name[0..m]);
    }
    return n;
}

// The size of the file at `path`, or `0 - errno` (so `0 - 2` for one that does not exist).
pub fn size_of[&c, &p](fs: &c Fs(""), path: &p [byte]) -> [fs_read(""), file_read] int {
    match open_read(fs, path) {
        Opened::Failed(e) => {
            return 0 - e;
        }
        Opened::Ok(rd0) => {
            var rd = rd0;
            var size = 0 - 5;
            borrow mut rd as &!rh in {
                match file_size(rh) {
                    Done::Ok(n) => {
                        size = n;
                    }
                    Done::Failed(e) => {
                        size = 0 - e;
                    }
                }
            }
            file_close(rd);
            return size;
        }
    }
}

// Up to `len(into)` bytes of the file at `path`, from byte `at`. Answers how many (0 at the end), or `0 - errno`.
pub fn read_range[&c, &p, &b](fs: &c Fs(""), path: &p [byte], at: int, into: &!b [byte]) -> [fs_read(""), file_read] int {
    match open_read(fs, path) {
        Opened::Failed(e) => {
            return 0 - e;
        }
        Opened::Ok(rd0) => {
            var rd = rd0;
            var got = 0 - 5;
            borrow mut rd as &!rh in {
                match file_pread(rh, at, into) {
                    Read::Got(n) => {
                        got = n;
                    }
                    Read::End => {
                        got = 0;
                    }
                    Read::Failed(e) => {
                        got = 0 - e;
                    }
                }
            }
            file_close(rd);
            return got;
        }
    }
}

// Append `data` to the file at `path` (made if it is not there) and, if `sync`, `fsync` it. Answers 0 or an errno. After a nonzero answer the
// file's contents are unknown (a failed write or sync is never retried).
pub fn append_bytes[&c, &p, &b](fs: &c Fs(""), path: &p [byte], data: &b [byte], sync: bool) -> [fs_write(""), file_write] int {
    match open_append(fs, path) {
        Opened::Failed(e) => {
            return e;
        }
        Opened::Ok(w0) => {
            var w = w0;
            var rc = 0;
            var done = 0;
            borrow mut w as &!wh in {
                while done < len(data) && rc == 0 {
                    match file_write(wh, data[done..len(data)]) {
                        Done::Ok(n) => {
                            if n <= 0 {
                                rc = 5;
                            }
                            done = done + n;
                        }
                        Done::Failed(e) => {
                            rc = e;
                        }
                    }
                }
                if rc == 0 && sync {
                    match file_sync(wh) {
                        Done::Ok(n) => {
                        }
                        Done::Failed(e) => {
                            rc = e;
                        }
                    }
                }
            }
            file_close(w);
            return rc;
        }
    }
}

// Make `path` a new file holding `data`, truncating whatever was there; `fsync` it if `sync`. Answers 0 or an errno.
pub fn write_file[&c, &p, &b](fs: &c Fs(""), path: &p [byte], data: &b [byte], sync: bool) -> [fs_write(""), file_write] int {
    match open_write(fs, path) {
        Opened::Failed(e) => {
            return e;
        }
        Opened::Ok(w0) => {
            var w = w0;
            var rc = 0;
            var done = 0;
            borrow mut w as &!wh in {
                while done < len(data) && rc == 0 {
                    match file_write(wh, data[done..len(data)]) {
                        Done::Ok(n) => {
                            if n <= 0 {
                                rc = 5;
                            }
                            done = done + n;
                        }
                        Done::Failed(e) => {
                            rc = e;
                        }
                    }
                }
                if rc == 0 && sync {
                    match file_sync(wh) {
                        Done::Ok(n) => {
                        }
                        Done::Failed(e) => {
                            rc = e;
                        }
                    }
                }
            }
            file_close(w);
            return rc;
        }
    }
}

// Create the file at `path`, empty; refuses (17, EEXIST) if it is there. Answers 0 or an errno.
pub fn create_new[&c, &p](fs: &c Fs(""), path: &p [byte]) -> [fs_write("")] int {
    match open_new(fs, path) {
        Opened::Failed(e) => {
            return e;
        }
        Opened::Ok(w) => {
            file_close(w);
            return 0;
        }
    }
}

// `fsync` the file or directory at `path`. Answers 0 or an errno. (`fsync` of a descriptor opened read-only flushes the file; of a
// directory, its entries: a created, renamed or removed name.)
pub fn sync_path[&c, &p](fs: &c Fs(""), path: &p [byte]) -> [fs_read(""), file_write] int {
    match open_read(fs, path) {
        Opened::Failed(e) => {
            return e;
        }
        Opened::Ok(rd0) => {
            var rd = rd0;
            var rc = 0;
            borrow mut rd as &!rh in {
                match file_sync(rh) {
                    Done::Ok(n) => {
                    }
                    Done::Failed(e) => {
                        rc = e;
                    }
                }
            }
            file_close(rd);
            return rc;
        }
    }
}

// Rename `from` to `to` (atomic, within a filesystem). Answers 0 or an errno.
pub fn rename[&c, &a, &b](fs: &c Fs(""), from: &a [byte], to: &b [byte]) -> [fs_write("")] int {
    match fs_rename(fs, from, to) {
        Done::Ok(n) => {
            return 0;
        }
        Done::Failed(e) => {
            return e;
        }
    }
}

// Remove the file at `path`. Answers 0 or an errno (2 if there was none).
pub fn remove[&c, &p](fs: &c Fs(""), path: &p [byte]) -> [fs_write("")] int {
    match fs_remove(fs, path) {
        Done::Ok(n) => {
            return 0;
        }
        Done::Failed(e) => {
            return e;
        }
    }
}

// An advisory lock held on a file, or why there is none.
pub enum Locked {
    Got(File),
    Busy,
    Failed(int),
}

// Try, without waiting, to take the exclusive lock on the file at `path` (made if it is not there). The lock lasts until the file is closed
// (`release_lock`) or the process ends, however it ends.
pub fn try_lock[&c, &p](fs: &c Fs(""), path: &p [byte]) -> [fs_write(""), file_write] Locked {
    match open_append(fs, path) {
        Opened::Failed(e) => {
            return Locked::Failed(e);
        }
        Opened::Ok(f0) => {
            var f = f0;
            var rc = 0;
            borrow mut f as &!fh in {
                match file_lock(fh) {
                    Done::Ok(n) => {
                    }
                    Done::Failed(e) => {
                        rc = e;
                    }
                }
            }
            if rc == 0 {
                return Locked::Got(f);
            }
            file_close(f);
            // EWOULDBLOCK: 11 on Linux, 35 on macOS.
            if rc == 11 || rc == 35 {
                return Locked::Busy;
            }
            return Locked::Failed(rc);
        }
    }
}

pub fn release_lock(f: File) -> [] int {
    return file_close(f);
}
