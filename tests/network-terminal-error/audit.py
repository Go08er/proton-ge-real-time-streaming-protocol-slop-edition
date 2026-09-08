#!/usr/bin/env python3
"""Audit prepared Wine source for A3.12 network-error ownership."""

from __future__ import annotations

from pathlib import Path
import sys


def fail(message: str) -> None:
    raise SystemExit(f"network terminal-error audit failed: {message}")


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


def forbid(body: str, needle: str, message: str) -> None:
    if needle in body:
        fail(message)


def require_order(body: str, *needles: str) -> None:
    missing = [needle for needle in needles if needle not in body]
    if missing:
        fail(f"required ordering term is absent: {missing[0]}")
    positions = [body.index(needle) for needle in needles]
    if positions != sorted(positions):
        fail(f"required order changed: {' -> '.join(needles)}")


def read(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        fail(f"missing source file: {path}")
    return path.read_text(encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    root = Path(sys.argv[1]).resolve()
    unix = read(root, "dlls/winedmo/unix_demuxer.c")
    source = read(root, "dlls/winedmo/media_source.c")
    session = read(root, "dlls/mf/session.c")
    engine = read(root, "dlls/mfmediaengine/main.c")

    packet = region(
        unix,
        "static NTSTATUS demuxer_filter_packet(",
        "NTSTATUS demuxer_read(",
    )
    seek = region(unix, "NTSTATUS demuxer_seek(", "NTSTATUS demuxer_stream_lang(")
    free_context = region(
        unix,
        "static void demuxer_free_context(",
        "NTSTATUS demuxer_create(",
    )
    start = region(
        source,
        "static HRESULT media_source_start(",
        "static HRESULT media_source_pause(",
    )
    start_commit = start[
        start.index("/* Commit stream/source Started or Seeked events") :
    ]
    wait = region(source, "static HRESULT wait_on_sample(", "/* ========================================================================")
    sample_delivery = wait[wait.index("if (source->state != SOURCE_RUNNING)"):]
    terminal_delivery = sample_delivery[
        sample_delivery.index(
            "if (FAILED(hr = media_source_get_demux_error(source)))"
        ) :
    ]
    demux_thread = region(
        source,
        "static DWORD CALLBACK source_demux_thread(",
        "static HRESULT WINAPI source_async_commands_Invoke(",
    )
    queue_error = region(
        source,
        "static BOOL media_source_queue_demux_error(struct media_source *source, HRESULT error)\n{",
        "static DWORD CALLBACK source_demux_thread(",
    )
    runtime_failure = region(
        demux_thread,
        "if (status == STATUS_END_OF_FILE)",
        "if (!(stream = media_source_find_stream",
    )
    terminal_claim = region(
        demux_thread,
        "if (status == STATUS_CANCELLED || status == STATUS_IO_TIMEOUT",
        "if (status == STATUS_BUFFER_TOO_SMALL)",
    )
    session_events = region(
        session,
        "static HRESULT WINAPI session_events_callback_Invoke(",
        "static const IMFAsyncCallbackVtbl session_events_callback_vtbl",
    )
    session_error = region(session_events, "case MEError:", "        default:")
    engine_events = region(
        engine,
        "static HRESULT WINAPI media_engine_session_events_Invoke(",
        "static const IMFAsyncCallbackVtbl media_engine_session_events_vtbl",
    )
    async_commands = region(
        source,
        "static HRESULT WINAPI source_async_commands_Invoke(",
        "static const IMFAsyncCallbackVtbl source_async_commands_callback_vtbl",
    )
    set_source = region(
        engine,
        "static HRESULT media_engine_set_source(",
        "static HRESULT WINAPI media_engine_SetSource(",
    )

    # A genuine drained demuxer must retain END_OF_FILE ownership. Only the
    # later generic failure branch is eligible for direct-network mapping.
    require_order(
        packet,
        "input_error = ret < 0 && ret != AVERROR_EOF && ret != AVERROR(EAGAIN);",
        "if (ret == AVERROR_EOF) return STATUS_END_OF_FILE;",
        "if (!demuxer->direct_url || !input_error) return STATUS_END_OF_FILE;",
        "return STATUS_CONNECTION_DISCONNECTED;",
    )
    require(
        packet,
        "input_error = FALSE;",
        "packet-error origin is not reset before each demux iteration",
    )
    require(
        packet,
        "ret = av_bsf_send_packet( stream->filter, (*packet) );\n"
        "                input_error = FALSE;",
        "bitstream-filter send failure can be mislabeled as a transport error",
    )
    require(
        packet,
        "__atomic_load_n( &demuxer->cancelled, __ATOMIC_ACQUIRE )",
        "explicit cancellation no longer precedes timeout/read classification",
    )
    require(packet, "AVERROR(ETIMEDOUT)", "FFmpeg timeout is not distinguished")

    # MOV performs the HTTP Range reopen on its first packet read, not inside
    # avformat_seek_file(). Validate that read after flushing old packets and
    # keep it for the normal output path. Seeking to known duration is exempt.
    require_order(
        seek,
        "av_packet_free( &demuxer->last_packet );",
        "demuxer->last_stream = NULL;",
        "params->timestamp < demuxer->duration",
        "demuxer_filter_packet( demuxer, &packet )",
        "return demuxer->duration <= 0 ? STATUS_SUCCESS",
        ": STATUS_CONNECTION_DISCONNECTED;",
        "demuxer->last_packet = packet;",
    )
    if seek.rfind("return STATUS_SUCCESS;") < seek.index("demuxer->last_packet = packet;"):
        fail("successful retained prefetch no longer reaches successful seek return")
    require(seek, "demuxer->require_avio_seek", "prefetch is not progressive-only")
    require(
        seek,
        "demuxer->duration <= 0 || params->timestamp < demuxer->duration",
        "known-end seek is no longer excluded from prefetch",
    )
    require_order(
        seek,
        "ret = avformat_seek_file(",
        "if (ret == AVERROR_EOF && demuxer->direct_url && demuxer->require_avio_seek)",
        "if (demuxer->direct_url && demuxer->require_avio_seek)",
        "return STATUS_CONNECTION_DISCONNECTED;",
        "done:\n    demuxer->cancelled_read_pending_recovery = FALSE;",
    )
    require_order(
        free_context,
        "av_packet_free( &demuxer->last_packet );",
        "demuxer->last_stream = NULL;",
        "if (!demuxer->ctx) return;",
        "avformat_close_input( &demuxer->ctx );",
    )

    # A blocking network seek releases source->cs so Shutdown can set the
    # demux interrupt, but all other source commands remain serialized behind
    # an explicit pending flag. Synchronous failure uses the same one-shot
    # source error helper and never fabricates stream EOS.
    require_order(
        start,
        "source->demux_seek_pending = true;",
        "LeaveCriticalSection(&source->cs);",
        "winedmo_demuxer_seek(",
        "EnterCriticalSection(&source->cs);",
        "source->demux_seek_pending = false;",
        "WakeAllConditionVariable(&source->work_cv);",
        "media_source_queue_demux_error(source, error);",
    )
    require_order(
        start_commit,
        "EnterCriticalSection(&source->demux_error_cs);",
        "media_source_get_demux_error(source)",
        "IMFMediaEventQueue_QueueEventParamVar(source->event_queue",
    )
    if start_commit.rfind("LeaveCriticalSection(&source->demux_error_cs);") < \
            start_commit.index("IMFMediaEventQueue_QueueEventParamVar(source->event_queue"):
        fail("successful Started/Seeked event is no longer committed under demux_error_cs")
    forbid(start, "stream->eos = TRUE;", "failed seek fabricates stream EOS")
    require(
        async_commands,
        "while (source->demux_seek_pending && source->state != SOURCE_SHUTDOWN)",
        "other source commands can race the unlocked blocking seek",
    )
    require(
        async_commands,
        "if (!error_queued)",
        "retrying Start cannot re-signal its retained error to finish the session command",
    )

    # First-error-wins must precede shutdown and wakeups. Runtime network
    # errors may not fall through to the legacy retry loop or synthesize EOS.
    require(
        queue_error,
        "if (!media_source_set_demux_error(source, error))",
        "source can emit duplicate terminal errors",
    )
    require_order(
        queue_error,
        "EnterCriticalSection(&source->demux_error_cs);",
        "media_source_set_demux_error(source, error)",
        "source->demux_thread_shutdown = true;",
        "MEError",
        "InterlockedIncrement(&source->demux_error_event_generation);",
        "WakeAllConditionVariable",
    )
    if queue_error.rfind("LeaveCriticalSection(&source->demux_error_cs);") < \
            queue_error.index("WakeAllConditionVariable"):
        fail("terminal error publication lock is released before waiters are woken")
    require(
        queue_error,
        "EnterCriticalSection(&stream->queue_cs);\n"
        "        WakeAllConditionVariable(&stream->queue_cv);\n"
        "        LeaveCriticalSection(&stream->queue_cs);",
        "terminal error wake is not serialized with the queue wait predicate",
    )
    forbid(queue_error, "eos = TRUE", "terminal network error fabricates EOS")
    require_order(
        runtime_failure,
        "if (status == STATUS_END_OF_FILE)",
        "if (status)\n        {",
        "Sleep(1);",
    )
    require_order(
        terminal_claim,
        "if (status == STATUS_CANCELLED || status == STATUS_IO_TIMEOUT",
        "media_source_queue_demux_error(source, error);",
        "LeaveCriticalSection(&source->demuxer_cs);",
    )
    require(
        terminal_delivery,
        "media_source_get_demux_error(source)",
        "blocked sample request does not observe terminal source error",
    )
    require_order(
        terminal_delivery,
        "media_source_get_demux_error(source)",
        "if (pkt)",
        "eos = stream->eos;",
        "media_stream_send_eos",
    )

    # MediaSession must authenticate the current source before forwarding a
    # bounded error set. Its event value carries that source identity.
    require_order(
        session_error,
        "case MEError:",
        "session_get_media_source(session, (IMFMediaSource *)event_source)",
        "hr == MF_E_OPERATION_CANCELLED",
        "IMFMediaEventQueue_QueueEventParamUnk",
    )
    require(
        session_error,
        "(IUnknown *)(IMFMediaSource *)event_source",
        "forwarded error does not carry source identity",
    )
    require_order(
        session_error,
        "if (!(session_source->flags & SOURCE_FLAG_TERMINAL_ERROR_FORWARDED))",
        "IMFMediaEventQueue_QueueEventParamUnk",
        "if (FAILED(queue_hr))",
        "session_source->flags |= SOURCE_FLAG_TERMINAL_ERROR_FORWARDED;",
        "session->command_state == COMMAND_STATE_STARTING_SOURCES",
        "session_command_complete(session);",
    )
    forbid(
        session_error,
        "session->command_state == COMMAND_STATE_RESTARTING_SOURCES",
        "runtime error can complete before the required source Stop event",
    )
    forbid(
        session_error,
        "session_command_complete_with_event",
        "terminal network error synthesizes a successful-looking session start",
    )

    # MediaEngine independently rejects stale-source events, clears all
    # pending transport state, and emits ERROR. It must not synthesize ENDED.
    error_case = region(engine_events, "case MEError:", "case MEEndOfPresentation:")
    require(
        error_case,
        "engine->presentation.generation != engine->source_generation",
        "MediaEngine accepts an error from a superseded presentation generation",
    )
    require(
        error_case,
        "source.punkVal != (IUnknown *)engine->presentation.source",
        "MediaEngine accepts a stale source error",
    )
    require(error_case, "MF_MEDIA_ENGINE_ERR_NETWORK", "network mapping is absent")
    require(error_case, "MF_MEDIA_ENGINE_ERR_ABORTED", "cancel mapping is absent")
    for state in (
        "engine->playback_requested = FALSE;",
        "engine->current_seek = NAN;",
        "engine->next_seek = NAN;",
        "engine->deferred_seek = NAN;",
        "FLAGS_ENGINE_SEEKING",
        "FLAGS_ENGINE_DEFERRED_SEEK",
        "FLAGS_ENGINE_PLAY_PENDING",
        "FLAGS_ENGINE_PAUSE_PENDING",
        "FLAGS_ENGINE_WAITING",
        "FLAGS_ENGINE_SCRUBBING",
    ):
        require(error_case, state, f"error path does not clear {state}")
    require(
        error_case,
        "MF_MEDIA_ENGINE_EVENT_ERROR",
        "MediaEngine does not notify the application of the error",
    )
    accepted_error = region(
        error_case,
        "engine->error_code = error_code;",
        "PropVariantClear(&source);",
    )
    require_order(
        accepted_error,
        "MFMediaEngineNotify_EventNotify",
        "LeaveCriticalSection(&engine->cs);",
    )
    forbid(
        error_case,
        "MF_MEDIA_ENGINE_EVENT_ENDED",
        "network-error case incorrectly emits ENDED",
    )

    require_order(
        set_source,
        "generation = ++engine->source_generation;",
        "engine->error_code = MF_MEDIA_ENGINE_ERR_NOERROR;",
        "engine->extended_code = S_OK;",
        "SysFreeString(engine->current_source);",
    )
    require(
        engine,
        "engine->presentation.generation = 0;",
        "cleared presentation retains stale generation ownership",
    )
    require(
        engine,
        "engine->presentation.generation = generation;",
        "installed presentation does not record source generation ownership",
    )
    require(
        source,
        "InitializeCriticalSectionEx(&object->demux_error_cs",
        "terminal publication lock is not initialized",
    )
    require(
        source,
        "DeleteCriticalSection(&source->demux_error_cs);",
        "terminal publication lock is not destroyed on normal release",
    )
    require(
        source,
        "DeleteCriticalSection(&object->demux_error_cs);",
        "terminal publication lock is not destroyed on create failure",
    )

    print("network terminal-error source audit passed")


if __name__ == "__main__":
    main()
