#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
ALLOW_FETCH=0
REFERENCE_SOURCES=()
PREEXISTING_PATHS=()
LOCAL_REFERENCE_PATHS=()
REMOTE_FETCH_PATHS=()

usage() {
  cat <<'EOF'
Usage: materialize-pinned-ge-submodules.sh [OPTIONS]

Verify the exact submodule pins for a pinned GE checkout. With --fetch, sync
and initialize the build-required paths selected from that checkout's
.gitmodules, recursively and with one fetch job. The disabled Nvidia-library
and Vulkan-layer groups are deliberately excluded.

Options:
  --source DIR             pinned GE checkout
  --config FILE            pinned config (default: active exact snapshot)
  --fetch                  explicitly permit serial submodule retrieval
  --reference-source DIR   copy matching submodule objects from an existing
                           checkout without retaining a dependency; repeat
                           to supply ordered fallback object sources
  -h, --help               show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PINNED_GE_CONFIG="$2"
      shift 2
      ;;
    --fetch)
      ALLOW_FETCH=1
      shift
      ;;
    --reference-source)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      REFERENCE_SOURCES+=("$2")
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
SOURCE_DIR="$(realpath -m -- "$SOURCE_DIR")"
if [[ "${#REFERENCE_SOURCES[@]}" -gt 0 ]]; then
  [[ "$ALLOW_FETCH" == 1 ]] \
    || pinned_ge_fail "--reference-source requires --fetch"
  declare -A SEEN_REFERENCE_SOURCES=()
  for index in "${!REFERENCE_SOURCES[@]}"; do
    reference_source="${REFERENCE_SOURCES[$index]}"
    [[ ! -L "$reference_source" ]] \
      || pinned_ge_fail "reference source must not be a symlink"
    reference_source="$(realpath -e -- "$reference_source")" \
      || pinned_ge_fail "could not resolve reference source: $reference_source"
    [[ "$reference_source" != "$SOURCE_DIR" ]] \
      || pinned_ge_fail "reference source and destination must differ"
    [[ -e "$reference_source/.git" \
        && "$(pinned_ge_git -C "$reference_source" \
          rev-parse --is-inside-work-tree 2>/dev/null)" == true ]] \
      || pinned_ge_fail \
        "reference source is not a local Git checkout: $reference_source"
    [[ -z "${SEEN_REFERENCE_SOURCES[$reference_source]+present}" ]] \
      || pinned_ge_fail "duplicate reference source: $reference_source"
    SEEN_REFERENCE_SOURCES["$reference_source"]=1
    REFERENCE_SOURCES[$index]="$reference_source"
  done
fi
pinned_ge_require_source_identity "$SOURCE_DIR"
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || pinned_ge_fail "refusing to materialize submodules in a dirty checkout"

mapfile -t REGISTERED < <(pinned_ge_registered_submodules "$SOURCE_DIR")
[[ "${#REGISTERED[@]}" == "$PIN_REGISTERED_SUBMODULE_COUNT" ]] \
  || pinned_ge_fail "registered submodule list changed after identity validation"

