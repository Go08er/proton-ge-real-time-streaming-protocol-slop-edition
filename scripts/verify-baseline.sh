#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
EXPECTED_TAG="${EXPECTED_TAG:-GE-Proton11-3}"
SAFE_TAG="${EXPECTED_TAG//\//_}"
MANIFEST="$ROOT_DIR/release-manifests/$SAFE_TAG.manifest.tsv"
MATERIALIZED_ROOT="${MATERIALIZED_ROOT:-$ROOT_DIR/work/ge-wine-$SAFE_TAG}"
MATERIALIZATION_APPROVAL="$ROOT_DIR/release-approvals/$SAFE_TAG.wine-tree.tsv"
DRIVER_APPROVAL="$ROOT_DIR/release-approvals/$SAFE_TAG.wine-driver.tsv"
NORMALIZATION_APPROVAL="$ROOT_DIR/release-approvals/$SAFE_TAG.wine-normalizations.tsv"
export GIT_NO_LAZY_FETCH=1

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_committed_project_file() {
  local absolute_path="$1"
  local description="$2"
  local relative_path
  local head_blob working_blob

  [[ "$absolute_path" == "$ROOT_DIR/"* && -f "$absolute_path" \
      && ! -L "$absolute_path" ]] \
    || fail "$description is not a regular file at its canonical project path: $absolute_path"
  relative_path="${absolute_path#"$ROOT_DIR/"}"
  head_blob="$(git -C "$ROOT_DIR" rev-parse --verify "HEAD:$relative_path" 2>/dev/null)" \
    || fail "$description is not committed in project HEAD: $relative_path"
  working_blob="$(git -C "$ROOT_DIR" hash-object --no-filters "$absolute_path")"
  [[ "$working_blob" == "$head_blob" ]] \
    || fail "$description bytes differ from project HEAD: $relative_path"
  git -C "$ROOT_DIR" diff --quiet HEAD -- "$relative_path" \
    || fail "$description differs from the committed project HEAD: $relative_path"
  git -C "$ROOT_DIR" diff --cached --quiet HEAD -- "$relative_path" \
    || fail "$description has a staged change relative to project HEAD: $relative_path"
}

"$ROOT_DIR/scripts/check-ge-release.sh" \
  --source "$SOURCE_DIR" --tag "$EXPECTED_TAG" --quiet

ROOT_COMMIT="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  rev-parse --verify "refs/tags/$EXPECTED_TAG^{commit}")"
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$ROOT_COMMIT" ]] \
  || fail "GE checkout is not at the accepted release commit"
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || fail "GE checkout is dirty"

require_committed_project_file "$MANIFEST" "accepted release manifest"
TEMP_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/ge-manifest-check.XXXXXX")"
TEMP_AUDIT="$(mktemp "${TMPDIR:-/tmp}/ge-wine-audit.XXXXXX")"
TEMP_TREE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/ge-wine-tree.XXXXXX")"
TEMP_DRIVER_APPROVAL="$(mktemp "${TMPDIR:-/tmp}/ge-driver-approval.XXXXXX")"
TEMP_NORMALIZATION_APPROVAL="$(mktemp "${TMPDIR:-/tmp}/ge-normalization-approval.XXXXXX")"
TEMP_MATERIALIZATION_APPROVAL="$(mktemp "${TMPDIR:-/tmp}/ge-tree-approval.XXXXXX")"
cleanup() {
  rm -f "$TEMP_MANIFEST" "$TEMP_AUDIT" "$TEMP_TREE_MANIFEST" \
    "$TEMP_DRIVER_APPROVAL" "$TEMP_NORMALIZATION_APPROVAL" \
    "$TEMP_MATERIALIZATION_APPROVAL"
}
trap cleanup EXIT

"$ROOT_DIR/scripts/record-release-manifest.sh" \
  --source "$SOURCE_DIR" --tag "$EXPECTED_TAG" --output - >"$TEMP_MANIFEST"
cmp -s "$MANIFEST" "$TEMP_MANIFEST" \
  || fail "committed release manifest does not match the accepted tag"

WINE_COMMIT="$(awk -F '\t' '$1 == "gitlink" && $2 == "wine" {print $3}' "$MANIFEST")"
STAGING_COMMIT="$(awk -F '\t' '$1 == "gitlink" && $2 == "wine-staging" {print $3}' "$MANIFEST")"
DRIVER_BLOB="$(awk -F '\t' '$1 == "source_file" && $2 == "patches/protonprep-valve-staging.sh" {print $3}' "$MANIFEST")"
DRIVER_SHA256="$(awk -F '\t' '$1 == "source_file" && $2 == "patches/protonprep-valve-staging.sh" {print $4}' "$MANIFEST")"
[[ -n "$WINE_COMMIT" && -n "$STAGING_COMMIT" \
    && -n "$DRIVER_BLOB" && -n "$DRIVER_SHA256" ]] \
  || fail "release manifest lacks required Wine or patch-driver identity"

{
  printf 'driver_approval_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'patch_driver_blob\t%s\n' "$DRIVER_BLOB"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
} >"$TEMP_DRIVER_APPROVAL"
require_committed_project_file "$DRIVER_APPROVAL" \
  "reviewed patch-driver approval"
cmp -s "$DRIVER_APPROVAL" "$TEMP_DRIVER_APPROVAL" \
  || fail "patch-driver approval does not match the accepted release manifest"

[[ -d "$MATERIALIZED_ROOT/wine" ]] \
  || fail "materialized Wine source is absent: $MATERIALIZED_ROOT/wine"
[[ -f "$MATERIALIZED_ROOT/source-identity.tsv" ]] \
  || fail "source identity record is absent"
