# Proton GE Real-Time Streaming Protocol — Slop Edition

An experimental patchset for RTSP, other streaming video and livestream
playback on GE-Proton's WineDMO and FFmpeg stack. The goal is **a good
streaming experience in VRChat without GStreamer**, including RTSP
capabilities comparable to the GStreamer-based path. This is a development
goal, not a claim of complete parity. Support for other games is not the
project's focus, and no second media backend is added.

The project's patchset was developed by GPT-6 Astra under human-maintainer
direction, retaining credited upstream contributions.

> **Experimental: for users experienced with Proton debugging only.**
> This is not an official Valve or GloriousEggroll release. Keep a known-good
> compatibility tool and backups. Regressions are possible; help, fixes and
> response times are not guaranteed.

## What it offers

- RTSP/RTP network-video support on GE's WineDMO → FFmpeg path, including
  the `rtspt://` TCP alias used by tested VRChat players.
- Additional HTTP(S), HLS and on-demand video playback fixes, covering
  streaming and livestream scenarios beyond RTSP.
- Playback-state, seeking, source-replacement and audio-timeline corrections.
- Good audio/video synchronization in the maintainer's tested scenarios,
  reported as an improvement over the reference setup. This is an observation,
  not a universal or quantitatively benchmarked guarantee.
- A pinned GE-Proton foundation, retaining features such as WineWayland and
  the bundled Discord RPC bridge, subject to each release's documented build
  options. A separate host Steam-wrapper script is not installed or managed
  by this project.
- Bounded opt-in diagnostics for investigating failures without broad media
  tracing. Logs must still be treated as private.

## Trade-offs

Stability may be lower than
[SpookySkeletons' Proton-RTSP](https://github.com/SpookySkeletons/proton-rtsp),
and compatibility with particular video formats, servers or world players
may be worse. Tests cover selected paths,
not every game, codec or device. Included optional GE components and known
limitations are documented for each release; not every GE payload is
necessarily bundled. World-side URL handling, unavailable content, VPNs,
headset streaming and host audio can fail independently of this patchset.

## How the repository is organized

This is a downstream patchset and build-tooling repository, not a GitHub fork
containing GE's full source history. Builds acquire an exact upstream GE
revision and apply the selected, ordered patches at the documented points in
GE's preparation workflow. The upstream source is not duplicated in this
checkout.

[Reading the patch series](docs/PATCHES.md) explains how to find the selected
pin, follow the Wine and FFmpeg changes, and distinguish release inputs from
historical candidates. Exact versions, checksums, build options and test
results belong in the release and source documentation, not this overview.

## Start here

- [Release notes](RELEASE.md): exact build, changes and qualification.
- [Downloads and release history](https://github.com/Go08er/proton-ge-real-time-streaming-protocol-slop-edition/releases).
- [Installation and NixOS](docs/INSTALL.md).
- [Build from source](docs/BUILDING.md).
- [Testing and safe issue reports](docs/TESTING.md).
- [Contributing](CONTRIBUTING.md) and [source/licensing](docs/SOURCE.md).

Confirmed issue captures and reproducible test data are especially useful.
Human developers and reviewed AI-assisted contributions are welcome; see
the contribution guide for the requested model baseline and evidence standard.

Credit for Wine, Proton, GE-Proton, FFmpeg and other upstream components
belongs to their respective contributors. This project's attribution does
not replace upstream authorship or licenses.
