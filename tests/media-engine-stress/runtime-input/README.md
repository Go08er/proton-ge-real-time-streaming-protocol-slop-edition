<!-- SPDX-License-Identifier: BSD-3-Clause -->

# Immutable Steam Runtime input

This small Nix boundary copies an existing Steam Linux Runtime store snapshot
into a clean immutable test input. The required `runtimeFamily` declaration is
a closed choice:

- `sniper` audits `sniper_platform_*`
- `steamrt4` audits `steamrt4_platform_*` (Steam Linux Runtime 4)

It excludes only top-level `var`, which is a stale materialized/writable
runtime cache rather than the canonical pressure-vessel and declared platform
payload.

It does not execute the runtime, Proton, Wine, Steam, or a VM. It does not read
or write Steam libraries or compatdata.

## Build

Both inputs are strict top-level Nix-store path strings. `builtins.storePath`
adds dependency context while preserving their existing identities; passing a
2.6 GiB runtime as a Nix path value would needlessly import it under a second
store name.

```console
nix-build default.nix --no-out-link \
  --argstr nixpkgsStorePath /nix/store/…-source \
  --argstr runtimeFamily steamrt4 \
  --argstr runtimeStorePath /nix/store/…-SteamLinuxRuntime_steamrt4-snapshot
```

The existing sniper path remains supported by changing both runtime arguments:

```console
nix-build default.nix --no-out-link \
  --argstr nixpkgsStorePath /nix/store/…-source \
  --argstr runtimeFamily sniper \
  --argstr runtimeStorePath /nix/store/…-SteamLinuxRuntime_sniper-snapshot
```

The build copies every top-level runtime entry except exact `var`, preserving
links and executable bits. It then verifies nonempty executable
`_v2-entry-point`, `run`, `pressure-vessel/bin/pressure-vessel-wrap`, and
`pressure-vessel/bin/pressure-vessel-unruntime`; a real `pressure-vessel`
directory; and at least one platform directory matching the declared family
with a nonempty regular file below `files/`. A declaration with no matching
payload is rejected.

`$result/.runtime-input/provenance.json` records the source's descriptive store
name and a SHA-256 of its exact store path, declared runtime family and platform
prefix, omitted top-level name, qualifying platform names, required executable
hashes, tree counts/bytes, and payload-tree digest. It intentionally does not
embed the source's Nix store hash: doing so would keep the excluded multi-GiB
tree in the sanitized output's runtime closure. The derivation itself remains
bound to the exact source store input. The tree digest is independently
recomputable after Nix makes the result read-only because it binds the
Nix-preserved executable distinction instead of writable permission bits.
Generated provenance is excluded from its own digest.

## Pure checks

```console
./check.sh
```

The pure tests use temporary synthetic trees. They neither access nor execute
the real Steam Runtime.
