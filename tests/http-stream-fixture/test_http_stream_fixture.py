#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the localhost progressive HTTP fixture."""

from __future__ import annotations

import argparse
import io
import json
import threading
import time
import unittest

from http_stream_fixture import (
    CLOCK_BASIS,
    ChunkedWriter,
    FailureGate,
    PostOpenRangeGate,
    RangeNotSatisfiable,
    SafeJsonlLogger,
    bounded_nonnegative_float,
    make_response_plan,
    monotonic_raw_ns,
    parse_byte_range,
    route_path,
    safe_range_label,
    transfer_body,
    validate_bind_address,
)


class RangeParsingTests(unittest.TestCase):
    def test_explicit_open_and_suffix_ranges(self) -> None:
        self.assertEqual((2, 5), (parse_byte_range("bytes=2-5", 10).start, parse_byte_range("bytes=2-5", 10).end))
        self.assertEqual((7, 9), (parse_byte_range("bytes=7-", 10).start, parse_byte_range("bytes=7-", 10).end))
        self.assertEqual((6, 9), (parse_byte_range("bytes=-4", 10).start, parse_byte_range("bytes=-4", 10).end))
        self.assertEqual("bytes=2-9", parse_byte_range("bytes=2-99", 10).label)

    def test_invalid_and_multiple_ranges_are_rejected(self) -> None:
        for value in ("bytes=10-", "bytes=5-2", "bytes=-0", "bytes=0-1,4-5", "items=0-2", "secret"):
            with self.subTest(value=value), self.assertRaises(RangeNotSatisfiable):
                parse_byte_range(value, 10)

    def test_unsafe_range_text_is_never_returned_for_logging(self) -> None:
        self.assertEqual("invalid", safe_range_label("bytes=0-2?token=DO_NOT_LOG", 10))
        self.assertIsNone(safe_range_label(None, 10))


