# Deterministic media fixtures

This directory generates media inputs; it contains no checked-in media. Every
build requires an explicit immutable nixpkgs source path, hash-binds the full
tree inside `default.nix` before importing it, uses the FFmpeg from that pin,
pins each H.264 encoder's worker count, removes input metadata, and records the
exact FFmpeg commands, FFmpeg/ffprobe identities, normalized ffprobe reports,
byte sizes, and SHA-256 hashes. The checked-in pin is nixpkgs revision
`0bb7ec54c8483066ec9d7720e780a5caa71f8612` with NAR hash
`sha256-iffAls3iaNTyJC2faYcUXSI+Gp02cDjYl+MygxKl2GI=`.

`smoke` is an eight-second, 320x180 set for CI and VM gates. `full` specifies a
120-second, 1280x720 primary VOD, 60-second topology inputs, and a 30-second HLS
timeline for game qualification. Both profiles produce:

- fast-start, tail-moov, and fragmented MP4 A/V;
- audio-only, video-only, delayed-audio, and delayed-video members;
- finite muxed MPEG-TS HLS, an independent audio/video master, clear CMAF HLS,
  and immutable muxed/separate advancing-window inputs for a VM live service;
- same-track H.264/AAC MP4/MOV/MKV/MPEG-TS container controls;
- HEVC/AAC, VP8/Vorbis, VP9/Opus, MPEG-4/MP3, WMV2/WMAV2, and AV1/Opus
  reference-discovery members;
- stereo MP3, ADTS AAC, PCM WAV, FLAC, and Vorbis at 44.1/48 kHz, Opus at
  48 kHz, AAC 5.1, and a deterministic two-audio-track input; and
- empty, non-media, missing-child, malformed, and two differently truncated
  inputs.

`full` also produces a four-rung RTSP payload ladder. Every rung is 45 seconds,
30 fps, one-second GOP, H.264/AAC in MP4, and carries byte-identical 48 kHz
stereo AAC marker packets at 96 kbit/s. A is 320x180 Main at 270 kbit/s video;
B changes only resolution to 1280x720; C changes only the H.264 profile to
High; D changes only the video target to 8.2 Mbit/s. The low target plus audio
and mux overhead brackets the observed ~0.37 Mbit/s working stream; the high
target brackets the observed ~8.3 Mbit/s failure. `smoke` omits these roughly
50 MB of qualification inputs.

`full` also carries one reproduction input outside that ladder. It matches the
observed heavy fixture's measured encoder shape: 1280x720 High at 8 Mbit/s,
two-second GOPs, three B-frames, one reference frame, the `veryfast` preset,
and 128 kbit/s AAC. It may be generated deterministically or built from the
hash-locked captured bytes. Keeping it separate prevents the multi-axis
reproduction from being mistaken for a controlled ladder rung. A derived
stream-copy trim begins on a proven non-key video packet and retains the next
IDR within 1.6 seconds, making mid-GOP recovery deterministic rather than a
random join-timing claim.

The primary is H.264 Main/AAC-LC. Its video carries frame/time text; its left
and right PCM have continuous carriers plus a different 100 ms frequency
marker at every integer second. Validation decodes the audio to confirm
carrier/marker energy and identity at multiple positions, and decodes video to
frame hashes to reject a frozen or excessively repetitive fixture. Discovery
members measure reference behavior and do not add codecs to the Proton build
or create public support claims by themselves.

Run the pure contract tests without Nix:

```sh
./check.sh
```

Evaluate or build against a reviewed pin. No `result` symlink is created:

```sh
./check-eval.sh /nix/store/<hash>-source smoke
./build-fixtures.sh /nix/store/<hash>-source smoke
```

The build prints one immutable output path. `provenance/manifest.json` is the
machine-readable inventory, `SHA256SUMS` covers every retained file other than
itself and `READY`, and `READY` binds those two records to the selected profile.
The derivation validates track topology, duration, MP4 atom placement, packet
payload identity, delayed first-packet times, HLS child closure, invalid-input
rejection, every discovery member's codec/rate/channel/track metadata,
successful decoding of every declared valid member, decoded PCM marker energy,
video-frame identity, and decode failure for the truncated fast-start input.
Container durations are checked from ffprobe metadata. Raw ADTS AAC has no
authoritative container duration, so its duration is checked by counting
decoded PCM frames instead of accepting FFprobe's bitrate-based estimate as
the media timeline.

The wrapper compares `nix hash path` before evaluation for a clear mismatch
error. The Nix expression independently materializes/imports a hash-bound copy,
so calling `nix-build default.nix` directly cannot make an arbitrary supplied
tree appear under the checked-in revision/NAR provenance.

The live HLS files are finite schedule inputs, not a ten-minute qualification
and not a host daemon. A VM service should copy the
named playlists from `hls/live/schedule.json` into guest-writable state and
serve them in order at `interval_seconds`. This keeps mutation inside the VM
while the generated source set remains immutable.

For separate renditions, `hls/live-separate/schedule.json` binds each audio and
video window/hash into one publish generation. The service must stage both and
switch them atomically; advancing one child independently is an invalid
fixture. Validation also compares first/last packet PTS for every same-index
audio/video segment and rejects boundary skew over 150 ms. Before schedules
are generated, the live-only alignment step removes
unpaired encoder-padding tails and stops at the first segment shorter than 90%
of the declared interval. Finite VOD playlists retain their terminal segments
for EOS coverage.

AES-128, declared HLS discontinuities, audio/video gap members, and ICY are
not generated here yet. Their roadmap rows must not be inferred from the clear
HLS, finite-file, or RTSP fixture build.
