#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Deterministic models and patch-scope checks for Patches 14 through 16."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SERIES = ROOT / "patches/series"
NOTICE = ROOT / "NOTICE.md"
PATCH14_NAME = "experimental/0014-mfmediaengine-preserve-pending-start-position.patch"
SPLIT_PATCH15_NAME = (
    "experimental/0015-mf-subscribe-replacement-source-before-restart.patch"
)
SPLIT_PATCH16_NAME = (
    "experimental/0016-mf-stop-live-clock-before-replacement-start.patch"
)
COMBINED_PATCH15_NAME = "experimental/0015-mf-order-immediate-replacement-start.patch"
PATCH17_NAME = "experimental/0017-winedmo-propagate-terminal-network-read-errors.patch"
PATCH14 = SERIES.parent / PATCH14_NAME
PATCH15 = SERIES.parent / COMBINED_PATCH15_NAME
PATCH16 = PATCH15
ACTIVE_REPLACEMENT_WINDOW = (PATCH14_NAME, COMBINED_PATCH15_NAME, PATCH17_NAME)
SPLIT_PATCH_PRESENTATION = False
VT_EMPTY = "VT_EMPTY"
VT_I8 = "VT_I8"


@dataclass
class StartPosition:
    vt: str = VT_EMPTY
    value: int = 0


@dataclass
class Presentation:
    source: object | None = None
    descriptor: object | None = None
    start: StartPosition = field(default_factory=StartPosition)
    frame_sink: object | None = None


@dataclass
class Engine:
    generation: int = 0
    presentation: Presentation = field(default_factory=Presentation)

    def stage_source(self, generation: int) -> None:
        self.generation = generation
        self.presentation.start = StartPosition(VT_I8, 0)

    def set_pending_time(self, generation: int, seconds: float) -> None:
        if generation == self.generation:
            self.presentation.start = StartPosition(VT_I8, round(seconds * 10_000_000))


def legacy_clear(engine: Engine, during_shutdown=None) -> None:
    engine.presentation = Presentation()
    if during_shutdown:
        during_shutdown()


def patch14_clear(engine: Engine, during_shutdown=None) -> None:
    engine.presentation.source = None
    engine.presentation.descriptor = None
    engine.presentation.frame_sink = None
    if during_shutdown:
        during_shutdown()


def started_session_reaches_source_start(position: StartPosition) -> bool:
    if position.vt == VT_EMPTY:
        return False
    if position.vt != VT_I8:
        raise AssertionError(f"unexpected pending variant {position.vt}")
    return True


@dataclass
class SessionRestart:
    sources_subscribed: bool = False
    source_state: str = "stopped"
    session_state: str = "started"
    clock_started: bool = True
    command_state: str = "submitted"
    source_stop_events_emitted: int = 0
    source_stop_events_observed: int = 0
    source_starts: int = 0
    clock_only_starts: int = 0
    subscribe_attempts: int = 0
    physical_clock_state: str = "running"
    clock_state_queries: int = 0
    clock_stop_attempts: int = 0
    clock_stop_fails: bool = False
    start_completion_status: str | None = None
    outgoing_sinks_attached: int = 2
    sink_detach_attempts: int = 0
    expected_initial_clock_stops: set[str] = field(default_factory=set)

    def subscribe_sources(self) -> None:
        if self.sources_subscribed:
            return
        self.subscribe_attempts += 1
        self.sources_subscribed = True

    def stop_source(self) -> None:
        self.source_stop_events_emitted += 1
        if not self.sources_subscribed:
            return
        self.source_stop_events_observed += 1
        changed = self.source_state != "stopped"
        self.source_state = "stopped"
        if not changed:
            return
        self.command_state = "starting_sources"
        self.source_starts += 1


