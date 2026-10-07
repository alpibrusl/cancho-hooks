#!/bin/bash
# The C compiler driver `cancho build` links with (`CC`), with OpenSSL added to the link line. The project file has no linking options for a program (cancho
# docs/package-system.md: "no linking options in [[bin]]"), so the service, which calls libssl and libcrypto (src/ossl.cho), is built with
#
#   CC=scripts/cc-ssl.sh cancho build
#
# (scripts/build.sh does it). The libraries are the system's: libssl-dev (Debian, Ubuntu) or openssl-devel to build, libssl3 or openssl-libs to run.
exec "${REAL_CC:-cc}" "$@" -lssl -lcrypto
