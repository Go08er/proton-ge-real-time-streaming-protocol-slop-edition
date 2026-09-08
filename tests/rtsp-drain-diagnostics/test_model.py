#!/usr/bin/env python3
"""Deterministic model for RTSP queue reasons and diagnostic budgets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
import unittest


NORMAL_PACKETS = 8
NORMAL_BYTES = 8 * 1024 * 1024
EMERGENCY_PACKETS = 1024
EMERGENCY_BYTES = 64 * 1024 * 1024
DIAGNOSTIC_SOURCE_LIMIT = 64
DIAGNOSTIC_LIVE_RECORDS = 16
DIAGNOSTIC_STREAM_RECORDS = 8
DIAGNOSTIC_SAR_LIMIT = 64


class Reason(IntFlag):
    NONE = 0
    IDLE = 1
    STREAM_PACKETS = 2
    STREAM_BYTES = 4
    EMERGENCY_PACKETS = 8
    EMERGENCY_BYTES = 16


@dataclass(frozen=True)
class Stream:
    packets: int
    bytes: int
    active: bool = True
    eos: bool = False


def queue_reason(streams: list[Stream]) -> Reason:
    active = [stream for stream in streams if stream.active and not stream.eos]
    if not active:
        return Reason.IDLE

    packets = sum(stream.packets for stream in active)
    bytes_ = sum(stream.bytes for stream in active)
    primed = all(stream.packets for stream in active)
    reason = Reason.NONE
    if packets >= EMERGENCY_PACKETS:
        reason |= Reason.EMERGENCY_PACKETS
    if bytes_ >= EMERGENCY_BYTES:
        reason |= Reason.EMERGENCY_BYTES
    if primed and any(stream.packets >= NORMAL_PACKETS for stream in active):
        reason |= Reason.STREAM_PACKETS
    if primed and any(stream.bytes >= NORMAL_BYTES for stream in active):
        reason |= Reason.STREAM_BYTES
    return reason


def legacy_full(streams: list[Stream]) -> bool:
    active = [stream for stream in streams if stream.active and not stream.eos]
    if not active:
        return True
    packets = sum(stream.packets for stream in active)
    bytes_ = sum(stream.bytes for stream in active)
    primed = all(stream.packets for stream in active)
    any_full = any(
        stream.packets >= NORMAL_PACKETS or stream.bytes >= NORMAL_BYTES
        for stream in active
    )
    return (
        packets >= EMERGENCY_PACKETS
        or bytes_ >= EMERGENCY_BYTES
        or (primed and any_full)
    )


@dataclass
class SourceBudget:
    source_id: int
    enabled: bool
    live_limit: int = DIAGNOSTIC_LIVE_RECORDS
    live: int = 0
    exhausted: bool = False

    def claim_live(self) -> bool:
        if not self.enabled or self.exhausted:
            return False
        self.live += 1
        if self.live == self.live_limit:
            self.exhausted = True
        return True

    def final_records(self, stream_count: int, *, terminal: bool = False) -> int:
        if not self.enabled:
            return 0
        return (
            min(stream_count, DIAGNOSTIC_STREAM_RECORDS)
            + 1  # source summary
            + int(terminal)
        )


@dataclass
class SarDiagnostic:
    sar_id: int
    enabled: bool
    clock_start_calls: int = 0
    clock_start_successes: int = 0
    audio_client_start_calls: int = 0
    audio_client_start_successes: int = 0
    process_samples: int = 0
    preclock_samples: int = 0
    render_callbacks: int = 0
    queued_frames: int = 0
    peak_queued_frames: int = 0
    summary_emitted: bool = False

    def process_sample(self, frames: int = 0) -> None:
        if not self.enabled:
            return
        self.process_samples += 1
        if not self.clock_start_calls:
            self.preclock_samples += 1
        self.queued_frames += frames
        self.peak_queued_frames = max(
            self.peak_queued_frames,
            self.queued_frames,
        )

    def clock_start(self, *, audio_start_success: bool) -> None:
        if not self.enabled:
            return
        self.clock_start_calls += 1
        self.audio_client_start_calls += 1
        if audio_start_success:
            self.audio_client_start_successes += 1
            self.clock_start_successes += 1

    def render(self, frames: int = 0) -> None:
        if not self.enabled:
            return
        self.render_callbacks += 1
        self.queued_frames = max(0, self.queued_frames - frames)

    def emit_summary(self) -> int:
        if not self.enabled or self.summary_emitted:
            return 0
        self.summary_emitted = True
        return 1


@dataclass
class ProcessBudget:
    source_limit: int = DIAGNOSTIC_SOURCE_LIMIT
    sar_limit: int = DIAGNOSTIC_SAR_LIMIT
    next_source_id: int = 0
    next_sar_id: int = 0

    def open_source(self) -> SourceBudget:
        self.next_source_id += 1
        return SourceBudget(
            source_id=self.next_source_id,
            enabled=self.next_source_id <= self.source_limit,
        )

    def open_sar(self) -> SarDiagnostic:
        self.next_sar_id += 1
        return SarDiagnostic(
            sar_id=self.next_sar_id,
            enabled=self.next_sar_id <= self.sar_limit,
        )


class QueueReasonTests(unittest.TestCase):
    def test_no_active_stream_is_idle_not_a_pressure_bound(self) -> None:
        self.assertEqual(queue_reason([]), Reason.IDLE)

    def test_unprimed_stream_suppresses_normal_packet_bound(self) -> None:
        streams = [Stream(NORMAL_PACKETS, 1024), Stream(0, 0)]
        self.assertEqual(queue_reason(streams), Reason.NONE)

    def test_primed_stream_reports_normal_packet_bound(self) -> None:
        streams = [Stream(NORMAL_PACKETS, 1024), Stream(1, 128)]
        self.assertEqual(queue_reason(streams), Reason.STREAM_PACKETS)

    def test_primed_stream_reports_normal_byte_bound(self) -> None:
        streams = [Stream(1, NORMAL_BYTES), Stream(1, 128)]
        self.assertEqual(queue_reason(streams), Reason.STREAM_BYTES)

    def test_emergency_packet_bound_ignores_priming(self) -> None:
        streams = [Stream(EMERGENCY_PACKETS, 1024), Stream(0, 0)]
        self.assertEqual(queue_reason(streams), Reason.EMERGENCY_PACKETS)

    def test_emergency_byte_bound_ignores_priming(self) -> None:
        streams = [Stream(1, EMERGENCY_BYTES), Stream(0, 0)]
        self.assertEqual(queue_reason(streams), Reason.EMERGENCY_BYTES)

    def test_reason_refactor_preserves_legacy_decision(self) -> None:
        packet_values = (0, 1, 7, 8, 1024)
        byte_values = (0, 1, NORMAL_BYTES - 1, NORMAL_BYTES, EMERGENCY_BYTES)
        for first_packets in packet_values:
            for second_packets in packet_values:
                for first_bytes in byte_values:
                    streams = [
                        Stream(first_packets, first_bytes),
                        Stream(second_packets, 1),
                    ]
                    with self.subTest(streams=streams):
                        self.assertEqual(
                            bool(queue_reason(streams)),
                            legacy_full(streams),
                        )


class DiagnosticBudgetTests(unittest.TestCase):
    def test_early_exhaustion_does_not_consume_late_source_records(self) -> None:
        process = ProcessBudget()
        first = process.open_source()
        self.assertEqual(
            [first.claim_live() for _ in range(DIAGNOSTIC_LIVE_RECORDS + 2)],
            [True] * DIAGNOSTIC_LIVE_RECORDS + [False, False],
        )

        for _ in range(9):
            process.open_source()
        late = process.open_source()
        self.assertEqual(late.source_id, 11)
        self.assertTrue(late.claim_live())
        self.assertEqual(late.final_records(2), 3)

    def test_post_exhaustion_checks_do_not_mutate_state(self) -> None:
        source = ProcessBudget().open_source()
        for _ in range(DIAGNOSTIC_LIVE_RECORDS):
            self.assertTrue(source.claim_live())
        before = (source.live, source.exhausted)
        for _ in range(100_000):
            self.assertFalse(source.claim_live())
        self.assertEqual((source.live, source.exhausted), before)

    def test_source_horizon_proves_exact_process_line_bound(self) -> None:
        process = ProcessBudget()
        sources = [
            process.open_source()
            for _ in range(DIAGNOSTIC_SOURCE_LIMIT + 1)
        ]
        self.assertTrue(all(source.enabled for source in sources[:-1]))
        self.assertFalse(sources[-1].enabled)

        maximum = sum(
            (source.live_limit if source.enabled else 0)
            + source.final_records(
                DIAGNOSTIC_STREAM_RECORDS,
                terminal=True,
            )
            for source in sources
        )
        sar_objects = [
            process.open_sar()
            for _ in range(DIAGNOSTIC_SAR_LIMIT + 1)
        ]
        self.assertTrue(all(sar.enabled for sar in sar_objects[:-1]))
        self.assertFalse(sar_objects[-1].enabled)
        maximum += sum(sar.emit_summary() for sar in sar_objects)
        self.assertEqual(maximum, 1728)

    def test_sar_shutdown_and_release_emit_exactly_one_summary(self) -> None:
        sar = ProcessBudget().open_sar()
        self.assertEqual(sar.emit_summary(), 1)
        self.assertEqual(sar.emit_summary(), 0)

    def test_sar_preclock_and_started_then_stopped_are_distinguishable(self) -> None:
        never_started = ProcessBudget().open_sar()
        for _ in range(6):
            never_started.process_sample()
        self.assertEqual(never_started.clock_start_calls, 0)
        self.assertEqual(never_started.preclock_samples, 6)

        started = ProcessBudget().open_sar()
        started.clock_start(audio_start_success=True)
        for _ in range(6):
            started.process_sample(frames=256)
        started.render(frames=256)
        self.assertEqual(started.clock_start_successes, 1)
        self.assertEqual(started.audio_client_start_successes, 1)
        self.assertEqual(started.preclock_samples, 0)
        self.assertEqual(started.render_callbacks, 1)
        self.assertEqual(started.peak_queued_frames, 1536)
        self.assertEqual(started.queued_frames, 1280)

    def test_clock_callback_and_audio_client_failure_remain_distinct(self) -> None:
        sar = ProcessBudget().open_sar()
        sar.clock_start(audio_start_success=False)
        self.assertEqual(sar.clock_start_calls, 1)
        self.assertEqual(sar.clock_start_successes, 0)
        self.assertEqual(sar.audio_client_start_calls, 1)
        self.assertEqual(sar.audio_client_start_successes, 0)

    def test_buffer_growth_retry_is_not_a_completed_packet(self) -> None:
        results = ["BUFFER_TOO_SMALL", "SUCCESS"]
        completed = sum(result == "SUCCESS" for result in results)
        self.assertEqual(completed, 1)

    def test_queue_gate_and_read_timeout_are_independent_stages(self) -> None:
        gated = queue_reason([Stream(NORMAL_PACKETS, 1024)])
        self.assertTrue(gated)
        read_started = not bool(gated)
        self.assertFalse(read_started)

        ungated = queue_reason([Stream(1, 1024)])
        self.assertFalse(ungated)
        read_started = not bool(ungated)
        read_status = "IO_TIMEOUT" if read_started else "NOT_CALLED"
        self.assertEqual(read_status, "IO_TIMEOUT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
