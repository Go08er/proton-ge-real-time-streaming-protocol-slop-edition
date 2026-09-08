# Local RTSP-over-TCP fixture

This directory provides a small, synthetic A/V stream for testing RTSP behavior
independently of Proton/game testing. It uses FFmpeg and a user-supplied
MediaMTX binary; it does not download software, use GStreamer, contact the
public network, or contain credentials.

The automated normal-loopback smoke test passed on 2026-07-10: MediaMTX accepted
the synthetic publication, a second FFmpeg client probed and decoded both
tracks, and normal publisher/server cleanup was bounded. Separate native-only
FFmpeg checks covered an unreachable loopback endpoint and an
accepted-but-silent peer under their own timeout guards. Neither result used a
Wine or Proton consumer. They validate fixture and native FFmpeg behavior only;
they do not establish a Proton/WineDMO failure bound or validate VRChat, media
playback through Wine, Wayland, or XR.

The default endpoint is always:

```text
rtsp://127.0.0.1:8554/fixture
```

The default checked-in server configuration binds only to IPv4 loopback,
exposes only RTSP-over-TCP, and grants anonymous access only to the `fixture`
path from loopback. RTMP, HLS, WebRTC, SRT, MoQ, the API, metrics, profiling,
and playback are disabled explicitly. A separate opt-in configuration enables
only loopback HLS in addition to that same RTSP publisher for live-HLS tests.

## Requirements

- Bash 4 or newer;
- FFmpeg and FFprobe with the `lavfi`, `libx264`, and native AAC components;
- GNU `timeout` for bounded probes;
- a locally installed MediaMTX v1-compatible executable for server tests.

