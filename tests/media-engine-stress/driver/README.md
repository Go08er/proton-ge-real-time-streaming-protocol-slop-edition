# IMFMediaEngineEx scenario driver

This directory contains a standalone Win32 driver for the public
`IMFMediaEngineEx` boundary used by Unity/AVPro. It is a test client, not a
replacement backend: URLs are resolved and decoded by the Media Foundation
implementation inside the Proton build under test.

The driver is intentionally independent of Steam and Proton launch policy.
An outer isolated lab supplies a Windows path to a scenario, fixture URLs, a
fresh prefix, and the executable. Nothing here invokes Wine, Proton, Steam, or
a host compatdata directory.

## License and provenance

`media_engine_stress.c` is licensed LGPL-2.1-or-later because its optional
pass-through `IMFTransform` is adapted from Wine's
`dlls/mfmediaengine/tests/mfmediaengine.c` (including the test-transform COM
shape and pass-through sample handling). The retained upstream copyright is
in the source header and the license text is in `COPYING.LIB`. The audited
source snapshot was Wine commit
`31af7f983b2e345d11340b120ae3a39d88c9338a`; that complete source file had
SHA-256 `cbf3d8ede32f7041659285f23b4270a4babf9933bc82380206f0ebe3dacc9f14`
when this attribution was recorded.

The original Python, shell, Nix, scenario, and documentation files in this
directory use the repository's BSD-3-Clause license unless a file says
otherwise. The LGPL applies to the C driver and compiled executable; it does
not relicense unrelated Proton patches or test infrastructure.

## Build and pure tests

For quick local development, enter the repository's `nix-shell`, then compile
the 64-bit Windows driver used by VRChat and run the parser/oracle tests:

```sh
tests/media-engine-stress/driver/build.sh --arch x86_64 --check
```

The binary is written to the ignored `build/` directory. An optional i686
build is available when `i686-w64-mingw32-gcc` is installed; the repository's
default shell intentionally provides only its pinned x86_64 MinGW toolchain.
`build.sh` uses `-Wall -Wextra -Werror`; a build failure is therefore a
validation failure, not a warning-only result. `--check` also runs the parser
and source-contract regressions. Building is safe on the host because it only
runs MinGW and Python. Running the `.exe` belongs in an isolated runtime lab.

Frozen A/B evidence must not depend on the ambient `<nixpkgs>` channel. Build
the driver with an explicit immutable nixpkgs store path:

```sh
nix-build tests/media-engine-stress/driver/default.nix \
  --arg nixpkgsPath /nix/store/<hash>-source \
  --no-out-link
```

The derivation runs the pure tests and produces the sanitized EXE,
`SHA256SUMS`, source/scenario/oracle hashes, compiler identity, and both
examples. The build removes PE timestamps and maps build paths; two consecutive
builds of the same source were byte-identical and contained no workspace path.
Pass the recorded hashes to a VM run with `--driver-sha256` and
`--scenario-sha256`; both accept exactly one SHA-256 and copy it into every
JSONL record.

Those arguments are labels, not self-attestation: the Windows executable does
not hash itself or reopen the scenario to verify the value. A qualifying VM
wrapper must independently hash the exact store-resident EXE and scenario,
compare them with the selected oracle, and only then pass those computed
values. Caller-supplied expected strings alone are not evidence identity.

## Scenario language

The format is line-oriented UTF-8. Blank lines and lines beginning with `#`
are ignored. A trailing `#` starts a comment after the command arguments.
Quote URLs or labels containing whitespace; `\\` and `\"` are the supported
quoted escapes.

Commands:

| Command | Meaning |
| --- | --- |
| `load URL` | Start source generation 1 (and later generations if repeated). |
| `replace URL` | Replace the active URL and start a new source generation. |
| `wait_event NAME TIMEOUT_MS` | Consume one matching event from the current action window, including a synchronous event queued just before this wait. |
| `wait_time SECONDS TIMEOUT_MS` | Wait until absolute media time reaches the target. |
| `wait_seek_settled TARGET TOLERANCE TIMEOUT_MS` | Wait until seeking is false and media time is within the inclusive tolerance of the target. |
| `wait_ms MILLISECONDS` | Bounded wall-clock hold for pause/stall observation. |
| `play` / `pause` | Call the corresponding MediaEngine method. |
| `seek SECONDS` | Normal `SetCurrentTimeEx` seek and a new timeline generation. |
| `rate RATE` | Set playback rate from 0 through 16. |
| `autoplay on\|off` | Set MediaEngine autoplay policy. |
| `media_loop on\|off` | Set MediaEngine end-of-media looping. |
| `loop COUNT` ... `endloop` | Repeat a block; nesting is supported to depth 32. |
| `snapshot "LABEL"` | Emit a labelled state sample. The label is optional. |
| `shutdown` | Shut down the engine. It must be the final, top-level action. |

