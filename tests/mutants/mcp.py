# The mutants of hooks-mcp (docs/design.md section 51): (id, file, the text to change, what to change it to, the test files that should fail).
# Each `old` occurs exactly once in its file. Run with `CANCHO=/path/to/cancho python3 scripts/mutate.py tests/mutants/mcp.py [ids...]` after `cancho build` has made `build/hooks`:
# only `hooks-mcp` is rebuilt for each mutant, and `tests/mcp_test.py` is run (it stops at its first failure).
BUILD = ["{LEX}", "build", "--bin", "hooks-mcp"]
F = "tools/mcp.cho"
MUTANTS = [
 # what is offered and what is allowed
 ("M01", F, "        if tool <= last_read_tool() || allow_write {\n            w = json.put_fragment", "        if true {\n            w = json.put_fragment", ["mcp"]),
 ("M02", F, "    if tool == 0 || tool > last_read_tool() && !allow_write {", "    if tool == 0 {", ["mcp"]),
 ("M03", F, "fn last_read_tool() -> [] int {\n    return 6;", "fn last_read_tool() -> [] int {\n    return 7;", ["mcp"]),
 # the arguments
 ("M04", F, "    if v < low || v > high {\n        return 0 - 2;", "    if v > high {\n        return 0 - 2;", ["mcp"]),
 ("M05", F, "    if v < low || v > high {\n        return 0 - 2;", "    if v < low {\n        return 0 - 2;", ["mcp"]),
 ("M06", F, "    if !json.fits_int(src, tape, n) {\n        return 0 - 2;", "    if false {\n        return 0 - 2;", ["mcp"]),
 ("M07", F, "            if json.string_plain(tape, before) && bytes.equal(", "            if false && bytes.equal(", ["mcp"]),
 ("M08", F, "    if !keys_ok(src, tape, args, tool_args(tool)) {", "    if false {", ["mcp"]),
 ("M09", F, "        } else if fnode >= 0 && has_type_key(src, tape, fnode) {", "        } else if false {", ["mcp"]),
 ("M10", F, "depth_of(tape, fnode) > max_nesting()", "false", ["mcp"]),
 ("M11", F, "            if made > most {", "            if made > most + 100000 {", ["mcp"]),
 ("M12", F, "|| key_len > 0 && !key_text(idem[0..key_len]) {", "{", ["mcp"]),
 ("M13", F, "        if c < 33 || c > 126 {\n            return false;\n        }\n        i = i + 1;\n    }\n    return true;\n}\n\n// How deeply", "        if c < 32 || c > 126 {\n            return false;\n        }\n        i = i + 1;\n    }\n    return true;\n}\n\n// How deeply", ["mcp"]),
 # the protocol
 ("M14", F, "    // A notification is never answered, and never acted on: there is nowhere to say what came of it.\n    if idn < 0 {\n        return buffer.empty(heap, 1);\n    }\n", "", ["mcp"]),
 ("M15", F, "src[tape[3 * idn + 1] - 1..tape[3 * idn + 2] + 1]", "src[tape[3 * idn + 1]..tape[3 * idn + 2]]", ["mcp"]),
 ("M16", F, "                    out = buffer.push(heap, joined, byte_of(']'));", "                    out = joined;", ["mcp"]),
 ("M17", F, "fn max_line() -> [] int {\n    return 1048576;", "fn max_line() -> [] int {\n    return 1048577;", ["mcp"]),
 ("M18", F, " || json.string_equals(src, tape, n, \"2024-11-05\")", "", ["mcp"]),
 ("M19", F, "    if method < 0 && (json.get(src, tape, node, \"result\") >= 0 || json.get(src, tape, node, \"error\") >= 0) {", "    if false {", ["mcp"]),
 # the token
 ("M20", F, "    if status == 0 && tlen > 0 && !is_loopback(host) && !allow_remote {", "    if false {", ["mcp"]),
 ("M21", F, "    if !bytes.starts_with(host, \"127.\") || bytes.count_byte(host, '.') != 3 {", "    if !bytes.starts_with(host, \"127.\") {", ["mcp"]),
 ("M22", F, "    if bytes.starts_with(a, \"--\") {\n        var end = 0;", "    if true {\n        var end = 0;", ["mcp"]),
 ("M23", F, "    while end > 0 && int_of(into[end - 1]) <= 32 {", "    while end > 0 && int_of(into[end - 1]) <= 0 {", ["mcp"]),
 ("M24", F, "    if len(token) > 0 {\n        r = buffer.append(heap, r, \"Authorization: Bearer \");", "    if true {\n        r = buffer.append(heap, r, \"Authorization: Bearer \");", ["mcp"]),
 # the HTTP client
 ("M25", F, "    } else if status >= 200 && status < 300 {", "    } else if status >= 200 && status < 400 {", ["mcp"]),
 ("M26", F, "                if iequals(value, \"chunked\") {\n                    chunked = 1;", "                if iequals(value, \"chunked\") {\n                    chunked = 0;", ["mcp"]),
 ("M27", F, "    let deadline = clock_ms(clock) + wait_ms;", "    let deadline = clock_ms(clock) + wait_ms / 1000;", ["mcp"]),
 ("M29", F, "    if tool == 10 {\n        return \"DELETE\";", "    if tool == 10 {\n        return \"POST\";", ["mcp"]),
 ("M30", F, "        if len(b) - at < clen {\n            code = 0 - 4;", "        if len(b) - at < 0 {\n            code = 0 - 4;", ["mcp"]),
]
