#!/bin/bash
# Make a release: a tarball of the service and what an operator needs to run it, SHA256SUMS, and an SBOM *stub*.
#
#   LEX_SYS=/path/to/lex-sys scripts/release.sh [--version V] [--out DIR] [--no-build] [--allow-dirty] [--keep-symbols]
#
# It builds with `lex-sys build` (which refuses any compiler but the commit lex-sys.toml pins), then writes into --out (default dist/<name>/):
#
#   hooks-<version>-<arch>.tar.gz          bin/hooks, deploy/ (unit, settings sample, health check), scripts/ (backup, restore, logcheck),
#                                          sql/schema.sql, docs/, README.md, LICENSE, Dockerfile, SBOM.json
#   hooks-<version>-<arch>.sbom.json       the same SBOM, beside the tarball
#   SHA256SUMS                             of both files (`sha256sum -c SHA256SUMS`)
#
# The tarball is deterministic for a given binary and tree: sorted names, owner 0, a fixed mtime (SOURCE_DATE_EPOCH, default the
# last commit's), `gzip -n`. Whether the BINARY is bit-for-bit reproducible is a property of the compiler, measured and stated in
# docs/runbook.md ("Releases"), not assumed here.
#
# NOT DONE, and the SBOM says so: the tarball and SHA256SUMS are not signed (anyone who can replace both can replace both); the SBOM is a
# hand-made listing of what this script could read from the tools, not CycloneDX or SPDX, and no scanner has checked it; there is no
# stable URL (a release is a file you publish yourself; CI keeps a binary as a run artifact for 90 days).
#
# Needs: bash, git, tar, gzip, sha256sum, ldd, python3 (3.11 for tomllib). Exit: 0 done; 2 usage or a missing tool; 3 refused (a dirty tree, an
# unbuilt binary); 4 the build failed.
set -euo pipefail
umask 022

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { local code=$1; shift; echo "release: $*" >&2; exit "$code"; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2; exit 2; }

version="" out="" build=1 dirty_ok=0 strip_it=1
while [ $# -gt 0 ]; do
  case $1 in
    --version) version=${2:?--version needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --no-build) build=0; shift ;;
    --allow-dirty) dirty_ok=1; shift ;;
    --keep-symbols) strip_it=0; shift ;;
    -h|--help) usage ;;
    *) echo "release: unknown argument: $1" >&2; usage ;;
  esac
done
for tool in git tar gzip sha256sum ldd python3; do command -v "$tool" >/dev/null || die 2 "$tool is needed"; done

cd "$here"
git rev-parse --git-dir >/dev/null 2>&1 || die 2 "$here is not a git checkout: a release names the commit it was built from"
commit=$(git rev-parse HEAD)
dirty=0
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then dirty=1; fi
if [ "$dirty" = 1 ] && [ "$dirty_ok" != 1 ]; then die 3 "the working tree has uncommitted changes: commit them, or --allow-dirty (the SBOM then says dirty)"; fi
[ -n "$version" ] || version=$(git describe --tags --always)
case $version in *[!A-Za-z0-9._+-]*|"") die 2 "--version may hold letters, digits and . _ + - only" ;; esac
[ "$dirty" = 0 ] || version="$version-dirty"
arch=$(uname -m)
name=hooks-$version-linux-$arch
[ -n "$out" ] || out="$here/dist/$name"
epoch=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}

if [ "$build" = 1 ]; then
  : "${LEX_SYS:?set LEX_SYS to the lex-sys compiler binary (the commit lex-sys.toml pins)}"
  rm -rf "$here/build/hooks" "$here/build/deps"
  (cd "$here" && CC="$here/scripts/cc-ssl.sh" "$LEX_SYS" build) >&2 || die 4 "lex-sys build failed (OpenSSL's development files are needed to link: libssl-dev)"
fi
[ -x "$here/build/hooks" ] || die 3 "build/hooks does not exist: build first, or drop --no-build"
if [ "$build" = 0 ]; then echo "release: --no-build: packaging whatever build/hooks is (the SBOM records its hash, not how it was made)" >&2; fi

