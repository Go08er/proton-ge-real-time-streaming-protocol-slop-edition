#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die()
{
    printf 'nix-vm fault-lab eval check: %s\n' "$*" >&2
    exit 2
}

[[ $# -eq 1 ]] || die 'usage: check-fault-eval.sh /nix/store/<pinned-nixpkgs-source>'
nixpkgs_path=$1
[[ "$nixpkgs_path" == /nix/store/* ]] || die 'nixpkgs must be an immutable /nix/store path'
[[ -f "$nixpkgs_path/default.nix" ]] || die 'nixpkgs default.nix is missing'

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
nix-instantiate --parse "$script_dir/fault-lab.nix" >/dev/null
bash -n "$script_dir/fault-profile.sh"
(
    cd "$script_dir"
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q \
        test_result_contract.py \
        test_store_input_digest.py \
        test_stress_fixture_service.py \
        test_transport_oracle.py
)

grep -Fq 'prio bands 2' "$script_dir/fault-profile.sh" ||
    die 'fault shaper does not separate target and control bands'
# This checks the literal runtime shell expression.
# shellcheck disable=SC2016
grep -Fq 'match ip dst "$target/32" flowid 1:2' "$script_dir/fault-profile.sh" ||
    die 'fault shaper is not destination-specific'

eval_attr()
{
    NIX_PATH='' nix-instantiate --eval --strict "$script_dir/fault-lab.nix" \
        --argstr nixpkgsPath "$nixpkgs_path" -A "$1"
}

[[ $(eval_attr config.name) == '"rtsp-media-private-fault-lab"' ]] ||
    die 'unexpected test name'
[[ $(eval_attr config.driverConfiguration.vlans) == '[ 1 ]' ]] ||
    die 'the test driver does not own exactly one private VLAN'

for node in origin clientA clientB; do
    [[ $(eval_attr "config.nodes.$node.virtualisation.vlans") == '[ 1 ]' ]] ||
        die "$node is not attached only to the private test VLAN"
    [[ $(eval_attr "config.nodes.$node.virtualisation.restrictNetwork") == true ]] ||
        die "$node network restriction is disabled"
    [[ $(eval_attr "config.nodes.$node.virtualisation.forwardPorts") == '[ ]' ]] ||
        die "$node forwards a host or guest port"
    [[ $(eval_attr "config.nodes.$node.virtualisation.sharedDirectories") == '{ }' ]] ||
        die "$node contains a host directory share"
    [[ $(eval_attr "config.nodes.$node.virtualisation.mountHostNixStore") == false ]] ||
        die "$node would mount the host Nix store"
    [[ $(eval_attr "config.nodes.$node.virtualisation.writableStore") == false ]] ||
        die "$node would have a writable Nix store"
    [[ $(eval_attr "config.nodes.$node.virtualisation.diskImage") == null ]] ||
        die "$node would have a persistent writable root"
    [[ $(eval_attr "config.nodes.$node.virtualisation.useNixStoreImage") == true ]] ||
        die "$node closure is not isolated in its own store image"
    [[ $(eval_attr "config.nodes.$node.virtualisation.useHostCerts") == false ]] ||
        die "$node would import host trust material"
    [[ $(eval_attr "config.nodes.$node.networking.defaultGateway") == null ]] ||
        die "$node unexpectedly has a default IPv4 gateway"
done

[[ $(eval_attr config.nodes.origin.networking.interfaces.eth1.ipv4.addresses.0.address) == '"192.0.2.10"' ]] ||
    die 'origin address differs from the fixed TEST-NET-1 topology'
[[ $(eval_attr config.nodes.clientA.networking.interfaces.eth1.ipv4.addresses.0.address) == '"192.0.2.20"' ]] ||
    die 'client A address differs from the shaping target'
[[ $(eval_attr config.nodes.clientB.networking.interfaces.eth1.ipv4.addresses.0.address) == '"192.0.2.21"' ]] ||
    die 'client B address differs from the clean control'
[[ $(eval_attr 'config.nodes.origin.environment.etc."rtsp-media-fault-lab/target-ipv4".text') == '"192.0.2.20\n"' ]] ||
    die 'origin shaping sentinel differs from client A'

[[ $(eval_attr config.nodes.origin.virtualisation.memorySize) == 1024 ]] ||
    die 'unexpected origin memory allocation'
[[ $(eval_attr config.nodes.clientA.virtualisation.memorySize) == 1536 ]] ||
    die 'unexpected client memory allocation'
[[ $(eval_attr config.nodes.clientB.virtualisation.memorySize) == 1536 ]] ||
    die 'unexpected client memory allocation'

test_script=$(eval_attr config.testScript)
grep -Fq 'clientB.succeed' <<<"$test_script" ||
    die 'fault lab does not verify the clean client during target impairment'
grep -Fq 'fault-summary.json' <<<"$test_script" ||
    die 'fault lab does not export a strict summary'
grep -Fq 'SHA256SUMS' <<<"$test_script" ||
    die 'fault lab does not hash its exported summary'
if grep -Fq 'copy_from_machine' <<<"$test_script" || grep -Fq 'copy_from_vm' <<<"$test_script"; then
    die 'fault test would require a forbidden host/guest shared directory'
fi

printf 'Private-VLAN fault-lab definition parsed and isolation options evaluated; no build or VM was started.\n'
