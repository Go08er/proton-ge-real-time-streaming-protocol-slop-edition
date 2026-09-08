#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

usage() {
  cat <<'EOF'
Usage: record-pinned-ge-resume-state.sh

Perform the one-shot adoption of the retained 9fad3bbe/vodfix2 interruption.
The source, build, state, cache, original invocation, generated Makefiles,
partial objects, progress stamps, and final compiler failure are all pinned.
No configurable or generic legacy-build adoption path exists.

The command does not configure, synchronize sources, compile, install into
Steam, or publish. New builds record configure state automatically.
EOF
}

case "${1:-}" in
  '') ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    printf 'ERROR: this one-shot adoption command accepts no options\n' >&2
    usage >&2
    exit 2
    ;;
esac

PINNED_GE_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260713.env"
pinned_ge_load_config
SOURCE_DIR="$(realpath -e -- "$ROOT_DIR/$PIN_RETAINED_RESUME_SOURCE")"
BUILD_DIR="$(realpath -e -- "$PIN_RETAINED_RESUME_BUILD")"
STATE_DIR="$(realpath -e -- "$ROOT_DIR/$PIN_RETAINED_RESUME_STATE")"
CACHE_HOME="$(pinned_ge_resolve_retained_resume_cache)"
for pair in \
    "GE source path:$SOURCE_DIR" "GE build path:$BUILD_DIR" \
    "GE state path:$STATE_DIR" "GE cache home:$CACHE_HOME"; do
  pinned_ge_require_safe_absolute_path "${pair%%:*}" "${pair#*:}"
done

BUILD_LOCK="${PINNED_GE_BUILD_LOCK:-${HOME:?HOME must be set}/Documents/.proton-build.lock}"
mkdir -p "$(dirname "$BUILD_LOCK")"
pinned_ge_require_regular_or_absent "workspace build lock" "$BUILD_LOCK"
exec 9>"$BUILD_LOCK"
flock -n 9 \
  || pinned_ge_fail "another cooperating Proton build holds $BUILD_LOCK"

STATE_FILE="$STATE_DIR/prepared-source.tsv"
INVOCATION_FILE="$STATE_DIR/build-invocation.tsv"
CONFIGURE_STATE="$STATE_DIR/configure-state.tsv"
CONTRIB_MANIFEST="$(pinned_ge_build_contrib_manifest_path)"

[[ ! -e "$CONFIGURE_STATE" && ! -L "$CONFIGURE_STATE" ]] \
  || pinned_ge_fail "configure state already exists; refusing to replace it"
pinned_ge_require_source_identity "$SOURCE_DIR"
pinned_ge_require_registered_submodules "$SOURCE_DIR"
[[ "$(pinned_ge_state_value "$STATE_FILE" source_path)" == "$SOURCE_DIR" ]] \
  || pinned_ge_fail "prepared state belongs to another source path"
[[ "$(pinned_ge_state_value "$STATE_FILE" root_repo_state_sha256)" \
    == "$(pinned_ge_repo_state_digest "$SOURCE_DIR" "$CONTRIB_MANIFEST")" ]] \
  || pinned_ge_fail "source differs beyond the validated build-contrib cache"
for repo_key in wine dxvk protonfixes; do
  [[ "$(pinned_ge_state_value "$STATE_FILE" "${repo_key}_repo_state_sha256")" \
      == "$(pinned_ge_repo_state_digest "$SOURCE_DIR/$repo_key")" ]] \
    || pinned_ge_fail "prepared $repo_key source state changed"
done

jobs="$(pinned_ge_state_value "$INVOCATION_FILE" jobs)"
make_jobs="$(pinned_ge_state_value "$INVOCATION_FILE" make_jobs)"
ninja_jobs="$(pinned_ge_state_value "$INVOCATION_FILE" ninja_jobs)"
product="$(pinned_ge_validate_job_schedule "$jobs" "$make_jobs" "$ninja_jobs")"
pinned_ge_require_resume_invocation \
  "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$CACHE_HOME" \
  "$INVOCATION_FILE" "$STATE_FILE" \
  "$jobs" "$make_jobs" "$ninja_jobs" "$product"
for path in \
    "$BUILD_DIR/$PIN_BUILD_NAME" \
    "$BUILD_DIR/$PIN_BUILD_NAME.tar.gz" \
    "$BUILD_DIR/$PIN_BUILD_NAME.sha512sum" \
    "$BUILD_DIR/SHA256SUMS"; do
  pinned_ge_require_absent_path "retained adoption artifact" "$path"
done
pinned_ge_require_directory_or_absent \
  "retained redist staging directory" "$BUILD_DIR/redist"

pinned_ge_write_configure_state \
  "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$INVOCATION_FILE" \
  adopted-retained-vodfix2 "$CACHE_HOME" \
  "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" >/dev/null

printf 'Recorded one-shot retained configure state: %s\n' "$CONFIGURE_STATE"
printf 'No configure, source sync, compilation, Steam access, or publication occurred.\n'
