#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: build-se1.sh --work-dir NEW_DIRECTORY [--seed LOCAL_GE_REPO]' \
    '                    [--contrib-cache DIRECTORY] [--sdk-cache DIRECTORY]' \
    '                    [--jobs 1..16] [--dry-run]' \
    '' \
    'Fetch/verify pinned inputs, prepare source, then build offline under Podman.' \
    'Use nix run .#build-from-source to supply host tools automatically.' \
    'The work directory must not exist. Nothing is installed into Steam.'
}

work_dir='' seed='' contrib_cache='' sdk_cache='' jobs=16 dry_run=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-dir|--seed|--contrib-cache|--sdk-cache|--jobs)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      case "$1" in
        --work-dir) work_dir="$2" ;;
        --seed) seed="$2" ;;
        --contrib-cache) contrib_cache="$2" ;;
        --sdk-cache) sdk_cache="$2" ;;
        --jobs) jobs="$2" ;;
      esac
      shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ -n "$work_dir" ]] || { usage >&2; exit 2; }
[[ "$jobs" =~ ^([1-9]|1[0-6])$ ]] || { printf 'Jobs must be 1..16.\n' >&2; exit 2; }
[[ ! -e "$work_dir" && ! -L "$work_dir" ]] || {
  printf 'Use a fresh, absent work directory; existing work is never overwritten.\n' >&2
  exit 1
}
work_dir="$(realpath -m -- "$work_dir")"
case "$work_dir" in *[[:space:]]*) printf 'Build paths cannot contain whitespace.\n' >&2; exit 2 ;; esac
kit="${SE1_PROJECT_KIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
kit="$(realpath -e -- "$kit")"
case "$work_dir/" in "$kit/"*)
  printf 'The work directory must be outside the source kit (avoid recursive copying).\n' >&2
  exit 2 ;;
esac
if [[ -n "$seed" ]]; then seed="$(realpath -e -- "$seed")"; fi
if [[ -n "$contrib_cache" ]]; then contrib_cache="$(realpath -m -- "$contrib_cache")"; fi
if [[ -n "$sdk_cache" ]]; then sdk_cache="$(realpath -m -- "$sdk_cache")"; fi
if [[ "$dry_run" == 1 ]]; then
  printf 'Fresh work: %s\nJobs: %s\n' "$work_dir" "$jobs"
  printf '%s\n' 'Copy source kit; verify pin; fetch or seed GE and submodules;' \
    'prepare patches; prefetch 14 hashed inputs; verify SDK image and static gates;' \
    'build redist with network=none and shared lock; verify artifact; do not install.'
  exit 0
fi

mkdir -p -- "$(dirname "$work_dir")"
mkdir -- "$work_dir"
project="$work_dir/project"
mkdir -- "$project"
# A Nix store input lacks Git administration. Make a fresh local index for
# source/preflight checks; never alter the caller's repository or history.
tar -C "$kit" --exclude='./.git' --exclude='./result*' -cf - . | tar -C "$project" -xf -
chmod -R u+w "$project"
git -C "$project" init -q
git -C "$project" add .
cd "$project"
source scripts/pinned-ge-common.sh
PINNED_GE_CONFIG="$project/config/ge-proton11-6-se1.env"
pinned_ge_load_config
series="$project/patches/series-ge-proton11-6-se1"
source_dir="$work_dir/source"
state_dir="$work_dir/state"
build_dir="$work_dir/build"
use_release_inputs=0
if [[ -z "$contrib_cache" ]]; then
  contrib_cache="$work_dir/contrib-cache"
  use_release_inputs=1
fi
sdk_cache="${sdk_cache:-$work_dir/sdk-cache}"
export GIT_NO_LAZY_FETCH=1

if [[ -z "$seed" ]]; then
  seed="$work_dir/upstream-seed"
  git -c maintenance.auto=false clone --depth 1 --single-branch \
    --branch "${PIN_SOURCE_REF#refs/tags/}" "$PIN_SOURCE_URL" "$seed"
fi
scripts/bootstrap-pinned-ge.sh --config "$PINNED_GE_CONFIG" \
  --source "$source_dir" --seed "$seed"
scripts/materialize-pinned-ge-submodules.sh --config "$PINNED_GE_CONFIG" \
  --source "$source_dir" --reference-source "$seed" --fetch
scripts/prepare-pinned-ge-source.sh --config "$PINNED_GE_CONFIG" \
  --source "$source_dir" --state-dir "$state_dir" --series "$series"
if [[ "$use_release_inputs" == 1 ]]; then
  # Preserve the observed branch-derived Piper archive as well as tagged inputs.
  # The ordinary prefetcher below still checks every individual manifest entry.
  bundle="$work_dir/se1-build-inputs.tar.zst"
  curl --fail --location --proto '=https' --proto-redir '=https' \
    --output "$bundle" \
    'https://github.com/Go08er/proton-ge-real-time-streaming-protocol-slop-edition/releases/download/se1/proton-ge-11-6-rtsp-se1-build-inputs.tar.zst'
  printf '%s  %s\n' \
    36a76f1dfd8baf250056dfdff71295f68fb5ebf91786730d5b6420b283733033 \
    "$bundle" | sha256sum --check --status
  mkdir -- "$contrib_cache"
  tar --zstd -xf "$bundle" -C "$contrib_cache"
fi
scripts/prefetch-pinned-ge-contrib.sh --config "$PINNED_GE_CONFIG" \
  --source "$source_dir" --cache-dir "$contrib_cache"

# Match the build wrapper's isolated XDG storage, without changing host
# container settings. The pinned image is fetched only in this input phase.
mkdir -p "$sdk_cache/.config" "$sdk_cache/.local/share" "$sdk_cache/.cache"
sdk_env=(env "XDG_CONFIG_HOME=$sdk_cache/.config"
  "XDG_DATA_HOME=$sdk_cache/.local/share" "XDG_CACHE_HOME=$sdk_cache/.cache")
if ! "${sdk_env[@]}" podman image exists "$PIN_STEAMRT_IMAGE"; then
  "${sdk_env[@]}" podman pull "$PIN_STEAMRT_IMAGE"
fi
actual_image="$("${sdk_env[@]}" podman image inspect --format '{{.Id}}' "$PIN_STEAMRT_IMAGE")"
[[ "${actual_image#sha256:}" == "$PIN_STEAMRT_IMAGE_ID" ]] || {
  printf 'SDK image identity differs from the pin; refusing the build.\n' >&2
  exit 1
}
PREPARED_SOURCE="$source_dir" scripts/verify-preparation.sh \
  --config "$PINNED_GE_CONFIG" --series "$series"
scripts/build-pinned-ge.sh --config "$PINNED_GE_CONFIG" --series "$series" \
  --source "$source_dir" --state-dir "$state_dir" --build-dir "$build_dir" \
  --cache-home "$sdk_cache" --jobs "$jobs"
printf 'Verified local build: %s/%s.tar.gz\nNothing installed into Steam.\n' \
  "$build_dir" "$PIN_BUILD_NAME"
