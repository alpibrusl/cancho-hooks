#!/usr/bin/env python3
"""`production = 1`: a service that refuses to start unless it is safe on the internet (docs/design.md section 33).

    [HOOKS_PG=host:port:user:database [HOOKS_PG_PASSWORD=...]] python3 tests/production_test.py build/hooks

Every unsafe combination is one test, with its own exit status and a message that names the setting (or the path):

    30 admin-token is not set          31 ingest-token is not set          32 allow-private-hosts is 1
    33 the data directory or one of its files can be read or written by its group or by others
    34 two of the three tokens are the same            35 the mode of the directory (or of a file that opens) cannot be read

  1. each refusal: the status, the message, that nothing was created in the directory (a refusal comes before the logs are opened) and that
     nothing listens; the statuses are all different
  2. the modes: the directory and each of events.seg, delivery.seg and endpoints.conf, with a bit for the group or for others to read or write
     (0640, 0604, 0660, 0602, 0644, 0666, directories 0750, 0705, 0770, 0777) and the ones that pass (0600, 0400, 0700, 0710, 0701, 0711: a bit
     to search a directory is neither); a directory that is not there
  3. the first start under a umask of 022: the logs the service creates are 0644, and that is a refusal (33) naming the file; so is the next
  4. when all is safe it starts: with the three tokens, with two (the read routes then need the admin token), from a settings file, with
     `production = 0` and everything unsafe (the profile is off), and with a database named (HOOKS_PG) -- production does not need one
  5. several things wrong are said one at a time, in a fixed order (30, 31, 32, 34, then the modes)
  6. the status of a refusal is not the status of a bad setting (2) nor of the other starts that end (10 to 20)
  7. the one call into libc going wrong (tests/statx_shim.c, preloaded): refused by the kernel, or answered with no mode filled in, for a log or for
     the directory: each a refusal (35) naming the path, never a pass; and without `production = 1` the call is never made
"""
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/hooks"
FAILS = []
A, I, R = "admin-token-0001", "ingest-token-0002", "read-token-00003"


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def make_dir(mode=0o700):
    d = tempfile.mkdtemp(prefix="production-")
    os.chmod(d, mode)
    return d


def run(d, flags, umask=0o077, env=None):
    """Start the service. Answers (exit status or None if it is still running, stderr, the process)."""
    port = chaos.free_port()
    old = os.umask(umask)
    try:
        proc = subprocess.Popen([BIN, "--port", str(port), "--dir", d, *flags], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                env=dict(os.environ, **env) if env else None)
    finally:
        os.umask(old)
    watchdog = threading.Timer(15, proc.kill)  # a service that neither listens nor exits is a failure, not a hung test
    watchdog.daemon = True
    watchdog.start()
    deadline = time.time() + 10
    lines = []
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        line = proc.stderr.readline().decode()
        lines.append(line)
        if line.strip() == "listening":
            break
    if proc.poll() is None:
        watchdog.cancel()
        return None, "".join(lines), proc, port
    watchdog.cancel()
    lines.append(proc.stderr.read().decode())
    return proc.returncode, "".join(lines), proc, port


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
    proc.wait()
    proc.stderr.close()


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


SAFE = ["--production", "1", "--admin-token", A, "--ingest-token", I]


def refused(name, d, flags, status, mentions, umask=0o077):
    code, err, proc, port = run(d, flags, umask)
    stop(proc)
    check(f"1. {name}: exit {status}", code == status, f"{code} {err!r}")
    check(f"1. {name}: the message says production = 1 and names {mentions!r}", "production = 1 refuses to start" in err and mentions in err, err)
    return code, err


