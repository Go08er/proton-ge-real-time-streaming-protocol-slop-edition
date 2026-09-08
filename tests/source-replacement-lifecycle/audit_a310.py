#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Audit Patch 15's stopped-presentation fresh-start routing."""

from __future__ import annotations

from pathlib import Path
import sys


def fail(message: str) -> None:
    raise SystemExit(f"A3.10 replacement-start audit failed: {message}")


def region(text: str, start: str, end: str) -> str:
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError as exc:
        fail(f"could not isolate source region: {exc}")
    return text[begin:finish]


def require_order(body: str, *needles: str) -> None:
    position = -1
    for needle in needles:
        next_position = body.find(needle, position + 1)
        if next_position < 0:
            fail(f"required ordering term is absent: {needle}")
        position = next_position


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    session_path = Path(sys.argv[1]).resolve() / "dlls" / "mf" / "session.c"
    if not session_path.is_file():
        fail("prepared MediaSession source is missing")

    session = session_path.read_text(encoding="utf-8")
    session_start = region(session, "static void session_start(",
                           "static void session_set_started(")
    subscribe_sources = region(session, "static HRESULT session_subscribe_sources(",
                               "static HRESULT session_subscribe_sinks(")

    require_order(
        session_start,
        "keep_position = IsEqualGUID(time_format, &GUID_NULL) && start_position->vt == VT_EMPTY;",
        "sources_stopped = session_is_source_nodes_state(session, OBJ_STATE_STOPPED);",
        "if (session->state == SESSION_STATE_STARTED && keep_position && !sources_stopped)",
        "session_start_clock(session);",
        "return;",
        "if (sources_stopped)",
        "session->state = SESSION_STATE_STOPPED;",
        "session->clock_started = FALSE;",
        "else if (!keep_position)",
        "session->command_state = COMMAND_STATE_RESTARTING_SOURCES;",
        "IMFMediaSource_Stop(source->source)",
        "case SESSION_STATE_STOPPED:",
        "session->command_state = COMMAND_STATE_STARTING_SOURCES;",
        "if (FAILED(hr = session_subscribe_sources(session)))",
        "IMFMediaSource_Start(source->source, source->pd, &GUID_NULL, start_position)",
    )
    require_order(
        subscribe_sources,
        "if (session->presentation.flags & SESSION_FLAG_SOURCES_SUBSCRIBED)",
        "return hr;",
        "IMFMediaSource_BeginGetEvent(source->source, &session->events_callback,",
        "session->presentation.flags |= SESSION_FLAG_SOURCES_SUBSCRIBED;",
    )
    print("A3.10 replacement-start source audit passed")


if __name__ == "__main__":
    main()
