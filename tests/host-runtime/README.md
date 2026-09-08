<!-- SPDX-License-Identifier: BSD-3-Clause -->

# Guarded host MediaEngine runner

This runner is the practical GPU/display counterpart to the isolated Nix VM.
It runs only the standalone MediaEngine driver. It does not start Steam or
VRChat, discover a Steam library, or write the real `438100` prefix.
The Steam Runtime waits around each process boundary. A harmless
`getcompatpath` invocation first performs Proton's normal managed-prefix setup,
then `runinprefix` sends the driver straight to Wine's MediaEngine. This
deliberately bypasses Proton's built-in `steam.exe`; older RTSP launchers assert
there when no real Steam client exists, even though the media test itself does
not use Steam APIs. Both comparison builds use this exact sequence.

Use it when a test needs the host graphics stack. Keep system-changing fault
injection, certificates, routing, firewall, and service tests in the Nix VM.
`run-contained-host-stress.sh` places the complete runner inside separate user,
PID, network, IPC, UTS, and mount namespaces. The network namespace contains
only loopback and no route to the host or Internet. Proxy variables are absent
from the cleared runtime environment so both GStreamer and FFmpeg can reach the
declared loopback fixture directly; neither can route beyond that namespace.
Every scenario source is also validated as the declared `127.0.0.1` fixture.

## Safety contract

- Every software/media input must be an explicit immutable `/nix/store` path.
- AppIDs are limited to `990000..999999`; `438100` is rejected independently.
- The outer Bubblewrap boundary is mandatory; direct execution of
  `run-host-stress.sh` is rejected.
- HOME, cache, temporary files, compatdata, the fake Steam root, Pressure
  Vessel variable data, and a writable Runtime copy live below one new private
  outer `/var/tmp/rtsp-media-outer.*` directory.
- The host root is mounted read-only. `/home`, `/root`, `/srv`, `/mnt`,
  `/media`, all of `/run`, and both temporary directories are replaced by
  empty or run-private mounts before Pressure Vessel starts. Private `/run`
  contains only exact Nix-store symlinks for `current-system` and the 64/32-bit
  graphics-driver closures; host service sockets are absent. The inner runtime
  has writable access only to private mounts.
- CPython 3.11 or newer is supplied by exact store path. The Runtime's bundled
  Python is not selected accidentally.
- Audio uses a private PulseAudio server and null sink. It cannot emit to or
  record from the user's normal audio server. Optional endpoint observation
  records only that null sink's monitor.
- An existing X11/Xwayland display is exposed only after the exact consent
  token is supplied. An optional Xauthority file is copied into run state.
  This socket is a deliberate capability: an X11 client can interact with the
  current X session. The boundary prevents filesystem and network writes; it is
  not an isolation boundary against a malicious graphical client.
- The containment manifest records each exposed DRI/NVIDIA device by relative
  name and kernel major/minor identity; audio, input, and KFD devices remain
  absent.
- The selected fixture validator rejects any `load`/`replace` URL that is not
  its one configured IPv4-loopback media endpoint.
- On exit or interruption, tracked children and any same-user process tied to
  the run directory are terminated. Prefix/runtime/cache/audio state is then
  deleted. Logs and path-free input/evidence manifests remain under `results/`.

The retained raw Proton log can still contain low-level system details. Review
it before sharing. The two canonical manifests contain hashes and fixed labels,
not local paths or usernames.

The current outer root is read-only rather than minimal, so host paths outside
the explicit masks can still be read. Common user and game-library roots are
masked, including `/home`, `/mnt`, `/media`, and all of `/run`; an unusual custom
mount elsewhere remains readable but cannot be modified or reached over the
isolated network. Treat the harness and tested binaries as trusted graphical
clients because the deliberately exposed X11 socket is still a powerful
session capability.

Before any Proton run, validate only the containment boundary:

```sh
tests/host-runtime/run-contained-host-stress.sh \
  --containment-preflight-only \
  --display :0 \
  --python-bin /nix/store/jxyrvv4gbpnp3ap5iy7wxwl1sg4x2x88-python3-3.14.6/bin/python3.14
```

That command neither starts Proton nor creates a prefix. A successful preflight
records its private diagnostics below `/var/tmp/rtsp-media-outer.*`.

## Captured-input scheduler-pressure profile

