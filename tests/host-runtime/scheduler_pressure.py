#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Fail-closed CPU scheduler pressure for contained MediaEngine runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
from multiprocessing.connection import Connection, wait
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any


SCHEMA = 1
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
PROFILE_NONE = "none"
PROFILE_V1 = "scheduler-pressure-v1"
APPLICATION_CPUS = tuple(range(12))
FIXTURE_CPUS = tuple(range(12, 16))
WORKER_COUNT = len(APPLICATION_CPUS)
MINIMUM_DRIVER_DURATION_MS = 5_000
MINIMUM_AGGREGATE_DRIVER_CPU_TIME_PERMILLE = 4_000
MINIMUM_WORKER_DRIVER_CPU_TIME_PERMILLE = 200
READY_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0
HASH_BLOCK = bytes(range(256)) * 256
RAW_KEYS = {
    "allWorkersReadyMonotonicNs",
    "allWorkersStartedMonotonicNs",
    "applicationCpuSet",
    "clockBasis",
    "fixtureCpuSet",
    "loadStartedMonotonicNs",
    "loadStoppedMonotonicNs",
    "profile",
    "schema",
    "status",
    "workerCount",
    "workers",
}
WORKER_KEYS = {
    "cpu",
    "cpuTimeNs",
    "iterations",
    "pressureStartedMonotonicNs",
    "readyMonotonicNs",
    "stoppedMonotonicNs",
}
SUMMARY_KEYS = RAW_KEYS | {
    "aggregateDriverCpuTimeLowerBoundNs",
    "aggregateDriverCpuTimeLowerBoundPermille",
    "driverExitedMonotonicNs",
    "driverPressureDurationMs",
    "driverStartedMonotonicNs",
    "minimumWorkerDriverCpuTimeLowerBoundPermille",
}
SUMMARY_WORKER_KEYS = WORKER_KEYS | {
    "driverCpuTimeLowerBoundNs",
    "driverCpuTimeLowerBoundPermille",
}
DISABLED_KEYS = {
    "applicationCpuSet",
    "clockBasis",
    "fixtureCpuSet",
    "profile",
    "schema",
    "status",
    "workerCount",
}


class PressureError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PressureError(message)


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def atomic_json(path: Path, value: object) -> None:
    require(path.is_absolute(), "output path must be absolute")
    require(path.parent.is_dir() and not path.parent.is_symlink(),
            "output parent must be a nonsymlink directory")
    require(not path.exists(), "output path must be fresh")
    payload = canonical_bytes(value)
    require(len(payload) <= 64 * 1024, "output exceeds 64 KiB")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    require(path.is_file() and not path.is_symlink(), "input is not a regular nonsymlink file")
    payload = path.read_bytes()
    require(0 < len(payload) <= 64 * 1024 and b"\x00" not in payload,
            "input size or encoding is invalid")
    value = json.loads(payload.decode("ascii"))
    require(payload == canonical_bytes(value), "input is not canonical JSON")
    return value


def monotonic_raw_ns() -> int:
    require(hasattr(time, "CLOCK_MONOTONIC_RAW"), "CLOCK_MONOTONIC_RAW is unavailable")
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    require(set(value) == keys, f"{label} keys differ: {sorted(set(value) ^ keys)}")
    return value


def profile_document() -> dict[str, object]:
    return {
        "applicationCpuSet": list(APPLICATION_CPUS),
        "fixtureCpuSet": list(FIXTURE_CPUS),
        "minimumAggregateDriverCpuTimePermille": (
            MINIMUM_AGGREGATE_DRIVER_CPU_TIME_PERMILLE
        ),
        "minimumDriverDurationMs": MINIMUM_DRIVER_DURATION_MS,
        "minimumWorkerDriverCpuTimePermille": MINIMUM_WORKER_DRIVER_CPU_TIME_PERMILLE,
        "profile": PROFILE_V1,
        "workerCount": WORKER_COUNT,
    }


def disabled_document() -> dict[str, object]:
    return {
        "applicationCpuSet": [],
        "clockBasis": CLOCK_BASIS,
        "fixtureCpuSet": [],
        "profile": PROFILE_NONE,
        "schema": SCHEMA,
        "status": "disabled",
        "workerCount": 0,
    }


