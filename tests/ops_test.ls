edition 5;

import std.buffer;
import std.bytes;
import std.test;
import metrics;
import ops;
import reason;

// What an operator watches (`src/ops.ls`, `src/metrics.ls`; `docs/design.md` sections 34.1 to 34.4): the counters, when the service is ready, how it learns it is to stop,
// and the text `/metrics` is made of.

fn fresh[&a](o: &!a [int]) -> [] int {
    ops.init(o);
    ops.begin(o, 1000, 0, 0);
    return 0;
}

fn test_counters_start_at_zero_and_count() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 7);
        fresh(o);
        test.assert_eq(ops.accepted_count(o), 0);
        test.assert_eq(ops.duplicate_count(o), 0);
        test.assert_eq(ops.refused_count(o), 0);
        test.assert_eq(ops.started(o), 1000);
        ops.accepted(o);
        ops.accepted(o);
        ops.duplicate(o);
        test.assert_eq(ops.accepted_count(o), 2);
        test.assert_eq(ops.duplicate_count(o), 1);
        test.assert_eq(ops.refused_count(o), 0);
    }
    return 0;
}

// A refusal is counted once in the total and once under its status; a status the metric does not name goes under "other".
fn test_a_refusal_is_counted_by_its_status() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        fresh(o);
        ops.refused(o, 400);
        ops.refused(o, 413);
        ops.refused(o, 413);
        ops.refused(o, 422);
        ops.refused(o, 503);
        ops.refused(o, 507);
        ops.refused(o, 418);
        test.assert_eq(ops.refused_count(o), 7);
        test.assert_eq(ops.refused_by(o, 0), 1);
        test.assert_eq(ops.refused_by(o, 1), 2);
        test.assert_eq(ops.refused_by(o, 2), 1);
        test.assert_eq(ops.refused_by(o, 3), 1);
        test.assert_eq(ops.refused_by(o, 4), 1);
        test.assert_eq(ops.refused_by(o, 5), 1);
        test.assert_eq(ops.refused_status(0), 400);
        test.assert_eq(ops.refused_status(4), 507);
        test.assert_eq(ops.statuses(), 6);
    }
    return 0;
}

// A commit is a flush that moved how far a log is durable; the size at the start is the baseline, not a commit; a turn that moved nothing is none.
fn test_a_commit_is_a_log_becoming_durable_further() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        ops.init(o);
        ops.begin(o, 5, 300, 40);
        ops.look_at_log(o, 0, 300);
        ops.look_at_log(o, 1, 40);
        test.assert_eq(ops.commits(o, 0), 0);
        test.assert_eq(ops.commits(o, 1), 0);
        ops.look_at_log(o, 0, 353);
        test.assert_eq(ops.commits(o, 0), 1);
        test.assert_eq(ops.commits(o, 1), 0);
        ops.look_at_log(o, 0, 353);
        test.assert_eq(ops.commits(o, 0), 1);
        ops.look_at_log(o, 0, 900);
        ops.look_at_log(o, 1, 117);
        test.assert_eq(ops.commits(o, 0), 2);
        test.assert_eq(ops.commits(o, 1), 1);
    }
    return 0;
}

fn test_failures_are_counted_by_reason_and_remembered_per_endpoint() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        fresh(o);
        ops.attempt_ended(o, 3, reason.connect_refused());
        ops.attempt_ended(o, 3, reason.connect_refused());
        ops.attempt_ended(o, 4, reason.status_5xx());
        test.assert_eq(ops.failures_for(o, reason.connect_refused()), 2);
        test.assert_eq(ops.failures_for(o, reason.status_5xx()), 1);
        test.assert_eq(ops.failures_for(o, reason.reset()), 0);
        test.assert_eq(ops.last_reason(o, 3), reason.connect_refused());
        test.assert_eq(ops.last_reason(o, 4), reason.status_5xx());
        test.assert_eq(ops.last_reason(o, 5), 0);
        // a delivery ends the endpoint's failures and counts none
        ops.attempt_ended(o, 3, 0);
        test.assert_eq(ops.last_reason(o, 3), 0);
        test.assert_eq(ops.failures_for(o, reason.connect_refused()), 2);
        // recovery sets it; a slot out of range is ignored, not written beyond the array
        ops.set_last_reason(o, 61, reason.gone());
        test.assert_eq(ops.last_reason(o, 61), reason.gone());
        ops.set_last_reason(o, 62, reason.gone());
        ops.set_last_reason(o, 0 - 1, reason.gone());
        ops.attempt_ended(o, 99, reason.reset());
        test.assert_eq(ops.failures_for(o, reason.reset()), 1);
        // the last reason cell of the last slot is the last cell of the array but one at most
        test.assert(ops.size() >= 110);
    }
    return 0;
}