`--scheduler-pressure scheduler-pressure-v1` adds one bounded host-behavior
model to the exact captured-input RTSPT qualification. It is intentionally
case-specific: the runner rejects this profile unless the immutable service
configuration selects the reviewed 46,486,179-byte input, `rtspt://`, the
three-generation running-replacement scenario, decoded-audio monitoring, and
the streaming candidate role.

The profile fails closed unless the host exposes the reviewed eight-core,
16-thread topology with adjacent SMT sibling IDs. The application process tree
and twelve interpreted SHA-256 workers share logical CPUs 0-11; the loopback
fixture, MediaMTX, and FFmpeg inherit logical CPUs 12-15. All workers must be
ready before the driver starts and remain alive until it exits. The retained
summary requires at least five seconds of measured driver time, at least four
aggregate CPU equivalents from the workers, and at least 20 percent of one
logical CPU from every worker. Those floors use conservative lower bounds:
each worker's measured CPU time is reduced by the complete wall-clock interval
before driver start and after driver exit. Startup or teardown work therefore
cannot satisfy the playback-pressure gate. A missing worker, changed affinity
map, short run, weak pressure, or unsealed summary is a harness failure rather
than a media result.

This approximates CPU scheduling contention on the application's own cores
while preserving enough isolated capacity for the deterministic loopback
publisher. It does not model GPU contention, a headset render loop, AVPro's
MediaEngine creation flags, or application calls that transfer decoded video
frames. The current standalone driver does not call `OnVideoStreamTick()` or
`TransferVideoFrame()`, so changing its snapshot timing would not faithfully
model frame-consumption cadence without rebuilding the driver.

The first retained run of this profile passed on immutable A3.13 with
synthetic AppID `999312`. The driver completed all three generations in
15.240 seconds with advancing A/V and fresh nonzero PCM. The workers spanned
27.596 seconds of the complete application process, supplied a conservative
aggregate lower bound of 11,673 permille, and supplied at least 955 permille
per worker. Transport scoring matched three generations to three
media-carrying interleaved-TCP connections. This excludes only the declared
CPU scheduler-pressure profile as a sufficient cause in the standalone
driver path.

## Bounded RTSP drain diagnostics

`--rtsp-drain-diagnostics` is a closed opt-in accepted only with the immutable
`rtsp-live-tcp-drain-diagnostics` case. That case is itself fixed to
`rtspt://`, the reviewed high-rate capture, ten source generations in one
MediaEngine process, and decoded-audio monitoring. The switch selects exactly:

```text
WINEDEBUG=-all,+timestamp,+pid,+tid,+rtspdrain,warn+dmo,err+dmo,warn+mfplat,err+mfplat
```

Without the switch the runner remains at `WINEDEBUG=-all`; it accepts no
caller-supplied debug-channel string. The case makes later generations and a
successful source observable without broad MF tracing. It remains a bounded
contained diagnostic check, not a reproduction or resolution of the game-only
startup race.

## Endpoint-audio continuity mode

`--instrumentation endpoint-monitor` observes audio after MediaEngine's audio
renderer and WinePulse without inserting the driver's decoded-audio effect.
The runner starts an exact Nix-store `parec` against only
`rtsp_host_null.monitor`, records raw 48 kHz stereo `s16le` PCM, and maps the
capture to the driver's `CLOCK_MONOTONIC_RAW` checkpoints. Every declared live
label scores the 800 ms before its first checkpoint as four time buckets.
Later checkpoints with that label bridge the complete same-generation
interval using buckets no longer than 200 ms. Every bucket must contain a
signal for at least 90 percent of its frames. Using only audio at or before a
checkpoint keeps the final window independent of immediate replacement or
shutdown. The private null sink can take about two seconds to deliver its first
recorder block even when `parec` requests 50 ms latency. The runner therefore
waits at most five seconds for complete PCM, records the observed frame offset
before taking a `CLOCK_MONOTONIC_RAW` readiness timestamp, and scores only
frames after that anchor. Pre-anchor recorder startup is not mistaken for a
stream discontinuity. The post-anchor PCM duration must still calibrate to the
host clock within 150 ms in either direction. A conservative clock mapping
assigns every positive suffix gap before the scored suffix; a small negative
gap from bytes racing the size observation receives no earlier shift. In both
cases a window cannot borrow later shutdown audio. A startup blip, sparse
periodic clicks, a 200 ms inter-checkpoint dropout, or a displaced capture
therefore fails.

