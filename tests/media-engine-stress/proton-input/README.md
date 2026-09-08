<!-- SPDX-License-Identifier: BSD-3-Clause -->

# Immutable Proton archive input

This Nix function turns one already-pinned, flat Proton `.tar.gz` store object
into a validated, immutable compatibility-tool directory. It is intended as a
safe boundary between archived test candidates and isolated VM tests. It does
not run Proton, Wine, Steam, or a VM, and it never reads or writes Steam data.

## Required inputs

- `nixpkgsStorePath`: the exact path string of a pinned nixpkgs source directory
  that already exists directly in `/nix/store`;
- `archiveStorePath`: the exact path string of a regular, flat archive file
  that already exists directly in `/nix/store`;
- `archiveSha256`: the exact lowercase SHA-256 of the raw archive bytes;
- `topLevelIdentity`: the exact safe top-level archive directory and internal
  Steam compatibility-tool key expected inside the archive;
- `displayName` (optional): the exact user-visible `display_name` expected in
  `compatibilitytool.vdf`. It defaults to `topLevelIdentity`.

The internal identity remains restricted to 1–128 characters from the safe
Steam/archive-key set. The display name is independently validated as
nonempty, Unicode-NFC, printable text of at most 256 UTF-8 bytes; quotes,
backslashes, leading/trailing whitespace, control characters, and other
non-printing characters are rejected so its quoted VDF representation is
unambiguous.

The two store-path strings are restricted to the canonical Nix-store character
set before `builtins.storePath` gives them store dependency context. This
retains the existing store identities; using Nix path values here would
unnecessarily copy large, already-stored archives under new names. Example:

```console
nix-build default.nix \
  --argstr nixpkgsStorePath /nix/store/…-source \
  --argstr archiveStorePath /nix/store/…-Proton-example.tar.gz \
  --argstr archiveSha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --argstr topLevelIdentity Proton-example \
  --argstr displayName "Proton Example"
```

The result itself is the compatibility-tool root: `proton`,
`compatibilitytool.vdf`, `LICENSE`, and `files/` are directly beneath the Nix
result path. No extra archive root remains.

## Rejection boundary

Validation happens before extraction. The packager rejects:

- a raw archive hash mismatch or a mismatched/multiple top-level root;
- absolute, traversal, empty-component, dot-component, backslash, non-NFC,
  duplicate, overlong, or otherwise noncanonical member names;
- devices, FIFOs, sockets, sparse entries, special permission bits, and
  non-file members carrying data;
- absolute or root-escaping symlinks, ambiguous/escaping hardlinks, missing or
  non-file hardlink targets, and any child beneath a file or link;
- a member collision with the generated `.proton-input` provenance directory;
- a missing/non-executable `proton`, a missing or malformed
  `compatibilitytool.vdf`, or a missing/nonregular/empty root `LICENSE`;
- a `compatibilitytool.vdf` that does not bind exactly one matching internal
  identity object to the separately expected `display_name` and
  `install_path "."`. Matching values in another tool object do not count.

Extraction never calls Python's `extract()` or `extractall()`. Directories and
regular files are materialized first, validated hardlinks follow, and symlinks
are created last. File executable bits are preserved; ownership is not.
Timestamps are normalized to one second after the Unix epoch. The archive and
its extracted Proton payload are not rewritten; only the separate generated
`.proton-input` provenance directory is added to the result.

## Evidence in every result

`$result/.proton-input/` contains:

- `archive.sha256`: the verified raw archive hash;
- `payload-tree.sha256`: a deterministic digest of every extracted payload
  node's type, relative path, Nix-preserved executable status, size/content
  hash, or link target;
- `provenance.json`: canonical JSON recording the archive and nixpkgs store
  paths, exact internal identity and display name, archive-member manifest
  digest, payload-tree digest, member/byte counts, required-file checks, and
  the license inventory.

The payload-tree digest intentionally excludes `.proton-input` to avoid a
circular digest. The archive-member digest still binds original directory,
file, hardlink, and symlink declarations, including their full archive modes.
The payload digest records only the executable permission distinction because
that is the permission metadata Nix preserves after making store paths
read-only; it can therefore be recomputed from the final realized store path.

## Checks

```console
./check.sh
```

The pure suite constructs archives in temporary directories and exercises the
accept path plus hostile path, link, type, duplicate, root, hash, permission,
license, distinct display-name, and separately mismatched VDF binding cases. It
also verifies that accepting a distinct display name does not rewrite the
archived VDF payload. It does not access `/nix/store` or execute a compatibility
runtime. A real candidate is validated only when `nix-build` successfully
creates its immutable result.
