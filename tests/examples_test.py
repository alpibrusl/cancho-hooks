#!/usr/bin/env python3
"""The examples (examples/*.sh) and the page that shows them (docs/examples.html): the page cannot say what the scripts do not.

    pip install standardwebhooks            # the reference library, to check examples/receiver.py against
    HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...] python3 tests/examples_test.py build/hooks
    EXAMPLES=2,3,5 python3 tests/examples_test.py build/hooks      # a subset, for a machine without PostgreSQL (examples 1 and 4 need it)

  1. examples/receiver.py against the reference library: the same signatures, the same refusals (a changed body, a wrong secret, an old timestamp, several
     signatures in one header), and as a server: it answers 204 to a good request, 401 to a bad one, 500 to the first N or to every one when told to, and says
     `duplicate` for a webhook-id it has seen
  2. every script, run against the service: exit status 0 (it exits 0 only when every expectation it prints as `ok:` held)
  3. the page against the run: every block of the page marked `data-example="N"` is lines of what script N printed (a `…` stands for any text, a line of
     only `…` for any lines; the ids and times that change are normalised), in the order the script printed them, and each card has its script and its docs
  5. the pages share one header and one footer (the same markup and the same CSS on index, evidence and examples, the current page marked), every internal link of
     the header resolves, and a change to one page's header that the others do not have is refused
  6. the recordings (examples/casts/*.cast, copied to docs/casts/ for the site): valid asciicast v2; the text of each is what a fresh run of its script prints,
     line for line, with only these values normalised because they genuinely vary from run to run: Unix times, dates, secrets, ports, the milliseconds a script
     measures (`done after N ms`), and the counts that depend on timing (`events_expired`, `segments_expired`, `cron_fired`, and the number of cron fires and
     of the lines about them); the page's block for the example is lines of the cast; each card links its cast and the file is there; a cast with one line
     changed is refused
  4. the checks themselves can fail: a script whose expectation does not hold exits non-zero, a page that says what the script did not print is refused, and
     so is a page whose lines are out of order
"""
import base64
import hashlib
import html
import html.parser
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "build", "hooks")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402
PAGE = os.path.join(ROOT, "docs", "examples.html")
GITHUB = "https://github.com/alpibrusl/cancho-hooks/blob/main/"
NEEDS_PG = {1, 4}
ONLY = {int(x) for x in os.environ["EXAMPLES"].split(",")} if os.environ.get("EXAMPLES") else None


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, name, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
        if not ok:
            self.fails.append(name)
        return ok

    def finish(self, label):
        if self.fails:
            print(f"FAILED {len(self.fails)}: " + "; ".join(self.fails))
            return 1
        print(f"all {label} checks passed")
        return 0


check = Checks()


# ---- the page against the output --------------------------------------------------------------------------------------

def normalise(line):
    """What differs from run to run, made the same: runs of white space, Unix times (ten digits or more), secrets."""
    line = " ".join(line.split())
    line = re.sub(r"\d{10,}", "T", line)
    line = re.sub(r"whsec_[A-Za-z0-9+/=]{16,}", "whsec_S", line)
    return line


def line_pattern(page_line):
    """A regular expression for a line of the page: `…` stands for any text. None for a line that is only `…` (any lines)."""
    if page_line == "…":
        return None
    parts = [re.escape(p) for p in page_line.split("…")]
    return re.compile("".join(p + ("" if i == len(parts) - 1 else ".*") for i, p in enumerate(parts)) + r"\Z", re.S)


