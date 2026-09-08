#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Bounded literal-loopback RTSP/TCP service and live-result oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit


SCHEMA = 1
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DRIVER_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_RTSP_CONTROL_BYTES = 64 * 1024
MAX_OBSERVATION_LAG_MS = 250
MIN_OBSERVATION_LAG_MS = -2
BLACKHOLE_PAUSE_RESUME_LIMIT_MS = 1000
BLACKHOLE_SHUTDOWN_LIMIT_MS = 5000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CONFIG_KEYS = {
    "backendPort",
    "bind",
    "blackholeAfterServerBytes",
    "caseId",
    "ffmpegBin",
    "ffprobeBin",
    "fixtureRelativePath",
    "fixtureSha256",
    "holdDurationMs",
    "maxBytesPerDirection",
    "maxConnections",
    "maxLogBytes",
    "maxObservedConnections",
    "mediamtxBin",
    "minObservedConnections",
    "nativeProbeSeconds",
    "port",
    "relayMode",
    "schema",
    "service",
    "sourceScheme",
    "streamPath",
}
CONNECTION_KEYS = {
    "blackhole_after_server_bytes",
    "blackhole_started_monotonic_ns",
    "case_id",
    "client_to_server_bytes_after_blackhole",
    "client_to_server_bytes",
    "clock_basis",
    "connection_seq",
    "finished_monotonic_ns",
    "interleaved_tcp_setups",
    "schema",
    "server_to_client_bytes",
    "started_monotonic_ns",
    "status",
    "terminal_reason",
    "relay_mode",
}
FINITE_HOLD_CONNECTION_KEYS = CONNECTION_KEYS | {"hold_resumed_monotonic_ns"}
COMPLETION_KEYS = {
    "acceptedConnectionCount",
    "caseId",
    "clockBasis",
    "ffmpegSha256",
    "ffprobeSha256",
    "fixtureSha256",
    "logBytes",
    "logSha256",
    "mediamtxSha256",
    "nativeAudioNonzeroUnits",
    "nativeAudioPcmBytes",
    "nativeVideoFramesMinimum",
    "rejectedConnectionCount",
    "schema",
    "status",
    "terminalRecordCount",
}
WATCH_KEYS = {
    "clockBasis",
    "generationWindows",
    "maxObservationLagMs",
    "resultMonotonicNs",
    "schema",
    "sourceGenerations",
}
BLACKHOLE_WATCH_KEYS = {
    "apiWindows",
    "clockBasis",
    "preconditionMonotonicNs",
    "preconditionSeq",
    "relayMode",
    "resultMonotonicNs",
    "resultSeq",
    "schema",
    "sourceGenerations",
}
FINITE_HOLD_WATCH_KEYS = {
    "clockBasis",
    "generationWindows",
    "holdPreconditionMonotonicNs",
    "holdPreconditionSeq",
    "maxObservationLagMs",
    "resultMonotonicNs",
    "schema",
    "sourceGenerations",
}
BLACKHOLE_API_WINDOW_KEYS = {
    "api",
    "beginMonotonicNs",
    "beginSeq",
    "eventMonotonicNs",
    "eventSeq",
    "maxLatencyMs",
    "returnMonotonicNs",
    "returnSeq",
    "waitCompletedMonotonicNs",
    "waitCompletedSeq",
}


class RTSPCaseError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RTSPCaseError(message)


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def atomic_json(path: Path, value: object) -> None:
    payload = canonical_bytes(value)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_bytes(path: Path, maximum: int, label: str) -> bytes:
    require(path.is_file() and not path.is_symlink(), f"{label} is not a regular nonsymlink file")
    size = path.stat().st_size
    require(0 < size <= maximum, f"{label} size is outside (0, {maximum}]")
    return path.read_bytes()


