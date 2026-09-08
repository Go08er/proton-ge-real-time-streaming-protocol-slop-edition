# Source and licensing

SE1 is an additive patchset over GE-Proton11-6, not an independent media
backend. Upstream authors retain their copyrights and licenses. The root
BSD-3-Clause license covers this repository's independently authored tooling
where applicable; it does not relicense Wine, FFmpeg or third-party code.
Patches modifying upstream files follow the licenses of those files.

## Exact build inputs

- GE commit: `7e88cefffc122ea1584c2156b8d7bae6cf69b2a7`.
- GE root tree: `851680369dc46489360e4f67bfa3acc6b701ef4a`.
- Wine: `9358696fe9a2261329f4a83aa6a65fd436106154`.
- FFmpeg: `9047fa1b084f76b1b4d065af2d743df1b40dfb56`, plus the declared
  security/TLS and crypto-build patch series.
- Wine patch series: `patches/series-ge-proton11-6-se1`, 21 entries.
- Ordered Wine series/content SHA-256:
  `b3d0a1348a4206e9fc9be9ee5683ed54da4be3419be0aaef0edff6d398c642f3`.
- Pin: `config/ge-proton11-6-se1.env`; this selects the upstream launcher.
- Complete GE root gitlinks: `release-manifests/GE-Proton11-6.manifest.tsv`.
- All 79 initialized top-level/nested submodule revisions: release asset
  `SUBMODULES.tsv` (source revisions, not a count of compiled components).
- Contrib inputs: `config/ge-build-contrib-7e88ceff.tsv` (14 entries).

The exact SDK image and its verified image ID are in the pin. The build uses
Make 8 × Ninja 2 and an offline container; source/input acquisition is a
separate step. Optional NVIDIA-library and Vulkan-layer payload groups are
disabled, as recorded in the pin. The original GE source, all active patches
and the preparation/build scripts are part of the source account.

Wine changes concern the Media Foundation/MediaEngine, WineDMO and
WineWayland paths. FFmpeg remains the demux/decode backend. The SE1 launcher
matches upstream GE byte-for-byte; `PROTON_XR_MODE` is not parsed or applied.

## Distribution materials

The prerelease includes verified binary/source materials and checksums;
their final filenames and hashes are recorded in [the release notes](../RELEASE.md).
The prepared-source archive is intended for source inspection and the exact
modified source record. Use the pinned clean-input workflow for rebuilding,
not a stale configure-state or an unrelated prepared checkout.
Git administration, Python caches and the three unused FEX precompiled test
collections (`fex-gcc-target-tests-bins`, `fex-gvisor-tests-bins`,
`fex-posixtest-bins`) are omitted. GE's Makefile enables FEX only for
aarch64; it is not part of this x86_64 artifact. Their initialized revisions
remain recorded in `SUBMODULES.tsv`. No compiled/modified SE1 source is
omitted by that exclusion.

The binary retains upstream `LICENSE` and `LICENSE.OFL`. Component license
texts remain in their corresponding source trees. Gecko, Mono, Xalia, ONNX
Runtime and zenity-rs are upstream prebuilt inputs, as they are in GE's
workflow: "from source" here means rebuilding the Proton/GE candidate, not
claiming that every third-party bootstrap input is rebuilt from source.
Their origins and expected hashes are recorded rather than hidden.

The release also supplies the official Wine Mono 11.2.0 and Wine Gecko
2.47.4 source distributions, and source snapshots for ONNX Runtime 1.14.1,
Xalia 0.4.9 and zenity-rs 0.2.8. The snapshots retain their dependency
manifests/submodule declarations; they are not claims that all optional
upstream dependencies have been vendored or independently rebuilt.
The build-input archive includes the actual Piper/espeak-ng/Sonic source
archives used by this build. See `UPSTREAM-SOURCES.tsv` in the release for
origins, sizes and independently observed SHA-256 values.

The inherited GE license file mentions GStreamer from its broader upstream
distribution. SE1's artifact check requires that no GStreamer payload is
present; retaining that notice is not evidence of another media backend.

No private Proton logs, game prefixes, account data or captured third-party
media are part of the public source export.
