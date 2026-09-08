#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 077

die()
{
    printf 'contained host MediaEngine runner: %s\n' "$*" >&2
    exit 2
}

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
inner_runner=/srv/harness/tests/host-runtime/run-host-stress.sh

inner_preflight()
{
    local python_bin='' display='' argument='' previous=''

    [[ ${RTSP_HOST_OUTER_ACTIVE:-} == outer-bwrap-v1 ]] ||
        die 'inner preflight is not inside the declared outer boundary'
    for argument in "$@"; do
        case $previous in
            --python-bin) python_bin=$argument ;;
            --display) display=$argument ;;
        esac
        previous=$argument
    done
    [[ $python_bin == /nix/store/* && -x $python_bin ]] ||
        die 'inner preflight requires the exact Nix-store Python argument'
    [[ $display =~ ^:([0-9]+)([.][0-9]+)?$ ]] ||
        die 'inner preflight requires a local X11 display'
    [[ $(readlink /proc/self/ns/mnt) != "${RTSP_HOST_MNTNS:-}" ]] ||
        die 'mount namespace was not isolated'
    [[ $(readlink /proc/self/ns/net) != "${RTSP_HOST_NETNS:-}" ]] ||
        die 'network namespace was not isolated'
    [[ $(readlink /proc/self/ns/pid) != "${RTSP_HOST_PIDNS:-}" ]] ||
        die 'PID namespace was not isolated'
    [[ $(readlink /proc/self/ns/user) != "${RTSP_HOST_USERNS:-}" ]] ||
        die 'user namespace was not isolated'
    [[ $(readlink /proc/self/ns/ipc) != "${RTSP_HOST_IPCNS:-}" ]] ||
        die 'IPC namespace was not isolated'
    [[ $(readlink /proc/self/ns/uts) != "${RTSP_HOST_UTSNS:-}" ]] ||
        die 'UTS namespace was not isolated'

    "$python_bin" -I "$script_dir/containment_preflight.py" \
        "$display" /var/tmp/outer-containment.json

    export RTSP_HOST_CONTAINMENT=outer-bwrap-v1
    export RTSP_HOST_CONTAINMENT_MANIFEST=/var/tmp/outer-containment.json
    if [[ ${RTSP_HOST_PREFLIGHT_ONLY:-0} == 1 ]]; then
        printf 'contained host MediaEngine runner: containment preflight passed\n' >&2
        exit 0
    fi
    exec "$inner_runner" "$@"
}

if [[ ${1:-} == --inner ]]; then
    shift
    inner_preflight "$@"
fi

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    cat <<'EOF'
Usage: run-contained-host-stress.sh [--bwrap-bin PATH] [--containment-preflight-only]
                                    RUNNER_OPTIONS...

Creates a read-only-filesystem, masked-home, loopback-only Bubblewrap boundary
around run-host-stress.sh. All ordinary runner options are forwarded.

--containment-preflight-only validates the outer boundary without starting
Proton. It requires only --display and --python-bin from the runner options.
EOF
    "$script_dir/run-host-stress.sh" --help
    exit 0
fi

[[ $(id -u) -ne 0 ]] || die 'refusing to run Proton as root'

bwrap_bin=
display=
xauthority=
preflight_only=0
runner_args=()
while (($#)); do
    case $1 in
        --bwrap-bin)
            (($# >= 2)) || die '--bwrap-bin requires a value'
            bwrap_bin=$2
            shift 2
            ;;
        --containment-preflight-only)
            preflight_only=1
            shift
            ;;
        --display)
            (($# >= 2)) || die '--display requires a value'
            display=$2
            runner_args+=("$1" "$2")
            shift 2
            ;;
        --xauthority)
            (($# >= 2)) || die '--xauthority requires a value'
            xauthority=$2
            runner_args+=("$1" __RTSP_OUTER_XAUTHORITY__)
            shift 2
            ;;
        *)
            runner_args+=("$1")
            shift
            ;;
    esac
done

[[ $display =~ ^:([0-9]+)([.][0-9]+)?$ ]] || die 'a local --display is required'
display_number=${BASH_REMATCH[1]}
x_socket=/tmp/.X11-unix/X$display_number
[[ -S $x_socket && ! -L $x_socket ]] || die "selected X11 socket is unavailable: $x_socket"

if [[ -z $bwrap_bin ]]; then
    bwrap_bin=$(command -v bwrap 2>/dev/null) || die 'bubblewrap is unavailable; pass --bwrap-bin'
fi
bwrap_bin=$(readlink -f -- "$bwrap_bin") || die 'bubblewrap could not be resolved'
[[ $bwrap_bin == /nix/store/* && -x $bwrap_bin ]] || die 'bubblewrap must resolve into /nix/store'

[[ -d /var/tmp && -w /var/tmp && ! -L /var/tmp ]] || die '/var/tmp is not a safe writable directory'
outer_root=$(mktemp -d --tmpdir=/var/tmp rtsp-media-outer.XXXXXXXX) || die 'could not create outer run root'
[[ $outer_root =~ ^/var/tmp/rtsp-media-outer[.][A-Za-z0-9]+$ && -d $outer_root && ! -L $outer_root ]] ||
    die 'outer run root is not canonical'
chmod 700 "$outer_root"
mkdir -m 700 "$outer_root/outer-home" "$outer_root/outer-xdg"

if [[ -n $xauthority ]]; then
    [[ $xauthority == /* && -f $xauthority && ! -L $xauthority && -r $xauthority ]] ||
        die 'Xauthority must be an absolute readable nonsymlink regular file'
    install -m 600 -- "$xauthority" "$outer_root/outer-xauthority"
    for index in "${!runner_args[@]}"; do
        [[ ${runner_args[index]} != __RTSP_OUTER_XAUTHORITY__ ]] ||
            runner_args[index]=/var/tmp/outer-xauthority
    done
fi

host_mntns=$(readlink /proc/self/ns/mnt)
host_netns=$(readlink /proc/self/ns/net)
host_pidns=$(readlink /proc/self/ns/pid)
host_userns=$(readlink /proc/self/ns/user)
host_ipcns=$(readlink /proc/self/ns/ipc)
host_utsns=$(readlink /proc/self/ns/uts)
user_name=$(id -un)

mask_arguments=()
for mount_root in /mnt /media; do
    [[ ! -d $mount_root ]] || mask_arguments+=(--tmpfs "$mount_root")
done

runtime_link_arguments=()
for link_name in current-system opengl-driver opengl-driver-32; do
    link_path=/run/$link_name
    [[ -L $link_path ]] || die "required NixOS runtime link is absent: $link_path"
    link_target=$(readlink -f -- "$link_path") ||
        die "required NixOS runtime link could not be resolved: $link_path"
    [[ $link_target == /nix/store/* && -d $link_target && ! -L $link_target ]] ||
        die "required NixOS runtime link has an unsafe target: $link_path"
    runtime_link_arguments+=(--symlink "$link_target" "$link_path")
done

device_arguments=(--dir /dev/dri)
dri_render_count=0
shopt -s nullglob
for node in /dev/dri/card[0-9]* /dev/dri/renderD[0-9]*; do
    if [[ -c $node ]]; then
        device_arguments+=(--dev-bind "$node" "$node")
        [[ $(basename -- "$node") != renderD* ]] || ((dri_render_count += 1))
    fi
done
((dri_render_count > 0)) || die 'no DRI render node is available'
for node in /dev/nvidia[0-9]* /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools; do
    [[ -c $node ]] && device_arguments+=(--dev-bind "$node" "$node")
done
if [[ -d /dev/nvidia-caps ]]; then
    device_arguments+=(--dir /dev/nvidia-caps)
    for node in /dev/nvidia-caps/*; do
        [[ -c $node ]] && device_arguments+=(--dev-bind "$node" "$node")
    done
fi
shopt -u nullglob

set +e
"$bwrap_bin" \
    --unshare-user --unshare-pid --unshare-net --unshare-ipc --unshare-uts \
    --die-with-parent --new-session --hostname rtsp-media-test \
    --ro-bind / / \
    --proc /proc \
    --dev /dev \
    --tmpfs /dev/shm \
    "${device_arguments[@]}" \
    --tmpfs /tmp \
    --dir /tmp/.X11-unix \
    --ro-bind "$x_socket" "$x_socket" \
    --tmpfs /home \
    --tmpfs /root \
    --tmpfs /run \
    "${runtime_link_arguments[@]}" \
    "${mask_arguments[@]}" \
    --tmpfs /srv \
    --dir /srv/harness \
    --ro-bind "$repo_root" /srv/harness \
    --bind "$outer_root" /var/tmp \
    --chdir /srv/harness \
    --clearenv \
    --setenv HOME /var/tmp/outer-home \
    --setenv USER "$user_name" \
    --setenv LOGNAME "$user_name" \
    --setenv LANG C.UTF-8 \
    --setenv PATH /run/current-system/sw/bin \
    --setenv TMPDIR /tmp \
    --setenv XDG_RUNTIME_DIR /var/tmp/outer-xdg \
    --setenv DISPLAY "$display" \
    --setenv RTSP_HOST_OUTER_ACTIVE outer-bwrap-v1 \
    --setenv RTSP_HOST_MNTNS "$host_mntns" \
    --setenv RTSP_HOST_NETNS "$host_netns" \
    --setenv RTSP_HOST_PIDNS "$host_pidns" \
    --setenv RTSP_HOST_USERNS "$host_userns" \
    --setenv RTSP_HOST_IPCNS "$host_ipcns" \
    --setenv RTSP_HOST_UTSNS "$host_utsns" \
    --setenv RTSP_HOST_PREFLIGHT_ONLY "$preflight_only" \
    -- /run/current-system/sw/bin/bash /srv/harness/tests/host-runtime/run-contained-host-stress.sh --inner "${runner_args[@]}"
status=$?
set -e

rm -rf -- "$outer_root/outer-home" "$outer_root/outer-xdg" "$outer_root/outer-xauthority"
mapfile -t retained_results < <(find "$outer_root" -mindepth 2 -maxdepth 2 -type d -path '*/rtsp-media-host.*/results' -print | LC_ALL=C sort)
if ((${#retained_results[@]})); then
    printf 'contained host MediaEngine runner: host-visible evidence:\n' >&2
    printf '  %s\n' "${retained_results[@]}" >&2
else
    printf 'contained host MediaEngine runner: no result directory; outer diagnostics: %s\n' "$outer_root" >&2
fi
exit "$status"
