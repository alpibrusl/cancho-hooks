edition 5;

module config;

import std.bytes;
import destination;

// `config` -- the service's settings, from a file and from flags (`docs/design.md` section 20).
//
// Five settings, each with a default or a refusal:
//
//     port         the TCP port to listen on                       required, 1 to 65535
//     dir          the data directory                              required
//     schedule     the retry delays in ms, `100,200,400`           default: the built-in schedule (section 4)
//     deadline-ms  how long one delivery attempt may take          default 0, which means the built-in 2000
//     window-ms    how long an idempotency key is remembered       default 86400000 (a day)
//     pg-host      the PostgreSQL to write the history to          default none: no history (section 24)
//     pg-port      its port                                        default 5432, 1 to 65535
//     pg-user, pg-database, pg-password                            default `hooks`, `hooks`, none
//     import-endpoints  `1`: copy `endpoints.conf` into the database and exit   default 0 (section 24)
//     allow-private-hosts  `1`: endpoints may be names and non-public addresses (section 26)   default 0
//     admin-token  the bearer token that lets a request change endpoints       default none: management is off (section 25.2)
//     ingest-token the bearer token that lets a request post events            default none: ingest is open (section 33)
//     read-token   the bearer token that lets a request read                   default none: the reads are open (section 33)
//     production   `1`: refuse to start unless the settings and the data directory are safe on the internet   default 0 (section 33)
//     breaker-days  pause an endpoint whose every attempt has failed for this many days; 0 turns it off   default 5 (section 31)
//     cron-catchup `1`: a schedule whose fires were missed while the service was stopped fires once for them; `0`: it skips them   default 1 (section 32)
//     cron-seconds `1`: a schedule's expression has a leading seconds field (six fields; a test mode)   default 0 (section 32)
//     stop-deadline-ms  how long attempts on the wire may take to finish after SIGTERM or SIGINT   default 5000 (section 34.4)
//     repair-logs  `1`: cut a log that has damage in the middle at the damage instead of refusing to start; the cut is reported   default 0 (section 34.5)
//     rotation-grace-ms  how long the previous secret is still signed with after `PATCH ... {"keep_old": true}` (1 to 2592000000)   default 86400000, a day (section 35)
//     history-days  delete the rows of the attempts table older than this, in batches; 0 keeps them for ever   default 30 (`docs/design.md` section 43)
//     retention-days  drop events that are final everywhere and older than this; 0 keeps them for ever   default 30 (`docs/retention.md`)
//     segment-bytes  seal the active events segment at this size   default 67108864
//     delivery-log-bytes  replace the outcomes log by a snapshot at this size   default 33554432
//     idem-keys  how many idempotency keys the index holds   default 262144
//     compact-now  `1`: compact once and exit   default 0
//     retention-ms, compact-kill-at  **test knobs**: retention in ms instead of days; stop at a numbered step of a compaction   default 0
//     pg-backoff-min-ms, pg-backoff-max-ms  the wait after a failed connection to the database, from the first to the longest (it doubles)   default 100, 5000 (section 37)
//     pg-attempt-ms  the longest one attempt to connect, log in and prepare may take   default 5000 (section 37)
//     pg-request-ms  the longest a request to the database may wait for any answer before its connection is given up; 0 never   default 10000 (section 37)
//     pg-start-wait-ms  how long the service may go without having read its endpoints from the database before it ends with status 20; 0 never   default 30000 (section 37)
//     retry-jitter  how far each retry delay is moved either way, in percent of itself (0 to 50; 0 is exactly the schedule)   default 10 (section 39.3)
//     endpoint-concurrency  the most attempts one endpoint has in flight (1 to 8; an endpoint's own "concurrency" replaces it)   default 8 (section 39.4)
//     endpoint-rate  the most attempts one endpoint starts a second (0 to 100000, 0: no limit; an endpoint's own "rate" replaces it)   default 0 (section 39.4)
//     dns-server   the name server that resolves the names of endpoints, `ip` or `ip:port` (IPv4)   default the first `nameserver` of /etc/resolv.conf, port 53 (section 40)
//     tls-ca-file  the PEM file of certificates an `https` endpoint's chain must lead to, instead of the system's   default none: the system's trust store (section 40)
//     tls-resume   `1`: keep a TLS session for each `https` endpoint and resume it; `0`: a full handshake every time   default 1 (section 40)
//
// They come from three places and the **last one that names a setting wins**: the defaults above, then the file given with
// `--config`, then the flags in the order they were written. All three go through `set`, so a value is judged by one rule
// wherever it came from.
//
// The settings are a table of integers and a blob of bytes, which the caller sizes with `size` and `blob_size`:
//
//     cfg[0] port (-1 until set)   cfg[1] deadline-ms   cfg[2] window-ms   cfg[3] dir length   cfg[4] schedule length
//     cfg[5] why the last refusal happened (`why_*`)    cfg[6] pg-port (5432 until set)
//     cfg[7] pg-host length   cfg[8] pg-user length   cfg[9] pg-database length   cfg[10] pg-password length   cfg[11] import-endpoints (0 or 1)   cfg[12] admin-token length   cfg[13] allow-private-hosts (0 or 1)   cfg[14] breaker-days (0 to 36500)
//     cfg[15] cron-catchup (0 or 1; 1 until set)   cfg[16] cron-seconds (0 or 1)   cfg[17] stop-deadline-ms (5000 until set)   cfg[18] repair-logs (0 or 1)
//     cfg[19] ingest-token length   cfg[20] read-token length   cfg[21] production (0 or 1)   cfg[22] rotation-grace-ms (86400000 until set)
//     cfg[40] retention-days   cfg[41] segment-bytes   cfg[42] delivery-log-bytes   cfg[43] idem-keys   cfg[44] compact-now   cfg[45] retention-ms   cfg[46] compact-kill-at
//     cfg[23] pg-backoff-min-ms (100 until set)   cfg[24] pg-backoff-max-ms (5000)   cfg[25] pg-attempt-ms (5000)   cfg[26] pg-request-ms (10000)   cfg[27] pg-start-wait-ms (30000)
//     cfg[28] retry-jitter (0 to 50; 10 until set)   cfg[29] endpoint-concurrency (1 to 8; 8 until set)   cfg[30] endpoint-rate (0 to 100000)
//     cfg[31] dns-server address (packed, 0 until set)   cfg[32] its port (53 until set)   cfg[33] tls-ca-file length   cfg[34] tls-resume (0 or 1; 1 until set)
//     cfg[35] history-days (0 to 36500; 30 until set)   cfg[36] audit-log (0 or 1; 1 until set)   cfg[37] audit-log-bytes (65536 to 2^40; 67108864)
//     cfg[38] audit-log-files (1 to 100; 8)
//     (`tests/config_test.ls` sets every numeric setting to a value of its own and reads each back: two settings on one index fail it)
//
//     blob[0 .. 2048] the directory, blob[2048 .. 2304] the schedule, then the database's host (256), user (64), database (64)
//     and password (256), at `pg_host_at()` and the offsets after it, then the admin token (256), the ingest token (256) and the read
//     token (256), at `token_at()`, `ingest_token_at()` and `read_token_at()`, then the tls-ca-file (256) at `ca_file_at()`

