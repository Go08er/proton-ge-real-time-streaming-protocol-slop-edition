#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
EXPECTED_TAG="${EXPECTED_TAG:-GE-Proton11-3}"
REMOTE="${GE_REMOTE:-origin}"
ALLOW_FETCH=0
QUIET=0
PATCH_SCOPES=()

usage() {
  cat <<'EOF'
Usage: check-ge-release.sh [OPTIONS]

Validate an exact GE-Proton release tag using a local Git object database.
No network access is attempted unless --fetch is supplied.

Options:
  --source DIR       GE Proton source repository
  --tag TAG          exact release tag (default: GE-Proton11-3)
  --remote NAME      remote used only with --fetch (default: origin)
  --fetch            fetch only the requested tag before validating it
  --patch-scope PATH relevant source path to require (repeatable)
  --quiet            suppress the successful summary
  -h, --help         show this help

Default patch scopes:
  patches/ge-video-rework
  patches/protonprep-valve-staging.sh
  patches/video-rework.sh
  patches/wineopenxr
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
    --quiet)
      QUIET=1
      shift
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

git check-ref-format "refs/tags/$EXPECTED_TAG" >/dev/null 2>&1 \
  || fail "invalid tag name: $EXPECTED_TAG"
[[ -d "$SOURCE_DIR" ]] || fail "missing source directory: $SOURCE_DIR"
git -C "$SOURCE_DIR" rev-parse --git-dir >/dev/null 2>&1 \
  || fail "not a Git repository: $SOURCE_DIR"

# Partial clones may otherwise contact their promisor remote while reading a
# missing object. Keep the default path genuinely offline.
git_read() {
  GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" "$@"
}

if [[ $ALLOW_FETCH -eq 1 ]]; then
  git -C "$SOURCE_DIR" remote get-url "$REMOTE" >/dev/null 2>&1 \
    || fail "unknown remote: $REMOTE"
  if ! git -C "$SOURCE_DIR" fetch --no-tags --no-filter \
      --no-recurse-submodules --depth=1 "$REMOTE" \
      "refs/tags/$EXPECTED_TAG:refs/tags/$EXPECTED_TAG"; then
    fail "could not fetch the exact tag $EXPECTED_TAG from $REMOTE"
  fi
fi

if ! git_read show-ref --verify --quiet "refs/tags/$EXPECTED_TAG"; then
  fail "exact local tag $EXPECTED_TAG is absent; no branch or master substitute was used (rerun with --fetch to permit a tag-only fetch)"
fi

if ! TAG_OBJECT="$(git_read rev-parse --verify "refs/tags/$EXPECTED_TAG" 2>/dev/null)"; then
  fail "cannot resolve tag object for $EXPECTED_TAG"
fi
if ! ROOT_COMMIT="$(git_read rev-parse --verify "refs/tags/$EXPECTED_TAG^{commit}" 2>/dev/null)"; then
  fail "$EXPECTED_TAG does not peel to a commit"
fi
if ! ROOT_TREE="$(git_read rev-parse --verify "$ROOT_COMMIT^{tree}" 2>/dev/null)"; then
  fail "the root tree for $EXPECTED_TAG is unavailable locally (rerun with --fetch to permit object retrieval)"
fi

if ! GITMODULES_RECORD="$(git_read ls-tree "$ROOT_COMMIT" -- .gitmodules)"; then
  fail "cannot inspect .gitmodules at $EXPECTED_TAG"
fi
[[ -n "$GITMODULES_RECORD" ]] || fail "$EXPECTED_TAG has no tracked .gitmodules file"
read -r _ GITMODULES_TYPE GITMODULES_BLOB _ <<<"$GITMODULES_RECORD"
[[ "$GITMODULES_TYPE" == blob ]] || fail ".gitmodules is not a blob at $EXPECTED_TAG"
git_read cat-file -e "$GITMODULES_BLOB" 2>/dev/null \
  || fail ".gitmodules content is unavailable locally (rerun with --fetch)"

if ! GITLINK_RECORDS="$(git_read ls-tree -r "$ROOT_COMMIT")"; then
  fail "cannot enumerate gitlinks at $EXPECTED_TAG"
fi
GITLINK_COUNT="$(awk '$1 == "160000" && $2 == "commit" {count++} END {print count + 0}' <<<"$GITLINK_RECORDS")"
[[ "$GITLINK_COUNT" -gt 0 ]] || fail "$EXPECTED_TAG contains no gitlink/submodule pins"

for scope in "${PATCH_SCOPES[@]}"; do
  [[ "$scope" != /* && "$scope" != *$'\n'* && "$scope" != *$'\t'* ]] \
    || fail "unsafe patch scope: $scope"
  if ! SCOPE_RECORDS="$(git_read ls-tree -r "$ROOT_COMMIT" -- "$scope")"; then
    fail "cannot inspect required patch scope: $scope"
  fi
  if ! awk '$2 == "blob" {found=1} END {exit !found}' <<<"$SCOPE_RECORDS"; then
    fail "required patch scope is absent or contains no files: $scope"
  fi

  while IFS=$'\t' read -r metadata path; do
    [[ -n "$metadata" ]] || continue
    read -r _ type object_id <<<"$metadata"
    [[ "$type" == blob ]] || continue
    if ! git_read cat-file -e "$object_id" 2>/dev/null; then
      fail "content for $path is unavailable locally (rerun with --fetch)"
    fi
  done <<<"$SCOPE_RECORDS"
done

if [[ $QUIET -eq 0 ]]; then
  printf 'Validated exact tag: %s\n' "$EXPECTED_TAG"
  printf 'Tag object:          %s\n' "$TAG_OBJECT"
  printf 'Root commit:         %s\n' "$ROOT_COMMIT"
  printf 'Root tree:           %s\n' "$ROOT_TREE"
  printf 'Gitlink pins:        %s\n' "$GITLINK_COUNT"
  printf 'Network policy:      %s\n' "$([[ $ALLOW_FETCH -eq 1 ]] && printf 'explicit tag-only fetch allowed' || printf 'offline')"
fi
