#!/usr/bin/env python3
"""Deterministic model of the state-preserving SAR audio-flush policy."""

from __future__ import annotations

from dataclasses import dataclass, field
import unittest


S_OK = 0
S_FALSE = 1
E_STOP = -1
E_RESET = -2
E_START = -3

PAUSED = "paused"
RUNNING = "running"


def failed(result: int) -> bool:
    return result < 0


@dataclass
class AudioFlushModel:
    clock_state: str = RUNNING
    rate: float = 1.0
    stop_result: int = S_OK
    reset_result: int = S_OK
    start_result: int = S_OK
    position: int = 1234
    pts: int = 5678
    start_pending: bool = False
    operations: list[str] = field(default_factory=list)

    def start_audio_client(self) -> int:
        if self.rate == 0.0:
            self.start_pending = False
            return S_OK
        if failed(self.start_result):
            self.start_pending = True
        else:
            self.start_pending = False
        return self.start_result

    def flush(self) -> int:
        restart = self.clock_state == RUNNING and self.rate != 0.0

        if restart:
            self.operations.append("stop")
            if failed(self.stop_result):
                return self.stop_result

        self.operations.append("reset")
        result = self.reset_result
        if not failed(result):
            self.position = 0
            self.pts = 0

        if restart:
            self.start_pending = True
            self.operations.append("start")
            start_result = self.start_audio_client()
            if failed(start_result) and not failed(result):
                result = start_result

        return result


class AudioFlushTests(unittest.TestCase):
    def test_running_flush_stops_resets_and_restarts(self) -> None:
        model = AudioFlushModel()
        self.assertEqual(model.flush(), S_OK)
        self.assertEqual(model.operations, ["stop", "reset", "start"])
        self.assertEqual(model.clock_state, RUNNING)
        self.assertEqual((model.position, model.pts), (0, 0))

    def test_paused_flush_only_resets(self) -> None:
        model = AudioFlushModel(clock_state=PAUSED)
        self.assertEqual(model.flush(), S_OK)
        self.assertEqual(model.operations, ["reset"])

    def test_rate_zero_running_client_was_never_started(self) -> None:
        model = AudioFlushModel(clock_state=RUNNING, rate=0.0)
        self.assertEqual(model.flush(), S_OK)
        self.assertEqual(model.operations, ["reset"])

    def test_stop_failure_does_not_reset_or_start(self) -> None:
        model = AudioFlushModel(stop_result=E_STOP)
        self.assertEqual(model.flush(), E_STOP)
        self.assertEqual(model.operations, ["stop"])
        self.assertEqual(model.clock_state, RUNNING)
        self.assertEqual((model.position, model.pts), (1234, 5678))

    def test_stop_s_false_is_success_and_flush_continues(self) -> None:
        model = AudioFlushModel(stop_result=S_FALSE)
        self.assertEqual(model.flush(), S_OK)
        self.assertEqual(model.operations, ["stop", "reset", "start"])
        self.assertEqual((model.position, model.pts), (0, 0))
        self.assertFalse(model.start_pending)

    def test_reset_failure_still_restarts_and_preserves_first_error(self) -> None:
        model = AudioFlushModel(reset_result=E_RESET)
        self.assertEqual(model.flush(), E_RESET)
        self.assertEqual(model.operations, ["stop", "reset", "start"])
        self.assertEqual(model.clock_state, RUNNING)
        self.assertEqual((model.position, model.pts), (1234, 5678))

    def test_start_failure_is_retryable_without_falsifying_clock_state(self) -> None:
        model = AudioFlushModel(start_result=E_START)
        self.assertEqual(model.flush(), E_START)
        self.assertEqual(model.operations, ["stop", "reset", "start"])
        self.assertEqual(model.clock_state, RUNNING)
        self.assertTrue(model.start_pending)

    def test_reset_error_wins_when_reset_and_restart_fail(self) -> None:
        model = AudioFlushModel(reset_result=E_RESET, start_result=E_START)
        self.assertEqual(model.flush(), E_RESET)
        self.assertEqual(model.clock_state, RUNNING)
        self.assertTrue(model.start_pending)

    def test_later_clock_start_can_retry_failed_flush_restart(self) -> None:
        model = AudioFlushModel(start_result=E_START)
        self.assertEqual(model.flush(), E_START)
        model.start_result = S_OK
        model.operations.append("start")
        self.assertEqual(model.start_audio_client(), S_OK)
        self.assertFalse(model.start_pending)
        self.assertEqual(model.clock_state, RUNNING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
