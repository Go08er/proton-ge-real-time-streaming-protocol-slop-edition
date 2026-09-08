#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../scripts/pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-resume-test.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT
REPO="$TMP_ROOT/source"
BUILD="$TMP_ROOT/build"
STATE="$TMP_ROOT/state"
CACHE="$TMP_ROOT/cache"
TEST_PROJECT="$TMP_ROOT/project"
MANIFEST="$TEST_PROJECT/config/build-contrib.tsv"
mkdir -p "$REPO" "$BUILD" "$STATE" "$CACHE" "$TEST_PROJECT/config"

git -C "$REPO" init -q
printf 'tracked\n' >"$REPO/tracked.txt"
git -C "$REPO" add tracked.txt
git -C "$REPO" -c user.name=Test -c user.email=test.invalid commit -qm initial

PIN_SOURCE_COMMIT="$(git -C "$REPO" rev-parse HEAD)"
allowed_content='validated build download'
fixture_size="${#allowed_content}"
fixture_sha="$(printf '%s' "$allowed_content" | sha256sum | awk '{print $1}')"
protonfixes_content='validated ProtonFixes build download'
protonfixes_size="${#protonfixes_content}"
protonfixes_sha="$(printf '%s' "$protonfixes_content" \
  | sha256sum | awk '{print $1}')"
zenity_content='validated ProtonFixes zenity-rs binary'
zenity_size="${#zenity_content}"
zenity_sha="$(printf '%s' "$zenity_content" | sha256sum | awk '{print $1}')"
{
  printf 'build_contrib_manifest_version\t2\n'
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  printf 'entry\tcontrib/test.tar.xz\t0644\t%s\t%s\n' \
    "$fixture_size" "$fixture_sha"
  printf 'entry\tcontrib/protonfixes/downloads/unzip/test.tar.xz\t0644\t%s\t%s\n' \
    "$protonfixes_size" "$protonfixes_sha"
  printf 'entry\tcontrib/protonfixes/zenity-rs/zenity-rs\t0644\t%s\t%s\n' \
    "$zenity_size" "$zenity_sha"
} >"$MANIFEST"

baseline="$(pinned_ge_repo_state_digest "$REPO" "$MANIFEST")"
mkdir -p "$REPO/contrib/protonfixes/downloads/unzip" \
  "$REPO/contrib/protonfixes/zenity-rs"
printf '%s' "$allowed_content" >"$REPO/contrib/test.tar.xz"
chmod 0644 "$REPO/contrib/test.tar.xz"
printf '%s' "$protonfixes_content" \
  >"$REPO/contrib/protonfixes/downloads/unzip/test.tar.xz"
chmod 0644 "$REPO/contrib/protonfixes/downloads/unzip/test.tar.xz"
printf '%s' "$zenity_content" \
  >"$REPO/contrib/protonfixes/zenity-rs/zenity-rs"
chmod 0644 "$REPO/contrib/protonfixes/zenity-rs/zenity-rs"
with_allowed="$(pinned_ge_repo_state_digest "$REPO" "$MANIFEST")"
[[ "$with_allowed" == "$baseline" ]] \
  || fail "validated build download changed the prepared-source digest"
pinned_ge_require_complete_build_contrib "$REPO" "$MANIFEST"

eval "$(python3 - "$ROOT_DIR/scripts/build-pinned-ge.sh" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()
start = text.index("require_prefetched_build_contrib() {")
end = text.index("\n# Fail before the low-impact re-exec", start)
print(text[start:end])
start = text.index("manifest_build_input_record() {")
end = text.index('\nif [[ "$RESUME" == 1 ]]', start)
print(text[start:end])
PY
)"
SOURCE_DIR="$REPO"
BUILD_DIR="$BUILD"
CONTRIB_MANIFEST="$MANIFEST"
PINNED_GE_CONFIG_PATH="$TEST_PROJECT/config/test-pin.env"
export SOURCE_DIR BUILD_DIR CONTRIB_MANIFEST PINNED_GE_CONFIG_PATH

rm -f -- "$REPO/contrib/test.tar.xz"
if pinned_ge_require_complete_build_contrib "$REPO" "$MANIFEST" \
    >/dev/null 2>&1; then
  fail "retained resume accepted an incomplete build-contrib cache"
fi
if missing_output="$(require_prefetched_build_contrib "$MANIFEST" 2>&1)"; then
  fail "early build-contrib check accepted a missing declared input"
