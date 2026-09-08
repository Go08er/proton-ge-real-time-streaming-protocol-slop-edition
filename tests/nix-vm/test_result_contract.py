# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest

import result_contract


def smoke_result() -> dict:
    return {
        "case": "isolation-smoke",
        "hostShares": False,
        "network": "loopback-only",
        "runtimeProbe": False,
        "schema": 1,
        "status": "passed",
        "suite": "rtsp-media-lab",
    }


def runtime_result() -> dict:
    return {
        "case": "runtime-probe",
        "expected": "candidate-pass",
        "parserAccepted": True,
        "processExit": 0,
        "schema": 1,
        "status": "passed",
        "suite": "rtsp-media-lab",
    }


def fault_result() -> dict:
    return {
        "case": "targeted-client-impairment",
        "control": "client-b",
        "faultOwner": "origin-egress",
        "measurements": {
            "blackholeControlServed": True,
            "blackholeTargetBlocked": True,
            "cleanControlSeconds": 0.02,
            "cleanTargetSeconds": 0.02,
            "delayControlSeconds": 0.03,
            "delayTargetSeconds": 0.28,
            "recoveryTargetServed": True,
        },
        "profiles": result_contract.FAULT_PROFILES,
        "schema": 1,
        "status": "passed",
        "suite": "rtsp-media-fault-lab",
        "target": "client-a",
    }


def stress_result() -> dict:
    return {
        "audioEndpoint": "pulseaudio-null-sink-s16le-48000-stereo",
        "buildRole": "streaming-base-candidate",
        "case": "media-engine-stress",
        "caseRole": "qualification",
        "digestSchema": "sha256-file-or-tree-v1",
        "driver": {
            "durationMs": 12000,
            "exitCode": 0,
            "parserAccepted": True,
            "processExit": 0,
            "records": 80,
            "result": "pass",
            "sourceGenerations": 1,
            "staleEvents": 0,
        },
        "fixtureService": "progressive-http-loopback-v1",
        "inputSha256": {name: "a" * 64 for name in result_contract.STRESS_INPUT_KEYS},
        "instrumentation": "audio-monitor",
        "mediaKind": "av",
        "oracleOutcome": "accepted",
        "oracleFailureCodes": [],
        "schema": 1,
        "status": "passed",
        "suite": "rtsp-media-lab",
        "transport": {
            "bodyResponseCount": 4,
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "errorResponseCount": 0,
            "firstPlaybackObservationLagMs": 2,
            "getRequestCount": 4,
            "initialResponseBytesExpected": 100000,
            "initialResponseBytesSent": 50000,
            "initialResponseCompleted": False,
            "initialRequestRange": "bytes=0-99999",
            "initialRequestSequence": 1,
            "maxAllowedErrorResponses": 0,
            "maxRangeRepeatCount": 2,
            "maxWatcherObservationLagMs": 3,
            "minExpectedErrorResponses": 0,
            "minExpectedPostSeekRangeResponses": 1,
            "postSeekRangeResponseCount": 2,
            "progressiveScored": True,
            "rangeResponseCount": 3,
            "requestCount": 4,
            "role": "streaming-first-qualification",
            "schema": 1,
            "startupAfterRequestMs": 900,
            "status": "passed",
            "transferTailAfterPlaybackMs": 11000,
        },
        "videoSetup": "xvfb-llvmpipe-headless-no-frame-oracle",
    }


