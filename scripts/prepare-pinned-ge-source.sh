#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
STATE_DIR=""
PATCH_ROOT="$ROOT_DIR/patches"
PATCH_SERIES="$ROOT_DIR/patches/series"
FFMPEG_SECURITY_PATCH_ROOT="$ROOT_DIR/patches/ffmpeg-security"
FFMPEG_SECURITY_PATCH_SERIES="$ROOT_DIR/patches/ffmpeg-security/series"
FFMPEG_CRYPTO_BUILD_PATCH_ROOT="$ROOT_DIR/patches/ffmpeg-build"
FFMPEG_CRYPTO_BUILD_PATCH_SERIES="$ROOT_DIR/patches/ffmpeg-build/series"
PROTON_LAUNCHER_PATCH="$ROOT_DIR/patches/proton/0001-launcher-add-scoped-xrizer-mode.patch"

usage() {
  cat <<'EOF'
Usage: prepare-pinned-ge-source.sh [OPTIONS]

Generate and execute a fail-fast copy of the pinned GE patch driver. The copy
inserts the project RTSP series immediately after GE's ge-video-rework series,
before autoreconf/request generation, and invokes make_requests through Perl
for NixOS. The accepted upstream driver is never edited in place.

Options:
  --source DIR      fully materialized pinned GE checkout
  --state-dir DIR   ignored audit/output directory
  --patch-root DIR  root for paths listed by the RTSP series
  --series FILE     project patch series
  --config FILE     pinned config (default: active exact snapshot)
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
    --state-dir)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      STATE_DIR="$2"
      shift 2
      ;;
    --patch-root)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_ROOT="$2"
      shift 2
      ;;
    --series)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_SERIES="$2"
      shift 2
      ;;
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PINNED_GE_CONFIG="$2"
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
[[ -n "$STATE_DIR" ]] || STATE_DIR="$(pinned_ge_default_state_dir)"
SOURCE_DIR="$(realpath -m -- "$SOURCE_DIR")"
STATE_DIR="$(realpath -m -- "$STATE_DIR")"
PATCH_ROOT="$(realpath -m -- "$PATCH_ROOT")"
PATCH_SERIES="$(realpath -m -- "$PATCH_SERIES")"

for command in autoreconf diff patch perl python3 rg; do
  command -v "$command" >/dev/null 2>&1 \
    || pinned_ge_fail "required preparation command is unavailable: $command"
done

