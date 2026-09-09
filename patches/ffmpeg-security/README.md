# FFmpeg security patch series

This directory contains the FFmpeg security patch series for the GE-pinned
FFmpeg commit `9047fa1b084f76b1b4d065af2d743df1b40dfb56`. Apply the files in
the exact order recorded by the [machine-readable series](series).

[Back to all patches](../README.md).

## Patch-by-patch guide

These are FFmpeg changes, not the Wine playback series. Click a descriptive
title to read the patch explanation and diff. The first three also have
individual upstream commit pages; the fourth is a project adaptation.

| Order | Patch — open explanation and diff | Purpose and origin |
| --- | --- | --- |
| 1 | [Reject misaligned MagicYUV slice heights](0001-avcodec-magicyuv-reject-slice_height-misaligned-with.patch) | Rejects slice heights inconsistent with chroma subsampling to prevent out-of-array access. [Upstream commit](https://github.com/FFmpeg/FFmpeg/commit/374b726ffa878ee1cadb987bd1e1e20cc7ed8845). |
| 2 | [Expand the interlaced MagicYUV slice check](0002-avcodec-magicyuv-Expand-the-s-interlaced-slice-heigh.patch) | Extends slice-height validation for interlaced content to address out-of-array access. [Upstream commit](https://github.com/FFmpeg/FFmpeg/commit/5806e8b9f34f1b0663b3017ef9dd1aa5d08116d1). |
| 3 | [Fix one-line MagicYUV MEDIAN slices](0003-avcodec-magicyuv-Fix-1-line-MEDIAN-slices.patch) | Corrects decoding of the one-line slice case. [Upstream commit](https://github.com/FFmpeg/FFmpeg/commit/c23d4da3128c279b714b282e6ec292e8755007e3). |
| 4 | [Verify TLS peers by default](0004-avformat-tls-enable-peer-verification-by-default-on-ffmpeg-62.patch) | Enables the existing certificate-verification default for the retained libavformat 62 ABI, including nested HLS requests. Project-authored adaptation; not a CVE backport. |

The detailed provenance, exact hashes and verification requirements follow.

## Upstream CVE backports

The first three patches are unmodified `git format-patch --no-signature`
exports of the official FFmpeg fixes for CVE-2026-8461. Their upstream commit
identities and patch-file SHA-256 digests are:

| Order | Upstream commit | Patch SHA-256 |
| --- | --- | --- |
| 1 | `374b726ffa878ee1cadb987bd1e1e20cc7ed8845` | `f07177e18e34aa77589c3f538d521486a6538425fb3413c48ed1059a98ece3fd` |
| 2 | `5806e8b9f34f1b0663b3017ef9dd1aa5d08116d1` | `ded196b52596004204a5e891a410ab86596b7b7f45a9b1cfa32f3ed7c2d67560` |
| 3 | `c23d4da3128c279b714b282e6ec292e8755007e3` | `28b83b58193a9587212d401bb90a88d31b282b93dc4af551fe62a93e2e99e89c` |

Their mail headers preserve the upstream authors, author dates, commit
messages, and original commit identities.

## Project TLS hardening

The fourth patch is an RTSP-on-GE11-authored hardening change, not an
unmodified upstream patch and not a CVE backport:

- File: `0004-avformat-tls-enable-peer-verification-by-default-on-ffmpeg-62.patch`
- Format-patch identity: `8db766d029d2ab52f3a4e98bb2bcfbe438586bf4`
- Patch SHA-256: `fb4c595355de74552cde8b1b18b1bb89c4dbf94e6275b16b962bec0d6755b390`

Upstream commit
[`5621eee672391680f432075865e7580189ad0097`](https://github.com/FFmpeg/FFmpeg/commit/5621eee672391680f432075865e7580189ad0097)
introduced `FF_API_NO_DEFAULT_TLS_VERIFY`, made the TLS option default depend
on that compatibility macro, and announced that verification would become the
default at the next libavformat major bump. Upstream commit
[`9549c9ad79ee399ff469a420223d4b6118498f7e`](https://github.com/FFmpeg/FFmpeg/commit/9549c9ad79ee399ff469a420223d4b6118498f7e)
completed the change in version 63 by removing the compatibility path and
using a default value of 1.

This project retains the pinned version 62 ABI. The fourth patch therefore
changes only the pinned `FF_API_NO_DEFAULT_TLS_VERIFY` definition to 0. That
selects the existing `TLS_VERIFY_DEFAULT 1` branch for libavformat TLS client
options globally; a caller can still explicitly opt out by setting the
corresponding option to 0.

## Strict verification

The complete `series` file has SHA-256 digest
`7a319e979dbcde1c557ceba1e6c52c1da292f5acdce8985d502456bb9239ea26`.
A strict sequential replay from the pinned commit must use
`git apply --check --whitespace=error-all` before applying each patch, use
`git apply --whitespace=error-all` to apply it, and finish with a clean
`git diff --check`.

The only paths modified by a successful replay are:

- `libavcodec/magicyuv.c`
- `libavformat/version_major.h`

The expected post-series file identities are:

| Path | Git blob | SHA-256 |
| --- | --- | --- |
| `libavcodec/magicyuv.c` | `04fb6cc147e8da1fd8470416a133b0952d772eb0` | `8e2a7df2e7bbbb3d08e243fe2e4b4bdf47e13af144925507ef51033fd46a3da2` |
| `libavformat/version_major.h` | `62109262aae3bd3523f7b608b2d52a39d2b9f718` | `0a755e585e1ceaaeff92506737d712575921fdd35b694b4dd8e4d2858056d449` |

Any base-commit, patch-digest, series-digest, touched-path, whitespace, or
post-series file-identity mismatch must fail preparation rather than being
accepted as a fuzzy backport.

## Licensing and provenance

The modified FFmpeg files declare the GNU Lesser General Public License,
version 2.1 or later. The three upstream backports retain their upstream
copyright, authorship, and license context. See FFmpeg's
`COPYING.LGPLv2.1` and `LICENSE.md` in the pinned source tree.

The fourth patch's original project-authored metadata is covered by this
repository's BSD-3-Clause license. Applying it modifies FFmpeg's
LGPL-2.1-or-later `libavformat/version_major.h`; redistributors must continue
to comply with FFmpeg's applicable license terms.

Upstream references:

- <https://ffmpeg.org/security.html>
- <https://github.com/FFmpeg/FFmpeg/commit/374b726ffa878ee1cadb987bd1e1e20cc7ed8845>
- <https://github.com/FFmpeg/FFmpeg/commit/5806e8b9f34f1b0663b3017ef9dd1aa5d08116d1>
- <https://github.com/FFmpeg/FFmpeg/commit/c23d4da3128c279b714b282e6ec292e8755007e3>
- <https://github.com/FFmpeg/FFmpeg/commit/5621eee672391680f432075865e7580189ad0097>
- <https://github.com/FFmpeg/FFmpeg/commit/9549c9ad79ee399ff469a420223d4b6118498f7e>
