#!/usr/bin/env python3
"""Executable policy model for Patch 4/6/7/8 VOD and session control."""

from dataclasses import dataclass, field
import unittest


DEBOUNCE_MS = 250
SYNC_TOLERANCE_SECONDS = 3.0
DUPLICATE_TOLERANCE_SECONDS = 0.001
MAX_PACKETS = 8
MAX_BYTES = 8 * 1024 * 1024
EMERGENCY_MAX_PACKETS = 1024
EMERGENCY_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class StreamQueue:
    active: bool = True
    eos: bool = False
    packets: int = 0
    bytes: int = 0


def queues_should_stop(streams: list[StreamQueue]) -> bool:
    active = [stream for stream in streams if stream.active and not stream.eos]
    if not active:
        return True

    primed = all(stream.packets > 0 for stream in active)
    any_full = any(
        stream.packets >= MAX_PACKETS or stream.bytes >= MAX_BYTES
        for stream in active
    )
    emergency_full = (
        sum(stream.packets for stream in active) >= EMERGENCY_MAX_PACKETS
        or sum(stream.bytes for stream in active) >= EMERGENCY_MAX_BYTES
    )
    return emergency_full or (primed and any_full)


@dataclass
class SeekController:
    current: float = 0.0
    paused: bool = False
    shutdown: bool = False
    schedule_fails: bool = False
    deferred: float | None = None
    deferred_at: int = 0
    timer_due: int | None = None
    backend_seeking: bool = False
    current_seek: float | None = None
    next_seek: float | None = None
    committed: list[float] = field(default_factory=list)
    resume_count: int = 0

    def _start_backend_seek(self, target: float) -> None:
        if self.backend_seeking:
            if (
                self.current_seek is not None
                and abs(self.current_seek - target) <= DUPLICATE_TOLERANCE_SECONDS
            ):
                self.next_seek = None
            elif (
                self.next_seek is None
                or abs(self.next_seek - target) > DUPLICATE_TOLERANCE_SECONDS
            ):
                self.next_seek = target
            return
        self.backend_seeking = True
        self.current_seek = target
        self.committed.append(target)

    def _commit_deferred(self) -> None:
        target = self.deferred
        if target is None:
            return
        self.deferred = None
        if (
            self.backend_seeking
            and self.current_seek is not None
            and abs(self.current_seek - target) <= DUPLICATE_TOLERANCE_SECONDS
        ):
            # The newest duplicate target cancels an older queued seek.
            self.next_seek = None
            return
        self._start_backend_seek(target)

    def _commit_paused(self, target: float) -> None:
        self.deferred = None
        self.current = target
        self.committed.append(target)

    def request_direct(self, target: float) -> None:
        """Model IMFMediaEngine::SetCurrentTime superseding a pending Ex seek."""
        target = max(0.0, target)
        self.deferred = None
        if self.paused:
            self._commit_paused(target)
        else:
            self._start_backend_seek(target)

    def complete_backend_seek(self) -> None:
        """Model MESessionStarted: collapse sync echoes, preserve real seeks."""
        if not self.backend_seeking:
            return
        completed = self.current_seek
        self.current = completed if completed is not None else self.current
        self.backend_seeking = False
        self.current_seek = None

        if self.deferred is not None:
            if abs(self.deferred - self.current) <= DUPLICATE_TOLERANCE_SECONDS:
                self.deferred = None
                self.next_seek = None
            else:
                self.next_seek = None
                return

        if self.next_seek is not None:
            target = self.next_seek
            self.next_seek = None
            if abs(target - self.current) > DUPLICATE_TOLERANCE_SECONDS:
                self._start_backend_seek(target)
                return

        self.resume_count += 1

    def request(self, target: float, now_ms: int) -> None:
        target = max(0.0, target)
        if self.shutdown:
            return
        if (
            not self.paused
            and not self.backend_seeking
            and self.deferred is None
            and abs(self.current - target) <= SYNC_TOLERANCE_SECONDS
        ):
            return
        if self.paused:
            self._commit_paused(target)
            return
        if (
            self.deferred is not None
            and abs(self.deferred - target) <= DUPLICATE_TOLERANCE_SECONDS
        ):
            return

        self.deferred = target
        self.deferred_at = now_ms
        if self.timer_due is None:
            if self.schedule_fails:
                self._commit_deferred()
            else:
                self.timer_due = now_ms + DEBOUNCE_MS

    def run_timer(self, now_ms: int) -> None:
        if self.timer_due is None or now_ms < self.timer_due:
            return
        self.timer_due = None
        if self.shutdown or self.deferred is None:
            return
        elapsed = now_ms - self.deferred_at
        if elapsed < DEBOUNCE_MS:
            self.timer_due = now_ms + (DEBOUNCE_MS - elapsed)
        else:
            self._commit_deferred()

    def poll(self, now_ms: int) -> None:
        if self.deferred is not None and now_ms - self.deferred_at >= DEBOUNCE_MS:
            self._commit_deferred()

    def replace_source(self) -> None:
        self.deferred = None
        self.next_seek = None


