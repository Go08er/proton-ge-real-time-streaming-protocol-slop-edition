# Experimental patches

This directory contains the A3.14 Wine series for the immutable
`GE-Proton11-3` release commit
`8c8003f7f5473d883fbe1bc7ac070c79955754e8`. Earlier entries retain their
historical development identities; [`../series`](../series) is the only active
order. Historical diagnostic file-number 12 and the trace-only portion of the
old file-number 13 revision remain excluded. A3.14 adds only a normally
inactive, hard-bounded drain diagnostic after the 15 integrated product
patches. [`../series`](../series) names the exact active order:

1. direct literal RTSP and the narrow RTSP-over-TCP alias;
2. FFmpeg interruption, RTSP operation deadlines, flush-safe handoff, and live
   seekability;
3. WOW64 demuxer-destroy parameter correctness;
4. asynchronous URLMon HTTP open, cached-stream seek metadata, deferred and
   coalesced VOD seek scheduling, the correct MediaSource seek flag,
   no-active-stream demux gating, paused-token resubmission after seek, and
   broader lifecycle safety;
5. direct FFmpeg HTTP(S) I/O for progressive finite VOD, with explicit
   progressive/HLS root TLS, downgrade-resistant protocol allowlists,
   HLS-only `crypto` support for AES-128 segments, Range-aware seek reporting,
   and a plain-HTTP invalid-media-only URLMon compatibility fallback. Nested
   HLS TLS requires the companion FFmpeg TLS-default patch.
   AES-128 playback also requires the separately pinned GE build-enable patch;
   the original pinned configuration has `CONFIG_CRYPTO_PROTOCOL=0`;
6. cancelled partial-read recovery, bounded A/V priming, and seek-work
   cancellation during teardown;
7. restoration of Wine's upstream post-restart sink-demand re-priming order,
   plus exact direct/deferred latest-target handling without discarding nearby
   real seeks;
8. preservation of WineDMO's established audio timestamp origin across a
   decoder flush, without suppressing the first post-flush discontinuity
   marker;
9. preservation of a running SAR audio client's logical state through a
   stop/reset/restart flush sequence;
10. file-number 11's narrow first-presentation-clock-start correction, derived
    from RTSP Wine commit `c1f40112915a6e58f631e0ef15a926c97a42710f`;
11. file-number 13's direct `SCRUBBING`/`PAUSE_PENDING` reconciliation and
    stale `PLAY_PENDING`/`WAITING` cancellation. MediaEngine autoplay remains
    property-only; its historical transition probes are not active;
12. file-number 14's separation of current-generation pending start-position
    state from refcounted old-presentation resources during source replacement;
13. file-number 15's combined direct fresh-start routing, physical clock stop,
    and outgoing-sink detachment when an immediate replacement presentation
    is already stopped;
14. file-number 17's terminal direct-network error classification and
    current-generation propagation without false EOS;
15. file-number 18's WineWayland advertisement of Wine's OpenVR and OpenXR
    Vulkan device extensions; and
16. file-number 19's opt-in, process-bounded, privacy-safe RTSP drain summary
    at the compressed-packet boundary. It changes no timeout, queue limit, or
    recovery policy.