pub fn size() -> [] int {
    return 48;
}

pub fn blob_size() -> [] int {
    return 3968;
}

pub fn ca_file_at() -> [] int {
    return 3712;
}

pub fn token_at() -> [] int {
    return 2944;
}

pub fn ingest_token_at() -> [] int {
    return 3200;
}

pub fn read_token_at() -> [] int {
    return 3456;
}

pub fn pg_host_at() -> [] int {
    return 2304;
}

pub fn pg_user_at() -> [] int {
    return 2560;
}

pub fn pg_database_at() -> [] int {
    return 2624;
}

pub fn pg_password_at() -> [] int {
    return 2688;
}

pub fn sched_at() -> [] int {
    return 2048;
}

pub fn why_key() -> [] int {
    return 1;
}

pub fn why_value() -> [] int {
    return 2;
}

pub fn why_line() -> [] int {
    return 3;
}

pub fn port_of[&c](cfg: &c [int]) -> [] int {
    return cfg[0];
}

pub fn deadline_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[1];
}

pub fn window_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[2];
}

pub fn dir_len[&c](cfg: &c [int]) -> [] int {
    return cfg[3];
}

pub fn sched_len[&c](cfg: &c [int]) -> [] int {
    return cfg[4];
}

pub fn why[&c](cfg: &c [int]) -> [] int {
    return cfg[5];
}

pub fn pg_port[&c](cfg: &c [int]) -> [] int {
    return cfg[6];
}

pub fn pg_host_len[&c](cfg: &c [int]) -> [] int {
    return cfg[7];
}

