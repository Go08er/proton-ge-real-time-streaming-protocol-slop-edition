#!/usr/bin/env bash
set -euo pipefail
umask 077

usage()
{
    cat <<'EOF'
Usage:
  EXPECT=a3.7-failure|candidate-pass \
  PROTON_TOOL=/path/to/proton-tool \
  FIXTURE=/path/to/i420-64x64.avi \
  LAB_ROOT=/new/private/lab-directory \
  tests/media-engine-pause-scrub/run_runtime_probe.sh

Optional environment variables:
  APP_ID         Synthetic Steam application ID (default: 999107)
  STEAM_ROOT      Steam installation (default: ~/.local/share/Steam)
  STEAM_RUNTIME   Entry point (default: SteamLinuxRuntime_4/_v2-entry-point)
  STEAM_RUN       Host Steam wrapper (default: /run/current-system/sw/bin/steam-run)
  NIX_PYTHON      Host Python executable used to launch Proton
  MINGW_CC        MinGW compiler (default: x86_64-w64-mingw32-gcc)

LAB_ROOT must not exist, and its parent directory must already exist. Paths inside a Steam tree or any
steamapps/compatdata directory are rejected. APP_ID must be a positive decimal
without leading zeroes and cannot
be VRChat's 438100. These checks prevent accidental reuse or modification of a
game prefix and make every result an isolated, private run.
EOF
}

die()
{
    printf 'runtime probe: %s\n' "$*" >&2
    exit 2
}

case "${EXPECT:-}" in
    a3.7-failure|candidate-pass) ;;
    *) usage; die 'EXPECT must be a3.7-failure or candidate-pass' ;;
esac

: "${PROTON_TOOL:?set PROTON_TOOL to the unpacked Proton tool directory}"
: "${FIXTURE:?set FIXTURE to the Wine i420-64x64.avi fixture}"
: "${LAB_ROOT:?set LAB_ROOT to a new private test directory}"

unset CDPATH
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
LAB_ROOT=$(readlink -m "$LAB_ROOT")
STEAM_ROOT=${STEAM_ROOT:-"$HOME/.local/share/Steam"}
STEAM_ROOT=$(readlink -m "$STEAM_ROOT")
APP_ID=${APP_ID:-999107}

case "$LAB_ROOT" in
    /tmp|/tmp/*)
        die 'Steam Linux Runtime uses a private /tmp; choose a path under your home directory'
        ;;
    "$STEAM_ROOT"|"$STEAM_ROOT"/*)
        die 'LAB_ROOT must be outside the Steam installation tree'
        ;;
    */steamapps/compatdata|*/steamapps/compatdata/*)
        die 'LAB_ROOT must not be inside any Steam compatdata directory'
        ;;
esac
[[ ! -e "$LAB_ROOT" ]] ||
    die "refusing to reuse existing LAB_ROOT $LAB_ROOT; choose a new path"
[[ -d "$(dirname -- "$LAB_ROOT")" ]] ||
    die "LAB_ROOT parent does not exist: $(dirname -- "$LAB_ROOT")"
[[ "$APP_ID" =~ ^[1-9][0-9]*$ ]] ||
    die 'APP_ID must be a positive decimal integer without leading zeroes'
[[ "$APP_ID" != 438100 ]] || die "APP_ID 438100 is reserved for VRChat; use a synthetic ID"

STEAM_RUNTIME=${STEAM_RUNTIME:-"$STEAM_ROOT/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point"}
STEAM_RUN=${STEAM_RUN:-/run/current-system/sw/bin/steam-run}
NIX_PYTHON=${NIX_PYTHON:-$(readlink -f "$(command -v python3)")}
MINGW_CC=${MINGW_CC:-x86_64-w64-mingw32-gcc}
EXPECTED_SOURCE_SHA256=47e1b80ff3e8c58d9a97ba4134a664fcd4984ad9b6192a4bdae03184d3517eb8
EXPECTED_FIXTURE_SHA256=2e111c938d1847cba8a4527dc72412cd1aecc330ceeab9779f5ae70a8322ade7

[[ -f "$SCRIPT_DIR/runtime_probe.c" ]] || die 'checked-in runtime_probe.c is missing'
[[ -f "$PROTON_TOOL/proton" ]] || die "missing $PROTON_TOOL/proton"
[[ -f "$FIXTURE" ]] || die "missing fixture $FIXTURE"
[[ -x "$STEAM_RUNTIME" ]] || die "missing Steam Runtime entry point $STEAM_RUNTIME"
[[ -x "$STEAM_RUN" ]] || die "missing Steam wrapper $STEAM_RUN"
[[ -x "$NIX_PYTHON" ]] || die "missing Python executable $NIX_PYTHON"
command -v "$MINGW_CC" >/dev/null ||
    die "missing MinGW compiler $MINGW_CC"
command -v sha256sum >/dev/null || die 'missing sha256sum'

SOURCE_HASH=$(sha256sum -- "$SCRIPT_DIR/runtime_probe.c")
SOURCE_HASH=${SOURCE_HASH%% *}
[[ "$SOURCE_HASH" == "$EXPECTED_SOURCE_SHA256" ]] ||
    die "probe-source SHA-256 mismatch: expected $EXPECTED_SOURCE_SHA256, got $SOURCE_HASH"

FIXTURE=$(readlink -f "$FIXTURE")
FIXTURE_HASH=$(sha256sum -- "$FIXTURE")
FIXTURE_HASH=${FIXTURE_HASH%% *}
[[ "$FIXTURE_HASH" == "$EXPECTED_FIXTURE_SHA256" ]] ||
    die "fixture SHA-256 mismatch: expected $EXPECTED_FIXTURE_SHA256, got $FIXTURE_HASH"

case "$STEAM_RUNTIME" in
    */_v2-entry-point)
        RUNTIME_ARGUMENTS=(--verb=waitforexitandrun --)
        ;;
    */run-in-sniper)
        RUNTIME_ARGUMENTS=(--)
        ;;
    *)
        die 'STEAM_RUNTIME must be a Runtime 4 _v2-entry-point or tested run-in-sniper entry point'
        ;;
