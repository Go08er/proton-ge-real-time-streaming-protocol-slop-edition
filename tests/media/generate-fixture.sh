#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_command ffmpeg

encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null)" ||
  die "FFmpeg could not enumerate its encoders"
[[ "$encoders" =~ [[:space:]]libx264[[:space:]] ]] ||
  die "FFmpeg lacks the required libx264 encoder"
[[ "$encoders" =~ [[:space:]]aac[[:space:]] ]] ||
  die "FFmpeg lacks the required native AAC encoder"

filters="$(ffmpeg -hide_banner -filters 2>/dev/null)" ||
  die "FFmpeg could not enumerate its filters"
[[ "$filters" =~ [[:space:]]testsrc2[[:space:]] ]] ||
  die "FFmpeg lacks the required testsrc2 filter"
[[ "$filters" =~ [[:space:]]sine[[:space:]] ]] ||
  die "FFmpeg lacks the required sine filter"

mkdir -p "$GENERATED_DIR"
temporary_path="$GENERATED_DIR/.synthetic-av.$$.mkv"
trap 'rm -f -- "$temporary_path"' EXIT

ffmpeg -hide_banner -nostdin -loglevel warning -y \
  -f lavfi -i 'testsrc2=size=320x180:rate=30:duration=8' \
  -f lavfi -i 'sine=frequency=1000:sample_rate=48000:duration=8' \
  -map 0:v:0 -map 1:a:0 -map_metadata -1 \
  -c:v libx264 -preset medium -pix_fmt yuv420p -profile:v main \
  -g 30 -keyint_min 30 -sc_threshold 0 -threads:v 1 \
  -c:a aac -b:a 96k -ar 48000 -ac 2 \
  -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
  -shortest -f matroska "$temporary_path" ||
  die "FFmpeg failed to generate the synthetic A/V fixture"

mv -f -- "$temporary_path" "$FIXTURE_PATH"
trap - EXIT
printf 'Generated %s\n' "$FIXTURE_PATH"
