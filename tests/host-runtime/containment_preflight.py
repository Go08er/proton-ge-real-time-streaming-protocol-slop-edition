#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Validate the native host runner's outer Bubblewrap boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import sys


def fail(message: str) -> None:
    raise SystemExit(f"contained host preflight: {message}")


def beneath(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def main() -> None:
    display, manifest_name = sys.argv[1:]
    display_number = display[1:].split(".", 1)[0]
    selected_socket = Path(f"/tmp/.X11-unix/X{display_number}")

    interfaces = socket.if_nameindex()
    if interfaces != [(1, "lo")]:
        fail(f"network interfaces are not exactly loopback: {interfaces!r}")

    route_lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
    if len(route_lines) != 1:
        fail("IPv4 route table is not empty")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(1.0)
    client.connect(listener.getsockname())
    peer, _ = listener.accept()
    client.sendall(b"loopback")
    if peer.recv(8) != b"loopback":
        fail("loopback roundtrip changed payload")
    peer.close()
    client.close()
    listener.close()

    allowed_rw = (
        "/proc",
        "/dev",
        "/tmp",
        "/home",
        "/root",
        "/run",
        "/var/tmp",
        "/srv",
        "/mnt",
        "/media",
    )
    root_seen = False
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 7:
            fail("malformed mountinfo record")
        mountpoint = fields[4].replace("\\040", " ")
        options = set(fields[5].split(","))
        if mountpoint == "/":
            root_seen = True
            if "ro" not in options:
                fail("outer root mount is writable")
        if any(beneath(mountpoint, prefix) for prefix in allowed_rw):
            continue
        if "ro" not in options:
            fail(f"unexpected writable mount outside private prefixes: {mountpoint}")
    if not root_seen:
        fail("root mount was not found")

    for masked in (Path("/home"), Path("/mnt"), Path("/media")):
        if masked.exists() and any(masked.iterdir()):
            fail(f"masked {masked} is not empty")

    expected_run_links = {"current-system", "opengl-driver", "opengl-driver-32"}
    run_entries = {entry.name for entry in Path("/run").iterdir()}
    if run_entries != expected_run_links:
        fail(f"private /run has unexpected entries: {sorted(run_entries)!r}")
    for name in sorted(expected_run_links):
        link = Path("/run") / name
        if not link.is_symlink() or not os.readlink(link).startswith("/nix/store/"):
            fail(f"private /run link is not an exact Nix-store target: {name}")
    for root, directories, filenames in os.walk("/run", followlinks=False):
        for name in directories + filenames:
            candidate = Path(root) / name
            if stat.S_ISSOCK(candidate.lstat().st_mode):
                fail(f"host service socket survived private /run: {candidate}")

    for forbidden in (
        Path("/dev/input"),
        Path("/dev/snd"),
        Path("/dev/kfd"),
        Path("/run/dbus/system_bus_socket"),
    ):
        if forbidden.exists():
            fail(f"forbidden host capability is exposed: {forbidden}")

    for parent in (Path("/var/tmp"), Path("/tmp")):
        probe = parent / ".rtsp-contained-write-probe"
        probe.write_bytes(b"private")
        probe.unlink()
    for protected in (
        Path("/etc/.rtsp-write-probe"),
        Path("/srv/harness/.rtsp-write-probe"),
        Path("/nix/store/.rtsp-write-probe"),
    ):
        try:
            protected.write_bytes(b"forbidden")
        except OSError:
            pass
        else:
            protected.unlink(missing_ok=True)
            fail(f"protected host path was writable: {protected}")

    entries = list(Path("/tmp/.X11-unix").iterdir())
    if entries != [selected_socket]:
        fail(f"X11 exposure is not exactly the selected socket: {entries!r}")
    socket_info = selected_socket.lstat()
    if selected_socket.is_symlink() or not stat.S_ISSOCK(socket_info.st_mode):
        fail("selected X11 endpoint is not a direct socket")

    graphics = []
    dri = Path("/dev/dri")
    if not dri.is_dir():
        fail("no DRI graphics directory is exposed")
    for node in sorted(dri.iterdir()):
        info = node.lstat()
        if node.is_symlink() or not stat.S_ISCHR(info.st_mode):
            fail(f"non-character DRI entry is exposed: {node}")
        if not (node.name.startswith("card") or node.name.startswith("renderD")):
            fail(f"unexpected DRI entry is exposed: {node}")
        graphics.append(
            {
                "major": os.major(info.st_rdev),
                "minor": os.minor(info.st_rdev),
                "name": f"dri/{node.name}",
            }
        )
    if not any(record["name"].startswith("dri/renderD") for record in graphics):
        fail("no DRI render node is exposed")

    allowed_nvidia = {
        "nvidia0",
        "nvidia1",
        "nvidia2",
        "nvidia3",
        "nvidiactl",
        "nvidia-modeset",
        "nvidia-uvm",
        "nvidia-uvm-tools",
    }
    nvidia = []
    for node in sorted(Path("/dev").glob("nvidia*")):
        if node.name == "nvidia-caps" and node.is_dir():
            for cap in sorted(node.iterdir()):
                cap_info = cap.lstat()
                if cap.is_symlink() or not stat.S_ISCHR(cap_info.st_mode):
                    fail(f"invalid NVIDIA capability node: {cap}")
                nvidia.append(
                    {
                        "major": os.major(cap_info.st_rdev),
                        "minor": os.minor(cap_info.st_rdev),
                        "name": f"nvidia-caps/{cap.name}",
                    }
                )
            continue
        node_info = node.lstat()
        if (
            node.name not in allowed_nvidia
            or node.is_symlink()
            or not stat.S_ISCHR(node_info.st_mode)
        ):
            fail(f"unexpected NVIDIA device is exposed: {node}")
        nvidia.append(
            {
                "major": os.major(node_info.st_rdev),
                "minor": os.minor(node_info.st_rdev),
                "name": node.name,
            }
        )

    for variable in ("DBUS_SESSION_BUS_ADDRESS", "PIPEWIRE_REMOTE", "PULSE_SERVER"):
        if os.environ.get(variable):
            fail(f"ambient host endpoint survived clearenv: {variable}")

    if os.uname().nodename != "rtsp-media-test":
        fail("private UTS hostname differs")

    manifest = {
        "boundary": "outer-bwrap-v1",
        "devices": {
            "audio": "private-pulseaudio-only",
            "graphics": {"dri": graphics, "nvidia": nvidia},
        },
        "filesystem": {
            "hostRoot": "read-only",
            "maskedCommonGameRoots": ["/home", "/media", "/mnt", "/run"],
            "mutable": "private-mounts-only",
            "retainedRoot": "/var/tmp",
        },
        "namespaces": {
            "ipc": os.readlink("/proc/self/ns/ipc"),
            "mount": os.readlink("/proc/self/ns/mnt"),
            "network": os.readlink("/proc/self/ns/net"),
            "pid": os.readlink("/proc/self/ns/pid"),
            "user": os.readlink("/proc/self/ns/user"),
            "uts": os.readlink("/proc/self/ns/uts"),
        },
        "network": "loopback-only-no-routes",
        "schema": 1,
        "x11": "single-selected-socket-capability",
    }
    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = Path(manifest_name)
    path.write_bytes(payload)
    path.chmod(0o600)


if __name__ == "__main__":
    main()
