#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Host-safe healthy-HLS service and evidence oracle for MediaEngine tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
import time
from typing import Any


SCHEMA = 1
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_DRIVER_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_COMPLETION_BYTES = 4096
MAX_OBSERVATION_LAG_MS = 250
MIN_OBSERVATION_LAG_MS = -2
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMON_CONFIG_KEYS = {
    "adapterSha256",
    "autoAdvance",
    "bind",
    "caseId",
    "controlToken",
    "faultMode",
    "initialGeneration",
    "maxConcurrent",
    "maxErrorResponses",
    "maxLogBytes",
    "maxRequests",
    "maxStartupMs",
    "minPlaybackSpan",
    "port",
    "publisherSha256",
    "requiredInstrumentation",
    "schema",
    "service",
    "source",
}
MUX_CONFIG_KEYS = COMMON_CONFIG_KEYS | {
    "minMuxPlaylistGenerations",
    "minMuxPlaylistRequests",
    "minMuxSegmentRequests",
}
SEPARATE_CONFIG_KEYS = COMMON_CONFIG_KEYS | {
    "minAudioPlaylistRequests",
    "minAudioSegmentRequests",
    "minMasterRequests",
    "minPairedPlaylistGenerations",
    "minVideoPlaylistRequests",
    "minVideoSegmentRequests",
}
COMPLETION_KEYS = {
    "allocatedRequestCount",
    "caseId",
    "clockBasis",
    "faultMode",
    "finalGeneration",
    "logBytes",
    "logSha256",
    "rejectedConnectionCount",
    "schema",
    "status",
    "terminalRecordCount",
}
WATCH_KEYS = {
    "clockBasis",
    "firstPlaybackMonotonicNs",
    "firstTime",
    "lastPlaybackObservedMonotonicNs",
    "lastPlaybackMonotonicNs",
    "lastTime",
    "maxObservationLagMs",
    "playbackSpan",
    "playingMonotonicNs",
    "schema",
    "snapshotCount",
    "sourceGeneration",
}

HERE = Path(__file__).resolve().parent
LIVE_FIXTURE_PATH = Path(os.environ.get(
    "RTSP_LIVE_FIXTURE_PATH",
    HERE.parent / "media-engine-stress" / "live-fixture" / "live_fixture.py",
)).resolve()


