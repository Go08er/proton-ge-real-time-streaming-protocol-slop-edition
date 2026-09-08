#!/usr/bin/env python3
"""Model AVPro's MediaEngine pause/seek audio-silence gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
import itertools
import unittest


S_OK = 0
E_FAIL = 0x80004005


@dataclass(frozen=True)
class TraceIdentity:
    engine: str
    session: str


DEFAULT_IDENTITY = TraceIdentity("engine-0", "session-0")


@dataclass(frozen=True)
class EngineState:
    paused: bool = False
    real_seek: bool = False
    deferred_seek: bool = False

    @property
    def is_seeking(self) -> bool:
        """Pinned GE reports both real and internally deferred seeks publicly."""
        return self.real_seek or self.deferred_seek

    @property
    def avpro_zero_fills(self) -> bool:
        """The audited AudioGrabber gate is IsPaused() || IsSeeking()."""
        return self.paused or self.is_seeking


@dataclass(frozen=True)
class StateTrace:
    operation: str
    identity: TraceIdentity
    flags_before: EngineState
    flags_after: EngineState
    hr: int
    deferred_flush_attempted: bool = False
    deferred_flush_hr: int = S_OK
    stages: tuple[str, ...] = ("enter_lock", "snapshot", "leave_lock", "emit_trace")

    @property
    def emitted_after_unlock(self) -> bool:
        return self.stages.index("leave_lock") < self.stages.index("emit_trace")


def set_current_time_ex_deferred(state: EngineState,
                                 identity: TraceIdentity = DEFAULT_IDENTITY
                                 ) -> tuple[EngineState, StateTrace]:
    """Model a running SetCurrentTimeEx call which enters GE's coalescing window."""
    after = replace(state, deferred_seek=True)
    return after, StateTrace("SetCurrentTimeEx", identity, state, after, S_OK)


def flush_deferred_seek(state: EngineState, start_hr: int = S_OK,
                        identity: TraceIdentity = DEFAULT_IDENTITY
                        ) -> tuple[EngineState, StateTrace]:
    """GE clears deferred first, then marks a real seek only if Session Start succeeds."""
    if not state.deferred_seek:
        return state, StateTrace("GetCurrentTime", identity, state, state,
                                 S_OK, False, S_OK)

    after = replace(state, deferred_seek=False, real_seek=start_hr == S_OK)
    return after, StateTrace("GetCurrentTime", identity, state, after,
                             start_hr, True, start_hr)


def observe_state(operation: str, state: EngineState,
                  identity: TraceIdentity = DEFAULT_IDENTITY) -> StateTrace:
    """Represent a read-only API trace built from one in-lock state snapshot."""
    return StateTrace(operation, identity, state, state, S_OK)


def complete_session_start(state: EngineState) -> EngineState:
    """MESessionStarted clears the real in-flight seek state."""
    return replace(state, real_seek=False)


class AvproAudioStateTests(unittest.TestCase):
    def test_zero_fill_gate_matches_all_pause_and_seek_combinations(self) -> None:
        for paused, real_seek, deferred_seek in itertools.product((False, True), repeat=3):
            with self.subTest(paused=paused, real_seek=real_seek,
                              deferred_seek=deferred_seek):
                state = EngineState(paused, real_seek, deferred_seek)
                self.assertEqual(state.avpro_zero_fills,
                                 paused or real_seek or deferred_seek)

    def test_deferred_only_state_is_publicly_seeking_and_silences_grabber(self) -> None:
        state = EngineState(deferred_seek=True)
        self.assertTrue(state.is_seeking)
        self.assertTrue(state.avpro_zero_fills)
        self.assertFalse(state.real_seek)

    def test_no_flags_leave_audio_capture_open(self) -> None:
        state = EngineState()
        self.assertFalse(state.is_seeking)
        self.assertFalse(state.avpro_zero_fills)

    def test_deferred_to_real_to_complete_transition_is_explicit(self) -> None:
        deferred, set_trace = set_current_time_ex_deferred(EngineState())
        self.assertEqual((set_trace.flags_before, set_trace.flags_after),
                         (EngineState(), deferred))
        self.assertTrue(deferred.avpro_zero_fills)

        real, flush_trace = flush_deferred_seek(deferred)
        self.assertTrue(flush_trace.deferred_flush_attempted)
        self.assertEqual(flush_trace.deferred_flush_hr, S_OK)
        self.assertEqual(real, EngineState(real_seek=True))
        self.assertTrue(real.avpro_zero_fills)

        complete = complete_session_start(real)
        self.assertEqual(complete, EngineState())
        self.assertFalse(complete.avpro_zero_fills)

    def test_failed_deferred_flush_does_not_claim_real_seek(self) -> None:
        before = EngineState(deferred_seek=True)
        after, trace = flush_deferred_seek(before, E_FAIL)
        self.assertTrue(trace.deferred_flush_attempted)
        self.assertEqual((trace.hr, trace.deferred_flush_hr), (E_FAIL, E_FAIL))
        self.assertEqual(after, EngineState())
        self.assertFalse(after.is_seeking)

    def test_get_current_time_without_due_deferred_seek_has_no_flush(self) -> None:
        state = EngineState()
        after, trace = flush_deferred_seek(state)
        self.assertEqual(after, state)
        self.assertFalse(trace.deferred_flush_attempted)
        self.assertEqual(trace.flags_before, trace.flags_after)

    def test_pause_remains_an_independent_silence_owner_after_seek_completion(self) -> None:
        state = complete_session_start(EngineState(paused=True, real_seek=True))
        self.assertEqual(state, EngineState(paused=True))
        self.assertFalse(state.is_seeking)
        self.assertTrue(state.avpro_zero_fills)

    def test_state_trace_is_observational_and_retains_both_snapshots(self) -> None:
        before = EngineState(real_seek=True)
        after, trace = set_current_time_ex_deferred(before)
        self.assertEqual(before, EngineState(real_seek=True))
        self.assertEqual(trace.flags_before, before)
        self.assertEqual(trace.flags_after, after)
        self.assertTrue(trace.flags_after.real_seek)
        self.assertTrue(trace.flags_after.deferred_seek)

    def test_all_traced_operations_carry_engine_and_session_identity(self) -> None:
        identity = TraceIdentity("engine-a", "session-a")
        traces = [
            observe_state("IsPaused", EngineState(), identity),
            observe_state("IsSeeking", EngineState(deferred_seek=True), identity),
            observe_state("SetCurrentTime", EngineState(), identity),
            flush_deferred_seek(EngineState(), identity=identity)[1],
            set_current_time_ex_deferred(EngineState(), identity)[1],
        ]
        self.assertEqual({trace.operation for trace in traces}, {
            "IsPaused", "IsSeeking", "SetCurrentTime", "GetCurrentTime",
            "SetCurrentTimeEx",
        })
        self.assertTrue(all(trace.identity == identity for trace in traces))

    def test_every_state_trace_snapshots_under_lock_and_emits_after_unlock(self) -> None:
        traces = [
            observe_state("IsPaused", EngineState(paused=True)),
            observe_state("IsSeeking", EngineState(real_seek=True)),
            flush_deferred_seek(EngineState(deferred_seek=True))[1],
            set_current_time_ex_deferred(EngineState())[1],
        ]
        for trace in traces:
            with self.subTest(operation=trace.operation):
                self.assertEqual(trace.stages,
                                 ("enter_lock", "snapshot", "leave_lock", "emit_trace"))
                self.assertTrue(trace.emitted_after_unlock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
