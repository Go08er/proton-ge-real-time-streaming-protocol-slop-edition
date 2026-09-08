#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 022

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
BUILD_DIR=""
STATE_DIR=""
PATCH_SERIES="$ROOT_DIR/patches/series"
CACHE_HOME="${PINNED_GE_CACHE_HOME:-}"
JOBS="${JOBS:-}"
MAKE_JOBS="${MAKE_JOBS:-}"
NINJA_JOBS="${NINJA_JOBS:-}"
PACKAGE_RELEASE="${PACKAGE_RELEASE:-}"
STREAM_LOG="${PINNED_GE_STREAM_LOG:-1}"
NICE_LEVEL="${PINNED_GE_NICE:-19}"
SCHEDULE_PRODUCT=""
RESUME=0

usage() {
  cat <<'EOF'
Usage: build-pinned-ge.sh [OPTIONS]

Build and verify the already-prepared pinned GE source through GE's direct
out-of-tree configure path. This command never installs into Steam and never
publishes. The selected development snapshot is forced to PACKAGE_RELEASE=0.

Options:
  --source DIR      prepared pinned GE checkout
  --build-dir DIR   isolated out-of-tree build directory
  --state-dir DIR   preparation audit/state directory
  --cache-home DIR  isolated Podman, Cargo, and ccache home
  --jobs N          global nested scheduler budget (active default/cap comes from the selected config)
  --make-jobs N     orchestrator GNU Make jobs (default comes from selected config)
  --ninja-jobs N    jobs per component (default comes from selected config)
  --resume           reuse a separately fingerprinted existing configure result
  --config FILE     pinned config (default: active exact snapshot)
  --series FILE     project patch series (use the archived series for historical pins)
  -h, --help        show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --build-dir)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      BUILD_DIR="$2"
      shift 2
      ;;
    --state-dir)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      STATE_DIR="$2"
      shift 2
      ;;
    --cache-home)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      CACHE_HOME="$2"
      shift 2
      ;;
    --jobs)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      JOBS="$2"
      shift 2
      ;;
    --make-jobs)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      MAKE_JOBS="$2"
      shift 2
      ;;
    --ninja-jobs)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      NINJA_JOBS="$2"
      shift 2
      ;;
    --resume)
      RESUME=1
      shift
      ;;
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PINNED_GE_CONFIG="$2"
      shift 2
      ;;
    --series)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_SERIES="$2"
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
if [[ -z "$CACHE_HOME" ]]; then
  CACHE_HOME="${HOME:?HOME must be set}/Documents/proton-rtsp/.podman-home"
fi
[[ -n "$SOURCE_DIR" ]] || SOURCE_DIR="$(pinned_ge_default_source)"
[[ -n "$BUILD_DIR" ]] || BUILD_DIR="$(pinned_ge_default_build_dir)"
[[ -n "$STATE_DIR" ]] || STATE_DIR="$(pinned_ge_default_state_dir)"
[[ -n "$JOBS" ]] || JOBS="$PIN_DEFAULT_JOBS"
[[ -n "$PACKAGE_RELEASE" ]] || PACKAGE_RELEASE="$PIN_PACKAGE_RELEASE"
schedule="$(pinned_ge_resolve_job_schedule \
  "$JOBS" "$MAKE_JOBS" "$NINJA_JOBS")" || exit 1
IFS=$'\t' read -r JOBS MAKE_JOBS NINJA_JOBS SCHEDULE_PRODUCT <<<"$schedule"

SOURCE_DIR="$(realpath -m -- "$SOURCE_DIR")"
BUILD_DIR="$(realpath -m -- "$BUILD_DIR")"
STATE_DIR="$(realpath -m -- "$STATE_DIR")"
PATCH_SERIES="$(realpath -e -- "$PATCH_SERIES")"
CACHE_HOME="$(realpath -m -- "$CACHE_HOME")"
for pair in \
    "GE source path:$SOURCE_DIR" "GE build path:$BUILD_DIR" \
    "GE state path:$STATE_DIR" "GE cache home:$CACHE_HOME"; do
  pinned_ge_require_safe_absolute_path "${pair%%:*}" "${pair#*:}"
done
CONTRIB_MANIFEST="$(pinned_ge_build_contrib_manifest_path)"

pinned_ge_validate_job_schedule "$JOBS" "$MAKE_JOBS" "$NINJA_JOBS" >/dev/null
[[ "$PACKAGE_RELEASE" == 0 && "$PIN_PACKAGE_RELEASE" == 0 ]] \
  || pinned_ge_fail "this local game-test path requires PACKAGE_RELEASE=0"
[[ "$PIN_WITHOUT_NVIDIA_LIBS" == 1 && "$PIN_WITHOUT_VKLAYERS" == 1 ]] \
  || pinned_ge_fail "this snapshot requires both optional component groups disabled"
[[ "$STREAM_LOG" == 0 || "$STREAM_LOG" == 1 ]] \
  || pinned_ge_fail "PINNED_GE_STREAM_LOG must be 0 or 1"
[[ "$NICE_LEVEL" =~ ^([0-9]|1[0-9])$ ]] \
  || pinned_ge_fail "PINNED_GE_NICE must be between 0 and 19"

require_prefetched_build_contrib() {
  local manifest="$1"
  local entries_text entry path _mode _size _sha
  local -a entries missing

  entries_text="$(pinned_ge_build_contrib_entries "$manifest")" || return 1
  entries=()
  missing=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path _mode _size _sha <<<"$entry"
    [[ -f "$SOURCE_DIR/$path" && ! -L "$SOURCE_DIR/$path" ]] \
      || missing+=("$path")
  done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    printf 'ERROR: declared build inputs are missing from the prepared source:\n' >&2
    printf '  %s\n' "${missing[@]}" >&2
    printf '\nFetch and verify them before building by running:\n' >&2
    printf '  ./scripts/prefetch-pinned-ge-contrib.sh --config %q --source %q\n' \
      "$PINNED_GE_CONFIG_PATH" "$SOURCE_DIR" >&2
    return 1
  fi

  pinned_ge_require_complete_build_contrib "$SOURCE_DIR" "$manifest"
}

# Fail before the low-impact re-exec and before taking the shared build lock.
# A missing prefetched input should cost seconds, not a build slot.
require_prefetched_build_contrib "$CONTRIB_MANIFEST"

if [[ "${PINNED_GE_LOW_IMPACT_REEXEC:-0}" != 1 ]]; then
  export PINNED_GE_LOW_IMPACT_REEXEC=1
  REEXEC_ARGS=(
    --source "$SOURCE_DIR" --build-dir "$BUILD_DIR"
    --state-dir "$STATE_DIR" --cache-home "$CACHE_HOME"
    --series "$PATCH_SERIES"
    --jobs "$JOBS" --make-jobs "$MAKE_JOBS"
    --ninja-jobs "$NINJA_JOBS" --config "$PINNED_GE_CONFIG_PATH"
  )
  [[ "$RESUME" == 0 ]] || REEXEC_ARGS+=(--resume)
  if command -v ionice >/dev/null 2>&1; then
    exec ionice -c 3 nice -n "$NICE_LEVEL" bash \
      "$ROOT_DIR/scripts/build-pinned-ge.sh" "${REEXEC_ARGS[@]}"
  fi
  exec nice -n "$NICE_LEVEL" bash "$ROOT_DIR/scripts/build-pinned-ge.sh" \
    "${REEXEC_ARGS[@]}"