pinned_ge_require_source_identity "$SOURCE_DIR"
pinned_ge_require_original_build_rules "$SOURCE_DIR"
pinned_ge_require_registered_submodules "$SOURCE_DIR"
[[ -z "$(pinned_ge_git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || pinned_ge_fail "pinned GE source must be pristine before patch preparation"

series_digest="$(pinned_ge_series_digest "$PATCH_ROOT" "$PATCH_SERIES")"
ffmpeg_security_series_digest=""
ffmpeg_crypto_build_series_digest=""
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  ffmpeg_security_series_digest="$(pinned_ge_series_digest \
    "$FFMPEG_SECURITY_PATCH_ROOT" "$FFMPEG_SECURITY_PATCH_SERIES")"
  pinned_ge_ffmpeg_security_series_records \
    "$FFMPEG_SECURITY_PATCH_ROOT" "$FFMPEG_SECURITY_PATCH_SERIES" >/dev/null
  ffmpeg_crypto_build_series_digest="$(pinned_ge_series_digest \
    "$FFMPEG_CRYPTO_BUILD_PATCH_ROOT" "$FFMPEG_CRYPTO_BUILD_PATCH_SERIES")"
  pinned_ge_ffmpeg_crypto_build_patch_record \
    "$FFMPEG_CRYPTO_BUILD_PATCH_ROOT" \
    "$FFMPEG_CRYPTO_BUILD_PATCH_SERIES" >/dev/null
fi
mkdir -p "$STATE_DIR"
GENERATED_DRIVER="$STATE_DIR/protonprep-valve-staging.generated.sh"
PATCH_LOG="$STATE_DIR/patch-driver.log"
PATCH_AUDIT="$STATE_DIR/rtsp-patch-audit.tsv"
SOURCE_AUDIT="$STATE_DIR/wine-source-audit.tsv"
FFMPEG_SECURITY_PATCH_AUDIT="$STATE_DIR/ffmpeg-security-patch-audit.tsv"
FFMPEG_SOURCE_AUDIT="$STATE_DIR/ffmpeg-source-audit.tsv"
FFMPEG_CRYPTO_BUILD_AUDIT="$STATE_DIR/ffmpeg-crypto-build-audit.tsv"
STATE_FILE="$STATE_DIR/prepared-source.tsv"
GE_DIFF_CHECK_BASELINE="$STATE_DIR/ge-pre-rtsp-diff-check.txt"
GE_DIFF_CHECK_STATUS="$STATE_DIR/ge-pre-rtsp-diff-check.status"
GE_MEDIA_CLEANUP_AUDIT="$STATE_DIR/ge-media-cleanup-audit.tsv"
GE_PATCH_OVERLAP_AUDIT="$STATE_DIR/ge-patch-overlap-audit.tsv"
for output in "$GENERATED_DRIVER" "$PATCH_LOG" "$PATCH_AUDIT" \
    "$SOURCE_AUDIT" "$FFMPEG_SECURITY_PATCH_AUDIT" \
    "$FFMPEG_SOURCE_AUDIT" "$FFMPEG_CRYPTO_BUILD_AUDIT" \
    "$STATE_FILE" "$GE_DIFF_CHECK_BASELINE" \
    "$GE_DIFF_CHECK_STATUS" "$GE_MEDIA_CLEANUP_AUDIT" \
    "$GE_PATCH_OVERLAP_AUDIT"; do
  [[ ! -L "$output" ]] || pinned_ge_fail "refusing symlinked preparation output: $output"
done

python3 - "$SOURCE_DIR/patches/protonprep-valve-staging.sh" \
  "$GENERATED_DRIVER" "$PINNED_GE_HAS_PATCH_OVERLAP" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1])
output = Path(sys.argv[2])
has_patch_overlap = sys.argv[3] == "1"
text = source.read_text()

if output.is_symlink():
    raise SystemExit("refusing to replace a generated-driver symlink")
if output.exists():
    output.unlink()

if not text.startswith("#!/bin/bash\n"):
    raise SystemExit("unexpected GE patch-driver interpreter line")
text = text.replace("#!/bin/bash\n", "#!/usr/bin/env bash\nset -euo pipefail\n", 1)

patch_call = "patch -Np1 <"
if text.count(patch_call) != 2:
    raise SystemExit("expected exactly two GE patch command forms")
text = text.replace(patch_call, "patch -Np1 --batch <")

protonfixes_call = '    apply_all_in_dir "../patches/protonfixes/"\n'
if text.count(protonfixes_call) != 1:
    raise SystemExit("expected exactly one GE protonfixes patch application point")
if has_patch_overlap:
    protonfixes_hook = (
        '    : "${RTSP_GE_PATCH_OVERLAP_HOOK:'
        '?RTSP_GE_PATCH_OVERLAP_HOOK is required}"\n'
        '    bash "$RTSP_GE_PATCH_OVERLAP_HOOK"\n'
    )
    text = text.replace(protonfixes_call, protonfixes_hook, 1)

video_call = '    apply_all_in_dir "../patches/ge-video-rework/"\n'
if text.count(video_call) != 1:
    raise SystemExit("expected exactly one GE video-rework application point")
hook = (
    video_call
    + "\n"
    + '    echo "WINE: -RTSP- apply pinned project patch series"\n'
    + '    : "${RTSP_PATCH_HOOK:?RTSP_PATCH_HOOK is required}"\n'
    + '    bash "$RTSP_PATCH_HOOK"\n'
)
text = text.replace(video_call, hook, 1)

