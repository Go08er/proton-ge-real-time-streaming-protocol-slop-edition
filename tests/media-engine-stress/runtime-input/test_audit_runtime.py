#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Pure tests for the immutable Steam Runtime audit boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import audit_runtime as subject


SOURCES = {
    "sniper": "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-SteamLinuxRuntime_sniper",
    "steamrt4": "/nix/store/9876543210abcdfghijklmnpqrsvwxyz-SteamLinuxRuntime_steamrt4",
}


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def make_runtime(root: Path, runtime_family: str = "sniper") -> None:
    write(root / "_v2-entry-point", b"#!/bin/sh\n", 0o755)
    write(root / "run", b"#!/bin/sh\n", 0o755)
    write(root / "pressure-vessel/bin/pressure-vessel-wrap", b"wrap\n", 0o755)
    write(root / "pressure-vessel/bin/pressure-vessel-unruntime", b"unruntime\n", 0o755)
    platform_prefix = subject.RUNTIME_FAMILIES[runtime_family]
    write(root / f"{platform_prefix}3.0.test/files/ab/payload.bin", b"payload\n")
    write(root / "README.md", b"runtime\n")
    os.symlink("run", root / "run-link")


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "runtime"
        self.output.mkdir()
        make_runtime(self.output)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_runtime_and_writes_recomputable_provenance(self) -> None:
        for runtime_family in sorted(subject.RUNTIME_FAMILIES):
            with self.subTest(runtime_family=runtime_family), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "runtime"
                output.mkdir()
                make_runtime(output, runtime_family)
                source = SOURCES[runtime_family]
                provenance = subject.audit_runtime(output, source, runtime_family)
                recorded = json.loads(
                    (output / ".runtime-input/provenance.json").read_text()
                )
                self.assertEqual(recorded, provenance)
                self.assertEqual(
                    recorded["runtime_source_name"],
                    f"SteamLinuxRuntime_{runtime_family}",
                )
                self.assertEqual(
                    recorded["runtime_source_path_sha256"],
                    hashlib.sha256(source.encode()).hexdigest(),
                )
                self.assertEqual(recorded["runtime_family"], runtime_family)
                platform_prefix = subject.RUNTIME_FAMILIES[runtime_family]
                self.assertEqual(recorded["platform_prefix"], platform_prefix)
                self.assertEqual(recorded["provenance_version"], 2)
                self.assertEqual(recorded["excluded_top_level"], ["var"])
                self.assertEqual(
                    recorded["nonempty_platform_payloads"],
                    [f"{platform_prefix}3.0.test"],
                )
                recomputed, counts, regular_bytes = subject.payload_tree_evidence(
                    output, exclude_generated_metadata=True
                )
                self.assertEqual(recomputed, recorded["payload_tree_sha256"])
                self.assertEqual(counts, recorded["tree_counts"])
                self.assertEqual(regular_bytes, recorded["tree_regular_bytes"])
                self.assertNotIn(temporary, json.dumps(recorded))
                self.assertNotIn(
                    Path(source).name.split("-", 1)[0], json.dumps(recorded)
                )

    def test_digest_survives_nix_read_only_mode_normalization(self) -> None:
        before, _, _ = subject.payload_tree_evidence(self.output)
        for path in self.output.rglob("*"):
            if path.is_symlink():
                continue
            current = stat.S_IMODE(path.stat().st_mode)
            os.chmod(path, 0o555 if path.is_dir() or current & 0o111 else 0o444)
        after, _, _ = subject.payload_tree_evidence(self.output)
        self.assertEqual(before, after)
        os.chmod(self.output / "run", 0o444)
        changed, _, _ = subject.payload_tree_evidence(self.output)
        self.assertNotEqual(before, changed)

    def test_rejects_top_level_var(self) -> None:
        (self.output / "var").mkdir()
        with self.assertRaisesRegex(subject.RuntimeRejected, "excluded top-level var"):
            subject.audit_runtime(self.output, SOURCES["sniper"], "sniper")

    def test_rejects_missing_empty_or_nonexecutable_required_files(self) -> None:
        cases = ("missing", "empty", "nonexec")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "runtime"
                output.mkdir()
                make_runtime(output)
                target = output / "run"
                if case == "missing":
                    target.unlink()
                elif case == "empty":
                    target.write_bytes(b"")
                else:
                    target.chmod(0o644)
                with self.assertRaisesRegex(subject.RuntimeRejected, "required runtime executable"):
                    subject.audit_runtime(output, SOURCES["sniper"], "sniper")

    def test_rejects_missing_pressure_vessel_directory(self) -> None:
        for child in (self.output / "pressure-vessel/bin").iterdir():
            child.unlink()
        (self.output / "pressure-vessel/bin").rmdir()
        (self.output / "pressure-vessel").rmdir()
        with self.assertRaisesRegex(subject.RuntimeRejected, "required runtime executable"):
            subject.audit_runtime(self.output, SOURCES["sniper"], "sniper")

    def test_rejects_empty_platform_payload(self) -> None:
        (self.output / "sniper_platform_3.0.test/files/ab/payload.bin").write_bytes(b"")
        with self.assertRaisesRegex(subject.RuntimeRejected, "no nonempty sniper_platform"):
            subject.audit_runtime(self.output, SOURCES["sniper"], "sniper")

    def test_rejects_declared_family_without_matching_payload(self) -> None:
        with self.assertRaisesRegex(
            subject.RuntimeRejected, r"no nonempty steamrt4_platform_\* files payload"
        ):
            subject.audit_runtime(self.output, SOURCES["sniper"], "steamrt4")

    def test_family_selection_uses_a_closed_allowlist(self) -> None:
        for runtime_family in ("", "STEAMRT4", "steamrt4_platform_", "../sniper", None):
            with self.subTest(runtime_family=runtime_family):
                with self.assertRaisesRegex(subject.RuntimeRejected, "unsupported runtime family"):
                    subject.validate_runtime_family(runtime_family)  # type: ignore[arg-type]

    def test_only_declared_family_platforms_are_recorded(self) -> None:
        write(
            self.output / "steamrt4_platform_4.0.test/files/ab/other.bin",
            b"other\n",
        )
        provenance = subject.audit_runtime(
            self.output, SOURCES["sniper"], "sniper"
        )
        self.assertEqual(
            provenance["nonempty_platform_payloads"],
            ["sniper_platform_3.0.test"],
        )
        self.assertEqual(provenance["platform_prefix"], "sniper_platform_")

    def test_rejects_noncanonical_store_source(self) -> None:
        for path in (self.root, Path("/nix/store/../outside")):
            with self.subTest(path=path):
                with self.assertRaises(subject.RuntimeRejected):
                    subject.validate_store_source(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
