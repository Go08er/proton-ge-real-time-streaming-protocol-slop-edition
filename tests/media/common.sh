#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause

MEDIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATED_DIR="$MEDIA_DIR/generated"
FIXTURE_PATH="$GENERATED_DIR/synthetic-av.mkv"
# These are consumed by scripts which source this shared file.
# shellcheck disable=SC2034
MEDIAMTX_CONFIG="$MEDIA_DIR/mediamtx.yml"
# shellcheck disable=SC2034
RTSP_FIXTURE_URL="rtsp://127.0.0.1:8554/fixture"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  local command_name="$1"

  command -v "$command_name" >/dev/null 2>&1 ||
    die "required command is unavailable: $command_name"
}

resolve_mediamtx() {
  local candidate="${MEDIAMTX_BIN:-mediamtx}"

  if [[ "$candidate" == */* ]]; then
    [[ -f "$candidate" ]] || die "MediaMTX executable does not exist: $candidate"
    [[ -x "$candidate" ]] || die "MediaMTX file is not executable: $candidate"
    printf '%s\n' "$candidate"
    return
  fi

  command -v "$candidate" 2>/dev/null ||
    die "MediaMTX is unavailable; put mediamtx on PATH or set MEDIAMTX_BIN to its executable"
}

require_fixture() {
  [[ -s "$FIXTURE_PATH" ]] ||
    die "synthetic fixture is missing; run $MEDIA_DIR/generate-fixture.sh first"
}
