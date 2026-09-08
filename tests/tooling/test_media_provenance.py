#!/usr/bin/env python3
"""Exercise selected-series provenance with a tiny offline prepared fixture."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(path, values, repeated=()):
    path.write_text("".join(f"{key}\t{value}\n" for key, value in values.items())
                    + "".join("\t".join(row) + "\n" for row in repeated))


class SelectedSeriesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rtsp-ge-media-provenance.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.wine = self.source / "wine"
        self.state = self.root / "state"
        for path in (self.wine, self.state, self.root / "config", self.root / "patches",
                     self.source / "patches/ge-video-rework"):
            path.mkdir(parents=True, exist_ok=True)
        self.active = self.root / "patches/series"
        self.archived = self.root / "patches/archived-series"
        self.active.write_text("current.patch\n")
        self.archived.write_text("historical.patch\n")
        self.upstream = self.source / "patches/ge-video-rework/cleanup.patch"
        self.upstream.write_text("fixture cleanup patch\n")
        (self.wine / "source.c").write_text("fixture tracked source\n")
        self.git("init", "-q")
        self.git("add", "source.c")
        self.manifest = self.root / "config/cleanup.tsv"
        self.commit = "1" * 40
        self.deletions = [(f"obsolete-{index}.c", "2" * 40, "3" * 64)
                          for index in range(3)]
        records(self.manifest, {
            "media_cleanup_version": "1", "source_commit": self.commit,
            "upstream_patch": "patches/ge-video-rework/cleanup.patch",
            "upstream_patch_sha256": sha(self.upstream),
        }, (("delete", *row) for row in self.deletions))
        cleanup = self.state / "ge-media-cleanup-audit.tsv"
        records(cleanup, {
            "media_cleanup_audit_version": "1", "source_commit": self.commit,
            "normalization_digest": sha(self.manifest), "manifest_sha256": sha(self.manifest),
            "upstream_patch": "patches/ge-video-rework/cleanup.patch",
            "upstream_patch_sha256": sha(self.upstream),
            "external_reference_check": "passed",
            "generated_configure_reference_check": "deferred-until-autoreconf",
            "remaining_non_evidence_winegstreamer_files": "0", "media_cleanup": "passed",
        }, (("deleted", *row) for row in self.deletions))
        baseline = self.state / "ge-pre-rtsp-diff-check.txt"
        baseline.write_text("")
        (self.state / "ge-pre-rtsp-diff-check.status").write_text("0\n")
        self.audit_values = {
            "patch_audit_version": "1", "series_sha256": sha(self.archived),
            "ge_baseline_diff_check_status": "0", "ge_baseline_diff_check_sha256": sha(baseline),
            "ge_media_cleanup_normalization_digest": sha(self.manifest),
            "touched_path_diff_check": "passed", "complete_diff_check_baseline_match": "passed",
        }
        self.state_values = {
            "ge_media_cleanup_normalization_digest": sha(self.manifest),
            "ge_media_cleanup_audit_sha256": sha(cleanup),
            "final_winegstreamer_external_references": "0", "final_diff_check_baseline_match": "passed",
            "ge_pre_rtsp_diff_check_status": "0", "ge_pre_rtsp_diff_check_sha256": sha(baseline),
        }
        self.write_evidence()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.wine), *args], check=True,
                              capture_output=True, text=True,
                              env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})

    def write_evidence(self):
        audit = self.state / "rtsp-patch-audit.tsv"
        records(audit, self.audit_values, [("touched_path", "source.c")])
        self.state_values["patch_audit_sha256"] = sha(audit)
        records(self.state / "prepared-source.tsv", self.state_values)

    def check(self, series=None):
        command = (
            'source "$1"; PINNED_GE_ROOT="$2"; '
            'PIN_SOURCE_COMMIT="$3"; PIN_GE_MEDIA_CLEANUP_MANIFEST=config/cleanup.tsv; '
            'PIN_GE_MEDIA_CLEANUP_MANIFEST_SHA256="$4"; '
            'pinned_ge_require_prepared_media_provenance "$5" "$6"'
        )
        args = ["bash", "-c", command + (' "$7"' if series is not None else ""),
                "fixture", str(ROOT / "scripts/pinned-ge-common.sh"), str(self.root),
                self.commit, sha(self.manifest), str(self.source), str(self.state)]
        if series is not None:
            args.append(str(series))
        return subprocess.run(args, capture_output=True, text=True,
                              env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})

    def test_explicit_archive_passes_when_active_series_is_different(self):
        result = self.check(self.archived)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_explicit_series_is_rejected(self):
        result = self.check(self.active)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RTSP patch audit identity differs", result.stderr)

    def test_omitted_series_keeps_the_existing_current_series_default(self):
        self.audit_values["series_sha256"] = sha(self.active)
        self.write_evidence()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_is_not_inferred_when_argument_is_omitted(self):
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RTSP patch audit identity differs", result.stderr)

    def test_missing_or_symlink_series_is_rejected(self):
        missing = self.root / "patches/missing"
        linked = self.root / "patches/linked"
        linked.symlink_to(self.archived)
        for path in (missing, linked):
            with self.subTest(path=path.name):
                result = self.check(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("selected Wine patch series is absent or unsafe", result.stderr)

    def test_other_provenance_checks_remain_active(self):
        (self.wine / "source.c").write_text("fixture with trailing space \n")
        result = self.check(self.archived)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("an RTSP-touched Wine path fails diff --check", result.stderr)

    def test_build_and_artifact_verifier_forward_selected_series(self):
        call = 'pinned_ge_require_prepared_media_provenance "$SOURCE_DIR" "$STATE_DIR" "$PATCH_SERIES"'
        for script in ("build-pinned-ge.sh", "verify-pinned-ge-artifact.sh"):
            with self.subTest(script=script):
                self.assertIn(call, (ROOT / "scripts" / script).read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
