#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 077

die()
{
    printf 'rtsp-media-lab stress driver: %s\n' "$*" >&2
    exit 2
}

[[ $(id -u) -ne 0 ]] || die 'refusing to run a Proton prefix as root'
[[ -r /etc/rtsp-media-lab/runtime.env ]] || die 'the declared runtime bundle is absent'
[[ -r /etc/rtsp-media-lab/manifest.json ]] || die 'the isolated VM manifest is absent'
grep -Fq '"guestNetwork":"loopback-only"' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest does not declare the loopback-only VM'
grep -Fq '"hostDirectoryShares":false' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest permits host directory shares'
grep -Fq '"legacyVaLayout":false' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest does not declare the Wine-compatible legacy VA policy'
grep -Fq '"mmapRandomizationBits":31' /etc/rtsp-media-lab/manifest.json ||
    die 'the manifest does not declare the Wine-compatible mmap entropy policy'
if findmnt -rn -t 9p,virtiofs | grep -q .; then
    die 'a host filesystem share is mounted in the runtime guest'
fi
mapfile -t network_devices < <(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1)
[[ ${#network_devices[@]} -eq 1 && ${network_devices[0]} == lo ]] ||
    die "stress driver requires the loopback-only VM, found: ${network_devices[*]}"
[[ -z $(ip route show) ]] || die 'stress driver VM has an IPv4 route'

# shellcheck disable=SC1091
source /etc/rtsp-media-lab/runtime.env

required_variables=(
    RTSP_LAB_STRESS_PROTON_TOOL
    RTSP_LAB_STRESS_STEAM_RUNTIME
    RTSP_LAB_STRESS_DRIVER_EXE
    RTSP_LAB_STRESS_SCENARIO
    RTSP_LAB_STRESS_FIXTURE_MANIFEST
    RTSP_LAB_STRESS_FIXTURE_BYTES
    RTSP_LAB_STRESS_SERVICE_CONFIG
    RTSP_LAB_STRESS_CONTROL_ORACLE
    RTSP_LAB_STRESS_INSTRUMENTED_ORACLE
    RTSP_LAB_STRESS_PARSER
    RTSP_LAB_STRESS_BUILD_ROLE
    RTSP_LAB_STRESS_CASE_ROLE
    RTSP_LAB_STRESS_MEDIA_KIND
    RTSP_LAB_STORE_DIGEST
    RTSP_LAB_STRESS_FIXTURE_SERVICE
    RTSP_LAB_TRANSPORT_ORACLE
    RTSP_LAB_FAILURE_DIAGNOSTIC
    RTSP_LAB_HTTP_FIXTURE
    RTSP_LAB_HARNESS_ROOT
    RTSP_LAB_RESULT_CONTRACT
)
for variable in "${required_variables[@]}"; do
    [[ -n ${!variable:-} ]] || die "missing $variable"
done

store_inputs=(
    "$RTSP_LAB_STRESS_PROTON_TOOL"
    "$RTSP_LAB_STRESS_STEAM_RUNTIME"
    "$RTSP_LAB_STRESS_DRIVER_EXE"
    "$RTSP_LAB_STRESS_SCENARIO"
    "$RTSP_LAB_STRESS_FIXTURE_MANIFEST"
    "$RTSP_LAB_STRESS_FIXTURE_BYTES"
    "$RTSP_LAB_STRESS_SERVICE_CONFIG"
    "$RTSP_LAB_STRESS_CONTROL_ORACLE"
    "$RTSP_LAB_STRESS_INSTRUMENTED_ORACLE"
    "$RTSP_LAB_STRESS_PARSER"
)
for input in "${store_inputs[@]}"; do
    [[ "$input" == /nix/store/* ]] || die 'a stress input is not an immutable Nix store path'
    [[ -e "$input" ]] || die 'a declared stress input does not exist'
done
[[ -f "$RTSP_LAB_STRESS_PROTON_TOOL/proton" ]] || die 'the Proton launcher is missing'
[[ -d "$RTSP_LAB_STRESS_STEAM_RUNTIME" ]] || die 'the Steam runtime is not a directory'
for file in \
    "$RTSP_LAB_STRESS_DRIVER_EXE" \
    "$RTSP_LAB_STRESS_SCENARIO" \
    "$RTSP_LAB_STRESS_FIXTURE_MANIFEST" \
    "$RTSP_LAB_STRESS_SERVICE_CONFIG" \
    "$RTSP_LAB_STRESS_CONTROL_ORACLE" \
    "$RTSP_LAB_STRESS_INSTRUMENTED_ORACLE" \
    "$RTSP_LAB_STRESS_PARSER"; do
    [[ -f "$file" ]] || die 'a declared stress file input is not a regular file'
done
[[ -d "$RTSP_LAB_STRESS_FIXTURE_BYTES" ]] || die 'fixture bytes input is not a directory'

case "$RTSP_LAB_STRESS_BUILD_ROLE" in
    stock-ge-control|rtsp-reference-control|frozen-regression-control|streaming-base-candidate|full-parity-candidate) ;;
    *) die 'invalid stress build role' ;;
esac
case "$RTSP_LAB_STRESS_CASE_ROLE" in
    expected-pass|negative-control|qualification) ;;
    *) die 'invalid stress case role' ;;
esac
case "$RTSP_LAB_STRESS_MEDIA_KIND" in
    av|audio-only|video-only) ;;
    *) die 'invalid stress media kind' ;;
esac

if [[ -x "$RTSP_LAB_STRESS_STEAM_RUNTIME/_v2-entry-point" ]]; then
    runtime_entry_relative=_v2-entry-point
elif [[ -x "$RTSP_LAB_STRESS_STEAM_RUNTIME/SteamLinuxRuntime_4/_v2-entry-point" ]]; then
    runtime_entry_relative=SteamLinuxRuntime_4/_v2-entry-point
else
    die 'the declared Steam Linux Runtime entry point is missing'
fi

digest_file()
{
    python3 "$RTSP_LAB_STORE_DIGEST" --expect file --path "$1"
}

digest_tree()
{
    python3 "$RTSP_LAB_STORE_DIGEST" --expect directory --path "$1"
}

# These are computed from the actual guest-visible bytes. No caller-provided
# hash label is accepted as evidence.
proton_tool_sha256=$(digest_tree "$RTSP_LAB_STRESS_PROTON_TOOL")
steam_runtime_sha256=$(digest_tree "$RTSP_LAB_STRESS_STEAM_RUNTIME")
driver_exe_sha256=$(digest_file "$RTSP_LAB_STRESS_DRIVER_EXE")
scenario_sha256=$(digest_file "$RTSP_LAB_STRESS_SCENARIO")
fixture_manifest_sha256=$(digest_file "$RTSP_LAB_STRESS_FIXTURE_MANIFEST")
fixture_bytes_sha256=$(digest_tree "$RTSP_LAB_STRESS_FIXTURE_BYTES")
service_config_sha256=$(digest_file "$RTSP_LAB_STRESS_SERVICE_CONFIG")
control_oracle_sha256=$(digest_file "$RTSP_LAB_STRESS_CONTROL_ORACLE")
instrumented_oracle_sha256=$(digest_file "$RTSP_LAB_STRESS_INSTRUMENTED_ORACLE")
parser_sha256=$(digest_file "$RTSP_LAB_STRESS_PARSER")
lab_harness_sha256=$(digest_tree "$RTSP_LAB_HARNESS_ROOT")

python3 "$RTSP_LAB_RESULT_CONTRACT" check-stress-oracles \
    --control "$RTSP_LAB_STRESS_CONTROL_ORACLE" \
    --instrumented "$RTSP_LAB_STRESS_INSTRUMENTED_ORACLE" \
    --driver-sha256 "$driver_exe_sha256" \
    --scenario-sha256 "$scenario_sha256" \
    --media-kind "$RTSP_LAB_STRESS_MEDIA_KIND" ||
    die 'stress oracles do not bind the actual driver and scenario bytes'

service_port=$(python3 "$RTSP_LAB_STRESS_FIXTURE_SERVICE" check \
    --config "$RTSP_LAB_STRESS_SERVICE_CONFIG" \
    --fixture-root "$RTSP_LAB_STRESS_FIXTURE_BYTES" \
    --fixture-manifest "$RTSP_LAB_STRESS_FIXTURE_MANIFEST" \
    --scenario "$RTSP_LAB_STRESS_SCENARIO") ||
    die 'fixture, service, or scenario contract validation failed'
[[ "$service_port" =~ ^[1-9][0-9]{0,4}$ ]] || die 'fixture service returned an invalid port'

state_root=/var/lib/rtsp-media-lab
[[ -d "$state_root/runs" && -w "$state_root/runs" ]] || die 'ephemeral run root is unavailable'
python_bin=$(readlink -f -- "$(command -v python3)")
steam_run_bin=$(readlink -f -- "$(command -v steam-run)")
# Keep the `timeout` basename: Nix coreutils is a multicall binary and resolves
# its applet from argv[0]. Dereferencing this symlink invokes plain `coreutils`.
timeout_bin=$(command -v timeout)
pulse_bin=$(readlink -f -- "$(command -v pulseaudio)")
pactl_bin=$(readlink -f -- "$(command -v pactl)")
xvfb_bin=$(readlink -f -- "$(command -v Xvfb)")
xdpyinfo_bin=$(readlink -f -- "$(command -v xdpyinfo)")
glxinfo_bin=$(readlink -f -- "$(command -v glxinfo)")

windows_path()
{
    printf 'Z:%s' "$1" | sed 's,/,\\,g'
}

child_pids=()
forget_child_pid()
{
    local target=$1 pid
    local -a retained=()
    for pid in "${child_pids[@]:-}"; do
        [[ "$pid" == "$target" ]] || retained+=("$pid")
    done
    child_pids=("${retained[@]}")
}

child_has_exited()
{
    local pid=$1 state
    [[ -r "/proc/$pid/status" ]] || return 0
    state=$(awk '$1 == "State:" { print $2; exit }' "/proc/$pid/status" 2>/dev/null) || return 0
    [[ "$state" == Z || "$state" == X ]]
}

wait_child_bounded()
{
    local pid=$1 iterations=$2 delay=$3
    for _ in $(seq 1 "$iterations"); do
        child_has_exited "$pid" && return 0
        sleep "$delay"
    done
    return 1
}

cleanup_children()
{
    local pid
    for pid in "${child_pids[@]:-}"; do
        if ! child_has_exited "$pid"; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "${child_pids[@]:-}"; do
        if ! wait_child_bounded "$pid" 500 0.01; then
            kill -KILL "$pid" 2>/dev/null || true
            wait_child_bounded "$pid" 100 0.01 || true
        fi
        wait "$pid" 2>/dev/null || true
    done
    child_pids=()
}
trap cleanup_children EXIT HUP INT TERM

graceful_stop_fixture_service()
{
    local pid=$1 completion_marker=$2 service_exit
    if ! child_has_exited "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
    fi
    if ! wait_child_bounded "$pid" 1500 0.01; then
        kill -KILL "$pid" 2>/dev/null || true
        wait_child_bounded "$pid" 100 0.01 || true
        wait "$pid" 2>/dev/null || true
        forget_child_pid "$pid"
        die 'fixture service did not stop and seal its request log within 15 seconds'
    fi
    set +e
    wait "$pid"
    service_exit=$?
    set -e
    forget_child_pid "$pid"
    [[ $service_exit -eq 0 ]] || die 'fixture service rejected graceful log finalization'
    [[ -s "$completion_marker" ]] || die 'fixture service produced no completion marker'
}

wait_for_path()
{
    local path=$1 pid=$2
    for _ in $(seq 1 100); do
        [[ -S "$path" ]] && return 0
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.05
    done
    return 1
}

wait_for_port()
{
    local port=$1 pid=$2
    # The service repeats the fixture READY/SHA256SUMS verification inside its
    # own process before binding. Full-profile fixture trees can take longer
    # than a few seconds to verify on a small VM, so keep this bounded at two
    # minutes while also failing immediately if the validator exits.
    for _ in $(seq 1 2400); do
        if python3 - "$port" <<'PY'
import socket
import sys
s = socket.socket()
s.settimeout(0.1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
        then
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.05
    done
    return 1
}

run_root_survivors()
{
    python3 - "$@" <<'PY'
import os
from pathlib import Path
import sys

needles = tuple(os.fsencode(value) for value in sys.argv[1:])
own = {os.getpid(), os.getppid()}
found = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    if pid in own:
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        process_bytes = (entry / "environ").read_bytes() + (entry / "cmdline").read_bytes()
        matched = any(needle in process_bytes for needle in needles)
        if not matched:
            for descriptor in (entry / "fd").iterdir():
                try:
                    target = os.fsencode(os.readlink(descriptor))
                except OSError:
                    continue
                if any(target.startswith(needle) for needle in needles):
                    matched = True
                    break
        if matched:
            found.append(pid)
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
print(" ".join(str(pid) for pid in sorted(found)))
PY
}

assert_run_teardown()
{
    local run_root=$1 survivors
    local -a survivor_pids=()
    for _ in $(seq 1 100); do
        survivors=$(run_root_survivors "$run_root")
        [[ -z "$survivors" ]] && return 0
        sleep 0.05
    done
    # Cleanup stays narrowly scoped to processes whose environment, command
    # line, or open files bind them to this synthetic guest run root.
    read -r -a survivor_pids <<<"$survivors"
    kill "${survivor_pids[@]}" 2>/dev/null || true
    sleep 0.25
    survivors=$(run_root_survivors "$run_root")
    if [[ -n "$survivors" ]]; then
        read -r -a survivor_pids <<<"$survivors"
        kill -KILL "${survivor_pids[@]}" 2>/dev/null || true
    fi
    die 'a stress run left guest processes or open files tied to its fresh prefix'
}

run_one()
{
    local mode=$1 oracle=$2 app_id=$3 monitor_argument=()
    local run_root runtime_variable_dir runtime_copy_dir runtime_entry copied_runtime_sha256
    local pulse_socket pulse_config driver_exe_windows driver_result_windows scenario_windows stress_install_dir
    local renderer_line error_line
    local service_pid pulse_pid xvfb_pid watcher_pid watcher_exit process_exit parser_exit parser_summary result_summary oracle_outcome transport_summary producer_done watch_summary_present failure_diagnostic watcher_stuck service_completion

    run_root=$(mktemp -d "$state_root/runs/stress-$mode.XXXXXXXX")
    mkdir -m 700 \
        "$run_root/home" \
        "$run_root/cache" \
        "$run_root/tmp" \
        "$run_root/xdg-runtime" \
        "$run_root/results" \
        "$run_root/compatdata" \
        "$run_root/fake-steam" \
        "$run_root/pulse" \
        "$run_root/results/steam-runtime" \
        "$run_root/steam-runtime-var" \
        "$run_root/steam-runtime-copy"

    # Pressure Vessel applies first-run permissions and writes mutable runtime
    # state, so each member of the paired experiment receives its own verified
    # byte-identical copy and variable directory. Nothing can warm or poison
    # the second run through a shared runtime cache.
    runtime_variable_dir="$run_root/steam-runtime-var"
    runtime_copy_dir="$run_root/steam-runtime-copy"
    cp -a --no-preserve=ownership "$RTSP_LAB_STRESS_STEAM_RUNTIME/." "$runtime_copy_dir/"
    chmod -R u+w "$runtime_copy_dir"
    copied_runtime_sha256=$(python3 "$RTSP_LAB_STORE_DIGEST" \
        --expect directory \
        --path "$RTSP_LAB_STRESS_STEAM_RUNTIME" \
        --compare-copy "$runtime_copy_dir") || die 'Steam Runtime writable copy verification failed'
    [[ "$copied_runtime_sha256" == "$steam_runtime_sha256" ]] ||
        die 'Steam Runtime writable copy identity differs'
    runtime_entry="$runtime_copy_dir/$runtime_entry_relative"
    [[ -x "$runtime_entry" ]] || die 'Steam Runtime writable entry point is not executable'

    pulse_socket="$run_root/pulse/native"
    pulse_config="$run_root/pulse/default.pa"
    printf '%s\n' \
        '.fail' \
        "load-module module-native-protocol-unix socket=$pulse_socket auth-anonymous=1" \
        'load-module module-null-sink sink_name=rtsp_lab_null format=s16le rate=48000 channels=2 channel_map=front-left,front-right' \
        'set-default-sink rtsp_lab_null' \
        'set-default-source rtsp_lab_null.monitor' \
        >"$pulse_config"
    HOME="$run_root/home" XDG_RUNTIME_DIR="$run_root/xdg-runtime" \
        "$pulse_bin" --daemonize=no --exit-idle-time=-1 --disable-shm=true \
        --log-target="file:$run_root/results/pulseaudio.log" --file="$pulse_config" &
    pulse_pid=$!
    child_pids+=("$pulse_pid")
    wait_for_path "$pulse_socket" "$pulse_pid" || die 'deterministic PulseAudio endpoint did not start'
    PULSE_SERVER="unix:$pulse_socket" "$pactl_bin" info >"$run_root/results/pulse-info.txt"
    PULSE_SERVER="unix:$pulse_socket" "$pactl_bin" list sinks >"$run_root/results/pulse-sinks.txt"
    grep -Fq 'Name: rtsp_lab_null' "$run_root/results/pulse-sinks.txt" || die 'null sink setup check failed'
    grep -Fq 'Sample Specification: s16le 2ch 48000Hz' "$run_root/results/pulse-sinks.txt" ||
        die 'null sink sample specification differs'

    DISPLAY=:77 "$xvfb_bin" :77 -nolisten tcp -noreset -screen 0 1280x720x24 \
        >"$run_root/results/xvfb.log" 2>&1 &
    xvfb_pid=$!
    child_pids+=("$xvfb_pid")
    for _ in $(seq 1 100); do
        if DISPLAY=:77 "$xdpyinfo_bin" >/dev/null 2>&1; then break; fi
        kill -0 "$xvfb_pid" 2>/dev/null || die 'Xvfb exited before setup control'
        sleep 0.05
    done
    DISPLAY=:77 "$xdpyinfo_bin" >"$run_root/results/xdpyinfo.txt" || die 'headless X display setup failed'
    DISPLAY=:77 LC_ALL=C LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe \
        "$glxinfo_bin" -B >"$run_root/results/glxinfo.txt" 2>&1 || die 'software GL setup check failed'
    if ! grep -Eq '^OpenGL renderer string:[[:space:]]*llvmpipe([[:space:](]|$)' \
        "$run_root/results/glxinfo.txt" || \
       ! grep -Eq '^[[:space:]]*Accelerated:[[:space:]]*no[[:space:]]*$' \
        "$run_root/results/glxinfo.txt"; then
        renderer_line=$(grep -Ei -m 1 '(^|[[:space:]])(OpenGL .*renderer string|Device):' \
            "$run_root/results/glxinfo.txt" || true)
        error_line=$(grep -Ei -m 1 '^(Error:|libGL error:|MESA-LOADER:)' \
            "$run_root/results/glxinfo.txt" || true)
        printf 'rtsp-media-lab stress driver: observed GL renderer: %s\n' \
            "${renderer_line:-missing}" >&2
        printf 'rtsp-media-lab stress driver: GL setup diagnostic: %s\n' \
            "${error_line:-no allowlisted error line}" >&2
        die 'headless video setup did not select a software renderer'
    fi

    service_completion="$run_root/results/http-complete.json"
    [[ ! -e "$service_completion" ]] || die 'fixture completion marker was not fresh'
    python3 "$RTSP_LAB_STRESS_FIXTURE_SERVICE" serve \
        --config "$RTSP_LAB_STRESS_SERVICE_CONFIG" \
        --fixture-root "$RTSP_LAB_STRESS_FIXTURE_BYTES" \
        --fixture-manifest "$RTSP_LAB_STRESS_FIXTURE_MANIFEST" \
        --scenario "$RTSP_LAB_STRESS_SCENARIO" \
        --fixture-script "$RTSP_LAB_HTTP_FIXTURE" \
        --log "$run_root/results/http-requests.jsonl" \
        --completion-marker "$service_completion" \
        >"$run_root/results/http-service.log" 2>&1 &
    service_pid=$!
    child_pids+=("$service_pid")
    wait_for_port "$service_port" "$service_pid" || die 'loopback fixture service did not start'

    [[ "$app_id" != 438100 ]] || die 'AppID 438100 is forbidden in the lab'
    scenario_windows=$(windows_path "$RTSP_LAB_STRESS_SCENARIO")
    driver_exe_windows=$(windows_path "$RTSP_LAB_STRESS_DRIVER_EXE")
    driver_result_windows=$(windows_path "$run_root/results/driver.jsonl")
    stress_install_dir=$(dirname -- "$RTSP_LAB_STRESS_DRIVER_EXE")
    [[ "$mode" == audio-monitor ]] && monitor_argument=(--audio-monitor)
    producer_done="$run_root/results/driver.done"
    [[ ! -e "$producer_done" ]] || die "$mode producer sentinel was not fresh"

    python3 "$RTSP_LAB_TRANSPORT_ORACLE" watch \
        --driver-json "$run_root/results/driver.jsonl" \
        --output "$run_root/results/transport-watch.json" \
        --producer-done "$producer_done" \
        --timeout-seconds 330 \
        >"$run_root/results/transport-watch.log" 2>&1 &
    watcher_pid=$!
    child_pids+=("$watcher_pid")

    # This is a standalone compatibility-tool test, not a Steam client.  A
    # synthetic AppID plus an empty Steam directory is not enough: Proton's
    # Steam path can wait for IPC that this isolated guest intentionally does
    # not provide.  Use GE's supported non-Steam route, suppress unrelated
    # per-game fixes, and make GE-main's optional upscaler lookup fail at a
    # closed loopback port while preserving direct 127.0.0.1 fixture access.
    # Keep the declared Nix Python: this pinned GE snapshot imports typing.Self,
    # while the matching Sniper payload still contains Python 3.9.
    set +e
    (
    # The declared mmap policy is the active Wine preloader safeguard. Also
    # fail closed if this guest ever stops inheriting Steam's ordinary 8 MiB
    # soft stack limit; do not silently rewrite another test environment.
    [[ $(ulimit -S -s) == 8192 ]] || die 'guest stack limit is not the representative 8 MiB value'
    exec env -i \
        HOME="$run_root/home" \
        USER=media-lab \
        LOGNAME=media-lab \
        PATH=/run/current-system/sw/bin \
        LANG=C.UTF-8 \
        TMPDIR="$run_root/tmp" \
        XDG_RUNTIME_DIR="$run_root/xdg-runtime" \
        XDG_CACHE_HOME="$run_root/cache" \
        DISPLAY=:77 \
        PULSE_SERVER="unix:$pulse_socket" \
        LIBGL_ALWAYS_SOFTWARE=1 \
        GALLIUM_DRIVER=llvmpipe \
        PYTHONDONTWRITEBYTECODE=1 \
        PROTONFIXES_DISABLE=1 \
        PROTON_USE_XALIA=0 \
        PROTON_LOG=1 \
        PROTON_LOG_DIR="$run_root/results" \
        UMU_ID="$app_id" \
        UMU_USE_STEAM=0 \
        http_proxy=http://127.0.0.1:9 \
        https_proxy=http://127.0.0.1:9 \
        no_proxy=127.0.0.1,localhost \
        STEAM_LINUX_RUNTIME_LOG=1 \
        STEAM_LINUX_RUNTIME_VERBOSE=1 \
        PRESSURE_VESSEL_VARIABLE_DIR="$runtime_variable_dir" \
        PRESSURE_VESSEL_FILESYSTEMS_RO=/nix/store \
        PRESSURE_VESSEL_FILESYSTEMS_RW="$run_root" \
        STEAM_LINUX_RUNTIME_LOG_DIR="$run_root/results/steam-runtime" \
        SteamAppId="$app_id" \
        SteamGameId="$app_id" \
        STEAM_COMPAT_CLIENT_INSTALL_PATH="$run_root/fake-steam" \
        STEAM_COMPAT_DATA_PATH="$run_root/compatdata" \
        STEAM_COMPAT_INSTALL_PATH="$stress_install_dir" \
        STEAM_COMPAT_TOOL_PATHS="$RTSP_LAB_STRESS_PROTON_TOOL:$runtime_copy_dir" \
        WINEDEBUG=-all,+timestamp,+pid,+tid,+mfplat,+quartz,+avprostate \
        "$timeout_bin" --signal=TERM --kill-after=10s 300s \
        "$steam_run_bin" "$runtime_entry" --verb=waitforexitandrun -- \
        "$python_bin" "$RTSP_LAB_STRESS_PROTON_TOOL/proton" waitforexitandrun \
        "$driver_exe_windows" \
        --script "$scenario_windows" \
        --output "$driver_result_windows" \
        --driver-sha256 "$driver_exe_sha256" \
        --scenario-sha256 "$scenario_sha256" \
        "${monitor_argument[@]}"
    ) \
        >"$run_root/results/driver-console.log" 2>&1
    process_exit=$?
    set -e
    printf '%s\n' "$process_exit" >"$producer_done.tmp"
    mv -- "$producer_done.tmp" "$producer_done"

    for _ in $(seq 1 1000); do
        kill -0 "$watcher_pid" 2>/dev/null || break
        sleep 0.01
    done
    watcher_stuck=no
    set +e
    if kill -0 "$watcher_pid" 2>/dev/null; then
        watcher_stuck=yes
        kill "$watcher_pid" 2>/dev/null
    fi
    wait "$watcher_pid"
    watcher_exit=$?
    set -e
    forget_child_pid "$watcher_pid"
    if [[ $process_exit -ne 0 || $watcher_stuck == yes || $watcher_exit -ne 0 || \
          ! -s "$run_root/results/transport-watch.json" ]]; then
        if [[ -s "$run_root/results/transport-watch.json" ]]; then
            watch_summary_present=yes
        else
            watch_summary_present=no
        fi
        printf 'rtsp-media-lab stress driver: %s process exit: %s\n' \
            "$mode" "$process_exit" >&2
        printf 'rtsp-media-lab stress driver: transport watcher exit: %s; summary present: %s\n' \
            "$watcher_exit" "$watch_summary_present" >&2
        if [[ $process_exit -ne 0 ]]; then
            if failure_diagnostic=$(python3 "$RTSP_LAB_FAILURE_DIAGNOSTIC" \
                --console "$run_root/results/driver-console.log" \
                --runtime-log-dir "$run_root/results/steam-runtime" \
                --proton-log "$run_root/results/steam-$app_id.log" \
                --process-exit "$process_exit"); then
                printf 'rtsp-media-lab stress driver: sanitized failure classification: %s\n' \
                    "$failure_diagnostic" >&2
            else
                printf 'rtsp-media-lab stress driver: sanitized failure classification unavailable\n' >&2
            fi
            die "$mode Proton/driver process exited unsuccessfully"
        fi
        [[ $watcher_stuck == no ]] || die "$mode transport watcher did not finish after the driver"
        die "$mode transport watcher rejected the completed driver timeline"
    fi

    [[ -s "$run_root/results/driver.jsonl" ]] || die "$mode driver produced no JSONL result"
    parser_summary="$run_root/results/parser-summary.json"
    set +e
    python3 "$RTSP_LAB_STRESS_PARSER" \
        "$run_root/results/driver.jsonl" \
        --oracle "$oracle" \
        --summary "$parser_summary" \
        >"$run_root/results/parser-console.log" 2>&1
    parser_exit=$?
    set -e
    if [[ "$RTSP_LAB_STRESS_CASE_ROLE" == negative-control && "$mode" == audio-monitor ]]; then
        [[ $parser_exit -eq 1 ]] || die 'instrumented negative control did not produce its declared oracle rejection'
        oracle_outcome=rejected-as-expected
    else
        [[ $parser_exit -eq 0 ]] || die "$mode parser rejected the ephemeral driver evidence"
        oracle_outcome=accepted
    fi
    [[ -s "$parser_summary" ]] || die "$mode parser produced no bounded summary"

    # Stop acceptance, cancel any bounded delay/stall, join all request
    # handlers, and atomically seal the final request-log bytes before scoring.
    graceful_stop_fixture_service "$service_pid" "$service_completion"
    transport_summary="$run_root/results/transport-summary.json"
    python3 "$RTSP_LAB_TRANSPORT_ORACLE" score \
        --http-log "$run_root/results/http-requests.jsonl" \
        --service-completion "$service_completion" \
        --watch "$run_root/results/transport-watch.json" \
        --config "$RTSP_LAB_STRESS_SERVICE_CONFIG" \
        --output "$transport_summary" \
        >"$run_root/results/transport-score.log" 2>&1 ||
        die "$mode transport oracle rejected HTTP/MediaEngine correlation"

    result_summary="$state_root/results/stress-$mode-summary.json"
    python3 "$RTSP_LAB_RESULT_CONTRACT" write-stress \
        --output "$result_summary" \
        --parser-summary "$parser_summary" \
        --parser-log "$run_root/results/parser-console.log" \
        --transport-summary "$transport_summary" \
        --process-exit "$process_exit" \
        --instrumentation "$mode" \
        --build-role "$RTSP_LAB_STRESS_BUILD_ROLE" \
        --case-role "$RTSP_LAB_STRESS_CASE_ROLE" \
        --media-kind "$RTSP_LAB_STRESS_MEDIA_KIND" \
        --oracle-outcome "$oracle_outcome" \
        --proton-tool-sha256 "$proton_tool_sha256" \
        --steam-runtime-sha256 "$steam_runtime_sha256" \
        --driver-exe-sha256 "$driver_exe_sha256" \
        --scenario-sha256 "$scenario_sha256" \
        --fixture-manifest-sha256 "$fixture_manifest_sha256" \
        --fixture-bytes-sha256 "$fixture_bytes_sha256" \
        --service-config-sha256 "$service_config_sha256" \
        --control-oracle-sha256 "$control_oracle_sha256" \
        --instrumented-oracle-sha256 "$instrumented_oracle_sha256" \
        --lab-harness-sha256 "$lab_harness_sha256" \
        --parser-sha256 "$parser_sha256" || die 'could not write strict stress result'
    python3 "$RTSP_LAB_RESULT_CONTRACT" validate --kind stress --path "$result_summary" ||
        die 'strict stress result validation failed'
    cleanup_children
    assert_run_teardown "$run_root"
    printf 'passed\n' >"$state_root/results/stress-$mode.ok"
    rm -rf -- "$run_root"
    [[ ! -e "$run_root" ]] || die 'completed stress run root could not be discarded'
}

run_one control "$RTSP_LAB_STRESS_CONTROL_ORACLE" 999108
run_one audio-monitor "$RTSP_LAB_STRESS_INSTRUMENTED_ORACLE" 999109
printf 'paired stress driver passed; raw evidence remains ephemeral inside the guest.\n'
