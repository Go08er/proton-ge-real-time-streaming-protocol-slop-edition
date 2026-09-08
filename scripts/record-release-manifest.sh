#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
EXPECTED_TAG="${EXPECTED_TAG:-GE-Proton11-3}"
REMOTE="${GE_REMOTE:-origin}"
ALLOW_FETCH=0
OUTPUT=""
PATCH_SCOPES=()

usage() {
  cat <<'EOF'
Usage: record-release-manifest.sh [OPTIONS]

Record deterministic provenance for an exact GE-Proton release tag.
No network access is attempted unless --fetch is supplied.

Options:
  --source DIR       GE Proton source repository
  --tag TAG          exact release tag (default: GE-Proton11-3)
  --remote NAME      remote used only with --fetch (default: origin)
  --fetch            fetch only the requested tag before recording it
  --patch-scope PATH relevant source path to hash (repeatable)
  --output FILE      output path; use - for stdout
  -h, --help         show this help

The default output is release-manifests/GE-Proton11-3.manifest.tsv.
An existing manifest is retained if identical and rejected if different.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_value() {
  [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      need_value "$@"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --tag)
      need_value "$@"
      EXPECTED_TAG="$2"
      shift 2
      ;;
    --remote)
      need_value "$@"
      REMOTE="$2"
      shift 2
      ;;
    --fetch)
      ALLOW_FETCH=1
      shift
      ;;
    --patch-scope)
      need_value "$@"
      PATCH_SCOPES+=("$2")
      shift 2
      ;;
    --output)
      need_value "$@"
      OUTPUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

if [[ ${#PATCH_SCOPES[@]} -eq 0 ]]; then
  PATCH_SCOPES=(
    patches/ge-video-rework
    patches/protonprep-valve-staging.sh
    patches/video-rework.sh
    patches/wineopenxr
  )
fi
mapfile -t PATCH_SCOPES < <(printf '%s\n' "${PATCH_SCOPES[@]}" | LC_ALL=C sort -u)

if [[ -z "$OUTPUT" ]]; then
  SAFE_TAG="${EXPECTED_TAG//\//_}"
  OUTPUT="$ROOT_DIR/release-manifests/$SAFE_TAG.manifest.tsv"
fi

CHECK_ARGS=(
  --source "$SOURCE_DIR"
  --tag "$EXPECTED_TAG"
  --remote "$REMOTE"
  --quiet
)
if [[ $ALLOW_FETCH -eq 1 ]]; then
  CHECK_ARGS+=(--fetch)
fi
for scope in "${PATCH_SCOPES[@]}"; do
  CHECK_ARGS+=(--patch-scope "$scope")
done
"$ROOT_DIR/scripts/check-ge-release.sh" "${CHECK_ARGS[@]}"

git_read() {
  if [[ $ALLOW_FETCH -eq 1 ]]; then
    git -C "$SOURCE_DIR" "$@"
  else
    GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" "$@"
  fi
}

TAG_OBJECT="$(git_read rev-parse --verify "refs/tags/$EXPECTED_TAG")"
TAG_TYPE="$(git_read cat-file -t "$TAG_OBJECT")"
ROOT_COMMIT="$(git_read rev-parse --verify "refs/tags/$EXPECTED_TAG^{commit}")"
ROOT_TREE="$(git_read rev-parse --verify "$ROOT_COMMIT^{tree}")"

GITMODULES_RECORD="$(git_read ls-tree "$ROOT_COMMIT" -- .gitmodules)"
read -r _ _ GITMODULES_BLOB _ <<<"$GITMODULES_RECORD"
if ! GITMODULES_SHA256="$(git_read cat-file blob "$GITMODULES_BLOB" | sha256sum | awk '{print $1}')"; then
  fail "could not hash .gitmodules"
fi

if ! TREE_RECORDS="$(git_read ls-tree -r "$ROOT_COMMIT")"; then
  fail "could not enumerate the release tree"
fi

declare -a GITLINK_LINES=()
while IFS=$'\t' read -r metadata path; do
  [[ -n "$metadata" ]] || continue
  read -r mode type object_id <<<"$metadata"
  if [[ "$mode" == 160000 && "$type" == commit ]]; then
    [[ "$path" != *$'\n'* && "$path" != *$'\t'* ]] \
      || fail "gitlink path cannot be represented safely in TSV"
    GITLINK_LINES+=("gitlink"$'\t'"$path"$'\t'"$object_id")
  fi
done <<<"$TREE_RECORDS"

declare -A FILES_SEEN=()
declare -a SOURCE_FILE_LINES=()
for scope in "${PATCH_SCOPES[@]}"; do
  if ! SCOPE_RECORDS="$(git_read ls-tree -r "$ROOT_COMMIT" -- "$scope")"; then
    fail "could not enumerate patch scope: $scope"
  fi
  while IFS=$'\t' read -r metadata path; do
    [[ -n "$metadata" ]] || continue
    read -r mode type object_id <<<"$metadata"
    [[ "$type" == blob ]] || continue
    [[ -z "${FILES_SEEN[$path]+present}" ]] || continue
    [[ "$path" != *$'\n'* && "$path" != *$'\t'* ]] \
      || fail "source path cannot be represented safely in TSV"
    if ! content_sha256="$(git_read cat-file blob "$object_id" | sha256sum | awk '{print $1}')"; then
      fail "could not hash relevant source file: $path"
    fi
    FILES_SEEN["$path"]=1
    SOURCE_FILE_LINES+=("source_file"$'\t'"$path"$'\t'"$object_id"$'\t'"$content_sha256")
  done <<<"$SCOPE_RECORDS"
done

[[ ${#GITLINK_LINES[@]} -gt 0 ]] || fail "no gitlink pins were collected"
[[ ${#SOURCE_FILE_LINES[@]} -gt 0 ]] || fail "no relevant patch files were collected"

TEMP_FILE="$(mktemp "${TMPDIR:-/tmp}/ge-release-manifest.XXXXXX")"
cleanup() {
  rm -f "$TEMP_FILE"
}
trap cleanup EXIT

{
  printf 'manifest_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'tag_object\t%s\t%s\n' "$TAG_OBJECT" "$TAG_TYPE"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'root_tree\t%s\n' "$ROOT_TREE"
  printf 'gitmodules\t%s\t%s\n' "$GITMODULES_BLOB" "$GITMODULES_SHA256"
  printf 'gitlink_count\t%s\n' "${#GITLINK_LINES[@]}"
  printf 'source_file_count\t%s\n' "${#SOURCE_FILE_LINES[@]}"
  printf '%s\n' "${GITLINK_LINES[@]}" | LC_ALL=C sort
  printf '%s\n' "${SOURCE_FILE_LINES[@]}" | LC_ALL=C sort
} >"$TEMP_FILE"
chmod 0644 "$TEMP_FILE"

if [[ "$OUTPUT" == - ]]; then
  cat "$TEMP_FILE"
  exit 0
fi

OUTPUT_DIR="$(dirname "$OUTPUT")"
mkdir -p "$OUTPUT_DIR"
if [[ -e "$OUTPUT" ]]; then
  if cmp -s "$TEMP_FILE" "$OUTPUT"; then
    printf 'Manifest is unchanged: %s\n' "$OUTPUT"
    exit 0
  fi
  fail "refusing to replace a different existing manifest: $OUTPUT"
fi

mv "$TEMP_FILE" "$OUTPUT"
trap - EXIT
printf 'Recorded deterministic release manifest: %s\n' "$OUTPUT"
