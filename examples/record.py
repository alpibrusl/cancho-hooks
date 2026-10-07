#!/usr/bin/env python3
"""Record the examples as terminal recordings (asciicast v2, https://docs.asciinema.org/manual/asciicast/v2/).

    python3 examples/record.py [--out examples/casts] [--hooks build/hooks] [01 03 ...]

Each example script is run for real, against the service, in a pseudo-terminal, and everything it writes is kept with the time it was written
(a `[seconds, "o", text]` line). Play one with `asciinema play examples/casts/02-payments.cast`, or on docs/examples.html.

What is edited, and nothing else:
  * a gap between two pieces of output longer than 1.5 s is shortened to 1.5 s (the scripts wait for retries, a 6 s slow receiver, a fire every 2 s: a
    recording that sits still is not watchable). The header says so (`idle_time_limit`, and the title); every other time is the real one, so a
    script that takes 6 s of work shows the work, not the waiting
  * `whsec_` secrets are shortened to `whsec_…`, as the page's transcripts show them (they are throwaway secrets of that run)
  * colours: the scripts print none and the terminal is `TERM=dumb`; any escape sequence is stripped at record time, so a cast is plain text
The text is not invented or reordered: tests/examples_test.py runs every script again and requires the cast's text to be what that run printed.

The casts are written to examples/casts/ and copied to docs/casts/ (the site serves docs/, and the page plays them from there; the test requires the
two to be identical). `--out DIR` writes to DIR only.

Needs what the scripts need (bash, curl, python3; HOOKS_PG for examples 1 and 4, see the script).
"""
import json
import os
import pty
import re
import select
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "examples", "casts")
IDLE = 1.5
WIDTH, HEIGHT = 120, 30
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]|\x1b[=>]")
SECRET = re.compile(r"whsec_[A-Za-z0-9+/=]{16,}")
TITLES = {
    "01": "A SaaS sends its customers webhooks",
    "02": "Payments that must not be lost or doubled",
    "03": "A customer's endpoint is down",
    "04": "A report every Monday, with no scheduler",
    "05": "One event, several internal systems",
    "06": "Erase and expire personal data",
    "07": "Let an agent look",
}


def clean(text):
    return SECRET.sub("whsec_…", ANSI.sub("", text))


def run_in_pty(path, env):
    """Run the script in a pty; the list of (seconds since the start, text) it wrote, in order."""
    master, slave = pty.openpty()
    t0 = time.time()
    proc = subprocess.Popen(["bash", path], stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, cwd=ROOT, close_fds=True)
    os.close(slave)
    events, pending = [], b""
    while True:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            pending += data
            try:  # keep a multi-byte character whole
                text = pending.decode("utf-8")
                pending = b""
            except UnicodeDecodeError:
                continue
            events.append((time.time() - t0, text))
        elif proc.poll() is not None:
            break
    rc = proc.wait()
    os.close(master)
    return rc, events


def to_cast(events, title):
    """asciicast v2 text: a header line and an `o` event for each piece of output, with idle gaps capped."""
    header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": int(time.time()), "idle_time_limit": IDLE,
              "title": title + f" (recorded from a real run; waits longer than {IDLE} s are shortened to {IDLE} s)",
              "env": {"SHELL": "/bin/bash", "TERM": "dumb"}}
    lines, shown, prev = [json.dumps(header, ensure_ascii=False)], 0.0, 0.0
    for t, text in events:
        text = clean(text)
        if not text:
            continue
        shown += min(max(t - prev, 0.0), IDLE)
        prev = t
        lines.append(json.dumps([round(shown, 3), "o", text], ensure_ascii=False))
    return "\n".join(lines) + "\n"


def main(argv):
    out, hooks, only = DEFAULT_OUT, os.path.join(ROOT, "build", "hooks"), []
    it = iter(argv)
    for a in it:
        if a == "--out":
            out = next(it)
        elif a == "--hooks":
            hooks = os.path.abspath(next(it))
        else:
            only.append(a)
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ, HOOKS=hooks, TERM="dumb")
    env.setdefault("HOOKS_MCP", os.path.join(os.path.dirname(hooks), "hooks-mcp"))
    names = sorted(f for f in os.listdir(os.path.join(ROOT, "examples")) if re.match(r"\d\d-.*\.sh$", f))
    status = 0
    for f in names:
        if only and f[:2] not in only:
            continue
        rc, events = run_in_pty(os.path.join(ROOT, "examples", f), env)
        if rc != 0:
            print(f"{f}: exit {rc}: not recorded", file=sys.stderr)
            status = 1
            continue
        cast = to_cast(events, TITLES.get(f[:2], f))
        dest = os.path.join(out, f[:-3] + ".cast")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(cast)
        if out == DEFAULT_OUT:
            os.makedirs(os.path.join(ROOT, "docs", "casts"), exist_ok=True)
            with open(os.path.join(ROOT, "docs", "casts", f[:-3] + ".cast"), "w", encoding="utf-8") as fh:
                fh.write(cast)
        print(f"{dest}: {len(events)} writes, {events[-1][0]:.1f} s real, {len(cast)} bytes")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
