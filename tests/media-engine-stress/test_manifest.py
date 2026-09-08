#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Pure tests for the VRChat MediaEngine stress-suite manifest."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from validate_manifest import ManifestError, load_and_validate, validate_manifest


MANIFEST = Path(__file__).with_name("manifest.json")


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load_and_validate(MANIFEST)

    def test_manifest_is_canonical_json(self) -> None:
        text = MANIFEST.read_text(encoding="utf-8")
        expected = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
        self.assertEqual(expected, text)

    def test_base_has_product_and_standalone_coverage(self) -> None:
        base = [item for item in self.data["scenarios"] if "base" in item["profiles"]]
        layers = {item["layer"] for item in base}
        self.assertIn("proton-probe", layers)
        self.assertIn("vrchat-single", layers)
        self.assertIn("vrchat-multiplayer", layers)

    def test_required_stress_families_exist(self) -> None:
        scenario_ids = {item["id"] for item in self.data["scenarios"]}
        required = {
            "aggressive-seek-latest-wins",
            "hls-live-held-window-oracle-negative",
            "hls-live-muxed-healthy-ab",
            "hls-live-separate-audio-404-oracle-negative",
            "hls-live-separate-audio-503-oracle-negative",
            "hls-live-separate-audio-delay-recovery",
            "hls-live-separate-healthy-ab",
            "join-at-offset",
            "live-seek-rejected",
            "multiplayer-late-join",
            "pause-play-race",
            "repeated-open-close",
            "rtsp-server-loss",
            "running-http-replacement",
            "source-replacement-generation",
        }
        self.assertTrue(required <= scenario_ids)

    def test_existing_proton_diagnostics_do_not_claim_full_implementation(self) -> None:
        scenarios = {item["id"]: item for item in self.data["scenarios"]}
        for scenario_id in (
                "aggressive-seek-latest-wins", "running-http-replacement"):
            with self.subTest(scenario_id=scenario_id):
                scenario = scenarios[scenario_id]
                self.assertEqual("compose", scenario["status"])
                self.assertEqual("proton-probe", scenario["layer"])
                self.assertEqual("vm-only", scenario["isolation"])

    def test_vm_cases_cannot_claim_host_execution(self) -> None:
        vm_cases = [item for item in self.data["scenarios"] if item["isolation"] == "vm-only"]
        self.assertTrue(vm_cases)
        self.assertTrue(all(item["layer"] in {"proton-probe", "vm-proton"}
                            for item in vm_cases))

    def test_proton_probes_are_vm_only(self) -> None:
        probes = [item for item in self.data["scenarios"]
                  if item["layer"] == "proton-probe"]
        self.assertTrue(probes)
        self.assertTrue(all(item["isolation"] == "vm-only" for item in probes))

    def test_proton_probe_host_execution_is_rejected(self) -> None:
        broken = copy.deepcopy(self.data)
        probe = next(item for item in broken["scenarios"]
                     if item["layer"] == "proton-probe")
        probe["isolation"] = "host-loopback"
        with self.assertRaisesRegex(ManifestError, "proton-probe requires vm-only"):
            validate_manifest(broken)

    def test_no_media_scenario_claims_full_implementation(self) -> None:
        implemented = {item["id"] for item in self.data["scenarios"]
                       if item["status"] == "implemented"}
        self.assertEqual(set(), implemented)

    def test_fixture_availability_is_explicit(self) -> None:
        availability = {item["id"]: item["availability"] for item in self.data["fixtures"]}
        self.assertEqual("planned", availability["topology-gap-audio"])
        self.assertEqual("planned", availability["topology-gap-video"])
        self.assertEqual("implemented", availability["hls-live-set"])
        self.assertEqual("implemented", availability["vod-primary"])

    def test_missing_fixture_availability_is_rejected(self) -> None:
        broken = copy.deepcopy(self.data)
        del broken["fixtures"][0]["availability"]
        with self.assertRaisesRegex(ManifestError, "invalid availability"):
            validate_manifest(broken)

    def test_unknown_fixture_is_rejected(self) -> None:
        broken = copy.deepcopy(self.data)
        broken["scenarios"][0]["fixtures"] = ["does-not-exist"]
        with self.assertRaisesRegex(ManifestError, "unknown fixtures"):
            validate_manifest(broken)

    def test_duplicate_scenario_is_rejected(self) -> None:
        broken = copy.deepcopy(self.data)
        broken["scenarios"].append(copy.deepcopy(broken["scenarios"][0]))
        with self.assertRaisesRegex(ManifestError, "duplicate scenarios id"):
            validate_manifest(broken)


if __name__ == "__main__":
    unittest.main()
