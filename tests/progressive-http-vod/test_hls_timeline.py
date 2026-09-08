#!/usr/bin/env python3
"""HLS origin source contracts; actual-MF offset/zero fixtures provide runtime proof."""
import argparse
from pathlib import Path
import unittest

SOURCE = None


class HlsTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SOURCE.read_text()
        cls.create = cls.text.split('NTSTATUS demuxer_create( void *arg )', 1)[1]
        cls.seek = cls.text.split('NTSTATUS demuxer_seek( void *arg )', 1)[1]

    def test_probe_hls_timing_before_publishing_source(self):
        condition = self.create.split('format = demuxer->ctx->iformat;', 1)[1].split(
            'avformat_find_stream_info', 1)[0]
        self.assertIn('strstr( format->name, "hls" )', condition)
        self.assertIn('demuxer_set_deadline( demuxer, RTSP_OPEN_TIMEOUT_US )', condition)

    def test_common_origin_includes_finite_hls(self):
        anchor = ('if (demuxer->ctx->start_time != AV_NOPTS_VALUE &&\n'
                  '        strstr( format->name, "hls" ))')
        self.assertIn(anchor, self.create)
        self.assertIn('demuxer->hls_timestamp_base = AV_NOPTS_VALUE;', self.create)
        self.assertIn('demuxer->hls_timestamp_base = get_user_time( demuxer->ctx->start_time, AV_TIME_BASE_Q );',
                      self.create)

    def test_both_sample_times_use_the_same_origin(self):
        self.assertIn('normalize_hls_timestamps( demuxer, sample );', self.text)
        for field in ('pts', 'dts'):
            self.assertIn(f'if (sample->{field} != AV_NOPTS_VALUE)', self.text)
            self.assertIn(f'av_sat_sub64( sample->{field}, demuxer->hls_timestamp_base )', self.text)

    def test_seek_converts_back_without_advertising_new_seekability(self):
        guard = 'if (!demuxer_is_seekable( demuxer )) return STATUS_NOT_SUPPORTED;'
        translation = 'timestamp = av_sat_add64( timestamp, av_rescale( demuxer->hls_timestamp_base, AV_TIME_BASE, 10000000 ) );'
        self.assertIn(translation, self.seek)
        self.assertLess(self.seek.index(guard), self.seek.index(translation))
        self.assertLess(self.seek.index(translation), self.seek.index('ret = avformat_seek_file'))
        self.assertIn('if (demuxer->hls_timestamp_base != AV_NOPTS_VALUE)', self.seek)
        self.assertIn('if (demuxer->ctx->ctx_flags & AVFMTCTX_UNSEEKABLE) return FALSE;', self.text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--wine-tree', type=Path)
    source.add_argument('--source-file', type=Path)
    args, rest = parser.parse_known_args()
    SOURCE = args.source_file if args.source_file else args.wine_tree / 'dlls/winedmo/unix_demuxer.c'
    unittest.main(argv=[__file__, *rest])
