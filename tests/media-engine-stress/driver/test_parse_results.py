#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import unittest

import parse_results


def common(seq: int, kind: str, *, generation: int = 1, timeline: int = 1,
           time: float | None = 0.0, ready: int = 3, audio: bool = True,
           video: bool = True, paused: bool = False, seeking: bool = False) -> dict:
    return {
        "type": kind,
        "seq": seq,
        "monotonic_ms": seq * 10,
        "monotonic_origin_ms": 100000,
        "event": None,
        "source_generation": generation,
        "timeline_generation": timeline,
        "engine_alive": True,
        "time": time,
        "duration": 60.0,
        "rate": 1.0,
        "paused": paused,
        "seeking": seeking,
        "ended": False,
        "has_audio": audio,
        "has_video": video,
        "network": 1,
        "network_name": "IDLE",
        "ready": ready,
        "ready_name": "HAVE_FUTURE_DATA",
        "media_error": 0,
        "media_error_hr": "0x00000000",
        "audio_monitor_enabled": False,
        "audio_samples_total": 0,
        "audio_bytes_total": 0,
        "audio_samples_generation": 0,
        "audio_bytes_generation": 0,
        "audio_last_sample_time_100ns": None,
        "audio_last_sample_duration_100ns": None,
        "audio_last_monotonic_ms": None,
        "audio_payload_observed": False,
        "audio_payload_format": "unknown",
        "audio_channels": 0,
        "audio_rate": 0,
        "audio_bits": 0,
        "audio_payload_bytes_scanned_total": 0,
        "audio_nonzero_bytes_total": 0,
        "audio_nonzero_units_total": 0,
        "audio_nonzero_units_generation": 0,
        "audio_peak_abs": 0.0,
        "audio_last_nonzero_monotonic_ms": None,
        "driver_sha256": None,
        "scenario_sha256": None,
    }


def event(seq: int, name: str, **kwargs) -> dict:
    record = common(seq, "event", **kwargs)
    record.update(
        event=name,
        event_id=14,
        param1="0x0",
        param2="0x00000000",
        generation_current=True,
    )
    return record


def snapshot(seq: int, action: str, **kwargs) -> dict:
    record = common(seq, "snapshot", **kwargs)
    record.update(action=action, label=None, status="ok", hr="0x00000000", action_value=None)
    return record


def result(seq: int, status: str = "pass", exit_code: int = 0, **kwargs) -> dict:
    record = common(seq, "result", **kwargs)
    record.update(status=status, exit_code=exit_code, actions_completed=4, message="done")
    return record


def passing_records() -> list[dict]:
    return [
        snapshot(1, "load", time=0.0, ready=0, audio=False, video=False),
        event(2, "CANPLAY", time=0.0),
        snapshot(3, "play", time=0.0),
        event(4, "PLAYING", time=0.1),
        snapshot(5, "wait_time", time=2.0),
        result(6, time=2.0),
    ]


class ParserTests(unittest.TestCase):
    def test_example_oracles_bind_exact_scenario_bytes(self) -> None:
        examples = pathlib.Path(__file__).with_name("examples")
        for stem in (
            "join-at-offset",
            "live-av",
            "seek-storm-latest-wins",
            "vod-seek",
        ):
            with self.subTest(stem=stem):
                scenario = examples / f"{stem}.scenario"
                oracle = json.loads((examples / f"{stem}.oracle.json").read_text(
                    encoding="utf-8"))
                actual = hashlib.sha256(scenario.read_bytes()).hexdigest()
                self.assertEqual(actual, oracle["expected_scenario_sha256"])

    def test_valid_jsonl_and_summary(self) -> None:
        records = passing_records()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory, "run.jsonl")
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            parsed = parse_results.load_records(path)
        self.assertEqual(parse_results.summary(parsed)["result"], "pass")

    def test_rejects_sequence_gap(self) -> None:
        records = passing_records()
        records[2]["seq"] = 9
        with self.assertRaisesRegex(parse_results.ParseError, "expected seq"):
            parse_results.validate_sequence(records)

    def test_rejects_nonfinal_result(self) -> None:
        records = passing_records()
        records[3], records[-1] = records[-1], records[3]
        for index, record in enumerate(records, 1):
            record["seq"] = index
            record["monotonic_ms"] = index * 10
        with self.assertRaisesRegex(parse_results.ParseError, "result record must be last"):
            parse_results.validate_sequence(records)

    def test_rejects_unknown_oracle_key(self) -> None:
        with self.assertRaisesRegex(parse_results.ParseError, "unknown"):
            parse_results.validate_oracle({"made_up": True})

    def test_rejects_invalid_checkpoint_contract(self) -> None:
        with self.assertRaisesRegex(parse_results.ParseError, "requires action and label"):
            parse_results.validate_oracle({
                "checkpoint_expectations": [{"label": "final"}],
            })
        with self.assertRaisesRegex(parse_results.ParseError, "unknown keys"):
            parse_results.validate_oracle({
                "checkpoint_expectations": [{
                    "action": "snapshot",
                    "label": "final",
                    "raw_url": "forbidden",
                }],
            })
        with self.assertRaisesRegex(parse_results.ParseError, "min_time exceeds max_time"):
            parse_results.validate_oracle({
                "checkpoint_expectations": [{
                    "action": "snapshot",
                    "label": "final",
                    "min_time": 8.0,
                    "max_time": 7.0,
                }],
            })

    def test_rejects_unapproved_extra_record_field(self) -> None:
        record = passing_records()[0]
        record["raw_url"] = "https://example.invalid/?token=secret"
        with self.assertRaisesRegex(parse_results.ParseError, "unexpected fields"):
            parse_results.validate_record(record, 1)

    def test_rejects_pass_with_nonzero_exit(self) -> None:
        record = result(1, status="pass", exit_code=5)
        with self.assertRaisesRegex(parse_results.ParseError, "disagree"):
            parse_results.validate_record(record, 1)

    def test_rejects_nonzero_audio_counter_regression(self) -> None:
        records = passing_records()
        records[2]["audio_nonzero_units_total"] = 10
        records[4]["audio_nonzero_units_total"] = 9
        with self.assertRaisesRegex(parse_results.ParseError, "nonzero audio unit total regressed"):
            parse_results.validate_sequence(records)

    def test_rejects_monotonic_origin_change(self) -> None:
        records = passing_records()
        records[3]["monotonic_origin_ms"] += 1
        with self.assertRaisesRegex(parse_results.ParseError, "monotonic_origin_ms changed"):
            parse_results.validate_sequence(records)


