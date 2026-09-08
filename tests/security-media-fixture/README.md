# Loopback media-security fixture

This is a small, auditable phase-one fixture for HTTPS, redirects, HLS child
origins, and AES-128 HLS. It is native test infrastructure, not part of the
Proton artifact. It makes no outbound request and every listener is fixed to
`127.0.0.1` on an ephemeral port.

The fixture derives an eight-second MP4 and two small HLS sets from the
repository's existing 45-second H.264/AAC fixture. It creates a new local CA,
valid `localhost` certificate, self-signed certificate, hostname-mismatch
certificate, and AES key for each run. Those files live only in the ignored
`generated/` directory and are deleted after a clean stop. No generated
certificate, private key, AES key, media file, or request log belongs in Git.

## Native verification

From this directory, run:

```bash
python3 -m unittest -v test_security_media_fixture.py
python3 smoke_test.py
```

On NixOS, if `openssl` is not in the active system profile, use the temporary
package environment instead of installing anything:

```bash
nix-shell -p openssl --run 'python3 smoke_test.py'
```

The smoke test requires `openssl`, `curl`, and an FFmpeg build with HTTPS, HLS,
and `crypto` input protocols. It creates a temporary session, then verifies:

- every server is bound to `127.0.0.1`;
- a CA-signed `localhost` endpoint succeeds only when the ephemeral CA is
  supplied explicitly;
- default trust, a self-signed leaf, and a CA-signed hostname mismatch fail;
- the direct MP4 supports a bounded byte Range;
- the controlled HTTPS response redirects to a fixed loopback HTTP target;
- plain, AES-128, mixed-content, and invalid-child HLS playlists have the
  intended fixed children;
- the finite plain-HTTP HLS VOD exposes a fixed segment and native FFmpeg can
  demux both H.264 video and AAC audio after seeking near four seconds;
- native FFmpeg decrypts the AES-128 HLS case over HTTPS when the CA is passed
  only to that process; and
- FFmpeg's restricted protocol list rejects the HTTPS-to-HTTP redirect and
  HTTPS-root mixed-content playlist; and
- failed preparation and catchable `SIGTERM` remove the complete generated
  session and stale ready file.

This is native fixture validation. It does not prove that the Wine/Proton media
path applied the candidate policy.

## Run for a controlled game test

From this directory, keep the fixture open in one terminal:

```bash
python3 security_media_fixture.py \
  --case-id a31-security-01 \
  --ready-file /tmp/a31-security-media-endpoints.json \
  --log /tmp/a31-security-media-requests.jsonl
```

The equivalent command when `openssl` is not in the active NixOS profile is:

```bash
nix-shell -p openssl --run 'python3 security_media_fixture.py --case-id a31-security-01 --ready-file /tmp/a31-security-media-endpoints.json --log /tmp/a31-security-media-requests.jsonl'
```

It prints the fixed loopback endpoints as JSON and writes the same endpoints,
plus the current public CA-certificate path, to the private ready file. Print
only the plain-HTTP HLS VOD endpoint without copying the full document:

```bash
python3 -c 'import json; print(json.load(open("/tmp/a31-security-media-endpoints.json"))["endpoints"]["hls_plain_http"])'
```

`hls_plain_http` is a finite eight-second functional HLS VOD derived from the
fixed H.264/AAC fixture. In a private VRChat instance, an AVPro/Stream-capable
player, and with untrusted URLs enabled, use it for basic start, A/V, pause,
resume, and seek checks. It is deliberately cleartext loopback HTTP. A pass is
evidence for basic HLS VOD behavior only; it is not live-stream, sustained
throughput, internet-origin, or TLS evidence.

The other product-path case available without changing trust is
`hls_aes_http`. It can test whether the candidate's HLS route can play AES-128
media. A pass proves the local HLS/crypto function, not TLS security. Stop or
reload the player and recover to a known-good source after either test.

The other endpoint names are:

- `direct_https`: CA-signed direct MP4;
- `redirect_https_to_http`: CA-signed HTTPS root with a fixed HTTP redirect;
- `hls_plain_https` and `hls_aes_https`: CA-signed HTTPS HLS;
- `hls_mixed_https`: CA-signed HTTPS AES playlist whose key and segments are
  fixed HTTP loopback children;
- `hls_invalid_child_https`: CA-signed HTTPS AES playlist whose key and
  segments are served by the self-signed loopback origin;
- `direct_self_signed_https`: self-signed direct MP4; and
- `direct_wrong_host_https`: CA-signed direct MP4 with the wrong SAN.

Press `Ctrl+C` to stop all four servers and delete the session certificates,
keys, and generated media. The sanitized JSONL remains in `/tmp` for local
inspection. Ordinary failures and catchable `SIGINT`, `SIGTERM`, and `SIGHUP`
also unwind through the same session cleanup. No process can clean up after an
uncatchable `SIGKILL` or power loss; any such leftover remains confined to the
ignored, mode-`0700` `generated/` directory and must not be committed.

## Trust boundary and in-game limitation

The generated CA is private and is **not automatically trusted by Proton,
Wine, FFmpeg, VRChat, or the operating system**. Do not install it into system
trust and do not mutate a Proton prefix's trust store for this test. The native
smoke test passes it to `curl` and FFmpeg for those processes only.

There is currently no reviewed, noninvasive way for the VRChat product path to
trust this ephemeral CA. Consequently, the controlled positive HTTPS root,
hostname-mismatch distinction, downgrade redirect, and HTTPS-root child-policy
cases are native policy/fixture tests only. They remain pending as positive
in-game product-path tests. An in-game failure at any of those private-CA URLs
can occur at the untrusted root certificate before the intended redirect or
child is reached, so it must not be reported as proof of downgrade or HLS-child
enforcement. Continue to use the public HTTPS/HLS canaries in the A3.1 game
runbook for noninvasive positive TLS coverage.

The smoke test validates the invalid HLS child's certificate directly and
checks that the parent playlist names only that fixed child origin. It does not
claim a nested-HLS enforcement result from the host FFmpeg CLI: per-input
`-tls_verify` and `-ca_file` options are not guaranteed to propagate from an
HLS root to every nested request. The candidate's compile-time TLS default and
the product media path therefore need their own artifact audit and controlled
runtime evidence.

## Privacy-minimized request log

Each JSONL record is an exact allowlist: UTC timestamp, impersonal case ID,
fixed server label, `GET`/`HEAD`/`OTHER`, fixed resource class, numeric status,
canonical Range or `invalid`, and transmitted byte count. The logger never
receives or stores the request target, query, URL, client address, filename,
filesystem path, cookies, authorization, referrer, or user agent. Do not put a
person, account, world, or media title into `--case-id`.

The endpoint ready file contains loopback URLs and a local public-certificate
path; it is not a request log. Keep both files local and never share raw Proton,
Wine, Steam, VRChat, or media traces.
