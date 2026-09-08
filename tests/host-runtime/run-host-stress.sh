#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

die()
{
    printf 'host MediaEngine runner: %s\n' "$*" >&2
    exit 2
}

usage()
{
    cat <<'EOF'
Usage: run-host-stress.sh [required options]

Required immutable inputs:
  --proton-tool PATH       unpacked Proton tool in /nix/store
  --steam-runtime PATH     Steam Linux Runtime tree in /nix/store
  --driver-exe PATH        MediaEngine stress driver in /nix/store
  --fixture-root PATH      generated fixture tree in /nix/store
  --case-dir PATH          generated stress-case directory in /nix/store
  --python-bin PATH        exact CPython >= 3.11 executable in /nix/store

Required test identity:
  --app-id 990000..999999  synthetic AppID; 438100 is always refused
  --instrumentation MODE   control, audio-monitor, or endpoint-monitor
  --build-role ROLE        stock-ge-control, rtsp-reference-control,
                           frozen-regression-control, streaming-base-candidate,
                           or full-parity-candidate
  --case-role ROLE         expected-pass, negative-control, or qualification
  --media-kind KIND        av, audio-only, or video-only

Required host-graphics opt-in:
  --display DISPLAY
  --host-graphics-consent I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY

Optional tool overrides:
  --pulseaudio-bin PATH
  --pactl-bin PATH
  --parec-bin PATH          required by endpoint-monitor
  --steam-run-bin PATH
  --timeout-bin PATH
  --xauthority PATH        copied read-only into the fresh run directory
  --scheduler-pressure PROFILE
                           none (default) or scheduler-pressure-v1
  --rtsp-drain-diagnostics
                           select the bounded rtspdrain channel and narrow
                           DMO/MF warning/error channels for its reviewed case

This inner runner must be invoked by run-contained-host-stress.sh. It does not
invoke Steam and never uses AppID 438100. It keeps raw evidence below a fresh
/var/tmp/rtsp-media-host.* directory, removes the copied runtime and prefix
after teardown, and prints the retained evidence directory.
EOF
}

[[ $(id -u) -ne 0 ]] || die 'refusing to run Proton as root'

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
contract="$script_dir/host_run_contract.py"
store_digest="$repo_root/tests/nix-vm/store_input_digest.py"
result_contract="$repo_root/tests/nix-vm/result_contract.py"
fixture_service="$repo_root/tests/nix-vm/stress_fixture_service.py"
transport_oracle="$repo_root/tests/nix-vm/transport_oracle.py"
http_fixture="$repo_root/tests/http-stream-fixture/http_stream_fixture.py"
parser="$repo_root/tests/media-engine-stress/driver/parse_results.py"
live_hls_case="$script_dir/live_hls_case.py"
live_fixture="$repo_root/tests/media-engine-stress/live-fixture/live_fixture.py"
rtsp_live_case="$script_dir/rtsp_live_case.py"
endpoint_audio_oracle="$script_dir/endpoint_audio_oracle.py"
scheduler_pressure_helper="$script_dir/scheduler_pressure.py"

proton_tool=
steam_runtime=
driver_exe=
fixture_root=
case_dir=
python_bin=
app_id=
instrumentation=
build_role=
case_role=
media_kind=
host_display=
graphics_consent=
xauthority=
pulseaudio_bin=
pactl_bin=
parec_bin=
steam_run_bin=
timeout_bin=
scheduler_pressure_profile=none
rtsp_drain_diagnostics=0

while (($#)); do
    case $1 in
        --proton-tool|--steam-runtime|--driver-exe|--fixture-root|--case-dir|--python-bin|\
        --app-id|--instrumentation|--build-role|--case-role|--media-kind|--display|\
        --host-graphics-consent|--xauthority|--pulseaudio-bin|--pactl-bin|--parec-bin|\
        --steam-run-bin|--timeout-bin|--scheduler-pressure)
            option=$1
            shift
            (($#)) || die "$option requires a value"
            case $option in
                --proton-tool) proton_tool=$1 ;;
                --steam-runtime) steam_runtime=$1 ;;
                --driver-exe) driver_exe=$1 ;;
                --fixture-root) fixture_root=$1 ;;
                --case-dir) case_dir=$1 ;;
                --python-bin) python_bin=$1 ;;
                --app-id) app_id=$1 ;;
                --instrumentation) instrumentation=$1 ;;
                --build-role) build_role=$1 ;;
                --case-role) case_role=$1 ;;
                --media-kind) media_kind=$1 ;;
                --display) host_display=$1 ;;
                --host-graphics-consent) graphics_consent=$1 ;;
                --xauthority) xauthority=$1 ;;
                --pulseaudio-bin) pulseaudio_bin=$1 ;;
                --pactl-bin) pactl_bin=$1 ;;
                --parec-bin) parec_bin=$1 ;;
                --steam-run-bin) steam_run_bin=$1 ;;
                --timeout-bin) timeout_bin=$1 ;;
                --scheduler-pressure) scheduler_pressure_profile=$1 ;;
            esac
            ;;
        --rtsp-drain-diagnostics)
            rtsp_drain_diagnostics=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

for variable in proton_tool steam_runtime driver_exe fixture_root case_dir python_bin \
    app_id instrumentation build_role case_role media_kind host_display graphics_consent; do
    [[ -n ${!variable:-} ]] || die "missing --${variable//_/-}"
done

[[ ${RTSP_HOST_CONTAINMENT:-} == outer-bwrap-v1 ]] ||
    die 'direct execution is forbidden; use run-contained-host-stress.sh'
containment_manifest=${RTSP_HOST_CONTAINMENT_MANIFEST:-}
[[ $containment_manifest == /var/tmp/outer-containment.json \
    && -f $containment_manifest && ! -L $containment_manifest ]] ||
    die 'validated outer-containment manifest is absent'

[[ "$graphics_consent" == I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY ]] ||
    die 'the exact host-graphics consent token is required'
[[ "$host_display" =~ ^:[0-9]+([.][0-9]+)?$ ]] ||
    die 'DISPLAY must be a local X11 display such as :0'
