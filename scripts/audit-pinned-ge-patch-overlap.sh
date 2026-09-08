#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

: "${RTSP_GE_SOURCE_ROOT:?RTSP_GE_SOURCE_ROOT is required}"
: "${RTSP_GE_PATCH_OVERLAP_AUDIT:?RTSP_GE_PATCH_OVERLAP_AUDIT is required}"

pinned_ge_load_config
[[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]] \
  || pinned_ge_fail "selected pin does not authorize a GE patch-overlap exception"

SOURCE_ROOT="$(realpath -e -- "$RTSP_GE_SOURCE_ROOT")"
[[ ! -L "$RTSP_GE_PATCH_OVERLAP_AUDIT" ]] \
  || pinned_ge_fail "refusing symlinked GE patch-overlap audit output"
AUDIT="$(realpath -m -- "$RTSP_GE_PATCH_OVERLAP_AUDIT")"
MANIFEST="$(pinned_ge_patch_overlap_manifest_path)"
PATCH_RECORDS="$(pinned_ge_patch_overlap_records "$MANIFEST")"
COMPONENT_PATH="$(pinned_ge_state_value "$MANIFEST" component_path)"
COMPONENT="$SOURCE_ROOT/$COMPONENT_PATH"
COMPONENT_COMMIT="$(pinned_ge_state_value "$MANIFEST" component_commit)"
PARENT_COMMIT="$(pinned_ge_state_value "$MANIFEST" component_parent_commit)"
TARGET_PATH="$(pinned_ge_state_value "$MANIFEST" target_path)"
PARENT_BLOB="$(pinned_ge_state_value "$MANIFEST" component_parent_target_blob)"
EFFECTIVE_BLOB="$(pinned_ge_state_value "$MANIFEST" effective_target_blob)"
EFFECTIVE_SHA256="$(pinned_ge_state_value "$MANIFEST" effective_target_sha256)"
DECLARED_BASE_BLOB="$(pinned_ge_state_value "$MANIFEST" declared_series_base_blob)"
DECLARED_INTERMEDIATE_BLOB="$(
  pinned_ge_state_value "$MANIFEST" declared_series_intermediate_blob
)"
DECLARED_TARGET_BLOB="$(
  pinned_ge_state_value "$MANIFEST" declared_series_target_blob
)"
DECLARED_TARGET_SHA256="$(
  pinned_ge_state_value "$MANIFEST" declared_series_target_sha256
)"
RESIDUAL_ADDITIONS="$(pinned_ge_state_value "$MANIFEST" residual_additions)"
RESIDUAL_DELETIONS="$(pinned_ge_state_value "$MANIFEST" residual_deletions)"
RESIDUAL_DIFF_SHA256="$(pinned_ge_state_value "$MANIFEST" residual_diff_sha256)"

[[ "$(pwd -P)" == "$(cd "$COMPONENT" && pwd -P)" ]] \
  || pinned_ge_fail \
    "GE patch-overlap hook must run from the exact protonfixes submodule"
[[ ! -L "$AUDIT" ]] \
  || pinned_ge_fail "refusing symlinked GE patch-overlap audit target"

pinned_ge_require_source_identity "$SOURCE_ROOT"
pinned_ge_require_registered_submodules "$SOURCE_ROOT"
[[ "$(pinned_ge_git -C "$SOURCE_ROOT" rev-parse HEAD:protonfixes)" \
      == "$COMPONENT_COMMIT" \
    && "$(pinned_ge_git -C "$COMPONENT" rev-parse HEAD)" == "$COMPONENT_COMMIT" \
    && "$(pinned_ge_git -C "$COMPONENT" rev-parse HEAD^)" == "$PARENT_COMMIT" \
    && "$(pinned_ge_git -C "$COMPONENT" rev-parse "HEAD:$TARGET_PATH")" \
      == "$EFFECTIVE_BLOB" \
    && "$(pinned_ge_git -C "$COMPONENT" rev-parse \
      "$PARENT_COMMIT:$TARGET_PATH")" == "$PARENT_BLOB" \
    && "$(pinned_ge_git -C "$COMPONENT" hash-object --no-filters -- \
      "$TARGET_PATH")" == "$EFFECTIVE_BLOB" \
    && "$(pinned_ge_sha256 "$COMPONENT/$TARGET_PATH")" == "$EFFECTIVE_SHA256" \
    && -z "$(pinned_ge_git -C "$COMPONENT" \
      status --porcelain --untracked-files=all)" ]] \
  || pinned_ge_fail \
    "protonfixes does not match the exact reviewed GE overlap boundary"