Patches 1--8 are the protected A3.4 foundation. Patch 9 passed its A3.5 VOD
runtime gate. Historical Patch 10 attempted a paused-frame refresh but is not
active: the same tested world pauses black with the working RTSP reference, so
no project regression was established. The preserved A3.7 chain is Patches
1--9, 11, and the revised diagnostic Patch 12. A3.7 proved that live audio
reaches AVPro but a GE-owned pause/scrub race leaves `IsPaused()` true while video and
the clock run. A3.8 appends Patch 13 on an isolated branch; it does not alter
the preserved A3.7 fallback. A3.9 appends only Patch 14 from committed A3.8;
it preserves A3.8 unchanged. A3.10 appends only Patch 15 from committed A3.9.
A3.11 historically appended Patch 16 and rebased the series onto `bb1caad3`.
A3.12 appended Patch 17 on that same GE base. The successor consolidated
historical Patches 15 and 16, removed the old PCM/AVPro diagnostics, added
Patch 18, and was re-pinned to `GE-Proton11-3`. A3.14 appends diagnostic Patch
19 without reopening the 10-behavior Patch 4 monolith.
Patch 9, Patch 11, and A3.7 Patch 12 SHA-256
values are, respectively,
`9380db62ee4f9a3eb6a06a091b746ad2c8f4436b154c4abf2d99d12f19003fe2`,
`695e89c878ee2429306325ea7c46b8b6f0cf7762549892e41f6e788297ac0dfb`,
and
`24488a2d9bd99892cdf5bb6ff66c717830837b7404bd45e0fcce9443cce46b88`.
The corrected Patch 12 commit header is
`a3dd7362b047968eecaa092c9d47f37c0c172e64`.
The preserved eleven-entry A3.7 series-file SHA-256 is
`09b3e95315b0065cb1267cca2a9d0b81fe1d8d2f27abd504d038c1b4c619b3e5`;
its A3.7 series-and-content digest is
`6251d4b52515cf12f2ccba149a0a3aa08f2e2203c65475ac704ac608b536d363`.
The A3.8 Patch 13 SHA-256 is
`372b1eed33b16c02efdf7797e45fea5d442ec4d3cee2b5f156b888ac3414644c`;
the twelve-entry series-file SHA-256 is
`16ec3013b28ef96902d47967fa430d044cd19e724ce38456d7dd61f5e0df2f8a`,
and its series-and-content digest is
`4fa2fccd39a24b71fdd7f6a573aa0c7a8bb75e6ba18887ccba1876c248439b46`.
The A3.9 Patch 14 SHA-256 is
`ae221163fe601554b29887f7d7b6a0c2e975adbb8b764bbb9afa67b78f6ba295`;
the thirteen-entry series-file SHA-256 is
`7bcdbefa36522f4d2a8c19a66c90383c3cf199f4b1e35f44a986455c07c75343`,
and its series-and-content digest is
`4ca13e7136a58f86a3ded082dd4c393afa39b37b738d4de350eb2f62b1475d39`.
Patch 14 is project-authored under Wine's LGPL-2.1-or-later boundary. It
addresses a stock GE/Wine MF running-replacement gap retained at Patch 4's
presentation-cleanup site; it is not an RTSP/FFmpeg transport change. A3.9 was
built and independently verified, but its progressive-HTTP replacement control
then exposed a second stock Wine MediaSession immediate-topology state gap.
The replacement source was already recorded stopped while the session remained
started, so the restart path stopped an unsubscribed never-started source and
waited indefinitely.

The A3.10 Patch 15 SHA-256 is
`5134c124f8d3b48c218c4b862343d86ec09389ed512c7870271e664e069abbe1`;
the fourteen-entry series-file SHA-256 is
`9caf1d762422cb7d50577fae8a978e41680d4c0eed62648cbc935f3d54b715f1`,
and its series-and-content digest is
`324b37063ef0df4688684af7f3cf8c99a38cdc76009a26d43325a65a8758daa9`.
Patch 15 is project-authored under Wine's LGPL-2.1-or-later boundary. Source,
model, compilation, and both exact artifact verifiers passed. The contained
replacement control proved that it starts and advances the replacement media,
but the still-running presentation clock made new sinks reach `STARTED` before
preroll completed; no `MESessionStarted`/`PLAYING` followed. A3.10 is a frozen
failed-gate diagnostic, not a candidate. Fresh isolated `a310r1` preparation
and the full preparation verifier passed;
the prepared-source record SHA-256 is
`0a86837c3198be87e517fd0224132480397e29b2caf55cae309bf9158e330c07`,
and the independently repeated source-audit output SHA-256 is
`accd7e0af77b88d13a16cd7be1f55796b6da84be49677f4f463a1f6a9a4eb126`.
Historical A3.7 r1 source passed strict replay, preparation, model suites, source/series
audits, and the full preparation verifier. Its build then failed solely in the
x86_64 `dlls/mfmediaengine/main.c` compiler gate: five inserted flag snapshots
used `DWORD` where `engine->flags` and `%#x` require `unsigned int`, producing
eight `-Werror=format` diagnostics. No artifact or Steam mutation resulted.
That r1 patch SHA-256 was
`3576142607aef83704d33032f6474096885ec25353db9ed5859acf7750ebc1ad`,
and its series-and-content digest was
`b700d09a26c099c78f153df94291548e39f61c34aac7815598a850c1b293e64c`.
The corrected A3.7 patch produces a byte-identical postimage under Git and
strict GNU replay, and its updated PCM-probe and AVPro-state audits/models
pass. Fresh r2 full preparation and verification passed with prepared-source
record SHA-256
`ae0fa75b7f14defecdb4c5b57c86295ac827fac6ce52bee3d40619d9e9995f9c`.
Both x86_64 and i386 `dlls/mfmediaengine/main.c` compiled under `-Werror`, the
build wrapper exited 0, and integrated plus independent exact-artifact
verification passed. The `572303414`-byte archive has SHA-256
`a4460e7bfe9b40ec0510eab3d7944da0a9f1a269aa3c64a782f0c616214042b6`;
its `1561496018`-byte tree has 8,860 nodes and 6,089 regular files. It is only
+28,997 archive bytes and +17,452 tree bytes over exact A3.6. The artifact was
preserved locally and atomically staged in Steam while prior tools were
preserved, compatdata remained untouched, and a fresh-log boundary was
established. Its later private runtime completed the diagnosis described
above: valid live PCM reached AVPro, seeking remained false, and the GE
pause/scrub race stranded public paused state while video and time advanced.
A3.7 remains diagnostic rather than a fix, and nothing was published.
Historical A3.6 Patch 12 SHA-256 was
`d777ef8b68555b2d3cb70af465f4d0f094fdfbeeb39c281c050e6f2da54622b8`,
and its series-and-content digest was
`0a1822231dd7a7f9c97d9ab32a6ce33884e68d5d68db04c11d6daffaaac41f7d`.
Historical Patch 10 SHA-256 is
`55fcb660c391b2a969ed5ef03c488254da5a4800ef085380d2cb927f4c258a62`.

