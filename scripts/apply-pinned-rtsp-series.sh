#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

: "${RTSP_PATCH_ROOT:=$ROOT_DIR/patches}"
: "${RTSP_PATCH_SERIES:=$ROOT_DIR/patches/series}"
: "${RTSP_PATCH_AUDIT:=${TMPDIR:-/tmp}/rtsp-on-ge-patch-audit.tsv}"
: "${RTSP_GE_DIFF_CHECK_BASELINE:?RTSP_GE_DIFF_CHECK_BASELINE is required}"
: "${RTSP_GE_DIFF_CHECK_STATUS:?RTSP_GE_DIFF_CHECK_STATUS is required}"
: "${RTSP_GE_SOURCE_ROOT:?RTSP_GE_SOURCE_ROOT is required}"
: "${RTSP_GE_MEDIA_CLEANUP_AUDIT:?RTSP_GE_MEDIA_CLEANUP_AUDIT is required}"

[[ -f "$RTSP_PATCH_SERIES" && ! -L "$RTSP_PATCH_SERIES" ]] \
  || pinned_ge_fail "RTSP patch series is not a regular file: $RTSP_PATCH_SERIES"
[[ -d dlls/winedmo ]] \
  || pinned_ge_fail "strict RTSP patch hook must run from the patched GE Wine tree"

entries_text="$(pinned_ge_series_entries "$RTSP_PATCH_ROOT" "$RTSP_PATCH_SERIES")"
ENTRIES=()
if [[ -n "$entries_text" ]]; then
  mapfile -t ENTRIES <<<"$entries_text"
fi
[[ "${#ENTRIES[@]}" -gt 0 ]] \
  || pinned_ge_fail "the RTSP patch series has no active entries"

mkdir -p "$(dirname "$RTSP_PATCH_AUDIT")"
for output in "$RTSP_PATCH_AUDIT" "$RTSP_GE_DIFF_CHECK_BASELINE" \
    "$RTSP_GE_DIFF_CHECK_STATUS" "$RTSP_GE_MEDIA_CLEANUP_AUDIT"; do
  [[ ! -L "$output" ]] || pinned_ge_fail "refusing symlinked RTSP audit output: $output"
done

# This current-GE-only cleanup is deliberately separate from the sixteen-patch
# RTSP/media test series. It repairs three exact deletion bodies omitted from
# GE's own WineGStreamer removal patch and fails if upstream changes them.
bash "$ROOT_DIR/scripts/normalize-pinned-ge-media-cleanup.sh"
media_cleanup_digest="$(pinned_ge_state_value \
  "$RTSP_GE_MEDIA_CLEANUP_AUDIT" normalization_digest)"

# GE's accepted snapshot contains known whitespace diagnostics outside this
# project's files. Capture that exact source-boundary output before applying
# the local series, then require the complete output to remain byte-identical.
# A separate path-scoped check below still requires every file touched by this
# project to be wholly clean.
set +e
git diff --check >"$RTSP_GE_DIFF_CHECK_BASELINE" 2>&1
ge_diff_check_status=$?
set -e
case "$ge_diff_check_status" in
  0|2) ;;
  *) pinned_ge_fail "unexpected GE baseline git diff --check status: $ge_diff_check_status" ;;
esac
printf '%s\n' "$ge_diff_check_status" >"$RTSP_GE_DIFF_CHECK_STATUS"

{
  printf 'patch_audit_version\t1\n'
  printf 'series_sha256\t%s\n' "$(pinned_ge_sha256 "$RTSP_PATCH_SERIES")"
  printf 'ge_baseline_diff_check_status\t%s\n' "$ge_diff_check_status"
  printf 'ge_baseline_diff_check_sha256\t%s\n' \
    "$(pinned_ge_sha256 "$RTSP_GE_DIFF_CHECK_BASELINE")"
  printf 'ge_media_cleanup_normalization_digest\t%s\n' \
    "$media_cleanup_digest"
} >"$RTSP_PATCH_AUDIT"

declare -A TOUCHED_PATH_MAP=()
TOUCHED_PATHS=()
NUMSTAT_FILE="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-numstat.XXXXXX")"
trap 'rm -f -- "$NUMSTAT_FILE"' EXIT