def start_session(session: SessionRestart, *, keep_position: bool,
                  route_stopped_presentation: bool,
                  synchronize_physical_clock: bool = False) -> None:
    sources_stopped = session.source_state == "stopped"
    if session.session_state == "started" and keep_position \
            and not (route_stopped_presentation and sources_stopped):
        session.clock_only_starts += 1
        session.command_state = "complete"
        return

    if session.session_state in {"started", "paused"}:
        if route_stopped_presentation and sources_stopped:
            if synchronize_physical_clock:
                session.clock_state_queries += 1
                if session.physical_clock_state not in {"invalid", "stopped"}:
                    session.clock_stop_attempts += 1
                    if session.clock_stop_fails:
                        session.command_state = "complete"
                        session.start_completion_status = "clock-stop-failed"
                        return
                    session.physical_clock_state = "stopped"
                if session.physical_clock_state != "invalid":
                    session.expected_initial_clock_stops = {"audio", "video"}
            session.session_state = "stopped"
            session.clock_started = False
        elif not keep_position:
            session.command_state = "restarting_sources"
            session.stop_source()
            return

    session.command_state = "starting_sources"
    if not session.sources_subscribed:
        session.subscribe_sources()
    session.source_starts += 1


def clear_presentation(session: SessionRestart, *, detach_outgoing_sinks: bool) -> None:
    if detach_outgoing_sinks:
        session.sink_detach_attempts += session.outgoing_sinks_attached
        session.outgoing_sinks_attached = 0


@dataclass
class SinkTransition:
    audio: str = "stopped"
    video: str = "stopped"
    command_state: str = "prerolling_sinks"
    physical_clock_starts: int = 0
    session_started_events: int = 0
    stale_sink_starts: int = 0

    def event(self, sink: str, state: str) -> None:
        setattr(self, sink, state)
        if self.command_state == "prerolling_sinks" \
                and self.audio == self.video == "prerolled":
            self.physical_clock_starts += 1
            self.command_state = "starting_sinks"
        if self.command_state == "starting_sinks" \
                and self.audio == self.video == "started":
            self.session_started_events += 1
            self.command_state = "complete"


def deliver_sink_event(session: SessionRestart, transition: SinkTransition,
                       sink: str, state: str) -> None:
    if sink in session.expected_initial_clock_stops:
        if state == "stopped":
            session.expected_initial_clock_stops.remove(sink)
            return
        if state == "started":
            session.expected_initial_clock_stops.remove(sink)
    transition.event(sink, state)


def drive_replacement_sinks(session: SessionRestart) -> SinkTransition:
    transition = SinkTransition()
    if session.physical_clock_state == "running":
        # Exact A3.10 trace order: video inherited the live-clock Start before
        # audio's preroll acknowledgement, then audio inherited Start.
        transition.video = "prerolled"  # non-preroll sink marker
        transition.event("video", "started")
        transition.event("audio", "prerolled")
        transition.event("audio", "started")
        return transition

    # A stopped clock cannot synthesize Start during attachment, but its
    # asynchronous current-state Stop can arrive after the non-preroll video
    # sink was marked PREROLLED. Reproduce that adverse ordering explicitly.
    transition.video = "prerolled"
    deliver_sink_event(session, transition, "video", "stopped")
    transition.event("audio", "prerolled")
    if transition.physical_clock_starts == 0:
        return transition

    transition.stale_sink_starts += session.outgoing_sinks_attached
    deliver_sink_event(session, transition, "audio", "stopped")
    deliver_sink_event(session, transition, "audio", "started")
    deliver_sink_event(session, transition, "video", "started")
    return transition


