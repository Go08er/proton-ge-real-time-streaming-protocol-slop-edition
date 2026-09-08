#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

: "${RTSP_GE_SOURCE_ROOT:?RTSP_GE_SOURCE_ROOT is required}"
: "${RTSP_GE_MEDIA_CLEANUP_AUDIT:?RTSP_GE_MEDIA_CLEANUP_AUDIT is required}"

pinned_ge_load_config
SOURCE_ROOT="$(realpath -e -- "$RTSP_GE_SOURCE_ROOT")"
[[ "$PWD" == "$SOURCE_ROOT/wine" ]] \
  || pinned_ge_fail "GE media cleanup must run from the pinned Wine tree"

MANIFEST="$(realpath -e -- "$ROOT_DIR/$PIN_GE_MEDIA_CLEANUP_MANIFEST")"
[[ "$MANIFEST" == "$ROOT_DIR/config/"* && -f "$MANIFEST" && ! -L "$MANIFEST" ]] \
  || pinned_ge_fail "GE media-cleanup manifest is not a regular project config file"
manifest_sha256="$(pinned_ge_sha256 "$MANIFEST")"
[[ "$manifest_sha256" == "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256" ]] \
  || pinned_ge_fail "GE media-cleanup manifest differs from its pinned digest"
[[ ! -L "$RTSP_GE_MEDIA_CLEANUP_AUDIT" ]] \
  || pinned_ge_fail "refusing symlinked GE media-cleanup audit output"

manifest_version=""
manifest_commit=""
upstream_patch_rel=""
upstream_patch_sha256=""
DELETE_PATHS=()
DELETE_BLOBS=()
DELETE_SHA256S=()
declare -A DELETE_PATH_MAP=()

while IFS=$'\t' read -r kind field1 field2 field3 extra \
    || [[ -n "${kind}${field1}${field2}${field3}${extra}" ]]; do
  [[ -z "$extra" ]] || pinned_ge_fail "extra field in GE media-cleanup manifest"
  case "$kind" in
    media_cleanup_version)
      [[ -z "$manifest_version" && -n "$field1" && -z "$field2$field3" ]] \
        || pinned_ge_fail "invalid or duplicate media-cleanup version"
      manifest_version="$field1"
      ;;
    source_commit)
      [[ -z "$manifest_commit" && -n "$field1" && -z "$field2$field3" ]] \
        || pinned_ge_fail "invalid or duplicate media-cleanup source commit"
      manifest_commit="$field1"
      ;;
    upstream_patch)
      [[ -z "$upstream_patch_rel" && -n "$field1" && -z "$field2$field3" ]] \
        || pinned_ge_fail "invalid or duplicate media-cleanup upstream patch"
      upstream_patch_rel="$field1"
      ;;
    upstream_patch_sha256)
      [[ -z "$upstream_patch_sha256" && -n "$field1" && -z "$field2$field3" ]] \
        || pinned_ge_fail "invalid or duplicate media-cleanup patch digest"
      upstream_patch_sha256="$field1"
      ;;
    delete)
      [[ -n "$field1" && "$field2" =~ ^[0-9a-f]{40}$ \
          && "$field3" =~ ^[0-9a-f]{64}$ ]] \
        || pinned_ge_fail "invalid media-cleanup delete record"
      [[ -z "${DELETE_PATH_MAP[$field1]+present}" ]] \
        || pinned_ge_fail "duplicate media-cleanup path: $field1"
      case "$field1" in
        dlls/winegstreamer/*) ;;
        *) pinned_ge_fail "media cleanup may delete only WineGStreamer paths: $field1" ;;
      esac
      case "$field1" in
        /*|../*|*/../*|*/..|..) pinned_ge_fail "unsafe media-cleanup path: $field1" ;;
      esac
      DELETE_PATH_MAP["$field1"]=1
      DELETE_PATHS+=("$field1")
      DELETE_BLOBS+=("$field2")
      DELETE_SHA256S+=("$field3")
      ;;
    *) pinned_ge_fail "unknown GE media-cleanup manifest record: $kind" ;;
  esac
done <"$MANIFEST"

[[ "$manifest_version" == 1 ]] \
  || pinned_ge_fail "unsupported GE media-cleanup manifest version"
[[ "$manifest_commit" == "$PIN_SOURCE_COMMIT" ]] \
  || pinned_ge_fail "GE media cleanup is pinned to a different source commit"
[[ "${#DELETE_PATHS[@]}" == 3 ]] \
  || pinned_ge_fail "GE media cleanup must contain exactly three accepted omissions"
