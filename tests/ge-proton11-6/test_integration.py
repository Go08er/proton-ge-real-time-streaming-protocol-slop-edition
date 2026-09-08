#!/usr/bin/env python3
"""Portable merge contracts plus mutations of a supplied integrated Wine tree."""

import argparse
import importlib.util
from pathlib import Path
import sys
import unittest

from audit import audit

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "pause_scrub_control", ROOT / "tests/media-engine-pause-scrub/test_model.py")
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)
WINE_TREE = None


class MergeTests(unittest.TestCase):
    def test_old_series_is_preserved(self):
        import hashlib
        self.assertEqual(hashlib.sha256(
            (ROOT / "patches/series-ge-proton11-3").read_bytes()).hexdigest(),
            "f1bb59433222a088921b7eba0095369da5f86083b0da7a6e4cc660a7a8d1ebdd")

    def test_autoplay_load_after_pause_requests_play(self):
        model = control.PauseScrubModel()
        model.play()
        model.pause()
        model.set_autoplay(True)
        # GE 0031 Load delegates to Play, not merely a property change.
        if model.autoplay:
            model.play()
        model.session_paused()
        model.rate_changed(1.0)
        self.assertTrue(model.playback_requested)
        self.assertFalse(model.paused)
        self.assertFalse(model.pause_pending)
        self.assertEqual(model.playback_starts, 1)

    def test_new_pause_after_autoplay_load_still_wins(self):
        model = control.PauseScrubModel()
        model.set_autoplay(True)
        if model.autoplay:
            model.play()
        model.pause()
        model.session_paused()
        model.rate_changed(1.0)
        self.assertFalse(model.playback_requested)
        self.assertTrue(model.paused)
        self.assertFalse(model.pause_pending)
        self.assertEqual(model.playback_starts, 0)


class SourceMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if WINE_TREE is None:
            raise unittest.SkipTest("pass --wine-tree to run source mutations too")
        cls.engine = (WINE_TREE / "dlls/mfmediaengine/main.c").read_text()
        cls.demux = (WINE_TREE / "dlls/winedmo/unix_demuxer.c").read_text()

    def test_integrated_source_passes(self):
        self.assertEqual(audit(self.engine, self.demux), [])

    def test_regressions_are_detected(self):
        for old, new in (
            ("hr = IMFMediaEngineEx_Play(iface);", "hr = S_OK;"),
            ("MFMEDIASOURCE_CAN_SEEK", "MFBYTESTREAM_IS_SEEKABLE"),
            ("!(engine->flags & FLAGS_ENGINE_PAUSE_PENDING)", "TRUE"),
            ('TRACE("Live ', 'WARN("Live '),
            ("ID3D11DeviceContext_Draw", "removed_production_draw"),
        ):
            with self.subTest(regression=old):
                mutated = self.engine.replace(old, new)
                self.assertNotEqual(mutated, self.engine)
                self.assertTrue(audit(mutated, self.demux))
        for extra in ('\n/* "reconnect" */', '\n/* media_engine_diagnose_live_texture_pixels */',
                      '\n/* rgb_sum */'):
            with self.subTest(extra=extra):
                self.assertTrue(audit(self.engine + extra, self.demux + extra))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wine-tree", type=Path)
    args, remaining = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