def added_lines(patch: str) -> str:
    return "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def configure_patch_contract(series: Path) -> None:
    global PATCH14_NAME, PATCH17_NAME, COMBINED_PATCH15_NAME
    global ACTIVE_REPLACEMENT_WINDOW
    global NOTICE
    global PATCH14
    global PATCH15
    global PATCH16
    global SERIES
    global SPLIT_PATCH_PRESENTATION

    SERIES = series.resolve()
    if not SERIES.is_file():
        raise SystemExit(f"replacement model series is not a file: {SERIES}")
    entries = tuple(
        line
        for raw in SERIES.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )
    # Same lifecycle contract, two immutable source-base presentations.
    prefix = "ge-proton11-6" if any(
        entry.startswith("ge-proton11-6/") for entry in entries
    ) else "experimental"
    PATCH14_NAME = prefix + "/0014-mfmediaengine-preserve-pending-start-position.patch"
    COMBINED_PATCH15_NAME = prefix + "/0015-mf-order-immediate-replacement-start.patch"
    PATCH17_NAME = prefix + "/0017-winedmo-propagate-terminal-network-read-errors.patch"
    a320_terminal = "ge-proton11-6-a320/0017-winedmo-propagate-terminal-network-read-errors.patch"
    if a320_terminal in entries:
        if PATCH17_NAME in entries:
            raise SystemExit("replacement model series declares two terminal-error patches")
        PATCH17_NAME = a320_terminal
    split_window = (
        PATCH14_NAME,
        SPLIT_PATCH15_NAME,
        SPLIT_PATCH16_NAME,
        PATCH17_NAME,
    )
    combined_window = (PATCH14_NAME, COMBINED_PATCH15_NAME, PATCH17_NAME)

    def window_count(window: tuple[str, ...]) -> int:
        return sum(
            entries[index:index + len(window)] == window
            for index in range(len(entries) - len(window) + 1)
        )

    split_count = window_count(split_window)
    combined_count = window_count(combined_window)
    if (split_count, combined_count) not in ((1, 0), (0, 1)):
        raise SystemExit(
            "replacement model series must declare exactly one supported "
            "Patch 14-to-17 presentation"
        )
    expected_counts = {
        PATCH14_NAME: 1,
        SPLIT_PATCH15_NAME: int(split_count == 1),
        SPLIT_PATCH16_NAME: int(split_count == 1),
        COMBINED_PATCH15_NAME: int(combined_count == 1),
        PATCH17_NAME: 1,
    }
    if any(entries.count(name) != count for name, count in expected_counts.items()):
        raise SystemExit(
            "replacement model series contains a duplicate, mixed, or partial "
            "Patch 14-to-17 presentation"
        )

    patch_root = SERIES.parent
    NOTICE = patch_root.parent / "NOTICE.md"
    PATCH14 = patch_root / PATCH14_NAME
    SPLIT_PATCH_PRESENTATION = split_count == 1
    if SPLIT_PATCH_PRESENTATION:
        PATCH15 = patch_root / SPLIT_PATCH15_NAME
        PATCH16 = patch_root / SPLIT_PATCH16_NAME
        ACTIVE_REPLACEMENT_WINDOW = split_window
    else:
        PATCH15 = patch_root / COMBINED_PATCH15_NAME
        PATCH16 = PATCH15
        ACTIVE_REPLACEMENT_WINDOW = combined_window


