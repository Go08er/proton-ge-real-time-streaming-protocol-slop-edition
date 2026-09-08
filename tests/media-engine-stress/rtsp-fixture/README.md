# Literal-loopback RTSP/TCP qualification slice

This directory implements the smallest useful host qualification slice of the
manifest's larger `rtsp-live-tcp` scenario. It does not claim the complete
ten-minute/five-reload matrix member. The bounded case establishes:

- one hash-locked 120-second H.264/AAC marker fixture published in real time;
- RTSP control and interleaved RTP over TCP only;
- a reviewed `rtsp://` or `rtspt://` literal-loopback MediaEngine source;
- three source generations (`load`, `replace`, `replace`), with four
  one-second checkpoints in each generation;
- simultaneous A/V discovery and advancing presentation time in every
  generation;
- fresh delivered audio samples, bytes, and nonzero decoded PCM at every
  checkpoint;
- a null MediaEngine duration and no seek action or `SEEKING`/`SEEKED` event;
- three distinct media-carrying TCP sessions overlapping the three source
  generations; and
- bounded service, connection, process, and mutable-state teardown.

UDP, RTSP-over-HTTP aliases, server loss, packet faults, ten-minute soak, and
the full five-reload gate remain separate cases. A literal forced-TCP result
must not be reported as coverage of those paths.

## Why the public TCP gate exists

MediaMTX listens only on private loopback backend port `18555`. FFmpeg first
publishes the immutable source there. Before public port `18554` exists, the
service requires one H.264 video track, one AAC audio track, at least 30
decoded video frames, two seconds of stereo 48 kHz S16 PCM, and at least 1,000
nonzero PCM units.

Only then does a bounded Python TCP relay expose port `18554`. The relay never
rewrites RTSP. It accepts only IPv4 loopback, has fixed connection, byte, and
log budgets, and retains at most 64 KiB of client control bytes in memory long
enough to count `SETUP` requests selecting `RTP/AVP/TCP` with `interleaved=`.
Only the count is recorded; no URL, header, address, filesystem path, or
wall-clock time is persisted. Native preflight readers connect directly to
`18555`; therefore they cannot masquerade as public MediaEngine consumption.
The transport scorer requires two interleaved-TCP SETUPs on each distinct
media-carrying public connection overlapping a driver generation. Together
with MediaMTX's TCP-only configuration, returned server bytes, and the
driver's decoded A/V and PCM gates, that proves the selected transport rather
than merely proving that the alias was accepted.

## Immutable inputs

Build the case without launching Wine, Proton, Steam, or a service:

```sh
case_pkg=$(nix-build tests/media-engine-stress/rtsp-fixture/default.nix \
  --argstr nixpkgsPath /nix/store/gdsajkamj68va7gl12sdqj4q562jg6rm-source \
  --argstr driverExePath /nix/store/ck8zv5pssrd0mpaphmrbqv7nr3wbm5aa-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --argstr fixtureRootPath /nix/store/znlz5y2dx00z18pkr4y7ypipcz7nqiac-vrchat-media-engine-fixtures-full \
  --no-out-link)
```

If the fixed MediaMTX archive is unavailable from the selected Nix
substituter, import the already-verified v1.19.2 workspace executable with
`nix-store --add-fixed sha256` and pass the resulting immutable file as
`--argstr mediamtxBinPath /nix/store/...-mediamtx`. The case checks its
independent executable SHA-256 and mode, then places it in the same immutable
`bin/mediamtx` package shape as the archive-backed route. It never accepts a
mutable host path.

For the scheme-only control, build the same case and payload with
`--argstr sourceScheme rtspt`. For the controlled payload ladder, also pass
`--argstr payloadRung a`, `b`, `c`, or `d`; the rung is a closed enum and its
fixture path and SHA-256 are bound into the immutable service config. The
default remains the historical `rtsp://` full-profile primary fixture.

The separate `--argstr reproductionInput observed-heavy-v1` input preserves
the measured shape of the high-rate fixture that stalled in-game: 45 seconds,
1280x720 H.264 High, an 8 Mbit/s CBR video stream, a two-second GOP, three
B-frames, and 128 kbit/s AAC. It is not another ladder rung because several
encoder properties differ together. By default the fixture builder
reconstructs that shape deterministically; passing the reviewed local capture
as `--argstr observedRtspFixturePath PATH` to the fixture build instead
hash-locks and packages its exact bytes without recording the host path.
`--argstr reproductionInput observed-midgop-v1` selects a stream-copy trim of
the same input whose first video packet is proven non-key and whose next IDR
arrives within 1.6 seconds. It isolates recovery from a mid-GOP join without
changing server, codec, or transport.

Building these case directories only composes immutable inputs. It does not
show that Wine accepted `rtspt://`, drained a payload, or produced media.