@dataclass
class SessionDemandController:
    """Model sink demand surviving a source/transform seek restart."""

    sink_requests: int = 0
    transform_requests: int = 0
    source_requests: int = 0
    sink_flushes: int = 0

    def request_sample(self) -> None:
        self.sink_requests += 1
        self.transform_requests += 1
        self.source_requests += 1

    def begin_seek_restart(self) -> None:
        self.transform_requests = 0

    def finish_source_restart(self) -> None:
        if self.sink_requests:
            self.sink_requests -= 1
            self.request_sample()
        self.sink_flushes += 1


@dataclass
class AudioTimelineController:
    """Model WineDMO's audio timestamp origin across output and flush."""

    split_aac_output: bool = False
    output_pts_adjust: int | None = None
    pts_offset: int = 0
    audio_started: bool = False

    def flush(self) -> None:
        # A decoder flush starts a new output run, not a new timeline.
        self.audio_started = False

    def input_discontinuity(self) -> None:
        # Preserve GE's existing explicit-discontinuity re-anchor policy.
        self.pts_offset = 0
        self.output_pts_adjust = None
        self.audio_started = False

    def input(self, pts: int) -> None:
        if pts < self.pts_offset:
            self.pts_offset = pts

    def output(self, base_pts: int | None) -> tuple[int | None, bool]:
        if base_pts is not None and self.pts_offset:
            base_pts -= self.pts_offset

        if (
            not self.audio_started
            and self.output_pts_adjust is None
            and base_pts is not None
            and not self.split_aac_output
        ):
            self.output_pts_adjust = base_pts

        pts = base_pts
        if pts is not None and self.output_pts_adjust is not None:
            pts -= self.output_pts_adjust

        discontinuous = not self.audio_started
        self.audio_started = True
        return pts, discontinuous