request_call = "    ./tools/make_requests\n"
if text.count(request_call) != 1:
    raise SystemExit("expected exactly one Wine make_requests invocation")
text = text.replace(request_call, "    perl ./tools/make_requests\n", 1)

output.write_text(text)
output.chmod(0o555)
PY

export RTSP_PATCH_ROOT="$PATCH_ROOT"
export RTSP_PATCH_SERIES="$PATCH_SERIES"
export RTSP_PATCH_HOOK="$ROOT_DIR/scripts/apply-pinned-rtsp-series.sh"
export RTSP_PATCH_AUDIT="$PATCH_AUDIT"
export RTSP_GE_DIFF_CHECK_BASELINE="$GE_DIFF_CHECK_BASELINE"
export RTSP_GE_DIFF_CHECK_STATUS="$GE_DIFF_CHECK_STATUS"
export RTSP_GE_SOURCE_ROOT="$SOURCE_DIR"
export RTSP_GE_MEDIA_CLEANUP_AUDIT="$GE_MEDIA_CLEANUP_AUDIT"
export RTSP_GE_PATCH_OVERLAP_HOOK="$ROOT_DIR/scripts/audit-pinned-ge-patch-overlap.sh"
export RTSP_GE_PATCH_OVERLAP_AUDIT="$GE_PATCH_OVERLAP_AUDIT"
export PINNED_GE_CONFIG="$PINNED_GE_CONFIG_PATH"

set +e
(
  cd "$SOURCE_DIR"
  bash "$GENERATED_DRIVER"
) >"$PATCH_LOG" 2>&1
driver_status=$?
set -e

rejects="$({
  find "$SOURCE_DIR" -path '*/.git' -prune -o -type f -name '*.rej' -print
} | LC_ALL=C sort)"
if [[ -n "$rejects" ]]; then
  printf 'Patch rejects remain:\n%s\n' "$rejects" >&2
  pinned_ge_fail "GE or project patch driver left rejected hunks; see $PATCH_LOG"
fi
if rg -n 'FAILED|malformed patch|can.t find file to patch|Reversed .* patch' \
    "$PATCH_LOG" >&2; then
  pinned_ge_fail "patch-driver log contains a patch failure signature: $PATCH_LOG"
fi
[[ "$driver_status" == 0 ]] \
  || pinned_ge_fail "fail-fast GE patch driver exited $driver_status; see $PATCH_LOG"
[[ -s "$PATCH_AUDIT" ]] || pinned_ge_fail "strict RTSP patch audit was not produced"
[[ -s "$GE_MEDIA_CLEANUP_AUDIT" && ! -L "$GE_MEDIA_CLEANUP_AUDIT" ]] \
  || pinned_ge_fail "pinned current-GE media-cleanup audit was not produced"
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  [[ -s "$GE_PATCH_OVERLAP_AUDIT" && ! -L "$GE_PATCH_OVERLAP_AUDIT" ]] \
    || pinned_ge_fail "pinned GE patch-overlap audit was not produced"
fi

