# VOD control model

This dependency-free model protects the Patch 4, Patch 6, Patch 7, and Patch 8 policies
implicated by the A3.1 and A3.2 game traces:

- all active, non-EOS A/V streams must receive a packet before one full queue
  stops the shared demux producer;
- running seek updates are coalesced for 250 ms, while a final seek is still
  guaranteed without depending on application polling;
- pathological pre-priming growth stops at the aggregate emergency bound;
- newer seek targets survive older completion events, exact in-flight direct
  or deferred duplicates cancel obsolete queued targets, and nearby
  `SetCurrentTimeEx()` plus direct/paused seeks remain authoritative;
- already-accounted sink demand re-primes the transform/source pull chain after
  source restart, while a sink with no demand does not gain a fabricated one;
- an established audio timestamp mapping, including negative-preroll offset,
  survives decoder flushes while the first post-flush output remains marked
  discontinuous;
- shutdown and source replacement supersede or cancel obsolete scheduled work.

Run it from the project root:

```bash
python3 tests/vod-control/test_model.py
```

The model is a policy regression test, not a substitute for the VRChat gate.
The runtime pass requires uninterrupted VOD playback with player audio, no
10-second visible stall cadence, and continuous A/V recovery after one slider
gesture. Backend seek counts require a separate diagnostic trace.
