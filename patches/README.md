# Master patch index

Start here to understand what the project changes. Each named patch below
links directly to its explanation and code diff. The plain `series` files
are machine-readable application lists, not the browsing interface.

This index tracks the **released SE1 set on GE-Proton11-6**: 21 Wine patches,
four FFmpeg source patches and one GE build patch. Its exact inputs are the
[release pin](../config/ge-proton11-6-se1.env) and
[Wine application list](series-ge-proton11-6-se1). For qualification and
known limits, see the [release notes](../RELEASE.md); the descriptions below
explain intent, not a guarantee that every scenario works.

## Choose a component

- **Wine media and VR:** the 21 individual patches below.
- **FFmpeg security and TLS:** [four patches with explanations and upstream commit links](ffmpeg-security/README.md#patch-by-patch-guide).
- **Encrypted HLS build support:** [one GE build-configuration patch](ffmpeg-build/README.md#patch-at-a-glance).
- **How preparation fits together:** [review and replay guide](../docs/PATCHES.md).

## Wine media and VR patches

Read these in the listed order. File numbers are stable identifiers, not
application positions: numbers have gaps, and diagnostic 0019 is last.
Our Wine series is inserted after GE's `ge-video-rework` set, not applied
directly to an unpatched Wine checkout.

| Order | Patch — open explanation and diff | What it addresses |
| --- | --- | --- |
| 1 | [0001 — RTSP and RTSP-over-TCP](ge-proton11-6/0001-winedmo-open-rtsp-over-tcp.patch) | Opens RTSP through FFmpeg and recognizes the `rtspt://` TCP alias used by tested VRChat players. |
| 2 | [0002 — Interruptible network operations](ge-proton11-6/0002-winedmo-interrupt-network-demux-and-report-live-seekability.patch) | Adds cancellation and RTSP deadlines; reports actual seekability and discards reads overtaken by a flush. |
| 3 | [0003 — Correct 32-bit demuxer teardown](ge-proton11-6/0003-winedmo-use-destroy-params-for-wow64-demuxer.patch) | Fixes the WOW64 parameter layout so teardown passes the correct demuxer handle to the Unix side. |
| 4 | [0004 — Asynchronous loading and source lifecycle](ge-proton11-6/0004-winedmo-async-http-and-repair-vod-seeking.patch) | Handles stale loads, source generations, sample queues, seek scheduling and teardown. This is a multi-behavior patch, not one small fix. |
| 5 | [0005 — HTTP streaming and HLS compatibility](ge-proton11-6-a322/0005-winedmo-stream-progressive-http-vod.patch) | Streams before a full download; adds HTTP seek/trust handling, a Windows Media Foundation user agent, and content-based HLS segment acceptance. |
| 6 | [0006 — Interrupted reads during seeking](ge-proton11-6/0006-winedmo-recover-interrupted-vod-seeks.patch) | Handles deliberately cancelled partial reads and deferred seek work. Failed seek transitions report a terminal error rather than retrying. |
| 7 | [0007 — Resume sample delivery after seeking](ge-proton11-6-a320/0007-mf-reprime-and-resume-after-vod-seeks.patch) | Retains sink demand until sources restart, re-primes the pull chain, and preserves the newest seek target. |
| 8 | [0008 — Preserve the audio timestamp origin](ge-proton11-6/0008-winedmo-preserve-audio-timeline-across-flush.patch) | Keeps an established audio timeline across decoder flushes so post-seek audio is not mistaken for samples before the target. |
| 9 | [0009 — Flush a running audio renderer correctly](ge-proton11-6/0009-mf-sar-preserve-running-state-across-flush.patch) | Stops the audio client before resetting its buffer, then restores its running state and preserves operation failures. |
| 10 | [0011 — Correct the first presentation-clock offset](ge-proton11-6/0011-mf-fix-initial-presentation-clock-offset.patch) | Prevents an initial pause/unpause from using system time as the media offset. Credits the original RTSP Wine change in its header. |
| 11 | [0013 — Honor the latest pause/play intent](ge-proton11-6/0013-mfmediaengine-reconcile-pause-scrub-intent.patch) | Reconciles pause requests during initial frame preparation instead of allowing stale pending-play state to restart playback. |
| 12 | [0014 — Keep the replacement source's start position](ge-proton11-6/0014-mfmediaengine-preserve-pending-start-position.patch) | Separates pending playback state from old presentation resources so cleanup does not erase the new source's requested position. |
| 13 | [0015 — Order source replacement correctly](ge-proton11-6/0015-mf-order-immediate-replacement-start.patch) | Detaches old sinks and stops the old clock before starting the new presentation, avoiding startup notifications arriving out of order. |
| 14 | [0017 — Surface terminal network and seek failures](ge-proton11-6-a320/0017-winedmo-propagate-terminal-network-read-errors.patch) | Reports errors to the current MediaEngine source instead of disguising them as normal end-of-stream or successful seeking. |
| 15 | [0018 — Advertise VR extensions on Wayland](ge-proton11-6/0018-winewayland-advertise-wine-vr-device-extensions.patch) | Exposes Wine's synthetic OpenVR and OpenXR device-extension requests through the Wayland driver. |
| 16 | [0020 — Remove costly/noisy media diagnostics](ge-proton11-6/0020-quiet-ge-media-diagnostics.patch) | Removes diagnostic pixel scans, forced readbacks and URL-bearing probes; reduces routine chatter while retaining failure warnings. |
| 17 | [0022 — Report the frame actually selected](ge-proton11-6-a320/0022-mfmediaengine-preserve-selected-frame-tick-result.patch) | Keeps a successful frame tick and its timestamp when a later queued sample is future-dated or discarded as preroll. |
| 18 | [0023 — Check seekability before clearing frames](ge-proton11-6-a320/0023-mfmediaengine-check-seekability-before-recovery-reset.patch) | Prevents a refused recovery seek from clearing a live source's frames and sample-request state first. |
| 19 | [0025 — Align HLS VOD timestamps and seek positions](ge-proton11-6-a322-r2/0025-winedmo-normalize-hls-vod-timeline.patch) | Maps samples and seek targets to one origin, handling streams whose timestamps do not begin at zero. |
| 20 | [0026 — Replenish decoder output buffers after preroll](ge-proton11-6-a323/0026-mf-replenish-output-buffers-after-preroll.patch) | Replaces released output buffers before decoding again, addressing the reproduced HLS seek freeze with a stopped audio-driven clock. |
| 21 | [0019 — Bounded drain diagnostics](ge-proton11-6/0019-winedmo-trace-bounded-rtsp-drain-state.patch) | Opt-in summaries of queues, demand, reads and audio-clock startup. Diagnostic only: no timeout, queue-limit or playback-policy change. |

### Reviewing a suspected regression

Read the patch header and changed functions, then the later patches touching
the same area. For example, HLS seeking involves the HTTP route (0005),
restart ordering (0007), audio origin (0008), HLS origin (0025) and output
buffers (0026). Individual patches are not independent toggles.

Useful evidence entry points are the [VOD control tests](../tests/vod-control/README.md),
[HTTP/HLS tests](../tests/progressive-http-vod/README.md), and
[source replacement tests](../tests/source-replacement-lifecycle/README.md).
Distinguish source/model tests from actual playback results; the
[release qualification](../RELEASE.md#qualification-and-limits) records
what was tested for the released artifact.

## Historical files and commit history

The SE1 release series intentionally references exact patch revisions across
several candidate directories. `experimental/`, older `ge-proton*` variants,
and the other `series-*` lists remain for comparison; inclusion in a directory
does not mean a patch ships. The unqualified `patches/series` is the retained
A3.21 selection, not SE1's list. The separate launcher compatibility patch
under `proton/` is not part of SE1, which uses the unchanged GE launcher.

The public repository began with one source-kit import, so its initial
patches do not each have a separate GitHub commit. Their files retain their
existing explanations and attribution. This index makes them independently
browsable without fabricating development history, changing patch hashes,
or moving files referenced by immutable pins. For FFmpeg's unmodified
backports, the guide also links the actual upstream commits.

When selecting a new release, update this index's release identity, links and
application order against that release's series files. Document added,
replaced or dropped patches in the release notes; preserve prior release tags
and their inputs. An index entry describes inclusion and purpose, not runtime
qualification.
