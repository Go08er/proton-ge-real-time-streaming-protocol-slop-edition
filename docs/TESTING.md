# Testing and issue reports

Test one changed variable at a time. Record the exact tool name, game/world,
media type, steps, visible failure stage and recovery behavior. Confirm moving
video and continuous audio; a loaded poster frame is not a playback pass.
For seeking, check forward/backward movement and sustained playback afterward.
For a live stream without a clock, say that rather than inventing timing data.

The SE1 development baseline, A3.23, passed the maintainer's reported service
seek, YouTube seek, Waterwolf RTSP and multiplayer join-in-progress checks.
Exact attempt counts, recovery latency and synchronization offsets were not
measured. No all-codec, all-world or long-soak claim follows.

The focused MediaEngine test consumes frames and checks clock advancement.
The prior R2 candidate reproduces a finite-HLS seek freeze while a matching
MP4 control passes; A3.23 passes the same HLS cases. A SourceReader decode
test alone does not prove renderer/clock continuity.

SE1 qualification re-ran the retained R2 web-style HLS failure, then passed
MP4, ordinary HLS and web-style HLS with that same probe and fixtures.
These are clock/software-frame results with private null audio, not audible
audio, endpoint synchronization, DXGI, remote-service or headset results.

## Local diagnostics

Use normal logging off unless investigating a problem. The existing bounded
channel may be enabled with `WINEDEBUG="-all,+rtspdrain"`; do not add
`trace+process`, `trace+mfplat` or `trace+dmo`. Diagnostics are not a playback
oracle and have per-source budgets. Never share a raw Proton log without
review: other output can expose private data even when a diagnostic is bounded.

`PROTON_LOG=1` replaces the same AppID log on each launch. Use a new local
log directory per trial or preserve it before relaunching. The optional live
watcher continuously copies generations into a private capture directory.

For a useful report, include a synthetic reproduction or confirmed, redacted
capture; exact revisions; expected/observed results; and whether Stop/manual
reopen recovers. Record successes as well as failures. Do not provide game
prefixes, account tokens, resolved signed URLs or private copyrighted fixtures.

Contained test runners require loopback and synthetic AppIDs. Never point
them at a real game's AppID or compatibility data.
