<!-- SPDX-License-Identifier: BSD-3-Clause -->

# Isolated Nix VM media lab

This directory is the isolation boundary for media/runtime tests which may
create prefixes, certificates, sockets, crash residue, or other system state.
It is deliberately separate from Steam and VRChat. The default gate boots one
small NixOS VM, proves the guest has no host-directory mounts or network
interfaces beyond loopback, validates the checked-in loopback configurations,
and runs the deterministic progressive-HTTP fixture unit tests. Proton execution
is a separate, explicit opt-in.

The definitions have been parsed, evaluated, and instantiated against the
local pinned Nixpkgs source for revision
`0bb7ec54c8483066ec9d7720e780a5caa71f8612`. The pure harness assets and
full-profile stress case have been built and checked. The minimal offline VM
has also booted and passed its isolation smoke assertions. One A3.7 diagnostic
VM reached the bounded Proton launcher step but timed out and was correctly
rejected; it is not a media-runtime result. The per-run cold-runtime design
described below was hardened after that diagnostic and still requires a fresh
qualification run.

## Isolation contract

- The guest has an ephemeral tmpfs root. Its prefixes and results live only
  below `/var/lib/rtsp-media-lab` and disappear when QEMU exits.
- The guest closure is copied to a Nix-store image. The host Nix store is not
  mounted, the guest store is read-only, and the usual NixOS-test `xchg` and
  `shared` 9p mounts are forcibly removed.
- `virtualisation.vlans = []`, restricted networking, no port forwards, and no
  accepted firewall ports leave only the guest's own loopback interface.
- The guest contains one unprivileged `media-lab` home. It has no Steam tree,
  no `steamapps/compatdata/438100`, and the optional runner rejects AppID
  `438100` explicitly.
- CPU and memory default to two vCPUs and 2048 MiB for the smoke-only VM.
  Evaluation rejects more than eight vCPUs or 8192 MiB, and stress execution
  rejects less than 8192 MiB because its root and run state are RAM-backed.
- The definition accepts Nixpkgs and optional runtime inputs only as immutable
  `/nix/store` paths. It never discovers or reads a live Steam installation.
- No test downloads media or software. Use `nix build --offline`; it fails if
  the complete declared closure is not already available locally.
- Wine and Proton are consumers, never host-side test tools. The legacy probe
  needs its complete five-input bundle; the scenario driver needs its complete
  ten-input bundle. Either runner exists only inside the isolated one-node VM.
  The private-VLAN lab does not run Proton yet.

The Nix build process still creates ordinary build products and temporary QEMU
files in Nix's own build area. The guest cannot address the host's home,
Steam, compatdata, workspace, or arbitrary filesystem paths.

## Result boundary

Raw Proton logs, HTTP request logs, prefixes, crash residue, timing scratch
files, and source URLs stay in the ephemeral guests. Each guest first writes a
compact result through [`result_contract.py`](result_contract.py). The contract rejects extra
fields, non-finite measurements, unexpected labels, oversized output, and a
fault result in which the clean control was impaired along with the target.

The NixOS test driver reads that one canonical JSON object through its command
channel, validates it again, reserializes it, and writes it under
`$out/evidence/` with `SHA256SUMS`. It deliberately does not use
`copy_from_machine`: in the pinned Nixpkgs driver that helper requires the
shared VM directory which this lab removes. Normal NixOS-test harness logs may
still exist elsewhere in the test derivation output; the retained media
evidence directory contains only the strict JSON summary and its checksum.
Stress summaries contain no paths or URLs. They bind hashes computed inside the
guest from the actual Proton tree, Steam Runtime tree, driver EXE, scenario,
fixture manifest, complete fixture byte tree, service config, both oracles,
parser, and VM harness tree. File inputs use raw SHA-256; directory inputs use
the documented `sha256-file-or-tree-v1` deterministic tree digest. Store-path
names supplied by a caller are not accepted as content identities.

## Review-only evaluation

First identify an already materialized, pinned Nixpkgs source in `/nix/store`.
Pass that exact path explicitly; do not use a registry name or URL.

```sh
cd tests/nix-vm
./check-eval.sh /nix/store/<hash>-source
```