pub fn pg_user_len[&c](cfg: &c [int]) -> [] int {
    return cfg[8];
}

pub fn pg_database_len[&c](cfg: &c [int]) -> [] int {
    return cfg[9];
}

pub fn pg_password_len[&c](cfg: &c [int]) -> [] int {
    return cfg[10];
}

pub fn token_len[&c](cfg: &c [int]) -> [] int {
    return cfg[12];
}

pub fn ingest_token_len[&c](cfg: &c [int]) -> [] int {
    return cfg[19];
}

pub fn read_token_len[&c](cfg: &c [int]) -> [] int {
    return cfg[20];
}

// Is the production profile asked for (`production = 1`, `docs/design.md` section 33)?
pub fn production[&c](cfg: &c [int]) -> [] bool {
    return cfg[21] == 1;
}

pub fn import_endpoints[&c](cfg: &c [int]) -> [] bool {
    return cfg[11] == 1;
}

pub fn allow_private_hosts[&c](cfg: &c [int]) -> [] bool {
    return cfg[13] == 1;
}

// Days of failure after which the circuit breaker pauses an endpoint (`docs/design.md` section 31); 0 is off.
pub fn breaker_days[&c](cfg: &c [int]) -> [] int {
    return cfg[14];
}

pub fn cron_catchup[&c](cfg: &c [int]) -> [] int {
    return cfg[15];
}

pub fn cron_seconds[&c](cfg: &c [int]) -> [] int {
    return cfg[16];
}

// How long the attempts on the wire may take to finish after the service is asked to stop, in ms (`docs/design.md` section 34.4).
pub fn stop_deadline_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[17];
}

// 1: a log with damage in the middle is cut at the damage, and the cut reported, instead of the start being refused (section 34.5).
pub fn repair_logs[&c](cfg: &c [int]) -> [] bool {
    return cfg[18] == 1;
}

// How long a previous secret stays valid after a rotation that asks for the default (`docs/design.md` section 35), in ms.
pub fn rotation_grace_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[22];
}

// Retention (`docs/retention.md` section 3).
pub fn audit_log[&c](cfg: &c [int]) -> [] bool {
    return cfg[36] == 1;
}

pub fn audit_log_bytes[&c](cfg: &c [int]) -> [] int {
    return cfg[37];
}

pub fn audit_log_files[&c](cfg: &c [int]) -> [] int {
    return cfg[38];
}

pub fn history_days[&c](cfg: &c [int]) -> [] int {
    return cfg[35];
}

pub fn retention_days[&c](cfg: &c [int]) -> [] int {
    return cfg[40];
}

pub fn segment_bytes[&c](cfg: &c [int]) -> [] int {
    return cfg[41];
}

pub fn delivery_log_bytes[&c](cfg: &c [int]) -> [] int {
    return cfg[42];
}

pub fn idem_keys[&c](cfg: &c [int]) -> [] int {
    return cfg[43];
}

pub fn compact_now[&c](cfg: &c [int]) -> [] bool {
    return cfg[44] == 1;
}

pub fn retention_ms_knob[&c](cfg: &c [int]) -> [] int {
    return cfg[45];
}

pub fn compact_kill_at[&c](cfg: &c [int]) -> [] int {
    return cfg[46];
}

// How far a retry delay is moved, in percent (`docs/design.md` section 39.3); the limits of an endpoint's attempts (section 39.4).
pub fn retry_jitter[&c](cfg: &c [int]) -> [] int {
    return cfg[28];
}

pub fn endpoint_concurrency[&c](cfg: &c [int]) -> [] int {
    return cfg[29];
}

pub fn endpoint_rate[&c](cfg: &c [int]) -> [] int {
    return cfg[30];
}

// The wait after a failed connection to the database starts at this many ms and doubles up to `pg_backoff_max_ms` (`docs/design.md` section 37).
pub fn pg_backoff_min_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[23];
}

pub fn pg_backoff_max_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[24];
}

// The longest one attempt to make a connection (dial, login, prepare the statements) may take, in ms.
pub fn pg_attempt_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[25];
}

// The longest a request to the database may wait without any byte coming back before the connection is given up, in ms; 0 is for ever.
pub fn pg_request_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[26];
}

// How long the service may run without having read its endpoints from the database before it ends with status 20, in ms; 0 is for ever.
pub fn pg_start_wait_ms[&c](cfg: &c [int]) -> [] int {
    return cfg[27];
}