fi

BUILD_LOCK="${PINNED_GE_BUILD_LOCK:-${HOME:?HOME must be set}/Documents/.proton-build.lock}"
mkdir -p "$(dirname "$BUILD_LOCK")"
pinned_ge_require_regular_or_absent "workspace build lock" "$BUILD_LOCK"
exec 9>"$BUILD_LOCK"
flock -n 9 \
  || pinned_ge_fail "another cooperating Proton build holds $BUILD_LOCK"

STATE_FILE="$STATE_DIR/prepared-source.tsv"
PATCH_LOG="$STATE_DIR/patch-driver.log"
PATCH_AUDIT="$STATE_DIR/rtsp-patch-audit.tsv"
SOURCE_AUDIT="$STATE_DIR/wine-source-audit.tsv"
FFMPEG_SECURITY_PATCH_AUDIT="$STATE_DIR/ffmpeg-security-patch-audit.tsv"
FFMPEG_SOURCE_AUDIT="$STATE_DIR/ffmpeg-source-audit.tsv"
FFMPEG_CRYPTO_BUILD_AUDIT="$STATE_DIR/ffmpeg-crypto-build-audit.tsv"
GENERATED_DRIVER="$STATE_DIR/protonprep-valve-staging.generated.sh"
PATCH_ROOT="$ROOT_DIR/patches"
FFMPEG_SECURITY_PATCH_ROOT="$ROOT_DIR/patches/ffmpeg-security"
FFMPEG_SECURITY_PATCH_SERIES="$ROOT_DIR/patches/ffmpeg-security/series"
FFMPEG_CRYPTO_BUILD_PATCH_ROOT="$ROOT_DIR/patches/ffmpeg-build"
FFMPEG_CRYPTO_BUILD_PATCH_SERIES="$ROOT_DIR/patches/ffmpeg-build/series"
PROTON_LAUNCHER_PATCH="$ROOT_DIR/patches/proton/0001-launcher-add-scoped-xrizer-mode.patch"
INVOCATION_FILE="$STATE_DIR/build-invocation.tsv"
CONFIGURE_STATE="$STATE_DIR/configure-state.tsv"

require_state() {
  local key="$1"
  local expected="$2"
  local actual
  actual="$(pinned_ge_state_value "$STATE_FILE" "$key")"
  [[ "$actual" == "$expected" ]] \
    || pinned_ge_fail "prepared-state $key is $actual, expected $expected"
}

pinned_ge_require_source_identity "$SOURCE_DIR"
pinned_ge_require_registered_submodules "$SOURCE_DIR"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  require_state prepared_source_version 2
else
  require_state prepared_source_version 1
fi
require_state pin_id "$PIN_ID"
require_state pin_kind "$PIN_KIND"
require_state source_commit "$PIN_SOURCE_COMMIT"
require_state source_tree "$PIN_SOURCE_TREE"
require_state source_path "$(cd "$SOURCE_DIR" && pwd -P)"
require_state package_release 0
require_state upstream_patch_driver_sha256 "$PIN_PATCH_DRIVER_SHA256"
require_state patch_series_digest \
  "$(pinned_ge_series_digest "$PATCH_ROOT" "$PATCH_SERIES")"
require_state proton_launcher_patch \
  "$(pinned_ge_launcher_patch_record)"
require_state proton_launcher_patch_sha256 \
  "$(pinned_ge_launcher_patch_sha256)"
require_state make_requests_interpreter perl
require_state upstream_patch_mode fail-fast-batch
require_state rtsp_hook_order after-ge-video-rework-before-autoreconf
require_state ninja_job_source make-variable-NINJA_JOBS

[[ -f "$GENERATED_DRIVER" && ! -L "$GENERATED_DRIVER" ]] \
  || pinned_ge_fail "generated fail-fast patch driver is absent"
require_state generated_patch_driver_sha256 \
  "$(pinned_ge_sha256 "$GENERATED_DRIVER")"
[[ -f "$PATCH_AUDIT" && ! -L "$PATCH_AUDIT" ]] \
  || pinned_ge_fail "strict patch audit is absent"
require_state patch_audit_sha256 "$(pinned_ge_sha256 "$PATCH_AUDIT")"
[[ -f "$SOURCE_AUDIT" && ! -L "$SOURCE_AUDIT" ]] \
  || pinned_ge_fail "Wine source audit is absent"
require_state source_audit_sha256 "$(pinned_ge_sha256 "$SOURCE_AUDIT")"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  [[ -f "$FFMPEG_SECURITY_PATCH_AUDIT" \
      && ! -L "$FFMPEG_SECURITY_PATCH_AUDIT" ]] \
    || pinned_ge_fail "strict FFmpeg security patch audit is absent"
  [[ -f "$FFMPEG_SOURCE_AUDIT" && ! -L "$FFMPEG_SOURCE_AUDIT" ]] \
    || pinned_ge_fail "FFmpeg source audit is absent"
  require_state ffmpeg_security_fix_commits \
    "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
  require_state ffmpeg_security_series_sha256 \
    "$PIN_FFMPEG_SECURITY_SERIES_SHA256"
  require_state ffmpeg_security_series_digest \
    "$(pinned_ge_series_digest \
      "$FFMPEG_SECURITY_PATCH_ROOT" "$FFMPEG_SECURITY_PATCH_SERIES")"
  require_state ffmpeg_security_patch_audit_sha256 \
    "$(pinned_ge_sha256 "$FFMPEG_SECURITY_PATCH_AUDIT")"
  require_state ffmpeg_source_audit_sha256 \
    "$(pinned_ge_sha256 "$FFMPEG_SOURCE_AUDIT")"
  require_state ffmpeg_security_magicyuv_blob \
    "$PIN_FFMPEG_SECURITY_MAGICYUV_BLOB"
  require_state ffmpeg_security_tls_version_blob \
    "$PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB"
  [[ -f "$FFMPEG_CRYPTO_BUILD_AUDIT" \
      && ! -L "$FFMPEG_CRYPTO_BUILD_AUDIT" ]] \
    || pinned_ge_fail "FFmpeg crypto-build audit is absent"
  require_state ffmpeg_crypto_build_origin "$PIN_FFMPEG_CRYPTO_BUILD_ORIGIN"
  require_state ffmpeg_crypto_build_patch_sha256 \
    "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256"
  require_state ffmpeg_crypto_build_series_sha256 \
    "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256"
  require_state ffmpeg_crypto_build_series_digest \
    "$(pinned_ge_series_digest \
      "$FFMPEG_CRYPTO_BUILD_PATCH_ROOT" "$FFMPEG_CRYPTO_BUILD_PATCH_SERIES")"
  require_state ffmpeg_crypto_build_audit_sha256 \
    "$(pinned_ge_sha256 "$FFMPEG_CRYPTO_BUILD_AUDIT")"
  require_state ffmpeg_crypto_build_makefile_blob \
    "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB"
