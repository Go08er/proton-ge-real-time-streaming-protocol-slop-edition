#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause

PINNED_GE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINNED_GE_DEFAULT_CONFIG="$PINNED_GE_ROOT/config/ge-master-snapshot-20260723-a312.env"

pinned_ge_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

pinned_ge_launcher_patch_record() {
  if [[ "$PIN_PROTON_LAUNCHER_POLICY" == upstream ]]; then
    printf 'none\n'
  else
    printf 'patches/proton/0001-launcher-add-scoped-xrizer-mode.patch\n'
  fi
}

pinned_ge_launcher_patch_sha256() {
  if [[ "$PIN_PROTON_LAUNCHER_POLICY" == upstream ]]; then
    # Digest of the empty patch set, not the digest of the launcher itself.
    printf '' | sha256sum | cut -d ' ' -f 1
  else
    pinned_ge_sha256 "$PINNED_GE_ROOT/$(pinned_ge_launcher_patch_record)"
  fi
}

pinned_ge_require_upstream_launcher() {
  local source="$1" launcher="$2" expected actual
  expected="$(pinned_ge_git -C "$source" rev-parse "$PIN_SOURCE_COMMIT:proton")" || return 1
  actual="$(pinned_ge_git -C "$source" hash-object -- "$launcher")" || return 1
  [[ "$actual" == "$expected" ]] \
    || pinned_ge_fail "launcher differs from the pinned upstream Proton launcher"
}