This parses the expression and evaluates the important isolation/resource
options. It neither builds nor starts the VM.

After review, the offline full smoke gate is:

```sh
nix build --offline --no-link --file ./default.nix \
  --argstr nixpkgsPath /nix/store/<hash>-source
```

Do not remove `--offline`. A missing dependency should stop the run rather
than silently becoming a network fetch.

## Optional compiled Proton probe

The VM has a guarded slot for the existing compiled MediaEngine pause/scrub
probe. It is disabled by default. A complete bundle consists of:

1. an unpacked Proton tool;
2. the matching Steam Linux Runtime tool tree containing `_v2-entry-point`;
3. the compiled Windows probe executable;
4. the exact `i420-64x64.avi` fixture; and
5. `parse_runtime_probe.py` from the matching test revision.

Every item must already be a `/nix/store` path. Supply all five or none. A
bundle can be included and checked without executing it by leaving
`runRuntimeProbe` false. Execution is an explicit second gate:

```sh
nix build --offline --no-link --file ./default.nix \
  --argstr nixpkgsPath /nix/store/<hash>-source \
  --argstr protonToolPath /nix/store/<hash>-proton-tool \
  --argstr steamRuntimePath /nix/store/<hash>-steam-runtime \
  --argstr probeExePath /nix/store/<hash>-runtime-probe.exe \
  --argstr probeFixturePath /nix/store/<hash>-i420-64x64.avi \
  --argstr probeParserPath /nix/store/<hash>-parse-runtime-probe.py \
  --arg runRuntimeProbe true
```

This older probe slot remains disabled by default and has not yet received the
paired stress runner's verified writable-runtime copy and teardown contract.
Do not use it for qualification or combine it with a stress run until that
boundary is hardened. Its setup/loader failures are not media regression
results.

## Paired MediaEngine stress driver

The current scenario-driver adapter is a progressive HTTP VOD foundation. It
starts a bounded service on `127.0.0.1`, runs the same scenario first without
the MediaEngine audio effect and then with `--audio-monitor`, and gives each run
a different synthetic AppID and a fresh prefix. A run is not accepted until
the service, X server, PulseAudio process, Proton/Wine helpers, and all open
files tied to that run root have stopped. This prevents the control run from
contaminating the instrumented run.

The all-or-none bundle contains:

1. Proton tool tree;
2. matching Steam Linux Runtime tool tree;
3. stress-driver EXE;
4. scenario;
5. fixture provenance manifest;
6. complete fixture byte tree;
7. bounded loopback service config;
8. uninstrumented control oracle;
9. audio-instrumented oracle; and
10. matching `parse_results.py`.

[`stress-case.nix`](stress-case.nix) declaratively composes these loopback
cases from one service, driver, and oracle contract:

| `caseName` | Required outcome | Case role for `full` |
| --- | --- | --- |
| `vod-seek` (default) | Progressive Range startup plus repeated seek/resume | `qualification` |
| `progressive-no-range` | Progressive linear playback; after a far seek, a fixed wall-clock hold must end on the uninterrupted linear clock with no `SEEKED`, no 206, and no second full transfer | `qualification` |
| `progressive-fixed-redirect` | The fixed 307 target remains progressive and resumes a 70-second seek through 206 | `qualification` |
| `http-failed-range-terminal` | Exactly one post-open 503 causes exactly one terminal MediaEngine error, no successful seek, and no retry | `expected-pass` (the bounded error is the expected product result) |
| `http-failed-range-recovery` | The same exact 503/error is followed by `SetSource(/recovery)`; generation two must clear the error, expose A/V and fresh PCM, and advance from zero | `expected-pass` diagnostic |
| `finite-eof-near-end` | A successful near-end Range seek must emit `SEEKED`, advance, then produce exactly one ordinary `ENDED` with no media error | `qualification` |
| `source-replacement-generation` | A delayed generation-one far-seek cannot leak into generation two; only generation two becomes ready and plays near zero, while stale/duplicate events are rejected | `expected-pass` diagnostic |
| `running-http-replacement` | Generation one reaches steady playback, then generation two must expose A/V, deliver fresh PCM, and advance for at least 2.5 seconds after replacement | `expected-pass` diagnostic |
| `rapid-scrub-parity` | Eight immediate seeks against the 120-second H.264/AAC member; ordinary health requires final settlement, while the RTSP sidecar requires completed leading and final targets | `qualification` |
| `rapid-scrub-common-codec` | The same scheduler discriminator over the full profile's 20-second VP9/Opus WebM member shared by Alpha2 GStreamer and A3.11 | `expected-pass` diagnostic |