if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  bash "$ROOT_DIR/scripts/apply-pinned-ffmpeg-security-series.sh" \
    --source "$SOURCE_DIR" --state-dir "$STATE_DIR" \
    --patch-root "$FFMPEG_SECURITY_PATCH_ROOT" \
    --series "$FFMPEG_SECURITY_PATCH_SERIES" \
    --config "$PINNED_GE_CONFIG_PATH"
  [[ -s "$FFMPEG_SECURITY_PATCH_AUDIT" \
      && ! -L "$FFMPEG_SECURITY_PATCH_AUDIT" ]] \
    || pinned_ge_fail "strict FFmpeg security patch audit was not produced"

  crypto_record="$(pinned_ge_ffmpeg_crypto_build_patch_record \
    "$FFMPEG_CRYPTO_BUILD_PATCH_ROOT" \
    "$FFMPEG_CRYPTO_BUILD_PATCH_SERIES")"
  IFS=$'\t' read -r crypto_patch_entry crypto_patch_origin \
    crypto_patch_sha256 <<<"$crypto_record"
  crypto_patch_file="$FFMPEG_CRYPTO_BUILD_PATCH_ROOT/$crypto_patch_entry"
  pinned_ge_git -C "$SOURCE_DIR" apply --check --whitespace=error-all -- \
    "$crypto_patch_file"
  pinned_ge_git -C "$SOURCE_DIR" apply --whitespace=error-all -- \
    "$crypto_patch_file"
  ffmpeg_crypto_build_makefile_blob="$(pinned_ge_git -C "$SOURCE_DIR" \
    hash-object --no-filters -- Makefile.in)"
  [[ "$ffmpeg_crypto_build_makefile_blob" \
      == "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" ]] \
    || pinned_ge_fail "FFmpeg crypto-enabled Makefile differs from its pin"
  crypto_enable_count="$(rg -c \
    '^[[:space:]]*--enable-protocol=crypto[[:space:]]*\\$' \
    "$SOURCE_DIR/Makefile.in" || true)"
  [[ "$crypto_enable_count" == 1 ]] \
    || pinned_ge_fail "FFmpeg crypto protocol is not enabled exactly once"
  {
    printf 'ffmpeg_crypto_build_audit_version\t1\n'
    printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
    printf 'patch_origin\t%s\n' "$crypto_patch_origin"
    printf 'patch_path\tpatches/ffmpeg-build/%s\n' "$crypto_patch_entry"
    printf 'patch_sha256\t%s\n' "$crypto_patch_sha256"
    printf 'series_sha256\t%s\n' "$PIN_FFMPEG_CRYPTO_BUILD_SERIES_SHA256"
    printf 'series_digest\t%s\n' "$ffmpeg_crypto_build_series_digest"
    printf 'touched_path\tMakefile.in\n'
    printf 'final_makefile_blob\t%s\n' "$ffmpeg_crypto_build_makefile_blob"
    printf 'crypto_enable_count\t1\n'
    printf 'application\tpassed\n'
  } >"$FFMPEG_CRYPTO_BUILD_AUDIT"
  chmod 0644 "$FFMPEG_CRYPTO_BUILD_AUDIT"
fi

[[ -f "$GE_DIFF_CHECK_BASELINE" && ! -L "$GE_DIFF_CHECK_BASELINE" ]] \
  || pinned_ge_fail "GE pre-RTSP diff-check baseline was not produced"
[[ -f "$GE_DIFF_CHECK_STATUS" && ! -L "$GE_DIFF_CHECK_STATUS" ]] \
  || pinned_ge_fail "GE pre-RTSP diff-check status was not produced"
ge_diff_check_status="$(cat "$GE_DIFF_CHECK_STATUS")"
[[ "$ge_diff_check_status" == 0 || "$ge_diff_check_status" == 2 ]] \
  || pinned_ge_fail "invalid GE pre-RTSP diff-check status: $ge_diff_check_status"
final_diff_check="$(mktemp "${TMPDIR:-/tmp}/rtsp-ge-final-diff-check.XXXXXX")"
trap 'rm -f -- "$final_diff_check"' EXIT
set +e
git -C "$SOURCE_DIR/wine" diff --check >"$final_diff_check" 2>&1
final_diff_check_status=$?
set -e
[[ "$final_diff_check_status" == "$ge_diff_check_status" ]] \
  || pinned_ge_fail "final Wine diff-check status differs from the GE hook boundary"
cmp -s -- "$GE_DIFF_CHECK_BASELINE" "$final_diff_check" \
  || pinned_ge_fail "final Wine diff-check output differs from the GE hook boundary"

# At the hook boundary, Wine's tracked generated configure script still
# reflects the pristine tree. GE's driver regenerates it immediately after the
# RTSP hook. Require the final generated source to contain no surviving
# WineGStreamer reference outside the now-evidence-only dormant directory.
set +e
winegstreamer_references="$(
  pinned_ge_git -C "$SOURCE_DIR/wine" grep -n -I -e winegstreamer -- . \
    ':!dlls/winegstreamer/**' 2>&1
)"
winegstreamer_reference_status=$?
set -e
case "$winegstreamer_reference_status" in
  1) ;;
  0)
    printf '%s\n' "$winegstreamer_references" >&2
    pinned_ge_fail "final generated Wine source still references WineGStreamer"
    ;;
  *)
    printf '%s\n' "$winegstreamer_references" >&2
    pinned_ge_fail "could not audit final WineGStreamer references"
    ;;