fi
[[ "$missing_output" == *$'  contrib/test.tar.xz\n'* ]] \
  || fail "early build-contrib error did not name the exact missing path"
[[ "$missing_output" == *"./scripts/prefetch-pinned-ge-contrib.sh --config $PINNED_GE_CONFIG_PATH --source $REPO"* ]] \
  || fail "early build-contrib error did not print the recovery command"
printf '%s' "$allowed_content" >"$REPO/contrib/test.tar.xz"
chmod 0644 "$REPO/contrib/test.tar.xz"

printf 'tampered' >"$REPO/contrib/test.tar.xz"
if pinned_ge_repo_state_digest "$REPO" "$MANIFEST" >/dev/null 2>&1; then
  fail "tampered allowlisted download was accepted"
fi
printf '%s' "$allowed_content" >"$REPO/contrib/test.tar.xz"
chmod 0600 "$REPO/contrib/test.tar.xz"
if pinned_ge_repo_state_digest "$REPO" "$MANIFEST" >/dev/null 2>&1; then
  fail "allowlisted download with the wrong mode was accepted"
fi
chmod 0644 "$REPO/contrib/test.tar.xz"
printf 'unreviewed' >"$REPO/contrib/extra.tar.xz"
with_extra="$(pinned_ge_repo_state_digest "$REPO" "$MANIFEST")"
[[ "$with_extra" != "$baseline" ]] \
  || fail "unreviewed untracked source entry was broadly ignored"
rm -f -- "$REPO/contrib/extra.tar.xz"

seed_protonfixes_build_contrib
seeded_protonfixes_input="$BUILD/obj-protonfixes-x86_64/downloads/unzip/test.tar.xz"
[[ -f "$seeded_protonfixes_input" && ! -L "$seeded_protonfixes_input" ]] \
  || fail "manifest ProtonFixes input was not seeded into its build object path"
[[ "$(cat "$seeded_protonfixes_input")" == "$protonfixes_content" ]] \
  || fail "seeded ProtonFixes input content differs from the manifest source"
printf 'partial' >"$seeded_protonfixes_input"
seed_protonfixes_build_contrib >/dev/null
[[ "$(cat "$seeded_protonfixes_input")" == "$protonfixes_content" ]] \
  || fail "partial ProtonFixes download was not replaced by the verified input"
seeded_zenity="$BUILD/obj-protonfixes-x86_64/zenity-rs/zenity-rs"
zenity_stamp="$BUILD/obj-protonfixes-x86_64/.build-zenity-rs-dist"
[[ -f "$seeded_zenity" && ! -L "$seeded_zenity" \
    && "$(stat -c '%a' "$seeded_zenity")" == 755 ]] \
  || fail "manifest zenity-rs input was not installed executable"
[[ "$(cat "$seeded_zenity")" == "$zenity_content" ]] \
  || fail "seeded zenity-rs content differs from the manifest source"
[[ -f "$zenity_stamp" && ! -L "$zenity_stamp" ]] \
  || fail "offline zenity-rs build stamp was not created"
printf 'partial' >"$seeded_zenity"
seed_protonfixes_build_contrib >/dev/null
[[ "$(cat "$seeded_zenity")" == "$zenity_content" \
    && "$(stat -c '%a' "$seeded_zenity")" == 755 ]] \
  || fail "partial zenity-rs input was not repaired idempotently"
unknown_protonfixes_path=contrib/protonfixes/future/unknown.bin
mkdir -p "$(dirname "$REPO/$unknown_protonfixes_path")"
printf 'unknown\n' >"$REPO/$unknown_protonfixes_path"
chmod 0644 "$REPO/$unknown_protonfixes_path"
PROTONFIXES_BAD_MANIFEST="$TEST_PROJECT/config/protonfixes-contrib-unknown.tsv"
cp -- "$MANIFEST" "$PROTONFIXES_BAD_MANIFEST"
printf 'entry\t%s\t0644\t%s\t%s\n' \
  "$unknown_protonfixes_path" \
  "$(stat -c '%s' "$REPO/$unknown_protonfixes_path")" \
  "$(pinned_ge_sha256 "$REPO/$unknown_protonfixes_path")" \
  >>"$PROTONFIXES_BAD_MANIFEST"
CONTRIB_MANIFEST="$PROTONFIXES_BAD_MANIFEST"
if seed_protonfixes_build_contrib >/dev/null 2>&1; then
  fail "unknown ProtonFixes build-input mapping was accepted"
