edition 5;

module audit;

import std.buffer;
import std.json;
import store;

// `audit` -- the audit log (`docs/design.md` section 47.1): one line of JSON for each request that reads an event's data, changes anything, or is refused
// for its credentials, in `<dir>/audit.log`. The lines of a turn are kept in a buffer and appended once, with the turn's group commit; at `audit-log-bytes`
// the file is renamed `audit.log.1` (the older ones shift, `audit-log-files` are kept) and a new one begun. No body, no header value and no token is ever
// written: the path, the method, the scope of the token presented (not of the route), the status, and `X-Forwarded-For` as it was sent.
//
// The state is `size()` integers, the caller's (a slice of the delivery state):
//
//     [0] 1 when the log is kept   [1] the last sequence number given   [2] writes that failed   [3] bytes in the current file   [4] audit-log-bytes
//     [5] audit-log-files   [6] 1 after a write failed and until one succeeds (`/readyz` says `audit_log`)   [7] lines written since the start
//     [16 ...] for each connection slot of the server, the sequence number of its request that is held for the database (0: none)

pub fn slots() -> [] int {
    return 1024;
}

pub fn size() -> [] int {
    return 16 + slots();
}

pub fn init[&s](st: &!s [int], on: bool, max_bytes: int, files: int, current: int) -> [] int {
    var i = 0;
    while i < size() {
        st[i] = 0;
        i = i + 1;
    }
    if on {
        st[0] = 1;
    }
    st[3] = current;
    st[4] = max_bytes;
    st[5] = files;
    return 0;
}

pub fn on[&s](st: &s [int]) -> [] bool {
    return st[0] == 1;
}

pub fn failures[&s](st: &s [int]) -> [] int {
    return st[2];
}

pub fn broken[&s](st: &s [int]) -> [] bool {
    return st[6] == 1;
}

pub fn written[&s](st: &s [int]) -> [] int {
    return st[7];
}

// The words for the scope of the token presented (`authz.presented`).
fn scope_word(scope: int) -> [] &static [byte] {
    if scope == 1 {
        return "admin";
    }
    if scope == 2 {
        return "read";
    }
    if scope == 3 {
        return "ingest";
    }
    if scope == 4 {
        return "bad";
    }
    return "none";
}

// The status of an answer, from its first line (`HTTP/1.1 200 ...`); 0 if there is none (the request is held).
pub fn status_of[&b](answer: &b [byte]) -> [] int {
    if len(answer) < 12 {
        return 0;
    }
    var n = 0;
    var i = 9;
    while i < 12 {
        let c = int_of(answer[i]);
        if c < '0' || c > '9' {
            return 0;
        }
        n = n * 10 + c - '0';
        i = i + 1;
    }
    return n;
}

// Whether a request is written: the routes of events, attempts, endpoints, replays, schedules and dead letters, and `/config`; an ingest only when it was
// refused for its credentials; a path that matched no route (somebody looking); never `/healthz`, `/readyz`, `/metrics` or `/stats`. `id` is the route
// (`api.declare`), 0 or less for none.
pub fn wanted(id: int, status: int) -> [] bool {
    if id == 1 || id == 4 || id == 25 || id == 26 {
        return false;
    }
    if id == 2 {
        return status == 401 || status == 403;
    }
    return true;
}

// The line of one request, added to `lines`. `status` is 0 for a request held for the database: its outcome is written by `answered` for the same `slot`.
pub fn request[&h, &s, &m, &p, &f](heap: &!h Heap, lines: buffer.Buffer, st: &!s [int], now: int, scope: int, method: &m [byte], path: &p [byte], fwd: &f [byte], status: int, slot: int) -> [heap] buffer.Buffer {
    var out = lines;
    st[1] = st[1] + 1;
    var w = json.writer(heap, 256);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "t");
    w = json.put_int(heap, w, now);
    w = json.put_key(heap, w, "seq");
    w = json.put_int(heap, w, st[1]);
    w = json.put_key(heap, w, "scope");
    w = json.put_string(heap, w, scope_word(scope));
    w = json.put_key(heap, w, "method");
    w = json.put_string(heap, w, method);
    w = json.put_key(heap, w, "path");
    w = json.put_string(heap, w, path);
    w = json.put_key(heap, w, "status");
    w = json.put_int(heap, w, status);
    if len(fwd) > 0 {
        w = json.put_key(heap, w, "fwd");
        w = json.put_string(heap, w, fwd);
    }
    w = json.end_object(heap, w);
    let line = json.finish(w);
    borrow line as &lr in {
        out = buffer.append(heap, out, buffer.bytes(lr));
    }
    buffer.drop(heap, line);
    out = buffer.append(heap, out, "\n");
    if status == 0 && slot >= 0 && slot < slots() {
        st[16 + slot] = st[1];
    }
    return out;
}

