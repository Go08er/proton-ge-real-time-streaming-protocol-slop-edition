#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
export GIT_NO_LAZY_FETCH=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
BASE_COMMIT="fd07a035fadfbe989aef887cb999d0ef236c50d0"
XR_REPAIR_COMMIT="20ce609c432d3cc5e6c6051680ee1a75e0930782"
FIX_COMMIT="d3e7ba31eae11373e6483930187e1f79a759fc8d"
PATCH="$ROOT_DIR/patches/upstream-ge/0001-patches-fix-video-and-stream-playback-in-VRChat.-fix.patch"
PATCH_SHA256="41324bfdfb072ff8fe664d16e8aacbb76932b791a2f6edd63186aa7fda0ce4c3"
XR_PATCH="$SOURCE_DIR/patches/wineopenxr/wineopenxr_decouple.patch"
XR_PATCH_SHA256="34234cca36c1a38c52e316ded723c6165ae1e76c7554ded013c560003efd21d2"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -d "$SOURCE_DIR/.git" ]] || fail "missing source checkout: $SOURCE_DIR"
[[ "$(git -C "$SOURCE_DIR" rev-parse 'GE-Proton11-1^{commit}')" == "$BASE_COMMIT" ]] || fail "GE-Proton11-1 tag moved"
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$FIX_COMMIT" ]] || fail "source HEAD is not the historical VRChat research fix"
git -C "$SOURCE_DIR" cat-file -e "$FIX_COMMIT^{commit}"
git -C "$SOURCE_DIR" merge-base --is-ancestor "$XR_REPAIR_COMMIT" "$FIX_COMMIT" || fail "research fix does not include the XR repair"

[[ -f "$SOURCE_DIR/patches/ge-video-rework/0015-winedmo-open-remote-hls-playlists-by-url.patch" ]] || fail "missing official GE 0015 patch"
grep -Fq -- '--enable-gnutls' "$SOURCE_DIR/Makefile.in" || fail "FFmpeg GnuTLS support is absent"
grep -Fq -- '--enable-protocol=https' "$SOURCE_DIR/Makefile.in" || fail "FFmpeg HTTPS protocol is absent"
grep -Fq 'is_http_hls_url' "$SOURCE_DIR/patches/ge-video-rework/0015-winedmo-open-remote-hls-playlists-by-url.patch" || fail "HLS direct-URL logic is absent"

for path in gstreamer gst-orc gst-plugins-rs; do
  if git -C "$SOURCE_DIR" ls-tree --name-only "$FIX_COMMIT" -- "$path" | grep -q .; then
    fail "unexpected GStreamer root entry: $path"
  fi
done

[[ -f "$PATCH" ]] || fail "missing exported upstream patch"
printf '%s  %s\n' "$PATCH_SHA256" "$PATCH" | sha256sum -c -

[[ -f "$XR_PATCH" ]] || fail "missing repaired wineopenxr patch"
printf '%s  %s\n' "$XR_PATCH_SHA256" "$XR_PATCH" | sha256sum -c -

if git -C "$SOURCE_DIR" grep -qiE 'PROTON_XR_MODE|wineopenxr=d|xrizer' "$FIX_COMMIT" -- proton protonfixes patches; then
  fail "unexpected XRizer-specific launcher policy in the historical snapshot"
fi

if [[ -n "$(git -C "$SOURCE_DIR" diff --name-only "$BASE_COMMIT..$FIX_COMMIT" -- wine ffmpeg)" ]]; then
  fail "Wine or FFmpeg gitlinks unexpectedly changed between the tag and fix"
fi

printf 'Verified historical d3e7ba31 research snapshot; this is not the GE-Proton11-2 build baseline.\n'
