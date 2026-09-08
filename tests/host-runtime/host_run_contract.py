#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Write and validate path-free manifests for host-safe MediaEngine runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


SCHEMA = 5
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PYTHON_VERSION_RE = re.compile(r"^CPython [0-9]+\.[0-9]+\.[0-9]+$")
BUILD_ROLES = {
    "stock-ge-control",
    "rtsp-reference-control",
    "frozen-regression-control",
    "streaming-base-candidate",
    "full-parity-candidate",
}
CASE_ROLES = {"expected-pass", "negative-control", "qualification"}
MEDIA_KINDS = {"av", "audio-only", "video-only"}
INSTRUMENTATION = {"control", "audio-monitor", "endpoint-monitor"}
FIXTURE_NETWORKS = {
    "http-ipv4-loopback-only",
    "rtsp-interleaved-tcp-ipv4-loopback-only",
}
SCHEDULER_PRESSURE = {
    "none": {
        "applicationCpuSet": [],
        "fixtureCpuSet": [],
        "minimumAggregateDriverCpuTimePermille": 0,
        "minimumDriverDurationMs": 0,
        "minimumWorkerDriverCpuTimePermille": 0,
        "profile": "none",
        "workerCount": 0,
    },
    "scheduler-pressure-v1": {
        "applicationCpuSet": list(range(12)),
        "fixtureCpuSet": list(range(12, 16)),
        "minimumAggregateDriverCpuTimePermille": 4000,
        "minimumDriverDurationMs": 5000,
        "minimumWorkerDriverCpuTimePermille": 200,
        "profile": "scheduler-pressure-v1",
        "workerCount": 12,
    },
}
OUTCOMES = {
    "accepted",
    "driver-failed",
    "endpoint-audio-rejected",
    "harness-failed",
    "incomplete",
    "oracle-rejected",
    "rejected-as-expected",
    "transport-rejected",
    "watcher-failed",
}
STAGES = {
    "preflight",
    "fixture",
    "scheduler-pressure",
    "driver",
    "watcher",
    "parser",
    "endpoint-audio",
    "transport",
    "complete",
}
INPUT_KEYS = {
    "caseDirectory",
    "containmentManifest",
    "controlOracle",
    "driverExe",
    "endpointAudioOracle",
    "fixtureBytes",
    "fixtureManifest",
    "hostContract",
    "hostRunner",
    "instrumentedOracle",
    "parser",
    "parecExecutable",
    "pactlExecutable",
    "protonTool",
    "pulseaudioExecutable",
    "pythonExecutable",
    "scenario",
    "schedulerPressureHelper",
    "serviceConfig",
    "steamRuntime",
    "transportOracle",
}
OPTIONAL_INPUT_KEYS = {"parecExecutable"}
ARTIFACT_NAMES = {
    "driverConsole": "driver-console.log",
    "driverJsonl": "driver.jsonl",
    "endpointAudioCaptureLog": "endpoint-audio-capture.log",
    "endpointAudioLog": "endpoint-audio-score.log",
    "endpointAudioRaw": "endpoint-audio.raw",
    "endpointAudioSummary": "endpoint-audio-summary.json",
    "fixtureCompletion": "http-complete.json",
    "fixtureLog": "http-requests.jsonl",
    "parserLog": "parser-console.log",
    "parserSummary": "parser-summary.json",
    "protonLog": "proton.log",
    "schedulerPressureSummary": "scheduler-pressure-summary.json",
    "transportLog": "transport-score.log",
    "transportSummary": "transport-summary.json",
    "transportWatch": "transport-watch.json",
    "transportWatchLog": "transport-watch.log",
}


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    require(set(value) == keys, f"{label} keys differ: {sorted(set(value) ^ keys)}")
    return value