pinned_ge_load_config() {
  local config="${PINNED_GE_CONFIG:-$PINNED_GE_DEFAULT_CONFIG}"
  local name line key value retained_resume_count=0 patch_overlap_count=0
  local -a schema_common_keys=(
    PIN_SCHEMA_VERSION PIN_KIND PIN_ID PIN_SOURCE_URL PIN_SOURCE_REF
    PIN_SOURCE_COMMIT PIN_SOURCE_TREE PIN_SOURCE_VERSION
    PIN_GITMODULES_BLOB PIN_GITMODULES_SHA256
    PIN_PATCH_DRIVER_BLOB PIN_PATCH_DRIVER_SHA256
    PIN_MAKEFILE_IN_BLOB PIN_MAKEFILE_IN_SHA256
    PIN_CONFIGURE_BLOB PIN_CONFIGURE_SHA256
    PIN_RULES_MESON_BLOB PIN_RULES_MESON_SHA256
    PIN_RULES_MESON_NORMALIZED_SHA256
    PIN_REGISTERED_SUBMODULE_COUNT PIN_GITLINK_COUNT
    PIN_UNMAPPED_OPTIONAL_GITLINK_COUNT PIN_WINE_COMMIT
    PIN_WINE_STAGING_COMMIT PIN_FFMPEG_COMMIT
    PIN_GE_MEDIA_CLEANUP_MANIFEST PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256
    PIN_BUILD_CONTRIB_MANIFEST PIN_BUILD_CONTRIB_MANIFEST_SHA256
    PIN_STEAMRT_IMAGE PIN_STEAMRT_IMAGE_ID
    PIN_BUILD_NAME PIN_PACKAGE_RELEASE PIN_DEFAULT_JOBS
    PIN_DEFAULT_MAKE_JOBS PIN_DEFAULT_NINJA_JOBS
    PIN_WITHOUT_NVIDIA_LIBS PIN_WITHOUT_VKLAYERS
  )
  local -a schema5_keys=(
    PIN_FFMPEG_SECURITY_FIX_COMMITS
    PIN_FFMPEG_SECURITY_PATCH_SHA256S
    PIN_FFMPEG_SECURITY_SERIES_SHA256
    PIN_FFMPEG_SECURITY_MAGICYUV_BLOB
    PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB
    PIN_FFMPEG_CRYPTO_BUILD_ORIGIN
    PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256
    PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256
    PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB
  )
  local -a retained_resume_keys=(
    PIN_RETAINED_RESUME_SOURCE PIN_RETAINED_RESUME_BUILD
    PIN_RETAINED_RESUME_STATE PIN_RETAINED_RESUME_CACHE
    PIN_RETAINED_RESUME_LOG PIN_RETAINED_RESUME_INVOCATION_SHA256
    PIN_RETAINED_RESUME_HOST_SHELL PIN_RETAINED_RESUME_ANCHOR_MANIFEST
    PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256
  )
  local -a patch_overlap_keys=(
    PIN_GE_PATCH_OVERLAP_MANIFEST
    PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256
  )
  local -a optional_keys=(PIN_PROTON_LAUNCHER_POLICY)
  local -A seen=() allowed=()

  # The loader may be called repeatedly in one shell, and callers may export
  # names which happen to match config fields. Clear the complete supported
  # namespace before reading so only records physically present in this file
  # can satisfy the schema.
  unset PINNED_GE_CONFIG_PATH PINNED_GE_HAS_RETAINED_RESUME \
    PINNED_GE_HAS_PATCH_OVERLAP
  for name in "${schema_common_keys[@]}" "${schema5_keys[@]}" \
      "${retained_resume_keys[@]}" "${patch_overlap_keys[@]}" "${optional_keys[@]}"; do
    allowed["$name"]=1
    unset "$name" \
      || pinned_ge_fail "could not clear inherited pinned config value: $name" \
      || return 1
  done
  [[ -f "$config" && ! -L "$config" ]] \
    || pinned_ge_fail "pinned GE config is not a regular file: $config" \
    || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^(PIN_[A-Z0-9_]+)=([^[:space:]]+)$ ]] \
      || pinned_ge_fail "invalid non-data line in pinned config: $line" \
      || return 1
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ -n "${allowed[$key]+present}" ]] \
      || pinned_ge_fail "unknown pinned config key: $key" || return 1
    [[ -z "${seen[$key]+present}" ]] \
      || pinned_ge_fail "duplicate pinned config key: $key" || return 1
    seen["$key"]=1
    printf -v "$key" '%s' "$value"
  done <"$config"
  PINNED_GE_CONFIG_PATH="$(cd "$(dirname "$config")" && pwd -P)/$(basename "$config")"

  # Historical pins retain their launcher patch. New pins can explicitly
  # select the unmodified upstream launcher without changing old identities.
  PIN_PROTON_LAUNCHER_POLICY="${PIN_PROTON_LAUNCHER_POLICY:-scoped-xrizer}"
  case "$PIN_PROTON_LAUNCHER_POLICY" in
    scoped-xrizer|upstream) ;;
    *) pinned_ge_fail "unsupported Proton launcher policy" || return 1 ;;
  esac

  for name in "${schema_common_keys[@]}"; do
    [[ -n "${seen[$name]+present}" ]] \
      || pinned_ge_fail "config key is absent from current file: $name" || return 1
    [[ -n "${!name}" ]] \
      || pinned_ge_fail "config value is empty: $name" || return 1
  done
  for name in "${retained_resume_keys[@]}"; do
    [[ -z "${seen[$name]+present}" ]] || retained_resume_count=$((retained_resume_count + 1))
  done
  case "$retained_resume_count" in
    0) PINNED_GE_HAS_RETAINED_RESUME=0 ;;
    9)
      PINNED_GE_HAS_RETAINED_RESUME=1
      for name in "${retained_resume_keys[@]}"; do
        [[ -n "${!name}" ]] \
          || pinned_ge_fail "retained-resume config value is empty: $name" \
          || return 1
      done
      ;;
    *)
      pinned_ge_fail \
        "retained-resume fields must be either all absent or all present" \
        || return 1
      ;;
  esac
  for name in "${patch_overlap_keys[@]}"; do
    [[ -z "${seen[$name]+present}" ]] \
      || patch_overlap_count=$((patch_overlap_count + 1))
  done
  case "$patch_overlap_count" in
    0) PINNED_GE_HAS_PATCH_OVERLAP=0 ;;
    2)
      PINNED_GE_HAS_PATCH_OVERLAP=1
      for name in "${patch_overlap_keys[@]}"; do
        [[ -n "${!name}" ]] \
          || pinned_ge_fail "patch-overlap config value is empty: $name" \
          || return 1
      done
      ;;
    *)
      pinned_ge_fail \
        "patch-overlap fields must be either both absent or both present" \
        || return 1
      ;;
  esac

  case "$PIN_SCHEMA_VERSION" in
    4)
      for name in "${schema5_keys[@]}"; do
        [[ -z "${seen[$name]+present}" ]] \
          || pinned_ge_fail "schema-5 key is forbidden in schema 4: $name" \
          || return 1
      done
      ;;
    5)
      for name in "${schema5_keys[@]}"; do
        [[ -n "${seen[$name]+present}" ]] \
          || pinned_ge_fail "schema-5 key is absent from current file: $name" \
          || return 1
        [[ -n "${!name}" ]] \
          || pinned_ge_fail "schema-5 config value is empty: $name" || return 1
      done
      ;;
    *) pinned_ge_fail "unsupported pin schema: $PIN_SCHEMA_VERSION" || return 1 ;;
  esac
  [[ "$PIN_KIND" == development-snapshot || "$PIN_KIND" == release-tag ]] \
    || pinned_ge_fail "unsupported pin kind: $PIN_KIND" || return 1
  [[ "$PIN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ \
      && "$PIN_BUILD_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] \
    || pinned_ge_fail "pin ID or build name is not path-safe" || return 1
  [[ "$PIN_SOURCE_URL" == https://* ]] \
    || pinned_ge_fail "source URL is malformed" || return 1
  [[ "$PIN_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ && "$PIN_SOURCE_TREE" =~ ^[0-9a-f]{40}$ ]] \
    || pinned_ge_fail "source commit/tree is malformed" || return 1
  case "$PIN_KIND" in
    development-snapshot)
      [[ "$PIN_SOURCE_REF" =~ ^[0-9a-f]{40}$ \
          && "$PIN_SOURCE_REF" == "$PIN_SOURCE_COMMIT" ]] \
        || pinned_ge_fail \
          "development snapshot ref must be its exact pinned commit" \
        || return 1
      ;;
    release-tag)
      [[ "$PIN_SOURCE_REF" == refs/tags/* ]] \
        || pinned_ge_fail "release source ref must be a full tag ref" || return 1
      ;;
  esac
  for name in PIN_GITMODULES_BLOB PIN_PATCH_DRIVER_BLOB PIN_MAKEFILE_IN_BLOB \
      PIN_CONFIGURE_BLOB PIN_RULES_MESON_BLOB PIN_WINE_COMMIT \
      PIN_WINE_STAGING_COMMIT PIN_FFMPEG_COMMIT; do
    [[ "${!name}" =~ ^[0-9a-f]{40}$ ]] \
      || pinned_ge_fail "$name is not a full object ID" || return 1
  done
  for name in PIN_GITMODULES_SHA256 PIN_PATCH_DRIVER_SHA256 \
      PIN_MAKEFILE_IN_SHA256 PIN_CONFIGURE_SHA256 PIN_RULES_MESON_SHA256 \
      PIN_RULES_MESON_NORMALIZED_SHA256 PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256 \
      PIN_BUILD_CONTRIB_MANIFEST_SHA256; do
    [[ "${!name}" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "$name is not a SHA-256 digest" || return 1
  done
  if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
    [[ "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "patch-overlap manifest digest is malformed" \
      || return 1
  fi
  [[ "$PIN_STEAMRT_IMAGE_ID" =~ ^[0-9a-f]{64}$ ]] \
    || pinned_ge_fail "SteamRT image ID is malformed" || return 1
  if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
    [[ "$PIN_FFMPEG_SECURITY_SERIES_SHA256" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "PIN_FFMPEG_SECURITY_SERIES_SHA256 is malformed" \
      || return 1
    [[ "$PIN_FFMPEG_SECURITY_MAGICYUV_BLOB" =~ ^[0-9a-f]{40}$ ]] \
      || pinned_ge_fail "PIN_FFMPEG_SECURITY_MAGICYUV_BLOB is malformed" \
      || return 1
    [[ "$PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB" =~ ^[0-9a-f]{40}$ ]] \
      || pinned_ge_fail "PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB is malformed" \
      || return 1
    [[ "$PIN_FFMPEG_CRYPTO_BUILD_ORIGIN" =~ ^[0-9a-f]{40}$ \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" =~ ^[0-9a-f]{40}$ ]] \
      || pinned_ge_fail "FFmpeg crypto-build origin or Makefile blob is malformed" \
      || return 1
    [[ "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256" =~ ^[0-9a-f]{64}$ \
        && "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "FFmpeg crypto-build patch or series digest is malformed" \
      || return 1
    pinned_ge_ffmpeg_security_records >/dev/null || return 1
  fi
  [[ "$PIN_PACKAGE_RELEASE" =~ ^[0-9]+$ && "$PIN_DEFAULT_JOBS" =~ ^[1-9][0-9]*$ \
      && "$PIN_DEFAULT_MAKE_JOBS" =~ ^[1-9][0-9]*$ \
      && "$PIN_DEFAULT_NINJA_JOBS" =~ ^[1-9][0-9]*$ ]] \
    || pinned_ge_fail "package-release or job-count config is malformed" || return 1
  case "$PIN_GE_MEDIA_CLEANUP_MANIFEST" in
    /*|../*|*/../*|*/..|..)
      pinned_ge_fail "GE media-cleanup manifest path is unsafe" || return 1 ;;
  esac
  case "$PIN_BUILD_CONTRIB_MANIFEST" in
    /*|../*|*/../*|*/..|..)
      pinned_ge_fail "build-contrib manifest path is unsafe" || return 1 ;;
  esac
  if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
    case "$PIN_GE_PATCH_OVERLAP_MANIFEST" in
      /*|../*|*/../*|*/..|..)
        pinned_ge_fail "patch-overlap manifest path is unsafe" || return 1 ;;
    esac
  fi
  if [[ "$PINNED_GE_HAS_RETAINED_RESUME" == 1 ]]; then
    [[ "$PIN_RETAINED_RESUME_INVOCATION_SHA256" =~ ^[0-9a-f]{64}$ \
        && "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "retained-resume digest is malformed" || return 1
    case "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST" in
      /*|../*|*/../*|*/..|..)
        pinned_ge_fail "retained-resume manifest path is unsafe" || return 1 ;;
    esac
    for name in PIN_RETAINED_RESUME_SOURCE PIN_RETAINED_RESUME_STATE \
        PIN_RETAINED_RESUME_LOG; do
      case "${!name}" in
        /*|../*|*/../*|*/..|..|*[$'\n\t']*)
          pinned_ge_fail "$name is not a safe project-relative path" || return 1 ;;
      esac
    done
    [[ "$PIN_RETAINED_RESUME_CACHE" == \
        \~/Documents/proton-rtsp/.podman-home ]] \
      || pinned_ge_fail "retained-resume cache is not the recorded shared cache" \
      || return 1
    PIN_RETAINED_RESUME_CACHE="${HOME:?HOME must be set}/Documents/proton-rtsp/.podman-home"
    pinned_ge_require_safe_absolute_path \
      PIN_RETAINED_RESUME_CACHE "$PIN_RETAINED_RESUME_CACHE" || return 1
    pinned_ge_require_safe_absolute_path \
      PIN_RETAINED_RESUME_BUILD "$PIN_RETAINED_RESUME_BUILD" || return 1
    pinned_ge_require_safe_absolute_path \
      PIN_RETAINED_RESUME_HOST_SHELL "$PIN_RETAINED_RESUME_HOST_SHELL" || return 1
  fi
  pinned_ge_validate_job_schedule "$PIN_DEFAULT_JOBS" \
    "$PIN_DEFAULT_MAKE_JOBS" "$PIN_DEFAULT_NINJA_JOBS" >/dev/null \
    || return 1
  for name in PIN_REGISTERED_SUBMODULE_COUNT PIN_GITLINK_COUNT \
      PIN_UNMAPPED_OPTIONAL_GITLINK_COUNT; do
    [[ "${!name}" =~ ^[0-9]+$ ]] \
      || pinned_ge_fail "$name is not a nonnegative integer" || return 1
  done
  [[ "$PIN_WITHOUT_NVIDIA_LIBS" =~ ^[01]$ && "$PIN_WITHOUT_VKLAYERS" =~ ^[01]$ ]] \
    || pinned_ge_fail "optional-component flags must be 0 or 1" || return 1
}

pinned_ge_require_safe_absolute_path() {
  local label="$1"
  local path="$2"

  [[ "$path" =~ ^/[A-Za-z0-9._/+@=-]+$ ]] \
    || pinned_ge_fail \
      "$label contains characters which are unsafe in GNU Make, shell, or container paths: $path" \
    || return 1
}

pinned_ge_resolve_retained_resume_cache() {
  local path="$PIN_RETAINED_RESUME_CACHE"

  [[ "$path" == /* ]] || path="$PINNED_GE_ROOT/$path"
  realpath -e -- "$path"
}

pinned_ge_require_absent_path() {
  local label="$1"
  local path="$2"

  [[ ! -e "$path" && ! -L "$path" ]] \
    || pinned_ge_fail "$label already exists or is a symlink: $path" \
    || return 1
}

pinned_ge_require_regular_or_absent() {
  local label="$1"
  local path="$2"

  [[ ! -L "$path" && ( ! -e "$path" || -f "$path" ) ]] \
    || pinned_ge_fail "$label is not a regular file or absent: $path" \
    || return 1
}

pinned_ge_require_directory_or_absent() {
  local label="$1"
  local path="$2"

  [[ ! -L "$path" && ( ! -e "$path" || -d "$path" ) ]] \
    || pinned_ge_fail "$label is not a real directory or absent: $path" \
    || return 1
}

pinned_ge_require_local_steamrt_image() {
  local engine="$1"
  local image_id

  engine="$(realpath -e -- "$engine")" \
    || pinned_ge_fail "Podman executable could not be resolved: $engine" \
    || return 1
  pinned_ge_require_safe_absolute_path "Podman executable" "$engine" || return 1
  [[ -x "$engine" && ! -L "$engine" ]] \
    || pinned_ge_fail "Podman executable is absent, unsafe, or not executable: $engine" \
    || return 1
  image_id="$("$engine" image inspect --format '{{.Id}}' \
    "$PIN_STEAMRT_IMAGE" 2>/dev/null)" \
    || pinned_ge_fail \
      "the pinned SteamRT image is not available locally; no pull was attempted" \
    || return 1
  image_id="${image_id#sha256:}"
  [[ "$image_id" =~ ^[0-9a-f]{64}$ ]] \
    || pinned_ge_fail "local SteamRT image returned a malformed image ID" \
    || return 1
  [[ "$image_id" == "$PIN_STEAMRT_IMAGE_ID" ]] \
    || pinned_ge_fail \
      "local SteamRT image ID is $image_id, expected $PIN_STEAMRT_IMAGE_ID" \
    || return 1
  printf '%s\n' "$image_id"
}

pinned_ge_git() {
  GIT_NO_LAZY_FETCH=1 git \
    -c maintenance.auto=false \
    -c maintenance.autoDetach=false \
    -c gc.auto=0 \
    -c gc.autoDetach=false \
    "$@"
}

pinned_ge_repo_has_complete_commit() {
  local repo="$1"
  local expected="$2"
  local require_full_history="${3:-1}"

  [[ "$expected" =~ ^[0-9a-f]{40}$ ]] \
    || pinned_ge_fail "reference object ID is malformed: $expected" || return 1
  [[ "$require_full_history" == 0 || "$require_full_history" == 1 ]] \
    || pinned_ge_fail "reference history policy must be 0 or 1" || return 1
  [[ -d "$repo" && ! -L "$repo" && -e "$repo/.git" ]] || return 1
  if [[ "$require_full_history" == 1 \
      && "$(pinned_ge_git -C "$repo" rev-parse --is-shallow-repository \
        2>/dev/null)" != false ]]; then
    return 1
  fi
  GIT_NO_LAZY_FETCH=1 pinned_ge_git -C "$repo" \
    cat-file -e "$expected^{commit}" 2>/dev/null \
    || return 1
  GIT_NO_LAZY_FETCH=1 pinned_ge_git -C "$repo" \
    rev-list --objects --missing=error "$expected" >/dev/null 2>&1
}

pinned_ge_find_complete_reference_repo() {
  local expected="$1"
  local relative="$2"
  shift 2
  local reference_root candidate resolved

  case "$relative" in
    ""|/*|..|../*|*/../*|*/..)
      pinned_ge_fail "unsafe reference repository path: $relative"
      return 1
      ;;
  esac
  for reference_root in "$@"; do
    candidate="$reference_root/$relative"
    pinned_ge_repo_has_complete_commit "$candidate" "$expected" 1 \
      || continue
    resolved="$(realpath -e -- "$candidate")" || continue
    case "$resolved/" in
      "$reference_root"/*)
        printf '%s\n' "$resolved"
        return 0
        ;;
    esac
  done
  return 1
}

pinned_ge_require_existing_submodule() {
  local repo="$1"
  local expected="$2"
  local resolved_url="$3"
  local actual

  [[ -d "$repo" && ! -L "$repo" && -e "$repo/.git" ]] \
    || pinned_ge_fail "pre-existing submodule is not a real Git directory" \
    || return 1
  actual="$(pinned_ge_git -C "$repo" rev-parse HEAD)" \
    || pinned_ge_fail "could not resolve pre-existing submodule HEAD" \
    || return 1
  [[ "$actual" == "$expected" ]] \
    || pinned_ge_fail \
      "pre-existing submodule is at $actual, expected $expected" || return 1
  pinned_ge_repo_has_complete_commit "$repo" "$expected" 0 \
    || pinned_ge_fail "pre-existing submodule lacks its pinned object graph" \
    || return 1
  [[ "$(pinned_ge_git -C "$repo" remote get-url origin)" == "$resolved_url" ]] \
    || pinned_ge_fail "pre-existing submodule does not use its declared origin" \
    || return 1
}

pinned_ge_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

pinned_ge_require_exact_ffmpeg_define() {
  local file="$1"
  local symbol="$2"
  local expected="$3"

  [[ -f "$file" && ! -L "$file" ]] \
    || pinned_ge_fail "generated FFmpeg config is absent or unsafe: $file" \
    || return 1
  [[ "$symbol" =~ ^CONFIG_[A-Z0-9_]+$ && "$expected" =~ ^[01]$ ]] \
    || pinned_ge_fail "invalid FFmpeg config assertion: $symbol=$expected" \
    || return 1
  awk -v symbol="$symbol" -v expected="$expected" '
    $1 == "#define" && $2 == symbol {
      definitions++
      if (NF == 3 && $3 == expected) matches++
    }
    END { exit !(definitions == 1 && matches == 1) }
  ' "$file" \
    || pinned_ge_fail \
      "$symbol must have exactly one '#define $symbol $expected' in $file" \
    || return 1
}

pinned_ge_require_ffmpeg_config_surface() {
  local schema="$1"
  local config="$2"
  local components="$3"
  local symbol enabled_protocols expected_protocols crypto_value
  local -a required_components=(
    CONFIG_RTSP_DEMUXER CONFIG_HLS_DEMUXER
    CONFIG_HTTP_PROTOCOL CONFIG_HTTPS_PROTOCOL CONFIG_RTP_PROTOCOL
    CONFIG_TCP_PROTOCOL CONFIG_TLS_PROTOCOL CONFIG_UDP_PROTOCOL
  )
  local -a disabled_tls_backends=(
    CONFIG_LIBTLS CONFIG_MBEDTLS CONFIG_OPENSSL CONFIG_SCHANNEL
    CONFIG_SECURETRANSPORT
  )
  local -a disabled_protocols=(
    CONFIG_FILE_PROTOCOL CONFIG_DATA_PROTOCOL CONFIG_HTTPPROXY_PROTOCOL
    CONFIG_CONCAT_PROTOCOL CONFIG_SUBFILE_PROTOCOL
  )

  [[ "$schema" == 4 || "$schema" == 5 ]] \
    || pinned_ge_fail "unsupported FFmpeg config-surface schema: $schema" \
    || return 1
  for symbol in "${required_components[@]}"; do
    pinned_ge_require_exact_ffmpeg_define "$components" "$symbol" 1 \
      || return 1
  done
  pinned_ge_require_exact_ffmpeg_define "$config" CONFIG_GNUTLS 1 \
    || return 1
  pinned_ge_require_exact_ffmpeg_define "$config" CONFIG_LIBZMQ 0 \
    || return 1
  pinned_ge_require_exact_ffmpeg_define "$components" CONFIG_ZMQ_FILTER 0 \
    || return 1
  for symbol in CONFIG_FFMPEG CONFIG_FFPLAY CONFIG_FFPROBE; do
    pinned_ge_require_exact_ffmpeg_define "$config" "$symbol" 0 \
      || return 1
  done
  for symbol in "${disabled_tls_backends[@]}"; do
    pinned_ge_require_exact_ffmpeg_define "$config" "$symbol" 0 \
      || return 1
  done
  for symbol in "${disabled_protocols[@]}"; do
    pinned_ge_require_exact_ffmpeg_define "$components" "$symbol" 0 \
      || return 1
  done
  crypto_value=0
  [[ "$schema" == 4 ]] || crypto_value=1
  pinned_ge_require_exact_ffmpeg_define \
    "$components" CONFIG_CRYPTO_PROTOCOL "$crypto_value" || return 1

  enabled_protocols="$(
    LC_ALL=C awk '
      $1 == "#define" && $2 ~ /^CONFIG_[A-Z0-9_]+_PROTOCOL$/ \
          && NF == 3 && $3 == 1 { print $2 }
    ' "$components" | LC_ALL=C sort
  )"
  expected_protocols="$({
    printf '%s\n' \
      CONFIG_HTTP_PROTOCOL CONFIG_HTTPS_PROTOCOL CONFIG_RTP_PROTOCOL \
      CONFIG_TCP_PROTOCOL CONFIG_TLS_PROTOCOL CONFIG_UDP_PROTOCOL
    if [[ "$schema" == 5 ]]; then
      printf '%s\n' CONFIG_CRYPTO_PROTOCOL
    fi
  } | LC_ALL=C sort)"
  [[ "$enabled_protocols" == "$expected_protocols" ]] || {
    printf 'Enabled FFmpeg protocols:\n%s\n' "$enabled_protocols" >&2
    printf 'Expected exact protocol set:\n%s\n' "$expected_protocols" >&2
    pinned_ge_fail "generated FFmpeg config enables an unexpected protocol set"
    return 1
  }
}

pinned_ge_validate_job_schedule() {
  local global_jobs="$1"
  local make_jobs="$2"
  local ninja_jobs="$3"
  local product

  [[ "$global_jobs" =~ ^[1-9][0-9]*$ \
      && "$make_jobs" =~ ^[1-9][0-9]*$ \
      && "$ninja_jobs" =~ ^[1-9][0-9]*$ ]] \
    || pinned_ge_fail "global, Make, and Ninja job counts must be positive integers" \
    || return 1
  [[ "$global_jobs" -le "$PIN_DEFAULT_JOBS" ]] \
    || pinned_ge_fail "global jobs exceed the accepted cap $PIN_DEFAULT_JOBS" \
    || return 1
  [[ "$make_jobs" -le "$global_jobs" && "$ninja_jobs" -le "$global_jobs" ]] \
    || pinned_ge_fail "Make and Ninja jobs must not exceed the global budget $global_jobs" \
    || return 1
  (( make_jobs <= global_jobs / ninja_jobs )) \
    || pinned_ge_fail \
      "nested scheduler product ${make_jobs}x${ninja_jobs} exceeds global budget $global_jobs" \
    || return 1
  product=$((make_jobs * ninja_jobs))
  printf '%s\n' "$product"
}

pinned_ge_resolve_job_schedule() {
  local global_jobs="$1"
  local make_jobs="${2:-}"
  local ninja_jobs="${3:-}"
  local max_jobs product

  [[ "$global_jobs" =~ ^[1-9][0-9]*$ ]] \
    || pinned_ge_fail "global jobs must be a positive integer" || return 1
  [[ "$global_jobs" -le "$PIN_DEFAULT_JOBS" ]] \
    || pinned_ge_fail "global jobs exceed the accepted cap $PIN_DEFAULT_JOBS" \
    || return 1
  if [[ -n "$make_jobs" && ! "$make_jobs" =~ ^[1-9][0-9]*$ ]]; then
    pinned_ge_fail "Make jobs must be a positive integer"
    return 1
  fi
  if [[ -n "$ninja_jobs" && ! "$ninja_jobs" =~ ^[1-9][0-9]*$ ]]; then
    pinned_ge_fail "Ninja jobs must be a positive integer"
    return 1
  fi

  if [[ -z "$make_jobs" ]]; then
    if [[ -n "$ninja_jobs" ]]; then
      [[ "$ninja_jobs" -le "$global_jobs" ]] \
        || pinned_ge_fail "Ninja jobs exceed the global budget $global_jobs" \
        || return 1
      max_jobs=$((global_jobs / ninja_jobs))
      make_jobs="$PIN_DEFAULT_MAKE_JOBS"
      (( make_jobs <= max_jobs )) || make_jobs="$max_jobs"
    else
      make_jobs="$PIN_DEFAULT_MAKE_JOBS"
      (( make_jobs <= global_jobs )) || make_jobs="$global_jobs"
    fi
  fi
  [[ "$make_jobs" -le "$global_jobs" ]] \
    || pinned_ge_fail "Make jobs exceed the global budget $global_jobs" \
    || return 1

  if [[ -z "$ninja_jobs" ]]; then
    max_jobs=$((global_jobs / make_jobs))
    ninja_jobs="$PIN_DEFAULT_NINJA_JOBS"
    (( ninja_jobs <= max_jobs )) || ninja_jobs="$max_jobs"
  fi

  product="$(pinned_ge_validate_job_schedule \
    "$global_jobs" "$make_jobs" "$ninja_jobs")" || return 1
  printf '%s\t%s\t%s\t%s\n' \
    "$global_jobs" "$make_jobs" "$ninja_jobs" "$product"
}

pinned_ge_require_prepared_media_provenance() (
  set -euo pipefail

  local source="$1"
  local state_dir="$2"
  local series="${3:-$PINNED_GE_ROOT/patches/series}"
  local state_file patch_audit cleanup_audit baseline status_file manifest
  local manifest_sha cleanup_sha baseline_sha recorded_status current_status
  local normalization_digest upstream_patch_rel upstream_patch_sha upstream_patch
  local manifest_deletions audit_deletions survivors external_references reference_status
  local current_diff touched_path
  local -a touched_paths
  local -A touched_path_map=()

  [[ -f "$series" && ! -L "$series" ]] \
    || pinned_ge_fail "selected Wine patch series is absent or unsafe: $series" \
    || return 1
  source="$(realpath -e -- "$source")"
  state_dir="$(realpath -e -- "$state_dir")"
  state_file="$state_dir/prepared-source.tsv"
  patch_audit="$state_dir/rtsp-patch-audit.tsv"
  cleanup_audit="$state_dir/ge-media-cleanup-audit.tsv"
  baseline="$state_dir/ge-pre-rtsp-diff-check.txt"
  status_file="$state_dir/ge-pre-rtsp-diff-check.status"

  for path in "$state_file" "$patch_audit" "$cleanup_audit" "$baseline" "$status_file"; do
    [[ -f "$path" && ! -L "$path" ]] \
      || pinned_ge_fail "prepared media provenance file is absent or unsafe: $path" \
      || return 1
  done
  [[ -e "$source/wine/.git" ]] \
    || pinned_ge_fail "prepared Wine worktree is absent: $source/wine" || return 1

  manifest="$(realpath -e -- "$PINNED_GE_ROOT/$PIN_GE_MEDIA_CLEANUP_MANIFEST")"
  [[ "$manifest" == "$PINNED_GE_ROOT/config/"* \
      && -f "$manifest" && ! -L "$manifest" ]] \
    || pinned_ge_fail "GE media-cleanup manifest is not a regular project config file" \
    || return 1
  manifest_sha="$(pinned_ge_sha256 "$manifest")"
  [[ "$manifest_sha" == "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256" ]] \
    || pinned_ge_fail "GE media-cleanup manifest differs from its pinned digest" \
    || return 1

  [[ "$(pinned_ge_state_value "$manifest" media_cleanup_version)" == 1 ]] \
    || pinned_ge_fail "unsupported GE media-cleanup manifest version" || return 1
  [[ "$(pinned_ge_state_value "$manifest" source_commit)" == "$PIN_SOURCE_COMMIT" ]] \
    || pinned_ge_fail "GE media-cleanup manifest is pinned to another source" \
    || return 1
  upstream_patch_rel="$(pinned_ge_state_value "$manifest" upstream_patch)"
  upstream_patch_sha="$(pinned_ge_state_value "$manifest" upstream_patch_sha256)"
  case "$upstream_patch_rel" in
    patches/ge-video-rework/*) ;;
    *) pinned_ge_fail "GE media-cleanup evidence is not a video-rework patch" \
      || return 1 ;;
  esac
  upstream_patch="$(realpath -e -- "$source/$upstream_patch_rel")"
  [[ "$upstream_patch" == "$source/patches/ge-video-rework/"* \
      && -f "$upstream_patch" && ! -L "$upstream_patch" \
      && "$(pinned_ge_sha256 "$upstream_patch")" == "$upstream_patch_sha" ]] \
    || pinned_ge_fail "GE media-cleanup upstream evidence changed" || return 1

  awk -F '\t' '
    BEGIN {
      unique["media_cleanup_audit_version"] = 1
      unique["source_commit"] = 1
      unique["normalization_digest"] = 1
      unique["manifest_sha256"] = 1
      unique["upstream_patch"] = 1
      unique["upstream_patch_sha256"] = 1
      unique["external_reference_check"] = 1
      unique["generated_configure_reference_check"] = 1
      unique["remaining_non_evidence_winegstreamer_files"] = 1
      unique["media_cleanup"] = 1
    }
    $1 == "deleted" {
      if (NF != 4) bad = 1
      deleted++
      next
    }
    {
      if (!($1 in unique) || NF != 2 || seen[$1]++) bad = 1
    }
    END {
      if (deleted != 3) bad = 1
      for (key in unique) if (seen[key] != 1) bad = 1
      exit bad
    }
  ' "$cleanup_audit" \
    || pinned_ge_fail "GE media-cleanup audit has an unexpected schema" || return 1

  [[ "$(pinned_ge_state_value "$cleanup_audit" media_cleanup_audit_version)" == 1 \
      && "$(pinned_ge_state_value "$cleanup_audit" source_commit)" == "$PIN_SOURCE_COMMIT" \
      && "$(pinned_ge_state_value "$cleanup_audit" normalization_digest)" == "$manifest_sha" \
      && "$(pinned_ge_state_value "$cleanup_audit" manifest_sha256)" == "$manifest_sha" \
      && "$(pinned_ge_state_value "$cleanup_audit" upstream_patch)" == "$upstream_patch_rel" \
      && "$(pinned_ge_state_value "$cleanup_audit" upstream_patch_sha256)" == "$upstream_patch_sha" \
      && "$(pinned_ge_state_value "$cleanup_audit" external_reference_check)" == passed \
      && "$(pinned_ge_state_value "$cleanup_audit" generated_configure_reference_check)" \
        == deferred-until-autoreconf \
      && "$(pinned_ge_state_value "$cleanup_audit" remaining_non_evidence_winegstreamer_files)" == 0 \
      && "$(pinned_ge_state_value "$cleanup_audit" media_cleanup)" == passed ]] \
    || pinned_ge_fail "GE media-cleanup audit facts do not match the pinned cleanup" \
    || return 1

  manifest_deletions="$(awk -F '\t' '$1 == "delete" {print $2 "\t" $3 "\t" $4}' "$manifest")"
  audit_deletions="$(awk -F '\t' '$1 == "deleted" {print $2 "\t" $3 "\t" $4}' "$cleanup_audit")"
  [[ "$manifest_deletions" == "$audit_deletions" ]] \
    || pinned_ge_fail "GE media-cleanup audit deletion tuples differ from the manifest" \
    || return 1

  cleanup_sha="$(pinned_ge_sha256 "$cleanup_audit")"
  normalization_digest="$(pinned_ge_state_value \
    "$state_file" ge_media_cleanup_normalization_digest)"
  [[ "$normalization_digest" == "$manifest_sha" \
      && "$(pinned_ge_state_value "$state_file" ge_media_cleanup_audit_sha256)" \
        == "$cleanup_sha" \
      && "$(pinned_ge_state_value "$state_file" final_winegstreamer_external_references)" == 0 \
      && "$(pinned_ge_state_value "$state_file" final_diff_check_baseline_match)" == passed ]] \
    || pinned_ge_fail "prepared-state media-cleanup provenance is inconsistent" || return 1

  baseline_sha="$(pinned_ge_sha256 "$baseline")"
  recorded_status="$(pinned_ge_state_value "$state_file" ge_pre_rtsp_diff_check_status)"
  [[ "$recorded_status" == 0 || "$recorded_status" == 2 ]] \
    || pinned_ge_fail "invalid recorded GE diff-check status: $recorded_status" || return 1
  [[ "$(cat "$status_file")" == "$recorded_status" \
      && "$(pinned_ge_state_value "$state_file" ge_pre_rtsp_diff_check_sha256)" \
        == "$baseline_sha" ]] \
    || pinned_ge_fail "GE diff-check baseline files differ from prepared state" || return 1
  [[ "$(pinned_ge_state_value "$patch_audit" ge_baseline_diff_check_status)" \
        == "$recorded_status" \
      && "$(pinned_ge_state_value "$patch_audit" ge_baseline_diff_check_sha256)" \
        == "$baseline_sha" \
      && "$(pinned_ge_state_value "$patch_audit" ge_media_cleanup_normalization_digest)" \
        == "$manifest_sha" \
      && "$(pinned_ge_state_value "$patch_audit" touched_path_diff_check)" == passed \
      && "$(pinned_ge_state_value "$patch_audit" complete_diff_check_baseline_match)" == passed ]] \
    || pinned_ge_fail "RTSP patch audit is not bound to the GE baseline and cleanup" \
    || return 1
  [[ "$(pinned_ge_state_value "$state_file" patch_audit_sha256)" \
        == "$(pinned_ge_sha256 "$patch_audit")" \
      && "$(pinned_ge_state_value "$patch_audit" patch_audit_version)" == 1 \
      && "$(pinned_ge_state_value "$patch_audit" series_sha256)" \
        == "$(pinned_ge_sha256 "$series")" ]] \
    || pinned_ge_fail "RTSP patch audit identity differs from prepared state or series" \
    || return 1

  touched_paths=()
  while IFS=$'\t' read -r kind touched_path extra; do
    [[ "$kind" == touched_path ]] || continue
    [[ -n "$touched_path" && -z "$extra" ]] \
      || pinned_ge_fail "malformed RTSP touched-path audit record" || return 1
    case "$touched_path" in
      /*|../*|*/../*|*/..|..)
        pinned_ge_fail "unsafe RTSP touched path in audit: $touched_path" || return 1 ;;
    esac
    [[ -z "${touched_path_map[$touched_path]+present}" ]] \
      || pinned_ge_fail "duplicate RTSP touched path in audit: $touched_path" \
      || return 1
    touched_path_map["$touched_path"]=1
    touched_paths+=("$touched_path")
  done <"$patch_audit"
  [[ "${#touched_paths[@]}" -gt 0 ]] \
    || pinned_ge_fail "RTSP patch audit has no touched paths" || return 1
  pinned_ge_git -C "$source/wine" diff --check -- "${touched_paths[@]}" \
    || pinned_ge_fail "an RTSP-touched Wine path fails diff --check" || return 1

  current_diff="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-build-diff-check.XXXXXX")"
  trap 'rm -f -- "$current_diff"' EXIT
  set +e
  pinned_ge_git -C "$source/wine" diff --check >"$current_diff" 2>&1
  current_status=$?
  set -e
  [[ "$current_status" == "$recorded_status" ]] \
    || pinned_ge_fail "current Wine diff-check status differs from the recorded GE baseline" \
    || return 1
  cmp -s -- "$baseline" "$current_diff" \
    || pinned_ge_fail "current Wine diff-check output differs from the recorded GE baseline" \
    || return 1

  survivors="$({
    find "$source/wine/dlls/winegstreamer" -type f \
      ! -name '*.orig' ! -name '*.rej' -print 2>/dev/null || true
  } | LC_ALL=C sort)"
  [[ -z "$survivors" ]] || {
    printf '%s\n' "$survivors" >&2
    pinned_ge_fail "non-evidence WineGStreamer source survived prepared cleanup"
    return 1
  }

  set +e
  external_references="$(
    pinned_ge_git -C "$source/wine" grep -n -I -e winegstreamer -- . \
      ':!dlls/winegstreamer/**' 2>&1
  )"
  reference_status=$?
  set -e
  case "$reference_status" in
    1) ;;
    0)
      printf '%s\n' "$external_references" >&2
      pinned_ge_fail "final prepared Wine source has external WineGStreamer references"
      return 1
      ;;
    *)
      printf '%s\n' "$external_references" >&2
      pinned_ge_fail "could not verify final WineGStreamer references"
      return 1
      ;;
  esac
)