COMPONENT_STATE_BEFORE="$(pinned_ge_repo_state_digest "$COMPONENT")"

COMPONENT_DELTA_PATHS="$(
  pinned_ge_git -C "$COMPONENT" diff --name-only "$PARENT_COMMIT..HEAD"
)"
[[ "$COMPONENT_DELTA_PATHS" == "$TARGET_PATH" ]] \
  || pinned_ge_fail \
    "reviewed protonfixes integration commit changes more than upscalers.py"

EXPECTED_PATCH_PATHS="$(printf '%s\n' "$PATCH_RECORDS" | cut -f1)"
ACTUAL_PATCH_PATHS="$(
  find "$SOURCE_ROOT/patches/protonfixes" -maxdepth 1 -type f \
    -name '*.patch' -print \
    | sed "s|^$SOURCE_ROOT/||" \
    | LC_ALL=C sort
)"
[[ "$ACTUAL_PATCH_PATHS" == "$EXPECTED_PATCH_PATHS" ]] \
  || pinned_ge_fail \
    "GE protonfixes patch directory differs from the exact two-patch overlap"

PROOF_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-overlap-proof.XXXXXX")"
TEMP_AUDIT=""
trap 'rm -rf -- "$PROOF_DIR"; [[ -z "$TEMP_AUDIT" ]] || rm -f -- "$TEMP_AUDIT"' EXIT
pinned_ge_git -C "$COMPONENT" cat-file blob "$DECLARED_BASE_BLOB" \
  >"$PROOF_DIR/$TARGET_PATH"

patch_index=0
while IFS=$'\t' read -r patch_path patch_sha additions deletions; do
  patch_index=$((patch_index + 1))
  patch_file="$SOURCE_ROOT/$patch_path"
  [[ -f "$patch_file" && ! -L "$patch_file" \
      && "$(pinned_ge_sha256 "$patch_file")" == "$patch_sha" ]] \
    || pinned_ge_fail "GE overlap patch differs from its review: $patch_path"
  numstat="$(pinned_ge_git -C "$SOURCE_ROOT" apply --numstat -- "$patch_file")" \
    || pinned_ge_fail "could not parse GE overlap patch: $patch_path"
  IFS=$'\t' read -r actual_add actual_del actual_path extra <<<"$numstat"
  [[ "$actual_add" == "$additions" && "$actual_del" == "$deletions" \
      && "$actual_path" == "$TARGET_PATH" && -z "$extra" \
      && "$(wc -l <<<"$numstat")" == 1 ]] \
    || pinned_ge_fail "GE overlap patch scope differs from review: $patch_path"
  if pinned_ge_git -C "$COMPONENT" apply --check --whitespace=error-all -- \
      "$patch_file" >/dev/null 2>&1; then
    pinned_ge_fail \
      "GE overlap patch unexpectedly applies to the reviewed effective blob: $patch_path"
  fi
  (
    cd "$PROOF_DIR"
    patch -Np1 --batch --fuzz=0 --no-backup-if-mismatch <"$patch_file"
  ) >/dev/null
  proof_blob="$(pinned_ge_git hash-object --no-filters -- \
    "$PROOF_DIR/$TARGET_PATH")"
  case "$patch_index" in
    1)
      [[ "$proof_blob" == "$DECLARED_INTERMEDIATE_BLOB" ]] \
        || pinned_ge_fail \
          "first GE overlap patch no longer produces the declared intermediate"
      ;;
    2)
      [[ "$proof_blob" == "$DECLARED_TARGET_BLOB" ]] \
        || pinned_ge_fail \
          "GE overlap series no longer produces the declared final target"
      ;;
    *) pinned_ge_fail "GE overlap proof received more than two patches" ;;
  esac
