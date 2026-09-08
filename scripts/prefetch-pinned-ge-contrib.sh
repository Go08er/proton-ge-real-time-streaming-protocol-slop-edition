#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 022
export GIT_NO_LAZY_FETCH=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

CONFIG=""
SOURCE_DIR=""
CACHE_DIR=""
CURL_BIN="${PINNED_GE_CURL:-curl}"
TEMP_FILE=""

usage() {
  cat <<'EOF'
Usage: prefetch-pinned-ge-contrib.sh --config FILE --source DIR [OPTIONS]

Fetch every input declared by the selected pin's build-contrib manifest,
verify its mode, size, and SHA-256 digest, and place it under the prepared
source tree. Existing correct files are retained; mismatches are never
overwritten.

Required:
  --config FILE     exact pinned config
  --source DIR      matching prepared GE source tree

Options:
  --cache-dir DIR   reuse and populate a manifest-shaped local download cache
  -h, --help        show this help
EOF
}

cleanup() {
  if [[ -n "$TEMP_FILE" ]]; then
    rm -f -- "$TEMP_FILE"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      CONFIG="$2"
      shift 2
      ;;
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --cache-dir)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      CACHE_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$CONFIG" ]] || pinned_ge_fail "--config is required; no historical pin is selected implicitly"
[[ -n "$SOURCE_DIR" ]] || pinned_ge_fail "--source is required; no prepared tree is selected implicitly"

PINNED_GE_CONFIG="$CONFIG"
pinned_ge_load_config

[[ ! -L "$SOURCE_DIR" ]] \
  || pinned_ge_fail "prepared GE source must not be a symlink: $SOURCE_DIR"
SOURCE_DIR="$(realpath -e -- "$SOURCE_DIR")" \
  || pinned_ge_fail "prepared GE source is absent: $SOURCE_DIR"
pinned_ge_require_safe_absolute_path "prepared GE source" "$SOURCE_DIR"
[[ -d "$SOURCE_DIR" && -e "$SOURCE_DIR/.git" ]] \
  || pinned_ge_fail "prepared GE source is not a Git checkout: $SOURCE_DIR"
actual_commit="$(pinned_ge_git -C "$SOURCE_DIR" \
  rev-parse --verify 'HEAD^{commit}')"
[[ "$actual_commit" == "$PIN_SOURCE_COMMIT" ]] \
  || pinned_ge_fail \
    "prepared GE source is at $actual_commit, expected $PIN_SOURCE_COMMIT for $PIN_ID"

if [[ -n "$CACHE_DIR" ]]; then
  [[ ! -L "$CACHE_DIR" ]] \
    || pinned_ge_fail "build-input cache must not be a symlink: $CACHE_DIR"
  CACHE_DIR="$(realpath -m -- "$CACHE_DIR")"
  pinned_ge_require_safe_absolute_path "build-input cache" "$CACHE_DIR"
  if [[ -e "$CACHE_DIR" ]]; then
    [[ -d "$CACHE_DIR" && ! -L "$CACHE_DIR" ]] \
      || pinned_ge_fail \
        "build-input cache is not a regular directory: $CACHE_DIR"
  else
    mkdir -p -- "$CACHE_DIR"
  fi
  CACHE_DIR="$(realpath -e -- "$CACHE_DIR")"
fi

CURL_BIN="$(command -v "$CURL_BIN")" \
  || pinned_ge_fail "curl is required to fetch absent build inputs"
CONTRIB_MANIFEST="$(pinned_ge_build_contrib_manifest_path)"
fetch_records="$(pinned_ge_build_contrib_fetch_entries "$CONTRIB_MANIFEST")"
mapfile -t FETCH_RECORDS <<<"$fetch_records"

