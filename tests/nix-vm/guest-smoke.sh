#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 077

die()
{
    printf 'rtsp-media-lab smoke: %s\n' "$*" >&2
    exit 1
}

[[ $(id -u) -ne 0 ]] || die 'the smoke gate must run as the unprivileged media-lab user'

: "${RTSP_LAB_ASSET_ROOT:?missing RTSP_LAB_ASSET_ROOT}"
: "${RTSP_LAB_STATE_ROOT:?missing RTSP_LAB_STATE_ROOT}"
: "${RTSP_LAB_RESULT_CONTRACT:?missing RTSP_LAB_RESULT_CONTRACT}"

[[ "$RTSP_LAB_ASSET_ROOT" == /nix/store/*/share ]] ||
    die 'assets must come from an immutable Nix store path'
[[ "$RTSP_LAB_STATE_ROOT" == /var/lib/rtsp-media-lab ]] ||
    die 'guest state escaped the declared ephemeral root'
[[ "$RTSP_LAB_RESULT_CONTRACT" == /nix/store/*/bin/result-contract ]] ||
    die 'result contract must come from an immutable Nix store path'
[[ -d "$RTSP_LAB_STATE_ROOT/results" ]] || die 'guest result directory is missing'
[[ -w "$RTSP_LAB_STATE_ROOT/results" ]] || die 'guest result directory is not writable'
[[ ! -w /nix/store ]] || die 'the guest Nix store is unexpectedly writable'

if findmnt -rn -t 9p,virtiofs | grep -q .; then
    die 'a host filesystem share is mounted in the guest'
fi

mapfile -t network_devices < <(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1)
[[ ${#network_devices[@]} -eq 1 && ${network_devices[0]} == lo ]] ||
    die "expected only loopback, found: ${network_devices[*]}"
[[ -z $(ip route show) ]] || die 'the isolated guest has an IPv4 route'
[[ -z $(ip -6 route show default) ]] || die 'the isolated guest has an IPv6 default route'

mapfile -t home_entries < <(find /home -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
[[ ${#home_entries[@]} -eq 1 && ${home_entries[0]} == media-lab ]] ||
    die "unexpected guest home directories: ${home_entries[*]}"
[[ ! -e /var/lib/media-lab/.local/share/Steam ]] || die 'a Steam tree exists in the guest home'
[[ ! -e /var/lib/media-lab/.steam ]] || die 'a Steam compatibility path exists in the guest home'

config_root="$RTSP_LAB_ASSET_ROOT/media-config"
for config in mediamtx.yml mediamtx-hls-live.yml; do
    config_path="$config_root/$config"
    [[ -r "$config_path" ]] || die "missing read-only media configuration: $config"
    grep -Fxq 'rtspAddress: 127.0.0.1:8554' "$config_path" ||
        die "$config is not restricted to RTSP loopback"
    if grep -Eq '(^|[[:space:]])(0\.0\.0\.0|::)(:|$)' "$config_path"; then
        die "$config contains a wildcard listener"
    fi
done
grep -Fxq 'hlsAddress: 127.0.0.1:8888' "$config_root/mediamtx-hls-live.yml" ||
    die 'the HLS fixture is not restricted to loopback'

fixture_tests="$RTSP_LAB_ASSET_ROOT/http-stream-fixture"
(
    cd "$fixture_tests"
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q \
        test_http_stream_fixture.py smoke_test.py
) || die 'deterministic HTTP fixture unit tests failed inside the guest'

python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
s.listen(1)
host, port = s.getsockname()
assert host == "127.0.0.1" and port > 0
s.close()
' || die 'loopback socket smoke failed'

python3 "$RTSP_LAB_RESULT_CONTRACT" write-smoke \
    --output "$RTSP_LAB_STATE_ROOT/results/smoke-summary.json" ||
    die 'could not write the strict smoke result'
python3 "$RTSP_LAB_RESULT_CONTRACT" validate \
    --kind smoke \
    --path "$RTSP_LAB_STATE_ROOT/results/smoke-summary.json" ||
    die 'strict smoke result validation failed'

printf 'passed\n' >"$RTSP_LAB_STATE_ROOT/results/smoke.ok"
printf 'RTSP media lab smoke passed in an isolated ephemeral guest.\n'