The scripts check their own dependencies and print actionable errors. They
never install or download a missing component. The configuration follows the
current MediaMTX v1
[configuration reference](https://mediamtx.org/docs/references/configuration-file).

## Quick smoke test

Set `MEDIAMTX_BIN` only when the executable is not named `mediamtx` on `PATH`:

```bash
MEDIAMTX_BIN=/absolute/path/to/mediamtx ./tests/media/smoke-test.sh
```

The smoke test performs the following locally:

1. generates an eight-second 320x180, 30 fps H.264/AAC Matroska fixture;
2. starts MediaMTX with [`mediamtx.yml`](mediamtx.yml);
3. publishes the fixture repeatedly over RTSP interleaved TCP;
4. waits for both an audio and a video track;
5. decodes three seconds through a second RTSP-over-TCP connection;
6. asks the publisher and server to exit and verifies bounded shutdown.

Runtime media and logs are written to `generated/`, which is ignored by Git.
The synthetic content is deterministic, but its encoded bytes are not promised
to match across FFmpeg or encoder versions.

A reader which joins between keyframes can print transient H.264 reference
warnings before the next keyframe. MediaMTX can also report informational RTP
packet-size remuxing. These messages are not a failure when both tracks decode,
the command exits successfully, and cleanup remains bounded; persistent decode
errors or a failed exit status are still failures.

## Manual operation

Use four terminals if a game, Wine test program, or another client will consume
the stream:

```bash
./tests/media/generate-fixture.sh
MEDIAMTX_BIN=/absolute/path/to/mediamtx ./tests/media/run-server.sh
./tests/media/publish-fixture.sh
./tests/media/probe-stream.sh 5
```

The first three commands can remain running while another client opens either
`rtsp://127.0.0.1:8554/fixture` or the compatibility spelling
`rtspt://127.0.0.1:8554/fixture`. FFmpeg itself understands the first spelling;
the second is reserved for testing the proposed Wine URL normalization.

`probe-stream.sh` requires both audio and video and decodes them to null. Its
duration argument must be an integer from 1 through 30 seconds. A wall-clock
guard and FFmpeg I/O timeout prevent a failed local endpoint from hanging the
test indefinitely.

## One-command A3.4 game fixture

For the focused A3.4 VRChat test, the trap-safe launcher uses the pinned
MediaMTX v1.19.2 executable and 45-second H.264/AAC fixture already stored in
the workspace-level `.test-tools/` directory:

```bash
./tests/media/run-a34-fixture.sh --check
./tests/media/run-a34-fixture.sh
```

`--check` validates the local executables, fixture codecs, checked-in
loopback-only configuration, and availability of TCP port 8554 without
starting anything. The normal command starts MediaMTX and a real-time looping
FFmpeg publisher, verifies that both tracks are readable, and then prints the
exact URLs to paste into VRChat:

```text
rtsp://127.0.0.1:8554/fixture
rtspt://127.0.0.1:8554/fixture
```

Keep its terminal open throughout the test and press `Ctrl-C` when finished.
The exit trap stops and reaps both child processes, with a bounded forced-stop
fallback. Runtime logs go to ignored files under `generated/`. If the pinned
assets are absent, a required executable is unavailable, the fixture is not
H.264/AAC, or another service already owns port 8554, the launcher fails before
starting the fixture and identifies the problem. It performs no downloads and
does not inspect, launch, or modify Steam, Proton, VRChat, or compatdata.

The default command remains RTSP-only. To test a live HLS consumer against the
same looping H.264/AAC source, select the separate mode explicitly:

```bash
./tests/media/run-a34-fixture.sh --hls-live --check
./tests/media/run-a34-fixture.sh --hls-live
```

This mode also checks port 8888, starts the loopback-only HLS listener from
[`mediamtx-hls-live.yml`](mediamtx-hls-live.yml), and does not report ready
until FFmpeg has decoded both audio and video from:

```text
http://127.0.0.1:8888/fixture/index.m3u8
```

The stream uses compatibility-oriented MPEG-TS HLS with two-second segments,
seven retained segments, and in-memory storage. It is a live sliding window,
not a VOD playlist: `EXT-X-MEDIA-SEQUENCE` advances and `EXT-X-ENDLIST` is
absent. RTMP, WebRTC, SRT, MoQ, the API, metrics, profiling, and playback remain
disabled. Both RTSP publication and HLS reads are restricted to the `fixture`
path from IPv4 loopback. The HLS mode was locally exercised end to end with the
pinned MediaMTX v1.19.2 and 45-second fixture: A/V decode passed, only
127.0.0.1:8554 and 127.0.0.1:8888 listened, the media sequence advanced, and
the launcher left no fixture processes, listeners, or generated keypairs after
`Ctrl-C`.

Use `MEDIAMTX_BIN`, `FFMPEG_BIN`, `FFPROBE_BIN`, `TIMEOUT_BIN`, or
`RTSP_FIXTURE_FILE` only when deliberately testing equivalent local assets;
`./tests/media/run-a34-fixture.sh --help` documents these overrides.

## Failure-mode checklist

These cases need the eventual Wine/Proton consumer and therefore are described
rather than fully automated here. Always record the consumer, exact build,
expected deadline, observed deadline, and whether any Wine processes remained.

### Unreachable endpoint

With the server stopped, open `rtsp://127.0.0.1:8554/fixture`. The open operation
must fail within the consumer's documented connection timeout. Verify that no
demux or Media Foundation worker survives after closing the consumer. Do not use
a public or private-network address as the test target.

### Accepted connection that stalls

MediaMTX deliberately sends valid media and cannot model a half-open RTSP peer
by itself. Use an auditable loopback-only fault injector in a later integration
test, or pause the publisher process after playback begins. Require the read to
time out or cancellation to interrupt it, then resume or close the consumer.
Do not add firewall rules or network namespaces to this repository's smoke
test; those require host-specific privileges and cleanup.

### Server loss during playback

While the publisher and consumer are active, stop `run-server.sh` with
`Ctrl-C`. The consumer must report loss within a bounded interval and must exit
cleanly. Restart the server and publisher separately; do not count automatic
reconnection as successful unless that behavior is part of the tested API.

### Clean shutdown and repetition

During active playback, close the consumer before stopping the publisher and
server. Repeat at least 20 open/play/close cycles. Each close must finish within
the chosen deadline, and no Wine, FFmpeg, or server process may accumulate.
Repeat once with the publisher stopped first and once with the server stopped
first to exercise cancellation while a read is outstanding.

### Stream asymmetry

Pause or constrain one track only when a suitable loopback fault fixture is
available. Confirm that loss or delay of audio does not permanently starve
video, and vice versa. GE's WineDMO backend is already multithreaded, so this is
a behavioral regression test rather than a reason to port GStreamer work-queue
code.

## Privacy and safety

The configuration contains no passwords, tokens, hostnames, external URLs, or
LAN bindings. Keep it that way. Before sharing an integration log, remove user
paths, machine names, Steam identifiers, remote URLs, query strings, and any
unrelated environment variables. Never replace the checked-in loopback address
with `0.0.0.0` or a LAN address for a routine test.