def require_sha256(value: Any, label: str) -> None:
    require(type(value) is str and SHA256_RE.fullmatch(value) is not None,
            f"{label} is not lowercase SHA-256")


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write(path: Path, value: Any) -> None:
    payload = canonical_bytes(value)
    require(len(payload) <= MAX_MANIFEST_BYTES, "manifest exceeds the size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.replace(temporary, path)


def read_manifest(path: Path) -> Any:
    require(path.is_file() and not path.is_symlink(), "manifest is not a regular nonsymlink file")
    payload = path.read_bytes()
    require(0 < len(payload) <= MAX_MANIFEST_BYTES, "manifest is empty or oversized")
    require(b"\x00" not in payload, "manifest contains a NUL byte")
    value = json.loads(payload.decode("utf-8"))
    require(payload == canonical_bytes(value), "manifest is not canonical JSON")
    return value


def validate_plan(value: Any) -> dict[str, Any]:
    plan = exact_object(
        value,
        {
            "appId",
            "audioEndpoint",
            "buildRole",
            "case",
            "caseRole",
            "digestSchema",
            "fixtureNetwork",
            "graphicsBoundary",
            "hostSteamDiscovery",
            "hostSteamWriteAccess",
            "inputSha256",
            "instrumentation",
            "mediaKind",
            "networkBoundary",
            "protonInvocation",
            "python",
            "runtimeState",
            "schedulerPressure",
            "schema",
            "steamClientUsed",
            "suite",
        },
        "host input manifest",
    )
    require(type(plan["schema"]) is int and plan["schema"] == SCHEMA, "unsupported schema")
    require(plan["suite"] == "rtsp-media-host-runtime", "unexpected suite")
    require(plan["case"] == "media-engine-stress", "unexpected case")
    require(type(plan["appId"]) is int and 990000 <= plan["appId"] <= 999999,
            "AppID is outside the reserved synthetic range")
    require(plan["appId"] != 438100, "VRChat AppID is forbidden")
    require(plan["audioEndpoint"] == "private-pulseaudio-null-sink-s16le-48000-stereo",
            "unexpected audio endpoint")
    require(plan["buildRole"] in BUILD_ROLES, "invalid build role")
    require(plan["caseRole"] in CASE_ROLES, "invalid case role")
    require(plan["mediaKind"] in MEDIA_KINDS, "invalid media kind")
    require(plan["instrumentation"] in INSTRUMENTATION, "invalid instrumentation")
    require(plan["caseRole"] != "negative-control" or plan["instrumentation"] == "audio-monitor",
            "negative controls require decoded-audio instrumentation")
    require(plan["digestSchema"] == "sha256-file-or-tree-v1", "unexpected digest schema")
    require(plan["fixtureNetwork"] in FIXTURE_NETWORKS,
            "fixture network is not a declared loopback transport")
    require(plan["graphicsBoundary"] == "explicit-existing-x11-display-opt-in",
            "host graphics was not explicitly bounded")
    require(plan["hostSteamDiscovery"] is False, "host Steam discovery is enabled")
    require(plan["hostSteamWriteAccess"] is False, "host Steam write access is enabled")
    require(plan["steamClientUsed"] is False, "Steam client use is enabled")
    require(plan["runtimeState"] == "fresh-private-var-tmp-purged-after-teardown",
            "unexpected runtime-state policy")
    pressure = exact_object(
        plan["schedulerPressure"],
        {
            "applicationCpuSet",
            "fixtureCpuSet",
            "minimumAggregateDriverCpuTimePermille",
            "minimumDriverDurationMs",
            "minimumWorkerDriverCpuTimePermille",
            "profile",
            "workerCount",
        },
        "scheduler pressure",
    )
    require(pressure.get("profile") in SCHEDULER_PRESSURE,
            "unknown scheduler-pressure profile")
    require(pressure == SCHEDULER_PRESSURE[pressure["profile"]],
            "scheduler-pressure profile parameters differ")
    require(plan["networkBoundary"] == {
        "interfaces": ["lo"],
        "internet": "unreachable",
        "ipv4RouteTable": "empty",
        "mediaEndpointPolicy": "declared-ipv4-loopback-only",
        "proxyEnvironment": "absent",
    }, "unexpected network boundary")
    require(plan["protonInvocation"] == {
        "compatToolVerb": "runinprefix",
        "prefixSetupVerb": "getcompatpath",
        "runtimeWaitVerb": "waitforexitandrun",
        "targetBoundary": "direct-media-driver",
    }, "unexpected Proton invocation boundary")
    python = exact_object(plan["python"], {"implementationVersion", "minimum"}, "python")
    require(type(python["implementationVersion"]) is str
            and PYTHON_VERSION_RE.fullmatch(python["implementationVersion"]),
            "invalid Python implementation/version")
    require(python["minimum"] == "3.11", "unexpected Python minimum")
    identities = exact_object(plan["inputSha256"], INPUT_KEYS, "input identities")
    for name, identity in identities.items():
        if name in OPTIONAL_INPUT_KEYS and plan["instrumentation"] != "endpoint-monitor":
            require(identity is None, f"unused input {name} must be null")
        else:
            require_sha256(identity, f"input {name}")
    require(plan["instrumentation"] != "endpoint-monitor" or plan["mediaKind"] != "video-only",
            "endpoint monitoring cannot be selected for video-only media")
    return plan


def validate_evidence(value: Any) -> dict[str, Any]:
    evidence = exact_object(
        value,
        {
            "artifactsSha256",
            "case",
            "exit",
            "inputManifestSha256",
            "instrumentation",
            "outcome",
            "schedulerPressureProfile",
            "schema",
            "stage",
            "status",
            "suite",
        },
        "host evidence manifest",
    )
    require(type(evidence["schema"]) is int and evidence["schema"] == SCHEMA, "unsupported schema")
    require(evidence["suite"] == "rtsp-media-host-runtime", "unexpected suite")
    require(evidence["case"] == "media-engine-stress", "unexpected case")
    require(evidence["instrumentation"] in INSTRUMENTATION, "invalid instrumentation")
    require(evidence["schedulerPressureProfile"] in SCHEDULER_PRESSURE,
            "invalid scheduler-pressure profile")
    require(evidence["outcome"] in OUTCOMES, "invalid outcome")
    require(evidence["outcome"] != "rejected-as-expected"
            or evidence["instrumentation"] == "audio-monitor",
            "expected oracle rejection requires decoded-audio instrumentation")
    require(evidence["stage"] in STAGES, "invalid stage")
    expected_status = "passed" if evidence["outcome"] in {"accepted", "rejected-as-expected"} else "failed"
    require(evidence["status"] == expected_status, "status and outcome differ")
    if expected_status == "passed":
        require(evidence["stage"] == "complete", "passing evidence is not complete")
    require_sha256(evidence["inputManifestSha256"], "input manifest identity")
    exits = exact_object(evidence["exit"], {"endpointAudio", "parser", "process", "watcher"}, "exit")
    for name, code in exits.items():
        require(type(code) is int and -1 <= code <= 255, f"{name} exit is outside [-1, 255]")
    artifacts = exact_object(evidence["artifactsSha256"], set(ARTIFACT_NAMES), "artifact identities")
    for name, identity in artifacts.items():
        require(identity is None or (type(identity) is str and SHA256_RE.fullmatch(identity)),
                f"artifact {name} identity is invalid")
    if expected_status == "passed":
        for name in ("driverJsonl", "fixtureCompletion", "fixtureLog", "parserLog",
                     "parserSummary", "schedulerPressureSummary",
                     "transportSummary", "transportWatch"):
            require(artifacts[name] is not None, f"passing evidence has no {name}")
        endpoint_artifacts = (
            "endpointAudioCaptureLog",
            "endpointAudioLog",
            "endpointAudioRaw",
            "endpointAudioSummary",
        )
        if evidence["instrumentation"] == "endpoint-monitor":
            for name in endpoint_artifacts:
                require(artifacts[name] is not None, f"passing endpoint evidence has no {name}")
            expected_endpoint_exit = 0
        else:
            for name in endpoint_artifacts:
                require(artifacts[name] is None, f"non-endpoint evidence unexpectedly has {name}")
            expected_endpoint_exit = -1
        expected_parser_exit = 1 if evidence["outcome"] == "rejected-as-expected" else 0
        require(exits == {
            "endpointAudio": expected_endpoint_exit,
            "parser": expected_parser_exit,
            "process": 0,
            "watcher": 0,
        },
                "passing evidence has an unexpected exit")
    return evidence


def sha256_file(path: Path, *, allow_empty: bool = True) -> str:
    info = path.lstat()
    require(path.is_file() and not path.is_symlink(), "evidence artifact is not a regular nonsymlink file")
    require(info.st_size <= MAX_ARTIFACT_BYTES, "evidence artifact exceeds the hashing limit")
    require(allow_empty or info.st_size > 0, "required evidence artifact is empty")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_scheduler_pressure_summary(value: Any,
                                        expected_profile: str) -> dict[str, Any]:
    require(expected_profile in SCHEDULER_PRESSURE,
            "unknown expected scheduler-pressure profile")
    profile = SCHEDULER_PRESSURE[expected_profile]
    if expected_profile == "none":
        summary = exact_object(
            value,
            {
                "applicationCpuSet",
                "clockBasis",
                "fixtureCpuSet",
                "profile",
                "schema",
                "status",
                "workerCount",
            },
            "disabled scheduler-pressure summary",
        )
        require(summary == {
            "applicationCpuSet": [],
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "fixtureCpuSet": [],
            "profile": "none",
            "schema": 1,
            "status": "disabled",
            "workerCount": 0,
        }, "disabled scheduler-pressure summary differs")
        return summary

    summary = exact_object(
        value,
        {
            "aggregateDriverCpuTimeLowerBoundNs",
            "aggregateDriverCpuTimeLowerBoundPermille",
            "allWorkersReadyMonotonicNs",
            "allWorkersStartedMonotonicNs",
            "applicationCpuSet",
            "clockBasis",
            "driverExitedMonotonicNs",
            "driverPressureDurationMs",
            "driverStartedMonotonicNs",
            "fixtureCpuSet",
            "loadStartedMonotonicNs",
            "loadStoppedMonotonicNs",
            "minimumWorkerDriverCpuTimeLowerBoundPermille",
            "profile",
            "schema",
            "status",
            "workerCount",
            "workers",
        },
        "scheduler-pressure summary",
    )
    require(summary["schema"] == 1
            and summary["clockBasis"] == "linux-clock-monotonic-raw-v1"
            and summary["profile"] == expected_profile
            and summary["status"] == "complete",
            "scheduler-pressure summary identity differs")
    require(summary["applicationCpuSet"] == profile["applicationCpuSet"]
            and summary["fixtureCpuSet"] == profile["fixtureCpuSet"]
            and summary["workerCount"] == profile["workerCount"],
            "scheduler-pressure summary profile differs")
    integer_keys = (
        "aggregateDriverCpuTimeLowerBoundNs",
        "aggregateDriverCpuTimeLowerBoundPermille",
        "allWorkersReadyMonotonicNs",
        "allWorkersStartedMonotonicNs",
        "driverExitedMonotonicNs",
        "driverPressureDurationMs",
        "driverStartedMonotonicNs",
        "loadStartedMonotonicNs",
        "loadStoppedMonotonicNs",
        "minimumWorkerDriverCpuTimeLowerBoundPermille",
    )
    for key in integer_keys:
        require(type(summary[key]) is int and summary[key] > 0,
                f"scheduler-pressure summary {key} is invalid")
    started = summary["driverStartedMonotonicNs"]
    exited = summary["driverExitedMonotonicNs"]
    require(summary["loadStartedMonotonicNs"]
            <= summary["allWorkersReadyMonotonicNs"]
            <= summary["allWorkersStartedMonotonicNs"]
            <= started < exited
            <= summary["loadStoppedMonotonicNs"],
            "scheduler-pressure summary clock order differs")
    duration_ns = exited - started
    require(summary["driverPressureDurationMs"] == duration_ns // 1_000_000
            and summary["driverPressureDurationMs"] >= profile["minimumDriverDurationMs"],
            "scheduler-pressure driver duration differs")
    workers = summary["workers"]
    require(type(workers) is list and len(workers) == profile["workerCount"],
            "scheduler-pressure summary worker count differs")
    lower_bounds = []
    lower_permilles = []
    observed_cpus = []
    worker_keys = {
        "cpu",
        "cpuTimeNs",
        "driverCpuTimeLowerBoundNs",
        "driverCpuTimeLowerBoundPermille",
        "iterations",
        "pressureStartedMonotonicNs",
        "readyMonotonicNs",
        "stoppedMonotonicNs",
    }
    for item in workers:
        worker = exact_object(item, worker_keys, "scheduler-pressure summary worker")
        observed_cpus.append(worker["cpu"])
        for key in worker_keys - {"cpu"}:
            require(type(worker[key]) is int and worker[key] > 0,
                    f"scheduler-pressure summary worker {key} is invalid")
        require(summary["loadStartedMonotonicNs"] <= worker["readyMonotonicNs"]
                <= summary["allWorkersReadyMonotonicNs"]
                <= worker["pressureStartedMonotonicNs"]
                <= summary["allWorkersStartedMonotonicNs"]
                <= worker["stoppedMonotonicNs"]
                <= summary["loadStoppedMonotonicNs"],
                "scheduler-pressure summary worker clock order differs")
        outside_driver_ns = (
            started - worker["pressureStartedMonotonicNs"]
            + worker["stoppedMonotonicNs"] - exited
        )
        expected_lower_bound = max(0, worker["cpuTimeNs"] - outside_driver_ns)
        expected_permille = expected_lower_bound * 1000 // duration_ns
        require(worker["driverCpuTimeLowerBoundNs"] == expected_lower_bound
                and worker["driverCpuTimeLowerBoundPermille"] == expected_permille,
                "scheduler-pressure worker lower bound differs")
        lower_bounds.append(expected_lower_bound)
        lower_permilles.append(expected_permille)
    require(observed_cpus == profile["applicationCpuSet"],
            "scheduler-pressure summary worker CPU identities differ")
    aggregate = sum(lower_bounds)
    aggregate_permille = aggregate * 1000 // duration_ns
    minimum_worker_permille = min(lower_permilles)
    require(summary["aggregateDriverCpuTimeLowerBoundNs"] == aggregate
            and summary["aggregateDriverCpuTimeLowerBoundPermille"] == aggregate_permille
            and summary["minimumWorkerDriverCpuTimeLowerBoundPermille"]
            == minimum_worker_permille,
            "scheduler-pressure aggregate lower bound differs")
    require(aggregate_permille >= profile["minimumAggregateDriverCpuTimePermille"],
            "scheduler-pressure aggregate lower bound is below its floor")
    require(minimum_worker_permille >= profile["minimumWorkerDriverCpuTimePermille"],
            "scheduler-pressure worker lower bound is below its floor")
    return summary


def command_write_plan(args: argparse.Namespace) -> None:
    identities = {name: getattr(args, _argument_name(name)) for name in sorted(INPUT_KEYS)}
    value = {
        "appId": args.app_id,
        "audioEndpoint": "private-pulseaudio-null-sink-s16le-48000-stereo",
        "buildRole": args.build_role,
        "case": "media-engine-stress",
        "caseRole": args.case_role,
        "digestSchema": "sha256-file-or-tree-v1",
        "fixtureNetwork": args.fixture_network,
        "graphicsBoundary": "explicit-existing-x11-display-opt-in",
        "hostSteamDiscovery": False,
        "hostSteamWriteAccess": False,
        "inputSha256": identities,
        "instrumentation": args.instrumentation,
        "mediaKind": args.media_kind,
        "networkBoundary": {
            "interfaces": ["lo"],
            "internet": "unreachable",
            "ipv4RouteTable": "empty",
            "mediaEndpointPolicy": "declared-ipv4-loopback-only",
            "proxyEnvironment": "absent",
        },
        "protonInvocation": {
            "compatToolVerb": "runinprefix",
            "prefixSetupVerb": "getcompatpath",
            "runtimeWaitVerb": "waitforexitandrun",
            "targetBoundary": "direct-media-driver",
        },
        "python": {"implementationVersion": args.python_version, "minimum": "3.11"},
        "runtimeState": "fresh-private-var-tmp-purged-after-teardown",
        "schedulerPressure": SCHEDULER_PRESSURE[args.scheduler_pressure_profile],
        "schema": SCHEMA,
        "steamClientUsed": False,
        "suite": "rtsp-media-host-runtime",
    }
    validate_plan(value)
    atomic_write(args.output, value)


def _argument_name(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def command_write_evidence(args: argparse.Namespace) -> None:
    plan = read_manifest(args.plan)
    validate_plan(plan)
    plan_identity = sha256_file(args.plan, allow_empty=False)
    root = args.results_root.resolve(strict=True)
    require(root.is_dir() and not root.is_symlink(), "results root is not a nonsymlink directory")
    require(str(root).startswith("/var/tmp/rtsp-media-host."),
            "results root is outside a fresh host-run directory")
    artifacts: dict[str, str | None] = {}
    for label, filename in ARTIFACT_NAMES.items():
        path = root / filename
        if not path.exists():
            artifacts[label] = None
            continue
        require(path.parent == root, "evidence artifact escaped the results root")
        artifacts[label] = sha256_file(path)
    pressure_summary_path = root / ARTIFACT_NAMES["schedulerPressureSummary"]
    if pressure_summary_path.exists():
        validate_scheduler_pressure_summary(
            read_manifest(pressure_summary_path),
            plan["schedulerPressure"]["profile"],
        )
    if args.outcome in {"accepted", "rejected-as-expected"}:
        require(pressure_summary_path.exists(),
                "passing evidence has no scheduler-pressure summary")
    value = {
        "artifactsSha256": artifacts,
        "case": "media-engine-stress",
        "exit": {
            "endpointAudio": args.endpoint_audio_exit,
            "parser": args.parser_exit,
            "process": args.process_exit,
            "watcher": args.watcher_exit,
        },
        "inputManifestSha256": plan_identity,
        "instrumentation": plan["instrumentation"],
        "outcome": args.outcome,
        "schedulerPressureProfile": plan["schedulerPressure"]["profile"],
        "schema": SCHEMA,
        "stage": args.stage,
        "status": "passed" if args.outcome in {"accepted", "rejected-as-expected"} else "failed",
        "suite": "rtsp-media-host-runtime",
    }
    validate_evidence(value)
    atomic_write(args.output, value)


def command_validate(args: argparse.Namespace) -> None:
    value = read_manifest(args.path)
    (validate_plan if args.kind == "plan" else validate_evidence)(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("write-plan")
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--app-id", required=True, type=int)
    plan.add_argument("--build-role", required=True, choices=sorted(BUILD_ROLES))
    plan.add_argument("--case-role", required=True, choices=sorted(CASE_ROLES))
    plan.add_argument("--media-kind", required=True, choices=sorted(MEDIA_KINDS))
    plan.add_argument("--instrumentation", required=True, choices=sorted(INSTRUMENTATION))
    plan.add_argument("--fixture-network", required=True, choices=sorted(FIXTURE_NETWORKS))
    plan.add_argument("--python-version", required=True)
    plan.add_argument(
        "--scheduler-pressure-profile",
        choices=sorted(SCHEDULER_PRESSURE),
        required=True,
    )
    for name in sorted(INPUT_KEYS):
        plan.add_argument(
            "--" + re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower(),
            required=name not in OPTIONAL_INPUT_KEYS,
        )
    plan.set_defaults(function=command_write_plan)

    evidence = subparsers.add_parser("write-evidence")
    evidence.add_argument("--output", required=True, type=Path)
    evidence.add_argument("--plan", required=True, type=Path)
    evidence.add_argument("--results-root", required=True, type=Path)
    evidence.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    evidence.add_argument("--stage", required=True, choices=sorted(STAGES))
    evidence.add_argument("--process-exit", type=int, default=-1)
    evidence.add_argument("--watcher-exit", type=int, default=-1)
    evidence.add_argument("--parser-exit", type=int, default=-1)
    evidence.add_argument("--endpoint-audio-exit", type=int, default=-1)
    evidence.set_defaults(function=command_write_evidence)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--kind", choices=("plan", "evidence"), required=True)
    validate.add_argument("--path", required=True, type=Path)
    validate.set_defaults(function=command_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (ContractError, json.JSONDecodeError, OSError, UnicodeError, ValueError) as error:
        print(f"host run contract: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