## Ten-generation drain-diagnostics case

The diagnostic-lifetime case is deliberately closed to the measured high-rate
input and the `rtspt://` spelling:

```sh
--argstr variant drain-diagnostics \
--argstr sourceScheme rtspt \
--argstr reproductionInput observed-heavy-v1
```

It performs one load followed by nine replacements in one MediaEngine process.
Each of the ten generations must reach `CANPLAY` and `PLAYING`, expose
simultaneous A/V, deliver fresh nonzero decoded PCM, and advance across four
one-second checkpoints. The transport watcher requires ten distinct
media-carrying interleaved-TCP sessions. The service remains loopback-only and
retains the existing 16-connection, 1 GiB-per-direction, and 1 MiB service-log
bounds.

The host runner also requires the closed
`--rtsp-drain-diagnostics` switch for this case. That switch selects exactly:

```text
WINEDEBUG=-all,+timestamp,+pid,+tid,+rtspdrain,warn+dmo,err+dmo,warn+mfplat,err+mfplat
```

All other runs retain the `WINEDEBUG=-all` default. This case tests whether
bounded per-source diagnostic records survive later source generations; even
a pass does not exclude the VRChat startup race.

The derivation fetches the official MediaMTX v1.19.2 Linux/amd64 archive by
SHA-256, verifies the installed executable and license hashes independently,
and includes the pinned FFmpeg binary output from the same Nixpkgs snapshot as
the fixture. The generated case binds the exact driver and scenario hashes in
both control and instrumented oracles.

For the diagnostic pause-before-replace variant, add this argument to the same
`nix-build` command:

```sh
--argstr variant pause-before-replace
```

That variant pauses generation 1 and generation 2 immediately before each
replacement. If it passes while the default running replacement fails, the
result distinguishes the MediaSession `STARTED + VT_EMPTY` early-return path
from an RTSP transport or demux-open failure. It reuses the same A/V, advancing
time, fresh PCM, connection, and teardown requirements; the exact scenario
hash records the added pause actions.

## RTSP/TCP blackhole cancellation gate

Build the focused cancellation case with:

```sh
--argstr variant blackhole-cancel
```

Its public relay mode is explicit in the immutable service config:
`server-to-client-blackhole`, with an exact 2,097,152-byte server-to-client
cutoff. Once that many bytes have been forwarded, the relay stops reading the
backend direction. It does not close either socket, discard-and-continue, or
block client-to-server control traffic. The connection must remain open until
MediaEngine shutdown produces client-driven EOF.

The scenario first proves healthy live H.264/AAC and nonzero decoded PCM, then
allows the cutoff to engage. Under that stalled read it requires:

- `Pause()` plus `PAUSE` completion within 1 second;
- resume `Play()` plus the new `PLAYING` completion within 1 second; and
- `Shutdown()` plus the terminal driver result within 5 seconds.

The transport scorer uses the common `CLOCK_MONOTONIC_RAW` basis to place the
relay cutoff after the healthy precondition and before the pause API window.
Driver records have 1 ms timestamp resolution, so records that share a
timestamp are ordered by their contiguous JSONL sequence number. API latency
limits remain timestamp-only; the sequence number is only a same-tick ordering
tiebreaker.
It also requires exactly the configured forwarded-byte count, a socket
lifetime spanning the shutdown call, and `client-eof` rather than fixture
teardown, upstream EOF, reset, or service stop. Static mutation tests reject
each of those substitutions. This is a cancellation/teardown gate only: it
does not claim packet loss recovery, reconnection, UDP, tunnel aliases,
endpoint audibility after the cutoff, or the full ten-minute live matrix.

## Finite backpressure recovery

`--argstr variant finite-hold-recovery --argstr reproductionInput
observed-heavy-v1` composes one focused recovery case. After exactly 4 MiB of
the measured high-rate stream, the public relay stops reading the server
direction for three seconds while keeping client control traffic open, then
resumes the same connection once. The scenario proves decoded A/V and nonzero
PCM before the hold, waits past it, and requires four fresh recovery
checkpoints whose media clock advances by at least 2.5 seconds.

Connection evidence records both the trigger and first resumed-delivery
timestamps. Scoring requires one interleaved-TCP session to span the
precondition, hold, resumed server bytes, recovery checkpoints, and
client-driven teardown. This is a deterministic transient-backpressure test,
not a generic network-shaping facility.

## Host scheduler-pressure qualification

The guarded host runner can execute the immutable exact-input RTSPT case with
`--scheduler-pressure scheduler-pressure-v1`. This does not rebuild or mutate
the case directory: it binds a runner-level CPU-affinity and load profile into
the input manifest and retains a calibrated
`scheduler-pressure-summary.json`. The profile is accepted only for the
reviewed captured-input, three-generation, decoded-audio qualification.

