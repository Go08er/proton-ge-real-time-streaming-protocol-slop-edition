#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die()
{
    printf 'media stress static check: %s\n' "$*" >&2
    exit 2
}

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
run_loopback=0
nixpkgs_path=

while (($#)); do
    case $1 in
        --loopback)
            run_loopback=1
            ;;
        --nixpkgs)
            shift
            (($#)) || die '--nixpkgs requires an immutable store path'
            nixpkgs_path=$1
            [[ "$nixpkgs_path" == /nix/store/* && -f "$nixpkgs_path/default.nix" ]] ||
                die '--nixpkgs must name an immutable Nixpkgs source'
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
    shift
done

export PYTHONDONTWRITEBYTECODE=1
export PYTHONWARNINGS=error

(
    cd "$script_dir"
    python3 validate_manifest.py manifest.json
    python3 -m unittest -v test_manifest.py
)
(
    cd "$script_dir/driver"
    python3 -m unittest -v test_parse_results.py test_driver_source.py
    bash -n build.sh
)
(
    cd "$script_dir/fixtures"
    ./check.sh
)
(
    cd "$script_dir/proton-input"
    ./check.sh
)
(
    cd "$script_dir/runtime-input"
    ./check.sh
)
(
    cd "$repo_root/tests/http-stream-fixture"
    python3 -m unittest -v test_http_stream_fixture.py
)
(
    cd "$script_dir/live-fixture"
    python3 -m unittest -v \
        test_live_fixture.FixtureContractTests \
        test_live_fixture.PublisherAndLoggerTests \
        test_rtsp_contract.RtspContractTests
    python3 check_rtsp_contract.py
)
(
    cd "$repo_root/tests/nix-vm"
    python3 -m unittest -v \
        test_result_contract.py \
        test_failure_diagnostic.py \
        test_runtime_writable_state.py \
        test_store_input_digest.py \
        test_stress_fixture_service.py \
        test_transport_oracle.py
    bash -n \
        check-eval.sh \
        check-fault-eval.sh \
        fault-profile.sh \
        guest-smoke.sh \
        run-proton-probe.sh \
        run-stress-driver.sh
)
(
    cd "$repo_root/tests/host-runtime"
    python3 -m unittest -v \
        test_live_hls_case.LiveHlsCaseContractTests \
        test_rtsp_live_case.RTSPCaseContractTests \
        test_rtsp_live_case.RTSPCaseRuntimeOracleTests \
        test_endpoint_audio_oracle.py \
        test_host_run_contract.py \
        test_host_runner_source.py \
        test_scheduler_pressure.py
    bash -n run-contained-host-stress.sh run-host-stress.sh
)
(
    cd "$repo_root/tests/network-terminal-error"
    python3 -m unittest -v test_model.py test_patch_contract.py
)
(
    cd "$repo_root/tests/rtsp-drain-diagnostics"
    python3 -m unittest -v test_model.py test_patch_contract.py
)

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck "$0"
    shellcheck "$repo_root/tests/host-runtime/run-contained-host-stress.sh"
    shellcheck "$repo_root/tests/host-runtime/run-host-stress.sh"
fi

if ((run_loopback)); then
    (
        cd "$repo_root/tests/http-stream-fixture"
        python3 -m unittest -v smoke_test.py
    )
    (
        cd "$script_dir/live-fixture"
        python3 -m unittest -v test_live_fixture.LoopbackIntegrationTests
    )
    (
        cd "$repo_root/tests/host-runtime"
        python3 -m unittest -v test_live_hls_case.LoopbackLiveHlsCaseTests
        python3 -m unittest -v test_rtsp_live_case.LoopbackRTSPCaseTests
    )
fi

if [[ -n "$nixpkgs_path" ]]; then
    "$repo_root/tests/nix-vm/check-eval.sh" "$nixpkgs_path"
    "$repo_root/tests/nix-vm/check-fault-eval.sh" "$nixpkgs_path"
    "$script_dir/fixtures/check-eval.sh" "$nixpkgs_path" smoke
fi

printf 'media stress static checks passed (loopback=%d, nix-eval=%s)\n' \
    "$run_loopback" "$([[ -n "$nixpkgs_path" ]] && printf yes || printf no)"
