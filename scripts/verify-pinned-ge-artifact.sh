#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

SOURCE_DIR=""
BUILD_DIR=""
STATE_DIR=""
PATCH_SERIES="$ROOT_DIR/patches/series"

usage() {
  cat <<'EOF'
Usage: verify-pinned-ge-artifact.sh [OPTIONS]

Verify the local pinned-snapshot game-test directory and redist archive without
running Wine, modifying Steam, or publishing anything.

Options:
  --source DIR      prepared pinned GE checkout
  --build-dir DIR   completed out-of-tree build directory
  --state-dir DIR   preparation/build audit directory
  --config FILE     pinned config (default: active exact snapshot)
  --series FILE     Wine patch series used to prepare the source
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
[[ -n "$SOURCE_DIR" ]] || SOURCE_DIR="$(pinned_ge_default_source)"
[[ -n "$BUILD_DIR" ]] || BUILD_DIR="$(pinned_ge_default_build_dir)"
[[ -n "$STATE_DIR" ]] || STATE_DIR="$(pinned_ge_default_state_dir)"
SOURCE_DIR="$(realpath -m -- "$SOURCE_DIR")"
BUILD_DIR="$(realpath -m -- "$BUILD_DIR")"
STATE_DIR="$(realpath -m -- "$STATE_DIR")"
PATCH_SERIES="$(realpath -e -- "$PATCH_SERIES")" \
  || pinned_ge_fail "could not resolve Wine patch series: $PATCH_SERIES"
[[ -f "$PATCH_SERIES" && ! -L "$PATCH_SERIES" ]] \
  || pinned_ge_fail "Wine patch series is not a regular file: $PATCH_SERIES"
for pair in \
    "GE source path:$SOURCE_DIR" "GE build path:$BUILD_DIR" \
    "GE state path:$STATE_DIR"; do
  pinned_ge_require_safe_absolute_path "${pair%%:*}" "${pair#*:}"
done

TOOL_DIR="$BUILD_DIR/$PIN_BUILD_NAME"
ARCHIVE="$BUILD_DIR/$PIN_BUILD_NAME.tar.gz"
SHA512="$BUILD_DIR/$PIN_BUILD_NAME.sha512sum"
SHA256="$BUILD_DIR/SHA256SUMS"
INVOCATION="$STATE_DIR/build-invocation.tsv"
CONFIGURE_STATE="$STATE_DIR/configure-state.tsv"
PROTON_LAUNCHER_PATCH="$ROOT_DIR/patches/proton/0001-launcher-add-scoped-xrizer-mode.patch"

for path in "$TOOL_DIR" "$ARCHIVE" "$SHA512" "$SHA256" "$INVOCATION" \
    "$CONFIGURE_STATE"; do
  [[ -e "$path" && ! -L "$path" ]] \
    || pinned_ge_fail "required build output is absent or is a symlink: $path"
done
[[ -d "$TOOL_DIR" ]] || pinned_ge_fail "game-test output is not a directory: $TOOL_DIR"
[[ -f "$ARCHIVE" && -f "$SHA512" && -f "$SHA256" ]] \
  || pinned_ge_fail "redist checksums are not regular files"
PREPARED_STATE="$STATE_DIR/prepared-source.tsv"
expected_prepared_source_version=1
[[ "$PIN_SCHEMA_VERSION" == 4 ]] || expected_prepared_source_version=2
[[ "$(pinned_ge_state_value "$PREPARED_STATE" prepared_source_version)" \
    == "$expected_prepared_source_version" ]] \
  || pinned_ge_fail \
    "prepared-source schema does not match pin schema $PIN_SCHEMA_VERSION"
pinned_ge_require_source_identity "$SOURCE_DIR"
pinned_ge_require_registered_submodules "$SOURCE_DIR"
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
CONTRIB_MANIFEST="$(pinned_ge_build_contrib_manifest_path)"
pinned_ge_require_complete_build_contrib "$SOURCE_DIR" "$CONTRIB_MANIFEST"
[[ "$(pinned_ge_repo_state_digest "$SOURCE_DIR" "$CONTRIB_MANIFEST")" \
    == "$(pinned_ge_state_value "$PREPARED_STATE" root_repo_state_sha256)" ]] \
  || pinned_ge_fail "built source differs beyond the validated build-contrib cache"
for repo_state_record in \
    'wine:wine_repo_state_sha256' \
    'dxvk:dxvk_repo_state_sha256' \
    'protonfixes:protonfixes_repo_state_sha256'; do
  repo_path="${repo_state_record%%:*}"
  repo_state_key="${repo_state_record#*:}"
  [[ "$(pinned_ge_repo_state_digest "$SOURCE_DIR/$repo_path")" \
      == "$(pinned_ge_state_value "$PREPARED_STATE" "$repo_state_key")" ]] \
    || pinned_ge_fail \
      "$repo_path source state differs from the exact prepared-state digest"
done
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  [[ "$(pinned_ge_repo_state_digest "$SOURCE_DIR/ffmpeg")" \
      == "$(pinned_ge_state_value "$PREPARED_STATE" ffmpeg_repo_state_sha256)" ]] \
    || pinned_ge_fail \
      "FFmpeg source state differs from the exact prepared-state digest"
fi
CACHE_HOME="$(realpath -e -- \
  "$(pinned_ge_state_value "$CONFIGURE_STATE" cache_home)")"
pinned_ge_require_safe_absolute_path "GE cache home" "$CACHE_HOME"
pinned_ge_require_configure_state \
  "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$INVOCATION" "$CACHE_HOME"

[[ "$(pinned_ge_state_value "$INVOCATION" pin_id)" == "$PIN_ID" ]] \
  || pinned_ge_fail "build invocation used a different pin"
[[ "$(pinned_ge_state_value "$INVOCATION" source_commit)" == "$PIN_SOURCE_COMMIT" ]] \
  || pinned_ge_fail "build invocation used a different source commit"
[[ "$(pinned_ge_state_value "$INVOCATION" build_name)" == "$PIN_BUILD_NAME" ]] \
  || pinned_ge_fail "build invocation used a different build name"
invocation_version="$(pinned_ge_state_value "$INVOCATION" build_invocation_version)"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  [[ "$invocation_version" == 6 ]] \
    || pinned_ge_fail "security-refresh artifact does not use invocation schema 6"
else
  [[ "$invocation_version" == 5 || "$invocation_version" == 3 ]] \
    || pinned_ge_fail "legacy artifact does not use a supported invocation schema"
fi
[[ "$(pinned_ge_state_value "$INVOCATION" package_release)" == 0 ]] \
  || pinned_ge_fail "artifact was not recorded as PACKAGE_RELEASE=0"
[[ "$(pinned_ge_state_value "$INVOCATION" target)" == redist ]] \
  || pinned_ge_fail "artifact was not produced by the redist target"
[[ "$(pinned_ge_state_value "$INVOCATION" steam_install)" == disabled ]] \
  || pinned_ge_fail "Steam-install state is not disabled"
[[ "$(pinned_ge_state_value "$INVOCATION" publication)" == disabled ]] \
  || pinned_ge_fail "publication state is not disabled"
jobs="$(pinned_ge_state_value "$INVOCATION" jobs)"
global_jobs="$(pinned_ge_state_value "$INVOCATION" global_jobs)"
make_jobs="$(pinned_ge_state_value "$INVOCATION" make_jobs)"
ninja_jobs="$(pinned_ge_state_value "$INVOCATION" ninja_jobs)"
[[ "$jobs" == "$global_jobs" ]] \
  || pinned_ge_fail "legacy jobs record does not match the global budget"
expected_product="$(pinned_ge_validate_job_schedule \
  "$global_jobs" "$make_jobs" "$ninja_jobs")"
pinned_ge_require_resume_invocation \
  "$SOURCE_DIR" "$BUILD_DIR" "$STATE_DIR" "$CACHE_HOME" \
  "$INVOCATION" "$PREPARED_STATE" \
  "$global_jobs" "$make_jobs" "$ninja_jobs" "$expected_product"
[[ "$(pinned_ge_state_value "$INVOCATION" scheduler_product)" == "$expected_product" ]] \
  || pinned_ge_fail "recorded scheduler product does not match Make x Ninja jobs"
[[ "$(pinned_ge_state_value "$INVOCATION" scheduler_policy)" \
    == make-times-ninja-at-most-global ]] \
  || pinned_ge_fail "build invocation did not record the nested-pool budget policy"
[[ "$(pinned_ge_state_value "$INVOCATION" recursive_make_parallelism)" == jobserver ]] \
  || pinned_ge_fail "recursive Make was not recorded as jobserver-managed"
for key in cargo_jobs_env cmake_jobs_env nix_build_cores_env; do
  [[ "$(pinned_ge_state_value "$INVOCATION" "$key")" == "$ninja_jobs" ]] \
    || pinned_ge_fail "$key did not receive the conservative component environment cap $ninja_jobs"
done
[[ "$(pinned_ge_state_value "$INVOCATION" proton_launcher_patch_sha256)" \
    == "$(pinned_ge_state_value "$PREPARED_STATE" proton_launcher_patch_sha256)" ]] \
  || pinned_ge_fail "artifact invocation launcher-patch digest differs from prepared state"
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  for key in \
      ffmpeg_security_fix_commits \
      ffmpeg_security_series_sha256 \
      ffmpeg_security_series_digest \
      ffmpeg_security_patch_audit_sha256 \
      ffmpeg_source_audit_sha256 \
      ffmpeg_repo_state_sha256 \
      ffmpeg_security_magicyuv_blob \
      ffmpeg_security_tls_version_blob \
      ffmpeg_crypto_build_origin \
      ffmpeg_crypto_build_patch_sha256 \
      ffmpeg_crypto_build_series_sha256 \
      ffmpeg_crypto_build_series_digest \
      ffmpeg_crypto_build_audit_sha256 \
      ffmpeg_crypto_build_makefile_blob; do
    [[ "$(pinned_ge_state_value "$INVOCATION" "$key")" \
        == "$(pinned_ge_state_value "$PREPARED_STATE" "$key")" ]] \
      || pinned_ge_fail "artifact invocation FFmpeg provenance differs for $key"
  done
fi
for key in \
    ge_pre_rtsp_diff_check_status \
    ge_pre_rtsp_diff_check_sha256 \
    ge_media_cleanup_normalization_digest \
    ge_media_cleanup_audit_sha256 \
    final_diff_check_baseline_match \
    final_winegstreamer_external_references; do
  [[ "$(pinned_ge_state_value "$INVOCATION" "$key")" \
      == "$(pinned_ge_state_value "$PREPARED_STATE" "$key")" ]] \
    || pinned_ge_fail "artifact invocation media provenance differs for $key"
done
[[ "$(pinned_ge_state_value "$INVOCATION" ge_media_cleanup_manifest_sha256)" \
    == "$PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256" ]] \
  || pinned_ge_fail "artifact invocation used a different media-cleanup manifest"
[[ "$(pinned_ge_state_value "$INVOCATION" rtsp_hook_order)" \
    == "$(pinned_ge_state_value "$PREPARED_STATE" rtsp_hook_order)" ]] \
  || pinned_ge_fail "artifact invocation RTSP hook order differs from prepared state"
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  for key in \
      ge_patch_overlap_mode \
      ge_patch_overlap_manifest_sha256 \
      ge_patch_overlap_audit_sha256 \
      ge_patch_overlap_component_commit \
      ge_patch_overlap_target_blob \
      ge_patch_overlap_declared_target_blob \
      ge_patch_overlap_residual_diff_sha256; do
    [[ "$(pinned_ge_state_value "$INVOCATION" "$key")" \
        == "$(pinned_ge_state_value "$PREPARED_STATE" "$key")" ]] \
      || pinned_ge_fail \
        "artifact invocation patch-overlap provenance differs for $key"
  done
fi

(
  cd "$BUILD_DIR"
  sha512sum -c "$(basename "$SHA512")"
  sha256sum -c "$(basename "$SHA256")"
)

python3 "$ROOT_DIR/scripts/verify-artifact-archive.py" \
  --archive "$ARCHIVE" --tool-dir "$TOOL_DIR" --root-name "$PIN_BUILD_NAME" \
  || pinned_ge_fail \
    "redist archive headers or payload differ from the verified game-test tree"

for path in \
  compatibilitytool.vdf version proton filelock.py LICENSE RTSP-GE-BUILD.txt \
  files/bin/wine files/bin/wineserver \
  files/lib/wine/i386-unix/winedmo.so \
  files/lib/wine/x86_64-unix/winedmo.so \
  files/lib/wine/i386-windows/winedmo.dll \
  files/lib/wine/x86_64-windows/winedmo.dll \
  files/lib/wine/i386-windows/mf.dll \
  files/lib/wine/x86_64-windows/mf.dll \
  files/lib/wine/i386-windows/mfmediaengine.dll \
  files/lib/wine/x86_64-windows/mfmediaengine.dll \
  files/lib/wine/i386-windows/mfplat.dll \
  files/lib/wine/x86_64-windows/mfplat.dll \
  files/lib/wine/i386-windows/mfreadwrite.dll \
  files/lib/wine/x86_64-windows/mfreadwrite.dll \
  files/lib/wine/i386-unix/winepulse.so \
  files/lib/wine/x86_64-unix/winepulse.so \
  files/lib/wine/i386-windows/winepulse.drv \
  files/lib/wine/x86_64-windows/winepulse.drv \
  files/lib/wine/i386-unix/winewayland.so \
  files/lib/wine/x86_64-unix/winewayland.so \
  files/lib/wine/i386-windows/winewayland.drv \
  files/lib/wine/x86_64-windows/winewayland.drv; do
  [[ -f "$TOOL_DIR/$path" ]] || pinned_ge_fail "required artifact file is absent: $path"
done

version_file="$TOOL_DIR/version"
version_line_count="$(wc -l <"$version_file")"
read -r version_timestamp version_name version_extra <"$version_file" || true
[[ "$version_line_count" == 1 ]] \
  || pinned_ge_fail "Proton version metadata must contain exactly one line"
[[ "$version_timestamp" =~ ^[0-9]+$ ]] \
  || pinned_ge_fail "Proton version metadata has a non-numeric build timestamp"
[[ "$version_name" == "$PIN_BUILD_NAME" ]] \
  || pinned_ge_fail "Proton version metadata does not use the exact build name"
[[ -z "$version_extra" ]] \
  || pinned_ge_fail "Proton version metadata contains unexpected extra fields"
# Scan every payload node and every byte of every regular file without ignore
# rules. Upstream llvm-mingw binaries legitimately retain their immutable
# public CI root, and Proton's template prefix legitimately names the generic
# Windows profiles steamuser and Public. No other home path is allowlisted.
# The archive helper has already proved its complete node inventory, modes,
# link targets, and regular-file bytes identical to this directory, so this
# single privacy scan applies to both distribution forms.
if ! python3 - "$TOOL_DIR" "${HOME:-}" <<'PY'
from pathlib import Path
import mmap
import os
import re
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
builder_home = os.fsencode(sys.argv[2]) if sys.argv[2] else b""
regular_files = []
filesystem_violations = []
walk_errors = []


def relative(path):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def record_filesystem(message):
    filesystem_violations.append(message)


stack = [root]
while stack:
    directory = stack.pop()
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        walk_errors.append(f"{relative(directory)}: {error}")
        continue
    for entry in entries:
        path = Path(entry.path)
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError as error:
            walk_errors.append(f"{relative(path)}: {error}")
            continue

        if stat.S_ISDIR(mode):
            if mode & (stat.S_ISUID | stat.S_ISGID):
                record_filesystem(f"setuid/setgid directory: {relative(path)}")
            if mode & stat.S_IWOTH:
                record_filesystem(f"world-writable directory: {relative(path)}")
            stack.append(path)
        elif stat.S_ISREG(mode):
            if mode & (stat.S_ISUID | stat.S_ISGID):
                record_filesystem(f"setuid/setgid file: {relative(path)}")
            if mode & stat.S_IWOTH:
                record_filesystem(f"world-writable file: {relative(path)}")
            regular_files.append(path)
        elif stat.S_ISLNK(mode):
            try:
                target = os.readlink(path)
            except OSError as error:
                walk_errors.append(f"{relative(path)}: {error}")
                continue
            if os.path.isabs(target):
                record_filesystem(f"absolute symlink: {relative(path)} -> {target}")
                continue
            lexical_target = Path(os.path.normpath(os.path.join(path.parent, target)))
            try:
                lexical_target.relative_to(root)
            except ValueError:
                record_filesystem(f"out-of-root symlink: {relative(path)} -> {target}")
                continue
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                record_filesystem(
                    f"broken symlink: {relative(path)} -> {target} ({error})"
                )
                continue
            try:
                resolved.relative_to(root)
            except ValueError:
                record_filesystem(
                    f"out-of-root symlink: {relative(path)} -> {target} ({resolved})"
                )
        else:
            record_filesystem(
                f"special file (mode {stat.filemode(mode)}): {relative(path)}"
            )

root_mode = root.stat().st_mode
if root_mode & (stat.S_ISUID | stat.S_ISGID):
    record_filesystem("setuid/setgid artifact root: .")
if root_mode & stat.S_IWOTH:
    record_filesystem("world-writable artifact root: .")

if walk_errors or filesystem_violations:
    for item in walk_errors:
        print(f"scan error: {item}", file=sys.stderr)
    for item in filesystem_violations:
        print(item, file=sys.stderr)
    raise SystemExit("artifact filesystem safety checks failed")

LLVM_MINGW_PREFIX = b"/home/runner/work/llvm-mingw/"
WINDOWS_PROFILE_ALLOWLIST = {b"public", b"steamuser"}
PATH_CONTINUATION = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+%/-\\"
)
PATH_END = rb"(?=$|[/\\\x00\r\n\t '\";:,)\]}])"
UNIX_PATH = re.compile(
    rb"(?:(?P<home>/home/(?P<home_user>[A-Za-z0-9_.-]+))" + PATH_END
    + rb"|(?P<users>/Users/(?P<users_user>[A-Za-z0-9_.-]+))" + PATH_END
    # Bare /root is also a common Windows command switch and document token.
    # A generic root-home path is unambiguous when it continues; HOME=/root is
    # still checked exactly below.
    + rb"|(?P<root>/root)(?=[/\\]))"
)
WINDOWS_PATH = re.compile(
    rb"(?i)(?P<windows>[a-z]:[\\/]+users[\\/]+"
    rb"(?P<windows_user>[a-z0-9_.-]+))" + PATH_END
)
UTF16_LE_ASCII = re.compile(rb"(?:[\x09\x20-\x7e]\x00){4,}")
UTF16_BE_ASCII = re.compile(rb"(?:\x00[\x09\x20-\x7e]){4,}")
home_pattern = re.compile(re.escape(builder_home), re.IGNORECASE) if builder_home else None
home_is_runner = builder_home.rstrip(b"/").lower() == b"/home/runner"
privacy_violations = set()
scan_errors = []
allowed_llvm_occurrences = 0