esac
mkdir -m 700 -- "$LAB_ROOT" ||
    die "could not atomically create new LAB_ROOT $LAB_ROOT"
mkdir -m 700 "$LAB_ROOT/results" "$LAB_ROOT/cache" "$LAB_ROOT/compatdata"
PROBE_EXE="$LAB_ROOT/runtime_probe.exe"
RESULT_FILE="$LAB_ROOT/results/probe-result-$APP_ID.txt"
LOG_FILE="$LAB_ROOT/results/steam-$APP_ID.log"

"$MINGW_CC" -std=gnu11 -Wall -Wextra -Werror -municode \
    -o "$PROBE_EXE" "$SCRIPT_DIR/runtime_probe.c" \
    -lole32 -loleaut32 -lmf -lmfplat -lmfuuid -luuid

FIXTURE_WIN="Z:$(printf '%s' "$FIXTURE" | sed 's,/,\\,g')"

set +e
"$STEAM_RUN" "$STEAM_RUNTIME" "${RUNTIME_ARGUMENTS[@]}" env \
    PROTON_LOG=1 \
    PROTON_LOG_DIR="$LAB_ROOT/results" \
    SteamGameId="$APP_ID" \
    XDG_CACHE_HOME="$LAB_ROOT/cache" \
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM_ROOT" \
    STEAM_COMPAT_DATA_PATH="$LAB_ROOT/compatdata" \
    WINEDEBUG=-all,+timestamp,+pid,+tid,+mfplat,+quartz,+avprostate \
    "$NIX_PYTHON" "$PROTON_TOOL/proton" run \
    "$PROBE_EXE" "$FIXTURE_WIN"
PROBE_EXIT=$?
set -e

PREFIX_RESULT="$LAB_ROOT/compatdata/pfx/drive_c/probe-result.txt"
[[ -f "$PREFIX_RESULT" ]] || die "probe produced no $PREFIX_RESULT"
[[ -f "$LOG_FILE" ]] || die "Proton produced no $LOG_FILE"
cp "$PREFIX_RESULT" "$RESULT_FILE"

PYTHONDONTWRITEBYTECODE=1 python3 "$SCRIPT_DIR/parse_runtime_probe.py" \
    --expect "$EXPECT" \
    --result "$RESULT_FILE" \
    --log "$LOG_FILE" \
    --process-exit "$PROBE_EXIT"

printf 'probe executable: %s\n' "$PROBE_EXE"
printf 'probe result:     %s\n' "$RESULT_FILE"
printf 'Proton trace:     %s\n' "$LOG_FILE"
