# SPDX-License-Identifier: BSD-3-Clause
"""Pure tests for the contained scheduler-pressure helper."""

import copy
import json
from pathlib import Path
import tempfile
import types
import unittest

import scheduler_pressure as pressure


def raw_summary() -> dict[str, object]:
    started = 1_000_000_000
    ready = 1_100_000_000
    pressure_started = 1_200_000_000
    stopped = 12_100_000_000
    return {
        "allWorkersReadyMonotonicNs": ready,
        "allWorkersStartedMonotonicNs": pressure_started,
        "applicationCpuSet": list(pressure.APPLICATION_CPUS),
        "clockBasis": pressure.CLOCK_BASIS,
        "fixtureCpuSet": list(pressure.FIXTURE_CPUS),
        "loadStartedMonotonicNs": started,
        "loadStoppedMonotonicNs": stopped,
        "profile": pressure.PROFILE_V1,
        "schema": pressure.SCHEMA,
        "status": "complete",
        "workerCount": pressure.WORKER_COUNT,
        "workers": [
            {
                "cpu": cpu,
                "cpuTimeNs": 4_500_000_000,
                "iterations": 10_000,
                "pressureStartedMonotonicNs": pressure_started,
                "readyMonotonicNs": ready,
                "stoppedMonotonicNs": stopped - 50_000_000,
            }
            for cpu in pressure.APPLICATION_CPUS
        ],
    }


class SchedulerPressureTests(unittest.TestCase):
    def test_profile_is_one_exact_six_core_application_partition(self) -> None:
        self.assertEqual(
            pressure.profile_document(),
            {
                "applicationCpuSet": list(range(12)),
                "fixtureCpuSet": list(range(12, 16)),
                "minimumAggregateDriverCpuTimePermille": 4000,
                "minimumDriverDurationMs": 5000,
                "minimumWorkerDriverCpuTimePermille": 200,
                "profile": "scheduler-pressure-v1",
                "workerCount": 12,
            },
        )
        self.assertEqual(pressure.affinity_for_role("application"), tuple(range(12)))
        self.assertEqual(pressure.affinity_for_role("fixture"), tuple(range(12, 16)))
        with self.assertRaises(pressure.PressureError):
            pressure.affinity_for_role("ambient")

    def test_disabled_summary_is_path_free_and_canonical(self) -> None:
        value = pressure.disabled_document()
        self.assertEqual(
            set(value),
            pressure.DISABLED_KEYS,
        )
        self.assertEqual(value["profile"], pressure.PROFILE_NONE)
        payload = pressure.canonical_bytes(value)
        self.assertEqual(json.loads(payload), value)
        self.assertTrue(payload.endswith(b"\n"))

    def test_seal_requires_workers_to_span_driver_and_meet_cpu_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.json"
            output = root / "summary.json"
            source.write_bytes(pressure.canonical_bytes(raw_summary()))
            pressure.command_seal(types.SimpleNamespace(
                raw_summary=source,
                output=output,
                driver_started_ns=2_000_000_000,
                driver_exited_ns=12_000_000_000,
            ))
            summary = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(set(summary), pressure.SUMMARY_KEYS)
            self.assertEqual(summary["driverPressureDurationMs"], 10_000)
            self.assertEqual(
                summary["aggregateDriverCpuTimeLowerBoundPermille"],
                4_380,
            )
            self.assertEqual(
                summary["minimumWorkerDriverCpuTimeLowerBoundPermille"],
                365,
            )
            self.assertTrue(all(
                set(worker) == pressure.SUMMARY_WORKER_KEYS
                for worker in summary["workers"]
            ))

            early = copy.deepcopy(raw_summary())
            early["allWorkersReadyMonotonicNs"] = 1_900_000_000
            early["allWorkersStartedMonotonicNs"] = 2_000_000_001
            for worker in early["workers"]:
                worker["pressureStartedMonotonicNs"] = 2_000_000_001
            source.write_bytes(pressure.canonical_bytes(early))
            with self.assertRaisesRegex(pressure.PressureError, "complete driver interval"):
                pressure.command_seal(types.SimpleNamespace(
                    raw_summary=source,
                    output=root / "early.json",
                    driver_started_ns=2_000_000_000,
                    driver_exited_ns=12_000_000_000,
                ))

            inflated_outside_interval = copy.deepcopy(raw_summary())
            inflated_outside_interval["loadStoppedMonotonicNs"] = 21_200_000_000
            for worker in inflated_outside_interval["workers"]:
                worker["cpuTimeNs"] = 9_200_000_000
                worker["stoppedMonotonicNs"] = 21_100_000_000
            source.write_bytes(pressure.canonical_bytes(inflated_outside_interval))
            with self.assertRaisesRegex(pressure.PressureError, "four CPU equivalents"):
                pressure.command_seal(types.SimpleNamespace(
                    raw_summary=source,
                    output=root / "outside.json",
                    driver_started_ns=11_000_000_000,
                    driver_exited_ns=21_000_000_000,
                ))

            weak = copy.deepcopy(raw_summary())
            for worker in weak["workers"]:
                worker["cpuTimeNs"] = 1_000_000_000
            source.write_bytes(pressure.canonical_bytes(weak))
            with self.assertRaisesRegex(pressure.PressureError, "four CPU equivalents"):
                pressure.command_seal(types.SimpleNamespace(
                    raw_summary=source,
                    output=root / "weak.json",
                    driver_started_ns=2_000_000_000,
                    driver_exited_ns=12_000_000_000,
                ))

    def test_raw_summary_rejects_missing_or_reassigned_worker(self) -> None:
        pressure.validate_raw(raw_summary())
        for mutation in ("missing", "reassigned", "short-lived"):
            value = copy.deepcopy(raw_summary())
            if mutation == "missing":
                value["workers"].pop()
            elif mutation == "reassigned":
                value["workers"][0]["cpu"] = 15
            else:
                value["workers"][0]["stoppedMonotonicNs"] = (
                    value["allWorkersReadyMonotonicNs"] - 1
                )
            with self.subTest(mutation=mutation), self.assertRaises(pressure.PressureError):
                pressure.validate_raw(value)


if __name__ == "__main__":
    unittest.main()
