#!/usr/bin/env python3
"""The public build entrypoint must be inspectable without any build/fetch."""
from pathlib import Path
import os
import shutil
import subprocess
import tarfile
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

    def test_input_cache_modes_under_private_umask(self):
        # The manifest requires 0644. A private umask must not turn the
        # release cache into invalid 0600 inputs before prefetch can use it.
        with tempfile.TemporaryDirectory(prefix="se1-cache-mode-") as tmp:
            root = Path(tmp)
            sample = root / "input"
            sample.write_bytes(b"synthetic input")
            sample.chmod(0o644)
            archive = root / "fixture.tar"
            with tarfile.open(archive, "w") as output:
                output.add(sample, arcname="input")
            for label, flag, expected in (("prior", "", 0o600),
                                          ("fixed", "--same-permissions", 0o644)):
                dest = root / label
                dest.mkdir()
                result = subprocess.run(
                    ["bash", "-c", 'umask 077; tar $1 -xf "$2" -C "$3"',
                     "mode-test", flag, str(archive), str(dest)],
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((dest / "input").stat().st_mode & 0o777, expected)
            self.assertIn('tar --zstd --same-permissions -xf "$bundle"', SCRIPT.read_text())

    def fake_workflow(self, fail_phase=""):
        """Exercise orchestration only; all fetch/build commands are doubles."""
        with tempfile.TemporaryDirectory(prefix="se1-workflow-") as tmp:
            root = Path(tmp)
            kit = root / "kit"
            for path in (kit / "scripts", kit / "config", root / "bin", root / "seed"):
                path.mkdir(parents=True)
            shutil.copy2(SCRIPT, kit / "scripts/build-se1.sh")
            shutil.copy2(ROOT / "scripts/pinned-ge-common.sh", kit / "scripts")
            shutil.copy2(ROOT / "config/ge-proton11-6-se1.env", kit / "config")
            phases = ["bootstrap-pinned-ge.sh", "materialize-pinned-ge-submodules.sh",
                      "prepare-pinned-ge-source.sh", "prefetch-pinned-ge-contrib.sh",
                      "verify-preparation.sh", "build-pinned-ge.sh"]
            for phase in phases:
                script = kit / "scripts" / phase
                script.write_text('#!/usr/bin/env bash\n'
                                  'printf "%s\\n" "${0##*/}" >> "$SE1_TEST_CALLS"\n'
                                  '[[ "${0##*/}" != "$SE1_TEST_FAIL" ]]\n')
                script.chmod(0o755)
            podman = root / "bin/podman"
            podman.write_text('#!/usr/bin/env bash\n'
                              'printf "podman %s %s\\n" "$1" "${2:-}" >> "$SE1_TEST_CALLS"\n'
                              'case "$*" in\n'
                              '  "image exists "*) exit 0 ;;\n'
                              '  "image inspect "*) echo '
                              '01f74089b0ffb6fc127eadca189776ab56461515ba575ff1bfbd66a39d3f753b ;;\n'
                              '  *) exit 73 ;;\nesac\n')
            podman.chmod(0o755)
            calls = root / "calls"
            result = subprocess.run(
                ["bash", str(SCRIPT), "--work-dir", str(root / "work"),
                 "--seed", str(root / "seed"), "--contrib-cache", str(root / "cache")],
                env={**os.environ, "SE1_PROJECT_KIT": str(kit),
                     "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                     "SE1_TEST_CALLS": str(calls), "SE1_TEST_FAIL": fail_phase},
                text=True, capture_output=True, timeout=20)
            return result, calls.read_text().splitlines()

    def test_mocked_phase_order(self):
        result, calls = self.fake_workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            "bootstrap-pinned-ge.sh", "materialize-pinned-ge-submodules.sh",
            "prepare-pinned-ge-source.sh", "prefetch-pinned-ge-contrib.sh",
            "podman image exists", "podman image inspect",
            "verify-preparation.sh", "build-pinned-ge.sh"])

    def test_mocked_failed_gate_never_builds(self):
        result, calls = self.fake_workflow("verify-preparation.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], "verify-preparation.sh")
        self.assertNotIn("build-pinned-ge.sh", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
