#!/usr/bin/env python3
"""Model recovery-reset ordering, not execution of Wine or a playback test.

Both inherited OnVideoStreamTick heuristics call the same seek helper. It
checks capability before resetting the sink. The proposal removes the two
premature caller resets; it does not change when either heuristic triggers.
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
import unittest


S_OK, E_FAIL = 0, -1
ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/proposed/0023-mfmediaengine-check-seekability-before-recovery-reset.patch"
WINE_TREE = None


@dataclass
class Recovery:
    seek_cap: bool = True
    caps_error: bool = False
    paused: bool = False
    seeking: bool = False
    deferred_seek: bool = False
    at_end: bool = False
    start_fails: bool = False
    queue: list[int] = field(default_factory=lambda: [10, 20])
    presentation: int | None = 5
    sample_request_pending: bool = True
    resets: int = 0
    helper_calls: int = 0
    starts: int = 0
    saved_position: int | None = None
    ended: bool = False
    helper_result: int | None = None

    def reset(self):
        self.queue.clear()
        self.presentation = None
        self.sample_request_pending = False
        self.resets += 1

    def set_current_time(self, target):
        self.helper_calls += 1
        if self.caps_error:
            return E_FAIL
        if not self.seek_cap:
            return S_OK  # Existing helper behavior; no seek was performed.
        if self.at_end and not self.paused:
            self.ended = True
            return S_OK
        self.reset()
        if self.paused:
            self.saved_position = target
            return S_OK
        self.starts += 1
        if self.start_fails:
            return E_FAIL
        self.seeking = True
        self.saved_position = target
        return S_OK

    def recover(self, *, legacy=False):
        # Both actual heuristic branches exclude SEEKING and DEFERRED_SEEK.
        if self.seeking or self.deferred_seek:
            return
        if legacy:
            self.reset()
        # The real caller ignores this HRESULT. Recording it here checks that
        # the overlay does not introduce retries or change helper error policy.
        self.helper_result = self.set_current_time(100)


class RecoverySeekTests(unittest.TestCase):
    def test_legacy_unseekable_recovery_discards_frames_and_demand(self):
        model = Recovery(seek_cap=False)
        model.recover(legacy=True)
        self.assertEqual((model.queue, model.presentation), ([], None))
        self.assertFalse(model.sample_request_pending)
        self.assertEqual((model.starts, model.helper_result), (0, S_OK))

    def test_unseekable_recovery_preserves_frames_and_pending_request(self):
        model = Recovery(seek_cap=False)
        model.recover()
        self.assertEqual((model.queue, model.presentation), ([10, 20], 5))
        self.assertTrue(model.sample_request_pending)
        self.assertEqual((model.resets, model.starts), (0, 0))

    def test_failed_capability_query_preserves_sink_and_failure(self):
        model = Recovery(caps_error=True)
        model.recover()
        self.assertEqual((model.resets, model.starts, model.helper_result), (0, 0, E_FAIL))
        self.assertEqual(model.queue, [10, 20])
        self.assertTrue(model.sample_request_pending)

    def test_supported_seek_still_resets_before_one_start(self):
        old, new = Recovery(), Recovery()
        old.recover(legacy=True)
        new.recover()
        self.assertEqual((old.resets, new.resets), (2, 1))
        self.assertEqual((new.starts, new.saved_position, new.seeking), (1, 100, True))
        self.assertEqual((old.queue, old.presentation, old.sample_request_pending),
                         (new.queue, new.presentation, new.sample_request_pending))

    def test_supported_paused_seek_retains_deferred_position_semantics(self):
        model = Recovery(paused=True)
        model.recover()
        self.assertEqual((model.resets, model.starts, model.saved_position), (1, 0, 100))

    def test_failed_start_is_not_retried_or_hidden(self):
        model = Recovery(start_fails=True)
        model.recover()
        self.assertEqual((model.resets, model.starts, model.helper_calls), (1, 1, 1))
        self.assertEqual(model.helper_result, E_FAIL)

    def test_logical_end_uses_helpers_existing_early_return(self):
        model = Recovery(at_end=True)
        model.recover()
        self.assertTrue(model.ended)
        self.assertEqual((model.resets, model.starts), (0, 0))
        self.assertEqual(model.queue, [10, 20])

    def test_inflight_and_deferred_seeks_do_not_enter_recovery(self):
        for flag in ("seeking", "deferred_seek"):
            for legacy in (True, False):
                with self.subTest(flag=flag, legacy=legacy):
                    model = Recovery(**{flag: True})
                    model.recover(legacy=legacy)
                    self.assertEqual((model.resets, model.helper_calls), (0, 0))

    def test_proposal_only_removes_the_two_caller_resets(self):
        text = PATCH.read_text(encoding="utf-8")
        self.assertEqual(text.count("diff --git "), 1)
        added = [line for line in text.splitlines() if line.startswith("+")
                 and not line.startswith("+++")]
        removed = [line[1:].strip() for line in text.splitlines() if line.startswith("-")
                   and not line.startswith("---")]
        self.assertEqual(added, [])
        self.assertEqual(removed, ["video_frame_sink_reset(engine->presentation.frame_sink);"] * 2)
        self.assertNotIn("proposed/" + PATCH.name, (ROOT / "patches/series").read_text())


class IntegratedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if WINE_TREE is None:
            raise unittest.SkipTest("pass --wine-tree for integrated source contracts")
        cls.source = (WINE_TREE / "dlls/mfmediaengine/main.c").read_text()

    def body(self, source, name):
        match = re.search(r"^static HRESULT [^\n]*\b" + re.escape(name)
                          + r"\([^;]*?\n\{.*?^}", source, re.M | re.S)
        self.assertIsNotNone(match, "missing function " + name)
        return " ".join(match.group().split())

    def assert_source_contract(self, source):
        tick = self.body(source, "media_engine_OnVideoStreamTick")
        self.assertTrue("video_frame_sink_reset" not in tick,
                        "OnVideoStreamTick must not reset the sink before the seekability check")
        self.assertEqual(tick.count("media_engine_set_current_time(engine, mftime_to_seconds(clocktime));"), 2)
        self.assertEqual(tick.count("FLAGS_ENGINE_SEEKING | FLAGS_ENGINE_DEFERRED_SEEK"), 2)
        seek = self.body(source, "media_engine_set_current_time")
        caps = "hr = IMFMediaSession_GetSessionCapabilities(engine->session, &caps); "
        guard = "if (FAILED(hr) || !(caps & MFSESSIONCAP_SEEK)) return hr;"
        reset = "video_frame_sink_reset(engine->presentation.frame_sink);"
        start = "IMFMediaSession_Start("
        self.assertTrue(caps + guard in seek,
                        "seek helper must return before reset when capability lookup fails or seek is unsupported")
        self.assertEqual(seek.count(reset), 1)
        self.assertTrue(start in seek, "seek helper must retain its source-start operation")
        self.assertLess(seek.index(guard), seek.index(reset))
        self.assertLess(seek.index(reset), seek.index(start))

    def test_integrated_source_lets_seek_helper_own_reset(self):
        self.assert_source_contract(self.source)

    def test_premature_reset_or_missing_capability_guard_is_rejected(self):
        call = "media_engine_set_current_time(engine, mftime_to_seconds(clocktime));"
        for old, new in (
            (call, "video_frame_sink_reset(engine->presentation.frame_sink);\n" + call),
            ("FAILED(hr) || !(caps & MFSESSIONCAP_SEEK)", "FAILED(hr)"),
        ):
            with self.subTest(regression=old):
                mutated = self.source.replace(old, new, 1)
                self.assertTrue(mutated != self.source, "mutation anchor missing: " + old)
                with self.assertRaises(AssertionError):
                    self.assert_source_contract(mutated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wine-tree", type=Path)
    args, remaining = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