done <<<"$PATCH_RECORDS"
[[ "$patch_index" == 2 \
    && "$(pinned_ge_sha256 "$PROOF_DIR/$TARGET_PATH")" \
      == "$DECLARED_TARGET_SHA256" ]] \
  || pinned_ge_fail "GE overlap declared-series reconstruction changed"

RESIDUAL_DIFF="$PROOF_DIR/residual.diff"
set +e
diff -U3 --label f455 --label 9109 \
  "$COMPONENT/$TARGET_PATH" "$PROOF_DIR/$TARGET_PATH" >"$RESIDUAL_DIFF"
diff_status=$?
set -e
[[ "$diff_status" == 1 \
    && "$(pinned_ge_sha256 "$RESIDUAL_DIFF")" == "$RESIDUAL_DIFF_SHA256" \
    && "$(rg -c '^\+[^+]' "$RESIDUAL_DIFF")" == "$RESIDUAL_ADDITIONS" \
    && "$(rg -c '^-[^-]' "$RESIDUAL_DIFF")" == "$RESIDUAL_DELETIONS" ]] \
  || pinned_ge_fail \
    "effective-versus-declared GE overlap residual is no longer exact"

python3 - "$COMPONENT/$TARGET_PATH" "$PROOF_DIR/$TARGET_PATH" <<'PY'
from pathlib import Path
import sys

for argument in sys.argv[1:]:
    path = Path(argument)
    compile(path.read_text(), str(path), "exec")
PY
[[ "$(pinned_ge_repo_state_digest "$COMPONENT")" == "$COMPONENT_STATE_BEFORE" ]] \
  || pinned_ge_fail "GE overlap proof changed the protonfixes source state"

mkdir -p "$(dirname "$AUDIT")"
TEMP_AUDIT="$(mktemp "$(dirname "$AUDIT")/.ge-patch-overlap-audit.XXXXXX")"
{
  printf 'ge_patch_overlap_audit_version\t1\n'
  printf 'policy\tstock-effective-exact-skip\n'
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  printf 'manifest_sha256\t%s\n' "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256"
  printf 'component_path\t%s\n' "$COMPONENT_PATH"
  printf 'component_commit\t%s\n' "$COMPONENT_COMMIT"
  printf 'component_parent_commit\t%s\n' "$PARENT_COMMIT"
  printf 'target_path\t%s\n' "$TARGET_PATH"
  printf 'component_parent_target_blob\t%s\n' "$PARENT_BLOB"
  printf 'effective_target_blob\t%s\n' "$EFFECTIVE_BLOB"
  printf 'effective_target_sha256\t%s\n' "$EFFECTIVE_SHA256"
  printf 'declared_series_base_blob\t%s\n' "$DECLARED_BASE_BLOB"
  printf 'declared_series_intermediate_blob\t%s\n' \
    "$DECLARED_INTERMEDIATE_BLOB"
  printf 'declared_series_target_blob\t%s\n' "$DECLARED_TARGET_BLOB"
  printf 'declared_series_target_sha256\t%s\n' "$DECLARED_TARGET_SHA256"
  printf 'component_delta_paths\t%s\n' "$COMPONENT_DELTA_PATHS"
  while IFS=$'\t' read -r patch_path patch_sha additions deletions; do
    printf 'skipped_patch\t%s\t%s\t%s\t%s\tstock-effective-skip\n' \
      "$patch_path" "$patch_sha" "$additions" "$deletions"
  done <<<"$PATCH_RECORDS"
  printf 'residual_class\texception-logging-only\n'
  printf 'residual_additions\t%s\n' "$RESIDUAL_ADDITIONS"
  printf 'residual_deletions\t%s\n' "$RESIDUAL_DELETIONS"
  printf 'residual_diff_sha256\t%s\n' "$RESIDUAL_DIFF_SHA256"
  printf 'declared_series_reconstruction\tpassed\n'
  printf 'python_parse\tpassed\n'
  printf 'source_change\tnone\n'
  printf 'overlap_validation\tpassed\n'
} >"$TEMP_AUDIT"
chmod 0644 "$TEMP_AUDIT"
mv -f -- "$TEMP_AUDIT" "$AUDIT"
TEMP_AUDIT=""
rm -rf -- "$PROOF_DIR"
trap - EXIT

printf 'Verified and skipped exactly two inherited GE/protonfixes overlaps.\n'
