# Candidate generic Media Foundation fixes

This is a quarantine area, not an apply-all series. RTSP Wine commits listed in
`docs/RESEARCH.md` may be exported here only after release Gate R or a later C
gate demonstrates the specific failure they address.

Every candidate needs:

1. a source commit and original authorship;
2. a minimal failing reproduction;
3. a clean apply record against the pinned GE Wine tree;
4. an A/B result;
5. a clear decision to keep, revise, or reject it.

Use [`../../docs/PATCH_PORTABILITY.md`](../../docs/PATCH_PORTABILITY.md) as the
triage record. GStreamer-specific commits and items classified as superseded or
conflicting must not be exported here.

## Historical promotion record

`0001-winedmo-use-destroy-params-for-wow64-demuxer.patch` corrects a
high-confidence type/layout mismatch in `wow64_demuxer_destroy()`. This file is
the earlier quarantined form and is not applied. The correction was rebased
and promoted as active final-v2 patch 3 under `patches/experimental/`; only the
entry named by `patches/series` belongs to the Alpha chain.

The fix is relevant to 32-bit media teardown and is not inherently
RTSP-specific. Recheck the exact GE-Proton11-2 Wine pin and drop the active
patch if that release fixes the bug upstream. The modified Wine file's LGPL
terms apply.

## Source-load generation and End draining

`0002-mfmediaengine-track-source-load-generations.patch` is an original,
quarantined candidate built against the then-current four-patch A2 development
snapshot. It is deliberately absent from `patches/series`; its reviewed core
behavior was absorbed into active A3 patch 4 rather than stacked as a second
copy.

The candidate gives every asynchronous MediaEngine source load an explicit
Begin/End origin and generation. A completion always drains its matching End
operation; a completion from an older generation or after shutdown shuts down
and releases its returned media source instead of installing it. The captured
generation is rechecked across notification, resolver Begin, old-source
shutdown, and topology-installation reentrancy windows. Old presentation state
is detached before the lock drop, and the latest Play/Pause intent is retained
while a source load is pending. The same candidate clears null-source pending
state and handles URL-copy allocation failure.

The broader source-error forwarding proposed in the first revision was
removed after integration review. It lacked generation identity at the media
session boundary and could clear state belonging to a newer source request.
That work remains deferred and is not part of this candidate.

See [`../../docs/SOURCE_LOAD_GENERATION_CANDIDATE.md`](../../docs/SOURCE_LOAD_GENERATION_CANDIDATE.md)
for its boundaries and
[`../../tests/source-load-generation/README.md`](../../tests/source-load-generation/README.md)
for the source-only positive and mutation-negative checks. Its revision record
is in
[`../../docs/SOURCE_LOAD_GENERATION_PATCH_HISTORY.md`](../../docs/SOURCE_LOAD_GENERATION_PATCH_HISTORY.md).
It still needs a compile as an isolated patch plus controlled reentrant source
replacement, Play/Pause, shutdown, and fault-injection runtime tests before
any promotion decision. The modified Wine files' LGPL terms apply.

## Progressive HTTP VOD promotion record

The reviewed progressive HTTP VOD candidate was source-promoted as experimental
Alpha patch 5 under `patches/experimental/`. It prefers direct FFmpeg HTTP(S)
I/O for incremental VOD playback and retains a single URLMon cached fallback
only for a distinguished direct-open failure. The source promotion makes an A3
game-test build possible; it is not a runtime or release approval.

Read
[`../../docs/PROGRESSIVE_HTTP_VOD_CANDIDATE.md`](../../docs/PROGRESSIVE_HTTP_VOD_CANDIDATE.md)
for the remaining Range, fallback, cancellation, HLS-regression, and VRChat
lobby-sync gates.
