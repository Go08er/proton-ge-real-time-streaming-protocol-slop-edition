#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 077

die()
{
    printf 'rtsp-media-lab Proton probe: %s\n' "$*" >&2
    exit 2
}

[[ $(id -u) -ne 0 ]] || die 'refusing to run a Proton prefix as root'
[[ -r /etc/rtsp-media-lab/runtime.env ]] || die 'the declared runtime bundle is absent'
[[ -r /etc/rtsp-media-lab/manifest.json ]] || die 'the isolated VM manifest is absent'

grep -Fq '"guestNetwork":"loopback-only"' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest does not declare the loopback-only VM'
grep -Fq '"hostDirectoryShares":false' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest permits host directory shares'
if findmnt -rn -t 9p,virtiofs | grep -q .; then
    die 'a host filesystem share is mounted in the runtime guest'
fi
mapfile -t network_devices < <(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1)
[[ ${#network_devices[@]} -eq 1 && ${network_devices[0]} == lo ]] ||
    die "runtime probe requires the loopback-only VM, found: ${network_devices[*]}"
[[ -z $(ip route show) ]] || die 'runtime probe VM has an IPv4 route'

# shellcheck disable=SC1091
source /etc/rtsp-media-lab/runtime.env

: "${RTSP_LAB_PROTON_TOOL:?missing RTSP_LAB_PROTON_TOOL}"
: "${RTSP_LAB_STEAM_RUNTIME:?missing RTSP_LAB_STEAM_RUNTIME}"
: "${RTSP_LAB_PROBE_EXE:?missing RTSP_LAB_PROBE_EXE}"
: "${RTSP_LAB_PROBE_FIXTURE:?missing RTSP_LAB_PROBE_FIXTURE}"
: "${RTSP_LAB_PROBE_PARSER:?missing RTSP_LAB_PROBE_PARSER}"
: "${RTSP_LAB_RESULT_CONTRACT:?missing RTSP_LAB_RESULT_CONTRACT}"

for input in \
    "$RTSP_LAB_PROTON_TOOL" \
    "$RTSP_LAB_STEAM_RUNTIME" \
    "$RTSP_LAB_PROBE_EXE" \
    "$RTSP_LAB_PROBE_FIXTURE" \
    "$RTSP_LAB_PROBE_PARSER"; do
    resolved=$(readlink -f -- "$input")
    [[ "$resolved" == /nix/store/* ]] || die "runtime input escaped the Nix store: $input"
done

[[ -f "$RTSP_LAB_PROTON_TOOL/proton" ]] || die 'the Proton launcher is missing'
[[ -f "$RTSP_LAB_PROBE_EXE" ]] || die 'the compiled probe is missing'
[[ -f "$RTSP_LAB_PROBE_FIXTURE" ]] || die 'the probe fixture is missing'
[[ -f "$RTSP_LAB_PROBE_PARSER" ]] || die 'the result parser is missing'

if [[ -x "$RTSP_LAB_STEAM_RUNTIME/_v2-entry-point" ]]; then
    runtime_entry="$RTSP_LAB_STEAM_RUNTIME/_v2-entry-point"
elif [[ -x "$RTSP_LAB_STEAM_RUNTIME/SteamLinuxRuntime_4/_v2-entry-point" ]]; then
    runtime_entry="$RTSP_LAB_STEAM_RUNTIME/SteamLinuxRuntime_4/_v2-entry-point"
else
    die 'the declared Steam Linux Runtime entry point is missing'
fi

expect=${RTSP_LAB_EXPECT:-candidate-pass}
case "$expect" in
    a3.7-failure|candidate-pass) ;;
    *) die 'RTSP_LAB_EXPECT must be a3.7-failure or candidate-pass' ;;
esac

app_id=${RTSP_LAB_APP_ID:-999108}
[[ "$app_id" =~ ^[1-9][0-9]*$ ]] || die 'RTSP_LAB_APP_ID must be a positive decimal'
[[ "$app_id" != 438100 ]] || die 'AppID 438100 is reserved for VRChat and forbidden in the lab'

state_root=/var/lib/rtsp-media-lab
[[ -d "$state_root/runs" && -w "$state_root/runs" ]] || die 'ephemeral run root is unavailable'
run_root=$(mktemp -d "$state_root/runs/probe.XXXXXXXX")
mkdir -m 700 \
    "$run_root/home" \
    "$run_root/cache" \
    "$run_root/tmp" \
    "$run_root/xdg-runtime" \
    "$run_root/results" \
    "$run_root/compatdata" \
    "$run_root/fake-steam" \
    "$run_root/steam-runtime-var" \
    "$run_root/results/steam-runtime"

fixture_windows="Z:$(printf '%s' "$RTSP_LAB_PROBE_FIXTURE" | sed 's,/,\\,g')"
log_file="$run_root/results/steam-$app_id.log"
python_bin=$(readlink -f -- "$(command -v python3)")
steam_run_bin=$(readlink -f -- "$(command -v steam-run)")

set +e
env -i \
    HOME="$run_root/home" \
    USER=media-lab \
    LOGNAME=media-lab \
    PATH=/run/current-system/sw/bin \
    LANG=C.UTF-8 \
    TMPDIR="$run_root/tmp" \
    XDG_RUNTIME_DIR="$run_root/xdg-runtime" \
    PYTHONDONTWRITEBYTECODE=1 \
    PROTON_LOG=1 \
    PROTON_LOG_DIR="$run_root/results" \
    PRESSURE_VESSEL_VARIABLE_DIR="$run_root/steam-runtime-var" \
    STEAM_LINUX_RUNTIME_LOG_DIR="$run_root/results/steam-runtime" \
    SteamAppId="$app_id" \
    SteamGameId="$app_id" \
    XDG_CACHE_HOME="$run_root/cache" \
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$run_root/fake-steam" \
    STEAM_COMPAT_DATA_PATH="$run_root/compatdata" \
    WINEDEBUG=-all,+timestamp,+pid,+tid,+mfplat,+quartz,+avprostate \
    "$steam_run_bin" "$runtime_entry" --verb=waitforexitandrun -- \
    "$python_bin" "$RTSP_LAB_PROTON_TOOL/proton" run \
    "$RTSP_LAB_PROBE_EXE" "$fixture_windows" \
    >"$run_root/results/probe-console.log" 2>&1
probe_exit=$?
set -e

prefix_result="$run_root/compatdata/pfx/drive_c/probe-result.txt"
[[ -f "$prefix_result" ]] || die 'the compiled probe produced no result file'
[[ -f "$log_file" ]] || die 'Proton produced no trace log'

if ! python3 "$RTSP_LAB_PROBE_PARSER" \
    --expect "$expect" \
    --result "$prefix_result" \
    --log "$log_file" \
    --process-exit "$probe_exit" \
    >"$run_root/results/parser-console.log" 2>&1; then
    die 'runtime parser rejected the ephemeral probe evidence'
fi

python3 "$RTSP_LAB_RESULT_CONTRACT" write-runtime \
    --output "$state_root/results/runtime-probe-summary.json" \
    --expected "$expect" \
    --process-exit "$probe_exit" ||
    die 'could not write the strict runtime result'
python3 "$RTSP_LAB_RESULT_CONTRACT" validate \
    --kind runtime \
    --path "$state_root/results/runtime-probe-summary.json" ||
    die 'strict runtime result validation failed'

printf 'passed\n' >"$state_root/results/runtime-probe.ok"
printf 'runtime probe passed; raw evidence remains ephemeral inside the guest.\n'