class ReplacementLifecycleModelTests(unittest.TestCase):
    def populated_engine(self) -> Engine:
        engine = Engine()
        engine.stage_source(2)
        engine.presentation.source = object()
        engine.presentation.descriptor = object()
        engine.presentation.frame_sink = object()
        return engine

    def test_a38_whole_clear_reproduces_no_source_start(self) -> None:
        engine = self.populated_engine()
        legacy_clear(engine)
        self.assertEqual(engine.presentation.start.vt, VT_EMPTY)
        self.assertFalse(started_session_reaches_source_start(engine.presentation.start))

    def test_patch14_running_replacement_restarts_source_at_zero(self) -> None:
        engine = self.populated_engine()
        patch14_clear(engine)
        self.assertEqual(engine.presentation.start, StartPosition(VT_I8, 0))
        self.assertTrue(started_session_reaches_source_start(engine.presentation.start))
        self.assertIsNone(engine.presentation.source)
        self.assertIsNone(engine.presentation.descriptor)
        self.assertIsNone(engine.presentation.frame_sink)

    def test_same_generation_pre_ready_offset_survives(self) -> None:
        engine = self.populated_engine()
        engine.set_pending_time(2, 47.25)
        patch14_clear(engine)
        self.assertEqual(engine.presentation.start, StartPosition(VT_I8, 472_500_000))

    def test_reentrant_new_generation_update_is_not_overwritten(self) -> None:
        engine = self.populated_engine()

        def newer_request_during_shutdown() -> None:
            engine.stage_source(3)
            engine.set_pending_time(3, 91.5)

        patch14_clear(engine, newer_request_during_shutdown)
        self.assertEqual(engine.generation, 3)
        self.assertEqual(engine.presentation.start, StartPosition(VT_I8, 915_000_000))

    def test_a39_orphans_replacement_source_stop_event(self) -> None:
        session = SessionRestart()
        start_session(session, keep_position=False, route_stopped_presentation=False)
        self.assertEqual(session.source_stop_events_emitted, 1)
        self.assertEqual(session.source_stop_events_observed, 0)
        self.assertEqual(session.command_state, "restarting_sources")
        self.assertEqual(session.source_starts, 0)

    def test_patch15_routes_stopped_replacement_through_fresh_start(self) -> None:
        session = SessionRestart()
        start_session(session, keep_position=False, route_stopped_presentation=True)
        self.assertTrue(session.sources_subscribed)
        self.assertEqual(session.subscribe_attempts, 1)
        self.assertEqual(session.source_stop_events_emitted, 0)
        self.assertEqual(session.session_state, "stopped")
        self.assertFalse(session.clock_started)
        self.assertEqual(session.command_state, "starting_sources")
        self.assertEqual(session.source_starts, 1)
        self.assertEqual(session.physical_clock_state, "running")

    def test_a310_live_clock_reproduces_preroll_deadlock(self) -> None:
        session = SessionRestart()
        start_session(session, keep_position=False,
                      route_stopped_presentation=True)
        transition = drive_replacement_sinks(session)
        self.assertEqual(transition.command_state, "prerolling_sinks")
        self.assertEqual(transition.session_started_events, 0)
        self.assertEqual(transition.physical_clock_starts, 0)

    def test_patch16_stops_physical_clock_before_sink_attachment(self) -> None:
        session = SessionRestart()
        clear_presentation(session, detach_outgoing_sinks=True)
        start_session(session, keep_position=False,
                      route_stopped_presentation=True,
                      synchronize_physical_clock=True)
        self.assertEqual(session.clock_state_queries, 1)
        self.assertEqual(session.clock_stop_attempts, 1)
        self.assertEqual(session.physical_clock_state, "stopped")
        self.assertEqual(session.sink_detach_attempts, 2)
        self.assertEqual(session.expected_initial_clock_stops, {"audio", "video"})
        transition = drive_replacement_sinks(session)
        self.assertEqual(transition.physical_clock_starts, 1)
        self.assertEqual(transition.session_started_events, 1)
        self.assertEqual(transition.command_state, "complete")
        self.assertEqual(transition.stale_sink_starts, 0)
        self.assertEqual(session.expected_initial_clock_stops, set())

    def test_stopped_clock_without_stale_stop_filter_reproduces_preroll_deadlock(self) -> None:
        session = SessionRestart()
        clear_presentation(session, detach_outgoing_sinks=True)
        start_session(session, keep_position=False,
                      route_stopped_presentation=True,
                      synchronize_physical_clock=True)
        session.expected_initial_clock_stops.clear()
        transition = drive_replacement_sinks(session)
        self.assertEqual(transition.video, "stopped")
        self.assertEqual(transition.audio, "prerolled")
        self.assertEqual(transition.physical_clock_starts, 0)
        self.assertEqual(transition.command_state, "prerolling_sinks")

    def test_one_shot_filter_preserves_real_later_stop_transition(self) -> None:
        session = SessionRestart(expected_initial_clock_stops=set())
        transition = SinkTransition(audio="started", video="started",
                                    command_state="stopping_sinks")
        deliver_sink_event(session, transition, "audio", "stopped")
        self.assertEqual(transition.audio, "stopped")

    def test_clock_stop_without_sink_detach_restarts_obsolete_renderers(self) -> None:
        session = SessionRestart()
        clear_presentation(session, detach_outgoing_sinks=False)
        start_session(session, keep_position=False,
                      route_stopped_presentation=True,
                      synchronize_physical_clock=True)
        transition = drive_replacement_sinks(session)
        self.assertEqual(transition.session_started_events, 1)
        self.assertEqual(transition.stale_sink_starts, 2)

    def test_patch16_propagates_clock_stop_failure_without_false_state(self) -> None:
        session = SessionRestart(clock_stop_fails=True)
        start_session(session, keep_position=False,
                      route_stopped_presentation=True,
                      synchronize_physical_clock=True)
        self.assertEqual(session.start_completion_status, "clock-stop-failed")
        self.assertEqual(session.session_state, "started")
        self.assertTrue(session.clock_started)
        self.assertEqual(session.source_starts, 0)

    def test_patch16_skips_stop_for_an_invalid_or_stopped_clock(self) -> None:
        for state in ("invalid", "stopped"):
            with self.subTest(state=state):
                session = SessionRestart(physical_clock_state=state)
                start_session(session, keep_position=False,
                              route_stopped_presentation=True,
                              synchronize_physical_clock=True)
                self.assertEqual(session.clock_state_queries, 1)
                self.assertEqual(session.clock_stop_attempts, 0)
                self.assertEqual(session.source_starts, 1)

    def test_patch15_is_noop_for_an_ordinary_subscribed_seek(self) -> None:
        session = SessionRestart(sources_subscribed=True, source_state="started")
        start_session(session, keep_position=False, route_stopped_presentation=True)
        self.assertEqual(session.subscribe_attempts, 0)
        self.assertEqual(session.source_stop_events_observed, 1)
        self.assertEqual(session.command_state, "starting_sources")
        self.assertEqual(session.source_starts, 1)

    def test_patch15_does_not_clock_only_start_a_stopped_replacement(self) -> None:
        session = SessionRestart()
        start_session(session, keep_position=True, route_stopped_presentation=True)
        self.assertEqual(session.clock_only_starts, 0)
        self.assertEqual(session.source_starts, 1)
        self.assertFalse(session.clock_started)

    def test_existing_started_keep_position_remains_clock_only(self) -> None:
        session = SessionRestart(sources_subscribed=True, source_state="started")
        start_session(session, keep_position=True, route_stopped_presentation=True)
        self.assertEqual(session.clock_only_starts, 1)
        self.assertEqual(session.source_starts, 0)


