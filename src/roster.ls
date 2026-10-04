edition 5;

module roster;

import std.buffer;
import std.bytes;
import endpoints;
import history;
import pg;
import queries;

// `roster` -- the endpoints in PostgreSQL (`docs/design.md` section 24, C3c).
//
// `fetch` reads the `endpoints` table and writes it as the text `endpoints.parse` already reads, one `id host port secret` a
// line, so the rules that judge a row are the rules that judge a line of `endpoints.conf`, and a bad row is refused with the
// same words. `copy_in` is the other direction: it copies a file's lines into the table. Both use one blocking connection and
// run **before** the service listens: unlike the history, the endpoints are not best effort. A service that cannot read the
// table does not know who to deliver to, and refuses to start (`hooks.ls`), rather than start with the wrong list.

// A decimal number of one to six digits that `endpoints.number` would accept, or the reason it is not: used on the text the
// database sent, which is trusted for nothing.
fn plain[&r](rep: &r [byte], span: (int, int)) -> [] bool {
    if span.0 < 0 || span.1 <= span.0 {
        return false;
    }
    var i = span.0;
    while i < span.1 {
        let c = int_of(rep[i]);
        if c <= 32 || c >= 127 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

fn put[&o, &r](out: &!o [byte], at: int, rep: &r [byte], span: (int, int), last: bool) -> [] int {
    if at + (span.1 - span.0) + 1 > len(out) {
        return 0 - 1;
    }
    var i = 0;
    while i < span.1 - span.0 {
        out[at + i] = rep[span.0 + i];
        i = i + 1;
    }
    var end = at + (span.1 - span.0);
    if last {
        out[end] = byte_of('\n');
    } else {
        out[end] = byte_of(' ');
    }
    return end + 1;
}

// An optional column: empty, or a run of printable bytes with no space (the same rule as `plain`).
fn optional[&r](rep: &r [byte], span: (int, int)) -> [] bool {
    if span.1 <= span.0 {
        return true;
    }
    return plain(rep, span);
}

// ` prefix` and the value of `span` and a space, as `put` does for a field: the word of an optional column. Answers where the next goes, or -1.
fn put_word[&o, &r](out: &!o [byte], at: int, prefix: &static [byte], rep: &r [byte], span: (int, int)) -> [] int {
    if at + len(prefix) > len(out) {
        return 0 - 1;
    }
    var i = 0;
    while i < len(prefix) {
        out[at + i] = prefix[i];
        i = i + 1;
    }
    return put(out, at + len(prefix), rep, span, false);
}

// Open one connection and log in. `Dialed::Ok` is a connection that is logged in with the queries prepared; `Dialed::Failed` carries
// -1 (could not connect), -2 (could not log in) or -3 (a query could not be prepared: a table is missing).
fn open_db[&h, &n, &t, &u, &w, &d, &z](heap: &!h Heap, net: &n Net(""), host: &t [byte], port: int, user: &u [byte], password: &w [byte], database: &d [byte], rng: &z Fs("")) -> [heap, net_out(""), conn_read, conn_write, fs_read("")] Dialed {
    match tcp_connect(net, host, port) {
        Dialed::Ok(dialed) => {
            var conn = dialed;
            var s = 5;
            borrow mut conn as &!ch in {
                s = history.login(heap, ch, user, password, database, rng);
            }
            if s == 0 {
                return Dialed::Ok(conn);
            }
            conn_close(conn);
            if s == history.prepare_failed() {
                return Dialed::Failed(0 - 3);
            }
            return Dialed::Failed(0 - 2);
        }
        Dialed::Failed(e) => {
            return Dialed::Failed(0 - 1);
        }
    }
}

// The table as `endpoints.parse` reads it, written into `out`. Answers the number of bytes, or -1 (could not connect), -2 (could
// not log in), -3 (the query failed), -4 (a row has an empty field or a byte that is not printable), -5 (the table is too large for `out`).
pub fn fetch[&h, &n, &t, &u, &w, &d, &z, &o](heap: &!h Heap, net: &n Net(""), host: &t [byte], port: int, user: &u [byte], password: &w [byte], database: &d [byte], rng: &z Fs(""), out: &!o [byte]) -> [heap, net_out(""), conn_read, conn_write, fs_read("")] int {
    match open_db(heap, net, host, port, user, password, database, rng) {
        Dialed::Failed(code) => {
            return code;
        }
        Dialed::Ok(dialed) => {
            var conn = dialed;
            var result = 0 - 3;
            borrow mut conn as &!ch in {
                let (reply, st) = queries.endpoints_all(heap, ch);
                borrow reply as &rb in {
                    let rep = buffer.bytes(rb);
                    if st == 0 && pg.failure(rep) < 0 {
                        var at = 0;
                        var why = 0;
                        var row = pg.first_row(rep);
                        while row >= 0 && at >= 0 {
                            let id = pg.value(rep, row, 0);
                            let hostspan = pg.value(rep, row, 1);
                            let portspan = pg.value(rep, row, 2);
                            let secret = pg.value(rep, row, 3);
                            let typesspan = pg.value(rep, row, 4);
                            let headersspan = pg.value(rep, row, 5);
                            let oldspan = pg.value(rep, row, 6);
                            let untilspan = pg.value(rep, row, 7);
                            if plain(rep, id) && plain(rep, hostspan) && plain(rep, portspan) && plain(rep, secret) && optional(rep, typesspan) && optional(rep, headersspan) && optional(rep, oldspan) && optional(rep, untilspan) {
                                at = put(out, at, rep, id, false);
                                if at >= 0 {
                                    at = put(out, at, rep, hostspan, false);
                                }
                                if at >= 0 {
                                    at = put(out, at, rep, portspan, false);
                                }
                                if at >= 0 {
                                    at = put(out, at, rep, secret, false);
                                }
                                if at >= 0 && typesspan.1 > typesspan.0 {
                                    at = put_word(out, at, "types=", rep, typesspan);
                                }
                                if at >= 0 && headersspan.1 > headersspan.0 {
                                    at = put_word(out, at, "headers=", rep, headersspan);
                                }
                                if at >= 0 && oldspan.1 > oldspan.0 && untilspan.1 > untilspan.0 && pg.int_text(rep, untilspan.0, untilspan.1) > 0 {
                                    at = put_word(out, at, "old=", rep, oldspan);
                                    if at >= 0 {
                                        out[at - 1] = byte_of('@');
                                        at = put(out, at, rep, untilspan, false);
                                    }
                                }
                                if at >= 0 {
                                    out[at - 1] = byte_of('\n');
                                }
                                if at < 0 {
                                    why = 0 - 5;
                                }
                            } else {
                                at = 0 - 1;
                                why = 0 - 4;
                            }
                            row = pg.next_row(rep, row);
                        }
                        if at >= 0 {
                            result = at;
                        } else {
                            result = why;
                        }
                    }
                }
                buffer.drop(heap, reply);
                let (bye, st2) = (pg.terminate(heap), 0);
                borrow bye as &bb in {
                    pg.send(ch, buffer.bytes(bb));
                }
                buffer.drop(heap, bye);
            }
            conn_close(conn);
            return result;
        }
    }
}

// Copy the lines of `text` (an `endpoints.conf`, which the caller has already had `endpoints.parse` accept) into the table, in
// one transaction. A row whose id is already there is left as it is. Answers how many rows were added, or -1 (could not connect),
// -2 (could not log in), -3 (the database refused one; nothing was added).
pub fn copy_in[&h, &n, &t, &u, &w, &d, &z, &x](heap: &!h Heap, net: &n Net(""), host: &t [byte], port: int, user: &u [byte], password: &w [byte], database: &d [byte], rng: &z Fs(""), text: &x [byte]) -> [heap, net_out(""), conn_read, conn_write, fs_read("")] int {
    match open_db(heap, net, host, port, user, password, database, rng) {
        Dialed::Failed(code) => {
            return code;
        }
        Dialed::Ok(dialed) => {
            var conn = dialed;
            var added = 0;
            var bad = false;
            borrow mut conn as &!ch in {
                let (b, bs) = pg.simple(heap, ch, "begin");
                buffer.drop(heap, b);
                var at = 0;
                while at < len(text) && !bad {
                    var end = at;
                    while end < len(text) && int_of(text[end]) != '\n' {
                        end = end + 1;
                    }
                    let first = endpoints.field(text, at, end);
                    if first.0 < first.1 && int_of(text[first.0]) != '#' {
                        let hostf = endpoints.field(text, first.1, end);
                        let portf = endpoints.field(text, hostf.1, end);
                        let secret = endpoints.field(text, portf.1, end);
                        // the optional words (`endpoints.parse_x` has judged them)
                        var types_w = text[0..0];
                        var headers_w = text[0..0];
                        var old_w = text[0..0];
                        var until = 0;
                        var rest = secret.1;
                        var more = true;
                        while more {
                            let w = endpoints.field(text, rest, end);
                            if w.0 == w.1 {
                                more = false;
                            } else {
                                rest = w.1;
                                let word = text[w.0..w.1];
                                if bytes.starts_with(word, "types=") {
                                    types_w = word[6..len(word)];
                                } else if bytes.starts_with(word, "headers=") {
                                    headers_w = word[8..len(word)];
                                } else if bytes.starts_with(word, "old=") {
                                    var at2 = 4;
                                    while at2 < len(word) && int_of(word[at2]) != '@' {
                                        at2 = at2 + 1;
                                    }
                                    old_w = word[4..at2];
                                    until = endpoints.number_ms(word, at2 + 1, len(word));
                                }
                            }
                        }
                        let (reply, st) = queries.add_endpoint(heap, ch, endpoints.number(text, first.0, first.1), text[hostf.0..hostf.1], endpoints.number(text, portf.0, portf.1), text[secret.0..secret.1], types_w, headers_w, old_w, until);
                        borrow reply as &rb in {
                            if st != 0 || pg.failure(buffer.bytes(rb)) >= 0 {
                                bad = true;
                            } else {
                                added = added + pg.affected(buffer.bytes(rb));
                            }
                        }
                        buffer.drop(heap, reply);
                    }
                    at = end + 1;
                }
                var finish = "commit";
                if bad {
                    finish = "rollback";
                }
                let (c, cs) = pg.simple(heap, ch, finish);
                buffer.drop(heap, c);
                let bye = pg.terminate(heap);
                borrow bye as &bb in {
                    pg.send(ch, buffer.bytes(bb));
                }
                buffer.drop(heap, bye);
            }
            conn_close(conn);
            if bad {
                return 0 - 3;
            }
            return added;
        }
    }
}
