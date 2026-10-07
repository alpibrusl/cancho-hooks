# The mutants of the build with cancho's own TLS (docs/pure-tls.md): (id, the file, the text to change, what to change it to, the test files that should fail).
# Each `old` occurs exactly once in its file. Run with `PURE=1 python3 scripts/mutate.py tests/mutants/pure.py [ids...]` (`scripts/mutate.py` says how).
# `pure/src/tlsx.cho` is the adapter. The reasons a failed handshake is recorded under are what the tests read, so most of these change one of them.
MUTANTS = [
 ("P01", "pure/src/tlsx.cho", "    if code == tls_record.x509_expired() {\n        return 10;", "    if code == tls_record.x509_expired() {\n        return 62;", ["https"]),
 ("P02", "pure/src/tlsx.cho", "    if code == tls_record.x509_not_yet_valid() {\n        return 9;", "    if code == tls_record.x509_not_yet_valid() {\n        return 20;", ["https"]),
 ("P03", "pure/src/tlsx.cho", "    if code == tls_record.x509_name_mismatch() {\n        return 62;", "    if code == tls_record.x509_name_mismatch() {\n        return 20;", ["https"]),
 ("P04", "pure/src/tlsx.cho", "    if code == tls_record.x509_unknown_issuer() {\n        return 20;", "    if code == tls_record.x509_unknown_issuer() {\n        return 62;", ["https"]),
 ("P05", "pure/src/tlsx.cho", "    return 16777216;\n}\n\n// ----", "    return 6;\n}\n\n// ----", ["https"]),
 ("P06", "pure/src/tlsx.cho", "                tt[b + 8] = 1;\n            }\n            return done();", "                tt[b + 8] = 1;\n            }\n            return pending();", ["https"]),
 ("P07", "pure/src/tlsx.cho", "            return fail(tt, b, stage_handshake(), 0 - 1);\n        }\n        if step == 3 {", "            return pending();\n        }\n        if step == 3 {", ["https"]),
 ("P08", "pure/src/tlsx.cho", "            Sent::Again => {\n                return 1;\n            }\n            Sent::Failed(e) => {\n                tt[b + 7] = 0 - 1000 - e;\n                return 2;", "            Sent::Again => {\n                return 0;\n            }\n            Sent::Failed(e) => {\n                tt[b + 7] = 0 - 1000 - e;\n                return 2;", ["https"]),
 ("P09", "pure/src/tlsx.cho", "                tt[b + 3] = tt[b + 3] + k;", "                tt[b + 3] = tt[b + 3] + 1;", ["https"]),
 ("P10", "pure/src/tlsx.cho", "        // The engine's output queue is full: wait for the socket to take it.\n        want(tab, poller, tt, b, slot, token, 2);\n        return 0 - 1;", "        // The engine's output queue is full: wait for the socket to take it.\n        want(tab, poller, tt, b, slot, token, 2);\n        return 0 - 2;", ["https"]),
 ("P12", "pure/src/tlsx.cho", "    let code = tls.start_with(engine, slot, host, now_ms, session);", "    let code = tls.start_with(engine, slot, host, 0, session);", ["https"]),
 ("P13", "pure/src/tlsx.cho", "        if len(cafile) > 0 {\n            roots = load(engine, fs, cafile, buf);", "        if false {\n            roots = load(engine, fs, cafile, buf);", ["https"]),
 ("P14", "pure/src/tlsx.cho", "    if n < 1 || n >= len(into) {\n        return 0;\n    }", "    if n < 1 {\n        return 0;\n    }", ["https", "pure"]),
 ("P15", "pure/src/tlsx.cho", "    tls.finish(engine, slot);\n    flush(engine, tab, tt, b, out, slot);", "    flush(engine, tab, tt, b, out, slot);", ["https", "pure"]),
 ("P16", "pure/src/tlsx.cho", "                tls.set_tickets_per_pool(engine, epx.max_concurrency());\n                seeded = true;", "                tls.set_tickets_per_pool(engine, epx.max_concurrency());\n                seeded = false;", ["https"]),
 ("P17", "pure/src/tlsx.cho", "        if c >= 0 && c < k {\n                tt[b + 1] = c;", "        if false {\n                tt[b + 1] = c;", ["https", "pure"]),
 ("P18", "pure/src/tlsx.cho", "    if code == tls_record.x509_bad_signature() {\n        return 7;", "    if code == tls_record.x509_bad_signature() {\n        return 62;", ["https", "pure"]),
 ("P19", "pure/src/tlsx.cho", "    return tls.save_to(engine, slot, session);", "    return 0;", ["pure"]),
 ("P20", "pure/src/tlsx.cho", "            if tls.resumed(engine, slot) {\n                tt[b + 8] = 1;", "            if false {\n                tt[b + 8] = 1;", ["pure"]),
 ("P21", "pure/src/tlsx.cho", "    let code = tls.start_with(engine, slot, host, now_ms, session);", "    let code = tls.start_with(engine, slot, host, now_ms, 0);", ["pure"]),
 ("P22", "pure/src/tlsx.cho", "                tls.set_resumption(engine, true);", "                tls.set_resumption(engine, false);", ["pure"]),
 # A pool of tickets an endpoint (cancho docs/tls-resumption.md §12): a burst to one endpoint resumes only if each connection finds a ticket.
 ("P23", "pure/src/tlsx.cho", "tls.set_tickets_per_pool(engine, epx.max_concurrency());", "tls.set_tickets_per_pool(engine, 1);", ["sessions"]),
 ("P24", "pure/src/tlsx.cho", "    return tls.save_to(engine, slot, session);", "    return tls.save_to(engine, slot, 0);", ["sessions"]),
 ("P25", "scripts/make_pure.py", "    if at[env_at() + e_keys() + e] != key {\n        drop_session(engine, at, e);\n    }\n", "    drop_session(engine, at, e);\n", ["sessions"]),
]

# Survivors, and why (docs/pure-tls.md): P07, P10 and P17 each guard a state the service cannot reach. P07: by the time a closed socket reaches the last line of the handshake's handling,
# the engine has failed with `tls-peer-closed` and the branch before it returned. P10: `write` has just flushed the engine's output queue, so `send` cannot find it full. P17: the engine
# takes everything the socket gave unless its plaintext buffer is full, and the service reads a status line of 12 bytes and finishes.
