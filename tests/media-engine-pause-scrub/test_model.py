#!/usr/bin/env python3
"""Deterministic models of GE and A3.8 MediaEngine control ordering."""

from __future__ import annotations

from dataclasses import dataclass
import unittest


# Control bits copied from Wine's enum media_engine_flags.  The full A3.7
# runtime words also contain stream/create flags which are irrelevant to this
# state-machine test.
AUTO_PLAY = 0x40
PAUSED = 0x100
WAITING = 0x200
PLAY_PENDING = 0x20000
SCRUBBING = 0x80000
PAUSE_PENDING = 0x100000
CONTROL_MASK = AUTO_PLAY | PAUSED | WAITING | PLAY_PENDING | SCRUBBING | PAUSE_PENDING

# Poll snapshots from the first A3.7 livestream session (6D503800).  They do
# not reveal the relative order of Pause() and SetAutoPlay(TRUE), but their
# masked control state is sufficient to reproduce GE's stale-start failure.
A37_OBSERVED_FLAGS = (0x881908, 0x8A1A08, 0x9A1948, 0x901948)


@dataclass
class PauseScrubModel:
    scrubbing: bool = True
    pause_pending: bool = False
    play_pending: bool = False
    paused: bool = True
    playback_requested: bool = False
    autoplay: bool = False
    waiting: bool = False
    playback_starts: int = 0
    scrub_sample_starts: int = 0
    rate_restores: int = 0

    def play(self) -> None:
        self.playback_requested = True
        if not self.waiting:
            self.paused = False
            if self.pause_pending or self.scrubbing:
                self.play_pending = True
            else:
                self.playback_starts += 1
            self.waiting = True
        elif self.pause_pending:
            self.play_pending = True

    def pause(self) -> None:
        self.playback_requested = False
        # Scheduling/wait state from an older Play must never outlive Pause.
        self.play_pending = False
        self.waiting = False
        if self.pause_pending:
            self.paused = True
        elif not self.paused:
            self.paused = True
            self.pause_pending = True

    def set_autoplay(self, enabled: bool) -> None:
        # A3.8 deliberately leaves GE's property-only behavior unchanged.
        self.autoplay = enabled

    def session_paused(self) -> None:
        was_scrubbing = self.scrubbing
        if was_scrubbing:
            self.rate_restores += 1

        if not self.pause_pending:
            return

        self.pause_pending = False
        if not self.playback_requested:
            self.play_pending = False
            self.paused = True
        elif was_scrubbing:
            # Play() already made the public state unpaused. Nonzero
            # RateChanged owns only the physical post-scrub restart.
            self.play_pending = True
            self.paused = False
        else:
            self.play_pending = False
            self.paused = False
            self.playback_starts += 1

    def rate_changed(self, rate: float) -> None:
        if not self.scrubbing:
            return
        if rate == 0.0:
            # The rate-zero start asks for the initial scrub frame; it is not
            # an application-visible playback restart.
            self.scrub_sample_starts += 1
            return

        self.scrubbing = False
        if not self.playback_requested:
            self.play_pending = False
            self.paused = True
        elif self.pause_pending:
            # This pause is newer than the Paused event that requested the
            # rate change. Its own completion must reconcile it.
            self.play_pending = True
        elif self.play_pending:
            self.play_pending = False
            self.paused = False
            self.playback_starts += 1