mkdir -p "$out"
out=$(cd "$out" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/$name"
mkdir -p "$root/bin" "$root/deploy" "$root/scripts" "$root/sql" "$root/docs"
cp "$here/build/hooks" "$root/bin/hooks"
# What the compiler leaves in the binary that differs between two builds of the same sources is a temporary file's name with a process id
# (a FILE symbol, `lex-sys-llvm-<pid>-0.ll`) and, in turn, the build-id note that hashes it. Without them the binary is bit-for-bit the same
# from build to build (measured: docs/runbook.md "Releases"), so that is what is shipped. --keep-symbols ships what the compiler wrote.
if [ "$strip_it" = 1 ]; then
  command -v strip >/dev/null || die 2 "strip is needed (binutils), or --keep-symbols"
  strip --strip-all --remove-section=.note.gnu.build-id "$root/bin/hooks"
fi
cp "$here"/deploy/hooks.service "$here"/deploy/hooks.conf.example "$here"/deploy/hooks.docker.conf "$here"/deploy/hooks-healthcheck.sh "$here"/deploy/hooks-entrypoint.sh "$root/deploy/"
cp "$here"/scripts/backup.sh "$here"/scripts/restore.sh "$here"/scripts/logcheck.py "$here"/scripts/release.sh "$root/scripts/"
cp "$here"/sql/schema.sql "$here"/sql/queries.sql "$root/sql/"
cp "$here"/docs/runbook.md "$here"/docs/design.md "$here"/docs/production.md "$root/docs/"
cp "$here"/README.md "$here"/LICENSE "$here"/Dockerfile "$here"/lex-sys.toml "$root/"
chmod 0755 "$root/bin/hooks" "$root/deploy/hooks-healthcheck.sh" "$root/deploy/hooks-entrypoint.sh" "$root"/scripts/*

# The SBOM stub: read from the tools, and honest about what it does not list.
python3 - "$here" "$root" "$name" "$version" "$commit" "$dirty" "${LEX_SYS:-}" "$strip_it" <<'PY' > "$out/$name.sbom.json"
import hashlib, json, os, re, shutil, subprocess, sys, tomllib

here, root, name, version, commit, dirty, lex_sys, stripped = sys.argv[1:9]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


project = tomllib.load(open(os.path.join(here, "lex-sys.toml"), "rb"))
pinned = project["package"]["lex-sys"]
binary = os.path.join(root, "bin", "hooks")

compiler = {"pinned_by_lex_sys_toml": pinned}
if lex_sys:
    compiler["binary"] = lex_sys
    compiler["reports"] = run(lex_sys, "--version")        # "lex-sys 0.0.0 (rev <commit>, host <triple>)"
    compiler["rev_matches_pin"] = bool(compiler["reports"] and pinned in compiler["reports"])
else:
    compiler["reports"] = None
    compiler["note"] = "LEX_SYS was not set (--no-build): the compiler that made build/hooks is not recorded"
# The compiler's LLVM backend shells out to clang and its linker step to cc, so they shaped the binary too.
compiler["clang"] = (run("clang", "--version") or "not found").splitlines()[0]
compiler["cc"] = (run("cc", "--version") or "not found").splitlines()[0]

# std is compiled into the compiler (it has no version of its own): the compiler commit IS the std version.
std_modules = sorted({m for f in os.listdir(os.path.join(here, "src")) if f.endswith(".ls")
                      for m in re.findall(r"^import (std\.[a-z_0-9]+);", open(os.path.join(here, "src", f)).read(), re.M)})
std = {"version": f"embedded in the compiler at {pinned}", "modules_imported_by_src": std_modules}

libc = []
for line in (run("ldd", binary) or "").splitlines():
    m = re.match(r"\s*(\S+) => (\S+) \(0x", line) or re.match(r"\s*(/\S+) \(0x", line)
    if not m or m.group(1).startswith("linux-vdso"):
        continue
    path = m.group(2) if m.lastindex == 2 else m.group(1)
    entry = {"soname": m.group(1), "path": path}
    real = os.path.realpath(path)
    if os.path.exists(real):
        entry["sha256"] = sha256(real)
    pkg = run("dpkg", "-S", real) or run("dpkg", "-S", path)
    if pkg:
        package = pkg.split(":")[0]
        entry["package_of_build_host"] = package
        entry["package_version_of_build_host"] = run("dpkg-query", "-W", "-f=${Version}", package)
    libc.append(entry)

libraries = []
for lib, spec in sorted(project.get("dependencies", {}).items()):
    libraries.append({"name": lib, "git": spec.get("git"), "rev": spec.get("rev"), "path": spec.get("path")})
sbom = {
    "format": "lexsys-hooks-sbom-stub/1",
    "complete": False,
    "read_this_first": ("A stub, not CycloneDX or SPDX: a listing of what scripts/release.sh could read from the build tools, written so "
                        "that what it leaves out is as visible as what it lists. No scanner has checked it and nothing here is signed."),
    "component": {
        "name": "hooks", "version": version, "license": "EUPL-1.2", "git_commit": commit, "git_tree_dirty": dirty == "1",
        "binary": {"path": "bin/hooks", "sha256": sha256(binary), "bytes": os.path.getsize(binary),
                   "normalized": ("strip --strip-all --remove-section=.note.gnu.build-id: no symbols, no build-id, so that two builds of the same "
                                  "sources on the same toolchain are byte-identical" if stripped == "1" else "no: as the compiler wrote it, with symbols")},
        "lex_sys_toml_sha256": sha256(os.path.join(here, "lex-sys.toml")),
    },
    "compiler": compiler,
    "std": std,
    "lex_sys_libraries_pinned_by_commit": libraries,
    "dynamic_libraries_of_the_binary": {
        "from": "ldd on the build host; the target host supplies its own copies, which must be this glibc or newer",
        "entries": libc,
    },
    "statically_linked_native_code": "none known beyond the foreign symbols the compiler's authority report lists, pinned in docs/authority.json (scripts/check-authority.sh): libc (statx, src/perm.ls), and libssl and libcrypto for the TLS client of an https endpoint (src/tls.ls), which are the dynamic libraries above; not verified by this script",
    "not_listed": [
        "the Rust toolchain that built the compiler (named by the compiler repository's rust-toolchain.toml at the pinned commit)",
        "the crates the compiler was built from (its Cargo.lock at the pinned commit)",
        "LLVM and the linker behind clang and cc beyond the version lines above",
        "the contents of the libraries' own dependencies, if they had any (lex-sys libraries record theirs in their own lex-sys.toml)",
        "the base image and its packages, for the container image (the Dockerfile does not pin a digest)",
        "any signature, attestation or provenance statement",
    ],
}
json.dump(sbom, sys.stdout, indent=2, sort_keys=False)
print()
PY
cp "$out/$name.sbom.json" "$root/SBOM.json"

# A fixed order, owner and time; gzip without the name and time in its header.
(cd "$stage" && tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" -cf - "$name") | gzip -n -9 > "$out/$name.tar.gz"
(cd "$out" && sha256sum -- "$name.tar.gz" "$name.sbom.json" > SHA256SUMS)
echo "release: $out/$name.tar.gz" >&2
echo "release: $out/$name.sbom.json" >&2
echo "release: $out/SHA256SUMS" >&2
cat "$out/SHA256SUMS"