class ResponsePlanTests(unittest.TestCase):
    def test_no_range_mode_ignores_valid_and_invalid_range_requests(self) -> None:
        for header, expected_label in (("bytes=2-5", "bytes=2-5"), ("token=DO_NOT_LOG", "invalid")):
            with self.subTest(header=header):
                plan = make_response_plan(
                    mode="no-range",
                    request_target="/media?credential=DO_NOT_LOG",
                    range_header=header,
                    file_size=10,
                )
                self.assertEqual(200, plan.status)
                self.assertEqual(0, plan.offset)
                self.assertEqual(10, plan.declared_bytes)
                self.assertEqual("none", plan.accept_ranges)
                self.assertEqual(expected_label, plan.range_label)

    def test_range_and_unsatisfied_range_plans(self) -> None:
        plan = make_response_plan(
            mode="range", request_target="/media", range_header="bytes=2-5", file_size=10
        )
        self.assertEqual((206, 2, 4), (plan.status, plan.offset, plan.declared_bytes))
        self.assertEqual("bytes 2-5/10", plan.content_range)

        unsatisfied = make_response_plan(
            mode="range", request_target="/media", range_header="bytes=99-", file_size=10
        )
        self.assertEqual(416, unsatisfied.status)
        self.assertEqual("bytes */10", unsatisfied.content_range)

    def test_redirect_strips_sensitive_query_and_targets_fixed_route(self) -> None:
        self.assertEqual("/media", route_path("/media?token=DO_NOT_LOG#fragment"))
        redirect = make_response_plan(
            mode="redirect",
            request_target="/media?token=DO_NOT_LOG",
            range_header=None,
            file_size=10,
        )
        self.assertEqual(307, redirect.status)
        self.assertEqual("/redirect-target", redirect.location)

        target = make_response_plan(
            mode="redirect",
            request_target=redirect.location,
            range_header="bytes=4-",
            file_size=10,
        )
        self.assertEqual((206, 4, 6), (target.status, target.offset, target.declared_bytes))

    def test_fail_first_then_serves_the_same_resource(self) -> None:
        gate = FailureGate(2)
        statuses = [
            make_response_plan(
                mode="fail-first",
                request_target="/media",
                range_header=None,
                file_size=10,
                failure_gate=gate,
            ).status
            for _ in range(3)
        ]
        self.assertEqual([503, 503, 200], statuses)

    def test_fail_first_uses_the_selected_bounded_status(self) -> None:
        gate = FailureGate(1)
        failed = make_response_plan(
            mode="fail-first",
            request_target="/media",
            range_header=None,
            file_size=10,
            failure_gate=gate,
            failure_status=429,
        )
        recovered = make_response_plan(
            mode="fail-first",
            request_target="/media",
            range_header=None,
            file_size=10,
            failure_gate=gate,
            failure_status=429,
        )
        self.assertEqual((429, 200), (failed.status, recovered.status))

    def test_post_open_range_failure_ignores_head_and_rejects_later_ranges(self) -> None:
        gate = PostOpenRangeGate()

        head = make_response_plan(
            mode="fail-post-open-range",
            method="HEAD",
            request_target="/media",
            range_header=None,
            file_size=10,
            post_open_range_gate=gate,
        )
        opened = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header=None,
            file_size=10,
            post_open_range_gate=gate,
        )
        failed_seek = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header="bytes=5-",
            file_size=10,
            post_open_range_gate=gate,
        )
        failed_retry = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header="bytes=6-",
            file_size=10,
            post_open_range_gate=gate,
        )
        ordinary_retry = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header=None,
            file_size=10,
            post_open_range_gate=gate,
        )

        self.assertEqual((200, 200), (head.status, opened.status))
        self.assertEqual((503, 0, "bytes=5-9"),
                         (failed_seek.status, failed_seek.declared_bytes, failed_seek.range_label))
        self.assertEqual(503, failed_retry.status)
        self.assertEqual(200, ordinary_retry.status)

    def test_initial_range_open_is_allowed_before_range_failures(self) -> None:
        gate = PostOpenRangeGate()
        opened = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header="bytes=0-",
            file_size=10,
            post_open_range_gate=gate,
        )
        failed_seek = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header="bytes=5-",
            file_size=10,
            post_open_range_gate=gate,
        )

        self.assertEqual((206, 0, 10), (opened.status, opened.offset, opened.declared_bytes))
        self.assertEqual(503, failed_seek.status)

    def test_failed_media_range_does_not_poison_the_recovery_route(self) -> None:
        gate = PostOpenRangeGate()
        opened = make_response_plan(
            mode="fail-post-open-range-recovery",
            method="GET",
            request_target="/media",
            range_header="bytes=0-",
            file_size=10,
            post_open_range_gate=gate,
        )
        failed_seek = make_response_plan(
            mode="fail-post-open-range-recovery",
            method="GET",
            request_target="/media",
            range_header="bytes=5-",
            file_size=10,
            post_open_range_gate=gate,
        )
        recovered_open = make_response_plan(
            mode="fail-post-open-range-recovery",
            method="GET",
            request_target="/recovery?case=bounded-recovery",
            range_header="bytes=0-",
            file_size=10,
            post_open_range_gate=gate,
        )
        recovered_seek = make_response_plan(
            mode="fail-post-open-range-recovery",
            method="GET",
            request_target="/recovery",
            range_header="bytes=6-",
            file_size=10,
            post_open_range_gate=gate,
        )
        media_retry = make_response_plan(
            mode="fail-post-open-range-recovery",
            method="GET",
            request_target="/media",
            range_header="bytes=6-",
            file_size=10,
            post_open_range_gate=gate,
        )

        self.assertEqual((206, 503), (opened.status, failed_seek.status))
        self.assertEqual((206, 0, 10), (
            recovered_open.status,
            recovered_open.offset,
            recovered_open.declared_bytes,
        ))
        self.assertEqual((206, 6, 4), (
            recovered_seek.status,
            recovered_seek.offset,
            recovered_seek.declared_bytes,
        ))
        self.assertEqual(503, media_retry.status)

    def test_recovery_route_is_reserved_for_the_recovery_mode(self) -> None:
        ordinary = make_response_plan(
            mode="range",
            request_target="/recovery",
            range_header=None,
            file_size=10,
        )
        terminal_only = make_response_plan(
            mode="fail-post-open-range",
            request_target="/recovery",
            range_header=None,
            file_size=10,
            post_open_range_gate=PostOpenRangeGate(),
        )
        unknown = make_response_plan(
            mode="fail-post-open-range-recovery",
            request_target="/recovered",
            range_header=None,
            file_size=10,
            post_open_range_gate=PostOpenRangeGate(),
        )

        self.assertEqual((404, 404, 404), (
            ordinary.status,
            terminal_only.status,
            unknown.status,
        ))

    def test_invalid_range_does_not_arm_or_expose_unsafe_text(self) -> None:
        gate = PostOpenRangeGate()
        invalid = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media?credential=DO_NOT_LOG",
            range_header="bytes=0-2?token=DO_NOT_LOG",
            file_size=10,
            post_open_range_gate=gate,
        )
        opened = make_response_plan(
            mode="fail-post-open-range",
            method="GET",
            request_target="/media",
            range_header="bytes=0-",
            file_size=10,
            post_open_range_gate=gate,
        )

        self.assertEqual((416, "invalid"), (invalid.status, invalid.range_label))
        self.assertEqual(206, opened.status)

    def test_truncation_is_part_of_successful_range_plan(self) -> None:
        plan = make_response_plan(
            mode="truncate",
            request_target="/media",
            range_header="bytes=1-8",
            file_size=10,
            truncate_after=3,
        )
        self.assertEqual(206, plan.status)
        self.assertEqual(8, plan.declared_bytes)
        self.assertEqual(3, plan.truncate_after)

    def test_chunked_plan_keeps_range_semantics_without_content_length(self) -> None:
        plan = make_response_plan(
            mode="chunked",
            request_target="/media",
            range_header="bytes=2-5",
            file_size=10,
        )
        self.assertEqual((206, 2, 4), (plan.status, plan.offset, plan.declared_bytes))
        self.assertTrue(plan.chunked)


