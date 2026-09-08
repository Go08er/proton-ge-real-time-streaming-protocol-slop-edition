# AVPro audio-state control

The locally audited AVPro AudioGrabber zero-fills Unity's audio buffer whenever
the MediaEngine reports `IsPaused() || IsSeeking()`. In the pinned GE tree,
`IsSeeking()` is true for either a real positioned Media Session start or GE's
internal deferred/coalesced seek window. The working RTSP tree reports only its
real seek flag, so A3.7 measures this application-visible state difference
before changing any behavior.

The deterministic model covers all combinations of paused, real-seek, and
deferred-seek state. It also models the relevant source transition:

```text
SetCurrentTimeEx -> deferred seek -> real seek -> MESessionStarted -> clear
```

The source audit requires a dedicated `avprostate` Wine debug channel and
coherent in-lock snapshots from `IsPaused()`, `IsSeeking()`,
`GetCurrentTime()`, and `SetCurrentTimeEx()`. The snapshots expose return or
HRESULT, flags before and after stateful calls, deferred-work state, and the
result of a `GetCurrentTime()`-triggered deferred flush. They do not add
persistent AVPro-specific fields or change the existing state transitions.
Flag snapshots must retain the `unsigned int` type of `engine->flags`, keeping
their `%#x` records valid under Wine's `-Werror=format` policy.
Every line includes an opaque engine identity and its `IMFMediaSession`
identity so it can be correlated directly with `pcmprobe` records.

A3.8 also adds exactly one bounded record for each of `Play`, `Pause`,
`SetAutoPlay`, `MESessionPaused`, and `MESessionRateChanged`. The audit treats
that as an all-or-nothing transition set, rejects any other additional
`avprostate` emission, and applies the same identity and privacy rules to both
sets. This preserves the original five-method audio-state contract while
allowing the ordering evidence needed for the pause/scrub regression.

All state and identity fields are copied to local variables while the
MediaEngine lock is held. The lock is released before the diagnostic line is
emitted, and the emission may use only those snapshots and ordinary function
arguments. This keeps high-volume debug output from extending the critical
section or reading changing engine fields after unlock.

`avprostate` is normally disabled and contains no media payload, URL, or
endpoint. The engine/session identities are process addresses, and the channel
can be called at frame/audio-update frequency while also exposing playback
timing and flags. Enable it only for one short private diagnostic run. Broad
selectors also enable unrelated high-volume and potentially sensitive
channels; use the narrow pair needed for A3.7:

```text
WINEDEBUG=-all,+timestamp,+pid,+tid,+pcmprobe,+avprostate
```

Run the deterministic state model with:

```sh
python3 tests/avpro-audio-state/test_model.py
```

Audit a prepared Wine source tree with:

```sh
python3 tests/avpro-audio-state/audit.py /path/to/src-wine
```