fi
CONTRIB_MANIFEST="$MANIFEST"
rm -f -- "$REPO/$unknown_protonfixes_path"

PIPER_SOURCE="$TMP_ROOT/piper-source"
PIPER_BUILD="$TMP_ROOT/piper-build"
PIPER_MANIFEST="$TEST_PROJECT/config/piper-contrib.tsv"
mkdir -p \
  "$PIPER_SOURCE/contrib/piper/downloads/fmt" \
  "$PIPER_SOURCE/contrib/piper/downloads/spdlog" \
  "$PIPER_SOURCE/contrib/piper/downloads/piper-phonemize/onnxruntime" \
  "$PIPER_SOURCE/contrib/piper/downloads/piper-phonemize/espeak-ng/sonic"
piper_paths=(
  contrib/piper/downloads/fmt/10.0.0.zip
  contrib/piper/downloads/spdlog/v1.12.0.zip
  contrib/piper/downloads/piper-phonemize/pic.zip
  contrib/piper/downloads/piper-phonemize/onnxruntime/onnxruntime-linux-x64-1.14.1.tgz
  contrib/piper/downloads/piper-phonemize/espeak-ng/0f65aa301e0d6bae5e172cc74197d32a6182200f.zip
)
for path in "${piper_paths[@]}"; do
  printf 'fixture for %s\n' "$path" >"$PIPER_SOURCE/$path"
  chmod 0644 "$PIPER_SOURCE/$path"
done
sonic_path=contrib/piper/downloads/piper-phonemize/espeak-ng/sonic/fbf75c3d6d846bad3bb3d456cbc5d07d9fd8c104.zip
python3 - "$PIPER_SOURCE/$sonic_path" <<'PY'
from pathlib import Path
import sys
import zipfile

with zipfile.ZipFile(Path(sys.argv[1]), "w") as archive:
    archive.writestr("sonic-fixture/", "")
    archive.writestr("sonic-fixture/sonic.c", "int sonic_fixture;\n")
    archive.writestr("sonic-fixture/sonic.h", "#pragma once\n")
PY
chmod 0644 "$PIPER_SOURCE/$sonic_path"
piper_paths+=("$sonic_path")
{
  printf 'build_contrib_manifest_version\t2\n'
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  for path in "${piper_paths[@]}"; do
    printf 'entry\t%s\t0644\t%s\t%s\n' \
      "$path" "$(stat -c '%s' "$PIPER_SOURCE/$path")" \
      "$(pinned_ge_sha256 "$PIPER_SOURCE/$path")"
  done
} >"$PIPER_MANIFEST"

ORIGINAL_SOURCE_DIR="$SOURCE_DIR"
ORIGINAL_BUILD_DIR="$BUILD_DIR"
ORIGINAL_CONTRIB_MANIFEST="$CONTRIB_MANIFEST"
SOURCE_DIR="$PIPER_SOURCE"
BUILD_DIR="$PIPER_BUILD"
CONTRIB_MANIFEST="$PIPER_MANIFEST"

fmt_script="$PIPER_BUILD/obj-piper-x86_64/f/src/fmt_external-stamp/download-fmt_external.cmake"
spdlog_script="$PIPER_BUILD/obj-piper-x86_64/s/src/spdlog_external-stamp/download-spdlog_external.cmake"
phonemize_script="$PIPER_BUILD/obj-piper-x86_64/p/src/piper_phonemize_external-stamp/download-piper_phonemize_external.cmake"
mkdir -p "$(dirname "$fmt_script")" "$(dirname "$spdlog_script")" \
  "$(dirname "$phonemize_script")"
fmt_url=https://github.com/fmtlib/fmt/archive/refs/tags/10.0.0.zip
spdlog_url=https://github.com/gabime/spdlog/archive/refs/tags/v1.12.0.zip
phonemize_url=https://github.com/shaunren/piper-phonemize/archive/refs/heads/pic.zip
printf '%s\n%s\n' "$fmt_url" "$fmt_url" >"$fmt_script"
printf '%s\n' "$spdlog_url" >"$spdlog_script"
printf '%s\n' "$phonemize_url" >"$phonemize_script"
if rewrite_piper_top_level_downloads >/dev/null 2>&1; then
  fail "Piper URL rewrite accepted a generated script with two source URLs"
