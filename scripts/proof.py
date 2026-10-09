#!/usr/bin/env python3
"""The report tables on the proof page (docs/proof.html), spliced from the pinned authority reports
(docs/authority*.json) so the page cannot claim a bound the compiler did not derive, and cannot drift
from the reports a person approves by committing (scripts/check-authority.sh --update).

    python3 scripts/proof.py          # splice the tables into docs/proof.html
    python3 scripts/proof.py --check # change nothing; exit 1 if the page is stale

The fragment is everything between the two markers; the rest of the page is written by hand, like
scripts/figures.py writes docs/figures/soak.svg into docs/index.html.
"""
import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "proof.html"
BEGIN, END = "<!-- report:begin -->", "<!-- report:end -->"

# What a derived label means, in the words of the service's own docs. A label the compiler derives but
# this table does not name is a build failure here: the page must explain every capability it reports.
MEANINGS = {
    "args": "the command line: the settings, not a config file",
    "clock": "the clock, for retry schedules and timeouts",
    "conn_accept": "accepting connections on the listening socket",
    "conn_read": "reading from an open connection",
    "conn_write": "writing to an open connection",
    "dir_read": "reading a directory (the data directory, through a handle)",
    "err_write": "standard error",
    "ffi(\"libcrypto\")": "<strong>OpenSSL&rsquo;s libcrypto</strong>: the digest and signature operations of https delivery",
    "ffi(\"libssl\")": "<strong>OpenSSL&rsquo;s libssl</strong>: the TLS of https delivery (the default build)",
    "file_read": "reading a file: the event log, the endpoints, the state",
    "file_write": "writing a file: the event log and its compaction",
    "fs_read(\"\")": "reading a path given at runtime (the data directory)",
    "fs_write(\"\")": "writing a path given at runtime (the data directory)",
    "heap": "the heap",
    "io_write": "standard output: the log",
    "io_read": "standard input",
    "net_in(\"\")": "listening: the bound is shared with connecting and <strong>not narrowed</strong>",
    "net_out(\"\")": "connecting: the endpoints&rsquo; addresses, enforced in code (public addresses only), not proved by the compiler",
    "poll": "the poller: one thread, one loop",
    "signals(\"INT,TERM\")": "graceful stop: INT and TERM are waited for, never a kill",
    "signals_read": "reading a delivered signal",
}


def label_text(label):
    if label["argument"] is None:
        return label["name"]
    return '%s("%s")' % (label["name"], label["argument"])


def load(name):
    return json.loads((ROOT / "docs" / ("%s.json" % name)).read_text())


def row(label):
    text = label_text(label)
    meaning = MEANINGS.get(text)
    if meaning is None:
        sys.exit("proof.py: no meaning written for %s; the page must explain every derived label" % text)
    cls = "good" if label["bounded"] else "bad"
    bounded = "bounded" if label["bounded"] else "UNBOUNDED"
    return ('      <tr><th><code>%s</code></th><td>%s</td><td class="%s">%s</td></tr>'
            % (html.escape(text), meaning, cls, bounded))


def table(name, title, note):
    report = load(name)
    labels = report["labels"]
    return [
        '  <div class="fitwrap"><table class="fit wide">',
        "    <thead><tr><th>%s &middot; <code>docs/%s.json</code></th><td>what it means</td><td>the compiler says</td></tr></thead>"
        % (html.escape(title), name),
        "    <tbody>",
    ] + [row(l) for l in labels] + [
        "    </tbody>",
        "  </table></div>",
        '  <p class="note">%s</p>' % html.escape(note),
    ]


def fragment():
    hooks, pure = load("authority"), load("authority-pure")
    mcp, logcheck = load("authority-mcp"), load("authority-logcheck")
    unbounded = hooks["unbounded_by"]
    stats = [
        '<div><b>0</b><span>capabilities the <code>hooks-pure</code> build does not have: its report is <strong>bounded: true</strong>, no foreign call</span></div>',
        '<div><b>%d</b><span>foreign symbols the default build can call, every one OpenSSL&rsquo;s (libssl, libcrypto), named in its report: that is what makes it <strong>not bounded</strong></span></div>' % len(unbounded),
        '<div><b>%d</b><span>signals that stop it: INT and TERM, waited for gracefully; there is no kill capability</span></div>' % len([l for l in hooks["labels"] if l["name"].startswith("signals")]),
        '<div><b>2</b><span>of the four programs are pure cancho end to end (<code>hooks-pure</code>, <code>hooks-mcp</code>, <code>hooks-logcheck</code>); the default <code>hooks</code> names its OpenSSL</span></div>',
    ]
    parts = ['  <div class="stats">'] + stats + ["  </div>"]
    parts += table("authority", "hooks, the default build (https via OpenSSL)",
                   "The report is what the compiler derives; docs/authority.json is the same report as of the last time a person approved it, and CI fails on any difference. unbounded_by names all %d OpenSSL symbols it can call; each is one line in the committed report." % len(unbounded))
    parts += table("authority-pure", "hooks-pure: the same service with cancho's own TLS",
                   "No ffi capability at all: TLS is cancho's packages/tls (not yet independently reviewed). Everything else the default build does, this build does too - one process, on-disk before the answer.")
    parts += table("authority-mcp", "hooks-mcp: the agent surface (tools/mcp.cho)",
                   "The MCP server agents talk to: reads the log through stdin, answers on stdout, no network capability at all - the agent tool cannot reach anywhere.")
    parts += table("authority-logcheck", "hooks-logcheck: the log reader (tools/logcheck.cho)",
                   "Reads the log, checks it, and for trim cuts a torn tail: file read and write, no network, no foreign call.")
    parts.append('  <p>A change to any of the four reports fails CI before it fails a reader (<code>scripts/check-authority.sh</code>, its <code>--pure</code>, <code>--mcp</code> and <code>--logcheck</code> variants). The approval of a new capability is the commit of its report.')
    return "\n".join(parts) + "\n"


def splice(page, fragment_text):
    i, j = page.index(BEGIN) + len(BEGIN), page.index(END)
    return page[:i] + "\n" + fragment_text + page[j:]


def main():
    page = PAGE.read_text()
    if BEGIN not in page or END not in page:
        sys.exit("proof.py: docs/proof.html lacks the splice markers")
    spliced = splice(page, fragment())
    if "--check" in sys.argv[1:]:
        if spliced != page:
            print("stale: docs/proof.html (report tables)")
            return 1
        print("the proof page's report tables are current")
        return 0
    PAGE.write_text(spliced)
    print("wrote the report tables into docs/proof.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
