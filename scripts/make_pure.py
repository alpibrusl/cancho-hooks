#!/usr/bin/env python3
"""Make the sources of the `hooks-pure` build: the service with lex-sys's own TLS in place of OpenSSL (docs/pure-tls.md).

    python3 scripts/make_pure.py [<out-dir>]       (default pure/build/src)

`lex-sys` has no function values and no effect polymorphism (lex-sys `docs/effect-polymorphism.md`), so the 11 functions that carry an `Ffi("libssl")` row
cannot be written once for both backends. This script writes the other one. It copies every file of `src/` except `src/ossl.ls` (the OpenSSL module) into
<out-dir>, with the changes listed in `PATCHES`, and adds `pure/tlsx.ls` (the module that takes its place). The result is not committed, so there is no copy
to drift from `src/`.

**Every change is an exact-match replacement with the number of matches it must make**, and the script fails, saying which, if the source no longer has
exactly that many: a change to the shape of these functions in `src/` cannot be half applied. Line numbers are kept (a change adds no line and removes none),
so a compiler error in the output is at the line of `src/` that caused it.

What the changes do, in short:
- the `Ffi("libcrypto,libssl")` or `Ffi("libssl")` of a function becomes the engine (`tls.Engine`, `lex-sys`'s `packages/tls`) in the same place, and the two
  `ffi(...)` entries leave every row. `attempt.advance` also takes the time (the certificates' dates) and `main` opens the engine once, seeds it and loads the
  trust store (`tlsx.setup`), and closes it at the end;
- every `ossl.` of `attempt.ls` and `hooks.ls` (the OpenSSL module) is `tlsx.` (the adapter), and `import ossl;` imports lex-sys's `tls` and `tlsx`;
- `main` holds no foreign library: the `Ffi` it is given is released at once (libssl and libcrypto were the only ones it narrowed to).
"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Mismatch(Exception):
    pass


def exact(text, old, new, count, what):
    n = text.count(old)
    if n != count:
        raise Mismatch(f"{what}: expected {count} occurrence(s) of\n    {old!r}\nfound {n}")
    return text.replace(old, new)


def regex(text, pattern, new, count, what):
    found = len(re.findall(pattern, text, re.M))
    if found != count:
        raise Mismatch(f"{what}: expected {count} match(es) of /{pattern}/, found {found}")
    return re.sub(pattern, new, text, flags=re.M)


def rows_and_types(text, name, sole, tail, lead, typed_both, typed_ssl):
    """The `ffi(...)` entries of every row and the `Ffi(...)` types of the parameters of the TLS functions."""
    text = regex(text, r'\[ffi\("(?:libcrypto|libssl)"\)\]', "[]", sole, f"{name}: a row that was only ffi")
    text = regex(text, r'ffi\("(?:libcrypto|libssl)"\), ', "", lead, f"{name}: ffi entries followed by others")
    text = regex(text, r', ffi\("(?:libcrypto|libssl)"\)', "", tail, f"{name}: ffi entries at the end of a row")
    text = exact(text, 'ffi: &f Ffi("libcrypto,libssl")', "engine: &!f tls.Engine", typed_both, f"{name}: both-library parameters")
    text = exact(text, 'ffi: &f Ffi("libssl")', "engine: &!f tls.Engine", typed_ssl, f"{name}: libssl parameters")
    return text


def attempt(text):
    # The module that was `ossl` (OpenSSL) is `tlsx`; `tls` is lex-sys's package, named only for `tls.Engine`.
    text = regex(text, r"\bossl\.(?=\w)", "tlsx.", 43, "attempt.ls: calls of the TLS module")
    text = exact(text, "import ossl;\n", "import tls; import tlsx;\n", 1, "attempt.ls: imports") if "import ossl;\n" in text else \
        exact(text, "import std.conns;\n", "import std.conns;\nimport tls; import tlsx;\n", 1, "attempt.ls: imports")
    # `advance` takes the time for the certificates' dates (`tlsx.open`), the other functions only the engine.
    text = exact(text, 'pub fn advance[&f, &t, &p, &a, &r, &s](ffi: &f Ffi("libcrypto,libssl"), tab:',
                 "pub fn advance[&f, &t, &p, &a, &r, &s](engine: &!f tls.Engine, now_ms: int, tab:", 1, "attempt.ls: advance")
    text = exact(text, "tlsx.open(ffi, at[env_at() + e_ctx()], at, tb, ", "tlsx.open(engine, slot, now_ms, at, tb, ", 1, "attempt.ls: open")
    text = exact(text, "tlsx.drop(ffi, at, slot * stride() + f_tls());", "tlsx.drop(engine, slot, at, slot * stride() + f_tls());", 1, "attempt.ls: drop")
    # A session is a pool of the engine's tickets (lex-sys `docs/tls-resumption.md` §12): the attempt's ticket joins its endpoint's pool, if that pool was
    # saved for the same name and port, instead of replacing it, so that a burst to one endpoint finds a ticket for each connection.
    text = exact(text, """    let saved = tlsx.save_session(ffi, at, b + f_tls());
    if saved == 0 {
        return 0;
    }
    drop_session(ffi, at, e);
    let base = slot * slot_bytes();
    at[env_at() + e_sessions() + e] = saved;
    at[env_at() + e_keys() + e] = name_key(req[base + name_at()..base + name_at() + at[b + f_name()]], at[b + f_port()]);