@dataclass
class LegacyGEModel:
    """A direct transcription of the relevant pre-A3.8 GE branches."""

    scrubbing: bool = True
    pause_pending: bool = False
    play_pending: bool = False
    paused: bool = True
    playback_requested: bool = False
    autoplay: bool = False
    waiting: bool = False
    playback_starts: int = 0
    rate_restores: int = 0

    def control_word(self) -> int:
        return (
            (AUTO_PLAY if self.autoplay else 0)
            | (PAUSED if self.paused else 0)
            | (WAITING if self.waiting else 0)
            | (PLAY_PENDING if self.play_pending else 0)
            | (SCRUBBING if self.scrubbing else 0)
            | (PAUSE_PENDING if self.pause_pending else 0)
        )

    def play(self) -> None:
        self.playback_requested = True
        if not self.waiting:
            self.paused = False
            if self.pause_pending or self.scrubbing:
                self.play_pending = True
            else:
                self.playback_starts += 1
            self.waiting = True
        elif self.pause_pending:
            self.play_pending = True

    def pause(self) -> None:
        self.playback_requested = False
        if self.pause_pending:
            self.play_pending = False
            self.paused = True
        elif not self.paused:
            # Model a successful IMFMediaSession_Pause().  Stock GE leaves a
            # preceding Play's PLAY_PENDING bit set in this branch.
            self.waiting = False
            self.paused = True
            self.pause_pending = True

    def set_autoplay(self, enabled: bool) -> None:
        # Stock GE's SetAutoPlay() is a property-only stub.
        self.autoplay = enabled

    def session_paused(self) -> None:
        # This else-if is the regression: SCRUBBING wins and PAUSE_PENDING is
        # not reconciled by the same completion event.
        if self.scrubbing:
            self.rate_restores += 1
        elif self.pause_pending:
            self.pause_pending = False
            if self.play_pending:
                self.play_pending = False
                self.paused = False
                self.playback_starts += 1

    def rate_changed(self, rate: float) -> None:
        if not self.scrubbing or rate == 0.0:
            return
        self.scrubbing = False
        if self.play_pending:
            self.play_pending = False
            self.playback_starts += 1


def assert_settled_control(test: unittest.TestCase,
                           model: PauseScrubModel | LegacyGEModel) -> None:
    """Shared semantic oracle, intentionally independent of either model."""

    test.assertFalse(model.scrubbing, "scrub completion remained pending")
    test.assertFalse(model.pause_pending, "pause completion remained pending")
    test.assertFalse(model.play_pending, "play completion remained pending")
    test.assertFalse(model.waiting, "stale waiting state blocks the next Play")
    test.assertEqual(model.paused, not model.playback_requested,
                     "public pause state contradicts final playback intent")
    expected_starts = 1 if model.playback_requested else 0
    test.assertEqual(model.playback_starts, expected_starts,
                     "session start contradicts final playback intent")