pinned_ge_default_source() {
  printf '%s/worktrees/%s-rtsp\n' "$PINNED_GE_ROOT" "$PIN_ID"
}

pinned_ge_default_build_dir() {
  # Piper/eSpeak has a nested-path failure on long build roots. Keep the
  # disposable object path short; verified artifacts remain named by the full
  # compatibility-tool identity inside it.
  printf '%s/rtsp-ge11-%s\n' "${PINNED_GE_BUILD_ROOT:-/tmp}" \
    "${PIN_SOURCE_COMMIT:0:8}"
}

pinned_ge_default_state_dir() {
  printf '%s/work/%s\n' "$PINNED_GE_ROOT" "$PIN_ID"
}

pinned_ge_registered_submodules() {
  local source="$1"
  pinned_ge_git -C "$source" config -f .gitmodules \
    --get-regexp '^submodule\..*\.path$' \
    | awk '$2 !~ /^(nvidia-libs|vklayers)\// {print $2}'
}

pinned_ge_require_original_build_rules() {
  local source="$1"
  local actual_blob actual_sha

  actual_blob="$(pinned_ge_git -C "$source" rev-parse 'HEAD:make/rules-meson.mk')"
  [[ "$actual_blob" == "$PIN_RULES_MESON_BLOB" ]] \
    || pinned_ge_fail "rules-meson blob is $actual_blob, expected $PIN_RULES_MESON_BLOB" \
    || return 1
  actual_sha="$(pinned_ge_sha256 "$source/make/rules-meson.mk")"
  [[ "$actual_sha" == "$PIN_RULES_MESON_SHA256" ]] \
    || pinned_ge_fail "rules-meson content differs from the accepted snapshot" \
    || return 1
}

pinned_ge_require_source_identity() {
  local source="$1"
  local actual count record path
  local -a registered gitlinks unmapped
  declare -A registered_map=()

  [[ -e "$source/.git" ]] || pinned_ge_fail "missing GE source repository: $source" || return 1
  actual="$(pinned_ge_git -C "$source" rev-parse --verify "$PIN_SOURCE_REF^{commit}")" \
    || pinned_ge_fail "pinned source ref does not resolve locally: $PIN_SOURCE_REF" \
    || return 1
  [[ "$actual" == "$PIN_SOURCE_COMMIT" ]] \
    || pinned_ge_fail \
      "pinned source ref resolves to $actual, expected $PIN_SOURCE_COMMIT" \
    || return 1
  actual="$(pinned_ge_git -C "$source" rev-parse HEAD)"
  [[ "$actual" == "$PIN_SOURCE_COMMIT" ]] \
    || pinned_ge_fail "GE source HEAD is $actual, expected $PIN_SOURCE_COMMIT" || return 1
  actual="$(pinned_ge_git -C "$source" rev-parse 'HEAD^{tree}')"
  [[ "$actual" == "$PIN_SOURCE_TREE" ]] \
    || pinned_ge_fail "GE source tree is $actual, expected $PIN_SOURCE_TREE" || return 1

  [[ "$(pinned_ge_sha256 "$source/.gitmodules")" == "$PIN_GITMODULES_SHA256" ]] \
    || pinned_ge_fail ".gitmodules content differs from the accepted snapshot" || return 1
  [[ "$(pinned_ge_sha256 "$source/patches/protonprep-valve-staging.sh")" == "$PIN_PATCH_DRIVER_SHA256" ]] \
    || pinned_ge_fail "GE patch driver differs from the accepted snapshot" || return 1
  if [[ "$(pinned_ge_sha256 "$source/Makefile.in")" != "$PIN_MAKEFILE_IN_SHA256" ]]; then
    [[ "$PIN_SCHEMA_VERSION" == 5 \
        && "$(pinned_ge_git -C "$source" \
          hash-object --no-filters -- Makefile.in)" \
          == "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" ]] \
      || pinned_ge_fail \
        "GE Makefile.in is neither the accepted snapshot nor schema-5 crypto result" \
      || return 1
  fi
  [[ "$(pinned_ge_sha256 "$source/configure.sh")" == "$PIN_CONFIGURE_SHA256" ]] \
    || pinned_ge_fail "GE configure.sh differs from the accepted snapshot" || return 1

  mapfile -t registered < <(pinned_ge_registered_submodules "$source")
  [[ "${#registered[@]}" == "$PIN_REGISTERED_SUBMODULE_COUNT" ]] \
    || pinned_ge_fail "registered submodule count is ${#registered[@]}, expected $PIN_REGISTERED_SUBMODULE_COUNT" \
    || return 1
  for path in "${registered[@]}"; do
    registered_map["$path"]=1
    record="$(pinned_ge_git -C "$source" ls-tree HEAD -- "$path")"
    [[ "$record" == 160000\ commit\ *$'\t'"$path" ]] \
      || pinned_ge_fail "registered submodule is not a gitlink: $path" || return 1
  done

  mapfile -t gitlinks < <(
    pinned_ge_git -C "$source" ls-tree -r HEAD \
      | awk '$1 == "160000" && $2 == "commit" {print $4}'
  )
  [[ "${#gitlinks[@]}" == "$PIN_GITLINK_COUNT" ]] \
    || pinned_ge_fail "gitlink count is ${#gitlinks[@]}, expected $PIN_GITLINK_COUNT" || return 1
  unmapped=()
  for path in "${gitlinks[@]}"; do
    [[ -n "${registered_map[$path]+present}" ]] || unmapped+=("$path")
  done
  [[ "${#unmapped[@]}" == "$PIN_UNMAPPED_OPTIONAL_GITLINK_COUNT" ]] \
    || pinned_ge_fail "unmapped gitlink count is ${#unmapped[@]}, expected $PIN_UNMAPPED_OPTIONAL_GITLINK_COUNT" \
    || return 1
  for path in "${unmapped[@]}"; do
    case "$path" in
      nvidia-libs/*|vklayers/*) ;;
      *) pinned_ge_fail "unexpected unmapped gitlink: $path" || return 1 ;;
    esac
  done
}

pinned_ge_require_registered_submodules() {
  local source="$1"
  local path expected actual status_text line
  local -a registered status_lines

  mapfile -t registered < <(pinned_ge_registered_submodules "$source")
  for path in "${registered[@]}"; do
    expected="$(pinned_ge_git -C "$source" ls-tree HEAD -- "$path" \
      | awk '$1 == "160000" && $2 == "commit" {print $3}')"
    [[ -n "$expected" ]] || pinned_ge_fail "missing gitlink pin: $path" || return 1
    [[ -e "$source/$path/.git" ]] \
      || pinned_ge_fail "registered submodule is not initialized: $path" || return 1
    actual="$(pinned_ge_git -C "$source/$path" rev-parse HEAD)"
    [[ "$actual" == "$expected" ]] \
      || pinned_ge_fail "$path is at $actual, expected $expected" || return 1
  done

  [[ "$(pinned_ge_git -C "$source/wine" rev-parse HEAD)" == "$PIN_WINE_COMMIT" ]] \
    || pinned_ge_fail "Wine does not match the pinned snapshot" || return 1
  [[ "$(pinned_ge_git -C "$source/wine-staging" rev-parse HEAD)" == "$PIN_WINE_STAGING_COMMIT" ]] \
    || pinned_ge_fail "Wine-Staging does not match the pinned snapshot" || return 1
  [[ "$(pinned_ge_git -C "$source/ffmpeg" rev-parse HEAD)" == "$PIN_FFMPEG_COMMIT" ]] \
    || pinned_ge_fail "FFmpeg does not match the pinned snapshot" || return 1

  status_text="$(pinned_ge_git -C "$source" \
    submodule status --recursive -- "${registered[@]}")" || return 1
  status_lines=()
  if [[ -n "$status_text" ]]; then
    mapfile -t status_lines <<<"$status_text"
  fi
  for line in "${status_lines[@]}"; do
    case "$line" in
      [-+U]*) pinned_ge_fail "recursive submodule does not match its recorded pin: $line" \
        || return 1 ;;
    esac
  done
}

pinned_ge_series_entries() {
  local patch_root="$1"
  local series="$2"
  local line entry resolved root

  [[ -d "$patch_root" ]] || pinned_ge_fail "patch root is absent: $patch_root" || return 1
  [[ -f "$series" && ! -L "$series" ]] \
    || pinned_ge_fail "patch series is not a regular non-symlink file: $series" \
    || return 1
  root="$(cd "$patch_root" && pwd -P)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    entry="${line%%#*}"
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    [[ -n "$entry" ]] || continue
    [[ "$entry" != *$'\t'* && "$entry" != *$'\n'* ]] \
      || pinned_ge_fail "patch-series path is not safely representable: $entry" \
      || return 1
    case "$entry" in
      /*|../*|*/../*|*/..|..) pinned_ge_fail "unsafe patch-series path: $entry" || return 1 ;;
    esac
    resolved="$(realpath -e -- "$root/$entry")"
    [[ "$resolved" == "$root/"* && -f "$resolved" && ! -L "$root/$entry" ]] \
      || pinned_ge_fail "patch-series entry escapes its root or is not a regular file: $entry" \
      || return 1
    printf '%s\n' "$entry"
  done < "$series"
}

