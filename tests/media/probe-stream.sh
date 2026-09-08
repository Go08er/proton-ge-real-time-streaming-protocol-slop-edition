#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

duration="${1:-3}"
[[ "$duration" =~ ^[0-9]+$ ]] || die "probe duration must be an integer from 1 through 30"
(( duration >= 1 && duration <= 30 )) || die "probe duration must be from 1 through 30 seconds"

require_command ffmpeg
require_command timeout

printf 'Decoding %s for %s second(s) over RTSP/TCP.\n' "$RTSP_FIXTURE_URL" "$duration"

if ! timeout --foreground "$((duration + 10))s" \
  ffmpeg -hide_banner -nostdin -loglevel warning -xerror \
    -rtsp_transport tcp -timeout 5000000 -i "$RTSP_FIXTURE_URL" \
    -map 0:v:0 -map 0:a:0 -t "$duration" -f null -; then
  die "RTSP/TCP probe failed or exceeded its $((duration + 10))-second wall-clock limit"
fi

printf 'RTSP/TCP A/V decode passed.\n'