class PageParser(html.parser.HTMLParser):
    """The text of every `<pre data-example="N">`, in order, by N; and the `data-example` numbers and links of the page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.links, self.cards = {}, [], []
        self._n, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "pre" and "data-example" in a:
            self._n, self._buf = int(a["data-example"]), []
        if tag == "a" and "href" in a:
            self.links.append(a["href"])
        if tag == "section" and "data-card" in a:
            self.cards.append(int(a["data-card"]))

    def handle_endtag(self, tag):
        if tag == "pre" and self._n is not None:
            self.blocks.setdefault(self._n, []).extend("".join(self._buf).split("\n"))
            self._n = None

    def handle_data(self, data):
        if self._n is not None:
            self._buf.append(data)


def parse_page(text):
    p = PageParser()
    p.feed(text)
    return p


def page_problems(lines, output):
    """The lines of a page block that script output does not hold, in order: a list of (page line, why)."""
    out = [normalise(x) for x in output.split("\n")]
    pos, bad = 0, []
    for raw in lines:
        want = normalise(raw)
        if not want:
            continue
        pat = line_pattern(want)
        if pat is None:
            continue
        i = pos
        while i < len(out) and not pat.match(out[i]):
            i += 1
        if i == len(out):
            seen = any(pat.match(o) for o in out)
            bad.append((raw, "it is in the output, but before an earlier line of the block" if seen else "no such line in the output"))
        else:
            pos = i + 1
    return bad


# ---- 1. the receiver --------------------------------------------------------------------------------------------------

def load_receiver():
    spec = importlib.util.spec_from_file_location("receiver", os.path.join(ROOT, "examples", "receiver.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port():
    return chaos.free_port()   # below the ephemeral range: a port bound to 0 and released can be given to another socket before the receiver has it


def post(port, headers, body, timeout=10):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("POST", "/hook", body=body, headers=headers)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def signed(secret, msg_id, body, when=None):
    from standardwebhooks import Webhook
    ts = datetime.fromtimestamp(when or time.time(), tz=timezone.utc)
    sig = Webhook(secret).sign(msg_id, ts, body.decode())
    return {"webhook-id": msg_id, "webhook-timestamp": str(int(ts.timestamp())), "webhook-signature": sig, "Content-Type": "application/json"}


def test_receiver():
    rcv = load_receiver()
    try:
        from standardwebhooks import Webhook, WebhookVerificationError
    except ImportError:
        check("1. the reference library is installed (pip install standardwebhooks)", False)
        return
    agree = True
    detail = ""
    for i in range(200):
        secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
        other = "whsec_" + base64.b64encode(os.urandom(24)).decode()
        body = json.dumps({"pad": os.urandom(i % 40).hex()}).encode()
        h = signed(secret, f"evt_{i}", body)
        mine = rcv.sign(secret, f"evt_{i}", int(h["webhook-timestamp"]), body)
        try:
            Webhook(secret).verify(body, h)
            lib_ok = True
        except WebhookVerificationError:
            lib_ok = False
        ok_mine = rcv.verify([secret], f"evt_{i}", h["webhook-timestamp"], h["webhook-signature"], body)[0]
        wrong = rcv.verify([other], f"evt_{i}", h["webhook-timestamp"], h["webhook-signature"], body)[0]
        tampered = rcv.verify([secret], f"evt_{i}", h["webhook-timestamp"], h["webhook-signature"], body + b" ")[0]
        # the second signature is made at the SAME second as the first: a fresh time.time() could be the next second, and then it is
        # (rightly) a signature of another timestamp, which the receiver refuses (a flake of this check, CI run 37624285380)
        two = h["webhook-signature"] + " " + signed(other, f"evt_{i}", body, when=int(h["webhook-timestamp"]))["webhook-signature"]
        both = rcv.verify([secret], f"evt_{i}", h["webhook-timestamp"], two, body)[0] and rcv.verify([other], f"evt_{i}", h["webhook-timestamp"], two, body)[0]
        if not (mine == h["webhook-signature"] and lib_ok and ok_mine and not wrong and not tampered and both):
            agree, detail = False, f"i={i} mine={mine} lib={h['webhook-signature']} {lib_ok} {ok_mine} {wrong} {tampered} {both}"
            break
    check("1. 200 secrets and bodies: the receiver's signature is the library's, and it verifies, refuses a wrong secret and a changed body, and takes either of two signatures", agree, detail)
    old = signed("whsec_" + base64.b64encode(b"k" * 24).decode(), "evt_1", b"{}", when=time.time() - 3600)
    check("1. a timestamp an hour old is refused, as the library refuses it",
          not rcv.verify(["whsec_" + base64.b64encode(b"k" * 24).decode()], "evt_1", old["webhook-timestamp"], old["webhook-signature"], b"{}")[0])

    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()
    tmp = tempfile.mkdtemp(prefix="examples-receiver-")
    try:
        results = {}
        for label, extra, requests in (
            ("fail 2", ["--fail", "2"], 4),
            ("fail always", ["--fail", "always"], 2),
            ("normal", [], 2),
        ):
            port = free_port()
            proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "examples", "receiver.py"), "--port", str(port), "--name", "r", "--secret", secret] + extra,
                                    stdout=subprocess.PIPE, text=True)
            try:
                proc.stdout.readline()  # listening
                body = b'{"type":"t"}'
                # The receiver answers a request and then prints its line, on a thread of its own for each connection: a request sent as soon as the answer has come may have its line
                # printed before the line of the one before. So, where the order of the lines is what is checked, the line of a request is read before the next is sent.
                statuses, lines = [], []
                for _ in range(requests):
                    statuses.append(post(port, signed(secret, "evt_7", body), body))
                    if label == "normal":
                        lines.append(proc.stdout.readline().strip())
                results[label] = statuses
                if label == "normal":
                    bad = post(port, dict(signed(secret, "evt_8", body), **{"webhook-signature": "v1," + base64.b64encode(b"x" * 32).decode()}), body)
                    results["bad signature"] = bad
                    lines.append(proc.stdout.readline().strip())
                    results["lines"] = lines
            finally:
                proc.terminate()
                proc.wait(5)
        check("1. as a server: 500 to the first N when told so and then 204, 500 to every request with `--fail always`, 204 to a good one",
              results["fail 2"] == [500, 500, 204, 204] and results["fail always"] == [500, 500] and results["normal"] == [204, 204], str(results))
        check("1. a bad signature is a 401, and the line it prints says so", results["bad signature"] == 401 and "SIGNATURE INVALID" in results["lines"][2], str(results))
        check("1. a webhook-id it has seen is printed as a duplicate", results["lines"][0].endswith("{\"type\":\"t\"}") and results["lines"][1].endswith("(1 signature) duplicate"), str(results["lines"]))
        port = free_port()
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "examples", "receiver.py"), "--port", str(port), "--name", "r", "--secret", secret, "--delay", "0.6"], stdout=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()
            body = b"{}"
            t0 = time.time()
            post(port, signed(secret, "evt_9", body), body)
            check("1. with `--delay` it answers late", time.time() - t0 >= 0.55)
        finally:
            proc.terminate()
            proc.wait(5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



# ---- 5. the shared header and footer ----------------------------------------------------------------------------------

DOCS = os.path.join(ROOT, "docs")
PAGES = {"index.html": "Overview", "examples.html": "Examples", "evidence.html": "Evidence"}


def shared_parts(text):
    """The header, the footer and the CSS block every page shares, as text; the current-page marker taken out."""
    def between(a, b):
        i, j = text.index(a), text.index(b) + len(b)
        return text[i:j]
    parts = {"header": between("<!-- shared:header:begin -->", "<!-- shared:header:end -->"),
             "footer": between("<!-- shared:footer:begin -->", "<!-- shared:footer:end -->"),
             "css": between("/* shared:begin", "/* shared:end */")}
    return {k: v.replace(' aria-current="page"', "") for k, v in parts.items()}


def shared_differences(texts):
    """What the pages do not share: a list of (name of the part, name of the page that differs from the first)."""
    names = sorted(texts)
    base = shared_parts(texts[names[0]])
    bad = []
    for n in names[1:]:
        other = shared_parts(texts[n])
        bad += [(k, n) for k in base if base[k] != other[k]]
    return bad


def check_shared_chrome():
    texts = {n: open(os.path.join(DOCS, n), encoding="utf-8").read() for n in PAGES}
    check("5. index, evidence and examples have the same header, the same footer and the same CSS for them", not shared_differences(texts), str(shared_differences(texts)))
    for n, label in PAGES.items():
        head = shared_parts_raw(texts[n], "header")
        current = re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]*)</a>', head)
        check(f"5. {n} marks one link as the current page, {label}", current == [label], str(current))
    head = shared_parts(texts["index.html"])["header"]
    want = ["Overview", "Examples", "Evidence", "Install", "GitHub"]
    check("5. the header says Overview, Examples, Evidence, Install and GitHub", re.findall(r"<a [^>]*>(?:<img[^>]*>)?([^<]+)</a>", head)[1:] == want, head)
    ids = {n: set(re.findall(r'\bid="([^"]+)"', t)) for n, t in texts.items()}
    bad = []
    for href in re.findall(r'href="([^"]+)"', head + shared_parts(texts["index.html"])["footer"]):
        if href.startswith("http"):
            continue
        page, _, frag = href.partition("#")
        page = page if page not in ("", "./") else "index.html"
        if not os.path.exists(os.path.join(DOCS, page)) or (frag and frag not in ids.get(page, set())):
            bad.append(href)
    check("5. every internal link of the header and the footer is a page of the site, and its anchor is on it", not bad, str(bad))
    # the check can fail: one page's header changed, or its footer, or the CSS
    for part, edit in (("header", ("Examples</a>", "Samples</a>")), ("footer", ("Releases</a>", "Release notes</a>")), ("css", ("min-height: 3.4rem", "min-height: 4rem"))):
        mutated = dict(texts)
        mutated["evidence.html"] = mutated["evidence.html"].replace(*edit)
        check(f"5. a change to the {part} of one page only is refused (mutation)", (part, "index.html") in shared_differences(mutated) or any(k == part for k, _ in shared_differences(mutated)),
              str(shared_differences(mutated)))


def shared_parts_raw(text, which):
    return text[text.index(f"<!-- shared:{which}:begin -->"):text.index(f"<!-- shared:{which}:end -->")]


# ---- 6. the recordings ------------------------------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
VARIES = [  # (what, pattern, replacement): the values of a run that are not the same in the next one
    ("a date and time", re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"), "DATETIME"),
    ("the milliseconds a script measured", re.compile(r"done after \d+ ms"), "done after N ms"),
    ("a port", re.compile(r'"port":\d+'), '"port":P'),
    ("counts that depend on timing", re.compile(r'"(events_expired|segments_expired|cron_fired)":\d+'), r'"\1":N'),
    ("the id of the schedule is the same, its fires are not counted", re.compile(r"^\s*reports evt_\d+ signature ok \(1 signature\) \{\"type\":\"report.due\".*$"), "reports FIRE"),
]


def cast_events(path):
    """The events of an asciicast v2 file, after checking it is one: a header object with version 2, then [number, "o", text] lines in time order."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    assert lines[-1] == "" and len(lines) > 2, "a header and events, each line ended"
    head = json.loads(lines[0])
    assert head["version"] == 2 and head["width"] > 0 and head["height"] > 0, head
    events, last = [], 0.0
    for ln in lines[1:-1]:
        t, kind, data = json.loads(ln)
        assert isinstance(t, (int, float)) and t >= last and kind == "o" and isinstance(data, str), ln
        events.append((t, data))
        last = t
    return head, events