class TransferTests(unittest.TestCase):
    def test_throttle_uses_injected_sleep_between_chunks(self) -> None:
        sleeps: list[float] = []
        output = io.BytesIO()
        result = transfer_body(
            io.BytesIO(b"0123456789"),
            output,
            byte_count=10,
            chunk_bytes=4,
            bytes_per_second=4,
            sleeper=sleeps.append,
        )
        self.assertEqual(b"0123456789", output.getvalue())
        self.assertEqual([1.0, 1.0], sleeps)
        self.assertEqual((10, False), (result.bytes_sent, result.disconnected))

    def test_mid_stream_truncation_declares_disconnect(self) -> None:
        output = io.BytesIO()
        result = transfer_body(
            io.BytesIO(b"0123456789"),
            output,
            byte_count=10,
            chunk_bytes=4,
            bytes_per_second=0,
            truncate_after=5,
        )
        self.assertEqual(b"01234", output.getvalue())
        self.assertEqual((5, True), (result.bytes_sent, result.disconnected))

    def test_mid_stream_stall_occurs_once_at_the_exact_boundary(self) -> None:
        sleeps: list[float] = []
        output = io.BytesIO()
        result = transfer_body(
            io.BytesIO(b"0123456789"),
            output,
            byte_count=10,
            chunk_bytes=6,
            bytes_per_second=0,
            stall_after=5,
            stall_seconds=1.25,
            sleeper=sleeps.append,
        )
        self.assertEqual(b"0123456789", output.getvalue())
        self.assertEqual([1.25], sleeps)
        self.assertEqual((10, False), (result.bytes_sent, result.disconnected))

    def test_stall_wait_is_cancellable_without_waiting_for_its_deadline(self) -> None:
        cancel = threading.Event()
        output = io.BytesIO()
        result: list[object] = []

        worker = threading.Thread(target=lambda: result.append(transfer_body(
            io.BytesIO(b"0123456789"),
            output,
            byte_count=10,
            chunk_bytes=5,
            bytes_per_second=0,
            stall_after=5,
            stall_seconds=300.0,
            cancel_event=cancel,
        )))
        worker.start()
        deadline = time.monotonic() + 1.0
        while len(output.getvalue()) < 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(b"01234", output.getvalue())
        cancel.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual((5, True), (result[0].bytes_sent, result[0].disconnected))

    def test_chunked_writer_frames_payload_and_terminates(self) -> None:
        output = io.BytesIO()
        writer = ChunkedWriter(output)
        self.assertEqual(3, writer.write(b"abc"))
        self.assertEqual(2, writer.write(b"de"))
        writer.finish()
        self.assertEqual(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n", output.getvalue())


class SafeLogTests(unittest.TestCase):
    def test_log_schema_is_allowlisted_and_sensitive_inputs_are_absent(self) -> None:
        stream = io.StringIO()
        logger = SafeJsonlLogger(stream, "vod-range-01")
        unsafe_range = "bytes=0-4?token=DO_NOT_LOG"
        logger.record(
            method="GET",
            status=416,
            range_label=safe_range_label(unsafe_range, 10),
            bytes_expected=0,
            bytes_sent=0,
            started_at="2026-01-01T00:00:00.000Z",
            finished_at="2026-01-01T00:00:00.001Z",
            disconnected=False,
            request_seq=1,
            started_monotonic_ns=100,
            finished_monotonic_ns=200,
        )

        serialized = stream.getvalue()
        self.assertNotIn("DO_NOT_LOG", serialized)
        self.assertNotIn("cookie", serialized.lower())
        self.assertNotIn("url", serialized.lower())
        self.assertNotIn("path", serialized.lower())
        record = json.loads(serialized)
        self.assertEqual(
            {
                "bytes_expected",
                "bytes_sent",
                "case_id",
                "clock_basis",
                "disconnected",
                "finished_at",
                "finished_monotonic_ns",
                "method",
                "range",
                "request_seq",
                "started_at",
                "started_monotonic_ns",
                "status",
            },
            set(record),
        )
        self.assertEqual("invalid", record["range"])
        self.assertEqual(CLOCK_BASIS, record["clock_basis"])

    def test_log_rejects_regressing_or_noninteger_monotonic_interval(self) -> None:
        logger = SafeJsonlLogger(io.StringIO(), "clock-check")
        common = {
            "method": "GET",
            "status": 200,
            "range_label": None,
            "bytes_expected": 0,
            "bytes_sent": 0,
            "started_at": "2026-01-01T00:00:00.000Z",
            "finished_at": "2026-01-01T00:00:00.001Z",
            "disconnected": False,
            "request_seq": 1,
        }
        with self.assertRaisesRegex(ValueError, "monotonic"):
            logger.record(**common, started_monotonic_ns=200, finished_monotonic_ns=100)
        with self.assertRaisesRegex(ValueError, "monotonic"):
            logger.record(**common, started_monotonic_ns=True, finished_monotonic_ns=200)

    def test_raw_monotonic_clock_is_available_and_nondecreasing(self) -> None:
        first = monotonic_raw_ns()
        second = monotonic_raw_ns()
        self.assertIs(type(first), int)
        self.assertGreaterEqual(second, first)

    def test_log_budget_fails_closed(self) -> None:
        logger = SafeJsonlLogger(io.StringIO(), "bounded", max_bytes=1)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            logger.record(
                method="GET",
                status=200,
                range_label=None,
                bytes_expected=0,
                bytes_sent=0,
                started_at="2026-01-01T00:00:00.000Z",
                finished_at="2026-01-01T00:00:00.001Z",
                disconnected=False,
                request_seq=1,
                started_monotonic_ns=100,
                finished_monotonic_ns=200,
            )

    def test_existing_append_content_counts_toward_log_budget(self) -> None:
        stream = io.StringIO("x" * 32)
        stream.seek(0, io.SEEK_END)
        with self.assertRaisesRegex(ValueError, "existing sanitized log"):
            SafeJsonlLogger(stream, "bounded", max_bytes=31)
        logger = SafeJsonlLogger(stream, "bounded", max_bytes=33)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            logger.record(
                method="GET",
                status=200,
                range_label=None,
                bytes_expected=0,
                bytes_sent=0,
                started_at="2026-01-01T00:00:00.000Z",
                finished_at="2026-01-01T00:00:00.001Z",
                disconnected=False,
                request_seq=1,
                started_monotonic_ns=100,
                finished_monotonic_ns=200,
            )


class ArgumentBoundaryTests(unittest.TestCase):
    def test_delay_rejects_nonfinite_and_excessive_values(self) -> None:
        for value in ("nan", "inf", "-inf", "-1", "301"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                bounded_nonnegative_float(value)
        self.assertEqual(0.25, bounded_nonnegative_float("0.25"))

    def test_bind_scope_is_fail_closed(self) -> None:
        self.assertEqual("127.0.0.1", validate_bind_address("127.0.0.1", "loopback"))
        self.assertEqual("192.0.2.10", validate_bind_address("192.0.2.10", "vm-private"))
        for value, scope in (
            ("0.0.0.0", "loopback"),
            ("192.168.1.2", "vm-private"),
            ("localhost", "loopback"),
        ):
            with self.subTest(value=value, scope=scope), self.assertRaises(argparse.ArgumentTypeError):
                validate_bind_address(value, scope)


if __name__ == "__main__":
    unittest.main()
