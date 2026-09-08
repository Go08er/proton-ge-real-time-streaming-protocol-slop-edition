#!/usr/bin/env python3
"""Model the selected-frame status regression; this does not execute Wine C.

A3.19 video_frame_sink_get_pts_after() overwrites its last successful
selection result while examining a future or preroll sample. The proposed
overlay retains only a frame selected in this call, without hiding errors
or changing the queue, preroll, or request decisions.
"""

import argparse
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import unittest


S_OK, S_FALSE, E_FAIL = 0, 1, -1
MIN_TIME = -(1 << 63)
PREROLL_TOLERANCE = 500000  # 50 ms, in the source's 100 ns units.
ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/proposed/0022-mfmediaengine-preserve-selected-frame-tick-result.patch"
WINE_TREE = None


@dataclass(frozen=True)
class TickResult:
    hr: int
    pts: int
    queue: tuple[int | None, ...]
    presentation: int | None
    request_helper_called: bool  # Pending/EOS can suppress the helper's event.


def tick(queue, clock, *, floor=MIN_TIME, presentation=None, presented=False,
         preserve=True):
    """None in the queue models GetSampleTime failure, not a real frame."""
    queue = deque(queue)
    hr, pts, transfer = S_FALSE, MIN_TIME, False
    selected_pts = None
    while queue:
        sample = queue.popleft()
        preroll = sample is not None and sample + PREROLL_TOLERANCE < floor
        if sample is None:
            hr = E_FAIL
        elif preroll:
            hr = S_FALSE
        else:
            hr, pts = (S_OK if clock >= sample else S_FALSE), sample
        if hr == S_OK:
            presentation, presented, transfer = sample, False, True
            selected_pts = pts
        elif preroll:
            transfer, hr = True, S_FALSE
        else:
            queue.appendleft(sample)
            break

    # Preserve the C function's existing mutually exclusive request/fallback.
    if not transfer and presentation is not None and not presented:
        hr, pts = S_OK, presentation
    if preserve and selected_pts is not None and hr >= 0:
        hr, pts = S_OK, selected_pts
    return TickResult(hr, pts, tuple(queue), presentation, transfer)


class FrameTickTests(unittest.TestCase):
    def test_legacy_due_then_future_loses_selected_status_and_pts(self):
        result = tick([1000000, 2000000], 1500000, preserve=False)
        self.assertEqual((result.hr, result.pts), (S_FALSE, 2000000))
        self.assertEqual(result.presentation, 1000000)

    def test_due_then_future_reports_the_selected_frame(self):
        result = tick([1000000, 2000000], 1500000)
        self.assertEqual((result.hr, result.pts), (S_OK, 1000000))
        self.assertEqual(result.queue, (2000000,))
        self.assertTrue(result.request_helper_called)

    def test_two_due_frames_report_the_last_selected_frame(self):
        result = tick([900000, 1000000], 1500000)
        self.assertEqual((result.hr, result.pts), (S_OK, 1000000))
        self.assertEqual(result.queue, ())

    def test_selected_frame_does_not_trigger_future_pts_resynchronization(self):
        clock = 1500000
        future = clock + 31 * 10000000
        old = tick([1000000, future], clock, preserve=False)
        new = tick([1000000, future], clock)
        self.assertEqual(old.hr, S_FALSE)
        self.assertGreater(old.pts - clock, 30 * 10000000)
        self.assertEqual((new.hr, new.pts), (S_OK, 1000000))
        self.assertEqual(new.queue, (future,))

    def test_due_then_preroll_does_not_erase_selection(self):
        args = ([1000000, 100000], 1500000)
        old = tick(*args, floor=1000000, preserve=False)
        new = tick(*args, floor=1000000)
        self.assertEqual(old.hr, S_FALSE)
        self.assertEqual((new.hr, new.pts), (S_OK, 1000000))

    def test_preroll_only_does_not_resurrect_old_presentation(self):
        result = tick([100000], 1500000, floor=1000000, presentation=0)
        self.assertEqual((result.hr, result.pts), (S_FALSE, MIN_TIME))
        self.assertTrue(result.request_helper_called)

    def test_future_only_retains_its_future_pts(self):
        result = tick([2000000], 1500000)
        self.assertEqual((result.hr, result.pts), (S_FALSE, 2000000))
        self.assertFalse(result.request_helper_called)

    def test_preroll_then_future_without_selection_is_unchanged(self):
        result = tick([100000, 2000000], 1500000, floor=1000000)
        self.assertEqual((result.hr, result.pts), (S_FALSE, 2000000))
        self.assertEqual(result.queue, (2000000,))

    def test_actual_later_error_is_not_hidden(self):
        result = tick([1000000, None], 1500000)
        self.assertEqual(result.hr, E_FAIL)
        self.assertEqual(result.queue, (None,))

    def test_empty_queue_does_not_repeat_transferred_presentation(self):
        result = tick([], 1500000, presentation=1000000, presented=True)
        self.assertEqual((result.hr, result.pts), (S_FALSE, MIN_TIME))

    def test_existing_untransferred_fallback_is_preserved(self):
        result = tick([2000000], 1500000, presentation=1000000)
        self.assertEqual((result.hr, result.pts), (S_OK, 1000000))

    def test_legacy_can_recover_next_tick_not_proof_of_permanent_stall(self):
        first = tick([1000000, 2000000], 1500000, preserve=False)
        second = tick(first.queue, 1600000, presentation=first.presentation,
                      preserve=False)
        self.assertEqual((second.hr, second.pts), (S_OK, 1000000))

    def test_overlay_leaves_queue_presentation_and_demand_decisions_identical(self):
        for queue in ([], [100000], [1000000], [2000000], [None],
                      [1000000, 2000000], [1000000, 100000], [1000000, None]):
            with self.subTest(queue=queue):
                old = tick(queue, 1500000, floor=1000000, preserve=False)
                new = tick(queue, 1500000, floor=1000000)
                self.assertEqual((old.queue, old.presentation, old.request_helper_called),
                                 (new.queue, new.presentation, new.request_helper_called))

    def test_proposal_is_narrow_and_not_in_the_active_series(self):
        text = PATCH.read_text(encoding="utf-8")
        self.assertEqual(text.count("diff --git "), 1)
        self.assertIn("diff --git a/dlls/mfmediaengine/video_frame_sink.c ", text)
        self.assertIn("+                selected_sample = TRUE;", text)
        self.assertIn("+                sample_pts = *pts;", text)
        self.assertIn("+        if (selected_sample && SUCCEEDED(hr))", text)
        self.assertIn("+            hr = S_OK;", text)
        self.assertIn("+            *pts = sample_pts;", text)
        self.assertNotIn("proposed/" + PATCH.name, (ROOT / "patches/series").read_text())


class IntegratedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if WINE_TREE is None:
            raise unittest.SkipTest("pass --wine-tree for integrated source contracts")
        cls.source = (WINE_TREE / "dlls/mfmediaengine/video_frame_sink.c").read_text()

    def assert_source_contract(self, source):
        match = re.search(r"^HRESULT video_frame_sink_get_pts_after\([^;]*?\n\{.*?^}",
                          source, re.M | re.S)
        self.assertIsNotNone(match, "missing video_frame_sink_get_pts_after")
        body = " ".join(match.group().split())
        self.assertTrue("selected_sample = FALSE;" in body,
                        "frame tick must initialize per-call selected-frame state")
        self.assertTrue(
            "if (hr == S_OK) { video_frame_sink_sample_queue_set_presentation(sink, sample); "
            "transfer_sample = TRUE; selected_sample = TRUE; sample_pts = *pts; }" in body,
            "frame tick must save status and PTS when selecting a due frame")
        restore = "if (selected_sample && SUCCEEDED(hr)) { hr = S_OK; *pts = sample_pts; }"
        self.assertTrue(restore in body,
                        "frame tick must restore selected status/PTS without hiding later errors")
        self.assertLess(body.index("video_frame_sink_sample_queue_pop("), body.index(restore))
        self.assertLess(body.index(restore), body.index("LeaveCriticalSection"))
        self.assertTrue("video_frame_sink_sample_queue_push(sink, sample, TRUE); break;" in body,
                        "frame tick must retain the future sample in its queue")
        self.assertTrue(
            "if (transfer_sample) video_frame_sink_stream_request_sample(sink); "
            "else if (sink->queue.presentation_sample && !sink->queue.sample_presented)" in body,
            "frame tick must preserve mutually exclusive request/fallback handling")

    def test_integrated_source_reports_selected_frame_without_hiding_errors(self):
        self.assert_source_contract(self.source)

    def test_missing_selection_or_error_guard_is_rejected(self):
        for old, new in (
            ("selected_sample = FALSE;", "selected_sample = TRUE;"),
            ("selected_sample = TRUE;", "/* selection not recorded */"),
            ("sample_pts = *pts;", "/* selected timestamp lost */"),
            ("selected_sample && SUCCEEDED(hr)", "selected_sample"),
            ("video_frame_sink_sample_queue_push(sink, sample, TRUE);", "/* future frame discarded */"),
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
