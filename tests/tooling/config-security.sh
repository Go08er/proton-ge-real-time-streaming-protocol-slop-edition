#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../scripts/pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-config-security.XXXXXX")"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

expect_load_rejected() {
  local label="$1"
  local config="$2"

  if (
    PINNED_GE_CONFIG="$config"
    pinned_ge_load_config
  ) >/dev/null 2>&1; then
    fail "pinned config loader accepted $label"
  fi
}

A31_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260713-a31.env"
ACTIVE_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260723-a312.env"
RELEASE_CONFIG="$ROOT_DIR/config/ge-proton11-3-a314.env"
LEGACY_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260713.env"

(
  PINNED_GE_CONFIG="$ACTIVE_CONFIG"
  pinned_ge_load_config
  [[ "$PINNED_GE_HAS_RETAINED_RESUME" == 0 ]] \
    || fail "fresh active config unexpectedly carries retained-resume provenance"
  [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]] \
    || fail "active bb1 pin lost its exact GE patch-overlap provenance"
  overlap_manifest="$(pinned_ge_patch_overlap_manifest_path)"
  [[ "$(pinned_ge_patch_overlap_records "$overlap_manifest" | wc -l)" == 2 ]] \
    || fail "active GE patch-overlap manifest is not the reviewed two-patch set"
)
(
  PINNED_GE_CONFIG="$RELEASE_CONFIG"
  pinned_ge_load_config
  [[ "$PIN_ID" == ge-proton11-3-a314 \
      && "$PIN_KIND" == release-tag \
      && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-3 \
      && "$PIN_SOURCE_COMMIT" \
        == 8c8003f7f5473d883fbe1bc7ac070c79955754e8 \
      && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
        == bfb3c4316dc348e93e5571c822a634ce98f5ac6c \
      && "$PINNED_GE_HAS_RETAINED_RESUME" == 0 \
      && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
    || fail "A3.14 release config did not load as a fresh exact release pin"
)
(
  PINNED_GE_CONFIG="$A31_CONFIG"
  pinned_ge_load_config
  [[ "$PINNED_GE_HAS_RETAINED_RESUME" == 1 ]] \
    || fail "old-base config lost its complete retained-resume provenance"
  [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
    || fail "old-base config inherited a bb1-only patch-overlap exception"
)

cp -- "$ACTIVE_CONFIG" "$FIXTURE_DIR/partial-retained-from-fresh.env"
printf '%s\n' \
  'PIN_RETAINED_RESUME_SOURCE=worktrees/forbidden-partial-state' \
  >>"$FIXTURE_DIR/partial-retained-from-fresh.env"
expect_load_rejected 'one retained-resume field added to a fresh config' \
  "$FIXTURE_DIR/partial-retained-from-fresh.env"

sed '/^PIN_RETAINED_RESUME_LOG=/d' "$A31_CONFIG" \
  >"$FIXTURE_DIR/partial-retained-from-old.env"
expect_load_rejected 'one retained-resume field removed from an old config' \
  "$FIXTURE_DIR/partial-retained-from-old.env"

sed '/^PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256=/d' "$ACTIVE_CONFIG" \
  >"$FIXTURE_DIR/partial-overlap-from-active.env"
expect_load_rejected 'one patch-overlap field removed from the active config' \
  "$FIXTURE_DIR/partial-overlap-from-active.env"

cp -- "$A31_CONFIG" "$FIXTURE_DIR/partial-overlap-from-old.env"
printf '%s\n' \
  'PIN_GE_PATCH_OVERLAP_MANIFEST=config/ge-patch-overlap-bb1caad3.tsv' \
  >>"$FIXTURE_DIR/partial-overlap-from-old.env"
expect_load_rejected 'one patch-overlap field added to an old config' \
  "$FIXTURE_DIR/partial-overlap-from-old.env"

mkdir "$FIXTURE_DIR/submodule-filter"
printf '%s\n' \
  '[submodule "wine"]' \
  '	path = wine' \
  '[submodule "dxvk-nvapi"]' \
  '	path = dxvk-nvapi' \
  '[submodule "optional-nested-nvapi"]' \
  '	path = nvidia-libs/dxvk-nvapi' \
  '[submodule "optional-layer"]' \
  '	path = vklayers/vkBasalt' \
  >"$FIXTURE_DIR/submodule-filter/.gitmodules"
selected_submodules="$(
  pinned_ge_registered_submodules "$FIXTURE_DIR/submodule-filter"
)"
[[ "$selected_submodules" == $'wine\ndxvk-nvapi' ]] \
  || fail "build-required submodule selector did not exclude only optional groups"

sed 's/^PIN_SCHEMA_VERSION=5$/PIN_SCHEMA_VERSION=4/' \
  "$A31_CONFIG" >"$FIXTURE_DIR/a31-mutated-to-schema4.env"
expect_load_rejected \
  'a schema-5 file whose version line was mutated to schema 4' \
  "$FIXTURE_DIR/a31-mutated-to-schema4.env"

cp -- "$LEGACY_CONFIG" "$FIXTURE_DIR/legacy-extra-schema5.env"
printf '%s\n' \
  'PIN_FFMPEG_SECURITY_FIX_COMMITS=374b726ffa878ee1cadb987bd1e1e20cc7ed8845' \
  >>"$FIXTURE_DIR/legacy-extra-schema5.env"
expect_load_rejected 'a schema-5-only key in schema 4' \
  "$FIXTURE_DIR/legacy-extra-schema5.env"

cp -- "$A31_CONFIG" "$FIXTURE_DIR/unknown-key.env"
printf '%s\n' 'PIN_UNREVIEWED_FIELD=1' >>"$FIXTURE_DIR/unknown-key.env"
expect_load_rejected 'an unknown PIN_* key' "$FIXTURE_DIR/unknown-key.env"

cp -- "$A31_CONFIG" "$FIXTURE_DIR/duplicate-key.env"
printf '%s\n' 'PIN_BUILD_NAME=duplicate' >>"$FIXTURE_DIR/duplicate-key.env"
expect_load_rejected 'a duplicate supported key' "$FIXTURE_DIR/duplicate-key.env"

sed '/^PIN_BUILD_NAME=/d' "$LEGACY_CONFIG" \
  >"$FIXTURE_DIR/missing-common-after-prior-load.env"
if (
  PINNED_GE_CONFIG="$A31_CONFIG"
  pinned_ge_load_config
  export PIN_BUILD_NAME=environment-injection
  PINNED_GE_CONFIG="$FIXTURE_DIR/missing-common-after-prior-load.env"
  pinned_ge_load_config
) >/dev/null 2>&1; then
  fail 'prior-load/environment value satisfied a missing current-file common key'
fi

sed '/^PIN_FFMPEG_CRYPTO_BUILD_ORIGIN=/d' "$A31_CONFIG" \
  >"$FIXTURE_DIR/missing-schema5-after-prior-load.env"
if (
  PINNED_GE_CONFIG="$A31_CONFIG"
  pinned_ge_load_config
  export PIN_FFMPEG_CRYPTO_BUILD_ORIGIN=0000000000000000000000000000000000000000
  PINNED_GE_CONFIG="$FIXTURE_DIR/missing-schema5-after-prior-load.env"
  pinned_ge_load_config
) >/dev/null 2>&1; then
  fail 'prior-load/environment value satisfied a missing current-file schema-5 key'
fi

write_tls_config() {
  local output="$1"
  printf '%s\n' \
    '#define CONFIG_GNUTLS 1' \
    '#define CONFIG_LIBZMQ 0' \
    '#define CONFIG_FFMPEG 0' \
    '#define CONFIG_FFPLAY 0' \
    '#define CONFIG_FFPROBE 0' \
    '#define CONFIG_LIBTLS 0' \
    '#define CONFIG_MBEDTLS 0' \
    '#define CONFIG_OPENSSL 0' \
    '#define CONFIG_SCHANNEL 0' \
    '#define CONFIG_SECURETRANSPORT 0' >"$output"
}

write_components() {
  local output="$1"
  local crypto="$2"
  printf '%s\n' \
    '#define CONFIG_RTSP_DEMUXER 1' \
    '#define CONFIG_HLS_DEMUXER 1' \
    '#define CONFIG_HTTP_PROTOCOL 1' \
    '#define CONFIG_HTTPS_PROTOCOL 1' \
    '#define CONFIG_RTP_PROTOCOL 1' \
    '#define CONFIG_TCP_PROTOCOL 1' \
    '#define CONFIG_TLS_PROTOCOL 1' \
    '#define CONFIG_UDP_PROTOCOL 1' \
    "#define CONFIG_CRYPTO_PROTOCOL $crypto" \
    '#define CONFIG_FILE_PROTOCOL 0' \
    '#define CONFIG_DATA_PROTOCOL 0' \
    '#define CONFIG_HTTPPROXY_PROTOCOL 0' \
    '#define CONFIG_CONCAT_PROTOCOL 0' \
    '#define CONFIG_SUBFILE_PROTOCOL 0' \
    '#define CONFIG_ZMQ_FILTER 0' \
    '#define CONFIG_MAGICYUV_DECODER 1' >"$output"
}

expect_surface_rejected() {
  local label="$1"
  local schema="$2"
  local config="$3"
  local components="$4"

  if pinned_ge_require_ffmpeg_config_surface \
      "$schema" "$config" "$components" >/dev/null 2>&1; then
    fail "FFmpeg config-surface gate accepted $label"
  fi
}

write_tls_config "$FIXTURE_DIR/config-good.h"
write_components "$FIXTURE_DIR/components-schema4.h" 0
write_components "$FIXTURE_DIR/components-schema5.h" 1
pinned_ge_require_ffmpeg_config_surface \
  4 "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-schema4.h"
pinned_ge_require_ffmpeg_config_surface \
  5 "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-schema5.h"
pinned_ge_require_exact_ffmpeg_define \
  "$FIXTURE_DIR/components-schema5.h" CONFIG_MAGICYUV_DECODER 1

for backend in CONFIG_LIBTLS CONFIG_SECURETRANSPORT; do
  sed "s/^#define $backend 0$/#define $backend 1/" \
    "$FIXTURE_DIR/config-good.h" >"$FIXTURE_DIR/config-bad-$backend.h"
  expect_surface_rejected "$backend enabled" 5 \
    "$FIXTURE_DIR/config-bad-$backend.h" "$FIXTURE_DIR/components-schema5.h"
done

sed 's/^#define CONFIG_LIBZMQ 0$/#define CONFIG_LIBZMQ 1/' \
  "$FIXTURE_DIR/config-good.h" >"$FIXTURE_DIR/config-libzmq.h"
expect_surface_rejected 'enabled libzmq dependency' 5 \
  "$FIXTURE_DIR/config-libzmq.h" "$FIXTURE_DIR/components-schema5.h"

sed 's/^#define CONFIG_ZMQ_FILTER 0$/#define CONFIG_ZMQ_FILTER 1/' \
  "$FIXTURE_DIR/components-schema5.h" >"$FIXTURE_DIR/components-zmq-filter.h"
expect_surface_rejected 'enabled ZMQ filter' 5 "$FIXTURE_DIR/config-good.h" \
  "$FIXTURE_DIR/components-zmq-filter.h"

for program in CONFIG_FFMPEG CONFIG_FFPLAY CONFIG_FFPROBE; do
  sed "s/^#define $program 0$/#define $program 1/" \
    "$FIXTURE_DIR/config-good.h" >"$FIXTURE_DIR/config-bad-$program.h"
  expect_surface_rejected "$program enabled" 5 \
    "$FIXTURE_DIR/config-bad-$program.h" "$FIXTURE_DIR/components-schema5.h"
done

sed 's/^#define CONFIG_HLS_DEMUXER 1$/#define CONFIG_HLS_DEMUXER 0/' \
  "$FIXTURE_DIR/components-schema5.h" >"$FIXTURE_DIR/components-no-hls.h"
expect_surface_rejected 'disabled HLS demuxer' 5 "$FIXTURE_DIR/config-good.h" \
  "$FIXTURE_DIR/components-no-hls.h"

cp -- "$FIXTURE_DIR/components-schema5.h" \
  "$FIXTURE_DIR/components-unexpected-protocol.h"
printf '%s\n' '#define CONFIG_FTP_PROTOCOL 1' \
  >>"$FIXTURE_DIR/components-unexpected-protocol.h"
expect_surface_rejected 'an extra enabled FTP protocol' 5 \
  "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-unexpected-protocol.h"

sed 's/^#define CONFIG_HTTP_PROTOCOL 1$/#define CONFIG_HTTP_PROTOCOL 10/' \
  "$FIXTURE_DIR/components-schema5.h" >"$FIXTURE_DIR/components-prefix-value.h"
expect_surface_rejected 'a prefix-only value of 10' 5 \
  "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-prefix-value.h"

cp -- "$FIXTURE_DIR/components-schema5.h" \
  "$FIXTURE_DIR/components-duplicate-define.h"
printf '%s\n' '#define CONFIG_TLS_PROTOCOL 0' \
  >>"$FIXTURE_DIR/components-duplicate-define.h"
expect_surface_rejected 'duplicate contradictory protocol definitions' 5 \
  "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-duplicate-define.h"

expect_surface_rejected 'schema-5 crypto in schema 4' 4 \
  "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-schema5.h"
expect_surface_rejected 'schema-4 crypto in schema 5' 5 \
  "$FIXTURE_DIR/config-good.h" "$FIXTURE_DIR/components-schema4.h"

printf 'Strict config-loader and FFmpeg surface regressions passed.\n'