def has_absolute_boundary(view, start):
    if start == 0:
        return True
    prefix = bytes(view[max(0, start - 7):start]).lower()
    if prefix.endswith(b"file://"):
        return True
    return view[start - 1] not in PATH_CONTINUATION


def is_llvm_mingw(view, start):
    candidate = bytes(view[start:start + len(LLVM_MINGW_PREFIX)])
    return candidate == LLVM_MINGW_PREFIX


def add_violation(rel, kind, match, encoding):
    rendered = match.decode("ascii", errors="backslashreplace")
    privacy_violations.add((rel, kind, rendered, encoding))


def scan_view(view, rel, encoding):
    global allowed_llvm_occurrences
    if home_pattern is not None:
        for match in home_pattern.finditer(view):
            if not has_absolute_boundary(view, match.start()):
                continue
            if home_is_runner and is_llvm_mingw(view, match.start()):
                allowed_llvm_occurrences += 1
            else:
                add_violation(rel, "exact builder HOME", match.group(0), encoding)

    for match in UNIX_PATH.finditer(view):
        if not has_absolute_boundary(view, match.start()):
            continue
        if match.group("home") and is_llvm_mingw(view, match.start()):
            allowed_llvm_occurrences += 1
            continue
        add_violation(rel, "absolute Unix home path", match.group(0), encoding)

    for match in WINDOWS_PATH.finditer(view):
        if not has_absolute_boundary(view, match.start()):
            continue
        if match.group("windows_user").lower() in WINDOWS_PROFILE_ALLOWLIST:
            continue
        add_violation(rel, "absolute Windows user profile", match.group(0), encoding)


