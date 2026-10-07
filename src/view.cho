edition 5;

module view;

import std.buffer;
import std.json;
import http.server;
import pg;
import queries;
import reason;

// `view` -- what the database says, as an HTTP answer (`docs/design.md` section 24).

fn outcome_name(n: int) -> [] &static [byte] {
    if n == 1 {
        return "delivered";
    }
    if n == 2 {
        return "failed";
    }
    return "dead";
}

// The answer to `GET /events/:id/attempts`: the reply of `queries.attempts_of`, as a whole HTTP response. A reply that is not a
// success, or a request the pool could not answer (`status` not 0), is a `503`.
pub fn attempts_reply[&h, &m](heap: &!h Heap, rep: &m [byte], status: int, keep: bool) -> [heap] buffer.Buffer {
    if status != 0 || pg.failure(rep) >= 0 {
        return server.failure(heap, buffer.empty(heap, 256), 503, "the database could not answer", keep);
    }
    var w = json.writer(heap, 4096);
    w = json.begin_array(heap, w);
    var at = pg.first_row(rep);
    while at >= 0 {
        w = json.begin_object(heap, w);
        w = json.put_key(heap, w, "endpoint");
        w = json.put_int(heap, w, queries.attempts_of_endpoint(rep, at));
        w = json.put_key(heap, w, "replay");
        w = json.put_bool(heap, w, queries.attempts_of_replay(rep, at) == 1);
        w = json.put_key(heap, w, "attempt");
        w = json.put_int(heap, w, queries.attempts_of_attempt(rep, at));
        w = json.put_key(heap, w, "outcome");
        w = json.put_string(heap, w, outcome_name(queries.attempts_of_outcome(rep, at)));
        w = json.put_key(heap, w, "status");
        w = json.put_int(heap, w, queries.attempts_of_status(rep, at));
        w = json.put_key(heap, w, "reason");
        if queries.attempts_of_reason(rep, at) == 0 && queries.attempts_of_outcome(rep, at) != 1 {
            // A row written before the reason was recorded.
            w = json.put_string(heap, w, "unrecorded");
        } else {
            w = json.put_string(heap, w, reason.name(queries.attempts_of_reason(rep, at)));
        }
        w = json.put_key(heap, w, "at");
        w = json.put_int(heap, w, queries.attempts_of_at_ms(rep, at));
        w = json.put_key(heap, w, "latency_ms");
        w = json.put_int(heap, w, queries.attempts_of_latency_ms(rep, at));
        w = json.end_object(heap, w);
        at = pg.next_row(rep, at);
    }
    w = json.end_array(heap, w);
    let body = json.finish(w);
    var answer = buffer.empty(heap, 256);
    borrow body as &bb in {
        answer = server.reply(heap, answer, 200, buffer.bytes(bb), keep);
    }
    buffer.drop(heap, body);
    return answer;
}
