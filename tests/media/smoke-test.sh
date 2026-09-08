#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_command ffprobe
require_command timeout
server_binary="$(resolve_mediamtx)"

server_pid=''
publisher_pid=''

stop_process() {
  local pid="$1"
  local label="$2"
  local signal="${3:-TERM}"
  local attempt

  [[ -n "$pid" ]] || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi

  kill -s "$signal" "$pid" 2>/dev/null || true
  for ((attempt = 0; attempt < 50; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done

  printf 'ERROR: %s did not stop within five seconds; sending KILL\n' "$label" >&2
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  return 1
}

cleanup() {
  set +e
  stop_process "$publisher_pid" publisher
  stop_process "$server_pid" server
}

on_signal() {
  trap - INT TERM
  exit 130
}

trap cleanup EXIT
trap on_signal INT TERM

"$SCRIPT_DIR/generate-fixture.sh"
mkdir -p "$GENERATED_DIR"
server_log="$GENERATED_DIR/mediamtx.log"
publisher_log="$GENERATED_DIR/publisher.log"
: >"$server_log"
: >"$publisher_log"

"$server_binary" "$MEDIAMTX_CONFIG" >"$server_log" 2>&1 &
server_pid=$!

server_ready=false
for ((attempt = 0; attempt < 50; attempt++)); do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    tail -n 40 "$server_log" >&2 || true
    die "MediaMTX exited before its RTSP listener became ready"
  fi
  if (exec 3<>/dev/tcp/127.0.0.1/8554) >/dev/null 2>&1; then
    server_ready=true
    break
  fi
  sleep 0.1
done
[[ "$server_ready" == true ]] || die "MediaMTX did not open 127.0.0.1:8554 within five seconds"

"$SCRIPT_DIR/publish-fixture.sh" >"$publisher_log" 2>&1 &
publisher_pid=$!

stream_ready=false
for ((attempt = 0; attempt < 20; attempt++)); do
  if ! kill -0 "$publisher_pid" 2>/dev/null; then
    tail -n 40 "$publisher_log" >&2 || true
    die "FFmpeg publisher exited before the fixture became available"
  fi

  stream_types="$(timeout --foreground 1s ffprobe -v error \
    -rtsp_transport tcp -timeout 500000 \
    -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=1 \
    "$RTSP_FIXTURE_URL" 2>/dev/null || true)"
  if [[ "$stream_types" == *video* && "$stream_types" == *audio* ]]; then
    stream_ready=true
    break
  fi
  sleep 0.1
done
if [[ "$stream_ready" != true ]]; then
  tail -n 40 "$publisher_log" >&2 || true
  tail -n 40 "$server_log" >&2 || true
  die "published stream did not expose both audio and video within the readiness deadline"
fi

"$SCRIPT_DIR/probe-stream.sh" 3

if ! stop_process "$publisher_pid" publisher; then
  die "publisher shutdown was not clean and bounded"
fi
publisher_pid=''

if ! stop_process "$server_pid" server; then
  die "server shutdown was not clean and bounded"
fi
server_pid=''

trap - EXIT INT TERM
printf 'Local RTSP-over-TCP smoke test passed, including bounded shutdown.\n'
printf 'Logs: %s and %s\n' "$server_log" "$publisher_log"
