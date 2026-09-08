# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stderr
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import stress_fixture_service


def config() -> dict[str, object]:
    return {
        "bind": "127.0.0.1",
        "caseId": "vod-seek",
        "chunkBytes": 65536,
        "failCount": 1,
        "failStatus": 503,
        "fixtureRelativePath": "av/faststart.mp4",
        "headerDelaySeconds": 0.0,
        "maxConcurrent": 4,
        "maxErrorResponses": 0,
        "maxLogBytes": 32768,
        "maxObservedRequests": 32,
        "maxRangeRepeats": 8,
        "maxRequests": 64,
        "maxStartupMs": 5000,
        "minPostSeekRangeResponses": 1,
        "minTransferTailMs": 0,
        "mode": "range",
        "port": 18765,
        "rateKiB": 1024,
        "schema": 1,
        "service": "progressive-http-v1",
        "stallAfterBytes": 1024,
        "stallSeconds": 1.0,
        "truncateAfterBytes": 1024,
        "transportRole": "media-engine-diagnostic",
    }


class StressFixtureServiceTests(unittest.TestCase):
    def test_accepts_exact_config(self) -> None:
        self.assertEqual(stress_fixture_service.validate_config(config()), config())

    def test_rejects_extra_config_key_and_nonloopback_bind(self) -> None:
        extra = config()
        extra["url"] = "https://must-not-enter-contract.example/"
        with self.assertRaises(stress_fixture_service.FixtureError):
            stress_fixture_service.validate_config(extra)
        nonloopback = config()
        nonloopback["bind"] = "0.0.0.0"
        with self.assertRaises(stress_fixture_service.FixtureError):
            stress_fixture_service.validate_config(nonloopback)

    def test_rejects_path_traversal_and_unbounded_log_budget(self) -> None:
        traversal = config()
        traversal["fixtureRelativePath"] = "../secret"
        with self.assertRaises(stress_fixture_service.FixtureError):
            stress_fixture_service.validate_config(traversal)
        budget = config()
        budget["maxLogBytes"] = 1024
        with self.assertRaises(stress_fixture_service.FixtureError):
            stress_fixture_service.validate_config(budget)

    def test_scenario_requires_exact_declared_loopback_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "case.scenario"
            scenario.write_text('load "http://127.0.0.1:18765/media?case=seek"\nshutdown\n')
            stress_fixture_service.validate_scenario(scenario, 18765, 20.0)
            scenario.write_text('load "http://192.0.2.10:18765/media"\nshutdown\n')
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_scenario(scenario, 18765, 20.0)
            scenario.write_text(
                'load "http://127.0.0.1:18765/media"\n'
                'REPLACE "http://192.0.2.10:18765/media"\nshutdown\n'
            )
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_scenario(scenario, 18765, 20.0)

    def test_recovery_route_requires_ordered_recovery_mode_replacement(self) -> None:
        valid = (
            'load "http://127.0.0.1:18765/media?case=recovery"\n'
            'replace "http://127.0.0.1:18765/recovery?case=recovery"\n'
            "shutdown\n"
        )
        mutations = {
            "ordinary-mode": ("range", valid),
            "recovery-first": (
                "fail-post-open-range-recovery",
                'load "http://127.0.0.1:18765/recovery"\n'
                'replace "http://127.0.0.1:18765/media"\nshutdown\n',
            ),
            "recovery-loaded": (
                "fail-post-open-range-recovery",
                'load "http://127.0.0.1:18765/media"\n'
                'load "http://127.0.0.1:18765/recovery"\nshutdown\n',
            ),
            "missing-recovery": (
                "fail-post-open-range-recovery",
                'load "http://127.0.0.1:18765/media"\nshutdown\n',
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "case.scenario"
            scenario.write_text(valid)
            stress_fixture_service.validate_scenario(
                scenario, 18765, 20.0, "fail-post-open-range-recovery"
            )
            for label, (mode, text) in mutations.items():
                with self.subTest(mutation=label):
                    scenario.write_text(text)
                    with self.assertRaises(stress_fixture_service.FixtureError):
                        stress_fixture_service.validate_scenario(
                            scenario, 18765, 20.0, mode
                        )

    def test_scenario_rejects_seek_beyond_fixture_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "case.scenario"
            scenario.write_text(
                'load "http://127.0.0.1:18765/media"\nseek 12.0\nwait_time 13.0 5000\nshutdown\n'
            )
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_scenario(scenario, 18765, 8.0)
            stress_fixture_service.validate_scenario(scenario, 18765, 120.0)
            scenario.write_text(
                'load "http://127.0.0.1:18765/media"\nSEEK 1e2\nshutdown\n'
            )
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_scenario(scenario, 18765, 8.0)

    def test_seek_settle_target_and_arguments_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "case.scenario"
            prefix = 'load "http://127.0.0.1:18765/media"\n'
            scenario.write_text(
                prefix + "wait_seek_settled 7.5 0.5 15000\nshutdown\n"
            )
            stress_fixture_service.validate_scenario(scenario, 18765, 8.0)

            invalid_commands = (
                "wait_seek_settled 8.0 0.5 15000",
                "wait_seek_settled 7.5 60.1 15000",
                "wait_seek_settled 7.5 0.5 3600001",
                "wait_seek_settled 7.5 0.5",
                "wait_seek_settled 7.5 0.5 15000 extra",
            )
            for command in invalid_commands:
                with self.subTest(command=command):
                    scenario.write_text(prefix + command + "\nshutdown\n")
                    with self.assertRaises(stress_fixture_service.FixtureError):
                        stress_fixture_service.validate_scenario(
                            scenario, 18765, 8.0
                        )

    def test_seek_settle_advances_static_clock_and_clears_pending_seek(self) -> None:
        stress_fixture_service.validate_seek_event_contract(
            'load "http://127.0.0.1:18765/media"\n'
            "play\n"
            "wait_time 1.0 5000\n"
            "seek 6.0\n"
            "wait_seek_settled 6.0 0.5 15000\n"
            "wait_ms 1000\n"
            "seek 10.5\n"
            "wait_event SEEKED 5000\n"
            "shutdown\n"
        )

    def test_running_seeked_wait_must_be_outside_ge_suppression_window(self) -> None:
        prefix = (
            'load "http://127.0.0.1:18765/media"\n'
            'play\n'
            'wait_time 2.5 5000\n'
        )
        with self.assertRaisesRegex(
            stress_fixture_service.FixtureError,
            "inside GE's 3.0-second suppression window",
        ):
            stress_fixture_service.validate_seek_event_contract(
                prefix + 'seek 5.0\nwait_event SEEKED 5000\nshutdown\n'
            )
        with self.assertRaisesRegex(
            stress_fixture_service.FixtureError,
            "inside GE's 3.0-second suppression window",
        ):
            stress_fixture_service.validate_seek_event_contract(
                prefix + 'seek 5.5\nwait_event SEEKED 5000\nshutdown\n'
            )
        stress_fixture_service.validate_seek_event_contract(
            prefix + 'seek 6.0\nwait_event SEEKED 5000\nshutdown\n'
        )

    def test_seek_contract_expands_loops_and_allows_suppressed_corrections(self) -> None:
        stress_fixture_service.validate_seek_event_contract(
            'play\n'
            'wait_time 1.0 5000\n'
            'loop 2\n'
            'seek 6.0\nwait_event SEEKED 5000\nwait_time 6.5 5000\n'
            'seek 0.5\nwait_event SEEKED 5000\nwait_time 1.0 5000\n'
            'endloop\n'
            '# A small synchronization correction deliberately has no SEEKED wait.\n'
            'seek 1.15\nwait_ms 50\nshutdown\n'
        )

    def test_paused_near_clock_seek_can_expect_seeked(self) -> None:
        stress_fixture_service.validate_seek_event_contract(
            'play\nwait_time 10.0 5000\npause\n'
            'seek 10.1\nwait_event SEEKED 5000\nshutdown\n'
        )

    def test_checked_in_driver_examples_obey_seek_event_contract(self) -> None:
        examples = Path(__file__).parents[1] / "media-engine-stress/driver/examples"
        scenarios = sorted(examples.glob("*.scenario"))
        self.assertTrue(scenarios)
        for scenario in scenarios:
            with self.subTest(scenario=scenario.name):
                stress_fixture_service.validate_seek_event_contract(
                    scenario.read_text(encoding="utf-8")
                )

    def test_vod_scenarios_consume_each_playing_event_once(self) -> None:
        scenarios = [
            Path(__file__).parents[1]
            / "media-engine-stress/driver/examples/vod-seek.scenario",
            Path(__file__).with_name("examples") / "vod-seek-loopback.scenario.in",
        ]
        for scenario in scenarios:
            commands = [
                [word.lower() for word in words]
                for _number, words in stress_fixture_service.expanded_scenario_commands(
                    scenario.read_text(encoding="utf-8")
                )
            ]
            with self.subTest(scenario=scenario.name):
                self.assertEqual(sum(words[0] == "play" for words in commands), 2)
                self.assertEqual(
                    sum(words[:2] == ["wait_event", "playing"] for words in commands),
                    2,
                )

    def test_loopback_template_observes_startup_before_the_pre_seek_hold(self) -> None:
        template = (
            Path(__file__).with_name("examples")
            / "vod-seek-loopback.scenario.in"
        )
        commands = stress_fixture_service.expanded_scenario_commands(
            template.read_text(encoding="utf-8")
        )
        waits_before_first_seek: list[str] = []
        for _number, words in commands:
            command = words[0].lower()
            if command == "seek":
                break
            if command == "wait_time":
                waits_before_first_seek.append(words[1])
        self.assertEqual(
            waits_before_first_seek,
            ["@STARTUP_WAIT@", "@INITIAL_WAIT@"],
        )

    def test_common_codec_rapid_scrub_is_consecutive_and_fits_twenty_seconds(self) -> None:
        template = (
            Path(__file__).with_name("examples")
            / "rapid-scrub-common-codec.scenario.in"
        )
        text = template.read_text(encoding="utf-8")
        replacements = {
            "@STARTUP_WAIT@": "1.0",
            "@INITIAL_WAIT@": "2.0",
            "@SCRUB_1@": "6.0",
            "@SCRUB_2@": "7.5",
            "@SCRUB_3@": "9.0",
            "@SCRUB_4@": "10.5",
            "@SCRUB_5@": "12.0",
            "@SCRUB_6@": "13.5",
            "@SCRUB_7@": "15.0",
            "@SCRUB_8@": "16.25",
            "@FINAL_WAIT@": "17.25",
        }
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)

        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "rapid-scrub-common-codec.scenario"
            scenario.write_text(text, encoding="utf-8")
            stress_fixture_service.validate_scenario(scenario, 18765, 20.007)
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_scenario(scenario, 18765, 8.0)

        commands = [
            [word.lower() for word in words]
            for _number, words in stress_fixture_service.expanded_scenario_commands(text)
        ]
        first_seek = next(
            index for index, words in enumerate(commands) if words[0] == "seek"
        )
        self.assertEqual(
            commands[first_seek : first_seek + 8],
            [
                ["seek", "6.0"],
                ["seek", "7.5"],
                ["seek", "9.0"],
                ["seek", "10.5"],
                ["seek", "12.0"],
                ["seek", "13.5"],
                ["seek", "15.0"],
                ["seek", "16.25"],
            ],
        )
        self.assertEqual(
            commands[first_seek + 8],
            ["wait_seek_settled", "16.25", "0.500", "20000"],
        )

    def test_no_range_recovery_uses_wall_clock_after_far_seek(self) -> None:
        template = (
            Path(__file__).with_name("examples")
            / "progressive-no-range.scenario.in"
        )
        commands = [
            [word.lower() for word in words]
            for _number, words in stress_fixture_service.expanded_scenario_commands(
                template.read_text(encoding="utf-8")
            )
        ]
        seek_index = next(
            index for index, words in enumerate(commands) if words[0] == "seek"
        )
        self.assertEqual(
            commands[seek_index : seek_index + 3],
            [
                ["seek", "@unsupported_seek@"],
                ["wait_ms", "@recovery_hold_ms@"],
                ["snapshot", "no-range-sequential"],
            ],
        )

    def test_recovery_and_finite_eof_templates_use_only_existing_driver_actions(self) -> None:
        examples = Path(__file__).with_name("examples")
        allowed_actions = {
            "load", "replace", "wait_event", "wait_time", "play", "seek",
            "snapshot", "shutdown",
        }
        substitutions = {
            "http-failed-range-recovery.scenario.in": {
                "@STARTUP_WAIT@": "1.0",
                "@INITIAL_WAIT@": "12.0",
                "@FAILED_SEEK@": "83.0",
                "@RECOVERY_WAIT@": "3.0",
            },
            "finite-eof-near-end.scenario.in": {
                "@STARTUP_WAIT@": "1.0",
                "@INITIAL_WAIT@": "12.0",
                "@EOF_SEEK@": "118.0",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            rendered = Path(directory) / "case.scenario"
            for name, replacements in substitutions.items():
                text = (examples / name).read_text(encoding="utf-8")
                for placeholder, value in replacements.items():
                    text = text.replace(placeholder, value)
                commands = stress_fixture_service.expanded_scenario_commands(text)
                with self.subTest(template=name):
                    self.assertTrue(commands)
                    self.assertTrue(
                        all(words[0].lower() in allowed_actions for _line, words in commands)
                    )
                    rendered.write_text(text, encoding="utf-8")
                    stress_fixture_service.validate_scenario(
                        rendered,
                        18765,
                        120.0,
                        "fail-post-open-range-recovery"
                        if name.startswith("http-failed") else "range",
                    )
            eof_commands = [
                [word.lower() for word in words]
                for _line, words in stress_fixture_service.expanded_scenario_commands(
                    rendered.read_text(encoding="utf-8")
                )
            ]
            self.assertEqual(
                eof_commands[-4:],
                [
                    ["wait_event", "seeked", "10000"],
                    ["wait_event", "ended", "15000"],
                    ["snapshot", "finite-eof-ended"],
                    ["shutdown"],
                ],
            )

    def test_recovery_and_finite_eof_oracles_keep_fail_closed_invariants(self) -> None:
        source = Path(__file__).with_name("stress-case.nix").read_text(
            encoding="utf-8"
        )
        recovery = source.split(
            "failedRangeRecoveryControl = identity // {", 1
        )[1].split("failedRangeRecoveryInstrumented =", 1)[0]
        finite_eof = source.split(
            "finiteEofControl = healthy // {", 1
        )[1].split("finiteEofInstrumented =", 1)[0]
        recovery_case = source.split(
            '"http-failed-range-recovery" = {', 1
        )[1].split('"finite-eof-near-end" = {', 1)[0]

        for required in (
            'label = "failed-range-terminal";',
            'label = "recovery-generation-steady";',
            'media_error_hr = "0xc00d426a";',
            "min_source_generation = 2;",
            "max_source_generation = 2;",
            "max_stale_events = 0;",
            "SEEKED = 0;",
            '"CANPLAY" "PLAYING" "ERROR" "CANPLAY" "FIRSTFRAMEREADY"',
            "FIRSTFRAMEREADY = 2;",
            'required_events_per_generation = [ "CANPLAY" ];',
        ):
            with self.subTest(recovery_invariant=required):
                self.assertIn(required, recovery)
        self.assertIn(
            'mode = "fail-post-open-range-recovery";',
            recovery_case,
        )
        self.assertIn("maxErrors = 1;", recovery_case)

        self.assertIn("finiteEofControl = healthy // {", source)
        for required in (
            'label = "finite-eof-ended";',
            'event = "ENDED";',
            "ended = true;",
            "ERROR = 0;",
            "ENDED = 1;",
            "require_seek_completion = true;",
            "required_completed_seek_targets = [ (number selected.eofSeek) ];",
        ):
            with self.subTest(finite_eof_invariant=required):
                self.assertIn(required, finite_eof)

    def test_fixture_ready_sums_and_manifest_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "av").mkdir()
            (root / "provenance").mkdir()
            media = root / "av/faststart.mp4"
            media.write_bytes(b"fixture-bytes")
            media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
            manifest = root / "provenance/manifest.json"
            probe = root / "provenance/faststart.probe.json"
            probe.write_text(json.dumps({"format": {"duration": "8.000000"}}) + "\n")
            probe_hash = hashlib.sha256(probe.read_bytes()).hexdigest()
            manifest.write_text(json.dumps({
                "artifacts": [{
                    "bytes": media.stat().st_size,
                    "ffprobe": {
                        "path": "provenance/faststart.probe.json",
                        "sha256": probe_hash,
                        "status": "accepted",
                    },
                    "path": "av/faststart.mp4",
                    "sha256": media_hash,
                }],
            }, sort_keys=True) + "\n")
            sums = root / "SHA256SUMS"
            sums.write_text(
                f"{media_hash}  av/faststart.mp4\n"
                f"{probe_hash}  provenance/faststart.probe.json\n"
            )
            ready = root / "READY"
            ready.write_text(json.dumps({
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "profile": "smoke",
                "schema": 1,
                "sha256sums_sha256": hashlib.sha256(sums.read_bytes()).hexdigest(),
            }) + "\n")
            self.assertEqual(
                stress_fixture_service.validate_fixture_set(root, manifest, config()),
                (media, 8.0),
            )
            media.write_bytes(b"tampered")
            with self.assertRaises(stress_fixture_service.FixtureError):
                stress_fixture_service.validate_fixture_set(root, manifest, config())

    def test_serve_passes_a_required_completion_marker_to_the_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.mp4"
            fixture.write_bytes(b"fixture")
            fixture_script = root / "http_stream_fixture.py"
            fixture_script.write_text("# fixture\n")
            args = Namespace(
                completion_marker=root / "http.complete.json",
                config=root / "config.json",
                fixture_manifest=root / "manifest.json",
                fixture_root=root,
                fixture_script=fixture_script,
                log=root / "http.jsonl",
                scenario=root / "case.scenario",
            )
            with mock.patch.object(
                stress_fixture_service,
                "load_and_validate",
                return_value=(config(), fixture),
            ), mock.patch.object(stress_fixture_service.os, "execv") as execv:
                stress_fixture_service.command_serve(args)

            argv = execv.call_args.args[1]
            marker_index = argv.index("--completion-marker")
            self.assertEqual(str(args.completion_marker), argv[marker_index + 1])
            self.assertEqual(1, argv.count("--completion-marker"))
            self.assertEqual(1, argv.count("--fail-count"))

    def test_serve_cli_requires_the_completion_marker(self) -> None:
        parser = stress_fixture_service.build_parser()
        common = [
            "serve",
            "--config", "/tmp/config",
            "--fixture-root", "/tmp/fixtures",
            "--fixture-manifest", "/tmp/manifest",
            "--scenario", "/tmp/scenario",
            "--fixture-script", "/tmp/service",
            "--log", "/tmp/log",
        ]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(common)
        parsed = parser.parse_args(common + ["--completion-marker", "/tmp/complete"])
        self.assertEqual(Path("/tmp/complete"), parsed.completion_marker)


if __name__ == "__main__":
    unittest.main()
