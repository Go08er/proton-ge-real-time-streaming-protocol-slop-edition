#!/usr/bin/env python3
"""Self-tests for the compiled runtime-probe evidence gate."""

from __future__ import annotations

import unittest

from parse_runtime_probe import parse_result, validate_result, validate_trace


A37_RESULT = """\
Play=0 Pause=0 paused=1 t0=0.133467 t1=0.133467 delta=0.000000 ended=1
FAIL: final Pause intent did not keep presentation paused
"""

CANDIDATE_RESULT = """\
Play=0 Pause=0 paused=1 t0=0.000000 t1=0.000000 delta=0.000000 ended=0
PASS: final Pause intent owns initial scrub completion
"""

TRACE_PREFIX = """\
winedmo_demuxer_create created demuxer 0x1 stream_count 2
media_engine_Play probe enter flags 0x881900
media_engine_Play probe leave hr 0 flags 0x8a1a00
media_engine_Pause probe enter flags 0x8a1a00
"""

TRACE_EVENTS = """\
MFCreateMediaEvent MESessionScrubSampleComplete
MFCreateMediaEvent MESessionPaused probe, 0, (null)
MFCreateMediaEvent MESessionRateChanged probe VT_R4: 1.0e+00
"""

CANDIDATE_EVENTS = """\
MFCreateMediaEvent MESessionScrubSampleComplete
MFCreateMediaEvent MESessionPaused probe, 0, (null)
event MESessionPaused, scrubbing 1, started 0, playback_requested 0, flags_before 0x981900, flags_after 0x881900
MFCreateMediaEvent MESessionRateChanged probe VT_R4: 1.0e+00
event MESessionRateChanged, rate 1, started 0, playback_requested 0, flags_before 0x881900, flags_after 0x801900
"""

A37_TRACE = (
    TRACE_PREFIX
    + "media_engine_Pause probe leave hr 0 flags 0x9a1900\n"
    + TRACE_EVENTS
    + "MFCreateMediaEvent MESessionStarted\n"
)

CANDIDATE_TRACE = (
    TRACE_PREFIX
    + "media_engine_Pause probe leave hr 0 flags 0x981900\n"
    + CANDIDATE_EVENTS
)


class RuntimeProbeParserTests(unittest.TestCase):
    def assert_invalid(self, fragment: str, callback) -> None:
        with self.assertRaisesRegex(SystemExit, fragment):
            callback()

    def test_accepts_valid_a37_negative_control(self) -> None:
        result = parse_result(A37_RESULT)
        validate_trace(A37_TRACE, "a3.7-failure")
        message = validate_result(result, "a3.7-failure", 1)
        self.assertIn("valid A3.7 negative control", message)

    def test_accepts_valid_candidate(self) -> None:
        result = parse_result(CANDIDATE_RESULT)
        validate_trace(CANDIDATE_TRACE, "candidate-pass")
        message = validate_result(result, "candidate-pass", 0)
        self.assertIn("valid candidate pass", message)

    def test_rejects_old_trace_as_candidate(self) -> None:
        self.assert_invalid(
            "candidate Pause completion retained forbidden flag",
            lambda: validate_trace(A37_TRACE, "candidate-pass"),
        )

    def test_rejects_setup_failure(self) -> None:
        self.assert_invalid(
            "probe did not reach the race",
            lambda: parse_result("setup failed 0xc0000135\n"),
        )

    def test_rejects_missing_two_stream_demux(self) -> None:
        trace = A37_TRACE.replace(
            "winedmo_demuxer_create created demuxer 0x1 stream_count 2\n", ""
        )
        self.assert_invalid(
            "two-stream A/V fixture",
            lambda: validate_trace(trace, "a3.7-failure"),
        )

    def test_rejects_trace_that_never_reaches_race(self) -> None:
        trace = "winedmo_demuxer_create created demuxer 0x1 stream_count 2\n"
        self.assert_invalid(
            "Play enter transition is absent",
            lambda: validate_trace(trace, "a3.7-failure"),
        )

    def test_rejects_candidate_stale_restart(self) -> None:
        trace = CANDIDATE_TRACE + "MFCreateMediaEvent MESessionStarted\n"
        self.assert_invalid(
            "candidate still performed a session start",
            lambda: validate_trace(trace, "candidate-pass"),
        )

    def test_rejects_candidate_stale_pause_pending(self) -> None:
        trace = CANDIDATE_TRACE.replace(
            "flags_after 0x881900", "flags_after 0x981900", 1
        )
        self.assert_invalid(
            "candidate Paused event completion retained forbidden flag",
            lambda: validate_trace(trace, "candidate-pass"),
        )

    def test_rejects_candidate_rate_restart_snapshot(self) -> None:
        trace = CANDIDATE_TRACE.replace(
            "event MESessionRateChanged, rate 1, started 0",
            "event MESessionRateChanged, rate 1, started 1",
        )
        self.assert_invalid(
            "candidate rate completion restarted playback",
            lambda: validate_trace(trace, "candidate-pass"),
        )


if __name__ == "__main__":
    unittest.main()