pinned_ge_series_digest() {
  local patch_root="$1"
  local series="$2"
  local entry entries_text
  local -a entries

  entries_text="$(pinned_ge_series_entries "$patch_root" "$series")" || return 1
  entries=()
  if [[ -n "$entries_text" ]]; then
    mapfile -t entries <<<"$entries_text"
  fi
  [[ "${#entries[@]}" -gt 0 ]] \
    || pinned_ge_fail "the RTSP patch series has no active entries" || return 1
  {
    printf 'series\t%s\n' "$(pinned_ge_sha256 "$series")"
    for entry in "${entries[@]}"; do
      printf 'patch\t%s\t%s\n' "$entry" "$(pinned_ge_sha256 "$patch_root/$entry")"
    done
  } | sha256sum | awk '{print $1}'
}

pinned_ge_ffmpeg_security_records() {
  local fixes_text="${PIN_FFMPEG_SECURITY_FIX_COMMITS:-}"
  local hashes_text="${PIN_FFMPEG_SECURITY_PATCH_SHA256S:-}"
  local index fix hash
  local -a fixes hashes
  declare -A seen=()

  IFS=',' read -r -a fixes <<<"$fixes_text"
  IFS=',' read -r -a hashes <<<"$hashes_text"
  [[ "${#fixes[@]}" == 4 && "${#hashes[@]}" == 4 ]] \
    || pinned_ge_fail \
      "schema 5 requires exactly four FFmpeg security records and matching hashes" \
    || return 1
  for index in "${!fixes[@]}"; do
    fix="${fixes[$index]}"
    hash="${hashes[$index]}"
    [[ "$fix" =~ ^[0-9a-f]{40}$ && "$hash" =~ ^[0-9a-f]{64}$ ]] \
      || pinned_ge_fail "malformed FFmpeg security identity at position $((index + 1))" \
      || return 1
    [[ -z "${seen[$fix]+present}" ]] \
      || pinned_ge_fail "duplicate FFmpeg security fix identity: $fix" || return 1
    seen["$fix"]=1
    printf '%s\t%s\t%s\n' "$((index + 1))" "$fix" "$hash"
  done
  [[ "${fixes[0]}" == 374b726ffa878ee1cadb987bd1e1e20cc7ed8845 \
      && "${fixes[1]}" == 5806e8b9f34f1b0663b3017ef9dd1aa5d08116d1 \
      && "${fixes[2]}" == c23d4da3128c279b714b282e6ec292e8755007e3 \
      && "${fixes[3]}" == 8db766d029d2ab52f3a4e98bb2bcfbe438586bf4 ]] \
    || pinned_ge_fail \
      "the three upstream MagicYUV fixes or project TLS-default policy patch are absent or reordered" \
    || return 1
}

pinned_ge_ffmpeg_security_series_records() {
  local patch_root="$1"
  local series="$2"
  local entries_text identities_text entry identity first_line numstat numstat_sha
  local index fix expected_sha additions deletions path extra line_count
  local -a entries identities

  entries_text="$(pinned_ge_series_entries "$patch_root" "$series")" || return 1
  identities_text="$(pinned_ge_ffmpeg_security_records)" || return 1
  mapfile -t entries <<<"$entries_text"
  mapfile -t identities <<<"$identities_text"
  [[ "${#entries[@]}" == "${#identities[@]}" ]] \
    || pinned_ge_fail "FFmpeg security series length differs from the pinned fix train" \
    || return 1
  [[ "$(pinned_ge_sha256 "$series")" == "$PIN_FFMPEG_SECURITY_SERIES_SHA256" ]] \
    || pinned_ge_fail "FFmpeg security series file differs from its pin" || return 1

  for index in "${!entries[@]}"; do
    entry="${entries[$index]}"
    identity="${identities[$index]}"
    IFS=$'\t' read -r _ fix expected_sha <<<"$identity"
    first_line="$(sed -n '1p' "$patch_root/$entry")"
    [[ "$first_line" == "From $fix Mon Sep 17 00:00:00 2001" ]] \
      || pinned_ge_fail "FFmpeg patch $entry does not preserve origin $fix" \
      || return 1
    [[ "$(pinned_ge_sha256 "$patch_root/$entry")" == "$expected_sha" ]] \
      || pinned_ge_fail "FFmpeg security patch differs from its pin: $entry" \
      || return 1
    numstat="$(git apply --numstat -- "$patch_root/$entry")" \
      || pinned_ge_fail "could not parse FFmpeg security patch: $entry" || return 1
    line_count=0
    while IFS=$'\t' read -r additions deletions path extra; do
      line_count=$((line_count + 1))
      [[ "$additions" =~ ^[0-9]+$ && "$deletions" =~ ^[0-9]+$ \
          && -n "$path" && -z "$extra" ]] \
        || pinned_ge_fail "FFmpeg security patch has invalid numstat: $entry" \
        || return 1
      case "$path" in
        libavcodec/*|libavformat/*) ;;
        *) pinned_ge_fail \
          "FFmpeg security patch escapes the codec/format security scope: $entry:$path" \
          || return 1 ;;
      esac
      case "$path" in
        /*|../*|*/../*|*/..|..|*'{'*|*'}'*|*' => '*)
          pinned_ge_fail "FFmpeg security patch uses an unsafe path: $entry:$path" \
          || return 1 ;;
      esac
    done <<<"$numstat"
    [[ "$line_count" -gt 0 ]] \
      || pinned_ge_fail "FFmpeg security patch has no source changes: $entry" \
      || return 1
    numstat_sha="$(printf '%s\n' "$numstat" | sha256sum | awk '{print $1}')"
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$((index + 1))" "$entry" "$fix" "$expected_sha" "$numstat_sha"
  done
}

pinned_ge_ffmpeg_security_touched_paths() {
  local patch_root="$1"
  local series="$2"
  local entry additions deletions path extra paths

  pinned_ge_ffmpeg_security_series_records "$patch_root" "$series" >/dev/null \
    || return 1
  paths="$({
    while IFS= read -r entry; do
      while IFS=$'\t' read -r additions deletions path extra; do
        printf '%s\n' "$path"
      done < <(git apply --numstat -- "$patch_root/$entry")
    done < <(pinned_ge_series_entries "$patch_root" "$series")
  } | LC_ALL=C sort -u)"
  [[ "$paths" == $'libavcodec/magicyuv.c\nlibavformat/version_major.h' ]] \
    || pinned_ge_fail \
      "schema-5 FFmpeg security series changed paths outside the reviewed pair" \
    || return 1
  printf '%s\n' "$paths"
}

pinned_ge_ffmpeg_crypto_build_patch_record() {
  local patch_root="$1"
  local series="$2"
  local entries_text entry first_line numstat additions deletions path extra

  entries_text="$(pinned_ge_series_entries "$patch_root" "$series")" || return 1
  [[ "$(wc -l <<<"$entries_text")" == 1 ]] \
    || pinned_ge_fail "FFmpeg crypto-build series must contain exactly one patch" \
    || return 1
  entry="$entries_text"
  [[ "$(pinned_ge_sha256 "$series")" \
      == "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256" ]] \
    || pinned_ge_fail "FFmpeg crypto-build series differs from its pin" || return 1
  first_line="$(sed -n '1p' "$patch_root/$entry")"
  [[ "$first_line" \
      == "From $PIN_FFMPEG_CRYPTO_BUILD_ORIGIN Mon Sep 17 00:00:00 2001" ]] \
    || pinned_ge_fail "FFmpeg crypto-build patch origin differs from its pin" \
    || return 1
  [[ "$(pinned_ge_sha256 "$patch_root/$entry")" \
      == "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256" ]] \
    || pinned_ge_fail "FFmpeg crypto-build patch differs from its pin" || return 1
  numstat="$(git apply --numstat -- "$patch_root/$entry")" \
    || pinned_ge_fail "could not parse FFmpeg crypto-build patch" || return 1
  IFS=$'\t' read -r additions deletions path extra <<<"$numstat"
  [[ "$additions" == 1 && "$deletions" == 0 && "$path" == Makefile.in \
      && -z "$extra" && "$(wc -l <<<"$numstat")" == 1 ]] \
    || pinned_ge_fail \
      "FFmpeg crypto-build patch is not the reviewed one-line Makefile change" \
    || return 1
  printf '%s\t%s\t%s\n' "$entry" "$PIN_FFMPEG_CRYPTO_BUILD_ORIGIN" \
    "$PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256"
}

pinned_ge_state_value() {
  local state_file="$1"
  local key="$2"
  local value count

  [[ -f "$state_file" && ! -L "$state_file" ]] \
    || pinned_ge_fail "prepared-state record is not a regular file: $state_file" \
    || return 1
  count="$(awk -F '\t' -v key="$key" '$1 == key {count++} END {print count + 0}' \
    "$state_file")"
  [[ "$count" == 1 ]] \
    || pinned_ge_fail "prepared-state key $key occurs $count times" || return 1
  value="$(awk -F '\t' -v key="$key" '$1 == key {sub(/^[^\t]*\t/, ""); print}' \
    "$state_file")"
  [[ -n "$value" ]] || pinned_ge_fail "prepared-state key is empty: $key" || return 1
  printf '%s\n' "$value"
}

