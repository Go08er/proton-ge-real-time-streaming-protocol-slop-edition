#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOTSTRAP="$ROOT_DIR/scripts/bootstrap-pinned-ge.sh"
FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-bootstrap-ownership.XXXXXX")"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

mkdir_line="$(rg -n -F 'mkdir -- "$SOURCE_DIR"' "$BOOTSTRAP" | cut -d: -f1)"
ownership_line="$(rg -n -F 'CREATED_SOURCE=1' "$BOOTSTRAP" | cut -d: -f1)"
[[ "$mkdir_line" =~ ^[0-9]+$ && "$ownership_line" =~ ^[0-9]+$ \
    && "$mkdir_line" -lt "$ownership_line" ]] \
  || fail 'bootstrap cleanup ownership is claimed before atomic destination mkdir'

target="$FIXTURE_DIR/dangling-target"
source_link="$FIXTURE_DIR/source-link"
ln -s -- "$target" "$source_link"
if bash "$BOOTSTRAP" --source "$source_link" \
    --seed "$FIXTURE_DIR/missing-seed" >/dev/null 2>&1; then
  fail 'bootstrap accepted a dangling source-destination symlink'
fi
[[ -L "$source_link" && "$(readlink -- "$source_link")" == "$target" \
    && ! -e "$target" ]] \
  || fail 'failed bootstrap removed or followed a destination symlink it did not own'

existing="$FIXTURE_DIR/existing-source"
mkdir -- "$existing"
printf 'caller-owned\n' >"$existing/sentinel"
if bash "$BOOTSTRAP" --source "$existing" \
    --seed "$FIXTURE_DIR/missing-seed" >/dev/null 2>&1; then
  fail 'bootstrap accepted a pre-existing non-repository destination'
fi
[[ -d "$existing" && -f "$existing/sentinel" \
    && "$(cat "$existing/sentinel")" == caller-owned ]] \
  || fail 'failed bootstrap removed a pre-existing destination it did not own'

printf 'Bootstrap destination ownership regressions passed.\n'