def validate_host_topology() -> None:
    required = set(APPLICATION_CPUS) | set(FIXTURE_CPUS)
    allowed = os.sched_getaffinity(0)
    require(required <= allowed, "scheduler-pressure-v1 requires logical CPUs 0..15")
    identities: list[tuple[int, int]] = []
    for cpu in sorted(required):
        root = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            package = int((root / "physical_package_id").read_text(encoding="ascii").strip())
            core = int((root / "core_id").read_text(encoding="ascii").strip())
        except (OSError, ValueError) as error:
            raise PressureError(f"cannot identify topology for logical CPU {cpu}") from error
        identities.append((package, core))
    require(len(set(identities)) == 8, "scheduler-pressure-v1 requires eight physical cores")
    for first in range(0, 16, 2):
        require(identities[first] == identities[first + 1],
                "scheduler-pressure-v1 requires adjacent SMT sibling pairs")
    require(len({identities[first] for first in range(0, 16, 2)}) == 8,
            "scheduler-pressure-v1 physical-core identities are not unique")


def affinity_for_role(role: str) -> tuple[int, ...]:
    if role == "application":
        return APPLICATION_CPUS
    if role == "fixture":
        return FIXTURE_CPUS
    raise PressureError("unknown scheduler-pressure affinity role")


def _worker_main(cpu: int, begin: Any, stop: Any, output: Connection) -> None:
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.sched_setaffinity(0, {cpu})
        require(os.sched_getaffinity(0) == {cpu}, "worker affinity did not take effect")
        ready = monotonic_raw_ns()
        output.send({"cpu": cpu, "readyMonotonicNs": ready})
        while not begin.wait(0.05):
            require(not stop.is_set(), "worker stopped before pressure began")
        pressure_started = monotonic_raw_ns()
        started_cpu = time.process_time_ns()
        output.send({
            "cpu": cpu,
            "pressureStartedMonotonicNs": pressure_started,
        })
        iterations = 0
        digest = hashlib.sha256()
        while not stop.is_set():
            for _ in range(64):
                digest.update(HASH_BLOCK)
                iterations += 1
        stopped_cpu = time.process_time_ns()
        output.send({
            "cpu": cpu,
            "cpuTimeNs": stopped_cpu - started_cpu,
            "iterations": iterations,
            "pressureStartedMonotonicNs": pressure_started,
            "readyMonotonicNs": ready,
            "stoppedMonotonicNs": monotonic_raw_ns(),
        })
        output.close()
    except BaseException:
        try:
            output.close()
        finally:
            raise


def command_profile(_args: argparse.Namespace) -> None:
    print(canonical_bytes(profile_document()).decode("ascii"), end="")


def command_now(_args: argparse.Namespace) -> None:
    print(monotonic_raw_ns())


def command_disabled(args: argparse.Namespace) -> None:
    atomic_json(args.output, disabled_document())


def command_exec(args: argparse.Namespace) -> None:
    validate_host_topology()
    cpus = affinity_for_role(args.role)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    require(command and os.path.isabs(command[0]), "exec command must begin with an absolute path")
    os.sched_setaffinity(0, set(cpus))
    require(os.sched_getaffinity(0) == set(cpus), "exec affinity did not take effect")
    os.execvpe(command[0], command, os.environ.copy())


