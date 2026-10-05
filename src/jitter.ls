edition 5;

module jitter;

// `jitter` -- the spread given to a retry delay (`docs/design.md` section 39.3).
//
// The Standard Webhooks schedule says when to try again (5 s, 5 min, 30 min, ...). A receiver that was down for a minute fails every event that
// arrived in it at nearly the same moment, and a schedule with no spread asks all of them again at the same moment, and again at the next
// step: the recovering receiver is hit by the whole backlog at once, over and over. `retry-jitter` is the cure: each delay is moved by a
// number of percent of itself, up or down, so the retries of events that failed together fall due over a stretch of time.
//
// **The spread is a function of (endpoint, event, attempt) and of nothing else**: no clock, no random source, no state. The same pair at the
// same attempt always gets the same spread, so a restart that works the delay out again would get the same one; the service does not even
// need to, because the time of the next attempt is written to `delivery.seg` with the outcome (`state.failed()`, its fifth field) and a
// restart reads that, whatever the setting is by then. Events that failed at the same instant get different spreads because their ids differ.
//
// `percent` is 0 to 50: the delay is `base + d` with `d` between `-base * percent / 100` and `+base * percent / 100` (integer division, so a
// delay too short for the spread to be a whole millisecond is not moved), every value in between about equally likely. 0 returns `base`
// exactly: today's schedule. 50 is the largest: a delay is then never less than half of itself, and never more than one and a half times.

pub fn max_percent() -> [] int {
    return 50;
}

// The prime 2^31 - 1: the modulus of the mixing below. Every product in it stays under 2^47.
fn modulus() -> [] int {
    return 2147483647;
}

// A number from 0 to 2^31 - 2 that depends on the three arguments, and changes a lot when any of them changes by one. Each round multiplies by a
// constant of 16 bits (so nothing overflows) and folds the high bits back into the low ones, so that neighbouring events are not neighbours in
// the result.
pub fn mix(endpoint: int, event: int, attempt: int) -> [] int {
    var h = (endpoint % modulus() + 1) * 48271 % modulus();
    h = (h + event % modulus() + 1) * 48271 % modulus();
    h = (h + attempt % modulus() + 1) * 16807 % modulus();
    var round = 0;
    while round < 3 {
        h = (h ^ h >> 15) * 48271 % modulus();
        h = (h ^ h >> 13) * 16807 % modulus();
        round = round + 1;
    }
    return h;
}

// How far a delay of `base` ms may move either way with `percent` (0 to 50).
pub fn span(base: int, percent: int) -> [] int {
    if base <= 0 || percent <= 0 {
        return 0;
    }
    var p = percent;
    if p > max_percent() {
        p = max_percent();
    }
    return base * p / 100;
}

// The delay to wait before attempt number `attempt` (the one just failed, counted from 1) of `event` at `endpoint`, in ms: `base` moved by up to
// `percent` of itself in either direction, deterministically (above).
pub fn delay(base: int, percent: int, endpoint: int, event: int, attempt: int) -> [] int {
    let s = span(base, percent);
    if s == 0 {
        return base;
    }
    // 2s + 1 values, from -s to +s, picked by scaling the mix (not by its remainder, which would favour the low values for a wide span).
    let pick = mix(endpoint, event, attempt) * (2 * s + 1) / modulus();
    return base - s + pick;
}
