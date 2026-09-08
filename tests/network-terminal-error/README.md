# Network terminal-error contract

This focused A3.12 gate covers one confirmed A3.11 defect: after a progressive
HTTP seek, FFmpeg can defer the Range reopen until the first packet read. If
that reopen fails, A3.11 reports `SEEKED` and then `ENDED` instead of reporting
the network failure.

`test_model.py` keeps the intended behavioral boundary explicit:

- true EOF still ends playback;
- local byte-stream failures retain Wine's existing policy;
- only direct HTTP/HLS/RTSP input failures become network/aborted errors;
- bitstream-filter failures are not mislabeled as transport failures;
- synchronous and deferred progressive-HTTP failures use the same bounded
  error classification;
- a progressive seek before a known end validates and retains its first packet;
- a seek exactly to the known end, or an unknown-duration seek that reaches
  true EOF, returns to the normal EOS path instead of becoming an error;
- a blocked network seek can be cancelled by source shutdown while other
  source commands remain serialized;
- only the current presentation generation may reach MediaEngine;
- the first terminal error wins; later Play/Start requests may re-signal it
  internally to complete MediaSession commands, but only one successfully
  forwarded `ERROR` reaches MediaEngine and no `ENDED` is fabricated; and
- selecting a new source clears the previous error.

`audit.py /path/to/prepared/wine` checks the corresponding four-file source
contract after every project patch has been applied. Runtime authority remains
the contained `vod-failed-range-negative` case: after the seek checkpoint, the
HTTP fixture must record exactly one failed `GET` with status `503` and a valid
nonzero `Range`, no retry or later media request, while MediaEngine records one
network `ERROR`, zero `SEEKED`, zero `ENDED`, and cleared seeking/ended state.
