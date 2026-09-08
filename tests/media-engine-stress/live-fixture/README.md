# Deterministic live-HLS fixture

This directory provides the bounded loopback publication layer missing from
the generated media corpus. It serves the full fixture's muxed live schedule
and its separately scheduled audio/video master without modifying the fixture
tree. It does not invoke Steam, Wine, Proton, FFmpeg, MediaMTX, or the public
network.

This is transport stimulus, not a Proton fix or proof of an RTSP result. A
passing native HTTP test only proves the fixture contract and fault behavior.

## Integrity boundary

Before opening a socket, `live_fixture.py` requires all of the following:

- a `READY` document for the `full` profile;
- a byte-exact `SHA256SUMS` digest matching `READY`;
- the provenance-manifest digest recorded by `READY`;
- checksummed muxed, audio, video, and paired schedule documents;
- every schedule entry's own playlist digest;
- live, non-traversing relative segment URIs whose files match
  `SHA256SUMS`; and
- an exact separate-rendition master containing only
  `audio/index.m3u8` and `video/index.m3u8`.

These checks detect an incomplete or internally corrupted fixture. They are
self-consistency checks; they do not authenticate an intentionally replaced
fixture root. The outer Nix test must still pin the fixture derivation.

The server accepts only literal `127.0.0.1`. Request count, concurrent handler
count, delay, evidence size, schedule size, playlist size, and segment size are
bounded. The JSONL evidence records only an allow-listed route category,
method category, status, publication generation, byte count, and absolute
Linux `CLOCK_MONOTONIC_RAW` nanoseconds. Every record declares the exact
`linux-clock-monotonic-raw-v1` basis used by the VM Wine-event watcher, so the
two streams can be joined without a wall-clock timestamp or a process-local
origin estimate. It never records request targets, queries, headers, control
tokens, peer addresses, filesystem paths, or wall-clock time.

## Publication and fault modes

One locked generation selects the muxed window and both separate audio/video
windows. Advancing therefore cannot publish a torn audio/video pair. FFmpeg
can leave the muxed schedule with one more complete terminal segment than the
independently encoded pair; the contract permits that difference, requires the
entire paired sequence to be an exact prefix of the muxed sequence, and bounds
both publication and routable mux-segment closure to that common prefix. A
segment that appears only in an unmatched mux tail remains a 404 even though
its immutable file exists in the fixture derivation.

Available modes are:

- `normal`: advance through every immutable window, then hold the last one;
- `hold`: refuse advances and retain the selected initial window;
- `delayed-audio-window`: delay only the separate audio child playlist after
  the selected generation;
- `audio-404`: return 404 for separate audio playlist and segment requests
  at/after a selected generation, or after a selected successful segment-GET
  budget; and
- `audio-503`: the same topology using a retryable 503 response.

Generation and segment-budget triggers are mutually exclusive.
`--audio-success-budget 2` makes the error modes request-deterministic: child
playlist polling, segment `HEAD`, invalid segment names, and unrelated routes
never spend the budget; exactly the first two valid audio-segment `GET`
responses succeed, including under concurrency, and subsequent valid audio
segment `GET`s fail while video and the audio playlist remain available. This
deterministically models the *transport stimulus* for “brief audio, then
silent video” without depending on whether a client prefetches an entire
seven-segment window. Two admitted HTTP 200 responses do not prove that the
consumer decoded either response. The VM negative-control adapter must first
observe fresh, nonzero PCM and only then require sustained silence while video
and requests continue. This mode deliberately creates a broken source and is
an expected-negative oracle, not behavior a healthy Proton build should hide.

Publication advances either through authenticated control requests or at the
schedule's declared interval with `--auto-advance`. Automatic advancement uses
absolute monotonic deadlines so handler delays do not accumulate timer drift.

## Run

Use a fresh evidence path and a synthetic test token. The token is not logged:

```sh
python3 live_fixture.py \
  --fixture-root /nix/store/<hash>-vrchat-media-engine-fixtures-full \
  --case-id live-av-normal \
  --control-token local-test-token \
  --fault-mode normal \
  --auto-advance \
  --host 127.0.0.1 \
  --port 18080 \
  --log /tmp/live-av-normal.jsonl
```

The single readiness line contains local URLs. The relevant media URLs are:

```text
http://127.0.0.1:18080/mux/index.m3u8
http://127.0.0.1:18080/separate/master.m3u8
```

For manual deterministic advancement, omit `--auto-advance` and send an empty
authenticated POST:

```sh
curl --fail --request POST \
  --header 'X-Live-Fixture-Token: local-test-token' \
  --data '' \
  http://127.0.0.1:18080/__control__/advance
```

The service refuses to append to an existing log. Stop it with `Ctrl-C`.

## Driver scenario/oracle pairing

Do not edit the checked-in driver example in place. In an isolated VM wrapper:

1. copy `driver/examples/live-av.scenario` into the ephemeral run directory;
2. replace its `load` URL with the local muxed or separate HLS URL;
3. hash that exact generated scenario and bind the hash into a copied oracle;
4. independently verify and pass the driver executable hash;
5. run with `--audio-monitor`; and
6. run the existing VM watcher calibration for the Wine driver's
   `GetTickCount64()` timeline; and
7. join its absolute `CLOCK_MONOTONIC_RAW` event timestamps directly to this
   fixture's same-basis request intervals. Reject either input if its declared
   clock basis differs.

The useful A/B set is:

| Source | Fixture mode | Expected driver oracle |
| --- | --- | --- |
| muxed HLS | `normal --auto-advance` | existing `live-av` continuity oracle passes |
| separate A/V HLS | `normal --auto-advance` | same continuity oracle passes |
| separate A/V HLS | `audio-404 --audio-success-budget 2` | continuity oracle must fail on sustained audio/nonzero-audio requirements while video requests continue |
| separate A/V HLS | `audio-503 --audio-success-budget 2` | same expected-negative result, with retry behavior visible in sanitized request evidence |
| separate A/V HLS | `delayed-audio-window` | a recovery oracle must require resumed audio after the bounded delay; do not treat a timeout alone as recovery |
| either HLS form | `hold` | clock/live-window stall oracle fails rather than silently accepting a frozen source |

The 404/503 negative controls demonstrate that the oracle can detect the
reported symptom. They cannot establish that stock GE or a custom Proton build
spontaneously produces it. Promotion still requires old-build failure and
candidate success against the same healthy source, plus an intentionally
broken-source negative control.

Suggested manifest changes, for the parent integrator:

- mark the deterministic HLS publication/fault service as implemented only
  after a reviewed runner owns the complete fixture/consumer/scorer path;
- keep MediaEngine/Proton consumption `vm-only`;
- add separate healthy muxed, healthy separate-rendition, audio-404,
  audio-503, delayed-audio recovery, and held-window scenario IDs;
- record that request-budget error cases are oracle negatives, not required
  playback successes; and
- keep ten-minute continuity planned because the generated full schedule is
  finite and must not loop its marker timeline.

## Tests

Pure contract tests and loopback integration tests are in
`test_live_fixture.py`. They cover integrity rejection, traversal rejection,
atomic generations, common-prefix mux closure, strict absolute-clock evidence,
schema/basis mismatch rejection, bounded logging, muxed and separate
publication, control authentication, hold, audio delay, concurrent
request-budget 404, generation-based 503, HEAD handling, and query redaction.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error -m unittest -v \
  test_live_fixture.py test_rtsp_contract.py
```

`check_rtsp_contract.py` only audits the existing MediaMTX configuration and
helper scripts. It verifies literal loopback, RTSP-over-TCP, disabled unrelated
services, bounded probe use, and hashes the reviewed files. It starts no
server or publisher and explicitly reports `rtsp_runtime_executed: false`.
HLS fixture results must never be presented as RTSP coverage.