This proves dense delivery into the private PulseAudio sink across each
declared same-generation observation interval. It does not cover the
replacement transition before the next generation's first window, prove
physical-speaker audibility, or prove VRChat/AVPro's application-side audio
capture. The raw PCM is retained as private synthetic-fixture evidence and is
hash-bound by the current schema 5; do not publish it as part of a sanitized
summary.

The literal-RTSP cases can select this mode directly by replacing
`--instrumentation audio-monitor` and adding:

```text
--instrumentation endpoint-monitor
--parec-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/parec
```

Healthy-HLS case inputs bind their observation mode. Compose a new immutable
case with `--argstr instrumentation endpoint-monitor`; do not reuse an
`audio-monitor` case directory under a different mode. The endpoint oracle and
its fail-closed pure tests are implemented, but no Proton runtime result is
claimed until a guarded run produces a passing
`endpoint-audio-summary.json`.

## Healthy live-HLS topology qualifications

The healthy adapter can publish either the muxed H.264/AAC playlist or the
separate audio/video master at `127.0.0.1:18082`. Both cases require eight
one-second checkpoints, at least five seconds of advancing media time, null
live duration, simultaneous A/V discovery, no media or HTTP errors,
request-relative startup within ten seconds, and bounded teardown. The
case-bound observation mode requires either fresh nonzero decoded PCM or
continuous private-endpoint PCM at every checkpoint.

The separate case additionally requires successful master, audio-playlist,
video-playlist, audio-segment, and video-segment requests. Both child tracks
must be requested during the observed playback interval, and at least three
playlist generations must match across audio and video. The fixture selects
both child playlists under one publisher lock, so these matching generations
come from one atomic A/V publication rather than independently advancing
sources.

The first muxed A3.7 and A3.8 runs both produced healthy advancing A/V and
fresh nonzero PCM. Each made exactly six mux-segment GETs during the eight
second observation. The old
`/nix/store/4d7wp5lxl0dvdwasmig8fd558z1i4giz-rtsp-media-stress-live-hls-muxed-case`
incorrectly required seven, so its transport-only rejection was a harness
error. There is no demonstrated A3.7
negative signature; both builds are ordinary healthy qualifications. The
rebuilt mux case uses the observed, cadence-consistent minimum of six.

The following immutable cases are the historical schema-3 audio-monitor
inputs:

```text
/nix/store/anx8jnhw5i9hp59kzcy3xlggf8yqaf2s-rtsp-media-stress-live-hls-muxed-case
/nix/store/c3d5ya7par3dpdpm7fwp294ckcp7389p-rtsp-media-stress-live-hls-separate-case
```

They bind the pre-schema-4 adapter hash and are retained only with their
historical evidence; rebuild either source before a new run with the current
adapter. All four corrected A3.7/A3.8 muxed and separate qualifications passed before
the outer containment boundary was added. Those traces remain useful media
evidence, but they do not attest the new boundary. Run the commands again for
contained-run evidence. Run them separately from the repository root; every
case binds the same fixed loopback port. Replace `:0` only if the intended local
X11/Xwayland display differs.