class LiveCaseError(ValueError):
    """Raised when a live case or its evidence violates the contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveCaseError(message)


def _load_live_fixture() -> Any:
    require(LIVE_FIXTURE_PATH.is_file() and not LIVE_FIXTURE_PATH.is_symlink(),
            "live fixture implementation is absent or is a symlink")
    spec = importlib.util.spec_from_file_location("rtsp_live_fixture", LIVE_FIXTURE_PATH)
    require(spec is not None and spec.loader is not None, "cannot load live fixture implementation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live_fixture = _load_live_fixture()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_bytes(path: Path, maximum: int, label: str, *, nonempty: bool = True) -> bytes:
    require(path.is_file() and not path.is_symlink(), f"{label} is not a regular nonsymlink file")
    size = path.stat().st_size
    require((not nonempty or size > 0) and size <= maximum, f"{label} size is outside bounds")
    return path.read_bytes()


def read_json(path: Path, maximum: int, label: str, *, canonical: bool = False) -> Any:
    payload = read_bytes(path, maximum, label)
    require(b"\x00" not in payload, f"{label} contains a NUL byte")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiveCaseError(f"invalid {label}: {error}") from error
    if canonical:
        require(payload == canonical_bytes(value), f"{label} is not canonical JSON")
    return value


def atomic_exclusive_json(path: Path, value: Any) -> None:
    require(not path.exists(), "refusing to replace an existing output")
    require(path.parent.is_dir() and not path.parent.is_symlink(), "output parent is unsafe")
    payload = canonical_bytes(value)
    require(len(payload) <= MAX_DOCUMENT_BYTES, "output exceeds its size bound")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def positive_int(value: Any, label: str, maximum: int) -> int:
    require(type(value) is int and 1 <= value <= maximum, f"{label} is outside [1, {maximum}]")
    return value


def finite_number(value: Any, label: str, lower: float, upper: float) -> float:
    require(type(value) in (int, float) and not isinstance(value, bool), f"{label} must be numeric")
    number = float(value)
    require(math.isfinite(number) and lower <= number <= upper,
            f"{label} is outside [{lower}, {upper}]")
    return number


def validate_config(value: Any, *, verify_sources: bool = True) -> dict[str, Any]:
    require(type(value) is dict, "live HLS config is not an object")
    source = value.get("source")
    require(source in {"mux", "separate"}, "live HLS source must be mux or separate")
    expected_keys = MUX_CONFIG_KEYS if source == "mux" else SEPARATE_CONFIG_KEYS
    require(set(value) == expected_keys, "live HLS config keys differ")
    require(type(value["schema"]) is int and value["schema"] == SCHEMA, "unsupported config schema")
    require(value["service"] == "live-hls-v1", "unexpected fixture service")
    require(value["bind"] == "127.0.0.1", "live HLS must bind literal IPv4 loopback")
    positive_int(value["port"], "port", 65535)
    require(value["port"] != 9, "fixture port collides with the closed proxy port")
    require(type(value["caseId"]) is str and CASE_ID_RE.fullmatch(value["caseId"]), "invalid caseId")
    require(value["faultMode"] == "normal", "the healthy case cannot inject a fault")
    require(value["autoAdvance"] is True, "the healthy case must auto-advance")
    require(type(value["initialGeneration"]) is int and value["initialGeneration"] == 0,
            "the healthy case must start at generation zero")
    require(value["requiredInstrumentation"] in {"audio-monitor", "endpoint-monitor"},
            "the healthy case must require a reviewed audio observation mode")
    require(type(value["controlToken"]) is str
            and live_fixture.TOKEN_RE.fullmatch(value["controlToken"]), "invalid control token")
    max_requests = positive_int(value["maxRequests"], "maxRequests", 4096)
    positive_int(value["maxConcurrent"], "maxConcurrent", 64)
    max_log_bytes = positive_int(value["maxLogBytes"], "maxLogBytes", 64 * 1024 * 1024)
    require(max_log_bytes >= max_requests * 256, "maxLogBytes does not reserve per-request evidence")
    require(type(value["maxErrorResponses"]) is int and value["maxErrorResponses"] == 0,
            "the healthy case must reject every HTTP error")
    positive_int(value["maxStartupMs"], "maxStartupMs", 60_000)
    if source == "mux":
        positive_int(value["minMuxPlaylistGenerations"], "minMuxPlaylistGenerations", 64)
        minima = (
            positive_int(value["minMuxPlaylistRequests"], "minMuxPlaylistRequests", max_requests),
            positive_int(value["minMuxSegmentRequests"], "minMuxSegmentRequests", max_requests),
        )
    else:
        positive_int(
            value["minPairedPlaylistGenerations"], "minPairedPlaylistGenerations", 64
        )
        minima = tuple(
            positive_int(value[field], field, max_requests)
            for field in (
                "minMasterRequests",
                "minAudioPlaylistRequests",
                "minAudioSegmentRequests",
                "minVideoPlaylistRequests",
                "minVideoSegmentRequests",
            )
        )
    require(sum(minima) <= max_requests, "minimum live request evidence exceeds the request cap")
    finite_number(value["minPlaybackSpan"], "minPlaybackSpan", 1.0, 60.0)
    for field in ("adapterSha256", "publisherSha256"):
        require(type(value[field]) is str and SHA256_RE.fullmatch(value[field]), f"invalid {field}")
    if verify_sources:
        require(sha256_file(Path(__file__).resolve()) == value["adapterSha256"],
                "live HLS adapter bytes differ from the case")
        require(sha256_file(LIVE_FIXTURE_PATH) == value["publisherSha256"],
                "live fixture bytes differ from the case")
    return value


def parse_scenario(path: Path, config: dict[str, Any]) -> None:
    payload = read_bytes(path, MAX_DOCUMENT_BYTES, "scenario")
    require(b"\x00" not in payload, "scenario contains a NUL byte")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LiveCaseError("scenario is not UTF-8") from error
    commands: list[list[str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise LiveCaseError(f"invalid scenario quoting on line {line_number}") from error
        if words:
            commands.append(words)
    require(commands and commands[-1] == ["shutdown"], "scenario must end with shutdown")
    endpoint = "mux/index.m3u8" if config["source"] == "mux" else "separate/master.m3u8"
    expected_url = f"http://127.0.0.1:{config['port']}/{endpoint}"
    loads = [words for words in commands if words[0].lower() == "load"]
    require(loads == [["load", expected_url]],
            "scenario does not load the declared live URL exactly once")
    require(not any(words[0].lower() in {"replace", "seek"} for words in commands),
            "live nonseekable qualification cannot replace or seek")
    require(sum(words[0].lower() == "play" for words in commands) == 1,
            "live scenario must play exactly once")
    require([words for words in commands if words[:1] == ["snapshot"]]
            == [["snapshot", "live-steady"]],
            "live scenario must contain one looped live-steady snapshot action")
    loops = [words for words in commands if words[0].lower() == "loop"]
    require(loops == [["loop", "8"]], "live scenario must take exactly eight steady samples")


def validate_inputs(config_path: Path, fixture_root: Path, fixture_manifest: Path,
                    scenario: Path) -> tuple[dict[str, Any], Any]:
    config = validate_config(read_json(config_path, MAX_DOCUMENT_BYTES, "live HLS config"))
    resolved_root = fixture_root.resolve(strict=True)
    resolved_manifest = fixture_manifest.resolve(strict=True)
    require(resolved_root.is_dir(), "fixture root is not a directory")
    require(resolved_manifest == resolved_root / "provenance/manifest.json",
            "fixture manifest is not rooted in the selected fixture")
    bundle = live_fixture.FixtureBundle.load(resolved_root)
    require(sha256_file(resolved_manifest) == bundle.ready["manifest_sha256"],
            "fixture manifest identity differs from READY")
    minimum_generations = (
        config["minMuxPlaylistGenerations"] if config["source"] == "mux"
        else config["minPairedPlaylistGenerations"]
    )
    require(minimum_generations <= bundle.publication_count,
            "requested live generation coverage exceeds the fixture schedule")
    parse_scenario(scenario, config)
    return config, bundle


def command_check(args: argparse.Namespace) -> None:
    config, _bundle = validate_inputs(args.config, args.fixture_root, args.fixture_manifest, args.scenario)
    print(config["port"])


def completion_value(config: dict[str, Any], publisher: Any, server: Any, log: Path) -> dict[str, Any]:
    allocated, terminal, rejected = server.completion_counts()
    payload = read_bytes(log, config["maxLogBytes"], "live request evidence", nonempty=False)
    require(allocated == terminal, "a live HLS request has no terminal evidence record")
    require(rejected == 0, "live HLS rejected a connection before shutdown")
    return {
        "allocatedRequestCount": allocated,
        "caseId": config["caseId"],
        "clockBasis": CLOCK_BASIS,
        "faultMode": config["faultMode"],
        "finalGeneration": publisher.snapshot().generation,
        "logBytes": len(payload),
        "logSha256": sha256_bytes(payload),
        "rejectedConnectionCount": rejected,
        "schema": SCHEMA,
        "status": "complete",
        "terminalRecordCount": terminal,
    }


def command_serve(args: argparse.Namespace) -> None:
    config, bundle = validate_inputs(args.config, args.fixture_root, args.fixture_manifest, args.scenario)
    require(not args.log.exists(), "refusing to append to an existing live request log")
    require(not args.completion_marker.exists(), "refusing to replace a completion marker")
    require(args.log.parent == args.completion_marker.parent, "live evidence outputs must share a parent")
    require(args.log.parent.is_dir() and not args.log.parent.is_symlink(), "live evidence parent is unsafe")

    with args.log.open("x", encoding="utf-8", buffering=1) as log_stream:
        os.chmod(args.log, 0o600)
        logger = live_fixture.SafeRequestLogger(
            log_stream, config["caseId"], max_bytes=config["maxLogBytes"]
        )
        publisher = live_fixture.LivePublisher(
            bundle, config["faultMode"], config["initialGeneration"]
        )
        state = live_fixture.LiveFixtureState(
            bundle,
            publisher,
            live_fixture.AudioFaultGate(config["faultMode"], None, None, 0),
            logger,
            config["controlToken"],
            config["maxRequests"],
            config["maxConcurrent"],
        )
        server = live_fixture.LiveFixtureServer((config["bind"], config["port"]), state)
        advancer = live_fixture.AutoAdvancer(publisher)
        stopping = threading.Event()

        def stop_server(_signum: int, _frame: Any) -> None:
            if not stopping.is_set():
                stopping.set()
                threading.Thread(target=server.shutdown, name="live-hls-host-stop", daemon=True).start()

        prior = {signum: signal.signal(signum, stop_server) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            endpoint = (
                "mux/index.m3u8" if config["source"] == "mux"
                else "separate/master.m3u8"
            )
            print(canonical_bytes({
                config["source"]: f"http://{config['bind']}:{config['port']}/{endpoint}",
                "schema": SCHEMA,
                "service": config["service"],
            }).decode("ascii"), end="", flush=True)
            advancer.start()
            server.serve_forever(poll_interval=0.1)
        finally:
            advancer.stop()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                allocated, terminal, _rejected = server.completion_counts()
                if allocated == terminal:
                    break
                time.sleep(0.01)
            server.server_close()
            for signum, handler in prior.items():
                signal.signal(signum, handler)
        require(server.fatal_error is None, f"live fixture failed closed: {server.fatal_error}")
        completion = completion_value(config, publisher, server, args.log)
    atomic_exclusive_json(args.completion_marker, completion)


def live_watch_value(records: list[dict[str, Any]], observed_ns: int,
                     min_playback_span: float, instrumentation: str,
                     max_observation_lag_ms: int | None = None) -> dict[str, Any]:
    require(instrumentation in {"audio-monitor", "endpoint-monitor"},
            "live watcher instrumentation is invalid")
    require(records and records[-1].get("type") == "result", "driver result is absent")
    require(records[-1].get("status") == "pass" and records[-1].get("exit_code") == 0,
            "driver did not finish a valid scenario")
    require(not any(record.get("type") == "event"
                    and record.get("event") in {"ERROR", "STREAMRENDERINGERROR"}
                    for record in records), "MediaEngine emitted a fatal media event")
    origins = {record.get("monotonic_origin_ms") for record in records}
    require(len(origins) == 1 and all(type(value) is int and value >= 0 for value in origins),
            "driver monotonic origin is invalid")
    origin_ms = next(iter(origins))
    playing = [
        record for record in records
        if record.get("type") == "event"
        and record.get("event") == "PLAYING"
        and record.get("generation_current") is True
        and record.get("source_generation") == 1
    ]
    require(playing, "current-generation PLAYING event is absent")
    playing_relative_ms = playing[0].get("monotonic_ms")
    require(type(playing_relative_ms) is int and playing_relative_ms >= 0,
            "PLAYING event monotonic time is invalid")
    playing_ns = (origin_ms + playing_relative_ms) * 1_000_000
    steady = [
        record for record in records
        if record.get("type") == "snapshot"
        and record.get("action") == "snapshot"
        and record.get("label") == "live-steady"
    ]
    require(len(steady) == 8, "live scenario did not emit exactly eight steady checkpoints")
    generations = {record.get("source_generation") for record in steady}
    require(len(generations) == 1 and next(iter(generations)) == 1,
            "live checkpoints do not belong to one source generation")
    times: list[float] = []
    event_ns: list[int] = []
    for record in steady:
        require(record.get("status") == "ok", "live checkpoint did not complete")
        require(record.get("duration") is None, "live source exposed a finite seekable duration")
        require(record.get("paused") is False and record.get("seeking") is False
                and record.get("ended") is False,
                "live checkpoint is paused, seeking, or ended")
        require(record.get("has_audio") is True and record.get("has_video") is True,
                "live checkpoint does not expose simultaneous audio and video")
        if instrumentation == "audio-monitor":
            require(record.get("audio_monitor_enabled") is True,
                    "live qualification lacks decoded-audio instrumentation")
        else:
            require(record.get("audio_monitor_enabled") is False,
                    "live endpoint qualification unexpectedly changed driver topology")
        media_time = record.get("time")
        relative_ms = record.get("monotonic_ms")
        require(type(media_time) in (int, float) and not isinstance(media_time, bool)
                and math.isfinite(float(media_time)), "live checkpoint media time is invalid")
        require(type(relative_ms) is int and relative_ms >= 0, "live checkpoint monotonic time is invalid")
        times.append(float(media_time))
        event_ns.append((origin_ms + relative_ms) * 1_000_000)
    require(all(right >= left for left, right in zip(times, times[1:])),
            "live media time regressed")
    span = times[-1] - times[0]
    require(span >= min_playback_span, "live media time did not continuously advance")
    require(all(right >= left for left, right in zip(event_ns, event_ns[1:])),
            "live checkpoint clock regressed")
    lag_ms = (observed_ns - event_ns[-1]) // 1_000_000
    require(MIN_OBSERVATION_LAG_MS <= lag_ms <= MAX_OBSERVATION_LAG_MS,
            "driver and host CLOCK_MONOTONIC_RAW timestamps do not calibrate")
    if max_observation_lag_ms is None:
        max_observation_lag_ms = max(0, lag_ms)
    require(type(max_observation_lag_ms) is int
            and max(0, lag_ms) <= max_observation_lag_ms <= MAX_OBSERVATION_LAG_MS,
            "maximum driver observation lag is inconsistent")
    return {
        "clockBasis": CLOCK_BASIS,
        "firstPlaybackMonotonicNs": event_ns[0],
        "firstTime": times[0],
        "lastPlaybackMonotonicNs": event_ns[-1],
        "lastPlaybackObservedMonotonicNs": observed_ns,
        "lastTime": times[-1],
        "maxObservationLagMs": max_observation_lag_ms,
        "playbackSpan": span,
        "playingMonotonicNs": playing_ns,
        "schema": SCHEMA,
        "snapshotCount": len(steady),
        "sourceGeneration": 1,
    }


def command_watch(args: argparse.Namespace) -> None:
    config = validate_config(read_json(args.config, MAX_DOCUMENT_BYTES, "live HLS config"))
    require(args.instrumentation == config["requiredInstrumentation"],
            "live watcher instrumentation differs from its immutable config")
    deadline = time.monotonic() + args.timeout_seconds
    offset = 0
    pending = b""
    expected_sequence = 1
    records: list[dict[str, Any]] = []
    observed_by_sequence: dict[int, int] = {}
    max_lag_ms = 0
    origin_ms: int | None = None
    saw_result = False
    producer_done_observed = False
    while time.monotonic() < deadline:
        done_before_read = args.producer_done.exists()
        producer_done_observed = producer_done_observed or done_before_read
        if not args.driver_json.exists():
            if done_before_read:
                break
            time.sleep(0.01)
            continue
        with args.driver_json.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            offset = stream.tell()
        if not chunk:
            if done_before_read:
                break
            time.sleep(0.01)
            continue
        pending += chunk
        require(len(pending) <= MAX_DRIVER_BYTES, "driver JSONL exceeded the watcher bound")
        lines = pending.split(b"\n")
        pending = lines.pop()
        for payload in lines:
            require(payload and len(payload) <= MAX_DOCUMENT_BYTES,
                    "driver record is empty or oversized")
            record = json.loads(payload.decode("utf-8"))
            require(type(record) is dict, "driver record is not an object")
            require(not saw_result, "driver record followed the terminal result")
            require(record.get("seq") == expected_sequence, "driver sequence is not contiguous")
            expected_sequence += 1
            current_origin = record.get("monotonic_origin_ms")
            relative_ms = record.get("monotonic_ms")
            require(type(current_origin) is int and current_origin >= 0,
                    "driver monotonic origin is invalid")
            require(type(relative_ms) is int and relative_ms >= 0,
                    "driver relative monotonic time is invalid")
            if origin_ms is None:
                origin_ms = current_origin
            require(current_origin == origin_ms, "driver monotonic origin changed")
            event_ns = (current_origin + relative_ms) * 1_000_000
            observed_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
            lag_ms = (observed_ns - event_ns) // 1_000_000
            require(MIN_OBSERVATION_LAG_MS <= lag_ms <= MAX_OBSERVATION_LAG_MS,
                    "driver and host CLOCK_MONOTONIC_RAW timestamps do not calibrate")
            max_lag_ms = max(max_lag_ms, max(0, lag_ms))
            observed_by_sequence[record["seq"]] = observed_ns
            records.append(record)
            if record.get("type") == "result":
                saw_result = True
        if done_before_read:
            break
    require(producer_done_observed
            and args.producer_done.is_file() and not args.producer_done.is_symlink(),
            "producer completion was not observed before watcher timeout")
    require(not pending.strip(), "driver JSONL ended with a partial record")
    require(saw_result, "driver result was not observed before producer completion")
    steady_sequences = [
        record["seq"] for record in records
        if record.get("type") == "snapshot"
        and record.get("action") == "snapshot"
        and record.get("label") == "live-steady"
    ]
    require(steady_sequences, "live steady checkpoints are absent")
    observed_ns = observed_by_sequence[steady_sequences[-1]]
    value = live_watch_value(
        records, observed_ns, config["minPlaybackSpan"], args.instrumentation, max_lag_ms
    )
    atomic_exclusive_json(args.output, value)


def validate_completion(path: Path, config: dict[str, Any], log_payload: bytes) -> dict[str, Any]:
    value = read_json(path, MAX_COMPLETION_BYTES, "live completion marker", canonical=True)
    require(type(value) is dict and set(value) == COMPLETION_KEYS, "live completion keys differ")
    require(value["schema"] == SCHEMA and value["status"] == "complete", "live service did not complete")
    require(value["caseId"] == config["caseId"], "live completion case differs")
    require(value["clockBasis"] == CLOCK_BASIS, "live completion clock basis differs")
    require(value["faultMode"] == config["faultMode"], "live completion fault mode differs")
    for field in ("allocatedRequestCount", "terminalRecordCount", "rejectedConnectionCount",
                  "finalGeneration", "logBytes"):
        require(type(value[field]) is int and value[field] >= 0, f"invalid live completion {field}")
    require(value["allocatedRequestCount"] == value["terminalRecordCount"],
            "live completion has an unterminated request")
    require(value["allocatedRequestCount"] <= config["maxRequests"], "live request cap was exceeded")
    require(value["rejectedConnectionCount"] == 0, "live service rejected a connection")
    require(value["logBytes"] == len(log_payload), "live completion byte count differs")
    require(type(value["logSha256"]) is str and SHA256_RE.fullmatch(value["logSha256"]),
            "invalid live completion digest")
    require(value["logSha256"] == sha256_bytes(log_payload), "live completion digest differs")
    return value


def live_evidence(payload: bytes, config: dict[str, Any]) -> list[dict[str, Any]]:
    require(payload.endswith(b"\n") and b"\x00" not in payload, "live request JSONL is incomplete")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        require(line and len(line) <= MAX_DOCUMENT_BYTES,
                f"live request record {line_number} is empty or oversized")
        value = json.loads(line.decode("utf-8"))
        try:
            live_fixture.validate_evidence_record(value, config["caseId"])
        except live_fixture.ContractError as error:
            raise LiveCaseError(str(error)) from error
        records.append(value)
    require([record["request_seq"] for record in records] == list(range(1, len(records) + 1)),
            "live request sequence is not contiguous")
    return records


def command_score(args: argparse.Namespace) -> None:
    config = validate_config(read_json(args.config, MAX_DOCUMENT_BYTES, "live HLS config"))
    watch = read_json(args.watch, MAX_DOCUMENT_BYTES, "live watch", canonical=True)
    require(type(watch) is dict and set(watch) == WATCH_KEYS, "live watch keys differ")
    require(watch["schema"] == SCHEMA and watch["clockBasis"] == CLOCK_BASIS,
            "live watch schema or clock basis differs")
    require(type(watch["snapshotCount"]) is int and watch["snapshotCount"] == 8,
            "live watch snapshot count differs")
    require(type(watch["playbackSpan"]) in (int, float)
            and float(watch["playbackSpan"]) >= config["minPlaybackSpan"],
            "live watch playback span is too small")
    first_ns = watch["firstPlaybackMonotonicNs"]
    last_ns = watch["lastPlaybackMonotonicNs"]
    playing_ns = watch["playingMonotonicNs"]
    require(type(first_ns) is int and type(last_ns) is int and 0 <= first_ns < last_ns,
            "live watch interval is invalid")
    require(type(playing_ns) is int and 0 <= playing_ns <= first_ns,
            "live PLAYING event is outside the observed playback interval")

    log_payload = read_bytes(args.http_log, MAX_EVIDENCE_BYTES, "live request JSONL")
    completion = validate_completion(args.service_completion, config, log_payload)
    records = live_evidence(log_payload, config)
    require(len(records) == completion["terminalRecordCount"], "live completion record count differs")
    errors = [record for record in records if record["status"] != 200]
    require(len(errors) <= config["maxErrorResponses"], "healthy live HLS emitted an HTTP error")
    require(all(record["fault_mode"] == "normal" for record in records), "live evidence used a fault mode")
    if config["source"] == "mux":
        allowed_routes = {live_fixture.ROUTE_MUX_PLAYLIST, live_fixture.ROUTE_MUX_SEGMENT}
        playlist = [
            record for record in records
            if record["route"] == live_fixture.ROUTE_MUX_PLAYLIST and record["method"] == "GET"
        ]
        segments = [
            record for record in records
            if record["route"] == live_fixture.ROUTE_MUX_SEGMENT and record["method"] == "GET"
        ]
        require(all(record["route"] in allowed_routes for record in records),
                "muxed live case requested an undeclared route")
        require(len(playlist) >= config["minMuxPlaylistRequests"],
                "too few mux playlist requests")
        require(len(segments) >= config["minMuxSegmentRequests"],
                "too few mux segment requests")
        generations = sorted({record["generation"] for record in playlist})
        require(len(generations) >= config["minMuxPlaylistGenerations"],
                "playlist polling did not observe enough publication generations")
        require(any(first_ns <= record["started_monotonic_ns"] <= last_ns for record in playlist),
                "no mux playlist poll overlapped the steady playback interval")
        successful_bodies = playlist + segments
        summary = {
            "firstObservedGeneration": generations[0],
            "lastObservedGeneration": generations[-1],
            "muxPlaylistGenerationCount": len(generations),
            "muxPlaylistRequestCount": len(playlist),
            "muxSegmentRequestCount": len(segments),
            "role": "live-hls-muxed-qualification",
        }
    else:
        route_fields = (
            (live_fixture.ROUTE_MASTER, "minMasterRequests", "master"),
            (live_fixture.ROUTE_AUDIO_PLAYLIST, "minAudioPlaylistRequests", "audio playlist"),
            (live_fixture.ROUTE_AUDIO_SEGMENT, "minAudioSegmentRequests", "audio segment"),
            (live_fixture.ROUTE_VIDEO_PLAYLIST, "minVideoPlaylistRequests", "video playlist"),
            (live_fixture.ROUTE_VIDEO_SEGMENT, "minVideoSegmentRequests", "video segment"),
        )
        selected: dict[str, list[dict[str, Any]]] = {}
        for route, minimum_field, label in route_fields:
            selected[route] = [
                record for record in records
                if record["route"] == route and record["method"] == "GET"
            ]
            require(len(selected[route]) >= config[minimum_field], f"too few {label} requests")
        require(all(record["route"] in selected for record in records),
                "separate live case requested an undeclared route")
        for route, _minimum_field, label in route_fields[1:]:
            require(any(first_ns <= record["started_monotonic_ns"] <= last_ns
                        for record in selected[route]),
                    f"no {label} request overlapped the steady playback interval")
        audio_generations = {record["generation"]
                             for record in selected[live_fixture.ROUTE_AUDIO_PLAYLIST]}
        video_generations = {record["generation"]
                             for record in selected[live_fixture.ROUTE_VIDEO_PLAYLIST]}
        generations = sorted(audio_generations & video_generations)
        require(len(generations) >= config["minPairedPlaylistGenerations"],
                "audio/video playlist polling did not observe enough matching generations")
        successful_bodies = [record for group in selected.values() for record in group]
        summary = {
            "audioPlaylistRequestCount": len(selected[live_fixture.ROUTE_AUDIO_PLAYLIST]),
            "audioSegmentRequestCount": len(selected[live_fixture.ROUTE_AUDIO_SEGMENT]),
            "firstPairedGeneration": generations[0],
            "lastPairedGeneration": generations[-1],
            "masterRequestCount": len(selected[live_fixture.ROUTE_MASTER]),
            "pairedPlaylistGenerationCount": len(generations),
            "role": "live-hls-separate-qualification",
            "videoPlaylistRequestCount": len(selected[live_fixture.ROUTE_VIDEO_PLAYLIST]),
            "videoSegmentRequestCount": len(selected[live_fixture.ROUTE_VIDEO_SEGMENT]),
        }
    require(completion["finalGeneration"] >= generations[-1], "completion generation regressed")
    require(all(record["bytes_sent"] > 0 for record in successful_bodies),
            "a successful live body was empty")
    first_body_started_ns = min(record["started_monotonic_ns"] for record in successful_bodies)
    require(first_body_started_ns <= playing_ns,
            "live playback began before the first successful fixture request")
    startup_ms = (playing_ns - first_body_started_ns) // 1_000_000
    require(startup_ms <= config["maxStartupMs"], "live startup exceeded its request-relative bound")
    atomic_exclusive_json(args.output, {
        "clockBasis": CLOCK_BASIS,
        "errorResponseCount": len(errors),
        "finalGeneration": completion["finalGeneration"],
        "playbackSpan": watch["playbackSpan"],
        "requestCount": len(records),
        "schema": SCHEMA,
        "startupAfterRequestMs": startup_ms,
        "status": "passed",
        **summary,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--fixture-root", required=True, type=Path)
        command.add_argument("--fixture-manifest", required=True, type=Path)
        command.add_argument("--scenario", required=True, type=Path)
        if name == "serve":
            command.add_argument("--log", required=True, type=Path)
            command.add_argument("--completion-marker", required=True, type=Path)
            command.set_defaults(function=command_serve)
        else:
            command.set_defaults(function=command_check)
    watch = commands.add_parser("watch")
    watch.add_argument("--driver-json", required=True, type=Path)
    watch.add_argument("--output", required=True, type=Path)
    watch.add_argument("--producer-done", required=True, type=Path)
    watch.add_argument("--config", required=True, type=Path)
    watch.add_argument("--instrumentation", required=True,
                       choices=("audio-monitor", "endpoint-monitor"))
    watch.add_argument("--timeout-seconds", type=int, choices=range(1, 601), default=330)
    watch.set_defaults(function=command_watch)
    score = commands.add_parser("score")
    score.add_argument("--http-log", required=True, type=Path)
    score.add_argument("--service-completion", required=True, type=Path)
    score.add_argument("--watch", required=True, type=Path)
    score.add_argument("--config", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.set_defaults(function=command_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (LiveCaseError, OSError, json.JSONDecodeError, UnicodeError,
            live_fixture.ContractError) as error:
        print(f"live HLS case: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
