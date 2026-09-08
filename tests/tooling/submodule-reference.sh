#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../scripts/pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-submodule-reference.XXXXXX")"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

make_repo() {
  local repo="$1"
  local first second

  mkdir -p "$repo"
  git -C "$repo" init -q
  git -C "$repo" config user.name fixture
  git -C "$repo" config user.email fixture.invalid
  printf 'first\n' >"$repo/payload"
  git -C "$repo" add payload
  git -C "$repo" commit -qm first
  first="$(git -C "$repo" rev-parse HEAD)"
  printf 'second\n' >"$repo/payload"
  git -C "$repo" commit -qam second
  second="$(git -C "$repo" rev-parse HEAD)"
  git -C "$repo" checkout -q --detach "$first"
  printf '%s\t%s\n' "$first" "$second"
}

mkdir -p "$FIXTURE_DIR/first" "$FIXTURE_DIR/second/component"
IFS=$'\t' read -r old_commit target_commit < <(
  make_repo "$FIXTURE_DIR/second/component"
)
git -C "$FIXTURE_DIR/second/component" remote add \
  origin https://example.invalid/component

[[ "$(git -C "$FIXTURE_DIR/second/component" rev-parse HEAD)" == "$old_commit" ]] \
  || fail "fixture did not retain a non-target HEAD"
pinned_ge_repo_has_complete_commit \
  "$FIXTURE_DIR/second/component" "$target_commit" 1 \
  || fail "complete target object was rejected because HEAD differs"

selected="$(
  pinned_ge_find_complete_reference_repo "$target_commit" component \
    "$FIXTURE_DIR/first" "$FIXTURE_DIR/second"
)"
[[ "$selected" == "$(realpath -e "$FIXTURE_DIR/second/component")" ]] \
  || fail "ordered heterogeneous reference selection chose the wrong repository"
pinned_ge_require_existing_submodule \
  "$FIXTURE_DIR/second/component" "$old_commit" \
  https://example.invalid/component \
  || fail "exact pre-existing child was not reusable"
if pinned_ge_require_existing_submodule \
    "$FIXTURE_DIR/second/component" "$target_commit" \
    https://example.invalid/component >/dev/null 2>&1; then
  fail "pre-existing child at the wrong HEAD was accepted"
fi

missing=0000000000000000000000000000000000000000
if pinned_ge_find_complete_reference_repo "$missing" component \
    "$FIXTURE_DIR/first" "$FIXTURE_DIR/second" >/dev/null 2>&1; then
  fail "missing target object was accepted"
fi
if pinned_ge_find_complete_reference_repo "$target_commit" ../component \
    "$FIXTURE_DIR/second" >/dev/null 2>&1; then
  fail "escaping reference path was accepted"
fi

git -c advice.detachedHead=false -c protocol.file.allow=always \
  clone -q --depth 1 \
  "file://$FIXTURE_DIR/second/component" "$FIXTURE_DIR/shallow"
if pinned_ge_repo_has_complete_commit \
    "$FIXTURE_DIR/shallow" "$old_commit" 1 >/dev/null 2>&1; then
  fail "shallow repository was accepted as a reusable complete-history seed"
fi
pinned_ge_repo_has_complete_commit \
  "$FIXTURE_DIR/shallow" "$old_commit" 0 \
  || fail "exact pre-existing shallow checkout was rejected"

printf 'Heterogeneous submodule reference selection regressions passed.\n'