```sh
common=(
  --steam-runtime /nix/store/xa4f0mnckkwbqx8g6s3j6c3z1qbcq1as-immutable-steam-runtime-sniper
  --driver-exe /nix/store/x34hzydwdxwwl9fvkzb9pd2jba69b913-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe
  --fixture-root /nix/store/2yj0qpby5b10vjslmicdvn8r5hk7ah9g-vrchat-media-engine-fixtures-full
  --python-bin /nix/store/jxyrvv4gbpnp3ap5iy7wxwl1sg4x2x88-python3-3.14.6/bin/python3.14
  --pulseaudio-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pulseaudio
  --pactl-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pactl
  --instrumentation audio-monitor
  --case-role qualification
  --media-kind av
  --display :0
  --host-graphics-consent I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY
)

# A3.7, muxed HLS
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/r8v3h1857h3ygpy55ack6jpg8n3zygyc-immutable-Proton-RTSP-on-GE11-A3.7-AudioGate-9fad3bbe \
  --case-dir /nix/store/anx8jnhw5i9hp59kzcy3xlggf8yqaf2s-rtsp-media-stress-live-hls-muxed-case \
  --app-id 999133 \
  --build-role frozen-regression-control \
  "${common[@]}"

# A3.8, muxed HLS
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/g3jhgpsg17c0v7lf88fd2fna5s29j7ig-immutable-Proton-RTSP-on-GE11-A3.8-LiveAudio-9fad3bbe \
  --case-dir /nix/store/anx8jnhw5i9hp59kzcy3xlggf8yqaf2s-rtsp-media-stress-live-hls-muxed-case \
  --app-id 999134 \
  --build-role streaming-base-candidate \
  "${common[@]}"

# A3.7, separate audio/video HLS
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/r8v3h1857h3ygpy55ack6jpg8n3zygyc-immutable-Proton-RTSP-on-GE11-A3.7-AudioGate-9fad3bbe \
  --case-dir /nix/store/c3d5ya7par3dpdpm7fwp294ckcp7389p-rtsp-media-stress-live-hls-separate-case \
  --app-id 999135 \
  --build-role frozen-regression-control \
  "${common[@]}"

# A3.8, separate audio/video HLS
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/g3jhgpsg17c0v7lf88fd2fna5s29j7ig-immutable-Proton-RTSP-on-GE11-A3.8-LiveAudio-9fad3bbe \
  --case-dir /nix/store/c3d5ya7par3dpdpm7fwp294ckcp7389p-rtsp-media-stress-live-hls-separate-case \
  --app-id 999136 \
  --build-role streaming-base-candidate \
  "${common[@]}"
```

Each invocation creates a fresh prefix, private null-audio server, cache,
Runtime copy, synthetic AppID, and fake Steam root. It starts neither Steam nor
VRChat and never opens AppID `438100`.

Rebuild either immutable case only when the driver, adapter, publisher, or
case contract changes. Select `mux` or `separate` explicitly:

```sh
nix-build tests/host-runtime/live-hls-case.nix \
  --argstr nixpkgsPath /nix/store/gdsajkamj68va7gl12sdqj4q562jg6rm-source \
  --argstr driverExePath /nix/store/x34hzydwdxwwl9fvkzb9pd2jba69b913-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --argstr fixtureRootPath /nix/store/2yj0qpby5b10vjslmicdvn8r5hk7ah9g-vrchat-media-engine-fixtures-full \
  --argstr source separate \
  --argstr instrumentation audio-monitor \
  --no-out-link
```

Use `--argstr instrumentation endpoint-monitor` for a private-endpoint case
and pass the matching runner mode plus exact `parec` path.

## Literal RTSP/TCP A3.8 qualification

This shorter live case exercises the actual RTSP route without claiming the
separate ten-minute, five-reload, UDP, tunnel-alias, or fault matrix. MediaMTX
publishes the hash-locked H.264/AAC fixture on private backend loopback port
`18555`. A bounded TCP gate exposes literal
`rtsp://127.0.0.1:18554/fixture` only after native video decode and nonzero PCM
have passed. The gate records no URLs or addresses. Its scorer requires three
distinct media-carrying TCP sessions overlapping the driver's `load`,
`replace`, and `replace` generations.

Each generation must expose simultaneous A/V, advance for at least 2.5
seconds, and deliver fresh nonzero decoded PCM at all four one-second
checkpoints. Live/nonseekable behavior is explicit: duration remains null, the
scenario contains no seek, and `SEEKING`/`SEEKED` are forbidden. The resulting
input manifest records `rtsp-interleaved-tcp-ipv4-loopback-only`, not HTTP.

Build the immutable case, then run A3.8. The native service/gate smoke passed.
A3.8 also executed both bounded replacement controls: the ordinary running
case advanced generation 1 with A/V, then stalled generations 2 and 3; the
pause-before-replace variant advanced all three generations with fresh nonzero
PCM but exposed a transient old-clock observation. These are diagnostic
runtime results, not a full RTSP qualification.

The refreshed A3.11 candidate has now run both cases with the current schema-3
runner, Runtime 4, driver SHA-256
`70ba4f15f8d96a0890555ff1847050d7a45ebe1efe6479c42dd62a0cb22e808a`,
and tool payload SHA-256
`30c81016ae0496a24600d9d32c457080b6f36a271046838ad557c0031cc89902`.
The ordinary running case used synthetic AppID 999203 and the
pause-before-replace case used 999204. Both passed three advancing A/V
generations, fresh nonzero decoded PCM, no stale event, and three matched
media-carrying RTSP/TCP connections. The paused discriminator emitted its two
expected `PAUSE` events and resumed each replacement generation. These are
focused literal-RTSP replacement passes, not the planned ten-minute live
qualification, endpoint-audibility proof, or a VRChat result. Steam discovery
and Steam writes were disabled and AppID 438100 was inaccessible.

