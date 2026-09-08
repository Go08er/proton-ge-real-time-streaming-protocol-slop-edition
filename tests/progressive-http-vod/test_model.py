#!/usr/bin/env python3
"""Small executable model of Alpha patch 5's routing and seek contract."""

from __future__ import annotations

import unittest
from urllib.parse import urlsplit


FFMPEG_INVALID_MEDIA = "AVERROR_INVALIDDATA"
FFMPEG_EXIT = "AVERROR_EXIT"
HTTP_PROTOCOLS = frozenset(("http", "https", "httpproxy", "tcp", "tls"))
HTTPS_PROTOCOLS = frozenset(("https", "httpproxy", "tcp", "tls"))
HTTP_HLS_PROTOCOLS = HTTP_PROTOCOLS | {"crypto"}
HTTPS_HLS_PROTOCOLS = HTTPS_PROTOCOLS | {"crypto"}


def direct_open_allows_cached_fallback(url: str, direct_result: str) -> bool:
    """Model the narrow Wine status used to authorize URLMon."""
    return urlsplit(url).scheme.lower() == "http" and direct_result == FFMPEG_INVALID_MEDIA


def route(
    url_kind: str,
    direct_result: str,
    url: str = "http://media.invalid/video.mp4",
) -> tuple[str, ...]:
    if url_kind == "hls":
        return ("direct-hls",)
    if url_kind == "http-vod":
        steps = ["direct-http"]
        if direct_open_allows_cached_fallback(url, direct_result):
            steps.append("urlmon-cache")
            steps.append("cached-bytestream")
        return tuple(steps)
    return ("existing-route",)


