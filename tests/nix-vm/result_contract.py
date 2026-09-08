#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Create and validate the only guest evidence allowed to leave the VM lab."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
import re


SCHEMA = 1
MAX_RESULT_BYTES = 16 * 1024
FAULT_PROFILES = [
    "clean",
    "delay",
    "jitter",
    "loss",
    "duplicate",
    "reorder",
    "rate",
    "blackhole",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STRESS_INPUT_KEYS = {
    "controlOracle",
    "driverExe",
    "fixtureBytes",
    "fixtureManifest",
    "instrumentedOracle",
    "labHarness",
    "parser",
    "protonTool",
    "scenario",
    "serviceConfig",
    "steamRuntime",
}
STRESS_BUILD_ROLES = {
    "stock-ge-control",
    "rtsp-reference-control",
    "frozen-regression-control",
    "streaming-base-candidate",
    "full-parity-candidate",
}
STRESS_CASE_ROLES = {"expected-pass", "negative-control", "qualification"}
STRESS_INSTRUMENTATION = {"control", "audio-monitor"}
STRESS_MEDIA_KINDS = {"av", "audio-only", "video-only"}
CONTROL_FORBIDDEN_ORACLE_KEYS = {
    "audio_check_labels",
    "max_audio_nonzero_silence_ms",
    "max_audio_silence_ms",
    "min_audio_bytes_generation",
    "min_audio_nonzero_units_generation",
    "min_audio_samples_generation",
    "min_post_seek_audio_bytes",
    "min_post_seek_audio_samples",
    "min_post_seek_nonzero_units",
    "required_audio_payload_formats",
}
STRESS_AUDIO_FAILURE_PATTERNS = (
    (re.compile(r"^timeline .*: post-seek audio samples [0-9]+ below [0-9]+$"), "post-seek-audio-samples"),
    (re.compile(r"^timeline .*: post-seek audio bytes [0-9]+ below [0-9]+$"), "post-seek-audio-bytes"),
    (re.compile(r"^timeline .*: post-seek nonzero audio units [0-9]+ below [0-9]+$"), "post-seek-audio-nonzero"),
    (re.compile(r"^audio checkpoint .*: [0-9]+ samples below [0-9]+$"), "checkpoint-audio-samples"),
    (re.compile(r"^audio checkpoint .*: [0-9]+ bytes below [0-9]+$"), "checkpoint-audio-bytes"),
    (re.compile(r"^audio checkpoint .*: no payload buffer was inspected$"), "checkpoint-audio-payload-missing"),
    (re.compile(r"^audio checkpoint .*: payload format .* not in .*$"), "checkpoint-audio-format"),
    (re.compile(r"^audio checkpoint .*: [0-9]+ nonzero units below [0-9]+$"), "checkpoint-audio-nonzero"),
    (re.compile(r"^audio checkpoint .*: no delivered audio timestamp$"), "checkpoint-audio-delivery-stale"),
    (re.compile(r"^audio checkpoint .*: no delivered sample for [0-9]+ ms$"), "checkpoint-audio-delivery-stale"),
    (re.compile(r"^audio checkpoint .*: no nonzero audio unit observed$"), "checkpoint-audio-nonzero-stale"),
    (re.compile(r"^audio checkpoint .*: no nonzero audio unit for [0-9]+ ms$"), "checkpoint-audio-nonzero-stale"),
)
STRESS_AUDIO_FAILURE_CODES = {code for _pattern, code in STRESS_AUDIO_FAILURE_PATTERNS}


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    actual = set(value)
    require(actual == expected, f"{label} keys differ: {sorted(actual ^ expected)}")
    return value


def require_number(value: Any, label: str, lower: float, upper: float) -> float:
    require(type(value) in (int, float), f"{label} must be a number")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    require(lower <= number <= upper, f"{label} is outside [{lower}, {upper}]")
    return number


def validate_smoke(value: Any) -> None:
    result = require_exact_keys(
        value,
        {"case", "hostShares", "network", "runtimeProbe", "schema", "status", "suite"},
        "smoke result",
    )
    require(type(result["schema"]) is int and result["schema"] == SCHEMA, "unsupported smoke schema")
    require(result["suite"] == "rtsp-media-lab", "unexpected smoke suite")
    require(result["case"] == "isolation-smoke", "unexpected smoke case")
    require(result["status"] == "passed", "smoke result did not pass")
    require(result["network"] == "loopback-only", "smoke network is not loopback-only")
    require(result["hostShares"] is False, "smoke result permits host shares")
    require(result["runtimeProbe"] is False, "smoke result mislabeled a runtime run")


def validate_runtime(value: Any) -> None:
    result = require_exact_keys(
        value,
        {
            "case",
            "expected",
            "parserAccepted",
            "processExit",
            "schema",
            "status",
            "suite",
        },
        "runtime result",
    )
    require(type(result["schema"]) is int and result["schema"] == SCHEMA, "unsupported runtime schema")
    require(result["suite"] == "rtsp-media-lab", "unexpected runtime suite")
    require(result["case"] == "runtime-probe", "unexpected runtime case")
    require(result["status"] == "passed", "runtime result did not pass")
    require(result["expected"] in {"a3.7-failure", "candidate-pass"}, "invalid expectation")
    require(result["parserAccepted"] is True, "runtime parser did not accept the evidence")
    require(type(result["processExit"]) is int, "processExit must be an integer")
    require(0 <= result["processExit"] <= 255, "processExit is outside [0, 255]")


def validate_fault(value: Any) -> None:
    result = require_exact_keys(
        value,
        {
            "case",
            "control",
            "faultOwner",
            "measurements",
            "profiles",
            "schema",
            "status",
            "suite",
            "target",
        },
        "fault result",
    )
    require(type(result["schema"]) is int and result["schema"] == SCHEMA, "unsupported fault schema")
    require(result["suite"] == "rtsp-media-fault-lab", "unexpected fault suite")
    require(result["case"] == "targeted-client-impairment", "unexpected fault case")
    require(result["status"] == "passed", "fault result did not pass")
    require(result["faultOwner"] == "origin-egress", "unexpected fault owner")
    require(result["target"] == "client-a", "unexpected fault target")
    require(result["control"] == "client-b", "unexpected fault control")
    require(result["profiles"] == FAULT_PROFILES, "fault profile set or order differs")

    measurements = require_exact_keys(
        result["measurements"],
        {
            "blackholeControlServed",
            "blackholeTargetBlocked",
            "cleanControlSeconds",
            "cleanTargetSeconds",
            "delayControlSeconds",
            "delayTargetSeconds",
            "recoveryTargetServed",
        },
        "fault measurements",
    )
    clean_target = require_number(measurements["cleanTargetSeconds"], "cleanTargetSeconds", 0, 30)
    clean_control = require_number(measurements["cleanControlSeconds"], "cleanControlSeconds", 0, 30)
    delay_target = require_number(measurements["delayTargetSeconds"], "delayTargetSeconds", 0, 30)
    delay_control = require_number(measurements["delayControlSeconds"], "delayControlSeconds", 0, 30)
    require(measurements["blackholeTargetBlocked"] is True, "target was not black-holed")
    require(measurements["blackholeControlServed"] is True, "control failed during target black hole")
    require(measurements["recoveryTargetServed"] is True, "target did not recover")
    require(delay_target >= 0.20, "target delay was not measurable")
    require(delay_target - clean_target >= 0.15, "target did not slow relative to its clean baseline")
    require(delay_target - delay_control >= 0.15, "delay did not distinguish target and control")
    require(
        delay_control <= max(0.50, clean_control + 0.25),
        "control was unexpectedly delayed by the target profile",
    )


def validate_sha256(value: Any, label: str) -> None:
    require(type(value) is str and SHA256_RE.fullmatch(value) is not None, f"{label} is not lowercase SHA-256")


def validate_stress(value: Any) -> None:
    result = require_exact_keys(
        value,
        {
            "audioEndpoint",
            "buildRole",
            "case",
            "caseRole",
            "digestSchema",
            "driver",
            "fixtureService",
            "inputSha256",
            "instrumentation",
            "mediaKind",
            "oracleOutcome",
            "oracleFailureCodes",
            "schema",
            "status",
            "suite",
            "transport",
            "videoSetup",
        },
        "stress result",
    )
    require(type(result["schema"]) is int and result["schema"] == SCHEMA, "unsupported stress schema")
    require(result["suite"] == "rtsp-media-lab", "unexpected stress suite")
    require(result["case"] == "media-engine-stress", "unexpected stress case")
    require(result["status"] == "passed", "stress oracle did not pass")
    require(result["digestSchema"] == "sha256-file-or-tree-v1", "unexpected digest schema")
    require(result["audioEndpoint"] == "pulseaudio-null-sink-s16le-48000-stereo", "unexpected audio endpoint")
    require(result["videoSetup"] == "xvfb-llvmpipe-headless-no-frame-oracle", "unexpected video setup")
    require(result["fixtureService"] == "progressive-http-loopback-v1", "unexpected fixture service")
    require(result["buildRole"] in STRESS_BUILD_ROLES, "invalid stress build role")
    require(result["caseRole"] in STRESS_CASE_ROLES, "invalid stress case role")
    require(result["instrumentation"] in STRESS_INSTRUMENTATION, "invalid instrumentation mode")
    require(result["mediaKind"] in STRESS_MEDIA_KINDS, "invalid stress media kind")
    require(result["oracleOutcome"] in {"accepted", "rejected-as-expected"}, "invalid oracle outcome")
    require(
        type(result["oracleFailureCodes"]) is list
        and result["oracleFailureCodes"] == sorted(set(result["oracleFailureCodes"]))
        and all(code in STRESS_AUDIO_FAILURE_CODES for code in result["oracleFailureCodes"]),
        "invalid or noncanonical oracle failure codes",
    )

    identities = require_exact_keys(result["inputSha256"], STRESS_INPUT_KEYS, "stress input identities")
    for name, digest in identities.items():
        validate_sha256(digest, f"stress input {name}")

    transport = require_exact_keys(
        result["transport"],
        {
            "bodyResponseCount", "clockBasis", "errorResponseCount", "firstPlaybackObservationLagMs",
            "getRequestCount", "initialResponseBytesExpected", "initialResponseBytesSent",
            "initialResponseCompleted", "initialRequestRange", "initialRequestSequence",
            "maxAllowedErrorResponses", "maxRangeRepeatCount", "maxWatcherObservationLagMs",
            "minExpectedErrorResponses", "minExpectedPostSeekRangeResponses",
            "postSeekRangeResponseCount",
            "progressiveScored", "rangeResponseCount", "requestCount", "role", "schema",
            "startupAfterRequestMs", "status", "transferTailAfterPlaybackMs",
        },
        "stress transport summary",
    )
    require(type(transport["schema"]) is int and transport["schema"] == 1, "unsupported transport schema")
    require(transport["status"] == "passed", "transport oracle did not pass")
    require(transport["clockBasis"] == "linux-clock-monotonic-raw-v1",
            "transport clock basis differs")
    require(transport["role"] in {"media-engine-diagnostic", "streaming-first-qualification"},
            "invalid transport role")
    for key in (
        "bodyResponseCount", "errorResponseCount", "getRequestCount", "maxAllowedErrorResponses",
        "maxRangeRepeatCount", "maxWatcherObservationLagMs", "minExpectedErrorResponses",
        "minExpectedPostSeekRangeResponses", "postSeekRangeResponseCount",
        "rangeResponseCount", "requestCount",
    ):
        require(type(transport[key]) is int and transport[key] >= 0, f"transport {key} is invalid")
    require(1 <= transport["requestCount"] <= 64, "transport request count is outside qualification bounds")
    require(transport["getRequestCount"] <= transport["requestCount"], "transport GET count is invalid")
    require(transport["bodyResponseCount"] >= 1, "transport has no body response")
    require(transport["minExpectedErrorResponses"] <= transport["errorResponseCount"]
            <= transport["maxAllowedErrorResponses"] <= 64,
            "transport HTTP error count is outside its declared bounds")
    require(transport["postSeekRangeResponseCount"]
            >= transport["minExpectedPostSeekRangeResponses"],
            "transport post-seek Range count is below its declared bound")
    require(transport["rangeResponseCount"] >= transport["postSeekRangeResponseCount"],
            "transport post-seek Range count exceeds all Range responses")
    if transport["errorResponseCount"]:
        require(result["caseRole"] == "expected-pass",
                "bounded HTTP-error evidence cannot be labeled a qualification")
    require(transport["maxRangeRepeatCount"] <= 8, "transport Range repetition exceeds bound")
    require(type(transport["firstPlaybackObservationLagMs"]) is int
            and -2 <= transport["firstPlaybackObservationLagMs"] <= 250,
            "transport watcher lag is outside bounds")
    require(0 <= transport["maxWatcherObservationLagMs"] <= 250,
            "transport maximum watcher lag is outside bounds")
    require(transport["maxWatcherObservationLagMs"]
            >= max(0, transport["firstPlaybackObservationLagMs"]),
            "transport maximum watcher lag is inconsistent")
    if transport["role"] == "streaming-first-qualification":
        require(transport["progressiveScored"] is True, "streaming-first transport was not scored")
        require(type(transport["startupAfterRequestMs"]) is int
                and 0 <= transport["startupAfterRequestMs"] <= 5_000,
                "streaming-first startup timing failed")
        require(type(transport["transferTailAfterPlaybackMs"]) is int
                and transport["transferTailAfterPlaybackMs"] >= 10_000,
                "streaming-first transfer tail is too short")
        require(type(transport["initialResponseBytesExpected"]) is int
                and type(transport["initialResponseBytesSent"]) is int,
                "streaming-first response byte counts are missing")
        require(type(transport["initialResponseCompleted"]) is bool,
                "streaming-first response terminal state is missing")
        require(type(transport["initialRequestSequence"]) is int
                and 1 <= transport["initialRequestSequence"] <= transport["requestCount"],
                "streaming-first request sequence is missing or invalid")
        require(
            transport["initialRequestRange"] is None
            or (
                type(transport["initialRequestRange"]) is str
                and re.fullmatch(r"bytes=[0-9]+-[0-9]+", transport["initialRequestRange"])
            ),
            "streaming-first initial request range is invalid",
        )
        if transport["initialResponseCompleted"]:
            require(transport["initialResponseBytesSent"] == transport["initialResponseBytesExpected"],
                    "completed initial response byte counts differ")
        else:
            require(transport["initialResponseBytesSent"] < transport["initialResponseBytesExpected"],
                    "partial initial response did not remain below its declared bytes")
    else:
        require(transport["progressiveScored"] is False, "diagnostic transport claims progressive scoring")
        for key in (
            "startupAfterRequestMs", "transferTailAfterPlaybackMs",
            "initialResponseBytesExpected", "initialResponseBytesSent", "initialResponseCompleted",
            "initialRequestRange", "initialRequestSequence",
        ):
            require(transport[key] is None, f"diagnostic transport unexpectedly claims {key}")

    driver = require_exact_keys(
        result["driver"],
        {
            "durationMs",
            "exitCode",
            "parserAccepted",
            "processExit",
            "records",
            "result",
            "sourceGenerations",
            "staleEvents",
        },
        "stress driver summary",
    )
    for key in ("durationMs", "exitCode", "processExit", "records", "sourceGenerations", "staleEvents"):
        require(type(driver[key]) is int, f"stress driver {key} must be an integer")
    require(0 <= driver["durationMs"] <= 3_600_000, "stress duration is outside bounds")
    require(1 <= driver["records"] <= 1_000_000, "stress record count is outside bounds")
    require(0 <= driver["sourceGenerations"] <= 100_000, "stress source generation count is outside bounds")
    require(0 <= driver["staleEvents"] <= driver["records"], "stress stale event count is outside bounds")
    require(0 <= driver["exitCode"] <= 255, "stress driver exit code is outside bounds")
    require(0 <= driver["processExit"] <= 255, "stress process exit is outside bounds")
    require(driver["exitCode"] == driver["processExit"], "driver and process exit codes differ")
    require(driver["result"] in {"pass", "fail"}, "stress driver result is invalid")
    if result["caseRole"] == "negative-control":
        require(result["mediaKind"] != "video-only", "audio negative control cannot use video-only media")
        require(
            driver["result"] == "pass" and driver["exitCode"] == 0,
            "audio negative control did not keep the driver process healthy",
        )
        if result["instrumentation"] == "control":
            require(driver["parserAccepted"] is True, "uninstrumented negative control did not stay healthy")
            require(result["oracleOutcome"] == "accepted", "uninstrumented negative control outcome is mislabeled")
            require(not result["oracleFailureCodes"], "accepted control has failure codes")
        else:
            require(driver["parserAccepted"] is False, "instrumented negative control oracle unexpectedly accepted")
            require(result["oracleOutcome"] == "rejected-as-expected", "negative control outcome is mislabeled")
            require(bool(result["oracleFailureCodes"]), "negative control has no classified audio failure")
    else:
        require(driver["parserAccepted"] is True, "stress parser did not accept the evidence")
        require(result["oracleOutcome"] == "accepted", "passing oracle outcome is mislabeled")
        require(not result["oracleFailureCodes"], "accepted result has failure codes")
        require(driver["result"] == "pass" and driver["exitCode"] == 0, "passing case role did not complete cleanly")


VALIDATORS = {
    "smoke": validate_smoke,
    "runtime": validate_runtime,
    "fault": validate_fault,
    "stress": validate_stress,
}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write(path_text: str, expected_path: str, value: Any) -> None:
    path = Path(path_text)
    require(path == Path(expected_path), "result path is not the declared contract path")
    payload = canonical_bytes(value)
    require(len(payload) <= MAX_RESULT_BYTES, "result exceeds the size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_and_validate(kind: str, path_text: str) -> Any:
    path = Path(path_text)
    payload = path.read_bytes()
    require(len(payload) <= MAX_RESULT_BYTES, "result exceeds the size limit")
    require(b"\x00" not in payload, "result contains a NUL byte")
    value = json.loads(payload.decode("utf-8"))
    VALIDATORS[kind](value)
    require(payload == canonical_bytes(value), "result is not canonical JSON")
    return value


def parse_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a finite duration") from error
    if not math.isfinite(parsed) or parsed < 0 or parsed > 30:
        raise argparse.ArgumentTypeError("duration must be finite and within [0, 30]")
    return parsed


def command_write_smoke(args: argparse.Namespace) -> None:
    value = {
        "case": "isolation-smoke",
        "hostShares": False,
        "network": "loopback-only",
        "runtimeProbe": False,
        "schema": SCHEMA,
        "status": "passed",
        "suite": "rtsp-media-lab",
    }
    validate_smoke(value)
    atomic_write(
        args.output,
        "/var/lib/rtsp-media-lab/results/smoke-summary.json",
        value,
    )


def command_write_runtime(args: argparse.Namespace) -> None:
    value = {
        "case": "runtime-probe",
        "expected": args.expected,
        "parserAccepted": True,
        "processExit": args.process_exit,
        "schema": SCHEMA,
        "status": "passed",
        "suite": "rtsp-media-lab",
    }
    validate_runtime(value)
    atomic_write(
        args.output,
        "/var/lib/rtsp-media-lab/results/runtime-probe-summary.json",
        value,
    )


def command_write_fault(args: argparse.Namespace) -> None:
    value = {
        "case": "targeted-client-impairment",
        "control": "client-b",
        "faultOwner": "origin-egress",
        "measurements": {
            "blackholeControlServed": True,
            "blackholeTargetBlocked": True,
            "cleanControlSeconds": args.clean_control_seconds,
            "cleanTargetSeconds": args.clean_target_seconds,
            "delayControlSeconds": args.delay_control_seconds,
            "delayTargetSeconds": args.delay_target_seconds,
            "recoveryTargetServed": True,
        },
        "profiles": FAULT_PROFILES,
        "schema": SCHEMA,
        "status": "passed",
        "suite": "rtsp-media-fault-lab",
        "target": "client-a",
    }
    validate_fault(value)
    atomic_write(
        args.output,
        "/var/lib/rtsp-media-fault-lab/results/fault-summary.json",
        value,
    )


def load_bounded_json(path_text: str, label: str) -> Any:
    payload = Path(path_text).read_bytes()
    require(len(payload) <= MAX_RESULT_BYTES, f"{label} exceeds the size limit")
    require(b"\x00" not in payload, f"{label} contains a NUL byte")
    return json.loads(payload.decode("utf-8"))


def command_check_stress_oracles(args: argparse.Namespace) -> None:
    validate_sha256(args.driver_sha256, "driver identity")
    validate_sha256(args.scenario_sha256, "scenario identity")
    for mode, path_text in (("control", args.control), ("audio-monitor", args.instrumented)):
        oracle = load_bounded_json(path_text, f"{mode} oracle")
        require(type(oracle) is dict, f"{mode} oracle must be an object")
        require(
            oracle.get("expected_driver_sha256") == args.driver_sha256,
            f"{mode} oracle does not bind the actual driver bytes",
        )
        require(
            oracle.get("expected_scenario_sha256") == args.scenario_sha256,
            f"{mode} oracle does not bind the actual scenario bytes",
        )
        if mode == "control":
            forbidden = sorted(set(oracle) & CONTROL_FORBIDDEN_ORACLE_KEYS)
            require(not forbidden, "control oracle requires unavailable audio-monitor counters")
        elif args.media_kind != "video-only":
            labels = oracle.get("audio_check_labels")
            formats = oracle.get("required_audio_payload_formats")
            require(type(labels) is list and labels and all(type(item) is str and item for item in labels),
                    "instrumented audio case has no labelled delivery checkpoints")
            require(type(formats) is list and formats and all(item in {"pcm16", "pcm32", "float32"} for item in formats),
                    "instrumented audio case has no allowed decoded PCM format")
            for key in (
                "min_audio_samples_generation",
                "min_audio_bytes_generation",
                "min_audio_nonzero_units_generation",
                "max_audio_silence_ms",
                "max_audio_nonzero_silence_ms",
            ):
                require(type(oracle.get(key)) is int and oracle[key] > 0,
                        f"instrumented audio case has no positive {key}")


def classify_stress_parser_log(path_text: str, expected_rejection: bool) -> list[str]:
    parser_log = Path(path_text).read_bytes()
    require(len(parser_log) <= 128 * 1024, "stress parser log exceeds the classification limit")
    require(b"\x00" not in parser_log, "stress parser log contains a NUL byte")
    failure_codes: set[str] = set()
    failure_lines = 0
    for line in parser_log.decode("utf-8").splitlines():
        if not line.startswith("FAIL: "):
            continue
        failure_lines += 1
        message = line[6:]
        matches = [code for pattern, code in STRESS_AUDIO_FAILURE_PATTERNS if pattern.fullmatch(message)]
        require(len(matches) == 1, "stress oracle rejection contains an unclassified failure")
        failure_codes.add(matches[0])
    if expected_rejection:
        require(failure_lines > 0, "expected oracle rejection has no classified failure lines")
    else:
        require(failure_lines == 0, "accepted oracle emitted failure lines")
    return sorted(failure_codes)


def command_write_stress(args: argparse.Namespace) -> None:
    parser_summary = load_bounded_json(args.parser_summary, "stress parser summary")
    parser_summary = require_exact_keys(
        parser_summary,
        {
            "duration_ms",
            "event_counts",
            "exit_code",
            "records",
            "result",
            "source_generations",
            "stale_events",
        },
        "stress parser summary",
    )
    require(type(parser_summary["event_counts"]) is dict, "event_counts must be an object")
    for name, count in parser_summary["event_counts"].items():
        require(type(name) is str and type(count) is int and count >= 0, "invalid event_counts member")

    failure_codes = classify_stress_parser_log(
        args.parser_log,
        args.oracle_outcome == "rejected-as-expected",
    )
    transport = load_bounded_json(args.transport_summary, "stress transport summary")

    input_sha256 = {
        "controlOracle": args.control_oracle_sha256,
        "driverExe": args.driver_exe_sha256,
        "fixtureBytes": args.fixture_bytes_sha256,
        "fixtureManifest": args.fixture_manifest_sha256,
        "instrumentedOracle": args.instrumented_oracle_sha256,
        "labHarness": args.lab_harness_sha256,
        "parser": args.parser_sha256,
        "protonTool": args.proton_tool_sha256,
        "scenario": args.scenario_sha256,
        "serviceConfig": args.service_config_sha256,
        "steamRuntime": args.steam_runtime_sha256,
    }
    value = {
        "audioEndpoint": "pulseaudio-null-sink-s16le-48000-stereo",
        "buildRole": args.build_role,
        "case": "media-engine-stress",
        "caseRole": args.case_role,
        "digestSchema": "sha256-file-or-tree-v1",
        "driver": {
            "durationMs": parser_summary["duration_ms"],
            "exitCode": parser_summary["exit_code"],
            "parserAccepted": args.oracle_outcome == "accepted",
            "processExit": args.process_exit,
            "records": parser_summary["records"],
            "result": parser_summary["result"],
            "sourceGenerations": parser_summary["source_generations"],
            "staleEvents": parser_summary["stale_events"],
        },
        "fixtureService": "progressive-http-loopback-v1",
        "inputSha256": input_sha256,
        "instrumentation": args.instrumentation,
        "mediaKind": args.media_kind,
        "oracleOutcome": args.oracle_outcome,
        "oracleFailureCodes": failure_codes,
        "schema": SCHEMA,
        "status": "passed",
        "suite": "rtsp-media-lab",
        "transport": transport,
        "videoSetup": "xvfb-llvmpipe-headless-no-frame-oracle",
    }
    validate_stress(value)
    expected_path = f"/var/lib/rtsp-media-lab/results/stress-{args.instrumentation}-summary.json"
    atomic_write(args.output, expected_path, value)


def command_validate(args: argparse.Namespace) -> None:
    load_and_validate(args.kind, args.path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_smoke = subparsers.add_parser("write-smoke")
    write_smoke.add_argument("--output", required=True)
    write_smoke.set_defaults(function=command_write_smoke)

    write_runtime = subparsers.add_parser("write-runtime")
    write_runtime.add_argument("--output", required=True)
    write_runtime.add_argument("--expected", choices=["a3.7-failure", "candidate-pass"], required=True)
    write_runtime.add_argument("--process-exit", type=int, required=True)
    write_runtime.set_defaults(function=command_write_runtime)

    write_fault = subparsers.add_parser("write-fault")
    write_fault.add_argument("--output", required=True)
    write_fault.add_argument("--clean-target-seconds", type=parse_seconds, required=True)
    write_fault.add_argument("--clean-control-seconds", type=parse_seconds, required=True)
    write_fault.add_argument("--delay-target-seconds", type=parse_seconds, required=True)
    write_fault.add_argument("--delay-control-seconds", type=parse_seconds, required=True)
    write_fault.set_defaults(function=command_write_fault)

    check_stress_oracles = subparsers.add_parser("check-stress-oracles")
    check_stress_oracles.add_argument("--control", required=True)
    check_stress_oracles.add_argument("--instrumented", required=True)
    check_stress_oracles.add_argument("--driver-sha256", required=True)
    check_stress_oracles.add_argument("--scenario-sha256", required=True)
    check_stress_oracles.add_argument("--media-kind", choices=sorted(STRESS_MEDIA_KINDS), required=True)
    check_stress_oracles.set_defaults(function=command_check_stress_oracles)

    write_stress = subparsers.add_parser("write-stress")
    write_stress.add_argument("--output", required=True)
    write_stress.add_argument("--parser-summary", required=True)
    write_stress.add_argument("--parser-log", required=True)
    write_stress.add_argument("--transport-summary", required=True)
    write_stress.add_argument("--process-exit", type=int, required=True)
    write_stress.add_argument("--instrumentation", choices=sorted(STRESS_INSTRUMENTATION), required=True)
    write_stress.add_argument("--build-role", choices=sorted(STRESS_BUILD_ROLES), required=True)
    write_stress.add_argument("--case-role", choices=sorted(STRESS_CASE_ROLES), required=True)
    write_stress.add_argument("--media-kind", choices=sorted(STRESS_MEDIA_KINDS), required=True)
    write_stress.add_argument("--oracle-outcome", choices=("accepted", "rejected-as-expected"), required=True)
    for name in (
        "control-oracle",
        "driver-exe",
        "fixture-bytes",
        "fixture-manifest",
        "instrumented-oracle",
        "lab-harness",
        "parser",
        "proton-tool",
        "scenario",
        "service-config",
        "steam-runtime",
    ):
        write_stress.add_argument(f"--{name}-sha256", required=True)
    write_stress.set_defaults(function=command_write_stress)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--kind", choices=sorted(VALIDATORS), required=True)
    validate.add_argument("--path", required=True)
    validate.set_defaults(function=command_validate)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.function(args)
    except (ContractError, json.JSONDecodeError, OSError) as error:
        print(f"result contract: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