Patch 11 is deliberately narrower than the RTSP Wine source sequence. GE does
not contain the adjacent broader buffering-state patch, so A3.7 retains A3.6's
unchanged logic which tracks only
whether this Media Session clock has really started, normalizes an undefined
first current-position start to zero, clears that state on successful explicit
Stop/Close, and prevents buffering-stop from starting a never-started clock.
Patch 12 is diagnostic rather than a playback workaround. A3.6 proved that the
tested live AAC decoder produced valid, non-silent PCM. It also showed zero at
the application effect output for both the silent livestream and the audible
VOD. Reverse engineering the exact tested AVPro binary then established that
its AudioGrabber intentionally captures input into a private FIFO and zeroes
its downstream effect output to avoid duplicate audio.

A3.7 therefore moves the second bounded `pcmprobe` observation to the effect
input immediately before `IMFTransform::ProcessInput()` and records the actual
ProcessInput/backpressure/queue outcome. Its separate `avprostate` channel
records `IsPaused()`, real seeking, deferred seeking, and state/target values
before and after the calls which can change them. The AVPro silence gate is
exactly `IsPaused() || IsSeeking()`; pinned GE reports `IsSeeking()` for either
its real-seek or deferred-seek flag. Each `avprostate` call snapshots its data
under `engine->cs`, releases that lock, and only then emits TRACE, so the
high-frequency state logging does not hold the MediaEngine lock through file
I/O. `pcmprobe` unlocks its sample before the real ProcessInput call and reports
the result afterward. Both channels are normally disabled and alter no media
buffer or control decision.

Enable only narrow `+pcmprobe,+avprostate` for the focused private run. The
aggregates, timestamps, and opaque pointer identities are private even though
the diagnostic emits no raw PCM, URL, hash, or endpoint. Do not use
`WINEDEBUG=all`, which also enables these diagnostics and unrelated inherited
TRACE channels.

The first six patches passed strict A3.2 source preparation against the pinned
Wine media model. Patch 5 also passed exact replay, structural/model tests, and
targeted x86_64/i386 WineDMO compilation and linking. Final Patch 7 is 43
insertions and 3 deletions with SHA-256
`99536ea03fc22e1b1a835a231a2c6550bd436ca757d4c61be36d3c39ee6992ce`;
the historical seven-patch series-and-content digest is
`fde22cfb1f18f7d1c723599820c4095cfce1f06c70222c1df8b6a59d491c5b13`.
A clean `a33r2f` source preparation, full `verify-preparation`, and all 28
then-current VOD-control model tests passed before the seven-patch A3.3 build.

A3.3 compiled and passed its artifact gates, then began the focused game test
with continuous video, audio, and per-second time. Fifty slider updates over
0.614 seconds correctly coalesced to one backend seek, and the source/selected
streams restarted successfully. Continuous post-seek playback still failed:
the direct source supplied compressed AAC near `780.260136` seconds, but stock
GE WineDMO reset the decoder's established timestamp mapping and emitted audio
from zero. Media Session mark-in consequently discarded that output before it
could reach the resampler or SAR, while video appeared only in small bursts.
This remains the failed A3.3 runtime result; Patch 7 did not pass the complete
seek-recovery gate.

