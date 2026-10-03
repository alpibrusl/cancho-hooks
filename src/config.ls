edition 5;

module config;

import std.bytes;

// `config` -- the service's settings, from a file and from flags (`docs/design.md` section 20).
//
// Five settings, each with a default or a refusal:
//
//     port         the TCP port to listen on                       required, 1 to 65535
//     dir          the data directory                              required
//     schedule     the retry delays in ms, `100,200,400`           default: the built-in schedule (section 4)
//     deadline-ms  how long one delivery attempt may take          default 0, which means the built-in 2000
//     window-ms    how long an idempotency key is remembered       default 86400000 (a day)
//
// They come from three places and the **last one that names a setting wins**: the defaults above, then the file given with
// `--config`, then the flags in the order they were written. All three go through `set`, so a value is judged by one rule
// wherever it came from.
//
// The settings are a table of integers and a blob of bytes, which the caller sizes with `size` and `blob_size`:
//
//     cfg[0] port (-1 until set)   cfg[1] deadline-ms   cfg[2] window-ms   cfg[3] dir length   cfg[4] schedule length
//     cfg[5] why the last refusal happened (`why_*`)    cfg[6], cfg[7] unused
//
//     blob[0 .. 2048] the directory, blob[2048 .. 2304] the schedule

pub fn size() -> [] int {
    return 8;
}

pub fn blob_size() -> [] int {
    return 2304;
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

pub fn defaults[&c](cfg: &!c [int]) -> [] int {
    var i = 0;
    while i < size() {
        cfg[i] = 0;
        i = i + 1;
    }
    cfg[0] = 0 - 1;
    cfg[2] = 86400000;
    return 0;
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