```sh
case_pkg=$(nix-build tests/media-engine-stress/rtsp-fixture/default.nix \
  --argstr nixpkgsPath /nix/store/gdsajkamj68va7gl12sdqj4q562jg6rm-source \
  --argstr driverExePath /nix/store/x34hzydwdxwwl9fvkzb9pd2jba69b913-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --argstr fixtureRootPath /nix/store/2yj0qpby5b10vjslmicdvn8r5hk7ah9g-vrchat-media-engine-fixtures-full \
  --no-out-link)

tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/g3jhgpsg17c0v7lf88fd2fna5s29j7ig-immutable-Proton-RTSP-on-GE11-A3.8-LiveAudio-9fad3bbe \
  --steam-runtime /nix/store/xa4f0mnckkwbqx8g6s3j6c3z1qbcq1as-immutable-steam-runtime-sniper \
  --driver-exe /nix/store/x34hzydwdxwwl9fvkzb9pd2jba69b913-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --fixture-root /nix/store/2yj0qpby5b10vjslmicdvn8r5hk7ah9g-vrchat-media-engine-fixtures-full \
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

The native-only command, exact protocol boundary, pinned MediaMTX provenance,
and full oracle explanation are in
[`../media-engine-stress/rtsp-fixture/README.md`](../media-engine-stress/rtsp-fixture/README.md).

## Progressive VOD example

Run from the repository root with Steam and VRChat closed only if you want to
remove unrelated GPU load; the script itself does not contact Steam. Replace
`:0` if the intended local X display differs.

```sh
tests/host-runtime/run-contained-host-stress.sh \
  --proton-tool /nix/store/r8v3h1857h3ygpy55ack6jpg8n3zygyc-immutable-Proton-RTSP-on-GE11-A3.7-AudioGate-9fad3bbe \
  --steam-runtime /nix/store/xa4f0mnckkwbqx8g6s3j6c3z1qbcq1as-immutable-steam-runtime-sniper \
  --driver-exe /nix/store/x34hzydwdxwwl9fvkzb9pd2jba69b913-media-engine-stress-driver/bin/media-engine-stress-x86_64.exe \
  --fixture-root /nix/store/2yj0qpby5b10vjslmicdvn8r5hk7ah9g-vrchat-media-engine-fixtures-full \
  --case-dir /nix/store/326gpysy7kkgdx55psa98y1fwadc6bkv-rtsp-media-stress-vod-seek-smoke-case \
  --python-bin /nix/store/jxyrvv4gbpnp3ap5iy7wxwl1sg4x2x88-python3-3.14.6/bin/python3.14 \
  --pulseaudio-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pulseaudio \
  --pactl-bin /nix/store/504am7li7cmw4vvcmiz268z2cgw5rk3p-pulseaudio-17.0/bin/pactl \
  --app-id 999120 \
  --instrumentation control \
  --build-role frozen-regression-control \
  --case-role qualification \
  --media-kind av \
  --display :0 \
  --host-graphics-consent I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY
