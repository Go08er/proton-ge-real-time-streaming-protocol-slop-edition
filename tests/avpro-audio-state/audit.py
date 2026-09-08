#!/usr/bin/env python3
"""Audit the normally-off A3.7 AVPro-visible MediaEngine state trace."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sys


def fail(message: str) -> None:
    raise SystemExit(f"avpro-audio-state audit failed: {message}")


def require(text: str, needle: str, message: str) -> None:
    if needle not in text:
        fail(message)


def region(text: str, start: str, end: str) -> str:
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError as exc:
        fail(f"could not isolate source region: {exc}")
    return text[begin:finish]


def require_trace_snapshot(method: str, *fields: str) -> None:
    require(method, "TRACE_(avprostate)", "method lacks the dedicated state trace")
    for field in fields:
        require(method, field, f"state trace omits {field}")
    trace_at = method.index("TRACE_(avprostate)")
    unlock_at = method.rfind("LeaveCriticalSection")
    if unlock_at > trace_at:
        fail("state trace is emitted while the MediaEngine lock is still held")
    before_unlock = method[:unlock_at]
    invocation = method[trace_at:]
    require(before_unlock, "session = engine->session;",
            "Media Session identity is not snapshotted under the lock")
    require(before_unlock, "source_generation = engine->source_generation;",
            "source generation is not snapshotted under the lock")
    if "engine->" in invocation:
        fail("post-unlock trace dereferences live MediaEngine state")
    require(invocation, "engine %p, session %p",
            "trace lacks paired engine/session correlation identities")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    source_root = Path(sys.argv[1]).resolve()
    source_path = source_root / "dlls" / "mfmediaengine" / "main.c"
    if not source_path.is_file():
        fail(f"missing source file: {source_path}")
    text = source_path.read_text(encoding="utf-8")

    require(text, "WINE_DECLARE_DEBUG_CHANNEL(avprostate);",
            "diagnostic does not use a dedicated normally-off channel")

    engine = region(text, "struct media_engine\n{", "\n};")
    if "avpro" in engine.lower():
        fail("diagnostic adds persistent AVPro-specific MediaEngine state")

    is_seeking = region(text, "static BOOL WINAPI media_engine_IsSeeking(",
                        "static double WINAPI media_engine_GetCurrentTime(")
    require(is_seeking, "FLAGS_ENGINE_SEEKING | FLAGS_ENGINE_DEFERRED_SEEK",
            "public seeking state no longer reflects the pinned GE behavior under test")
    require_trace_snapshot(is_seeking, "engine %p", "session %p", "source_gen", "return", "flags",
                           "real", "deferred")
    require(is_seeking, "unsigned int flags;",
            "IsSeeking flag snapshot does not preserve engine->flags format type")
    if is_seeking.index("value =") > is_seeking.index("TRACE_(avprostate)"):
        fail("IsSeeking is traced before its return value is computed")

    get_time = region(text, "static double WINAPI media_engine_GetCurrentTime(",
                      "static HRESULT media_engine_set_current_time(")
    require(get_time, "media_engine_flush_deferred_seek(engine)",
            "GetCurrentTime no longer exposes its deferred-seek flush transition")
    require_trace_snapshot(get_time, "engine %p", "session %p", "source_gen",
                           "flags_before", "flags_after",
                           "flush_attempted", "flush_hr", "deferred_work_before",
                           "deferred_work_after", "return")
    require(get_time, "unsigned int flags_before, flags_after;",
            "GetCurrentTime flag snapshots do not preserve engine->flags format type")
    flush_call = get_time.index("media_engine_flush_deferred_seek(engine)")
    state_trace = get_time.index("TRACE_(avprostate)")
    if flush_call > state_trace:
        fail("GetCurrentTime trace precedes a possible deferred-seek flush")

    is_paused = region(text, "static BOOL WINAPI media_engine_IsPaused(",
                       "static double WINAPI media_engine_GetDefaultPlaybackRate(")
    require(is_paused, "FLAGS_ENGINE_PAUSED",
            "IsPaused no longer reports the MediaEngine paused flag")
    require_trace_snapshot(is_paused, "engine %p", "session %p", "source_gen", "return", "flags")
    require(is_paused, "unsigned int flags;",
            "IsPaused flag snapshot does not preserve engine->flags format type")
    if is_paused.index("value =") > is_paused.index("TRACE_(avprostate)"):
        fail("IsPaused is traced before its return value is computed")

    set_time_ex = region(text, "static HRESULT WINAPI media_engine_SetCurrentTimeEx(",
                         "static HRESULT WINAPI media_engine_EnableTimeUpdateTimer(")
    require(set_time_ex, "media_engine_defer_current_time(engine, seektime)",
            "SetCurrentTimeEx no longer uses GE's deferred-seek path")
    require_trace_snapshot(set_time_ex, "engine %p", "session %p", "source_gen",
                           "flags_before", "flags_after",
                           "deferred_work_before", "deferred_work_after", "target",
                           "mode", "hr")
    require(set_time_ex, "unsigned int flags_before, flags_after;",
            "SetCurrentTimeEx flag snapshots do not preserve engine->flags format type")
    if set_time_ex.index("media_engine_defer_current_time(engine, seektime)") > set_time_ex.index("TRACE_(avprostate)"):
        fail("SetCurrentTimeEx trace precedes the state transition and HRESULT")

    defer = region(text, "static HRESULT media_engine_defer_current_time(",
                   "static HRESULT WINAPI media_engine_SetCurrentTime(")
    require(defer, "engine->deferred_seek = seektime",
            "deferred target is not retained")
    require(defer, "media_engine_set_flag(engine, FLAGS_ENGINE_DEFERRED_SEEK, TRUE)",
            "deferred flag is not set when coalescing a seek")
    require(defer, "media_engine_schedule_deferred_seek(engine, 250)",
            "deferred seek work is not scheduled")

    flush = region(text, "static HRESULT media_engine_flush_deferred_seek(struct media_engine *engine)\n{",
                   "static HRESULT media_engine_schedule_deferred_seek(")
    require(flush, "media_engine_set_flag(engine, FLAGS_ENGINE_DEFERRED_SEEK, FALSE)",
            "deferred flag is not cleared before committing the target")
    require(flush, "media_engine_set_current_time(engine, seektime)",
            "deferred target is not forwarded to the real seek path")
    if flush.index("FLAGS_ENGINE_DEFERRED_SEEK, FALSE") > flush.index("media_engine_set_current_time(engine, seektime)"):
        fail("deferred flag is cleared after rather than before real seek dispatch")

    set_time = region(text,
                      "static HRESULT media_engine_set_current_time(struct media_engine *engine, double seektime)\n{",
                      "static HRESULT media_engine_flush_deferred_seek(struct media_engine *engine)\n{")
    require(set_time, "IMFMediaSession_Start(engine->session", "real seek does not start the session")
    require(set_time, "media_engine_set_flag(engine, FLAGS_ENGINE_SEEKING, TRUE)",
            "successful positioned Start does not mark the real seek in flight")
    if set_time.index("IMFMediaSession_Start(engine->session") > set_time.index("FLAGS_ENGINE_SEEKING, TRUE"):
        fail("real seeking is advertised before Session Start succeeds")

    events = region(text, "static HRESULT WINAPI media_engine_session_events_Invoke(",
                    "static const IMFAsyncCallbackVtbl media_engine_session_events_vtbl")
    require(events, "case MESessionStarted:", "session-start completion handling is absent")
    started = region(events, "case MESessionStarted:", "case MESessionEnded:")
    require(started, "media_engine_set_flag(engine, FLAGS_ENGINE_SEEKING | FLAGS_ENGINE_IS_ENDED, FALSE)",
            "session-start completion does not clear the real seek flag")

    set_time_public = region(text, "static HRESULT WINAPI media_engine_SetCurrentTime(",
                             "static double WINAPI media_engine_GetStartTime(")
    require_trace_snapshot(set_time_public, "engine %p", "session %p", "source_gen",
                           "flags_before", "flags_after", "deferred_work_before",
                           "deferred_work_after", "target", "hr")
    require(set_time_public, "unsigned int flags_before, flags_after;",
            "SetCurrentTime flag snapshots do not preserve engine->flags format type")

    methods = (is_seeking, get_time, is_paused, set_time_ex, set_time_public)
    invocations = re.findall(r"TRACE_\(avprostate\)\((.*?)\);", text, re.DOTALL)
    audited_invocations: list[str] = []
    for method in methods:
        method_invocations = re.findall(
            r"TRACE_\(avprostate\)\((.*?)\);", method, re.DOTALL
        )
        if len(method_invocations) != 1:
            fail("each audited audio-state API method must emit exactly one avprostate snapshot")
        audited_invocations.extend(method_invocations)

    # A3.8 adds a bounded, all-or-nothing set of caller/event ordering records
    # on the same normally-off channel.  Keep the original five audio-state
    # snapshots exact, reject arbitrary new emissions, and subject both sets to
    # the same identity and privacy checks below.
    remaining = list((Counter(invocations) - Counter(audited_invocations)).elements())
    transition_markers = (
        "call SetAutoPlay",
        "call Play",
        "call Pause",
        "event MESessionPaused",
        "event MESessionRateChanged",
    )
    if remaining:
        if len(remaining) != len(transition_markers):
            fail("A3.8 transition trace set must contain exactly five additional emissions")
        for marker in transition_markers:
            if sum(marker in invocation for invocation in remaining) != 1:
                fail(f"A3.8 transition trace set does not contain exactly one {marker} record")
        for invocation in remaining:
            if sum(marker in invocation for marker in transition_markers) != 1:
                fail("an additional avprostate emission is outside the bounded A3.8 transition set")

        events = region(text, "static HRESULT WINAPI media_engine_session_events_Invoke(",
                        "static const IMFAsyncCallbackVtbl media_engine_session_events_vtbl")
        transition_regions = (
            (
                region(text, "static HRESULT WINAPI media_engine_SetAutoPlay(",
                       "static BOOL WINAPI media_engine_GetLoop("),
                ("call SetAutoPlay", "requested", "playback_requested",
                 "flags_before", "flags_after"),
            ),
            (
                region(text, "static HRESULT WINAPI media_engine_Play(",
                       "static HRESULT WINAPI media_engine_Pause("),
                ("call Play", "hr", "playback_requested",
                 "flags_before", "flags_after"),
            ),
            (
                region(text, "static HRESULT WINAPI media_engine_Pause(",
                       "static BOOL WINAPI media_engine_GetMuted("),
                ("call Pause", "hr", "playback_requested",
                 "flags_before", "flags_after"),
            ),
            (
                region(events, "case MESessionRateChanged:",
                       "case MESessionScrubSampleComplete:"),
                ("event MESessionRateChanged", "rate", "started",
                 "playback_requested", "flags_before", "flags_after"),
            ),
            (
                region(events, "case MESessionPaused:", "    }\n\nfailed:"),
                ("event MESessionPaused", "scrubbing", "started",
                 "playback_requested", "flags_before", "flags_after"),
            ),
        )
        for transition_region, fields in transition_regions:
            require_trace_snapshot(transition_region, *fields)
    elif len(invocations) != len(methods):
        fail("the original audio-state trace set contains an unrecognized emission")

    if len(invocations) != len(audited_invocations) + len(remaining):
        fail("an avprostate emission was not assigned to an audited trace set")
    trace_text = "\n".join(invocations).lower()
    for invocation in invocations:
        if "engine %p, session %p" not in invocation:
            fail("an avprostate line lacks engine/session correlation identities")
        if invocation.count("%p") != 2:
            fail("avprostate permits only the engine and Media Session pointer identities")
        if "engine->" in invocation:
            fail("an avprostate line reads live engine state after unlock")
    for forbidden in ("current_source", "debugstr_w", "url", "endpoint", "buffer", "sample"):
        if forbidden in trace_text:
            fail(f"dedicated state trace exposes unnecessary data: {forbidden}")

    print("avpro-audio-state source audit passed")


if __name__ == "__main__":
    main()
