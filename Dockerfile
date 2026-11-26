# cancho-hooks: the service as a container image.
#
#   docker build -t cancho-hooks .
#   docker volume create hooks-data
#   docker run -d --name hooks -p 8080:8080 -v hooks-data:/var/lib/hooks cancho-hooks
#   docker run --rm --entrypoint cat cancho-hooks /usr/share/hooks/schema.sql | psql ...     # the tables, if you use PostgreSQL
#
# TWO STAGES, and what is pinned in each:
#   compiler  the cancho compiler at the release `cancho.toml` names ([package] cancho: one place, which `cancho build` itself
#             checks), installed prebuilt by scripts/install.sh.
#   build     `cancho build` of this repository: it fetches the libraries cancho.toml pins by commit and checks them.
#   runtime   the binary, tini, a non-root user, a volume and a health check. Nothing else: no shell tools beyond the base image's,
#             no compiler, no git.
#
# NOT pinned, and the honest limit of "reproducible" here: the base image is a tag (ubuntu:24.04), not a digest; apt packages are
# whatever the archive has that day (clang 18 in noble: the compiler's LLVM backend shells out to `clang`); the compiler is the prebuilt release
# cancho.toml names, fetched by scripts/install.sh over https. To pin the base: `--build-arg BASE=ubuntu:24.04@sha256:<digest>`.
# The runtime base must be the build base (the binary links the glibc of the image it was built in).
#
# .github/workflows/release.yml builds this image, waits for it to be healthy, posts an event, reads it back and stops it (a dry run on a push that
# touches it; a push of a tag publishes it). See docs/runbook.md "Container" for what was verified by hand and what was not.
ARG BASE=ubuntu:24.04

FROM ${BASE} AS compiler
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates clang curl git libssl-dev \
 && rm -rf /var/lib/apt/lists/*
# The pin lives in cancho.toml and nowhere else.
COPY cancho.toml /src/hooks/cancho.toml
RUN set -eu; \
    REV=$(sed -n 's/^cancho *= *"\([0-9a-f.]*\)".*/\1/p' /src/hooks/cancho.toml); \
    test -n "$REV"; \
    curl -fsSL https://raw.githubusercontent.com/alpibrusl/cancho/main/scripts/install.sh | sh -s "$REV" /usr/local; \
    cancho --version | tee /cancho.version; \
    echo "clang $(clang --version | head -n 1)" >> /cancho.version

FROM compiler AS build
WORKDIR /src/hooks
COPY src ./src
COPY tests ./tests
COPY tools ./tools
COPY sql ./sql
# The binary is normalized as scripts/release.sh does (no symbols, no build-id: the only bytes that differ between two builds of
# the same sources), so the image holds the same bytes as bin/hooks in the release tarball when the toolchain is the same.
# `-l ssl -l crypto` is the link of OpenSSL, which the service calls (src/ossl.cho). The standalone compiler reads no `CC` and project mode takes no `-l`, so the
# bin is built from its files (its sources, then the libraries `cancho install` writes to build/deps), as scripts/build.sh does.
RUN cancho install \
 && mkdir -p build \
 && cancho build $(find src -name '*.cho' | LC_ALL=C sort) $(find build/deps -maxdepth 1 -name '*.cho' | LC_ALL=C sort) --std -l ssl -l crypto -o build/hooks \
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
COPY --from=build /cancho.version /ldd.txt /hooks.sha256 /usr/share/hooks/
COPY sql/schema.sql /usr/share/hooks/schema.sql
COPY deploy/hooks.docker.conf /etc/hooks/hooks.conf
COPY deploy/hooks-healthcheck.sh /usr/local/bin/hooks-healthcheck
COPY deploy/hooks-entrypoint.sh /usr/local/bin/hooks-entrypoint
RUN chmod 0755 /usr/local/bin/hooks /usr/local/bin/hooks-healthcheck /usr/local/bin/hooks-entrypoint

LABEL org.opencontainers.image.title="cancho-hooks" \
      org.opencontainers.image.description="Webhook delivery service written in cancho: signed, at least once, retried" \
      org.opencontainers.image.source="https://github.com/alpibrusl/cancho-hooks" \
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
