#!/usr/bin/env python3
"""The settings (docs/design.md section 20): where they come from, which wins, and what is refused.

    python3 tests/config_test.py build/hooks

  1. every setting from a file, and the same five from flags, end up the same (read back from `GET /config`, and the data
     directory is the one named)
  2. the later source wins: defaults < file < flags, flags in the order written, `--config` wherever it stands, a second
     `--config` replaces the first; a setting the flags do not name keeps the file's value
  3. what is not set is the default (and the endpoints file in the data directory is not a setting: its secret is nowhere in
     `GET /config`)
  4. each way to be refused: the service exits 2 before it listens and before it writes anything to the data directory, and
     says which argument, or which line of which file, is wrong
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAILS = []
SECRET = "whsec_c2VjcmV0c2VjcmV0c2VjcmV0"


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def settings(**kw):
    base = {"schedule": [5000, 300000, 1800000, 7200000, 18000000, 36000000, 50400000, 72000000, 86400000],
            "deadline-ms": 2000, "window-ms": 86400000, "allow-private-hosts": 0, "breaker-days": 5, "production": 0, "cron-catchup": 1, "cron-seconds": 0,
            "stop-deadline-ms": 5000, "repair-logs": 0, "rotation-grace-ms": 86400000,
            "retention-days": 30, "segment-bytes": 67108864, "delivery-log-bytes": 33554432, "idem-keys": 262144,
            "pg-backoff-min-ms": 100, "pg-backoff-max-ms": 5000, "pg-attempt-ms": 5000, "pg-request-ms": 10000, "pg-start-wait-ms": 30000,
            "retry-jitter": 10, "endpoint-concurrency": 8, "endpoint-rate": 0}
    base.update(kw)
    return base


def seen(work):
    return sorted(os.listdir(work))


def launch(args, work):
    """Start the service, wait for its first line of stderr, and read `GET /config` if it is listening."""
    proc = subprocess.Popen([BIN, *args], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd=work)
    line = proc.stderr.readline().decode().strip()
    cfg = None
    if line == "listening":
        port = int(args[args.index("--port") + 1]) if "--port" in args else None
        if port is None:
            conf = open(args[args.index("--config") + 1]).read()
            port = int(next(l.split("=")[1] for l in conf.splitlines() if l.startswith("port")))
        cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=5).read())
    proc.kill()
    proc.wait()
    return line, cfg


def stage1():
    want = settings(schedule=[100, 200, 400], **{"deadline-ms": 700, "window-ms": 60000})
    p = chaos.free_port()
    work = tempfile.mkdtemp(prefix="hooks-config-")
    os.makedirs(os.path.join(work, "data"))
    conf = os.path.join(work, "hooks.conf")
    with open(conf, "w") as f:
        f.write(f"# the service\nport = {p}\ndir = {work}/data\nschedule = 100,200,400\ndeadline-ms = 700\nwindow-ms = 60000\n")
    line, cfg = launch(["--config", conf], work)
    check("1. a file sets all five settings", line == "listening" and cfg == want, f"{line!r} {cfg}")
    check("1. ... and the data directory is the one it names", "events.seg" in os.listdir(os.path.join(work, "data")),
          str(os.listdir(os.path.join(work, "data"))))
    shutil.rmtree(work)

    p = chaos.free_port()
    work = tempfile.mkdtemp(prefix="hooks-config-")
    line, cfg = launch(["--port", str(p), "--dir", work, "--schedule", "100,200,400", "--deadline-ms=700",
                        "--window-ms", "60000"], work)
    check("1. the same five as flags, `--key value` and `--key=value` alike", cfg == want, f"{line!r} {cfg}")
    check("1. ... in the directory they name", "events.seg" in os.listdir(work), str(os.listdir(work)))
    shutil.rmtree(work)


def serve(args, conf=None, extra_files=None):
    """Run with a port and directory of its own appended; the config file (if any) is `hooks.conf` in the directory."""
    p = chaos.free_port()
    work = tempfile.mkdtemp(prefix="hooks-config-")
    if conf is not None:
        with open(os.path.join(work, "hooks.conf"), "w") as f:
            f.write(conf.replace("@PORT", str(p)).replace("@DIR", work))
    for name, text in (extra_files or {}).items():
        with open(os.path.join(work, name), "w") as f:
            f.write(text)
    full = [a.replace("@", work) for a in args]
    full += ["--port", str(p), "--dir", work]
    proc = subprocess.Popen([BIN, *full], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd=work)
    line = proc.stderr.readline().decode().strip()
    cfg = None
    if line == "listening":
        cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{p}/config", timeout=5).read())
        proc.kill()
        proc.wait()
        code = None
    else:
        code = proc.wait(timeout=10)
    return code, line, cfg, work


def stage2():
    f = "window-ms = 1000\ndeadline-ms = 300\nschedule = 50,60\n"
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], f)
    shutil.rmtree(w)
    check("2. the file alone", cfg == settings(schedule=[50, 60], **{"deadline-ms": 300, "window-ms": 1000}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf", "--window-ms", "2000"], f)
    shutil.rmtree(w)
    check("2. a flag beats the file, and the file's other settings stay",
          cfg == settings(schedule=[50, 60], **{"deadline-ms": 300, "window-ms": 2000}), str(cfg))
    _, _, cfg, w = serve(["--window-ms", "2000", "--config", "@/hooks.conf"], f)
    shutil.rmtree(w)
    check("2. ... wherever `--config` stands among the flags",
          cfg == settings(schedule=[50, 60], **{"deadline-ms": 300, "window-ms": 2000}), str(cfg))
    _, _, cfg, w = serve(["--window-ms", "2000", "--window-ms=3000"], None)
    shutil.rmtree(w)
    check("2. the later of two flags wins", cfg == settings(**{"window-ms": 3000}), str(cfg))
    work = tempfile.mkdtemp(prefix="hooks-config-")
    other = os.path.join(work, "other.conf")
    with open(other, "w") as fh:
        fh.write("window-ms = 7000\n")
    _, _, cfg, w = serve(["--config", "@/hooks.conf", "--config", other], f)
    shutil.rmtree(w)
    shutil.rmtree(work)
    check("2. a second `--config` replaces the first (not both: the first's deadline is gone)",
          cfg == settings(**{"window-ms": 7000}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "window-ms = 1\nwindow-ms = 9\n")
    shutil.rmtree(w)
    check("2. the later of two lines wins", cfg == settings(**{"window-ms": 9}), str(cfg))


def stage3():
    _, _, cfg, w = serve([], None, {"endpoints.conf": f"0 8.8.8.8 9 {SECRET}\n"})
    body = json.dumps(cfg)
    shutil.rmtree(w)
    check("3. nothing else set: the defaults", cfg == settings(), str(cfg))
    check("3. the endpoints file is not a setting: its secret and host are not in /config",
          SECRET not in body and "8.8.8.8" not in body, body)
    _, _, cfg, w = serve(["--allow-private-hosts", "1"], None, {"endpoints.conf": f"0 127.0.0.1 9 {SECRET}\n"})
    shutil.rmtree(w)
    check("3. allow-private-hosts 1 is a setting, read back", cfg == settings(**{"allow-private-hosts": 1}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "port = @PORT\ndir = @DIR\nallow-private-hosts = 1\n")
    shutil.rmtree(w)
    check("3. ... from a file too", cfg == settings(**{"allow-private-hosts": 1}), str(cfg))
    _, _, cfg, w = serve(["--allow-private-hosts", "1", "--breaker-days", "0"], None, {"endpoints.conf": f"0 127.0.0.1 9 {SECRET}\n"})
    shutil.rmtree(w)
    check("3. breaker-days 0 (off) is a setting, read back", cfg == settings(**{"breaker-days": 0, "allow-private-hosts": 1}), str(cfg))
    _, _, cfg, w = serve(["--allow-private-hosts", "1", "--breaker-days", "2"], None, {"endpoints.conf": f"0 127.0.0.1 9 {SECRET}\n"})
    shutil.rmtree(w)
    check("3. breaker-days 2 from a flag, read back", cfg == settings(**{"breaker-days": 2, "allow-private-hosts": 1}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "port = @PORT\ndir = @DIR\nbreaker-days = 9\n")
    shutil.rmtree(w)
    check("3. breaker-days 9 from a file, read back", cfg == settings(**{"breaker-days": 9}), str(cfg))
    _, _, cfg, w = serve(["--cron-catchup", "0", "--cron-seconds", "1"], None, {})
    shutil.rmtree(w)
    check("3. cron-catchup 0 and cron-seconds 1 are settings, read back", cfg == settings(**{"cron-catchup": 0, "cron-seconds": 1}), str(cfg))
    _, _, cfg, w = serve(["--stop-deadline-ms", "1500", "--repair-logs", "1"], None, {})
    shutil.rmtree(w)
    check("3. stop-deadline-ms and repair-logs are settings, read back", cfg == settings(**{"stop-deadline-ms": 1500, "repair-logs": 1}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "port = @PORT\ndir = @DIR\nstop-deadline-ms = 0\n")
    shutil.rmtree(w)
    check("3. stop-deadline-ms 0 (do not wait) is a setting, from a file too", cfg == settings(**{"stop-deadline-ms": 0}), str(cfg))
    _, _, cfg, w = serve(["--rotation-grace-ms", "3600000"], None, {})
    shutil.rmtree(w)
    check("3. rotation-grace-ms is a setting, read back", cfg == settings(**{"rotation-grace-ms": 3600000}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "port = @PORT\ndir = @DIR\nrotation-grace-ms = 2592000000\n")
    shutil.rmtree(w)
    check("3. rotation-grace-ms 30 days (the most) from a file, read back", cfg == settings(**{"rotation-grace-ms": 2592000000}), str(cfg))
    # the settings of the database's connections (docs/design.md section 37): by flag, by file, the file's beaten by the flag, 0 where 0 means "never"
    _, _, cfg, w = serve(["--pg-backoff-min-ms", "20", "--pg-backoff-max-ms=800", "--pg-attempt-ms", "1500", "--pg-request-ms", "4000", "--pg-start-wait-ms", "9000"], None, {})
    shutil.rmtree(w)
    check("3. the five settings of the database's connections, as flags, read back",
          cfg == settings(**{"pg-backoff-min-ms": 20, "pg-backoff-max-ms": 800, "pg-attempt-ms": 1500, "pg-request-ms": 4000, "pg-start-wait-ms": 9000}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf", "--pg-request-ms", "0"], "port = @PORT\ndir = @DIR\npg-backoff-min-ms = 50\npg-backoff-max-ms = 60\npg-attempt-ms = 7\npg-request-ms = 4000\npg-start-wait-ms = 0\n", {})
    shutil.rmtree(w)
    check("3. ... from a file too, and a flag beats the file (pg-request-ms 0: never; pg-start-wait-ms 0: for ever)",
          cfg == settings(**{"pg-backoff-min-ms": 50, "pg-backoff-max-ms": 60, "pg-attempt-ms": 7, "pg-request-ms": 0, "pg-start-wait-ms": 0}), str(cfg))
    # the settings of the operating, credentials and per-endpoint work sit in one table of integers: every one given at once must come back as given
    _, _, cfg, w = serve(["--config", "@/hooks.conf", "--stop-deadline-ms", "1500", "--ingest-token", "ingest-token-1", "--rotation-grace-ms", "7200000",
                          "--repair-logs", "1", "--cron-seconds", "1", "--breaker-days", "3"], "port = @PORT\ndir = @DIR\nadmin-token = admin-token-1\nrotation-grace-ms = 1000\n", {})
    shutil.rmtree(w)
    check("3. every side's settings given together come back as given (the flag over the file)",
          cfg == settings(**{"stop-deadline-ms": 1500, "repair-logs": 1, "rotation-grace-ms": 7200000, "cron-seconds": 1, "breaker-days": 3}), str(cfg))
    _, _, cfg, w = serve(["--retention-days", "0", "--segment-bytes", "262144", "--delivery-log-bytes=65536", "--idem-keys", "1000"], None, {})
    shutil.rmtree(w)
    check("3. retention-days, segment-bytes, delivery-log-bytes and idem-keys are settings, from flags",
          cfg == settings(**{"retention-days": 0, "segment-bytes": 262144, "delivery-log-bytes": 65536, "idem-keys": 1000}), str(cfg))
    _, _, cfg, w = serve(["--config", "@/hooks.conf"], "port = @PORT\ndir = @DIR\nretention-days = 7\nsegment-bytes = 1048576\nidem-keys = 5000\n")
    shutil.rmtree(w)
    check("3. ... and from a file", cfg == settings(**{"retention-days": 7, "segment-bytes": 1048576, "idem-keys": 5000}), str(cfg))
    code, line, _, w = serve(["--allow-private-hosts", "0"], None, {"endpoints.conf": f"0 127.0.0.1 9 {SECRET}\n"})
    shutil.rmtree(w)
    check("3. a private host in the file with the default is refused before the service listens (status 13)", code == 13 and line != "listening", str((code, line)))


def refused(name, args, files=None, expect=(), absent=()):
    p = chaos.free_port()
    work = tempfile.mkdtemp(prefix="hooks-config-")
    for fname, text in (files or {}).items():
        with open(os.path.join(work, fname), "w") as fh:
            fh.write(text)
    full = [a.replace("@PORT", str(p)).replace("@", work) for a in args]
    proc = subprocess.run([BIN, *full], capture_output=True, timeout=10, cwd=work)
    err = proc.stderr.decode()
    ok = proc.returncode == 2 and "listening" not in err and all(e in err for e in expect)
    listing = sorted(x for x in os.listdir(work) if x not in (files or {}))
    check(f"4. {name}: exit 2, says why, writes nothing",
          ok and not listing and not any(a in err for a in absent), f"exit {proc.returncode} {err!r} wrote {listing}")
    shutil.rmtree(work)


def stage4():
    refused("a positional argument (the old form)", ["8080", "@/data"], expect=["`8080` is not a flag"])
    refused("an unknown flag", ["--port", "@PORT", "--dir", "@", "--prot", "1"], expect=["`--prot` is not a setting"])
    refused("a flag with no value", ["--dir", "@", "--port"], expect=["`--port` needs a value"])
    refused("a bad port", ["--port", "70000", "--dir", "@"], expect=["`--port` has a value"])
    refused("a bad window", ["--port", "@PORT", "--dir", "@", "--window-ms=-1"], expect=["`--window-ms=-1` has a value"])
    refused("no port", ["--dir", "@"], expect=["--port and --dir are required"])
    refused("no directory", ["--port", "@PORT"], expect=["--port and --dir are required"])
    refused("a file that is not there", ["--config", "@/nope.conf"], expect=["nope.conf", "cannot be read"])
    refused("a file with an unknown setting, with its line",
            ["--config", "@/hooks.conf"], {"hooks.conf": "port = 1\n\nprot = 2\n"}, expect=["hooks.conf", "line 3", "none of"])
    refused("a file with a bad value, with its line",
            ["--config", "@/hooks.conf"], {"hooks.conf": "# c\nwindow-ms = soon\n"}, expect=["line 2", "value"])
    refused("a file line that is not key = value", ["--config", "@/hooks.conf"], {"hooks.conf": "port\n"},
            expect=["line 1", "key = value"])
    refused("a file of 16 KiB or more", ["--config", "@/hooks.conf"], {"hooks.conf": "# " + "x" * 16384 + "\n"},
            expect=["16 KiB"])
    refused("cron-catchup that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--cron-catchup", "2"], expect=["`--cron-catchup` has a value"])
    refused("cron-seconds that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--cron-seconds", "yes"], expect=["`--cron-seconds` has a value"])
    refused("allow-private-hosts that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--allow-private-hosts", "yes"], expect=["`--allow-private-hosts` has a value"])
    refused("stop-deadline-ms that is not a number", ["--port", "@PORT", "--dir", "@", "--stop-deadline-ms", "soon"], expect=["`--stop-deadline-ms` has a value"])
    refused("stop-deadline-ms over an hour", ["--port", "@PORT", "--dir", "@", "--stop-deadline-ms", "3600001"], expect=["`--stop-deadline-ms` has a value"])
    refused("repair-logs that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--repair-logs", "yes"], expect=["`--repair-logs` has a value"])
    refused("rotation-grace-ms that is 0", ["--port", "@PORT", "--dir", "@", "--rotation-grace-ms", "0"], expect=["`--rotation-grace-ms` has a value"])
    refused("rotation-grace-ms over 30 days", ["--port", "@PORT", "--dir", "@", "--rotation-grace-ms", "2592000001"], expect=["`--rotation-grace-ms` has a value"])
    refused("rotation-grace-ms that is not a number", ["--port", "@PORT", "--dir", "@", "--rotation-grace-ms", "a day"], expect=["`--rotation-grace-ms` has a value"])
    refused("pg-backoff-min-ms that is 0", ["--port", "@PORT", "--dir", "@", "--pg-backoff-min-ms", "0"], expect=["`--pg-backoff-min-ms` has a value"])
    refused("pg-backoff-max-ms over an hour", ["--port", "@PORT", "--dir", "@", "--pg-backoff-max-ms", "3600001"], expect=["`--pg-backoff-max-ms` has a value"])
    refused("pg-backoff-max-ms below pg-backoff-min-ms", ["--port", "@PORT", "--dir", "@", "--pg-backoff-min-ms", "500", "--pg-backoff-max-ms", "499"], expect=["pg-backoff-max-ms is below pg-backoff-min-ms"])
    refused("pg-backoff-max-ms alone below the default first wait", ["--port", "@PORT", "--dir", "@", "--pg-backoff-max-ms", "50"], expect=["pg-backoff-max-ms is below pg-backoff-min-ms"])
    refused("pg-attempt-ms that is 0", ["--port", "@PORT", "--dir", "@", "--pg-attempt-ms", "0"], expect=["`--pg-attempt-ms` has a value"])
    refused("pg-request-ms that is not a number", ["--port", "@PORT", "--dir", "@", "--pg-request-ms", "never"], expect=["`--pg-request-ms` has a value"])
    refused("pg-request-ms over an hour", ["--port", "@PORT", "--dir", "@", "--pg-request-ms", "3600001"], expect=["`--pg-request-ms` has a value"])
    refused("pg-start-wait-ms over a day", ["--port", "@PORT", "--dir", "@", "--pg-start-wait-ms", "86400001"], expect=["`--pg-start-wait-ms` has a value"])
    refused("breaker-days that is not a number of days", ["--port", "@PORT", "--dir", "@", "--breaker-days", "soon"], expect=["`--breaker-days` has a value"])
    refused("breaker-days over 100 years", ["--port", "@PORT", "--dir", "@", "--breaker-days=36501"], expect=["`--breaker-days=36501` has a value"])
    refused("ingest-token that is too short", ["--port", "@PORT", "--dir", "@", "--ingest-token", "short"], expect=["`--ingest-token` has a value"])
    refused("read-token with a space in it", ["--port", "@PORT", "--dir", "@", "--read-token", "has a space"], expect=["`--read-token` has a value"])
    refused("production that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--production", "yes"], expect=["`--production` has a value"])
    refused("retention-days that is not a number of days", ["--port", "@PORT", "--dir", "@", "--retention-days", "month"], expect=["`--retention-days` has a value"])
    refused("retention-days over 100 years", ["--port", "@PORT", "--dir", "@", "--retention-days=36501"], expect=["`--retention-days=36501` has a value"])
    refused("a segment smaller than 256 KiB", ["--port", "@PORT", "--dir", "@", "--segment-bytes", "1000"], expect=["`--segment-bytes` has a value"])
    refused("a delivery log limit under 64 KiB", ["--port", "@PORT", "--dir", "@", "--delivery-log-bytes", "1000"], expect=["`--delivery-log-bytes` has a value"])
    refused("idem-keys under 16", ["--port", "@PORT", "--dir", "@", "--idem-keys", "3"], expect=["`--idem-keys` has a value"])
    refused("idem-keys over 4,194,304", ["--port", "@PORT", "--dir", "@", "--idem-keys", "4194305"], expect=["`--idem-keys` has a value"])
    refused("compact-now that is not 0 or 1", ["--port", "@PORT", "--dir", "@", "--compact-now", "2"], expect=["`--compact-now` has a value"])
    refused("compact-kill-at over 64", ["--port", "@PORT", "--dir", "@", "--compact-kill-at", "65"], expect=["`--compact-kill-at` has a value"])
    refused("a bad flag after a good file", ["--config", "@/hooks.conf", "--port", "0"],
            {"hooks.conf": "dir = /tmp\nport = 5\n"}, expect=["`--port` has a value"])


for stage in (stage1, stage2, stage3, stage4):
    stage()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all config checks passed")
sys.exit(1 if FAILS else 0)