// Ready means: not stopping, no log broken, the directory took the last probe, and a database that was named has a connection. The first that fails is the one told.
fn test_readiness_is_each_check_in_its_order() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        fresh(o);
        test.assert_eq(ops.not_ready(o, false, false, false, 0), 0);
        test.assert_eq(ops.not_ready(o, false, false, true, 2), 0);
        test.assert_eq(ops.not_ready(o, false, false, true, 1), 0);
        test.assert_eq(ops.not_ready(o, false, false, true, 0), 5);
        test.assert_eq(ops.not_ready(o, false, false, false, 0), 0);
        test.assert_eq(ops.not_ready(o, true, false, false, 0), 2);
        test.assert_eq(ops.not_ready(o, false, true, false, 0), 3);
        test.assert_eq(ops.not_ready(o, true, true, true, 0), 2);
        ops.probe_set(o, false, 2000);
        test.assert_eq(ops.not_ready(o, false, false, false, 0), 4);
        test.assert_eq(ops.not_ready(o, true, false, false, 0), 2);
        ops.probe_set(o, true, 3000);
        test.assert_eq(ops.not_ready(o, false, false, false, 0), 0);
        ops.begin_stop(o, ops.sigterm(), 4000, 5000);
        test.assert_eq(ops.not_ready(o, false, false, false, 0), 1);
        test.assert_eq(ops.not_ready(o, true, true, true, 0), 1);
        var c = 1;
        while c <= 5 {
            test.assert(len(ops.why_not(c)) > 10);
            test.assert(len(ops.check_name(c)) > 3);
            c = c + 1;
        }
        test.assert_eq(len(ops.why_not(0)), 0);
        test.assert(bytes.equal(ops.check_name(1), "stopping"));
        test.assert(bytes.equal(ops.check_name(5), "database"));
    }
    return 0;
}

// The directory is probed at the first look, then once a second; a clock that went backwards probes again.
fn test_the_probe_is_due_at_first_and_then_once_a_second() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        fresh(o);
        test.assert(ops.probe_due(o, 1000));
        test.assert(ops.probe_ok(o));
        ops.probe_set(o, true, 1000);
        test.assert(!ops.probe_due(o, 1000));
        test.assert(!ops.probe_due(o, 1999));
        test.assert(ops.probe_due(o, 2000));
        test.assert(ops.probe_due(o, 500));
        ops.probe_set(o, false, 2000);
        test.assert(!ops.probe_ok(o));
        ops.probe_set(o, true, 3000);
        test.assert(ops.probe_ok(o));
    }
    return 0;
}

fn test_stopping_has_a_deadline() -> [] int {
    region a {
        let o = alloc_slice[a](ops.size(), 0);
        fresh(o);
        test.assert(!ops.stopping(o));
        test.assert(!ops.deadline_passed(o, 999999));
        ops.begin_stop(o, ops.sigint(), 10000, 3000);
        test.assert(ops.stopping(o));
        test.assert_eq(ops.stop_signal(o), 2);
        test.assert(!ops.deadline_passed(o, 10000));
        test.assert(!ops.deadline_passed(o, 12999));
        test.assert(ops.deadline_passed(o, 13000));
        test.assert(ops.deadline_passed(o, 13001));
        // a deadline of 0 is "do not wait"
        ops.init(o);
        ops.begin_stop(o, ops.sigterm(), 500, 0);
        test.assert(ops.deadline_passed(o, 500));
        ops.set_settings(o, 1500, true);
        test.assert_eq(ops.stop_deadline(o), 1500);
        test.assert_eq(ops.repair_flag(o), 1);
        ops.set_settings(o, 5000, false);
        test.assert_eq(ops.repair_flag(o), 0);
    }
    return 0;
}