def stage_refusals():
    seen = {}
    d = make_dir()
    code, _ = refused("no admin-token", d, ["--production", "1", "--ingest-token", I], 30, "admin-token")
    seen[30] = code
    check("1. a refusal on a settings fault creates nothing in the directory", os.listdir(d) == [], str(os.listdir(d)))
    shutil.rmtree(d)

    d = make_dir()
    code, _ = refused("no ingest-token", d, ["--production", "1", "--admin-token", A], 31, "ingest-token")
    seen[31] = code
    check("1. ... nor does this one", os.listdir(d) == [], str(os.listdir(d)))
    shutil.rmtree(d)

    d = make_dir()
    code, _ = refused("allow-private-hosts = 1", d, [*SAFE, "--allow-private-hosts", "1"], 32, "allow-private-hosts")
    seen[32] = code
    shutil.rmtree(d)

    d = make_dir(0o755)
    code, err = refused("a data directory of 0755", d, SAFE, 33, d)
    seen[33] = code
    check("1. ... and it was refused before any log was made", os.listdir(d) == [], str(os.listdir(d)))
    shutil.rmtree(d)

    d = make_dir()
    code, _ = refused("admin-token = ingest-token", d, ["--production", "1", "--admin-token", A, "--ingest-token", A], 34, "ingest-token")
    seen[34] = code
    shutil.rmtree(d)
    d = make_dir()
    refused("admin-token = read-token", d, [*SAFE, "--read-token", A], 34, "read-token")
    shutil.rmtree(d)
    d = make_dir()
    refused("ingest-token = read-token", d, [*SAFE, "--read-token", I], 34, "read-token")
    shutil.rmtree(d)

    base = tempfile.mkdtemp(prefix="production-")
    gone = os.path.join(base, "not-there")
    code, _ = refused("a data directory that is not there", gone, SAFE, 35, gone)
    seen[35] = code
    shutil.rmtree(base)

    check("1. six causes, six different statuses", len(set(seen.values())) == len(seen) == 6, str(seen))
    check("6. none of them is 0, 2 (a bad setting), 3, or a start that failed for another reason (10 to 20)", all(c not in (0, 2, 3) and not 10 <= c <= 20 for c in seen.values()), str(seen))


def stage_modes():
    for m in [0o750, 0o740, 0o705, 0o704, 0o770, 0o707, 0o777, 0o755, 0o775, 0o702, 0o720, 0o760]:
        d = make_dir(m)
        code, err, proc, _ = run(d, SAFE)
        stop(proc)
        check(f"2. a data directory of {m:04o} is refused (33) and named", code == 33 and d in err, f"{code} {err!r}")
        os.chmod(d, 0o700)
        shutil.rmtree(d)
    for m in [0o700, 0o710, 0o701, 0o711, 0o500]:
        d = make_dir(m)
        code, err, proc, _ = run(d, SAFE)
        stop(proc)
        # 0500 and the like: the owner cannot write, so the logs cannot be made; that is a failed start (10), not a mode refusal
        ok = (code is None) if m != 0o500 else (code in (10, None))
        check(f"2. a data directory of {m:04o} is not refused for its mode", ok and code not in (33, 35), f"{code} {err!r}")
        os.chmod(d, 0o700)
        shutil.rmtree(d)
    for name in ("events.seg", "delivery.seg", "endpoints.conf"):
        for m in [0o640, 0o604, 0o660, 0o602, 0o644, 0o666, 0o620, 0o606, 0o664]:
            d = make_dir()
            path = os.path.join(d, name)
            open(path, "wb").close()
            os.chmod(path, m)
            code, err, proc, _ = run(d, SAFE)
            stop(proc)
            check(f"2. {name} of {m:04o} is refused (33) and named", code == 33 and path in err, f"{code} {err!r}")
            shutil.rmtree(d)
        for m in [0o600, 0o400, 0o700, 0o000]:
            d = make_dir()
            path = os.path.join(d, name)
            with open(path, "wb"):
                pass
            os.chmod(path, m)
            code, err, proc, _ = run(d, SAFE)
            stop(proc)
            # an unreadable log is a failed start of its own (10, 12), an unreadable endpoints.conf is not read: never the mode refusal
            check(f"2. {name} of {m:04o} is not refused for its mode", code not in (33, 35), f"{code} {err!r}")
            shutil.rmtree(d)
    # a symbolic link to a good directory is judged by what it points to
    base = tempfile.mkdtemp(prefix="production-")
    good, bad = os.path.join(base, "good"), os.path.join(base, "bad")
    os.mkdir(good, 0o700)
    os.mkdir(bad, 0o755)
    os.symlink(good, os.path.join(base, "to-good"))
    os.symlink(bad, os.path.join(base, "to-bad"))
    code, err, proc, _ = run(os.path.join(base, "to-good"), SAFE)
    stop(proc)
    check("2. a symbolic link to a private directory is judged by its target (starts)", code is None, f"{code} {err!r}")
    code, err, proc, _ = run(os.path.join(base, "to-bad"), SAFE)
    stop(proc)
    check("2. a symbolic link to an open directory is refused", code == 33, f"{code} {err!r}")
    os.chmod(bad, 0o700)
    shutil.rmtree(base)


