# SPDX-License-Identifier: BSD-3-Clause
"""Fail-closed pure tests for private endpoint-audio evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import endpoint_audio_oracle as oracle


CAPTURE_START_NS = 1_000_000_000
CAPTURE_SECONDS = 12
CAPTURE_FINISH_NS = CAPTURE_START_NS + CAPTURE_SECONDS * 1_000_000_000
CAPTURE_ANCHOR_FRAME = oracle.SAMPLE_RATE // 10
CAPTURE_ANCHOR_NS = CAPTURE_START_NS + 100 * 1_000_000
EXPECTATIONS = {"live-g1": 1, "live-g2": 1, "live-g3": 1}


def driver_records(checkpoint_seconds: tuple[float, ...] = (2.0, 5.0, 8.0),
                   *, monitor: bool = False) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    labels = tuple(EXPECTATIONS)
    origin_ms = CAPTURE_START_NS // 1_000_000
    for index, (label, seconds) in enumerate(zip(labels, checkpoint_seconds), 1):
        records.append({
            "action": "snapshot",
            "audio_monitor_enabled": monitor,
            "ended": False,
            "has_audio": True,
            "label": label,
            "monotonic_ms": round(seconds * 1000),
            "monotonic_origin_ms": origin_ms,
            "paused": False,
            "seq": index,
            "source_generation": index,
            "status": "ok",
            "type": "snapshot",
        })
    records.append({
        "exit_code": 0,
        "monotonic_ms": 10_000,
        "monotonic_origin_ms": origin_ms,
        "seq": len(records) + 1,
        "status": "pass",
        "type": "result",
    })
    return records


def write_driver(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def write_pcm(path: Path, active_ranges: tuple[tuple[float, float], ...],
              *, seconds: int = CAPTURE_SECONDS, amplitude: int = 2000) -> None:
    frames = seconds * oracle.SAMPLE_RATE
    payload = bytearray(frames * oracle.FRAME_BYTES)
    frame = struct.pack("<hh", amplitude, -amplitude)
    for first_seconds, last_seconds in active_ranges:
        first = round(first_seconds * oracle.SAMPLE_RATE)
        last = round(last_seconds * oracle.SAMPLE_RATE)
        payload[first * oracle.FRAME_BYTES:last * oracle.FRAME_BYTES] = frame * (last - first)
    path.write_bytes(payload)


class EndpointAudioOracleTests(unittest.TestCase):
    def run_score(self, root: Path, *, records: list[dict[str, object]] | None = None,
                  active_ranges: tuple[tuple[float, float], ...] = ((0.0, 12.0),),
                  seconds: int = CAPTURE_SECONDS, amplitude: int = 2000,
                  finished_ns: int = CAPTURE_FINISH_NS,
                  started_ns: int = CAPTURE_START_NS,
                  anchor_frame: int = CAPTURE_ANCHOR_FRAME,
                  anchor_ns: int = CAPTURE_ANCHOR_NS,
                  expectations: dict[str, int] | None = None) -> dict[str, object]:
        driver = root / "driver.jsonl"
        pcm = root / "endpoint-audio.raw"
        write_driver(driver, records if records is not None else driver_records())
        write_pcm(pcm, active_ranges, seconds=seconds, amplitude=amplitude)
        return oracle.score(
            driver,
            pcm,
            started_ns,
            anchor_frame,
            anchor_ns,
            finished_ns,
            expectations if expectations is not None else EXPECTATIONS,
        )

    def test_continuous_endpoint_signal_passes_every_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_score(Path(directory))
        self.assertEqual(value["status"], "passed")
        self.assertEqual(value["checkpointCount"], 3)
        self.assertEqual(value["windowBeforeMs"], 800)
        self.assertEqual(value["windowAfterMs"], 0)
        self.assertEqual(value["minimumSignalFramesPerMille"], 900)
        self.assertEqual(value["sourceGenerations"], [1, 2, 3])
        self.assertEqual(value["firstCheckpointBucketCount"], 4)
        self.assertEqual(value["maximumBucketMs"], 200)
        self.assertEqual(
            [window["activeBucketCount"] for window in value["windows"]],
            [4, 4, 4],
        )
        self.assertNotIn("/" + "home/", oracle.canonical_bytes(value).decode("ascii"))

    def test_non_frame_aligned_capture_origin_keeps_four_lookback_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_score(
                Path(directory),
                started_ns=CAPTURE_START_NS + 1,
                anchor_ns=CAPTURE_ANCHOR_NS + 1,
                finished_ns=CAPTURE_FINISH_NS + 1,
            )
        self.assertEqual(
            [window["activeBucketCount"] for window in value["windows"]],
            [4, 4, 4],
        )
        self.assertEqual(
            [window["frames"] for window in value["windows"]],
            [38_400, 38_400, 38_400],
        )

    def test_initial_blip_then_silence_fails_later_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "live-g2.*no endpoint signal"):
                self.run_score(Path(directory), active_ranges=((0.0, 2.5),))

    def test_silence_in_one_source_generation_fails_that_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "live-g2"):
                self.run_score(
                    Path(directory),
                    active_ranges=((0.0, 4.0), (6.0, 12.0)),
                )

    def test_audio_outside_checkpoint_windows_cannot_satisfy_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "live-g1"):
                self.run_score(Path(directory), active_ranges=((10.0, 11.0),))

    def test_sparse_periodic_pulses_do_not_count_as_continuity(self) -> None:
        pulses = tuple(
            (offset / 1000, (offset + 2) / 1000)
            for offset in range(0, CAPTURE_SECONDS * 1000, 200)
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "mostly silent"):
                self.run_score(Path(directory), active_ranges=pulses, amplitude=32_000)

    def test_same_generation_windows_bridge_checkpoint_gaps(self) -> None:
        records = driver_records((2.0, 3.0, 4.0))
        for record in records[:-1]:
            record["label"] = "live-steady"
            record["source_generation"] = 1
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_score(
                Path(directory),
                records=records,
                expectations={"live-steady": 3},
            )
        self.assertEqual(
            [window["bridgesPreviousCheckpoint"] for window in value["windows"]],
            [False, True, True],
        )
        self.assertEqual(
            [window["activeBucketCount"] for window in value["windows"]],
            [4, 5, 5],
        )
        # Each old 800 ms lookback is active, but every 200 ms gap immediately
        # after a checkpoint is silent. Bridging must expose the first gap.
        active = ((1.2, 2.0), (2.2, 3.0), (3.2, 4.0))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "live-steady.*bucket 1"):
                self.run_score(
                    Path(directory),
                    records=records,
                    active_ranges=active,
                    expectations={"live-steady": 3},
                )

    def test_positive_post_anchor_gap_is_mapped_before_final_checkpoint(self) -> None:
        # A 150 ms post-readiness gap places the third checkpoint at PCM frame
        # time 7.85 s. Audio after that point represents immediate shutdown
        # silence and must not be pulled into the preceding-only window.
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_score(
                Path(directory),
                active_ranges=((0.0, 7.85),),
                finished_ns=CAPTURE_FINISH_NS + 150 * 1_000_000,
            )
        self.assertEqual(value["capturePostAnchorClockGapNs"], 150 * 1_000_000)
        self.assertEqual(
            value["sampleClockAnchorMonotonicNs"],
            CAPTURE_ANCHOR_NS + 150 * 1_000_000,
        )

    def test_recorder_startup_latency_is_excluded_after_readiness_anchor(self) -> None:
        records = driver_records((4.0, 7.0, 10.0))
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_score(
                Path(directory),
                records=records,
                anchor_ns=CAPTURE_START_NS + 2_100 * 1_000_000,
                finished_ns=CAPTURE_START_NS + 14 * 1_000_000_000,
            )
        self.assertEqual(value["captureStartupNs"], 2_100 * 1_000_000)
        self.assertEqual(value["capturePostAnchorClockGapNs"], 0)
        self.assertEqual(value["sampleClockAnchorMonotonicNs"],
                         CAPTURE_START_NS + 2_100 * 1_000_000)

    def test_positive_and_negative_post_anchor_errors_are_strictly_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            negative = self.run_score(
                Path(directory),
                active_ranges=((0.0, 8.0),),
                finished_ns=CAPTURE_FINISH_NS - 150 * 1_000_000,
            )
        self.assertEqual(negative["capturePostAnchorClockGapNs"], -150 * 1_000_000)
        self.assertEqual(negative["sampleClockAnchorMonotonicNs"], CAPTURE_ANCHOR_NS)
        self.assertEqual(
            negative["windows"][-1]["lastFrameExclusive"],
            8 * oracle.SAMPLE_RATE,
        )

        for delta_ns in (-151 * 1_000_000, 151 * 1_000_000):
            with self.subTest(delta_ns=delta_ns), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    oracle.EndpointAudioError,
                    r"post-anchor duration does not calibrate.*gap_ns=",
                ):
                    self.run_score(
                        Path(directory),
                        finished_ns=CAPTURE_FINISH_NS + delta_ns,
                    )

    def test_invalid_partial_and_late_anchors_fail_closed(self) -> None:
        for anchor_frame, anchor_ns, message in (
            (0, CAPTURE_ANCHOR_NS, "anchor frame"),
            (CAPTURE_SECONDS * oracle.SAMPLE_RATE, CAPTURE_ANCHOR_NS, "anchor frame"),
            (CAPTURE_ANCHOR_FRAME, CAPTURE_START_NS, "monotonic interval"),
            (CAPTURE_ANCHOR_FRAME, CAPTURE_FINISH_NS, "monotonic interval"),
            (
                CAPTURE_ANCHOR_FRAME,
                CAPTURE_START_NS + oracle.MAX_CAPTURE_STARTUP_NS + 1,
                "startup limit",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(oracle.EndpointAudioError, message):
                    self.run_score(
                        Path(directory),
                        anchor_frame=anchor_frame,
                        anchor_ns=anchor_ns,
                    )

        with tempfile.TemporaryDirectory() as directory:
            pcm = Path(directory) / "endpoint-audio.raw"
            pcm.write_bytes(b"\x00")
            with self.assertRaisesRegex(oracle.EndpointAudioError, "partial stereo frame"):
                oracle.observe_capture_anchor(pcm)

    def test_anchor_observation_timestamps_only_existing_complete_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pcm = Path(directory) / "endpoint-audio.raw"
            pcm.write_bytes(b"\x01\x00\xff\xff" * 25)
            with mock.patch.object(
                oracle.time,
                "clock_gettime_ns",
                return_value=9_876_543_210,
            ) as clock:
                frame, observed_ns = oracle.observe_capture_anchor(pcm)
        self.assertEqual(frame, 25)
        self.assertEqual(observed_ns, 9_876_543_210)
        clock.assert_called_once_with(oracle.time.CLOCK_MONOTONIC_RAW)

    def test_checkpoint_lookback_must_be_after_readiness_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                oracle.EndpointAudioError,
                "outside the complete post-readiness endpoint capture",
            ):
                self.run_score(
                    Path(directory),
                    anchor_frame=oracle.SAMPLE_RATE * 3 // 2,
                    anchor_ns=CAPTURE_START_NS + 1_500 * 1_000_000,
                )

    def test_all_zero_and_low_level_wrong_payload_fail_closed(self) -> None:
        for amplitude in (0, 1):
            with self.subTest(amplitude=amplitude), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(oracle.EndpointAudioError):
                    self.run_score(Path(directory), amplitude=amplitude)

    def test_partial_frame_too_short_and_bad_clock_calibration_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            pcm = root / "endpoint-audio.raw"
            write_driver(driver, driver_records())
            pcm.write_bytes(b"\x00")
            with self.assertRaisesRegex(oracle.EndpointAudioError, "partial stereo frame"):
                oracle.score(
                    driver,
                    pcm,
                    CAPTURE_START_NS,
                    CAPTURE_ANCHOR_FRAME,
                    CAPTURE_ANCHOR_NS,
                    CAPTURE_FINISH_NS,
                    EXPECTATIONS,
                )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "does not calibrate"):
                self.run_score(Path(directory), seconds=1, active_ranges=((0.0, 1.0),))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(oracle.EndpointAudioError, "does not calibrate"):
                self.run_score(
                    Path(directory),
                    finished_ns=CAPTURE_FINISH_NS + 151 * 1_000_000,
                )

        with tempfile.TemporaryDirectory() as directory:
            records = driver_records((0.2, 5.0, 8.0))
            with self.assertRaisesRegex(
                oracle.EndpointAudioError,
                "outside the complete post-readiness endpoint capture",
            ):
                self.run_score(Path(directory), records=records)

    def test_driver_must_be_complete_contiguous_and_uninstrumented(self) -> None:
        variants = []
        monitor = driver_records(monitor=True)
        variants.append((monitor, "in-driver audio monitor"))
        missing_result = driver_records()[:-1]
        variants.append((missing_result, "passing result"))
        broken_sequence = copy.deepcopy(driver_records())
        broken_sequence[1]["seq"] = 99
        variants.append((broken_sequence, "sequence is not contiguous"))
        for records, message in variants:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(oracle.EndpointAudioError, message):
                    self.run_score(Path(directory), records=records)

    def test_checkpoint_expectations_are_exact_and_bounded(self) -> None:
        self.assertEqual(
            oracle.parse_expectations(["live-g1=4", "live-g2=4"]),
            {"live-g1": 4, "live-g2": 4},
        )
        for values in (
            [],
            ["live-g1"],
            ["../bad=1"],
            ["live-g1=0"],
            ["live-g1=65"],
            ["live-g1=1", "live-g1=1"],
            [f"label-{index}=1" for index in range(oracle.MAX_EXPECTED_LABELS + 1)],
        ):
            with self.subTest(values=values), self.assertRaises(oracle.EndpointAudioError):
                oracle.parse_expectations(values)
        with tempfile.TemporaryDirectory() as directory:
            records = driver_records()
            del records[1]
            for sequence, record in enumerate(records, 1):
                record["seq"] = sequence
            with self.assertRaisesRegex(oracle.EndpointAudioError, "checkpoint counts differ"):
                self.run_score(Path(directory), records=records)

    def test_oversized_pcm_and_driver_inputs_are_rejected_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            pcm = root / "endpoint-audio.raw"
            write_driver(driver, driver_records())
            with pcm.open("wb") as stream:
                stream.truncate(oracle.MAX_PCM_BYTES + oracle.FRAME_BYTES)
            with self.assertRaisesRegex(oracle.EndpointAudioError, "PCM size"):
                oracle.score(
                    driver,
                    pcm,
                    CAPTURE_START_NS,
                    CAPTURE_ANCHOR_FRAME,
                    CAPTURE_ANCHOR_NS,
                    CAPTURE_FINISH_NS,
                    EXPECTATIONS,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            pcm = root / "endpoint-audio.raw"
            with driver.open("wb") as stream:
                stream.truncate(oracle.MAX_DRIVER_BYTES + 1)
            write_pcm(pcm, ((0.0, 12.0),))
            with self.assertRaisesRegex(oracle.EndpointAudioError, "driver evidence size"):
                oracle.score(
                    driver,
                    pcm,
                    CAPTURE_START_NS,
                    CAPTURE_ANCHOR_FRAME,
                    CAPTURE_ANCHOR_NS,
                    CAPTURE_FINISH_NS,
                    EXPECTATIONS,
                )

    def test_cli_writes_canonical_summary_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            pcm = root / "endpoint-audio.raw"
            output = root / "summary.json"
            write_driver(driver, driver_records())
            write_pcm(pcm, ((0.0, 12.0),))
            arguments = [
                "score",
                "--driver-json", str(driver),
                "--pcm", str(pcm),
                "--capture-started-ns", str(CAPTURE_START_NS),
                "--capture-anchor-frame", str(CAPTURE_ANCHOR_FRAME),
                "--capture-anchor-ns", str(CAPTURE_ANCHOR_NS),
                "--capture-finished-ns", str(CAPTURE_FINISH_NS),
                "--checkpoint", "live-g1=1",
                "--checkpoint", "live-g2=1",
                "--checkpoint", "live-g3=1",
                "--output", str(output),
            ]
            self.assertEqual(oracle.main(arguments), 0)
            payload = output.read_bytes()
            self.assertEqual(payload, oracle.canonical_bytes(json.loads(payload)))
            self.assertEqual(oracle.main(arguments), 2)


if __name__ == "__main__":
    unittest.main()
