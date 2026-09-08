#!/usr/bin/env python3
"""Deterministic checks for bounded PCM aggregate inspection states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import unittest


SAMPLE_LIMIT = 16
VALUE_LIMIT = 16_384
S_OK = 0
S_FALSE = 1
E_FAIL = 0x80004005
E_NOTIMPL = 0x80004001
MF_E_INVALIDMEDIATYPE = 0xC00D36B4
MF_E_NOTACCEPTING = 0xC00D36B5


def succeeded(hr: int) -> bool:
    return not bool(hr & 0x80000000)


class InspectionStatus(Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    INVALID = "invalid"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    UNSUPPORTED_BUFFERS = "unsupported_buffers"


class ProbeBoundary(Enum):
    AAC_DECODER_INPUT = "aac_decoder_input"
    AAC_DECODER_OUTPUT = "aac_decoder_output"
    MEDIA_ENGINE_EFFECT_INPUT = "media_engine_audio_effect_input"
    MEDIA_ENGINE_EFFECT_OUTPUT = "media_engine_audio_effect_output"


@dataclass(frozen=True)
class ProbeResult:
    status: InspectionStatus
    inspection_hr: int
    total_values: int
    inspected_values: int
    nonzero: int
    peak: float
    mean_square: float
    nonfinite: int

    @property
    def is_full_silence(self) -> bool:
        return (
            self.status is InspectionStatus.COMPLETE
            and self.inspection_hr == S_OK
            and self.total_values > 0
            and self.inspected_values == self.total_values
            and self.nonzero == 0
            and self.nonfinite == 0
        )


@dataclass(frozen=True)
class EffectInputTrace:
    sequence: int
    session_identity: str
    inspection: ProbeResult
    process_hr: int
    queue_attempted: bool
    queue_hr: int
    delivery_hr: int
    stages: tuple[str, ...]
    process_valid: bool = True
    delivery_valid: bool = True

    @property
    def accepted(self) -> bool:
        return succeeded(self.process_hr)

    @property
    def queued(self) -> bool:
        return self.queue_attempted and succeeded(self.queue_hr)


@dataclass(frozen=True)
class DecoderOutputTrace:
    session_identity: str
    inspection: ProbeResult
    process_valid: bool = False
    process_hr: int = E_NOTIMPL
    delivery_valid: bool = False
    delivery_hr: int = E_NOTIMPL


def result_for_failure(hr: int = E_FAIL) -> ProbeResult:
    if succeeded(hr):
        raise ValueError("a failed inspection requires a failing HRESULT")
    return ProbeResult(InspectionStatus.FAILED, hr, 0, 0, 0, 0.0, 0.0, 0)


def result_for_unsupported(hr: int = MF_E_INVALIDMEDIATYPE) -> ProbeResult:
    return ProbeResult(InspectionStatus.UNSUPPORTED, hr, 0, 0, 0, 0.0, 0.0, 0)


def result_for_unsupported_buffers() -> ProbeResult:
    return ProbeResult(InspectionStatus.UNSUPPORTED_BUFFERS, E_NOTIMPL,
                       0, 0, 0, 0.0, 0.0, 0)


def result_for_invalid(hr: int = MF_E_INVALIDMEDIATYPE) -> ProbeResult:
    return ProbeResult(InspectionStatus.INVALID, hr, 0, 0, 0, 0.0, 0.0, 0)


def inspection_shape(total_values: int) -> tuple[InspectionStatus, int]:
    inspected_values = min(total_values, VALUE_LIMIT)
    status = (InspectionStatus.COMPLETE if inspected_values == total_values
              else InspectionStatus.TRUNCATED)
    return status, inspected_values


def summarize_f32(values: list[float]) -> ProbeResult:
    status, inspected_values = inspection_shape(len(values))
    nonzero = 0
    nonfinite = 0
    peak = 0.0
    sum_square = 0.0

    for value in values[:inspected_values]:
        if not math.isfinite(value):
            nonfinite += 1
            continue
        magnitude = abs(value)
        nonzero += magnitude != 0.0
        peak = max(peak, magnitude)
        sum_square += magnitude * magnitude

    finite = inspected_values - nonfinite
    return ProbeResult(status, S_OK, len(values), inspected_values, nonzero,
                       peak, sum_square / finite if finite else 0.0, nonfinite)


def summarize_s16(values: list[int]) -> ProbeResult:
    status, inspected_values = inspection_shape(len(values))
    normalized = [value / 32768.0 for value in values[:inspected_values]]
    nonzero = sum(value != 0.0 for value in normalized)
    peak = max((abs(value) for value in normalized), default=0.0)
    mean_square = sum(value * value for value in normalized) / len(normalized) if normalized else 0.0
    return ProbeResult(status, S_OK, len(values), inspected_values, nonzero,
                       peak, mean_square, 0)


class ProbeCounter:
    def __init__(self) -> None:
        self.count = 0

    def begin_attempt(self) -> bool:
        return self.claim_sequence() is not None

    def claim_sequence(self) -> int | None:
        if self.count >= SAMPLE_LIMIT:
            return None
        sequence = self.count
        self.count += 1
        return sequence


def boundary_is_selected(boundary: ProbeBoundary) -> bool:
    """A3.7 keeps the useful decoder output and moves the effect probe to input."""
    return boundary in {
        ProbeBoundary.AAC_DECODER_OUTPUT,
        ProbeBoundary.MEDIA_ENGINE_EFFECT_INPUT,
    }


def decoder_output_trace(inspection: ProbeResult,
                         session_identity: str = "session-0") -> DecoderOutputTrace:
    """Model an output-only boundary with no meaningful input or delivery result."""
    return DecoderOutputTrace(session_identity, inspection)


def effect_input_attempt(counter: ProbeCounter, inspection: ProbeResult,
                         process_hr: int, queue_hr: int = S_OK,
                         session_identity: str = "session-0") -> EffectInputTrace | None:
    """Model inspect/unlock -> ProcessInput -> optional queue -> one combined trace."""
    sequence = counter.claim_sequence()
    if sequence is None:
        return None

    stages = ["inspect", "unlock", "process_input"]
    queue_attempted = process_hr == MF_E_NOTACCEPTING
    if queue_attempted:
        stages.append("queue")
        delivery_hr = queue_hr
    else:
        queue_hr = S_FALSE
        delivery_hr = process_hr
    stages.append("combined_trace")

    return EffectInputTrace(sequence, session_identity, inspection, process_hr, queue_attempted,
                            queue_hr, delivery_hr, tuple(stages))


class PcmProbeTests(unittest.TestCase):
    def test_a37_selects_decoder_output_and_effect_input_only(self) -> None:
        selected = {boundary for boundary in ProbeBoundary if boundary_is_selected(boundary)}
        self.assertEqual(selected, {
            ProbeBoundary.AAC_DECODER_OUTPUT,
            ProbeBoundary.MEDIA_ENGINE_EFFECT_INPUT,
        })

    def test_float_silence_is_unambiguous(self) -> None:
        result = summarize_f32([0.0] * 2048)
        self.assertEqual(result.status, InspectionStatus.COMPLETE)
        self.assertEqual((result.total_values, result.inspected_values), (2048, 2048))
        self.assertEqual((result.nonzero, result.peak, result.mean_square, result.nonfinite),
                         (0, 0.0, 0.0, 0))
        self.assertTrue(result.is_full_silence)

    def test_float_signal_reports_peak_and_energy(self) -> None:
        result = summarize_f32([0.5, -0.25, 0.0, 0.25])
        self.assertEqual(result.nonzero, 3)
        self.assertEqual(result.peak, 0.5)
        self.assertAlmostEqual(result.mean_square, 0.09375)
        self.assertEqual(result.nonfinite, 0)
        self.assertFalse(result.is_full_silence)

    def test_nonfinite_float_values_are_counted_and_excluded_from_energy(self) -> None:
        result = summarize_f32([float("nan"), float("inf"), -0.5])
        self.assertEqual((result.nonzero, result.peak, result.nonfinite), (1, 0.5, 2))
        self.assertAlmostEqual(result.mean_square, 0.25)
        self.assertFalse(result.is_full_silence)

    def test_pcm16_silence_is_unambiguous(self) -> None:
        result = summarize_s16([0] * 2048)
        self.assertEqual((result.nonzero, result.peak, result.mean_square, result.nonfinite),
                         (0, 0.0, 0.0, 0))
        self.assertTrue(result.is_full_silence)

    def test_pcm16_normalization_handles_full_negative_scale(self) -> None:
        result = summarize_s16([-32768, 16384, 0, -16384])
        self.assertEqual(result.nonzero, 3)
        self.assertEqual(result.peak, 1.0)
        self.assertAlmostEqual(result.mean_square, 0.375)
        self.assertEqual(result.nonfinite, 0)
        self.assertFalse(result.is_full_silence)

    def test_exact_scalar_limit_is_complete(self) -> None:
        result = summarize_f32([0.0] * VALUE_LIMIT)
        self.assertEqual(result.status, InspectionStatus.COMPLETE)
        self.assertEqual(result.inspected_values, VALUE_LIMIT)
        self.assertTrue(result.is_full_silence)

    def test_scalar_overflow_is_truncated_and_cannot_claim_silence(self) -> None:
        values = [0.0] * VALUE_LIMIT + [0.5]
        result = summarize_f32(values)
        self.assertEqual(result.status, InspectionStatus.TRUNCATED)
        self.assertEqual(result.total_values, VALUE_LIMIT + 1)
        self.assertEqual(result.inspected_values, VALUE_LIMIT)
        self.assertEqual(result.nonzero, 0)
        self.assertFalse(result.is_full_silence)

    def test_empty_complete_inspection_cannot_claim_silence(self) -> None:
        result = summarize_f32([])
        self.assertEqual(result.status, InspectionStatus.COMPLETE)
        self.assertFalse(result.is_full_silence)

    def test_invalid_inspection_cannot_claim_silence(self) -> None:
        result = result_for_invalid()
        self.assertEqual(result.status, InspectionStatus.INVALID)
        self.assertEqual(result.inspection_hr, MF_E_INVALIDMEDIATYPE)
        self.assertFalse(result.is_full_silence)

    def test_failed_inspection_preserves_hresult_and_cannot_claim_silence(self) -> None:
        result = result_for_failure()
        self.assertEqual(result.status, InspectionStatus.FAILED)
        self.assertEqual(result.inspection_hr, E_FAIL)
        self.assertFalse(result.is_full_silence)

    def test_failure_requires_failing_hresult(self) -> None:
        with self.assertRaises(ValueError):
            result_for_failure(S_OK)
        with self.assertRaises(ValueError):
            result_for_failure(S_FALSE)

    def test_unsupported_inspection_cannot_claim_silence(self) -> None:
        result = result_for_unsupported()
        self.assertEqual(result.status, InspectionStatus.UNSUPPORTED)
        self.assertEqual(result.inspection_hr, MF_E_INVALIDMEDIATYPE)
        self.assertFalse(result.is_full_silence)

    def test_multibuffer_inspection_is_unsupported_without_claiming_silence(self) -> None:
        result = result_for_unsupported_buffers()
        self.assertEqual(result.status, InspectionStatus.UNSUPPORTED_BUFFERS)
        self.assertEqual(result.inspection_hr, E_NOTIMPL)
        self.assertEqual(result.inspected_values, 0)
        self.assertFalse(result.is_full_silence)

    def test_sample_attempts_increment_and_stop_at_limit(self) -> None:
        counter = ProbeCounter()
        self.assertEqual(sum(counter.begin_attempt() for _ in range(64)), SAMPLE_LIMIT)
        self.assertEqual(counter.count, SAMPLE_LIMIT)

    def test_failed_attempt_still_consumes_one_bounded_slot(self) -> None:
        counter = ProbeCounter()
        self.assertTrue(counter.begin_attempt())
        self.assertEqual(result_for_failure().status, InspectionStatus.FAILED)
        self.assertEqual(counter.count, 1)

    def test_effect_input_is_inspected_and_unlocked_before_process_then_traced(self) -> None:
        record = effect_input_attempt(ProbeCounter(), summarize_f32([0.5, -0.25]), S_OK)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.stages,
                         ("inspect", "unlock", "process_input", "combined_trace"))
        self.assertTrue(record.accepted)
        self.assertFalse(record.queue_attempted)
        self.assertEqual(record.queue_hr, S_FALSE)
        self.assertEqual(record.delivery_hr, S_OK)
        self.assertTrue(record.process_valid)
        self.assertTrue(record.delivery_valid)
        self.assertGreater(record.inspection.nonzero, 0)

    def test_decoder_output_marks_process_and_delivery_results_invalid(self) -> None:
        record = decoder_output_trace(summarize_f32([0.25]), "mf-session-a")
        self.assertEqual(record.session_identity, "mf-session-a")
        self.assertFalse(record.process_valid)
        self.assertEqual(record.process_hr, E_NOTIMPL)
        self.assertFalse(record.delivery_valid)
        self.assertEqual(record.delivery_hr, E_NOTIMPL)

    def test_effect_trace_retains_opaque_session_identity_for_cross_channel_join(self) -> None:
        record = effect_input_attempt(ProbeCounter(), summarize_f32([0.25]), S_OK,
                                      session_identity="mf-session-a")
        assert record is not None
        self.assertEqual(record.session_identity, "mf-session-a")

    def test_not_accepting_preserves_raw_hresult_and_successful_queue_outcome(self) -> None:
        record = effect_input_attempt(ProbeCounter(), summarize_f32([0.25]),
                                      MF_E_NOTACCEPTING, S_OK)
        assert record is not None
        self.assertEqual(record.stages,
                         ("inspect", "unlock", "process_input", "queue", "combined_trace"))
        self.assertEqual(record.process_hr, MF_E_NOTACCEPTING)
        self.assertFalse(record.accepted)
        self.assertTrue(record.queue_attempted)
        self.assertTrue(record.queued)
        self.assertEqual(record.queue_hr, S_OK)
        self.assertEqual(record.delivery_hr, S_OK)

    def test_nonzero_success_hresult_is_still_accepted(self) -> None:
        record = effect_input_attempt(ProbeCounter(), summarize_f32([0.25]), S_FALSE)
        assert record is not None
        self.assertTrue(record.accepted)
        self.assertFalse(record.queue_attempted)
        self.assertEqual(record.delivery_hr, S_FALSE)

    def test_failed_queue_is_distinct_from_no_queue_attempt(self) -> None:
        record = effect_input_attempt(ProbeCounter(), summarize_f32([0.25]),
                                      MF_E_NOTACCEPTING, E_FAIL)
        assert record is not None
        self.assertTrue(record.queue_attempted)
        self.assertFalse(record.queued)
        self.assertEqual(record.queue_hr, E_FAIL)
        self.assertEqual(record.delivery_hr, E_FAIL)

    def test_failed_inspection_does_not_suppress_process_input(self) -> None:
        record = effect_input_attempt(ProbeCounter(), result_for_failure(), S_OK)
        assert record is not None
        self.assertEqual(record.inspection.status, InspectionStatus.FAILED)
        self.assertIn("process_input", record.stages)
        self.assertTrue(record.accepted)

    def test_effect_input_attempts_share_the_same_hard_limit_on_all_outcomes(self) -> None:
        counter = ProbeCounter()
        records = [
            effect_input_attempt(counter, result_for_failure(),
                                 MF_E_NOTACCEPTING, S_OK)
            for _ in range(SAMPLE_LIMIT * 2)
        ]
        self.assertEqual(sum(record is not None for record in records), SAMPLE_LIMIT)
        self.assertEqual(counter.count, SAMPLE_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