class PauseScrubTests(unittest.TestCase):
    def finish_scrub(self, model: PauseScrubModel) -> None:
        model.session_paused()
        model.rate_changed(1.0)

    def test_initial_paused_scrub_stays_paused(self) -> None:
        model = PauseScrubModel()
        self.finish_scrub(model)
        self.assertTrue(model.paused)
        self.assertFalse(model.scrubbing)
        self.assertEqual(model.playback_starts, 0)

    def test_play_during_scrub_starts_once_after_rate_restore(self) -> None:
        model = PauseScrubModel()
        model.play()
        self.finish_scrub(model)
        self.assertFalse(model.paused)
        self.assertEqual(model.playback_starts, 1)

    def test_observed_play_then_pause_obeys_final_pause(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        self.finish_scrub(model)
        self.assertEqual(
            (model.scrubbing, model.pause_pending, model.play_pending,
             model.paused, model.playback_requested, model.playback_starts),
            (False, False, False, True, False, 0),
        )

    def test_play_pause_play_obeys_final_play(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.play()
        model.session_paused()
        # IMFMediaEngine::Play makes IsPaused false synchronously even while
        # the physical Media Session start is still pending.
        self.assertFalse(model.paused)
        model.rate_changed(1.0)
        self.assertFalse(model.paused)
        self.assertEqual(model.playback_starts, 1)

    def test_play_pause_play_pause_obeys_final_pause(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.play()
        model.pause()
        self.finish_scrub(model)
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)

    def test_play_between_paused_and_rate_events_starts_once(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.session_paused()
        model.play()
        model.rate_changed(1.0)
        self.assertFalse(model.paused)
        self.assertEqual(model.playback_starts, 1)

    def test_new_pause_between_events_survives_old_rate_event(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.play()
        model.session_paused()
        model.pause()
        model.rate_changed(1.0)
        # Play made public PAUSED false while the rate restart was pending,
        # so this newer Pause owns a distinct completion. The older rate
        # event must not consume that PAUSE_PENDING operation.
        self.assertTrue(model.pause_pending)
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)
        model.session_paused()
        self.assertFalse(model.pause_pending)
        assert_settled_control(self, model)

    def test_play_after_final_pause_and_rate_event_resumes_once(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.play()
        model.session_paused()
        model.pause()
        model.rate_changed(1.0)
        model.play()
        model.session_paused()
        self.assertFalse(model.paused)
        self.assertEqual(model.playback_starts, 1)

    def test_non_scrub_pause_completes_without_restart(self) -> None:
        model = PauseScrubModel(scrubbing=False, paused=False,
                                playback_requested=True)
        model.pause()
        model.session_paused()
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)

    def test_non_scrub_pause_then_play_restarts_once(self) -> None:
        model = PauseScrubModel(scrubbing=False, paused=False,
                                playback_requested=True)
        model.pause()
        model.play()
        model.session_paused()
        self.assertFalse(model.paused)
        self.assertEqual(model.playback_starts, 1)

    def test_non_scrub_pause_play_pause_obeys_final_pause(self) -> None:
        model = PauseScrubModel(scrubbing=False, paused=False,
                                playback_requested=True)
        model.pause()
        model.play()
        model.pause()
        model.session_paused()
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)

    def test_stale_play_pending_cannot_override_pause_at_rate_change(self) -> None:
        model = PauseScrubModel(play_pending=True, playback_requested=False)
        model.rate_changed(1.0)
        self.assertTrue(model.paused)
        self.assertFalse(model.play_pending)
        self.assertEqual(model.playback_starts, 0)

    def test_duplicate_completion_events_do_not_duplicate_start(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.play()
        self.finish_scrub(model)
        model.session_paused()
        model.rate_changed(1.0)
        self.assertEqual(model.playback_starts, 1)

    def test_zero_rate_starts_only_the_scrub_sample(self) -> None:
        model = PauseScrubModel()
        model.rate_changed(0.0)
        self.assertTrue(model.scrubbing)
        self.assertEqual(model.scrub_sample_starts, 1)
        self.assertEqual(model.playback_starts, 0)


class RegressionNegativeControlTests(unittest.TestCase):
    def finish_scrub(self, model: PauseScrubModel | LegacyGEModel) -> None:
        model.session_paused()
        model.rate_changed(1.0)

    def test_a37_reproduces_runtime_flags_and_fails_shared_oracle(self) -> None:
        model = LegacyGEModel()
        snapshots = [model.control_word()]

        model.play()
        snapshots.append(model.control_word())
        model.pause()
        model.set_autoplay(True)
        snapshots.append(model.control_word())
        self.finish_scrub(model)
        snapshots.append(model.control_word())

        # This comparison ties the legacy model to the captured A3.7 run,
        # rather than merely constructing an arbitrary known-bad state.
        self.assertEqual(
            snapshots,
            [flags & CONTROL_MASK for flags in A37_OBSERVED_FLAGS],
        )
        self.assertEqual(
            (model.scrubbing, model.pause_pending, model.play_pending,
             model.paused, model.playback_requested, model.playback_starts),
            (False, True, False, True, False, 1),
        )

        # The same semantic assertion used for A3.8 must reject stock GE:
        # playback started even though final explicit intent is Pause, while
        # PAUSED|PAUSE_PENDING remained visible to AVPro.
        with self.assertRaisesRegex(
                AssertionError, "pause completion remained pending"):
            assert_settled_control(self, model)

    def test_a37_fails_if_autoplay_precedes_pause_between_polls(self) -> None:
        model = LegacyGEModel()
        snapshots = [model.control_word()]
        model.play()
        snapshots.append(model.control_word())
        model.set_autoplay(True)
        model.pause()
        snapshots.append(model.control_word())
        self.finish_scrub(model)
        snapshots.append(model.control_word())

        self.assertEqual(
            snapshots,
            [flags & CONTROL_MASK for flags in A37_OBSERVED_FLAGS],
        )
        with self.assertRaisesRegex(
                AssertionError, "pause completion remained pending"):
            assert_settled_control(self, model)

    def test_a38_passes_if_pause_is_last_unobserved_call(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.set_autoplay(True)
        model.pause()
        self.finish_scrub(model)

        assert_settled_control(self, model)
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)

    def test_a38_passes_if_autoplay_is_last_unobserved_call(self) -> None:
        model = PauseScrubModel()
        model.play()
        model.pause()
        model.set_autoplay(True)
        self.finish_scrub(model)

        assert_settled_control(self, model)
        self.assertTrue(model.autoplay)
        self.assertTrue(model.paused)
        self.assertEqual(model.playback_starts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
