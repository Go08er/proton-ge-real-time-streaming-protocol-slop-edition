#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die()
{
    printf 'nix-vm eval check: %s\n' "$*" >&2
    exit 2
}

[[ $# -eq 1 ]] || die 'usage: check-eval.sh /nix/store/<pinned-nixpkgs-source>'
nixpkgs_path=$1
[[ "$nixpkgs_path" == /nix/store/* ]] || die 'nixpkgs must be an immutable /nix/store path'
[[ -f "$nixpkgs_path/default.nix" ]] || die 'nixpkgs default.nix is missing'

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)

nix-instantiate --parse "$script_dir/default.nix" >/dev/null
nix-instantiate --parse "$script_dir/stress-case.nix" >/dev/null
bash -n \
    "$script_dir/guest-smoke.sh" \
    "$script_dir/run-proton-probe.sh" \
    "$script_dir/run-stress-driver.sh"
(
    cd "$script_dir"
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q \
        test_result_contract.py \
        test_failure_diagnostic.py \
        test_runtime_writable_state.py \
        test_store_input_digest.py \
        test_stress_fixture_service.py \
        test_transport_oracle.py
)

eval_attr()
{
    NIX_PATH='' nix-instantiate --eval --strict "$script_dir/default.nix" \
        --argstr nixpkgsPath "$nixpkgs_path" -A "$1"
}

[[ $(eval_attr config.name) == '"rtsp-media-lab-smoke"' ]] ||
    die 'unexpected test name'
[[ $(eval_attr config.nodes.machine.virtualisation.memorySize) == 2048 ]] ||
    die 'unexpected default memory cap'
[[ $(eval_attr config.nodes.machine.virtualisation.cores) == 2 ]] ||
    die 'unexpected default vCPU cap'
[[ $(eval_attr config.nodes.machine.virtualisation.vlans) == '[ ]' ]] ||
    die 'the guest unexpectedly has a test VLAN'
[[ $(eval_attr config.nodes.machine.virtualisation.restrictNetwork) == true ]] ||
    die 'guest network restriction is disabled'
[[ $(eval_attr config.nodes.machine.virtualisation.forwardPorts) == '[ ]' ]] ||
    die 'the VM definition forwards a host or guest port'
[[ $(eval_attr config.nodes.machine.virtualisation.diskImage) == null ]] ||
    die 'the writable guest root is not ephemeral tmpfs'
[[ $(eval_attr config.nodes.machine.virtualisation.useNixStoreImage) == true ]] ||
    die 'the guest closure is not isolated in its own store image'
[[ $(eval_attr config.nodes.machine.virtualisation.mountHostNixStore) == false ]] ||
    die 'the host Nix store would be mounted'
[[ $(eval_attr config.nodes.machine.virtualisation.writableStore) == false ]] ||
    die 'the guest Nix store would be writable'
[[ $(eval_attr config.nodes.machine.nix.enable) == false ]] ||
    die 'the isolated guest unexpectedly enables Nix or its store-registration writer'
address_families=$(eval_attr \
    config.nodes.machine.systemd.services.rtsp-media-lab-smoke.serviceConfig.RestrictAddressFamilies)
grep -Fq '"AF_NETLINK"' <<<"$address_families" ||
    die 'the smoke service cannot inspect the guest network namespace'
[[ $(eval_attr config.nodes.machine.virtualisation.sharedDirectories) == '{ }' ]] ||
    die 'the VM definition contains a host directory share'
[[ $(eval_attr config.nodes.machine.virtualisation.useHostCerts) == false ]] ||
    die 'the VM would import host trust material'
[[ $(eval_attr config.driverConfiguration.vlans) == '[ ]' ]] ||
    die 'the test driver unexpectedly created a VLAN'
[[ $(eval_attr config.nodes.machine.virtualisation.qemu.networkingOptions) == '[ "-nic none" ]' ]] ||
    die 'QEMU would create an implicit network interface'
qemu_options=$(eval_attr config.nodes.machine.virtualisation.qemu.options)
if grep -Eq -- '(-net|-nic|virtio-net)' <<<"$qemu_options"; then
    die 'the evaluated QEMU command contains a network-device option'
fi

test_script=$(eval_attr config.testScript)
grep -Fq 'smoke-summary.json' <<<"$test_script" ||
    die 'the test driver does not export the strict smoke summary'
grep -Fq 'SHA256SUMS' <<<"$test_script" ||
    die 'the test driver does not hash exported summaries'
grep -Fq 'stress-control-summary.json' "$script_dir/default.nix" ||
    die 'the test driver does not declare the uninstrumented stress export'
grep -Fq 'stress-audio-monitor-summary.json' "$script_dir/default.nix" ||
    die 'the test driver does not declare the instrumented stress export'
if grep -Fq 'copy_from_machine' <<<"$test_script" || grep -Fq 'copy_from_vm' <<<"$test_script"; then
    die 'the test driver would require a forbidden host/guest shared directory'
fi

if NIX_PATH='' nix-instantiate --eval --strict "$script_dir/default.nix" \
    --argstr nixpkgsPath "$nixpkgs_path" \
    --argstr stressProtonToolPath "$nixpkgs_path" \
    -A config.name >/dev/null 2>&1; then
    die 'a partial stress-driver bundle was accepted'
fi

stress_bundle_args=(
    --argstr stressProtonToolPath "$nixpkgs_path"
    --argstr stressSteamRuntimePath "$nixpkgs_path"
    --argstr stressDriverExePath "$nixpkgs_path"
    --argstr stressScenarioPath "$nixpkgs_path"
    --argstr stressFixtureManifestPath "$nixpkgs_path"
    --argstr stressFixtureBytesPath "$nixpkgs_path"
    --argstr stressServiceConfigPath "$nixpkgs_path"
    --argstr stressControlOraclePath "$nixpkgs_path"
    --argstr stressInstrumentedOraclePath "$nixpkgs_path"
    --argstr stressParserPath "$nixpkgs_path"
)
if NIX_PATH='' nix-instantiate --eval --strict "$script_dir/default.nix" \
    --argstr nixpkgsPath "$nixpkgs_path" \
    "${stress_bundle_args[@]}" \
    --arg runStressDriver true --arg memoryMiB 8191 \
    -A config.name >/dev/null 2>&1; then
    die 'stress execution accepted less than 8192 MiB'
fi
stress_memory=$(NIX_PATH='' nix-instantiate --eval --strict "$script_dir/default.nix" \
    --argstr nixpkgsPath "$nixpkgs_path" \
    "${stress_bundle_args[@]}" \
    --arg runStressDriver true --arg memoryMiB 8192 \
    -A config.nodes.machine.virtualisation.memorySize)
[[ "$stress_memory" == 8192 ]] || die 'stress execution rejected its declared 8192 MiB budget'

printf 'Nix VM definition parsed and isolation/resource options evaluated; no build or VM was started.\n'
