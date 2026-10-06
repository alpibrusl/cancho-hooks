#!/bin/bash
# The text of a release page, on standard output.
#
#   scripts/release-notes.sh <tag> <prerelease: 0|1>
#
# It says what the files are, how to check them, and what the release does not claim. It states nothing about a number or a test that is not
# in the repository's own documents, which it links.
set -euo pipefail
tag=${1:?usage: release-notes.sh <tag> <0|1>}
pre=${2:-1}
version=${tag#v}
repo=alpibrusl/lexsys-hooks

if [ "$pre" = 1 ]; then
cat <<'TEXT'
**Alpha.** The API may still change, and nothing here is certified for production: the 24-hour chaos soak on a release candidate and an independent review of lex-sys's own TLS decide when that changes (what is and is not done: [docs/status.md](https://github.com/alpibrusl/lexsys-hooks/blob/main/docs/status.md)). Open source under the EUPL-1.2, provided as is, without warranty: you run it at your own responsibility.

TEXT
fi

cat <<TEXT
## What is here

| file | |
|---|---|
| \`hooks-${version}-linux-x86_64.tar.gz\`, \`hooks-${version}-linux-aarch64.tar.gz\` | the service (\`bin/hooks\`), the MCP server for agents (\`bin/hooks-mcp\`, [docs/agents.md](https://github.com/${repo}/blob/main/docs/agents.md)), the unit file, backup and restore scripts, the SQL schema, the documents, the Dockerfile |
| \`*.sbom.json\` | a listing of what the build used, read from the tools. It is not CycloneDX or SPDX and no scanner has checked it |
| \`SHA256SUMS\` | the checksum of each file above |

The binary is linked against the system's OpenSSL (\`libssl3\` on Debian and Ubuntu) for \`https\` delivery; it is for Linux with a glibc as new as Ubuntu 24.04's.

## Check what you downloaded

\`\`\`sh
sha256sum -c SHA256SUMS --ignore-missing
gh attestation verify hooks-${version}-linux-x86_64.tar.gz --repo ${repo}
\`\`\`

The second command checks a build-provenance attestation, made by this repository's release workflow, that the file was built there from the tagged commit. It does not say the code is free of bugs or that anyone has audited it.

## Run it

\`\`\`sh
tar -xzf hooks-${version}-linux-x86_64.tar.gz && cd hooks-${version}-linux-x86_64
mkdir data && echo "0 127.0.0.1 9100 whsec_\$(head -c24 /dev/urandom | base64)" > data/endpoints.conf
bin/hooks --port 8080 --dir data --allow-private-hosts 1 &
curl -d '{"type":"user.created"}' localhost:8080/events      # {"id":1}
\`\`\`

\`README.md\` in the tarball has the rest; \`docs/runbook.md\` says how to run it for real (a systemd unit is in \`deploy/\`).

## As a container

\`\`\`sh
docker run -d --name hooks -p 8080:8080 -v hooks-data:/var/lib/hooks ghcr.io/${repo}:${version}
\`\`\`

For x86-64 only. The image is built by the same workflow from the same commit.

## Not claimed

* Reproducibility was measured on one OS and architecture (the same Ubuntu toolchain gives the same bytes after the normalisation \`release.sh\` applies: docs/runbook.md, "Releases"). A different toolchain or architecture is not claimed to give the same bytes, and the container's base image is a tag, not a digest.
* No build for macOS or Windows, and no build of \`hooks-pure\` (the variant with lex-sys's own TLS and no foreign function; [docs/pure-tls.md](https://github.com/${repo}/blob/main/docs/pure-tls.md)): it is built from source.
TEXT
