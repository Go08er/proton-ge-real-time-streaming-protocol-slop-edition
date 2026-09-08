#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Validate the VRChat MediaEngine stress-suite manifest without side effects."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
PROFILE_IDS = {"base", "parity", "discovery"}
LAYERS = {
    "static",
    "native-loopback",
    "proton-probe",
    "vm-proton",
    "vrchat-single",
    "vrchat-multiplayer",
}
STATUSES = {"implemented", "compose", "planned", "manual-vrchat"}
ISOLATION = {"read-only", "host-loopback", "vm-only", "manual-product"}
FIXTURE_AVAILABILITY = {"implemented", "compose", "planned"}


class ManifestError(ValueError):
    """Raised when a manifest invariant is violated."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def require_id(value: Any, label: str) -> str:
    require(isinstance(value, str) and ID_RE.fullmatch(value) is not None,
            f"{label} must be a lowercase kebab-case identifier")
    return value


def require_string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    require(isinstance(value, list), f"{label} must be a list")
    require(not nonempty or bool(value), f"{label} must not be empty")
    require(all(isinstance(item, str) and item for item in value),
            f"{label} must contain only nonempty strings")
    return value


def unique_ids(items: list[dict[str, Any]], label: str) -> set[str]:
    seen: set[str] = set()
    for index, item in enumerate(items):
        require(isinstance(item, dict), f"{label}[{index}] must be an object")
        item_id = require_id(item.get("id"), f"{label}[{index}].id")
        require(item_id not in seen, f"duplicate {label} id: {item_id}")
        seen.add(item_id)
    return seen


def validate_manifest(data: Any) -> None:
    require(isinstance(data, dict), "manifest root must be an object")
    require(data.get("schema_version") == 1, "schema_version must be 1")
    require_id(data.get("suite_id"), "suite_id")

    profiles = data.get("profiles")
    require(isinstance(profiles, dict), "profiles must be an object")
    require(set(profiles) == PROFILE_IDS,
            f"profiles must be exactly {sorted(PROFILE_IDS)}")
    for profile_id, profile in profiles.items():
        require(isinstance(profile, dict), f"profile {profile_id} must be an object")
        require_string_list(profile.get("purpose"), f"profile {profile_id}.purpose")

    fixtures = data.get("fixtures")
    scenarios = data.get("scenarios")
    require(isinstance(fixtures, list) and fixtures, "fixtures must be a nonempty list")
    require(isinstance(scenarios, list) and scenarios, "scenarios must be a nonempty list")
    fixture_ids = unique_ids(fixtures, "fixtures")
    unique_ids(scenarios, "scenarios")

    for fixture in fixtures:
        fixture_id = fixture["id"]
        require(fixture.get("availability") in FIXTURE_AVAILABILITY,
                f"fixture {fixture_id} has invalid availability")
        require(fixture.get("kind") in {"generated-file", "derived-set", "service", "invalid"},
                f"fixture {fixture_id} has an invalid kind")
        require_string_list(fixture.get("topology"), f"fixture {fixture_id}.topology")
        require_string_list(fixture.get("identity"), f"fixture {fixture_id}.identity")
        require_string_list(fixture.get("oracles"), f"fixture {fixture_id}.oracles")

    covered_profiles: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario["id"]
        scenario_profiles = set(require_string_list(
            scenario.get("profiles"), f"scenario {scenario_id}.profiles"))
        require(scenario_profiles <= PROFILE_IDS,
                f"scenario {scenario_id} has an unknown profile")
        covered_profiles.update(scenario_profiles)
        require(scenario.get("layer") in LAYERS,
                f"scenario {scenario_id} has an invalid layer")
        require(scenario.get("status") in STATUSES,
                f"scenario {scenario_id} has an invalid status")
        isolation = scenario.get("isolation")
        require(isolation in ISOLATION,
                f"scenario {scenario_id} has an invalid isolation")
        if isolation == "vm-only":
            require(scenario.get("layer") in {"proton-probe", "vm-proton"},
                    f"scenario {scenario_id}: vm-only requires a Proton layer")
        if scenario.get("layer") == "proton-probe":
            require(isolation == "vm-only",
                    f"scenario {scenario_id}: proton-probe requires vm-only isolation")
        if scenario.get("status") == "manual-vrchat":
            require(scenario.get("layer") in {"vrchat-single", "vrchat-multiplayer"},
                    f"scenario {scenario_id}: manual-vrchat has the wrong layer")

        referenced = require_string_list(
            scenario.get("fixtures"), f"scenario {scenario_id}.fixtures", nonempty=False)
        unknown = set(referenced) - fixture_ids
        require(not unknown,
                f"scenario {scenario_id} references unknown fixtures: {sorted(unknown)}")
        require_string_list(scenario.get("actions"), f"scenario {scenario_id}.actions")
        require_string_list(scenario.get("oracles"), f"scenario {scenario_id}.oracles")
        require_string_list(scenario.get("negative_controls"),
                            f"scenario {scenario_id}.negative_controls")
        timeout = scenario.get("timeout_s")
        require(isinstance(timeout, int) and 1 <= timeout <= 3600,
                f"scenario {scenario_id}.timeout_s must be an integer from 1 through 3600")

    require(covered_profiles == PROFILE_IDS,
            f"every profile must have scenarios; covered {sorted(covered_profiles)}")


def load_and_validate(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    validate_manifest(data)
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?", type=Path,
                        default=Path(__file__).with_name("manifest.json"))
    args = parser.parse_args()
    data = load_and_validate(args.manifest)
    print(f"validated {len(data['fixtures'])} fixtures and {len(data['scenarios'])} scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
