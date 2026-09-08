#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

server_binary="$(resolve_mediamtx)"
[[ -r "$MEDIAMTX_CONFIG" ]] || die "MediaMTX configuration is unreadable: $MEDIAMTX_CONFIG"

printf 'Starting loopback-only RTSP server at %s\n' "$RTSP_FIXTURE_URL"
exec "$server_binary" "$MEDIAMTX_CONFIG"
