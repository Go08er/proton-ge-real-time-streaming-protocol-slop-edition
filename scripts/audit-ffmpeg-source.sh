#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GE_SOURCE="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
FFMPEG_SOURCE=""
REQUIRE_SECURITY_SERIES=0

usage() {
  cat <<'EOF'
Usage: audit-ffmpeg-source.sh [--source GE_DIR] [--ffmpeg DIR]
                              [--require-security-series]

Verify source-level GE/FFmpeg RTSP dependencies and options without compiling.
This cannot replace inspection of the eventual generated config.h.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || fail "--source requires a directory"
      GE_SOURCE="$2"
      shift 2
      ;;
    --ffmpeg)
      [[ $# -ge 2 && -n "$2" ]] || fail "--ffmpeg requires a directory"
      FFMPEG_SOURCE="$2"
      shift 2
      ;;
    --require-security-series)
      REQUIRE_SECURITY_SERIES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ -e "$GE_SOURCE/.git" ]] || fail "missing GE source repository"
[[ -n "$FFMPEG_SOURCE" ]] || FFMPEG_SOURCE="$GE_SOURCE/ffmpeg"
[[ -e "$FFMPEG_SOURCE/.git" ]] || fail "FFmpeg submodule is not initialized"

GE_COMMIT="$(git -C "$GE_SOURCE" rev-parse HEAD)"
FFMPEG_PIN="$(git -C "$GE_SOURCE" ls-tree "$GE_COMMIT" -- ffmpeg \
  | awk '$1 == "160000" && $2 == "commit" {print $3}')"
[[ -n "$FFMPEG_PIN" ]] || fail "GE source has no FFmpeg gitlink"
[[ "$(git -C "$FFMPEG_SOURCE" rev-parse HEAD)" == "$FFMPEG_PIN" ]] \
  || fail "FFmpeg checkout does not match GE gitlink $FFMPEG_PIN"

require_pattern() {
  local pattern="$1"
  local file="$2"
  local description="$3"
  rg -q -- "$pattern" "$file" || fail "$description"
}

require_pattern '--enable-demuxers' "$GE_SOURCE/Makefile.in" \
  'GE does not enable FFmpeg demuxers'
require_pattern '--enable-gnutls' "$GE_SOURCE/Makefile.in" \
  'GE does not enable FFmpeg GnuTLS'
require_pattern '--enable-protocol=https' "$GE_SOURCE/Makefile.in" \
  'GE does not enable FFmpeg HTTPS'
require_pattern '^rtsp_demuxer_select="[^"]*http_protocol[^"]*rtpdec' \
  "$FFMPEG_SOURCE/configure" 'RTSP dependency graph no longer selects HTTP and RTP'
require_pattern '^http_protocol_select="[^"]*tcp_protocol' \
  "$FFMPEG_SOURCE/configure" 'HTTP dependency graph no longer selects TCP'
require_pattern '^https_protocol_select="[^"]*tls_protocol' \
  "$FFMPEG_SOURCE/configure" 'HTTPS dependency graph no longer selects TLS'
require_pattern '\{ "timeout", "set timeout \(in microseconds\) of socket I/O operations"' \
  "$FFMPEG_SOURCE/libavformat/rtsp.c" 'RTSP timeout option changed'
require_pattern '"tcp", "TCP".*rtsp_transport' \
  "$FFMPEG_SOURCE/libavformat/rtsp.c" 'RTSP TCP transport option changed'
require_pattern 'ctx_flags \|= AVFMTCTX_UNSEEKABLE' \
  "$FFMPEG_SOURCE/libavformat/rtsp.c" 'RTSP live-source unseekable marking changed'

if [[ "$REQUIRE_SECURITY_SERIES" == 1 ]]; then
  require_pattern \
    's->vshift\[1\].*s->slice_height.*1 << s->vshift\[1\]' \
    "$FFMPEG_SOURCE/libavcodec/magicyuv.c" \
    'MagicYUV chroma-alignment rejection from 374b726f is absent'
  require_pattern \
    '\(s->slice_height >> s->vshift\[1\]\) <= s->interlaced' \
    "$FFMPEG_SOURCE/libavcodec/magicyuv.c" \
    'MagicYUV expanded slice-height check from 5806e8b9 is absent'
  require_pattern \
    'if \(1 \+ interlaced < height\)' \
    "$FFMPEG_SOURCE/libavcodec/magicyuv.c" \
    'MagicYUV one-line MEDIAN guard from c23d4da3 is absent'
  require_pattern \
    '^#define FF_API_NO_DEFAULT_TLS_VERIFY[[:space:]]+0([[:space:]]|$)' \
    "$FFMPEG_SOURCE/libavformat/version_major.h" \
    'FFmpeg still defaults TLS certificate verification off'
fi

printf 'ge_commit\t%s\n' "$GE_COMMIT"
printf 'ffmpeg_commit\t%s\n' "$FFMPEG_PIN"
printf 'rtsp_demuxer_dependency\thttp_protocol+rtpdec\n'
printf 'http_dependency\ttcp_protocol\n'
printf 'https_dependency\ttls_protocol\n'
printf 'rtsp_socket_timeout_option\ttimeout_microseconds\n'
printf 'rtsp_tcp_transport_option\tpresent\n'
printf 'rtsp_live_unseekable_marker\tpresent\n'
if [[ "$REQUIRE_SECURITY_SERIES" == 1 ]]; then
  printf 'magicyuv_chroma_alignment_fix\tpresent\n'
  printf 'magicyuv_slice_height_fix\tpresent\n'
  printf 'magicyuv_one_line_median_fix\tpresent\n'
  printf 'ffmpeg_tls_verify_default\tenabled\n'
  printf 'ffmpeg_security_series_audit\tpassed\n'
fi
printf 'source_audit\tpassed\n'