esac
"$ROOT_DIR/scripts/audit-patched-wine.sh" --require-alpha-series --require-a311-series \
  --require-a312-series \
  --series "$PATCH_SERIES" \
  --wine-tree "$SOURCE_DIR/wine" \
  >"$SOURCE_AUDIT"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" \
    --require-security-series >"$FFMPEG_SOURCE_AUDIT"
else
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" >/dev/null
fi

if [[ "$PIN_PROTON_LAUNCHER_POLICY" == scoped-xrizer ]]; then
  [[ -f "$PROTON_LAUNCHER_PATCH" && ! -L "$PROTON_LAUNCHER_PATCH" ]] \
    || pinned_ge_fail "Proton XRizer launcher patch is absent or unsafe"
  pinned_ge_git -C "$SOURCE_DIR" apply --check -- "$PROTON_LAUNCHER_PATCH"
  pinned_ge_git -C "$SOURCE_DIR" apply -- "$PROTON_LAUNCHER_PATCH"
else
  pinned_ge_require_upstream_launcher "$SOURCE_DIR" "$SOURCE_DIR/proton"
fi
python3 - "$SOURCE_DIR/proton" <<'PY'
from pathlib import Path
import sys

launcher = Path(sys.argv[1])
compile(launcher.read_text(), str(launcher), "exec")
PY

# GE's Meson rule invokes Ninja directly, so GNU Make's jobserver does not
# bound it. Normalize that one accepted line in the disposable source tree;
# the immutable upstream rule and its digest remain recorded in the pin file.
BUILD_RULES="$SOURCE_DIR/make/rules-meson.mk"
python3 - "$BUILD_RULES" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = '\tninja -C "$$($(2)_$(3)_OBJ)" install\n'
new = '\tninja -j"$$(NINJA_JOBS)" -C "$$($(2)_$(3)_OBJ)" install\n'
if text.count(old) != 1:
    raise SystemExit("expected exactly one unbounded GE Meson/Ninja invocation")
text = text.replace(old, new, 1)
path.write_text(text)
PY

git -C "$SOURCE_DIR" diff --check
normalized_rules_sha256="$(pinned_ge_sha256 "$BUILD_RULES")"
[[ "$normalized_rules_sha256" == "$PIN_RULES_MESON_NORMALIZED_SHA256" ]] \
  || pinned_ge_fail "Meson/Ninja normalization does not match the accepted result"
rg -Fq 'ninja -j"$$(NINJA_JOBS)" -C "$$($(2)_$(3)_OBJ)" install' \
  "$BUILD_RULES" \
  || pinned_ge_fail "Meson/Ninja job normalization is absent"

root_state_digest="$(pinned_ge_repo_state_digest "$SOURCE_DIR")"
wine_state_digest="$(pinned_ge_repo_state_digest "$SOURCE_DIR/wine")"
ffmpeg_state_digest=""
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  ffmpeg_state_digest="$(pinned_ge_repo_state_digest "$SOURCE_DIR/ffmpeg")"
fi
dxvk_state_digest="$(pinned_ge_repo_state_digest "$SOURCE_DIR/dxvk")"
protonfixes_state_digest="$(pinned_ge_repo_state_digest "$SOURCE_DIR/protonfixes")"
proton_launcher_patch_sha256="$(pinned_ge_launcher_patch_sha256)"