fi
printf '%s\n' "$fmt_url" >"$fmt_script"
rewrite_piper_top_level_downloads
rewrite_piper_top_level_downloads
[[ "$(rg -o -F "file://$PIPER_SOURCE/${piper_paths[0]}" "$fmt_script" | wc -l)" == 1 \
    && "$(rg -o -F "file://$PIPER_SOURCE/${piper_paths[1]}" "$spdlog_script" | wc -l)" == 1 \
    && "$(rg -o -F "file://$PIPER_SOURCE/${piper_paths[2]}" "$phonemize_script" | wc -l)" == 1 ]] \
  || fail "Piper generated download scripts were not rewritten exactly once"
if rg -q 'https://' "$fmt_script" "$spdlog_script" "$phonemize_script"; then
  fail "Piper generated download scripts retained a network URL"
fi

phonemize_cmake="$PIPER_BUILD/obj-piper-x86_64/p/src/piper_phonemize_external/CMakeLists.txt"
mkdir -p "$(dirname "$phonemize_cmake")"
{
  printf '%s\n' \
    'ExternalProject_Add(' \
    '    espeak_ng_external' \
    '    URL "https://github.com/rhasspy/espeak-ng/archive/0f65aa301e0d6bae5e172cc74197d32a6182200f.zip"' \
    "        CMAKE_ARGS -DCMAKE_INSTALL_PREFIX:PATH=\${ESPEAK_NG_DIR}" \
    ')'
} >"$phonemize_cmake"
configure_piper_phonemize_offline_inputs
configure_piper_phonemize_offline_inputs
seeded_onnx="$PIPER_BUILD/obj-piper-x86_64/p/src/piper_phonemize_external-build/download/onnxruntime-linux-x64-1.14.1.tgz"
[[ "$(cat "$seeded_onnx")" \
    == "$(cat "$PIPER_SOURCE/${piper_paths[3]}")" ]] \
  || fail "Piper ONNX Runtime input was not seeded into the nested build"
[[ -f "$PIPER_BUILD/obj-piper-x86_64/pinned-contrib/sonic-source/sonic.c" ]] \
  || fail "verified Sonic source was not extracted into the build-only tree"
[[ "$(rg -o -F "file://$PIPER_SOURCE/${piper_paths[4]}" "$phonemize_cmake" | wc -l)" == 1 ]] \
  || fail "piper-phonemize espeak URL was not rewritten exactly once"
[[ "$(rg -o -F -- \
    '-DFETCHCONTENT_SOURCE_DIR_SONIC-GIT:PATH=' "$phonemize_cmake" | wc -l)" == 1 ]] \
  || fail "piper-phonemize Sonic source override was not inserted exactly once"
printf 'partial' >"$seeded_onnx"
configure_piper_phonemize_offline_inputs >/dev/null
[[ "$(cat "$seeded_onnx")" \
    == "$(cat "$PIPER_SOURCE/${piper_paths[3]}")" ]] \
  || fail "partial nested ONNX Runtime input was not repaired idempotently"
printf 'tampered\n' \
  >"$PIPER_BUILD/obj-piper-x86_64/pinned-contrib/sonic-source/sonic.c"
if configure_piper_phonemize_offline_inputs >/dev/null 2>&1; then
  fail "tampered extracted Sonic source was trusted on resume"
fi

unknown_piper_path=contrib/piper/downloads/future/unknown.zip
mkdir -p "$(dirname "$PIPER_SOURCE/$unknown_piper_path")"
printf 'unknown\n' >"$PIPER_SOURCE/$unknown_piper_path"
chmod 0644 "$PIPER_SOURCE/$unknown_piper_path"
PIPER_BAD_MANIFEST="$TEST_PROJECT/config/piper-contrib-unknown.tsv"
cp -- "$PIPER_MANIFEST" "$PIPER_BAD_MANIFEST"
printf 'entry\t%s\t0644\t%s\t%s\n' \
  "$unknown_piper_path" "$(stat -c '%s' "$PIPER_SOURCE/$unknown_piper_path")" \
  "$(pinned_ge_sha256 "$PIPER_SOURCE/$unknown_piper_path")" \
  >>"$PIPER_BAD_MANIFEST"
CONTRIB_MANIFEST="$PIPER_BAD_MANIFEST"
if require_declared_piper_build_inputs >/dev/null 2>&1; then
  fail "unknown Piper build-input mapping was accepted"
fi

SOURCE_DIR="$ORIGINAL_SOURCE_DIR"
BUILD_DIR="$ORIGINAL_BUILD_DIR"
CONTRIB_MANIFEST="$ORIGINAL_CONTRIB_MANIFEST"

PINNED_GE_ROOT="$TEST_PROJECT"
PIN_BUILD_CONTRIB_MANIFEST=config/build-contrib.tsv
PIN_BUILD_CONTRIB_MANIFEST_SHA256="$(pinned_ge_sha256 "$MANIFEST")"
PIN_ID=test-pin
PIN_BUILD_NAME=Test-Proton
PIN_STEAMRT_IMAGE=example.invalid/steamrt:test
PIN_STEAMRT_IMAGE_ID="$(printf 'a%.0s' {1..64})"
PIN_CONFIGURE_SHA256="$(printf '1%.0s' {1..64})"
PIN_MAKEFILE_IN_SHA256="$(printf '2%.0s' {1..64})"
PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256="$(printf '3%.0s' {1..64})"

FAKE_PODMAN="$TMP_ROOT/fake-podman"
cat >"$FAKE_PODMAN" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == image && "$2" == inspect && "$3" == --format \
    && "$4" == '{{.Id}}' && "$5" == example.invalid/steamrt:test ]]
printf '%s\n' "${FAKE_IMAGE_ID:?}"
EOF
chmod 0755 "$FAKE_PODMAN"
export FAKE_IMAGE_ID="$PIN_STEAMRT_IMAGE_ID"
[[ "$(pinned_ge_require_local_steamrt_image "$FAKE_PODMAN")" \
    == "$PIN_STEAMRT_IMAGE_ID" ]] \
  || fail "exact local SteamRT image ID was not accepted"
FAKE_IMAGE_ID="$(printf 'b%.0s' {1..64})"
if pinned_ge_require_local_steamrt_image "$FAKE_PODMAN" >/dev/null 2>&1; then
  fail "different local SteamRT image ID was accepted"
fi
unset FAKE_IMAGE_ID

ABSENT_PATH="$TMP_ROOT/absent-output"
pinned_ge_require_absent_path fixture "$ABSENT_PATH"
ln -s "$TMP_ROOT/missing-target" "$ABSENT_PATH"
if pinned_ge_require_absent_path fixture "$ABSENT_PATH" >/dev/null 2>&1; then
  fail "dangling artifact symlink was treated as absent"
fi
rm -f -- "$ABSENT_PATH"

OPTIONAL_FILE="$TMP_ROOT/optional-file"
pinned_ge_require_regular_or_absent fixture "$OPTIONAL_FILE"
ln -s "$TMP_ROOT/missing-target" "$OPTIONAL_FILE"
if pinned_ge_require_regular_or_absent fixture "$OPTIONAL_FILE" \
    >/dev/null 2>&1; then
  fail "dangling policy or lock symlink was accepted"
fi
rm -f -- "$OPTIONAL_FILE"
printf 'regular\n' >"$OPTIONAL_FILE"
pinned_ge_require_regular_or_absent fixture "$OPTIONAL_FILE"

OPTIONAL_DIR="$TMP_ROOT/optional-directory"
pinned_ge_require_directory_or_absent fixture "$OPTIONAL_DIR"
ln -s "$TMP_ROOT/missing-target" "$OPTIONAL_DIR"
if pinned_ge_require_directory_or_absent fixture "$OPTIONAL_DIR" \
    >/dev/null 2>&1; then
  fail "dangling redist symlink was accepted"
fi
rm -f -- "$OPTIONAL_DIR"
mkdir "$OPTIONAL_DIR"
pinned_ge_require_directory_or_absent fixture "$OPTIONAL_DIR"

pinned_ge_emit_expected_build_makefile "$REPO" >"$BUILD/Makefile"
makefile_record="$(pinned_ge_validate_configured_build_makefile "$REPO" "$BUILD")"
[[ "$makefile_record" =~ ^[1-9][0-9]*$'\t'[0-9a-f]{64}$ ]] \
  || fail "exact generated Makefile did not produce a fingerprint"
printf '# mutation\n' >>"$BUILD/Makefile"
if pinned_ge_validate_configured_build_makefile "$REPO" "$BUILD" \
    >/dev/null 2>&1; then
  fail "modified generated Makefile was accepted"
fi
pinned_ge_emit_expected_build_makefile "$REPO" >"$BUILD/Makefile"

{
  printf 'root_repo_state_sha256\t%s\n' "$baseline"
  printf 'source_path\t%s\n' "$REPO"
  printf 'ge_pre_rtsp_diff_check_status\t2\n'
  printf 'ge_pre_rtsp_diff_check_sha256\t%s\n' "$(printf '4%.0s' {1..64})"
  printf 'ge_media_cleanup_normalization_digest\t%s\n' \
    "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256"
  printf 'ge_media_cleanup_audit_sha256\t%s\n' "$(printf '5%.0s' {1..64})"
  printf 'proton_launcher_patch_sha256\t%s\n' "$(printf '6%.0s' {1..64})"
  printf 'rtsp_hook_order\tafter-ge-video-rework-before-autoreconf\n'
} >"$STATE/prepared-source.tsv"
HOST_BASH="$(realpath -e -- "$(command -v bash)")"
{
  printf 'build_invocation_version\t5\n'
  printf 'pin_id\t%s\n' "$PIN_ID"
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  printf 'build_name\t%s\n' "$PIN_BUILD_NAME"
  printf 'source_path\t%s\n' "$REPO"
  printf 'build_path\t%s\n' "$BUILD"
  printf 'state_path\t%s\n' "$STATE"
  printf 'cache_home\t%s\n' "$CACHE"
  printf 'package_release\t0\n'
  printf 'jobs\t2\n'
  printf 'global_jobs\t2\n'
  printf 'make_jobs\t2\n'
  printf 'ninja_jobs\t1\n'
  printf 'scheduler_product\t2\n'
  printf 'scheduler_policy\tmake-times-ninja-at-most-global\n'
  printf 'recursive_make_parallelism\tjobserver\n'
  printf 'cargo_jobs_env\t1\n'
  printf 'cmake_jobs_env\t1\n'
  printf 'nix_build_cores_env\t1\n'
  printf 'host_make_shell\t%s\n' "$HOST_BASH"
  printf 'ge_pre_rtsp_diff_check_status\t2\n'
  printf 'ge_pre_rtsp_diff_check_sha256\t%s\n' "$(printf '4%.0s' {1..64})"
  printf 'ge_media_cleanup_manifest_sha256\t%s\n' \
    "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256"
  printf 'ge_media_cleanup_normalization_digest\t%s\n' \
    "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256"
  printf 'ge_media_cleanup_audit_sha256\t%s\n' "$(printf '5%.0s' {1..64})"
  printf 'proton_launcher_patch_sha256\t%s\n' "$(printf '6%.0s' {1..64})"
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
} >"$STATE/build-invocation.tsv"
pinned_ge_require_resume_invocation \
  "$REPO" "$BUILD" "$STATE" "$CACHE" \
  "$STATE/build-invocation.tsv" "$STATE/prepared-source.tsv" 2 2 1 2
PIN_SCHEMA_VERSION=5
if pinned_ge_require_resume_invocation \
    "$REPO" "$BUILD" "$STATE" "$CACHE" \
    "$STATE/build-invocation.tsv" "$STATE/prepared-source.tsv" 2 2 1 2 \
    >/dev/null 2>&1; then
  fail "schema-5 security build accepted legacy invocation schema 5"
fi
PIN_SCHEMA_VERSION=4
cp -- "$STATE/build-invocation.tsv" "$STATE/build-invocation.good.tsv"
sed -i 's/^steamrt_image_id\t.*/steamrt_image_id\tffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/' \
  "$STATE/build-invocation.tsv"
if pinned_ge_require_resume_invocation \
    "$REPO" "$BUILD" "$STATE" "$CACHE" \
    "$STATE/build-invocation.tsv" "$STATE/prepared-source.tsv" 2 2 1 2 \
    >/dev/null 2>&1; then
  fail "resume invocation accepted a different SteamRT image ID"
fi
mv -- "$STATE/build-invocation.good.tsv" "$STATE/build-invocation.tsv"
OTHER_CACHE="$TMP_ROOT/other-cache"
mkdir -p "$OTHER_CACHE"
if pinned_ge_require_resume_invocation \
    "$REPO" "$BUILD" "$STATE" "$OTHER_CACHE" \
    "$STATE/build-invocation.tsv" "$STATE/prepared-source.tsv" 2 2 1 2 \
    >/dev/null 2>&1; then
  fail "resume invocation accepted a different cache home"
fi
if pinned_ge_write_configure_state \
    "$REPO" "$BUILD" "$STATE" "$STATE/build-invocation.tsv" \
    adopted-existing "$CACHE" none >/dev/null 2>&1; then
  fail "removed generic adoption origin was still accepted"
fi
PIN_RETAINED_RESUME_SOURCE=../source
PIN_RETAINED_RESUME_BUILD="$BUILD"
PIN_RETAINED_RESUME_STATE=../state
PIN_RETAINED_RESUME_CACHE=../cache
PIN_RETAINED_RESUME_INVOCATION_SHA256="$(pinned_ge_sha256 \
  "$STATE/build-invocation.tsv")"
PIN_RETAINED_RESUME_ANCHOR_MANIFEST=config/missing-retained-anchors.tsv
PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256="$(printf '7%.0s' {1..64})"
if pinned_ge_write_configure_state \
    "$REPO" "$BUILD" "$STATE" "$STATE/build-invocation.tsv" \
    adopted-retained-vodfix2 "$CACHE" \
    "$PIN_RETAINED_RESUME_ANCHOR_MANIFEST_SHA256" >/dev/null 2>&1; then
  fail "empty never-configured fixture bypassed retained-build anchors"
fi
pinned_ge_write_configure_state \
  "$REPO" "$BUILD" "$STATE" "$STATE/build-invocation.tsv" \
  fresh-configure "$CACHE" none >/dev/null
pinned_ge_require_configure_state \
  "$REPO" "$BUILD" "$STATE" "$STATE/build-invocation.tsv" "$CACHE"

chmod 0644 "$STATE/configure-state.tsv"
printf 'fixture\tchanged\n' >>"$STATE/build-invocation.tsv"
if pinned_ge_require_configure_state \
    "$REPO" "$BUILD" "$STATE" "$STATE/build-invocation.tsv" "$CACHE" \
    >/dev/null 2>&1; then
  fail "configure state accepted a changed original invocation"
fi

unsafe_dollar_path=$'/tmp/shell\x24expansion'
for unsafe_path in \
    '/tmp/path with spaces' '/tmp/make#comment' "$unsafe_dollar_path" \
    '/tmp/shell;command'; do
  if pinned_ge_require_safe_absolute_path fixture "$unsafe_path" \
      >/dev/null 2>&1; then
    fail "unsafe GNU Make/shell path was accepted: $unsafe_path"
  fi
done
pinned_ge_require_safe_absolute_path fixture "$TMP_ROOT"

if bash "$ROOT_DIR/scripts/record-pinned-ge-resume-state.sh" \
    --source "$REPO" >/dev/null 2>&1; then
  fail "one-shot retained adoption accepted a caller-selected source"
fi

python3 - "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  "$ROOT_DIR/scripts/verify-preparation.sh" \
  "$ROOT_DIR/scripts/record-pinned-ge-resume-state.sh" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()
privacy_text = Path(sys.argv[2]).read_text()
record_text = Path(sys.argv[3]).read_text()
for expected in (
    'pinned_ge_require_absent_path "build artifact" "$path"',
    'pinned_ge_require_regular_or_absent "container policy" "$POLICY_FILE"',
    'pinned_ge_require_regular_or_absent "workspace build lock" "$BUILD_LOCK"',
):
    if expected not in text:
        raise SystemExit(f"build wrapper omits safety gate: {expected}")
early_check = text.index(
    '\nrequire_prefetched_build_contrib "$CONTRIB_MANIFEST"\n'
)
low_impact_reexec = text.index(
    'if [[ "${PINNED_GE_LOW_IMPACT_REEXEC:-0}" != 1 ]]'
)
build_lock = text.index('BUILD_LOCK=')
if not early_check < low_impact_reexec < build_lock:
    raise SystemExit(
        "build-contrib completeness is not checked before re-exec and lock"
    )
early_start = text.index("require_prefetched_build_contrib() {")
early_end = text.index("\n# Fail before the low-impact re-exec", early_start)
early_body = text[early_start:early_end]
for expected in (
    'printf \'  %s\\n\' "${missing[@]}"',
    "./scripts/prefetch-pinned-ge-contrib.sh --config %q --source %q",
    '"$PINNED_GE_CONFIG_PATH" "$SOURCE_DIR"',
    'pinned_ge_require_complete_build_contrib "$SOURCE_DIR" "$manifest"',
):
    if expected not in early_body:
        raise SystemExit(
            f"early build-contrib failure omits actionable detail: {expected}"
        )
seed_start = text.index("seed_protonfixes_build_contrib() {")
seed_end = text.index('\nif [[ "$RESUME" == 1 ]]', seed_start)
seed_body = text[seed_start:seed_end]
for expected in (
    'manifest_prefix="contrib/protonfixes/"',
    'unzip_prefix="${manifest_prefix}downloads/unzip/"',
    'zenity_path="${manifest_prefix}zenity-rs/zenity-rs"',
    'target_root="$BUILD_DIR/obj-protonfixes-x86_64"',
    'relative_path="${path#"$manifest_prefix"}"',
    '"$zenity_target" 0755 "ProtonFixes zenity-rs input"',
    'touch -- "$zenity_stamp"',
    'unknown ProtonFixes build-input mapping',
):
    if expected not in seed_body:
        raise SystemExit(
            f"ProtonFixes build-input seeding omits manifest mapping: {expected}"
        )
for hardcoded_archive in (
    "unzip_6.0.orig.tar.gz",
    "unzip_6.0-29.debian.tar.xz",
):
    if hardcoded_archive in seed_body:
        raise SystemExit(
            f"ProtonFixes seeding special-cases an archive: {hardcoded_archive}"
        )
for expected in (
    'pinned_ge_require_absent_path "retained adoption artifact" "$path"',
    'pinned_ge_require_regular_or_absent "workspace build lock" "$BUILD_LOCK"',
    '"retained redist staging directory" "$BUILD_DIR/redist"',
):
    if expected not in record_text:
        raise SystemExit(f"retained adoption omits safety gate: {expected}")
start = text.index("run_build() {")
end = text.index("\nnormalize_proton_version_metadata()", start)
body = text[start:end]
fresh = body.index('if [[ "$RESUME" == 0 ]]')
configure = body.index('bash "$SOURCE_DIR/configure.sh"')
fingerprint = body.index("pinned_ge_write_configure_state")
resume = body.index("else", fingerprint)
reuse = body.index("Reusing verified configure result", resume)
seed = body.index("seed_protonfixes_build_contrib", reuse)
barrier_make = body.index(
    'make "${make_args[@]}" pinned-resume-all-source', seed
)
barrier_check = body.index(
    "pinned_ge_require_resume_source_barrier", barrier_make
)
piper_configure = body.index(
    'make "${make_args[@]}" pinned-piper-x86_64-configure', barrier_check
)
piper_rewrite = body.index("rewrite_piper_top_level_downloads", piper_configure)
phonemize_download = body.index(
    'make "${make_args[@]}" pinned-piper-phonemize-download', piper_rewrite
)
piper_nested = body.index(
    "configure_piper_phonemize_offline_inputs", phonemize_download
)
redist_guard = body.index("pinned_ge_require_directory_or_absent", piper_nested)
redist_make = body.index('make "${make_args[@]}" "$make_target"', redist_guard)
if not (
    fresh < configure < fingerprint < resume < reuse < seed
    < barrier_make < barrier_check < piper_configure < piper_rewrite
    < phonemize_download < piper_nested < redist_guard < redist_make
):
    raise SystemExit("configure/fingerprint/resume control flow is not ordered safely")
if body.count('bash "$SOURCE_DIR/configure.sh"') != 1:
    raise SystemExit("resume build body contains an additional configure path")
if "--resume" not in text or "REEXEC_ARGS+=(--resume)" not in text:
    raise SystemExit("resume option is not preserved through low-impact re-exec")
if "DOCKER_OPTS=--pull=never" not in text:
    raise SystemExit("container run does not prohibit an implicit image pull")
if "--network=none" not in text:
    raise SystemExit("container run does not prohibit network access")
for expected in (
    "$(DOCKER_BASE) $(MAKE) -j$(J) $(filter -j%,$(MAKEFLAGS)) "
    "-f $(firstword $(MAKEFILE_LIST)) $(MFLAGS) $(MAKEOVERRIDES) "
    "CONTAINER=1 piper-x86_64-configure",
    "$(DOCKER_BASE) $(MAKE) -j$(J) $(filter -j%,$(MAKEFLAGS)) "
    "-C $(OBJ)/obj-piper-x86_64 "
    "-f CMakeFiles/piper_phonemize_external.dir/build.make "
    "$(MFLAGS) CONTAINER=1 "
    "p/src/piper_phonemize_external-stamp/"
    "piper_phonemize_external-download",
):
    if expected not in body:
        raise SystemExit("Piper preparation is not run in the offline container")
if 'git -C "$ROOT_DIR" grep -I' not in privacy_text:
    raise SystemExit("privacy gate does not scan all tracked public text")
if "-g '*.md'" in privacy_text:
    raise SystemExit("privacy gate still relies on a file-extension allowlist")
PY

"$ROOT_DIR/tests/tooling/resume-source-barrier.sh"

printf 'Resumable-build fixture checks passed without configuring or compiling Proton.\n'
