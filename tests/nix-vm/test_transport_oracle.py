# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import transport_oracle
from test_stress_fixture_service import config as fixture_config


def http_record(sequence: int, *, start: int, finish: int, byte_range: str | None) -> dict:
    return {
        "bytes_expected": 1000 if sequence == 1 else 500,
        "bytes_sent": 500,
        "case_id": "vod-seek",
        "clock_basis": "linux-clock-monotonic-raw-v1",
        "disconnected": sequence == 1,
        "finished_at": "2026-01-01T00:00:01.000Z",
        "finished_monotonic_ns": finish,
        "method": "GET",
        "range": byte_range,
        "request_seq": sequence,
        "started_at": "2026-01-01T00:00:00.000Z",
        "started_monotonic_ns": start,
        "status": 206,
    }


class TransportOracleTests(unittest.TestCase):
    def seal_case(self, args: Namespace) -> None:
        payload = args.http_log.read_bytes()
        count = len(payload.splitlines())
        marker = {
            "allocatedRequestCount": count,
            "caseId": "vod-seek",
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "logBytes": len(payload),
            "logSha256": hashlib.sha256(payload).hexdigest(),
            "rejectedConnectionCount": 0,
            "schema": 1,
            "status": "complete",
            "terminalRecordCount": count,
        }
        args.service_completion.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )

    def test_exact_installed_filenames_support_import(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "bin"
            installed.mkdir()
            shutil.copy2(source_root / "transport_oracle.py", installed / "transport-oracle")
            shutil.copy2(
                source_root / "stress_fixture_service.py",
                installed / "stress_fixture_service.py",
            )
            completed = subprocess.run(
                [sys.executable, str(installed / "transport-oracle"), "--help"],
                cwd="/",
                env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(installed)},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def write_case(self, directory: Path, *, role: str, first_finish: int) -> Namespace:
        configuration = fixture_config()
        configuration["transportRole"] = role
        configuration["minTransferTailMs"] = 10_000 if role == "streaming-first-qualification" else 0
        configuration["minPostSeekRangeResponses"] = (
            1 if role == "streaming-first-qualification" else 0
        )
        config_path = directory / "config.json"
        config_path.write_text(json.dumps(configuration))
        watch_path = directory / "watch.json"
        watch_path.write_text(json.dumps({
            "clockBasis": "linux-clock-monotonic-raw-v1",
            "firstPlaybackMonotonicNs": 2_000_000_000,
            "firstPlaybackObservedMonotonicNs": 2_010_000_000,
            "maxObservationLagMs": 10,
            "schema": 1,
            "seekMonotonicNs": [2_500_000_000],
        }))
        http_path = directory / "http.jsonl"
        # Finish order is deliberately opposite request order; concurrent
        # request logging must be normalized by request_seq before scoring.
        records = [
            http_record(2, start=3_000_000_000, finish=4_000_000_000, byte_range="bytes=500-999"),
            http_record(1, start=1_000_000_000, finish=first_finish, byte_range="bytes=0-999"),
        ]
        http_path.write_text("".join(json.dumps(record) + "\n" for record in records))
        args = Namespace(
            config=config_path,
            watch=watch_path,
            http_log=http_path,
            output=directory / "transport.json",
            service_completion=directory / "http.complete.json",
        )
        self.seal_case(args)
        return args

    def write_failed_range_case(
        self, directory: Path
    ) -> tuple[Namespace, list[dict]]:
        args = self.write_case(
            directory,
            role="streaming-first-qualification",
            first_finish=13_000_000_000,
        )
        configuration = json.loads(args.config.read_text())
        configuration.update({
            "mode": "fail-post-open-range",
            "failCount": 1,
            "failStatus": 503,
            "maxErrorResponses": 1,
            "minPostSeekRangeResponses": 0,
        })
        args.config.write_text(json.dumps(configuration))
        records = [
            http_record(
                1,
                start=1_000_000_000,
                finish=13_000_000_000,
                byte_range="bytes=0-999",
            ),
            http_record(
                2,
                start=3_000_000_000,
                finish=3_010_000_000,
                byte_range="bytes=500-999",
            ),
        ]
        records[1].update({
            "bytes_expected": 0,
            "bytes_sent": 0,
            "disconnected": False,
            "status": 503,
        })
        args.http_log.write_text(
            "".join(json.dumps(item) + "\n" for item in records)
        )
        self.seal_case(args)
        return args, records

    def write_failed_range_recovery_case(
        self, directory: Path
    ) -> tuple[Namespace, list[dict]]:
        args, records = self.write_failed_range_case(directory)
        configuration = json.loads(args.config.read_text())
        configuration["mode"] = "fail-post-open-range-recovery"
        args.config.write_text(json.dumps(configuration))
        recovery = http_record(
            3,
            start=3_020_000_000,
            finish=5_000_000_000,
            byte_range="bytes=0-999",
        )
        recovery.update({
            "bytes_expected": 1000,
            "bytes_sent": 500,
            "disconnected": True,
        })
        records.append(recovery)
        args.http_log.write_text(
            "".join(json.dumps(item) + "\n" for item in records)
        )
        self.seal_case(args)
        return args, records

    def test_qualification_requires_active_transfer_tail_and_post_seek_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertTrue(result["progressiveScored"])
            self.assertEqual(result["transferTailAfterPlaybackMs"], 11_000)
            self.assertEqual(result["postSeekRangeResponseCount"], 1)
            self.assertEqual(result["minExpectedPostSeekRangeResponses"], 1)
            self.assertEqual(result["minExpectedErrorResponses"], 0)
            self.assertEqual(result["maxAllowedErrorResponses"], 0)
            self.assertEqual(result["initialRequestSequence"], 1)
            self.assertEqual(result["initialRequestRange"], "bytes=0-999")

    def test_qualification_rejects_download_before_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=1_500_000_000)
            with self.assertRaises(transport_oracle.TransportError):
                transport_oracle.command_score(args)

    def test_later_concurrent_range_cannot_mask_completed_initial_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=1_500_000_000)
            records = [
                http_record(1, start=1_000_000_000, finish=1_500_000_000,
                            byte_range="bytes=0-999"),
                http_record(2, start=1_750_000_000, finish=20_000_000_000,
                            byte_range="bytes=500-999"),
                http_record(3, start=3_000_000_000, finish=4_000_000_000,
                            byte_range="bytes=500-999"),
            ]
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "first successful"):
                transport_oracle.command_score(args)

    def test_pre_seek_or_zero_offset_range_cannot_qualify_seek(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            records = [
                http_record(1, start=1_000_000_000, finish=13_000_000_000,
                            byte_range="bytes=0-999"),
                http_record(2, start=2_475_000_000, finish=4_000_000_000,
                            byte_range="bytes=500-999"),
            ]
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "post-seek"):
                transport_oracle.command_score(args)

            records[1] = http_record(2, start=3_000_000_000, finish=4_000_000_000,
                                     byte_range="bytes=0-499")
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "post-seek"):
                transport_oracle.command_score(args)

    def test_rejects_inconsistent_http_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args = self.write_case(directory, role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            records = [json.loads(line) for line in args.http_log.read_text().splitlines()]
            first = next(record for record in records if record["request_seq"] == 1)
            first["bytes_sent"] = first["bytes_expected"] + 1
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "declared bytes"):
                transport_oracle.command_score(args)

            first["bytes_sent"] = first["bytes_expected"] - 1
            first["disconnected"] = False
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "completed HTTP body"):
                transport_oracle.command_score(args)

            first["bytes_sent"] = first["bytes_expected"]
            first["disconnected"] = True
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "not partial"):
                transport_oracle.command_score(args)

    def test_rejects_noncanonical_wall_clock_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            records = [json.loads(line) for line in args.http_log.read_text().splitlines()]
            records[0]["started_at"] = "not-a-time"
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "canonical UTC"):
                transport_oracle.command_score(args)

    def test_diagnostic_is_explicitly_not_progressive_scored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertFalse(result["progressiveScored"])
            self.assertIsNone(result["transferTailAfterPlaybackMs"])

    def test_diagnostic_allows_no_post_seek_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            records = [http_record(1, start=1_000_000_000, finish=1_500_000_000,
                                   byte_range="bytes=0-999")]
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertEqual(result["postSeekRangeResponseCount"], 0)

    def test_no_range_qualification_proves_linear_progress_without_206(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            configuration = json.loads(args.config.read_text())
            configuration["mode"] = "no-range"
            configuration["minPostSeekRangeResponses"] = 0
            args.config.write_text(json.dumps(configuration))
            watch = json.loads(args.watch.read_text())
            watch["seekMonotonicNs"] = []
            args.watch.write_text(json.dumps(watch))
            record = http_record(1, start=1_000_000_000, finish=13_000_000_000,
                                 byte_range="bytes=0-999")
            record["status"] = 200
            args.http_log.write_text(json.dumps(record) + "\n")
            self.seal_case(args)
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertTrue(result["progressiveScored"])
            self.assertEqual(result["rangeResponseCount"], 0)
            self.assertEqual(result["postSeekRangeResponseCount"], 0)

            duplicate = dict(record)
            duplicate.update({
                "request_seq": 2,
                "started_monotonic_ns": 3_000_000_000,
                "finished_monotonic_ns": 4_000_000_000,
            })
            args.http_log.write_text(json.dumps(record) + "\n" + json.dumps(duplicate) + "\n")
            self.seal_case(args)
            with self.assertRaisesRegex(transport_oracle.TransportError, "duplicate full-body"):
                transport_oracle.command_score(args)

    def test_fixed_redirect_is_not_an_error_and_target_is_scored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            configuration = json.loads(args.config.read_text())
            configuration["mode"] = "redirect"
            args.config.write_text(json.dumps(configuration))
            redirect = http_record(1, start=900_000_000, finish=910_000_000,
                                   byte_range="bytes=0-999")
            redirect.update({
                "bytes_expected": 0,
                "bytes_sent": 0,
                "disconnected": False,
                "status": 307,
            })
            initial = http_record(2, start=1_000_000_000, finish=13_000_000_000,
                                  byte_range="bytes=0-999")
            initial.update({"bytes_expected": 1000, "bytes_sent": 500, "disconnected": True})
            seek = http_record(3, start=3_000_000_000, finish=4_000_000_000,
                               byte_range="bytes=500-999")
            args.http_log.write_text(
                "".join(json.dumps(item) + "\n" for item in (redirect, initial, seek))
            )
            self.seal_case(args)
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertEqual(result["errorResponseCount"], 0)
            self.assertEqual(result["initialRequestSequence"], 2)
            self.assertEqual(result["postSeekRangeResponseCount"], 1)

    def test_post_open_failure_requires_one_bounded_http_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, _records = self.write_failed_range_case(Path(temporary))
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertEqual(result["errorResponseCount"], 1)
            self.assertEqual(result["minExpectedErrorResponses"], 1)
            self.assertEqual(result["maxAllowedErrorResponses"], 1)

    def test_failed_range_mutations_are_rejected(self) -> None:
        def later_request(records: list[dict], _args: Namespace) -> None:
            retry = http_record(
                3,
                start=3_020_000_000,
                finish=3_030_000_000,
                byte_range=None,
            )
            retry.update(status=200, bytes_expected=500, bytes_sent=500)
            records.append(retry)

        def successful_retry_before_failure(
            records: list[dict], _args: Namespace
        ) -> None:
            records[1]["request_seq"] = 3
            retry = http_record(
                2,
                start=2_750_000_000,
                finish=2_900_000_000,
                byte_range="bytes=500-999",
            )
            records.insert(1, retry)

        def no_seek(_records: list[dict], args: Namespace) -> None:
            watch = json.loads(args.watch.read_text())
            watch["seekMonotonicNs"] = []
            args.watch.write_text(json.dumps(watch))

        mutations = {
            "wrong-status": lambda records, _args: records[1].__setitem__("status", 502),
            "zero-range": lambda records, _args: records[1].__setitem__(
                "range", "bytes=0-499"
            ),
            "reversed-range": lambda records, _args: records[1].__setitem__(
                "range", "bytes=900-500"
            ),
            "invalid-range": lambda records, _args: records[1].__setitem__(
                "range", "invalid"
            ),
            "pre-seek-failure": lambda records, _args: records[1].update({
                "started_monotonic_ns": 2_400_000_000,
            }),
            "later-media-request": later_request,
            "successful-retry-before-failure": successful_retry_before_failure,
            "missing-seek-fence": no_seek,
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as temporary:
                args, records = self.write_failed_range_case(Path(temporary))
                mutate(records, args)
                args.http_log.write_text(
                    "".join(json.dumps(item) + "\n" for item in records)
                )
                self.seal_case(args)
                with self.assertRaises(transport_oracle.TransportError):
                    transport_oracle.command_score(args)

    def test_failed_range_recovery_requires_a_later_nonoverlapping_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, _records = self.write_failed_range_recovery_case(Path(temporary))
            transport_oracle.command_score(args)
            result = json.loads(args.output.read_text())
            self.assertEqual(1, result["errorResponseCount"])
            self.assertEqual(2, result["bodyResponseCount"])

        mutations = {
            "missing-recovery": lambda records: records.pop(),
            "overlapping-recovery": lambda records: records[2].update({
                "started_monotonic_ns": 3_005_000_000,
            }),
            "recovery-before-failure": lambda records: (
                records[1].__setitem__("request_seq", 3),
                records[2].__setitem__("request_seq", 2),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as temporary:
                args, records = self.write_failed_range_recovery_case(Path(temporary))
                mutate(records)
                args.http_log.write_text(
                    "".join(json.dumps(item) + "\n" for item in records)
                )
                self.seal_case(args)
                with self.assertRaises(transport_oracle.TransportError):
                    transport_oracle.command_score(args)

    def test_score_rejects_excessive_watcher_lag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            watch = json.loads(args.watch.read_text())
            watch["maxObservationLagMs"] = 251
            args.watch.write_text(json.dumps(watch))
            with self.assertRaisesRegex(transport_oracle.TransportError, "maximum watcher lag"):
                transport_oracle.command_score(args)

    def test_score_rejects_clock_basis_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="streaming-first-qualification",
                                   first_finish=13_000_000_000)
            watch = json.loads(args.watch.read_text())
            watch["clockBasis"] = "linux-clock-monotonic-v1"
            args.watch.write_text(json.dumps(watch))
            with self.assertRaisesRegex(transport_oracle.TransportError, "clock basis"):
                transport_oracle.command_score(args)

            records = [json.loads(line) for line in args.http_log.read_text().splitlines()]
            records[0]["clock_basis"] = "linux-clock-monotonic-v1"
            args.http_log.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.seal_case(args)
            watch["clockBasis"] = "linux-clock-monotonic-raw-v1"
            args.watch.write_text(json.dumps(watch))
            with self.assertRaisesRegex(transport_oracle.TransportError, "HTTP record clock basis"):
                transport_oracle.command_score(args)

    def test_score_requires_a_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            args.service_completion.unlink()
            with self.assertRaises(FileNotFoundError):
                transport_oracle.command_score(args)

    def test_score_rejects_log_content_that_differs_from_sealed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            payload = args.http_log.read_bytes()
            self.assertIn(b'"bytes_sent": 500', payload)
            args.http_log.write_bytes(payload.replace(b'"bytes_sent": 500',
                                                       b'"bytes_sent": 501', 1))
            with self.assertRaisesRegex(transport_oracle.TransportError, "log digest differs"):
                transport_oracle.command_score(args)

    def test_score_rejects_sealed_count_that_differs_from_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            marker = json.loads(args.service_completion.read_text(encoding="ascii"))
            marker["allocatedRequestCount"] += 1
            marker["terminalRecordCount"] += 1
            args.service_completion.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(transport_oracle.TransportError,
                                        "terminal record count differs"):
                transport_oracle.command_score(args)

    def test_score_rejects_noncanonical_or_extended_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            marker = json.loads(args.service_completion.read_text(encoding="ascii"))
            args.service_completion.write_text(json.dumps(marker) + "\n", encoding="ascii")
            with self.assertRaisesRegex(transport_oracle.TransportError, "not canonical JSON"):
                transport_oracle.command_score(args)

            marker["unexpected"] = "field"
            args.service_completion.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(transport_oracle.TransportError, "keys differ"):
                transport_oracle.command_score(args)

    def test_score_rejects_fixture_connection_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.write_case(Path(temporary), role="media-engine-diagnostic",
                                   first_finish=1_500_000_000)
            marker = json.loads(args.service_completion.read_text(encoding="ascii"))
            marker["rejectedConnectionCount"] = 1
            args.service_completion.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(transport_oracle.TransportError, "rejected a connection"):
                transport_oracle.command_score(args)

    def test_watcher_ignores_stale_event_state_and_requires_current_wait_snapshot(self) -> None:
        def record(sequence: int, kind: str, action: str | None = None) -> dict:
            value = {
                "action": action,
                "monotonic_ms": sequence,
                "monotonic_origin_ms": 1_000,
                "paused": False,
                "seeking": False,
                "seq": sequence,
                "source_generation": 1,
                "status": "ok",
                "time": 1.0,
                "type": kind,
            }
            return value

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            driver_json = directory / "driver.jsonl"
            output = directory / "watch.json"
            args = Namespace(
                driver_json=driver_json,
                output=output,
                producer_done=directory / "producer.done",
                timeout_seconds=1,
            )
            args.producer_done.write_text("0\n")
            stale = [record(1, "event"), record(2, "snapshot", "seek"), record(3, "result")]
            driver_json.write_text("".join(json.dumps(item) + "\n" for item in stale))
            with mock.patch.object(transport_oracle, "monotonic_raw_ns", return_value=1_003_000_000):
                with self.assertRaisesRegex(transport_oracle.TransportError, "no advancing playback"):
                    transport_oracle.command_watch(args)

            current = [
                record(1, "snapshot", "wait_time"),
                record(2, "snapshot", "seek"),
                record(3, "result"),
            ]
            driver_json.write_text("".join(json.dumps(item) + "\n" for item in current))
            with mock.patch.object(transport_oracle, "monotonic_raw_ns", return_value=1_003_000_000):
                transport_oracle.command_watch(args)
            self.assertEqual(json.loads(output.read_text())["firstPlaybackMonotonicNs"], 1_001_000_000)

    def test_watcher_accepts_linear_case_without_seek_action(self) -> None:
        def record(sequence: int, kind: str, action: str | None = None) -> dict:
            return {
                "action": action,
                "monotonic_ms": sequence,
                "monotonic_origin_ms": 1_000,
                "paused": False,
                "seeking": False,
                "seq": sequence,
                "source_generation": 1,
                "status": "ok",
                "time": 1.0,
                "type": kind,
            }

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            driver_json = directory / "driver.jsonl"
            output = directory / "watch.json"
            producer_done = directory / "producer.done"
            driver_json.write_text("".join(json.dumps(item) + "\n" for item in (
                record(1, "snapshot", "wait_time"),
                record(2, "result"),
            )))
            producer_done.write_text("0\n")
            args = Namespace(
                driver_json=driver_json,
                output=output,
                producer_done=producer_done,
                timeout_seconds=1,
            )
            with mock.patch.object(transport_oracle, "monotonic_raw_ns", return_value=1_002_000_000):
                transport_oracle.command_watch(args)
            self.assertEqual(json.loads(output.read_text())["seekMonotonicNs"], [])

    def test_watcher_rejects_promptly_after_producer_finishes_without_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            driver_json = directory / "driver.jsonl"
            producer_done = directory / "producer.done"
            driver_json.write_text("")
            producer_done.write_text("1\n")
            args = Namespace(
                driver_json=driver_json,
                output=directory / "watch.json",
                producer_done=producer_done,
                timeout_seconds=60,
            )
            started = time.monotonic()
            with self.assertRaisesRegex(
                transport_oracle.TransportError, "before producer completion"
            ):
                transport_oracle.command_watch(args)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_watcher_rejects_missing_jsonl_after_producer_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            producer_done = directory / "producer.done"
            producer_done.write_text("1\n")
            args = Namespace(
                driver_json=directory / "missing.jsonl",
                output=directory / "watch.json",
                producer_done=producer_done,
                timeout_seconds=60,
            )
            started = time.monotonic()
            with self.assertRaisesRegex(
                transport_oracle.TransportError, "before producer completion"
            ):
                transport_oracle.command_watch(args)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertFalse(args.output.exists())

    def test_watcher_rejects_partial_final_record_after_producer_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            producer_done = directory / "producer.done"
            producer_done.write_text("1\n")
            driver_json = directory / "driver.jsonl"
            driver_json.write_text('{"type":"result"')
            args = Namespace(
                driver_json=driver_json,
                output=directory / "watch.json",
                producer_done=producer_done,
                timeout_seconds=60,
            )
            with self.assertRaisesRegex(transport_oracle.TransportError, "partial record"):
                transport_oracle.command_watch(args)
            self.assertFalse(args.output.exists())

    def test_watcher_rejects_record_after_terminal_result(self) -> None:
        def record(sequence: int, kind: str, action: str | None = None) -> dict:
            return {
                "action": action,
                "monotonic_ms": sequence,
                "monotonic_origin_ms": 1_000,
                "paused": False,
                "seeking": False,
                "seq": sequence,
                "source_generation": 1,
                "status": "ok",
                "time": 1.0,
                "type": kind,
            }

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            records = [
                record(1, "snapshot", "wait_time"),
                record(2, "snapshot", "seek"),
                record(3, "result"),
                record(4, "snapshot", "wait_time"),
            ]
            driver_json = directory / "driver.jsonl"
            driver_json.write_text("".join(json.dumps(item) + "\n" for item in records))
            producer_done = directory / "producer.done"
            producer_done.write_text("0\n")
            args = Namespace(
                driver_json=driver_json,
                output=directory / "watch.json",
                producer_done=producer_done,
                timeout_seconds=60,
            )
            with mock.patch.object(transport_oracle, "monotonic_raw_ns", return_value=1_004_000_000):
                with self.assertRaisesRegex(
                    transport_oracle.TransportError, "followed the terminal result"
                ):
                    transport_oracle.command_watch(args)
            self.assertFalse(args.output.exists())

    def test_result_without_producer_completion_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = {
                "action": "wait_time",
                "monotonic_ms": 1,
                "monotonic_origin_ms": 1_000,
                "paused": False,
                "seeking": False,
                "seq": 1,
                "source_generation": 1,
                "status": "ok",
                "time": 1.0,
                "type": "result",
            }
            driver_json = directory / "driver.jsonl"
            driver_json.write_text(json.dumps(record) + "\n")
            args = Namespace(
                driver_json=driver_json,
                output=directory / "watch.json",
                producer_done=directory / "producer.done",
                timeout_seconds=1,
            )
            with mock.patch.object(transport_oracle, "monotonic_raw_ns", return_value=1_001_000_000), \
                 mock.patch.object(transport_oracle.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
                with self.assertRaisesRegex(
                    transport_oracle.TransportError, "producer completion"
                ):
                    transport_oracle.command_watch(args)
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
