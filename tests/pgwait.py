"""`listening` on stderr means the socket is open (the start does not wait for the database: docs/design.md section 37). With a database named, the
endpoints are read from its table a moment later, and `hooks: endpoints loaded: N` says so; until then the routes that need them answer 503 and nothing is
delivered. A harness that wants "ready for requests" waits for that line too."""


def after_listening(proc, lines, flags):
    """Read stderr up to `endpoints loaded` (appending to `lines`) if a database is among `flags`; True if the process ended first."""
    if not any(f == "--pg-host" or f.startswith("--pg-host=") for f in flags):
        return False
    while True:
        line = proc.stderr.readline().decode().strip()
        lines.append(line)
        if line == "":
            return True
        if "endpoints loaded" in line:
            return False
