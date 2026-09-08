# Progressive HTTP runtime fixture

This fixture distinguishes progressive playback from download-before-playback
without GStreamer, yt-dlp, a public web server, or any third-party Python
package. It serves one operator-selected media file over loopback at a bounded
rate and records a privacy-minimized request timeline.

It is test infrastructure, not an HTTP implementation intended for production.
It does not modify Steam, Proton prefixes, or build output.

## Safety and privacy

The default listener is `127.0.0.1`. Host-safe mode accepts only a literal
loopback address. `--bind-scope vm-private` accepts only an address from the
RFC 5737 documentation ranges and is reserved for the declared isolated VM;
it cannot be used as a general LAN-listen escape hatch. The server makes no
outbound network requests.

Request logging is an exact allow list. Each JSONL record contains only:

- opaque case ID;
- exact request ordinal;
- `GET` or `HEAD`, numeric status, and a canonical byte range;
- expected and transmitted byte counts;
- UTC plus monotonic start and finish timestamps;
- the fixed `linux-clock-monotonic-raw-v1` clock-basis identifier used to
  correlate those absolute timestamps with Wine `GetTickCount64`; and
- whether the connection ended before the declared body was sent.

The monotonic values come directly from Linux `CLOCK_MONOTONIC_RAW`; startup
fails if that clock is unavailable. The request target, query string, client
address, cookies, credentials, referrer, user agent, media filename, and
filesystem path are never passed to the logger. Invalid or suspicious `Range`
values become the literal `invalid`.
Use an impersonal case ID such as `vod-range-01`; do not encode a username or
media title in it.

## Run it

Use a legally redistributable or locally generated fast-start MP4 with H.264
video and AAC audio. The MP4 metadata must be available before the complete
payload; otherwise container layout can be mistaken for cache-first client
behavior. A file large enough to take at least 30 seconds at the selected rate
makes the progressive startup boundary easy to see. If a suitable H.264/AAC
source already exists, an FFmpeg remux can place its metadata first without
re-encoding:

```bash
ffmpeg -i input.mp4 -map 0:v:0 -map 0:a:0 -c copy -movflags +faststart /tmp/http-vod-faststart.mp4
```

Confirm the output still has H.264 video and AAC audio before using it as
release evidence. A remux cannot add a missing codec stream.

```bash
cd tests/http-stream-fixture
python3 http_stream_fixture.py \
  --file /tmp/http-vod-faststart.mp4 \
  --mode range \
  --case-id vod-range-01 \
  --rate-kib 1024 \
  --log /tmp/vod-range-01.jsonl
```

The media URL is `http://127.0.0.1:8765/media`. `--port 0` selects an
ephemeral port and prints the selected host and port to standard error.
Sanitized JSONL goes to standard output when `--log` is omitted.

The server is deliberately bounded. `--max-requests` defaults to 4096 and
stops the server after the final admitted request; `--max-concurrent` defaults
to 16 and cannot exceed 64; and `--max-log-bytes` defaults to 8 MiB. The log
budget includes existing bytes when appending. Each request reserves at least
512 bytes of budget, so a run is rejected up front when its selected limits
cannot be logged. Header/stall delays must be finite and between zero and 300
seconds.

Stop the fixture with `Ctrl+C`. The important controls are:

| Mode | Behavior |
| --- | --- |
| `range` | Throttled `200`/`206` service with single-byte-range seeking. |
| `no-range` | Throttled `200` service which deliberately ignores `Range` and advertises `Accept-Ranges: none`. |
| `redirect` | `/media` returns a relative `307`; `/redirect-target` is Range-capable and throttled. |
| `fail-first` | The first `--fail-count` requests return the selected bounded `--fail-status`, then the same URL becomes Range-capable. |
| `fail-post-open-range` | Allows the first media `GET`, then returns `503` for later Range `GET`s while ordinary non-Range requests remain available. |
| `fail-post-open-range-recovery` | Gives `/media` the same permanent post-open Range failure while `/recovery` remains independently Range-capable for a same-MediaEngine `SetSource` recovery gate. |
| `truncate` | A successful response declares its full length, sends only `--truncate-after` bytes, and closes the connection. |
| `chunked` | Keeps byte-range behavior but sends the body with valid HTTP/1.1 chunk framing and no `Content-Length`. |
| `stall` | Sends exactly `--stall-after` bytes, waits `--stall-seconds`, then resumes the same response. |

All body-producing modes use `--chunk-bytes` and `--rate-kib`. A rate of zero
disables per-chunk throttling. `--header-delay-seconds` separately delays the
response headers for any mode. The `fail-first` case can exercise a direct-open failure and a
subsequent compatibility fallback, but it cannot by itself prove which client
made the retry. Confirm the direct failure and fallback transition in the
sanitized Proton/Wine media trace as well.