def command_run(args: argparse.Namespace) -> None:
    validate_host_topology()
    require(args.ready.parent == args.raw_summary.parent,
            "ready and raw-summary paths must share one private directory")
    require(not args.ready.exists() and not args.raw_summary.exists(),
            "scheduler-pressure state paths must be fresh")
    os.sched_setaffinity(0, set(APPLICATION_CPUS))

    context = multiprocessing.get_context("fork")
    begin = context.Event()
    stop = context.Event()
    processes: list[multiprocessing.Process] = []
    receivers: dict[Connection, int] = {}
    ready_records: dict[int, dict[str, int]] = {}
    started_records: dict[int, dict[str, int]] = {}
    worker_records: list[dict[str, int]] = []
    started = monotonic_raw_ns()
    interrupted = False

    def request_stop(_number: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    prior_term = signal.signal(signal.SIGTERM, request_stop)
    prior_int = signal.signal(signal.SIGINT, request_stop)
    status = "failed"
    try:
        for cpu in APPLICATION_CPUS:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_worker_main, args=(cpu, begin, stop, sender))
            process.start()
            sender.close()
            processes.append(process)
            receivers[receiver] = cpu

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        pending = set(receivers)
        while pending:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "CPU workers did not become ready within ten seconds")
            readable = wait(pending, timeout=remaining)
            require(readable, "CPU worker readiness timed out")
            for receiver in readable:
                message = exact_object(receiver.recv(), {"cpu", "readyMonotonicNs"},
                                       "CPU worker readiness")
                cpu = receivers[receiver]
                require(message["cpu"] == cpu
                        and type(message["readyMonotonicNs"]) is int
                        and message["readyMonotonicNs"] >= started,
                        "CPU worker readiness identity is invalid")
                ready_records[cpu] = message
                pending.remove(receiver)

        all_ready = monotonic_raw_ns()
        require(not interrupted, "scheduler pressure stopped before workers began")
        begin.set()
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        pending = set(receivers)
        while pending:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "CPU workers did not start within ten seconds")
            readable = wait(pending, timeout=remaining)
            require(readable, "CPU worker start timed out")
            for receiver in readable:
                message = exact_object(
                    receiver.recv(),
                    {"cpu", "pressureStartedMonotonicNs"},
                    "CPU worker start",
                )
                cpu = receivers[receiver]
                require(message["cpu"] == cpu
                        and type(message["pressureStartedMonotonicNs"]) is int
                        and message["pressureStartedMonotonicNs"] >= all_ready,
                        "CPU worker start identity is invalid")
                started_records[cpu] = message
                pending.remove(receiver)

        all_started = max(
            record["pressureStartedMonotonicNs"] for record in started_records.values()
        )
        atomic_json(args.ready, {
            "allWorkersReadyMonotonicNs": all_ready,
            "allWorkersStartedMonotonicNs": all_started,
            "applicationCpuSet": list(APPLICATION_CPUS),
            "clockBasis": CLOCK_BASIS,
            "fixtureCpuSet": list(FIXTURE_CPUS),
            "profile": PROFILE_V1,
            "schema": SCHEMA,
            "workerCount": WORKER_COUNT,
        })

        while not interrupted:
            for process in processes:
                require(process.is_alive(), f"CPU worker {process.name} exited before driver completion")
            time.sleep(0.05)

        stop.set()
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        for process in processes:
            process.join(max(0.0, deadline - time.monotonic()))
        require(all(not process.is_alive() for process in processes),
                "CPU workers did not stop within five seconds")
        require(all(process.exitcode == 0 for process in processes),
                "a CPU worker exited unsuccessfully")
        for receiver, cpu in receivers.items():
            require(receiver.poll(), f"CPU worker {cpu} produced no completion record")
            record = exact_object(receiver.recv(), WORKER_KEYS, "CPU worker completion")
            require(record["cpu"] == cpu, "CPU worker completion identity differs")
            worker_records.append(record)
            receiver.close()
        worker_records.sort(key=lambda record: record["cpu"])
        status = "complete"
    finally:
        stop.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)
        raw = {
            "allWorkersReadyMonotonicNs": (
                max(record["readyMonotonicNs"] for record in ready_records.values())
                if ready_records else None
            ),
            "allWorkersStartedMonotonicNs": (
                max(record["pressureStartedMonotonicNs"]
                    for record in started_records.values())
                if started_records else None
            ),
            "applicationCpuSet": list(APPLICATION_CPUS),
            "clockBasis": CLOCK_BASIS,
            "fixtureCpuSet": list(FIXTURE_CPUS),
            "loadStartedMonotonicNs": started,
            "loadStoppedMonotonicNs": monotonic_raw_ns(),
            "profile": PROFILE_V1,
            "schema": SCHEMA,
            "status": status,
            "workerCount": WORKER_COUNT,
            "workers": worker_records,
        }
        if not args.raw_summary.exists():
            atomic_json(args.raw_summary, raw)
    require(status == "complete", "scheduler pressure did not complete cleanly")


def validate_raw(value: Any) -> dict[str, Any]:
    raw = exact_object(value, RAW_KEYS, "raw scheduler-pressure summary")
    require(raw["schema"] == SCHEMA and raw["clockBasis"] == CLOCK_BASIS
            and raw["profile"] == PROFILE_V1 and raw["status"] == "complete",
            "raw scheduler-pressure identity differs")
    require(raw["applicationCpuSet"] == list(APPLICATION_CPUS)
            and raw["fixtureCpuSet"] == list(FIXTURE_CPUS)
            and raw["workerCount"] == WORKER_COUNT,
            "raw scheduler-pressure profile differs")
    for key in ("loadStartedMonotonicNs", "allWorkersReadyMonotonicNs",
                "allWorkersStartedMonotonicNs", "loadStoppedMonotonicNs"):
        require(type(raw[key]) is int and raw[key] > 0,
                f"raw scheduler-pressure {key} is invalid")
    require(raw["loadStartedMonotonicNs"] <= raw["allWorkersReadyMonotonicNs"]
            <= raw["allWorkersStartedMonotonicNs"]
            <= raw["loadStoppedMonotonicNs"],
            "raw scheduler-pressure clock order differs")
    require(type(raw["workers"]) is list and len(raw["workers"]) == WORKER_COUNT,
            "raw scheduler-pressure worker count differs")
    observed_cpus = []
    for item in raw["workers"]:
        worker = exact_object(item, WORKER_KEYS, "scheduler-pressure worker")
        observed_cpus.append(worker["cpu"])
        for key in ("cpuTimeNs", "iterations", "pressureStartedMonotonicNs",
                    "readyMonotonicNs", "stoppedMonotonicNs"):
            require(type(worker[key]) is int and worker[key] > 0,
                    f"scheduler-pressure worker {key} is invalid")
        require(raw["loadStartedMonotonicNs"] <= worker["readyMonotonicNs"]
                <= raw["allWorkersReadyMonotonicNs"]
                <= worker["pressureStartedMonotonicNs"]
                <= raw["allWorkersStartedMonotonicNs"]
                <= worker["stoppedMonotonicNs"]
                <= raw["loadStoppedMonotonicNs"],
                "scheduler-pressure worker lifetime does not span the load")
    require(observed_cpus == list(APPLICATION_CPUS),
            "scheduler-pressure worker CPU identities differ")
    return raw


