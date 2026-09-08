# Bounded RTSP drain diagnostics

Patch 0019 adds the normally inactive `rtspdrain` Wine debug channel at the
WineDMO compressed-packet boundary and the streaming audio renderer (SAR).
It is a diagnostic, not a timeout, reconnect, backoff, or queue-policy
change.

The channel records only fixed labels and numeric aggregate state:

- process-local source and stream identifiers;
- per-stream queue high-water marks and sample request/dequeue/delivery counts;
- aggregate normal and emergency backpressure reasons;
- completed compressed-packet counts and bytes; and
- slow/terminal WineDMO read duration and numeric status.

For SAR, one terminal numeric summary records:

- presentation-clock start calls and successful returns;
- actual `IAudioClient_Start()` calls and successes;
- all `ProcessSample()` calls and the subset received before the first
  `OnClockStart()` call;
- render-callback count; and
- final, peak, and client-capacity frame counts, sample rate and frame size,
  final clock state, and whether an audio-client start remains pending.

The call and success fields are separate because SAR currently sets its
logical clock state to running even when `IAudioClient_Start()` fails.
`preclock_samples` means samples ordered before the first `OnClockStart()`
under SAR's existing critical section; it does not lump later pause or stop
states into that count.

Direct RTSP is an FFmpeg `AVFMT_NOFILE` demuxer. Its socket and transport-byte
counter are private to libavformat, so the Wine-only diagnostic deliberately
omits a transport-byte field. `packet_bytes_completed` must not be interpreted
as raw socket progress.

The diagnostic instruments at most the first 64 RTSP sources in a process.
Each instrumented source gets 16 live records, one reserved terminal-read
record, one unconditional source summary, and at most eight unconditional
stream summaries. The first 64 SAR objects each get exactly one guarded
summary on normal shutdown or final release. The hard process bound is
therefore 1,664 WineDMO records plus 64 SAR records: 1,728 `rtspdrain`
records. An early source cannot consume a later source's allowance, and a
late successful source or SAR object retains its own summary. Once a source
spends its live allowance, it takes a fast exhausted path instead of
repeatedly incrementing and rolling back a process-global counter.

SAR does not receive the WineDMO source identity. Correlate `sar_id` and
`source_id` only by process-local creation order and by the Wine timestamp;
the SAR summary's `lifetime_ms` permits its creation time to be reconstructed
from the terminal record. This is a temporal association, not an asserted
one-to-one mapping. Threading a media identifier through Media Session would
be a larger and behavior-adjacent diagnostic change.

In the A3.14 session, a diagnostic line was at most 324 bytes. At that observed
line size the 1,728-record hard bound is about 547 KiB. A SAR summary is also
a fixed-label numeric record; allowing 400 bytes per SAR line adds at most
25 KiB to the earlier WineDMO estimate. Eleven two-stream sources plus eleven
SAR objects would produce at most 220 records, about 67 KiB at the observed
line size, but the source/SAR count is not assumed to be one-to-one. These
are byte estimates; the exact invariant is the line count. Wine's separate
warning and error channels are not byte bounded. Normal runs do no timing,
counter, or log work unless the dedicated channel is selected.

Run the source and patch contracts with:

```sh
python3 tests/rtsp-drain-diagnostics/audit.py \
  --wine-tree <prepared-wine-tree>
python3 -m unittest -v \
  tests/rtsp-drain-diagnostics/test_model.py \
  tests/rtsp-drain-diagnostics/test_patch_contract.py
```

The contained runtime smoke is the existing loopback RTSP high-rate case with:

```sh
WINEDEBUG=-all,+timestamp,+pid,+tid,+rtspdrain,warn+dmo,err+dmo,warn+mfplat,err+mfplat
```

The `warn`/`err` classes restore the narrow failure records that the A3.14
line accidentally omitted. They are expected to remain low-volume because
`trace+dmo`, `trace+mfplat`, and `trace+process` stay disabled, but they have
no mathematical byte cap; retain the Proton log locally and watch its size.
The smoke checks that the bounded source/stream and SAR summaries appear and
contain no fixture URL. A failing source with preclock samples and zero
clock-start calls identifies a clock callback which never arrived. A
successful audio-client start followed by stopped demand identifies a later
stage. It does not claim to reproduce AVPro frame consumption or the in-game
stall.
