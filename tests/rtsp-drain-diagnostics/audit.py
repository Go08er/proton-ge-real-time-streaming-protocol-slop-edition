#!/usr/bin/env python3
"""Audit the bounded, privacy-safe RTSP drain diagnostic in prepared Wine."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def fail(message: str) -> None:
    raise SystemExit(f"rtsp-drain diagnostic audit failed: {message}")


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


def trace_statements(text: str) -> list[str]:
    return re.findall(
        r"TRACE_\(rtspdrain\)\((.*?)\);",
        text,
        flags=re.DOTALL,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wine-tree", required=True, type=Path)
    args = parser.parse_args()

    wine_tree = args.wine_tree.resolve()
    source_path = wine_tree / "dlls" / "winedmo" / "media_source.c"
    sar_path = wine_tree / "dlls" / "mf" / "sar.c"
    if not source_path.is_file():
        fail(f"missing source file: {source_path}")
    if not sar_path.is_file():
        fail(f"missing source file: {sar_path}")
    text = source_path.read_text(encoding="utf-8")
    sar = sar_path.read_text(encoding="utf-8")

    required = (
        "WINE_DECLARE_DEBUG_CHANNEL(rtspdrain);",
        "#define MEDIA_SOURCE_DRAIN_DIAG_SOURCE_LIMIT 64",
        "#define MEDIA_SOURCE_DRAIN_DIAG_LIVE_RECORDS 16",
        "#define MEDIA_SOURCE_DRAIN_DIAG_STREAM_RECORDS 8",
        "records = ++source->drain_diag_live_records",
        "source->drain_diag_live_exhausted = TRUE",
        "object->drain_diag_id <= MEDIA_SOURCE_DRAIN_DIAG_SOURCE_LIMIT",
        'wcsnicmp(context->url, L"rtsp://", 7)',
        'wcsnicmp(context->url, L"rtspt://", 8)',
        "DRAIN_DIAG_QUEUE_STREAM_PACKETS",
        "DRAIN_DIAG_QUEUE_STREAM_BYTES",
        "DRAIN_DIAG_QUEUE_EMERGENCY_PACKETS",
        "DRAIN_DIAG_QUEUE_EMERGENCY_BYTES",
        "InterlockedIncrement(&stream->drain_diag_requests);",
        "InterlockedIncrement(&stream->drain_diag_dequeued);",
        "InterlockedIncrement(&stream->drain_diag_delivered);",
        "source->drain_diag_completed_bytes += data_len;",
        "media_source_drain_diag_read_result(source, status, read_elapsed, TRUE);",
    )
    for needle in required:
        require(text, needle, f"missing contract: {needle}")

    sar_required = (
        "WINE_DECLARE_DEBUG_CHANNEL(rtspdrain);",
        "#define SAR_DRAIN_DIAG_SINK_LIMIT 64",
        "InterlockedIncrement(&rtspdrain_next_sar_id)",
        "renderer->drain_diag_id <= SAR_DRAIN_DIAG_SINK_LIMIT",
        "renderer->drain_diag_clock_start_calls",
        "renderer->drain_diag_clock_start_successes",
        "renderer->drain_diag_audio_client_start_calls",
        "renderer->drain_diag_audio_client_start_successes",
        "renderer->drain_diag_process_samples",
        "renderer->drain_diag_preclock_samples",
        "renderer->drain_diag_render_callbacks",
        "renderer->drain_diag_peak_queued_frames",
        "renderer->sample_rate",
        "renderer->frame_size",
        "if (!renderer->drain_diag_clock_start_calls)",
        "renderer->queued_frames > renderer->drain_diag_peak_queued_frames",
    )
    for needle in sar_required:
        require(sar, needle, f"missing SAR contract: {needle}")

    for needle in (
        "#define MEDIA_SOURCE_MAX_QUEUED_PACKETS 8",
        "#define MEDIA_SOURCE_MAX_QUEUED_BYTES   (8 * 1024 * 1024)",
        "#define MEDIA_SOURCE_EMERGENCY_QUEUED_PACKETS 1024",
        "#define MEDIA_SOURCE_EMERGENCY_QUEUED_BYTES   (64 * 1024 * 1024)",
    ):
        require(text, needle, f"queue policy changed: {needle}")

    queue = region(
        text,
        "static BOOL media_source_stream_queues_full(",
        "/* ========================================================================\n"
        " * source_async_command COM boilerplate",
    )
    for needle in (
        "if (!any_active)\n        reason |= DRAIN_DIAG_QUEUE_IDLE;",
        "if (primed && any_packet_full)",
        "if (primed && any_byte_full)",
        "return !!reason;",
    ):
        require(queue, needle, f"queue predicate lost: {needle}")

    diagnostic = region(
        text,
        "static BOOL media_source_drain_diag_live_record(",
        "static BOOL media_source_stream_queues_full(",
    )
    for obsolete in (
        "rtspdrain_live_records",
        "rtspdrain_final_records",
        "media_source_drain_diag_final_record",
        "drain_diag_suppressed",
        "transport_bytes=",
    ):
        if obsolete in diagnostic:
            fail(f"obsolete diagnostic budget/field survived: {obsolete}")
    require(
        diagnostic,
        "if (source->drain_diag_live_exhausted)",
        "exhausted queue detail still retries its claim path",
    )
    for forbidden in (
        "InterlockedIncrement(&source->drain_diag_live",
        "InterlockedDecrement(&source->drain_diag_live",
        "InterlockedCompareExchange(&source->drain_diag_live",
    ):
        if forbidden in diagnostic:
            fail(f"exhausted live budget retains an atomic hot path: {forbidden}")

    summary = region(
        text,
        "static void media_source_drain_diag_summary(",
        "static BOOL media_source_stream_queues_full(",
    )
    require(
        summary,
        "i < MEDIA_SOURCE_DRAIN_DIAG_STREAM_RECORDS",
        "per-source stream summary lost its fixed bound",
    )
    require(summary, 'TRACE_(rtspdrain)("event=stream_summary',
            "stream summary is missing")
    require(summary, 'TRACE_(rtspdrain)("event=source_summary',
            "source summary is missing")
    for forbidden in ("live_record(source)", "final_record(source)"):
        if forbidden in summary:
            fail(f"final summary can still exhaust a record budget: {forbidden}")

    demux = region(
        text,
        "static DWORD CALLBACK source_demux_thread(",
        "/* ========================================================================\n"
        " * source_async_commands_Invoke",
    )
    require(
        demux,
        "status = winedmo_demuxer_read(source->demuxer",
        "read diagnostic no longer wraps the real WineDMO read",
    )
    require(
        demux,
        "LeaveCriticalSection(&source->demuxer_cs);\n"
        "            if (drain_diag)\n"
        "                media_source_drain_diag_read_result",
        "terminal trace can run while demuxer_cs is held",
    )
    require(
        demux,
        "LeaveCriticalSection(&source->demuxer_cs);\n"
        "        if (drain_diag)\n"
        "            media_source_drain_diag_read_result",
        "ordinary slow-read trace can run while demuxer_cs is held",
    )

    sar_summary = region(
        sar,
        "static void audio_renderer_drain_diag_summary(",
        "static ULONG WINAPI audio_renderer_sink_Release(",
    )
    require(
        sar_summary,
        "if (!renderer->drain_diag_enabled || renderer->drain_diag_summary_emitted)",
        "SAR summary lost its enable/once guard",
    )
    require(
        sar_summary,
        "renderer->drain_diag_summary_emitted = TRUE;",
        "SAR summary does not arm its guard before tracing",
    )
    require(
        sar_summary,
        'TRACE_(rtspdrain)("event=sar_summary',
        "SAR summary is missing",
    )
    for needle in (
        "sar_id=%lu lifetime_ms=%I64u",
        "clock_start_calls=%I64u clock_start_successes=%I64u",
        "audio_client_start_calls=%I64u audio_client_start_successes=%I64u",
        "process_samples=%I64u preclock_samples=%I64u render_callbacks=%I64u",
        "queued_frames=%u peak_queued_frames=%u max_frames=%u sample_rate=%lu",
        "frame_size=%u clock_state=%u start_pending=%u",
    ):
        require(sar_summary, needle, f"SAR summary field set lost: {needle}")

    release = region(
        sar,
        "static ULONG WINAPI audio_renderer_sink_Release(",
        "static HRESULT WINAPI audio_renderer_sink_GetCharacteristics(",
    )
    shutdown = region(
        sar,
        "static HRESULT WINAPI audio_renderer_sink_Shutdown(",
        "static const IMFMediaSinkVtbl audio_renderer_sink_vtbl",
    )
    for name, lifecycle in (("release", release), ("shutdown", shutdown)):
        summary_index = lifecycle.find("audio_renderer_drain_diag_summary(renderer);")
        release_index = lifecycle.find("audio_renderer_release_audio_client(renderer);")
        if summary_index < 0 or release_index < 0 or summary_index > release_index:
            fail(f"SAR {name} fallback does not summarize before queued frames are cleared")
    if sar.count("audio_renderer_drain_diag_summary(renderer);") != 2:
        fail("SAR summary must have exactly Shutdown and final-Release call sites")

    process_sample = region(
        sar,
        "static HRESULT WINAPI audio_renderer_stream_ProcessSample(",
        "static HRESULT stream_place_marker(",
    )
    require(
        process_sample,
        "if (!renderer->drain_diag_clock_start_calls)\n"
        "                ++renderer->drain_diag_preclock_samples;",
        "preclock count is not ordered against the first OnClockStart call",
    )
    if "clock_state !=" in process_sample:
        fail("preclock count incorrectly includes later non-running clock states")

    clock_start = region(
        sar,
        "static HRESULT WINAPI audio_renderer_clock_sink_OnClockStart(",
        "static HRESULT WINAPI audio_renderer_clock_sink_OnClockStop(",
    )
    require(
        clock_start,
        "EnterCriticalSection(&renderer->cs);\n"
        "    if (renderer->drain_diag_enabled)\n"
        "        ++renderer->drain_diag_clock_start_calls;",
        "clock-start call counter is not ordered under SAR's critical section",
    )
    audio_start = region(
        sar,
        "static HRESULT audio_renderer_start_audio_client(",
        "static HRESULT WINAPI audio_renderer_clock_sink_QueryInterface(",
    )
    require(
        audio_start,
        "if (FAILED(hr = IAudioClient_Start(renderer->audio_client)))",
        "audio-client success count no longer wraps the actual start call",
    )
    require(
        audio_start,
        "++renderer->drain_diag_audio_client_start_successes;",
        "actual audio-client start success is not counted",
    )

    source_statements = trace_statements(text)
    sar_statements = trace_statements(sar)
    if len(source_statements) != 5:
        fail(f"expected exactly five bounded WineDMO trace sites, found {len(source_statements)}")
    if len(sar_statements) != 1:
        fail(f"expected exactly one bounded SAR trace site, found {len(sar_statements)}")
    statements = source_statements + sar_statements
    diagnostic_surface = "\n".join(statements).lower()
    for forbidden in (
        "%p",
        "debugstr_",
        "context->url",
        "source->current",
        "mime",
        "header",
        "cookie",
        "credential",
        "token",
        "address",
        "transport_bytes=",
    ):
        if forbidden in diagnostic_surface:
            fail(f"privacy-sensitive trace argument present: {forbidden}")

    print("rtsp-drain diagnostic source audit passed")


if __name__ == "__main__":
    main()
