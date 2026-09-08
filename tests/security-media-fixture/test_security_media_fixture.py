#!/usr/bin/env python3
"""Pure unit tests for the loopback media-security fixture."""

from __future__ import annotations

import io
import json
import unittest

from security_media_fixture import (
    BIND_HOST,
    LOG_KEYS,
    SafeLogger,
    parse_range,
    rewrite_aes_playlist,
    safe_range_label,
)


class RangeTests(unittest.TestCase):
    def test_explicit_open_and_clamped_ranges(self) -> None:
        explicit = parse_range("bytes=2-5", 10)
        open_ended = parse_range("bytes=7-", 10)
        self.assertEqual((2, 5), (explicit.start, explicit.end))
        self.assertEqual((7, 9), (open_ended.start, open_ended.end))
        self.assertEqual("bytes=2-9", parse_range("bytes=2-99", 10).label)

    def test_unsafe_or_unsatisfied_ranges_are_redacted(self) -> None:
        for value in ("bytes=10-", "bytes=5-2", "bytes=-2", "bytes=0-1,3-4", "token=DO_NOT_LOG"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_range(value, 10)
            self.assertEqual("invalid", safe_range_label(value, 10))


class PlaylistTests(unittest.TestCase):
    def test_mixed_playlist_rewrites_only_fixed_key_and_segment_children(self) -> None:
        source = (
            "#EXTM3U\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"/hls/aes/key.bin\",IV=0x00\n"
            "#EXTINF:2.0,\n"
            "segment000.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        result = rewrite_aes_playlist(source, "http://127.0.0.1:12345")
        self.assertIn('URI="http://127.0.0.1:12345/hls/aes/key.bin"', result)
        self.assertIn("http://127.0.0.1:12345/hls/aes/segment000.ts", result)
        self.assertNotIn("DO_NOT_LOG", result)


class SafeLogTests(unittest.TestCase):
    def test_log_is_an_exact_allowlist(self) -> None:
        output = io.StringIO()
        logger = SafeLogger(output, "security-case-01")
        logger.record(
            server="https_valid",
            method="GET",
            resource="direct-mp4",
            status=416,
            bytes_sent=0,
            range_label=safe_range_label("token=DO_NOT_LOG", 10),
        )
        serialized = output.getvalue()
        self.assertNotIn("DO_NOT_LOG", serialized)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertNotIn("path", serialized.lower())
        self.assertNotIn("url", serialized.lower())
        record = json.loads(serialized)
        self.assertEqual(LOG_KEYS, set(record))
        self.assertEqual("invalid", record["range"])

    def test_case_id_cannot_carry_freeform_personal_text(self) -> None:
        with self.assertRaises(ValueError):
            SafeLogger(io.StringIO(), "contains spaces or a name")

    def test_freeform_resource_or_server_labels_are_rejected(self) -> None:
        logger = SafeLogger(io.StringIO(), "security-case-02")
        for server, resource in (
            ("https://DO_NOT_LOG", "direct-mp4"),
            ("https_valid", "/media?token=DO_NOT_LOG"),
        ):
            with self.subTest(server=server, resource=resource), self.assertRaises(ValueError):
                logger.record(
                    server=server,
                    method="GET",
                    resource=resource,
                    status=200,
                    bytes_sent=0,
                    range_label=None,
                )


class BindTests(unittest.TestCase):
    def test_listener_address_is_fixed_loopback(self) -> None:
        self.assertEqual("127.0.0.1", BIND_HOST)


if __name__ == "__main__":
    unittest.main()