## Progressive-playback acceptance

Use a fresh case ID and log for each run. Compare the first decoded Media
Foundation sample (or the first visible/audio playback event) with the matching
server request's `started_at` and `finished_at` timestamps.

For release evidence, use one same-host UTC timeline: start a local screen
recording before URL submission which shows both the player and a UTC clock
generated by that host, while the fixture writes its UTC JSONL. Identify the
recording frame where playback first visibly advances and the frame where each
seek resumes, and convert those events to UTC using the clock in the recording.
Do not combine a stopwatch or a second device's clock with the JSONL timeline.
Keep the recording local; if any excerpt is shared, crop and manually inspect
it for world, account, notification, and URL information.

### Failed post-open Range check

`fail-post-open-range` isolates seek failure from initial-open failure. Use the
same fast-start fixture as the normal Range test, wait until audio and video
are visibly advancing, and then seek well away from the current position:

```bash
python3 http_stream_fixture.py \
  --file /tmp/http-vod-faststart.mp4 \
  --mode fail-post-open-range \
  --case-id vod-failed-range-01 \
  --rate-kib 1024 \
  --log /tmp/vod-failed-range-01.jsonl
```

The first body-producing `GET` is allowed even when FFmpeg opens with
`Range: bytes=0-`. `HEAD` does not arm the failure gate. Every later valid
Range `GET` receives an empty `503`; invalid Range text remains the sanitized
literal `invalid`. For the terminal-seek error gate, require one bounded media
error, no repeated millisecond-scale demux reads, and a clean shutdown. The
JSONL should show the successful open followed by one or more `503` Range
records without containing the request target, filename, or credentials.

The recovery variant reserves `/recovery` for the healthy second source. Its
sanitized log still omits paths: the hash-bound scenario and service-mode
validator prove route selection, while the transport oracle requires a healthy
body response after the exact failed Range has completed.

The required progressive VOD result is:

```text
request started_at < first decoded sample < request finished_at
```

In other words, playback must begin while the full response is still being
transferred. A start at or after `finished_at` is download-before-playback and
fails the primary requirement, even if playback is otherwise correct.

### Canonical foundation release gate

Use fixture rate `--rate-kib 1024` and a fast-start media transfer lasting at
least 30 seconds. Begin with mode `range`, then restart the fixture with a
fresh case ID for each additional mode named below. All of the following are
required for the foundation handoff:

1. The first advancing sample arrives within 5 seconds of the active media
   `GET`, and that request finishes at least 10 seconds after the sample.
2. A seek produces a satisfiable `Range` request and `206` response, then
   playback resumes within 3 seconds. The requested byte range must not first
   be downloaded in full.
3. `no-range` playback starts before transfer completion, but the media source
   must not claim reliable random seeking.
4. `redirect` preserves progressive startup and Range behavior at the fixed
   target.
5. `truncate` and an unrecovered `fail-first` case surface an error or bounded
   fallback within 10 seconds; neither may hang shutdown indefinitely.

The semantic inequality remains mandatory even if a future canonical fixture
changes. Runs using a different rate, a transfer shorter than 30 seconds, or a
different timing/capture method are diagnostic only and must not be reported as
passing this release gate. Record the fixture mode, rate, media duration/size,
Proton build identity, and client event timestamps alongside the sanitized
server log.

## Deterministic unit tests

`test_http_stream_fixture.py` uses in-memory files and injected sleeps and
opens no socket. `smoke_test.py` is the separate bounded-loopback integration
check; neither test contacts an external network endpoint.

```bash
cd tests/http-stream-fixture
python3 -m unittest -v test_http_stream_fixture.py smoke_test.py
```

The pure unit tests cover explicit, open-ended, suffix, invalid, and multiple ranges;
no-Range behavior; throttle and exact one-shot stall timing; valid chunk
framing; fixed redirects; configurable fail-first state; mid-stream
truncation; query stripping; listener/budget bounds; and the exact redacted
JSONL schema. `smoke_test.py` additionally opens a bounded loopback listener
to verify actual chunked, stall, delayed-header, and recovery behavior; run it
only where loopback sockets are allowed.

## Interpretation limits

- This fixture validates HTTP transport timing and failure behavior. It does
  not validate YouTube URL extraction; use a direct media URL or local file.
- One request may be a probe rather than the playback transfer. Correlate the
  request whose byte count and time span match the active media read.
- A `Range` request proves that the client attempted byte seeking. It does not
  prove that the decoder resumed on the correct presentation timestamp.
- `fail-first` is deliberately protocol-generic. A library-internal retry can
  consume the failure before the intended fallback does, so client-side trace
  evidence remains mandatory.
