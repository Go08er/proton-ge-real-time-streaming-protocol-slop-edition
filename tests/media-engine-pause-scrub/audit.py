#!/usr/bin/env python3
"""Audit prepared Wine source for A3.8 pause/scrub ordering."""

from __future__ import annotations

import argparse
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"media-engine pause/scrub audit failed: {message}")


def region(text: str, start: str, end: str) -> str:
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError as exc:
        fail(f"could not isolate source region: {exc}")
    return text[begin:finish]


def require(body: str, needle: str, message: str) -> None:
    if needle not in body:
        fail(message)


def require_order(body: str, *needles: str) -> None:
    try:
        positions = [body.index(needle) for needle in needles]
    except ValueError as exc:
        fail(f"required ordering term is absent: {exc}")
    if positions != sorted(positions):
        fail(f"required order changed: {' -> '.join(needles)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument(
        "--diagnostic-contract",
        choices=("present", "absent"),
        required=True,
    )
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    source_path = source_root / "dlls" / "mfmediaengine" / "main.c"
    if not source_path.is_file():
        fail(f"missing source file: {source_path}")
    text = source_path.read_text(encoding="utf-8")

    events = region(text, "static HRESULT WINAPI media_engine_session_events_Invoke(",
                    "static const IMFAsyncCallbackVtbl media_engine_session_events_vtbl")
    rate = region(events, "case MESessionRateChanged:",
                  "case MESessionScrubSampleComplete:")
    paused = region(events, "case MESessionPaused:", "    }\n\nfailed:")
    play = region(text, "static HRESULT WINAPI media_engine_Play(",
                  "static HRESULT WINAPI media_engine_Pause(")
    pause = region(text, "static HRESULT WINAPI media_engine_Pause(",
                   "static BOOL WINAPI media_engine_GetMuted(")
    autoplay = region(text, "static HRESULT WINAPI media_engine_SetAutoPlay(",
                      "static BOOL WINAPI media_engine_GetLoop(")

    pending_clear = (
        "media_engine_set_flag(engine, "
        "FLAGS_ENGINE_PLAY_PENDING | FLAGS_ENGINE_WAITING, FALSE);"
    )

    require(pause, pending_clear,
            "Pause can leave stale waiting/play scheduling state")
    require_order(
        pause,
        "engine->playback_requested = FALSE;",
        pending_clear,
        "if (engine->flags & FLAGS_ENGINE_PAUSE_PENDING)",
    )
    require(paused, pending_clear,
            "pause completion can leave stale waiting/play scheduling state")
    require(rate, pending_clear,
            "rate completion can leave stale waiting/play scheduling state")
    require(paused, "scrubbing = !!(engine->flags & FLAGS_ENGINE_SCRUBBING);",
            "pause completion does not snapshot scrub ownership")
    require_order(
        paused,
        "if (scrubbing)",
        "if (engine->flags & FLAGS_ENGINE_PAUSE_PENDING)",
        "media_engine_set_flag(engine, FLAGS_ENGINE_PAUSE_PENDING, FALSE);",
        "if (!engine->playback_requested)",
    )
    if "else if (engine->flags & FLAGS_ENGINE_PAUSE_PENDING)" in paused:
        fail("pending pause is still skipped while scrubbing")
    require(paused, "else if (scrubbing)",
            "latest Play is not deferred to the scrub rate completion")
    require_order(
        paused,
        "else if (scrubbing)",
        "media_engine_set_flag(engine, FLAGS_ENGINE_PLAY_PENDING, TRUE);",
        "media_engine_set_flag(engine, FLAGS_ENGINE_PAUSED, FALSE);",
    )
    require(paused, "start_pending = TRUE;",
            "non-scrub Play-after-Pause cannot restart")

    require_order(
        rate,
        "media_engine_set_flag(engine, FLAGS_ENGINE_SCRUBBING, FALSE);",
        "if (!engine->playback_requested)",
        "else if (engine->flags & FLAGS_ENGINE_PAUSE_PENDING)",
        "else if (engine->flags & FLAGS_ENGINE_PLAY_PENDING)",
    )
    require(rate, "media_engine_start_playback(engine);",
            "rate completion has no guarded restart")
    q_clear = "media_engine_set_flag(engine, FLAGS_ENGINE_PAUSE_PENDING, FALSE);"
    if q_clear in rate:
        fail("rate completion clears a possibly newer pending pause")

    require(play, "engine->playback_requested = TRUE;",
            "Play does not record latest caller intent")
    require(autoplay,
            "media_engine_set_flag(engine, FLAGS_ENGINE_AUTO_PLAY, autoplay);",
            "SetAutoPlay no longer preserves GE's property-only behavior")
    if "IMFMediaEngineEx_Play(iface)" in autoplay or "autoplay_armed" in text:
        fail("A3.8 unexpectedly broadens property-only autoplay behavior")

    trace_markers = (
        "call Play",
        "call Pause",
        "call SetAutoPlay",
        "event MESessionPaused",
        "event MESessionRateChanged",
    )
    if args.diagnostic_contract == "present":
        for marker in trace_markers:
            require(text, marker, f"bounded A3.8 transition trace lacks {marker}")
    else:
        for marker in (
            "WINE_DECLARE_DEBUG_CHANNEL(avprostate);",
            "TRACE_(avprostate)",
            *trace_markers,
        ):
            if marker in text:
                fail(f"diagnostic-free source retains {marker}")

    print("media-engine pause/scrub source audit passed")


if __name__ == "__main__":
    main()
