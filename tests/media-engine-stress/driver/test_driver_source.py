# SPDX-License-Identifier: BSD-3-Clause
"""Source guards for failure modes that occur before JSONL can exist."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class EventWindowModel:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.consumed: dict[str, int] = {}

    def emit(self, event: str) -> None:
        self.counts[event] = self.counts.get(event, 0) + 1

    def begin_action(self, *events: str) -> None:
        for event in events:
            self.consumed[event] = self.counts.get(event, 0)

    def wait(self, event: str) -> bool:
        count = self.counts.get(event, 0)
        consumed = self.consumed.get(event, 0)
        if count <= consumed:
            return False
        self.consumed[event] = consumed + 1
        return True


class DriverSourceTests(unittest.TestCase):
    def test_large_scenario_state_is_heap_allocated(self) -> None:
        source = (ROOT / "media_engine_stress.c").read_text(encoding="utf-8")
        self.assertIn("struct scenario *scenario = NULL;", source)
        self.assertIn("scenario = calloc(1, sizeof(*scenario))", source)
        self.assertIn("free(scenario);", source)
        self.assertNotIn("struct scenario scenario;", source)

    def test_build_rejects_future_megabyte_scale_stack_frames(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("-Wframe-larger-than=1048576", build)

    def test_action_event_windows_are_fenced_before_the_api_call(self) -> None:
        source = (ROOT / "media_engine_stress.c").read_text(encoding="utf-8")
        expected = (
            ("MF_MEDIA_ENGINE_EVENT_PLAYING, MAX_EVENT_CODE", "IMFMediaEngineEx_Play"),
            ("MF_MEDIA_ENGINE_EVENT_PAUSE, MAX_EVENT_CODE", "IMFMediaEngineEx_Pause"),
            ("MF_MEDIA_ENGINE_EVENT_SEEKING,\n                    MF_MEDIA_ENGINE_EVENT_SEEKED",
             "IMFMediaEngineEx_SetCurrentTimeEx"),
            ("MF_MEDIA_ENGINE_EVENT_RATECHANGE, MAX_EVENT_CODE",
             "IMFMediaEngineEx_SetPlaybackRate"),
        )
        for arguments, api in expected:
            with self.subTest(api=api):
                fence = f"begin_event_window(notify, {arguments});"
                self.assertIn(fence, source)
                self.assertLess(source.index(fence), source.index(api, source.index(fence)))

    def test_resume_cannot_consume_initial_or_seek_era_playing(self) -> None:
        model = EventWindowModel()
        model.emit("PLAYING")
        model.emit("PLAYING")
        model.begin_action("PLAYING")
        self.assertFalse(model.wait("PLAYING"))
        model.emit("PLAYING")
        self.assertTrue(model.wait("PLAYING"))
        self.assertFalse(model.wait("PLAYING"))

    def test_seek_window_preserves_raced_source_readiness(self) -> None:
        model = EventWindowModel()
        model.emit("CANPLAY")
        model.begin_action("SEEKING", "SEEKED")
        self.assertTrue(model.wait("CANPLAY"))

    def test_seek_window_rejects_a_preexisting_error(self) -> None:
        model = EventWindowModel()
        model.emit("ERROR")
        model.begin_action("SEEKING", "SEEKED", "ERROR")
        self.assertFalse(model.wait("ERROR"))
        model.emit("ERROR")
        self.assertTrue(model.wait("ERROR"))

        source = (ROOT / "media_engine_stress.c").read_text(encoding="utf-8")
        seek = source.index("case ACTION_SEEK:")
        api = source.index("IMFMediaEngineEx_SetCurrentTimeEx", seek)
        error_fence = source.index(
            "begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_ERROR, MAX_EVENT_CODE);",
            seek,
        )
        self.assertLess(error_fence, api)

    def test_seek_settle_wait_requires_both_idle_and_target_proximity(self) -> None:
        source = (ROOT / "media_engine_stress.c").read_text(encoding="utf-8")
        start = source.index("static int wait_for_seek_settled")
        end = source.index("static int execute_scenario", start)
        function = source[start:end]
        self.assertIn("IMFMediaEngineEx_GetCurrentTime(notify->engine)", function)
        self.assertIn("IMFMediaEngineEx_IsSeeking(notify->engine)", function)
        self.assertIn(
            "!seeking && isfinite(current) && fabs(current - target) <= tolerance",
            function,
        )

    def test_seek_settle_parser_has_strict_ranges_and_exact_arity(self) -> None:
        source = (ROOT / "media_engine_stress.c").read_text(encoding="utf-8")
        start = source.index('else if (!_stricmp(command, "wait_seek_settled"))')
        end = source.index('else if (!_stricmp(command, "wait_ms"))', start)
        parser = source[start:end]
        self.assertEqual(parser.count("next_word(&cursor"), 3)
        self.assertIn("!no_more_words(cursor)", parser)
        self.assertIn(
            "parse_double_value(arg1, 0.0, 86400.0, &action->number)",
            parser,
        )
        self.assertIn(
            "parse_double_value(arg2, 0.0, 60.0, &action->tolerance)",
            parser,
        )
        self.assertIn("parse_uint(arg3, 3600000, &parsed_uint)", parser)


if __name__ == "__main__":
    unittest.main()
