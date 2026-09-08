# SE1 — GE-Proton11-6 RTSP prerelease

Experimental x86_64 Linux prerelease, built and qualified on 2026-09-08.
Intended for users experienced with Proton debugging; assistance is not guaranteed.

Tool name: `proton-ge-11-6-rtsp-se1`.

SE1 carries the same 21 Wine media patches as the co-tested A3.23 candidate.
Its functional packaging change is removal of the obsolete launcher-side
`PROTON_XR_MODE` policy and its extra logging. The launcher must match the
pinned upstream GE file exactly. The WineWayland VR device-extension fix is
retained; there is no new playback, timeout or retry policy in this revision.

The preceding A3.23 trial reported working service playback/repeated seeking,
ordinary YouTube playback/seeking, Waterwolf RTSP and multiplayer YouTube
join-in-progress. Its contained regression reproduced the prior HLS seek
freeze before the fix. Those results are scoped to A3.23, not a substitute
for SE1 artifact verification or a new in-game trial. A separate earlier
whole-game/VR freeze remains unexplained.

## Download and verify

Binary: `proton-ge-11-6-rtsp-se1.tar.gz` — **526,749,599 bytes**.

SHA-256:
```text
d45096ad47e2489129b62b54175176914555d63f4dd5b8b4b58c1cb42b9c8895
```

Download `SHA256SUMS` alongside your chosen assets and run
`sha256sum --check --ignore-missing SHA256SUMS`; require an `OK` result for
each downloaded file. See [installation and NixOS](docs/INSTALL.md).

Source/build materials:

- `proton-ge-11-6-rtsp-se1-source-x86_64.tar.zst`: prepared corresponding
  source, 644,232,949 bytes; SHA-256
  `f163b48a80e06da79f61d982041f00c72deebd275a5a34ba2f8d2e8867ef93b0`.
- `proton-ge-11-6-rtsp-se1-build-inputs.tar.zst`: the 14 verified contrib
  inputs, 169,186,426 bytes; SHA-256
  `36a76f1dfd8baf250056dfdff71295f68fb5ebf91786730d5b6420b283733033`.
- `SUBMODULES.tsv`, `UPSTREAM-SOURCES.tsv` and the additional Mono, Gecko,
  ONNX Runtime, Xalia and zenity-rs source archives complete the source
  account described in [source and licensing](docs/SOURCE.md).
- Repository source contains the selected pin, patches, manifests, tests,
  Nix packaging and [automated build instructions](docs/BUILDING.md).

## Qualification and limits

- One offline GE `redist` build completed for both Wine architectures;
  Make 8 × Ninja 2, all 79 submodules and 14 contrib inputs seeded locally.
- Artifact checks passed: hashes, gzip integrity, exact archive/tree match,
  links/types/permissions, home-path privacy scan and no GStreamer payload.
  The release-directory copy was independently rehashed.
- Full preparation, selected-series Wine audits and progressive HTTP source
  audit passed. The complete prepared Wine content remained unchanged by
  compilation and is identical to A3.23. Exact-upstream launcher check passed.
- Same retained MediaEngine probe/fixtures: R2 again froze after the forward
  HLS seek; SE1 passed MP4, ordinary HLS and web-style HLS initial playback,
  forward seek and backward seek. Each successful phase required advancing
  clock and distinct-PTS software frame transfers. Loopback only, private
  null audio, fresh synthetic prefixes; no real-service or headset claim.
- Nix prebuilt package built from the local release archive; file contents
  and symlinks match the original tool. NixOS module evaluation passed.
  Source-build app built; dry-run and nine CLI/cache/orchestration tests
  passed. A fresh network-to-artifact run of that convenience wrapper has
  **not** been performed; it orchestrates the separately verified build path.

SE1 has not had a new VRChat/headset trial. No general stability, format
coverage, long-soak or measured A/V synchronization guarantee follows.
Keep a fallback compatibility tool and report confirmed, privacy-reviewed
reproductions. See [testing](docs/TESTING.md).