fi

require_state upstream_rules_meson_sha256 "$PIN_RULES_MESON_SHA256"
normalized_rules_sha256="$(pinned_ge_sha256 "$SOURCE_DIR/make/rules-meson.mk")"
[[ "$normalized_rules_sha256" == "$PIN_RULES_MESON_NORMALIZED_SHA256" ]] \
  || pinned_ge_fail "bounded Meson/Ninja rule differs from the accepted result"
require_state normalized_rules_meson_sha256 "$PIN_RULES_MESON_NORMALIZED_SHA256"
rg -Fq 'ninja -j"$$(NINJA_JOBS)" -C "$$($(2)_$(3)_OBJ)" install' \
  "$SOURCE_DIR/make/rules-meson.mk" \
  || pinned_ge_fail "bounded Meson/Ninja invocation is absent"

pinned_ge_require_complete_build_contrib "$SOURCE_DIR" "$CONTRIB_MANIFEST"
require_state root_repo_state_sha256 \
  "$(pinned_ge_repo_state_digest "$SOURCE_DIR" "$CONTRIB_MANIFEST")"
require_state wine_repo_state_sha256 \
  "$(pinned_ge_repo_state_digest "$SOURCE_DIR/wine")"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  require_state ffmpeg_repo_state_sha256 \
    "$(pinned_ge_repo_state_digest "$SOURCE_DIR/ffmpeg")"
fi
require_state dxvk_repo_state_sha256 \
  "$(pinned_ge_repo_state_digest "$SOURCE_DIR/dxvk")"
require_state protonfixes_repo_state_sha256 \
  "$(pinned_ge_repo_state_digest "$SOURCE_DIR/protonfixes")"
if [[ "$PIN_PROTON_LAUNCHER_POLICY" == scoped-xrizer ]]; then
  pinned_ge_git -C "$SOURCE_DIR" apply --check --reverse -- \
    "$PROTON_LAUNCHER_PATCH"
else
  pinned_ge_require_upstream_launcher "$SOURCE_DIR" "$SOURCE_DIR/proton"
fi
python3 - "$SOURCE_DIR/proton" <<'PY'
from pathlib import Path
import sys

launcher = Path(sys.argv[1])
compile(launcher.read_text(), str(launcher), "exec")
PY

mapfile -t REGISTERED < <(pinned_ge_registered_submodules "$SOURCE_DIR")
for path in "${REGISTERED[@]}"; do
  case "$path" in
    dxvk|ffmpeg|protonfixes|wine) continue ;;
    vkd3d-proton)
      if [[ -d "$SOURCE_DIR/patches/vkd3d-proton" ]]; then
        pinned_ge_require_upstream_component_patch_state \
          "$SOURCE_DIR/$path" "$SOURCE_DIR/patches/vkd3d-proton"
        continue
      fi
      ;;
  esac
  [[ -z "$(pinned_ge_git -C "$SOURCE_DIR/$path" status --porcelain --untracked-files=all)" ]] \
    || pinned_ge_fail "unexpected prepared-source changes in submodule: $path"
done

rejects="$({
  find "$SOURCE_DIR" -path '*/.git' -prune -o -type f -name '*.rej' -print
} | LC_ALL=C sort)"
[[ -z "$rejects" ]] || {
  printf 'Patch rejects remain:\n%s\n' "$rejects" >&2
  pinned_ge_fail "prepared source contains rejected hunks"
}
[[ -f "$PATCH_LOG" && ! -L "$PATCH_LOG" ]] \
  || pinned_ge_fail "patch-driver log is absent"
if rg -n 'FAILED|malformed patch|can.t find file to patch|Reversed .* patch' \
    "$PATCH_LOG" >&2; then
  pinned_ge_fail "patch-driver log contains a failure signature"
fi

pinned_ge_require_prepared_media_provenance "$SOURCE_DIR" "$STATE_DIR" "$PATCH_SERIES"
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  pinned_ge_require_prepared_patch_overlap_provenance \
    "$SOURCE_DIR" "$STATE_DIR"
fi
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  pinned_ge_require_prepared_ffmpeg_security_provenance \
    "$SOURCE_DIR" "$STATE_DIR"
  pinned_ge_require_prepared_ffmpeg_crypto_build_provenance \
    "$SOURCE_DIR" "$STATE_DIR"
fi
ge_diff_check_status_value="$(pinned_ge_state_value \
  "$STATE_FILE" ge_pre_rtsp_diff_check_status)"
ge_diff_check_sha256="$(pinned_ge_state_value \
  "$STATE_FILE" ge_pre_rtsp_diff_check_sha256)"
ge_media_cleanup_digest="$(pinned_ge_state_value \
  "$STATE_FILE" ge_media_cleanup_normalization_digest)"
ge_media_cleanup_audit_sha256="$(pinned_ge_state_value \
  "$STATE_FILE" ge_media_cleanup_audit_sha256)"

git -C "$SOURCE_DIR" diff --check
git -C "$SOURCE_DIR/dxvk" diff --check
git -C "$SOURCE_DIR/protonfixes" diff --check
"$ROOT_DIR/scripts/audit-patched-wine.sh" --require-alpha-series \
  --require-a311-series \
  --require-a312-series \
  --series "$PATCH_SERIES" \
  --wine-tree "$SOURCE_DIR/wine" \
  >/dev/null
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" \
    --require-security-series >/dev/null
else
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" >/dev/null
fi

TOOL_DIR="$BUILD_DIR/$PIN_BUILD_NAME"
ARCHIVE="$BUILD_DIR/$PIN_BUILD_NAME.tar.gz"
SHA512="$BUILD_DIR/$PIN_BUILD_NAME.sha512sum"
SHA256="$BUILD_DIR/SHA256SUMS"
REDIST_WORK_DIR="$BUILD_DIR/redist"
for path in "$TOOL_DIR" "$ARCHIVE" "$SHA512" "$SHA256"; do
  pinned_ge_require_absent_path "build artifact" "$path"
done
pinned_ge_require_directory_or_absent "redist staging directory" "$REDIST_WORK_DIR"

mkdir -p "$BUILD_DIR" "$STATE_DIR" "$ROOT_DIR/logs/$PIN_ID"
mkdir -p "$CACHE_HOME/.config/containers" "$CACHE_HOME/.cargo" \
  "$CACHE_HOME/.ccache" "$CACHE_HOME/.cache" "$CACHE_HOME/.local/share"

POLICY_FILE="$CACHE_HOME/.config/containers/policy.json"
pinned_ge_require_regular_or_absent "container policy" "$POLICY_FILE"
if [[ ! -e "$POLICY_FILE" ]]; then
  cat >"$POLICY_FILE" <<'JSON'
{
  "default": [
    {
      "type": "insecureAcceptAnything"
    }
  ]
}
JSON
fi

GIT_CONFIG_FILE="$STATE_DIR/build-gitconfig"
[[ ! -L "$GIT_CONFIG_FILE" ]] \
  || pinned_ge_fail "refusing symlinked build Git config: $GIT_CONFIG_FILE"