def command_seal(args: argparse.Namespace) -> None:
    raw = validate_raw(read_json(args.raw_summary))
    started = args.driver_started_ns
    exited = args.driver_exited_ns
    require(raw["allWorkersStartedMonotonicNs"] <= started < exited
            <= raw["loadStoppedMonotonicNs"],
            "CPU workers did not span the complete driver interval")
    duration_ns = exited - started
    duration_ms = duration_ns // 1_000_000
    require(duration_ms >= MINIMUM_DRIVER_DURATION_MS,
            "pressured driver interval is shorter than five seconds")
    summary_workers = []
    driver_cpu_lower_bounds = []
    for worker in raw["workers"]:
        outside_driver_ns = (
            started - worker["pressureStartedMonotonicNs"]
            + worker["stoppedMonotonicNs"] - exited
        )
        driver_cpu_lower_bound_ns = max(0, worker["cpuTimeNs"] - outside_driver_ns)
        driver_cpu_lower_bound_permille = (
            driver_cpu_lower_bound_ns * 1000 // duration_ns
        )
        driver_cpu_lower_bounds.append(driver_cpu_lower_bound_ns)
        summary_worker = {
            **worker,
            "driverCpuTimeLowerBoundNs": driver_cpu_lower_bound_ns,
            "driverCpuTimeLowerBoundPermille": driver_cpu_lower_bound_permille,
        }
        exact_object(summary_worker, SUMMARY_WORKER_KEYS,
                     "scheduler-pressure summary worker")
        summary_workers.append(summary_worker)
    aggregate = sum(driver_cpu_lower_bounds)
    aggregate_permille = aggregate * 1000 // duration_ns
    minimum_worker_permille = min(
        worker["driverCpuTimeLowerBoundPermille"] for worker in summary_workers
    )
    require(aggregate_permille >= MINIMUM_AGGREGATE_DRIVER_CPU_TIME_PERMILLE,
            "aggregate CPU pressure is below four CPU equivalents")
    require(minimum_worker_permille >= MINIMUM_WORKER_DRIVER_CPU_TIME_PERMILLE,
            "one CPU worker supplied less than 20 percent of a logical CPU")
    summary = {
        **raw,
        "aggregateDriverCpuTimeLowerBoundNs": aggregate,
        "aggregateDriverCpuTimeLowerBoundPermille": aggregate_permille,
        "driverExitedMonotonicNs": exited,
        "driverPressureDurationMs": duration_ms,
        "driverStartedMonotonicNs": started,
        "minimumWorkerDriverCpuTimeLowerBoundPermille": minimum_worker_permille,
        "workers": summary_workers,
    }
    exact_object(summary, SUMMARY_KEYS, "scheduler-pressure summary")
    atomic_json(args.output, summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    profile = subparsers.add_parser("profile")
    profile.set_defaults(function=command_profile)
    now = subparsers.add_parser("now")
    now.set_defaults(function=command_now)
    disabled = subparsers.add_parser("write-disabled")
    disabled.add_argument("--output", required=True, type=Path)
    disabled.set_defaults(function=command_disabled)
    execute = subparsers.add_parser("exec")
    execute.add_argument("--role", choices=("application", "fixture"), required=True)
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.set_defaults(function=command_exec)
    run = subparsers.add_parser("run")
    run.add_argument("--ready", required=True, type=Path)
    run.add_argument("--raw-summary", required=True, type=Path)
    run.set_defaults(function=command_run)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--raw-summary", required=True, type=Path)
    seal.add_argument("--output", required=True, type=Path)
    seal.add_argument("--driver-started-ns", required=True, type=int)
    seal.add_argument("--driver-exited-ns", required=True, type=int)
    seal.set_defaults(function=command_seal)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (PressureError, json.JSONDecodeError, OSError, EOFError, ValueError) as error:
        print(f"scheduler pressure: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