def stage_umask():
    d = make_dir()
    code, err, proc, _ = run(d, SAFE, umask=0o022)
    stop(proc)
    check("3. the first start under a umask of 022 makes logs of 0644 and is refused (33), naming events.seg", code == 33 and os.path.join(d, "events.seg") in err, f"{code} {err!r}")
    modes = {n: stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode) for n in os.listdir(d)}
    check("3. ... the logs are there, 0644", modes.get("events.seg") == 0o644, str(modes))
    code, err, proc, _ = run(d, SAFE, umask=0o077)
    stop(proc)
    check("3. the next start, with the umask right, is refused too: the files stay as they are", code == 33 and "events.seg" in err, f"{code} {err!r}")
    for n in os.listdir(d):
        os.chmod(os.path.join(d, n), 0o600)
    code, err, proc, _ = run(d, SAFE, umask=0o077)
    stop(proc)
    check("3. after chmod 600 it starts", code is None, f"{code} {err!r}")
    shutil.rmtree(d)
    d = make_dir()
    code, err, proc, port = run(d, SAFE, umask=0o077)
    check("3. under a umask of 077 the first start in an empty directory starts", code is None, f"{code} {err!r}")
    modes = {n: stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode) for n in os.listdir(d)}
    check("3. ... and the logs are 0600", set(modes.values()) == {0o600} and len(modes) == 2, str(modes))
    stop(proc)
    shutil.rmtree(d)


SHIM = os.path.join(os.path.dirname(BIN), "statx_shim.so")


def stage_statx_fails():
    """The ways the one call into libc can go wrong, which a file's mode cannot make happen: tests/statx_shim.c does."""
    if not os.path.exists(SHIM):
        print(f"skip 7. the statx shim ({SHIM}) is not built: scripts/build.sh builds it")
        return
    for how, what in [("fail", "the kernel refuses (EACCES)"), ("nomask", "the kernel answers and says it filled in no mode")]:
        d = make_dir()
        for name in ("events.seg", "delivery.seg"):
            path = os.path.join(d, name)
            open(path, "wb").close()
            os.chmod(path, 0o600)
        env = {"LD_PRELOAD": SHIM, "STATX_SHIM": how}
        code, err, proc, _ = run(d, SAFE, env=env)
        stop(proc)
        check(f"7. a log whose mode cannot be read ({what}) is a refusal (35) naming it, not a pass", code == 35 and os.path.join(d, "events.seg") in err and "mode cannot be read" in err, f"{code} {err!r}")
        shutil.rmtree(d)
    d = make_dir()
    code, err, proc, _ = run(d, SAFE, env={"LD_PRELOAD": SHIM, "STATX_SHIM": "failall"})
    stop(proc)
    check("7. a data directory whose mode cannot be read is a refusal (35) naming it", code == 35 and d in err and "mode cannot be read" in err, f"{code} {err!r}")
    shutil.rmtree(d)
    # the shim passes everything else to the real call: with it loaded and nothing to fail, a safe start is a start
    d = make_dir()
    code, err, proc, _ = run(d, SAFE, env={"LD_PRELOAD": SHIM, "STATX_SHIM": "none"})
    stop(proc)
    check("7. (control) with the shim loaded and asked for nothing, a safe start starts", code is None, f"{code} {err!r}")
    shutil.rmtree(d)
    # without the profile the call is never made: the same shim, nothing refused
    d = make_dir()
    code, err, proc, _ = run(d, [], env={"LD_PRELOAD": SHIM, "STATX_SHIM": "failall"})
    stop(proc)
    check("7. without production = 1 the mode is never asked for (failall changes nothing)", code is None, f"{code} {err!r}")
    shutil.rmtree(d)


