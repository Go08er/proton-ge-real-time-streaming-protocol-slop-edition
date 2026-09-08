# SPDX-License-Identifier: BSD-3-Clause
"""Pure contract tests for the host runtime evidence boundary."""

import copy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_run_contract as contract


DIGEST = hashlib.sha256(b"fixture").hexdigest()


def plan() -> dict[str, object]:
    return {
        "appId": 999120,
        "audioEndpoint": "private-pulseaudio-null-sink-s16le-48000-stereo",
        "buildRole": "frozen-regression-control",
        "case": "media-engine-stress",
        "caseRole": "qualification",
        "digestSchema": "sha256-file-or-tree-v1",
        "fixtureNetwork": "http-ipv4-loopback-only",
        "graphicsBoundary": "explicit-existing-x11-display-opt-in",
        "hostSteamDiscovery": False,
        "hostSteamWriteAccess": False,
        "inputSha256": {
            name: (None if name == "parecExecutable" else DIGEST)
            for name in contract.INPUT_KEYS
        },
        "instrumentation": "control",
        "mediaKind": "av",
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
        "python": {"implementationVersion": "CPython 3.14.6", "minimum": "3.11"},
        "runtimeState": "fresh-private-var-tmp-purged-after-teardown",
        "schedulerPressure": copy.deepcopy(contract.SCHEDULER_PRESSURE["none"]),
        "schema": contract.SCHEMA,
        "steamClientUsed": False,
        "suite": "rtsp-media-host-runtime",
    }


def evidence(outcome: str = "accepted",
             instrumentation: str = "control") -> dict[str, object]:
    parser_exit = 1 if outcome == "rejected-as-expected" else 0
    endpoint = instrumentation == "endpoint-monitor"
    return {
        "artifactsSha256": {
            name: (DIGEST if name in {
                "driverJsonl", "fixtureCompletion", "fixtureLog", "parserLog",
                "parserSummary", "schedulerPressureSummary",
                "transportSummary", "transportWatch",
            } or (endpoint and name in {
                "endpointAudioCaptureLog", "endpointAudioLog",
                "endpointAudioRaw", "endpointAudioSummary",
            }) else None)
            for name in contract.ARTIFACT_NAMES
        },
        "case": "media-engine-stress",
        "exit": {
            "endpointAudio": 0 if endpoint else -1,
            "parser": parser_exit,
            "process": 0,
            "watcher": 0,
        },
        "inputManifestSha256": DIGEST,
        "instrumentation": instrumentation,
        "outcome": outcome,
        "schedulerPressureProfile": "none",
        "schema": contract.SCHEMA,
        "stage": "complete",
        "status": "passed",
        "suite": "rtsp-media-host-runtime",
    }