for path in sorted(regular_files):
    rel = relative(path)
    try:
        if path.stat().st_size == 0:
            continue
        with path.open("rb") as stream, mmap.mmap(
                stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            scan_view(data, rel, "bytes")
            for match in UTF16_LE_ASCII.finditer(data):
                scan_view(match.group(0)[::2], rel, "UTF-16LE")
            for match in UTF16_BE_ASCII.finditer(data):
                scan_view(match.group(0)[1::2], rel, "UTF-16BE")
    except (OSError, ValueError) as error:
        scan_errors.append(f"{rel}: {error}")

if scan_errors:
    for item in scan_errors:
        print(f"scan error: {item}", file=sys.stderr)
    raise SystemExit("privacy scan could not read every regular file")

if privacy_violations:
    violations = sorted(privacy_violations)
    for rel, kind, match, encoding in violations[:100]:
        print(f"{rel}: {kind} ({encoding}): {match}", file=sys.stderr)
    if len(violations) > 100:
        print(f"... {len(violations) - 100} additional matches", file=sys.stderr)
    raise SystemExit("artifact contains a personal home-directory path")

print(
    f"Scanned {len(regular_files)} regular files without ignore rules "
    f"({allowed_llvm_occurrences} allowed LLVM-MinGW paths)"
)
PY
then
  pinned_ge_fail "artifact filesystem or privacy validation failed"
fi

python3 - "$TOOL_DIR/proton" <<'PY'
from pathlib import Path
import sys

launcher = Path(sys.argv[1])
compile(launcher.read_text(), str(launcher), "exec")
PY
if [[ "$PIN_PROTON_LAUNCHER_POLICY" == upstream ]]; then
  pinned_ge_require_upstream_launcher "$SOURCE_DIR" "$TOOL_DIR/proton"
else
for launcher_policy in \
    'self.env.get("PROTON_XR_MODE", "auto").strip().lower()' \
    'self.xr_mode not in ("auto", "xrizer")' \
    'if self.xr_mode == "xrizer":' \
    'self.dlloverrides["wineopenxr"] = "d"' \
    'self.log_file.write("XR mode: " + self.xr_mode + "\n")'; do
  rg -Fq "$launcher_policy" "$TOOL_DIR/proton" \
    || pinned_ge_fail "artifact launcher omits scoped XRizer policy: $launcher_policy"
done
fi

internal_pattern="\"$PIN_BUILD_NAME\" // Internal name of this tool"
display_pattern="\"display_name\" \"$PIN_BUILD_NAME\""
internal_count="$(rg -F -c "$internal_pattern" \
  "$TOOL_DIR/compatibilitytool.vdf" || true)"
display_count="$(rg -F -c "$display_pattern" \
  "$TOOL_DIR/compatibilitytool.vdf" || true)"
[[ "$internal_count" == 1 && "$display_count" == 1 ]] \
  || pinned_ge_fail "compatibilitytool.vdf does not identify the pinned build exactly once"
provenance="$TOOL_DIR/RTSP-GE-BUILD.txt"
provenance_lines=(
    "Build name: $PIN_BUILD_NAME" \
    "Snapshot kind: $PIN_KIND" \
    "GE source: $PIN_SOURCE_URL" \
    "GE source label: $PIN_SOURCE_VERSION" \
    "GE commit: $PIN_SOURCE_COMMIT" \
    "GE tree: $PIN_SOURCE_TREE" \
    "Wine commit: $PIN_WINE_COMMIT" \
    "FFmpeg commit: $PIN_FFMPEG_COMMIT" \
    "SteamRT image ID: $PIN_STEAMRT_IMAGE_ID" \
    "Wine patch-series SHA-256: $(pinned_ge_state_value "$PREPARED_STATE" patch_series_digest)" \
    "Proton launcher patch SHA-256: $(pinned_ge_launcher_patch_sha256)" \
    "Package release: 0 (local game-test artifact)" \
    "Project GStreamer payload: none"
)
if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  provenance_lines+=(
    "FFmpeg security records (3 upstream MagicYUV CVE fixes + project TLS policy): $PIN_FFMPEG_SECURITY_FIX_COMMITS"
    "FFmpeg security patch-series SHA-256: $(pinned_ge_state_value \
      "$PREPARED_STATE" ffmpeg_security_series_digest)"
    "FFmpeg security audit SHA-256: $(pinned_ge_state_value \
      "$PREPARED_STATE" ffmpeg_security_patch_audit_sha256)"
    "FFmpeg patched source-state SHA-256: $(pinned_ge_state_value \
      "$PREPARED_STATE" ffmpeg_repo_state_sha256)"
    "FFmpeg crypto-build patch SHA-256: $PIN_FFMPEG_CRYPTO_BUILD_PATCH_SHA256"
    "FFmpeg crypto-build audit SHA-256: $(pinned_ge_state_value \
      "$PREPARED_STATE" ffmpeg_crypto_build_audit_sha256)"
  )
fi
if [[ "$PINNED_GE_HAS_PATCH_OVERLAP" == 1 ]]; then
  provenance_lines+=(
    "GE inherited patch-overlap policy: stock-effective-exact-skip"
    "GE patch-overlap manifest SHA-256: $PIN_GE_PATCH_OVERLAP_MANIFEST_SHA256"
    "GE patch-overlap audit SHA-256: $(pinned_ge_state_value \
      "$PREPARED_STATE" ge_patch_overlap_audit_sha256)"
  )
fi
for provenance_line in "${provenance_lines[@]}"; do
  rg -Fqx "$provenance_line" "$provenance" \
    || pinned_ge_fail "artifact provenance omits: $provenance_line"
done

for arch in i386-linux-gnu x86_64-linux-gnu; do
  for library in libavcodec libavformat libavutil; do
    mapfile -t matches < <(
      find "$TOOL_DIR/files/lib/$arch" -maxdepth 1 \
        \( -type f -o -type l \) -name "$library.so*" -print | LC_ALL=C sort
    )
    [[ "${#matches[@]}" -gt 0 ]] \
      || pinned_ge_fail "artifact is missing $library for $arch"
  done
done

gstreamer_payload="$({
  find "$TOOL_DIR" \( -type f -o -type l \) -print
} | rg -i '/([^/]*winegstreamer[^/]*|libgst[^/]*|gstreamer-[^/]*)$' || true)"
[[ -z "$gstreamer_payload" ]] || {
  printf 'Unexpected GStreamer payload:\n%s\n' "$gstreamer_payload" >&2
  pinned_ge_fail "the no-GStreamer artifact contains a GStreamer payload"
}