[[ "$app_id" =~ ^[0-9]+$ ]] || die 'AppID must be decimal digits'
((app_id >= 990000 && app_id <= 999999)) || die 'AppID must be in the synthetic range 990000..999999'
((app_id != 438100)) || die 'AppID 438100 is forbidden'
case $instrumentation in
    control|audio-monitor|endpoint-monitor) ;;
    *) die 'invalid instrumentation mode' ;;
esac
case $build_role in
    stock-ge-control|rtsp-reference-control|frozen-regression-control|streaming-base-candidate|full-parity-candidate) ;;
    *) die 'invalid build role' ;;
esac
case $case_role in expected-pass|negative-control|qualification) ;; *) die 'invalid case role' ;; esac
case $media_kind in av|audio-only|video-only) ;; *) die 'invalid media kind' ;; esac
case $scheduler_pressure_profile in
    none|scheduler-pressure-v1) ;;
    *) die 'invalid scheduler-pressure profile' ;;
esac
[[ $case_role != negative-control || $instrumentation == audio-monitor ]] ||
    die 'a negative control requires audio-monitor instrumentation'
[[ $instrumentation != endpoint-monitor || $media_kind != video-only ]] ||
    die 'endpoint-monitor is invalid for video-only media'

require_store_directory()
{
    [[ "$1" == /nix/store/* && "$1" != /nix/store/ && -d "$1" && ! -L "$1" ]] ||
        die "$2 must be an explicit immutable Nix-store directory"
}

require_store_file()
{
    [[ "$1" == /nix/store/* && -f "$1" && ! -L "$1" ]] ||
        die "$2 must be an explicit immutable Nix-store regular file"
}

resolve_command()
{
    local supplied=$1 name=$2 resolved
    if [[ -z "$supplied" ]]; then
        supplied=$(command -v "$name" 2>/dev/null) || die "$name is unavailable; pass --$name-bin"
    fi
    [[ "$supplied" == /* && -x "$supplied" ]] || die "$name must be an absolute executable path"
    resolved=$(readlink -f -- "$supplied") || die "$name could not be resolved"
    [[ "$resolved" == /nix/store/* && -x "$resolved" ]] || die "$name must resolve into /nix/store"
    printf '%s\n' "$resolved"
}

require_store_directory "$proton_tool" 'Proton tool'
require_store_directory "$steam_runtime" 'Steam Runtime'
require_store_directory "$fixture_root" 'fixture root'
require_store_directory "$case_dir" 'case directory'
require_store_file "$driver_exe" 'driver executable'
python_bin=$(readlink -f -- "$python_bin") || die 'Python executable could not be resolved'
require_store_file "$python_bin" 'Python executable'
[[ -x "$python_bin" ]] || die 'Python input is not executable'

pulseaudio_bin=$(resolve_command "$pulseaudio_bin" pulseaudio)
pactl_bin=$(resolve_command "$pactl_bin" pactl)
if [[ $instrumentation == endpoint-monitor ]]; then
    parec_bin=$(resolve_command "$parec_bin" parec)
fi
steam_run_bin=$(resolve_command "$steam_run_bin" steam-run)
if [[ -z "$timeout_bin" ]]; then
    timeout_bin=$(command -v timeout 2>/dev/null) || die 'timeout is unavailable; pass --timeout-bin'
fi
[[ "$timeout_bin" == /* && -x "$timeout_bin" && $(basename -- "$timeout_bin") == timeout ]] ||
    die 'timeout must be an absolute executable whose basename remains timeout'
[[ $(readlink -f -- "$timeout_bin") == /nix/store/* ]] || die 'timeout must resolve into /nix/store'

if [[ -n "$xauthority" ]]; then
    [[ "$xauthority" == /* && -f "$xauthority" && ! -L "$xauthority" && -r "$xauthority" ]] ||
        die 'Xauthority must be an absolute readable nonsymlink regular file'
fi

for helper in "$contract" "$store_digest" "$result_contract" "$fixture_service" \
    "$transport_oracle" "$http_fixture" "$parser" "$live_hls_case" "$live_fixture" \
    "$rtsp_live_case" "$endpoint_audio_oracle" "$scheduler_pressure_helper"; do
    [[ -f "$helper" && ! -L "$helper" ]] || die 'a checked-in harness helper is absent or is a symlink'
done
[[ -f "$proton_tool/proton" && ! -L "$proton_tool/proton" ]] || die 'Proton launcher is missing'

scenario="$case_dir/scenario"
service_config="$case_dir/service-config.json"
control_oracle="$case_dir/control.oracle.json"
instrumented_oracle="$case_dir/instrumented.oracle.json"
case_metadata="$case_dir/case-metadata.json"
fixture_manifest="$fixture_root/provenance/manifest.json"
for file in "$scenario" "$service_config" "$control_oracle" "$instrumented_oracle" "$fixture_manifest"; do
    require_store_file "$file" 'case/fixture input'
done

IFS=$'\t' read -r service_kind case_id source_scheme fixture_sha256 < <(
    "$python_bin" -I - "$service_config" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
payload = path.read_bytes()
if not (0 < len(payload) <= 1024 * 1024) or b"\0" in payload:
    raise SystemExit(2)
value = json.loads(payload.decode("utf-8"))
service = value.get("service") if type(value) is dict else None
case_id = value.get("caseId") if type(value) is dict else None
source_scheme = value.get("sourceScheme") if type(value) is dict else None
fixture_sha256 = value.get("fixtureSha256") if type(value) is dict else None
if service not in {"progressive-http-v1", "live-hls-v1", "rtsp-live-v1"}:
    raise SystemExit(2)
if not isinstance(case_id, str) or not case_id or len(case_id) > 64:
    raise SystemExit(2)
if source_scheme is not None and not isinstance(source_scheme, str):
    raise SystemExit(2)
if fixture_sha256 is not None and not isinstance(fixture_sha256, str):
    raise SystemExit(2)
source_scheme = "-" if source_scheme is None else source_scheme
fixture_sha256 = "-" if fixture_sha256 is None else fixture_sha256
print(f"{service}\t{case_id}\t{source_scheme}\t{fixture_sha256}")
PY
) || die 'case service identity is invalid'

if [[ -e $case_metadata ]]; then
    require_store_file "$case_metadata" 'case metadata input'
    metadata_case_role=$("$python_bin" -I -c '
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if type(value) is not dict or value.get("schema") != 1:
    raise SystemExit(2)
if value.get("caseId") != sys.argv[2]:
    raise SystemExit(2)
role = value.get("caseRole")
if role not in {"expected-pass", "negative-control", "qualification"}:
    raise SystemExit(2)
print(role)
' "$case_metadata" "$case_id") || die 'case metadata identity is invalid'
    [[ $case_role == "$metadata_case_role" ]] ||
        die "declared case role differs from immutable metadata: $metadata_case_role"
fi
endpoint_checkpoint_arguments=()
case $service_kind in
    progressive-http-v1)
        [[ $instrumentation != endpoint-monitor ]] ||
            die 'endpoint-monitor currently requires a reviewed live HLS or RTSP case'
        fixture_adapter=$fixture_service
        selected_transport_oracle=$transport_oracle
        fixture_network=http-ipv4-loopback-only
        fixture_mode=$("$python_bin" -I -c '
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
mode = value.get("mode") if type(value) is dict else None
if type(mode) is not str: raise SystemExit(2)
print(mode)
' "$service_config") || die 'progressive fixture mode is invalid'
        case $fixture_mode in
            fail-post-open-range|fail-post-open-range-recovery)
                [[ $case_role == expected-pass ]] ||
                    die 'failed-Range modes are bounded expected-pass error cases, not qualifications'
                ;;
        esac
        ;;
    live-hls-v1)
        [[ $instrumentation == audio-monitor || $instrumentation == endpoint-monitor ]] ||
            die 'the live HLS qualification requires an audio observation mode'
        [[ $case_role == qualification ]] ||
            die 'the healthy live HLS adapter is qualification-only'
        required_live_instrumentation=$("$python_bin" -I -c '
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
selected = value.get("requiredInstrumentation") if type(value) is dict else None
if selected not in {"audio-monitor", "endpoint-monitor"}: raise SystemExit(2)
print(selected)
' "$service_config") || die 'live HLS instrumentation identity is invalid'
        [[ $required_live_instrumentation == "$instrumentation" ]] ||
            die "live HLS case requires $required_live_instrumentation instrumentation"
        fixture_adapter=$live_hls_case
        selected_transport_oracle=$live_hls_case
        fixture_network=http-ipv4-loopback-only
        endpoint_checkpoint_arguments=(--checkpoint live-steady=8)
        ;;
    rtsp-live-v1)
        [[ $instrumentation == audio-monitor || $instrumentation == endpoint-monitor ]] ||
            die 'the live RTSP qualification requires an audio observation mode'
        case $case_id in
            rtsp-live-tcp-reopen) expected_rtsp_role=qualification ;;
            rtsp-live-tcp-pause-before-replace) expected_rtsp_role=expected-pass ;;
            rtsp-live-tcp-blackhole-cancel)
                expected_rtsp_role=expected-pass
                [[ $instrumentation == audio-monitor ]] ||
                    die 'the RTSP blackhole cancellation gate requires audio-monitor instrumentation'
                ;;
            rtsp-live-tcp-finite-hold-recovery)
                expected_rtsp_role=expected-pass
                [[ $instrumentation == audio-monitor ]] ||
                    die 'the RTSP finite-hold recovery gate requires audio-monitor instrumentation'
                ;;
            rtsp-live-tcp-drain-diagnostics)
                expected_rtsp_role=qualification
                [[ $instrumentation == audio-monitor ]] ||
                    die 'the RTSP drain-diagnostics gate requires audio-monitor instrumentation'
                [[ $source_scheme == rtspt \
                   && $fixture_sha256 == 8e2a8df84d9f257d3d0d3c24372ec644d9366cd9d734b284fe50d3b6a6346bde ]] ||
                    die 'the RTSP drain-diagnostics gate requires the reviewed captured-input RTSPT case'
                ;;
            *) die "unreviewed RTSP case identity: $case_id" ;;
        esac
        [[ $case_role == "$expected_rtsp_role" ]] ||
            die "declared case role differs from reviewed RTSP role: $expected_rtsp_role"
        fixture_adapter=$rtsp_live_case
        selected_transport_oracle=$rtsp_live_case
        fixture_network=rtsp-interleaved-tcp-ipv4-loopback-only
        if [[ $case_id == rtsp-live-tcp-blackhole-cancel ||
              $case_id == rtsp-live-tcp-finite-hold-recovery ||
              $case_id == rtsp-live-tcp-drain-diagnostics ]]; then
            endpoint_checkpoint_arguments=()
        else
            endpoint_checkpoint_arguments=(
                --checkpoint rtsp-live-g1=4
                --checkpoint rtsp-live-g2=4
                --checkpoint rtsp-live-g3=4
            )
        fi
        ;;
esac
if ((rtsp_drain_diagnostics)); then
    [[ $service_kind == rtsp-live-v1 \
       && $case_id == rtsp-live-tcp-drain-diagnostics ]] ||
        die '--rtsp-drain-diagnostics is accepted only for the reviewed RTSP drain-diagnostics case'
elif [[ $case_id == rtsp-live-tcp-drain-diagnostics ]]; then
    die 'the RTSP drain-diagnostics case requires --rtsp-drain-diagnostics'
fi
if [[ $scheduler_pressure_profile == scheduler-pressure-v1 ]]; then
    [[ $service_kind == rtsp-live-v1 \
       && $case_id == rtsp-live-tcp-reopen \
       && $source_scheme == rtspt \
       && $fixture_sha256 == 8e2a8df84d9f257d3d0d3c24372ec644d9366cd9d734b284fe50d3b6a6346bde \
       && $instrumentation == audio-monitor \
       && $build_role == streaming-base-candidate \
       && $case_role == qualification \
       && $media_kind == av ]] ||
        die 'scheduler-pressure-v1 requires the reviewed captured-input RTSPT qualification'
fi
if [[ $instrumentation == audio-monitor ]]; then
    selected_oracle=$instrumented_oracle
    monitor_argument=(--audio-monitor)
else
    selected_oracle=$control_oracle
    monitor_argument=()
fi

python_version=$("$python_bin" -I -c '
import platform, sys
if sys.version_info < (3, 11): raise SystemExit(2)
print(f"{platform.python_implementation()} {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
') || die 'the exact Python input must be CPython 3.11 or newer'
[[ "$python_version" =~ ^CPython\ [0-9]+[.][0-9]+[.][0-9]+$ ]] || die 'unexpected Python implementation/version'

digest_file()
{
    "$python_bin" -I "$store_digest" --expect file --path "$1"
}

digest_tree()
{
    "$python_bin" -I "$store_digest" --expect directory --path "$1"
}

proton_tool_sha256=$(digest_tree "$proton_tool")
steam_runtime_sha256=$(digest_tree "$steam_runtime")
case_directory_sha256=$(digest_tree "$case_dir")
driver_exe_sha256=$(digest_file "$driver_exe")
scenario_sha256=$(digest_file "$scenario")
fixture_manifest_sha256=$(digest_file "$fixture_manifest")
fixture_bytes_sha256=$(digest_tree "$fixture_root")
service_config_sha256=$(digest_file "$service_config")
control_oracle_sha256=$(digest_file "$control_oracle")
instrumented_oracle_sha256=$(digest_file "$instrumented_oracle")
parser_sha256=$(sha256sum -- "$parser" | awk '{print $1}')
endpoint_audio_oracle_sha256=$(sha256sum -- "$endpoint_audio_oracle" | awk '{print $1}')
scheduler_pressure_helper_sha256=$(sha256sum -- "$scheduler_pressure_helper" | awk '{print $1}')
transport_oracle_sha256=$(sha256sum -- "$selected_transport_oracle" | awk '{print $1}')
pactl_executable_sha256=$(digest_file "$pactl_bin")
pulseaudio_executable_sha256=$(digest_file "$pulseaudio_bin")
parec_plan_argument=()
if [[ $instrumentation == endpoint-monitor ]]; then
    parec_executable_sha256=$(digest_file "$parec_bin")
    parec_plan_argument=(--parec-executable "$parec_executable_sha256")
fi
outer_runner="$script_dir/run-contained-host-stress.sh"
[[ -f $outer_runner && ! -L $outer_runner ]] || die 'outer host runner is absent or is a symlink'
host_runner_sha256=$("$python_bin" -I - \
    "$0" "$outer_runner" \
    "$contract" "$store_digest" "$result_contract" "$fixture_service" \
    "$transport_oracle" "$http_fixture" "$parser" "$live_hls_case" \
    "$live_fixture" "$rtsp_live_case" "$endpoint_audio_oracle" \
    "$scheduler_pressure_helper" <<'PY'
import hashlib
from pathlib import Path
import sys
digest = hashlib.sha256()
for name in sys.argv[1:]:
    payload = Path(name).read_bytes()
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
print(digest.hexdigest())
PY
) || die 'could not bind the complete host-runner boundary'
containment_manifest_sha256=$(sha256sum -- "$containment_manifest" | awk '{print $1}')
host_contract_sha256=$(sha256sum -- "$contract" | awk '{print $1}')
python_executable_sha256=$(digest_file "$python_bin")

"$python_bin" -I "$result_contract" check-stress-oracles \
    --control "$control_oracle" \
    --instrumented "$instrumented_oracle" \
    --driver-sha256 "$driver_exe_sha256" \
    --scenario-sha256 "$scenario_sha256" \
    --media-kind "$media_kind" || die 'oracles do not bind the selected driver/scenario bytes'

service_port=$("$python_bin" -I "$fixture_adapter" check \
    --config "$service_config" \
    --fixture-root "$fixture_root" \
    --fixture-manifest "$fixture_manifest" \
    --scenario "$scenario") || die 'loopback fixture closure validation failed'
[[ "$service_port" =~ ^[1-9][0-9]{0,4}$ && "$service_port" != 9 ]] || die 'invalid fixture port'

[[ -d /var/tmp && -w /var/tmp && ! -L /var/tmp ]] || die '/var/tmp is not a writable nonsymlink directory'
run_root=$(mktemp -d --tmpdir=/var/tmp rtsp-media-host.XXXXXXXX) || die 'could not create fresh run state'
[[ "$run_root" =~ ^/var/tmp/rtsp-media-host[.][A-Za-z0-9]+$ && -d "$run_root" && ! -L "$run_root" ]] ||
    die 'fresh run path is not canonical'
chmod 700 "$run_root"
results_root="$run_root/results"
mkdir -m 700 \
    "$results_root" \
    "$results_root/steam-runtime" \
    "$run_root/home" \
    "$run_root/cache" \
    "$run_root/tmp" \
    "$run_root/xdg-runtime" \
    "$run_root/compatdata" \
    "$run_root/fake-steam" \
    "$run_root/pulse" \
    "$run_root/steam-runtime-var" \
    "$run_root/steam-runtime-copy"
install -m 600 -- "$containment_manifest" "$results_root/containment-manifest.json"

if [[ -n "$xauthority" ]]; then
    install -m 600 -- "$xauthority" "$run_root/xauthority"
    xauthority_environment=(XAUTHORITY="$run_root/xauthority")
else
    xauthority_environment=()
fi

"$python_bin" -I "$contract" write-plan \
    --output "$results_root/input-manifest.json" \
    --app-id "$app_id" \
    --build-role "$build_role" \
    --case-role "$case_role" \
    --media-kind "$media_kind" \
    --instrumentation "$instrumentation" \
    --fixture-network "$fixture_network" \
    --python-version "$python_version" \
    --scheduler-pressure-profile "$scheduler_pressure_profile" \
    --case-directory "$case_directory_sha256" \
    --containment-manifest "$containment_manifest_sha256" \
    --control-oracle "$control_oracle_sha256" \
    --driver-exe "$driver_exe_sha256" \
    --endpoint-audio-oracle "$endpoint_audio_oracle_sha256" \
    --fixture-bytes "$fixture_bytes_sha256" \
    --fixture-manifest "$fixture_manifest_sha256" \
    --host-contract "$host_contract_sha256" \
    --host-runner "$host_runner_sha256" \
    --instrumented-oracle "$instrumented_oracle_sha256" \
    --parser "$parser_sha256" \
    "${parec_plan_argument[@]}" \
    --pactl-executable "$pactl_executable_sha256" \
    --proton-tool "$proton_tool_sha256" \
    --pulseaudio-executable "$pulseaudio_executable_sha256" \
    --python-executable "$python_executable_sha256" \
    --scenario "$scenario_sha256" \
    --scheduler-pressure-helper "$scheduler_pressure_helper_sha256" \
    --service-config "$service_config_sha256" \
    --steam-runtime "$steam_runtime_sha256" \
    --transport-oracle "$transport_oracle_sha256"
"$python_bin" -I "$contract" validate --kind plan --path "$results_root/input-manifest.json"

child_pids=()
stage=preflight
outcome=incomplete
process_exit=-1
watcher_exit=-1
parser_exit=-1
endpoint_audio_exit=-1
endpoint_capture_pid=
endpoint_capture_started_ns=
endpoint_capture_anchor_frame=
endpoint_capture_anchor_ns=
endpoint_capture_finished_ns=
scheduler_pressure_pid=
scheduler_pressure_ready="$run_root/tmp/scheduler-pressure-ready.json"
scheduler_pressure_raw="$run_root/tmp/scheduler-pressure-raw.json"
scheduler_pressure_started_ns=
scheduler_pressure_exited_ns=

if [[ $scheduler_pressure_profile == none ]]; then
    "$python_bin" -I "$scheduler_pressure_helper" write-disabled \
        --output "$results_root/scheduler-pressure-summary.json"
fi

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
        child_has_exited "$pid" || kill -TERM "$pid" 2>/dev/null || true
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

run_root_survivors()
{
    "$python_bin" -I - "$run_root" <<'PY'
import os
from pathlib import Path
import sys

needle = os.fsencode(sys.argv[1])
own = {os.getpid(), os.getppid()}
found = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) in own:
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        payload = (entry / "environ").read_bytes() + (entry / "cmdline").read_bytes()
        matched = needle in payload
        if not matched:
            for descriptor in (entry / "fd").iterdir():
                try:
                    target = os.fsencode(os.readlink(descriptor))
                except OSError:
                    continue
                if target.startswith(needle):
                    matched = True
                    break
        if matched:
            found.append(int(entry.name))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
print(" ".join(str(pid) for pid in sorted(found)))
PY
}

cleanup_survivors()
{
    local survivors
    local -a pids=()
    for _ in $(seq 1 100); do
        survivors=$(run_root_survivors)
        [[ -z "$survivors" ]] && return 0
        sleep 0.05
    done
    read -r -a pids <<<"$survivors"
    kill -TERM "${pids[@]}" 2>/dev/null || true
    sleep 0.25
    survivors=$(run_root_survivors)
    if [[ -n "$survivors" ]]; then
        read -r -a pids <<<"$survivors"
        kill -KILL "${pids[@]}" 2>/dev/null || true
    fi
    sleep 0.1
    [[ -z $(run_root_survivors) ]]
}

finalize()
{
    local saved_exit proton_log_source
    saved_exit=$1
    trap - EXIT HUP INT TERM
    cleanup_children
    if ! cleanup_survivors; then
        printf 'host MediaEngine runner: warning: run-bound processes survived teardown\n' >&2
        outcome=harness-failed
        saved_exit=2
    fi
    proton_log_source="$results_root/steam-$app_id.log"
    if [[ -f "$proton_log_source" && ! -L "$proton_log_source" ]]; then
        mv -- "$proton_log_source" "$results_root/proton.log"
    fi
    # The prefix, copied Runtime, caches, and private service state are never
    # retained. Only bounded logs/manifests under results remain for review.
    if [[ "$run_root" =~ ^/var/tmp/rtsp-media-host[.][A-Za-z0-9]+$ && -d "$run_root" && ! -L "$run_root" ]]; then
        if ! rm -rf -- \
            "${run_root:?}/cache" \
            "${run_root:?}/compatdata" \
            "${run_root:?}/fake-steam" \
            "${run_root:?}/home" \
            "${run_root:?}/pulse" \
            "${run_root:?}/steam-runtime-copy" \
            "${run_root:?}/steam-runtime-var" \
            "${run_root:?}/tmp" \
            "${run_root:?}/xdg-runtime" \
            "${run_root:?}/xauthority"; then
            printf 'host MediaEngine runner: mutable run-state cleanup failed\n' >&2
            outcome=harness-failed
            saved_exit=2
        fi
    else
        printf 'host MediaEngine runner: refusing cleanup of a noncanonical run root\n' >&2
        outcome=harness-failed
        saved_exit=2
    fi
    "$python_bin" -I "$contract" write-evidence \
        --output "$results_root/evidence-manifest.json" \
        --plan "$results_root/input-manifest.json" \
        --results-root "$results_root" \
        --outcome "$outcome" \
        --stage "$stage" \
        --process-exit "$process_exit" \
        --watcher-exit "$watcher_exit" \
        --parser-exit "$parser_exit" \
        --endpoint-audio-exit "$endpoint_audio_exit" || saved_exit=2
    if [[ -s "$results_root/evidence-manifest.json" ]]; then
        "$python_bin" -I "$contract" validate --kind evidence \
            --path "$results_root/evidence-manifest.json" || saved_exit=2
    fi
    printf 'host MediaEngine runner: retained evidence: %s\n' "$results_root" >&2
    exit "$saved_exit"
}

trap 'finalize $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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
    for _ in $(seq 1 2400); do
        if "$python_bin" -I - "$port" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.1)
try:
    raise SystemExit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
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

graceful_stop_fixture_service()
{
    local pid=$1 completion=$2 service_exit
    child_has_exited "$pid" || kill -TERM "$pid" 2>/dev/null || true
    if ! wait_child_bounded "$pid" 1500 0.01; then
        kill -KILL "$pid" 2>/dev/null || true
        wait_child_bounded "$pid" 100 0.01 || true
        wait "$pid" 2>/dev/null || true
        forget_child_pid "$pid"
        return 1
    fi
    set +e
    wait "$pid"
    service_exit=$?
    set -e
    forget_child_pid "$pid"
    [[ $service_exit -eq 0 && -s "$completion" ]]
}

stop_endpoint_capture()
{
    local capture_exit
    [[ -n $endpoint_capture_pid ]] || return 1
    if child_has_exited "$endpoint_capture_pid"; then
        set +e
        wait "$endpoint_capture_pid"
        capture_exit=$?
        set -e
        forget_child_pid "$endpoint_capture_pid"
        return 1
    fi
    kill -TERM "$endpoint_capture_pid" 2>/dev/null || return 1
    if ! wait_child_bounded "$endpoint_capture_pid" 500 0.01; then
        kill -KILL "$endpoint_capture_pid" 2>/dev/null || true
        wait_child_bounded "$endpoint_capture_pid" 100 0.01 || true
        wait "$endpoint_capture_pid" 2>/dev/null || true
        forget_child_pid "$endpoint_capture_pid"
        return 1
    fi
    set +e
    wait "$endpoint_capture_pid"
    capture_exit=$?
    set -e
    forget_child_pid "$endpoint_capture_pid"
    case $capture_exit in 0|143) ;; *) return 1 ;; esac
    endpoint_capture_finished_ns=$("$python_bin" -I "$endpoint_audio_oracle" now) ||
        return 1
    [[ $endpoint_capture_finished_ns =~ ^[1-9][0-9]+$ ]]
}

runtime_copy_dir="$run_root/steam-runtime-copy"
runtime_variable_dir="$run_root/steam-runtime-var"
cp -a --reflink=auto --no-preserve=ownership "$steam_runtime/." "$runtime_copy_dir/"
chmod -R u+w "$runtime_copy_dir"
copied_runtime_sha256=$("$python_bin" -I "$store_digest" \
    --expect directory --path "$steam_runtime" --compare-copy "$runtime_copy_dir") ||
    die 'Steam Runtime writable-copy verification failed'
[[ "$copied_runtime_sha256" == "$steam_runtime_sha256" ]] || die 'Steam Runtime copy identity differs'
if [[ -x "$runtime_copy_dir/_v2-entry-point" ]]; then
    runtime_entry="$runtime_copy_dir/_v2-entry-point"
elif [[ -x "$runtime_copy_dir/SteamLinuxRuntime_4/_v2-entry-point" ]]; then
    runtime_entry="$runtime_copy_dir/SteamLinuxRuntime_4/_v2-entry-point"
else
    die 'Steam Runtime entry point is absent'
fi

pulse_socket="$run_root/pulse/native"
pulse_config="$run_root/pulse/default.pa"
printf '%s\n' \
    '.fail' \
    "load-module module-native-protocol-unix socket=$pulse_socket auth-anonymous=1" \
    'load-module module-null-sink sink_name=rtsp_host_null format=s16le rate=48000 channels=2 channel_map=front-left,front-right' \
    'set-default-sink rtsp_host_null' \
    'set-default-source rtsp_host_null.monitor' \
    >"$pulse_config"
HOME="$run_root/home" XDG_RUNTIME_DIR="$run_root/xdg-runtime" \
    "$pulseaudio_bin" --daemonize=no --exit-idle-time=-1 --disable-shm=true \
    --log-target="file:$results_root/pulseaudio.log" --file="$pulse_config" &
pulse_pid=$!
child_pids+=("$pulse_pid")
wait_for_path "$pulse_socket" "$pulse_pid" || die 'private PulseAudio endpoint did not start'
PULSE_SERVER="unix:$pulse_socket" "$pactl_bin" list sinks >"$results_root/pulse-sinks.txt"
grep -Fq 'Name: rtsp_host_null' "$results_root/pulse-sinks.txt" || die 'private null sink is absent'
grep -Fq 'Sample Specification: s16le 2ch 48000Hz' "$results_root/pulse-sinks.txt" ||
    die 'private null sink format differs'

stage=fixture
service_completion="$results_root/http-complete.json"
service_command=(
    "$python_bin" -I "$fixture_adapter" serve
    --config "$service_config"
    --fixture-root "$fixture_root"
    --fixture-manifest "$fixture_manifest"
    --scenario "$scenario"
)
if [[ $service_kind == progressive-http-v1 ]]; then
    service_command+=(--fixture-script "$http_fixture")
fi
service_command+=(
    --log "$results_root/http-requests.jsonl"
    --completion-marker "$service_completion"
)
if [[ $scheduler_pressure_profile == scheduler-pressure-v1 ]]; then
    "$python_bin" -I "$scheduler_pressure_helper" exec --role fixture -- \
        "${service_command[@]}" >"$results_root/http-service.log" 2>&1 &
else
    "${service_command[@]}" >"$results_root/http-service.log" 2>&1 &
fi
service_pid=$!
child_pids+=("$service_pid")
wait_for_port "$service_port" "$service_pid" || die 'validated loopback fixture service did not start'

producer_done="$results_root/driver.done"
# transport_oracle imports the declared sibling stress_fixture_service module.
# -E/-s remove ambient/user Python configuration without -I's removal of the
# script directory; -B prevents workspace bytecode writes.
watch_config_argument=()
case $service_kind in
    live-hls-v1|rtsp-live-v1) watch_config_argument=(--config "$service_config") ;;
esac
watch_instrumentation_argument=()
[[ $service_kind == progressive-http-v1 ]] ||
    watch_instrumentation_argument=(--instrumentation "$instrumentation")
"$python_bin" -B -E -s "$selected_transport_oracle" watch \
    --driver-json "$results_root/driver.jsonl" \
    --output "$results_root/transport-watch.json" \
    --producer-done "$producer_done" \
    --timeout-seconds 330 \
    "${watch_config_argument[@]}" \
    "${watch_instrumentation_argument[@]}" \
    >"$results_root/transport-watch.log" 2>&1 &
watcher_pid=$!
child_pids+=("$watcher_pid")

if [[ $instrumentation == endpoint-monitor ]]; then
    endpoint_capture_started_ns=$("$python_bin" -I "$endpoint_audio_oracle" now) ||
        die 'could not timestamp endpoint capture start'
    [[ $endpoint_capture_started_ns =~ ^[1-9][0-9]+$ ]] ||
        die 'endpoint capture start timestamp is invalid'
    HOME="$run_root/home" XDG_RUNTIME_DIR="$run_root/xdg-runtime" \
        PULSE_SERVER="unix:$pulse_socket" \
        "$parec_bin" \
        --record \
        --device=rtsp_host_null.monitor \
        --format=s16le \
        --rate=48000 \
        --channels=2 \
        --channel-map=front-left,front-right \
        --no-remix \
        --no-remap \
        --raw \
        --latency-msec=50 \
        >"$results_root/endpoint-audio.raw" \
        2>"$results_root/endpoint-audio-capture.log" &
    endpoint_capture_pid=$!
    child_pids+=("$endpoint_capture_pid")
    for _ in $(seq 1 500); do
        child_has_exited "$endpoint_capture_pid" &&
            die 'private endpoint recorder exited before becoming ready'
        [[ -s "$results_root/endpoint-audio.raw" ]] && break
        sleep 0.01
    done
    [[ -s "$results_root/endpoint-audio.raw" ]] ||
        die 'private endpoint recorder produced no PCM within five seconds'
    endpoint_capture_anchor=$(
        "$python_bin" -I "$endpoint_audio_oracle" anchor \
            --pcm "$results_root/endpoint-audio.raw"
    ) || die 'could not establish endpoint capture readiness anchor'
    read -r endpoint_capture_anchor_frame endpoint_capture_anchor_ns endpoint_capture_anchor_extra \
        <<<"$endpoint_capture_anchor"
    [[ $endpoint_capture_anchor_frame =~ ^[1-9][0-9]*$
       && $endpoint_capture_anchor_ns =~ ^[1-9][0-9]*$
       && -z $endpoint_capture_anchor_extra ]] ||
        die 'endpoint capture readiness anchor is invalid'
    child_has_exited "$endpoint_capture_pid" &&
        die 'private endpoint recorder exited after becoming ready'
fi

windows_path()
{
    printf 'Z:%s' "$1" | sed 's,/,\\,g'
}

driver_exe_windows=$(windows_path "$driver_exe")
scenario_windows=$(windows_path "$scenario")
driver_result_windows=$(windows_path "$results_root/driver.jsonl")
stress_install_dir=$(dirname -- "$driver_exe")
wine_debug=-all
if ((rtsp_drain_diagnostics)); then
    wine_debug='-all,+timestamp,+pid,+tid,+rtspdrain,warn+dmo,err+dmo,warn+mfplat,err+mfplat'
fi

runtime_environment=(
    HOME="$run_root/home"
    USER="$(id -un)"
    LOGNAME="$(id -un)"
    PATH=/run/current-system/sw/bin
    LANG=C.UTF-8
    TMPDIR="$run_root/tmp"
    XDG_RUNTIME_DIR="$run_root/xdg-runtime"
    XDG_CACHE_HOME="$run_root/cache"
    DISPLAY="$host_display"
    "${xauthority_environment[@]}"
    PULSE_SERVER="unix:$pulse_socket"
    PYTHONDONTWRITEBYTECODE=1
    PROTONFIXES_DISABLE=1
    PROTON_USE_XALIA=0
    PROTON_LOG=1
    PROTON_LOG_DIR="$results_root"
    UMU_ID="$app_id"
    UMU_USE_STEAM=0
    STEAM_LINUX_RUNTIME_LOG=1
    STEAM_LINUX_RUNTIME_VERBOSE=1
    PRESSURE_VESSEL_VARIABLE_DIR="$runtime_variable_dir"
    PRESSURE_VESSEL_FILESYSTEMS_RO=/nix/store
    PRESSURE_VESSEL_FILESYSTEMS_RW="$run_root"
    PRESSURE_VESSEL_SHARE_HOME=0
    PRESSURE_VESSEL_HOME="$run_root/home"
    PRESSURE_VESSEL_SYSTEMD_SCOPE=0
    STEAM_LINUX_RUNTIME_LOG_DIR="$results_root/steam-runtime"
    SteamAppId="$app_id"
    SteamGameId="$app_id"
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$run_root/fake-steam"
    STEAM_COMPAT_DATA_PATH="$run_root/compatdata"
    STEAM_COMPAT_INSTALL_PATH="$stress_install_dir"
    STEAM_COMPAT_TOOL_PATHS="$proton_tool:$runtime_copy_dir"
    WINEDEBUG="$wine_debug"
)

# `runinprefix` intentionally skips Proton's managed-prefix refresh. Prime the
# fresh private prefix before starting scheduler pressure; this setup work is
# not part of the measured playback interval.
(
    cd "$run_root"
    exec env -i "${runtime_environment[@]}" PROTON_LOG=0 \
        "$timeout_bin" --signal=TERM --kill-after=10s 120s \
        "$steam_run_bin" "$runtime_entry" --verb=waitforexitandrun -- \
        "$python_bin" "$proton_tool/proton" getcompatpath "$driver_exe" \
        >/dev/null
) >"$results_root/driver-console.log" 2>&1

if [[ $scheduler_pressure_profile == scheduler-pressure-v1 ]]; then
    stage=scheduler-pressure
    "$python_bin" -I "$scheduler_pressure_helper" run \
        --ready "$scheduler_pressure_ready" \
        --raw-summary "$scheduler_pressure_raw" \
        >>"$results_root/driver-console.log" 2>&1 &
    scheduler_pressure_pid=$!
    child_pids+=("$scheduler_pressure_pid")
    for _ in $(seq 1 1000); do
        [[ -s "$scheduler_pressure_ready" ]] && break
        child_has_exited "$scheduler_pressure_pid" &&
            die 'scheduler-pressure workers exited before becoming ready'
        sleep 0.01
    done
    [[ -s "$scheduler_pressure_ready" ]] ||
        die 'scheduler-pressure workers did not become ready within ten seconds'
fi

driver_command=(
    /run/current-system/sw/bin/env -i "${runtime_environment[@]}"
    "$timeout_bin" --signal=TERM --kill-after=10s 300s
    "$steam_run_bin" "$runtime_entry" --verb=waitforexitandrun --
    "$python_bin" "$proton_tool/proton" runinprefix
    "$driver_exe_windows"
    --script "$scenario_windows"
    --output "$driver_result_windows"
    --driver-sha256 "$driver_exe_sha256"
    --scenario-sha256 "$scenario_sha256"
    "${monitor_argument[@]}"
)
if [[ $scheduler_pressure_profile == scheduler-pressure-v1 ]]; then
    scheduler_pressure_started_ns=$(
        "$python_bin" -I "$scheduler_pressure_helper" now
    ) || die 'could not timestamp scheduler-pressure driver start'
    driver_launch=(
        "$python_bin" -I "$scheduler_pressure_helper" exec --role application --
        "${driver_command[@]}"
    )
else
    driver_launch=("${driver_command[@]}")
fi

stage=driver
(
    cd "$run_root"
    exec "${driver_launch[@]}"
) >>"$results_root/driver-console.log" 2>&1 &
driver_pid=$!
child_pids+=("$driver_pid")
set +e
wait "$driver_pid"
process_exit=$?
set -e
forget_child_pid "$driver_pid"
printf '%s\n' "$process_exit" >"$producer_done.tmp"
mv -- "$producer_done.tmp" "$producer_done"
if [[ $scheduler_pressure_profile == scheduler-pressure-v1 ]]; then
    scheduler_pressure_exited_ns=$(
        "$python_bin" -I "$scheduler_pressure_helper" now
    ) || die 'could not timestamp scheduler-pressure driver exit'
    child_has_exited "$scheduler_pressure_pid" &&
        die 'scheduler-pressure workers exited before driver completion'
    kill -TERM "$scheduler_pressure_pid" 2>/dev/null ||
        die 'could not stop scheduler-pressure workers'
    if ! wait_child_bounded "$scheduler_pressure_pid" 700 0.01; then
        outcome=harness-failed
        stage=scheduler-pressure
        die 'scheduler-pressure workers did not stop within seven seconds'
    fi
    set +e
    wait "$scheduler_pressure_pid"
    scheduler_pressure_exit=$?
    set -e
    forget_child_pid "$scheduler_pressure_pid"
    scheduler_pressure_pid=
    if ((scheduler_pressure_exit != 0)); then
        outcome=harness-failed
        stage=scheduler-pressure
        die "scheduler-pressure workers exited $scheduler_pressure_exit"
    fi
    if ! "$python_bin" -I "$scheduler_pressure_helper" seal \
        --raw-summary "$scheduler_pressure_raw" \
        --output "$results_root/scheduler-pressure-summary.json" \
        --driver-started-ns "$scheduler_pressure_started_ns" \
        --driver-exited-ns "$scheduler_pressure_exited_ns"; then
        outcome=harness-failed
        stage=scheduler-pressure
        die 'scheduler-pressure evidence did not meet its calibrated floor'
    fi
fi
if [[ $instrumentation == endpoint-monitor ]]; then
    if ! stop_endpoint_capture; then
        outcome=endpoint-audio-rejected
        die 'private endpoint recorder did not stop cleanly'
    fi
fi

stage=watcher
for _ in $(seq 1 1000); do
    kill -0 "$watcher_pid" 2>/dev/null || break
    sleep 0.01
done
if kill -0 "$watcher_pid" 2>/dev/null; then
    kill -TERM "$watcher_pid" 2>/dev/null || true
fi
set +e
wait "$watcher_pid"
watcher_exit=$?
set -e
forget_child_pid "$watcher_pid"

if ((process_exit != 0)); then
    outcome=driver-failed
    die "driver process exited $process_exit"
fi
if ((watcher_exit != 0)) || [[ ! -s "$results_root/transport-watch.json" ]]; then
    outcome=watcher-failed
    die 'transport watcher rejected the driver timeline'
fi

stage=parser
set +e
"$python_bin" -I "$parser" "$results_root/driver.jsonl" \
    --oracle "$selected_oracle" \
    --summary "$results_root/parser-summary.json" \
    >"$results_root/parser-console.log" 2>&1
parser_exit=$?
set -e
if [[ $case_role == negative-control ]]; then
    if ((parser_exit != 1)); then
        outcome=oracle-rejected
        die 'negative control did not produce its declared oracle rejection'
    fi
    selected_outcome=rejected-as-expected
else
    if ((parser_exit != 0)); then
        outcome=oracle-rejected
        die 'parser rejected the completed driver evidence'
    fi
    selected_outcome=accepted
fi
[[ -s "$results_root/parser-summary.json" ]] || die 'parser produced no summary'

if [[ $instrumentation == endpoint-monitor ]]; then
    stage=endpoint-audio
    set +e
    "$python_bin" -I "$endpoint_audio_oracle" score \
        --driver-json "$results_root/driver.jsonl" \
        --pcm "$results_root/endpoint-audio.raw" \
        --capture-started-ns "$endpoint_capture_started_ns" \
        --capture-anchor-frame "$endpoint_capture_anchor_frame" \
        --capture-anchor-ns "$endpoint_capture_anchor_ns" \
        --capture-finished-ns "$endpoint_capture_finished_ns" \
        "${endpoint_checkpoint_arguments[@]}" \
        --output "$results_root/endpoint-audio-summary.json" \
        >"$results_root/endpoint-audio-score.log" 2>&1
    endpoint_audio_exit=$?
    set -e
    if ((endpoint_audio_exit != 0)); then
        outcome=endpoint-audio-rejected
        die 'endpoint-audio oracle rejected private sink continuity'
    fi
fi

stage=transport
graceful_stop_fixture_service "$service_pid" "$service_completion" || {
    outcome=transport-rejected
    die 'fixture service did not seal its request evidence'
}
set +e
"$python_bin" -B -E -s "$selected_transport_oracle" score \
    --http-log "$results_root/http-requests.jsonl" \
    --service-completion "$service_completion" \
    --watch "$results_root/transport-watch.json" \
    --config "$service_config" \
    --output "$results_root/transport-summary.json" \
    >"$results_root/transport-score.log" 2>&1
transport_exit=$?
set -e
if ((transport_exit != 0)); then
    outcome=transport-rejected
    die 'transport oracle rejected fixture/MediaEngine correlation'
fi

outcome=$selected_outcome
stage=complete
printf 'host MediaEngine run completed; teardown will retain only evidence.\n'