Every package computes the actual driver/scenario hashes, inserts them into
both oracles, validates the fixture/service closure, and writes
`case-metadata.json` with the declared outcome and any evidence limitation.
The replacement case intentionally uses one A/V member for both generations:
it proves generation/state isolation, not distinct-frame or topology
provenance, and its shared delay does not guarantee reverse completion order.
The running replacement case closes the complementary gap: generation one is
already playing before `replace`, and four wall-clock-spaced checkpoints on
each side reject a stale clock or a newly installed topology that never starts.
The common-codec scrub case is deliberately full-profile-only. Its VP9/Opus
member lets Alpha2's GStreamer path and A3.11 exercise the same codec pair
without depending on host FFmpeg codec libraries. Its eight seek calls are
consecutive, its ordinary oracle requires final settlement and subsequent
advancement, and its RTSP reference sidecar additionally requires completed
first and final targets. Because the member is only about 2.3 MiB and 20
seconds long, this case does not prove streaming-first transport or post-seek
Range behavior; the original 120-second H.264/AAC `rapid-scrub-parity` case
remains unchanged for that qualification. Neither scrub sidecar proves decoded
preview frames or a literal one-second completion cadence.
The no-Range case likewise uses `wait_ms` after its unsupported far seek. A
media-time wait is not a recovery observation: if `SetCurrentTimeEx()` merely
moves the public clock to the requested 83 seconds while remaining stuck in
`seeking`, a 15-second `wait_time` succeeds immediately. After the three-second
full-profile hold, the named checkpoint must instead be unpaused, non-seeking,
and between 15 and 17 seconds on the uninterrupted sequential timeline.

The default `full` VOD profile records an early successful playback checkpoint
at one second while continuing the same uninterrupted playback to the absolute
12-second hold, then alternates far 83-second and backward 3-second seeks
against the 120-second fixture so Range traffic cannot be satisfied by the
initial buffer.
`--argstr profile smoke` uses a 0.5-second startup checkpoint, retains its
one-second pre-seek hold, and substitutes bounded 6/0.5-second seek targets for
the eight-second fixture. Those targets remain more than three seconds from
the preceding steady-state clock in every loop: GE intentionally treats a running
`SetCurrentTimeEx()` request inside that window as a synchronization update
and does not emit `SEEKING`/`SEEKED`. The validator reads the selected
artifact's frozen ffprobe duration, so mixing the full scenario with smoke
bytes fails before a VM can start rather than producing a known timeout.

For `full`, a watcher reads each synchronously flushed driver record on Linux
`CLOCK_MONOTONIC_RAW`, the clock this Wine source uses for `GetTickCount64`,
and requires every observed record to arrive within 250 ms of that timestamp.
Only a successful, unpaused, non-seeking `wait_time` snapshot can establish
first playback; the separate early checkpoint ensures that startup is measured
when playback actually begins instead of at the end of the longer stability
hold. Event records carrying stale state cannot do so. The HTTP
fixture itself records `CLOCK_MONOTONIC_RAW` and an exact clock-basis field.
The scorer rejects a missing or different clock basis before joining the
Windows and HTTP timelines.

Progressive proof is bound to the earliest successful media `GET`, not whichever
concurrent retry happens to remain open longest. Playback must start within
five seconds of that request and at least ten seconds before that same response
ends. Every HTTP terminal record is checked for coherent declared/sent byte
counts, partial disconnects, bounded monotonic and canonical UTC intervals,
and contiguous request identities. The far seek needs a nonzero byte-range
request whose monotonic start is at or after a completed seek action. The
summary retains only that initial request's sequence/range and aggregate counts.
A Range response means a successful 206, never a Range header ignored by a 200
no-Range origin. Redirects are protocol transitions rather than errors. Error
maxima and post-seek-Range minima come from the hash-bound service config; the
single error minimum follows directly from its `fail-post-open-range` mode.
Those bounds enter the sanitized summary and are revalidated by the result
contract. The terminal 503 case therefore cannot pass without one error or with
a retry; healthy cases declare zero. More than 64 requests or eight identical
successful ranges still fail. These constraints reject download-before-playback, a redundant-open-
socket false pass, a pre-seek range false pass, duplicate no-Range full
transfers, redirect loops, and obvious retry storms.

