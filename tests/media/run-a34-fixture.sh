#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKSPACE_DIR="$(cd "$PROJECT_DIR/.." && pwd)"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

DEFAULT_MEDIAMTX_BIN="$WORKSPACE_DIR/.test-tools/mediamtx-v1.19.2/mediamtx"
DEFAULT_FIXTURE_PATH="$WORKSPACE_DIR/.test-tools/fixtures/http-vod-faststart-45s.mp4"
RTSPT_FIXTURE_URL="rtspt://127.0.0.1:8554/fixture"
HLS_LIVE_CONFIG="$SCRIPT_DIR/mediamtx-hls-live.yml"
HLS_LIVE_URL="http://127.0.0.1:8888/fixture/index.m3u8"

server_pid=''
publisher_pid=''
server_log=''
publisher_log=''

usage() {
  cat <<EOF
Usage: $(basename "$0") [--check] [--hls-live]
       $(basename "$0") --help

Start a loopback-only MediaMTX server and continuously publish the pinned
H.264/AAC fixture over RTSP interleaved TCP. The launcher stays in the
foreground until Ctrl-C and then stops every child process it started.

Options:
  --check  Validate tools, fixture codecs, configuration, and TCP port 8554,
           then print the URLs without starting any process.
  --hls-live
           Also expose the looping fixture as compatibility-oriented HLS live
           on IPv4 loopback. This opt-in mode checks TCP ports 8554 and 8888,
           and waits for the HLS URL to decode both audio and video.
  --help   Show this help and exit.

Optional environment overrides:
  MEDIAMTX_BIN       MediaMTX executable
  FFMPEG_BIN         FFmpeg executable (default: ffmpeg on PATH)
  FFPROBE_BIN        FFprobe executable (default: ffprobe on PATH)
  TIMEOUT_BIN        GNU timeout executable (default: timeout on PATH)
  RTSP_FIXTURE_FILE  Existing H.264/AAC media fixture

VRChat test URLs:
  $RTSP_FIXTURE_URL
  $RTSPT_FIXTURE_URL
  $HLS_LIVE_URL  (--hls-live only)

This helper performs no downloads and does not inspect, launch, or modify
Steam, Proton, VRChat, or compatdata.
EOF
}