[[ "$upstream_patch_rel" == patches/ge-video-rework/* ]] \
  || pinned_ge_fail "GE media-cleanup evidence is not the video-removal patch"
[[ "$upstream_patch_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || pinned_ge_fail "GE media-cleanup upstream patch digest is malformed"
UPSTREAM_PATCH="$(realpath -e -- "$SOURCE_ROOT/$upstream_patch_rel")"
[[ "$UPSTREAM_PATCH" == "$SOURCE_ROOT/patches/ge-video-rework/"* \
    && -f "$UPSTREAM_PATCH" && ! -L "$UPSTREAM_PATCH" ]] \
  || pinned_ge_fail "GE media-cleanup evidence patch escaped the pinned source"
[[ "$(pinned_ge_sha256 "$UPSTREAM_PATCH")" == "$upstream_patch_sha256" ]] \
  || pinned_ge_fail "GE media-removal patch differs from the cleanup manifest"

for index in "${!DELETE_PATHS[@]}"; do
  path="${DELETE_PATHS[$index]}"
  blob="${DELETE_BLOBS[$index]}"
  content_sha256="${DELETE_SHA256S[$index]}"
  [[ -f "$path" && ! -L "$path" ]] \
    || pinned_ge_fail "expected dormant WineGStreamer source is absent or unsafe: $path"
  [[ "$(pinned_ge_git rev-parse "HEAD:$path")" == "$blob" ]] \
    || pinned_ge_fail "base blob changed for GE media-cleanup path: $path"
  [[ "$(pinned_ge_git hash-object -- "$path")" == "$blob" ]] \
    || pinned_ge_fail "GE patching modified a media-cleanup path: $path"
  [[ "$(pinned_ge_sha256 "$path")" == "$content_sha256" ]] \
    || pinned_ge_fail "content digest changed for GE media-cleanup path: $path"
  [[ "$(grep -Fxc " delete mode 100644 $path" "$UPSTREAM_PATCH")" == 1 ]] \
    || pinned_ge_fail "GE removal patch does not advertise exactly one deletion: $path"
  if grep -Fq "diff --git a/$path b/$path" "$UPSTREAM_PATCH"; then
    pinned_ge_fail "GE removal patch now contains a real diff body; drop the local cleanup: $path"
  fi
done

set +e
external_references="$(
  pinned_ge_git grep -n -I -e winegstreamer -- . \
    ':!dlls/winegstreamer/**' ':!configure' 2>&1
)"
reference_status=$?
set -e
case "$reference_status" in
  1) ;;
  0)
    printf '%s\n' "$external_references" >&2
    pinned_ge_fail "WineGStreamer is still referenced outside its dormant source directory"
    ;;
  *)
    printf '%s\n' "$external_references" >&2
    pinned_ge_fail "could not prove dormant WineGStreamer sources are unreferenced"
    ;;
esac

for path in "${DELETE_PATHS[@]}"; do
  rm -- "$path"
done

survivors="$(
  find dlls/winegstreamer -type f ! -name '*.orig' ! -name '*.rej' -print \
    | LC_ALL=C sort
)"
[[ -z "$survivors" ]] || {
  printf '%s\n' "$survivors" >&2
  pinned_ge_fail "non-evidence WineGStreamer source survived pinned cleanup"
}

audit_tmp="$(mktemp "$(dirname "$RTSP_GE_MEDIA_CLEANUP_AUDIT")/.ge-media-cleanup.XXXXXX")"
trap 'rm -f -- "$audit_tmp"' EXIT
{
  printf 'media_cleanup_audit_version\t1\n'
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  printf 'normalization_digest\t%s\n' "$manifest_sha256"
  printf 'manifest_sha256\t%s\n' "$manifest_sha256"
  printf 'upstream_patch\t%s\n' "$upstream_patch_rel"
  printf 'upstream_patch_sha256\t%s\n' "$upstream_patch_sha256"
  printf 'external_reference_check\tpassed\n'
  printf 'generated_configure_reference_check\tdeferred-until-autoreconf\n'
  for index in "${!DELETE_PATHS[@]}"; do
    printf 'deleted\t%s\t%s\t%s\n' \
      "${DELETE_PATHS[$index]}" "${DELETE_BLOBS[$index]}" \
      "${DELETE_SHA256S[$index]}"
  done
  printf 'remaining_non_evidence_winegstreamer_files\t0\n'
  printf 'media_cleanup\tpassed\n'
} >"$audit_tmp"
mv -- "$audit_tmp" "$RTSP_GE_MEDIA_CLEANUP_AUDIT"
trap - EXIT

printf 'Pinned current-GE WineGStreamer cleanup removed %s dormant file(s).\n' \
  "${#DELETE_PATHS[@]}"
