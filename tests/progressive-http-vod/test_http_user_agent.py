#!/usr/bin/env python3
"""Source checks only; run_http_user_agent.py supplies the actual Wine test."""
import argparse
from pathlib import Path
import re
import unittest

WINE_TREE = None
AGENT = "NSPlayer/12.00.19041.4894 WMFSDK/12.00.19041.4894"
SET_AGENT = 'if (av_dict_set( &options, "user_agent", HTTP_USER_AGENT, 0 ) < 0) goto failed;'


class HttpUserAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WINE_TREE / "dlls/winedmo/unix_demuxer.c").read_text()
        create = cls.text.split("NTSTATUS demuxer_create( void *arg )", 1)[1]
        cls.hls, rest = create.split("else if (is_http_url( params->url ))", 1)
        cls.http, cls.rtsp = rest.split("else if ((rtsp_kind = get_rtsp_url_kind( params->url )))", 1)

    def test_windows_media_foundation_identity(self):
        self.assertIn(f'#define HTTP_USER_AGENT "{AGENT}"', self.text)

    def test_hls_identity_before_open(self):
        self.assertIn(SET_AGENT, self.hls)
        self.assertLess(self.hls.index(SET_AGENT), self.hls.index("avformat_open_input"))

    def test_progressive_identity_before_open(self):
        self.assertIn(SET_AGENT, self.http)
        self.assertLess(self.http.index(SET_AGENT), self.http.index("avformat_open_input"))

    def test_no_rtsp_identity_change(self):
        self.assertNotIn('"user_agent"', self.rtsp)
        self.assertEqual(self.text.count(SET_AGENT), 2)

    def test_tls_and_option_consumption_remain_checked(self):
        for branch in (self.hls, self.http):
            self.assertIn('av_dict_set_int( &options, "tls_verify", 1, 0 )', branch)
            self.assertIn("if (av_dict_count( options ))", branch)
            self.assertIn("demuxer_interrupt_callback", branch)

    def test_no_403_retry_or_fallback_expansion(self):
        gate = self.text.split("static BOOL http_open_allows_urlmon_fallback", 1)[1].split("static BOOL is_http_hls_url", 1)[0]
        self.assertIn('!strncasecmp( url, "http://", 7 ) && ret == AVERROR_INVALIDDATA', gate)
        self.assertNotIn("AVERROR_HTTP_FORBIDDEN", self.text)
        self.assertNotRegex(self.hls + self.http, r'"reconnect[^"\n]*"')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wine-tree", required=True, type=Path)
    args, rest = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[__file__, *rest])