grep -Fqx $'tag\t'"$EXPECTED_TAG" "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "materialized source records a different tag"
grep -Fqx $'root_commit\t'"$ROOT_COMMIT" "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "materialized source records a different root commit"

"$ROOT_DIR/scripts/audit-patched-wine.sh" \
  --wine-tree "$MATERIALIZED_ROOT/wine" >"$TEMP_AUDIT"
[[ -f "$MATERIALIZED_ROOT/wine-source-audit.tsv" ]] \
  || fail "recorded Wine source audit is absent"
cmp -s "$MATERIALIZED_ROOT/wine-source-audit.tsv" "$TEMP_AUDIT" \
  || fail "Wine source capability audit changed"
SOURCE_AUDIT_SHA256="$(sha256sum "$TEMP_AUDIT" | awk '{print $1}')"

[[ -f "$MATERIALIZED_ROOT/wine-tree.manifest.tsv" ]] \
  || fail "recorded complete Wine tree manifest is absent"
"$ROOT_DIR/scripts/hash-source-tree.sh" --tree "$MATERIALIZED_ROOT/wine" \
  >"$TEMP_TREE_MANIFEST"
cmp -s "$MATERIALIZED_ROOT/wine-tree.manifest.tsv" "$TEMP_TREE_MANIFEST" \
  || fail "materialized Wine filesystem changed after source replay"
TREE_MANIFEST_SHA256="$(sha256sum "$TEMP_TREE_MANIFEST" | awk '{print $1}')"
grep -Fqx $'wine_tree_manifest_sha256\t'"$TREE_MANIFEST_SHA256" \
  "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "source identity does not match the complete Wine tree manifest"

[[ -f "$MATERIALIZED_ROOT/normalized-rejects.tsv" \
    && -f "$MATERIALIZED_ROOT/normalization-candidates.tsv" ]] \
  || fail "normalization evidence is incomplete"
REJECT_NORMALIZATION_COUNT="$(awk -F '\t' \
  '$1 == "deleted_backend_file" {count++} END {print count + 0}' \
  "$MATERIALIZED_ROOT/normalized-rejects.tsv")"
MISSING_BODY_NORMALIZATION_COUNT="$(awk -F '\t' \
  '$1 == "missing_deletion_body" {count++} END {print count + 0}' \
  "$MATERIALIZED_ROOT/normalized-rejects.tsv")"
NORMALIZATION_COUNT="$((REJECT_NORMALIZATION_COUNT + MISSING_BODY_NORMALIZATION_COUNT))"
case "$REJECT_NORMALIZATION_COUNT:$MISSING_BODY_NORMALIZATION_COUNT" in
  0:0|0:3|2:0) ;;
  *)
    fail "unexpected normalization shape: rejects=$REJECT_NORMALIZATION_COUNT missing_bodies=$MISSING_BODY_NORMALIZATION_COUNT"
    ;;
esac
{
  printf 'normalization_approval_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'wine_commit\t%s\n' "$WINE_COMMIT"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
  cat "$MATERIALIZED_ROOT/normalized-rejects.tsv"
} >"$TEMP_NORMALIZATION_APPROVAL"
cmp -s "$MATERIALIZED_ROOT/normalization-candidates.tsv" \
  "$TEMP_NORMALIZATION_APPROVAL" \
  || fail "normalization candidate does not match the recorded replay result"
if [[ "$NORMALIZATION_COUNT" -gt 0 ]]; then
  require_committed_project_file "$NORMALIZATION_APPROVAL" \
    "reviewed normalization approval"
  cmp -s "$NORMALIZATION_APPROVAL" "$TEMP_NORMALIZATION_APPROVAL" \
    || fail "normalization result does not match its reviewed approval"
fi

grep -Fqx $'wine_commit\t'"$WINE_COMMIT" \
  "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "source identity records a different Wine pin"
grep -Fqx $'wine_staging_commit\t'"$STAGING_COMMIT" \
  "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "source identity records a different Wine-Staging pin"
grep -Fqx $'patch_driver_sha256\t'"$DRIVER_SHA256" \
  "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "source identity records a different patch driver"
grep -Fqx $'audited_normalization_count\t'"$NORMALIZATION_COUNT" \
  "$MATERIALIZED_ROOT/source-identity.tsv" \
  || fail "source identity records a different normalization count"

[[ -f "$MATERIALIZED_ROOT/wine-tree-approval-candidate.tsv" ]] \
  || fail "materialized tree approval candidate is absent"
require_committed_project_file "$MATERIALIZATION_APPROVAL" \
  "reviewed materialization approval"
{
  printf 'materialization_approval_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'wine_commit\t%s\n' "$WINE_COMMIT"
  printf 'wine_staging_commit\t%s\n' "$STAGING_COMMIT"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
  printf 'wine_tree_manifest_sha256\t%s\n' "$TREE_MANIFEST_SHA256"
  printf 'wine_source_audit_sha256\t%s\n' "$SOURCE_AUDIT_SHA256"
  printf 'audited_normalization_count\t%s\n' "$NORMALIZATION_COUNT"
} >"$TEMP_MATERIALIZATION_APPROVAL"
cmp -s "$MATERIALIZED_ROOT/wine-tree-approval-candidate.tsv" \
  "$TEMP_MATERIALIZATION_APPROVAL" \
  || fail "materialized tree approval candidate is not reproducible"
cmp -s "$MATERIALIZATION_APPROVAL" "$TEMP_MATERIALIZATION_APPROVAL" \
  || fail "materialized Wine tree does not match its reviewed approval"

"$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" >/dev/null

printf 'Verified immutable %s source identity and source-only Wine replay.\n' "$EXPECTED_TAG"
printf 'No Proton build was started.\n'