generated_driver_sha256="$(pinned_ge_sha256 "$GENERATED_DRIVER")"
patch_audit_sha256="$(pinned_ge_sha256 "$PATCH_AUDIT")"
source_audit_sha256="$(pinned_ge_sha256 "$SOURCE_AUDIT")"
ffmpeg_security_patch_audit_sha256=""
ffmpeg_source_audit_sha256=""
ffmpeg_crypto_build_audit_sha256=""
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  ffmpeg_security_patch_audit_sha256="$(pinned_ge_sha256 \
    "$FFMPEG_SECURITY_PATCH_AUDIT")"
  ffmpeg_source_audit_sha256="$(pinned_ge_sha256 "$FFMPEG_SOURCE_AUDIT")"
  ffmpeg_crypto_build_audit_sha256="$(pinned_ge_sha256 \
    "$FFMPEG_CRYPTO_BUILD_AUDIT")"
fi
ge_diff_check_baseline_sha256="$(pinned_ge_sha256 "$GE_DIFF_CHECK_BASELINE")"
ge_media_cleanup_audit_sha256="$(pinned_ge_sha256 "$GE_MEDIA_CLEANUP_AUDIT")"
ge_media_cleanup_digest="$(pinned_ge_state_value \
  "$GE_MEDIA_CLEANUP_AUDIT" normalization_digest)"
ge_patch_overlap_audit_sha256=""
ge_patch_overlap_component_commit=""
ge_patch_overlap_target_blob=""
ge_patch_overlap_declared_target_blob=""
ge_patch_overlap_residual_diff_sha256=""
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  ge_patch_overlap_audit_sha256="$(pinned_ge_sha256 "$GE_PATCH_OVERLAP_AUDIT")"
  patch_overlap_manifest="$(pinned_ge_patch_overlap_manifest_path)"
  ge_patch_overlap_component_commit="$(pinned_ge_state_value \
    "$patch_overlap_manifest" component_commit)"
  ge_patch_overlap_target_blob="$(pinned_ge_state_value \
    "$patch_overlap_manifest" effective_target_blob)"
  ge_patch_overlap_declared_target_blob="$(pinned_ge_state_value \
    "$patch_overlap_manifest" declared_series_target_blob)"
  ge_patch_overlap_residual_diff_sha256="$(pinned_ge_state_value \
    "$patch_overlap_manifest" residual_diff_sha256)"
