#!/usr/bin/env python3
"""Audit prepared MF source for the A3.6 clock-origin contract."""

from __future__ import annotations

from pathlib import Path
import sys


def fail(message: str) -> None:
    raise SystemExit(f"clock-start audit failed: {message}")


def require(body: str, needle: str, message: str) -> None:
    if needle not in body:
        fail(message)


def region(text: str, start: str, end: str) -> str:
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError as exc:
        fail(f"could not isolate source region: {exc}")
    return text[begin:finish]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    source_root = Path(sys.argv[1]).resolve()
    source_path = source_root / "dlls" / "mf" / "session.c"
    if not source_path.is_file():
        fail(f"missing source file: {source_path}")

    text = source_path.read_text(encoding="utf-8")
    require(text, "BOOL clock_started;", "media session does not track clock startup")

    start = region(text, "static HRESULT session_start_clock(",
                   "static struct topo_node *session_get_node_object(")
    require(start,
            "if (start_offset == PRESENTATION_CURRENT_POSITION && !session->clock_started)",
            "first current-position start is not distinguished from resume")
    require(start, "IMFPresentationClock_GetState(session->clock, 0, &state)",
            "first-start correction does not inspect the existing clock state")
    require(start, "start_offset = 0;",
            "undefined first current-position start is not normalized to zero")
    require(start, "session->clock_started = TRUE;",
            "successful clock start is not recorded")

    stop = region(text, "static void session_stop(",
                  "static HRESULT session_finalize_sinks(")
    close = region(text, "static void session_close(",
                   "static void session_clear_topologies(")
    require(stop, "session->clock_started = FALSE;",
            "successful session Stop does not clear clock-started state")
    require(close, "session->clock_started = FALSE;",
            "successful session Close does not clear clock-started state")

    events = region(text, "static HRESULT WINAPI session_events_callback_Invoke(",
                    "static const IMFAsyncCallbackVtbl session_events_callback_vtbl")
    require(events, "else if (session->clock_started)\n"
                    "                    IMFPresentationClock_Start(session->clock, PRESENTATION_CURRENT_POSITION);",
            "buffering-stop can start a never-started presentation clock")

    print("clock-start source audit passed")


if __name__ == "__main__":
    main()
