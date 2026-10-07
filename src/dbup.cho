edition 5;

module dbup;

import std.bytes;

// `dbup` -- the database coming up (`docs/design.md` section 37). The decision, kept apart from the loop so that it can be tested alone.
//
// The service starts without waiting for the database: it listens, takes events, and its pool makes the connections in the background.
// Until the endpoints have been read from the table once, nothing is delivered. Whether that is going to happen is judged here, from what
// the pool says about its last attempt (`pool.last_failure`, `pool.sqlstate`) and how long it has been:
//
//   * an attempt the server **refused for good** is the end at once, as the blocking start was: a login that the server refuses with an
//     invalid authorization (SQLSTATE class 28: a wrong password, a role that does not exist, no `pg_hba` line) or an unknown database
//     (`3D000`), a login this client cannot do, or a statement the server will not prepare (a table is missing). Waiting does not mend any
//     of these, and a service that waited would sit there not ready with nothing to say. A refusal that is only *for now* (`57P03` the
//     system is starting up, `53300` too many connections) is waited out like a connection that fails;
//   * a connection that cannot be made, or a server that does not answer, is waited for `pg-start-wait-ms` (0: for ever), and then it is the end.
//
// The reasons (`message` has the words, and the service ends with status 20 for each):
//
//     1 cannot connect   2 cannot log in   3 the query failed   4 the database did not answer in time   5 a row has an empty field or a
//     byte that is not printable   6 the table is too large (its text over `endpoints.text_limit()`, or the reply over the pool's input slab)

// The tag the read of the table goes under on the pool (an insert of the history is 1, the API's reads 100 and above, the schedules' above those).
pub fn load_tag() -> [] int {
    return 2;
}

// Is `state` (the five bytes of a SQLSTATE, or nothing) one that waiting does not mend: class 28, or `3D000`?
fn refused_for_good[&s](state: &s [byte]) -> [] bool {
    if len(state) < 5 {
        return false;
    }
    if int_of(state[0]) == '2' && int_of(state[1]) == '8' {
        return true;
    }
    return int_of(state[0]) == '3' && int_of(state[1]) == 'D' && int_of(state[2]) == '0' && int_of(state[3]) == '0' && int_of(state[4]) == '0';
}

// The words for a SQLSTATE that is a refusal only for now, after the code in the message the service ends with: after waiting `pg-start-wait-ms`, a
// server that still refused says why (`53300` is a server with every connection taken: by whom is in its `pg_stat_activity`).
pub fn state_words[&s](state: &s [byte]) -> [] &static [byte] {
    if bytes.equal(state, "53300") {
        return ": the server has too many connections";
    }
    if bytes.equal(state, "57P03") {
        return ": the server is starting up";
    }
    return "";
}

// 0 if the endpoints may still be read; otherwise the reason (above) the service ends. `failure` is `pool.last_failure` (1 the server closed the
// connection, 3 a read failed, 4 the server refused the login, 5 a login this client cannot do, 6 a write failed, 7 SCRAM failed on this side,
// 8 a message too large, 9 the server refused a statement, 10 not the protocol, 20 the connect failed, 21 the attempt ran out of time, 0 none yet),
// `state` the SQLSTATE of the server's last error, `waited_ms` the time since the service began to wait and `limit_ms` the most it may (0: for ever).
pub fn verdict[&s](failure: int, state: &s [byte], waited_ms: int, limit_ms: int) -> [] int {
    if failure == 4 && refused_for_good(state) {
        return 2;
    }
    if failure == 5 || failure == 7 {
        return 2;
    }
    if failure == 9 {
        return 3;
    }
    if limit_ms > 0 && waited_ms >= limit_ms {
        if failure == 4 {
            return 2;
        }
        if failure == 0 || failure == 21 {
            return 4;
        }
        return 1;
    }
    return 0;
}

// What `roster.text_of` answered (-3 the query was refused, -4 a field is empty or not printable, -5 the table is too large) as a reason.
pub fn reason_of_text(code: int) -> [] int {
    if code == 0 - 4 {
        return 5;
    }
    if code == 0 - 5 {
        return 6;
    }
    return 3;
}

pub fn message(reason: int) -> [] &static [byte] {
    if reason == 1 {
        return "cannot connect";
    }
    if reason == 2 {
        return "cannot log in";
    }
    if reason == 3 {
        return "the query failed (are the tables there? apply sql/schema.sql: endpoints, attempts and schedules)";
    }
    if reason == 4 {
        return "the database did not answer in time";
    }
    if reason == 5 {
        return "a row has an empty field or a byte that is not printable";
    }
    return "the table is too large (the service reads at most 540,672 bytes of it, 528 an endpoint, and the pool's answer at most 1 MiB)";
}