class ResultContractTests(unittest.TestCase):
    def test_accepts_all_four_exact_contracts(self) -> None:
        result_contract.validate_smoke(smoke_result())
        result_contract.validate_runtime(runtime_result())
        result_contract.validate_fault(fault_result())
        result_contract.validate_stress(stress_result())

    def test_rejects_extra_smoke_field(self) -> None:
        value = smoke_result()
        value["rawLog"] = "must-not-be-exported"
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_smoke(value)

    def test_rejects_boolean_process_exit(self) -> None:
        value = runtime_result()
        value["processExit"] = False
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_runtime(value)

    def test_rejects_boolean_schema(self) -> None:
        value = smoke_result()
        value["schema"] = True
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_smoke(value)

    def test_rejects_fault_that_also_delays_control(self) -> None:
        value = copy.deepcopy(fault_result())
        value["measurements"]["delayControlSeconds"] = 0.27
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_fault(value)

    def test_rejects_fault_without_target_slowdown(self) -> None:
        value = copy.deepcopy(fault_result())
        value["measurements"]["cleanTargetSeconds"] = 0.20
        value["measurements"]["delayTargetSeconds"] = 0.28
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_fault(value)

    def test_rejects_nonfinite_measurement(self) -> None:
        value = copy.deepcopy(fault_result())
        value["measurements"]["delayTargetSeconds"] = float("nan")
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_fault(value)

    def test_canonical_json_is_stable_and_compact(self) -> None:
        first = result_contract.canonical_bytes(fault_result())
        shuffled = json.loads(first)
        second = result_contract.canonical_bytes(shuffled)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertNotIn(b": ", first)

    def test_stress_rejects_missing_identity_and_url_field(self) -> None:
        missing = stress_result()
        del missing["inputSha256"]["parser"]
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(missing)
        extra = stress_result()
        extra["url"] = "https://must-not-be-exported.example/"
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(extra)

    def test_stress_rejects_identity_label_and_exit_disagreement(self) -> None:
        label = stress_result()
        label["inputSha256"]["driverExe"] = "caller-supplied-label"
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(label)
        disagreement = stress_result()
        disagreement["driver"]["processExit"] = 1
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(disagreement)

    def test_stress_rejects_invalid_initial_transport_identity(self) -> None:
        value = stress_result()
        value["transport"]["initialRequestSequence"] = 5
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(value)
        value = stress_result()
        value["transport"]["initialRequestRange"] = "bytes=secret"
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(value)

    def test_stress_diagnostic_transport_cannot_claim_progressive_timing(self) -> None:
        value = stress_result()
        transport = value["transport"]
        transport.update({
            "initialResponseBytesExpected": None,
            "initialResponseBytesSent": None,
            "initialResponseCompleted": None,
            "initialRequestRange": None,
            "initialRequestSequence": None,
            "progressiveScored": False,
            "role": "media-engine-diagnostic",
            "startupAfterRequestMs": None,
            "transferTailAfterPlaybackMs": None,
        })
        result_contract.validate_stress(value)
        transport["startupAfterRequestMs"] = 10
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(value)

    def test_stress_diagnostic_transport_allows_no_post_seek_range(self) -> None:
        value = stress_result()
        value["transport"].update({
            "role": "media-engine-diagnostic",
            "progressiveScored": False,
            "postSeekRangeResponseCount": 0,
            "minExpectedPostSeekRangeResponses": 0,
            "startupAfterRequestMs": None,
            "transferTailAfterPlaybackMs": None,
            "initialResponseBytesExpected": None,
            "initialResponseBytesSent": None,
            "initialResponseCompleted": None,
            "initialRequestRange": None,
            "initialRequestSequence": None,
        })
        result_contract.validate_stress(value)

    def test_stress_accepts_explicit_bounded_terminal_http_error(self) -> None:
        value = stress_result()
        value["caseRole"] = "expected-pass"
        value["transport"].update({
            "errorResponseCount": 1,
            "minExpectedErrorResponses": 1,
            "maxAllowedErrorResponses": 1,
            "postSeekRangeResponseCount": 0,
            "minExpectedPostSeekRangeResponses": 0,
        })
        result_contract.validate_stress(value)
        value["transport"]["errorResponseCount"] = 0
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(value)

        mislabeled = stress_result()
        mislabeled["transport"].update({
            "errorResponseCount": 1,
            "minExpectedErrorResponses": 1,
            "maxAllowedErrorResponses": 1,
            "postSeekRangeResponseCount": 0,
            "minExpectedPostSeekRangeResponses": 0,
        })
        with self.assertRaisesRegex(result_contract.ContractError, "cannot be labeled"):
            result_contract.validate_stress(mislabeled)

    def test_streaming_qualification_can_be_explicitly_linear_no_range(self) -> None:
        value = stress_result()
        value["transport"].update({
            "rangeResponseCount": 0,
            "postSeekRangeResponseCount": 0,
            "minExpectedPostSeekRangeResponses": 0,
            "initialRequestRange": "bytes=0-99999",
        })
        result_contract.validate_stress(value)

    def test_stress_negative_control_must_fail_as_declared(self) -> None:
        value = stress_result()
        value["caseRole"] = "negative-control"
        with self.assertRaises(result_contract.ContractError):
            result_contract.validate_stress(value)
        value["driver"]["parserAccepted"] = False
        value["oracleOutcome"] = "rejected-as-expected"
        value["oracleFailureCodes"] = ["checkpoint-audio-nonzero-stale"]
        result_contract.validate_stress(value)

        control = stress_result()
        control["caseRole"] = "negative-control"
        control["instrumentation"] = "control"
        result_contract.validate_stress(control)

    def test_stress_oracle_pair_requires_real_instrumented_audio_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "control.json"
            instrumented = Path(directory) / "instrumented.json"
            identity = {
                "expected_driver_sha256": "a" * 64,
                "expected_scenario_sha256": "b" * 64,
            }
            control.write_text(json.dumps(identity))
            monitored = {
                **identity,
                "audio_check_labels": ["steady"],
                "required_audio_payload_formats": ["pcm16"],
                "min_audio_samples_generation": 10,
                "min_audio_bytes_generation": 4096,
                "min_audio_nonzero_units_generation": 20,
                "max_audio_silence_ms": 750,
                "max_audio_nonzero_silence_ms": 750,
            }
            instrumented.write_text(json.dumps(monitored))
            arguments = Namespace(
                control=str(control),
                instrumented=str(instrumented),
                driver_sha256="a" * 64,
                scenario_sha256="b" * 64,
                media_kind="av",
            )
            result_contract.command_check_stress_oracles(arguments)
            monitored.pop("min_audio_nonzero_units_generation")
            instrumented.write_text(json.dumps(monitored))
            with self.assertRaises(result_contract.ContractError):
                result_contract.command_check_stress_oracles(arguments)

    def test_control_oracle_cannot_depend_on_audio_monitor_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "control.json"
            instrumented = Path(directory) / "instrumented.json"
            value = {
                "expected_driver_sha256": "a" * 64,
                "expected_scenario_sha256": "b" * 64,
                "min_audio_samples_generation": 1,
            }
            control.write_text(json.dumps(value))
            instrumented.write_text(json.dumps(value))
            with self.assertRaises(result_contract.ContractError):
                result_contract.command_check_stress_oracles(Namespace(
                    control=str(control),
                    instrumented=str(instrumented),
                    driver_sha256="a" * 64,
                    scenario_sha256="b" * 64,
                    media_kind="video-only",
                ))

    def test_parser_rejection_classifier_allows_only_audio_delivery_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parser.log"
            path.write_text(
                "FAIL: audio checkpoint 'steady': no nonzero audio unit for 1000 ms\n"
                "FAIL: timeline (1, 2): post-seek audio samples 0 below 10\n"
            )
            self.assertEqual(
                result_contract.classify_stress_parser_log(str(path), True),
                ["checkpoint-audio-nonzero-stale", "post-seek-audio-samples"],
            )
            path.write_text("FAIL: 1 media error events observed\n")
            with self.assertRaises(result_contract.ContractError):
                result_contract.classify_stress_parser_log(str(path), True)


if __name__ == "__main__":
    unittest.main()
