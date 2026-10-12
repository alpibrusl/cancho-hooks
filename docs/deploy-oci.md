# Deploying cancho-hooks with cancho-oci: image, signature, SBOM, and a machine that verifies all three

A guide for one small machine (the demo box: a Hetzner CX or similar), from a released binary to a
running, verified service, using the project's own image tool. Nothing here is required to run
cancho-hooks — the [runbook](runbook.md) covers the plain-tarball path with the same systemd unit —
but this path has a property no other stack has: the image was assembled by a tool whose authority
report has no network, the SBOM it attaches carries the *authority report of the binary inside*, the
signature is checked on the machine before anything runs, and every one of those documents is
verified, not trusted.

## What plays which part

| Part | Tool | What it adds |
|---|---|---|
| The image | `oci-build` (cancho-oci) | a `FROM scratch` OCI image from the static binary, byte-for-byte reproducible |
| The evidence | `oci-sbom`, `oci-sign`, `oci-ref` | a CycloneDX 1.5 SBOM carrying the binary's authority report, an Ed25519 signature over the manifest digest, both attached to the image in the registry |
| The registry | any OCI registry (ghcr.io, or a `crane registry serve` on the box itself) | storage and the referrers API |
| The machine's check | `oci-pull`, `oci-sign verify`, `sha256sum` | every document re-hashed, the signature checked offline before the unit starts |
| The running part | systemd, with the unit from `deploy/hooks.service` | sandboxing that matches the report: the enforcement layer around the compiler's boundary |