materialize_reference_children() {
  local destination_parent="$1"
  local display_parent="$2"
  local root_level="$3"
  local record key name path display_path expected gitlink_record
  local reference_child destination_child resolved_url child_git_dir
  local -a records=()

  [[ -f "$destination_parent/.gitmodules" ]] || return 0
  mapfile -d '' -t records < <(
    pinned_ge_git -C "$destination_parent" config -z -f .gitmodules \
      --get-regexp '^submodule\..*\.path$'
  )
  for record in "${records[@]}"; do
    [[ "$record" == *$'\n'* ]] \
      || pinned_ge_fail "malformed submodule path record"
    key="${record%%$'\n'*}"
    path="${record#*$'\n'}"
    name="${key#submodule.}"
    name="${name%.path}"
    if [[ "$root_level" == 1 ]]; then
      case "$path" in
        nvidia-libs/*|vklayers/*) continue ;;
      esac
    fi
    case "$path" in
      ""|/*|..|../*|*/../*|*/..) pinned_ge_fail "unsafe submodule path: $path" ;;
    esac

    display_path="${display_parent:+$display_parent/}$path"
    gitlink_record="$(pinned_ge_git -C "$destination_parent" ls-tree HEAD -- "$path")"
    expected="$(awk '$1 == "160000" && $2 == "commit" {print $3}' \
      <<<"$gitlink_record")"
    [[ -n "$expected" ]] \
      || pinned_ge_fail "missing pinned gitlink for $display_path"

    destination_child="$destination_parent/$path"
    pinned_ge_git -C "$destination_parent" submodule sync -- "$path" >/dev/null
    pinned_ge_git -C "$destination_parent" submodule init -- "$path" >/dev/null
    resolved_url="$(pinned_ge_git -C "$destination_parent" \
      config --get "submodule.$name.url")"
    [[ -n "$resolved_url" ]] \
      || pinned_ge_fail "could not resolve declared remote for $display_path"

    if [[ -e "$destination_child/.git" ]]; then
      pinned_ge_require_existing_submodule \
        "$destination_child" "$expected" "$resolved_url" \
        || pinned_ge_fail \
          "pre-existing submodule validation failed: $display_path"
      PREEXISTING_PATHS+=("$display_path")
    elif reference_child="$(
      pinned_ge_find_complete_reference_repo \
        "$expected" "$display_path" "${REFERENCE_SOURCES[@]}"
    )"; then
      if [[ -e "$destination_child" || -L "$destination_child" ]]; then
        [[ -d "$destination_child" && ! -L "$destination_child" \
            && -z "$(find "$destination_child" -mindepth 1 -print -quit)" ]] \
          || pinned_ge_fail \
            "reference-backed submodule destination is not empty: $display_path"
      fi
      mkdir -p "$(dirname "$destination_child")"
      env GIT_ALLOW_PROTOCOL=file git \
        -c protocol.file.allow=always \
        -c maintenance.auto=false \
        -c maintenance.autoDetach=false \
        -c gc.auto=0 \
        -c gc.autoDetach=false \
        clone --local --no-hardlinks --no-checkout \
          "$reference_child" "$destination_child"
      pinned_ge_git -C "$destination_child" remote set-url origin "$resolved_url"
      GIT_NO_LAZY_FETCH=1 pinned_ge_git -C "$destination_child" \
        cat-file -e "$expected^{commit}" \
        || pinned_ge_fail "$display_path did not copy its pinned commit"
      GIT_NO_LAZY_FETCH=1 pinned_ge_git -C "$destination_child" \
        rev-list --objects --missing=error "$expected" >/dev/null \
        || pinned_ge_fail "$display_path copied an incomplete object graph"
      pinned_ge_git -C "$destination_child" checkout --detach "$expected" >/dev/null
      LOCAL_REFERENCE_PATHS+=("$display_path")
    else
      if [[ -e "$destination_child" || -L "$destination_child" ]]; then
        [[ -d "$destination_child" && ! -L "$destination_child" \
            && -z "$(find "$destination_child" -mindepth 1 -print -quit)" ]] \
          || pinned_ge_fail \
            "remote-backed submodule destination is not empty: $display_path"
      fi
      git \
        -c maintenance.auto=false \
        -c maintenance.autoDetach=false \
        -c gc.auto=0 \
        -c gc.autoDetach=false \
        -c fetch.parallel=1 \
        -c submodule.fetchJobs=1 \
        -C "$destination_parent" submodule update --init --checkout --jobs 1 \
          -- "$path"
      REMOTE_FETCH_PATHS+=("$display_path")
    fi

    [[ "$(pinned_ge_git -C "$destination_child" rev-parse HEAD)" == "$expected" ]] \
      || pinned_ge_fail "$display_path did not land on its pinned gitlink"
    [[ "$(pinned_ge_git -C "$destination_child" remote get-url origin)" \
        == "$resolved_url" ]] \
      || pinned_ge_fail "$display_path retained a non-declared origin"
    [[ "$(pinned_ge_git -C "$destination_parent" \
        config --get "submodule.$name.url")" == "$resolved_url" ]] \
      || pinned_ge_fail "$display_path changed its parent's declared URL"
    child_git_dir="$(pinned_ge_git -C "$destination_child" \
      rev-parse --absolute-git-dir)"
    for alternates_name in alternates http-alternates; do
      [[ ! -e "$child_git_dir/objects/info/$alternates_name" ]] \
        || pinned_ge_fail \
          "$display_path retained $alternates_name after dissociation"
    done
    materialize_reference_children \
      "$destination_child" "$display_path" 0
  done
}