The short smoke fixture cannot establish that timing inequality at 1 MiB/s.
Its case exports `transport.role=media-engine-diagnostic`, leaves progressive
timing fields null, and permits zero post-seek Range requests because the
small file can already be buffered. It may test state/seek/audio behavior but
cannot promote the streaming-first foundation.

Build the pure inputs first. These steps compile/generate files but do not run
Wine, Proton, QEMU, Steam, or VRChat:

```sh
nixpkgs=/nix/store/<hash>-source
fixture=$(nix-build ../media-engine-stress/fixtures/default.nix \
  --argstr nixpkgsPath "$nixpkgs" --argstr profile full --no-out-link)
driver_pkg=$(nix-build ../media-engine-stress/driver/default.nix \
  --arg nixpkgsPath "$nixpkgs" --no-out-link)
case_pkg=$(nix-build ./stress-case.nix \
  --argstr nixpkgsPath "$nixpkgs" \
  --argstr driverExePath "$driver_pkg/bin/media-engine-stress-x86_64.exe" \
  --argstr fixtureRootPath "$fixture" \
  --argstr caseName progressive-fixed-redirect \
  --argstr profile full --no-out-link)
```

Review and instantiate the complete VM derivation without booting it. Replace
the Proton and Runtime placeholders with reviewed, already materialized store
paths. `runStressDriver` remains false by default:

```sh
nix-instantiate ./default.nix \
  --argstr nixpkgsPath "$nixpkgs" \
  --argstr stressProtonToolPath /nix/store/<hash>-proton-tool \
  --argstr stressSteamRuntimePath /nix/store/<hash>-steam-runtime \
  --argstr stressDriverExePath "$driver_pkg/bin/media-engine-stress-x86_64.exe" \
  --argstr stressScenarioPath "$case_pkg/scenario" \
  --argstr stressFixtureManifestPath "$fixture/provenance/manifest.json" \
  --argstr stressFixtureBytesPath "$fixture" \
  --argstr stressServiceConfigPath "$case_pkg/service-config.json" \
  --argstr stressControlOraclePath "$case_pkg/control.oracle.json" \
  --argstr stressInstrumentedOraclePath "$case_pkg/instrumented.oracle.json" \
  --argstr stressParserPath "$driver_pkg/share/parse_results.py" \
  --argstr stressBuildRole streaming-base-candidate \
  --argstr stressCaseRole qualification \
  --argstr stressMediaKind av
```

Only after reviewing that derivation, opt in to an isolated run by using the
same arguments with `nix build --offline --no-link --file ./default.nix` and
adding `--arg runStressDriver true --arg memoryMiB 8192`. Stress execution
requires the full 8 GiB because the RAM-backed guest creates a fresh Proton
prefix, a verified writable Steam Runtime copy, and fresh Pressure Vessel state
for each half of the paired experiment. The run is paired automatically; there
is no switch that silently substitutes an instrumented run for its control.

`stressCaseRole=negative-control` expects the uninstrumented state-machine
control to remain accepted, then expects only the audio-monitor run to be
rejected. The rejection is exportable only when every parser failure maps to
the allowlisted audio delivery/nonzero/freshness codes in
`result_contract.py`; unrelated seek, timeout, media-error, setup, malformed
input, or unknown failures remain hard failures. The sanitized result exports
only sorted failure codes, never the parser text or source URL.

That negative-control role currently classifies whole-run decoded-audio
absence or staleness for the progressive-VOD adapter. It does **not** prove the
live-specific sequence “fresh nonzero audio first, then continuity loss.” A
future live adapter must add a phase-specific initial decoded-PCM checkpoint
which passes before any later expected rejection is eligible; startup silence
must remain a setup/test failure.

