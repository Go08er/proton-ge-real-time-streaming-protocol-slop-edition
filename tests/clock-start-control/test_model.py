#!/usr/bin/env python3
"""Deterministic model of the A3.6 presentation-clock start policy."""

from __future__ import annotations

from dataclasses import dataclass, field
import unittest


CURRENT = "current"
INVALID = "invalid"
STOPPED = "stopped"
PAUSED = "paused"
RUNNING = "running"


@dataclass
class ClockModel:
    clock_started: bool = False
    clock_state: str = STOPPED
    start_succeeds: bool = True
    stop_succeeds: bool = True
    operations: list[tuple[str, int | str | None]] = field(default_factory=list)

    def start(self, requested: int | str) -> bool:
        offset = requested
        if requested == CURRENT and not self.clock_started:
            if self.clock_state not in (INVALID, STOPPED):
                self.operations.append(("stop", None))
                if self.stop_succeeds:
                    self.clock_state = STOPPED
                    self.clock_started = False
            offset = 0

        self.operations.append(("start", offset))
        if not self.start_succeeds:
            return False

        self.clock_state = RUNNING
        self.clock_started = True
        return True

    def stop(self) -> bool:
        self.operations.append(("stop", None))
        if not self.stop_succeeds:
            return False
        self.clock_state = STOPPED
        self.clock_started = False
        return True

    def buffering_stopped(self) -> None:
        if self.clock_started:
            self.start(CURRENT)


class ClockStartTests(unittest.TestCase):
    def test_first_current_position_normalizes_to_zero(self) -> None:
        model = ClockModel()
        self.assertTrue(model.start(CURRENT))
        self.assertEqual(model.operations, [("start", 0)])
        self.assertTrue(model.clock_started)

    def test_first_current_position_from_invalid_clock_normalizes_to_zero(self) -> None:
        model = ClockModel(clock_state=INVALID)
        self.assertTrue(model.start(CURRENT))
        self.assertEqual(model.operations, [("start", 0)])

    def test_explicit_first_position_is_preserved(self) -> None:
        model = ClockModel()
        self.assertTrue(model.start(35_000_000))
        self.assertEqual(model.operations, [("start", 35_000_000)])

    def test_resume_after_real_start_keeps_current_position(self) -> None:
        model = ClockModel(clock_started=True, clock_state=PAUSED)
        self.assertTrue(model.start(CURRENT))
        self.assertEqual(model.operations, [("start", CURRENT)])

    def test_initially_paused_clock_is_stopped_then_started_at_zero(self) -> None:
        model = ClockModel(clock_started=False, clock_state=PAUSED)
        self.assertTrue(model.start(CURRENT))
        self.assertEqual(model.operations, [("stop", None), ("start", 0)])

    def test_failed_prestart_stop_still_attempts_exact_zero_start(self) -> None:
        model = ClockModel(clock_started=False, clock_state=PAUSED, stop_succeeds=False)
        self.assertTrue(model.start(CURRENT))
        self.assertEqual(model.operations, [("stop", None), ("start", 0)])
        self.assertTrue(model.clock_started)

    def test_explicit_zero_from_paused_clock_does_not_force_prestart_stop(self) -> None:
        model = ClockModel(clock_started=False, clock_state=PAUSED)
        self.assertTrue(model.start(0))
        self.assertEqual(model.operations, [("start", 0)])

    def test_failed_start_does_not_mark_clock_started(self) -> None:
        model = ClockModel(start_succeeds=False)
        self.assertFalse(model.start(CURRENT))
        self.assertFalse(model.clock_started)

    def test_successful_stop_clears_clock_started(self) -> None:
        model = ClockModel(clock_started=True, clock_state=RUNNING)
        self.assertTrue(model.stop())
        self.assertFalse(model.clock_started)
        self.assertEqual(model.clock_state, STOPPED)

    def test_failed_stop_preserves_known_started_state(self) -> None:
        model = ClockModel(clock_started=True, clock_state=RUNNING, stop_succeeds=False)
        self.assertFalse(model.stop())
        self.assertTrue(model.clock_started)
        self.assertEqual(model.clock_state, RUNNING)

    def test_buffering_stop_does_not_start_never_started_clock(self) -> None:
        model = ClockModel(clock_started=False, clock_state=PAUSED)
        model.buffering_stopped()
        self.assertEqual(model.operations, [])

    def test_buffering_stop_resumes_an_established_clock(self) -> None:
        model = ClockModel(clock_started=True, clock_state=PAUSED)
        model.buffering_stopped()
        self.assertEqual(model.operations, [("start", CURRENT)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