```

For the A3.8 comparison, change only these arguments so the experiment remains
cold and independently identified:

```text
--proton-tool /nix/store/g3jhgpsg17c0v7lf88fd2fna5s29j7ig-immutable-Proton-RTSP-on-GE11-A3.8-LiveAudio-9fad3bbe
--app-id 999121
--build-role streaming-base-candidate
```

For the paired decoded-audio observation, repeat with a new synthetic AppID and
`--instrumentation audio-monitor`. The selected case's instrumented oracle is
used automatically. A declared `negative-control` is accepted only in that
instrumented mode.

Successful and failed invocations print a retained path such as:

```text
/var/tmp/rtsp-media-host.ABC12345/results
```

Schema-5 `input-manifest.json` additionally binds the scheduler-pressure
profile and helper. It retains schema 4's binding of the endpoint oracle, private
PulseAudio daemon and control executable, and, when selected, the exact
`parec`/`pacat` recorder executable. It otherwise retains schema 3's
direct-invocation and no-host-Steam contract. The
`input-manifest.json` binds the exact immutable inputs, the
`getcompatpath` prefix setup plus direct `runinprefix`/MediaEngine invocation
boundary, and separately records that host Steam discovery and host Steam
writes are disabled.
`evidence-manifest.json` binds the available raw artifacts and terminal stage.
Schema-3 records predate private endpoint capture and must not be interpreted
as endpoint-delivery evidence.
Schema-2 records used Proton's normal Steam-wrapper verb; they are not
launcher-equivalent to schema-3 comparisons. Older pre-containment schema-1
records used the broader `hostSteamAccess` label; keep their original validator
with them rather than interpreting that field as a proof that every read-only
custom mount was hidden.
`driver.jsonl`, `parser-summary.json`, and `transport-summary.json` are present
only when their respective stages completed.

## Selecting a direct HTTP case

Build a case directory without running Proton, Steam, a VM, or a service:

```sh
case_pkg=$(nix-build tests/nix-vm/stress-case.nix \
  --argstr nixpkgsPath /nix/store/<pinned-nixpkgs> \
  --argstr driverExePath /nix/store/<driver>/bin/media-engine-stress-x86_64.exe \
  --argstr fixtureRootPath /nix/store/<full-fixture> \
  --argstr profile full \
  --argstr caseName progressive-no-range \
  --no-out-link)
jq . "$case_pkg/case-metadata.json"
```

Valid direct case names are `vod-seek`, `progressive-no-range`,
`progressive-fixed-redirect`, `http-failed-range-terminal`,
`http-failed-range-recovery`, `finite-eof-near-end`,
`source-replacement-generation`, and `running-http-replacement`. Pass that
immutable directory as `--case-dir` to the command above and use the exact
`caseRole` printed by its metadata.
Allocate a fresh synthetic AppID for every invocation. The terminal-503 case is
an `expected-pass` harness case because passing means it observed the declared
single terminal error and no retry; it is not a claim that playback succeeded.

The current local full-profile set was composed against the same qualified
driver used by the progressive-VOD runs, SHA-256
`70ba4f15f8d96a0890555ff1847050d7a45ebe1efe6479c42dd62a0cb22e808a`:

| Case | Immutable case directory |
| --- | --- |
| `vod-seek` | `/nix/store/xlpxrx1grv3p36mygw3kvcr5yd6g8lzf-rtsp-media-stress-vod-seek-full-case` |
| `progressive-no-range` | `/nix/store/hihf08q5zd0wipl12p919abb7jvaipg5-rtsp-media-stress-progressive-no-range-full-case` |
| `progressive-fixed-redirect` | `/nix/store/6886n16lnkyhd7cx6a3jiyw842a66w5n-rtsp-media-stress-progressive-fixed-redirect-full-case` |
| `http-failed-range-terminal` | `/nix/store/6l8cdw7xh92f9bnmh6gmn7ln3d05qa40-rtsp-media-stress-http-failed-range-terminal-full-case` |
| `source-replacement-generation` | `/nix/store/s6psvm78kb1hm3caazh51yq83xm0slzv-rtsp-media-stress-source-replacement-generation-full-case` |
| `running-http-replacement` | `/nix/store/8pzv9i3gm57hh906hd1yvd5k8g0p5ydy-rtsp-media-stress-running-http-replacement-full-case` |

The earlier no-Range case at `myii96...` used `wait_time 15` after its far
seek. A3.8 moved the exposed clock directly to 83 seconds while leaving
`seeking=true`, so that wait returned in the same millisecond. The parser
correctly rejected the checkpoint, but the scenario did not observe whether
the backend recovered over time. The `hihf08...` replacement is pure-composed
but not yet runtime-qualified; it uses a three-second `wait_ms` before applying
the same 15--17-second, non-seeking sequential checkpoint.

Their internal checksums and driver/scenario bindings pass. Runtime status is
case-specific: `vod-seek`, `progressive-fixed-redirect`, and the corrected
`source-replacement-generation` run have accepted results;
`running-http-replacement` executed and currently fails its replacement
progress oracle, including on A3.9; `hihf08...` no-Range remains unrun; and the
terminal-503 result remains diagnostic while its ERROR-versus-EOS oracle is
resolved. An immutable path by itself is only a prepared input and never a
runtime pass.