Event names match `MF_MEDIA_ENGINE_EVENT_*` without the prefix. A numeric ID
below 2048 is also accepted. Every wait has an explicit timeout; the parser
caps any one wait at one hour, loops at 10,000 repetitions, the source file at
8,192 actions, and total execution at 100,000 non-structural actions.
Seek-settle targets are limited to 0 through 86,400 seconds and tolerances to
0 through 60 seconds. On timeout its result message records the target,
tolerance, last observed media time, and last `IsSeeking()` state.

Play, Pause, Seek, and Rate begin fresh windows for only the events they cause.
This prevents a resume wait from consuming an earlier `PLAYING`, while keeping
source-readiness events available across a pre-ready seek.

The [VOD seek example](examples/vod-seek.scenario) demonstrates start,
aggressive backwards/forwards seeking, pause hold, resume, and shutdown. An
outer fixture launcher should substitute a URL reachable from the VM before
running it. Run this example with `--audio-monitor`: after every `SEEKED`, it
waits for that individual timeline to advance by one second, and its oracle
requires per-seek clock progress plus a sustained batch of newly delivered,
nonzero audio. A repeated post-seek checkpoint also rejects a stale last-audio
timestamp. Healthy playback before the first seek cannot satisfy those
per-timeline checks.

The [live A/V example](examples/live-av.scenario) holds playback for eight
seconds and records a continuity checkpoint at roughly one-second intervals.
Run it with `--audio-monitor`; the oracle scores every matching checkpoint, so
the known "brief audio, then silent video" behavior fails even when audio
returns near the final checkpoint and `HasAudio()` remains true.

The [join-at-offset example](examples/join-at-offset.scenario) loads while
autoplay is disabled, applies the synchronized-world position before source
readiness and before Play, and requires advancing A/V near 47.250 seconds. The
[latest-wins seek storm](examples/seek-storm-latest-wins.scenario) dispatches
300 superseded alternating seeks followed by one authoritative 83.250-second
target. Its final checkpoints require timeline generation 302 and advancing
A/V near the last target; it does not incorrectly require 301 completion
events. Its oracle caps both `SEEKING` and `SEEKED` at one, so near-clock
synchronization corrections or superseded slider positions cannot pass by
creating a backend seek storm.

## JSONL contract

The output has three record types:

- `event`: every `IMFMediaEngineNotify::EventNotify` callback, with event ID,
  name, raw parameters, the state observed in that callback, and whether its
  captured source/timeline generation was still current when recorded;
- `snapshot`: initialization, each completed action, explicit snapshots, API
  failures, and timeouts; and
- `result`: exactly one final record with status, process exit code, completed
  action count, and a diagnostic message.

All records have a contiguous `seq`, relative `monotonic_ms`, one constant
`monotonic_origin_ms` captured from `GetTickCount64()`, `source_generation`,
`timeline_generation`, current time/duration/rate,
paused/seeking/ended flags, audio/video track discovery, numeric and named
network/ready state, and MediaEngine error code/HRESULT. Explicit seeks begin
a new timeline generation, allowing an oracle to distinguish intended
backward seeking from spontaneous clock regression. Source replacement begins
both a new source and timeline generation.

Inside a no-suspend VM, a transport scorer may calibrate
`monotonic_origin_ms + monotonic_ms` against the guest's monotonic clock and
join MediaEngine readiness/progress to the HTTP fixture's monotonic request
timestamps. It must reject a failed calibration; estimating the driver origin
from Proton process-launch time is not streaming-first evidence.

The driver intentionally does not copy source URLs into JSONL snapshots;
stream URLs can contain bearer tokens. The scenario and source-generation
number preserve test identity without duplicating credentials into logs.

With `--audio-monitor`, the driver inserts an optional pass-through
`IMFTransform` based on Wine's own mfmediaengine effect test. Records then
include total and per-source-generation samples/bytes delivered by
`ProcessOutput`, the last sample timestamp/duration, and the last delivery's
monotonic time. At most 4 KiB of each delivered sample is inspected. For
negotiated PCM16, PCM32, or float32, records identify the format and report
nonzero sample-unit counts, the largest observed absolute value, and the last
nonzero delivery time. This distinguishes track presence (`HasAudio`) from
continuing downstream delivery and catches the all-zero decoded-audio failure
mode. Labelled audio checkpoints can require sample/byte/nonzero counts,
allowed payload formats, and maximum delivery or nonzero-delivery gaps.
The reported sample timestamp/duration is diagnostic metadata only. This
driver does not inject a fixture marker or establish PTS-to-source identity;
matching PTS values must not be treated as proof that two clients decoded the
same source sample.