cat >"$GIT_CONFIG_FILE" <<'EOF'
[maintenance]
	auto = false
[gc]
	auto = 0
[fetch]
	parallel = 1
[submodule]
	fetchJobs = 1
EOF

export HOME="$CACHE_HOME"
export XDG_CACHE_HOME="$CACHE_HOME/.cache"
export XDG_CONFIG_HOME="$CACHE_HOME/.config"
export XDG_DATA_HOME="$CACHE_HOME/.local/share"
export CARGO_HOME="$CACHE_HOME/.cargo"
export CCACHE_DIR="$CACHE_HOME/.ccache"
export GIT_CONFIG_GLOBAL="$GIT_CONFIG_FILE"
export GIT_OPTIONAL_LOCKS=0
unset MAKEFLAGS MFLAGS GNUMAKEFLAGS
export NINJA_JOBS="$NINJA_JOBS"
export CARGO_BUILD_JOBS="$NINJA_JOBS"
export CMAKE_BUILD_PARALLEL_LEVEL="$NINJA_JOBS"
export NIX_BUILD_CORES="$NINJA_JOBS"

LOG_FILE="$ROOT_DIR/logs/$PIN_ID/build-redist.log"
HOST_BASH="$(command -v bash)"
pinned_ge_require_safe_absolute_path "host Bash" "$HOST_BASH"
[[ -x "$HOST_BASH" ]] \
  || pinned_ge_fail "could not resolve an executable host Bash"
PODMAN_BIN="$(command -v podman)" \
  || pinned_ge_fail "Podman is required in the pinned build environment"
pinned_ge_require_local_steamrt_image "$PODMAN_BIN" >/dev/null
for output in "$LOG_FILE" "$INVOCATION_FILE" "$CONFIGURE_STATE"; do
  [[ ! -L "$output" ]] || pinned_ge_fail "refusing symlinked build output: $output"
done

write_build_invocation() {
  {
    if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
      printf 'build_invocation_version\t6\n'
    else
      printf 'build_invocation_version\t5\n'
    fi
    printf 'pin_id\t%s\n' "$PIN_ID"
    printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
    printf 'build_name\t%s\n' "$PIN_BUILD_NAME"
    printf 'source_path\t%s\n' "$SOURCE_DIR"
    printf 'build_path\t%s\n' "$BUILD_DIR"
    printf 'state_path\t%s\n' "$STATE_DIR"
    printf 'cache_home\t%s\n' "$CACHE_HOME"
    printf 'package_release\t0\n'
    printf 'jobs\t%s\n' "$JOBS"
    printf 'global_jobs\t%s\n' "$JOBS"
    printf 'make_jobs\t%s\n' "$MAKE_JOBS"
    printf 'ninja_jobs\t%s\n' "$NINJA_JOBS"
    printf 'scheduler_product\t%s\n' "$SCHEDULE_PRODUCT"
    printf 'scheduler_policy\tmake-times-ninja-at-most-global\n'
    printf 'recursive_make_parallelism\tjobserver\n'
    printf 'cargo_jobs_env\t%s\n' "$NINJA_JOBS"
    printf 'cmake_jobs_env\t%s\n' "$NINJA_JOBS"
    printf 'nix_build_cores_env\t%s\n' "$NINJA_JOBS"
    printf 'host_make_shell\t%s\n' "$HOST_BASH"
    printf 'ge_pre_rtsp_diff_check_status\t%s\n' "$ge_diff_check_status_value"
    printf 'ge_pre_rtsp_diff_check_sha256\t%s\n' "$ge_diff_check_sha256"
    printf 'ge_media_cleanup_manifest_sha256\t%s\n' \
      "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256"
    printf 'ge_media_cleanup_normalization_digest\t%s\n' "$ge_media_cleanup_digest"
    printf 'ge_media_cleanup_audit_sha256\t%s\n' "$ge_media_cleanup_audit_sha256"
    if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
      printf 'ge_patch_overlap_mode\tstock-effective-exact-skip\n'
      printf 'ge_patch_overlap_manifest_sha256\t%s\n' \
        "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256"
      printf 'ge_patch_overlap_audit_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ge_patch_overlap_audit_sha256)"
      printf 'ge_patch_overlap_component_commit\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ge_patch_overlap_component_commit)"
      printf 'ge_patch_overlap_target_blob\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" ge_patch_overlap_target_blob)"
      printf 'ge_patch_overlap_declared_target_blob\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ge_patch_overlap_declared_target_blob)"
      printf 'ge_patch_overlap_residual_diff_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ge_patch_overlap_residual_diff_sha256)"
    fi
    printf 'proton_launcher_patch_sha256\t%s\n' \
      "$(pinned_ge_launcher_patch_sha256)"
    if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
      printf 'ffmpeg_security_fix_commits\t%s\n' \
        "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
      printf 'ffmpeg_security_series_sha256\t%s\n' \
        "$PIN_FFMPEG_SECURITY_SERIES_SHA256"
      printf 'ffmpeg_security_series_digest\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_security_series_digest)"
      printf 'ffmpeg_security_patch_audit_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_security_patch_audit_sha256)"
      printf 'ffmpeg_source_audit_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" ffmpeg_source_audit_sha256)"
      printf 'ffmpeg_repo_state_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" ffmpeg_repo_state_sha256)"
      printf 'ffmpeg_security_magicyuv_blob\t%s\n' \
        "$PIN_FFMPEG_SECURITY_MAGICYUV_BLOB"
      printf 'ffmpeg_security_tls_version_blob\t%s\n' \
        "$PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB"
      printf 'ffmpeg_crypto_build_origin\t%s\n' \
        "$PIN_FFMPEG_CRYPTO_BUILD_ORIGIN"
      printf 'ffmpeg_crypto_build_patch_sha256\t%s\n' \
        "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256"
      printf 'ffmpeg_crypto_build_series_sha256\t%s\n' \
        "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256"
      printf 'ffmpeg_crypto_build_series_digest\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_crypto_build_series_digest)"
      printf 'ffmpeg_crypto_build_audit_sha256\t%s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_crypto_build_audit_sha256)"
      printf 'ffmpeg_crypto_build_makefile_blob\t%s\n' \
        "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB"
    fi
    printf 'rtsp_hook_order\tafter-ge-video-rework-before-autoreconf\n'
    printf 'final_diff_check_baseline_match\tpassed\n'
    printf 'final_winegstreamer_external_references\t0\n'
    printf 'without_nvidia_libs\t1\n'
    printf 'without_vklayers\t1\n'
    printf 'target\tredist\n'
    printf 'steamrt_image\t%s\n' "$PIN_STEAMRT_IMAGE"
    printf 'steamrt_image_id\t%s\n' "$PIN_STEAMRT_IMAGE_ID"
    printf 'steam_install\tdisabled\n'
    printf 'publication\tdisabled\n'
  } >"$INVOCATION_FILE"
}