class QueuePolicyTests(unittest.TestCase):
    def test_no_active_stream_stops_demux(self) -> None:
        self.assertTrue(queues_should_stop([StreamQueue(active=False)]))

    def test_full_video_cannot_starve_empty_audio(self) -> None:
        self.assertFalse(
            queues_should_stop(
                [StreamQueue(packets=MAX_PACKETS), StreamQueue(packets=0)]
            )
        )

    def test_primed_streams_obey_shared_bound(self) -> None:
        self.assertTrue(
            queues_should_stop(
                [StreamQueue(packets=MAX_PACKETS), StreamQueue(packets=1)]
            )
        )

    def test_emergency_bound_stops_an_unprimed_malformed_stream(self) -> None:
        self.assertTrue(
            queues_should_stop(
                [StreamQueue(packets=EMERGENCY_MAX_PACKETS), StreamQueue(packets=0)]
            )
        )

    def test_emergency_bound_is_aggregate_across_selected_streams(self) -> None:
        self.assertTrue(
            queues_should_stop(
                [
                    StreamQueue(packets=EMERGENCY_MAX_PACKETS // 2),
                    StreamQueue(packets=EMERGENCY_MAX_PACKETS // 2),
                    StreamQueue(packets=0),
                ]
            )
        )

    def test_soft_bound_still_allows_normal_interleave_to_reach_audio(self) -> None:
        self.assertFalse(
            queues_should_stop(
                [StreamQueue(packets=MAX_PACKETS), StreamQueue(packets=0)]
            )
        )

    def test_eos_stream_does_not_block_active_stream(self) -> None:
        self.assertTrue(
            queues_should_stop(
                [StreamQueue(packets=MAX_PACKETS), StreamQueue(eos=True)]
            )
        )


class SeekPolicyTests(unittest.TestCase):
    def test_initial_and_periodic_near_clock_sync_are_suppressed(self) -> None:
        controller = SeekController()
        controller.request(0.0, 0)
        for now_ms in (10_000, 20_000, 30_000):
            controller.current = now_ms / 1000.0 - 0.4
            controller.request(now_ms / 1000.0, now_ms)
        self.assertEqual(controller.committed, [])

    def test_slider_burst_commits_only_the_final_target(self) -> None:
        controller = SeekController(current=5.0)
        targets = [100.0 + index for index in range(60)]
        for index, target in enumerate(targets):
            now_ms = index * 15
            controller.request(target, now_ms)
            controller.run_timer(now_ms)

        controller.run_timer(900)
        self.assertEqual(controller.committed, [])
        controller.run_timer(1_135)
        self.assertEqual(controller.committed, [targets[-1]])

    def test_paused_seek_is_immediate(self) -> None:
        controller = SeekController(paused=True)
        controller.request(42.0, 0)
        self.assertEqual(controller.committed, [42.0])

    def test_schedule_failure_falls_back_to_immediate_flush(self) -> None:
        controller = SeekController(current=0.0, schedule_fails=True)
        controller.request(42.0, 0)
        self.assertEqual(controller.committed, [42.0])

    def test_source_replacement_and_shutdown_make_timer_noop(self) -> None:
        replaced = SeekController()
        replaced.request(42.0, 0)
        replaced.replace_source()
        replaced.run_timer(DEBOUNCE_MS)
        self.assertEqual(replaced.committed, [])

        stopped = SeekController()
        stopped.request(42.0, 0)
        stopped.shutdown = True
        stopped.run_timer(DEBOUNCE_MS)
        self.assertEqual(stopped.committed, [])

    def test_paused_seek_supersedes_an_older_deferred_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request(40.0, 0)
        controller.paused = True
        controller.request(55.0, 50)
        controller.run_timer(DEBOUNCE_MS)
        self.assertEqual(controller.committed, [55.0])

    def test_direct_seek_supersedes_an_older_deferred_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request(40.0, 0)
        controller.request_direct(55.0)
        controller.run_timer(DEBOUNCE_MS)
        self.assertEqual(controller.committed, [55.0])

    def test_inflight_completion_preserves_newer_deferred_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(10.0)
        controller.request(42.0, 0)
        controller.complete_backend_seek()
        self.assertEqual(controller.deferred, 42.0)
        controller.run_timer(DEBOUNCE_MS)
        self.assertEqual(controller.committed, [10.0, 42.0])

    def test_completion_first_collapses_ex_seek_at_duplicate_boundary(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(0.0)
        controller.request(DUPLICATE_TOLERANCE_SECONDS, 0)
        controller.complete_backend_seek()
        controller.run_timer(DEBOUNCE_MS)

        self.assertEqual(controller.committed, [0.0])
        self.assertIsNone(controller.deferred)
        self.assertEqual(controller.resume_count, 1)

    def test_timer_first_collapses_ex_seek_at_duplicate_boundary(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(0.0)
        controller.request(DUPLICATE_TOLERANCE_SECONDS, 0)
        controller.run_timer(DEBOUNCE_MS)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [0.0])
        self.assertIsNone(controller.next_seek)
        self.assertEqual(controller.resume_count, 1)

    def test_completion_first_preserves_ex_seek_outside_duplicate_boundary(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(0.0)
        target = DUPLICATE_TOLERANCE_SECONDS + 0.0001
        controller.request(target, 0)
        controller.complete_backend_seek()
        controller.run_timer(DEBOUNCE_MS)

        self.assertEqual(controller.committed, [0.0, target])
        self.assertEqual(controller.resume_count, 0)
        controller.complete_backend_seek()
        self.assertEqual(controller.resume_count, 1)

    def test_timer_first_preserves_ex_seek_outside_duplicate_boundary(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(0.0)
        target = DUPLICATE_TOLERANCE_SECONDS + 0.0001
        controller.request(target, 0)
        controller.run_timer(DEBOUNCE_MS)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [0.0, target])
        self.assertEqual(controller.resume_count, 0)
        controller.complete_backend_seek()
        self.assertEqual(controller.resume_count, 1)

    def test_newer_duplicate_ex_seek_cancels_an_older_queued_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(42.0)
        controller.request_direct(60.0)
        controller.request(42.0005, 0)
        controller.complete_backend_seek()
        controller.run_timer(DEBOUNCE_MS)

        self.assertEqual(controller.committed, [42.0])
        self.assertIsNone(controller.next_seek)
        self.assertEqual(controller.resume_count, 1)

    def test_timer_first_duplicate_ex_seek_cancels_an_older_queued_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(42.0)
        controller.request_direct(60.0)
        controller.request(42.0005, 0)
        controller.run_timer(DEBOUNCE_MS)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [42.0])
        self.assertIsNone(controller.next_seek)
        self.assertEqual(controller.resume_count, 1)

    def test_direct_current_duplicate_cancels_an_older_direct_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(42.0)
        controller.request_direct(60.0)
        controller.request_direct(42.0005)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [42.0])
        self.assertIsNone(controller.next_seek)
        self.assertEqual(controller.resume_count, 1)

    def test_direct_current_duplicate_cancels_a_timer_queued_target(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(42.0)
        controller.request(60.0, 0)
        controller.run_timer(DEBOUNCE_MS)
        controller.request_direct(42.0005)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [42.0])
        self.assertIsNone(controller.next_seek)
        self.assertEqual(controller.resume_count, 1)

    def test_direct_nearby_seek_is_not_mistaken_for_a_sync_echo(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(42.0)
        controller.request_direct(43.0)
        controller.complete_backend_seek()

        self.assertEqual(controller.committed, [42.0, 43.0])
        self.assertEqual(controller.resume_count, 0)
        controller.complete_backend_seek()
        self.assertEqual(controller.resume_count, 1)

    def test_newer_deferred_target_beats_an_older_queued_direct_seek(self) -> None:
        controller = SeekController(current=0.0)
        controller.request_direct(10.0)
        controller.request_direct(20.0)
        controller.request(30.0, 0)
        controller.complete_backend_seek()
        controller.run_timer(DEBOUNCE_MS)
        self.assertEqual(controller.committed, [10.0, 30.0])

    def test_opportunistic_poll_flush_leaves_timer_as_noop(self) -> None:
        controller = SeekController()
        controller.request(42.0, 0)
        controller.poll(DEBOUNCE_MS)
        controller.run_timer(DEBOUNCE_MS)
        self.assertEqual(controller.committed, [42.0])


class SessionDemandTests(unittest.TestCase):
    def test_outstanding_sink_demand_is_reprimed_after_seek(self) -> None:
        controller = SessionDemandController()
        controller.request_sample()
        controller.begin_seek_restart()
        controller.finish_source_restart()

        self.assertEqual(controller.sink_requests, 1)
        self.assertEqual(controller.transform_requests, 1)
        self.assertEqual(controller.source_requests, 2)
        self.assertEqual(controller.sink_flushes, 1)

    def test_zero_sink_demand_is_not_fabricated_during_restart(self) -> None:
        controller = SessionDemandController()
        controller.begin_seek_restart()
        controller.finish_source_restart()

        self.assertEqual(controller.sink_requests, 0)
        self.assertEqual(controller.transform_requests, 0)
        self.assertEqual(controller.source_requests, 0)
        self.assertEqual(controller.sink_flushes, 1)


class AudioTimelineTests(unittest.TestCase):
    def test_initial_nonzero_timestamp_is_normalized(self) -> None:
        controller = AudioTimelineController()
        self.assertEqual(controller.output(10_000_000), (0, True))
        self.assertEqual(controller.output(10_232_200), (232_200, False))

    def test_seek_flush_preserves_the_established_timeline(self) -> None:
        controller = AudioTimelineController()
        controller.output(0)
        controller.flush()
        self.assertEqual(controller.output(7_802_601_360), (7_802_601_360, True))

    def test_nonzero_initial_origin_remains_valid_after_seek(self) -> None:
        controller = AudioTimelineController()
        controller.output(100_000_000)
        controller.flush()
        self.assertEqual(controller.output(7_900_000_000), (7_800_000_000, True))

    def test_flush_before_first_output_still_establishes_an_origin(self) -> None:
        controller = AudioTimelineController()
        controller.flush()
        self.assertEqual(controller.output(100_000_000), (0, True))

    def test_missing_first_pts_does_not_rebase_midstream(self) -> None:
        controller = AudioTimelineController()
        self.assertEqual(controller.output(None), (None, True))
        self.assertEqual(controller.output(100_000_000), (100_000_000, False))

    def test_negative_preroll_mapping_survives_flush(self) -> None:
        controller = AudioTimelineController()
        controller.input(-232_200)
        self.assertEqual(controller.output(-232_200), (0, True))
        controller.flush()
        controller.input(7_800_000_000)
        self.assertEqual(controller.output(7_800_000_000), (7_800_232_200, True))

    def test_repeated_forward_and_backward_seeks_keep_one_timeline(self) -> None:
        controller = AudioTimelineController()
        controller.output(100_000_000)
        controller.flush()
        self.assertEqual(controller.output(7_900_000_000), (7_800_000_000, True))
        controller.flush()
        self.assertEqual(controller.output(3_900_000_000), (3_800_000_000, True))

    def test_explicit_discontinuity_retains_ge_reanchor_policy(self) -> None:
        controller = AudioTimelineController()
        controller.output(0)
        controller.input_discontinuity()
        self.assertEqual(controller.output(9_000_000_000), (0, True))

    def test_split_aac_keeps_absolute_timestamps_across_flush(self) -> None:
        controller = AudioTimelineController(split_aac_output=True)
        self.assertEqual(controller.output(150_000_000), (150_000_000, True))
        controller.flush()
        self.assertEqual(controller.output(2_000_000_000), (2_000_000_000, True))

if __name__ == "__main__":
    unittest.main()
