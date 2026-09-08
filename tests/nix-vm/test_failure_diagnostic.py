# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import failure_diagnostic


class FailureDiagnosticTests(unittest.TestCase):
    def classify_texts(
        self,
        *,
        console_text: str = "",
        runtime_text: str = "",
        proton_text: str | None = None,
        process_exit: int | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            console.write_text(console_text)
            runtime = root / "runtime"
            runtime.mkdir()
            if runtime_text:
                (runtime / "slr.log").write_text(runtime_text)
            proton = None
            if proton_text is not None:
                proton = root / "steam-test.log"
                proton.write_text(proton_text)
            return failure_diagnostic.classify(
                console,
                runtime,
                proton_log=proton,
                process_exit=process_exit,
            )

    def test_classifies_without_copying_sensitive_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            runtime = root / "runtime"
            runtime.mkdir()
            console.write_text(
                "Traceback (most recent call last):\n"
                "FileNotFoundError: /srv/private/token at https://secret.invalid/media\n"
            )
            (runtime / "runtime.log").write_text(
                "pressure-vessel-wrap: E: bwrap: Creating new namespace failed: Operation not permitted\n"
            )
            result = failure_diagnostic.classify(console, runtime)
            encoded = json.dumps(result, sort_keys=True)
            self.assertEqual(result["schema"], 1)
            self.assertIn("python-launcher-traceback", result["codes"])
            self.assertIn("sandbox-namespace-denied", result["codes"])
            self.assertIn("pressure-vessel-launch-failed", result["codes"])
            self.assertNotIn("private", encoded)
            self.assertNotIn("secret.invalid", encoded)
            self.assertNotIn("token", encoded)

    def test_unknown_text_returns_only_fixed_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            runtime = root / "runtime"
            runtime.mkdir()
            console.write_text("opaque private diagnostic\n")
            result = failure_diagnostic.classify(console, runtime)
            self.assertEqual(result["codes"], ["unclassified-nonzero-exit"])
            self.assertNotIn("opaque", json.dumps(result))

    def test_optional_runtime_probes_are_not_launcher_failures(self) -> None:
        result = self.classify_texts(runtime_text=(
            "12:00:00: pressure-vessel-wrap[42]: D: An error occurred trying to resolve "
            "a graphics provider: No such file or directory\n"
            "12:00:00: pressure-vessel-wrap[42]: D: Failed to open an optional EGL directory: "
            "No such file or directory\n"
            "12:00:00: pressure-vessel-wrap[42]: D: Cannot resolve an optional library "
            "in any graphics provider\n"
            "12:00:00: pressure-vessel-wrap[42]: D: bwrap namespace probe failed: "
            "Operation not permitted\n"
            "12:00:00: pressure-vessel-wrap[42]: W: Unable to determine architecture of "
            "provider ldconfig: Error reading ELF header\n"
            "12:00:00: pressure-vessel-wrap[42]: I: Pulseaudio user configuration file: "
            "Error opening file: No such file or directory\n"
            "12:00:00: pv-adverb[43]: D: -> exit status 72\n"
            "12:00:00: pv-adverb[43]: W: Container startup will be faster if missing "
            "locales are created at OS level\n"
        ))
        self.assertEqual(result["codes"], ["unclassified-nonzero-exit"])
        self.assertEqual(result["signals"], [])

    def test_pressure_vessel_error_is_structural_and_does_not_match_path(self) -> None:
        result = self.classify_texts(runtime_text=(
            "12:00:00: pressure-vessel-wrap[42]: E: "
            "openat(/nix/store/immutable-steam-runtime-sniper/files/.ref): Permission denied\n"
        ))
        self.assertIn("pressure-vessel-launch-failed", result["codes"])
        self.assertIn("permission-denied", result["codes"])
        self.assertNotIn("steam-runtime-launch-failed", result["codes"])
        self.assertNotIn("missing-executable-or-library", result["codes"])

    def test_steam_runtime_errors_require_launcher_record(self) -> None:
        result = self.classify_texts(runtime_text=(
            "12:00:00: _v2-entry-point[42]: Error: A command to run is required\n"
            "12:00:01: steam-runtime-launch-client[43]: E: launcher service failed\n"
        ))
        self.assertIn("steam-runtime-launch-failed", result["codes"])
        self.assertNotIn("pressure-vessel-launch-failed", result["codes"])

    def test_component_name_in_debug_command_is_not_a_record(self) -> None:
        result = self.classify_texts(runtime_text=(
            "12:00:00: pressure-vessel-wrap[42]: D: run: "
            "'/tmp/steam-runtime-launch-client[43]: E: private failure'\n"
        ))
        self.assertEqual(result["codes"], ["unclassified-nonzero-exit"])

    def test_missing_file_requires_execution_or_loader_context(self) -> None:
        benign, _signals = failure_diagnostic.classify_lines(
            b"pressure-vessel-wrap[42]: D: execvp(optional-probe): No such file or directory\n"
        )
        executable, _signals = failure_diagnostic.classify_lines(
            b"execvp(required-command): No such file or directory\n"
        )
        library, _signals = failure_diagnostic.classify_lines(
            b"required: error while loading shared libraries: libprivate.so: "
            b"cannot open shared object file: No such file or directory\n"
        )
        self.assertEqual(benign, ["unclassified-nonzero-exit"])
        self.assertEqual(executable, ["missing-executable-or-library"])
        self.assertEqual(library, ["missing-executable-or-library"])

    def test_wrapped_command_nonzero_is_not_runtime_launch_failure(self) -> None:
        result = self.classify_texts(runtime_text=(
            "12:00:00: pv-adverb[42]: I: Command exited with status 3\n"
        ))
        self.assertEqual(result["codes"], ["wrapped-command-nonzero"])

    def test_process_timeout_is_reported_without_fabricated_runtime_failure(self) -> None:
        result = self.classify_texts(
            runtime_text=(
                "12:00:00: pressure-vessel-wrap[42]: D: Failed to open optional provider: "
                "No such file or directory\n"
            ),
            process_exit=124,
        )
        self.assertEqual(result["codes"], ["process-timeout"])

    def test_exact_optional_proton_log_is_sampled_privately(self) -> None:
        result = self.classify_texts(
            console_text="opaque console\n",
            runtime_text="pressure-vessel-wrap[42]: D: Cannot resolve optional provider\n",
            proton_text=(
                "ImportError: cannot import name 'PrivateToken' from 'typing' "
                "(/srv/private/secret.py) https://secret.invalid/media\n"
                "wine: could not load kernel32.dll, status c0000135\n"
            ),
            process_exit=1,
        )
        encoded = json.dumps(result, sort_keys=True)
        self.assertIn("python-module-missing", result["codes"])
        self.assertIn("wine-load-failed", result["codes"])
        self.assertEqual(result["sourceFileCount"], 3)
        self.assertNotIn("PrivateToken", encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret.invalid", encoded)

    def test_optional_proton_log_may_be_absent_or_symlinked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            console.write_text("")
            runtime = root / "runtime"
            runtime.mkdir()
            missing = root / "missing.log"
            absent = failure_diagnostic.classify(console, runtime, proton_log=missing)
            secret = root / "secret.log"
            secret.write_text("ImportError: secret\n")
            linked = root / "steam.log"
            linked.symlink_to(secret)
            symlinked = failure_diagnostic.classify(console, runtime, proton_log=linked)
            self.assertEqual(absent["sourceFileCount"], 1)
            self.assertEqual(symlinked["sourceFileCount"], 1)
            self.assertEqual(absent["codes"], ["unclassified-nonzero-exit"])
            self.assertEqual(symlinked["codes"], ["unclassified-nonzero-exit"])

    def test_head_and_tail_sampling_preserves_launcher_and_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            console.write_text("")
            runtime = root / "runtime"
            runtime.mkdir()
            runtime_log = runtime / "slr.log"
            runtime_log.write_bytes(
                b"pressure-vessel-wrap[42]: E: runtime startup failed\n"
                + (b"x" * (failure_diagnostic.MAX_FILE_SAMPLE_BYTES + 4096))
                + b"\nImportError: private terminal failure\n"
            )
            result = failure_diagnostic.classify(console, runtime)
            self.assertTrue(result["sampleTruncated"])
            self.assertIn("pressure-vessel-launch-failed", result["codes"])
            self.assertIn("python-module-missing", result["codes"])
            self.assertNotIn("private", json.dumps(result))

    def test_process_exit_range_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            console.write_text("")
            runtime = root / "runtime"
            runtime.mkdir()
            with self.assertRaisesRegex(failure_diagnostic.DiagnosticError, "out of range"):
                failure_diagnostic.classify(console, runtime, process_exit=256)

    def test_cli_never_echoes_oserror_text(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            failure_diagnostic,
            "classify",
            side_effect=OSError("secret-token\nINJECTED"),
        ), redirect_stderr(stderr):
            status = failure_diagnostic.main([
                "--console", "/does/not/matter",
                "--runtime-log-dir", "/does/not/matter",
            ])
        self.assertEqual(status, 2)
        self.assertEqual(stderr.getvalue(), "failure diagnostic unavailable\n")

    def test_cli_forwards_exact_proton_log_and_process_exit(self) -> None:
        expected = {
            "codes": ["process-timeout"],
            "sampleTruncated": False,
            "schema": 1,
            "signals": [],
            "sourceFileCount": 0,
        }
        stdout = io.StringIO()
        with mock.patch.object(
            failure_diagnostic,
            "classify",
            return_value=expected,
        ) as classify, mock.patch("sys.stdout", stdout):
            status = failure_diagnostic.main([
                "--console", "/tmp/console.log",
                "--runtime-log-dir", "/tmp/runtime",
                "--proton-log", "/tmp/steam-999.log",
                "--process-exit", "124",
            ])
        self.assertEqual(status, 0)
        classify.assert_called_once_with(
            Path("/tmp/console.log"),
            Path("/tmp/runtime"),
            proton_log=Path("/tmp/steam-999.log"),
            process_exit=124,
        )
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_runtime_traversal_stops_at_entry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            console.write_text("")
            runtime = root / "runtime"
            runtime.mkdir()
            target = root / "target"
            target.write_text("")
            for index in range(failure_diagnostic.MAX_ENTRIES + 1):
                (runtime / f"entry-{index}").symlink_to(target)
            with self.assertRaisesRegex(failure_diagnostic.DiagnosticError, "entry limit"):
                failure_diagnostic.classify(console, runtime)

    def test_large_repeated_prefix_is_scanned_linearly(self) -> None:
        payload = (b"pressure-vessel " * 60_000) + b"\n"
        started = time.monotonic()
        codes, _signals = failure_diagnostic.classify_lines(payload)
        elapsed = time.monotonic() - started
        self.assertEqual(codes, ["unclassified-nonzero-exit"])
        self.assertLess(elapsed, 1.0)

    def test_malformed_component_record_does_not_crash(self) -> None:
        codes, signals = failure_diagnostic.classify_lines(b"[42]: Error: opaque\n")
        self.assertEqual(codes, ["unclassified-nonzero-exit"])
        self.assertEqual(signals, [])

    def test_symlinks_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            console = root / "console.log"
            runtime = root / "runtime"
            runtime.mkdir()
            secret = root / "secret"
            secret.write_text("Traceback (most recent call last):\n")
            console.symlink_to(secret)
            (runtime / "linked").symlink_to(secret)
            result = failure_diagnostic.classify(console, runtime)
            self.assertEqual(result["sourceFileCount"], 0)
            self.assertEqual(result["codes"], ["unclassified-nonzero-exit"])


if __name__ == "__main__":
    unittest.main()
