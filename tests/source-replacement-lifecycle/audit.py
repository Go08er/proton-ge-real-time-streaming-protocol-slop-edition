#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Audit prepared Wine source for Patch 14 start-position ownership."""

from __future__ import annotations

from pathlib import Path
import sys


def fail(message: str) -> None:
    raise SystemExit(f"source-replacement lifecycle audit failed: {message}")


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
    position = -1
    for needle in needles:
        next_position = body.find(needle, position + 1)
        if next_position < 0:
            fail(f"required ordering term is absent: {needle}")
        if next_position <= position:
            fail(f"required order changed at: {needle}")
        position = next_position


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    source_root = Path(sys.argv[1]).resolve()
    engine_path = source_root / "dlls" / "mfmediaengine" / "main.c"
    session_path = source_root / "dlls" / "mf" / "session.c"
    if not engine_path.is_file() or not session_path.is_file():
        fail("prepared mfmediaengine or MediaSession source is missing")

    engine = engine_path.read_text(encoding="utf-8")
    session = session_path.read_text(encoding="utf-8")
    presentation = region(engine, "    BSTR current_source;", "    struct effects video_effects;")
    clear = region(engine, "static void media_engine_clear_presentation(",
                   "static void media_engine_clear_effects(")
    start = region(engine, "static void media_engine_start_playback(",
                   "static inline struct media_engine_load_context")
    set_source = region(engine, "static HRESULT media_engine_set_source(",
                        "static HRESULT WINAPI media_engine_Play(")
    session_start = region(session, "static void session_start(",
                           "static void session_set_started(")
    started_guard = "if (session->state == SESSION_STATE_STARTED && keep_position)"
    if started_guard not in session_start:
        started_guard = (
            "if (session->state == SESSION_STATE_STARTED && keep_position "
            "&& !sources_stopped)"
        )

    require(presentation, "Pending MediaSession start state; always VT_EMPTY or VT_I8.",
            "pending start-position ownership is undocumented")
    require_order(
        clear,
        "IMFPresentationDescriptor *pd = engine->presentation.pd;",
        "struct video_frame_sink *frame_sink = engine->presentation.frame_sink;",
        "IMFMediaSource *source = engine->presentation.source;",
        "engine->presentation.source = NULL;",
        "engine->presentation.pd = NULL;",
        "engine->presentation.frame_sink = NULL;",
        "LeaveCriticalSection(&engine->cs);",
        "IMFMediaSource_Shutdown(source);",
        "EnterCriticalSection(&engine->cs);",
    )
    if "memset(&engine->presentation" in clear:
        fail("presentation cleanup still erases pending value state")
    if "start_position =" in clear or "start_position.vt =" in clear:
        fail("presentation cleanup writes current-generation start state")
    require(clear, "start_position is pending value state owned by the current source",
            "clear path does not identify current-generation value ownership")

    require_order(
        set_source,
        "generation = ++engine->source_generation;",
        "engine->presentation.start_position.vt = VT_I8;",
        "engine->presentation.start_position.hVal.QuadPart = 0;",
        "media_engine_set_flag(engine, FLAGS_ENGINE_SOURCE_PENDING, TRUE);",
    )
    require_order(
        start,
        "IMFMediaSession_Start(engine->session, &GUID_NULL, &engine->presentation.start_position);",
        "engine->presentation.start_position.vt = VT_EMPTY;",
    )

    require_order(
        session_start,
        "keep_position = IsEqualGUID(time_format, &GUID_NULL) && start_position->vt == VT_EMPTY;",
        started_guard,
        "session_start_clock(session);",
        "return;",
        "if (!keep_position)",
        "session->command_state = COMMAND_STATE_RESTARTING_SOURCES;",
        "IMFMediaSource_Stop(source->source)",
        "case SESSION_STATE_STOPPED:",
        "session->command_state = COMMAND_STATE_STARTING_SOURCES;",
        "if (FAILED(hr = session_subscribe_sources(session)))",
        "IMFMediaSource_Start(source->source, source->pd, &GUID_NULL, start_position)",
    )
    require(session_start, "IMFMediaSource_Start(source->source, source->pd, &GUID_NULL, start_position)",
            "non-keep-position path no longer starts the installed source")
    print("source-replacement lifecycle source audit passed")


if __name__ == "__main__":
    main()