**Honest limits, said up front.** cancho-oci is alpha: `oci-push`/`oci-pull` are tested against mock
registries with injected faults, `crane registry serve` and `registry:2`, and pulled once from
`ghcr.io` ([cancho-oci docs/design.md](https://github.com/alpibrusl/cancho-oci/blob/main/docs/design.md)
says exactly what that does and does not show). There is no `cosign` interoperability: the signature
is verified by `oci-sign verify`, not by `cosign` (cancho-oci design 10 records the maintainer's
decision, 2026-10-08: Ed25519, no cosign claim). The gzip encoder is deterministic but has no dynamic
Huffman code yet, so layers are about 1.2x what `gzip -9` makes. None of these matter for a demo box;
all of them are reasons this is a guide and not a production procedure.

## 1. On the build machine: assemble the image

`oci-build` takes the static binary and writes an OCI layout. `--root` holds the inputs, `--out` the
image; the platform must match the target (`amd64` for most Hetzner CX).

```sh
# from a cancho-hooks release tarball, unpacked:
mkdir -p image/blobs/sha256 image/root/bin image/root/deploy

cp hooks-*/bin/hooks            image/root/bin/hooks
cp hooks-*/deploy/hooks.conf.example image/root/deploy/hooks.conf.example

oci-build --root image/root --out image \
  --platform linux/amd64 \
  --bin bin/hooks:bin/hooks --file deploy/hooks.conf.example:deploy/hooks.conf.example \
  --port 8080/tcp --ref 0.1.0-alpha.1-demo
# prints: sha256:<digest>  -- this is the manifest digest everything below names
```

The digest is the point: build it twice, on two machines, and it is the same digest (that is
cancho-oci's G1, held by a committed golden file across macOS/arm64, Linux/x86-64 and three Python
versions). Note the image has no shell, no package list, no `RUN` layer: `FROM scratch` with one
binary and one config file in it.

## 2. Sign it, write its SBOM, attach both

```sh
# a private key (32 random bytes, hex): keep it off the demo box
openssl rand -hex 32 > seed.hex
oci-sign pubkey --seed-file seed.hex                     # prints and writes the public key

oci-sign sign --seed-file seed.hex --image image --out-dir .
# the SBOM: names the image, hashes the same files from the same --root, and carries the
# authority report of the program as properties (--authority path-in-image=report.json)
oci-sbom --name cancho-hooks --version 0.1.0-alpha.1 \
  --image image --root image/root \
  --bin bin/hooks:bin/hooks --file deploy/hooks.conf.example:deploy/hooks.conf.example \
  --authority bin/hooks=docs/authority.json \
  --out-dir .
# both files attach to the image's manifest digest in the registry:
oci-ref attach --registry ghcr.io --repo <owner>/cancho-hooks --subject sha256:<manifest-hex> \
  --type application/vnd.cancho.oci.signature.v1+json --file sha256-<manifest-hex>.sig.json
oci-ref attach --registry ghcr.io --repo <owner>/cancho-hooks --subject sha256:<manifest-hex> \
  --type application/vnd.cyclonedx+json --file sha256-<image-hex>.sbom.cdx.json
oci-ref list --registry ghcr.io --repo <owner>/cancho-hooks --subject sha256:<manifest-hex>
```

The SBOM is the document no other image tool can produce: it carries what the cancho compiler says
the binary may do (the same report the [proof page](https://alpibrusl.github.io/cancho-hooks/proof.html)
shows), not just a list of package names.

## 3. Push and pull

```sh
oci-push --image image --registry ghcr.io --repo <owner>/cancho-hooks --tag 0.1.0-alpha.1-demo
```

`oci-push` re-hashes every document and blob as it reads it, sends blobs before the manifests that
name them, and skips what the registry already has.

## 4. On the demo machine: pull, verify, then run

The order matters: verify *before* the service exists, so a bad image never gets a start.

```sh
# once: the machine's own copy of the tools (built from source at pinned revisions, or from a
# release), and the public key, installed root-only:
install -m 0755 oci-pull oci-sign /usr/local/bin/
install -m 0644 hooks-pubkey.pub /etc/hooks/

# each deploy:
mkdir -p /var/lib/hooks-image/blobs/sha256
oci-pull --registry ghcr.io --repo <owner>/cancho-hooks --ref 0.1.0-alpha.1-demo \
         --out /var/lib/hooks-image

# the signature was fetched beside the image; verify it offline:
oci-ref fetch --registry ghcr.io --repo <owner>/cancho-hooks \
              --subject sha256:<manifest-hex> \
              --type application/vnd.cancho.oci.signature.v1+json --out /tmp/ref
oci-sign verify --pub-file /etc/hooks/hooks-pubkey.pub \
                --image /var/lib/hooks-image --sig /tmp/ref/sha256-<manifest-hex>.sig.json
# prints: verified sha256:<digest> by <key id>   -- or refuses, and nothing below runs
```

## 5. Unpack and install

There is deliberately no runtime in cancho-oci (no daemon, no `oci-run`: the decision record in
cancho-oci #18 keeps runtimes out of v1). The image is an archive; unpacking it is the deployment.

```sh
mkdir -p /opt/hooks/bin
tar -xzf /var/lib/hooks-image/blobs/sha256/<layer-digest> -C /opt/hooks --strip-components=0
# the layer holds bin/hooks and deploy/hooks.conf.example at the paths oci-build was given
install -m 0755 /opt/hooks/bin/hooks /opt/hooks/bin/hooks
```

From here the machine looks exactly like the tarball path: follow the header of
[deploy/hooks.service](../deploy/hooks.service) — `useradd`, the config file at `/etc/hooks/`
(root:hooks 0640, it holds the tokens), `systemctl enable --now hooks`. The unit's hardening
(`ProtectSystem=strict`, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute=yes`) is the
runtime layer around the boundary the compiler already reported.

Why no Docker/Podman on the box: this path needs no daemon, no socket, and no root-running agent —
the same reasoning as cancho-oci's "why" section. If you prefer a container runtime anyway, the
image is standard OCI and `docker run` works too; the [release](https://github.com/alpibrusl/cancho-hooks/releases)
already publishes a ghcr.io image.

## 6. What the machine checks, in one paragraph

Every deploy, the box re-derives rather than trusts: `oci-pull` hashes every blob and refuses a
digest that does not match the ask; `oci-sign verify` checks the signature offline against the public
key the machine already has; the manifest the service will run is the one the signature names. A
registry that serves something else — a different blob, a manifest that points elsewhere — is
refused by name, and the unit never starts. That is the whole point of assembling this path out of
tools that themselves carry authority reports.

## The gaps, and what to do about them

- **The signature does not interoperate with `cosign`.** It is Ed25519 over the manifest digest in a
  cancho-format bundle, verified by `oci-sign verify`. If a policy requires `cosign`-shaped
  signatures, use `cosign` alongside, or wait for cancho-oci's interop task — do not claim the
  existing signature is `cosign`-compatible.
- **`oci-ref`'s registry fallback** (for registries without the referrers API) can lose an entry if
  two writers race, as the OCI spec allows; a single writer (the build machine) does not race.
- **The unpack step is `tar`**, not a verified unpacker: after `oci-pull` and `oci-sign verify` the
  layer digest is known-good, but what `tar` writes to disk is not re-hashed afterwards. A `sha256sum -c`
  over the installed binary against the layer's contents closes that by hand.
- **cancho-oci's TLS** (for https registries) is cancho's own, not independently reviewed — the same
  caveat the proof page carries. For a private demo box, `crane registry serve` on loopback with
  `--plain-http` to `127.0.0.1` sidesteps it entirely.

## What this guide is not

Not a production procedure: the service itself says **not for production** (no completed valid soak
run; [soak.md](soak.md)), and this path inherits that. It is the shape of a deployment whose every
document is derived and verified — the demo the whole narrative wants.
