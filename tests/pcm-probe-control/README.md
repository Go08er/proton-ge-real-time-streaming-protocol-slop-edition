# PCM probe control

The A3.7 diagnostic uses a dedicated `pcmprobe` Wine debug channel to inspect
the two boundaries that still distinguish the live-audio failure:

- raw PCM returned by the selected Microsoft AAC decoder; and
- the sample presented to the selected raw-audio MediaEngine effect immediately
  before `IMFTransform::ProcessInput()`.

The effect's output is deliberately not selected in A3.7. The A3.6 run proved
that AVPro's AudioGrabber emits a zero-valued downstream sample for both an
audible VOD and the silent livestream, so repeating that expected output does
not locate the capture failure.

Each selected topology node consumes at most sixteen inspection attempts, and
each attempt reads at most 16,384 scalar values. Inspection is completed and
the buffer is unlocked before `ProcessInput()` is called. The bounded summary
is retained on the stack and emitted only after that call, avoiding diagnostic
log I/O between the read and the transform handoff. One combined line records
the aggregate together with whether a ProcessInput result is meaningful, the
raw ProcessInput HRESULT, whether a queue was attempted after
`MF_E_NOTACCEPTING`, the queue HRESULT, whether a final delivery result is
meaningful, and the final delivery HRESULT. Decoder-output records mark both
ProcessInput and delivery results invalid with `E_NOTIMPL` sentinels; effect
input records mark both results valid and retain the final return value.
Every line also carries the owning `IMFMediaSession` identity, allowing a PCM
boundary record to be joined to the corresponding `avprostate` engine/session
record without logging media or source strings.

Inspection identifies a complete, truncated, invalid, failed,
unsupported-format, or unsupported-buffer-layout result and reports total
separately from inspected values. Multi-buffer samples are not converted or
mutated. An inspection failure never suppresses the real `ProcessInput()` call,
and only a nonempty, complete inspection can establish whole-sample silence.

The probe never logs PCM bytes, hashes, URLs, or endpoint data. Its timing and
level summaries are derived from media content, and its opaque session identity
is process-address information, so the output must remain private diagnostics.

The channel is inactive under Wine's default logging. Explicit `+pcmprobe`
enables it, but broad selectors such as `WINEDEBUG=all` or `trace+all` enable it
too. Do not use either broad selector for this diagnostic; use a narrow selector
for the short run, for example:

```text
WINEDEBUG=-all,+timestamp,+pid,+tid,+pcmprobe
```

Without one of those enabling selectors, the normal runtime path pays only the
debug-channel gate.

Run the deterministic aggregate model with:

```sh
python3 tests/pcm-probe-control/test_model.py
```

Audit a prepared Wine source tree with:

```sh
python3 tests/pcm-probe-control/audit.py /path/to/src-wine
```