def get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def stage_starts():
    d = make_dir()
    code, err, proc, port = run(d, SAFE)
    check("4. all safe: it starts", code is None and err.strip() == "listening", f"{code} {err!r}")
    if code is None:
        check("4. ... and listens", listening(port))
        st, body = get(port, "/healthz")
        check("4. ... /healthz is open", st == 200, f"{st}")
        st, body = get(port, "/config")
        check("4. ... /config needs a token (read falls back to admin) and says production is on", st == 401, f"{st} {body}")
        st, body = get(port, "/config", A)
        check("4. ... /config with the admin token", st == 200 and '"production":1' in body, f"{st} {body}")
        st, body = get(port, "/config", I)
        check("4. ... the ingest token cannot read", st == 403, f"{st} {body}")
    stop(proc)
    shutil.rmtree(d)

    d = make_dir()
    code, err, proc, port = run(d, [*SAFE, "--read-token", R])
    check("4. with a read token as well it starts", code is None, f"{code} {err!r}")
    if code is None:
        st, _ = get(port, "/stats", R)
        check("4. ... and the read token reads", st == 200, str(st))
        st, _ = get(port, "/stats", A)
        check("4. ... and the admin token reads", st == 200, str(st))
        st, _ = get(port, "/stats", I)
        check("4. ... and the ingest token does not", st == 403, str(st))
    stop(proc)
    shutil.rmtree(d)

    d = make_dir()
    conf = d + ".conf"
    with open(conf, "w") as f:
        f.write(f"production = 1\nadmin-token = {A}\ningest-token = {I}\n")
    code, err, proc, port = run(d, ["--config", conf])
    check("4. the same settings from a file start", code is None, f"{code} {err!r}")
    stop(proc)
    # the flags win over the file, so `--production 0` turns the profile off even where the file asks for it
    code, err, proc, port = run(d, ["--config", conf, "--production", "0", "--allow-private-hosts", "1"])
    check("4. --production 0 over a file that says 1: the profile is off", code is None, f"{code} {err!r}")
    stop(proc)
    os.remove(conf)
    shutil.rmtree(d)

    # the sample file of deploy/, with the lines of its production profile uncommented, is a profile that starts (the flags win over its `dir`)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = open(os.path.join(root, "deploy", "hooks.conf.example")).read()
    lines = []
    for line in sample.splitlines():
        m = re.match(r"^#(admin-token|ingest-token|read-token|production|allow-private-hosts) =", line)
        lines.append(line[1:] if m else line)
    d = make_dir()
    conf = d + ".conf"
    with open(conf, "w") as f:
        f.write("\n".join(lines) + "\n")
    code, err, proc, port = run(d, ["--config", conf])
    check("4. the sample settings file with its production lines uncommented starts (the lines are valid and the tokens differ)", code is None, f"{code} {err!r}")
    if code is None:
        st, body = get(port, "/config", "change-me-to-a-third-long-random-string")
        check("4. ... with the read token from the sample, in production", st == 200 and '"production":1' in body, f"{st} {body}")
    stop(proc)
    os.remove(conf)
    shutil.rmtree(d)

    d = make_dir(0o755)
    code, err, proc, port = run(d, ["--allow-private-hosts", "1"])
    check("4. without production, nothing of this applies: open directory, no tokens, private hosts", code is None, f"{code} {err!r}")
    if code is None:
        st, body = get(port, "/config")
        check("4. ... and /config says production is off", st == 200 and '"production":0' in body, f"{st} {body}")
    stop(proc)
    os.chmod(d, 0o700)
    shutil.rmtree(d)
    d = make_dir(0o755)
    code, err, proc, port = run(d, ["--production", "0"])
    check("4. --production 0 is the same as leaving it out", code is None, f"{code} {err!r}")
    stop(proc)
    os.chmod(d, 0o700)
    shutil.rmtree(d)

    pg = os.environ.get("HOOKS_PG")
    if pg:
        host, port_, user, db = pg.split(":")
        flags = ["--pg-host", host, "--pg-port", port_, "--pg-user", user, "--pg-database", db]
        if os.environ.get("HOOKS_PG_PASSWORD"):
            flags += ["--pg-password", os.environ["HOOKS_PG_PASSWORD"]]
        env = dict(os.environ, PGPASSWORD=os.environ.get("HOOKS_PG_PASSWORD", ""))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(["psql", "-q", "-h", host, "-p", port_, "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-f", os.path.join(root, "sql", "schema.sql")],
                       check=True, capture_output=True, env=env)
        d = make_dir()
        code, err, proc, port = run(d, [*SAFE, *flags])
        check("4. with a database named it starts", code is None, f"{code} {err!r}")
        stop(proc)
        shutil.rmtree(d)
    else:
        print("skip 4. with a database named (HOOKS_PG is not set)")


def stage_order():
    d = make_dir(0o755)
    # everything wrong at once: the settings first, in a fixed order, then the modes
    flags = ["--production", "1", "--allow-private-hosts", "1"]
    code, err, proc, _ = run(d, flags)
    stop(proc)
    check("5. nothing set, private hosts on, directory open: the admin token is said first (30)", code == 30, f"{code} {err!r}")
    code, err, proc, _ = run(d, [*flags, "--admin-token", A])
    stop(proc)
    check("5. ... then the ingest token (31)", code == 31, f"{code} {err!r}")
    code, err, proc, _ = run(d, [*flags, "--admin-token", A, "--ingest-token", I])
    stop(proc)
    check("5. ... then private hosts (32)", code == 32, f"{code} {err!r}")
    code, err, proc, _ = run(d, ["--production", "1", "--admin-token", A, "--ingest-token", A])
    stop(proc)
    check("5. ... then the repeated token (34) before the directory", code == 34, f"{code} {err!r}")
    code, err, proc, _ = run(d, SAFE)
    stop(proc)
    check("5. ... and last the directory (33)", code == 33, f"{code} {err!r}")
    os.chmod(d, 0o700)
    shutil.rmtree(d)


def main():
    stage_refusals()
    stage_modes()
    stage_umask()
    stage_starts()
    stage_order()
    stage_statx_fails()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:10]))
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
