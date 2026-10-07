# The mutants of kept connections (docs/design.md section 53): (id, file, the text to change, what to change it to, the test files that should fail).
# Each `old` occurs exactly once in its file. Run with `python3 scripts/mutate.py tests/mutants/keepalive.py [ids...]` (and `PURE=1` for the pure build).
# Tally (default build, 2026-10-07): 20 killed, 4 argued equivalent:
#   K01  a connection is parked only when keep-alive is on (`e_keep`, where the status line is read), so with it off there is nothing idle for `reuse` to take.
#   K03  a connection never changes scheme within an endpoint: the scheme changes only by a `PATCH`, which retires the endpoint's connections (section 53.5).
#   K15  a head that fills the 8 KiB is doomed anyway: the next read is into an empty buffer, reads nothing, and the connection is closed at once (stage 15,
#        `bighead`, closes within 1 s with or without the check).
#   K17  an idle TLS connection that wakes is closed either way; the mutant only reads up to 16 bytes from the socket first, past the TLS library, which is
#        not told and so marks nothing.
MUTANTS = [
 # reuse: what may be taken
 ("K01", "src/attempt.ls", "    if at[env_at() + e_keep()] != 1 || len(request) > req_max() {", "    if len(request) > req_max() {", ["keepalive"]),
 ("K02", "src/attempt.ls", "        if at[b] == idle() && at[b + 1] == endpoint && at[b + f_key()] == key && at[b + f_uses()] < most_uses() {", "        if at[b] == idle() && at[b + f_key()] == key && at[b + f_uses()] < most_uses() {", ["keepalive"]),
 ("K03", "src/attempt.ls", "    var k = name_key(name, port) * 2;\n    if secure {\n        k = k + 1;\n    }", "    var k = name_key(name, port) * 2;", ["keepalive"]),
 ("K04", "src/attempt.ls", "    at[b + f_reused()] = 1;\n    at[b + f_uses()] = at[b + f_uses()] + 1;", "    at[b + f_reused()] = 0;\n    at[b + f_uses()] = at[b + f_uses()] + 1;", ["keepalive"]),
 # the race: one redial on a reused connection
 ("K05", "src/attempt.ls", "    if at[b + f_reused()] != 1 {\n        return code;\n    }\n    drop_tls(ffi, at, slot);", "    if true {\n        return code;\n    }\n    drop_tls(ffi, at, slot);", ["keepalive"]),
 ("K06", "src/attempt.ls", "    at[b + f_reused()] = 0;\n    at[b + f_born()] = 0;\n    at[b + f_uses()] = 1;\n    at[b] = redialing();", "    at[b + f_born()] = 0;\n    at[b + f_uses()] = 1;\n    at[b] = redialing();", ["keepalive"]),
 # framing (section 53.2)
 ("K07", "src/attempt.ls", "    if int_of(h[7]) != '1' {\n        return 0 - 1;\n    }", "    if false {\n        return 0 - 1;\n    }", ["keepalive"]),
 ("K08", "src/attempt.ls", "            if digits == 0 || v != end || n > body_max() || length >= 0 {", "            if digits == 0 || v != end || n > body_max() {", ["keepalive"]),
 ("K09", "src/attempt.ls", "            if digits == 0 || v != end || n > body_max() || length >= 0 {", "            if digits == 0 || v != end || length >= 0 {", ["keepalive"]),
 ("K10", "src/attempt.ls", "                    return 0 - 1;\n                }\n                v = v + 1;\n            }\n        }\n        line = end + 2;", "                    return m_none();\n                }\n                v = v + 1;\n            }\n        }\n        line = end + 2;", ["keepalive"]),
 ("K11", "src/attempt.ls", "    if chunked && length >= 0 {\n        return 0 - 1;\n    }", "    if false {\n        return 0 - 1;\n    }", ["keepalive"]),
 ("K12", "src/attempt.ls", "    if chunked {\n        return m_chunked();\n    }", "    if chunked {\n        return m_none();\n    }", ["keepalive"]),
 ("K13", "src/attempt.ls", "        if len(d) > at[b + f_left()] {\n            return false;\n        }", "        if false {\n            return false;\n        }", ["keepalive"]),
 ("K14", "src/attempt.ls", "    return at[b + f_chunk()] == 9;", "    return at[b + f_chunk()] >= 6;", ["keepalive"]),
 # drain and tend
 ("K15", "src/attempt.ls", "            } else if at[b + 5] >= head_max() {\n                at[b] = doomed();", "            } else if false {\n                at[b] = doomed();", ["keepalive"]),
 ("K16", "src/attempt.ls", "        if k != 0 - 1 {\n            at[b] = doomed();\n        }\n    }\n    return 0;", "        if k < 0 - 1 {\n            at[b] = doomed();\n        }\n    }\n    return 0;", ["keepalive"]),
 ("K17", "src/attempt.ls", "        if at[b + f_flags()] % 2 == 1 {\n            // Not read", "        if false {\n            // Not read", ["keepalive", "sessions"]),
 # sweep_parked: the bounds
 ("K18", "src/attempt.ls", "                at[b + 3] = now + idle_ms();", "                at[b + 3] = now + idle_ms() * 100;", ["keepalive"]),
 ("K19", "src/attempt.ls", "            if at[b] == draining() && now >= at[b + 3] {\n                close_it = true;\n            }", "", ["keepalive"]),
 ("K20", "src/attempt.ls", "    while slots() - held < starts_room() {", "    while false {", ["keepalive"]),
 ("K21", "src/attempt.ls", "    if at[b + f_flags()] % 2 == 1 && at[b] != draining() {\n        let base", "    if false {\n        let base", ["sessions", "keepalive"]),
 # retire, and who calls it
 ("K22", "src/attempt.ls", "        if at[b] >= draining() && at[b + 1] == e {\n            at[b] = doomed();", "        if false {\n            at[b] = doomed();", ["keepalive"]),
 ("K23", "src/hooks.ls", "    if code == 410 {\n        // The endpoint is being disabled: its kept connections go (section 53.5).\n        attempt.retire(at, e);\n    }", "", ["keepalive"]),
 # the switch
 ("K24", "src/hooks.ls", "links / 2 % 2 == 1", "true", ["keepalive"]),
]
