#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_command ffmpeg
require_fixture

printf 'Publishing %s to %s over interleaved TCP; press Ctrl-C to stop.\n' \
  "$FIXTURE_PATH" "$RTSP_FIXTURE_URL"

exec ffmpeg -hide_banner -nostdin -loglevel warning \
  -re -stream_loop -1 -i "$FIXTURE_PATH" \
  -map 0:v:0 -map 0:a:0 -c copy \
  -f rtsp -rtsp_transport tcp -muxdelay 0.1 "$RTSP_FIXTURE_URL"
