#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Audit A3.11's physical-clock synchronization before replacement start."""

from __future__ import annotations

from pathlib import Path
import sys


def fail(message: str) -> None:
    raise SystemExit(f"A3.11 replacement-clock audit failed: {message}")


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
        position = body.find(needle, position + 1)
        if position < 0:
            fail(f"required ordering term is absent: {needle}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    session_path = Path(sys.argv[1]).resolve() / "dlls" / "mf" / "session.c"
    if not session_path.is_file():
        fail("prepared MediaSession source is missing")

    session = session_path.read_text(encoding="utf-8")
    clear_presentation = region(
        session,
        "static void session_clear_presentation(",
        "static struct topo_node *session_get_topo_node(",
    )
    session_start = region(
        session,
        "static void session_start(",
        "static void session_set_started(",
    )
    session_stop = region(
        session,
        "static void session_stop(",
        "static HRESULT session_finalize_sinks(",
    )
    sink_state = region(
        session,
        "static void session_set_sink_stream_state(",
        "static HRESULT transform_get_external_output_sample(",
    )
    require_order(
        session_start,
        "sources_stopped = session_is_source_nodes_state(session, OBJ_STATE_STOPPED);",
        "if (sources_stopped)",
        "IMFPresentationClock_GetState(session->clock, 0, &clock_state)",
        'WARN("Failed to get replacement presentation clock state %p, hr %#lx.\\n", session, hr);',
        "session_command_complete_with_event(session, MESessionStarted, hr, NULL);",
        "return;",
        "if (clock_state != MFCLOCK_STATE_INVALID)",
        "if (clock_state != MFCLOCK_STATE_STOPPED",
        "IMFPresentationClock_Stop(session->clock)",
        'WARN("Failed to stop replacement presentation clock %p, hr %#lx.\\n", session, hr);',
        "session_command_complete_with_event(session, MESessionStarted, hr, NULL);",
        "return;",
        "LIST_FOR_EACH_ENTRY(topo_node, &session->presentation.nodes, struct topo_node, entry)",
        "topo_node->type == MF_TOPOLOGY_OUTPUT_NODE",
        "topo_node->flags |= TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP;",
        "session->state = SESSION_STATE_STOPPED;",
        "session->clock_started = FALSE;",
        "case SESSION_STATE_STOPPED:",
        "session->command_state = COMMAND_STATE_STARTING_SOURCES;",
        "session_subscribe_sources(session)",
        "IMFMediaSource_Start(source->source, source->pd, &GUID_NULL, start_position)",
    )
    if session_start.count("IMFPresentationClock_Stop(session->clock)") != 1:
        fail("replacement branch must own exactly one physical-clock stop")
    require_order(
        clear_presentation,
        "LIST_FOR_EACH_ENTRY_SAFE(sink, sink2, &session->presentation.sinks",
        "list_remove(&sink->entry);",
        "IMFMediaSink_SetPresentationClock(sink->sink, NULL)",
        "IMFMediaSink_Release(sink->sink);",
    )
    require_order(
        sink_state,
        "session_get_node_object(session, (IUnknown *)stream, MF_TOPOLOGY_OUTPUT_NODE)",
        "node->flags & TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP",
        "event_type == MEStreamSinkStopped",
        "node->flags &= ~TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP;",
        'TRACE("Ignoring initial sink Stop for replacement session %p.\\n", session);',
        "return;",
        "event_type == MEStreamSinkStarted",
        "node->flags &= ~TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP;",
        "session_get_object_state_for_event(event_type)",
    )
    if session.count("TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP = 0x10") != 1:
        fail("one-shot initial-Stop node flag is absent or duplicated")
    require_order(
        session_stop,
        "IMFPresentationClock_Stop(session->clock)",
        "session->clock_started = FALSE;",
        "session->command_state = COMMAND_STATE_STOPPING_SINKS;",
    )
    print("A3.11 replacement-clock source audit passed")


if __name__ == "__main__":
    main()