class Patch14ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patch = PATCH14.read_text(encoding="utf-8")
        cls.added = added_lines(cls.patch)

    def test_patch14_through_patch17_are_ordered_and_patch14_is_single_file(self) -> None:
        entries = [line for line in SERIES.read_text(encoding="utf-8").splitlines()
                   if line and not line.startswith("#")]
        start = entries.index(PATCH14_NAME)
        self.assertEqual(tuple(entries[start:start + len(ACTIVE_REPLACEMENT_WINDOW)]),
                         ACTIVE_REPLACEMENT_WINDOW)
        targets = re.findall(r"^diff --git a/(\S+) b/(\S+)$", self.patch, re.MULTILINE)
        self.assertEqual(targets, [("dlls/mfmediaengine/main.c", "dlls/mfmediaengine/main.c")])

    def test_patch14_preserves_project_authorship_and_wine_license_boundary(self) -> None:
        self.assertIn("From: RTSP-on-GE design <rtsp-on-ge@example.invalid>", self.patch)
        notice = NOTICE.read_text(encoding="utf-8")
        self.assertRegex(notice, r"Patch\s+14 is a project-authored change")
        self.assertIn("Wine LGPL-2.1-or-later licensing boundary", notice)

    def test_patch14_detaches_exact_refcounted_fields(self) -> None:
        for statement in (
            "engine->presentation.source = NULL;",
            "engine->presentation.pd = NULL;",
            "engine->presentation.frame_sink = NULL;",
        ):
            self.assertEqual(self.added.count(statement), 1)
        self.assertNotIn("memset(&engine->presentation", self.added)
        self.assertNotIn("engine->presentation.start_position =", self.added)
        self.assertNotIn("engine->presentation.start_position.vt =", self.added)

    def test_pending_variant_ownership_is_explicit(self) -> None:
        self.assertIn("always VT_EMPTY or VT_I8", self.added)
        self.assertIn("media_engine_start_playback() consumes it", self.added)


