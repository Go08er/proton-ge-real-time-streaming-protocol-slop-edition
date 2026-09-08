#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
export GIT_NO_LAZY_FETCH=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
ARCHIVE_SEED="${ARCHIVE_SEED:-$ROOT_DIR/worktrees/ge-master-snapshot-20260723-a312-rtsp}"
OFFICIAL_URL="https://github.com/GloriousEggroll/proton-ge-custom.git"
EXPECTED_TAG="${EXPECTED_TAG:-GE-Proton11-3}"
ALLOW_FETCH=0
INIT_AUTHORING_SUBMODULES=0

usage() {
  cat <<'EOF'
Usage: bootstrap-sources.sh [OPTIONS]

Prepare an exact stable GE release checkout without building Proton.

Options:
  --source DIR                  GE source checkout
  --tag TAG                     exact tag (default: GE-Proton11-3)
  --fetch                       permit official tag/source retrieval
  --init-authoring-submodules   initialize Wine, Wine-Staging, and FFmpeg; requires --fetch
  -h, --help                    show this help

Without --fetch the command is offline. It never substitutes master for an
absent release tag.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || fail "--source requires a directory"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 && -n "$2" ]] || fail "--tag requires a value"
      EXPECTED_TAG="$2"
      shift 2
      ;;
    --fetch)
      ALLOW_FETCH=1
      shift
      ;;
    --init-authoring-submodules)
      INIT_AUTHORING_SUBMODULES=1
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

if [[ $INIT_AUTHORING_SUBMODULES -eq 1 && $ALLOW_FETCH -ne 1 ]]; then
  fail "--init-authoring-submodules requires --fetch because submodule initialization may retrieve objects"
fi

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  [[ $ALLOW_FETCH -eq 1 ]] \
    || fail "source checkout is absent and network retrieval was not authorized with --fetch"
  mkdir -p "$(dirname "$SOURCE_DIR")"
  if [[ -d "$ARCHIVE_SEED/.git" ]]; then
    git clone --no-checkout "$ARCHIVE_SEED" "$SOURCE_DIR"
    git -C "$SOURCE_DIR" remote rename origin archive-seed
    git -C "$SOURCE_DIR" remote add origin "$OFFICIAL_URL"
  else
    git init -q "$SOURCE_DIR"
    git -C "$SOURCE_DIR" remote add origin "$OFFICIAL_URL"
  fi
fi

git -C "$SOURCE_DIR" rev-parse --git-dir >/dev/null 2>&1 \
  || fail "not a Git repository: $SOURCE_DIR"
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || fail "refusing to change a dirty source checkout"

if git -C "$SOURCE_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$SOURCE_DIR" remote set-url origin "$OFFICIAL_URL"
else
  git -C "$SOURCE_DIR" remote add origin "$OFFICIAL_URL"
fi

CHECK_ARGS=(--source "$SOURCE_DIR" --tag "$EXPECTED_TAG")
if [[ $ALLOW_FETCH -eq 1 ]]; then
  CHECK_ARGS+=(--fetch)
fi
"$ROOT_DIR/scripts/check-ge-release.sh" "${CHECK_ARGS[@]}"

ROOT_COMMIT="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  rev-parse --verify "refs/tags/$EXPECTED_TAG^{commit}")"
GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  switch --detach "refs/tags/$EXPECTED_TAG"
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$ROOT_COMMIT" ]] \
  || fail "checkout did not land on the accepted release commit"

if [[ $INIT_AUTHORING_SUBMODULES -eq 1 ]]; then
  git -C "$SOURCE_DIR" submodule update --init --depth 1 wine wine-staging ffmpeg

  REVERT_COMMIT="e813ca5771658b00875924ab88d525322e50d39f"
  if ! git -C "$SOURCE_DIR/wine" cat-file -e "$REVERT_COMMIT^" 2>/dev/null; then
    [[ $ALLOW_FETCH -eq 1 ]] \
      || fail "Wine revert history is absent; rerun with --fetch to permit its retrieval"
    git -C "$SOURCE_DIR/wine" fetch --depth 2 origin "$REVERT_COMMIT"
  fi
  git -C "$SOURCE_DIR/wine" cat-file -e "$REVERT_COMMIT^" 2>/dev/null \
    || fail "Wine revert commit and parent are unavailable"
fi

printf 'Accepted release: %s\n' "$EXPECTED_TAG"
printf 'Root commit:     %s\n' "$ROOT_COMMIT"
printf 'Source root:     %s\n' "$SOURCE_DIR"
if [[ $INIT_AUTHORING_SUBMODULES -eq 1 ]]; then
  printf 'Authoring inputs: Wine, Wine-Staging, and FFmpeg initialized at release pins.\n'
else
  printf 'Submodules were not initialized.\n'
fi
printf 'No Proton build was started.\n'