The fixture is isolated on two physical cores while the application shares
six physical cores with twelve pinned interpreted workers. The run is rejected
as a harness failure unless every worker spans the complete driver interval
and the retained CPU-time floors are met. See
`tests/host-runtime/README.md` for the exact topology, calibration, and claim
limits.

The reviewed inputs currently resolve to:

```text
MediaMTX: /nix/store/06l7w6iqaal1rqgmp1yh309wp0xa7an8-mediamtx-rtsp-test-1.19.2/bin/mediamtx
FFmpeg:   /nix/store/biq1laydwsk5vbc26qblm7n8bwz08hnm-ffmpeg-8.1.2-bin/bin/ffmpeg
FFprobe:  /nix/store/biq1laydwsk5vbc26qblm7n8bwz08hnm-ffmpeg-8.1.2-bin/bin/ffprobe
Driver:   /nix/store/ck8zv5pssrd0mpaphmrbqv7nr3wbm5aa-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe
Fixture:  /nix/store/znlz5y2dx00z18pkr4y7ypipcz7nqiac-vrchat-media-engine-fixtures-full
```

## Tests that do not launch Proton

Pure contracts and a disposable echo-gate integration test are run with:

```sh
bash tests/media-engine-stress/rtsp-fixture/check.sh
```

Some command sandboxes prohibit even `127.0.0.1` sockets; in that environment
the socket tests are explicitly skipped. On an ordinary host they must pass.

The full native service smoke starts the real MediaMTX/FFmpeg service and
performs the pre-open A/V+PCM proof. In forward mode it decodes A/V through the
public gate the configured minimum number of times (three normally and ten for
the drain-diagnostics case). For `blackhole-cancel`, it instead requires
one long native reader to stall, then verifies one exact 2 MiB cutoff with a
client-side terminal close. The finite-hold variant requires one reader to
cross the cutoff, resume delivery, and complete. All paths stop every child,
verify both ports closed, and remove the temporary directory:

```sh
python3 tests/media-engine-stress/rtsp-fixture/native_smoke.py \
  --config "$case_pkg/service-config.json" \
  --fixture-root /nix/store/znlz5y2dx00z18pkr4y7ypipcz7nqiac-vrchat-media-engine-fixtures-full \
  --fixture-manifest /nix/store/znlz5y2dx00z18pkr4y7ypipcz7nqiac-vrchat-media-engine-fixtures-full/provenance/manifest.json \
  --scenario "$case_pkg/scenario"
```

This smoke's native FFmpeg reader always uses literal `rtsp://`; a case
configured with `rtspt://` still validates its scenario bytes, but the smoke
does not exercise Wine's alias normalization. This is fixture qualification,
not Proton evidence.

## A3.8 host qualification command

The guarded host runner launches neither Steam nor VRChat, refuses AppID
`438100`, creates a fresh synthetic prefix and writable Runtime copy, and uses
a private PulseAudio null sink. It needs the existing host X11/Xwayland display
for the MediaEngine video path; that is why explicit graphics consent is still
required.

The guarded runner now dispatches `rtsp-live-v1`; the exact A3.8 command is:

```sh
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/g3jhgpsg17c0v7lf88fd2fna5s29j7ig-immutable-Proton-RTSP-on-GE11-A3.8-LiveAudio-9fad3bbe \
  --steam-runtime /nix/store/xa4f0mnckkwbqx8g6s3j6c3z1qbcq1as-immutable-steam-runtime-sniper \
  --driver-exe /nix/store/ck8zv5pssrd0mpaphmrbqv7nr3wbm5aa-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --fixture-root /nix/store/znlz5y2dx00z18pkr4y7ypipcz7nqiac-vrchat-media-engine-fixtures-full \
  --case-dir "$case_pkg" \
  --python-bin /nix/store/jxyrvv4gbpnp3ap5iy7wxwl1sg4x2x88-python3-3.14.6/bin/python3.14 \
  --pulseaudio-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pulseaudio \
  --pactl-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pactl \
  --app-id 999132 \
  --instrumentation audio-monitor \
  --build-role streaming-base-candidate \
  --case-role qualification \
  --media-kind av \
  --display :0 \
  --host-graphics-consent I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY
```

Do not run it concurrently with another case using ports `18554` or `18555`.
The run is an A3.8 qualification only; an A3.7 or frozen Proton-RTSP control
should use a fresh synthetic AppID and the identical case bytes.

The attempted A3.8 qualification produced useful diagnostic evidence rather
than a pass. The ordinary running-replacement case advanced generation 1 with
A/V, then stalled generations 2 and 3. The pause-before-replace variant
advanced all three generations with fresh nonzero PCM, but its parser caught a
transient old-clock observation. That split is the evidence for testing the
MediaSession running-source replacement path directly; it does not qualify the
full RTSP soak/fault scenario.
