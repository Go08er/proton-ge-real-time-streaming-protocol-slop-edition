#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
PATCH_SERIES="$ROOT_DIR/patches/series"
WOW64_PROBE="$ROOT_DIR/patches/candidates/0001-winedmo-use-destroy-params-for-wow64-demuxer.patch"
PROTON_LAUNCHER_PATCH="$ROOT_DIR/patches/proton/0001-launcher-add-scoped-xrizer-mode.patch"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: verify-preparation.sh [--config FILE] [--series FILE]

Run the static preparation, safety, and model gates. The default remains the
immutable A3.12 control; pass the candidate config explicitly when qualifying
a candidate. Historical pins can select their archived patch series explicitly.
Set PREPARED_SOURCE explicitly to also check the selected GE11-6 Wine source
contracts; this does not substitute for complete preparation provenance.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PINNED_GE_CONFIG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --series)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      PATCH_SERIES="$2"
      shift 2
      ;;
    *)
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PROJECT_HOME_PATTERN='(?<![A-Za-z0-9._~-])(?:/home/(?!runner/work/llvm-mingw(?:/|[^A-Za-z0-9._-]|$))[^ /]+|/Users/[^ /]+)'

mapfile -t SHELL_FILES < <(
  find "$ROOT_DIR/scripts" "$ROOT_DIR/tests/media" "$ROOT_DIR/tests/tooling" \
    "$ROOT_DIR/tests/media-engine-pause-scrub" \
    -maxdepth 1 \
    -type f -name '*.sh' -print | LC_ALL=C sort
)
[[ ${#SHELL_FILES[@]} -gt 0 ]] || fail "no shell files found"
bash -n "${SHELL_FILES[@]}"
bash -n "$ROOT_DIR"/config/*.env

if command -v shellcheck >/dev/null 2>&1; then
  # Config variables are consumed through strict indirect lookup in the shared
  # helper, which ShellCheck cannot model (SC2034). Follow that helper and keep
  # every other warning/error fatal.
  shellcheck -x -P "$ROOT_DIR/scripts" --severity=warning -e SC2034 \
    "$ROOT_DIR"/scripts/*.sh
  (
    cd "$ROOT_DIR/tests/media"
    shellcheck -x ./*.sh
  )
  (
    cd "$ROOT_DIR/tests/tooling"
    shellcheck -x ./*.sh
  )
  (
    cd "$ROOT_DIR/tests/media-engine-pause-scrub"
    shellcheck -x ./run_runtime_probe.sh ./test_runner_guards.sh
  )
else
  printf 'NOTE: shellcheck is unavailable outside the project Nix shell.\n'
fi

if command -v nix-instantiate >/dev/null 2>&1; then
  nix-instantiate --parse "$ROOT_DIR/shell.nix" >/dev/null
fi

"$ROOT_DIR/scripts/check-ge-release.sh" --help >/dev/null
"$ROOT_DIR/scripts/record-release-manifest.sh" --help >/dev/null
"$ROOT_DIR/scripts/materialize-ge-wine.sh" --help >/dev/null
"$ROOT_DIR/scripts/audit-patched-wine.sh" --help >/dev/null
"$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --help >/dev/null
"$ROOT_DIR/scripts/apply-pinned-ffmpeg-security-series.sh" --help >/dev/null
"$ROOT_DIR/scripts/verify-artifact-archive.py" --help >/dev/null
"$ROOT_DIR/scripts/hash-source-tree.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/bootstrap-pinned-ge.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/materialize-pinned-ge-submodules.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/prefetch-pinned-ge-contrib.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/prepare-pinned-ge-source.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/build-pinned-ge.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/record-pinned-ge-resume-state.sh" --help >/dev/null
bash "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" --help >/dev/null
for offline_object_probe in \
    bootstrap-sources.sh \
    materialize-ge-wine.sh \
    verify-baseline.sh \
    verify-research-snapshot.sh; do
  rg -Fq 'export GIT_NO_LAZY_FETCH=1' \
    "$ROOT_DIR/scripts/$offline_object_probe" \
    || fail "$offline_object_probe permits an implicit promisor fetch"
done
for bootstrap_gate in \
    'cat-file -e "$PIN_SOURCE_TREE^{tree}"' \
    'local clone omitted the pinned GE tree object' \
    '[[ ! -L "$SOURCE_DIR" ]]' \
    'pinned_ge_require_absent_path "pinned GE source destination" "$SOURCE_DIR"' \
    'mkdir -- "$SOURCE_DIR"' \
    'if [[ "$status" != 0 && "$CREATED_SOURCE" == 1 ]]'; do
  rg -Fq "$bootstrap_gate" "$ROOT_DIR/scripts/bootstrap-pinned-ge.sh" \
    || fail "offline bootstrap omits fail-closed object gate: $bootstrap_gate"
done
for materializer_gate in \
    'REFERENCE_SOURCES+=("$2")' \
    'pinned_ge_find_complete_reference_repo' \
    'pinned_ge_require_existing_submodule' \
    'GIT_NO_LAZY_FETCH=1' \
    'clone --local --no-hardlinks --no-checkout' \
    'Surviving Git alternates files: 0'; do
  rg -Fq "$materializer_gate" \
    "$ROOT_DIR/scripts/materialize-pinned-ge-submodules.sh" \
    || fail "submodule materializer omits local-reference gate: $materializer_gate"
done

if [[ -d "$SOURCE_DIR/wine/.git" || -f "$SOURCE_DIR/wine/.git" ]]; then
  if git -C "$SOURCE_DIR/wine" apply --check "$WOW64_PROBE"; then
    printf 'WOW64 teardown defect remains present in the local Wine research source; Alpha patch 3 carries the correction.\n'
  elif sed -n '/static NTSTATUS wow64_demuxer_destroy/,/^}/p' \
      "$SOURCE_DIR/wine/dlls/winedmo/unixlib.c" \
      | rg -q 'struct demuxer_destroy_params'; then
    printf 'WOW64 teardown correction is already implemented by the local Wine source.\n'
  else
    fail "WOW64 teardown probe neither applies nor finds an upstream correction"
  fi
else
  printf 'NOTE: Wine source is unavailable; WOW64 teardown probe skipped.\n'
fi

if [[ -e "$SOURCE_DIR/ffmpeg/.git" ]]; then
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" >/dev/null
else
  printf 'NOTE: FFmpeg source is unavailable; source dependency audit skipped.\n'
fi

pinned_ge_load_config
[[ "$PIN_SCHEMA_VERSION" == 5 ]] \
  || fail "selected security-refresh build does not use pin schema 5"
[[ "$PINNED_GE_HAS_RETAINED_RESUME" == 0 ]] \
  || fail "fresh selected pin carries misleading old-base resume provenance"
case "$PIN_ID" in
  ge-master-snapshot-20260723-a312)
    SELECTED_PROFILE=a312
    [[ "$PIN_BUILD_NAME" \
        == Proton-RTSP-on-GE11-A3.12-NetworkError-bb1caad3 \
        && "$PIN_KIND" == development-snapshot \
        && "$PIN_SOURCE_REF" \
          == bb1caad333b08cf87d49d0f794a538502d992eae \
        && "$PIN_SOURCE_COMMIT" \
          == bb1caad333b08cf87d49d0f794a538502d992eae \
        && "$PIN_SOURCE_TREE" \
          == 818b5a31bfa063e860f6817a1c1ac3ff55316c02 \
        && "$PIN_WINE_COMMIT" \
          == fc39a66977983a16a31318f98386386ec2dd6b55 \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]] \
      || fail "immutable A3.12 control identity changed"
    patch_overlap_manifest="$(pinned_ge_patch_overlap_manifest_path)"
    [[ "$(pinned_ge_patch_overlap_records \
        "$patch_overlap_manifest" | wc -l)" == 2 ]] \
      || fail "A3.12 GE patch-overlap manifest is not the reviewed pair"
    ;;
  ge-proton11-3-a314)
    SELECTED_PROFILE=a314
    [[ "$PIN_BUILD_NAME" \
        == Proton-RTSP-on-GE11-A3.14-GE-Proton11-3-8c8003f7 \
        && "$PIN_KIND" == release-tag \
        && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-3 \
        && "$PIN_SOURCE_COMMIT" \
          == 8c8003f7f5473d883fbe1bc7ac070c79955754e8 \
        && "$PIN_SOURCE_TREE" \
          == 1e55008bb5995b5cfaf2643e6ce1c44f4e11e208 \
        && "$PIN_WINE_COMMIT" \
          == 9578fa3613f3379179b576968bc77c8161ab6ea8 \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
          == bfb3c4316dc348e93e5571c822a634ce98f5ac6c \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
      || fail "immutable A3.14 GE-Proton11-3 identity changed"
    ;;
  ge-proton11-3-a315)
    SELECTED_PROFILE=a315
    [[ "$PIN_BUILD_NAME" \
        == Proton-RTSP-on-GE11-A3.15-GE-Proton11-3-dabf93db \
        && "$PIN_KIND" == release-tag \
        && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-3 \
        && "$PIN_SOURCE_COMMIT" \
          == 8c8003f7f5473d883fbe1bc7ac070c79955754e8 \
        && "$PIN_SOURCE_TREE" \
          == 1e55008bb5995b5cfaf2643e6ce1c44f4e11e208 \
        && "$PIN_WINE_COMMIT" \
          == 9578fa3613f3379179b576968bc77c8161ab6ea8 \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
          == bfb3c4316dc348e93e5571c822a634ce98f5ac6c \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
      || fail "immutable A3.15 GE-Proton11-3 identity changed"
    ;;
  ge-proton11-3-a316)
    SELECTED_PROFILE=a316
    [[ "$PIN_BUILD_NAME" \
        == Proton-RTSP-on-GE11-A3.16-GE-Proton11-3-3d49c777 \
        && "$PIN_KIND" == release-tag \
        && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-3 \
        && "$PIN_SOURCE_COMMIT" \
          == 8c8003f7f5473d883fbe1bc7ac070c79955754e8 \
        && "$PIN_SOURCE_TREE" \
          == 1e55008bb5995b5cfaf2643e6ce1c44f4e11e208 \
        && "$PIN_WINE_COMMIT" \
          == 9578fa3613f3379179b576968bc77c8161ab6ea8 \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
          == bfb3c4316dc348e93e5571c822a634ce98f5ac6c \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
      || fail "immutable A3.16 GE-Proton11-3 identity changed"
    ;;
  ge-proton11-6-a319)
    SELECTED_PROFILE=a319
    [[ "$PIN_BUILD_NAME" \
        == Proton-RTSP-on-GE11-A3.19-GE-Proton11-6-7e88ceff \
        && "$PIN_KIND" == release-tag \
        && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-6 \
        && "$PIN_SOURCE_COMMIT" \
          == 7e88cefffc122ea1584c2156b8d7bae6cf69b2a7 \
        && "$PIN_SOURCE_TREE" \
          == 851680369dc46489360e4f67bfa3acc6b701ef4a \
        && "$PIN_WINE_COMMIT" \
          == 9358696fe9a2261329f4a83aa6a65fd436106154 \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
          == 284157f8120d27f8bb7291acf662e799cddf717b \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
      || fail "immutable A3.19 GE-Proton11-6 identity changed"
    ;;
  ge-proton11-6-a320|ge-proton11-6-a321|ge-proton11-6-a322|ge-proton11-6-a322-r2|ge-proton11-6-a323|ge-proton11-6-se1)
    SELECTED_PROFILE=${PIN_ID##*-}
    candidate_number=${SELECTED_PROFILE#a3}
    if [[ "$PIN_ID" == ge-proton11-6-a322-r2 ]]; then
      SELECTED_PROFILE=a322
      candidate_number=22-R2
    fi
    expected_build_name="Proton-RTSP-on-GE11-A3.$candidate_number-GE-Proton11-6-7e88ceff"
    if [[ "$PIN_ID" == ge-proton11-6-se1 ]]; then
      # SE1 retains the exact A3.23 Wine series; only launcher policy changes.
      SELECTED_PROFILE=a323
      expected_build_name=proton-ge-11-6-rtsp-se1
      [[ "$PIN_PROTON_LAUNCHER_POLICY" == upstream ]] \
        || fail "SE1 must use the unmodified upstream launcher"
    fi
    [[ "$PIN_BUILD_NAME" \
        == "$expected_build_name" \
        && "$PIN_KIND" == release-tag \
        && "$PIN_SOURCE_REF" == refs/tags/GE-Proton11-6 \
        && "$PIN_SOURCE_COMMIT" \
          == 7e88cefffc122ea1584c2156b8d7bae6cf69b2a7 \
        && "$PIN_SOURCE_TREE" \
          == 851680369dc46489360e4f67bfa3acc6b701ef4a \
        && "$PIN_WINE_COMMIT" \
          == 9358696fe9a2261329f4a83aa6a65fd436106154 \
        && "$PIN_FFMPEG_CRYPTO_BUILD_MAKEFILE_BLOB" \
          == 284157f8120d27f8bb7291acf662e799cddf717b \
        && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 ]] \
      || fail "$SELECTED_PROFILE GE-Proton11-6 candidate identity changed"
    ;;
  *)
    fail "verify-preparation does not recognize selected pin $PIN_ID"
    ;;
esac
[[ "$PIN_PACKAGE_RELEASE" == 0 ]] \
  || fail "selected game-test series must remain PACKAGE_RELEASE=0"
[[ "$PIN_DEFAULT_JOBS" == 16 && "$PIN_DEFAULT_MAKE_JOBS" == 8 \
    && "$PIN_DEFAULT_NINJA_JOBS" == 2 ]] \
  || fail "full-system scheduler defaults are not global=16, Make=8, Ninja=2"
[[ "$PIN_WINE_STAGING_COMMIT" == 6cc805ea57132eeaf44764e9213823c9b8d0d300 \
    && "$PIN_FFMPEG_COMMIT" == 9047fa1b084f76b1b4d065af2d743df1b40dfb56 ]] \
  || fail "selected shared component pins differ from the reviewed GE delta"
default_schedule="$(pinned_ge_resolve_job_schedule \
  "$PIN_DEFAULT_JOBS" "" "")"
[[ "$default_schedule" == $'16\t8\t2\t16' ]] \
  || fail "default scheduler decomposition is not 8x2 within global budget 16"
lower_schedule="$(pinned_ge_resolve_job_schedule 4 "" "")"
[[ "$lower_schedule" == $'4\t4\t1\t4' ]] \
  || fail "lower global budget did not derive a conservative schedule"
if pinned_ge_validate_job_schedule 16 9 2 >/dev/null 2>&1; then
  fail "oversubscribed 9x2 schedule was accepted under global budget 16"
fi
if pinned_ge_resolve_job_schedule 17 "" "" >/dev/null 2>&1; then
  fail "global schedule above the accepted cap 16 was accepted"
fi
rg -Fq 'unset MAKEFLAGS MFLAGS GNUMAKEFLAGS' \
  "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper does not clear ambient Make parallelism"
if rg -q '^export MAKEFLAGS=' "$ROOT_DIR/scripts/build-pinned-ge.sh"; then
  fail "build wrapper replaces GNU Make's recursive jobserver"
fi
rg -Fq 'SHELL="$HOST_BASH" -j"$MAKE_JOBS"' \
  "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper does not use the orchestrator Make budget"
rg -Fq "'override MAKEOVERRIDES := \$(filter-out SHELL=%,\$(MAKEOVERRIDES))'" \
  "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper does not keep the host SHELL override out of recursive container Make"
rg -Fq 'J="$MAKE_JOBS"' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "GE container Make budget is not pinned to orchestrator jobs"
rg -Fq 'NINJA_JOBS="$NINJA_JOBS"' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "per-component Ninja budget is not passed to GE"
rg -Fq 'pinned_ge_require_local_steamrt_image "$PODMAN_BIN"' \
  "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper does not bind the local SteamRT image ID"
rg -Fq 'DOCKER_OPTS=--pull=never' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper permits an implicit SteamRT image pull"
rg -Fq -- '--network=none' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "build wrapper permits container network access"
rg -Fq 'REEXEC_ARGS+=(--resume)' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "resume mode is lost during the low-impact re-exec"
rg -Fq 'pinned_ge_require_configure_state' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "resume mode does not require a configure fingerprint"
rg -Fq 'Reusing verified configure result:' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "resume mode does not identify configure-result reuse"
rg -Fq 'pinned-resume-all-source' "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "resume mode omits the dedicated all-source Make pass"
rg -Fq 'pinned_ge_require_resume_source_barrier' \
  "$ROOT_DIR/scripts/build-pinned-ge.sh" \
  || fail "resume mode omits the post-source deletion/rsync barrier"
for record in \
    'build_invocation_version\t6' \
    'source_path\t%s' \
    'build_path\t%s' \
    'state_path\t%s' \
    'cache_home\t%s' \
    'steamrt_image_id\t%s' \
    'scheduler_policy\tmake-times-ninja-at-most-global' \
    'recursive_make_parallelism\tjobserver' \
    'ge_media_cleanup_manifest_sha256\t%s' \
    'ge_patch_overlap_mode\tstock-effective-exact-skip' \
    'ge_patch_overlap_manifest_sha256\t%s' \
    'ge_patch_overlap_audit_sha256\t%s' \
    'proton_launcher_patch_sha256\t%s' \
    'ffmpeg_security_fix_commits\t%s' \
    'ffmpeg_security_series_digest\t%s' \
    'ffmpeg_security_patch_audit_sha256\t%s' \
    'ffmpeg_repo_state_sha256\t%s' \
    'ffmpeg_crypto_build_patch_sha256\t%s' \
    'ffmpeg_crypto_build_audit_sha256\t%s' \
    'rtsp_hook_order\tafter-ge-video-rework-before-autoreconf' \
    'final_diff_check_baseline_match\tpassed' \
    'final_winegstreamer_external_references\t0'; do
  rg -Fq "$record" "$ROOT_DIR/scripts/build-pinned-ge.sh" \
    || fail "build invocation omits scheduler record: $record"
done
rg -Fq 'pinned_ge_validate_job_schedule' \
  "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  || fail "artifact verifier does not revalidate the recorded scheduler"
artifact_verifier="$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh"
for privacy_gate in \
    'python3 - "$TOOL_DIR" "${HOME:-}"' \
    'LLVM_MINGW_PREFIX = b"/home/runner/work/llvm-mingw/"' \
    'WINDOWS_PROFILE_ALLOWLIST = {b"public", b"steamuser"}' \
    'rb"|(?P<root>/root)(?=[/\\]))"' \
    'home_pattern = re.compile(re.escape(builder_home), re.IGNORECASE)' \
    'UTF16_LE_ASCII' \
    'UTF16_BE_ASCII' \
    'mmap.mmap(' \
    'os.scandir(directory)' \
    'absolute symlink:' \
    'out-of-root symlink:' \
    'broken symlink:' \
    'special file (mode' \
    'setuid/setgid file:' \
    'world-writable file:' \
    'privacy scan could not read every regular file' \
    'artifact filesystem or privacy validation failed'; do
  rg -Fq -- "$privacy_gate" "$artifact_verifier" \
    || fail "artifact verifier omits fail-closed privacy gate: $privacy_gate"
done
for script in build-pinned-ge.sh verify-pinned-ge-artifact.sh; do
  rg -Fq 'pinned_ge_require_prepared_media_provenance "$SOURCE_DIR" "$STATE_DIR"' \
    "$ROOT_DIR/scripts/$script" \
    || fail "$script does not validate prepared media-cleanup provenance"
  rg -Fq 'pinned_ge_require_prepared_patch_overlap_provenance' \
    "$ROOT_DIR/scripts/$script" \
    || fail "$script does not validate inherited GE patch-overlap provenance"
  rg -Fq 'pinned_ge_require_prepared_ffmpeg_security_provenance' \
    "$ROOT_DIR/scripts/$script" \
    || fail "$script does not validate prepared FFmpeg security provenance"
  rg -Fq 'pinned_ge_require_prepared_ffmpeg_crypto_build_provenance' \
    "$ROOT_DIR/scripts/$script" \
    || fail "$script does not validate prepared FFmpeg crypto-build provenance"
done
if rg -Fq 'git -C "$SOURCE_DIR/wine" diff --check' \
    "$ROOT_DIR/scripts/build-pinned-ge.sh"; then
  fail "build wrapper still rejects GE's exact recorded Wine diff-check baseline"
fi
prepared_source="${PREPARED_SOURCE:-$(pinned_ge_default_source)}"
prepared_state="${PREPARED_STATE:-$(pinned_ge_default_state_dir)}"
if [[ -e "$prepared_source/wine/.git" \
    && -f "$prepared_state/prepared-source.tsv" ]]; then
  pinned_ge_require_prepared_media_provenance \
    "$prepared_source" "$prepared_state" "$PATCH_SERIES"
  if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
    pinned_ge_require_prepared_patch_overlap_provenance \
      "$prepared_source" "$prepared_state"
  fi
  printf 'Prepared media cleanup, diff baseline, and zero-reference provenance passed.\n'
else
  printf 'NOTE: complete pinned prepared tree is unavailable; live media provenance check skipped.\n'
fi
printf 'Pinned scheduler uses global=16, Make=8, Ninja=2 and rejects product overflow.\n'
series_entries="$(pinned_ge_series_entries \
  "$ROOT_DIR/patches" "$PATCH_SERIES")"
[[ -n "$series_entries" ]] || fail "pinned Alpha patch series is empty"
mapfile -t series_paths <<<"$series_entries"
expected_base_series=(
  experimental/0001-winedmo-open-rtsp-over-tcp.patch
  experimental/0002-winedmo-interrupt-network-demux-and-report-live-seekability.patch
  experimental/0003-winedmo-use-destroy-params-for-wow64-demuxer.patch
  experimental/0004-winedmo-async-http-and-repair-vod-seeking.patch
  experimental/0005-winedmo-stream-progressive-http-vod.patch
  experimental/0006-winedmo-recover-interrupted-vod-seeks.patch
  experimental/0007-mf-reprime-and-resume-after-vod-seeks.patch
  experimental/0008-winedmo-preserve-audio-timeline-across-flush.patch
  experimental/0009-mf-sar-preserve-running-state-across-flush.patch
  experimental/0011-mf-fix-initial-presentation-clock-offset.patch
  experimental/0013-mfmediaengine-reconcile-pause-scrub-intent.patch
  experimental/0014-mfmediaengine-preserve-pending-start-position.patch
  experimental/0015-mf-order-immediate-replacement-start.patch
  experimental/0017-winedmo-propagate-terminal-network-read-errors.patch
  experimental/0018-winewayland-advertise-wine-vr-device-extensions.patch
)
if [[ "$SELECTED_PROFILE" == a319 || "$SELECTED_PROFILE" == a320 || "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
  expected_base_series=("${expected_base_series[@]/experimental\//ge-proton11-6/}")
  expected_base_series+=(ge-proton11-6/0020-quiet-ge-media-diagnostics.patch)
  if [[ "$SELECTED_PROFILE" == a320 || "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
    expected_base_series[6]=ge-proton11-6-a320/0007-mf-reprime-and-resume-after-vod-seeks.patch
    expected_base_series[13]=ge-proton11-6-a320/0017-winedmo-propagate-terminal-network-read-errors.patch
    expected_base_series+=(
      ge-proton11-6-a320/0022-mfmediaengine-preserve-selected-frame-tick-result.patch
      ge-proton11-6-a320/0023-mfmediaengine-check-seekability-before-recovery-reset.patch
    )
    if [[ "$PIN_ID" == ge-proton11-6-a322-r2 || "$SELECTED_PROFILE" == a323 ]]; then
      expected_base_series+=(ge-proton11-6-a322-r2/0025-winedmo-normalize-hls-vod-timeline.patch)
    fi
    if [[ "$SELECTED_PROFILE" == a323 ]]; then
      expected_base_series+=(ge-proton11-6-a323/0026-mf-replenish-output-buffers-after-preroll.patch)
    fi
    expected_series_count=$((${#expected_base_series[@]} + 1))
    [[ "${#series_paths[@]}" == "$expected_series_count" ]] \
      || fail "$PIN_ID requires the reviewed $expected_series_count-patch GE-Proton11-6 series"
    if [[ "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
      media_profile=$SELECTED_PROFILE
      [[ "$media_profile" != a323 ]] || media_profile=a322
      expected_base_series[4]=ge-proton11-6-$media_profile/0005-winedmo-stream-progressive-http-vod.patch
    fi
  else
    [[ "${#series_paths[@]}" == 17 ]] \
      || fail "A3.19 requires the seventeen-patch GE-Proton11-6 series"
  fi
  expected_base_series+=(ge-proton11-6/0019-winedmo-trace-bounded-rtsp-drain-state.patch)
else
  [[ "${#series_paths[@]}" == 15 || "${#series_paths[@]}" == 16 ]] \
    || fail "historical pin requires its 15/16-patch series; select --series patches/series-ge-proton11-3"
fi
for index in "${!expected_base_series[@]}"; do
  [[ "${series_paths[$index]}" == "${expected_base_series[$index]}" ]] \
    || fail "reviewed Wine media base changed at series position $((index + 1))"
done
if [[ "${#series_paths[@]}" == 16 ]]; then
  [[ "${series_paths[15]}" \
      == experimental/0019-winedmo-trace-bounded-rtsp-drain-state.patch ]] \
    || fail "bounded drain diagnostic is not the final Wine media patch"
elif [[ "$SELECTED_PROFILE" == a314 || "$SELECTED_PROFILE" == a315 \
    || "$SELECTED_PROFILE" == a316 ]]; then
  fail "selected diagnostic candidate requires the complete sixteen-patch series"
fi
current_series_digest="$(
  pinned_ge_series_digest "$ROOT_DIR/patches" "$PATCH_SERIES"
)"
if [[ "$SELECTED_PROFILE" == a315 ]]; then
  [[ "$PIN_BUILD_NAME" \
      == "Proton-RTSP-on-GE11-A3.15-GE-Proton11-3-${current_series_digest:0:8}" ]] \
    || fail "A3.15 build name does not identify the active Wine series digest"
elif [[ "$SELECTED_PROFILE" == a316 ]]; then
  [[ "$PIN_BUILD_NAME" \
      == "Proton-RTSP-on-GE11-A3.16-GE-Proton11-3-${current_series_digest:0:8}" ]] \
    || fail "A3.16 build name does not identify the active Wine series digest"
fi
for a312_gate in \
    scripts/prepare-pinned-ge-source.sh \
    scripts/build-pinned-ge.sh \
    scripts/verify-pinned-ge-artifact.sh; do
  rg -Fq -- '--require-a311-series' "$ROOT_DIR/$a312_gate" \
    || fail "A3.11 foundation audit is not fail-closed in $a312_gate"
  rg -Fq -- '--require-a312-series' "$ROOT_DIR/$a312_gate" \
    || fail "A3.12 source audit is not fail-closed in $a312_gate"
done
for git_patch_gate in \
    'git apply --check --whitespace=error-all' \
    'if ! git apply --numstat -z'; do
  rg -Fq -- "$git_patch_gate" "$ROOT_DIR/scripts/apply-pinned-rtsp-series.sh" \
    || fail "strict RTSP patch hook omits fail-closed Git gate: $git_patch_gate"
done
rg -Fq -- 'patch -Np1 --dry-run --batch --fuzz=0' \
  "$ROOT_DIR/scripts/apply-pinned-rtsp-series.sh" \
  || fail "strict RTSP patch hook omits fail-closed GNU dry-run gate"
for overlap_gate in \
    'pinned_ge_require_source_identity "$SOURCE_ROOT"' \
    'pinned_ge_require_registered_submodules "$SOURCE_ROOT"' \
    'GE protonfixes patch directory differs from the exact two-patch overlap' \
    'patch -Np1 --batch --fuzz=0' \
    'DECLARED_INTERMEDIATE_BLOB' \
    'RESIDUAL_DIFF_SHA256' \
    'stock-effective-exact-skip' \
    'source_change\tnone'; do
  rg -Fq -- "$overlap_gate" \
    "$ROOT_DIR/scripts/audit-pinned-ge-patch-overlap.sh" \
    || fail "GE patch-overlap hook omits fail-closed gate: $overlap_gate"
done
rg -Fq 'expected exactly one GE protonfixes patch application point' \
  "$ROOT_DIR/scripts/prepare-pinned-ge-source.sh" \
  || fail "generated GE driver does not replace only the reviewed overlap call"
ffmpeg_security_records="$(pinned_ge_ffmpeg_security_series_records \
  "$ROOT_DIR/patches/ffmpeg-security" \
  "$ROOT_DIR/patches/ffmpeg-security/series")"
[[ "$(wc -l <<<"$ffmpeg_security_records")" == 4 ]] \
  || fail "FFmpeg security series must contain the exact four reviewed patches"
ffmpeg_security_paths="$(pinned_ge_ffmpeg_security_touched_paths \
  "$ROOT_DIR/patches/ffmpeg-security" \
  "$ROOT_DIR/patches/ffmpeg-security/series")"
[[ "$ffmpeg_security_paths" == \
    $'libavcodec/magicyuv.c\nlibavformat/version_major.h' ]] \
  || fail "FFmpeg security series changed paths outside the reviewed pair"
for security_gate in \
    'apply --check' \
    '--whitespace=error-all' \
    'PIN_FFMPEG_SECURITY_MAGICYUV_BLOB' \
    'PIN_FFMPEG_SECURITY_TLS_VERSION_BLOB' \
    'pinned_ge_repo_state_digest "$SOURCE_DIR/ffmpeg"'; do
  rg -Fq -- "$security_gate" \
    "$ROOT_DIR/scripts/apply-pinned-ffmpeg-security-series.sh" \
    || fail "strict FFmpeg application omits gate: $security_gate"
done
rg -Fq 'CONFIG_MAGICYUV_DECODER 1' \
  <(tr '\n' ' ' <"$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh") \
  || fail "artifact verifier does not bind the enabled MagicYUV decoder to its fixes"
rg -Fq 'pinned_ge_require_exact_ffmpeg_define' \
  "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  || fail "artifact verifier does not use exact-one macro assertions"
rg -Fq 'pinned_ge_require_ffmpeg_config_surface' \
  "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  || fail "artifact verifier does not invoke the exact FFmpeg surface gate"
crypto_build_record="$(pinned_ge_ffmpeg_crypto_build_patch_record \
  "$ROOT_DIR/patches/ffmpeg-build" "$ROOT_DIR/patches/ffmpeg-build/series")"
[[ "$(wc -l <<<"$crypto_build_record")" == 1 ]] \
  || fail "FFmpeg crypto-build series must remain exactly one pinned patch"
for tls_backend_fact in \
    'CONFIG_HLS_DEMUXER' 'CONFIG_CRYPTO_PROTOCOL' 'CONFIG_GNUTLS' \
    'CONFIG_LIBZMQ' 'CONFIG_ZMQ_FILTER' \
    'CONFIG_FFMPEG CONFIG_FFPLAY CONFIG_FFPROBE' \
    'CONFIG_TLS_PROTOCOL' 'CONFIG_LIBTLS' 'CONFIG_MBEDTLS' \
    'CONFIG_OPENSSL' 'CONFIG_SCHANNEL' 'CONFIG_SECURETRANSPORT' \
    'CONFIG_FILE_PROTOCOL CONFIG_DATA_PROTOCOL CONFIG_HTTPPROXY_PROTOCOL'; do
  rg -Fq "$tls_backend_fact" \
    "$ROOT_DIR/scripts/pinned-ge-common.sh" \
    || fail "artifact verifier omits TLS backend binding: $tls_backend_fact"
done
rg -Fq -- "-iname '*zmqsend*'" \
  "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  || fail "artifact verifier does not reject a packaged zmqsend tool"
for source_state_gate in \
    'prepared_source_version' \
    'pinned_ge_require_source_identity "$SOURCE_DIR"' \
    'pinned_ge_require_registered_submodules "$SOURCE_DIR"' \
    'wine_repo_state_sha256' 'dxvk_repo_state_sha256' \
    'protonfixes_repo_state_sha256' 'ffmpeg_repo_state_sha256'; do
  rg -Fq "$source_state_gate" \
    "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
    || fail "artifact verifier omits prepared source-state gate: $source_state_gate"
done
rg -Fq 'verify-artifact-archive.py' \
  "$ROOT_DIR/scripts/verify-pinned-ge-artifact.sh" \
  || fail "artifact verifier does not compare the archive with the game-test tree"
for archive_gate in \
    'duplicate archive member' \
    'unsupported archive node type' \
    'archive symlink differs' \
    'archive mode differs' \
    'archive payload differs' \
    'non-zero payload after its tar end marker' \
    'archive omits'; do
  rg -Fq "$archive_gate" "$ROOT_DIR/scripts/verify-artifact-archive.py" \
    || fail "archive/tree verifier omits fail-closed gate: $archive_gate"
done
build_contrib_manifest="$(pinned_ge_build_contrib_manifest_path)"
build_contrib_entries="$(pinned_ge_build_contrib_entries "$build_contrib_manifest")"
mapfile -t parsed_build_contrib_entries <<<"$build_contrib_entries"
manifest_build_contrib_entry_count="$(
  awk -F '\t' '$1 == "entry" { count++ } END { print count + 0 }' \
    "$build_contrib_manifest"
)"
[[ "${#parsed_build_contrib_entries[@]}" \
    == "$manifest_build_contrib_entry_count" ]] \
  || fail \
    "parsed build-contrib entry count differs from the selected pin manifest"
if [[ "$PIN_PROTON_LAUNCHER_POLICY" == scoped-xrizer ]]; then
[[ -f "$PROTON_LAUNCHER_PATCH" && ! -L "$PROTON_LAUNCHER_PATCH" ]] \
  || fail "separate Proton XRizer launcher patch is absent or unsafe"
if [[ -e "$prepared_source/.git" \
    && -f "$prepared_state/prepared-source.tsv" ]]; then
  git -C "$prepared_source" apply --check --reverse -- "$PROTON_LAUNCHER_PATCH"
  [[ "$(pinned_ge_state_value \
      "$prepared_state/prepared-source.tsv" proton_launcher_patch_sha256)" \
      == "$(pinned_ge_sha256 "$PROTON_LAUNCHER_PATCH")" ]] \
    || fail "prepared launcher-patch digest differs from project patch"
fi
elif [[ -e "$prepared_source/.git" && -f "$prepared_state/prepared-source.tsv" ]]; then
  pinned_ge_require_upstream_launcher "$prepared_source" "$prepared_source/proton"
  [[ "$(pinned_ge_state_value "$prepared_state/prepared-source.tsv" proton_launcher_patch)" == none \
      && "$(pinned_ge_state_value "$prepared_state/prepared-source.tsv" proton_launcher_patch_sha256)" \
        == "$(pinned_ge_launcher_patch_sha256)" ]] \
    || fail "prepared upstream launcher has inconsistent no-patch provenance"
fi
if [[ "$PIN_PROTON_LAUNCHER_POLICY" == upstream ]]; then
  printf '%s Wine media patches; upstream Proton launcher (no XRizer compatibility flag). PACKAGE_RELEASE=0.\n' "${#series_paths[@]}"
elif [[ "$SELECTED_PROFILE" == a320 || "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
  printf '%s %s GE-Proton11-6 Wine media patches and the separate Proton XRizer launcher patch selected under PACKAGE_RELEASE=0.\n' "${#series_paths[@]}" "$SELECTED_PROFILE"
elif [[ "$SELECTED_PROFILE" == a319 ]]; then
  printf 'Seventeen GE-Proton11-6 Wine media patches and the separate Proton XRizer launcher patch selected under PACKAGE_RELEASE=0.\n'
elif [[ "${#series_paths[@]}" == 16 ]]; then
  printf 'The reviewed fifteen-patch Wine media foundation, the bounded drain diagnostic, and one separate Proton XRizer launcher patch are active under PACKAGE_RELEASE=0.\n'
else
  printf 'Fifteen Wine media patches and one separate Proton XRizer launcher patch are active under PACKAGE_RELEASE=0.\n'
fi

(
  PINNED_GE_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260713-a311.env"
  pinned_ge_load_config
  old_contrib_manifest="$(pinned_ge_build_contrib_manifest_path)"
  old_contrib_entries="$(pinned_ge_build_contrib_entries "$old_contrib_manifest")"
  [[ "$PIN_BUILD_NAME" == Proton-RTSP-on-GE11-A3.11-ReplacementClock-9fad3bbe \
      && "$PIN_SOURCE_COMMIT" == 9fad3bbe270409e67a8d2f4d123afc73d848306a \
      && "$PINNED_GE_HAS_RETAINED_RESUME" == 1 \
      && "$PINNED_GE_HAS_PATCH_OVERLAP" == 0 \
      && "$(wc -l <<<"$old_contrib_entries")" == 6 ]] \
    || fail "immutable old-base A3.11 control no longer loads independently"
)
(
  PINNED_GE_CONFIG="$ROOT_DIR/config/ge-master-snapshot-20260713.env"
  pinned_ge_load_config
  [[ "$PIN_SCHEMA_VERSION" == 4 \
      && "$PIN_BUILD_NAME" == Proton-RTSP-on-GE11-A3-9fad3bbe \
      && -z "${PIN_FFMPEG_SECURITY_FIX_COMMITS:-}" ]] \
    || fail "immutable legacy A3 config no longer loads as schema 4"
)
printf 'Old-base A3/A3.11 controls remain selectable; the refreshed schema-5 pin retains the exact FFmpeg security series.\n'

for privacy_fixture in \
    '/home/example/project' \
    '/Users/example/project' \
    'HOME=/home/example/project' \
    '](/home/example/project)' \
    'file:///home/example/project'; do
  printf '%s\n' "$privacy_fixture" \
    | rg --pcre2 -q -i "$PROJECT_HOME_PATTERN" \
    || fail "project privacy gate missed fixture: $privacy_fixture"
done
if printf 'https://vrchat.com/home/launch?worldId=fixture\n' \
    | rg --pcre2 -q -i "$PROJECT_HOME_PATTERN"; then
  fail "project privacy gate mistakes an HTTPS path for a local home path"
fi
if privacy_matches="$(git -C "$ROOT_DIR" grep -I -n -i -P \
    "$PROJECT_HOME_PATTERN" -- . \
    ':!scripts/verify-preparation.sh' \
    ':!scripts/verify-pinned-ge-artifact.sh')"; then
  printf '%s\n' "$privacy_matches" >&2
  fail "personal absolute path found in project-facing text"
else
  privacy_status=$?
  [[ "$privacy_status" == 1 ]] \
    || fail "could not scan every tracked public text file for personal paths"
fi

"$ROOT_DIR/tests/media/generate-fixture.sh" >/dev/null
python3 "$ROOT_DIR/tests/sar-flush-control/test_model.py" >/dev/null
python3 "$ROOT_DIR/tests/clock-start-control/test_model.py" >/dev/null
python3 "$ROOT_DIR/tests/pcm-probe-control/test_model.py" >/dev/null
python3 "$ROOT_DIR/tests/tooling/test_launcher_policy.py" >/dev/null
python3 "$ROOT_DIR/tests/avpro-audio-state/test_model.py" >/dev/null
python3 "$ROOT_DIR/tests/media-engine-pause-scrub/test_model.py" >/dev/null
python3 "$ROOT_DIR/tests/source-replacement-lifecycle/test_model.py" --series "$PATCH_SERIES" >/dev/null
if [[ "$SELECTED_PROFILE" == a319 || "$SELECTED_PROFILE" == a320 || "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
  integration_source_args=()
  if [[ -n "${PREPARED_SOURCE:-}" ]]; then
    [[ -f "$prepared_source/wine/dlls/mf/session.c" \
        && -f "$prepared_source/wine/dlls/mfmediaengine/main.c" \
        && -f "$prepared_source/wine/dlls/mfmediaengine/video_frame_sink.c" \
        && -f "$prepared_source/wine/dlls/winedmo/unix_demuxer.c" ]] \
      || fail "explicit PREPARED_SOURCE lacks the Wine sources required by integration tests"
    integration_source_args=(--wine-tree "$prepared_source/wine")
  fi
  python3 "$ROOT_DIR/tests/ge-proton11-6/test_integration.py" \
    "${integration_source_args[@]}" >/dev/null
  if [[ "$SELECTED_PROFILE" == a320 || "$SELECTED_PROFILE" == a321 || "$SELECTED_PROFILE" == a322 || "$SELECTED_PROFILE" == a323 ]]; then
    for regression in test_seek_restart.py test_frame_tick.py test_recovery_seek.py; do
      python3 "$ROOT_DIR/tests/vod-control/$regression" \
        "${integration_source_args[@]}" >/dev/null
    done
    python3 "$ROOT_DIR/tests/network-terminal-error/test_seek_failure.py" \
      "${integration_source_args[@]}" >/dev/null
  fi
  if [[ "$SELECTED_PROFILE" == a323 ]]; then
    python3 "$ROOT_DIR/tests/vod-control/test_preroll_buffers.py" \
      "${integration_source_args[@]}" >/dev/null
  fi
fi
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$ROOT_DIR/tests/media-engine-pause-scrub/test_runtime_parser.py" >/dev/null
"$ROOT_DIR/tests/media-engine-pause-scrub/test_runner_guards.sh" >/dev/null
"$ROOT_DIR/tests/tooling/config-security.sh"
"$ROOT_DIR/tests/tooling/bootstrap-ownership.sh"
"$ROOT_DIR/tests/tooling/archive_security.py"
"$ROOT_DIR/tests/tooling/resume-build.sh"
bash "$ROOT_DIR/tests/tooling/submodule-reference.sh"
bash "$ROOT_DIR/tests/tooling/prefetch-build-contrib.sh"
python3 "$ROOT_DIR/tests/tooling/test_upstream_component.py"
python3 "$ROOT_DIR/tests/tooling/test_media_provenance.py"

printf 'Preparation checks passed without building Proton.\n'