manifest_build_input_record() {
  local requested_path="$1"
  local entries_text entry path _mode _size _sha
  local found=""
  local -a entries

  entries_text="$(pinned_ge_build_contrib_entries "$CONTRIB_MANIFEST")" \
    || return 1
  entries=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path _mode _size _sha <<<"$entry"
    [[ "$path" == "$requested_path" ]] || continue
    [[ -z "$found" ]] \
      || pinned_ge_fail "duplicate build-input mapping: $requested_path" \
      || return 1
    found="$entry"
  done
  [[ -n "$found" ]] \
    || pinned_ge_fail "build input is not declared by the selected pin: $requested_path" \
    || return 1
  printf '%s\n' "$found"
}

stage_manifest_build_input() {
  local path="$1"
  local target_file="$2"
  local target_mode="$3"
  local label="$4"
  local record expected_mode expected_size expected_sha
  local source_file target_parent actual_mode actual_size actual_sha

  record="$(manifest_build_input_record "$path")" || return 1
  IFS=$'\t' read -r \
    _path expected_mode expected_size expected_sha <<<"$record"
  source_file="$SOURCE_DIR/$path"
  [[ -f "$source_file" && ! -L "$source_file" ]] \
    || pinned_ge_fail "$label source is absent or unsafe: $path" || return 1
  actual_mode="0$(stat -c '%a' -- "$source_file")"
  actual_size="$(stat -c '%s' -- "$source_file")"
  actual_sha="$(pinned_ge_sha256 "$source_file")"
  [[ "$actual_mode" == "$expected_mode" \
      && "$actual_size" == "$expected_size" \
      && "$actual_sha" == "$expected_sha" ]] \
    || pinned_ge_fail "$label source differs from its manifest: $path" \
    || return 1

  target_file="$(realpath -m -- "$target_file")"
  [[ "$target_file" == "$BUILD_DIR/"* ]] \
    || pinned_ge_fail "$label destination escapes the build directory" \
    || return 1
  target_parent="${target_file%/*}"
  pinned_ge_require_directory_or_absent "$label destination" "$target_parent" \
    || return 1
  mkdir -p -- "$target_parent" || return 1
  [[ "$(realpath -m -- "$target_parent")" == "$target_parent" ]] \
    || pinned_ge_fail "$label destination traverses a symlink" || return 1
  pinned_ge_require_regular_or_absent "$label" "$target_file" || return 1

  if [[ -f "$target_file" ]]; then
    actual_mode="0$(stat -c '%a' -- "$target_file")"
    actual_size="$(stat -c '%s' -- "$target_file")"
    actual_sha="$(pinned_ge_sha256 "$target_file")"
    if [[ "$actual_mode" == "$target_mode" \
        && "$actual_size" == "$expected_size" \
        && "$actual_sha" == "$expected_sha" ]]; then
      printf 'Using seeded %s: %s\n' "$label" "$path"
      return 0
    fi
  fi

  install -m "$target_mode" -- "$source_file" "$target_file"
  actual_mode="0$(stat -c '%a' -- "$target_file")"
  actual_size="$(stat -c '%s' -- "$target_file")"
  actual_sha="$(pinned_ge_sha256 "$target_file")"
  [[ "$actual_mode" == "$target_mode" \
      && "$actual_size" == "$expected_size" \
      && "$actual_sha" == "$expected_sha" ]] \
    || pinned_ge_fail "could not seed verified $label: $path" || return 1
  printf 'Seeded %s: %s\n' "$label" "$path"
}

seed_protonfixes_build_contrib() {
  local manifest_prefix="contrib/protonfixes/"
  local unzip_prefix="${manifest_prefix}downloads/unzip/"
  local zenity_path="${manifest_prefix}zenity-rs/zenity-rs"
  local target_root="$BUILD_DIR/obj-protonfixes-x86_64"
  local entries_text entry path _mode _size _sha relative_path
  local zenity_target="$target_root/zenity-rs/zenity-rs"
  local zenity_stamp="$target_root/.build-zenity-rs-dist"
  local saw_zenity=0
  local -a entries

  entries_text="$(pinned_ge_build_contrib_entries "$CONTRIB_MANIFEST")" \
    || return 1
  entries=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path _mode _size _sha <<<"$entry"
    case "$path" in
      "$unzip_prefix"*)
        relative_path="${path#"$manifest_prefix"}"
        stage_manifest_build_input \
          "$path" "$target_root/$relative_path" 0644 \
          "ProtonFixes build input" || return 1
        ;;
      "$zenity_path")
        stage_manifest_build_input \
          "$path" "$zenity_target" 0755 "ProtonFixes zenity-rs input" \
          || return 1
        saw_zenity=1
        ;;
      "$manifest_prefix"*)
        pinned_ge_fail "unknown ProtonFixes build-input mapping: $path"
        return 1
        ;;
    esac
  done

  if [[ "$saw_zenity" == 1 ]]; then
    pinned_ge_require_regular_or_absent \
      "ProtonFixes zenity-rs build stamp" "$zenity_stamp" || return 1
    touch -- "$zenity_stamp" || return 1
    printf 'Recorded ProtonFixes zenity-rs offline build stamp.\n'
  fi
}

