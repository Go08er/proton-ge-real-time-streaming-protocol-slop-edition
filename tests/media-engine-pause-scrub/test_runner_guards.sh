#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
RUNNER="$SCRIPT_DIR/run_runtime_probe.sh"
PROJECT_ROOT=$(readlink -m "$SCRIPT_DIR/../..")
SENTINEL="$PROJECT_ROOT/.runtime-probe-guard-selftest"

fail()
{
    printf 'runner guard self-test: %s\n' "$*" >&2
    exit 1
}

expect_rejection()
{
    local label=$1 expected=$2
    shift 2
    local output

    if output=$(env EXPECT=candidate-pass PROTON_TOOL=/nonexistent FIXTURE=/nonexistent "$@" "$RUNNER" 2>&1); then
        fail "$label unexpectedly succeeded"
    fi
    case "$output" in
        *"$expected"*) ;;
        *) fail "$label produced the wrong rejection: $output" ;;
    esac
}

[[ ! -e "$SENTINEL" ]] || fail "reserved sentinel already exists: $SENTINEL"

expect_rejection compatdata \
    'LAB_ROOT must not be inside any Steam compatdata directory' \
    LAB_ROOT="$SENTINEL/steamapps/compatdata/438100"
expect_rejection vrchat-appid \
    'APP_ID 438100 is reserved for VRChat' \
    LAB_ROOT="$SENTINEL-appid" APP_ID=438100
expect_rejection leading-zero-appid \
    'APP_ID must be a positive decimal integer without leading zeroes' \
    LAB_ROOT="$SENTINEL-leading-zero" APP_ID=000438100
expect_rejection existing-root \
    'refusing to reuse existing LAB_ROOT' \
    LAB_ROOT="$PROJECT_ROOT"
expect_rejection steam-tree \
    'LAB_ROOT must be outside the Steam installation tree' \
    STEAM_ROOT="$SENTINEL-steam" LAB_ROOT="$SENTINEL-steam/lab"
expect_rejection private-tmp \
    'Steam Linux Runtime uses a private /tmp' \
    LAB_ROOT=/tmp/runtime-probe-guard-selftest

[[ ! -e "$SENTINEL" ]] || fail 'a rejected check created the sentinel path'
[[ ! -e "$SENTINEL-appid" ]] || fail 'the AppID check created a lab path'
[[ ! -e "$SENTINEL-leading-zero" ]] || fail 'the leading-zero check created a lab path'
[[ ! -e "$SENTINEL-steam" ]] || fail 'the Steam-tree check created a path'

printf 'runtime-probe path/AppID guard self-tests passed\n'
