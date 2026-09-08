# MediaEngine pause/scrub control

These deterministic checks cover the A3.8 repair for an asynchronous
MediaEngine ordering failure observed with a live VRChat player. They include
an executable A3.7 negative control; a candidate-only model passing its own
assertions is not treated as evidence of a repair.

The captured A3.7 control-state sequence was:

```text
0x881908 -> 0x8a1a08 -> 0x9a1948 -> 0x901948
```

After masking unrelated stream/create bits, the `LegacyGEModel` reproduces
that sequence from the actual pre-A3.8 branches. In particular:

1. initial frame scrubbing was active;
2. the application called `Play()` and then `Pause()`;
3. `SetAutoPlay(TRUE)` also appeared before the next poll (the capture cannot
   determine whether it came immediately before or after `Pause()`);
4. the session pause event restored the normal rate but skipped the pending
   application pause; and
5. the rate-change event consumed a stale play flag and started the session.

The old model therefore fails the same settled-state assertion that the A3.8
model must pass: the presentation cannot start when the final explicit intent
is Pause, and it cannot retain pending control operations after their
completion events. The presentation clock and video in the captured run then
advanced while `IsPaused()` remained true.

AVPro correctly treated that public state as paused and supplied silence to
Unity. The correction makes the latest caller intent authoritative and gives
only one event path ownership of a restart. It also clears stale `WAITING`
together with stale `PLAY_PENDING`, so a later `Play()` cannot be ignored.

A3.8 intentionally leaves GE's property-only `SetAutoPlay()` behavior alone.
It adds transition tracing but no new autoplay or source-load policy. Because
the A3.7 poll cannot order `Pause()` relative to `SetAutoPlay(TRUE)`, the model
replays both possibilities. Property-only autoplay does not alter explicit
playback intent, so both must settle as a coherent Pause.

Run the policy model with:

```sh
python3 tests/media-engine-pause-scrub/test_model.py
```

The run is valid only when all of the following are true:

- the legacy model matches the four captured A3.7 control words;
- the shared settled-state oracle rejects that legacy result;
- A3.8 passes the oracle for both possible relative orders of the untraced
  `Pause()` and `SetAutoPlay(TRUE)` calls;
- a later `Play()` succeeds instead of being blocked by stale `WAITING`; and
- every additional ordering test passes.

After replaying the patch series into a prepared GE source tree, audit the
implemented control flow with:

```sh
python3 tests/media-engine-pause-scrub/audit.py /path/to/src-wine \
  --diagnostic-contract present
```

Use `--diagnostic-contract absent` for a series that omits the diagnostic
patch; that mode requires the trace markers to be absent while retaining every
pause/scrub control-flow check.

For an explicit source-level A/B gate, supply both prepared Wine trees:

```sh
python3 tests/media-engine-pause-scrub/source_ab.py \
  /path/to/a3.7-wine /path/to/a3.8-wine
```

That command succeeds only when the audit rejects A3.7 for the targeted
pause/scrub ordering and accepts A3.8. A baseline rejection caused by an
unrelated missing file or parsing error is not counted as a valid negative
control.

Neither command opens a network connection or launches Steam.

For the compiled A/B regression test, including the proven A3.7 negative
control and isolated Steam Runtime runner, see
[`RUNTIME-PROBE.md`](RUNTIME-PROBE.md).

Self-test the runtime evidence parser without launching Steam:

```sh
python3 tests/media-engine-pause-scrub/test_runtime_parser.py
tests/media-engine-pause-scrub/test_runner_guards.sh
```

The runner-guard test also launches no Steam process and creates no prefix. It
checks the fail-closed path and AppID protections around the compiled probe.

## Evidence boundary

The policy model, `audit.py`, and `source_ab.py` are deterministic model and
source-shape evidence. They do not execute the C code, Media Foundation,
AVPro, or a live stream. `audit.py` is also intentionally shallow: it
verifies required source fragments and their ordering, not compiled
behavior.

The runtime probe does execute the compiled Wine MediaEngine implementation,
but it remains narrower than VRChat. Build proof still requires the patch to
apply without fuzz, both Wine architectures to build, and the artifact audit
to pass. Full runtime proof requires replaying the same livestream in VRChat
and checking the new `avprostate` method/event trace for a settled state,
advancing clock, and continuing nonzero audio. Until the compiled candidate
probe and that application run succeed, A3.8 is a test candidate—not a
confirmed fix.
