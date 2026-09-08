#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die()
{
    printf 'rtsp-media fault profile: %s\n' "$*" >&2
    exit 2
}

[[ $(id -u) -eq 0 ]] || die 'network shaping must run as root inside the fixture VM'
[[ $# -eq 2 ]] ||
    die 'usage: fault-profile.sh clean|delay|jitter|loss|duplicate|reorder|rate|blackhole TARGET_IPV4'
[[ -r /etc/rtsp-media-fault-lab/target-ipv4 ]] ||
    die 'private fault-lab VM sentinel is absent'

profile=$1
target=$2
IFS= read -r declared_target </etc/rtsp-media-fault-lab/target-ipv4 ||
    die 'could not read the private fault-lab target'
[[ $target == "$declared_target" ]] || die 'target differs from the VM declaration'

# This helper is intentionally limited to the RFC 5737 TEST-NET-1 subnet used
# by fault-lab.nix.  It must never become a convenient host-network shaper.
[[ $target =~ ^192\.0\.2\.([1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$ ]] ||
    die 'TARGET_IPV4 must be a unicast address in 192.0.2.0/24'

case "$profile" in
    clean|delay|jitter|loss|duplicate|reorder|rate|blackhole) ;;
    *) die "unknown profile: $profile" ;;
esac

mapfile -t interfaces < <(find /sys/class/net -mindepth 1 -maxdepth 1 -printf '%f\n' | grep -v '^lo$' | sort)
[[ ${#interfaces[@]} -eq 1 ]] || die "expected one private-VLAN interface, found: ${interfaces[*]:-none}"
interface=${interfaces[0]}

# Delete first so every profile has one exact owner and repeated use cannot
# stack qdiscs. A missing root qdisc is the expected clean starting state.
tc qdisc del dev "$interface" root 2>/dev/null || true

if [[ $profile != clean ]]; then
    # Untargeted traffic uses the first prio band and has no child qdisc.
    # Only packets whose destination is the selected client enter band 1:2.
    # This lets client B remain a real clean control while client A is impaired
    # by origin egress faults on the same fixture and private VLAN.
    tc qdisc add dev "$interface" root handle 1: prio bands 2 \
        priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
fi

case "$profile" in
    clean)
        ;;
    delay)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem delay 250ms 25ms distribution normal seed 438100
        ;;
    jitter)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem delay 120ms 80ms 25% distribution normal seed 438101
        ;;
    loss)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem loss 10% 25% seed 438102
        ;;
    duplicate)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem duplicate 5% seed 438103
        ;;
    reorder)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem delay 40ms reorder 25% 50% seed 438104
        ;;
    rate)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            tbf rate 512kbit burst 32kbit latency 400ms
        ;;
    blackhole)
        tc qdisc add dev "$interface" parent 1:2 handle 20: \
            netem loss 100% seed 438105
        ;;
esac

if [[ $profile != clean ]]; then
    tc filter add dev "$interface" protocol ip parent 1: prio 1 u32 \
        match ip dst "$target/32" flowid 1:2
fi

printf '%s %s\n' "$profile" "$target" >/run/rtsp-media-fault-profile
tc qdisc show dev "$interface"
tc filter show dev "$interface" parent 1: 2>/dev/null || true
