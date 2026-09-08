#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
SEED_DIR="$ROOT_DIR/worktrees/ge-master-snapshot-20260723-a312-rtsp"
CREATED_SOURCE=0

cleanup_failed_bootstrap() {
  local status=$?
  trap - EXIT
  if [[ "$status" != 0 && "$CREATED_SOURCE" == 1 ]]; then
    rm -rf -- "$SOURCE_DIR"
  fi
  exit "$status"
}

trap cleanup_failed_bootstrap EXIT

usage() {
  cat <<'EOF'
Usage: bootstrap-pinned-ge.sh [OPTIONS]

Create or verify a standalone, detached checkout of the selected pinned GE
configuration using only an already-local seed repository. This command never
fetches a remote and never initializes submodules.

Options:
  --source DIR   snapshot checkout to create or verify
  --seed DIR     local GE object/source seed
  --config FILE  pinned config (default: active exact snapshot)
  -h, --help     show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --seed)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SEED_DIR="$2"
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
[[ -n "$SOURCE_DIR" ]] || SOURCE_DIR="$(pinned_ge_default_source)"
# realpath -m follows a dangling final symlink and would make the cleanup trap
# believe its target was the requested destination. Reject that input before
# canonicalization so the script never claims ownership of the link or target.
[[ ! -L "$SOURCE_DIR" ]] \
  || pinned_ge_fail "source destination must not be a symlink: $SOURCE_DIR"
SOURCE_DIR="$(realpath -m -- "$SOURCE_DIR")"
SEED_DIR="$(realpath -m -- "$SEED_DIR")"

if [[ ! -e "$SOURCE_DIR" && ! -L "$SOURCE_DIR" ]]; then
  [[ -e "$SEED_DIR/.git" ]] || pinned_ge_fail "local seed repository is absent: $SEED_DIR"
  pinned_ge_git -C "$SEED_DIR" cat-file -e "$PIN_SOURCE_COMMIT^{commit}" \
    || pinned_ge_fail "local seed lacks the pinned GE commit object"
  pinned_ge_git -C "$SEED_DIR" cat-file -e "$PIN_SOURCE_TREE^{tree}" \
    || pinned_ge_fail \
      "local seed advertises the pin but lacks its materialized tree object"
  [[ "$(pinned_ge_git -C "$SEED_DIR" rev-parse "$PIN_SOURCE_COMMIT^{tree}")" \
      == "$PIN_SOURCE_TREE" ]] \
    || pinned_ge_fail "local seed's pinned commit has an unexpected tree"
  mkdir -p "$(dirname "$SOURCE_DIR")"
  pinned_ge_require_absent_path "pinned GE source destination" "$SOURCE_DIR"
  # Reserve the destination atomically. Only after mkdir succeeds does the
  # failure trap own this exact path; clone accepts an existing empty target.
  mkdir -- "$SOURCE_DIR"
  CREATED_SOURCE=1
  GIT_NO_LAZY_FETCH=1 git \
    -c maintenance.auto=false \
    -c maintenance.autoDetach=false \
    -c gc.auto=0 \
    -c gc.autoDetach=false \
    clone --no-checkout --no-hardlinks "$SEED_DIR" "$SOURCE_DIR"

  pinned_ge_git -C "$SOURCE_DIR" cat-file -e "$PIN_SOURCE_COMMIT^{commit}" \
    || pinned_ge_fail "local clone omitted the pinned GE commit object"
  pinned_ge_git -C "$SOURCE_DIR" cat-file -e "$PIN_SOURCE_TREE^{tree}" \
    || pinned_ge_fail "local clone omitted the pinned GE tree object"

  pinned_ge_git -C "$SOURCE_DIR" remote rename origin snapshot-seed
  pinned_ge_git -C "$SOURCE_DIR" remote add origin "$PIN_SOURCE_URL"
  pinned_ge_git -C "$SOURCE_DIR" config maintenance.auto false
  pinned_ge_git -C "$SOURCE_DIR" config maintenance.autoDetach false
  pinned_ge_git -C "$SOURCE_DIR" config gc.auto 0
  pinned_ge_git -C "$SOURCE_DIR" config gc.autoDetach false
  pinned_ge_git -C "$SOURCE_DIR" checkout --detach "$PIN_SOURCE_COMMIT"
else
  [[ -e "$SOURCE_DIR/.git" ]] \
    || pinned_ge_fail "source path exists but is not a Git checkout: $SOURCE_DIR"
fi

pinned_ge_require_source_identity "$SOURCE_DIR"
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || pinned_ge_fail "pinned GE checkout is dirty: $SOURCE_DIR"

trap - EXIT

printf 'Pinned GE checkout: %s\n' "$SOURCE_DIR"
printf 'Pin kind:          %s\n' "$PIN_KIND"
printf 'Pinned commit:     %s\n' "$PIN_SOURCE_COMMIT"
printf 'Config:            %s\n' "$PINNED_GE_CONFIG_PATH"
printf 'Submodules were not initialized and no network access occurred.\n'
