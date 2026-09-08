#!/usr/bin/env python3
"""Model terminal seek-error ownership; this does not execute Wine C.

The old path retains an irreversible generic failure but neither the session
nor engine recognizes its MEError. The proposed path normalizes only that
post-flush failure and reuses the existing current-source terminal route.
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
import unittest


S_OK = 0
GENERIC_FAILURE = 0xD0000001  # HRESULT_FROM_NT(STATUS_UNSUCCESSFUL)
INVALID_REQUEST = 0xC00D36B2
INVALID_POSITION = 0xC00D36E5
CANCELLED = 0xC00D36ED
NET_TIMEOUT = 0xC00D4278
NET_READ = 0xC00D426A
NETWORK_TERMINAL = {CANCELLED, NET_TIMEOUT, NET_READ}
TERMINAL = NETWORK_TERMINAL | {INVALID_POSITION}
ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/proposed/0024-winedmo-report-terminal-seek-failures.patch"
WINE_TREE = None


@dataclass(eq=False)
class Source:
    error: int = S_OK
    producer_stopped: bool = False
    epoch_odd: bool = False
    requests_active: bool = True
    retries: int = 0
    events: list[int] = field(default_factory=list)

    def seek(self, error, *, post_flush=True, normalize=True):
        if not post_flush:
            return INVALID_REQUEST  # Synchronous validation did not mutate state.
        if error == S_OK:
            return self.error
        self.epoch_odd = True
        self.producer_stopped = True
        self.requests_active = False
        if normalize and error not in NETWORK_TERMINAL:
            error = INVALID_POSITION
        if self.error == S_OK:
            self.error = error
            self.events.append(error)
        return self.error

    def explicit_start(self):
        """An application call can re-signal a retained error, not restart I/O."""
        if self.error != S_OK:
            self.events.append(self.error)
        return self.error


@dataclass
class Engine:
    source: Source
    generation: int = 1
    presentation_generation: int = 1
    shut_down: bool = False
    error_kind: str | None = None
    extended_error: int = S_OK
    pending: set[str] = field(default_factory=lambda: {
        "seek", "deferred_seek", "play", "pause", "waiting", "scrub"
    })
    playback_requested: bool = True
    events: list[str] = field(default_factory=list)

    def receive(self, source, error):
        if (self.shut_down or source is not self.source
                or self.generation != self.presentation_generation
                or error not in TERMINAL):
            return
        self.error_kind = (
            "ABORTED" if error == CANCELLED else
            "DECODE" if error == INVALID_POSITION else "NETWORK"
        )
        self.extended_error = error
        self.pending.clear()
        self.playback_requested = False
        self.events.append("ERROR")

    def reopen(self, source):
        self.generation += 1
        self.presentation_generation = self.generation
        self.source = source
        self.error_kind, self.extended_error = None, S_OK
        self.pending.clear()
        self.playback_requested = True


@dataclass
class Session:
    engine: Engine
    source: Source
    allow_seek_failure: bool = True
    pending_start: bool = True
    forwarded: bool = False
    completions: int = 0

    def receive(self, source, error):
        accepted = TERMINAL if self.allow_seek_failure else NETWORK_TERMINAL
        if source is not self.source or error not in accepted:
            return
        if not self.forwarded:
            self.engine.receive(source, error)
            self.forwarded = True
        if self.pending_start:
            self.pending_start = False
            self.completions += 1  # No successful Started/Seeked completion.


class SeekFailureTests(unittest.TestCase):
    def make_chain(self, **session_options):
        source = Source()
        engine = Engine(source)
        return source, engine, Session(engine, source, **session_options)

    def test_legacy_generic_error_strands_the_start_command(self):
        source, engine, session = self.make_chain(allow_seek_failure=False)
        error = source.seek(GENERIC_FAILURE, normalize=False)
        session.receive(source, error)
        self.assertTrue(source.producer_stopped)
        self.assertTrue(session.pending_start)
        self.assertIn("seek", engine.pending)
        self.assertEqual(engine.events, [])

    def test_post_flush_failure_finishes_with_decode_error_not_success(self):
        source, engine, session = self.make_chain()
        session.receive(source, source.seek(GENERIC_FAILURE))
        self.assertEqual((engine.error_kind, engine.extended_error),
                         ("DECODE", INVALID_POSITION))
        self.assertEqual(engine.events, ["ERROR"])
        self.assertFalse(session.pending_start)
        self.assertEqual(session.completions, 1)
        self.assertEqual(engine.pending, set())
        self.assertFalse(engine.playback_requested)
        self.assertTrue(source.epoch_odd)
        self.assertTrue(source.producer_stopped)
        self.assertFalse(source.requests_active)
        self.assertEqual(source.retries, 0)

    def test_preflush_argument_rejection_remains_nonterminal(self):
        source = Source()
        self.assertEqual(source.seek(INVALID_REQUEST, post_flush=False),
                         INVALID_REQUEST)
        self.assertEqual(source.error, S_OK)
        self.assertEqual(source.events, [])
        self.assertFalse(source.epoch_odd)
        self.assertFalse(source.producer_stopped)
        self.assertTrue(source.requests_active)

    def test_successful_seek_is_not_normalized_to_failure(self):
        source = Source()
        self.assertEqual(source.seek(S_OK), S_OK)
        self.assertEqual(source.events, [])
        self.assertFalse(source.producer_stopped)
        self.assertFalse(source.epoch_odd)

    def test_cancel_and_known_network_errors_keep_their_classification(self):
        for error, kind in ((CANCELLED, "ABORTED"), (NET_TIMEOUT, "NETWORK"),
                            (NET_READ, "NETWORK")):
            with self.subTest(error=error):
                source, engine, session = self.make_chain()
                session.receive(source, source.seek(error))
                self.assertEqual((engine.error_kind, engine.extended_error),
                                 (kind, error))

    def test_first_terminal_error_wins_over_later_seek_failure(self):
        source, engine, session = self.make_chain()
        source.seek(NET_TIMEOUT)
        session.receive(source, source.seek(GENERIC_FAILURE))
        self.assertEqual(source.events, [NET_TIMEOUT])
        self.assertEqual(engine.extended_error, NET_TIMEOUT)

    def test_another_source_cannot_finish_current_start(self):
        source, engine, session = self.make_chain()
        other = Source()
        session.receive(other, other.seek(GENERIC_FAILURE))
        self.assertTrue(session.pending_start)
        self.assertEqual(engine.events, [])

    def test_engine_rejects_old_identity_and_old_generation(self):
        source, engine, _ = self.make_chain()
        engine.receive(Source(), INVALID_POSITION)
        engine.generation += 1  # Replacement began; old presentation still held.
        engine.receive(source, INVALID_POSITION)
        self.assertEqual(engine.events, [])
        self.assertIsNone(engine.error_kind)

    def test_shutdown_engine_ignores_error(self):
        source, engine, _ = self.make_chain()
        engine.shut_down = True
        engine.receive(source, INVALID_POSITION)
        self.assertEqual(engine.events, [])

    def test_unclassified_generic_error_is_not_forwarded(self):
        source, engine, session = self.make_chain()
        session.receive(source, GENERIC_FAILURE)
        engine.receive(source, GENERIC_FAILURE)
        self.assertTrue(session.pending_start)
        self.assertEqual(engine.events, [])

    def test_explicit_start_resignal_completes_without_retries_or_duplicate_error(self):
        source, engine, session = self.make_chain()
        session.receive(source, source.seek(GENERIC_FAILURE))
        session.pending_start = True
        session.receive(source, source.explicit_start())
        self.assertEqual(session.completions, 2)
        self.assertEqual(engine.events, ["ERROR"])
        self.assertEqual(source.retries, 0)
        self.assertTrue(source.producer_stopped)

    def test_manual_new_source_clears_error_and_rejects_old_error(self):
        source, engine, session = self.make_chain()
        session.receive(source, source.seek(GENERIC_FAILURE))
        fresh = Source()
        engine.reopen(fresh)
        engine.receive(source, INVALID_POSITION)
        self.assertEqual(fresh.explicit_start(), S_OK)
        self.assertIsNone(engine.error_kind)
        self.assertEqual(engine.extended_error, S_OK)
        self.assertTrue(engine.playback_requested)
        self.assertFalse(fresh.producer_stopped)
        self.assertEqual(engine.events, ["ERROR"])

    def test_overlay_is_scoped_and_keeps_existing_identity_guards(self):
        patch = PATCH.read_text()
        added = "\n".join(line[1:] for line in patch.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        self.assertEqual(patch.count("diff --git "), 3)
        self.assertIn("error = MF_E_INVALID_POSITION;", added)
        self.assertIn("media_source_queue_demux_error(source, error);", added)
        self.assertIn("MF_MEDIA_ENGINE_ERR_DECODE", added)
        self.assertIn("original error %#lx", added)
        self.assertNotIn("MF_MEDIA_ENGINE_EVENT_ENDED", added)
        self.assertNotIn("MESessionStarted", added)
        self.assertNotIn("MF_MEDIA_ENGINE_EVENT_SEEKED", added)
        removed = "\n".join(line[1:] for line in patch.splitlines()
                            if line.startswith("-") and not line.startswith("---"))
        self.assertNotIn("source_generation", removed)
        self.assertNotIn("source.punkVal", removed)
        self.assertNotIn("SOURCE_FLAG_TERMINAL_ERROR_FORWARDED", removed)


class IntegratedSourceTests(unittest.TestCase):
    """Check actual function bodies, not whether a patch filename is selected."""

    @classmethod
    def setUpClass(cls):
        if WINE_TREE is None:
            raise unittest.SkipTest("pass --wine-tree for integrated source contracts")
        cls.sources = {
            name: (WINE_TREE / path).read_text()
            for name, path in (
                ("source", "dlls/winedmo/media_source.c"),
                ("session", "dlls/mf/session.c"),
                ("engine", "dlls/mfmediaengine/main.c"),
            )
        }

    def body(self, text, name):
        match = re.search(r"^static [^\n]*\b" + re.escape(name)
                          + r"\([^;]*?\n\{.*?^}", text, re.M | re.S)
        self.assertIsNotNone(match, "missing function " + name)
        return " ".join(match.group().split())

    def ordered(self, text, *terms):
        positions = []
        for term in terms:
            self.require(text, term)
            positions.append(text.index(term))
        self.assertEqual(positions, sorted(positions))

    def require(self, text, term):
        self.assertTrue(term in text, "missing source contract: " + term)

    def assert_source_contract(self, sources):
        start = self.body(sources["source"], "media_source_start")
        self.ordered(start,
                     "media_source_begin_demux_transition(source);",
                     "media_packet_queue_flush(stream);",
                     "if (FAILED(seek_error) || status)",
                     "error = MF_E_INVALID_POSITION;",
                     "*error_queued = media_source_queue_demux_error(source, error);")
        self.require(start, "if (error != MF_E_OPERATION_CANCELLED && error != MF_E_NET_TIMEOUT "
                     "&& error != MF_E_NET_READ)")
        self.require(start, "original error %#lx")
        self.require(start, "return media_source_get_demux_error(source);")
        self.assertNotIn("stream->eos = TRUE", start)
        # Validation before the asynchronous post-flush operation is unchanged.
        public_start = self.body(sources["source"], "media_source_Start")
        self.require(public_start, "hr = MF_E_INVALIDREQUEST;")
        self.assertNotIn("MF_E_INVALID_POSITION", public_start)

        publish = self.body(sources["source"], "media_source_queue_demux_error")
        self.ordered(publish,
                     "EnterCriticalSection(&source->demux_error_cs);",
                     "if (!media_source_set_demux_error(source, error))",
                     "source->demux_thread_shutdown = true;",
                     "MEError",
                     "WakeAllConditionVariable(&stream->queue_cv);")
        async_start = self.body(sources["source"], "source_async_commands_Invoke")
        self.require(async_start, "if (!error_queued)")

        session = self.body(sources["session"], "session_events_callback_Invoke")
        session_error = session.split("case MEError:", 1)[1].split("default:", 1)[0]
        allowed = ("else if (hr == MF_E_OPERATION_CANCELLED || hr == MF_E_NET_TIMEOUT "
                   "|| hr == MF_E_NET_READ || hr == MF_E_INVALID_POSITION)")
        self.ordered(session_error,
                     "session_get_media_source(session, (IMFMediaSource *)event_source)",
                     allowed,
                     "if (!(session_source->flags & SOURCE_FLAG_TERMINAL_ERROR_FORWARDED))",
                     "IMFMediaEventQueue_QueueEventParamUnk",
                     "(IUnknown *)(IMFMediaSource *)event_source",
                     "session_source->flags |= SOURCE_FLAG_TERMINAL_ERROR_FORWARDED;",
                     "session_command_complete(session);")
        self.assertNotIn("session_command_complete_with_event", session_error)
        self.assertNotIn("COMMAND_STATE_RESTARTING_SOURCES", session_error)

        engine = self.body(sources["engine"], "media_engine_session_events_Invoke")
        error = engine.split("case MEError:", 1)[1].split("case MEEndOfPresentation:", 1)[0]
        self.require(error, "(error != MF_E_OPERATION_CANCELLED && error != MF_E_NET_TIMEOUT "
                     "&& error != MF_E_NET_READ && error != MF_E_INVALID_POSITION)")
        self.require(error, "source.vt != VT_UNKNOWN || !source.punkVal")
        self.require(error, "if (error == MF_E_OPERATION_CANCELLED) "
                      "error_code = MF_MEDIA_ENGINE_ERR_ABORTED; "
                      "else if (error == MF_E_INVALID_POSITION) "
                      "error_code = MF_MEDIA_ENGINE_ERR_DECODE; "
                      "else error_code = MF_MEDIA_ENGINE_ERR_NETWORK;")
        self.ordered(error,
                     "engine->presentation.generation != engine->source_generation",
                     "source.punkVal != (IUnknown *)engine->presentation.source",
                     "engine->error_code = error_code;",
                     "engine->extended_code = error;",
                     "MF_MEDIA_ENGINE_EVENT_ERROR")
        for cleared in ("playback_requested = FALSE", "current_seek = NAN",
                        "next_seek = NAN", "deferred_seek = NAN"):
            self.require(error, "engine->" + cleared)
        for forbidden in ("MF_MEDIA_ENGINE_EVENT_ENDED", "MF_MEDIA_ENGINE_EVENT_SEEKED",
                          "media_engine_set_current_time(", "media_engine_start_playback("):
            self.assertNotIn(forbidden, error)
        reopen = self.body(sources["engine"], "media_engine_set_source")
        self.ordered(reopen, "generation = ++engine->source_generation;",
                     "engine->error_code = MF_MEDIA_ENGINE_ERR_NOERROR;",
                     "engine->extended_code = S_OK;")

    def test_integrated_source_delivers_only_terminal_current_source_seek_failure(self):
        self.assert_source_contract(self.sources)

    def test_missing_normalization_delivery_or_identity_guard_is_rejected(self):
        for name, old, new in (
            ("source", "error = MF_E_INVALID_POSITION;", "error = E_FAIL;"),
            ("source", "*error_queued = media_source_queue_demux_error(source, error);",
             "media_source_set_demux_error(source, error);"),
            ("source", "if (!media_source_set_demux_error(source, error))", "if (FALSE)"),
            ("session", "|| hr == MF_E_NET_READ || hr == MF_E_INVALID_POSITION)",
             "|| hr == MF_E_NET_READ)"),
            ("session", "(session_source = session_get_media_source(session, (IMFMediaSource *)event_source))",
             "(session_source = NULL)"),
            ("session", "if (!(session_source->flags & SOURCE_FLAG_TERMINAL_ERROR_FORWARDED))",
             "if (TRUE)"),
            ("engine", "&& error != MF_E_NET_READ && error != MF_E_INVALID_POSITION)",
             "&& error != MF_E_NET_READ)"),
            ("engine", "error_code = MF_MEDIA_ENGINE_ERR_DECODE;",
             "error_code = MF_MEDIA_ENGINE_ERR_NETWORK;"),
            ("engine", "engine->presentation.generation != engine->source_generation", "FALSE"),
            ("engine", "source.punkVal != (IUnknown *)engine->presentation.source", "FALSE"),
        ):
            with self.subTest(file=name, regression=old):
                mutated = self.sources.copy()
                mutated[name] = mutated[name].replace(old, new, 1)
                self.assertTrue(mutated[name] != self.sources[name],
                                "mutation target absent: " + old)
                with self.assertRaises(AssertionError):
                    self.assert_source_contract(mutated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wine-tree", type=Path)
    args, remaining = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