resolve_executable() {
  local candidate="$1"
  local label="$2"

  if [[ "$candidate" == */* ]]; then
    [[ -f "$candidate" ]] || die "$label executable does not exist: $candidate"
    [[ -x "$candidate" ]] || die "$label file is not executable: $candidate"
    printf '%s\n' "$candidate"
    return
  fi

  command -v "$candidate" 2>/dev/null ||
    die "$label is unavailable; put $candidate on PATH or set its explicit override"
}

port_is_open() {
  local port="$1"

  (exec 3<>"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
}

print_log_tail() {
  local label="$1"
  local path="$2"

  [[ -n "$path" && -s "$path" ]] || return 0
  printf '%s log tail (%s):\n' "$label" "$path" >&2
  tail -n 40 -- "$path" >&2 || true
}

# Reached from the EXIT trap through cleanup().
# shellcheck disable=SC2329
stop_process() {
  local pid="$1"
  local label="$2"
  local attempt

  [[ -n "$pid" ]] || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi

  kill -TERM "$pid" 2>/dev/null || true
  for ((attempt = 0; attempt < 50; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done

  printf 'WARNING: %s did not stop within five seconds; sending KILL\n' "$label" >&2
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

# Installed as the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  local status=$?

  trap - EXIT INT TERM HUP
  set +e
  stop_process "$publisher_pid" 'FFmpeg publisher'
  stop_process "$server_pid" 'MediaMTX server'
  exit "$status"
}

# Installed as the INT, TERM, and HUP traps below.
# shellcheck disable=SC2329
on_signal() {
  local signal_name="$1"
  local status="$2"

  trap - "$signal_name"
  printf '\nStopping local media fixture...\n' >&2
  exit "$status"
}

print_urls() {
  printf 'RTSP URL:  %s\n' "$RTSP_FIXTURE_URL"
  printf 'RTSPT URL: %s\n' "$RTSPT_FIXTURE_URL"
  if [[ "$hls_live" == true ]]; then
    printf 'HLS live:  %s\n' "$HLS_LIVE_URL"
  fi
}

mode='run'
hls_live=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      [[ "$mode" != check ]] || die '--check was supplied more than once'
      mode='check'
      ;;
    --hls-live)
      [[ "$hls_live" != true ]] || die '--hls-live was supplied more than once'
      hls_live=true
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
  shift
done

mediamtx_config="$MEDIAMTX_CONFIG"
if [[ "$hls_live" == true ]]; then
  mediamtx_config="$HLS_LIVE_CONFIG"
fi

mediamtx_bin="$(resolve_executable "${MEDIAMTX_BIN:-$DEFAULT_MEDIAMTX_BIN}" MediaMTX)"
ffmpeg_bin="$(resolve_executable "${FFMPEG_BIN:-ffmpeg}" FFmpeg)"
ffprobe_bin="$(resolve_executable "${FFPROBE_BIN:-ffprobe}" FFprobe)"
timeout_bin="$(resolve_executable "${TIMEOUT_BIN:-timeout}" 'GNU timeout')"
fixture_path="${RTSP_FIXTURE_FILE:-$DEFAULT_FIXTURE_PATH}"

[[ -r "$mediamtx_config" ]] || die "MediaMTX configuration is unreadable: $mediamtx_config"
[[ -s "$fixture_path" ]] || die "H.264/AAC fixture is missing or empty: $fixture_path"

config_text="$(<"$mediamtx_config")"
[[ "$config_text" == *'rtspAddress: 127.0.0.1:8554'* ]] ||
  die 'MediaMTX configuration must bind RTSP to 127.0.0.1:8554'
[[ "$config_text" == *'rtspTransports: [tcp]'* ]] ||
  die 'MediaMTX configuration must restrict this fixture to RTSP-over-TCP'
disabled_services=(api metrics pprof playback rtmp webrtc srt moq)
if [[ "$hls_live" != true ]]; then
  disabled_services+=(hls)
fi
for disabled_service in "${disabled_services[@]}"; do
  [[ "$config_text" == *"$disabled_service: false"* ]] ||
    die "MediaMTX configuration must explicitly disable $disabled_service"
done
if [[ "$hls_live" == true ]]; then
  for required_setting in \
    'hls: true' \
    'hlsAddress: 127.0.0.1:8888' \
    'hlsEncryption: false' \
    'hlsAlwaysRemux: true' \
    'hlsVariant: mpegts' \
    'hlsSegmentCount: 7' \
    'hlsSegmentDuration: 2s' \
    'hlsDirectory: ""'; do
    [[ "$config_text" == *"$required_setting"* ]] ||
      die "HLS-live configuration is missing required setting: $required_setting"
  done
fi

video_codec="$("$ffprobe_bin" -v error -select_streams v:0 \
  -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 \
  "$fixture_path")" || die "FFprobe could not inspect video in: $fixture_path"
audio_codec="$("$ffprobe_bin" -v error -select_streams a:0 \
  -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 \
  "$fixture_path")" || die "FFprobe could not inspect audio in: $fixture_path"
[[ "$video_codec" == h264 ]] ||
  die "fixture video must be H.264, found: ${video_codec:-no video stream}"
[[ "$audio_codec" == aac ]] ||
  die "fixture audio must be AAC, found: ${audio_codec:-no audio stream}"

if port_is_open 8554; then
  die 'TCP port 127.0.0.1:8554 is already in use; stop the existing listener before starting this fixture'
fi
if [[ "$hls_live" == true ]] && port_is_open 8888; then
  die 'TCP port 127.0.0.1:8888 is already in use; stop the existing listener before starting this fixture'
fi

if [[ "$mode" == check ]]; then
  if [[ "$hls_live" == true ]]; then
    printf 'A3.4 local RTSP + HLS-live fixture preflight passed; no process was started.\n'
  else
    printf 'A3.4 local RTSP fixture preflight passed; no process was started.\n'
  fi
  printf 'MediaMTX: %s\n' "$mediamtx_bin"
  printf 'FFmpeg:   %s\n' "$ffmpeg_bin"
  printf 'Fixture:  %s\n' "$fixture_path"
  print_urls
  exit 0
fi

mkdir -p "$GENERATED_DIR"
server_log="$GENERATED_DIR/a34-mediamtx.log"
publisher_log="$GENERATED_DIR/a34-publisher.log"
: >"$server_log"
: >"$publisher_log"

trap cleanup EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP

"$mediamtx_bin" "$mediamtx_config" >"$server_log" 2>&1 &
server_pid=$!

server_ready=false
for ((attempt = 0; attempt < 50; attempt++)); do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid" 2>/dev/null || true
    print_log_tail MediaMTX "$server_log"
    die 'MediaMTX exited before opening 127.0.0.1:8554'
  fi
  if port_is_open 8554; then
    server_ready=true
    break
  fi
  sleep 0.1
done
if [[ "$server_ready" != true ]]; then
  print_log_tail MediaMTX "$server_log"
  die 'MediaMTX did not open 127.0.0.1:8554 within five seconds'
fi

"$ffmpeg_bin" -hide_banner -nostdin -loglevel warning \
  -re -stream_loop -1 -i "$fixture_path" \
  -map 0:v:0 -map 0:a:0 -c copy \
  -f rtsp -rtsp_transport tcp -muxdelay 0.1 "$RTSP_FIXTURE_URL" \
  >"$publisher_log" 2>&1 &
publisher_pid=$!

stream_ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if ! kill -0 "$publisher_pid" 2>/dev/null; then
    wait "$publisher_pid" 2>/dev/null || true
    print_log_tail 'FFmpeg publisher' "$publisher_log"
    print_log_tail MediaMTX "$server_log"
    die 'FFmpeg exited before publishing the fixture'
  fi

  stream_types="$("$timeout_bin" --foreground 1s "$ffprobe_bin" -v error \
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
  print_log_tail 'FFmpeg publisher' "$publisher_log"
  print_log_tail MediaMTX "$server_log"
  die 'published stream did not expose both H.264 video and AAC audio in time'
fi

if [[ "$hls_live" == true ]]; then
  hls_ready=false
  for ((attempt = 0; attempt < 4; attempt++)); do
    if ! kill -0 "$publisher_pid" 2>/dev/null; then
      wait "$publisher_pid" 2>/dev/null || true
      print_log_tail 'FFmpeg publisher' "$publisher_log"
      print_log_tail MediaMTX "$server_log"
      die 'FFmpeg exited before the HLS-live fixture became ready'
    fi

    if "$timeout_bin" --foreground 8s "$ffmpeg_bin" -hide_banner -nostdin \
      -loglevel error -i "$HLS_LIVE_URL" \
      -map 0:v:0 -map 0:a:0 -t 1 -f null - >/dev/null 2>&1; then
      hls_ready=true
      break
    fi
    sleep 0.25
  done
  if [[ "$hls_ready" != true ]]; then
    print_log_tail 'FFmpeg publisher' "$publisher_log"
    print_log_tail MediaMTX "$server_log"
    die 'HLS-live stream did not decode both H.264 video and AAC audio in time'
  fi
fi

if [[ "$hls_live" == true ]]; then
  printf '\nA3.4 local H.264/AAC RTSP + HLS-live fixture is ready for VRChat.\n'
else
  printf '\nA3.4 local H.264/AAC RTSP fixture is ready for VRChat.\n'
fi
print_urls
printf 'Press Ctrl-C here to stop it. Logs: %s and %s\n\n' \
  "$server_log" "$publisher_log"

set +e
wait -n "$server_pid" "$publisher_pid"
child_status=$?
set -e

print_log_tail 'FFmpeg publisher' "$publisher_log"
print_log_tail MediaMTX "$server_log"
die "MediaMTX or FFmpeg exited unexpectedly (status $child_status)"