// The outcome of the request held on `slot`, when it is answered: `{"t","seq","status"}`, the sequence number the request was written with.
pub fn answered[&h, &s](heap: &!h Heap, lines: buffer.Buffer, st: &!s [int], now: int, slot: int, status: int) -> [heap] buffer.Buffer {
    var out = lines;
    if slot < 0 || slot >= slots() || st[16 + slot] == 0 {
        return out;
    }
    var w = json.writer(heap, 96);
    w = json.begin_object(heap, w);
    w = json.put_key(heap, w, "t");
    w = json.put_int(heap, w, now);
    w = json.put_key(heap, w, "seq");
    w = json.put_int(heap, w, st[16 + slot]);
    w = json.put_key(heap, w, "status");
    w = json.put_int(heap, w, status);
    w = json.end_object(heap, w);
    let line = json.finish(w);
    borrow line as &lr in {
        out = buffer.append(heap, out, buffer.bytes(lr));
    }
    buffer.drop(heap, line);
    out = buffer.append(heap, out, "\n");
    st[16 + slot] = 0;
    return out;
}

// `<dir>/audit.log` or `<dir>/audit.log.<k>`, into `out`. Answers its length.
fn path_of[&o, &d](out: &!o [byte], dir: &d [byte], k: int) -> [] int {
    var n = store.path_join(out, dir, "audit.log");
    if k > 0 {
        out[n] = byte_of('.');
        n = n + 1 + store.nat_text(out, n + 1, k);
    }
    return n;
}

// The file is full: `audit.log.<files-1>` is removed, each older one moves up, `audit.log` becomes `audit.log.1`.
fn rotate[&c, &d, &s](fs: &c Fs(""), dir: &d [byte], st: &!s [int]) -> [fs_write("")] int {
    var rc = 0;
    region a {
        let from = alloc_slice[a](2112, byte_of(0));
        let to = alloc_slice[a](2112, byte_of(0));
        var k = st[5] - 1;
        let last = path_of(to, dir, k);
        store.remove(fs, to[0..last]);
        while k > 0 {
            let fl = path_of(from, dir, k - 1);
            let tl = path_of(to, dir, k);
            store.rename(fs, from[0..fl], to[0..tl]);
            k = k - 1;
        }
    }
    st[3] = 0;
    return rc;
}

// Append the turn's lines, synced, and empty the buffer. Answers 0, or -1 if the write failed (the lines are lost and counted; `broken` until one succeeds).
pub fn flush[&c, &d, &l, &s](fs: &c Fs(""), dir: &d [byte], lines: &!l buffer.Buffer, st: &!s [int]) -> [fs_write(""), file_write] int {
    let n = buffer.size(lines);
    if n == 0 || st[0] == 0 {
        buffer.clear(lines);
        return 0;
    }
    if st[4] > 0 && st[3] + n > st[4] && st[3] > 0 {
        rotate(fs, dir, st);
    }
    var rc = 0;
    region a {
        let path = alloc_slice[a](2112, byte_of(0));
        let pl = path_of(path, dir, 0);
        if store.append_bytes(fs, path[0..pl], buffer.bytes(lines), true) != 0 {
            rc = 0 - 1;
        }
    }
    if rc == 0 {
        st[3] = st[3] + n;
        st[6] = 0;
        var k = 0;
        let b = buffer.bytes(lines);
        while k < n {
            if int_of(b[k]) == '\n' {
                st[7] = st[7] + 1;
            }
            k = k + 1;
        }
    } else {
        st[2] = st[2] + 1;
        st[6] = 1;
    }
    buffer.clear(lines);
    return rc;
}