// The bits of `signals_pending` (`std.signals`: INT 2, TERM 8) become the signal that asked: TERM wins over INT, and a bit that is not a stop asks for nothing.
fn test_the_signal_that_asked_is_read_from_the_bits() -> [] int {
    test.assert_eq(ops.asked_by(0), 0);
    test.assert_eq(ops.asked_by(2), 2);
    test.assert_eq(ops.asked_by(8), 15);
    test.assert_eq(ops.asked_by(2 | 8), 15);
    // HUP (1), QUIT (4), USR1 (16) and the rest are not claimed by the service, and would not mean "stop" if they were
    test.assert_eq(ops.asked_by(1 | 4 | 16 | 32 | 64 | 128), 0);
    test.assert_eq(ops.asked_by(255), 15);
    return 0;
}

// ---- the text of /metrics

fn has[&t, &n](text: &t [byte], needle: &n [byte]) -> [] bool {
    return bytes.find(text, needle) >= 0;
}

fn count_of[&t, &n](text: &t [byte], needle: &n [byte]) -> [] int {
    var n = 0;
    var from = 0;
    var going = true;
    while going {
        let at = bytes.find(text[from..len(text)], needle);
        if at < 0 {
            going = false;
        } else {
            n = n + 1;
            from = from + at + len(needle);
        }
    }
    return n;
}

fn test_render_names_every_metric_once_with_its_type[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let g = alloc_slice[a](metrics.g_size(), 0);
        let ep = alloc_slice[a](metrics.row(), 0);
        let rs = alloc_slice[a](reason.count(), 0);
        g[metrics.g_ready()] = 1;
        g[metrics.g_accepted()] = 40;
        g[metrics.g_uptime_ms()] = 12345;
        let text = metrics.render(heap, g, ep, 0, rs);
        borrow text as &tb in {
            let t = buffer.bytes(tb);
            test.assert(has(t, "# TYPE hooks_ingest_events_total counter\n"));
            test.assert(has(t, "hooks_ingest_events_total{result=\"accepted\"} 40\n"));
            test.assert(has(t, "hooks_uptime_seconds 12.345\n"));
            test.assert(has(t, "hooks_ready 1\n"));
            test.assert(has(t, "hooks_stopping 0\n"));
            // a family has one HELP and one TYPE line
            test.assert_eq(count_of(t, "# HELP hooks_attempts_total "), 1);
            test.assert_eq(count_of(t, "# TYPE hooks_attempts_total counter\n"), 1);
            // every family is declared before its first sample: the number of TYPE lines equals the number of HELP lines
            test.assert_eq(count_of(t, "# HELP "), count_of(t, "# TYPE "));
            // no endpoints, no per-endpoint sample
            test.assert_eq(count_of(t, "{endpoint="), 0);
            test.assert(has(t, "# TYPE hooks_endpoint_lag_events gauge\n"));
        }
        buffer.drop(heap, text);
    }
    return 0;
}

fn test_uptime_pads_its_milliseconds[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let g = alloc_slice[a](metrics.g_size(), 0);
        let ep = alloc_slice[a](metrics.row(), 0);
        let rs = alloc_slice[a](reason.count(), 0);
        g[metrics.g_uptime_ms()] = 3005;
        var text = metrics.render(heap, g, ep, 0, rs);
        borrow text as &tb in {
            test.assert(has(buffer.bytes(tb), "hooks_uptime_seconds 3.005\n"));
        }
        buffer.drop(heap, text);
        g[metrics.g_uptime_ms()] = 60050;
        text = metrics.render(heap, g, ep, 0, rs);
        borrow text as &tb in {
            test.assert(has(buffer.bytes(tb), "hooks_uptime_seconds 60.050\n"));
        }
        buffer.drop(heap, text);
        g[metrics.g_uptime_ms()] = 7;
        text = metrics.render(heap, g, ep, 0, rs);
        borrow text as &tb in {
            test.assert(has(buffer.bytes(tb), "hooks_uptime_seconds 0.007\n"));
        }
        buffer.drop(heap, text);
    }
    return 0;
}

fn test_every_reason_has_a_series_and_the_numbers_are_the_counters[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let g = alloc_slice[a](metrics.g_size(), 0);
        let ep = alloc_slice[a](metrics.row(), 0);
        let rs = alloc_slice[a](reason.count(), 0);
        rs[reason.connect_refused()] = 11;
        rs[reason.status_5xx()] = 3;
        let text = metrics.render(heap, g, ep, 0, rs);
        borrow text as &tb in {
            let t = buffer.bytes(tb);
            test.assert_eq(count_of(t, "hooks_attempt_failures_total{reason="), reason.count() - 1);
            test.assert(has(t, "hooks_attempt_failures_total{reason=\"connect_refused\"} 11\n"));
            test.assert(has(t, "hooks_attempt_failures_total{reason=\"status_5xx\"} 3\n"));
            test.assert(has(t, "hooks_attempt_failures_total{reason=\"reset\"} 0\n"));
            test.assert(!has(t, "reason=\"none\""));
        }
        buffer.drop(heap, text);
    }
    return 0;
}

