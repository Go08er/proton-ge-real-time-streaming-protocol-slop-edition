#!/usr/bin/env python3
"""Validate the compiled MediaEngine pause/scrub runtime probe.

This parser deliberately distinguishes a real A3.7 negative control from a
setup failure.  A timeout, source error, missing winedmo Unix library, or a
trace which never reaches the targeted flag combination is not evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re


PAUSED = 0x100
WAITING = 0x200
PLAY_PENDING = 0x20000
SCRUBBING = 0x80000
PAUSE_PENDING = 0x100000

RESULT_RE = re.compile(
    r"^Play=(?P<play>0x[0-9a-fA-F]+|0) "
    r"Pause=(?P<pause>0x[0-9a-fA-F]+|0) "
    r"paused=(?P<paused>[01]) "
    r"t0=(?P<t0>-?[0-9]+(?:\.[0-9]+)?) "
    r"t1=(?P<t1>-?[0-9]+(?:\.[0-9]+)?) "
    r"delta=(?P<delta>-?[0-9]+(?:\.[0-9]+)?) "
    r"ended=(?P<ended>[0-9]+)\r?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ProbeResult:
    play: int
    pause: int
    paused: bool
    t0: float
    t1: float
    delta: float
    ended: int
    passed: bool


def fail(message: str) -> None:
    raise SystemExit(f"runtime probe invalid: {message}")


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fail(f"could not read {label}: {exc}")


def parse_result(text: str) -> ProbeResult:
    setup_failures = (
        "usage failure",
        "setup failed",
        "class factory failed",
        "MFStartup failed",
        "CoInitializeEx failed",
        "race hook timed out",
    )
    for marker in setup_failures:
        if marker.lower() in text.lower():
            fail(f"probe did not reach the race ({marker})")

    match = RESULT_RE.search(text)
    if not match:
        fail("result line is absent or malformed")

    passed = "PASS: final Pause intent owns initial scrub completion" in text
    failed = "FAIL: final Pause intent did not keep presentation paused" in text
    if passed == failed:
        fail("result must contain exactly one final PASS or FAIL marker")

    return ProbeResult(
        play=int(match["play"], 0),
        pause=int(match["pause"], 0),
        paused=bool(int(match["paused"])),
        t0=float(match["t0"]),
        t1=float(match["t1"]),
        delta=float(match["delta"]),
        ended=int(match["ended"]),
        passed=passed,
    )


def require_flag(flags: int, bit: int, label: str) -> None:
    if not flags & bit:
        fail(f"{label} did not contain required flag {bit:#x} (got {flags:#x})")


def forbid_flag(flags: int, bit: int, label: str) -> None:
    if flags & bit:
        fail(f"{label} retained forbidden flag {bit:#x} (got {flags:#x})")


def find_transition(log: str, method: str, phase: str) -> re.Match[str]:
    pattern = re.compile(
        rf"media_engine_{method} .* {phase}"
        rf"(?: hr (?P<hr>(?:0x)?[0-9a-fA-F]+))? flags "
        rf"(?P<flags>0x[0-9a-fA-F]+)"
    )
    match = pattern.search(log)
    if not match:
        fail(f"{method} {phase} transition is absent from mfplat trace")
    if phase == "leave" and int(match["hr"], 0) != 0:
        fail(f"{method} returned {match['hr']} instead of S_OK")
    return match


def find_after(log: str, pattern: str, start: int, label: str) -> re.Match[str]:
    match = re.compile(pattern).search(log, start)
    if not match:
        fail(f"{label} is absent after the Play/Pause race")
    return match


def validate_candidate_event_snapshots(
    log: str, paused_event_end: int, rate_event_end: int
) -> None:
    paused = find_after(
        log,
        r"event MESessionPaused, scrubbing (?P<scrubbing>[01]), "
        r"started (?P<started>[01]), playback_requested (?P<requested>[01]), "
        r"flags_before (?P<before>0x[0-9a-fA-F]+), "
        r"flags_after (?P<after>0x[0-9a-fA-F]+)",
        paused_event_end,
        "candidate MESessionPaused state snapshot",
    )
    if paused["scrubbing"] != "1":
        fail("candidate Paused event did not reconcile the active scrub")
    if paused["started"] != "0" or paused["requested"] != "0":
        fail("candidate Paused event restarted playback despite final Pause")

    paused_before = int(paused["before"], 0)
    paused_after = int(paused["after"], 0)
    for bit in (PAUSED, SCRUBBING, PAUSE_PENDING):
        require_flag(paused_before, bit, "candidate Paused event entry")
    for bit in (PAUSED, SCRUBBING):
        require_flag(paused_after, bit, "candidate Paused event completion")
    for bit in (PAUSE_PENDING, PLAY_PENDING, WAITING):
        forbid_flag(paused_after, bit, "candidate Paused event completion")

    rate = find_after(
        log,
        r"event MESessionRateChanged, rate (?P<rate>[-+0-9.eE]+), "
        r"started (?P<started>[01]), playback_requested (?P<requested>[01]), "
        r"flags_before (?P<before>0x[0-9a-fA-F]+), "
        r"flags_after (?P<after>0x[0-9a-fA-F]+)",
        rate_event_end,
        "candidate MESessionRateChanged state snapshot",
    )
    if abs(float(rate["rate"]) - 1.0) > 0.000001:
        fail(f"candidate rate snapshot recorded {rate['rate']} instead of 1")
    if rate["started"] != "0" or rate["requested"] != "0":
        fail("candidate rate completion restarted playback despite final Pause")

    rate_before = int(rate["before"], 0)
    rate_after = int(rate["after"], 0)
    for bit in (PAUSED, SCRUBBING):
        require_flag(rate_before, bit, "candidate rate event entry")
    for bit in (PAUSE_PENDING, PLAY_PENDING, WAITING):
        forbid_flag(rate_before, bit, "candidate rate event entry")
    require_flag(rate_after, PAUSED, "candidate rate event completion")
    for bit in (SCRUBBING, PAUSE_PENDING, PLAY_PENDING, WAITING):
        forbid_flag(rate_after, bit, "candidate rate event completion")


def validate_trace(log: str, expectation: str) -> None:
    if "Failed to init unixlib" in log:
        fail("winedmo Unix library did not load")
    if not re.search(
        r"winedmo_demuxer_create created demuxer .*stream_count 2", log
    ):
        fail("the exact two-stream A/V fixture was not opened by winedmo")

    play_enter = find_transition(log, "Play", "enter")
    play_leave = find_transition(log, "Play", "leave")
    pause_enter = find_transition(log, "Pause", "enter")
    pause_leave = find_transition(log, "Pause", "leave")
    if not (
        play_enter.start()
        < play_leave.start()
        < pause_enter.start()
        < pause_leave.start()
    ):
        fail("Play/Pause transition order does not match the targeted race")

    play_enter_flags = int(play_enter["flags"], 0)
    play_leave_flags = int(play_leave["flags"], 0)
    pause_leave_flags = int(pause_leave["flags"], 0)

    require_flag(play_enter_flags, SCRUBBING, "Play entry")
    require_flag(play_enter_flags, PAUSED, "Play entry")
    require_flag(play_leave_flags, SCRUBBING, "Play completion")
    require_flag(play_leave_flags, PLAY_PENDING, "Play completion")
    forbid_flag(play_leave_flags, PAUSED, "Play completion")
    require_flag(pause_leave_flags, SCRUBBING, "Pause completion")
    require_flag(pause_leave_flags, PAUSE_PENDING, "Pause completion")
    require_flag(pause_leave_flags, PAUSED, "Pause completion")

    if expectation == "a3.7-failure":
        require_flag(pause_leave_flags, PLAY_PENDING, "A3.7 Pause completion")
        forbid_flag(pause_leave_flags, WAITING, "A3.7 Pause completion")
    else:
        forbid_flag(pause_leave_flags, PLAY_PENDING, "candidate Pause completion")
        forbid_flag(pause_leave_flags, WAITING, "candidate Pause completion")

    scrub = find_after(
        log,
        r"MFCreateMediaEvent MESessionScrubSampleComplete",
        pause_leave.end(),
        "scrub-sample completion",
    )
    paused = find_after(
        log,
        r"MFCreateMediaEvent MESessionPaused\b.*,\s*0,\s*\(null\)",
        scrub.end(),
        "successful session-pause completion",
    )
    rate = find_after(
        log,
        r"MFCreateMediaEvent MESessionRateChanged\b.*VT_R4:\s*"
        r"1(?:\.0+)?e\+00",
        paused.end(),
        "nonzero rate restoration",
    )

    if expectation == "candidate-pass":
        validate_candidate_event_snapshots(log, paused.end(), rate.end())

    starts = list(re.finditer(r"MFCreateMediaEvent MESessionStarted\b", log))
    starts_after_rate = [match for match in starts if match.start() > rate.end()]
    if expectation == "a3.7-failure" and not starts_after_rate:
        fail("A3.7 trace did not perform the stale second session start")
    if expectation == "candidate-pass" and starts_after_rate:
        fail("candidate still performed a session start after final Pause")


def validate_result(
    result: ProbeResult, expectation: str, process_exit: int | None = None
) -> str:
    if result.play != 0 or result.pause != 0:
        fail(
            "Play/Pause did not both return S_OK "
            f"({result.play:#x}/{result.pause:#x})"
        )
    if not result.paused:
        fail("public IsPaused state was false after final Pause")

    if expectation == "a3.7-failure":
        if result.passed or result.ended < 1:
            fail("A3.7 did not run the paused fixture to ENDED")
        if process_exit is not None and process_exit != 1:
            fail(f"A3.7 negative control exited {process_exit}, expected 1")
        return (
            "valid A3.7 negative control: target race reached; "
            f"paused={int(result.paused)} ended={result.ended} "
            f"t0={result.t0:.6f} t1={result.t1:.6f}"
        )

    if result.passed is False or result.ended:
        fail("candidate did not preserve final Pause without reaching ENDED")
    if abs(result.delta) > 0.050:
        fail(f"candidate clock moved by {result.delta:.6f}s while paused")
    if process_exit is not None and process_exit != 0:
        fail(f"candidate probe exited {process_exit}, expected 0")
    return (
        "valid candidate pass: target race reached; final Pause kept "
        f"the clock stationary (delta={result.delta:.6f}s)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expect",
        required=True,
        choices=("a3.7-failure", "candidate-pass"),
    )
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--process-exit", type=int)
    args = parser.parse_args()

    result = parse_result(read_text(args.result, "probe result"))
    validate_trace(read_text(args.log, "Proton trace"), args.expect)
    print(validate_result(result, args.expect, args.process_exit))


if __name__ == "__main__":
    main()
