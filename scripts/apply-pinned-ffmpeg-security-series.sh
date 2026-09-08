#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
STATE_DIR=""
PATCH_ROOT="$ROOT_DIR/patches/ffmpeg-security"
PATCH_SERIES="$ROOT_DIR/patches/ffmpeg-security/series"

usage() {
  cat <<'EOF'
Usage: apply-pinned-ffmpeg-security-series.sh [OPTIONS]

Strictly apply the exact, byte-pinned FFmpeg security backport series to the
pinned FFmpeg submodule and emit a deterministic application audit.

Options:
  --source DIR      prepared GE checkout containing the FFmpeg submodule
  --state-dir DIR   preparation audit/output directory
  --patch-root DIR  root containing the FFmpeg security series
  --series FILE     FFmpeg security patch series
  --config FILE     pinned GE snapshot config
  -h, --help        show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --state-dir)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      STATE_DIR="$2"
      shift 2
      ;;
    --patch-root)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_ROOT="$2"
      shift 2
      ;;
    --series)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_SERIES="$2"
      shift 2
      ;;
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PINNED_GE_CONFIG="$2"
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

pinned_ge_load_config
[[ "$PIN_SCHEMA_VERSION" == 5 ]] \
  || pinned_ge_fail "FFmpeg security application requires a schema-5 pin"
[[ -n "$SOURCE_DIR" ]] || SOURCE_DIR="$(pinned_ge_default_source)"
[[ -n "$STATE_DIR" ]] || STATE_DIR="$(pinned_ge_default_state_dir)"
SOURCE_DIR="$(realpath -e -- "$SOURCE_DIR")"
STATE_DIR="$(realpath -m -- "$STATE_DIR")"
PATCH_ROOT="$(realpath -e -- "$PATCH_ROOT")"
PATCH_SERIES="$(realpath -e -- "$PATCH_SERIES")"
pinned_ge_require_safe_absolute_path "GE source path" "$SOURCE_DIR"
pinned_ge_require_safe_absolute_path "GE state path" "$STATE_DIR"
[[ "$PATCH_ROOT" == "$ROOT_DIR/patches/ffmpeg-security" \
    && "$PATCH_SERIES" == "$PATCH_ROOT/series" ]] \
  || pinned_ge_fail "FFmpeg security series must use the project-pinned location"
[[ -e "$SOURCE_DIR/ffmpeg/.git" ]] \
  || pinned_ge_fail "FFmpeg submodule is not initialized"
[[ "$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" rev-parse HEAD)" \
    == "$PIN_FFMPEG_COMMIT" ]] \
  || pinned_ge_fail "FFmpeg security series targets a different base commit"
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
    status --porcelain --untracked-files=all)" ]] \
  || pinned_ge_fail "FFmpeg must be pristine before security backports are applied"

records="$(pinned_ge_ffmpeg_security_series_records \
  "$PATCH_ROOT" "$PATCH_SERIES")"
[[ -n "$records" ]] || pinned_ge_fail "FFmpeg security series is empty"
fix_count="$(wc -l <<<"$records")"
expected_touched_paths="$(pinned_ge_ffmpeg_security_touched_paths \
  "$PATCH_ROOT" "$PATCH_SERIES")"
[[ -n "$expected_touched_paths" ]] \
  || pinned_ge_fail "FFmpeg security series has no touched source paths"
series_digest="$(pinned_ge_series_digest "$PATCH_ROOT" "$PATCH_SERIES")"

mkdir -p "$STATE_DIR"
AUDIT="$STATE_DIR/ffmpeg-security-patch-audit.tsv"
[[ ! -L "$AUDIT" ]] \
  || pinned_ge_fail "refusing symlinked FFmpeg security audit: $AUDIT"
audit_tmp="$(mktemp "$STATE_DIR/.ffmpeg-security-patch-audit.tsv.XXXXXX")"
trap 'rm -f -- "$audit_tmp"' EXIT

{
  printf 'ffmpeg_security_patch_audit_version\t1\n'
  printf 'base_commit\t%s\n' "$PIN_FFMPEG_COMMIT"
  printf 'fix_commits\t%s\n' "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
  printf 'fix_count\t%s\n' "$fix_count"
  printf 'series_path\tpatches/ffmpeg-security/series\n'
  printf 'series_sha256\t%s\n' "$PIN_FFMPEG_SECURITY_SERIES_SHA256"
  printf 'series_digest\t%s\n' "$series_digest"
} >"$audit_tmp"

while IFS=$'\t' read -r index entry fix patch_sha numstat_sha; do
  patch_file="$PATCH_ROOT/$entry"
  pinned_ge_git -C "$SOURCE_DIR/ffmpeg" apply --check \
    --whitespace=error-all -- "$patch_file"
  pinned_ge_git -C "$SOURCE_DIR/ffmpeg" apply \
    --whitespace=error-all -- "$patch_file"
  printf 'patch\t%s\t%s\t%s\t%s\t%s\n' \
    "$index" "$entry" "$fix" "$patch_sha" "$numstat_sha" \
    >>"$audit_tmp"
done <<<"$records"

pinned_ge_git -C "$SOURCE_DIR/ffmpeg" diff --check
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
    diff --name-only --cached --)" ]] \
  || pinned_ge_fail "FFmpeg security application unexpectedly staged changes"
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
    ls-files --others --exclude-standard)" ]] \
  || pinned_ge_fail "FFmpeg security application created untracked files"
touched_paths="$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
  diff --name-only HEAD -- | LC_ALL=C sort -u)"
[[ "$touched_paths" == "$expected_touched_paths" ]] \
  || pinned_ge_fail "FFmpeg security application changed unexpected paths"
magicyuv_blob="$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
  hash-object --no-filters -- libavcodec/magicyuv.c)"
[[ "$magicyuv_blob" == "$PIN_FFMPEG_SECURITY_MAGICYUV_BLOB" ]] \
  || pinned_ge_fail "patched MagicYUV source differs from the pinned final blob"
tls_version_blob="$(pinned_ge_git -C "$SOURCE_DIR/ffmpeg" \
  hash-object --no-filters -- libavformat/version_major.h)"
[[ "$tls_version_blob" == "$PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB" ]] \
  || pinned_ge_fail "patched TLS-default header differs from the pinned final blob"
repo_state="$(pinned_ge_repo_state_digest "$SOURCE_DIR/ffmpeg")"
{
  while IFS= read -r path; do
    printf 'touched_path\t%s\n' "$path"
  done <<<"$expected_touched_paths"
  printf 'final_magicyuv_blob\t%s\n' "$magicyuv_blob"
  printf 'final_tls_version_blob\t%s\n' "$tls_version_blob"
  printf 'ffmpeg_repo_state_sha256\t%s\n' "$repo_state"
  printf 'source_diff_check\tpassed\n'
  printf 'security_series_application\tpassed\n'
} >>"$audit_tmp"
chmod 0644 "$audit_tmp"
mv -f -- "$audit_tmp" "$AUDIT"
trap - EXIT

printf 'Applied pinned FFmpeg security series: %s\n' "$PATCH_SERIES"
printf 'FFmpeg security audit: %s\n' "$AUDIT"
