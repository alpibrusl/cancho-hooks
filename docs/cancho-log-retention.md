# What `cancho-log` would need for retention, and the cancho gaps this work met

*A proposal, not a request that blocks anything: `docs/retention.md` is built without changing `cancho-log` (pinned in `cancho.toml` at `6b4f46f`). Everything below replaces code in this repository (`src/evlog.cho`, `src/store.cho`, the snapshot in `src/compact.cho`) with a library's. Where the library cannot do it soundly today, the reason is a cancho gap, listed at the end with a reproducer.*

## 1. What the library is, and what retention needed

`cancho-log` is one file: `log.Log` holds an append handle and a read handle to a single segment, `recover` cuts a torn tail, `append`/`flush` give group commit, `read_at(offset)` reads only what a flush covered. Ids must increase within the segment, and offsets are physical.

Retention needed four things the library does not have:

| need | what this repository did instead |
|---|---|
| **a chain of segments**, with logical offsets that survive dropping the front | `evlog`: `events.seg`, `events-N.seg`, a manifest `events.first`, a header per segment with its base, a table in memory, a read cache |
| **a format version in the file** and a refusal on an unknown one | a header record per segment (`evlog.put_header`, `parse_head`) and one in `delivery.seg` (kind 15); the refusal is in `evlog.open` and `rt_check_format` |
| **replace a log by a snapshot**, atomically | write `delivery.seg.tmp`, `fsync`, open it, `rename` over `delivery.seg`, `fsync` the directory (`compact.cho: rt_snapshot`) |
| **durable file-system steps**: create, rename, remove, `fsync` of a directory, a lock | `store.cho` |

## 2. The proposal

### 2.1 A `Chain` next to `Log` (new module `chain`)

```
pub res struct Chain          // a directory of segments: <stem>.seg, <stem>-1.seg, ..., with a manifest <stem>.first
pub fn open[&c, &d, &w](fs: &c Fs(p), dir: &d [byte], stem: &static [byte], window: &!w [byte], max_len: int, segment_bytes: int, now_ms: int) -> [...] Opened
pub fn append[&k, &r](ch: &!k Chain, rec: &r [byte], ms: int, seq: int) -> [] int          // as log.append; stages, no I/O
pub fn flush[&k](ch: &!k Chain) -> [file_write] int                                        // group commit over the active segment
pub fn read_at[&k, &b](ch: &!k Chain, at: int, buf: &!b [byte]) -> [file_read] (int, int)  // `at` is a logical offset
pub fn first_offset[&k](ch: &k Chain) -> [] int                                            // the oldest retained
pub fn roll[&k](ch: &!k Chain, now_ms: int) -> [...] int                                   // seal the active segment, begin the next (crash-safe, 5 steps)
pub fn drop_front[&k](ch: &!k Chain) -> [...] int                                          // remove the oldest sealed segment (manifest first, then the file)
pub fn segments[&k](ch: &k Chain) -> [] int
pub fn sealed_at[&k](ch: &k Chain, i: int) -> [] (int, int)                                // (last id, when sealed) of the i-th oldest sealed segment
```