class HostRunContractTests(unittest.TestCase):
    def test_plan_accepts_only_synthetic_appid_and_no_host_steam(self) -> None:
        contract.validate_plan(plan())
        for app_id in (438100, 989999, 1000000):
            value = copy.deepcopy(plan())
            value["appId"] = app_id
            with self.assertRaises(contract.ContractError):
                contract.validate_plan(value)
        value = copy.deepcopy(plan())
        value["hostSteamWriteAccess"] = True
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(value)
        value = copy.deepcopy(plan())
        value["hostSteamDiscovery"] = True
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(value)

    def test_plan_rejects_non_loopback_or_routed_network_policy(self) -> None:
        value = copy.deepcopy(plan())
        value["fixtureNetwork"] = "rtsp-interleaved-tcp-ipv4-loopback-only"
        contract.validate_plan(value)
        value = copy.deepcopy(plan())
        value["fixtureNetwork"] = "host-network"
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(value)

    def test_plan_requires_direct_media_driver_invocation(self) -> None:
        for key, replacement in (
            ("compatToolVerb", "waitforexitandrun"),
            ("prefixSetupVerb", "runinprefix"),
            ("runtimeWaitVerb", "run"),
            ("targetBoundary", "builtin-steam-wrapper"),
        ):
            value = copy.deepcopy(plan())
            value["protonInvocation"][key] = replacement
            with self.assertRaises(contract.ContractError):
                contract.validate_plan(value)
        value = copy.deepcopy(plan())
        value["networkBoundary"]["internet"] = "reachable"
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(value)

    def test_plan_binds_the_exact_scheduler_pressure_profile(self) -> None:
        value = copy.deepcopy(plan())
        value["schedulerPressure"] = copy.deepcopy(
            contract.SCHEDULER_PRESSURE["scheduler-pressure-v1"]
        )
        contract.validate_plan(value)
        for mutation in ("cpu-set", "worker-count", "floor", "unknown"):
            changed = copy.deepcopy(value)
            if mutation == "cpu-set":
                changed["schedulerPressure"]["applicationCpuSet"].pop()
            elif mutation == "worker-count":
                changed["schedulerPressure"]["workerCount"] = 11
            elif mutation == "floor":
                changed["schedulerPressure"][
                    "minimumAggregateDriverCpuTimePermille"
                ] = 3999
            else:
                changed["schedulerPressure"]["profile"] = "ambient-load"
            with self.subTest(mutation=mutation), self.assertRaises(contract.ContractError):
                contract.validate_plan(changed)

    def test_scheduler_pressure_summary_matches_plan_and_lower_bounds(self) -> None:
        disabled = {
            "applicationCpuSet": [],
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "fixtureCpuSet": [],
            "profile": "none",
            "schema": 1,
            "status": "disabled",
            "workerCount": 0,
        }
        contract.validate_scheduler_pressure_summary(disabled, "none")
        with self.assertRaises(contract.ContractError):
            contract.validate_scheduler_pressure_summary(
                disabled,
                "scheduler-pressure-v1",
            )

        driver_started = 2_000_000_000
        driver_exited = 12_000_000_000
        pressure_started = 1_200_000_000
        worker_stopped = 12_050_000_000
        cpu_time = 4_500_000_000
        lower_bound = 3_650_000_000
        worker_permille = 365
        summary = {
            "aggregateDriverCpuTimeLowerBoundNs": lower_bound * 12,
            "aggregateDriverCpuTimeLowerBoundPermille": 4380,
            "allWorkersReadyMonotonicNs": 1_100_000_000,
            "allWorkersStartedMonotonicNs": pressure_started,
            "applicationCpuSet": list(range(12)),
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "driverExitedMonotonicNs": driver_exited,
            "driverPressureDurationMs": 10_000,
            "driverStartedMonotonicNs": driver_started,
            "fixtureCpuSet": list(range(12, 16)),
            "loadStartedMonotonicNs": 1_000_000_000,
            "loadStoppedMonotonicNs": 12_100_000_000,
            "minimumWorkerDriverCpuTimeLowerBoundPermille": worker_permille,
            "profile": "scheduler-pressure-v1",
            "schema": 1,
            "status": "complete",
            "workerCount": 12,
            "workers": [
                {
                    "cpu": cpu,
                    "cpuTimeNs": cpu_time,
                    "driverCpuTimeLowerBoundNs": lower_bound,
                    "driverCpuTimeLowerBoundPermille": worker_permille,
                    "iterations": 10_000,
                    "pressureStartedMonotonicNs": pressure_started,
                    "readyMonotonicNs": 1_100_000_000,
                    "stoppedMonotonicNs": worker_stopped,
                }
                for cpu in range(12)
            ],
        }
        contract.validate_scheduler_pressure_summary(
            summary,
            "scheduler-pressure-v1",
        )
        changed = copy.deepcopy(summary)
        changed["workers"][0]["driverCpuTimeLowerBoundNs"] += 1
        with self.assertRaises(contract.ContractError):
            contract.validate_scheduler_pressure_summary(
                changed,
                "scheduler-pressure-v1",
            )

    def test_endpoint_plan_requires_parec_and_rejects_video_only(self) -> None:
        value = copy.deepcopy(plan())
        value["instrumentation"] = "endpoint-monitor"
        value["inputSha256"]["parecExecutable"] = DIGEST
        contract.validate_plan(value)
        missing = copy.deepcopy(value)
        missing["inputSha256"]["parecExecutable"] = None
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(missing)
        video = copy.deepcopy(value)
        video["mediaKind"] = "video-only"
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(video)
        unused = copy.deepcopy(plan())
        unused["inputSha256"]["parecExecutable"] = DIGEST
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(unused)
        negative = copy.deepcopy(value)
        negative["caseRole"] = "negative-control"
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(negative)
        value = copy.deepcopy(plan())
        value["networkBoundary"]["proxyEnvironment"] = "inherited"
        with self.assertRaises(contract.ContractError):
            contract.validate_plan(value)

    def test_success_evidence_requires_complete_oracle_artifacts(self) -> None:
        contract.validate_evidence(evidence())
        value = copy.deepcopy(evidence())
        value["artifactsSha256"]["transportSummary"] = None
        with self.assertRaises(contract.ContractError):
            contract.validate_evidence(value)

    def test_endpoint_success_requires_endpoint_artifacts_and_exit(self) -> None:
        contract.validate_evidence(evidence(instrumentation="endpoint-monitor"))
        for name in (
            "endpointAudioCaptureLog",
            "endpointAudioLog",
            "endpointAudioRaw",
            "endpointAudioSummary",
        ):
            value = copy.deepcopy(evidence(instrumentation="endpoint-monitor"))
            value["artifactsSha256"][name] = None
            with self.subTest(name=name), self.assertRaises(contract.ContractError):
                contract.validate_evidence(value)
        value = copy.deepcopy(evidence(instrumentation="endpoint-monitor"))
        value["exit"]["endpointAudio"] = 2
        with self.assertRaises(contract.ContractError):
            contract.validate_evidence(value)

    def test_expected_negative_control_has_parser_exit_one(self) -> None:
        contract.validate_evidence(evidence("rejected-as-expected", "audio-monitor"))
        value = copy.deepcopy(evidence("rejected-as-expected", "audio-monitor"))
        value["exit"]["parser"] = 0
        with self.assertRaises(contract.ContractError):
            contract.validate_evidence(value)
        endpoint = evidence("rejected-as-expected", "endpoint-monitor")
        with self.assertRaises(contract.ContractError):
            contract.validate_evidence(endpoint)

    def test_failed_evidence_may_be_partial_but_cannot_claim_pass(self) -> None:
        value = evidence()
        value["outcome"] = "driver-failed"
        value["stage"] = "driver"
        value["status"] = "failed"
        value["exit"] = {"endpointAudio": -1, "parser": -1, "process": 5, "watcher": -1}
        value["artifactsSha256"] = {name: None for name in contract.ARTIFACT_NAMES}
        contract.validate_evidence(value)
        value["status"] = "passed"
        with self.assertRaises(contract.ContractError):
            contract.validate_evidence(value)

    def test_manifests_are_canonical_and_path_free(self) -> None:
        payload = contract.canonical_bytes(plan())
        self.assertTrue(payload.endswith(b"\n"))
        personal_home_prefix = b"/" + b"home/"
        self.assertNotIn(personal_home_prefix, payload)
        self.assertNotIn(b"/nix/store/", payload)


if __name__ == "__main__":
    unittest.main()