class Patch15ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patch = PATCH15.read_text(encoding="utf-8")
        cls.added = added_lines(cls.patch)

    def test_patch15_changes_only_media_session(self) -> None:
        targets = re.findall(r"^diff --git a/(\S+) b/(\S+)$", self.patch, re.MULTILINE)
        self.assertEqual(targets, [("dlls/mf/session.c", "dlls/mf/session.c")])

    def test_patch15_routes_only_an_already_stopped_presentation(self) -> None:
        self.assertIn("sources_stopped = session_is_source_nodes_state", self.added)
        self.assertIn("if (sources_stopped)", self.added)
        self.assertIn("session->state = SESSION_STATE_STOPPED;", self.added)
        self.assertIn("session->clock_started = FALSE;", self.added)
        self.assertIn("else if (!keep_position)", self.added)

    def test_patch15_does_not_directly_stop_clock_or_touch_mediaengine(self) -> None:
        if SPLIT_PATCH_PRESENTATION:
            self.assertNotIn("IMFPresentationClock_Stop", self.added)
        else:
            self.assertEqual(self.added.count("IMFPresentationClock_Stop"), 1)
        self.assertNotIn("mfmediaengine", self.added)
        self.assertNotIn("session_set_source_object_state", self.patch)


class Patch16ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patch = PATCH16.read_text(encoding="utf-8")
        cls.added = added_lines(cls.patch)

    def test_patch16_precedes_patch17_and_changes_only_media_session(self) -> None:
        entries = [line for line in SERIES.read_text(encoding="utf-8").splitlines()
                   if line and not line.startswith("#")]
        start = entries.index(PATCH14_NAME)
        self.assertEqual(tuple(entries[start:start + len(ACTIVE_REPLACEMENT_WINDOW)]),
                         ACTIVE_REPLACEMENT_WINDOW)
        targets = re.findall(r"^diff --git a/(\S+) b/(\S+)$", self.patch, re.MULTILINE)
        self.assertEqual(targets, [("dlls/mf/session.c", "dlls/mf/session.c")])

    def test_patch16_checks_and_stops_the_physical_clock(self) -> None:
        self.assertIn("IMFPresentationClock_GetState", self.added)
        self.assertIn("MFCLOCK_STATE_INVALID", self.added)
        self.assertIn("MFCLOCK_STATE_STOPPED", self.added)
        self.assertEqual(self.added.count("IMFPresentationClock_Stop"), 1)
        self.assertIn("session_command_complete_with_event", self.added)
        self.assertIn("IMFMediaSink_SetPresentationClock(sink->sink, NULL)", self.added)
        self.assertIn("TOPO_NODE_EXPECT_INITIAL_CLOCK_STOP", self.added)
        self.assertIn("event_type == MEStreamSinkStopped", self.added)
        self.assertIn("event_type == MEStreamSinkStarted", self.added)

    def test_patch16_does_not_change_transport_or_sink_implementations(self) -> None:
        self.assertNotIn("mfmediaengine", self.patch)
        self.assertNotIn("winedmo", self.patch)
        self.assertNotIn("dlls/mf/sar.c", self.patch)
        self.assertNotIn("video_frame_sink.c", self.patch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--series", type=Path, default=SERIES)
    args, unittest_args = parser.parse_known_args()
    configure_patch_contract(args.series)
    unittest.main(argv=[sys.argv[0], *unittest_args], verbosity=2)