def read_json(path: Path, maximum: int = MAX_DOCUMENT_BYTES) -> Any:
    payload = read_bytes(path, maximum, path.name)
    require(b"\x00" not in payload, f"{path.name} contains a NUL byte")
    return json.loads(payload.decode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def monotonic_raw_ns() -> int:
    require(hasattr(time, "CLOCK_MONOTONIC_RAW"), "CLOCK_MONOTONIC_RAW is unavailable")
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    require(set(value) == keys, f"{label} keys differ: {sorted(set(value) ^ keys)}")
    return value


def positive_integer(value: object, label: str, maximum: int) -> int:
    require(type(value) is int and 1 <= value <= maximum, f"{label} is outside [1, {maximum}]")
    return value


def normalized_relative(value: object, label: str) -> str:
    require(type(value) is str and value, f"{label} must be nonempty text")
    require("\\" not in value and "\x00" not in value, f"{label} is not canonical")
    relative = PurePosixPath(value)
    require(not relative.is_absolute() and relative.as_posix() == value, f"{label} is not normalized")
    require(all(part not in {"", ".", ".."} for part in relative.parts), f"{label} traverses")
    return value


def require_store_executable(value: object, basename: str) -> Path:
    require(type(value) is str and value.startswith("/nix/store/"), f"{basename} is not in /nix/store")
    path = Path(value)
    require(path.name == basename, f"{basename} input has an unexpected basename")
    require(path.is_file() and not path.is_symlink() and os.access(path, os.X_OK),
            f"{basename} is not an executable nonsymlink file")
    require(path.resolve() == path, f"{basename} input is not canonical")
    return path


def validate_config(value: object) -> dict[str, Any]:
    if (type(value) is dict
            and set(value) == CONFIG_KEYS - {"holdDurationMs"}
            and value.get("relayMode") in {"forward", "server-to-client-blackhole"}):
        # Cases composed before finite-hold support have the same schema and
        # an implicit zero duration. Normalize only that exact historical
        # shape; a finite-hold case must always declare its bounded duration.
        value = {**value, "holdDurationMs": 0}
    config = exact_object(value, CONFIG_KEYS, "RTSP service config")
    require(type(config["schema"]) is int and config["schema"] == SCHEMA, "unsupported RTSP schema")
    require(config["service"] == "rtsp-live-v1", "unsupported RTSP service")
    require(type(config["sourceScheme"]) is str
            and config["sourceScheme"] in {"rtsp", "rtspt"},
            "sourceScheme is not a reviewed RTSP spelling")
    require(config["bind"] == "127.0.0.1", "RTSP gate must bind literal IPv4 loopback")
    require(type(config["caseId"]) is str and CASE_ID_RE.fullmatch(config["caseId"]), "invalid caseId")
    require(config["streamPath"] == "fixture", "unexpected RTSP stream path")
    normalized_relative(config["fixtureRelativePath"], "fixtureRelativePath")
    require(type(config["fixtureSha256"]) is str and SHA256_RE.fullmatch(config["fixtureSha256"]),
            "fixtureSha256 is not lowercase SHA-256")
    public = positive_integer(config["port"], "port", 65535)
    backend = positive_integer(config["backendPort"], "backendPort", 65535)
    require(public != backend and public >= 1024 and backend >= 1024,
            "RTSP public/backend ports must be distinct and unprivileged")
    maximum = positive_integer(config["maxConnections"], "maxConnections", 64)
    minimum_observed = positive_integer(config["minObservedConnections"], "minObservedConnections", maximum)
    maximum_observed = positive_integer(config["maxObservedConnections"], "maxObservedConnections", maximum)
    require(minimum_observed <= maximum_observed, "observed RTSP connection bounds are inverted")
    byte_budget = positive_integer(
        config["maxBytesPerDirection"], "maxBytesPerDirection", 4 * 1024 * 1024 * 1024
    )
    require(config["relayMode"] in {
        "forward", "server-to-client-blackhole", "server-to-client-finite-hold",
    },
            "relayMode is not a reviewed RTSP relay mode")
    require(type(config["blackholeAfterServerBytes"]) is int
            and 0 <= config["blackholeAfterServerBytes"] <= byte_budget,
            "blackholeAfterServerBytes is outside the directional byte budget")
    require(type(config["holdDurationMs"]) is int
            and 0 <= config["holdDurationMs"] <= 4000,
            "holdDurationMs is outside [0, 4000]")
    if config["relayMode"] == "forward":
        require(config["blackholeAfterServerBytes"] == 0,
                "forward relayMode must use a zero blackhole threshold")
        require(config["holdDurationMs"] == 0,
                "forward relayMode must use a zero hold duration")
    elif config["relayMode"] == "server-to-client-blackhole":
        require(config["blackholeAfterServerBytes"] >= 64 * 1024,
                "blackhole relayMode requires an explicit threshold of at least 64 KiB")
        require(config["holdDurationMs"] == 0,
                "blackhole relayMode must use a zero hold duration")
    else:
        require(config["blackholeAfterServerBytes"] >= 64 * 1024,
                "finite-hold relayMode requires an explicit threshold of at least 64 KiB")
        require(100 <= config["holdDurationMs"] < 5000,
                "finite-hold relayMode requires a bounded duration below five seconds")
    positive_integer(config["maxLogBytes"], "maxLogBytes", 16 * 1024 * 1024)
    positive_integer(config["nativeProbeSeconds"], "nativeProbeSeconds", 10)
    require_store_executable(config["mediamtxBin"], "mediamtx")
    require_store_executable(config["ffmpegBin"], "ffmpeg")
    require_store_executable(config["ffprobeBin"], "ffprobe")
    return config


def load_sums(root: Path, ready: dict[str, Any]) -> dict[str, str]:
    path = root / "SHA256SUMS"
    payload = read_bytes(path, MAX_DOCUMENT_BYTES, "SHA256SUMS")
    require(sha256_file(path) == ready["sha256sums_sha256"], "SHA256SUMS does not match READY")
    sums: dict[str, str] = {}
    lines = payload.decode("ascii").splitlines()
    require(1 <= len(lines) <= 50_000, "SHA256SUMS entry count is outside bounds")
    for line in lines:
        require(len(line) >= 67 and line[64:66] == "  ", "malformed SHA256SUMS line")
        digest, name = line[:64], line[66:]
        require(SHA256_RE.fullmatch(digest) is not None, "invalid SHA256SUMS digest")
        normalized_relative(name, "SHA256SUMS path")
        require(name not in sums, "duplicate SHA256SUMS path")
        sums[name] = digest
    return sums


def checked_fixture(root_path: Path, manifest_path: Path, config: dict[str, Any]) -> Path:
    root = root_path.resolve(strict=True)
    require(root_path == root and root.is_dir() and not root.is_symlink(), "fixture root is not canonical")
    manifest = manifest_path.resolve(strict=True)
    require(manifest == root / "provenance/manifest.json", "fixture manifest is not rooted under fixture bytes")
    ready = exact_object(read_json(root / "READY"),
                         {"manifest_sha256", "profile", "schema", "sha256sums_sha256"}, "READY")
    require(ready["schema"] == SCHEMA and ready["profile"] == "full", "RTSP live test requires full fixtures")
    for key in ("manifest_sha256", "sha256sums_sha256"):
        require(type(ready[key]) is str and SHA256_RE.fullmatch(ready[key]), f"invalid READY {key}")
    require(sha256_file(manifest) == ready["manifest_sha256"], "fixture manifest does not match READY")
    sums = load_sums(root, ready)
    relative = normalized_relative(config["fixtureRelativePath"], "fixtureRelativePath")
    require(relative in sums, "selected RTSP fixture is absent from SHA256SUMS")
    require(sums[relative] == config["fixtureSha256"], "selected RTSP fixture config identity differs")
    fixture = (root / Path(*PurePosixPath(relative).parts)).resolve(strict=True)
    require(fixture.is_file() and fixture.is_relative_to(root), "selected RTSP fixture escaped its root")
    require(sha256_file(fixture) == sums[relative], "selected RTSP fixture hash differs")

    document = read_json(manifest)
    require(type(document) is dict and type(document.get("artifacts")) is list,
            "fixture manifest has no artifact list")
    matches = [item for item in document["artifacts"]
               if type(item) is dict and item.get("path") == relative]
    require(len(matches) == 1, "selected RTSP fixture has no unique manifest entry")
    artifact = matches[0]
    require(artifact.get("sha256") == sums[relative] and artifact.get("bytes") == fixture.stat().st_size,
            "selected RTSP fixture manifest identity differs")
    report_info = artifact.get("ffprobe")
    require(type(report_info) is dict and report_info.get("status") == "accepted",
            "selected RTSP fixture has no accepted probe report")
    report_relative = normalized_relative(report_info.get("path"), "probe report path")
    require(report_relative in sums and report_info.get("sha256") == sums[report_relative],
            "selected RTSP probe identity differs")
    report_path = (root / Path(*PurePosixPath(report_relative).parts)).resolve(strict=True)
    require(sha256_file(report_path) == sums[report_relative], "selected RTSP probe hash differs")
    report = read_json(report_path)
    streams = report.get("streams") if type(report) is dict else None
    require(type(streams) is list, "selected RTSP probe has no stream list")
    require(sum(type(item) is dict and item.get("codec_type") == "video"
                and item.get("codec_name") == "h264" for item in streams) == 1,
            "selected RTSP fixture is not single-track H.264 video")
    require(sum(type(item) is dict and item.get("codec_type") == "audio"
                and item.get("codec_name") == "aac" for item in streams) == 1,
            "selected RTSP fixture is not single-track AAC audio")
    return fixture


def validate_mediamtx_config(path: Path, config: dict[str, Any]) -> None:
    text = read_bytes(path, 256 * 1024, "MediaMTX config").decode("utf-8")
    require(text.startswith("# SPDX-License-Identifier: BSD-3-Clause\n"), "MediaMTX config lacks SPDX")
    required = (
        "api: false", "metrics: false", "pprof: false", "playback: false",
        "rtsp: true", "rtspTransports: [tcp]", 'rtspEncryption: "no"',
        f'rtspAddress: 127.0.0.1:{config["backendPort"]}',
        "rtmp: false", "hls: false", "webrtc: false", "srt: false", "moq: false",
        'ips: ["127.0.0.1"]', "path: fixture", "source: publisher",
    )
    for setting in required:
        require(re.search(rf"(?m)^[ \t]*{re.escape(setting)}[ \t]*$", text) is not None,
                f"MediaMTX config lacks exact setting: {setting}")
    require("0.0.0.0" not in text and "[::]" not in text and "localhost" not in text,
            "MediaMTX config contains a nonliteral listener")
    require(re.search(r"(?i)https?://|rtsp://|rtspt://", text) is None,
            "MediaMTX config contains a configured URL")


def validate_scenario(path: Path, config: dict[str, Any]) -> None:
    text = read_bytes(path, MAX_DOCUMENT_BYTES, "scenario").decode("utf-8")
    expected_url = (
        f'{config["sourceScheme"]}://127.0.0.1:{config["port"]}/{config["streamPath"]}'
    )
    source_commands: list[str] = []
    labels: list[str] = []
    commands: list[str] = []
    parsed_lines: list[list[str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise RTSPCaseError(f"scenario line {number} has invalid quoting") from error
        if not words:
            continue
        parsed_lines.append(words)
        command = words[0].lower()
        commands.append(command)
        if command in {"load", "replace"}:
            require(len(words) == 2, f"scenario source at line {number} is not canonical")
            parsed = urlsplit(words[1])
            require(words[1] == expected_url and parsed.scheme == config["sourceScheme"]
                    and parsed.hostname == "127.0.0.1" and parsed.username is None
                    and parsed.password is None and parsed.query == "" and parsed.fragment == "",
                    f"scenario source at line {number} is not literal loopback RTSP")
            source_commands.append(command)
        if command == "snapshot":
            require(len(words) == 2, f"scenario snapshot at line {number} has no exact label")
            labels.append(words[1])
        require(command != "seek", "live RTSP scenario must not issue an ordinary finite seek")
    require(commands[-1:] == ["shutdown"] and commands.count("shutdown") == 1,
            "RTSP scenario must end with one shutdown")
    if config["caseId"] == "rtsp-live-tcp-drain-diagnostics":
        require(config["relayMode"] == "forward",
                "RTSP drain diagnostics must use the forward relay")
        require(source_commands == ["load", "replace"],
                "RTSP drain diagnostics must loop nine bounded replacements")
        require(commands == [
            "load", "wait_event", "play", "wait_event", "loop", "wait_ms",
            "snapshot", "endloop", "loop", "replace", "wait_event", "play",
            "wait_event", "loop", "wait_ms", "snapshot", "endloop", "endloop",
            "shutdown",
        ], "RTSP drain-diagnostics scenario action order differs")
        require(labels == ["rtsp-drain-diagnostics", "rtsp-drain-diagnostics"],
                "RTSP drain-diagnostics checkpoint labels differ")
        require(parsed_lines.count(["loop", "4"]) == 2,
                "RTSP drain diagnostics must take four checkpoints per generation")
        require(parsed_lines.count(["loop", "9"]) == 1,
                "RTSP drain diagnostics must execute nine replacements")
    elif config["relayMode"] == "forward":
        require(source_commands == ["load", "replace", "replace"],
                "forward RTSP scenario must perform one load and two bounded replacements")
        require(set(labels) == {"rtsp-live-g1", "rtsp-live-g2", "rtsp-live-g3"},
                "forward RTSP scenario live checkpoint labels differ")
    elif config["relayMode"] == "server-to-client-blackhole":
        require(source_commands == ["load"],
                "blackhole RTSP scenario must perform exactly one source load")
        require(commands == [
            "load", "wait_event", "play", "wait_event", "wait_ms", "snapshot",
            "wait_ms", "snapshot", "pause", "wait_event", "snapshot", "play",
            "wait_event", "snapshot", "shutdown",
        ], "blackhole RTSP scenario action order differs")
        require(labels == [
            "rtsp-blackhole-precondition",
            "rtsp-blackhole-before-pause",
            "rtsp-blackhole-before-resume",
            "rtsp-blackhole-before-shutdown",
        ], "blackhole RTSP scenario checkpoint labels differ")
        require(["wait_event", "PAUSE", str(BLACKHOLE_PAUSE_RESUME_LIMIT_MS)] in parsed_lines,
                "blackhole pause wait is not bounded to one second")
        require(parsed_lines.count(
            ["wait_event", "PLAYING", str(BLACKHOLE_PAUSE_RESUME_LIMIT_MS)]
        ) == 1, "blackhole resume wait is not uniquely bounded to one second")
    else:
        require(source_commands == ["load"],
                "finite-hold RTSP scenario must perform exactly one source load")
        require(commands == [
            "load", "wait_event", "play", "wait_event", "wait_ms", "snapshot",
            "wait_ms", "loop", "wait_ms", "snapshot", "endloop", "shutdown",
        ], "finite-hold RTSP scenario action order differs")
        require(labels == ["rtsp-hold-precondition", "rtsp-live-g1"],
                "finite-hold RTSP scenario checkpoint labels differ")
        require(["loop", "4"] in parsed_lines,
                "finite-hold RTSP recovery observation is not four checkpoints")
        require(["wait_ms", "6000"] in parsed_lines,
                "finite-hold RTSP recovery margin differs")


def load_and_validate(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path]:
    config = validate_config(read_json(args.config))
    fixture = checked_fixture(args.fixture_root, args.fixture_manifest, config)
    mediamtx_config = args.config.parent / "mediamtx.yml"
    validate_mediamtx_config(mediamtx_config, config)
    validate_scenario(args.scenario, config)
    return config, fixture, mediamtx_config


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int, process: subprocess.Popen[bytes], seconds: float = 8.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        require(process.poll() is None, "MediaMTX exited before its backend listener opened")
        if port_open(port):
            return
        time.sleep(0.05)
    raise RTSPCaseError("MediaMTX backend listener did not open within its deadline")


def bounded_run(arguments: list[str], seconds: float, *, stdout: int | Any = subprocess.DEVNULL) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=seconds,
            check=False,
            env={"HOME": "/nonexistent", "LANG": "C.UTF-8", "PATH": "/run/current-system/sw/bin"},
        )
    except subprocess.TimeoutExpired as error:
        raise RTSPCaseError("native RTSP probe exceeded its wall-clock deadline") from error
    require(len(result.stderr) <= 1024 * 1024, "native RTSP probe diagnostics exceeded one MiB")
    return result


def wait_published(config: dict[str, Any], publisher: subprocess.Popen[bytes]) -> None:
    url = f'rtsp://127.0.0.1:{config["backendPort"]}/{config["streamPath"]}'
    command = [
        config["ffprobeBin"], "-v", "error", "-rtsp_transport", "tcp", "-timeout", "500000",
        "-show_entries", "stream=codec_name,codec_type", "-of", "json", url,
    ]
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        require(publisher.poll() is None, "FFmpeg publisher exited before RTSP publication became ready")
        result = bounded_run(command, 2.0, stdout=subprocess.PIPE)
        if result.returncode == 0:
            try:
                streams = json.loads(result.stdout.decode("utf-8")).get("streams")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                streams = None
            if type(streams) is list:
                types = {(item.get("codec_type"), item.get("codec_name"))
                         for item in streams if type(item) is dict}
                if ("video", "h264") in types and ("audio", "aac") in types:
                    return
        time.sleep(0.1)
    raise RTSPCaseError("RTSP publication did not expose one H.264 and one AAC track")


def native_decode(config: dict[str, Any]) -> tuple[int, int, int]:
    url = f'rtsp://127.0.0.1:{config["backendPort"]}/{config["streamPath"]}'
    video_frames = 30
    video = bounded_run([
        config["ffmpegBin"], "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
        "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url,
        "-map", "0:v:0", "-frames:v", str(video_frames), "-f", "null", "-",
    ], 15.0)
    require(video.returncode == 0, "native RTSP/TCP video decode failed")
    seconds = config["nativeProbeSeconds"]
    audio = bounded_run([
        config["ffmpegBin"], "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
        "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url,
        "-map", "0:a:0", "-t", str(seconds), "-ac", "2", "-ar", "48000",
        "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ], float(seconds + 12), stdout=subprocess.PIPE)
    require(audio.returncode == 0, "native RTSP/TCP audio decode failed")
    expected_minimum = seconds * 48000 * 2 * 2 * 3 // 4
    require(expected_minimum <= len(audio.stdout) <= seconds * 48000 * 2 * 2 * 2,
            "native decoded PCM byte count is outside its bounded range")
    units = [int.from_bytes(audio.stdout[index:index + 2], "little", signed=True)
             for index in range(0, len(audio.stdout) - 1, 2)]
    nonzero = sum(value != 0 for value in units)
    require(nonzero >= 1000, "native decoded PCM is effectively silent")
    return video_frames, len(audio.stdout), nonzero


def terminate_process(process: subprocess.Popen[bytes] | None) -> bool:
    if process is None:
        return True
    termination_requested = False
    if process.poll() is None:
        termination_requested = True
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    if termination_requested:
        # FFmpeg catches SIGTERM, performs its trailer/IO cleanup, and reports
        # 255 instead of exposing the Unix signal status. An unsolicited 255
        # before teardown remains a failure.
        return process.returncode in {0, 255, -signal.SIGTERM}
    return process.returncode == 0


class ConnectionLog:
    def __init__(self, path: Path, config: dict[str, Any]) -> None:
        self.path = path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(descriptor, "wb")
        self.config = config
        self.lock = threading.Lock()
        self.bytes_written = 0
        self.records = 0

    def write(self, record: dict[str, Any]) -> None:
        payload = canonical_bytes(record)
        with self.lock:
            require(self.bytes_written + len(payload) <= self.config["maxLogBytes"],
                    "RTSP connection evidence exceeded its byte budget")
            self.stream.write(payload)
            self.stream.flush()
            self.bytes_written += len(payload)
            self.records += 1

    def close(self) -> None:
        with self.lock:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()


class InterleavedSetupObserver:
    """Count bounded client SETUP requests that explicitly select RTP over TCP."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.count = 0

    def feed(self, payload: bytes) -> None:
        if self.count >= 2:
            return
        remaining = MAX_RTSP_CONTROL_BYTES - len(self.buffer)
        if remaining <= 0:
            return
        self.buffer.extend(payload[:remaining])
        while b"\r\n\r\n" in self.buffer:
            block, _separator, rest = self.buffer.partition(b"\r\n\r\n")
            self.buffer = bytearray(rest)
            lines = block.split(b"\r\n")
            if not lines or not lines[0].upper().startswith(b"SETUP "):
                continue
            transport = next(
                (line.split(b":", 1)[1].strip().lower()
                 for line in lines[1:] if line.lower().startswith(b"transport:")),
                b"",
            )
            if b"rtp/avp/tcp" in transport and b"interleaved=" in transport:
                self.count += 1


class TCPGate:
    def __init__(self, config: dict[str, Any], evidence: ConnectionLog) -> None:
        self.config = config
        self.evidence = evidence
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        self.workers: list[threading.Thread] = []
        self.active: set[socket.socket] = set()
        self.errors: list[str] = []

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.config["port"]))
        listener.listen(8)
        listener.settimeout(0.2)
        self.listener = listener
        self.accept_thread = threading.Thread(target=self._accept, name="rtsp-gate-accept", daemon=False)
        self.accept_thread.start()

    def _accept(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                client, peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    self.errors.append("RTSP gate listener failed")
                break
            if peer[0] != "127.0.0.1":
                client.close()
                with self.lock:
                    self.rejected += 1
                continue
            with self.lock:
                if self.accepted >= self.config["maxConnections"]:
                    self.rejected += 1
                    client.close()
                    continue
                self.accepted += 1
                sequence = self.accepted
            worker = threading.Thread(target=self._relay, args=(sequence, client),
                                      name=f"rtsp-gate-{sequence}", daemon=False)
            with self.lock:
                self.workers.append(worker)
            worker.start()

    def _relay(self, sequence: int, client: socket.socket) -> None:
        started = monotonic_raw_ns()
        upstream: socket.socket | None = None
        client_bytes = 0
        client_bytes_after_blackhole = 0
        server_bytes = 0
        status = "eof"
        terminal_reason = "relay-error"
        impairment_started: int | None = None
        hold_until_ns: int | None = None
        hold_resumed: int | None = None
        relay_mode = self.config["relayMode"]
        blackhole_after = self.config["blackholeAfterServerBytes"]
        hold_seconds = self.config["holdDurationMs"] / 1000.0
        transport = InterleavedSetupObserver()
        last_source: socket.socket | None = None
        last_target: socket.socket | None = None
        try:
            upstream = socket.create_connection(("127.0.0.1", self.config["backendPort"]), timeout=5)
            client.settimeout(5)
            upstream.settimeout(5)
            with self.lock:
                self.active.add(client)
                self.active.add(upstream)
            while not self.stop_event.is_set():
                if hold_until_ns is not None and monotonic_raw_ns() >= hold_until_ns:
                    hold_until_ns = None
                impaired = impairment_started is not None and (
                    relay_mode == "server-to-client-blackhole" or hold_until_ns is not None
                )
                inputs = [client] if impaired else [client, upstream]
                readable, _, _ = select.select(inputs, [], [], 0.05 if hold_until_ns else 0.2)
                if not readable:
                    continue
                for source in readable:
                    target = upstream if source is client else client
                    last_source = source
                    last_target = target
                    receive_limit = 64 * 1024
                    if (source is upstream and relay_mode != "forward"
                            and impairment_started is None):
                        remaining = blackhole_after - server_bytes
                        require(remaining > 0, "RTSP impairment read upstream after its cutoff")
                        receive_limit = min(receive_limit, remaining)
                    payload = source.recv(receive_limit)
                    if not payload:
                        terminal_reason = "client-eof" if source is client else "upstream-eof"
                        raise EOFError
                    if source is client:
                        transport.feed(payload)
                        target.sendall(payload)
                        client_bytes += len(payload)
                        if impairment_started is not None:
                            client_bytes_after_blackhole += len(payload)
                        require(client_bytes <= self.config["maxBytesPerDirection"],
                                "RTSP client-to-server byte budget exceeded")
                    else:
                        target.sendall(payload)
                        server_bytes += len(payload)
                        if (relay_mode == "server-to-client-finite-hold"
                                and impairment_started is not None
                                and hold_until_ns is None and hold_resumed is None):
                            hold_resumed = monotonic_raw_ns()
                        require(server_bytes <= self.config["maxBytesPerDirection"],
                                "RTSP server-to-client byte budget exceeded")
                        if (relay_mode != "forward" and impairment_started is None
                                and server_bytes == blackhole_after):
                            impairment_started = monotonic_raw_ns()
                            if relay_mode == "server-to-client-finite-hold":
                                hold_until_ns = impairment_started + int(
                                    hold_seconds * 1_000_000_000
                                )
            if self.stop_event.is_set():
                status = "service-stop"
                terminal_reason = "service-stop"
        except EOFError:
            status = "eof"
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # RTSP clients are allowed to close the transport immediately
            # after TEARDOWN. Treat a reset as a normal close only after the
            # connection carried protocol bytes in both directions.
            status = "eof" if client_bytes and server_bytes else "relay-error"
            if last_target is client or last_source is client:
                terminal_reason = "client-reset"
            elif last_target is upstream or last_source is upstream:
                terminal_reason = "upstream-reset"
        except (OSError, RTSPCaseError):
            status = "service-stop" if self.stop_event.is_set() else "relay-error"
            terminal_reason = status
        finally:
            finished = monotonic_raw_ns()
            for sock in (client, upstream):
                if sock is not None:
                    with self.lock:
                        self.active.discard(sock)
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
            try:
                record = {
                    "blackhole_after_server_bytes": blackhole_after,
                    "blackhole_started_monotonic_ns": impairment_started,
                    "case_id": self.config["caseId"],
                    "client_to_server_bytes": client_bytes,
                    "client_to_server_bytes_after_blackhole": client_bytes_after_blackhole,
                    "clock_basis": CLOCK_BASIS,
                    "connection_seq": sequence,
                    "finished_monotonic_ns": finished,
                    "interleaved_tcp_setups": transport.count,
                    "relay_mode": relay_mode,
                    "schema": SCHEMA,
                    "server_to_client_bytes": server_bytes,
                    "started_monotonic_ns": started,
                    "status": status,
                    "terminal_reason": terminal_reason,
                }
                if relay_mode == "server-to-client-finite-hold":
                    record["hold_resumed_monotonic_ns"] = hold_resumed
                self.evidence.write(record)
            except (OSError, RTSPCaseError) as error:
                self.errors.append(str(error))

    def stop(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        with self.lock:
            active = list(self.active)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=3)
            require(not self.accept_thread.is_alive(), "RTSP gate accept thread survived teardown")
        with self.lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(timeout=3)
            require(not worker.is_alive(), "RTSP gate relay thread survived teardown")
        require(not self.errors, "; ".join(self.errors))


def command_check(args: argparse.Namespace) -> None:
    config, _fixture, _mediamtx_config = load_and_validate(args)
    print(config["port"])


def command_serve(args: argparse.Namespace) -> None:
    config, fixture, mediamtx_config = load_and_validate(args)
    require(args.log.parent == args.completion_marker.parent,
            "RTSP log and completion marker must share one result directory")
    require(args.log.parent.is_dir() and not args.log.parent.is_symlink(),
            "RTSP result directory is invalid")
    require(not args.log.exists() and not args.completion_marker.exists(),
            "RTSP evidence paths must be fresh")
    require(not port_open(config["port"]) and not port_open(config["backendPort"]),
            "RTSP test port is already in use")

    state = Path(tempfile.mkdtemp(prefix=".rtsp-live-", dir=args.log.parent))
    os.chmod(state, 0o700)
    mediamtx_log = (state / "mediamtx.log").open("wb")
    publisher_log = (state / "publisher.log").open("wb")
    server: subprocess.Popen[bytes] | None = None
    publisher: subprocess.Popen[bytes] | None = None
    gate: TCPGate | None = None
    evidence: ConnectionLog | None = None
    stop = threading.Event()
    native_video = 0
    native_pcm = 0
    native_nonzero = 0
    accepted_connections = 0
    rejected_connections = 0

    def on_signal(_number: int, _frame: object) -> None:
        stop.set()

    prior_term = signal.signal(signal.SIGTERM, on_signal)
    prior_int = signal.signal(signal.SIGINT, on_signal)
    clean_children = False
    try:
        server = subprocess.Popen(
            [config["mediamtxBin"], os.fspath(mediamtx_config)],
            stdin=subprocess.DEVNULL, stdout=mediamtx_log, stderr=subprocess.STDOUT,
            cwd=state,
            env={"HOME": os.fspath(state), "LANG": "C.UTF-8", "PATH": "/run/current-system/sw/bin"},
        )
        wait_port(config["backendPort"], server)
        backend_url = f'rtsp://127.0.0.1:{config["backendPort"]}/{config["streamPath"]}'
        publisher = subprocess.Popen([
            config["ffmpegBin"], "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-re", "-stream_loop", "-1", "-i", os.fspath(fixture),
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp", "-muxdelay", "0.1", backend_url,
        ], stdin=subprocess.DEVNULL, stdout=publisher_log, stderr=subprocess.STDOUT,
            cwd=state,
            env={"HOME": os.fspath(state), "LANG": "C.UTF-8", "PATH": "/run/current-system/sw/bin"})
        wait_published(config, publisher)
        native_video, native_pcm, native_nonzero = native_decode(config)
        require(server.poll() is None and publisher.poll() is None,
                "RTSP service exited during native qualification")
        evidence = ConnectionLog(args.log, config)
        gate = TCPGate(config, evidence)
        gate.start()
        print("bounded literal-loopback RTSP/TCP fixture ready", flush=True)

        while not stop.wait(0.1):
            require(server.poll() is None, "MediaMTX exited during RTSP qualification")
            require(publisher.poll() is None, "FFmpeg publisher exited during RTSP qualification")

        gate.stop()
        accepted_connections = gate.accepted
        rejected_connections = gate.rejected
        gate = None
        clean_children = terminate_process(publisher) and terminate_process(server)
        publisher = None
        server = None
        require(clean_children, "RTSP service children did not stop cleanly")
        evidence.close()
        log_bytes = args.log.read_bytes()
        completion = {
            "acceptedConnectionCount": accepted_connections,
            "caseId": config["caseId"],
            "clockBasis": CLOCK_BASIS,
            "ffmpegSha256": sha256_file(Path(config["ffmpegBin"])),
            "ffprobeSha256": sha256_file(Path(config["ffprobeBin"])),
            "fixtureSha256": sha256_file(fixture),
            "logBytes": len(log_bytes),
            "logSha256": hashlib.sha256(log_bytes).hexdigest(),
            "mediamtxSha256": sha256_file(Path(config["mediamtxBin"])),
            "nativeAudioNonzeroUnits": native_nonzero,
            "nativeAudioPcmBytes": native_pcm,
            "nativeVideoFramesMinimum": native_video,
            "rejectedConnectionCount": rejected_connections,
            "schema": SCHEMA,
            "status": "complete",
            "terminalRecordCount": evidence.records,
        }
        atomic_json(args.completion_marker, completion)
    finally:
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)
        if gate is not None:
            try:
                gate.stop()
            except RTSPCaseError:
                pass
        if evidence is not None and not evidence.stream.closed:
            evidence.close()
        terminate_process(publisher)
        terminate_process(server)
        mediamtx_log.close()
        publisher_log.close()
        shutil.rmtree(state, ignore_errors=True)
def command_watch(args: argparse.Namespace) -> None:
    require(args.instrumentation in {"audio-monitor", "endpoint-monitor"},
            "RTSP watcher instrumentation is invalid")
    config_path = getattr(args, "config", None)
    relay_mode = "forward"
    case_id = "rtsp-live-tcp-reopen"
    if config_path is not None:
        config = validate_config(read_json(config_path))
        relay_mode = config["relayMode"]
        case_id = config["caseId"]
    expected_source_generations = (
        10 if case_id == "rtsp-live-tcp-drain-diagnostics" else 3
    )
    live_checkpoint_labels = (
        {"rtsp-drain-diagnostics"}
        if case_id == "rtsp-live-tcp-drain-diagnostics"
        else {"rtsp-live-g1", "rtsp-live-g2", "rtsp-live-g3"}
    )
    if relay_mode in {"server-to-client-blackhole", "server-to-client-finite-hold"}:
        require(args.instrumentation == "audio-monitor",
                "RTSP impaired-delivery gate requires decoded-audio instrumentation")
    deadline = time.monotonic() + args.timeout_seconds
    offset = 0
    pending = b""
    expected_seq = 1
    origin_ms: int | None = None
    max_lag = 0
    saw_result = False
    result_ns: int | None = None
    result_point: tuple[int, int] | None = None
    producer_done = False
    generation_windows: dict[int, dict[str, Any]] = {}
    forbidden_seek = False
    blackhole_markers: dict[str, list[tuple[int, int]]] = {}
    blackhole_actions: dict[str, list[tuple[int, int]]] = {}
    blackhole_events: dict[str, list[tuple[int, int]]] = {}
    blackhole_waits: dict[str, list[tuple[int, int]]] = {}
    blackhole_source_generations: set[int] = set()
    blackhole_forbidden = False
    hold_preconditions: list[tuple[int, int]] = []
    hold_recovery_points: list[tuple[int, int]] = []
    hold_source_generations: set[int] = set()
    hold_forbidden = False

    while time.monotonic() < deadline:
        done_before = args.producer_done.exists()
        producer_done = producer_done or done_before
        if not args.driver_json.exists():
            if done_before:
                break
            time.sleep(0.01)
            continue
        require(args.driver_json.stat().st_size <= MAX_DRIVER_BYTES, "driver JSONL exceeded its byte limit")
        with args.driver_json.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            offset = stream.tell()
        if not chunk:
            if done_before:
                break
            time.sleep(0.01)
            continue
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for payload in lines:
            require(0 < len(payload) <= MAX_RECORD_BYTES, "driver record is empty or oversized")
            record = json.loads(payload.decode("utf-8"))
            require(type(record) is dict and record.get("seq") == expected_seq,
                    "driver record sequence is not contiguous")
            expected_seq += 1
            require(not saw_result, "driver record followed terminal result")
            relative = record.get("monotonic_ms")
            current_origin = record.get("monotonic_origin_ms")
            require(type(relative) is int and relative >= 0 and type(current_origin) is int and current_origin >= 0,
                    "driver monotonic fields are invalid")
            if origin_ms is None:
                origin_ms = current_origin
            require(current_origin == origin_ms, "driver monotonic origin changed")
            event_ns = (current_origin + relative) * 1_000_000
            point = (event_ns, record["seq"])
            lag = (monotonic_raw_ns() - event_ns) // 1_000_000
            require(MIN_OBSERVATION_LAG_MS <= lag <= MAX_OBSERVATION_LAG_MS,
                    "driver and CLOCK_MONOTONIC_RAW do not calibrate")
            max_lag = max(max_lag, max(0, lag))

            if record.get("action") == "seek" or record.get("event") in {"SEEKING", "SEEKED"}:
                forbidden_seek = True
            generation = record.get("source_generation")
            if relay_mode == "server-to-client-blackhole":
                if type(generation) is int and generation > 0:
                    blackhole_source_generations.add(generation)
                if (record.get("action") in {"replace", "seek"}
                        or record.get("event") in {
                            "ERROR", "STREAMRENDERINGERROR", "SEEKING", "SEEKED", "ENDED",
                        }):
                    blackhole_forbidden = True
                label = record.get("label")
                if label in {
                    "rtsp-blackhole-precondition",
                    "rtsp-blackhole-before-pause",
                    "rtsp-blackhole-before-resume",
                    "rtsp-blackhole-before-shutdown",
                }:
                    require(record.get("type") == "snapshot" and record.get("action") == "snapshot",
                            "RTSP blackhole checkpoint is not a snapshot")
                    require(generation == 1 and record.get("duration") is None
                            and record.get("has_audio") is True and record.get("has_video") is True
                            and record.get("seeking") is False and record.get("ended") is False,
                            "RTSP blackhole checkpoint lacks live simultaneous A/V state")
                    if label == "rtsp-blackhole-precondition":
                        require(record.get("paused") is False
                                and record.get("audio_monitor_enabled") is True
                                and type(record.get("audio_samples_generation")) is int
                                and record["audio_samples_generation"] >= 10
                                and type(record.get("audio_bytes_generation")) is int
                                and record["audio_bytes_generation"] >= 4096
                                and type(record.get("audio_nonzero_units_generation")) is int
                                and record["audio_nonzero_units_generation"] >= 20,
                                "RTSP blackhole precondition lacks advancing decoded A/V")
                    elif label == "rtsp-blackhole-before-pause":
                        require(record.get("paused") is False,
                                "RTSP blackhole pause began from an already-paused engine")
                    elif label == "rtsp-blackhole-before-resume":
                        require(record.get("paused") is True,
                                "RTSP blackhole resume began from a nonpaused engine")
                    else:
                        require(record.get("paused") is False,
                                "RTSP blackhole shutdown began from a paused engine")
                    blackhole_markers.setdefault(label, []).append(point)
                action = record.get("action")
                if record.get("type") == "snapshot" and action in {"pause", "play", "shutdown"}:
                    require(record.get("status") == "ok", f"RTSP blackhole {action} API failed")
                    blackhole_actions.setdefault(action, []).append(point)
                event = record.get("event")
                if record.get("type") == "event" and event in {"PAUSE", "PLAYING"}:
                    blackhole_events.setdefault(event, []).append(point)
                if record.get("type") == "snapshot" and action == "wait_event":
                    wait_label = record.get("label")
                    if wait_label in {"PAUSE", "PLAYING"}:
                        require(record.get("status") == "ok",
                                f"RTSP blackhole {wait_label} wait did not complete")
                        blackhole_waits.setdefault(wait_label, []).append(point)
            elif relay_mode == "server-to-client-finite-hold":
                if type(generation) is int and generation > 0:
                    hold_source_generations.add(generation)
                if (record.get("action") in {"replace", "seek", "pause"}
                        or record.get("event") in {
                            "ERROR", "STREAMRENDERINGERROR", "SEEKING", "SEEKED", "ENDED",
                        }):
                    hold_forbidden = True
                if record.get("label") == "rtsp-hold-precondition":
                    require(record.get("type") == "snapshot"
                            and record.get("action") == "snapshot"
                            and generation == 1 and record.get("duration") is None
                            and record.get("has_audio") is True
                            and record.get("has_video") is True
                            and record.get("paused") is False
                            and record.get("seeking") is False
                            and record.get("ended") is False
                            and record.get("audio_monitor_enabled") is True,
                            "RTSP finite-hold precondition lacks live simultaneous A/V")
                    require(type(record.get("audio_samples_generation")) is int
                            and record["audio_samples_generation"] >= 10
                            and type(record.get("audio_bytes_generation")) is int
                            and record["audio_bytes_generation"] >= 4096
                            and type(record.get("audio_nonzero_units_generation")) is int
                            and record["audio_nonzero_units_generation"] >= 20,
                            "RTSP finite-hold precondition lacks decoded PCM")
                    for key in ("audio_last_monotonic_ms", "audio_last_nonzero_monotonic_ms"):
                        last = record.get(key)
                        require(type(last) is int and 0 <= relative - last <= 750,
                                f"RTSP finite-hold precondition has stale {key}")
                    hold_preconditions.append(point)
            if type(generation) is int and 1 <= generation <= expected_source_generations:
                window = generation_windows.setdefault(generation, {
                    "firstMonotonicNs": event_ns,
                    "lastMonotonicNs": event_ns,
                    "labelCount": 0,
                    "maxMediaTime": None,
                    "minMediaTime": None,
                })
                window["firstMonotonicNs"] = min(window["firstMonotonicNs"], event_ns)
            label = record.get("label")
            if label in live_checkpoint_labels:
                expected_generation = (
                    generation
                    if case_id == "rtsp-live-tcp-drain-diagnostics"
                    else int(label[-1])
                )
                require(type(expected_generation) is int
                        and 1 <= expected_generation <= expected_source_generations,
                        "RTSP live checkpoint source generation is invalid")
                require(generation == expected_generation, "RTSP live checkpoint generation differs")
                require(record.get("type") == "snapshot" and record.get("action") == "snapshot",
                        "RTSP live checkpoint is not a snapshot")
                require(record.get("duration") is None, "RTSP live source exposed a finite duration")
                require(record.get("has_audio") is True and record.get("has_video") is True,
                        "RTSP live checkpoint lacks simultaneous A/V")
                require(record.get("paused") is False and record.get("seeking") is False
                        and record.get("ended") is False, "RTSP live checkpoint is not advancing live state")
                if args.instrumentation == "audio-monitor":
                    require(record.get("audio_monitor_enabled") is True,
                            "RTSP live qualification lacks decoded-audio instrumentation")
                    require(type(record.get("audio_samples_generation")) is int
                            and record["audio_samples_generation"] >= 10,
                            "RTSP live checkpoint lacks delivered audio samples")
                    require(type(record.get("audio_bytes_generation")) is int
                            and record["audio_bytes_generation"] >= 4096,
                            "RTSP live checkpoint lacks delivered audio bytes")
                    require(type(record.get("audio_nonzero_units_generation")) is int
                            and record["audio_nonzero_units_generation"] >= 20,
                            "RTSP live checkpoint lacks nonzero PCM")
                    for key in ("audio_last_monotonic_ms", "audio_last_nonzero_monotonic_ms"):
                        last = record.get(key)
                        require(type(last) is int and 0 <= relative - last <= 750,
                                f"RTSP live checkpoint has stale {key}")
                else:
                    require(record.get("audio_monitor_enabled") is False,
                            "RTSP endpoint qualification unexpectedly changed driver topology")
                media_time = record.get("time")
                require(type(media_time) in (int, float) and not isinstance(media_time, bool)
                        and math.isfinite(float(media_time)), "RTSP live checkpoint has no finite media clock")
                window = generation_windows[expected_generation]
                window["labelCount"] += 1
                # The transport claim covers the declared live-observation
                # checkpoints.  Shutdown legitimately closes the final RTSP
                # socket before the driver writes its shutdown/result state,
                # so those records must not extend the media-carrying window.
                window["lastMonotonicNs"] = max(window["lastMonotonicNs"], event_ns)
                window["minMediaTime"] = (float(media_time) if window["minMediaTime"] is None
                                            else min(window["minMediaTime"], float(media_time)))
                window["maxMediaTime"] = (float(media_time) if window["maxMediaTime"] is None
                                            else max(window["maxMediaTime"], float(media_time)))
                if relay_mode == "server-to-client-finite-hold":
                    hold_recovery_points.append(point)
            if record.get("type") == "result":
                saw_result = True
                result_ns = event_ns
                result_point = point

        if done_before:
            break

    require(producer_done, "producer completion was not observed before watcher timeout")
    require(not pending.strip(), "driver JSONL ended with a partial record")
    require(saw_result and result_ns is not None, "driver result was not observed")
    require(not forbidden_seek, "live RTSP run issued or completed an ordinary seek")
    if relay_mode == "server-to-client-blackhole":
        require(not blackhole_forbidden, "RTSP blackhole run emitted a forbidden action or event")
        require(blackhole_source_generations == {1},
                "RTSP blackhole source generations differ from [1]")

        def one(mapping: dict[str, list[tuple[int, int]]], key: str) -> tuple[int, int]:
            values = mapping.get(key, [])
            require(len(values) == 1, f"RTSP blackhole {key} timestamp is not unique")
            return values[0]

        require(result_point is not None, "RTSP blackhole result point is absent")
        precondition = one(blackhole_markers, "rtsp-blackhole-precondition")
        pause_begin = one(blackhole_markers, "rtsp-blackhole-before-pause")
        resume_begin = one(blackhole_markers, "rtsp-blackhole-before-resume")
        shutdown_begin = one(blackhole_markers, "rtsp-blackhole-before-shutdown")
        pause_return = one(blackhole_actions, "pause")
        shutdown_return = one(blackhole_actions, "shutdown")
        pause_event = one(blackhole_events, "PAUSE")
        pause_wait = one(blackhole_waits, "PAUSE")
        playing_events = blackhole_events.get("PLAYING", [])
        playing_waits = blackhole_waits.get("PLAYING", [])
        play_returns = blackhole_actions.get("play", [])
        require(len(playing_events) == 2 and len(playing_waits) == 2 and len(play_returns) == 2,
                "RTSP blackhole initial/resume PLAYING timeline is not exact")
        resume_return = next(
            (value for value in play_returns if value >= resume_begin), (-1, -1)
        )
        resume_event = next(
            (value for value in playing_events if value >= resume_begin), (-1, -1)
        )
        resume_wait = next(
            (value for value in playing_waits if value >= resume_begin), (-1, -1)
        )

        require(precondition < pause_begin <= min(pause_return, pause_event)
                and max(pause_return, pause_event) <= pause_wait < resume_begin,
                "RTSP blackhole pause API/event ordering differs")
        require(resume_begin <= min(resume_return, resume_event)
                and max(resume_return, resume_event) <= resume_wait < shutdown_begin,
                "RTSP blackhole resume API/event ordering differs")
        require(shutdown_begin <= shutdown_return <= result_point,
                "RTSP blackhole shutdown API/result ordering differs")
        require(pause_wait[0] - pause_begin[0]
                <= BLACKHOLE_PAUSE_RESUME_LIMIT_MS * 1_000_000,
                "RTSP blackhole pause exceeded one second")
        require(resume_wait[0] - resume_begin[0]
                <= BLACKHOLE_PAUSE_RESUME_LIMIT_MS * 1_000_000,
                "RTSP blackhole resume exceeded one second")
        require(result_point[0] - shutdown_begin[0]
                <= BLACKHOLE_SHUTDOWN_LIMIT_MS * 1_000_000,
                "RTSP blackhole shutdown exceeded five seconds")
        atomic_json(args.output, {
            "apiWindows": [
                {
                    "api": "pause",
                    "beginMonotonicNs": pause_begin[0],
                    "beginSeq": pause_begin[1],
                    "eventMonotonicNs": pause_event[0],
                    "eventSeq": pause_event[1],
                    "maxLatencyMs": BLACKHOLE_PAUSE_RESUME_LIMIT_MS,
                    "returnMonotonicNs": pause_return[0],
                    "returnSeq": pause_return[1],
                    "waitCompletedMonotonicNs": pause_wait[0],
                    "waitCompletedSeq": pause_wait[1],
                },
                {
                    "api": "resume",
                    "beginMonotonicNs": resume_begin[0],
                    "beginSeq": resume_begin[1],
                    "eventMonotonicNs": resume_event[0],
                    "eventSeq": resume_event[1],
                    "maxLatencyMs": BLACKHOLE_PAUSE_RESUME_LIMIT_MS,
                    "returnMonotonicNs": resume_return[0],
                    "returnSeq": resume_return[1],
                    "waitCompletedMonotonicNs": resume_wait[0],
                    "waitCompletedSeq": resume_wait[1],
                },
                {
                    "api": "shutdown",
                    "beginMonotonicNs": shutdown_begin[0],
                    "beginSeq": shutdown_begin[1],
                    "eventMonotonicNs": None,
                    "eventSeq": None,
                    "maxLatencyMs": BLACKHOLE_SHUTDOWN_LIMIT_MS,
                    "returnMonotonicNs": shutdown_return[0],
                    "returnSeq": shutdown_return[1],
                    "waitCompletedMonotonicNs": result_point[0],
                    "waitCompletedSeq": result_point[1],
                },
            ],
            "clockBasis": CLOCK_BASIS,
            "preconditionMonotonicNs": precondition[0],
            "preconditionSeq": precondition[1],
            "relayMode": relay_mode,
            "resultMonotonicNs": result_point[0],
            "resultSeq": result_point[1],
            "schema": SCHEMA,
            "sourceGenerations": 1,
        })
        return
    if relay_mode == "server-to-client-finite-hold":
        require(not hold_forbidden, "RTSP finite-hold run emitted a forbidden action or event")
        require(hold_source_generations == {1},
                "RTSP finite-hold source generations differ from [1]")
        require(len(hold_preconditions) == 1,
                "RTSP finite-hold precondition timestamp is not unique")
        require(len(hold_recovery_points) == 4,
                "RTSP finite-hold recovery timestamps differ")
        require(set(generation_windows) == {1},
                "RTSP finite-hold source generations differ from [1]")
        window = generation_windows[1]
        require(window["labelCount"] == 4,
                "RTSP finite-hold recovery checkpoint count differs")
        require(window["maxMediaTime"] - window["minMediaTime"] >= 2.5,
                "RTSP finite-hold playback did not recover and advance by 2.5 seconds")
        require(result_point is not None and hold_preconditions[0] < result_point,
                "RTSP finite-hold result does not follow its precondition")
        window["firstMonotonicNs"] = hold_recovery_points[0][0]
        atomic_json(args.output, {
            "clockBasis": CLOCK_BASIS,
            "generationWindows": [{"generation": 1, **window}],
            "holdPreconditionMonotonicNs": hold_preconditions[0][0],
            "holdPreconditionSeq": hold_preconditions[0][1],
            "maxObservationLagMs": max_lag,
            "resultMonotonicNs": result_ns,
            "schema": SCHEMA,
            "sourceGenerations": 1,
        })
        return
    expected_generations = set(range(1, expected_source_generations + 1))
    require(set(generation_windows) == expected_generations,
            f"RTSP source generations differ from {sorted(expected_generations)}")
    output_windows = []
    for generation in range(1, expected_source_generations + 1):
        window = generation_windows[generation]
        require(window["labelCount"] == 4, f"RTSP generation {generation} checkpoint count differs")
        require(window["maxMediaTime"] - window["minMediaTime"] >= 2.5,
                f"RTSP generation {generation} did not advance by 2.5 seconds")
        output_windows.append({"generation": generation, **window})
    atomic_json(args.output, {
        "clockBasis": CLOCK_BASIS,
        "generationWindows": output_windows,
        "maxObservationLagMs": max_lag,
        "resultMonotonicNs": result_ns,
        "schema": SCHEMA,
        "sourceGenerations": expected_source_generations,
    })


def load_connection_log(path: Path, config: dict[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    payload = read_bytes(path, config["maxLogBytes"], "RTSP connection log")
    require(payload.endswith(b"\n") and b"\x00" not in payload, "RTSP connection log is incomplete")
    records = []
    for line in payload.splitlines():
        keys = (
            FINITE_HOLD_CONNECTION_KEYS
            if config["relayMode"] == "server-to-client-finite-hold"
            else CONNECTION_KEYS
        )
        record = exact_object(json.loads(line.decode("ascii")), keys, "RTSP connection record")
        require(record["schema"] == SCHEMA and record["case_id"] == config["caseId"]
                and record["clock_basis"] == CLOCK_BASIS, "RTSP connection record identity differs")
        for key in ("connection_seq", "started_monotonic_ns", "finished_monotonic_ns",
                    "client_to_server_bytes", "client_to_server_bytes_after_blackhole",
                    "server_to_client_bytes", "blackhole_after_server_bytes",
                    "interleaved_tcp_setups"):
            require(type(record[key]) is int and record[key] >= 0, f"RTSP connection {key} is invalid")
        require(record["finished_monotonic_ns"] >= record["started_monotonic_ns"],
                "RTSP connection monotonic interval regressed")
        require(record["interleaved_tcp_setups"] <= 2,
                "RTSP connection SETUP count exceeds its bounded observer")
        require(record["status"] in {"eof", "service-stop"}, "RTSP relay reported an error")
        require(record["terminal_reason"] in {
            "client-eof", "upstream-eof", "client-reset", "upstream-reset", "service-stop",
        }, "RTSP relay terminal reason is invalid")
        require(record["relay_mode"] == config["relayMode"]
                and record["blackhole_after_server_bytes"] == config["blackholeAfterServerBytes"],
                "RTSP connection relay policy differs from config")
        require(record["blackhole_started_monotonic_ns"] is None
                or (type(record["blackhole_started_monotonic_ns"]) is int
                    and record["started_monotonic_ns"]
                    <= record["blackhole_started_monotonic_ns"]
                    <= record["finished_monotonic_ns"]),
                "RTSP impairment timestamp is invalid")
        hold_resumed = record.get("hold_resumed_monotonic_ns")
        require(hold_resumed is None
                or (type(hold_resumed) is int
                    and record["started_monotonic_ns"]
                    <= hold_resumed
                    <= record["finished_monotonic_ns"]),
                "RTSP finite-hold resume timestamp is invalid")
        require(record["client_to_server_bytes_after_blackhole"]
                <= record["client_to_server_bytes"],
                "RTSP post-blackhole client byte count exceeds its total")
        if record["blackhole_started_monotonic_ns"] is not None:
            if record["relay_mode"] == "server-to-client-blackhole":
                require(record["server_to_client_bytes"]
                        == record["blackhole_after_server_bytes"]
                        and hold_resumed is None,
                        "RTSP blackhole did not stop at its exact forwarded-byte threshold")
            else:
                require(type(hold_resumed) is int,
                        "RTSP finite hold has no resumed-delivery timestamp")
                hold_elapsed_ns = hold_resumed - record["blackhole_started_monotonic_ns"]
                expected_hold_ns = config["holdDurationMs"] * 1_000_000
                require(record["relay_mode"] == "server-to-client-finite-hold"
                        and record["server_to_client_bytes"]
                        > record["blackhole_after_server_bytes"]
                        and record["blackhole_started_monotonic_ns"]
                        < hold_resumed
                        and expected_hold_ns <= hold_elapsed_ns
                        <= expected_hold_ns + MAX_OBSERVATION_LAG_MS * 1_000_000,
                        "RTSP finite hold did not resume after its exact trigger threshold")
        else:
            require(hold_resumed is None,
                    "RTSP connection resumed without an impairment")
        if record["relay_mode"] == "forward":
            require(record["blackhole_started_monotonic_ns"] is None
                    and hold_resumed is None
                    and record["client_to_server_bytes_after_blackhole"] == 0,
                    "forward RTSP relay recorded blackhole state")
        require((record["status"] == "service-stop") == (record["terminal_reason"] == "service-stop"),
                "RTSP relay status and terminal reason disagree")
        require(record["client_to_server_bytes"] <= config["maxBytesPerDirection"]
                and record["server_to_client_bytes"] <= config["maxBytesPerDirection"],
                "RTSP connection exceeded its byte budget")
        records.append(record)
    records.sort(key=lambda item: item["connection_seq"])
    require([item["connection_seq"] for item in records] == list(range(1, len(records) + 1)),
            "RTSP connection sequence is not contiguous")
    return payload, records


def command_score(args: argparse.Namespace) -> None:
    config = validate_config(read_json(args.config))
    payload, records = load_connection_log(args.http_log, config)
    completion_payload = read_bytes(args.service_completion, 16 * 1024, "RTSP completion marker")
    completion = exact_object(json.loads(completion_payload.decode("ascii")), COMPLETION_KEYS,
                              "RTSP completion marker")
    require(completion_payload == canonical_bytes(completion), "RTSP completion marker is not canonical")
    require(completion["schema"] == SCHEMA and completion["status"] == "complete"
            and completion["caseId"] == config["caseId"] and completion["clockBasis"] == CLOCK_BASIS,
            "RTSP completion identity differs")
    require(completion["logBytes"] == len(payload)
            and completion["logSha256"] == hashlib.sha256(payload).hexdigest(),
            "RTSP completion does not seal its connection log")
    require(completion["acceptedConnectionCount"] == len(records)
            and completion["terminalRecordCount"] == len(records),
            "RTSP completion connection counts differ")
    require(completion["rejectedConnectionCount"] == 0, "RTSP gate rejected a connection")
    require(config["minObservedConnections"] <= len(records) <= config["maxObservedConnections"],
            "RTSP observed connection count is outside its oracle bounds")
    for key in ("ffmpegSha256", "ffprobeSha256", "fixtureSha256", "mediamtxSha256"):
        require(type(completion[key]) is str and SHA256_RE.fullmatch(completion[key]),
                f"RTSP completion {key} is invalid")
    require(completion["ffmpegSha256"] == sha256_file(Path(config["ffmpegBin"]))
            and completion["ffprobeSha256"] == sha256_file(Path(config["ffprobeBin"]))
            and completion["mediamtxSha256"] == sha256_file(Path(config["mediamtxBin"])),
            "RTSP native tool identity differs")
    require(completion["fixtureSha256"] == config["fixtureSha256"],
            "RTSP completion fixture identity differs")
    require(type(completion["nativeVideoFramesMinimum"]) is int
            and completion["nativeVideoFramesMinimum"] >= 30,
            "RTSP native video proof is insufficient")
    require(type(completion["nativeAudioPcmBytes"]) is int
            and completion["nativeAudioPcmBytes"] >= config["nativeProbeSeconds"] * 48000 * 3,
            "RTSP native PCM proof is insufficient")
    require(type(completion["nativeAudioNonzeroUnits"]) is int
            and completion["nativeAudioNonzeroUnits"] >= 1000,
            "RTSP native nonzero-PCM proof is insufficient")

    watch_document = read_json(args.watch)
    if config["relayMode"] == "server-to-client-blackhole":
        watch = exact_object(watch_document, BLACKHOLE_WATCH_KEYS, "RTSP blackhole watcher")
        require(watch["schema"] == SCHEMA and watch["clockBasis"] == CLOCK_BASIS
                and watch["relayMode"] == config["relayMode"]
                and watch["sourceGenerations"] == 1,
                "RTSP blackhole watcher identity differs")
        require(type(watch["preconditionMonotonicNs"]) is int
                and type(watch["preconditionSeq"]) is int
                and type(watch["resultMonotonicNs"]) is int
                and type(watch["resultSeq"]) is int,
                "RTSP blackhole watcher point fields are invalid")
        precondition_point = (watch["preconditionMonotonicNs"], watch["preconditionSeq"])
        result_point = (watch["resultMonotonicNs"], watch["resultSeq"])
        require(0 <= precondition_point[0] and precondition_point[1] > 0
                and result_point[1] > 0 and precondition_point < result_point,
                "RTSP blackhole watcher interval is invalid")
        api_windows = watch["apiWindows"]
        require(type(api_windows) is list and len(api_windows) == 3,
                "RTSP blackhole API windows differ")
        expected_apis = (
            ("pause", BLACKHOLE_PAUSE_RESUME_LIMIT_MS),
            ("resume", BLACKHOLE_PAUSE_RESUME_LIMIT_MS),
            ("shutdown", BLACKHOLE_SHUTDOWN_LIMIT_MS),
        )
        for window, (api, limit_ms) in zip(api_windows, expected_apis, strict=True):
            window = exact_object(window, BLACKHOLE_API_WINDOW_KEYS,
                                  f"RTSP blackhole {api} API window")
            require(window["api"] == api and window["maxLatencyMs"] == limit_ms,
                    f"RTSP blackhole {api} API identity differs")
            for key in ("beginMonotonicNs", "returnMonotonicNs", "waitCompletedMonotonicNs"):
                require(type(window[key]) is int and window[key] >= 0,
                        f"RTSP blackhole {api} {key} is invalid")
            for key in ("beginSeq", "returnSeq", "waitCompletedSeq"):
                require(type(window[key]) is int and window[key] > 0,
                        f"RTSP blackhole {api} {key} is invalid")
            begin_point = (window["beginMonotonicNs"], window["beginSeq"])
            return_point = (window["returnMonotonicNs"], window["returnSeq"])
            wait_point = (window["waitCompletedMonotonicNs"], window["waitCompletedSeq"])
            require(begin_point <= return_point <= wait_point
                    and window["waitCompletedMonotonicNs"] - window["beginMonotonicNs"]
                    <= limit_ms * 1_000_000,
                    f"RTSP blackhole {api} exceeded its bounded API window")
            if api == "shutdown":
                require(window["eventMonotonicNs"] is None
                        and window["eventSeq"] is None
                        and wait_point == result_point,
                        "RTSP blackhole shutdown result boundary differs")
            else:
                require(type(window["eventMonotonicNs"]) is int
                        and type(window["eventSeq"]) is int and window["eventSeq"] > 0
                        and begin_point
                        <= (window["eventMonotonicNs"], window["eventSeq"])
                        <= wait_point,
                        f"RTSP blackhole {api} event boundary differs")
        require(
            (api_windows[0]["waitCompletedMonotonicNs"], api_windows[0]["waitCompletedSeq"])
            < (api_windows[1]["beginMonotonicNs"], api_windows[1]["beginSeq"])
            and (api_windows[1]["waitCompletedMonotonicNs"], api_windows[1]["waitCompletedSeq"])
            < (api_windows[2]["beginMonotonicNs"], api_windows[2]["beginSeq"]),
                "RTSP blackhole API windows overlap or regress")

        blackholed = [
            record for record in records if record["blackhole_started_monotonic_ns"] is not None
        ]
        require(len(blackholed) == 1, "RTSP run did not produce exactly one blackholed connection")
        connection = blackholed[0]
        blackhole_ns = connection["blackhole_started_monotonic_ns"]
        require(connection["server_to_client_bytes"] == config["blackholeAfterServerBytes"],
                "RTSP blackhole byte cutoff differs")
        require(connection["status"] == "eof" and connection["terminal_reason"] == "client-eof",
                "RTSP blackhole socket did not end with client-driven EOF")
        require(connection["client_to_server_bytes"] >= 128,
                "RTSP blackhole connection lacks client control traffic")
        require(connection["interleaved_tcp_setups"] >= 2,
                "RTSP blackhole connection lacks two interleaved-TCP SETUP requests")
        require(watch["preconditionMonotonicNs"] < blackhole_ns
                <= api_windows[0]["beginMonotonicNs"],
                "RTSP blackhole timestamp does not precede the pause API window")
        require(connection["started_monotonic_ns"] <= watch["preconditionMonotonicNs"]
                and api_windows[2]["beginMonotonicNs"] <= connection["finished_monotonic_ns"]
                <= watch["resultMonotonicNs"] + MAX_OBSERVATION_LAG_MS * 1_000_000,
                "RTSP blackhole socket lifetime does not span shutdown")
        atomic_json(args.output, {
            "blackholeAfterServerBytes": config["blackholeAfterServerBytes"],
            "blackholeStartedMonotonicNs": blackhole_ns,
            "clientDrivenEof": True,
            "clockBasis": CLOCK_BASIS,
            "nativeAudioNonzeroUnits": completion["nativeAudioNonzeroUnits"],
            "nativeAudioPcmBytes": completion["nativeAudioPcmBytes"],
            "nativeVideoFramesMinimum": completion["nativeVideoFramesMinimum"],
            "observedConnections": len(records),
            "pauseLatencyMs": (
                api_windows[0]["waitCompletedMonotonicNs"]
                - api_windows[0]["beginMonotonicNs"]
            ) // 1_000_000,
            "relayMode": config["relayMode"],
            "resumeLatencyMs": (
                api_windows[1]["waitCompletedMonotonicNs"]
                - api_windows[1]["beginMonotonicNs"]
            ) // 1_000_000,
            "schema": SCHEMA,
            "shutdownLatencyMs": (
                api_windows[2]["waitCompletedMonotonicNs"]
                - api_windows[2]["beginMonotonicNs"]
            ) // 1_000_000,
            "status": "passed",
            "transport": (
                f'literal-loopback-{config["sourceScheme"]}-interleaved-tcp-blackhole'
            ),
        })
        return

    if config["relayMode"] == "server-to-client-finite-hold":
        watch = exact_object(watch_document, FINITE_HOLD_WATCH_KEYS,
                             "RTSP finite-hold watcher")
        require(watch["schema"] == SCHEMA and watch["clockBasis"] == CLOCK_BASIS
                and watch["sourceGenerations"] == 1,
                "RTSP finite-hold watcher identity differs")
        require(type(watch["holdPreconditionMonotonicNs"]) is int
                and type(watch["holdPreconditionSeq"]) is int
                and type(watch["resultMonotonicNs"]) is int,
                "RTSP finite-hold watcher point fields are invalid")
        windows = watch["generationWindows"]
        require(type(windows) is list and len(windows) == 1,
                "RTSP finite-hold watcher window count differs")
        window = windows[0]
        require(type(window) is dict and set(window) == {
            "firstMonotonicNs", "generation", "labelCount", "lastMonotonicNs",
            "maxMediaTime", "minMediaTime",
        }, "RTSP finite-hold watcher window keys differ")
        require(window["generation"] == 1 and window["labelCount"] == 4
                and type(window["minMediaTime"]) in (int, float)
                and type(window["maxMediaTime"]) in (int, float)
                and window["maxMediaTime"] - window["minMediaTime"] >= 2.5,
                "RTSP finite-hold recovery window did not advance")
        impaired = [
            record for record in records
            if record["blackhole_started_monotonic_ns"] is not None
        ]
        require(len(impaired) == 1,
                "RTSP run did not produce exactly one finite-hold connection")
        connection = impaired[0]
        hold_ns = connection["blackhole_started_monotonic_ns"]
        resume_ns = connection["hold_resumed_monotonic_ns"]
        require(connection["server_to_client_bytes"] > config["blackholeAfterServerBytes"],
                "RTSP finite-hold connection did not resume server delivery")
        require(connection["client_to_server_bytes"] >= 128
                and connection["interleaved_tcp_setups"] >= 2,
                "RTSP finite-hold connection lacks interleaved-TCP control traffic")
        require(connection["terminal_reason"] in {"client-eof", "client-reset"},
                "RTSP finite-hold socket did not end from the client side")
        require(connection["started_monotonic_ns"]
                <= watch["holdPreconditionMonotonicNs"] < hold_ns,
                "RTSP finite hold did not begin after its live precondition")
        require(type(resume_ns) is int and hold_ns < resume_ns
                <= window["firstMonotonicNs"] <= window["lastMonotonicNs"]
                <= connection["finished_monotonic_ns"],
                "RTSP finite-hold recovery was not observed after delivery resumed")
        carrying = [
            record for record in records
            if record["client_to_server_bytes"] >= 128
            and record["server_to_client_bytes"] >= 4096
            and record["interleaved_tcp_setups"] >= 2
        ]
        require(len(carrying) == 1
                and carrying[0]["connection_seq"] == connection["connection_seq"],
                "RTSP finite-hold recovery was not isolated to the impaired connection")
        require(connection["finished_monotonic_ns"]
                <= watch["resultMonotonicNs"] + MAX_OBSERVATION_LAG_MS * 1_000_000,
                "RTSP finite-hold socket lifetime exceeds the driver result boundary")
        atomic_json(args.output, {
            "clockBasis": CLOCK_BASIS,
            "holdAfterServerBytes": config["blackholeAfterServerBytes"],
            "holdDurationMs": config["holdDurationMs"],
            "holdResumedMonotonicNs": resume_ns,
            "holdStartedMonotonicNs": hold_ns,
            "nativeAudioNonzeroUnits": completion["nativeAudioNonzeroUnits"],
            "nativeAudioPcmBytes": completion["nativeAudioPcmBytes"],
            "nativeVideoFramesMinimum": completion["nativeVideoFramesMinimum"],
            "observedConnections": len(records),
            "relayMode": config["relayMode"],
            "resumedServerToClientBytes": connection["server_to_client_bytes"],
            "schema": SCHEMA,
            "status": "passed",
            "transport": (
                f'literal-loopback-{config["sourceScheme"]}-interleaved-tcp-finite-hold'
            ),
        })
        return

    watch = exact_object(watch_document, WATCH_KEYS, "RTSP watcher")
    expected_source_generations = (
        10 if config["caseId"] == "rtsp-live-tcp-drain-diagnostics" else 3
    )
    require(watch["schema"] == SCHEMA and watch["clockBasis"] == CLOCK_BASIS
            and watch["sourceGenerations"] == expected_source_generations,
            "RTSP watcher identity differs")
    require(type(watch["maxObservationLagMs"]) is int
            and 0 <= watch["maxObservationLagMs"] <= MAX_OBSERVATION_LAG_MS,
            "RTSP watcher calibration is invalid")
    windows = watch["generationWindows"]
    require(type(windows) is list and len(windows) == expected_source_generations,
            "RTSP watcher generation windows differ")
    candidates = [record for record in records
                  if record["client_to_server_bytes"] >= 128 and record["server_to_client_bytes"] >= 4096]
    used: set[int] = set()
    for expected_generation, window in enumerate(windows, 1):
        require(type(window) is dict and set(window) == {
            "firstMonotonicNs", "generation", "labelCount", "lastMonotonicNs",
            "maxMediaTime", "minMediaTime",
        }, "RTSP watcher generation keys differ")
        require(window["generation"] == expected_generation and window["labelCount"] == 4,
                "RTSP watcher generation identity differs")
        match = next((record for record in candidates
                      if record["connection_seq"] not in used
                      and record["started_monotonic_ns"] >= window["firstMonotonicNs"] - 2_000_000_000
                      and record["started_monotonic_ns"] <= window["lastMonotonicNs"]
                      and record["finished_monotonic_ns"] >= window["lastMonotonicNs"]), None)
        require(match is not None, f"RTSP generation {expected_generation} has no distinct live TCP session")
        require(match["interleaved_tcp_setups"] >= 2,
                f"RTSP generation {expected_generation} lacks two interleaved-TCP SETUP requests")
        used.add(match["connection_seq"])
    require(type(watch["resultMonotonicNs"]) is int and watch["resultMonotonicNs"] >= 0,
            "RTSP watcher result time is invalid")
    atomic_json(args.output, {
        "clockBasis": CLOCK_BASIS,
        "matchedGenerationConnections": len(used),
        "nativeAudioNonzeroUnits": completion["nativeAudioNonzeroUnits"],
        "nativeAudioPcmBytes": completion["nativeAudioPcmBytes"],
        "nativeVideoFramesMinimum": completion["nativeVideoFramesMinimum"],
        "observedConnections": len(records),
        "schema": SCHEMA,
        "status": "passed",
        "transport": f'literal-loopback-{config["sourceScheme"]}-interleaved-tcp',
    })


def add_case_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--fixture-manifest", required=True, type=Path)
    parser.add_argument("--scenario", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    add_case_inputs(check)
    check.set_defaults(function=command_check)
    serve = commands.add_parser("serve")
    add_case_inputs(serve)
    serve.add_argument("--log", required=True, type=Path)
    serve.add_argument("--completion-marker", required=True, type=Path)
    serve.add_argument("--fixture-script", type=Path, help=argparse.SUPPRESS)
    serve.set_defaults(function=command_serve)
    watch = commands.add_parser("watch")
    watch.add_argument("--driver-json", required=True, type=Path)
    watch.add_argument("--output", required=True, type=Path)
    watch.add_argument("--producer-done", required=True, type=Path)
    watch.add_argument("--timeout-seconds", required=True, type=float)
    watch.add_argument("--config", type=Path)
    watch.add_argument("--instrumentation", required=True,
                       choices=("audio-monitor", "endpoint-monitor"))
    watch.set_defaults(function=command_watch)
    score = commands.add_parser("score")
    score.add_argument("--http-log", required=True, type=Path,
                       help="generic runner name for the RTSP connection JSONL")
    score.add_argument("--service-completion", required=True, type=Path)
    score.add_argument("--watch", required=True, type=Path)
    score.add_argument("--config", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.set_defaults(function=command_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        require(args.command != "watch" or 1 <= args.timeout_seconds <= 600,
                "watch timeout is outside [1, 600]")
        args.function(args)
    except (RTSPCaseError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"RTSP live case: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