// Whether the settings of the database's connections agree with each other: 0, or the reason (1: the longest wait is below the first).
pub fn pg_status[&c](cfg: &c [int]) -> [] int {
    if cfg[24] < cfg[23] {
        return 1;
    }
    return 0;
}

// The name server that was named (`dns-server`), packed, or 0 if none was: the caller then asks the system (`resolve.ls`).
pub fn dns_server[&c](cfg: &c [int]) -> [] int {
    return cfg[31];
}

pub fn dns_port[&c](cfg: &c [int]) -> [] int {
    return cfg[32];
}

// The length of the path of the trust store file (`tls-ca-file`), 0 for the system's.
pub fn ca_file_len[&c](cfg: &c [int]) -> [] int {
    return cfg[33];
}

pub fn tls_resume[&c](cfg: &c [int]) -> [] bool {
    return cfg[34] == 1;
}

pub fn defaults[&c](cfg: &!c [int]) -> [] int {
    var i = 0;
    while i < size() {
        cfg[i] = 0;
        i = i + 1;
    }
    cfg[0] = 0 - 1;
    cfg[2] = 86400000;
    cfg[6] = 5432;
    cfg[14] = 5;
    cfg[15] = 1;
    cfg[17] = 5000;
    cfg[22] = 86400000;
    cfg[32] = 53;
    cfg[34] = 1;
    cfg[35] = 30;
    cfg[36] = 1;
    cfg[37] = 67108864;
    cfg[38] = 8;
    cfg[40] = 30;
    cfg[41] = 67108864;
    cfg[42] = 33554432;
    cfg[43] = 262144;
    cfg[23] = 100;
    cfg[24] = 5000;
    cfg[25] = 5000;
    cfg[26] = 10000;
    cfg[27] = 30000;
    cfg[28] = 10;
    cfg[29] = 8;
    return 0;
}

// The production profile (`production = 1`, `docs/design.md` section 33): is every setting one that is safe on the internet? Answers 0 if
// so, or the exit status the service ends with, one for each cause (the data directory's modes are judged elsewhere: `perm.ls`, 33 and 35):
//
//     30 `admin-token` is not set     31 `ingest-token` is not set     32 `allow-private-hosts` is 1     34 two of the three tokens are the same
//
// `read-token` may be left out: the read routes then need the admin token. A database is not required: without one the endpoints are a
// file, `POST /endpoints` and the schedules answer `503`, and delivery works.
pub fn production_status[&c, &b](cfg: &c [int], blob: &b [byte]) -> [] int {
    if cfg[12] == 0 {
        return 30;
    }
    if cfg[19] == 0 {
        return 31;
    }
    if cfg[13] == 1 {
        return 32;
    }
    if same(blob, token_at(), cfg[12], ingest_token_at(), cfg[19]) {
        return 34;
    }
    if cfg[20] > 0 && (same(blob, token_at(), cfg[12], read_token_at(), cfg[20]) || same(blob, ingest_token_at(), cfg[19], read_token_at(), cfg[20])) {
        return 34;
    }
    // the audit log (`docs/design.md` section 47.1): who read and changed what is part of running it on the internet
    if cfg[36] == 0 {
        return 36;
    }
    return 0;
}