""", """    let base = slot * slot_bytes();
    let key = name_key(req[base + name_at()..base + name_at() + at[b + f_name()]], at[b + f_port()]);
    if at[env_at() + e_keys() + e] != key {
        drop_session(engine, at, e);
    }
    let saved = tlsx.save_session(engine, slot, at, b + f_tls(), at[env_at() + e_sessions() + e]);
    if saved == 0 {
        return 0;
    }
    at[env_at() + e_sessions() + e] = saved;
    at[env_at() + e_keys() + e] = key;
""", 1, "attempt.ls: keep_session")
    # The OpenSSL context is not there; the engine is closed by `main`.
    text = exact(text, "    tlsx.free_context(ffi, at[env_at() + e_ctx()]);\n", "    // (no context to free: `main` closes the engine)\n", 1, "attempt.ls: free_context")
    text = rows_and_types(text, "attempt.ls", sole=7, tail=0, lead=10, typed_both=4, typed_ssl=7)
    text = regex(text, r"\bffi\b", "engine", 23, "attempt.ls: the remaining `ffi` arguments")
    return text


def hooks(text):
    text = regex(text, r"\bossl\.(?=\w)", "tlsx.", 6, "hooks.ls: calls of the TLS module")
    text = exact(text, "import ossl;\n", "import tls; import tlsx;\n", 1, "hooks.ls: imports")
    text = start_tls(text)
    # The TLS functions of the delivery loop.
    text = exact(text, "attempt.advance(ffi, atab, poller,", "attempt.advance(engine, clock_unix_ms(clock), atab, poller,", 1, "hooks.ls: advance")
    text = regex(text, r"\b(attempt\.finish|attempt\.tend|attempt\.sweep_parked|conclude|settle|sweep)\(ffi,", r"\1(engine,", 8, "hooks.ls: calls in the delivery loop")
    text = exact(text, 'ssl: &c Ffi("libcrypto,libssl")', "ssl: &!c tls.Engine", 1, "hooks.ls: run's parameter")
    text = regex(text, r'\[ffi\("(?:libcrypto|libssl)"\)\]', "[]", 0, "hooks.ls: a row that was only ffi")
    text = regex(text, r'ffi\("(?:libcrypto|libssl)"\), ', "", 8, "hooks.ls: ffi entries followed by others")
    text = regex(text, r', ffi\("(?:libcrypto|libssl)"\)', "", 0, "hooks.ls: ffi entries at the end of a row")
    text = exact(text, 'ffi: &f Ffi("libcrypto,libssl")', "engine: &!f tls.Engine", 2, "hooks.ls: both-library parameters")
    text = exact(text, 'ffi: &f Ffi("libssl")', "engine: &!f tls.Engine", 2, "hooks.ls: libssl parameters")
    # `main`: the scope, the engine, the trust store.
    text = exact(text, 'let ssl = narrow(ffi, "libcrypto,libssl");', "release(ffi);", 1, "hooks.ls: no foreign scope")
    text = exact(text, "    release(ssl);\n", "\n", 1, "hooks.ls: nothing to release")
    # A ticket per endpoint at most, as the OpenSSL build keeps a session per endpoint.
    text = exact(text, "borrow mut heap as &!h in {\n", "borrow mut heap as &!h in { var engine = tls.open_with_tickets(h, attempt.slots(), state.max_endpoints());\n", 1, "hooks.ls: the engine is opened")
    text = close_engine(text)
    text = regex(text, r"borrow ssl as &lt in \{\n(\s*)tls_ctx = start_tls\(lt, (.*)\n(\s*)\}",
                 r"borrow mut engine as &!en in {\n\1tls_ctx = tlsx.setup(en, h, evlog.lend(lw), \2\n\3}", 1, "hooks.ls: the trust store")
    text = exact(text, "borrow ssl as &lb in {\n", "borrow mut engine as &!lb in {\n", 1, "hooks.ls: the engine is lent to run")
    text = regex(text, r"borrow ssl as &lt in \{\n\s*tlsx\.free_context\(lt, tls_ctx\);\n\s*\}\n",
                 "// (the engine is closed at the end of its block)\n\n\n", 1, "hooks.ls: the context of a failed listen")
    return text


def start_tls(text):
    """`start_tls` (the OpenSSL context) is not in the pure build: its lines are blank, so the others keep their numbers."""
    m = re.search(r"^fn start_tls\[.*?^}\n", text, re.M | re.S)
    if not m:
        raise Mismatch("hooks.ls: no `fn start_tls`")
    return text[:m.start()] + "\n" * m.group(0).count("\n") + text[m.end():]


def close_engine(text):
    """`tls.close(h, engine)` goes on the line of the brace that ends the block `engine` is opened in."""
    lines = text.split("\n")
    opens = [i for i, l in enumerate(lines) if l.rstrip().endswith("borrow mut heap as &!h in { var engine = tls.open_with_tickets(h, attempt.slots(), state.max_endpoints());")]
    if len(opens) != 1:
        raise Mismatch("hooks.ls: the engine's block")
    i = opens[0]
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, len(lines)):
        if lines[j] == " " * indent + "}":
            lines[j] = " " * indent + "tls.close(h, engine); }"
            return "\n".join(lines)
    raise Mismatch("hooks.ls: the end of the engine's block")


PATCHES = {"attempt.ls": attempt, "hooks.ls": hooks}


def main():
    out = os.path.join(ROOT, sys.argv[1] if len(sys.argv) > 1 else "pure/build/src")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    src = os.path.join(ROOT, "src")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".ls") or name == "ossl.ls":
            continue
        text = open(os.path.join(src, name)).read()
        if name in PATCHES:
            try:
                text = PATCHES[name](text)
            except Mismatch as e:
                raise SystemExit(f"make_pure: src/{name} is no longer what the changes expect:\n  {e}\n"
                                 "  (scripts/make_pure.py lists each change and how many places it must find)")
        elif re.search(r"\bossl\.|^import ossl;", text, re.M):
            raise SystemExit(f"make_pure: src/{name} uses `ossl` (the OpenSSL module) and has no changes listed for it")
        open(os.path.join(out, name), "w").write(text)
    shutil.copy(os.path.join(ROOT, "pure", "src", "tlsx.ls"), os.path.join(out, "tlsx.ls"))
    print(f"{out}: {len(os.listdir(out))} files")


if __name__ == "__main__":
    main()
