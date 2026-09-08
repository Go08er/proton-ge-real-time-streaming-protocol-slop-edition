#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

TREE=""

usage() {
  cat <<'EOF'
Usage: hash-source-tree.sh --tree DIR

Write a deterministic TSV inventory plus a normalized GNU-tar SHA-256 for the
complete tree, excluding only the Git administrative entry at DIR/.git.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tree)
      [[ $# -ge 2 && -n "$2" ]] || fail "--tree requires a directory"
      TREE="$2"
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

[[ -n "$TREE" ]] || fail "--tree is required"
[[ -d "$TREE" ]] || fail "missing source tree: $TREE"
TREE="$(cd "$TREE" && pwd -P)"
cd "$TREE"

if find . -path './.git' -prune -o \
    ! \( -type d -o -type f -o -type l \) -print -quit | grep -q .; then
  fail "source tree contains an unsupported special filesystem entry"
fi

ARCHIVE_SHA256="$(
  env -u TAR_OPTIONS -u POSIXLY_CORRECT LC_ALL=C \
    tar --sort=name --format=gnu --mtime='@0' --owner=0 --group=0 \
    --numeric-owner --exclude='./.git' -cf - . \
    | sha256sum | awk '{print $1}'
)"

printf 'tree_manifest_version\t2\n'
printf 'normalized_tar_sha256\t%s\n' "$ARCHIVE_SHA256"
while IFS= read -r -d '' record; do
  type="${record%%$'\t'*}"
  remainder="${record#*$'\t'}"
  mode="${remainder%%$'\t'*}"
  relative_path="${remainder#*$'\t'}"
  [[ "$relative_path" != *$'\n'* && "$relative_path" != *$'\t'* ]] \
    || fail "path cannot be represented safely in TSV"
  printf 'entry\t%s\t%s\t%s\n' "$type" "$mode" "$relative_path"
done < <(
  find . -mindepth 1 -path './.git' -prune -o \
    -printf '%y\t%m\t%P\0' | LC_ALL=C sort -z
)