// A row per endpoint: eight series each, labelled by the endpoint's id and nothing else; the reason of the last failure only where there is one.
fn test_each_endpoint_has_its_series[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let g = alloc_slice[a](metrics.g_size(), 0);
        let ep = alloc_slice[a](2 * metrics.row(), 0);
        let rs = alloc_slice[a](reason.count(), 0);
        ep[metrics.e_id()] = 7;
        ep[metrics.e_cursor()] = 90;
        ep[metrics.e_lag()] = 10;
        ep[metrics.e_retries()] = 4;
        ep[metrics.e_last_reason()] = reason.connect_refused();
        ep[metrics.row() + metrics.e_id()] = 12;
        ep[metrics.row() + metrics.e_cursor()] = 100;
        ep[metrics.row() + metrics.e_disabled()] = 1;
        ep[metrics.row() + metrics.e_paused()] = 1;
        ep[metrics.row() + metrics.e_failing_since()] = 1767225600000;
        let text = metrics.render(heap, g, ep, 2, rs);
        borrow text as &tb in {
            let t = buffer.bytes(tb);
            test.assert(has(t, "hooks_endpoint_cursor{endpoint=\"7\"} 90\n"));
            test.assert(has(t, "hooks_endpoint_lag_events{endpoint=\"7\"} 10\n"));
            test.assert(has(t, "hooks_endpoint_retries_waiting{endpoint=\"7\"} 4\n"));
            test.assert(has(t, "hooks_endpoint_disabled{endpoint=\"7\"} 0\n"));
            test.assert(has(t, "hooks_endpoint_cursor{endpoint=\"12\"} 100\n"));
            test.assert(has(t, "hooks_endpoint_disabled{endpoint=\"12\"} 1\n"));
            test.assert(has(t, "hooks_endpoint_paused{endpoint=\"12\"} 1\n"));
            test.assert(has(t, "hooks_endpoint_failing_since_ms{endpoint=\"12\"} 1767225600000\n"));
            test.assert(has(t, "hooks_endpoint_last_failure{endpoint=\"7\",reason=\"connect_refused\"} 1\n"));
            test.assert_eq(count_of(t, "hooks_endpoint_last_failure{"), 1);
            // 2 endpoints x 7 per-endpoint families
            test.assert_eq(count_of(t, "{endpoint="), 2 * 7 + 1);
        }
        buffer.drop(heap, text);
    }
    return 0;
}

// The most endpoints the service has (62) still fit one answer (the server's output buffer is 64 KiB), and the number of series is a function of the endpoints.
fn test_sixty_two_endpoints_fit_one_answer[&h](heap: &!h Heap) -> [heap] int {
    region a {
        let g = alloc_slice[a](metrics.g_size(), 0);
        let ep = alloc_slice[a](62 * metrics.row(), 0);
        let rs = alloc_slice[a](reason.count(), 0);
        var i = 0;
        while i < 62 {
            ep[i * metrics.row() + metrics.e_id()] = 999990 + i;
            ep[i * metrics.row() + metrics.e_cursor()] = 123456789012;
            ep[i * metrics.row() + metrics.e_lag()] = 123456789012;
            ep[i * metrics.row() + metrics.e_retries()] = 1024;
            ep[i * metrics.row() + metrics.e_in_flight()] = 8;
            ep[i * metrics.row() + metrics.e_failing_since()] = 1767225600000;
            ep[i * metrics.row() + metrics.e_last_reason()] = reason.status_5xx();
            ep[i * metrics.row() + metrics.e_disabled()] = 1;
            ep[i * metrics.row() + metrics.e_paused()] = 1;
            i = i + 1;
        }
        let text = metrics.render(heap, g, ep, 62, rs);
        borrow text as &tb in {
            let t = buffer.bytes(tb);
            test.assert(len(t) < 40000);
            test.assert_eq(count_of(t, "{endpoint="), 62 * 8);
        }
        buffer.drop(heap, text);
    }
    return 0;
}