class OracleTests(unittest.TestCase):
    def test_full_vod_oracle_passes(self) -> None:
        oracle = {
            "expected_result": "pass",
            "expected_exit_code": 0,
            "min_source_generations": 1,
            "required_events": ["CANPLAY", "PLAYING"],
            "required_events_per_generation": ["CANPLAY"],
            "forbidden_events": ["ERROR"],
            "required_actions": ["load", "play", "wait_time"],
            "require_audio_each_generation": True,
            "require_video_each_generation": True,
            "require_av_same_snapshot_each_generation": True,
            "max_media_error_events": 0,
            "max_timeout_snapshots": 0,
            "max_time_regression": 0.05,
            "min_playback_advance": 1.0,
            "event_order": ["CANPLAY", "PLAYING"],
        }
        self.assertEqual(parse_results.evaluate(passing_records(), oracle), [])

    def test_terminal_network_error_is_fenced_and_exact(self) -> None:
        seek = snapshot(1, "seek", generation=1, timeline=2, time=12.0)
        seek["action_value"] = 83.0
        error = event(2, "ERROR", generation=1, timeline=2, time=12.0)
        error.update(
            param1="0x2",
            param2="0xc00d426a",
            media_error=2,
            media_error_hr="0xc00d426a",
            seeking=False,
            ended=False,
        )
        records = [seek, error, result(3, generation=1, timeline=2, time=12.0)]
        oracle = {
            "event_expectations": [{
                "event": "ERROR",
                "min_matches": 1,
                "max_matches": 1,
                "after_action": "seek",
                "param1": "0x2",
                "param2": "0xc00d426a",
                "generation_current": True,
                "media_error": 2,
                "media_error_hr": "0xc00d426a",
                "seeking": False,
                "ended": False,
            }],
        }
        self.assertEqual(parse_results.evaluate(records, oracle), [])

        mutations = {
            "event-before-seek": lambda mutated: mutated.__setitem__(
                slice(None),
                [
                    {**mutated[1], "seq": 1, "monotonic_ms": 10},
                    {**mutated[0], "seq": 2, "monotonic_ms": 20},
                    mutated[2],
                ],
            ),
            "wrong-param1": lambda mutated: mutated[1].__setitem__("param1", "0x1"),
            "wrong-param2": lambda mutated: mutated[1].__setitem__("param2", "0xc00d36b0"),
            "wrong-media-error": lambda mutated: mutated[1].__setitem__("media_error", 1),
            "wrong-extended-error": lambda mutated: mutated[1].__setitem__(
                "media_error_hr", "0xc00d36b0"
            ),
            "still-seeking": lambda mutated: mutated[1].__setitem__("seeking", True),
            "ended": lambda mutated: mutated[1].__setitem__("ended", True),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label):
                mutated = [dict(record) for record in records]
                mutate(mutated)
                self.assertTrue(parse_results.evaluate(mutated, oracle))

    def test_recovery_accepts_first_frame_without_redundant_playing(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "CANPLAY", generation=1, timeline=1, time=0.0),
            snapshot(3, "play", generation=1, timeline=1, time=0.0),
            event(4, "FIRSTFRAMEREADY", generation=1, timeline=1, time=0.0),
            event(5, "PLAYING", generation=1, timeline=1, time=0.0),
            snapshot(6, "wait_time", generation=1, timeline=1, time=12.0),
            snapshot(7, "seek", generation=1, timeline=2, time=83.0),
            event(8, "ERROR", generation=1, timeline=2, time=83.0),
            snapshot(9, "replace", generation=2, timeline=3, time=12.0),
            event(10, "CANPLAY", generation=2, timeline=3, time=12.0),
            snapshot(11, "play", generation=2, timeline=3, time=0.0),
            event(12, "FIRSTFRAMEREADY", generation=2, timeline=3, time=0.02),
            snapshot(13, "snapshot", generation=2, timeline=3, time=3.0),
            result(14, generation=2, timeline=3, time=3.0),
        ]
        records[6]["action_value"] = 83.0
        records[7].update(
            param1="0x2",
            param2="0xc00d426a",
            media_error=2,
            media_error_hr="0xc00d426a",
        )
        records[8].update(media_error=0, media_error_hr="0x00000000")
        recovery = records[12]
        recovery["label"] = "recovery-generation-steady"
        for record in (records[11], recovery, records[-1]):
            record["audio_monitor_enabled"] = True
            record["audio_samples_total"] = 241
            record["audio_bytes_total"] = 1974272
            record["audio_samples_generation"] = 241
            record["audio_bytes_generation"] = 1974272
            record["audio_last_monotonic_ms"] = record["monotonic_ms"]
            record["audio_payload_observed"] = True
            record["audio_payload_format"] = "pcm16"
            record["audio_channels"] = 2
            record["audio_rate"] = 48000
            record["audio_bits"] = 16
            record["audio_payload_bytes_scanned_total"] = 1974272
            record["audio_nonzero_bytes_total"] = 493568
            record["audio_nonzero_units_total"] = 246784
            record["audio_nonzero_units_generation"] = 246784
            record["audio_peak_abs"] = 1000.0
            record["audio_last_nonzero_monotonic_ms"] = record["monotonic_ms"]

        oracle = {
            "checkpoint_expectations": [{
                "action": "snapshot",
                "label": "recovery-generation-steady",
                "min_matches": 1,
                "max_matches": 1,
                "min_time": 2.999,
                "max_time": 5.0,
                "min_source_generation": 2,
                "max_source_generation": 2,
                "min_timeline_generation": 3,
                "max_timeline_generation": 3,
                "paused": False,
                "seeking": False,
                "ended": False,
                "has_audio": True,
                "has_video": True,
                "media_error": 0,
                "media_error_hr": "0x00000000",
            }],
            "event_order": [
                "CANPLAY", "PLAYING", "ERROR", "CANPLAY", "FIRSTFRAMEREADY",
            ],
            "required_events_per_generation": ["CANPLAY"],
            "max_stale_events": 0,
            "audio_check_labels": ["recovery-generation-steady"],
            "min_audio_samples_generation": 10,
            "min_audio_bytes_generation": 4096,
            "min_audio_nonzero_units_generation": 20,
            "max_audio_silence_ms": 750,
            "max_audio_nonzero_silence_ms": 750,
            "required_audio_payload_formats": ["pcm16", "pcm32", "float32"],
        }
        self.assertEqual(parse_results.evaluate(records, oracle), [])

        mutations = {
            "no-recovery-advance": (
                lambda mutated: mutated[12].__setitem__("time", 0.0),
                "time 0.0 is below",
            ),
            "no-recovery-audio": (
                lambda mutated: mutated[12].update(
                    has_audio=False,
                    audio_samples_generation=0,
                    audio_bytes_generation=0,
                    audio_nonzero_units_generation=0,
                    audio_last_monotonic_ms=None,
                    audio_last_nonzero_monotonic_ms=None,
                ),
                "has_audio is False",
            ),
            "stale-recovery-first-frame": (
                lambda mutated: mutated[11].__setitem__(
                    "generation_current", False
                ),
                "stale-generation events observed",
            ),
        }
        for label, (mutate, expected_failure) in mutations.items():
            with self.subTest(mutation=label):
                mutated = [dict(record) for record in records]
                mutate(mutated)
                failures = parse_results.evaluate(mutated, oracle)
                self.assertTrue(
                    any(expected_failure in failure for failure in failures),
                    failures,
                )

    def test_event_expectation_schema_is_strict(self) -> None:
        invalid = (
            [],
            [{"event": "ERROR", "param1": "2"}],
            [{"event": "ERROR", "param2": "0x2"}],
            [{"event": "ERROR", "media_error_hr": "0xC00D426A"}],
            [{"event": "ERROR", "min_matches": 2, "max_matches": 1}],
            [{"event": "ERROR", "raw_url": "forbidden"}],
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(parse_results.ParseError):
                parse_results.validate_oracle({"event_expectations": value})

    def test_missing_audio_is_not_hidden_by_video(self) -> None:
        records = passing_records()
        for record in records:
            record["has_audio"] = False
        failures = parse_results.evaluate(records, {"require_av_same_snapshot_each_generation": True})
        self.assertTrue(any("never coexist" in failure for failure in failures))

    def test_live_oracle_rejects_persistent_public_pause_state(self) -> None:
        oracle_path = pathlib.Path(__file__).with_name("examples") / "live-av.oracle.json"
        live_oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
        records = []
        for index in range(8):
            record = snapshot(index + 1, "snapshot", time=float(index + 1), paused=True)
            record["label"] = "live-steady"
            records.append(record)
        records.append(result(9, time=8.0, paused=True))

        failures = parse_results.evaluate(
            records,
            {"checkpoint_expectations": live_oracle["checkpoint_expectations"]},
        )
        paused_failures = [failure for failure in failures if "paused is True" in failure]
        self.assertEqual(8, len(paused_failures))
        self.assertFalse(any("time span" in failure for failure in failures))

        self.assertGreaterEqual(live_oracle["min_playback_advance"], 5.0)

    def test_checkpoint_time_span_rejects_a_stalled_label_group(self) -> None:
        records = []
        for index in range(4):
            record = snapshot(index + 1, "snapshot", time=2.0)
            record["label"] = "steady"
            records.append(record)
        records.append(result(5, time=2.0))
        failures = parse_results.evaluate(
            records,
            {
                "checkpoint_expectations": [{
                    "action": "snapshot",
                    "label": "steady",
                    "min_matches": 4,
                    "min_time_span": 2.0,
                }],
            },
        )
        self.assertTrue(any("time span" in failure for failure in failures))

    def test_checkpoint_expectation_binds_final_time_state_and_generation(self) -> None:
        records = passing_records()
        checkpoint = records[4]
        checkpoint["action"] = "snapshot"
        checkpoint["label"] = "final-target"
        checkpoint["time"] = 48.5
        checkpoint["timeline_generation"] = 2
        records[-1]["time"] = 48.5
        records[-1]["timeline_generation"] = 2
        parse_results.validate_sequence(records)
        expectation = {
            "checkpoint_expectations": [{
                "action": "snapshot",
                "label": "final-target",
                "min_matches": 1,
                "max_matches": 1,
                "min_time": 48.0,
                "max_time": 49.0,
                "paused": False,
                "seeking": False,
                "has_audio": True,
                "has_video": True,
                "min_timeline_generation": 2,
                "max_timeline_generation": 2,
            }],
        }
        self.assertEqual(parse_results.evaluate(records, expectation), [])
        checkpoint["time"] = 3.0
        checkpoint["paused"] = True
        failures = parse_results.evaluate(records, expectation)
        self.assertTrue(any("time 3.0 is below" in failure for failure in failures))
        self.assertTrue(any("paused is True" in failure for failure in failures))

    def test_explicit_seek_starts_new_timeline(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "PLAYING", generation=1, timeline=1, time=10.0),
            snapshot(3, "seek", generation=1, timeline=2, time=10.0),
            event(4, "SEEKING", generation=1, timeline=2, time=10.0, seeking=True),
            event(5, "SEEKED", generation=1, timeline=2, time=0.2),
            snapshot(6, "wait_time", generation=1, timeline=2, time=0.3),
            result(7, generation=1, timeline=2, time=0.3),
        ]
        records[2]["action_value"] = 0.2
        self.assertEqual(parse_results.evaluate(records, {"max_time_regression": 0.0}), [])

    def test_source_clock_reset_before_playing_is_not_a_regression(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "PLAYING", generation=1, timeline=1, time=0.0),
            snapshot(3, "wait_time", generation=1, timeline=1, time=5.28),
            snapshot(4, "replace", generation=2, timeline=2, time=5.28),
            event(5, "CANPLAY", generation=2, timeline=2, time=5.28),
            event(6, "PLAYING", generation=2, timeline=2, time=0.04),
            event(7, "FIRSTFRAMEREADY", generation=2, timeline=2, time=0.05),
            snapshot(8, "wait_time", generation=2, timeline=2, time=2.0),
            result(9, generation=2, timeline=2, time=2.0),
        ]
        self.assertEqual(
            parse_results.evaluate(
                records,
                {"max_time_regression": 0.0, "min_playback_advance": 1.0},
            ),
            [],
        )

    def test_source_clock_regression_after_playing_still_fails(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=8.0),
            event(2, "CANPLAY", generation=1, timeline=1, time=8.0),
            event(3, "PLAYING", generation=1, timeline=1, time=0.0),
            snapshot(4, "wait_time", generation=1, timeline=1, time=2.0),
            snapshot(5, "wait_ms", generation=1, timeline=1, time=0.5),
            result(6, generation=1, timeline=1, time=0.5),
        ]
        failures = parse_results.evaluate(records, {"max_time_regression": 0.01})
        self.assertTrue(any("regressed" in failure for failure in failures))

    def test_seek_clock_regression_after_seeked_still_fails(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "PLAYING", generation=1, timeline=1, time=5.0),
            snapshot(3, "seek", generation=1, timeline=2, time=5.0),
            event(4, "SEEKED", generation=1, timeline=2, time=20.0),
            snapshot(5, "wait_time", generation=1, timeline=2, time=21.0),
            snapshot(6, "wait_ms", generation=1, timeline=2, time=19.0),
            result(7, generation=1, timeline=2, time=19.0),
        ]
        records[2]["action_value"] = 20.0
        failures = parse_results.evaluate(records, {"max_time_regression": 0.01})
        self.assertTrue(any("regressed" in failure for failure in failures))

    def test_seek_target_requires_matching_seeked_state(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            snapshot(2, "seek", generation=1, timeline=2, time=0.0),
            event(3, "SEEKED", generation=1, timeline=2, time=12.0),
            result(4, generation=1, timeline=2, time=12.0),
        ]
        records[1]["action_value"] = 12.0
        self.assertEqual(
            parse_results.evaluate(
                records,
                {
                    "required_seek_targets": [12.0],
                    "require_seek_completion": True,
                    "seek_target_tolerance": 0.1,
                },
            ),
            [],
        )
        records[2]["time"] = 8.0
        failures = parse_results.evaluate(
            records,
            {"require_seek_completion": True, "seek_target_tolerance": 0.1},
        )
        self.assertTrue(any("no matching SEEKED" in failure for failure in failures))

    def test_completed_seek_targets_accept_ordered_intermediate_completions(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "SEEKED", generation=1, timeline=2, time=30.04),
            event(3, "SEEKED", generation=1, timeline=3, time=55.0),
            event(4, "SEEKED", generation=1, timeline=4, time=83.25),
            result(5, generation=1, timeline=4, time=83.25),
        ]
        self.assertEqual(
            parse_results.evaluate(
                records,
                {"required_completed_seek_targets": [30.0, 83.25]},
            ),
            [],
        )

    def test_completed_seek_targets_reject_reversed_completion_order(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "SEEKED", generation=1, timeline=2, time=83.25),
            event(3, "SEEKED", generation=1, timeline=3, time=30.0),
            result(4, generation=1, timeline=3, time=30.0),
        ]
        failures = parse_results.evaluate(
            records,
            {"required_completed_seek_targets": [30.0, 83.25]},
        )
        self.assertEqual(len(failures), 1)
        self.assertIn(
            "first unmatched target 83.25 at required index 1",
            failures[0],
        )
        self.assertIn(
            "current-generation successful SEEKED times [83.25, 30.0]",
            failures[0],
        )

    def test_completed_seek_targets_ignore_stale_and_failed_seeked_events(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            event(2, "SEEKED", generation=1, timeline=2, time=30.0),
            event(3, "SEEKED", generation=1, timeline=3, time=83.25),
            result(4, generation=1, timeline=3, time=83.25),
        ]
        records[1]["generation_current"] = False
        records[2]["param2"] = "0x80004005"
        failures = parse_results.evaluate(
            records,
            {"required_completed_seek_targets": [30.0]},
        )
        self.assertEqual(len(failures), 1)
        self.assertIn(
            "current-generation successful SEEKED times []",
            failures[0],
        )

    def test_completed_seek_targets_schema_is_strict(self) -> None:
        invalid_values = (
            [],
            [True],
            [-1.0],
            [float("nan")],
            [float("inf")],
            ["30"],
            {"target": 30.0},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    parse_results.ParseError,
                    "required_completed_seek_targets",
                ):
                    parse_results.validate_oracle({
                        "required_completed_seek_targets": invalid,
                    })

    def test_seek_action_span_uses_first_and_last_successful_seek(self) -> None:
        records = [
            snapshot(1, "seek", generation=1, timeline=2, time=10.0),
            snapshot(2, "seek", generation=1, timeline=3, time=20.0),
            snapshot(3, "seek", generation=1, timeline=4, time=30.0),
            result(4, generation=1, timeline=4, time=30.0),
        ]
        for record, target in zip(records[:3], (10.0, 20.0, 30.0)):
            record["action_value"] = target
        records[0]["monotonic_ms"] = 100
        records[1]["monotonic_ms"] = 1000
        records[1]["status"] = "fail"
        records[2]["monotonic_ms"] = 220
        records[3]["monotonic_ms"] = 230

        self.assertEqual(
            parse_results.evaluate(records, {"max_seek_action_span_ms": 120}),
            [],
        )
        self.assertEqual(
            parse_results.evaluate(records, {"max_seek_action_span_ms": 119}),
            [
                "successful seek action span 120 ms (seq 1 to 3) exceeds 119 ms",
            ],
        )

    def test_seek_action_span_rejects_missing_successful_seek_data(self) -> None:
        records = [
            snapshot(1, "seek", generation=1, timeline=2, time=10.0),
            result(2, generation=1, timeline=2, time=10.0),
        ]
        records[0]["status"] = "fail"
        self.assertEqual(
            parse_results.evaluate(records, {"max_seek_action_span_ms": 250}),
            [
                "successful seek action span unavailable: "
                "no successful seek action snapshots",
            ],
        )

    def test_seek_action_span_schema_is_strict(self) -> None:
        for invalid in (True, "250", -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    parse_results.ParseError,
                    "max_seek_action_span_ms",
                ):
                    parse_results.validate_oracle({"max_seek_action_span_ms": invalid})

    def test_post_seek_nonzero_threshold_alone_is_enforced(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=0.0),
            snapshot(2, "seek", generation=1, timeline=2, time=12.0),
            event(3, "SEEKED", generation=1, timeline=2, time=12.0),
            snapshot(4, "wait_ms", generation=1, timeline=2, time=13.0),
            result(5, generation=1, timeline=2, time=13.0),
        ]
        records[1]["action_value"] = 12.0
        failures = parse_results.evaluate(records, {"min_post_seek_nonzero_units": 1})
        self.assertTrue(any("post-seek nonzero audio units" in failure for failure in failures))

    def test_vod_oracle_rejects_healthy_preseek_but_frozen_after_all_seeks(self) -> None:
        records: list[dict] = []

        def add(record: dict, *, audio_total: int = 50) -> dict:
            record["seq"] = len(records) + 1
            record["monotonic_ms"] = record["seq"] * 100
            record["audio_monitor_enabled"] = True
            record["audio_samples_total"] = audio_total
            record["audio_bytes_total"] = audio_total * 1024
            record["audio_samples_generation"] = audio_total
            record["audio_bytes_generation"] = audio_total * 1024
            record["audio_last_monotonic_ms"] = 100
            record["audio_payload_observed"] = True
            record["audio_payload_format"] = "pcm16"
            record["audio_channels"] = 2
            record["audio_rate"] = 48000
            record["audio_bits"] = 16
            record["audio_payload_bytes_scanned_total"] = audio_total * 1024
            record["audio_nonzero_bytes_total"] = audio_total * 100
            record["audio_nonzero_units_total"] = audio_total
            record["audio_nonzero_units_generation"] = audio_total
            record["audio_peak_abs"] = 1000.0
            record["audio_last_nonzero_monotonic_ms"] = 100
            records.append(record)
            return record

        add(snapshot(0, "load", generation=1, timeline=1, time=0.0))
        add(event(0, "CANPLAY", generation=1, timeline=1, time=0.0))
        wait = add(snapshot(0, "wait_event", generation=1, timeline=1, time=0.0))
        wait["label"] = "CANPLAY"
        add(snapshot(0, "play", generation=1, timeline=1, time=0.0))
        add(event(0, "PLAYING", generation=1, timeline=1, time=0.0))
        wait = add(snapshot(0, "wait_event", generation=1, timeline=1, time=0.0))
        wait["label"] = "PLAYING"
        wait = add(snapshot(0, "wait_time", generation=1, timeline=1, time=2.0))
        wait["action_value"] = 2.0

        for timeline, target in enumerate([12.0, 3.0] * 4, 2):
            seek = add(snapshot(0, "seek", generation=1, timeline=timeline, time=target))
            seek["action_value"] = target
            add(event(0, "SEEKING", generation=1, timeline=timeline, time=target))
            add(event(0, "SEEKED", generation=1, timeline=timeline, time=target))
            wait = add(snapshot(0, "wait_event", generation=1, timeline=timeline, time=target))
            wait["label"] = "SEEKED"
            frozen = add(snapshot(0, "wait_time", generation=1, timeline=timeline, time=target))
            frozen["action_value"] = target + 1.0

        timeline = 9
        add(snapshot(0, "pause", generation=1, timeline=timeline, time=3.0, paused=True))
        add(event(0, "PAUSE", generation=1, timeline=timeline, time=3.0, paused=True))
        wait = add(snapshot(0, "wait_event", generation=1, timeline=timeline, time=3.0, paused=True))
        wait["label"] = "PAUSE"
        add(snapshot(0, "play", generation=1, timeline=timeline, time=3.0))
        add(event(0, "PLAYING", generation=1, timeline=timeline, time=3.0))
        wait = add(snapshot(0, "wait_event", generation=1, timeline=timeline, time=3.0))
        wait["label"] = "PLAYING"
        add(snapshot(0, "shutdown", generation=1, timeline=timeline, time=3.0))
        add(result(0, generation=1, timeline=timeline, time=3.0))

        parse_results.validate_sequence(records)
        oracle_path = pathlib.Path(__file__).with_name("examples") / "vod-seek.oracle.json"
        failures = parse_results.evaluate(records, parse_results.load_oracle(oracle_path))
        self.assertTrue(any("post-seek advance" in failure for failure in failures))
        self.assertTrue(any("post-seek audio samples" in failure for failure in failures))
        self.assertTrue(any("post-seek nonzero audio units" in failure for failure in failures))

    def test_clock_regression_in_one_timeline_fails(self) -> None:
        records = passing_records()
        records[4]["time"] = 0.01
        failures = parse_results.evaluate(records, {"max_time_regression": 0.01})
        self.assertTrue(any("regressed" in failure for failure in failures))

    def test_shutdown_clock_reset_is_not_a_playback_regression(self) -> None:
        records = [
            snapshot(1, "load", time=10.0),
            event(2, "PLAYING", time=10.0),
            snapshot(3, "wait_time", time=11.0),
            snapshot(4, "shutdown", time=0.0),
            result(5, time=0.0),
        ]
        self.assertEqual(
            parse_results.evaluate(records, {"max_time_regression": 0.1}),
            [],
        )
        records[2]["time"] = 9.0
        failures = parse_results.evaluate(records, {"max_time_regression": 0.1})
        self.assertTrue(any("regressed" in failure for failure in failures))

    def test_shutdown_clock_reset_cannot_fake_playback_advance(self) -> None:
        records = [
            snapshot(1, "load", time=10.0),
            event(2, "PLAYING", time=10.0),
            snapshot(3, "wait_ms", time=10.1),
            snapshot(4, "shutdown", time=0.0),
            result(5, time=0.0),
        ]
        failures = parse_results.evaluate(records, {"min_playback_advance": 1.0})
        self.assertEqual(
            failures,
            ["greatest uninterrupted playback advance 0.100000 is below 1.000000"],
        )

    def test_timeout_and_error_event_fail(self) -> None:
        records = passing_records()
        records[1]["event"] = "ERROR"
        records[4]["status"] = "timeout"
        failures = parse_results.evaluate(
            records,
            {"max_timeout_snapshots": 0, "max_media_error_events": 0},
        )
        self.assertTrue(any("timeout snapshots" in failure for failure in failures))
        self.assertTrue(any("media error events" in failure for failure in failures))

    def test_max_event_counts_rejects_a_seek_storm_including_stale_events(self) -> None:
        records = [
            snapshot(1, "load", generation=1, timeline=1, time=10.0),
            event(2, "SEEKING", generation=1, timeline=2, time=10.0),
            event(3, "SEEKING", generation=1, timeline=3, time=10.0),
            event(4, "SEEKED", generation=1, timeline=3, time=83.25),
            result(5, generation=1, timeline=3, time=83.25),
        ]
        records[1]["generation_current"] = False
        failures = parse_results.evaluate(
            records,
            {"max_event_counts": {"SEEKING": 1, "SEEKED": 1}},
        )
        self.assertEqual(failures, ["event SEEKING count 2 exceeds 1"])

    def test_max_event_counts_schema_is_strict(self) -> None:
        for invalid in ({"SEEKED": -1}, {"SEEKED": True}, {"": 0}, []):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(parse_results.ParseError, "max_event_counts"):
                    parse_results.validate_oracle({"max_event_counts": invalid})

    def test_audio_checkpoint_catches_brief_then_silent_delivery(self) -> None:
        records = passing_records()
        checkpoint = records[4]
        checkpoint["label"] = "live-steady"
        checkpoint["audio_monitor_enabled"] = True
        checkpoint["audio_samples_total"] = 4
        checkpoint["audio_bytes_total"] = 4096
        checkpoint["audio_samples_generation"] = 4
        checkpoint["audio_bytes_generation"] = 4096
        checkpoint["audio_last_monotonic_ms"] = 10
        checkpoint["audio_payload_observed"] = True
        checkpoint["audio_payload_format"] = "pcm16"
        checkpoint["audio_channels"] = 2
        checkpoint["audio_rate"] = 48000
        checkpoint["audio_bits"] = 16
        checkpoint["audio_payload_bytes_scanned_total"] = 4096
        checkpoint["audio_nonzero_bytes_total"] = 100
        checkpoint["audio_nonzero_units_total"] = 50
        checkpoint["audio_nonzero_units_generation"] = 50
        checkpoint["audio_peak_abs"] = 1000.0
        checkpoint["audio_last_nonzero_monotonic_ms"] = 10
        checkpoint["monotonic_ms"] = 5000
        records[-1]["audio_monitor_enabled"] = True
        records[-1]["audio_samples_total"] = 4
        records[-1]["audio_bytes_total"] = 4096
        records[-1]["audio_samples_generation"] = 4
        records[-1]["audio_bytes_generation"] = 4096
        records[-1]["audio_last_monotonic_ms"] = 10
        for field in (
            "audio_payload_observed", "audio_payload_format", "audio_channels",
            "audio_rate", "audio_bits", "audio_payload_bytes_scanned_total",
            "audio_nonzero_bytes_total", "audio_nonzero_units_total",
            "audio_nonzero_units_generation", "audio_peak_abs",
            "audio_last_nonzero_monotonic_ms",
        ):
            records[-1][field] = checkpoint[field]
        records[-1]["monotonic_ms"] = 5010
        failures = parse_results.evaluate(
            records,
            {
                "audio_check_labels": ["live-steady"],
                "min_audio_samples_generation": 1,
                "min_audio_bytes_generation": 1,
                "max_audio_silence_ms": 1000,
            },
        )
        self.assertTrue(any("no delivered sample" in failure for failure in failures))

    def test_audio_checkpoint_accepts_recent_delivery(self) -> None:
        records = passing_records()
        checkpoint = records[4]
        checkpoint["label"] = "live-steady"
        checkpoint["audio_monitor_enabled"] = True
        checkpoint["audio_samples_total"] = 200
        checkpoint["audio_bytes_total"] = 819200
        checkpoint["audio_samples_generation"] = 200
        checkpoint["audio_bytes_generation"] = 819200
        checkpoint["audio_last_monotonic_ms"] = checkpoint["monotonic_ms"] - 5
        checkpoint["audio_payload_observed"] = True
        checkpoint["audio_payload_format"] = "float32"
        checkpoint["audio_channels"] = 2
        checkpoint["audio_rate"] = 48000
        checkpoint["audio_bits"] = 32
        checkpoint["audio_payload_bytes_scanned_total"] = 819200
        checkpoint["audio_nonzero_bytes_total"] = 400000
        checkpoint["audio_nonzero_units_total"] = 200000
        checkpoint["audio_nonzero_units_generation"] = 200000
        checkpoint["audio_peak_abs"] = 0.5
        checkpoint["audio_last_nonzero_monotonic_ms"] = checkpoint["monotonic_ms"] - 5
        records[-1]["audio_monitor_enabled"] = True
        records[-1]["audio_samples_total"] = 200
        records[-1]["audio_bytes_total"] = 819200
        records[-1]["audio_samples_generation"] = 200
        records[-1]["audio_bytes_generation"] = 819200
        records[-1]["audio_last_monotonic_ms"] = checkpoint["audio_last_monotonic_ms"]
        for field in (
            "audio_payload_observed", "audio_payload_format", "audio_channels",
            "audio_rate", "audio_bits", "audio_payload_bytes_scanned_total",
            "audio_nonzero_bytes_total", "audio_nonzero_units_total",
            "audio_nonzero_units_generation", "audio_peak_abs",
            "audio_last_nonzero_monotonic_ms",
        ):
            records[-1][field] = checkpoint[field]
        failures = parse_results.evaluate(
            records,
            {
                "audio_check_labels": ["live-steady"],
                "min_audio_samples_generation": 100,
                "min_audio_bytes_generation": 4096,
                "max_audio_silence_ms": 100,
            },
        )
        self.assertEqual(failures, [])

    def test_late_audio_burst_does_not_hide_earlier_checkpoint_dropout(self) -> None:
        records = passing_records()
        stale = records[4]
        stale["label"] = "live-steady"
        stale["monotonic_ms"] = 4000
        recovered = snapshot(6, "snapshot", time=3.0)
        recovered["label"] = "live-steady"
        recovered["monotonic_ms"] = 8000
        records.insert(-1, recovered)
        records[-1]["seq"] = 7
        records[-1]["monotonic_ms"] = 8010

        def set_audio(record: dict, total: int, last: int) -> None:
            record["audio_monitor_enabled"] = True
            record["audio_samples_total"] = total
            record["audio_bytes_total"] = total * 4096
            record["audio_samples_generation"] = total
            record["audio_bytes_generation"] = total * 4096
            record["audio_last_monotonic_ms"] = last
            record["audio_payload_observed"] = True
            record["audio_payload_format"] = "pcm16"
            record["audio_channels"] = 2
            record["audio_rate"] = 48000
            record["audio_bits"] = 16
            record["audio_payload_bytes_scanned_total"] = total * 4096
            record["audio_nonzero_bytes_total"] = total * 100
            record["audio_nonzero_units_total"] = total * 100
            record["audio_nonzero_units_generation"] = total * 100
            record["audio_peak_abs"] = 1000.0
            record["audio_last_nonzero_monotonic_ms"] = last

        set_audio(stale, 100, 1000)
        set_audio(recovered, 200, 7990)
        set_audio(records[-1], 200, 7990)
        parse_results.validate_sequence(records)
        failures = parse_results.evaluate(
            records,
            {
                "audio_check_labels": ["live-steady"],
                "min_audio_samples_generation": 20,
                "min_audio_nonzero_units_generation": 20,
                "max_audio_silence_ms": 500,
                "max_audio_nonzero_silence_ms": 500,
            },
        )
        self.assertTrue(any("no delivered sample for 3000 ms" in failure for failure in failures))
        self.assertTrue(any("no nonzero audio unit for 3000 ms" in failure for failure in failures))

    def test_audio_checkpoint_rejects_continuous_all_zero_pcm(self) -> None:
        records = passing_records()
        checkpoint = records[4]
        checkpoint["label"] = "live-steady"
        for record in (checkpoint, records[-1]):
            record["audio_monitor_enabled"] = True
            record["audio_samples_total"] = 200
            record["audio_bytes_total"] = 819200
            record["audio_samples_generation"] = 200
            record["audio_bytes_generation"] = 819200
            record["audio_last_monotonic_ms"] = checkpoint["monotonic_ms"]
            record["audio_payload_observed"] = True
            record["audio_payload_format"] = "pcm16"
            record["audio_channels"] = 2
            record["audio_rate"] = 48000
            record["audio_bits"] = 16
            record["audio_payload_bytes_scanned_total"] = 819200
            record["audio_nonzero_bytes_total"] = 0
            record["audio_nonzero_units_total"] = 0
            record["audio_nonzero_units_generation"] = 0
            record["audio_peak_abs"] = 0.0
            record["audio_last_nonzero_monotonic_ms"] = None
        failures = parse_results.evaluate(
            records,
            {
                "audio_check_labels": ["live-steady"],
                "min_audio_nonzero_units_generation": 1,
                "max_audio_nonzero_silence_ms": 100,
                "required_audio_payload_formats": ["pcm16"],
            },
        )
        self.assertTrue(any("nonzero units below" in failure for failure in failures))
        self.assertTrue(any("no nonzero audio unit" in failure for failure in failures))

    def test_generation_specific_event_requirement(self) -> None:
        records = passing_records()
        records.insert(-1, snapshot(6, "replace", generation=2, timeline=2))
        records[-1]["seq"] = 7
        records[-1]["source_generation"] = 2
        records[-1]["timeline_generation"] = 2
        failures = parse_results.evaluate(records, {"required_events_per_generation": ["CANPLAY"]})
        self.assertTrue(any("generation 2" in failure for failure in failures))

    def test_late_old_generation_event_is_preserved_but_not_current(self) -> None:
        records = passing_records()
        records.insert(-1, snapshot(6, "replace", generation=2, timeline=2))
        late = event(7, "ENDED", generation=1, timeline=1)
        late["generation_current"] = False
        records.insert(-1, late)
        records[-1]["seq"] = 8
        records[-1]["monotonic_ms"] = 80
        records[-1]["source_generation"] = 2
        records[-1]["timeline_generation"] = 2
        parse_results.validate_sequence(records)
        failures = parse_results.evaluate(records, {"max_stale_events": 0})
        self.assertTrue(any("stale-generation" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
