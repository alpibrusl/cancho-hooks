edition 5;

module reason;

// `reason` -- why a delivery attempt failed (`docs/design.md` section 34.3).
//
// An attempt ends with one *code*: an HTTP status (100 and up) or one of the negative answers of `attempt.ls`. This module turns a code into
// a **reason**, a small number that is written to the delivery log (kind 14, `state.reason()`), to the history table (`attempts.reason`) and
// counted in `/metrics`, and into the **legacy status** the history's `status` column has always held (-1 could not connect, -2 could not
// send, -3 timed out, -4 no answer), so that column keeps its meaning for everyone who reads it.
//
// The numbers of the reasons are stored on disk: a reason is never renumbered or reused, and a new one goes at the end. The names are what
// the API and `/metrics` print, and are fixed for the same reason. There are `count()` of them, which bounds the label of a metric.

// 0: not a failure (a `2xx`).
pub fn none() -> [] int {
    return 0;
}

pub fn connect_refused() -> [] int {
    return 1;
}

pub fn connect_timeout() -> [] int {
    return 2;
}

// Any other way the connection could not be made: unreachable, reset, no route, a descriptor limit.
pub fn connect_error() -> [] int {
    return 3;
}

pub fn send_timeout() -> [] int {
    return 4;
}

pub fn send_error() -> [] int {
    return 5;
}

// The deadline passed with the request sent and no status line back.
pub fn no_response() -> [] int {
    return 6;
}

pub fn reset() -> [] int {
    return 7;
}

pub fn closed_early() -> [] int {
    return 8;
}

pub fn bad_response() -> [] int {
    return 9;
}

pub fn status_3xx() -> [] int {
    return 10;
}

pub fn status_4xx() -> [] int {
    return 11;
}

pub fn status_5xx() -> [] int {
    return 12;
}

// `410 Gone`: the receiver says the endpoint no longer exists.
pub fn gone() -> [] int {
    return 13;
}

// A status that is none of the above: 1xx, or 6xx and up.
pub fn status_other() -> [] int {
    return 14;
}

// Not made: every one of the service's connections was in use.
pub fn busy() -> [] int {
    return 15;
}

// Not made: the request does not fit the slot.
pub fn too_large() -> [] int {
    return 16;
}

// How many reasons there are, counting 0.
pub fn count() -> [] int {
    return 17;
}

// The reason an attempt's code means: 0 for a `2xx`.
pub fn of(code: int) -> [] int {
    if code >= 200 && code < 300 {
        return none();
    }
    if code == 410 {
        return gone();
    }
    if code >= 300 && code < 400 {
        return status_3xx();
    }
    if code >= 400 && code < 500 {
        return status_4xx();
    }
    if code >= 500 && code < 600 {
        return status_5xx();
    }
    if code >= 100 {
        return status_other();
    }
    // The negative codes of `attempt.ls`, written as numbers so that this module needs nothing from it.
    if code == 0 - 5 {
        return connect_refused();
    }
    if code == 0 - 6 {
        return connect_timeout();
    }
    if code == 0 - 7 {
        return send_timeout();
    }
    if code == 0 - 8 {
        return no_response();
    }
    if code == 0 - 9 {
        return reset();
    }
    if code == 0 - 10 {
        return closed_early();
    }
    if code == 0 - 11 {
        return bad_response();
    }
    if code == 0 - 12 {
        return busy();
    }
    if code == 0 - 13 {
        return too_large();
    }
    if code == 0 - 2 {
        return send_error();
    }
    if code == 0 - 3 {
        return no_response();
    }
    if code == 0 - 4 {
        return closed_early();
    }
    return connect_error();
}

// The `status` the history table holds for an attempt: the HTTP status, or the coarse negative reason it always held.
pub fn legacy_status(code: int) -> [] int {
    if code >= 100 {
        return code;
    }
    if code == 0 - 5 || code == 0 - 12 {
        return 0 - 1;
    }
    if code == 0 - 6 || code == 0 - 7 || code == 0 - 8 {
        return 0 - 3;
    }
    if code == 0 - 9 || code == 0 - 10 || code == 0 - 11 {
        return 0 - 4;
    }
    if code == 0 - 13 {
        return 0 - 2;
    }
    return code;
}

// The name of reason `r`, as the API and the metrics print it. An unknown number (a log from a newer version) is "unknown".
pub fn name(r: int) -> [] &static [byte] {
    if r == 0 {
        return "none";
    }
    if r == 1 {
        return "connect_refused";
    }
    if r == 2 {
        return "connect_timeout";
    }
    if r == 3 {
        return "connect_error";
    }
    if r == 4 {
        return "send_timeout";
    }
    if r == 5 {
        return "send_error";
    }
    if r == 6 {
        return "no_response";
    }
    if r == 7 {
        return "reset";
    }
    if r == 8 {
        return "closed_early";
    }
    if r == 9 {
        return "bad_response";
    }
    if r == 10 {
        return "status_3xx";
    }
    if r == 11 {
        return "status_4xx";
    }
    if r == 12 {
        return "status_5xx";
    }
    if r == 13 {
        return "gone";
    }
    if r == 14 {
        return "status_other";
    }
    if r == 15 {
        return "busy";
    }
    if r == 16 {
        return "too_large";
    }
    return "unknown";
}