require_exact_file() {
  local label="$1"
  local file="$2"
  local display_path="$3"
  local expected_mode="$4"
  local expected_size="$5"
  local expected_sha="$6"
  local actual_mode actual_size actual_sha

  [[ -f "$file" && ! -L "$file" ]] \
    || pinned_ge_fail \
      "$label is not a regular file: $display_path" || return 1
  actual_mode="0$(stat -c '%a' -- "$file")"
  actual_size="$(stat -c '%s' -- "$file")"
  actual_sha="$(pinned_ge_sha256 "$file")"
  [[ "$actual_mode" == "$expected_mode" \
      && "$actual_size" == "$expected_size" \
      && "$actual_sha" == "$expected_sha" ]] \
    || pinned_ge_fail \
      "$label differs for $display_path (mode $actual_mode, size $actual_size, SHA-256 $actual_sha; expected $expected_mode, $expected_size, $expected_sha)" \
    || return 1
}

stage_exact_file() {
  local source_file="$1"
  local destination="$2"
  local display_path="$3"
  local expected_mode="$4"
  local expected_size="$5"
  local expected_sha="$6"
  local destination_dir

  destination_dir="$(dirname "$destination")"
  mkdir -p -- "$destination_dir"
  TEMP_FILE="$(mktemp "$destination_dir/.$(basename "$destination").XXXXXX")"
  cp -- "$source_file" "$TEMP_FILE"
  chmod "$expected_mode" "$TEMP_FILE"
  require_exact_file \
    "staged build input" "$TEMP_FILE" "$display_path" \
    "$expected_mode" "$expected_size" "$expected_sha"
  mv -- "$TEMP_FILE" "$destination"
  TEMP_FILE=""
}

present_count=0
cache_count=0
download_count=0

for record in "${FETCH_RECORDS[@]}"; do
  IFS=$'\t' read -r path expected_mode expected_size expected_sha url <<<"$record"
  destination="$SOURCE_DIR/$path"

  if pinned_ge_git -C "$SOURCE_DIR" ls-files --error-unmatch -- "$path" \
      >/dev/null 2>&1; then
    pinned_ge_fail \
      "declared build input is tracked by the pinned source and cannot be prefetched: $path"
  fi

  if [[ -e "$destination" || -L "$destination" ]]; then
    require_exact_file \
      "existing build input; refusing to overwrite it" \
      "$destination" "$path" \
      "$expected_mode" "$expected_size" "$expected_sha"
    printf 'Present:    %s\n' "$path"
    present_count=$((present_count + 1))
    continue
  fi

  cache_file=""
  if [[ -n "$CACHE_DIR" ]]; then
    cache_file="$CACHE_DIR/$path"
    if [[ -e "$cache_file" || -L "$cache_file" ]]; then
      require_exact_file \
        "cached build input; refusing to replace it" \
        "$cache_file" "$path" \
        "$expected_mode" "$expected_size" "$expected_sha"
      stage_exact_file \
        "$cache_file" "$destination" "$path" \
        "$expected_mode" "$expected_size" "$expected_sha"
      printf 'From cache: %s\n' "$path"
      cache_count=$((cache_count + 1))
      continue
    fi
  fi

  if [[ -n "$cache_file" ]]; then
    download_target="$cache_file"
  else
    download_target="$destination"
  fi
  download_dir="$(dirname "$download_target")"
  mkdir -p -- "$download_dir"
  TEMP_FILE="$(mktemp "$download_dir/.$(basename "$download_target").XXXXXX")"
  printf 'Fetching:   %s\n' "$path"
  "$CURL_BIN" --fail --location --silent --show-error \
    --output "$TEMP_FILE" -- "$url"
  chmod "$expected_mode" "$TEMP_FILE"
  require_exact_file \
    "downloaded build input" "$TEMP_FILE" "$path" \
    "$expected_mode" "$expected_size" "$expected_sha"
  mv -- "$TEMP_FILE" "$download_target"
  TEMP_FILE=""

  if [[ "$download_target" != "$destination" ]]; then
    stage_exact_file \
      "$download_target" "$destination" "$path" \
      "$expected_mode" "$expected_size" "$expected_sha"
  fi
  download_count=$((download_count + 1))
done

pinned_ge_require_complete_build_contrib "$SOURCE_DIR" "$CONTRIB_MANIFEST"
printf 'Build inputs complete for %s: %d already present, %d from cache, %d fetched.\n' \
  "$PIN_ID" "$present_count" "$cache_count" "$download_count"