Patch 8 is the direct stock-GE WineDMO correction. It decouples the
first-output marker from `audio_output_pts_adjust` and preserves an established
origin across decoder flush. Its SHA-256 is
`4148b3b11718ecf7110e49caa9470fcfb42a9d81560816714e48cfdbfe0e2777`.
The fresh eight-patch `a34r1` replay and source preparation, including the full
preparation verifier, pass, and all 37 current VOD-control model cases pass.
The subsequent A3.4 redist build completed, and both the integrated verifier
and an independent exact verifier passed against 8,860 nodes and 6,089 regular
files. The verified archive is 572,204,479 bytes with SHA-256
`634143cbf71febafcf0fd8fddd8843499418b7f7590325738ca743764166b931`
and SHA-512
`fe727d04828a0bccaba7b0e5fec8fc6a4c9b273a3e3d0f04f25e6fa113f51ad3b0f91a7ca0b295f5df32fe3a5ec176f8a55c427c725aa631e03acb82cc1b4d10`;
the exact tree is 1,561,468,960 bytes. Compared with A3.3, Patch 8 adds 4,283
archive bytes and 12,319 tree bytes.

The focused A3.3/A3.4 code-section comparison changed only the i386 and x86_64
Unix WineDMO modules. The corresponding Windows WineDMO modules,
both-architecture MF and MediaEngine DLLs, and Unix plus Windows Wine-Wayland
modules retained identical `.text` sections. Both changed Unix modules retain a
non-executable `GNU_STACK` marked `RW` and include `GNU_RELRO`. Steam installation
and its fresh-log boundary passed before launch. The subsequent focused
progressive-VOD runtime reduced 313 callbacks to nine real backend seeks,
including 306 stress callbacks to four; every real seek restored exact A/V in
about 0.20--0.66 seconds. Startup, pause/resume, multiplayer synchronization,
native-Wayland XR operation, and clean exit passed without a persistent stall,
decoder error, graphics-device loss, XR failure, or crash. RTSP, HLS, and live
input remain untested, and the baseline has not been published.

Pause shows black while MediaEngine has no new sample to transfer; the retained
presentation sample is not flushed and resume supplies a frame in 56--75 ms.
A3.5 Patch 10 tested a retained-frame-as-new opportunity without altering the
protected A3.4 source or artifact. It did not change the tested result, and the
same world also pauses black with the working RTSP reference. The experiment is
therefore preserved as A3.5 history but removed from A3.6.

Exact A3.4 reproduction must use `refs/archive/runtime-good/a3.4` at commit
`db94285ab3ab7b2b9714cc6dcc1aa81cdf343e34`, or the verified artifact recorded
above. The A3.4 config alone is insufficient in the current checkout because
the branch-global `patches/series` now names twelve patches.

Fresh A3.5r3 ten-patch replay, preparation, the preparation verifier, 37
VOD-control models, 19 polish-control models, and structural/source/security
audits pass. Fresh 16/8/2 compilation completed. The initial verbose upstream
archive-wrapper invocation was interrupted with exit 143 and preserved; a
quiet cached `--resume` reran canonical redist finalization and exited 0.
Integrated and independent exact verifiers each matched 8,860 nodes and 6,089
regular files and passed the source, provenance, privacy, file-type, mode,
permission, launcher, FFmpeg, scheduler, and zero-WineGStreamer gates. The
accepted `Proton-RTSP-on-GE11-A3.5-Polish-9fad3bbe` archive is `572244484`
bytes with SHA-256
`fa5c16989b157368dda2be377c7cfc3668c69d9ad32c1500b2bc4ee7c5564ca0`;
the tree is `1561472921` bytes, only `40005` archive bytes and `3961` tree bytes
above exact A3.4. Only the i386/x86_64 `mf.dll` and `mfmediaengine.dll` `.text`
sections changed; both architectures' WineDMO and Wine-Wayland DLL/SO outputs
stayed identical. The changed DLLs retain identical imports and PE flags i386
`0x150`, x86_64 `0x170`. With Steam, VR, and Wine closed, the tree matched its
archive before and after atomic installation under the exact A3.5 compatibility-
tool identity; A3.4 and five reference tools were preserved. The prior AppID
log was moved privately with mode `0600`, the canonical path was cleared, and
the focused guide was copied to Downloads. The later focused run passed VOD
startup, seeks, stress, pause/resume, multiplayer synchronization, and clean
exit. Patch 9 removed the invalid running-client Reset failures. Patch 10 is
withdrawn for the reference-behavior reason above. A live input displayed video
but had no audible AAC; that unresolved result motivates A3.6.