zmqsend_payload="$(find "$TOOL_DIR" -iname '*zmqsend*' -print | LC_ALL=C sort)"
[[ -z "$zmqsend_payload" ]] || {
  printf 'Unexpected zmqsend payload:\n%s\n' "$zmqsend_payload" >&2
  pinned_ge_fail \
    "artifact contains a zmqsend tool despite disabled FFmpeg programs/ZeroMQ"
}

for arch in i386 x86_64; do
  config="$BUILD_DIR/obj-ffmpeg-$arch/config.h"
  components="$BUILD_DIR/obj-ffmpeg-$arch/config_components.h"
  pinned_ge_require_ffmpeg_config_surface \
    "$PIN_SCHEMA_VERSION" "$config" "$components"
  if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
    pinned_ge_require_exact_ffmpeg_define \
      "$components" CONFIG_MAGICYUV_DECODER 1
  fi
done

if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  "$ROOT_DIR/scripts/audit-ffmpeg-source.sh" --source "$SOURCE_DIR" \
    --require-security-series >/dev/null
fi

if [[ "$PIN_SCHEMA_VERSION" == 5 ]]; then
  source_audit="$("$ROOT_DIR/scripts/audit-patched-wine.sh" \
    --require-alpha-series --require-a311-series --require-a312-series \
    --series "$PATCH_SERIES" \
    --wine-tree "$SOURCE_DIR/wine")"