def is_known_hls(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    path = parts.path.lower()
    return ".m3u8" in path or "/hls_playlist/" in path


def seekable(
    format_unseekable: bool,
    direct_url: bool,
    require_avio_seek: bool,
    has_avio: bool,
    avio_range_seekable: bool,
) -> bool:
    if format_unseekable:
        return False
    if require_avio_seek:
        return has_avio and avio_range_seekable
    if not direct_url and has_avio and not avio_range_seekable:
        return False
    return True


def use_custom_mp4_metadata_parser(direct_url: bool, format_name: str) -> bool:
    return not direct_url and "mp4" in format_name


def use_custom_stream_scanner(direct_url: bool) -> bool:
    return not direct_url


def direct_http_security(url: str) -> tuple[bool, frozenset[str]]:
    """Return the mandatory TLS policy and nested-protocol surface."""
    scheme = urlsplit(url).scheme.lower()
    if scheme == "https":
        return True, HTTPS_PROTOCOLS
    if scheme == "http":
        return True, HTTP_PROTOCOLS
    raise ValueError("not a direct HTTP URL")


def hls_child_security(
    root_url: str,
    ffmpeg_tls_verify_default: bool,
) -> tuple[bool, frozenset[str]]:
    """Compose the Wine root allowlist with FFmpeg's child TLS default."""
    scheme = urlsplit(root_url).scheme.lower()
    if scheme == "https":
        protocols = HTTPS_HLS_PROTOCOLS
    elif scheme == "http":
        protocols = HTTP_HLS_PROTOCOLS
    else:
        raise ValueError("not an HTTP HLS root")
    return ffmpeg_tls_verify_default, protocols


def hls_aes_ready(
    root_url: str,
    ffmpeg_tls_verify_default: bool,
    crypto_protocol_enabled: bool,
) -> bool:
    verified, protocols = hls_child_security(root_url, ffmpeg_tls_verify_default)
    return verified and crypto_protocol_enabled and "crypto" in protocols


def classify_progressive_read(
    *,
    require_avio_seek: bool,
    cancelled: bool,
    read_result: str,
    avio_error: str | None,
) -> tuple[str, bool, bool]:
    """Return status, recovery-pending, and whether a nominal packet is kept."""
    deliberate_exit = (
        require_avio_seek
        and cancelled
        and (read_result == FFMPEG_EXIT or avio_error == FFMPEG_EXIT)
    )
    if deliberate_exit:
        return "STATUS_CANCELLED", True, False
    if read_result == "success":
        return "STATUS_SUCCESS", False, True
    if read_result == "AVERROR_EOF":
        return "STATUS_END_OF_FILE", False, False
    return "STATUS_ERROR", False, False


def prepare_progressive_recovery_seek(
    *,
    require_avio_seek: bool,
    recovery_pending: bool,
    avio_error: str | None,
    eof_reached: bool,
) -> tuple[str | None, bool]:
    if not (require_avio_seek and recovery_pending):
        return avio_error, eof_reached
    if avio_error == FFMPEG_EXIT:
        return None, False
    if avio_error is None:
        return None, False
    return avio_error, eof_reached


def finish_progressive_recovery_seek(recovery_pending: bool, success: bool) -> bool:
    return recovery_pending and not success


def media_source_seek_transition(success: bool) -> tuple[bool, bool, int, int]:
    """Return producer-stopped, epoch-odd, later-read count, and MEError count."""
    if success:
        return False, False, 1, 0
    return True, True, 0, 1


class ProgressiveHttpModelTests(unittest.TestCase):
    def test_http_starts_directly(self) -> None:
        self.assertEqual(route("http-vod", "success"), ("direct-http",))

    def test_only_plain_http_invalid_media_uses_cached_fallback(self) -> None:
        self.assertEqual(
            route("http-vod", FFMPEG_INVALID_MEDIA),
            ("direct-http", "urlmon-cache", "cached-bytestream"),
        )
        self.assertEqual(route("http-vod", "later-demux-failure"), ("direct-http",))

    def test_https_never_uses_cached_fallback(self) -> None:
        self.assertEqual(
            route("http-vod", FFMPEG_INVALID_MEDIA, "https://media.invalid/video.mp4"),
            ("direct-http",),
        )

    def test_transport_and_tls_policy_errors_stay_terminal(self) -> None:
        for error in (
            "AVERROR(EIO):certificate-or-hostname",
            "AVERROR(EINVAL):protocol-whitelist-or-downgrade",
            "AVERROR_HTTP_UNAUTHORIZED",
            "AVERROR(ECONNREFUSED)",
        ):
            self.assertEqual(route("http-vod", error), ("direct-http",))
            self.assertEqual(
                route("http-vod", error, "https://media.invalid/video.mp4"),
                ("direct-http",),
            )

    def test_interrupted_open_stays_terminal(self) -> None:
        self.assertEqual(route("http-vod", "timeout"), ("direct-http",))
        self.assertEqual(route("http-vod", "cancelled"), ("direct-http",))

    def test_hls_never_enters_generic_fallback(self) -> None:
        self.assertEqual(route("hls", FFMPEG_INVALID_MEDIA), ("direct-hls",))

    def test_hls_classifier_ignores_query_and_fragment(self) -> None:
        self.assertFalse(is_known_hls("https://media.invalid/movie.mp4?token=.m3u8"))
        self.assertFalse(is_known_hls("https://media.invalid/movie.mp4#next=.m3u8"))
        self.assertFalse(is_known_hls("https://cdn.m3u8.invalid/movie.mp4"))

    def test_hls_classifier_keeps_path_markers_case_insensitive(self) -> None:
        self.assertTrue(is_known_hls("HTTPS://media.invalid/VIDEO.M3U8?token=x"))
        self.assertTrue(is_known_hls("https://media.invalid/HLS_PLAYLIST/id"))

    def test_non_http_is_unchanged(self) -> None:
        self.assertEqual(route("rtsp", FFMPEG_INVALID_MEDIA), ("existing-route",))

    def test_seek_requires_demuxer_and_range_support(self) -> None:
        self.assertFalse(seekable(True, True, True, True, True))
        self.assertFalse(seekable(False, True, True, False, False))
        self.assertFalse(seekable(False, True, True, True, False))
        self.assertTrue(seekable(False, True, True, True, True))

    def test_non_http_seek_semantics_stay_unchanged(self) -> None:
        self.assertTrue(seekable(False, True, False, True, False))  # direct HLS/RTSP
        self.assertFalse(seekable(False, False, False, True, False))  # custom bytestream

    def test_direct_http_never_uses_custom_bytestream_mp4_parser(self) -> None:
        self.assertFalse(use_custom_mp4_metadata_parser(True, "mov,mp4,m4a"))
        self.assertTrue(use_custom_mp4_metadata_parser(False, "mov,mp4,m4a"))

    def test_direct_urls_never_use_custom_duration_or_raw_seek_scanners(self) -> None:
        self.assertFalse(use_custom_stream_scanner(True))
        self.assertTrue(use_custom_stream_scanner(False))

    def test_direct_http_always_carries_tls_verification_for_https_hops(self) -> None:
        self.assertTrue(direct_http_security("http://media.invalid/video.mp4")[0])
        self.assertTrue(direct_http_security("https://media.invalid/video.mp4")[0])

    def test_https_cannot_redirect_to_plain_http(self) -> None:
        _, protocols = direct_http_security("https://media.invalid/video.mp4")
        self.assertIn("https", protocols)
        self.assertNotIn("http", protocols)

    def test_http_can_upgrade_to_verified_https(self) -> None:
        _, protocols = direct_http_security("http://media.invalid/video.mp4")
        self.assertIn("http", protocols)
        self.assertIn("https", protocols)

    def test_direct_http_excludes_local_and_unrelated_nested_protocols(self) -> None:
        forbidden = {"file", "concat", "subfile", "crypto", "udp", "rtsp"}
        for url in ("http://media.invalid/video.mp4", "https://media.invalid/video.mp4"):
            _, protocols = direct_http_security(url)
            self.assertTrue(forbidden.isdisjoint(protocols))

    def test_hls_root_uses_the_same_tls_and_downgrade_policy(self) -> None:
        verified, protocols = hls_child_security(
            "https://media.invalid/live/index.m3u8", True
        )
        self.assertTrue(verified)
        self.assertEqual(protocols, HTTPS_HLS_PROTOCOLS)
        self.assertNotIn("http", protocols)
        self.assertIn("crypto", protocols)

        verified, protocols = hls_child_security(
            "http://media.invalid/live/index.m3u8", True
        )
        self.assertTrue(verified)
        self.assertEqual(protocols, HTTP_HLS_PROTOCOLS)
        self.assertIn("http", protocols)
        self.assertIn("https", protocols)

    def test_hls_children_require_the_project_ffmpeg_tls_default(self) -> None:
        verified, protocols = hls_child_security(
            "https://media.invalid/live/index.m3u8", True
        )
        self.assertTrue(verified)
        self.assertNotIn("http", protocols)
        self.assertTrue({"file", "data", "udp", "rtsp"}.isdisjoint(protocols))
        self.assertFalse(
            hls_child_security("https://media.invalid/live/index.m3u8", False)[0]
        )
        self.assertTrue(
            hls_aes_ready("https://media.invalid/live/index.m3u8", True, True)
        )
        self.assertFalse(
            hls_aes_ready("https://media.invalid/live/index.m3u8", True, False)
        )

    def test_cancelled_partial_corrupt_packet_never_reaches_bsf(self) -> None:
        status, pending, send_to_bsf = classify_progressive_read(
            require_avio_seek=True,
            cancelled=True,
            read_result="success",
            avio_error=FFMPEG_EXIT,
        )
        self.assertEqual(status, "STATUS_CANCELLED")
        self.assertTrue(pending)
        self.assertFalse(send_to_bsf)

    def test_mov_invaliddata_with_latched_exit_is_cancellation(self) -> None:
        status, pending, _ = classify_progressive_read(
            require_avio_seek=True,
            cancelled=True,
            read_result=FFMPEG_INVALID_MEDIA,
            avio_error=FFMPEG_EXIT,
        )
        self.assertEqual(status, "STATUS_CANCELLED")
        self.assertTrue(pending)

    def test_timeout_exit_without_deliberate_cancellation_is_not_erased(self) -> None:
        status, pending, _ = classify_progressive_read(
            require_avio_seek=True,
            cancelled=False,
            read_result=FFMPEG_EXIT,
            avio_error=FFMPEG_EXIT,
        )
        self.assertEqual(status, "STATUS_ERROR")
        self.assertFalse(pending)

    def test_recovery_clears_only_deliberate_exit_and_eof(self) -> None:
        self.assertEqual(
            prepare_progressive_recovery_seek(
                require_avio_seek=True,
                recovery_pending=True,
                avio_error=FFMPEG_EXIT,
                eof_reached=True,
            ),
            (None, False),
        )
        self.assertEqual(
            prepare_progressive_recovery_seek(
                require_avio_seek=True,
                recovery_pending=True,
                avio_error="AVERROR(EIO)",
                eof_reached=True,
            ),
            ("AVERROR(EIO)", True),
        )

    def test_recovery_flag_clears_only_after_successful_seek(self) -> None:
        self.assertFalse(finish_progressive_recovery_seek(True, True))
        self.assertTrue(finish_progressive_recovery_seek(True, False))

    def test_failed_seek_transition_is_terminal_without_read_retry(self) -> None:
        producer_stopped, epoch_odd, later_reads, me_errors = (
            media_source_seek_transition(False)
        )
        self.assertTrue(producer_stopped)
        self.assertTrue(epoch_odd)
        self.assertEqual(later_reads, 0)
        self.assertEqual(me_errors, 1)

    def test_successful_seek_transition_resumes_the_new_timeline(self) -> None:
        producer_stopped, epoch_odd, later_reads, me_errors = (
            media_source_seek_transition(True)
        )
        self.assertFalse(producer_stopped)
        self.assertFalse(epoch_odd)
        self.assertEqual(later_reads, 1)
        self.assertEqual(me_errors, 0)

    def test_cancellation_recovery_does_not_change_hls_or_rtsp(self) -> None:
        status, pending, keep_packet = classify_progressive_read(
            require_avio_seek=False,
            cancelled=True,
            read_result="success",
            avio_error=FFMPEG_EXIT,
        )
        self.assertEqual(status, "STATUS_SUCCESS")
        self.assertFalse(pending)
        self.assertTrue(keep_packet)


if __name__ == "__main__":
    unittest.main()
