#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Exercise forward or blackhole RTSP service/gate mode without Wine or Steam."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import types


ADAPTER_PATH = Path(__file__).resolve().parents[2] / "host-runtime/rtsp_live_case.py"
SPEC = importlib.util.spec_from_file_location("rtsp_live_case", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
rtsp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rtsp)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise rtsp.RTSPCaseError(message)


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--fixture-manifest", required=True, type=Path)
    parser.add_argument("--scenario", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config, _fixture, _mediamtx_config = rtsp.load_and_validate(args)
        require(Path("/tmp").is_dir() and not Path("/tmp").is_symlink(), "/tmp is not a regular directory")
        with tempfile.TemporaryDirectory(prefix="rtsp-live-native-", dir="/tmp") as directory:
            root = Path(directory)
            connection_log = root / "connections.jsonl"
            completion = root / "complete.json"
            console = root / "service-console.log"
            with console.open("wb") as output:
                service = subprocess.Popen([
                    sys.executable, os.fspath(ADAPTER_PATH), "serve",
                    "--config", os.fspath(args.config),
                    "--fixture-root", os.fspath(args.fixture_root),
                    "--fixture-manifest", os.fspath(args.fixture_manifest),
                    "--scenario", os.fspath(args.scenario),
                    "--log", os.fspath(connection_log),
                    "--completion-marker", os.fspath(completion),
                ], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    cwd=root,
                    env={"HOME": os.fspath(root), "LANG": "C.UTF-8", "PATH": "/run/current-system/sw/bin"})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and service.poll() is None:
                    if port_open(config["port"]):
                        break
                    time.sleep(0.05)
                else:
                    raise rtsp.RTSPCaseError("RTSP service did not expose its qualified public gate")

                url = f'rtsp://127.0.0.1:{config["port"]}/{config["streamPath"]}'
                probe_runs = (
                    1 if config["relayMode"] != "forward"
                    else config["minObservedConnections"]
                )
                for _ in range(probe_runs):
                    duration = (
                        "30" if config["relayMode"] == "server-to-client-blackhole"
                        else "8" if config["relayMode"] == "server-to-client-finite-hold"
                        else "1"
                    )
                    try:
                        probe = subprocess.run([
                            config["ffmpegBin"], "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
                            "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url,
                            "-map", "0:v:0", "-map", "0:a:0", "-t", duration, "-f", "null", "-",
                        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=20, check=False,
                            env={"HOME": os.fspath(root), "LANG": "C.UTF-8", "PATH": "/run/current-system/sw/bin"})
                    except subprocess.TimeoutExpired as error:
                        if config["relayMode"] != "server-to-client-blackhole":
                            raise rtsp.RTSPCaseError("public RTSP/TCP smoke decode timed out") from error
                        probe = subprocess.CompletedProcess(error.cmd, 124, b"", error.stderr or b"")
                    if config["relayMode"] == "server-to-client-blackhole":
                        require(probe.returncode == 124 and len(probe.stderr) <= 1024 * 1024,
                                "public RTSP/TCP blackhole did not stall the native reader")
                    else:
                        require(probe.returncode == 0 and len(probe.stderr) <= 1024 * 1024,
                                "public RTSP/TCP smoke decode failed")

                service.terminate()
                try:
                    service_exit = service.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=3)
                    raise rtsp.RTSPCaseError("RTSP service did not stop within its deadline")
            if service_exit != 0:
                diagnostics = console.read_bytes()[-4096:].decode("utf-8", errors="replace").splitlines()
                detail = diagnostics[-1] if diagnostics else "no bounded service diagnostic"
                raise rtsp.RTSPCaseError(f"RTSP service returned a failure: {detail}")
            require(completion.is_file() and connection_log.is_file(), "RTSP service did not seal evidence")
            _, records = rtsp.load_connection_log(connection_log, config)
            carrying = [record for record in records
                        if record["client_to_server_bytes"] >= 128
                        and record["server_to_client_bytes"] >= 4096
                        and record["interleaved_tcp_setups"] >= 2]
            if config["relayMode"] == "server-to-client-blackhole":
                blackholed = [
                    record for record in records
                    if record["blackhole_started_monotonic_ns"] is not None
                ]
                require(len(blackholed) == 1, "one native public-gate blackhole was not observed")
                require(blackholed[0]["server_to_client_bytes"]
                        == config["blackholeAfterServerBytes"],
                        "native public-gate blackhole cutoff differs")
                require(blackholed[0]["terminal_reason"] in {"client-eof", "client-reset"},
                        "native blackhole reader did not terminate from the client side")
            elif config["relayMode"] == "server-to-client-finite-hold":
                held = [
                    record for record in records
                    if record["blackhole_started_monotonic_ns"] is not None
                ]
                require(len(held) == 1, "one native public-gate finite hold was not observed")
                require(held[0]["server_to_client_bytes"]
                        > config["blackholeAfterServerBytes"]
                        and held[0]["hold_resumed_monotonic_ns"] is not None,
                        "native public-gate delivery did not resume after its finite hold")
            else:
                require(len(carrying) >= config["minObservedConnections"],
                        "the configured minimum native public-gate A/V sessions was not observed")
            sealed = rtsp.read_json(completion, 16 * 1024)
            require(sealed["acceptedConnectionCount"] == sealed["terminalRecordCount"] == len(records),
                    "native RTSP completion did not terminate every accepted connection")
            require(sealed["rejectedConnectionCount"] == 0, "native RTSP gate rejected a connection")
            require(not port_open(config["port"]) and not port_open(config["backendPort"]),
                    "RTSP listener survived native smoke teardown")
            print(json.dumps({
                "acceptedConnections": len(records),
                "mediaCarryingConnections": len(carrying),
                "nativeAudioNonzeroUnits": sealed["nativeAudioNonzeroUnits"],
                "nativeAudioPcmBytes": sealed["nativeAudioPcmBytes"],
                "nativeVideoFramesMinimum": sealed["nativeVideoFramesMinimum"],
                "schema": 1,
                "status": "passed",
                "transport": (
                    "literal-loopback-rtsp-interleaved-tcp-blackhole"
                    if config["relayMode"] == "server-to-client-blackhole"
                    else "literal-loopback-rtsp-interleaved-tcp-finite-hold"
                    if config["relayMode"] == "server-to-client-finite-hold"
                    else "literal-loopback-rtsp-interleaved-tcp"
                ),
            }, sort_keys=True, separators=(",", ":")))
    except (rtsp.RTSPCaseError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"RTSP native smoke: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