A3.6 uses identity `Proton-RTSP-on-GE11-A3.6-LiveClock-9fad3bbe` and a global
16, Make 8, Ninja 2 build cap. The r1 attempt was stopped and invalidated after
GNU `patch` accepted Patch 12 while Git rejected malformed hunk metadata. The
r2 preparation failed closed before compilation when strict GNU `patch`
rejected the intermediate 5/12 EOF hunk which Git accepted. Fresh r3 preparation
with the final 6/13 hunk passed both parser applications, all preparation gates,
and the full `verify-preparation` check; its build was cleanly interrupted with
status 130 solely for the scheduler migration and is not a candidate. Fresh r4
preparation, the hard-16/8/2 build, canonical resume finalization, and both
exact artifact verifiers pass under normal CPU priority, idle-class I/O, and
`PACKAGE_RELEASE=0`. The accepted `572274417`-byte archive has SHA-256
`23d9e587c6e325c07085494cc9dc70b571d75e053229973c24ddd262a95de36f`.
It is installed and archive-verified and remains unpublished. The completed
focused run preserved ordinary-YouTube playback but did not restore audible
livestream audio. All 48 bounded AAC-decoder observations across the audible
VOD and two live topology groups contained valid nonzero PCM. The
application-effect output was zero in all three groups, including the audible
VOD, so that output is not a valid repair target and must not be bypassed.

The next temporary probe belongs on the application-effect input immediately
before `ProcessInput()`, with explicit HRESULT accounting. Patch 11 remains
unproven by this narrow run because presentation-clock TRACE was not enabled;
A3.6 is a diagnostic result rather than a release candidate. Normal runs
should omit `+pcmprobe`, and broad TRACE selectors must be avoided.

The snapshot is not GE-Proton11-2; every patch must be freshly reviewed and
replayed against that exact release when it appears.

The rejected `a33r1` draft was a broader 90-insertion/6-deletion design with a
three-second duplicate rule and restart request holdoff; its build was
interrupted and that design is not active. The first `a33r2` preparation then
failed safely when GNU `patch` rejected a context-free pure-insertion hunk.
The final patch retains the narrow logic, adds a useful declaration-line
comment so the hunk has context, and passes strict GNU `patch` and `git apply`
gates before the successful fresh `a33r2f` preparation.

No restart request gate or retry is present. A code-observed asynchronous
restart window remains a future diagnostic because the retained trace showed
all wrong-state pulls before the transform flush and none afterward; it is not
a blocker for the first game candidate.

Patch 8 deliberately does not absorb three separate follow-ups:

- A3.3's post-seek audio inputs carried no explicit generic discontinuity flag.
  Another source may use `WINEDMO_SAMPLE_FLAG_DISCONTINUITY` after seek and
  still establish a new origin; capture that case before changing the generic
  discontinuity rule.
- WineDMO's resampler currently returns `S_OK` from a stubbed
  `MFT_MESSAGE_COMMAND_FLUSH` path without flushing its backend. Implement and
  validate that operation as an isolated correction.
- A3.4's SAR calls `IAudioClient_Reset()` while its client is running and logs
  `AUDCLNT_E_NOT_STOPPED` on each effective seek. A3.5 Patch 9 implements the
  independently scoped SAR stop/reset/restart correction; it does not reorder
  the Media Session or hide renderer state management inside Patch 8.

Requirements:

- no GStreamer or WineGStreamer dependency;
- preserve HTTP(S) HLS routing and non-HTTP byte-stream behavior while
  hardening HLS root trust and mixed-content policy;
- prefer direct FFmpeg I/O for ordinary HTTP(S) finite media, while retaining
  URLMon only as the documented one-shot plain-HTTP/invalid-media full-cache
  compatibility fallback;
- minimal FFmpeg protocol enablement;
- bounded RTSP operation timeouts and deterministic RTSP-over-TCP first;
- explicit tests for shutdown, server loss, later manual reopen, seeking,
  audio-only, and EOS;
- no secrets or live signed URLs in patches, fixtures, or logs.

The current worker join remains
`WaitForSingleObject(..., INFINITE)` after cancellation and no reconnect/backoff
is implemented. Neither limitation may be hidden by the operation-deadline
work; both are called out in the implementation contract and test plan.
The successful direct HTTP route no longer intentionally downloads the whole
response before playback. Its FFmpeg interrupt deadlines are cooperative, not
hard wall-clock guarantees. The plain-HTTP invalid-media URLMon fallback still
completes a full cached download before FFmpeg opens the media, and its
scheme-handler operation has no independent cancellation or timeout. HTTPS,
TLS, protocol-policy, and network failures never enter that fallback.