Semantics are exactly `src/evlog.cho`'s, whose tests (`tests/retention_test.py`, the 19-step kill matrix) would move into the library's: logical offsets, the header with `format`/`segment`/`created`, the chain check at open, recovery of the last segment, a roll that did not finish repaired, a gap refused, the manifest written before the file is removed. The library would own the **format version** (its header's number) and refuse an unknown one with a status of its own. The service would keep policy: *which* segment may go (`retain.may_drop`), *when* to roll (`retain.should_roll`).

### 2.2 `Log` gets a header and a version

`log.Log` could write the same header as its first record and refuse a different version on `recover`. A file without a header is version 1 (what exists). This is what `docs/production.md` P2 asks for ("log format versions and a refusal, not a guess, on an unknown version") and what `docs/runbook.md` section 5 is waiting for.

### 2.3 `log.replace`

```
pub fn replace[&c, &p](fs: &c Fs(p), lg: Log, snapshot: &s [byte], window: &!w [byte], max_len: int) -> [...] (Log, int)
```
Write `<path>.tmp`, `fsync`, **open the new file's handles before the rename** (so they are the handles of the file that will have the name; there is no moment when the caller holds a handle to a file that is not the log), rename, `fsync` the directory, close the old, return the new `Log`. It takes and returns the `Log` by value because a resource can only be replaced by its owner (gap 1 below). `rt_snapshot` is this function.

### 2.4 `fs_sync_dir`, `fs_list` (cancho)

`file-writes.md` section 5.2 says a directory is synced by opening it and calling `file_sync`; that works and is what `store.sync_path` does, but a named builtin would make the intent checkable. The more important absence is **a directory listing** (`fs_list`, `file-writes.md` section 8): without it the chain's first segment must be named by a manifest, and a hand-deleted file cannot be told from a dropped one except by the manifest.

## 3. The cancho gaps, each with its reproducer

1. **A resource cannot be replaced through a mutable reference.** An object that holds a file cannot switch to another file while it is borrowed: there is no `take`/`replace`/`swap`, and a field cannot be moved out of `&!`. It is what made `Chain` impossible as a library object with handles, and forced `evlog.Ev` to hold no file at all (open, `pread`, close per read; a staging buffer and an open per flush) and `run` to own the outcomes log by value.

   ```
   // g1.cho
   edition 5;
   module g1;
   pub res struct Holder { file: File, n: int }
   pub fn replace[&h](holder: &!h Holder, next: File) -> [] int {
       file_close(holder.file);       // error: expected `File`, found `&!h File`
       holder.file = next;
       holder.n = holder.n + 1;
       return holder.n;
   }
   ```
   `cancho check g1main.cho g1.cho --std` (with any `main`). A `mem::replace`-like builtin for resources, or `ticket`s for files as `conn_detach`/`conn_attach` are for connections (`std.conns`), would fix it.

2. **A capability cannot be shared.** One `Fs("")` exists per program, `narrow` consumes it, and a struct that holds it owns it. `Ev` owns it; the rest of the program lends it (`evlog.lend`) and cannot borrow it at the same time as it borrows `Ev` mutably, so `prepare` and `run` lost their `fs` parameter. Not a bug: a design question (`narrow` that returns both halves, or `Fs` borrowed into a struct for the length of a region).

3. **Two files of a program that declares no module share their imports.** `compact.cho` (root, so that it can read `hooks.cho`'s layout functions) cannot `import std.buffer` when `hooks.cho` does:

   ```
   // a.cho                       // b.cho
   edition 5;                    edition 5;
   import std.buffer;            import std.buffer;       // error: `buffer` is already bound to another import here
   fn one(x: int) -> [] int { return x; }    fn main(world: World) -> [] int { ... return one(0); }
   ```
   `cancho check a.cho b.cho --std`. A per-file import scope would remove the surprise.

4. **A large `box_slice` was resident from the moment it was made. FIXED in the compiler at the pin (4c27593).** Measured on the compiler before it: the idempotency index at 1,048,576 keys was 291 MB resident right after `listening`, 153 MB more than the 138 MB of the default build, with no key in it, and 262,144 keys cost 38 MB the same way. With a lazily zeroed allocation the same service is **2.5 MB resident after `listening` whatever `idem-keys` is** (16, 262,144, 1,048,576 and 4,194,304 all measured, 2.5 MB each), and it grows with the keys held (200,000 distinct keys: 30 MB; `docs/design.md` section 38.2). `idem-keys` can be large at no cost until it is used. Kept here as the record of what the proposal asked for and what answered it.

5. **`join`, `size`, `remove`, `rename` and others are reserved names** (builtins), which a module cannot use as a function name; the refusal names the builtin. Harmless; the first attempt at `store.join` met it.

6. **A region that is left by a `return` is not given back.** The memory of `region a { ... }` is freed when the block ends by falling out of its last statement, and **not** when a `return` inside it leaves the function (the compiler's region of such a function is a 64 KiB `malloc` that nothing frees). In a long-running service every such function is a leak per call. It was in this repository before retention (`state.put_outcome`, `hooks.request_for`: about 11 KB per delivery attempt, so a million deliveries held 11 GB; measured: 100,000 deliveries took the process from 2 MB to 1,075 MB resident, and the same 100,000 take it to a flat resident size (172 MB on the compiler before 4c27593, 3.4 MB on it) once those two functions leave their region by falling out of it), and it was in this work's first draft (`evlog.flush`, `store.seg_path`: 36 MB per 100,000 events). Found by the ten-million-event run (`scripts/bench/retention_bench.py through`) and `gdb` on `malloc`. Reproducer, one file, `cancho build g6.cho --std`:

   ```
   // g6.cho: 100,000 calls; resident 396 MB. Change left_by_return to left_by_falling_out in main: 9 MB.
   edition 5;

   fn copy[&o, &v](out: &!o [byte], at: int, value: &v [byte]) -> [] int {
       var i = 0;
       while i < len(value) {
           out[at + i] = value[i];
           i = i + 1;
       }
       return at + len(value);
   }

   fn left_by_return[&o](out: &!o [byte], n: int) -> [] int {
       region a {
           let value = alloc_slice[a](40, byte_of(0));
           value[0] = byte_of(n & 127);
           return copy(out, 0, value);
       }
   }

   fn left_by_falling_out[&o](out: &!o [byte], n: int) -> [] int {
       var r = 0;
       region a {
           let value = alloc_slice[a](40, byte_of(0));
           value[0] = byte_of(n & 127);
           r = copy(out, 0, value);
       }
       return r;
   }

   fn main(world: World) -> [] int {
       let Split { io, ffi, fs, heap, args, net, clock } = split(world);
       release(io); release(ffi); release(fs); release(heap); release(net); release(clock); release(args);
       var total = 0;
       region outer {
           let buf = alloc_slice[outer](128, byte_of(0));
           var i = 0;
           while i < 100000 {
               total = total + left_by_return(buf, i);
               i = i + 1;
           }
       }
       return total & 1;
   }
   ```

   It does not leak when the region slice is not passed on to a call (a function that only indexes it), which is why small probes missed it. The fix is the compiler's (free the region on every exit); until then, **no `return` inside a `region`**. What this repository still has: `evlog.open` and `evlog.step` (once per start, and a park that never returns), `hooks.open_log`, `read_endpoints_file`, `load_config` (once per start), `finish_create`, `finish_patch`, `manage.make_secret`, `sched.request_for` (one per administrative request or schedule statement) and the two early exits of `fire_cron`: each loses 64 KiB per call, none is on the path of an event or a delivery, and they are listed in `docs/design.md` section 38 so that the next person does not have to find them again.