Callbacks whose captured generation changed while the callback snapshot was
being recorded are retained with `generation_current: false`. They cannot
satisfy a later `wait_event`, but an oracle may count them with
`max_stale_events`. This is a race heuristic, not a source token: MediaEngine
events carry no source identity, so an old-source callback that begins only
after replacement can still look current. Source-replacement tests must also
use event order, target time, and labelled state oracles.

Audio source-generation bases are sampled immediately before `SetSource`.
An old-source sample already queued in the topology can arrive after that
boundary and be counted in the new generation. A source-replacement test must
therefore require fresh growth after a post-ready checkpoint (or use an outer
capture with a source token); an absolute per-generation total alone is not a
source-attribution oracle.

`parse_results.py` rejects malformed/truncated JSONL, sequence or clock
regression, snapshot-generation regression or impossible generation jumps,
missing/non-final result records, and schema/type errors. With `--oracle`, it
can enforce event presence/order,
per-generation events and A/V discovery, exact action counts/order, seek
targets and matching `SEEKED` state, delivered-audio checkpoints,
error/timeout limits, runtime bounds, unintended time regression, and a
minimum uninterrupted playback advance. Clock regression and playback-span
checks begin at the active timeline boundary: the first current-generation
`PLAYING` for a new source, or `SEEKED` for an explicit seek. This excludes a
stopped outgoing clock observed while asynchronous source replacement is
still starting, without overlooking a rollback after playback begins.
`checkpoint_expectations` can also
bind explicitly labelled snapshots to exact state, a bounded time interval, a
minimum media-time span across every match, and source/timeline generation
bounds. The span remains observable when a broken public paused state is the
condition under test; ordinary `min_playback_advance` still requires unpaused
playback. These checks provide the final-target oracle for a superseding seek
storm:

```sh
python3 tests/media-engine-stress/driver/parse_results.py \
  run.jsonl \
  --oracle tests/media-engine-stress/driver/examples/vod-seek.oracle.json
```

Oracle keys are strict: unknown keys fail instead of silently weakening a
test. The parser exits 0 for a passing oracle, 1 for a valid run that violates
its oracle, and 2 for malformed input/oracle data.

## Driver exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Scenario completed and explicitly shut down. |
| 1 | Scenario/internal execution contract failed. |
| 2 | CLI or scenario parse error. |
| 3 | COM, Media Foundation, output, or engine initialization failed. |
| 4 | A MediaEngine action returned a failing HRESULT. |
| 5 | An event or media-time wait timed out. |

Except when setup fails before the output can be opened, the last JSONL result
record carries the same code.

## Deliberate oracle and instrumentation boundaries

The optional monitor proves decoded audio samples continued through its
pass-through effect and inspects a bounded prefix of known uncompressed PCM or
float payloads. A nonzero unit is evidence that the inspected decoded buffers
are not digital silence; it does not prove samples reached or were audible at
the selected endpoint. The effect adds one topology node and offers only
stereo PCM16/PCM32/float32 at common 44.1/48 kHz combinations, which can force
a conversion that an uninstrumented run would not use. Run the same scenario
once without it as a behavior control. Endpoint audibility still needs an
independent VM PulseAudio/PipeWire capture or backend trace oracle.
For the same reason, a one-sample post-seek threshold only proves that one
sample arrived. Promotion scenarios should require a realistic batch and a
later checkpoint with a bounded delivery/nonzero-delivery gap, as the VOD
example does.

The callback currently captures state, serializes JSON, and flushes each
event synchronously. That preserves crash evidence but perturbs timing and can
generate substantial output for long live runs. Treat this executable as a
functional/race diagnostic, not as a frame-time, throughput, underrun-rate, or
maximum-duration performance benchmark. A performance harness should use
lower-intrusion backend counters or an asynchronous bounded recorder.

This version does not transfer/hash video frames, scan complete audio samples,
interpret compressed audio payloads, or issue deliberately invalid
NaN/negative seek arguments. It can detect a stalled video clock/events but
cannot prove changing pixel content. Those are separate oracles rather than
claims inferred from `HasVideo()`.

Likewise, true multiplayer coordination is an outer-harness concern. Run two
isolated driver instances against the same deterministic fixture schedule and
compare their labelled samples; a single client cannot prove networked
VRChat/Udon ownership or synchronization semantics.

The bundled VOD example repeatedly validates completed seeks; it is not an
overlapping-seek storm. The language can issue back-to-back `seek` actions for
that separate stress case, whose oracle should validate the final target and
recovery rather than demand completion of every deliberately superseded seek.