The current adapter covers direct progressive HTTP Range, no-Range, fixed
redirect, one terminal post-open Range failure, same-engine recovery onto an
independently healthy route, true finite EOF after a near-end seek, and the
same-fixture source generation replacement diagnostic. Distinct-topology/reverse-completion source
replacement, live HLS, RTSP, and private-VLAN/multiclient cases still need their
declared bounded adapters or fixtures; they are not silently treated as this
coverage.

When live A/V composition is added, it must supply the repository live oracle
unchanged so its exact bytes are hash-bound by this runner. Promotion also
requires the exact frozen A3.7 build as a negative control: all eight
`live-steady` checkpoints must reject A3.7's paused=true zero-fill behavior and
require paused=false, seeking=false, ended=false, audio and video tracks, and
at least five seconds of uninterrupted media-time advance. A generic broken
build is not a substitute for that regression control.

## Extension boundary

The one-node profile intentionally has no network interface, so it is the
right place for loopback fixtures and single-client Wine/Proton state-machine
tests. It cannot emulate packet loss between machines, DNS routing, a remote
TLS origin, or multiplayer clocks.

Those cases belong in the separate private-VLAN definition below. Do not
weaken this smoke profile or mount host directories to add such coverage.
Future scenarios must retain the same state root, synthetic AppID, bounded
runtime, sanitized result schema, and offline-input rules.

## Evidence limits

A passing VM smoke or compiled probe does not validate VRChat UI behavior,
Wayland/XR presentation, WiVRn, GPU decoding, public YouTube extraction, or
real multiplayer synchronization. Those remain separate product-level gates.
The VM is for reproducible transport/MediaEngine faults and for preventing a
failed automated test from modifying the workstation.

The paired runner creates a private PulseAudio daemon with one stereo S16LE,
48 kHz null sink and verifies its topology before Proton starts. It also starts
an Xvfb display and requires Mesa to report llvmpipe. These are deterministic
setup controls, not endpoint-audibility or video-frame oracles. The optional
driver effect proves bounded decoded PCM delivery/nonzero samples inside
MediaEngine, but does not prove the sink was audible. The driver does not copy
or hash decoded video frames, so the headless VM cannot establish changing
pixels, A/V marker alignment, Wayland/XR presentation, or GPU-decoder behavior.

## Private-VLAN network fault lab

[`fault-lab.nix`](fault-lab.nix) is the separate, system-mutating network
profile. It creates one fixture guest and two client guests on exactly one
QEMU-private VLAN. None of the guests receives a default route, forwarded
port, host directory, host Nix-store mount, or host trust material. The origin
owns deterministic DNS and HTTP fixtures. Its `prio` root qdisc sends only
IPv4 packets whose destination is client A through the selected `netem` or
rate-limiting child qdisc; client B stays on an unshaped band as the clean
control. The script can configure clean, delay, jitter, loss, duplication,
reordering, rate limiting, and a complete black hole. Netem profiles use fixed
seeds so repeated schedules are comparable. The current test sends and measures
traffic for clean, delay, and black-hole/recovery only; jitter, loss,
duplication, reordering, and rate are configuration checks, not behavioral
media results.

The review-only gate is:

```sh
./check-fault-eval.sh /nix/store/<hash>-source
```

After review, build and run the isolated NixOS test with:

```sh
nix build --offline --no-link --file ./fault-lab.nix \
  --argstr nixpkgsPath /nix/store/<hash>-source
```

When it is eventually booted, the test is designed to prove isolation,
two-client reachability, fixed DNS and NXDOMAIN behavior, network-profile
ownership, a measurable delay only for client A, continued service for client
B during client A's black hole, recovery after clearing the qdisc, and a
strict sanitized summary. The current evaluation checks do not prove those
runtime behaviors.

The impairment is deliberately narrow: IPv4 origin egress on the test VLAN.
It does not shape client-to-origin ingress packets, simulate Wi-Fi/VR loss,
exercise QUIC, or establish multiplayer clock correctness. It also does not
run Proton yet. Runtime bundles and scenario actions should be added only
after the shared MediaEngine driver has deterministic result parsing and
frozen-reference negative controls.
