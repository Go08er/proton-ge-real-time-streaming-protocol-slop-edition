# Compiled MediaEngine pause/scrub probe

## What this proves

`runtime_probe.c` is a compiled Wine Media Foundation test for the exact
Play-then-Pause ordering that failed in A3.7. It is not a synthetic
candidate-only test.

The probe loads Wine's existing two-stream `i420-64x64.avi` test fixture
through `IMFMediaEngineEx::SetSourceFromByteStream`. It waits for
`MF_MEDIA_ENGINE_EVENT_FIRSTFRAMEREADY`, which is the deterministic point at
which the Media Session is running at rate zero and initial scrubbing is
still active. From that callback it calls `Play()` and then `Pause()`. After
the event pipeline settles, it checks all of the following:

- both methods returned `S_OK`;
- `IsPaused()` reports true;
- the media clock moved no more than 50 ms during a one-second observation;
- the short fixture did not reach `MF_MEDIA_ENGINE_EVENT_ENDED`; and
- the trace crossed the expected Play/Pause, scrub-complete, pause-complete,
  and rate-restoration path.

This synchronization point matters. A Play/Pause pair issued immediately
after `SetSourceFromByteStream` can run before the topology and Media Session
exist and therefore does not reliably exercise the defect.

## Proven A3.7 negative control

The recorded acceptance series ran the compiled probe five times against:

```text
Proton-RTSP-on-GE11-A3.7-AudioGate-9fad3bbe
```

All five runs reached the target race and failed identically:

```text
Play=0 Pause=0 paused=1 t0=0.133467 t1=0.133467 delta=0.000000 ended=1
FAIL: final Pause intent did not keep presentation paused
```

The trace in every run opened a two-stream winedmo demuxer, completed the
scrub and pause, restored rate 1.0, and then performed a stale second session
start. In other words, the fixture ran to its end even though the final
application intent and public MediaEngine state were Pause. The probe exited
1, as required for this negative control.

Attempts made outside the Proton tool's declared Steam Linux Runtime failed
to initialize the winedmo Unix library. They were setup failures and were
explicitly excluded; they are not part of the five-of-five result.

Evidence identities:

```text
runtime_probe.c       47e1b80ff3e8c58d9a97ba4134a664fcd4984ad9b6192a4bdae03184d3517eb8
verified probe.exe    d4f035a092f05e346cc7147b266e30d87d434456a13f0676e76992edf1fe75a4
i420-64x64.avi        2e111c938d1847cba8a4527dc72412cd1aecc330ceeab9779f5ae70a8322ade7
```

The executable hash includes its build path/debug metadata. Rebuilding this
checked-in source from a different path can change the whole-file hash even
when the executable `.text` section is byte-identical. The source and
fixture hashes are the portable identities.

The checked-in runner was also executed end to end against A3.7 through its
default Runtime 4 invocation and accepted the expected failure. Its parser
has nine deterministic self-tests covering valid old and candidate traces,
the old trace under the wrong expectation, setup failure, absent two-stream
demux, a trace that never reaches the race, a stale candidate restart, a
stranded candidate pause request, and a rate event which falsely reports a
restart:

```sh
python3 tests/media-engine-pause-scrub/test_runtime_parser.py
tests/media-engine-pause-scrub/test_runner_guards.sh
```

The second command verifies that rejected compatdata, Steam-tree, existing-
root, `/tmp`, and reserved-AppID inputs create no test path.

## Run the A/B test

Do not point this test at VRChat's `438100` compatdata. The runner requires a
nonexistent `LAB_ROOT`, creates its files with a private umask, rejects paths
inside the configured Steam tree or any `steamapps/compatdata` directory, and
rejects AppID `438100`. Put `LAB_ROOT` under your home directory rather than
`/tmp`; Steam Linux Runtime gives the container a private `/tmp` that cannot
see a host-side prefix created there. Every retry must use a different new
`LAB_ROOT`.

The preserved tool declares `require_tool_appid` `4183110`. The runner
therefore defaults to its installed Steam Linux Runtime 4 entry point and
uses `_v2-entry-point --verb=waitforexitandrun --`. The older
`SteamLinuxRuntime_sniper/run-in-sniper` form remains accepted as a tested
alternate when passed explicitly through `STEAM_RUNTIME`.

Use the exact fixture from the prepared GE Wine source:

```text
<PREPARED_GE_SOURCE>/wine/dlls/mfmediaengine/tests/i420-64x64.avi
```

The runner checks both its compiled source and that fixture against the
recorded SHA-256 values before creating `LAB_ROOT`; a modified source or
different file is rejected rather than treated as A/B evidence.

On NixOS, enter the project's `nix-shell` first so the MinGW compiler is
available. Then run the known-bad control:

```sh
cd <A3.8_CHECKOUT>
nix-shell

EXPECT=a3.7-failure \
PROTON_TOOL="$HOME/.local/share/Steam/compatibilitytools.d/<A3.7_TOOL>" \
FIXTURE=<PREPARED_GE_SOURCE>/wine/dlls/mfmediaengine/tests/i420-64x64.avi \
LAB_ROOT=<PRIVATE_LAB>/a3.7-run-1 \
tests/media-engine-pause-scrub/run_runtime_probe.sh
```

The command is successful only if the old build reaches and exhibits the
specific bug. A missing codec/runtime, timeout, source-load error, one-stream
demux, or wrong event sequence is rejected instead of being counted as a
negative control.

After an A3.8 artifact is built, run the same probe with only the expectation,
tool, lab directory, and synthetic application ID changed:

```sh
EXPECT=candidate-pass \
PROTON_TOOL="$HOME/.local/share/Steam/compatibilitytools.d/<A3.8_TOOL>" \
FIXTURE=<PREPARED_GE_SOURCE>/wine/dlls/mfmediaengine/tests/i420-64x64.avi \
LAB_ROOT=<PRIVATE_LAB>/a3.8-run-1 \
APP_ID=999108 \
tests/media-engine-pause-scrub/run_runtime_probe.sh
```

A candidate is accepted only if it reaches the same targeted trace path,
keeps the final Pause authoritative, clears the pause/play/waiting operations
in their owning events, does not restart the session during or after rate
restoration, keeps the clock stationary, and exits zero.

For manual validation of already captured evidence:

```sh
python3 tests/media-engine-pause-scrub/parse_runtime_probe.py \
  --expect a3.7-failure \
  --result <PROBE_RESULT> \
  --log <PROTON_LOG> \
  --process-exit 1
```

Change the expectation to `candidate-pass` and process exit to `0` for the
candidate. Parsing an A3.7 trace as a candidate must fail.

## Evidence boundary

A candidate pass here would be real compiled MediaEngine regression evidence,
not merely a model or source audit. It still would not be complete VRChat or
livestream proof. The final runtime gate remains the same affected VRChat
livestream, comparing A3.7 and A3.8 traces and verifying:

- the player remains unpaused after loading;
- the presentation clock advances normally;
- decoded PCM remains nonzero after the initial transition; and
- video, pause/play, seeking, refresh, and multiplayer synchronization remain
  stable.

Until both the compiled candidate probe and that application-level run pass,
A3.8 remains a test candidate rather than a confirmed fix.
