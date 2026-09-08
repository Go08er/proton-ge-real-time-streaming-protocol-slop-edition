#!/usr/bin/env python3
"""Structural guardrails for experimental progressive-HTTP Alpha patch 5."""

from __future__ import annotations

import argparse
from pathlib import Path


def require(text: str, needle: str, message: str) -> None:
    if needle not in text:
        raise SystemExit(f"FAIL: {message}")


def require_order(text: str, needles: list[str], message: str) -> None:
    position = -1
    for needle in needles:
        next_position = text.find(needle, position + 1)
        if next_position < 0:
            raise SystemExit(f"FAIL: {message}: missing {needle!r}")
        if next_position <= position:
            raise SystemExit(f"FAIL: {message}: {needle!r} is out of order")
        position = next_position


def function_body(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    finish = text.find(end, begin + len(start))
    if begin < 0 or finish < 0:
        raise SystemExit(f"FAIL: could not isolate {start}")
    return text[begin:finish]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wine-tree", required=True, type=Path)
    args = parser.parse_args()

    media_path = args.wine_tree / "dlls/winedmo/media_source.c"
    demuxer_path = args.wine_tree / "dlls/winedmo/unix_demuxer.c"
    unixlib_path = args.wine_tree / "dlls/winedmo/unixlib.c"
    media = media_path.read_text(encoding="utf-8")
    demuxer = demuxer_path.read_text(encoding="utf-8")
    unixlib = unixlib_path.read_text(encoding="utf-8")

    callback = function_body(
        media,
        "static HRESULT WINAPI scheme_handler_callback_Invoke",
        "static const IMFAsyncCallbackVtbl scheme_handler_callback_vtbl",
    )
    require_order(
        callback,
        [
            "media_source_create(context",
            "hr == HRESULT_FROM_NT(STATUS_BAD_NETWORK_PATH)",
            "cache_http_url(context)",
            "get_http_bytestream_url_hint(url)",
            "media_source_create(context",
            "context->url = url",
        ],
        "direct-first/fallback-once routing changed",
    )
    require(callback, "!is_http_hls_url(context->url)", "HLS is not excluded from URLMon fallback")
    require(callback, 'WARN("media-route=urlmon-cache-fallback reason=direct-open-failure',
            "fallback begin marker is absent or may expose a URL")
    require(callback, 'TRACE("media-route=urlmon-cache-fallback-opened',
            "fallback ready marker is absent or may expose a URL")
    if callback.find("URLOpenBlockingStreamW") >= 0:
        raise SystemExit("FAIL: scheme callback downloads before selecting the fallback")

    cache = function_body(media, "static HRESULT cache_http_url", "static HRESULT WINAPI scheme_handler_callback_Invoke")
    if cache.count("URLOpenBlockingStreamW") != 1:
        raise SystemExit("FAIL: fallback must perform exactly one URLMon open")
    require(cache, "context->file_size = -1", "fallback leaves a stale direct-stream length")
    require(cache, "MFBYTESTREAM_IS_SEEKABLE", "fallback does not re-read cached-stream capabilities")

    require_order(
        demuxer,
        [
            "if (is_http_hls_url( params->url ))",
            "else if (is_http_url( params->url ))",
            "else if ((rtsp_kind = get_rtsp_url_kind( params->url )))",
        ],
        "direct HLS must remain ahead of generic HTTP",
    )
    require(demuxer, '#define HTTP_PROTOCOL_WHITELIST "http,https,httpproxy,tcp,tls"',
            "HTTP protocol allowlist broadened or removed")
    require(demuxer, '#define HTTPS_PROTOCOL_WHITELIST "https,httpproxy,tcp,tls"',
            "HTTPS downgrade protection is absent or broadened")
    require(demuxer, '#define HTTP_HLS_PROTOCOL_WHITELIST "http,https,httpproxy,tcp,tls,crypto"',
            "HTTP-root HLS AES wrapper or protocol boundary changed")
    require(demuxer, '#define HTTPS_HLS_PROTOCOL_WHITELIST "https,httpproxy,tcp,tls,crypto"',
            "HTTPS-root HLS AES wrapper or downgrade protection changed")
    whitelist_selector = function_body(
        demuxer,
        "static const char *http_protocol_whitelist",
        "static const char *hls_protocol_whitelist",
    )
    require_order(
        whitelist_selector,
        [
            '!strncasecmp( url, "https://", 8 )',
            "? HTTPS_PROTOCOL_WHITELIST : HTTP_PROTOCOL_WHITELIST",
        ],
        "generic HTTP scheme-specific protocol selection changed",
    )
    hls_whitelist_selector = function_body(
        demuxer,
        "static const char *hls_protocol_whitelist",
        "static BOOL http_open_allows_urlmon_fallback",
    )
    require_order(
        hls_whitelist_selector,
        [
            '!strncasecmp( url, "https://", 8 )',
            "? HTTPS_HLS_PROTOCOL_WHITELIST : HTTP_HLS_PROTOCOL_WHITELIST",
        ],
        "HLS root scheme no longer constrains its AES wrapper and nested URL",
    )
    fallback_gate = function_body(
        demuxer,
        "static BOOL http_open_allows_urlmon_fallback",
        "static BOOL is_http_hls_url",
    )
    require_order(
        fallback_gate,
        [
            '!strncasecmp( url, "http://", 7 )',
            "ret == AVERROR_INVALIDDATA",
        ],
        "URLMon fallback is not restricted to plain-HTTP media validation",
    )
    if '"https://' in fallback_gate:
        raise SystemExit("FAIL: HTTPS can enter the URLMon compatibility fallback")
    if demuxer.count("STATUS_BAD_NETWORK_PATH") != 1:
        raise SystemExit("FAIL: direct-open fallback status must have one narrow producer")
    require(demuxer, "*cursor != '?' && *cursor != '#'", "HLS classifier still scans query or fragment")
    require(media, "*cursor != '?' && *cursor != '#'", "Windows HLS classifier still scans query or fragment")
    require(demuxer, "while (*cursor && *cursor != '/'", "Unix HLS classifier still scans the URL authority")
    require(media, "while (*cursor && *cursor != '/'", "Windows HLS classifier still scans the URL authority")
    http_branch = function_body(
        demuxer,
        "else if (is_http_url( params->url ))",
        "else if ((rtsp_kind = get_rtsp_url_kind( params->url )))",
    )
    require_order(
        http_branch,
        [
            "protocol_whitelist = http_protocol_whitelist( params->url )",
            'av_dict_set( &options, "protocol_whitelist", protocol_whitelist, 0 )',
            'av_dict_set_int( &options, "tls_verify", 1, 0 )',
            "demuxer->ctx->interrupt_callback.callback = demuxer_interrupt_callback",
            "demuxer_set_deadline( demuxer, HTTP_OPEN_TIMEOUT_US )",
            "avformat_open_input( &demuxer->ctx, params->url, NULL, &options )",
            "interrupted = demuxer_interrupt_callback( demuxer )",
            "if (!interrupted && http_open_allows_urlmon_fallback( params->url, ret ))",
            "failure_status = STATUS_BAD_NETWORK_PATH",
            'TRACE( "media-route=direct-http-progressive-opened',
            'av_dict_set( &options, "tls_verify", NULL, 0 )',
            "if (av_dict_count( options ))",
        ],
        "direct HTTP open or its fallback signal changed",
    )
    require(http_branch, "demuxer->io_timeout = HTTP_IO_TIMEOUT_US", "HTTP read/seek/close timeout is absent")
    require(http_branch, "demuxer->require_avio_seek = TRUE", "direct HTTP does not fail closed on missing AVIO")
    require(http_branch, 'TRACE( "media-route=direct-http-progressive',
            "direct HTTP begin marker is absent or may expose a URL")
    require(demuxer, 'if (!demuxer->direct_url && strstr( format->name, "mp4" ))',
            "direct HTTP may pass FFmpeg network AVIO opaque to the custom MP4 parser")

    duration = function_body(demuxer, "static INT64 get_context_duration", "NTSTATUS demuxer_check")
    require_order(
        duration,
        [
            "if (ctx->duration_estimation_method == AVFMT_DURATION_FROM_BITRATE)",
            "if (!custom_io) return AV_NOPTS_VALUE",
            "get_asf_header_duration",
            "get_raw_mpegvideo_duration",
        ],
        "direct URLs may reach custom-stream duration scanners",
    )
    raw_seek = function_body(demuxer, "static NTSTATUS raw_mpegvideo_seek", "static void fixup_asf_vc1_timestamps")
    require_order(
        raw_seek,
        ["if (demuxer->direct_url) return STATUS_NOT_SUPPORTED", "read_stream_at"],
        "direct URLs may reach the custom-stream raw MPEG seek scanner",
    )

    hls_branch = function_body(
        demuxer,
        "if (is_http_hls_url( params->url ))",
        "else if (is_http_url( params->url ))",
    )
    require_order(
        hls_branch,
        [
            "protocol_whitelist = hls_protocol_whitelist( params->url )",
            'av_dict_set( &options, "protocol_whitelist", protocol_whitelist, 0 )',
            'av_dict_set_int( &options, "tls_verify", 1, 0 )',
            "demuxer->ctx->interrupt_callback.callback = demuxer_interrupt_callback",
            "avformat_open_input( &demuxer->ctx, params->url, hls, &options )",
            'av_dict_set( &options, "tls_verify", NULL, 0 )',
            "if (av_dict_count( options ))",
            "av_dict_free( &options )",
        ],
        "direct HLS root trust or required-option checks changed",
    )
    require(hls_branch, 'TRACE( "media-route=direct-hls', "direct HLS begin marker is absent")

    source_start = function_body(
        media,
        "static HRESULT media_source_start",
        "static HRESULT media_source_pause",
    )
    seek_transition = source_start[source_start.index("winedmo_demuxer_seek"):]
    success_marker = (
        "\n        else\n"
        "        {\n"
        "            source->flushing = FALSE;"
    )
    if "if (FAILED(seek_error) || status)" in seek_transition:
        require_order(
            seek_transition,
            [
                "winedmo_demuxer_seek",
                "if (FAILED(seek_error) || status)",
                "media_source_queue_demux_error(source, error)",
                success_marker,
                "source->read_flushing = FALSE",
                "media_source_end_demux_transition(source)",
            ],
            "failed seeks may resume or retry a damaged demux timeline",
        )
        terminal_start = seek_transition.index("if (FAILED(seek_error) || status)")
    else:
        if "media_source_set_demux_error(source, media_source_error_from_demux_status(status))" in seek_transition:
            terminal_error_store = (
                "media_source_set_demux_error(source, "
                "media_source_error_from_demux_status(status))"
            )
        else:
            terminal_error_store = "source->demux_terminal_error = HRESULT_FROM_NT(status)"
        require_order(
            seek_transition,
            [
                "winedmo_demuxer_seek",
                "if (status)",
                "source->demux_thread_shutdown = true",
                terminal_error_store,
                success_marker,
                "source->read_flushing = FALSE",
                "media_source_end_demux_transition(source)",
            ],
            "failed seeks may resume or retry a damaged demux timeline",
        )
        terminal_start = seek_transition.index("if (status)")
    terminal_branch = seek_transition[
        terminal_start:seek_transition.index(success_marker)
    ]
    if "media_source_end_demux_transition" in terminal_branch:
        raise SystemExit("FAIL: a failed seek can return the transition epoch to readable state")
    if "cancel_transition_failed" in source_start:
        raise SystemExit("FAIL: demux seek failure is not included in the terminal transition path")

    seekable = function_body(demuxer, "static BOOL demuxer_is_seekable", "static INT64 get_user_time")
    require(seekable, "if (demuxer->require_avio_seek)", "direct HTTP seek policy is not isolated")
    require(seekable, "return demuxer->ctx->pb && (demuxer->ctx->pb->seekable & AVIO_SEEKABLE_NORMAL)",
            "direct HTTP does not require a range-seekable AVIO context")
    require(seekable, "if (!demuxer->direct_url && demuxer->ctx->pb",
            "legacy bytestream seek policy changed")

    free_context = function_body(demuxer, "static void demuxer_free_context", "NTSTATUS demuxer_create")
    read_packet = function_body(demuxer, "static NTSTATUS demuxer_filter_packet", "NTSTATUS demuxer_read")
    seek = function_body(demuxer, "NTSTATUS demuxer_seek", "NTSTATUS demuxer_stream_lang")
    for body, operation in ((free_context, "close"), (read_packet, "read"), (seek, "seek")):
        require(body, "demuxer_set_deadline( demuxer, demuxer->io_timeout )",
                f"direct HTTP {operation} deadline is absent")
        require(body, "demuxer_set_deadline( demuxer, 0 )", f"direct HTTP {operation} deadline is not cleared")

    require(demuxer, "BOOL cancelled_read_pending_recovery;",
            "progressive cancellation recovery state is absent")
    require(read_packet, "if (demuxer->cancelled_read_pending_recovery) return STATUS_CANCELLED;",
            "a damaged progressive timeline can continue reading before recovery")
    read_after_av = read_packet[read_packet.index("ret = av_read_frame"):]
    partial_read_guard = read_after_av[:read_after_av.index("if (!ret)")]
    require_order(
        partial_read_guard,
        [
            "ret = av_read_frame",
            "demuxer_set_deadline( demuxer, 0 )",
            "demuxer->require_avio_seek",
            "__atomic_load_n( &demuxer->cancelled",
            "ret == AVERROR_EXIT",
            "demuxer->ctx->pb->error == AVERROR_EXIT",
            "demuxer->cancelled_read_pending_recovery = TRUE",
            "av_packet_free( packet )",
            "return STATUS_CANCELLED",
        ],
        "cancelled positive-partial packet is not discarded before the BSF",
    )
    cancellation_guard = partial_read_guard[
        partial_read_guard.index("if (demuxer->require_avio_seek"):]
    cancellation_condition = cancellation_guard[:cancellation_guard.index("{")]
    if "ret < 0" in cancellation_condition:
        raise SystemExit("FAIL: positive-partial cancellation recovery is gated on a negative read result")
    require_order(
        read_after_av,
        ["return STATUS_CANCELLED", "av_bsf_send_packet( stream->filter, (*packet) )"],
        "cancelled or corrupt packet can reach the bitstream filter",
    )
    require_order(
        seek,
        [
            "demuxer->require_avio_seek && demuxer->cancelled_read_pending_recovery",
            "if (pb->error == AVERROR_EXIT)",
            "pb->error = 0",
            "pb->eof_reached = 0",
            "avformat_seek_file",
            "if (ret < 0)",
            "demuxer->cancelled_read_pending_recovery = FALSE",
            "av_bsf_flush",
        ],
        "progressive AVIO cancellation recovery order changed",
    )
    if seek.count("pb->error = 0") != 1:
        raise SystemExit("FAIL: seek may erase an AVIO error other than deliberate AVERROR_EXIT")

    vlog = function_body(unixlib, "static void vlog", "static const char *debugstr_version")
    require_order(
        vlog,
        ["if (level > av_log_get_level()) return;", "av_log_format_line"],
        "FFmpeg log levels are not filtered before formatting sensitive trace output",
    )

    print("PASS: progressive HTTP VOD structural contract")


if __name__ == "__main__":
    main()