piper_build_contrib_is_declared() {
  local entries_text entry path _mode _size _sha
  local -a entries

  entries_text="$(pinned_ge_build_contrib_entries "$CONTRIB_MANIFEST")" \
    || return 1
  entries=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path _mode _size _sha <<<"$entry"
    [[ "$path" == contrib/piper/* ]] || continue
    return 0
  done
  return 1
}

require_declared_piper_build_inputs() {
  local prefix="contrib/piper/downloads/"
  local entries_text entry path _mode _size _sha expected
  local -a entries expected_paths
  declare -A expected_map=() found=()

  expected_paths=(
    "${prefix}fmt/10.0.0.zip"
    "${prefix}spdlog/v1.12.0.zip"
    "${prefix}piper-phonemize/pic.zip"
    "${prefix}piper-phonemize/onnxruntime/onnxruntime-linux-x64-1.14.1.tgz"
    "${prefix}piper-phonemize/espeak-ng/0f65aa301e0d6bae5e172cc74197d32a6182200f.zip"
    "${prefix}piper-phonemize/espeak-ng/sonic/fbf75c3d6d846bad3bb3d456cbc5d07d9fd8c104.zip"
  )
  for expected in "${expected_paths[@]}"; do
    expected_map["$expected"]=1
  done

  entries_text="$(pinned_ge_build_contrib_entries "$CONTRIB_MANIFEST")" \
    || return 1
  entries=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path _mode _size _sha <<<"$entry"
    [[ "$path" == contrib/piper/* ]] || continue
    [[ -n "${expected_map[$path]+present}" ]] \
      || pinned_ge_fail "unknown Piper build-input mapping: $path" || return 1
    found["$path"]=1
  done
  for expected in "${expected_paths[@]}"; do
    [[ -n "${found[$expected]+present}" ]] \
      || pinned_ge_fail "required Piper build-input mapping is absent: $expected" \
      || return 1
  done
}

rewrite_generated_download_url() {
  local script="$1"
  local upstream_url="$2"
  local manifest_path="$3"
  local local_url="file://$SOURCE_DIR/$manifest_path"

  manifest_build_input_record "$manifest_path" >/dev/null || return 1
  [[ -f "$script" && ! -L "$script" ]] \
    || pinned_ge_fail "generated Piper download script is absent or unsafe: $script" \
    || return 1
  python3 - "$script" "$upstream_url" "$local_url" <<'PY'
from pathlib import Path
import os
import sys
import tempfile

path = Path(sys.argv[1])
upstream = sys.argv[2]
local = sys.argv[3]
text = path.read_text()
upstream_count = text.count(upstream)
local_count = text.count(local)
if upstream_count == 1 and local_count == 0:
    updated = text.replace(upstream, local)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        Path(temporary).write_text(updated)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
elif upstream_count == 0 and local_count == 1:
    pass
else:
    raise SystemExit(
        f"{path}: expected exactly one upstream URL or one local replacement; "
        f"found upstream={upstream_count}, local={local_count}"
    )
PY
}

rewrite_piper_top_level_downloads() {
  local object_root="$BUILD_DIR/obj-piper-x86_64"

  require_declared_piper_build_inputs || return 1
  rewrite_generated_download_url \
    "$object_root/f/src/fmt_external-stamp/download-fmt_external.cmake" \
    "https://github.com/fmtlib/fmt/archive/refs/tags/10.0.0.zip" \
    "contrib/piper/downloads/fmt/10.0.0.zip" || return 1
  rewrite_generated_download_url \
    "$object_root/s/src/spdlog_external-stamp/download-spdlog_external.cmake" \
    "https://github.com/gabime/spdlog/archive/refs/tags/v1.12.0.zip" \
    "contrib/piper/downloads/spdlog/v1.12.0.zip" || return 1
  rewrite_generated_download_url \
    "$object_root/p/src/piper_phonemize_external-stamp/download-piper_phonemize_external.cmake" \
    "https://github.com/shaunren/piper-phonemize/archive/refs/heads/pic.zip" \
    "contrib/piper/downloads/piper-phonemize/pic.zip" || return 1
}

extract_verified_sonic_source() {
  local archive_path="contrib/piper/downloads/piper-phonemize/espeak-ng/sonic/fbf75c3d6d846bad3bb3d456cbc5d07d9fd8c104.zip"
  local destination="$BUILD_DIR/obj-piper-x86_64/pinned-contrib/sonic-source"

  manifest_build_input_record "$archive_path" >/dev/null || return 1
  python3 - "$SOURCE_DIR/$archive_path" "$destination" "$BUILD_DIR" <<'PY' \
    || return 1
from pathlib import Path, PurePosixPath
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import zipfile

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
build_root = Path(sys.argv[3]).resolve()
if destination.parent.resolve() != (build_root / "obj-piper-x86_64" / "pinned-contrib").resolve():
    raise SystemExit("Sonic extraction destination escaped its fixed build-only directory")

with zipfile.ZipFile(archive) as zipped:
    infos = zipped.infolist()
    roots = set()
    archived_files = {}
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise SystemExit(f"unsafe Sonic archive path: {name!r}")
        roots.add(path.parts[0])
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise SystemExit(f"Sonic archive contains a symlink: {name}")
        relative = PurePosixPath(*path.parts[1:])
        if relative.parts and not info.is_dir():
            archived_files[relative.as_posix()] = hashlib.sha256(
                zipped.read(info)
            ).hexdigest()
    if len(roots) != 1:
        raise SystemExit("Sonic archive must contain exactly one non-empty top-level directory")
    root = next(iter(roots))

    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise SystemExit("existing Sonic source destination is unsafe")
        actual_files = {}
        for entry in destination.rglob("*"):
            relative = entry.relative_to(destination).as_posix()
            if entry.is_symlink():
                raise SystemExit(f"existing Sonic source contains a symlink: {relative}")
            if entry.is_file():
                actual_files[relative] = hashlib.sha256(entry.read_bytes()).hexdigest()
            elif not entry.is_dir():
                raise SystemExit(f"existing Sonic source contains a special file: {relative}")
        if actual_files != archived_files:
            raise SystemExit("existing Sonic source differs from the verified archive")
        raise SystemExit(0)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".sonic-source.", dir=destination.parent))
    try:
        zipped.extractall(temporary)
        extracted = temporary / root
        if not (extracted / "sonic.c").is_file():
            raise RuntimeError("verified Sonic archive does not contain sonic.c")
        os.rename(extracted, destination)
        temporary.rmdir()
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
PY
  printf '%s\n' "$destination"
}

configure_piper_phonemize_offline_inputs() {
  local object_root="$BUILD_DIR/obj-piper-x86_64"
  local onnx_path="contrib/piper/downloads/piper-phonemize/onnxruntime/onnxruntime-linux-x64-1.14.1.tgz"
  local espeak_path="contrib/piper/downloads/piper-phonemize/espeak-ng/0f65aa301e0d6bae5e172cc74197d32a6182200f.zip"
  local nested_build="$object_root/p/src/piper_phonemize_external-build"
  local cmake_file="$object_root/p/src/piper_phonemize_external/CMakeLists.txt"
  local sonic_source espeak_url sonic_argument

  require_declared_piper_build_inputs || return 1
  stage_manifest_build_input \
    "$onnx_path" "$nested_build/download/onnxruntime-linux-x64-1.14.1.tgz" \
    0644 "Piper ONNX Runtime input" || return 1
  sonic_source="$(extract_verified_sonic_source)" || return 1
  espeak_url="file://$SOURCE_DIR/$espeak_path"
  sonic_argument="        CMAKE_ARGS -DFETCHCONTENT_SOURCE_DIR_SONIC-GIT:PATH=$sonic_source"
  [[ -f "$cmake_file" && ! -L "$cmake_file" ]] \
    || pinned_ge_fail \
      "extracted piper-phonemize CMakeLists is absent or unsafe" || return 1
  python3 - "$cmake_file" "$espeak_url" "$sonic_argument" <<'PY'
from pathlib import Path
import os
import sys
import tempfile

path = Path(sys.argv[1])
local_url = sys.argv[2]
sonic_argument = sys.argv[3]
upstream_url = (
    "https://github.com/rhasspy/espeak-ng/archive/"
    "0f65aa301e0d6bae5e172cc74197d32a6182200f.zip"
)
anchor = "        CMAKE_ARGS -DCMAKE_INSTALL_PREFIX:PATH=${ESPEAK_NG_DIR}"
text = path.read_text()
upstream_count = text.count(upstream_url)
local_count = text.count(local_url)
argument_count = text.count(sonic_argument)
if upstream_count == 1 and local_count == 0:
    text = text.replace(upstream_url, local_url)
elif not (upstream_count == 0 and local_count == 1):
    raise SystemExit(
        f"{path}: espeak URL replacement is not exact "
        f"(upstream={upstream_count}, local={local_count})"
    )
if argument_count == 0:
    if text.count(anchor) != 1:
        raise SystemExit(f"{path}: espeak CMAKE_ARGS anchor is not exact")
    text = text.replace(anchor, f"{anchor}\n{sonic_argument}")
elif argument_count != 1:
    raise SystemExit(f"{path}: Sonic source override is not exact")

original = path.read_text()
if text != original:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        Path(temporary).write_text(text)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
PY
}

if [[ "$RESUME" == 1 ]]; then
  [[ -f "$INVOCATION_FILE" && ! -L "$INVOCATION_FILE" ]] \
    || pinned_ge_fail "resume requires the original build invocation"
  pinned_ge_require_resume_invocation \
    "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$CACHE_HOME" \
    "$INVOCATION_FILE" "$STATE_FILE" \
    "$JOBS" "$MAKE_JOBS" "$NINJA_JOBS" "$SCHEDULE_PRODUCT"
  [[ "$(pinned_ge_state_value "$INVOCATION_FILE" host_make_shell)" \
      == "$HOST_BASH" ]] \
    || pinned_ge_fail "resume must use the same host Bash as the original invocation"
  pinned_ge_require_configure_state \
    "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$INVOCATION_FILE" "$CACHE_HOME"
else
  [[ ! -e "$INVOCATION_FILE" ]] \
    || pinned_ge_fail \
      "build invocation already exists; use --resume after recording configure state"
  [[ ! -e "$CONFIGURE_STATE" ]] \
    || pinned_ge_fail "configure state already exists; use --resume"
  [[ -z "$(find "$BUILD_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || pinned_ge_fail "fresh build directory is not empty; use a new path or --resume"
  write_build_invocation
fi

run_build() {
  local host_shell_makefile="$BUILD_DIR/.host-shell.mk"
  local make_target
  local -a make_args

  cd "$BUILD_DIR"
  if [[ "$RESUME" == 0 ]]; then
    bash "$SOURCE_DIR/configure.sh" \
      --build-name="$PIN_BUILD_NAME" \
      --target-arch=x86_64 \
      --container-engine=podman \
      --proton-sdk-image="$PIN_STEAMRT_IMAGE"
    pinned_ge_write_configure_state \
      "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$INVOCATION_FILE" \
      fresh-configure "$CACHE_HOME" none >/dev/null
  else
    printf 'Reusing verified configure result: %s\n' "$CONFIGURE_STATE"
  fi

  seed_protonfixes_build_contrib

  [[ ! -L "$host_shell_makefile" ]] \
    || pinned_ge_fail "refusing symlinked host-shell make fragment"
  {
    printf 'SHELL := %s\n' "$HOST_BASH"
    printf '%s\n' \
      'override MAKEOVERRIDES := $(filter-out SHELL=%,$(MAKEOVERRIDES))'
    printf '%s\n' \
      '.PHONY: pinned-piper-x86_64-configure' \
      'pinned-piper-x86_64-configure:' \
      $'\t$(DOCKER_BASE) $(MAKE) -j$(J) $(filter -j%,$(MAKEFLAGS)) -f $(firstword $(MAKEFILE_LIST)) $(MFLAGS) $(MAKEOVERRIDES) CONTAINER=1 piper-x86_64-configure' \
      '.PHONY: pinned-piper-phonemize-download' \
      'pinned-piper-phonemize-download:' \
      $'\t$(DOCKER_BASE) $(MAKE) -j$(J) $(filter -j%,$(MAKEFLAGS)) -C $(OBJ)/obj-piper-x86_64 -f CMakeFiles/piper_phonemize_external.dir/build.make $(MFLAGS) CONTAINER=1 p/src/piper_phonemize_external-stamp/piper_phonemize_external-download' \
      '.PHONY: pinned-resume-all-source' \
      'pinned-resume-all-source:' \
      $'\tif [ "$(ENABLE_CCACHE)" = "1" ]; then mkdir -p $(CCACHE_DIR); fi' \
      $'\tmkdir -p $(CARGO_HOME)' \
      $'\t$(DOCKER_BASE) $(MAKE) -j$(J) $(filter -j%,$(MAKEFLAGS)) -f $(firstword $(MAKEFILE_LIST)) $(MFLAGS) $(MAKEOVERRIDES) CONTAINER=1 all-source'
  } >"$host_shell_makefile"

  # GE's Makefile intentionally uses /bin/bash inside SteamRT. NixOS has no
  # host /bin/bash, so override SHELL while the host Makefile is parsed. The
  # second, host-only fragment removes that one command-line override from
  # MAKEOVERRIDES before recursive Make starts inside SteamRT, where /bin/bash
  # is valid. The container recipe also passes only the first makefile.
  make_args=(
    -f "$BUILD_DIR/Makefile" -f "$host_shell_makefile"
    SHELL="$HOST_BASH" -j"$MAKE_JOBS"
    J="$MAKE_JOBS"
    NINJA_JOBS="$NINJA_JOBS"
    WITHOUT_NVIDIA_LIBS="$PIN_WITHOUT_NVIDIA_LIBS"
    WITHOUT_VKLAYERS="$PIN_WITHOUT_VKLAYERS"
    "DOCKER_OPTS=--pull=never --network=none -e NINJA_JOBS=$NINJA_JOBS -e CARGO_BUILD_JOBS=$NINJA_JOBS -e CMAKE_BUILD_PARALLEL_LEVEL=$NINJA_JOBS -e NIX_BUILD_CORES=$NINJA_JOBS"
  )
  if [[ "$RESUME" == 1 ]]; then
    printf 'Reconciling component sources in a dedicated Make process.\n'
    make "${make_args[@]}" pinned-resume-all-source
    pinned_ge_require_resume_source_barrier "$SOURCE_DIR" "$BUILD_DIR"
    printf 'Source barrier passed; starting redist in a fresh Make process.\n'
  fi
  if piper_build_contrib_is_declared; then
    printf 'Configuring Piper build files in the offline build container.\n'
    make "${make_args[@]}" pinned-piper-x86_64-configure
    rewrite_piper_top_level_downloads
    printf 'Extracting the pinned piper-phonemize source offline.\n'
    make "${make_args[@]}" pinned-piper-phonemize-download
    configure_piper_phonemize_offline_inputs
  fi
  make_target=redist
  pinned_ge_require_directory_or_absent \
    "redist staging directory" "$REDIST_WORK_DIR"
  make "${make_args[@]}" "$make_target"
}

normalize_proton_version_metadata() {
  local version_file="$TOOL_DIR/version"
  local version_timestamp _generated_name version_extra
  local version_tmp

  [[ -f "$version_file" && ! -L "$version_file" ]] \
    || pinned_ge_fail "redist Proton version file is absent or unsafe: $version_file"
  [[ "$PIN_BUILD_NAME" != *[[:space:]]* ]] \
    || pinned_ge_fail "build name cannot be represented in Proton's two-field version format"

  read -r version_timestamp _generated_name version_extra <"$version_file" || true
  [[ "$version_timestamp" =~ ^[0-9]+$ ]] \
    || pinned_ge_fail "redist Proton version timestamp is invalid"
  [[ -z "$version_extra" ]] \
    || pinned_ge_fail "redist Proton version contains unexpected extra fields"

  # GE derives the second field from `git describe --tags`. A pinned
  # development snapshot can intentionally have no local tag refs, in which
  # case the upstream recipe silently emits only the epoch. ProtonFixes
  # requires exactly `<epoch> <name>` and otherwise aborts before Wine starts.
  # Use the already validated compatibility-tool build name deterministically.
  version_tmp="$(mktemp "$TOOL_DIR/.version.XXXXXX")"
  printf '%s %s\n' "$version_timestamp" "$PIN_BUILD_NAME" >"$version_tmp"
  chmod 0644 "$version_tmp"
  mv -f -- "$version_tmp" "$version_file"
}

write_artifact_provenance() {
  local provenance="$TOOL_DIR/RTSP-GE-BUILD.txt"

  [[ -d "$TOOL_DIR" && ! -L "$TOOL_DIR" ]] \
    || pinned_ge_fail "redist tool directory is absent or unsafe: $TOOL_DIR"
  [[ ! -L "$provenance" ]] \
    || pinned_ge_fail "refusing symlinked artifact provenance file"

  {
    printf 'Build name: %s\n' "$PIN_BUILD_NAME"
    printf 'Snapshot kind: %s\n' "$PIN_KIND"
    printf 'GE source: %s\n' "$PIN_SOURCE_URL"
    printf 'GE source label: %s\n' "$PIN_SOURCE_VERSION"
    printf 'GE commit: %s\n' "$PIN_SOURCE_COMMIT"
    printf 'GE tree: %s\n' "$PIN_SOURCE_TREE"
    printf 'Wine commit: %s\n' "$PIN_WINE_COMMIT"
    printf 'FFmpeg commit: %s\n' "$PIN_FFMPEG_COMMIT"
    printf 'SteamRT image ID: %s\n' "$PIN_STEAMRT_IMAGE_ID"
    printf 'Wine patch-series SHA-256: %s\n' \
      "$(pinned_ge_state_value "$STATE_FILE" patch_series_digest)"
    printf 'Proton launcher patch SHA-256: %s\n' \
      "$(pinned_ge_launcher_patch_sha256)"
    if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
      printf 'FFmpeg security records (3 upstream MagicYUV CVE fixes + project TLS policy): %s\n' \
        "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
      printf 'FFmpeg security patch-series SHA-256: %s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_security_series_digest)"
      printf 'FFmpeg security audit SHA-256: %s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_security_patch_audit_sha256)"
      printf 'FFmpeg patched source-state SHA-256: %s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" ffmpeg_repo_state_sha256)"
      printf 'FFmpeg crypto-build patch SHA-256: %s\n' \
        "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256"
      printf 'FFmpeg crypto-build audit SHA-256: %s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ffmpeg_crypto_build_audit_sha256)"
    fi
    if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
      printf 'GE inherited patch-overlap policy: stock-effective-exact-skip\n'
      printf 'GE patch-overlap manifest SHA-256: %s\n' \
        "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256"
      printf 'GE patch-overlap audit SHA-256: %s\n' \
        "$(pinned_ge_state_value "$STATE_FILE" \
          ge_patch_overlap_audit_sha256)"
    fi
    printf 'Package release: 0 (local game-test artifact)\n'
    printf 'Project GStreamer payload: none\n'
  } >"$provenance"
  chmod 0644 "$provenance"
}

normalize_redist_permissions() {
  local archive_tmp

  [[ -d "$TOOL_DIR" && ! -L "$TOOL_DIR" ]] \
    || pinned_ge_fail "redist tool directory is absent or unsafe: $TOOL_DIR"

  # Xalia's upstream ZIP currently carries 0666 on several managed payloads.
  # Preserve owner and executable bits, but never distribute group/other-write
  # access. Apply the same invariant to the complete tool so future payloads
  # cannot silently reintroduce a world-writable file or directory.
  find "$TOOL_DIR" \( -type f -o -type d \) -perm /0022 \
    -exec chmod go-w -- {} +

  archive_tmp="$(mktemp "$BUILD_DIR/.${PIN_BUILD_NAME}.tar.gz.XXXXXX")"
  if ! tar --sort=name --owner=0 --group=0 --numeric-owner \
      -C "$BUILD_DIR" -czf "$archive_tmp" "$PIN_BUILD_NAME"; then
    rm -f -- "$archive_tmp"
    pinned_ge_fail "could not rebuild the permission-normalized redist archive"
  fi
  mv -f -- "$archive_tmp" "$ARCHIVE"
  (
    cd "$BUILD_DIR"
    sha512sum "$(basename "$ARCHIVE")" >"$(basename "$SHA512")"
    sha256sum "$(basename "$SHA512")" "$(basename "$ARCHIVE")" \
      >"$(basename "$SHA256")"
  )
}

printf 'Pinned GE source: %s\n' "$SOURCE_DIR"
printf 'Out-of-tree build: %s\n' "$BUILD_DIR"
printf 'Build name: %s\n' "$PIN_BUILD_NAME"
printf 'Global job budget: %s\n' "$JOBS"
printf 'Orchestrator Make jobs: %s\n' "$MAKE_JOBS"
printf 'Per-component Ninja jobs: %s\n' "$NINJA_JOBS"
printf 'Nested Make x Ninja ceiling: %s\n' "$SCHEDULE_PRODUCT"
printf 'Build mode: %s\n' \
  "$([[ "$RESUME" == 1 ]] && printf resume || printf fresh)"
printf 'Recursive Make scheduling: GNU Make jobserver\n'
printf 'GE Wine diff-check baseline: status %s, SHA-256 %s\n' \
  "$ge_diff_check_status_value" "$ge_diff_check_sha256"
printf 'GE media-cleanup manifest: SHA-256 %s\n' \
  "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256"
printf 'SteamRT image ID: %s (local, pull disabled)\n' "$PIN_STEAMRT_IMAGE_ID"
printf 'WineGStreamer external references: 0\n'
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  printf 'FFmpeg security records: 3 upstream MagicYUV CVE fixes + 1 project TLS policy (%s)\n' \
    "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
fi
printf 'Scheduling: nice %s%s\n' "$NICE_LEVEL" \
  "$(command -v ionice >/dev/null 2>&1 && printf ', idle I/O' || true)"
printf 'PACKAGE_RELEASE: 0 (game-test only)\n'
printf 'Log: %s\n' "$LOG_FILE"
printf '\n=== %s pin=%s mode=%s global_jobs=%s make_jobs=%s ninja_jobs=%s target=redist ===\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PIN_ID" \
  "$([[ "$RESUME" == 1 ]] && printf resume || printf fresh)" "$JOBS" \
  "$MAKE_JOBS" "$NINJA_JOBS" >>"$LOG_FILE"

if [[ "$STREAM_LOG" == 1 ]]; then
  run_build 2>&1 | tee -a "$LOG_FILE"
else
  run_build >>"$LOG_FILE" 2>&1
fi

normalize_proton_version_metadata
write_artifact_provenance
normalize_redist_permissions

bash "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  --source "$SOURCE_DIR" --build-dir "$BUILD_DIR" \
  --state-dir "$STATE_DIR" --config "$PINNED_GE_CONFIG_PATH" \
  --series "$PATCH_SERIES"

printf 'Verified game-test directory: %s\n' "$TOOL_DIR"
printf 'Redistributable archive:        %s\n' "$ARCHIVE"
printf 'No Steam installation or publication occurred.\n'