def cast_text(events):
    return ANSI.sub("", "".join(d for _, d in events)).replace("\r\n", "\n")


def loose_lines(text):
    """Lines of text as they are compared between a recording and a fresh run: normalised, the values that vary made alike, the cron fires counted once."""
    out = []
    for raw in text.split("\n"):
        line = normalise(raw).replace("whsec_…", "whsec_S")
        for _, pat, rep in VARIES:
            line = pat.sub(rep, line)
        if line == "reports FIRE" and out and out[-1] == "reports FIRE":
            continue  # the number of fires in the time the script waited is not fixed
        out.append(line)
    return out


def cast_matches(cast, fresh):
    a, b = loose_lines(cast), loose_lines(fresh)
    if a == b:
        return None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"line {i + 1}: recorded {x!r}, run {y!r}"
    return f"the recording has {len(a)} lines, the run {len(b)}"


def check_casts(outputs, page, have):
    cdir, ddir = os.path.join(ROOT, "examples", "casts"), os.path.join(DOCS, "casts")
    for n in sorted(have):
        name = os.path.basename(have[n])[:-3] + ".cast"
        path = os.path.join(cdir, name)
        try:
            head, events = cast_events(path)
            valid, why = True, ""
        except (OSError, AssertionError, ValueError, KeyError) as e:
            valid, why, events, head = False, str(e), [], {}
        check(f"6. {name} is valid asciicast v2 ({len(events)} events, {os.path.getsize(path) if os.path.exists(path) else 0} bytes)", valid and head.get("idle_time_limit") == 1.5, why)
        check(f"6. {name} is also in docs/casts, byte for byte (the site serves docs/)", os.path.exists(os.path.join(ddir, name)) and open(path, "rb").read() == open(os.path.join(ddir, name), "rb").read())
        check(f"6. card {n} links {name} to play and to download", f'data-cast="casts/{name}"' in open(PAGE, encoding="utf-8").read() and f'href="casts/{name}"' in open(PAGE, encoding="utf-8").read())
        if not valid:
            continue
        text = cast_text(events)
        if n in outputs:
            diff = cast_matches(text, outputs[n])
            check(f"6. the text of {name} is what a fresh run of the script printed (the values that vary normalised)", diff is None, diff or "")
            bad = page_problems(page.blocks.get(n, []), text)
            check(f"6. the page's block for example {n} is lines of the recording, in order", not bad, "\n".join(f"  {why}: {line}" for line, why in bad[:6]))
            lines = text.split("\n")
            k = next(i for i, x in enumerate(lines) if x.startswith("ok:"))
            changed = "\n".join(lines[:k] + ["ok: something the script never said"] + lines[k + 1:])
            check(f"6. a recording with one line changed is refused ({name})", cast_matches(changed, outputs[n]) is not None)
            check(f"6. a recording with a line missing is refused ({name})", cast_matches("\n".join(lines[:k] + lines[k + 1:]), outputs[n]) is not None)