else
  legacy_source_audit="$STATE_DIR/wine-source-audit.tsv"
  [[ -f "$legacy_source_audit" && ! -L "$legacy_source_audit" \
      && "$(pinned_ge_sha256 "$legacy_source_audit")" \
        == "$(pinned_ge_state_value "$PREPARED_STATE" source_audit_sha256)" ]] \
    || pinned_ge_fail "legacy Wine source audit differs from prepared A3 state"
  source_audit="$(cat "$legacy_source_audit")"
fi
for fact in \
  $'direct_hls_url\tyes' \
  $'rtsp_registry_scheme\tyes' \
  $'rtspt_registry_scheme\tyes' \
  $'rtspt_demuxer_handling\tyes' \
  $'rtsp_timeout_dictionary_option\tyes' \
  $'ffmpeg_interrupt_callback_assignment\tyes' \
  $'atomic_interrupt_cancellation\tyes' \
  $'rtsp_open_deadline\tyes' \
  $'rtsp_io_deadline\tyes' \
  $'hls_io_deadline\tyes' \
  $'ffmpeg_unseekable_flag_propagation\tyes' \
  $'conditional_mf_seek_capability_hint\tyes' \
  $'demux_transition_epoch\tyes' \
  $'stale_inflight_packet_discard\tyes' \
  $'flush_safe_packet_queue_handoff\tyes' \
  $'multithreaded_sample_request_queue\tyes' \
  $'per_stream_fifo_sample_worker\tyes' \
  $'sample_request_generation_guard\tyes' \
  $'counted_source_work_items\tyes' \
  $'one_shot_stream_eos\tyes' \
  $'aggregate_emergency_queue_bound\tyes' \
  $'simple_buffer_overflow_guard\tyes' \
  $'late_bytestream_buffer_ownership\tyes' \
  $'bytestream_endread_in_callback\tyes' \
  $'source_load_generation\tyes' \
  $'playback_intent_tracking\tyes' \
  $'demux_stream_index_mapping\tyes' \
  $'hls_urlmon_bypass\tyes' \
  $'case_insensitive_hls_classification\tyes' \
  $'hardened_custom_seek_origin\tyes' \
  $'overflow_safe_seek_rescale\tyes' \
  $'cancellation_and_seek_ordering\tyes' \
  $'async_http_and_vod_seek_repairs\tyes' \
  $'failed_seek_terminal_state\tyes' \
  $'sample_lifecycle_repairs\tyes' \
  $'source_load_generation_repairs\tyes' \
  $'media_engine_destructor_shutdown_order\tyes' \
  $'wow64_destroy_uses_destroy_params\tyes' \
  $'sar_state_preserving_flush\tyes' \
  $'alpha_series_audit\tpassed' \
  $'sar_flush_control_model\tpassed' \
  $'clock_start_control_model\tpassed' \
  $'clock_start_source_audit\tpassed' \
  $'a36_series_audit\tpassed' \
  $'a37_series_audit\tpassed' \
  $'media_engine_pause_scrub_model\tpassed' \
  $'media_engine_pause_scrub_source_audit\tpassed' \
  $'a38_series_audit\tpassed' \
  $'source_replacement_lifecycle_model\tpassed' \
  $'source_replacement_lifecycle_source_audit\tpassed' \
  $'a39_series_audit\tpassed' \
  $'a310_series_audit\tpassed' \
  $'a311_series_audit\tpassed' \
  $'a312_series_audit\tpassed' \
  $'network_terminal_error_model\tpassed' \
  $'network_terminal_error_source_audit\tpassed' \
  $'structural_audit\tpassed'; do
  grep -Fxq -- "$fact" <<<"$source_audit" \
    || pinned_ge_fail "patched-source audit fact is absent: $fact"
