# lexsys-hooks: the service as a container image.
#
#   docker build -t lexsys-hooks .
#   docker volume create hooks-data
#   docker run -d --name hooks -p 8080:8080 -v hooks-data:/var/lib/hooks lexsys-hooks
#   docker run --rm --entrypoint cat lexsys-hooks /usr/share/hooks/schema.sql | psql ...     # the tables, if you use PostgreSQL
#
# TWO STAGES, and what is pinned in each:
#   compiler  the lex-sys compiler at the commit `lex-sys.toml` names ([package] lex-sys: one place, which `lex-sys build` itself
#             checks), built with the Rust toolchain that commit's rust-toolchain.toml pins, from its Cargo.lock (--locked).
#   build     `lex-sys build` of this repository: it fetches the two libraries lex-sys.toml pins by commit and checks them.
#   runtime   the binary, tini, a non-root user, a volume and a health check. Nothing else: no shell tools beyond the base image's,
#             no compiler, no git.
#
# NOT pinned, and the honest limit of "reproducible" here: the base image is a tag (ubuntu:24.04), not a digest; apt packages are
# whatever the archive has that day (clang 18 in noble: the compiler's LLVM backend shells out to `clang`); rustup-init is a fixed
# version but its checksum comes from the same server. To pin the base: `--build-arg BASE=ubuntu:24.04@sha256:<digest>`.
# The runtime base must be the build base (the binary links the glibc of the image it was built in).
#
# .github/workflows/release.yml builds this image, waits for it to be healthy, posts an event, reads it back and stops it (a dry run on a push that
# touches it; a push of a tag publishes it). See docs/runbook.md "Container" for what was verified by hand and what was not.
ARG BASE=ubuntu:24.04

FROM ${BASE} AS compiler
ARG RUSTUP_VERSION=1.28.2
ENV DEBIAN_FRONTEND=noninteractive RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates clang curl git libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
 && curl -fsSL -o /tmp/rustup-init.sha256 "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init.sha256" \
 && (cd /tmp && echo "$(cut -d' ' -f1 rustup-init.sha256)  rustup-init" | sha256sum -c -) \
 && chmod +x /tmp/rustup-init \
 && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none \
 && rm /tmp/rustup-init /tmp/rustup-init.sha256
# The pin lives in lex-sys.toml and nowhere else.
COPY lex-sys.toml /src/hooks/lex-sys.toml
RUN set -eu; \
    REV=$(sed -n 's/^lex-sys *= *"\([0-9a-f]*\)".*/\1/p' /src/hooks/lex-sys.toml); \
    test -n "$REV"; \
    git clone --quiet https://github.com/alpibrusl/lex-sys /src/lex-sys; \
    git -C /src/lex-sys checkout --quiet "$REV"; \
    CHANNEL=$(sed -n 's/^channel *= *"\(.*\)"/\1/p' /src/lex-sys/rust-toolchain.toml); \
    test -n "$CHANNEL"; \
    rustup toolchain install "$CHANNEL" --profile minimal; \
    cd /src/lex-sys; \
    RUSTUP_TOOLCHAIN="$CHANNEL" cargo build --release --locked -p lex-sys; \
    cp target/release/lex-sys /usr/local/bin/lex-sys; \
    lex-sys --version | tee /lex-sys.version; \
    echo "rust $CHANNEL" >> /lex-sys.version; \
    echo "clang $(clang --version | head -n 1)" >> /lex-sys.version

FROM compiler AS build
WORKDIR /src/hooks
COPY src ./src
COPY tests ./tests
COPY sql ./sql
COPY scripts/cc-ssl.sh ./scripts/cc-ssl.sh
# The binary is normalized as scripts/release.sh does (no symbols, no build-id: the only bytes that differ between two builds of
# the same sources), so the image holds the same bytes as bin/hooks in the release tarball when the toolchain is the same.
# CC adds -lssl -lcrypto to the link (the project file has no linking options for a program): the service calls OpenSSL (src/tls.ls).
RUN CC=/src/hooks/scripts/cc-ssl.sh lex-sys build \
 && test -x build/hooks \
 && ldd build/hooks > /ldd.txt \
 && strip --strip-all --remove-section=.note.gnu.build-id -o /hooks build/hooks \
 && sha256sum /hooks | tee /hooks.sha256

FROM ${BASE} AS runtime
ENV DEBIAN_FRONTEND=noninteractive
# tini: the service is PID 1 in a container, and a PID 1 without a SIGTERM handler ignores SIGTERM (measured here with
# `unshare --pid`: it survives), so `docker stop` would wait ten seconds and SIGKILL. With tini the signal reaches the service,
# which then ends at once, as it does under systemd.
# libssl3 and ca-certificates: the service is linked against OpenSSL and verifies the certificate of an https endpoint against the system's store (or against
# the file `tls-ca-file` names). The same base as the build, so the same libssl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini libssl3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /var/lib/hooks --no-create-home --shell /usr/sbin/nologin hooks \
 && install -d -o hooks -g hooks -m 0700 /var/lib/hooks \
 && install -d -o root -g root -m 0755 /etc/hooks /usr/share/hooks
COPY --from=build /hooks /usr/local/bin/hooks
COPY --from=build /lex-sys.version /ldd.txt /hooks.sha256 /usr/share/hooks/
COPY sql/schema.sql /usr/share/hooks/schema.sql
COPY deploy/hooks.docker.conf /etc/hooks/hooks.conf
COPY deploy/hooks-healthcheck.sh /usr/local/bin/hooks-healthcheck
COPY deploy/hooks-entrypoint.sh /usr/local/bin/hooks-entrypoint
RUN chmod 0755 /usr/local/bin/hooks /usr/local/bin/hooks-healthcheck /usr/local/bin/hooks-entrypoint

LABEL org.opencontainers.image.title="lexsys-hooks" \
      org.opencontainers.image.description="Webhook delivery service written in lex-sys: signed, at least once, retried" \
      org.opencontainers.image.source="https://github.com/alpibrusl/lexsys-hooks" \
      org.opencontainers.image.licenses="EUPL-1.2"

# The data directory: the events log (events.seg, events-N.seg, events.first), delivery.seg, compact.lock and endpoints.conf, mode 0700 and made
# under a umask of 077 (hooks-entrypoint), which is what `production = 1` insists on. The logs are bounded by retention (docs/retention.md):
# events final at every endpoint are dropped after retention-days (default 30), so this volume holds about that many days of events and no
# more. A bind mount must be owned by uid 10001, and be 0700 too if the service is to run in production.
VOLUME /var/lib/hooks
WORKDIR /var/lib/hooks
EXPOSE 8080
USER 10001:10001
# GET /readyz: the logs are open and take a write, a named database has a live connection and its endpoints are read, and the service is not stopping (docs/design.md
# section 34.1). Each of those but the database is something a restart repairs; a database that is away is not, and the service reconnects by itself (section 37), so an
# orchestrator should use this to route traffic and not to restart. `docker stop` sends SIGTERM and waits 10 s by default; the drain takes at most
# stop-deadline-ms (5 s unless set), so the default is enough; with a longer stop-deadline-ms use `docker stop -t`.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 CMD ["/usr/local/bin/hooks-healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/hooks-entrypoint"]
CMD ["--config", "/etc/hooks/hooks.conf"]
