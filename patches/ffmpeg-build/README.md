# FFmpeg build-integration patch series

This directory contains the RTSP-on-GE11 build-integration series, originally
authored against `9fad3bbe270409e67a8d2f4d123afc73d848306a` and strictly replayed
for A3.12 on `bb1caad333b08cf87d49d0f794a538502d992eae`. Apply the files in the
exact order recorded by `series`; the selected build config is authoritative
for the base-source identity.

## AES-128 HLS support

The pinned GE `Makefile.in` configures FFmpeg with `--disable-everything` and
enables selected protocols individually. Its FFmpeg build therefore has
`CONFIG_CRYPTO_PROTOCOL=0`, even though the pinned HLS demuxer opens AES-128
encrypted media segments through a `crypto+<segment URL>` wrapper.

The single patch enables only FFmpeg's `crypto` URLProtocol:

- File: `0001-ffmpeg-enable-crypto-protocol-for-AES-128-HLS.patch`
- Format-patch identity: `b4e5b7566addfaa86a81b84d4f07259d1861c217`
- Patch SHA-256: `53c9396a347d9170e9fc519ac6894fac59bc78c216523ebbb2946041397e2d54`

This is an RTSP-on-GE11-authored build-integration patch, not an upstream GE
commit and not an upstream FFmpeg commit.

Enabling the protocol does not itself grant arbitrary nested access. The Wine
media patch keeps `crypto` out of the generic progressive HTTP allowlists and
permits it only in the HLS-specific allowlists. The pinned FFmpeg `crypto`
implementation propagates the parent protocol whitelist to the nested segment
URL. That HLS-specific whitelist remains the runtime policy boundary; this
build patch must not be used to justify adding `file`, `data`, proxy, or other
unreviewed protocols.

## Strict verification

The `series` file has SHA-256 digest
`ddf3239768eb96050f71d833950a426461b594ac0cbd6e1a4399d610cfde0932`.
A strict replay from the pinned GE commit must:

1. verify the base commit exactly;
2. run `git apply --check --whitespace=error-all` before application;
3. apply with `git apply --whitespace=error-all`;
4. require `Makefile.in` to be the only touched path;
5. require exactly one `--enable-protocol=crypto` configure argument; and
6. finish with a clean `git diff --check`.

The expected source identities are:

| State | Git blob | SHA-256 |
| --- | --- | --- |
| A3.12 original `Makefile.in` | `18112fd342e0edb9074883a78469be12733f2cbc` | `adce1da99345a4ecf692efbcd7ec03f12b9b1b989dccc09d5b00304f112d4907` |
| A3.12 patched `Makefile.in` | `666c504f6298796cf473409e5f4a6097ad2fa87a` | `d8ba23162043dc7993c37a0eaaf0fa990293532f5236fe6595f7b0c5675fa99d` |

The subsequent FFmpeg configuration and artifact checks must additionally
require `CONFIG_CRYPTO_PROTOCOL=1` for every packaged architecture. Source
replay proves the reviewed build input was applied; the generated-config check
proves the requested protocol was actually compiled into the artifact.

Any base-commit, patch-digest, series-digest, touched-path, whitespace,
argument-count, file-identity, or generated-config mismatch must fail
preparation or artifact verification rather than being accepted as a fuzzy
backport.

## Licensing and provenance

The patch's original project-authored metadata is covered by this repository's
BSD-3-Clause license. Applying it modifies GE/Proton's top-level `Makefile.in`,
whose redistribution terms are described by `LICENSE` and `LICENSE.proton` in
the pinned GE source tree. FFmpeg and the other bundled components retain their
own license terms; this configure-only change does not alter them.