done

if [[ "$PIN_SCHEMA_VERSION" != 5 ]]; then
  for fact in \
    $'pcm_probe_control_model\tpassed' \
    $'pcm_probe_source_audit\tpassed' \
    $'avpro_audio_state_model\tpassed' \
    $'avpro_audio_state_source_audit\tpassed'; do
    grep -Fxq -- "$fact" <<<"$source_audit" \
      || pinned_ge_fail "legacy diagnostic source-audit fact is absent: $fact"
  done
else
  diagnostic_present_count="$(
    grep -Fxc -- $'diagnostic_patch_declared\tyes' <<<"$source_audit" || true
  )"
  diagnostic_absent_count="$(
    grep -Fxc -- $'diagnostic_patch_declared\tno' <<<"$source_audit" || true
  )"
  [[ "$((diagnostic_present_count + diagnostic_absent_count))" == 1 ]] \
    || pinned_ge_fail \
      "patched-source audit does not declare exactly one diagnostic contract"
  if [[ "$diagnostic_present_count" == 1 ]]; then
    for fact in \
      $'pcm_probe_control_model\tpassed' \
      $'pcm_probe_source_audit\tpassed' \
      $'avpro_audio_state_model\tpassed' \
      $'avpro_audio_state_source_audit\tpassed'; do
      grep -Fxq -- "$fact" <<<"$source_audit" \
        || pinned_ge_fail "diagnostic source-audit fact is absent: $fact"
    done
    ! grep -Fq -- $'diagnostic_source_absence\t' <<<"$source_audit" \
      || pinned_ge_fail \
        "diagnostic-carrying source audit reported diagnostic absence"
  else
    grep -Fxq -- $'diagnostic_source_absence\tpassed' <<<"$source_audit" \
      || pinned_ge_fail "diagnostic-free source-audit absence fact is absent"
    for fact in \
      $'pcm_probe_control_model\tpassed' \
      $'pcm_probe_source_audit\tpassed' \
      $'avpro_audio_state_model\tpassed' \
      $'avpro_audio_state_source_audit\tpassed'; do
      ! grep -Fxq -- "$fact" <<<"$source_audit" \
        || pinned_ge_fail \
          "diagnostic-free source audit reported diagnostic fact: $fact"
    done
  fi
fi

printf 'Verified pinned game-test directory: %s\n' "$TOOL_DIR"
printf 'Verified redist archive:           %s\n' "$ARCHIVE"
printf 'Global job budget:                 %s\n' "$global_jobs"
printf 'Orchestrator Make jobs:            %s\n' "$make_jobs"
printf 'Per-component Ninja jobs:          %s\n' "$ninja_jobs"
printf 'Nested Make x Ninja ceiling:       %s\n' "$expected_product"
printf 'Recursive Make scheduling:         GNU Make jobserver\n'
printf 'GE diff-check baseline:             byte-identical\n'
printf 'WineGStreamer external references:  0\n'
printf 'Artifact home-path privacy scan:    passed\n'
printf 'Artifact links/types/permissions:   passed\n'
printf 'PACKAGE_RELEASE:                   0\n'
printf 'Steam/runtime test:                not run\n'
printf 'Publication:                       not performed\n'