# Check a GE-patched component against HEAD plus the pinned upstream patches,
# without changing its working tree or real index. The caller verifies the
# superproject state (and therefore the patch files) before using this check.
pinned_ge_require_upstream_component_patch_state() (
  local repo="$1" patch_dir="$2" temporary_index patch
  local -a patches
  temporary_index="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-component-index.XXXXXX")" || return 1
  trap 'rm -rf -- "$temporary_index"' EXIT
  export GIT_INDEX_FILE="$temporary_index/index"
  shopt -s nullglob
  patches=("$patch_dir"/*.patch)
  [[ ${#patches[@]} -gt 0 ]] \
    || pinned_ge_fail "no pinned upstream patches for component: $repo" || return 1
  pinned_ge_git -C "$repo" read-tree HEAD || return 1
  for patch in "${patches[@]}"; do
    pinned_ge_git -C "$repo" apply --cached --whitespace=nowarn -- "$patch" || return 1
  done
  pinned_ge_git -C "$repo" diff --quiet --no-ext-diff --no-textconv --ignore-submodules=all \
    || pinned_ge_fail "component differs from HEAD plus pinned GE patches: $repo" || return 1
  [[ -z "$(pinned_ge_git -C "$repo" ls-files --others --exclude-standard)" ]] \
    || pinned_ge_fail "unexpected untracked files in GE-patched component: $repo" || return 1
)

pinned_ge_repo_state_digest() {
  local repo="$1"
  local contrib_manifest="${2:-}"
  local path target expected_mode expected_size expected_sha
  local actual_mode actual_size actual_sha
  local entries_text
  local -a entries
  declare -A allowed_size=()
  declare -A allowed_sha=()

  [[ -e "$repo/.git" ]] || pinned_ge_fail "missing Git worktree for state digest: $repo" || return 1

  if [[ -n "$contrib_manifest" ]]; then
    entries_text="$(pinned_ge_build_contrib_entries "$contrib_manifest")" \
      || return 1
    entries=()
    mapfile -t entries <<<"$entries_text"
    for entry in "${entries[@]}"; do
      IFS=$'\t' read -r path expected_mode expected_size expected_sha <<<"$entry"
      allowed_size["$path"]="$expected_size"
      allowed_sha["$path"]="$expected_sha"

      if pinned_ge_git -C "$repo" ls-files --error-unmatch -- "$path" \
          >/dev/null 2>&1; then
        pinned_ge_fail "build-contrib allowlist path is tracked by the pinned source: $path"
        return 1
      fi
      [[ ! -e "$repo/$path" && ! -L "$repo/$path" ]] && continue
      [[ -f "$repo/$path" && ! -L "$repo/$path" ]] \
        || pinned_ge_fail "build-contrib cache entry is not a regular file: $path" \
        || return 1
      actual_mode="$(stat -c '%a' -- "$repo/$path")"
      [[ "0$actual_mode" == "$expected_mode" ]] \
        || pinned_ge_fail \
          "build-contrib cache mode differs for $path: 0$actual_mode, expected $expected_mode" \
        || return 1
      actual_size="$(stat -c '%s' -- "$repo/$path")"
      [[ "$actual_size" == "$expected_size" ]] \
        || pinned_ge_fail \
          "build-contrib cache size differs for $path: $actual_size, expected $expected_size" \
        || return 1
      actual_sha="$(pinned_ge_sha256 "$repo/$path")"
      [[ "$actual_sha" == "$expected_sha" ]] \
        || pinned_ge_fail "build-contrib cache digest differs for $path" || return 1
    done
  fi

  {
    printf 'repo-state-version\0001\000'
    pinned_ge_git -C "$repo" rev-parse HEAD
    pinned_ge_git -C "$repo" diff --binary --full-index --no-ext-diff HEAD --
    pinned_ge_git -C "$repo" diff --binary --full-index --no-ext-diff --cached --
    while IFS= read -r -d '' path; do
      if [[ -n "${allowed_sha[$path]+present}" ]]; then
        continue
      fi
      printf 'untracked\000%s\000' "$path"
      if [[ -L "$repo/$path" ]]; then
        target="$(readlink -- "$repo/$path")"
        printf 'symlink\000%s\000' "$target"
      elif [[ -f "$repo/$path" ]]; then
        printf 'file\000%s\000%s\000' \
          "$(stat -c '%a' -- "$repo/$path")" \
          "$(pinned_ge_sha256 "$repo/$path")"
      else
        pinned_ge_fail "unsupported untracked entry in prepared source: $repo/$path"
        return 1
      fi
    done < <(pinned_ge_git -C "$repo" ls-files --others --exclude-standard -z)
  } | sha256sum | awk '{print $1}'
}

pinned_ge_require_prepared_ffmpeg_security_provenance() (
  set -euo pipefail

  local source="$1"
  local state_dir="$2"
  local state_file patch_root series patch_audit source_audit
  local records expected_patch_rows actual_patch_rows series_digest repo_state
  local expected_touched_paths expected_touched_rows actual_touched_rows
  local touched_paths magicyuv_blob tls_version_blob regenerated_audit
  local fix_count touched_count

  source="$(realpath -e -- "$source")"
  state_dir="$(realpath -e -- "$state_dir")"
  state_file="$state_dir/prepared-source.tsv"
  patch_root="$PINNED_GE_ROOT/patches/ffmpeg-security"
  series="$patch_root/series"
  patch_audit="$state_dir/ffmpeg-security-patch-audit.tsv"
  source_audit="$state_dir/ffmpeg-source-audit.tsv"

  for path in "$state_file" "$series" "$patch_audit" "$source_audit"; do
    [[ -f "$path" && ! -L "$path" ]] \
      || pinned_ge_fail "FFmpeg security provenance file is absent or unsafe: $path" \
      || return 1
  done
  [[ -e "$source/ffmpeg/.git" ]] \
    || pinned_ge_fail "prepared FFmpeg worktree is absent" || return 1
  [[ "$(pinned_ge_git -C "$source/ffmpeg" rev-parse HEAD)" \
      == "$PIN_FFMPEG_COMMIT" ]] \
    || pinned_ge_fail "prepared FFmpeg HEAD differs from the pin" || return 1

  records="$(pinned_ge_ffmpeg_security_series_records "$patch_root" "$series")" \
    || return 1
  fix_count="$(wc -l <<<"$records")"
  expected_touched_paths="$(pinned_ge_ffmpeg_security_touched_paths \
    "$patch_root" "$series")" || return 1
  touched_count="$(wc -l <<<"$expected_touched_paths")"
  series_digest="$(pinned_ge_series_digest "$patch_root" "$series")" || return 1
  expected_patch_rows="$(while IFS=$'\t' read -r index entry fix patch_sha \
      numstat_sha; do
    printf 'patch\t%s\t%s\t%s\t%s\t%s\n' \
      "$index" "$entry" "$fix" "$patch_sha" "$numstat_sha"
  done <<<"$records")"
  actual_patch_rows="$(awk -F '\t' '$1 == "patch" {print}' "$patch_audit")"
  [[ "$actual_patch_rows" == "$expected_patch_rows" ]] \
    || pinned_ge_fail "FFmpeg security patch audit differs from the pinned series" \
    || return 1

  expected_touched_rows="$(while IFS= read -r path; do
    printf 'touched_path\t%s\n' "$path"
  done <<<"$expected_touched_paths")"
  actual_touched_rows="$(awk -F '\t' '$1 == "touched_path" {print}' \
    "$patch_audit")"
  [[ "$actual_touched_rows" == "$expected_touched_rows" ]] \
    || pinned_ge_fail "FFmpeg security touched-path audit differs from the series" \
    || return 1

  awk -F '\t' -v expected_patches="$fix_count" \
      -v expected_touched="$touched_count" '
    BEGIN {
      unique["ffmpeg_security_patch_audit_version"] = 1
      unique["base_commit"] = 1
      unique["fix_commits"] = 1
      unique["fix_count"] = 1
      unique["series_path"] = 1
      unique["series_sha256"] = 1
      unique["series_digest"] = 1
      unique["final_magicyuv_blob"] = 1
      unique["final_tls_version_blob"] = 1
      unique["ffmpeg_repo_state_sha256"] = 1
      unique["source_diff_check"] = 1
      unique["security_series_application"] = 1
    }
    $1 == "patch" {
      if (NF != 6) bad = 1
      patches++
      next
    }
    $1 == "touched_path" {
      if (NF != 2) bad = 1
      touched++
      next
    }
    {
      if (!($1 in unique) || NF != 2 || seen[$1]++) bad = 1
    }
    END {
      if (patches != expected_patches || touched != expected_touched) bad = 1
      for (key in unique) if (seen[key] != 1) bad = 1
      exit bad
    }
  ' "$patch_audit" \
    || pinned_ge_fail "FFmpeg security patch audit has an unexpected schema" \
    || return 1

  magicyuv_blob="$(pinned_ge_git -C "$source/ffmpeg" \
    hash-object --no-filters -- libavcodec/magicyuv.c)"
  [[ "$magicyuv_blob" == "$PIN_FFMPEG_SECURITY_MAGICYUV_BLOB" ]] \
    || pinned_ge_fail "prepared MagicYUV source differs from its pinned final blob" \
    || return 1
  tls_version_blob="$(pinned_ge_git -C "$source/ffmpeg" \
    hash-object --no-filters -- libavformat/version_major.h)"
  [[ "$tls_version_blob" == "$PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB" ]] \
    || pinned_ge_fail "prepared TLS-default header differs from its pinned final blob" \
    || return 1
  touched_paths="$(pinned_ge_git -C "$source/ffmpeg" diff --name-only HEAD -- \
    | LC_ALL=C sort -u)"
  [[ "$touched_paths" == "$expected_touched_paths" \
      && -z "$(pinned_ge_git -C "$source/ffmpeg" diff --name-only --cached --)" \
      && -z "$(pinned_ge_git -C "$source/ffmpeg" \
        ls-files --others --exclude-standard)" ]] \
    || pinned_ge_fail "prepared FFmpeg has changes outside the security backport" \
    || return 1
  pinned_ge_git -C "$source/ffmpeg" diff --check || return 1
  repo_state="$(pinned_ge_repo_state_digest "$source/ffmpeg")" || return 1

  [[ "$(pinned_ge_state_value "$patch_audit" \
        ffmpeg_security_patch_audit_version)" == 1 \
      && "$(pinned_ge_state_value "$patch_audit" base_commit)" \
        == "$PIN_FFMPEG_COMMIT" \
      && "$(pinned_ge_state_value "$patch_audit" fix_commits)" \
        == "$PIN_FFMPEG_SECURITY_FIX_COMMITS" \
      && "$(pinned_ge_state_value "$patch_audit" fix_count)" == "$fix_count" \
      && "$(pinned_ge_state_value "$patch_audit" series_path)" \
        == patches/ffmpeg-security/series \
      && "$(pinned_ge_state_value "$patch_audit" series_sha256)" \
        == "$PIN_FFMPEG_SECURITY_SERIES_SHA256" \
      && "$(pinned_ge_state_value "$patch_audit" series_digest)" == "$series_digest" \
      && "$(pinned_ge_state_value "$patch_audit" final_magicyuv_blob)" \
        == "$magicyuv_blob" \
      && "$(pinned_ge_state_value "$patch_audit" final_tls_version_blob)" \
        == "$tls_version_blob" \
      && "$(pinned_ge_state_value "$patch_audit" ffmpeg_repo_state_sha256)" \
        == "$repo_state" \
      && "$(pinned_ge_state_value "$patch_audit" source_diff_check)" == passed \
      && "$(pinned_ge_state_value "$patch_audit" security_series_application)" \
        == passed ]] \
    || pinned_ge_fail "FFmpeg security patch audit facts are inconsistent" || return 1

  regenerated_audit="$(mktemp "${TMPDIR:-/tmp}/ffmpeg-source-audit.XXXXXX")"
  trap 'rm -f -- "$regenerated_audit"' EXIT
  "$PINNED_GE_ROOT/scripts/audit-ffmpeg-source.sh" \
    --source "$source" --require-security-series >"$regenerated_audit"
  cmp -s -- "$regenerated_audit" "$source_audit" \
    || pinned_ge_fail "recorded FFmpeg source audit differs from the prepared source" \
    || return 1

  [[ "$(pinned_ge_state_value "$state_file" ffmpeg_security_fix_commits)" \
        == "$PIN_FFMPEG_SECURITY_FIX_COMMITS" \
      && "$(pinned_ge_state_value "$state_file" ffmpeg_security_series_sha256)" \
        == "$PIN_FFMPEG_SECURITY_SERIES_SHA256" \
      && "$(pinned_ge_state_value "$state_file" ffmpeg_security_series_digest)" \
        == "$series_digest" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_security_patch_audit_sha256)" \
        == "$(pinned_ge_sha256 "$patch_audit")" \
      && "$(pinned_ge_state_value "$state_file" ffmpeg_source_audit_sha256)" \
        == "$(pinned_ge_sha256 "$source_audit")" \
      && "$(pinned_ge_state_value "$state_file" ffmpeg_repo_state_sha256)" \
        == "$repo_state" \
      && "$(pinned_ge_state_value "$state_file" ffmpeg_security_magicyuv_blob)" \
        == "$magicyuv_blob" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_security_tls_version_blob)" == "$tls_version_blob" ]] \
    || pinned_ge_fail "prepared-state FFmpeg security provenance is inconsistent" \
    || return 1
)

pinned_ge_require_prepared_ffmpeg_crypto_build_provenance() (
  set -euo pipefail

  local source="$1"
  local state_dir="$2"
  local state_file patch_root series audit record entry origin patch_sha
  local series_digest makefile_blob enable_count

  source="$(realpath -e -- "$source")"
  state_dir="$(realpath -e -- "$state_dir")"
  state_file="$state_dir/prepared-source.tsv"
  patch_root="$PINNED_GE_ROOT/patches/ffmpeg-build"
  series="$patch_root/series"
  audit="$state_dir/ffmpeg-crypto-build-audit.tsv"
  for path in "$state_file" "$series" "$audit"; do
    [[ -f "$path" && ! -L "$path" ]] \
      || pinned_ge_fail "FFmpeg crypto-build provenance is absent or unsafe: $path" \
      || return 1
  done

  record="$(pinned_ge_ffmpeg_crypto_build_patch_record \
    "$patch_root" "$series")" || return 1
  IFS=$'\t' read -r entry origin patch_sha <<<"$record"
  series_digest="$(pinned_ge_series_digest "$patch_root" "$series")" || return 1
  pinned_ge_git -C "$source" apply --check --reverse \
    --whitespace=error-all -- "$patch_root/$entry" \
    || pinned_ge_fail "FFmpeg crypto-build patch is absent from prepared source" \
    || return 1
  makefile_blob="$(pinned_ge_git -C "$source" \
    hash-object --no-filters -- Makefile.in)"
  [[ "$makefile_blob" == "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" ]] \
    || pinned_ge_fail "FFmpeg crypto-enabled Makefile differs from its pin" \
    || return 1
  enable_count="$(rg -c \
    '^[[:space:]]*--enable-protocol=crypto[[:space:]]*\\$' \
    "$source/Makefile.in" || true)"
  [[ "$enable_count" == 1 ]] \
    || pinned_ge_fail "FFmpeg crypto protocol is not enabled exactly once" || return 1

  awk -F '\t' '
    BEGIN {
      unique["ffmpeg_crypto_build_audit_version"] = 1
      unique["source_commit"] = 1
      unique["patch_origin"] = 1
      unique["patch_path"] = 1
      unique["patch_sha256"] = 1
      unique["series_sha256"] = 1
      unique["series_digest"] = 1
      unique["touched_path"] = 1
      unique["final_makefile_blob"] = 1
      unique["crypto_enable_count"] = 1
      unique["application"] = 1
    }
    {
      if (!($1 in unique) || NF != 2 || seen[$1]++) bad = 1
    }
    END {
      for (key in unique) if (seen[key] != 1) bad = 1
      exit bad
    }
  ' "$audit" \
    || pinned_ge_fail "FFmpeg crypto-build audit has an unexpected schema" \
    || return 1
  [[ "$(pinned_ge_state_value "$audit" ffmpeg_crypto_build_audit_version)" == 1 \
      && "$(pinned_ge_state_value "$audit" source_commit)" == "$PIN_SOURCE_COMMIT" \
      && "$(pinned_ge_state_value "$audit" patch_origin)" == "$origin" \
      && "$(pinned_ge_state_value "$audit" patch_path)" \
        == "patches/ffmpeg-build/$entry" \
      && "$(pinned_ge_state_value "$audit" patch_sha256)" == "$patch_sha" \
      && "$(pinned_ge_state_value "$audit" series_sha256)" \
        == "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256" \
      && "$(pinned_ge_state_value "$audit" series_digest)" == "$series_digest" \
      && "$(pinned_ge_state_value "$audit" touched_path)" == Makefile.in \
      && "$(pinned_ge_state_value "$audit" final_makefile_blob)" \
        == "$makefile_blob" \
      && "$(pinned_ge_state_value "$audit" crypto_enable_count)" == 1 \
      && "$(pinned_ge_state_value "$audit" application)" == passed ]] \
    || pinned_ge_fail "FFmpeg crypto-build audit facts are inconsistent" || return 1

  [[ "$(pinned_ge_state_value "$state_file" ffmpeg_crypto_build_origin)" \
        == "$origin" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_crypto_build_patch_sha256)" == "$patch_sha" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_crypto_build_series_sha256)" \
        == "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_crypto_build_series_digest)" == "$series_digest" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_crypto_build_audit_sha256)" == "$(pinned_ge_sha256 "$audit")" \
      && "$(pinned_ge_state_value "$state_file" \
        ffmpeg_crypto_build_makefile_blob)" == "$makefile_blob" ]] \
    || pinned_ge_fail "prepared-state FFmpeg crypto-build provenance is inconsistent" \
    || return 1
)

pinned_ge_patch_overlap_manifest_path() {
  local manifest

  [[ "${PINNED_GE_HAS_PATCH_OVERLAP:-0}" == 1 ]] \
    || pinned_ge_fail "selected pin has no reviewed GE patch-overlap manifest" \
    || return 1
  manifest="$(realpath -e -- \
    "$PINNED_GE_ROOT/$PIN_GE_PATCH_OVERLAP_MANIFEST")" \
    || pinned_ge_fail "GE patch-overlap manifest is absent" || return 1
  [[ "$manifest" == "$PINNED_GE_ROOT/config/"* \
      && -f "$manifest" && ! -L "$manifest" ]] \
    || pinned_ge_fail \
      "GE patch-overlap manifest is not a regular project config file" \
    || return 1
  [[ "$(pinned_ge_sha256 "$manifest")" \
      == "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256" ]] \
    || pinned_ge_fail "GE patch-overlap manifest differs from its pinned digest" \
    || return 1
  printf '%s\n' "$manifest"
}

pinned_ge_patch_overlap_records() {
  local manifest="$1"
  local kind field1 field2 field3 field4 extra line_number=0
  local version_count=0 policy_count=0 source_count=0 component_path_count=0
  local component_commit_count=0 parent_commit_count=0 target_path_count=0
  local parent_blob_count=0 effective_blob_count=0 effective_sha_count=0
  local declared_base_count=0 declared_intermediate_count=0
  local declared_target_count=0 declared_sha_count=0
  local residual_additions_count=0 residual_deletions_count=0
  local residual_diff_count=0 residual_class_count=0 residual_disposition_count=0
  local skip_count=0 skip_records=""

  [[ -f "$manifest" && ! -L "$manifest" ]] \
    || pinned_ge_fail "GE patch-overlap manifest is absent or unsafe: $manifest" \
    || return 1
  while IFS=$'\t' read -r kind field1 field2 field3 field4 extra \
      || [[ -n "$kind$field1$field2$field3$field4$extra" ]]; do
    line_number=$((line_number + 1))
    [[ -z "$extra" ]] \
      || pinned_ge_fail \
        "GE patch-overlap manifest line $line_number has extra fields" \
      || return 1
    case "$kind" in
      ge_patch_overlap_manifest_version)
        [[ "$field1" == 1 && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "invalid GE patch-overlap manifest version" \
          || return 1
        version_count=$((version_count + 1))
        ;;
      policy)
        [[ "$field1" == stock-effective-exact-skip \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap policy is unreviewed" \
          || return 1
        policy_count=$((policy_count + 1))
        ;;
      source_commit)
        [[ "$field1" == "$PIN_SOURCE_COMMIT" && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap manifest is pinned to another source" \
          || return 1
        source_count=$((source_count + 1))
        ;;
      component_path)
        [[ "$field1" == protonfixes && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap component is not protonfixes" \
          || return 1
        component_path_count=$((component_path_count + 1))
        ;;
      component_commit)
        [[ "$field1" == 8a3be121ece070ff8b043e46a502ee25e48a5f5b \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap component commit is unreviewed" \
          || return 1
        component_commit_count=$((component_commit_count + 1))
        ;;
      component_parent_commit)
        [[ "$field1" == c43c368d83120cc1bcc1f1df52c094f75af5744c \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap parent commit is unreviewed" \
          || return 1
        parent_commit_count=$((parent_commit_count + 1))
        ;;
      target_path)
        [[ "$field1" == upscalers.py && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap target path is unreviewed" \
          || return 1
        target_path_count=$((target_path_count + 1))
        ;;
      component_parent_target_blob)
        [[ "$field1" == 0e381338f9d882ebf54c80d1294a6f66508e81ad \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap parent blob is unreviewed" \
          || return 1
        parent_blob_count=$((parent_blob_count + 1))
        ;;
      effective_target_blob)
        [[ "$field1" == f45515c6ca43b072800e9320ab64facffbf5d8cb \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap effective blob is unreviewed" \
          || return 1
        effective_blob_count=$((effective_blob_count + 1))
        ;;
      effective_target_sha256)
        [[ "$field1" \
              == 3380cea841b3a52de9fdfe151631e1c24b7ec9707d7d77fb0eea68d42aa6140c \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap effective SHA-256 is unreviewed" \
          || return 1
        effective_sha_count=$((effective_sha_count + 1))
        ;;
      declared_series_base_blob)
        [[ "$field1" == ab17ebd80e1ca486e5f68f5a2893a55f58e51628 \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap declared base blob is unreviewed" \
          || return 1
        declared_base_count=$((declared_base_count + 1))
        ;;
      declared_series_intermediate_blob)
        [[ "$field1" == 06f551ae5c8786f864840997f3c45ad1d4dd3664 \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail \
            "GE patch-overlap declared intermediate blob is unreviewed" \
          || return 1
        declared_intermediate_count=$((declared_intermediate_count + 1))
        ;;
      declared_series_target_blob)
        [[ "$field1" == 9109c992ffa2471f894c5ec96c0d9c000332f089 \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap declared target blob is unreviewed" \
          || return 1
        declared_target_count=$((declared_target_count + 1))
        ;;
      declared_series_target_sha256)
        [[ "$field1" \
              == 76a775782ce00a12559606fbfea1a9fb38811a702f8ab36b8890de3d63c1a5bf \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap declared target SHA-256 is unreviewed" \
          || return 1
        declared_sha_count=$((declared_sha_count + 1))
        ;;
      residual_additions)
        [[ "$field1" == 11 && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap residual additions are unreviewed" \
          || return 1
        residual_additions_count=$((residual_additions_count + 1))
        ;;
      residual_deletions)
        [[ "$field1" == 11 && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap residual deletions are unreviewed" \
          || return 1
        residual_deletions_count=$((residual_deletions_count + 1))
        ;;
      residual_diff_sha256)
        [[ "$field1" \
              == 7470f008c5aab82db3c31dabfeb702faf4e8f60e31c47fcbf622a58e7ed14f5d \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap residual diff is unreviewed" \
          || return 1
        residual_diff_count=$((residual_diff_count + 1))
        ;;
      residual_class)
        [[ "$field1" == exception-logging-only \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap residual class is unreviewed" \
          || return 1
        residual_class_count=$((residual_class_count + 1))
        ;;
      residual_disposition)
        [[ "$field1" == logging-static-typing-only-not-applied \
            && -z "$field2$field3$field4" ]] \
          || pinned_ge_fail "GE patch-overlap residual disposition is unreviewed" \
          || return 1
        residual_disposition_count=$((residual_disposition_count + 1))
        ;;
      skip)
        [[ "$field1" == patches/protonfixes/*.patch \
            && "$field2" =~ ^[0-9a-f]{64}$ \
            && "$field3" =~ ^[0-9]+$ && "$field4" =~ ^[0-9]+$ ]] \
          || pinned_ge_fail \
            "invalid GE patch-overlap skip record on line $line_number" \
          || return 1
        skip_records+="${field1}"$'\t'"${field2}"$'\t'"${field3}"$'\t'"${field4}"$'\n'
        skip_count=$((skip_count + 1))
        ;;
      *)
        pinned_ge_fail \
          "unknown GE patch-overlap manifest key on line $line_number: $kind"
        return 1
        ;;
    esac
  done <"$manifest"

  [[ "$PIN_SOURCE_COMMIT" == bb1caad333b08cf87d49d0f794a538502d992eae \
      && "$version_count" == 1 && "$policy_count" == 1 \
      && "$source_count" == 1 \
      && "$component_path_count" == 1 && "$component_commit_count" == 1 \
      && "$parent_commit_count" == 1 && "$target_path_count" == 1 \
      && "$parent_blob_count" == 1 && "$effective_blob_count" == 1 \
      && "$effective_sha_count" == 1 && "$declared_base_count" == 1 \
      && "$declared_intermediate_count" == 1 \
      && "$declared_target_count" == 1 && "$declared_sha_count" == 1 \
      && "$residual_additions_count" == 1 \
      && "$residual_deletions_count" == 1 \
      && "$residual_diff_count" == 1 && "$residual_class_count" == 1 \
      && "$residual_disposition_count" == 1 \
      && "$skip_count" == 2 ]] \
    || pinned_ge_fail \
      "GE patch-overlap manifest is incomplete, duplicated, or outside bb1caad3" \
    || return 1
  [[ "${skip_records%$'\n'}" == \
      $'patches/protonfixes/0001-upscalers-add-optiscaler-downloader.patch\t1a72d6f3d97e890719bf47f9d3b979392b2773d1df25bb025c3ce3d4d454effd\t206\t3\npatches/protonfixes/0002-upscalers-control-ffx4-version-through-fsr4-version-.patch\t7905d65d2d825fe4caaf9f525f7826ba29b83a3d15e0322ec07bcd5d80edc827\t8\t4' ]] \
    || pinned_ge_fail \
      "GE patch-overlap manifest is not the exact reviewed two-patch exception" \
    || return 1
  printf '%s' "$skip_records"
}

pinned_ge_require_prepared_patch_overlap_provenance() (
  set -euo pipefail

  local source="$1" state_dir="$2"
  local state_file audit manifest component target parent patch_records
  local patch_path patch_sha additions deletions numstat actual_add actual_del
  local actual_path extra manifest_skip audit_skip expected_patch_paths
  local actual_patch_paths

  [[ "${PINNED_GE_HAS_PATCH_OVERLAP:-0}" == 1 ]] \
    || pinned_ge_fail "patch-overlap provenance requested for a pin without it" \
    || return 1
  source="$(realpath -e -- "$source")"
  state_dir="$(realpath -e -- "$state_dir")"
  state_file="$state_dir/prepared-source.tsv"
  audit="$state_dir/ge-patch-overlap-audit.tsv"
  manifest="$(pinned_ge_patch_overlap_manifest_path)"
  patch_records="$(pinned_ge_patch_overlap_records "$manifest")"
  component="$source/$(pinned_ge_state_value "$manifest" component_path)"
  target="$(pinned_ge_state_value "$manifest" target_path)"
  parent="$(pinned_ge_state_value "$manifest" component_parent_commit)"
  expected_patch_paths="$(printf '%s\n' "$patch_records" | cut -f1)"
  actual_patch_paths="$(
    find "$source/patches/protonfixes" -maxdepth 1 -type f \
      -name '*.patch' -print \
      | sed "s|^$source/||" \
      | LC_ALL=C sort
  )"

  [[ -f "$state_file" && ! -L "$state_file" \
      && -f "$audit" && ! -L "$audit" ]] \
    || pinned_ge_fail "prepared GE patch-overlap evidence is absent or unsafe" \
    || return 1
  [[ "$actual_patch_paths" == "$expected_patch_paths" ]] \
    || pinned_ge_fail \
      "prepared GE protonfixes patch directory differs from the reviewed pair" \
    || return 1
  [[ "$(pinned_ge_git -C "$source" rev-parse HEAD)" == "$PIN_SOURCE_COMMIT" \
      && "$(pinned_ge_git -C "$source" rev-parse HEAD:protonfixes)" \
        == "$(pinned_ge_state_value "$manifest" component_commit)" ]] \
    || pinned_ge_fail "prepared root no longer has the reviewed protonfixes gitlink" \
    || return 1
  [[ "$(pinned_ge_git -C "$component" rev-parse HEAD)" \
        == "$(pinned_ge_state_value "$manifest" component_commit)" \
      && "$(pinned_ge_git -C "$component" rev-parse HEAD^)" == "$parent" \
      && "$(pinned_ge_git -C "$component" rev-parse "HEAD:$target")" \
        == "$(pinned_ge_state_value "$manifest" effective_target_blob)" \
      && "$(pinned_ge_git -C "$component" rev-parse "$parent:$target")" \
        == "$(pinned_ge_state_value "$manifest" component_parent_target_blob)" \
      && "$(pinned_ge_git -C "$component" hash-object --no-filters -- "$target")" \
        == "$(pinned_ge_state_value "$manifest" effective_target_blob)" \
      && "$(pinned_ge_sha256 "$component/$target")" \
        == "$(pinned_ge_state_value "$manifest" effective_target_sha256)" \
      && -z "$(pinned_ge_git -C "$component" \
        status --porcelain --untracked-files=all)" ]] \
    || pinned_ge_fail "prepared protonfixes differs from the reviewed overlap result" \
    || return 1
  [[ "$(pinned_ge_git -C "$component" diff --name-only "$parent..HEAD")" \
      == "$target" ]] \
    || pinned_ge_fail "reviewed protonfixes integration commit changed extra paths" \
    || return 1

  while IFS=$'\t' read -r patch_path patch_sha additions deletions; do
    [[ -f "$source/$patch_path" && ! -L "$source/$patch_path" \
        && "$(pinned_ge_sha256 "$source/$patch_path")" == "$patch_sha" ]] \
      || pinned_ge_fail "reviewed GE overlap patch changed: $patch_path" \
      || return 1
    numstat="$(pinned_ge_git -C "$source" apply --numstat -- \
      "$source/$patch_path")" \
      || pinned_ge_fail "could not parse reviewed GE overlap patch: $patch_path" \
      || return 1
    IFS=$'\t' read -r actual_add actual_del actual_path extra <<<"$numstat"
    [[ "$actual_add" == "$additions" && "$actual_del" == "$deletions" \
        && "$actual_path" == "$target" && -z "$extra" \
        && "$(wc -l <<<"$numstat")" == 1 ]] \
      || pinned_ge_fail "reviewed GE overlap patch scope changed: $patch_path" \
      || return 1
  done <<<"$patch_records"

  awk -F '\t' '
    BEGIN {
      unique["ge_patch_overlap_audit_version"] = 1
      unique["policy"] = 1
      unique["source_commit"] = 1
      unique["manifest_sha256"] = 1
      unique["component_path"] = 1
      unique["component_commit"] = 1
      unique["component_parent_commit"] = 1
      unique["target_path"] = 1
      unique["component_parent_target_blob"] = 1
      unique["effective_target_blob"] = 1
      unique["effective_target_sha256"] = 1
      unique["declared_series_base_blob"] = 1
      unique["declared_series_intermediate_blob"] = 1
      unique["declared_series_target_blob"] = 1
      unique["declared_series_target_sha256"] = 1
      unique["component_delta_paths"] = 1
      unique["residual_class"] = 1
      unique["residual_additions"] = 1
      unique["residual_deletions"] = 1
      unique["residual_diff_sha256"] = 1
      unique["declared_series_reconstruction"] = 1
      unique["python_parse"] = 1
      unique["source_change"] = 1
      unique["overlap_validation"] = 1
    }
    $1 == "skipped_patch" {
      if (NF != 6) bad = 1
      skipped++
      next
    }
    {
      if (!($1 in unique) || NF != 2 || seen[$1]++) bad = 1
    }
    END {
      if (skipped != 2) bad = 1
      for (key in unique) if (seen[key] != 1) bad = 1
      exit bad
    }
  ' "$audit" \
    || pinned_ge_fail "GE patch-overlap audit has an unexpected schema" \
    || return 1
  [[ "$(pinned_ge_state_value "$audit" ge_patch_overlap_audit_version)" == 1 \
      && "$(pinned_ge_state_value "$audit" policy)" \
        == stock-effective-exact-skip \
      && "$(pinned_ge_state_value "$audit" source_commit)" == "$PIN_SOURCE_COMMIT" \
      && "$(pinned_ge_state_value "$audit" manifest_sha256)" \
        == "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256" \
      && "$(pinned_ge_state_value "$audit" component_path)" == protonfixes \
      && "$(pinned_ge_state_value "$audit" component_commit)" \
        == "$(pinned_ge_state_value "$manifest" component_commit)" \
      && "$(pinned_ge_state_value "$audit" component_parent_commit)" == "$parent" \
      && "$(pinned_ge_state_value "$audit" target_path)" == "$target" \
      && "$(pinned_ge_state_value "$audit" component_parent_target_blob)" \
        == "$(pinned_ge_state_value "$manifest" component_parent_target_blob)" \
      && "$(pinned_ge_state_value "$audit" effective_target_blob)" \
        == "$(pinned_ge_state_value "$manifest" effective_target_blob)" \
      && "$(pinned_ge_state_value "$audit" effective_target_sha256)" \
        == "$(pinned_ge_state_value "$manifest" effective_target_sha256)" \
      && "$(pinned_ge_state_value "$audit" declared_series_base_blob)" \
        == "$(pinned_ge_state_value "$manifest" declared_series_base_blob)" \
      && "$(pinned_ge_state_value "$audit" declared_series_intermediate_blob)" \
        == "$(pinned_ge_state_value "$manifest" \
          declared_series_intermediate_blob)" \
      && "$(pinned_ge_state_value "$audit" declared_series_target_blob)" \
        == "$(pinned_ge_state_value "$manifest" declared_series_target_blob)" \
      && "$(pinned_ge_state_value "$audit" declared_series_target_sha256)" \
        == "$(pinned_ge_state_value "$manifest" \
          declared_series_target_sha256)" \
      && "$(pinned_ge_state_value "$audit" component_delta_paths)" == "$target" \
      && "$(pinned_ge_state_value "$audit" residual_class)" \
        == exception-logging-only \
      && "$(pinned_ge_state_value "$audit" residual_additions)" \
        == "$(pinned_ge_state_value "$manifest" residual_additions)" \
      && "$(pinned_ge_state_value "$audit" residual_deletions)" \
        == "$(pinned_ge_state_value "$manifest" residual_deletions)" \
      && "$(pinned_ge_state_value "$audit" residual_diff_sha256)" \
        == "$(pinned_ge_state_value "$manifest" residual_diff_sha256)" \
      && "$(pinned_ge_state_value "$audit" declared_series_reconstruction)" \
        == passed \
      && "$(pinned_ge_state_value "$audit" python_parse)" == passed \
      && "$(pinned_ge_state_value "$audit" source_change)" == none \
      && "$(pinned_ge_state_value "$audit" overlap_validation)" == passed ]] \
    || pinned_ge_fail "GE patch-overlap audit facts are inconsistent" \
    || return 1
  manifest_skip="$(printf '%s\n' "$patch_records" \
    | awk -F '\t' '{print $1 "\t" $2 "\t" $3 "\t" $4 "\tstock-effective-skip"}')"
  audit_skip="$(awk -F '\t' '$1 == "skipped_patch" {
    print $2 "\t" $3 "\t" $4 "\t" $5 "\t" $6
  }' "$audit")"
  [[ "$audit_skip" == "$manifest_skip" \
      && "$(pinned_ge_state_value "$state_file" ge_patch_overlap_mode)" \
        == stock-effective-exact-skip \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_manifest_sha256)" \
        == "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256" \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_audit_sha256)" == "$(pinned_ge_sha256 "$audit")" \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_component_commit)" \
        == "$(pinned_ge_state_value "$manifest" component_commit)" \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_target_blob)" \
        == "$(pinned_ge_state_value "$manifest" effective_target_blob)" \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_declared_target_blob)" \
        == "$(pinned_ge_state_value "$manifest" declared_series_target_blob)" \
      && "$(pinned_ge_state_value "$state_file" \
        ge_patch_overlap_residual_diff_sha256)" \
        == "$(pinned_ge_state_value "$manifest" residual_diff_sha256)" ]] \
    || pinned_ge_fail "prepared-state GE patch-overlap provenance is inconsistent" \
    || return 1
)

pinned_ge_build_contrib_manifest_path() {
  local manifest

  manifest="$(realpath -e -- \
    "$PINNED_GE_ROOT/$PIN_BUILD_CONTRIB_MANIFEST")" \
    || pinned_ge_fail "build-contrib manifest is absent" || return 1
  [[ "$manifest" == "$PINNED_GE_ROOT/config/"* \
      && -f "$manifest" && ! -L "$manifest" ]] \
    || pinned_ge_fail \
      "build-contrib manifest is not a regular project config file" \
    || return 1
  [[ "$(pinned_ge_sha256 "$manifest")" == "$PIN_BUILD_CONTRIB_MANIFEST_SHA256" ]] \
    || pinned_ge_fail "build-contrib manifest differs from its pinned digest" \
    || return 1
  printf '%s\n' "$manifest"
}

pinned_ge_build_contrib_records() {
  local manifest="$1"
  local include_urls="${2:-0}"
  local key value mode size sha extra line_number=0
  local version="" version_count=0 source_count=0 entry_count=0 fetch_count=0
  local path
  local -a paths=()
  declare -A seen=() entry_mode=() entry_size=() entry_sha=() fetch_url=()

  [[ -f "$manifest" && ! -L "$manifest" ]] \
    || pinned_ge_fail "build-contrib manifest is absent or unsafe: $manifest" \
    || return 1
  [[ "$include_urls" == 0 || "$include_urls" == 1 ]] \
    || pinned_ge_fail "invalid build-contrib record mode" || return 1
  while IFS=$'\t' read -r key value mode size sha extra \
      || [[ -n "$key$value$mode$size$sha$extra" ]]; do
    line_number=$((line_number + 1))
    [[ -n "$key$value$mode$size$sha$extra" ]] || continue
    if [[ "$key" == \#* ]]; then
      [[ -z "$value$mode$size$sha$extra" ]] \
        || pinned_ge_fail \
          "build-contrib manifest comment on line $line_number contains tabs" \
        || return 1
      continue
    fi
    [[ -z "$extra" ]] \
      || pinned_ge_fail "build-contrib manifest line $line_number has extra fields" \
      || return 1
    case "$key" in
      build_contrib_manifest_version)
        [[ -z "$mode$size$sha" && ( "$value" == 2 || "$value" == 3 ) ]] \
          || pinned_ge_fail "invalid build-contrib manifest version" || return 1
        version="$value"
        version_count=$((version_count + 1))
        ;;
      source_commit)
        [[ -z "$mode$size$sha" && "$value" == "$PIN_SOURCE_COMMIT" ]] \
          || pinned_ge_fail "build-contrib manifest is pinned to another source" \
          || return 1
        source_count=$((source_count + 1))
        ;;
      entry)
        [[ -n "$value" && "$mode" == 0644 && "$size" =~ ^[1-9][0-9]*$ \
            && "$sha" =~ ^[0-9a-f]{64}$ ]] \
          || pinned_ge_fail "invalid build-contrib entry on line $line_number" \
          || return 1
        case "$value" in
          /*|../*|*/../*|*/..|..|*[$'\n\t']*)
            pinned_ge_fail "unsafe build-contrib path: $value" || return 1 ;;
          contrib/*) ;;
          *) pinned_ge_fail "build-contrib path is outside contrib/: $value" \
            || return 1 ;;
        esac
        [[ -z "${seen[$value]+present}" ]] \
          || pinned_ge_fail "duplicate build-contrib path: $value" || return 1
        seen["$value"]=1
        paths+=("$value")
        entry_mode["$value"]="$mode"
        entry_size["$value"]="$size"
        entry_sha["$value"]="$sha"
        entry_count=$((entry_count + 1))
        ;;
      fetch)
        [[ -n "$value" && "$mode" =~ ^https://[^[:space:]]+$ \
            && -z "$size$sha" ]] \
          || pinned_ge_fail \
            "invalid build-contrib fetch record on line $line_number" \
          || return 1
        case "$value" in
          /*|../*|*/../*|*/..|..|*[$'\n\t']*)
            pinned_ge_fail "unsafe build-contrib fetch path: $value" \
              || return 1 ;;
          contrib/*) ;;
          *) pinned_ge_fail \
              "build-contrib fetch path is outside contrib/: $value" \
              || return 1 ;;
        esac
        [[ -z "${fetch_url[$value]+present}" ]] \
          || pinned_ge_fail "duplicate build-contrib fetch path: $value" \
          || return 1
        fetch_url["$value"]="$mode"
        fetch_count=$((fetch_count + 1))
        ;;
      *)
        pinned_ge_fail "unknown build-contrib manifest key on line $line_number: $key"
        return 1
        ;;
    esac
  done <"$manifest"
  [[ "$version_count" == 1 && "$source_count" == 1 && "$entry_count" -gt 0 ]] \
    || pinned_ge_fail "build-contrib manifest has incomplete or duplicate metadata" \
    || return 1
  case "$version" in
    2)
      [[ "$fetch_count" == 0 ]] \
        || pinned_ge_fail \
          "build-contrib manifest version 2 cannot contain fetch records" \
        || return 1
      [[ "$include_urls" == 0 ]] \
        || pinned_ge_fail \
          "build-contrib manifest version 2 has no prefetch URLs" \
        || return 1
      ;;
    3)
      [[ "$fetch_count" == "$entry_count" ]] \
        || pinned_ge_fail \
          "build-contrib manifest version 3 requires one fetch record per entry" \
        || return 1
      for path in "${!fetch_url[@]}"; do
        [[ -n "${seen[$path]+present}" ]] \
          || pinned_ge_fail \
            "build-contrib fetch record has no matching entry: $path" \
          || return 1
      done
      ;;
    *)
      pinned_ge_fail "invalid build-contrib manifest version" || return 1
      ;;
  esac

  for path in "${paths[@]}"; do
    if [[ "$include_urls" == 1 ]]; then
      printf '%s\t%s\t%s\t%s\t%s\n' \
        "$path" "${entry_mode[$path]}" "${entry_size[$path]}" \
        "${entry_sha[$path]}" "${fetch_url[$path]}"
    else
      printf '%s\t%s\t%s\t%s\n' \
        "$path" "${entry_mode[$path]}" "${entry_size[$path]}" \
        "${entry_sha[$path]}"
    fi
  done
}

pinned_ge_build_contrib_entries() {
  pinned_ge_build_contrib_records "$1" 0
}

pinned_ge_build_contrib_fetch_entries() {
  pinned_ge_build_contrib_records "$1" 1
}

pinned_ge_require_complete_build_contrib() {
  local repo="$1"
  local manifest="$2"
  local entries_text entry path expected_mode expected_size expected_sha
  local actual_mode actual_size actual_sha
  local -a entries

  repo="$(realpath -e -- "$repo")" || return 1
  entries_text="$(pinned_ge_build_contrib_entries "$manifest")" || return 1
  entries=()
  mapfile -t entries <<<"$entries_text"
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r path expected_mode expected_size expected_sha <<<"$entry"
    [[ -f "$repo/$path" && ! -L "$repo/$path" ]] \
      || pinned_ge_fail \
        "complete regular build-contrib cache is required: $path" \
      || return 1
    actual_mode="$(stat -c '%a' -- "$repo/$path")"
    actual_size="$(stat -c '%s' -- "$repo/$path")"
    actual_sha="$(pinned_ge_sha256 "$repo/$path")"
    [[ "0$actual_mode" == "$expected_mode" \
        && "$actual_size" == "$expected_size" \
        && "$actual_sha" == "$expected_sha" ]] \
      || pinned_ge_fail "retained build-contrib cache entry changed: $path" \
      || return 1
  done
}

pinned_ge_emit_expected_build_makefile() {
  local source="$1"

  pinned_ge_require_safe_absolute_path "GE source path" "$source" || return 1
  printf '# Generated by: %s/configure.sh --build-name=%s --target-arch=x86_64 --container-engine=podman --proton-sdk-image=%s\n\n' \
    "$source" "$PIN_BUILD_NAME" "$PIN_STEAMRT_IMAGE"
  printf 'SRCDIR     := %s\n' "$source"
  printf 'BUILD_NAME := %s\n' "$PIN_BUILD_NAME"
  printf 'TARGET_ARCH := x86_64\n'
  printf 'INTERNAL_TOOL_NAME := %s\n' "$PIN_BUILD_NAME"
  printf 'STEAMRT_IMAGE := %s\n' "$PIN_STEAMRT_IMAGE"
  printf 'ROOTLESS_CONTAINER := 1\n'
  printf 'CONTAINER_ENGINE := podman\n'
  printf 'ENABLE_CCACHE := 1\n\n'
  printf 'include $(SRCDIR)/Makefile.in\n'
}

pinned_ge_validate_configured_build_makefile() (
  set -euo pipefail

  local source="$1"
  local build="$2"
  local makefile expected

  source="$(realpath -e -- "$source")"
  build="$(realpath -m -- "$build")"
  makefile="$build/Makefile"
  [[ -f "$makefile" && ! -L "$makefile" ]] \
    || pinned_ge_fail "configured build Makefile is absent or unsafe: $makefile" \
    || return 1
  expected="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-makefile.XXXXXX")"
  trap 'rm -f -- "$expected"' EXIT
  pinned_ge_emit_expected_build_makefile "$source" >"$expected"
  cmp -s -- "$expected" "$makefile" \
    || pinned_ge_fail \
      "configured build Makefile does not exactly match the pinned configure arguments" \
    || return 1
  printf '%s\t%s\n' "$(stat -c '%s' -- "$makefile")" \
    "$(pinned_ge_sha256 "$makefile")"
)

pinned_ge_write_configure_state() (
  set -euo pipefail

  local source="$1" build="$2" state_dir="$3" invocation="$4" origin="$5"
  local cache_home="$6" adoption_anchor_sha="${7:-none}"
  local configure_state prepared_state contrib_manifest root_digest makefile_record
  local makefile_size makefile_sha temp

  source="$(realpath -e -- "$source")"
  build="$(realpath -e -- "$build")"
  state_dir="$(realpath -e -- "$state_dir")"
  invocation="$(realpath -e -- "$invocation")"
  cache_home="$(realpath -e -- "$cache_home")"
  pinned_ge_require_safe_absolute_path "GE source path" "$source"
  pinned_ge_require_safe_absolute_path "GE build path" "$build"
  pinned_ge_require_safe_absolute_path "GE state path" "$state_dir"
  pinned_ge_require_safe_absolute_path "GE cache home" "$cache_home"
  configure_state="$state_dir/configure-state.tsv"
  prepared_state="$state_dir/prepared-source.tsv"
  case "$origin" in
    fresh-configure)
      [[ "$adoption_anchor_sha" == none ]] \
        || pinned_ge_fail "fresh configure state cannot carry an adoption anchor" \
        || return 1
      ;;
    adopted-retained-vodfix2)
      [[ "$adoption_anchor_sha" == "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" ]] \
        || pinned_ge_fail "retained configure state lacks its exact adoption anchor" \
        || return 1
      pinned_ge_require_retained_resume_anchors \
        "$source" "$build" "$state_dir" "$cache_home" || return 1
      ;;
    *) pinned_ge_fail "invalid configure-state origin: $origin" || return 1 ;;
  esac
  [[ -f "$prepared_state" && ! -L "$prepared_state" \
      && -f "$invocation" && ! -L "$invocation" ]] \
    || pinned_ge_fail "configure-state inputs are absent or unsafe" || return 1
  [[ ! -e "$configure_state" && ! -L "$configure_state" ]] \
    || pinned_ge_fail "refusing to replace existing configure state: $configure_state" \
    || return 1

  contrib_manifest="$(pinned_ge_build_contrib_manifest_path)"
  root_digest="$(pinned_ge_repo_state_digest "$source" "$contrib_manifest")"
  [[ "$root_digest" == \
      "$(pinned_ge_state_value "$prepared_state" root_repo_state_sha256)" ]] \
    || pinned_ge_fail "source differs beyond the validated build-contrib cache" \
    || return 1
  makefile_record="$(pinned_ge_validate_configured_build_makefile "$source" "$build")"
  IFS=$'\t' read -r makefile_size makefile_sha <<<"$makefile_record"

  temp="$(mktemp "$state_dir/.configure-state.tsv.XXXXXX")"
  trap 'rm -f -- "$temp"' EXIT
  {
    printf 'configure_state_version\t3\n'
    printf 'origin\t%s\n' "$origin"
    printf 'pin_id\t%s\n' "$PIN_ID"
    printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
    printf 'source_path\t%s\n' "$source"
    printf 'build_path\t%s\n' "$build"
    printf 'cache_home\t%s\n' "$cache_home"
    printf 'build_name\t%s\n' "$PIN_BUILD_NAME"
    printf 'target_arch\tx86_64\n'
    printf 'container_engine\tpodman\n'
    printf 'steamrt_image\t%s\n' "$PIN_STEAMRT_IMAGE"
    printf 'steamrt_image_id\t%s\n' "$PIN_STEAMRT_IMAGE_ID"
    printf 'configure_sha256\t%s\n' "$PIN_CONFIGURE_SHA256"
    printf 'makefile_in_sha256\t%s\n' "$PIN_MAKEFILE_IN_SHA256"
    printf 'generated_makefile_size\t%s\n' "$makefile_size"
    printf 'generated_makefile_sha256\t%s\n' "$makefile_sha"
    printf 'prepared_state_sha256\t%s\n' "$(pinned_ge_sha256 "$prepared_state")"
    printf 'build_invocation_sha256\t%s\n' "$(pinned_ge_sha256 "$invocation")"
    printf 'prepared_root_repo_state_sha256\t%s\n' "$root_digest"
    printf 'build_contrib_manifest_sha256\t%s\n' \
      "$PIN_BUILD_CONTRIB_MANIFEST_SHA256"
    printf 'adoption_anchor_manifest_sha256\t%s\n' "$adoption_anchor_sha"
  } >"$temp"
  chmod 0444 "$temp"
  mv -- "$temp" "$configure_state"
  trap - EXIT
  printf '%s\n' "$configure_state"
)

pinned_ge_require_configure_state() {
  local source="$1" build="$2" state_dir="$3" invocation="$4" cache_home="$5"
  local configure_state prepared_state origin makefile_record makefile_size makefile_sha
  local adoption_anchor_sha contrib_manifest

  source="$(realpath -e -- "$source")" || return 1
  build="$(realpath -e -- "$build")" || return 1
  state_dir="$(realpath -e -- "$state_dir")" || return 1
  invocation="$(realpath -e -- "$invocation")" || return 1
  cache_home="$(realpath -e -- "$cache_home")" || return 1
  pinned_ge_require_safe_absolute_path "GE source path" "$source" || return 1
  pinned_ge_require_safe_absolute_path "GE build path" "$build" || return 1
  pinned_ge_require_safe_absolute_path "GE state path" "$state_dir" || return 1
  pinned_ge_require_safe_absolute_path "GE cache home" "$cache_home" || return 1
  configure_state="$state_dir/configure-state.tsv"
  prepared_state="$state_dir/prepared-source.tsv"
  [[ -f "$configure_state" && ! -L "$configure_state" ]] \
    || pinned_ge_fail "verified configure state is absent: $configure_state" \
    || return 1
  origin="$(pinned_ge_state_value "$configure_state" origin)" || return 1
  adoption_anchor_sha="$(pinned_ge_state_value \
    "$configure_state" adoption_anchor_manifest_sha256)" || return 1
  case "$origin" in
    fresh-configure)
      [[ "$adoption_anchor_sha" == none ]] \
        || pinned_ge_fail "fresh configure state carries an adoption anchor" \
        || return 1
      ;;
    adopted-retained-vodfix2)
      [[ "$adoption_anchor_sha" == "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" ]] \
        || pinned_ge_fail "retained configure state has the wrong adoption anchor" \
        || return 1
      contrib_manifest="$(pinned_ge_build_contrib_manifest_path)" || return 1
      pinned_ge_require_complete_build_contrib "$source" "$contrib_manifest" \
        || return 1
      ;;
    *) pinned_ge_fail "configure-state origin is invalid: $origin" || return 1 ;;
  esac

  while IFS=$'\t' read -r key expected; do
    [[ "$(pinned_ge_state_value "$configure_state" "$key")" == "$expected" ]] \
      || pinned_ge_fail "configure-state $key differs from the selected build" \
      || return 1
  done <<EOF
configure_state_version	3
pin_id	$PIN_ID
source_commit	$PIN_SOURCE_COMMIT
source_path	$source
build_path	$build
cache_home	$cache_home
build_name	$PIN_BUILD_NAME
target_arch	x86_64
container_engine	podman
steamrt_image	$PIN_STEAMRT_IMAGE
steamrt_image_id	$PIN_STEAMRT_IMAGE_ID
configure_sha256	$PIN_CONFIGURE_SHA256
makefile_in_sha256	$PIN_MAKEFILE_IN_SHA256
prepared_state_sha256	$(pinned_ge_sha256 "$prepared_state")
build_invocation_sha256	$(pinned_ge_sha256 "$invocation")
prepared_root_repo_state_sha256	$(pinned_ge_state_value "$prepared_state" root_repo_state_sha256)
build_contrib_manifest_sha256	$PIN_BUILD_CONTRIB_MANIFEST_SHA256
adoption_anchor_manifest_sha256	$adoption_anchor_sha
EOF

  makefile_record="$(pinned_ge_validate_configured_build_makefile "$source" "$build")" \
    || return 1
  IFS=$'\t' read -r makefile_size makefile_sha <<<"$makefile_record"
  [[ "$(pinned_ge_state_value "$configure_state" generated_makefile_size)" \
      == "$makefile_size" \
      && "$(pinned_ge_state_value "$configure_state" generated_makefile_sha256)" \
      == "$makefile_sha" ]] \
      || pinned_ge_fail "configured Makefile differs from its verified fingerprint" \
    || return 1
}

pinned_ge_require_resume_invocation() {
  local source="$1" build="$2" state_dir="$3" cache_home="$4"
  local invocation="$5" prepared_state="$6" jobs="$7" make_jobs="$8"
  local ninja_jobs="$9" product="${10}"
  local host_shell key expected version invocation_sha
  local pin_schema="${PIN_SCHEMA_VERSION:-4}"
  local retained_source retained_build retained_state retained_cache

  source="$(realpath -e -- "$source")" || return 1
  build="$(realpath -e -- "$build")" || return 1
  state_dir="$(realpath -e -- "$state_dir")" || return 1
  cache_home="$(realpath -e -- "$cache_home")" || return 1
  pinned_ge_require_safe_absolute_path "GE source path" "$source" || return 1
  pinned_ge_require_safe_absolute_path "GE build path" "$build" || return 1
  pinned_ge_require_safe_absolute_path "GE state path" "$state_dir" || return 1
  pinned_ge_require_safe_absolute_path "GE cache home" "$cache_home" || return 1
  [[ -f "$invocation" && ! -L "$invocation" \
      && -f "$prepared_state" && ! -L "$prepared_state" ]] \
    || pinned_ge_fail "resume invocation or prepared state is absent or unsafe" \
    || return 1
  version="$(pinned_ge_state_value "$invocation" build_invocation_version)" \
    || return 1
  if [[ "$pin_schema" == 5 && "$version" != 6 ]]; then
    pinned_ge_fail \
      "schema-5 security builds cannot adopt or resume a legacy invocation"
    return 1
  fi
  if [[ "$pin_schema" == 4 && "$version" == 6 ]]; then
    pinned_ge_fail "legacy schema-4 builds cannot consume a schema-6 invocation"
    return 1
  fi
  case "$version" in
    5|6)
      while IFS=$'\t' read -r key expected; do
        [[ "$(pinned_ge_state_value "$invocation" "$key")" == "$expected" ]] \
          || pinned_ge_fail "resume invocation $key differs from this request" \
          || return 1
      done <<EOF
source_path	$source
build_path	$build
state_path	$state_dir
cache_home	$cache_home
EOF
      ;;
    3)
      invocation_sha="$(pinned_ge_sha256 "$invocation")"
      retained_source="$(realpath -e -- \
        "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_SOURCE")" || return 1
      retained_build="$(realpath -e -- "$PIN_RETAINED_RESUME_BUILD")" || return 1
      retained_state="$(realpath -e -- \
        "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_STATE")" || return 1
      retained_cache="$(pinned_ge_resolve_retained_resume_cache)" || return 1
      [[ "$invocation_sha" == "$PIN_RETAINED_RESUME_INVOCATION_SHA256" \
          && "$source" == "$retained_source" \
          && "$build" == "$retained_build" \
          && "$state_dir" == "$retained_state" \
          && "$cache_home" == "$retained_cache" ]] \
        || pinned_ge_fail \
          "legacy invocation schema is accepted only for the exact retained vodfix2 interruption" \
        || return 1
      ;;
    *)
      pinned_ge_fail "unsupported resume invocation schema: $version"
      return 1
      ;;
  esac
  while IFS=$'\t' read -r key expected; do
    [[ "$(pinned_ge_state_value "$invocation" "$key")" == "$expected" ]] \
      || pinned_ge_fail "resume invocation $key differs from this request" \
      || return 1
  done <<EOF
pin_id	$PIN_ID
source_commit	$PIN_SOURCE_COMMIT
build_name	$PIN_BUILD_NAME
package_release	0
jobs	$jobs
global_jobs	$jobs
make_jobs	$make_jobs
ninja_jobs	$ninja_jobs
scheduler_product	$product
scheduler_policy	make-times-ninja-at-most-global
recursive_make_parallelism	jobserver
cargo_jobs_env	$ninja_jobs
cmake_jobs_env	$ninja_jobs
nix_build_cores_env	$ninja_jobs
ge_pre_rtsp_diff_check_status	$(pinned_ge_state_value "$prepared_state" ge_pre_rtsp_diff_check_status)
ge_pre_rtsp_diff_check_sha256	$(pinned_ge_state_value "$prepared_state" ge_pre_rtsp_diff_check_sha256)
ge_media_cleanup_manifest_sha256	$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256
ge_media_cleanup_normalization_digest	$(pinned_ge_state_value "$prepared_state" ge_media_cleanup_normalization_digest)
ge_media_cleanup_audit_sha256	$(pinned_ge_state_value "$prepared_state" ge_media_cleanup_audit_sha256)
proton_launcher_patch_sha256	$(pinned_ge_state_value "$prepared_state" proton_launcher_patch_sha256)
rtsp_hook_order	$(pinned_ge_state_value "$prepared_state" rtsp_hook_order)
final_diff_check_baseline_match	passed
final_winegstreamer_external_references	0
without_nvidia_libs	1
without_vklayers	1
target	redist
steamrt_image	$PIN_STEAMRT_IMAGE
steam_install	disabled
publication	disabled
EOF
  if [[ "${PINNED_GE_HAS_PATCH_OVERLAP:-0}" == 1 ]]; then
    while IFS=$'\t' read -r key expected; do
      [[ "$(pinned_ge_state_value "$invocation" "$key")" == "$expected" ]] \
        || pinned_ge_fail "resume patch-overlap provenance differs for $key" \
        || return 1
    done <<EOF
ge_patch_overlap_mode	stock-effective-exact-skip
ge_patch_overlap_manifest_sha256	$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256
ge_patch_overlap_audit_sha256	$(pinned_ge_state_value "$prepared_state" ge_patch_overlap_audit_sha256)
ge_patch_overlap_component_commit	$(pinned_ge_state_value "$prepared_state" ge_patch_overlap_component_commit)
ge_patch_overlap_target_blob	$(pinned_ge_state_value "$prepared_state" ge_patch_overlap_target_blob)
ge_patch_overlap_declared_target_blob	$(pinned_ge_state_value "$prepared_state" ge_patch_overlap_declared_target_blob)
ge_patch_overlap_residual_diff_sha256	$(pinned_ge_state_value "$prepared_state" ge_patch_overlap_residual_diff_sha256)
EOF
  fi
  if [[ "$version" == 6 ]]; then
    while IFS=$'\t' read -r key expected; do
      [[ "$(pinned_ge_state_value "$invocation" "$key")" == "$expected" ]] \
        || pinned_ge_fail "resume invocation $key differs from this request" \
        || return 1
    done <<EOF
ffmpeg_security_fix_commits	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_fix_commits)
ffmpeg_security_series_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_series_sha256)
ffmpeg_security_series_digest	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_series_digest)
ffmpeg_security_patch_audit_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_patch_audit_sha256)
ffmpeg_source_audit_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_source_audit_sha256)
ffmpeg_repo_state_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_repo_state_sha256)
ffmpeg_security_magicyuv_blob	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_magicyuv_blob)
ffmpeg_security_tls_version_blob	$(pinned_ge_state_value "$prepared_state" ffmpeg_security_tls_version_blob)
ffmpeg_crypto_build_origin	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_origin)
ffmpeg_crypto_build_patch_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_patch_sha256)
ffmpeg_crypto_build_series_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_series_sha256)
ffmpeg_crypto_build_series_digest	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_series_digest)
ffmpeg_crypto_build_audit_sha256	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_audit_sha256)
ffmpeg_crypto_build_makefile_blob	$(pinned_ge_state_value "$prepared_state" ffmpeg_crypto_build_makefile_blob)
EOF
  fi
  if [[ "$version" == 5 || "$version" == 6 ]]; then
    [[ "$(pinned_ge_state_value "$invocation" steamrt_image_id)" \
        == "$PIN_STEAMRT_IMAGE_ID" ]] \
      || pinned_ge_fail "resume invocation used a different SteamRT image ID" \
      || return 1
  fi
  host_shell="$(pinned_ge_state_value "$invocation" host_make_shell)"
  pinned_ge_require_safe_absolute_path "recorded host Bash" "$host_shell" \
    || return 1
  [[ -x "$host_shell" ]] \
    || pinned_ge_fail "recorded resume host Bash is no longer executable" \
    || return 1
  if [[ "$version" == 3 && "$host_shell" != "$PIN_RETAINED_RESUME_HOST_SHELL" ]]; then
    pinned_ge_fail "legacy retained invocation used an unexpected host Bash"
    return 1
  fi
}

pinned_ge_require_resume_source_barrier() (
  set -euo pipefail

  local source="$1" build="$2" package origin synced dry_run

  source="$(realpath -e -- "$source")"
  build="$(realpath -e -- "$build")"
  pinned_ge_require_safe_absolute_path "GE source path" "$source"
  pinned_ge_require_safe_absolute_path "GE build path" "$build"
  dry_run="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-source-barrier.XXXXXX")"
  trap 'rm -f -- "$dry_run"' EXIT

  for package in libpcap xz; do
    origin="$source/$package"
    synced="$build/src-$package"
    [[ -d "$origin" && ! -L "$origin" && -d "$synced" && ! -L "$synced" ]] \
      || pinned_ge_fail "resume source barrier lacks $package source directories" \
      || return 1
    [[ ! -e "$origin/configure" && ! -L "$origin/configure" \
        && ! -e "$origin/build-aux/missing" && ! -L "$origin/build-aux/missing" ]] \
      || pinned_ge_fail "$package upstream pin unexpectedly contains generated Autoconf files" \
      || return 1
    [[ ! -e "$synced/configure" && ! -L "$synced/configure" \
        && ! -e "$synced/build-aux/missing" && ! -L "$synced/build-aux/missing" ]] \
      || pinned_ge_fail \
        "$package generated Autoconf files survived the resume all-source barrier" \
      || return 1
    : >"$dry_run"
    rsync --dry-run --filter=:C --exclude '*~' --exclude .git \
      --exclude compile_commands.json --info=name -Oarx --delete \
      "$origin/" "$synced" >"$dry_run" \
      || pinned_ge_fail "$package resume-barrier rsync verification failed" \
      || return 1
    if awk 'NF {found = 1} END {exit !found}' "$dry_run"; then
      sed -n '1,40p' "$dry_run" >&2
      pinned_ge_fail "$package source is still dirty after the resume all-source barrier"
      return 1
    fi
  done
)

pinned_ge_require_retained_resume_anchors() (
  set -euo pipefail

  local source="$1" build="$2" state_dir="$3" cache_home="$4"
  local invocation="$state_dir/build-invocation.tsv"
  local prepared_state="$state_dir/prepared-source.tsv"
  local manifest contrib_manifest log_file kind path size sha extra target
  local actual_size actual_sha
  local line_number=0 version_count=0 source_count=0 makefile_count=0
  local host_makefile_count=0 file_count=0 present_count=0 absent_count=0
  local retained_source retained_build retained_state retained_cache
  declare -A seen=()

  source="$(realpath -e -- "$source")"
  build="$(realpath -e -- "$build")"
  state_dir="$(realpath -e -- "$state_dir")"
  cache_home="$(realpath -e -- "$cache_home")"
  retained_source="$(realpath -e -- \
    "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_SOURCE")"
  retained_build="$(realpath -e -- "$PIN_RETAINED_RESUME_BUILD")"
  retained_state="$(realpath -e -- \
    "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_STATE")"
  retained_cache="$(pinned_ge_resolve_retained_resume_cache)"
  [[ "$source" == "$retained_source" && "$build" == "$retained_build" \
      && "$state_dir" == "$retained_state" && "$cache_home" == "$retained_cache" ]] \
    || pinned_ge_fail "adoption is restricted to the exact retained vodfix2 paths" \
    || return 1
  for pair in \
      "GE source path:$source" "GE build path:$build" \
      "GE state path:$state_dir" "GE cache home:$cache_home"; do
    pinned_ge_require_safe_absolute_path "${pair%%:*}" "${pair#*:}"
  done

  [[ -f "$invocation" && ! -L "$invocation" \
      && "$(pinned_ge_sha256 "$invocation")" \
        == "$PIN_RETAINED_RESUME_INVOCATION_SHA256" ]] \
    || pinned_ge_fail "retained vodfix2 invocation differs from its one-shot pin" \
    || return 1
  [[ -f "$prepared_state" && ! -L "$prepared_state" \
      && "$(pinned_ge_state_value "$prepared_state" source_path)" == "$source" ]] \
    || pinned_ge_fail "retained vodfix2 prepared state belongs to another source" \
    || return 1
  contrib_manifest="$(pinned_ge_build_contrib_manifest_path)" || return 1
  pinned_ge_require_complete_build_contrib "$source" "$contrib_manifest" \
    || return 1

  manifest="$(realpath -e -- \
    "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_ANCHOR_MANIFEST")"
  [[ "$manifest" == "$PINNED_GE_ROOT/config/"* \
      && -f "$manifest" && ! -L "$manifest" \
      && "$(pinned_ge_sha256 "$manifest")" \
        == "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" ]] \
    || pinned_ge_fail "retained-resume anchor manifest is absent, unsafe, or changed" \
    || return 1

  while IFS=$'\t' read -r kind path size sha extra \
      || [[ -n "$kind$path$size$sha$extra" ]]; do
    line_number=$((line_number + 1))
    [[ -z "$extra" ]] \
      || pinned_ge_fail "retained-resume manifest line $line_number has extra fields" \
      || return 1
    case "$kind" in
      retained_resume_manifest_version)
        [[ "$path" == 1 && -z "$size$sha" ]] \
          || pinned_ge_fail "invalid retained-resume manifest version" || return 1
        version_count=$((version_count + 1))
        continue
        ;;
      source_commit)
        [[ "$path" == "$PIN_SOURCE_COMMIT" && -z "$size$sha" ]] \
          || pinned_ge_fail "retained-resume manifest is pinned to another source" \
          || return 1
        source_count=$((source_count + 1))
        continue
        ;;
      generated_makefile|host_shell_makefile|file)
        [[ "$size" =~ ^[1-9][0-9]*$ && "$sha" =~ ^[0-9a-f]{64}$ ]] \
          || pinned_ge_fail "invalid retained file anchor on line $line_number" \
          || return 1
        ;;
      present|absent)
        [[ -z "$size$sha" ]] \
          || pinned_ge_fail "invalid retained path anchor on line $line_number" \
          || return 1
        ;;
      *)
        pinned_ge_fail "unknown retained-resume manifest key on line $line_number: $kind"
        return 1
        ;;
    esac
    case "$path" in
      ''|/*|../*|*/../*|*/..|..|*[$'\n\t']*)
        pinned_ge_fail "unsafe retained-resume anchor path: $path" || return 1 ;;
    esac
    [[ -z "${seen[$path]+present}" ]] \
      || pinned_ge_fail "duplicate retained-resume anchor path: $path" || return 1
    seen["$path"]=1
    target="$build/$path"
    case "$kind" in
      generated_makefile|host_shell_makefile|file)
        [[ -f "$target" && ! -L "$target" ]] \
          || pinned_ge_fail "retained file anchor is absent or unsafe: $path" \
          || return 1
        actual_size="$(stat -c '%s' -- "$target")"
        actual_sha="$(pinned_ge_sha256 "$target")"
        [[ "$actual_size" == "$size" && "$actual_sha" == "$sha" ]] \
          || pinned_ge_fail "retained file anchor changed: $path" || return 1
        case "$kind" in
          generated_makefile) makefile_count=$((makefile_count + 1)) ;;
          host_shell_makefile) host_makefile_count=$((host_makefile_count + 1)) ;;
          file) file_count=$((file_count + 1)) ;;
        esac
        ;;
      present)
        [[ -f "$target" && ! -L "$target" ]] \
          || pinned_ge_fail "retained progress anchor is absent or unsafe: $path" \
          || return 1
        present_count=$((present_count + 1))
        ;;
      absent)
        [[ ! -e "$target" && ! -L "$target" ]] \
          || pinned_ge_fail "retained negative progress anchor now exists: $path" \
          || return 1
        absent_count=$((absent_count + 1))
        ;;
    esac
  done <"$manifest"
  [[ "$version_count" == 1 && "$source_count" == 1 \
      && "$makefile_count" == 1 && "$host_makefile_count" == 1 \
      && "$file_count" == 7 && "$present_count" == 14 \
      && "$absent_count" == 6 ]] \
    || pinned_ge_fail "retained-resume anchor manifest has an incomplete schema" \
    || return 1

  pinned_ge_validate_configured_build_makefile "$source" "$build" >/dev/null
  for path in obj-wine-i386/config.status obj-wine-x86_64/config.status; do
    rg -Fq "ac_pwd='$build/${path%/config.status}'" "$build/$path" \
      && rg -Fq "srcdir='$build/src-wine'" "$build/$path" \
      || pinned_ge_fail "retained Wine configure anchor names another object/source path" \
      || return 1
  done
  for path in obj-ffmpeg-x86_64/ffbuild/config.mak \
      obj-dxvk-x86_64/build.ninja obj-kaldi-x86_64/CMakeCache.txt; do
    rg -Fq "$build" "$build/$path" \
      || pinned_ge_fail "retained configure anchor names another build root: $path" \
      || return 1
  done

  log_file="$(realpath -e -- "$PINNED_GE_ROOT/$PIN_RETAINED_RESUME_LOG")"
  [[ -f "$log_file" && ! -L "$log_file" ]] \
    || pinned_ge_fail "retained build log is absent or unsafe" || return 1
  awk '
    /Proton build available at/ { last_success = NR }
    /media_engine_defer_current_time.*defined but not used/ { media_failure = NR }
    /[.]wine-i386-build] Error 2/ { final_failure = NR }
    END {
      exit !(media_failure && final_failure && final_failure >= media_failure && final_failure > last_success)
    }
  ' "$log_file" \
    || pinned_ge_fail "retained log no longer ends after the pinned vodfix2 compile failure" \
    || return 1
)