if [[ "$ALLOW_FETCH" == 1 ]]; then
  git \
    -c maintenance.auto=false \
    -c maintenance.autoDetach=false \
    -c gc.auto=0 \
    -c gc.autoDetach=false \
    -c fetch.parallel=1 \
    -c submodule.fetchJobs=1 \
    -C "$SOURCE_DIR" submodule sync --recursive -- "${REGISTERED[@]}"
  if [[ "${#REFERENCE_SOURCES[@]}" -gt 0 ]]; then
    materialize_reference_children "$SOURCE_DIR" "" 1
  else
    git \
      -c maintenance.auto=false \
      -c maintenance.autoDetach=false \
      -c gc.auto=0 \
      -c gc.autoDetach=false \
      -c fetch.parallel=1 \
      -c submodule.fetchJobs=1 \
      -C "$SOURCE_DIR" submodule update --init --recursive --jobs 1 -- "${REGISTERED[@]}"
  fi
fi

pinned_ge_require_registered_submodules "$SOURCE_DIR"
GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR/wine" \
  cat-file -e 'e813ca5771658b00875924ab88d525322e50d39f^' \
  || pinned_ge_fail "Wine history required by GE's revert is unavailable; do not use a shallow clone"

mapfile -t STATUS_LINES < <(
  GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
    submodule status --recursive -- "${REGISTERED[@]}"
)
for line in "${STATUS_LINES[@]}"; do
  case "$line" in
    [-+U]*) pinned_ge_fail "submodule does not match its recorded pin: $line" ;;
  esac
done

printf 'Verified %s registered GE submodules at exact gitlinks.\n' "${#REGISTERED[@]}"
if [[ "${#REFERENCE_SOURCES[@]}" -gt 0 ]]; then
  printf 'Validated local reference checkouts: %s\n' \
    "${#REFERENCE_SOURCES[@]}"
  mapfile -t MATERIALIZED_GIT_DIRS < <(
    {
      pinned_ge_git -C "$SOURCE_DIR" rev-parse --absolute-git-dir
      pinned_ge_git -C "$SOURCE_DIR" submodule foreach --quiet --recursive \
        'git rev-parse --absolute-git-dir'
    } | LC_ALL=C sort -u
  )
  ALTERNATES=()
  for child_git_dir in "${MATERIALIZED_GIT_DIRS[@]}"; do
    for alternates_name in alternates http-alternates; do
      if [[ -e "$child_git_dir/objects/info/$alternates_name" ]]; then
        ALTERNATES+=("$child_git_dir/objects/info/$alternates_name")
      fi
    done
  done
  if [[ "${#ALTERNATES[@]}" -gt 0 ]]; then
    printf 'Surviving Git alternates files:\n' >&2
    printf '  %s\n' "${ALTERNATES[@]}" >&2
    pinned_ge_fail "materialized submodules retain a reference-source dependency"
  fi
  printf 'Exact pre-existing submodules (%s):\n' "${#PREEXISTING_PATHS[@]}"
  if [[ "${#PREEXISTING_PATHS[@]}" -gt 0 ]]; then
    printf '  %s\n' "${PREEXISTING_PATHS[@]}"
  fi
  printf 'Local reference object sources (%s):\n' "${#LOCAL_REFERENCE_PATHS[@]}"
  if [[ "${#LOCAL_REFERENCE_PATHS[@]}" -gt 0 ]]; then
    printf '  %s\n' "${LOCAL_REFERENCE_PATHS[@]}"
  fi
  printf 'Declared-remote fallbacks (%s):\n' "${#REMOTE_FETCH_PATHS[@]}"
  if [[ "${#REMOTE_FETCH_PATHS[@]}" -gt 0 ]]; then
    printf '  %s\n' "${REMOTE_FETCH_PATHS[@]}"
  fi
  printf 'Surviving Git alternates files: 0\n'
fi
printf 'Excluded optional Nvidia/Vulkan-layer gitlinks: %s\n' \
  "$PIN_UNMAPPED_OPTIONAL_GITLINK_COUNT"
printf 'Build requirements: WITHOUT_NVIDIA_LIBS=%s WITHOUT_VKLAYERS=%s\n' \
  "$PIN_WITHOUT_NVIDIA_LIBS" "$PIN_WITHOUT_VKLAYERS"
if [[ "$ALLOW_FETCH" == 0 ]]; then
  printf 'Offline verification only; no submodule retrieval was attempted.\n'
fi
