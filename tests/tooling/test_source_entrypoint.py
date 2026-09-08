#!/usr/bin/env python3
"""The public build entrypoint must be inspectable without any build/fetch."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-se1.sh"


class EntrypointTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *map(str, args)],
                              text=True, capture_output=True, timeout=10)

    def test_help(self):
        self.assertEqual(self.run_script("--help").returncode, 0)

    def test_dry_run_has_no_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="se1-entrypoint-") as tmp:
            target = Path(tmp) / "fresh"
            result = self.run_script("--work-dir", target, "--jobs", "4", "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("network=none", result.stdout)
            self.assertIn("Jobs: 4", result.stdout)
            self.assertFalse(target.exists())

    def test_existing_directory_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="se1-entrypoint-") as tmp:
            marker = Path(tmp) / "keep"
            marker.write_text("unchanged")
            self.assertNotEqual(self.run_script("--work-dir", tmp, "--dry-run").returncode, 0)
            self.assertEqual(marker.read_text(), "unchanged")

    def test_recursive_copy_is_refused(self):
        target = ROOT / "never-create-recursive-test"
        result = self.run_script("--work-dir", target, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the source kit", result.stderr)
        self.assertFalse(target.exists())

    def test_job_limit(self):
        with tempfile.TemporaryDirectory(prefix="se1-entrypoint-") as tmp:
            result = self.run_script("--work-dir", Path(tmp) / "fresh", "--jobs", "17", "--dry-run")
            self.assertNotEqual(result.returncode, 0)

    def test_unknown_option(self):
        self.assertNotEqual(self.run_script("--unknown").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
