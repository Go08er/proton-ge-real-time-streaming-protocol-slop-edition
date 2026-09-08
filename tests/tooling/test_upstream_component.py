#!/usr/bin/env python3
"""Exact GE component patch verification, without compiling or networking."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ComponentPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtsp-ge-component-test.")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "component"
        self.repo.mkdir()
        self.patches = self.base / "patches"
        self.patches.mkdir()
        self.git("init", "-q")
        (self.repo / "source.c").write_text("before\n")
        self.git("add", "source.c")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid",
                 "commit", "-qm", "baseline")
        (self.repo / "source.c").write_text("after\n")
        (self.patches / "0001.patch").write_text(self.git("diff"))
        self.original_index = (self.repo / ".git/index").read_bytes()

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})

    def check(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; '
             'pinned_ge_require_upstream_component_patch_state "$2" "$3"',
             "fixture", str(ROOT / "scripts/pinned-ge-common.sh"),
             str(self.repo), str(self.patches)], capture_output=True, text=True)
        self.assertEqual((self.repo / ".git/index").read_bytes(), self.original_index)
        return result

    def test_exact_upstream_result_passes_without_altering_source(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "source.c").read_text(), "after\n")

    def test_missing_patch_application_fails(self):
        (self.repo / "source.c").write_text("before\n")
        self.assertNotEqual(self.check().returncode, 0)

    def test_extra_tracked_change_fails(self):
        (self.repo / "source.c").write_text("after\nnot authorized\n")
        self.assertNotEqual(self.check().returncode, 0)

    def test_untracked_file_fails(self):
        (self.repo / "unexpected.c").write_text("extra source\n")
        self.assertNotEqual(self.check().returncode, 0)

    def test_empty_patch_directory_fails(self):
        (self.patches / "0001.patch").unlink()
        self.assertNotEqual(self.check().returncode, 0)

    def test_low_impact_reexec_preserves_selected_series(self):
        script = (ROOT / "scripts/build-pinned-ge.sh").read_text()
        arguments = script.split("REEXEC_ARGS=(", 1)[1].split("\n  )", 1)[0]
        self.assertIn('--series "$PATCH_SERIES"', arguments)


if __name__ == "__main__":
    unittest.main(verbosity=2)