# ---- 2. the scripts ---------------------------------------------------------------------------------------------------

def scripts():
    d = os.path.join(ROOT, "examples")
    found = sorted(f for f in os.listdir(d) if re.match(r"\d\d-.*\.sh$", f))
    return {int(f[:2]): os.path.join(d, f) for f in found}


def run_script(path, extra_env=None, timeout=180):
    env = dict(os.environ, HOOKS=BIN, HOOKS_MCP=os.path.join(os.path.dirname(BIN), "hooks-mcp"))
    env.update(extra_env or {})
    t0 = time.time()
    p = subprocess.run(["bash", path], capture_output=True, text=True, env=env, timeout=timeout, cwd=ROOT)
    return p.returncode, p.stdout + p.stderr, time.time() - t0


def main():
    test_receiver()

    have = scripts()
    check("2. there are seven scripts, 01 to 07", sorted(have) == list(range(1, 8)), str(sorted(have)))
    runnable = {n: p for n, p in have.items() if (ONLY is None or n in ONLY) and (n not in NEEDS_PG or os.environ.get("HOOKS_PG"))}
    skipped = sorted(set(have) - set(runnable))
    if skipped:
        print(f"note: not run here: {skipped} (HOOKS_PG is not set, or EXAMPLES names others); the page's blocks for them are not checked")
    outputs = {}
    for n, path in sorted(runnable.items()):
        rc, out, secs = run_script(path)
        outputs[n] = out
        oks = len(re.findall(r"^ok: ", out, re.M))
        check(f"2. {os.path.basename(path)} exits 0 ({oks} expectations held, {secs:.1f} s)", rc == 0 and oks > 0 and "EXPECTATION FAILED" not in out, f"exit {rc}\n{out[-1500:]}")

    # ---- 3. the page
    with open(PAGE, encoding="utf-8") as f:
        text = f.read()
    page = parse_page(text)
    check("3. every script has a card on the page, and every block on the page belongs to a script",
          sorted(page.cards) == sorted(have) and set(page.blocks) <= set(have) and set(page.blocks) == set(have), f"cards {page.cards}, blocks {sorted(page.blocks)}")
    for n in sorted(have):
        name = os.path.basename(have[n])
        check(f"3. card {n} links to its script on GitHub ({name})", GITHUB + "examples/" + name in page.links)
    for href in page.links:
        if href.startswith(GITHUB):
            rel = href[len(GITHUB):].split("#")[0]
            check(f"3. the link to {rel} points at a file of the repository", os.path.exists(os.path.join(ROOT, rel)), href)
    for n in sorted(outputs):
        bad = page_problems(page.blocks.get(n, []), outputs[n])
        lines = len([x for x in page.blocks.get(n, []) if x.strip()])
        check(f"3. the page's block for example {n} ({lines} lines) is what the script printed, in order", not bad, "\n".join(f"  {why}: {line}" for line, why in bad[:6]))

    # ---- 4. the checks can fail
    if outputs:
        n = sorted(outputs)[0]
        lines = page.blocks[n]
        mutated = outputs[n].replace("[202]", "[200]", 1) if "[202]" in outputs[n] else outputs[n].replace("ok: ", "no: ", 1)
        check(f"4. a script whose output changed (example {n}: one status) is no longer what the page says", bool(page_problems(lines, mutated)))
        shuffled = list(reversed([x for x in lines if x.strip() and x.strip() != "…"]))
        check(f"4. the page's lines in another order are refused (example {n})", len(shuffled) > 1 and bool(page_problems(shuffled, outputs[n])))
        invented = lines + ["ok: a claim the script never made"]
        check(f"4. a line the script did not print is refused (example {n})", bool(page_problems(invented, outputs[n])))
        check("4. the wildcard `…` matches inside a line, and normalisation makes times alike",
              not page_problems(['{"a":…,"t":T}', "…", "x…"], '{"a":1,"t":1791000000123}\nxyz'))
    if 3 in runnable:
        # an expectation that does not hold: a copy of the script with the schedule cut to two delays, so that the events die after three attempts, not four
        tmp = tempfile.mkdtemp(prefix="examples-mutant-")
        try:
            src = open(runnable[3], encoding="utf-8").read()
            changed = src.replace("--schedule 200,300,400", "--schedule 200,300")
            assert changed != src
            os.makedirs(os.path.join(tmp, "examples"))
            for f in (x for x in os.listdir(os.path.join(ROOT, "examples")) if os.path.isfile(os.path.join(ROOT, "examples", x))):
                shutil.copy(os.path.join(ROOT, "examples", f), os.path.join(tmp, "examples", f))
            os.makedirs(os.path.join(tmp, "sql"))
            with open(os.path.join(tmp, "examples", os.path.basename(runnable[3])), "w", encoding="utf-8") as f:
                f.write(changed)
            rc, out, _ = run_script(os.path.join(tmp, "examples", os.path.basename(runnable[3])))
            check("4. a script whose expectation does not hold (3 attempts where it says 4) exits non-zero and says which", rc != 0 and "EXPECTATION FAILED" in out, f"exit {rc}\n{out[-600:]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- 5. one header and one footer
    check_shared_chrome()

    # ---- 6. the recordings
    check_casts(outputs, page, have)

    # ---- the page's own form
    check("3. the page names its canonical address", '<link rel="canonical" href="https://alpibrusl.github.io/cancho-hooks/examples.html">' in text)
    check("3. the page needs nothing from outside (no external script, stylesheet, image or font; the player is inline and fetches only the page's own casts)",
          not re.search(r"<script[^>]*\ssrc=", text) and not re.search(r'(src|href)="https?://[^"]*\.(css|js|png|jpg|woff2?)"', text) and "@import" not in text and "fetch(\"http" not in text)
    return check.finish("examples")


if __name__ == "__main__":
    sys.exit(main())