fi
source_realpath="$(cd "$SOURCE_DIR" && pwd -P)"
{
  if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
    printf 'prepared_source_version\t2\n'
  else
    printf 'prepared_source_version\t1\n'
  fi
  printf 'pin_id\t%s\n' "$PIN_ID"
  printf 'pin_kind\t%s\n' "$PIN_KIND"
  printf 'source_commit\t%s\n' "$PIN_SOURCE_COMMIT"
  printf 'source_tree\t%s\n' "$PIN_SOURCE_TREE"
  printf 'source_path\t%s\n' "$source_realpath"
  printf 'package_release\t%s\n' "$PIN_PACKAGE_RELEASE"
  printf 'upstream_patch_driver_sha256\t%s\n' "$PIN_PATCH_DRIVER_SHA256"
  printf 'generated_patch_driver_sha256\t%s\n' "$generated_driver_sha256"
  printf 'patch_series_digest\t%s\n' "$series_digest"
  printf 'proton_launcher_patch\t%s\n' \
    "$(pinned_ge_launcher_patch_record)"
  printf 'proton_launcher_patch_sha256\t%s\n' "$proton_launcher_patch_sha256"
  printf 'patch_audit_sha256\t%s\n' "$patch_audit_sha256"
  printf 'source_audit_sha256\t%s\n' "$source_audit_sha256"
  if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
    printf 'ffmpeg_security_fix_commits\t%s\n' \
      "$PIN_FFMPEG_SECURITY_FIX_COMMITS"
    printf 'ffmpeg_security_series_sha256\t%s\n' \
      "$PIN_FFMPEG_SECURITY_SERIES_SHA256"
    printf 'ffmpeg_security_series_digest\t%s\n' \
      "$ffmpeg_security_series_digest"
    printf 'ffmpeg_security_patch_audit_sha256\t%s\n' \
      "$ffmpeg_security_patch_audit_sha256"
    printf 'ffmpeg_source_audit_sha256\t%s\n' "$ffmpeg_source_audit_sha256"
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
      "$ffmpeg_crypto_build_series_digest"
    printf 'ffmpeg_crypto_build_audit_sha256\t%s\n' \
      "$ffmpeg_crypto_build_audit_sha256"
    printf 'ffmpeg_crypto_build_makefile_blob\t%s\n' \
      "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB"
  fi
  printf 'ge_pre_rtsp_diff_check_status\t%s\n' "$ge_diff_check_status"
  printf 'ge_pre_rtsp_diff_check_sha256\t%s\n' "$ge_diff_check_baseline_sha256"
  printf 'ge_media_cleanup_normalization_digest\t%s\n' "$ge_media_cleanup_digest"
  printf 'ge_media_cleanup_audit_sha256\t%s\n' "$ge_media_cleanup_audit_sha256"
  if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
    printf 'ge_patch_overlap_mode\tstock-effective-exact-skip\n'
    printf 'ge_patch_overlap_manifest_sha256\t%s\n' \
      "$PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256"
    printf 'ge_patch_overlap_audit_sha256\t%s\n' \
      "$ge_patch_overlap_audit_sha256"
    printf 'ge_patch_overlap_component_commit\t%s\n' \
      "$ge_patch_overlap_component_commit"
    printf 'ge_patch_overlap_target_blob\t%s\n' \
      "$ge_patch_overlap_target_blob"
    printf 'ge_patch_overlap_declared_target_blob\t%s\n' \
      "$ge_patch_overlap_declared_target_blob"
    printf 'ge_patch_overlap_residual_diff_sha256\t%s\n' \
      "$ge_patch_overlap_residual_diff_sha256"
  fi
  printf 'final_winegstreamer_external_references\t0\n'
  printf 'final_diff_check_baseline_match\tpassed\n'
  printf 'upstream_rules_meson_sha256\t%s\n' "$PIN_RULES_MESON_SHA256"
  printf 'normalized_rules_meson_sha256\t%s\n' "$normalized_rules_sha256"
  printf 'root_repo_state_sha256\t%s\n' "$root_state_digest"
  printf 'wine_repo_state_sha256\t%s\n' "$wine_state_digest"
  if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
    printf 'ffmpeg_repo_state_sha256\t%s\n' "$ffmpeg_state_digest"
  fi
  printf 'dxvk_repo_state_sha256\t%s\n' "$dxvk_state_digest"
  printf 'protonfixes_repo_state_sha256\t%s\n' "$protonfixes_state_digest"
  printf 'make_requests_interpreter\tperl\n'
  printf 'upstream_patch_mode\tfail-fast-batch\n'
  printf 'rtsp_hook_order\tafter-ge-video-rework-before-autoreconf\n'
  printf 'ninja_job_source\tmake-variable-NINJA_JOBS\n'
} >"$STATE_FILE"

printf 'Prepared pinned GE source: %s\n' "$SOURCE_DIR"
printf 'Patch log:                %s\n' "$PATCH_LOG"
printf 'Strict patch audit:       %s\n' "$PATCH_AUDIT"
printf 'GE media-cleanup audit:   %s\n' "$GE_MEDIA_CLEANUP_AUDIT"
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  printf 'GE patch-overlap audit:   %s\n' "$GE_PATCH_OVERLAP_AUDIT"
fi
printf 'Wine source audit:        %s\n' "$SOURCE_AUDIT"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  printf 'FFmpeg security audit:     %s\n' "$FFMPEG_SECURITY_PATCH_AUDIT"
  printf 'FFmpeg source audit:       %s\n' "$FFMPEG_SOURCE_AUDIT"
  printf 'FFmpeg crypto-build audit: %s\n' "$FFMPEG_CRYPTO_BUILD_AUDIT"
fi
printf 'Prepared-state record:    %s\n' "$STATE_FILE"
printf 'No compilation, packaging, Steam access, or publication occurred.\n'
