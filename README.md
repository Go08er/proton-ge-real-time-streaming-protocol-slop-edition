# Proton GE Real-Time Streaming Protocol — Slop Edition

An experimental RTSP-focused patchset for GE-Proton's WineDMO and FFmpeg
media stack. The project's patchset was developed by GPT-6 Astra under
human-maintainer direction, retaining credited upstream contributions.
The aim is practical network-video playback in Windows games under Linux,
including VRChat, without adding a second media backend.

> **Experimental: for users experienced with Proton debugging only.**
> This is not an official Valve or GloriousEggroll release. Keep a known-good
> compatibility tool and backups. Regressions are possible; help, fixes and
> response times are not guaranteed.

## What it offers

- RTSP/RTP network-video support on GE's WineDMO → FFmpeg path, including
  the `rtspt://` TCP alias used by tested VRChat players.
- Playback-state, seeking, source-replacement and audio-timeline corrections.
- Good audio/video synchronization in the maintainer's tested scenarios,
  reported as an improvement over the reference setup. This is an observation,
  not a universal or quantitatively benchmarked guarantee.
- The GE-Proton11-6 foundation, including WineWayland and its bundled Discord
  RPC bridge. A separate host Steam-wrapper script is not installed or managed
  by this project.
- Bounded opt-in diagnostics for investigating failures without broad media
  tracing. Logs must still be treated as private.

## Trade-offs

Stability may be lower than stock GE, and compatibility with particular video
formats, servers or world players may be worse. Tests cover selected paths,
not every game, codec or device. This build also excludes GE's optional
NVIDIA-library and Vulkan-layer bundles; it is not a claim that every optional
GE payload is included. World-side URL handling, unavailable content, VPNs,
headset streaming and host audio can fail independently of this patchset.

The obsolete `PROTON_XR_MODE` compatibility flag is not implemented in SE1.
WineWayland's VR device-extension support remains. There is no automatic
reconnect loop or blanket retry policy.

## Start here

- [SE1 prerelease notes](RELEASE.md): exact build, changes and qualification.
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