// Are the tokens at `a` (`la` bytes) and at `b` (`lb` bytes) the same? Not a secret comparison: both are the operator's, at start.
fn same[&k](blob: &k [byte], a: int, la: int, b: int, lb: int) -> [] bool {
    if la != lb {
        return false;
    }
    var i = 0;
    while i < la {
        if int_of(blob[a + i]) != int_of(blob[b + i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// What `production_status` (or `perm.files`, for 33 and 35) found, naming the setting. The data directory's own message is built by the caller.
pub fn unsafe_message(status: int) -> [] &static [byte] {
    if status == 30 {
        return "admin-token is not set (nothing would protect the changes to endpoints and schedules, nor replay and enable)";
    }
    if status == 31 {
        return "ingest-token is not set (anyone who can reach the port could post events)";
    }
    if status == 32 {
        return "allow-private-hosts is 1 (endpoints could aim the service at a private network); set it to 0";
    }
    if status == 34 {
        return "admin-token, ingest-token and read-token must be three different tokens (a token that is the same as the admin token gives its holder the admin scope)";
    }
    if status == 36 {
        return "audit-log is 0 (who read and changed what would not be written down); set it to 1";
    }
    if status == 33 {
        return "can be read or written by its group or by others; the data directory must be 0700 and its files 0600 (start the service with umask 077)";
    }
    return "the mode cannot be read";
}

// A non-negative number of at most twelve digits, or -1.
fn number[&t](text: &t [byte]) -> [] int {
    if len(text) == 0 || len(text) > 12 {
        return 0 - 1;
    }
    var n = 0;
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if !bytes.is_digit(c) {
            return 0 - 1;
        }
        n = n * 10 + (c - '0');
        i = i + 1;
    }
    return n;
}

// A bearer token (`admin-token`, `ingest-token`, `read-token`): 8 to 255 visible ASCII characters, no space. One rule for the three.
fn plain_token[&t](value: &t [byte]) -> [] bool {
    var plain = len(value) >= 8 && len(value) <= 255;
    var k = 0;
    while k < len(value) {
        if int_of(value[k]) <= 32 || int_of(value[k]) >= 127 {
            plain = false;
        }
        k = k + 1;
    }
    return plain;
}

fn keep[&t, &b](text: &t [byte], blob: &!b [byte], at: int) -> [] int {
    var k = 0;
    while k < len(text) {
        blob[at + k] = text[k];
        k = k + 1;
    }
    return len(text);
}

// Apply one setting. Answers 0, or the reason it was refused (`why_key`: no such setting, `why_value`: a value that setting
// does not take), which is also left in `cfg[5]`. A refused setting changes nothing.
pub fn set[&c, &b, &k, &v](cfg: &!c [int], blob: &!b [byte], key: &k [byte], value: &v [byte]) -> [] int {
    var why = 0;
    if bytes.equal(key, "port") {
        let n = number(value);
        if n < 1 || n > 65535 {
            why = why_value();
        } else {
            cfg[0] = n;
        }
    } else if bytes.equal(key, "dir") {
        if len(value) < 1 || len(value) > 2000 {
            why = why_value();
        } else {
            cfg[3] = keep(value, blob, 0);
        }
    } else if bytes.equal(key, "schedule") {
        if len(value) < 1 || len(value) > 250 {
            why = why_value();
        } else {
            cfg[4] = keep(value, blob, sched_at());
        }
    } else if bytes.equal(key, "deadline-ms") {
        let n = number(value);
        if n < 0 {
            why = why_value();
        } else {
            cfg[1] = n;
        }
    } else if bytes.equal(key, "window-ms") {
        let n = number(value);
        if n < 0 {
            why = why_value();
        } else {
            cfg[2] = n;
        }
    } else if bytes.equal(key, "breaker-days") {
        // At most 100 years: the days are multiplied by 86,400,000 and must not overflow.
        let n = number(value);
        if n < 0 || n > 36500 {
            why = why_value();
        } else {
            cfg[14] = n;
        }
    } else if bytes.equal(key, "rotation-grace-ms") {
        // At most 30 days, and at least a millisecond: 0 would be "no overlap", which is not asking for one.
        let n = number(value);
        if n < 1 || n > 2592000000 {
            why = why_value();
        } else {
            cfg[22] = n;
        }
    } else if bytes.equal(key, "pg-backoff-min-ms") {
        // 1 ms to 10 minutes. (That it is not above `pg-backoff-max-ms` is judged once all the settings are in: `pg_status`.)
        let n = number(value);
        if n < 1 || n > 600000 {
            why = why_value();
        } else {
            cfg[23] = n;
        }
    } else if bytes.equal(key, "pg-backoff-max-ms") {
        let n = number(value);
        if n < 1 || n > 3600000 {
            why = why_value();
        } else {
            cfg[24] = n;
        }
    } else if bytes.equal(key, "pg-attempt-ms") {
        let n = number(value);
        if n < 1 || n > 600000 {
            why = why_value();
        } else {
            cfg[25] = n;
        }
    } else if bytes.equal(key, "pg-request-ms") {
        // 0 is "no timeout"; an hour at most.
        let n = number(value);
        if n < 0 || n > 3600000 {
            why = why_value();
        } else {
            cfg[26] = n;
        }
    } else if bytes.equal(key, "pg-start-wait-ms") {
        // 0 is "wait for ever"; a day at most.
        let n = number(value);
        if n < 0 || n > 86400000 {
            why = why_value();
        } else {
            cfg[27] = n;
        }
    } else if bytes.equal(key, "retry-jitter") {
        let n = number(value);
        if n < 0 || n > 50 {
            why = why_value();
        } else {
            cfg[28] = n;
        }
    } else if bytes.equal(key, "endpoint-concurrency") {
        let n = number(value);
        if n < 1 || n > 8 {
            why = why_value();
        } else {
            cfg[29] = n;
        }
    } else if bytes.equal(key, "endpoint-rate") {
        let n = number(value);
        if n < 0 || n > 100000 {
            why = why_value();
        } else {
            cfg[30] = n;
        }
    } else if bytes.equal(key, "pg-host") {
        if len(value) < 1 || len(value) > 253 {
            why = why_value();
        } else {
            cfg[7] = keep(value, blob, pg_host_at());
        }
    } else if bytes.equal(key, "pg-port") {
        let n = number(value);
        if n < 1 || n > 65535 {
            why = why_value();
        } else {
            cfg[6] = n;
        }
    } else if bytes.equal(key, "pg-user") {
        if len(value) < 1 || len(value) > 63 {
            why = why_value();
        } else {
            cfg[8] = keep(value, blob, pg_user_at());
        }
    } else if bytes.equal(key, "pg-database") {
        if len(value) < 1 || len(value) > 63 {
            why = why_value();
        } else {
            cfg[9] = keep(value, blob, pg_database_at());
        }
    } else if bytes.equal(key, "pg-password") {
        if len(value) < 1 || len(value) > 255 {
            why = why_value();
        } else {
            cfg[10] = keep(value, blob, pg_password_at());
        }
    } else if bytes.equal(key, "admin-token") {
        if plain_token(value) {
            cfg[12] = keep(value, blob, token_at());
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "ingest-token") {
        if plain_token(value) {
            cfg[19] = keep(value, blob, ingest_token_at());
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "read-token") {
        if plain_token(value) {
            cfg[20] = keep(value, blob, read_token_at());
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "production") {
        if bytes.equal(value, "1") {
            cfg[21] = 1;
        } else if bytes.equal(value, "0") {
            cfg[21] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "allow-private-hosts") {
        if bytes.equal(value, "1") {
            cfg[13] = 1;
        } else if bytes.equal(value, "0") {
            cfg[13] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "cron-catchup") {
        if bytes.equal(value, "1") {
            cfg[15] = 1;
        } else if bytes.equal(value, "0") {
            cfg[15] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "cron-seconds") {
        if bytes.equal(value, "1") {
            cfg[16] = 1;
        } else if bytes.equal(value, "0") {
            cfg[16] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "stop-deadline-ms") {
        // At most an hour: the deadline is added to a monotonic reading in ms.
        let n = number(value);
        if n < 0 || n > 3600000 {
            why = why_value();
        } else {
            cfg[17] = n;
        }
    } else if bytes.equal(key, "repair-logs") {
        if bytes.equal(value, "1") {
            cfg[18] = 1;
        } else if bytes.equal(value, "0") {
            cfg[18] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "dns-server") {
        // `ip` or `ip:port`, an IPv4 literal (any: it is the operator's own, and is not an endpoint).
        var colon = len(value);
        var i = 0;
        while i < len(value) {
            if int_of(value[i]) == ':' {
                colon = i;
            }
            i = i + 1;
        }
        let a = destination.address(value[0..colon]);
        var port = 53;
        if colon < len(value) {
            port = number(value[colon + 1..len(value)]);
        }
        if a <= 0 || port < 1 || port > 65535 {
            why = why_value();
        } else {
            cfg[31] = a;
            cfg[32] = port;
        }
    } else if bytes.equal(key, "tls-ca-file") {
        if len(value) < 1 || len(value) > 255 {
            why = why_value();
        } else {
            cfg[33] = keep(value, blob, ca_file_at());
        }
    } else if bytes.equal(key, "tls-resume") {
        if bytes.equal(value, "1") {
            cfg[34] = 1;
        } else if bytes.equal(value, "0") {
            cfg[34] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "audit-log") {
        if bytes.equal(value, "1") {
            cfg[36] = 1;
        } else if bytes.equal(value, "0") {
            cfg[36] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "audit-log-bytes") {
        let n = number(value);
        if n < 65536 || n > 1099511627776 {
            why = why_value();
        } else {
            cfg[37] = n;
        }
    } else if bytes.equal(key, "audit-log-files") {
        let n = number(value);
        if n < 1 || n > 100 {
            why = why_value();
        } else {
            cfg[38] = n;
        }
    } else if bytes.equal(key, "history-days") {
        let n = number(value);
        if n < 0 || n > 36500 {
            why = why_value();
        } else {
            cfg[35] = n;
        }
    } else if bytes.equal(key, "retention-days") {
        let n = number(value);
        if n < 0 || n > 36500 {
            why = why_value();
        } else {
            cfg[40] = n;
        }
    } else if bytes.equal(key, "segment-bytes") {
        let n = number(value);
        if n < 262144 {
            why = why_value();
        } else {
            cfg[41] = n;
        }
    } else if bytes.equal(key, "delivery-log-bytes") {
        let n = number(value);
        if n < 65536 {
            why = why_value();
        } else {
            cfg[42] = n;
        }
    } else if bytes.equal(key, "idem-keys") {
        let n = number(value);
        if n < 16 || n > 4194304 {
            why = why_value();
        } else {
            cfg[43] = n;
        }
    } else if bytes.equal(key, "compact-now") {
        if bytes.equal(value, "1") {
            cfg[44] = 1;
        } else if bytes.equal(value, "0") {
            cfg[44] = 0;
        } else {
            why = why_value();
        }
    } else if bytes.equal(key, "retention-ms") {
        let n = number(value);
        if n < 0 {
            why = why_value();
        } else {
            cfg[45] = n;
        }
    } else if bytes.equal(key, "compact-kill-at") {
        let n = number(value);
        if n < 0 || n > 64 {
            why = why_value();
        } else {
            cfg[46] = n;
        }
    } else if bytes.equal(key, "import-endpoints") {
        if bytes.equal(value, "1") {
            cfg[11] = 1;
        } else if bytes.equal(value, "0") {
            cfg[11] = 0;
        } else {
            why = why_value();
        }
    } else {
        why = why_key();
    }
    if why != 0 {
        cfg[5] = why;
    }
    return why;
}

fn is_space(c: int) -> [] bool {
    return c == ' ' || c == '\t' || c == '\r';
}

// The file: one `key = value` a line (the spaces around the `=` are optional, and so is the `=` itself when a space
// separates them), `#` starts a comment on a line of its own, blank lines are ignored. Answers 0, or the 1-based number of
// the first line that is wrong, with the reason in `cfg[5]` (`why_line`: no value, or no key).
pub fn parse_file[&t, &c, &b](text: &t [byte], cfg: &!c [int], blob: &!b [byte]) -> [] int {
    var line = 0;
    var at = 0;
    while at < len(text) {
        var end = at;
        while end < len(text) && int_of(text[end]) != '\n' {
            end = end + 1;
        }
        line = line + 1;
        var s = at;
        while s < end && is_space(int_of(text[s])) {
            s = s + 1;
        }
        if s < end && int_of(text[s]) != '#' {
            var ke = s;
            while ke < end && !is_space(int_of(text[ke])) && int_of(text[ke]) != '=' {
                ke = ke + 1;
            }
            var vs = ke;
            while vs < end && is_space(int_of(text[vs])) {
                vs = vs + 1;
            }
            if vs < end && int_of(text[vs]) == '=' {
                vs = vs + 1;
                while vs < end && is_space(int_of(text[vs])) {
                    vs = vs + 1;
                }
            }
            var ve = end;
            while ve > vs && is_space(int_of(text[ve - 1])) {
                ve = ve - 1;
            }
            if ke == s || vs == ve {
                cfg[5] = why_line();
                return line;
            }
            if set(cfg, blob, text[s..ke], text[vs..ve]) != 0 {
                return line;
            }
        }
        at = end + 1;
    }
    return 0;
}

// One command-line argument as a flag: `--key value` (the value is the next argument) or `--key=value`. Answers
// `[key_start, key_end, value_start, value_end]` as a tuple of the four, where `value_start == 0 - 1` means the value is the
// next argument, and `key_start == 0 - 1` means the argument is not a flag at all.
pub fn split_flag[&t](arg: &t [byte]) -> [] (int, int, int, int) {
    if len(arg) < 3 || int_of(arg[0]) != '-' || int_of(arg[1]) != '-' {
        return (0 - 1, 0, 0, 0);
    }
    var i = 2;
    while i < len(arg) && int_of(arg[i]) != '=' {
        i = i + 1;
    }
    if i == len(arg) {
        return (2, i, 0 - 1, 0);
    }
    return (2, i, i + 1, len(arg));
}