for entry in "${ENTRIES[@]}"; do
  patch_path="$RTSP_PATCH_ROOT/$entry"
  patch_sha256="$(pinned_ge_sha256 "$patch_path")"
  if ! git apply --check --whitespace=error-all -- "$patch_path"; then
    printf 'patch\t%s\t%s\tfailed-git-check\n' "$entry" "$patch_sha256" \
      >>"$RTSP_PATCH_AUDIT"
    pinned_ge_fail "Git rejected RTSP patch before GNU patch application: $entry"
  fi
  if ! git apply --numstat -z -- "$patch_path" >"$NUMSTAT_FILE"; then
    printf 'patch\t%s\t%s\tfailed-git-parse\n' "$entry" "$patch_sha256" \
      >>"$RTSP_PATCH_AUDIT"
    pinned_ge_fail "Git could not parse RTSP patch paths: $entry"
  fi
  if ! patch -Np1 --dry-run --batch --fuzz=0 --no-backup-if-mismatch \
      <"$patch_path" >/dev/null; then
    printf 'patch\t%s\t%s\tfailed-gnu-check\n' "$entry" "$patch_sha256" \
      >>"$RTSP_PATCH_AUDIT"
    pinned_ge_fail "GNU patch rejected RTSP patch during dry run: $entry"
  fi
  while IFS= read -r -d '' record; do
    path="${record#*$'\t'}"
    path="${path#*$'\t'}"
    [[ -n "$path" && "$path" != /* && "$path" != ../* && "$path" != */../* ]] \
      || pinned_ge_fail "unsafe path reported by RTSP patch: $entry: $path"
    if [[ -z "${TOUCHED_PATH_MAP[$path]+present}" ]]; then
      TOUCHED_PATH_MAP["$path"]=1
      TOUCHED_PATHS+=("$path")
    fi
  done <"$NUMSTAT_FILE"
  printf 'Applying RTSP-on-GE patch: %s\n' "$entry"
  if ! patch -Np1 --batch --fuzz=0 --no-backup-if-mismatch <"$patch_path"; then
    printf 'patch\t%s\t%s\tfailed\n' "$entry" "$patch_sha256" \
      >>"$RTSP_PATCH_AUDIT"
    pinned_ge_fail "strict RTSP patch failed: $entry"
  fi
  printf 'patch\t%s\t%s\tapplied\n' "$entry" "$patch_sha256" \
    >>"$RTSP_PATCH_AUDIT"
done

rm -f -- "$NUMSTAT_FILE"
trap - EXIT

if find . -path './.git' -prune -o -type f -name '*.rej' -print -quit \
    | grep -q .; then
  find . -path './.git' -prune -o -type f -name '*.rej' -print >&2
  pinned_ge_fail "a patch reject remains after the strict RTSP hook"
fi
[[ "${#TOUCHED_PATHS[@]}" -gt 0 ]] \
  || pinned_ge_fail "RTSP patch series reported no touched source paths"
git diff --check -- "${TOUCHED_PATHS[@]}"
for path in "${TOUCHED_PATHS[@]}"; do
  printf 'touched_path\t%s\n' "$path" >>"$RTSP_PATCH_AUDIT"
done

post_diff_check="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-diff-check.XXXXXX")"
trap 'rm -f -- "$post_diff_check"' EXIT
set +e
git diff --check >"$post_diff_check" 2>&1
post_diff_check_status=$?
set -e
[[ "$post_diff_check_status" == "$ge_diff_check_status" ]] \
  || pinned_ge_fail "RTSP series changed the complete diff-check exit status"
cmp -s -- "$RTSP_GE_DIFF_CHECK_BASELINE" "$post_diff_check" \
  || pinned_ge_fail "RTSP series changed the accepted GE diff-check output"
{
  printf 'touched_path_diff_check\tpassed\n'
  printf 'complete_diff_check_baseline_match\tpassed\n'
} >>"$RTSP_PATCH_AUDIT"
printf 'Strict RTSP-on-GE patch hook applied %s patch(es).\n' "${#ENTRIES[@]}"
