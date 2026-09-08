#!/usr/bin/env python3
"""Deterministic model of A3.12 terminal network-error ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import unittest


class DemuxResult(Enum):
    OK = auto()
    EOF = auto()
    EXIT = auto()
    TIMEOUT = auto()
    READ_FAILURE = auto()
    FILTER_FAILURE = auto()


class Status(Enum):
    SUCCESS = auto()
    END_OF_FILE = auto()
    UNSUCCESSFUL = auto()
    CANCELLED = auto()
    IO_TIMEOUT = auto()
    CONNECTION_DISCONNECTED = auto()


class MediaError(Enum):
    NONE = auto()
    ABORTED = auto()
    NETWORK = auto()


def classify_read(result: DemuxResult, *, direct_url: bool,
                  cancelled: bool = False) -> Status:
    """Mirror the boundary between FFmpeg and WineDMO."""

    if result is DemuxResult.OK:
        return Status.SUCCESS
    if result is DemuxResult.EOF:
        return Status.END_OF_FILE
    if not direct_url or result is DemuxResult.FILTER_FAILURE:
        # The project deliberately preserves Wine's local-byte-stream policy.
        # Bitstream-filter failures are not transport failures either.
        return Status.END_OF_FILE
    if cancelled:
        return Status.CANCELLED
    if result in (DemuxResult.EXIT, DemuxResult.TIMEOUT):
        return Status.IO_TIMEOUT
    return Status.CONNECTION_DISCONNECTED


def classify_seek_failure(result: DemuxResult, *, direct_url: bool,
                          require_avio_seek: bool,
                          cancelled: bool = False) -> Status:
    """Mirror synchronous progressive-network seek failure classification."""

    if result is DemuxResult.EOF:
        return (
            Status.SUCCESS
            if direct_url and require_avio_seek
            else Status.UNSUCCESSFUL
        )
    if not (direct_url and require_avio_seek):
        return Status.UNSUCCESSFUL
    if cancelled:
        return Status.CANCELLED
    if result in (DemuxResult.EXIT, DemuxResult.TIMEOUT):
        return Status.IO_TIMEOUT
    return Status.CONNECTION_DISCONNECTED


def seek_result(*, direct_url: bool, require_avio_seek: bool, target: int,
                duration: int | None, first_read: DemuxResult,
                cancelled: bool = False) -> tuple[Status, bool]:
    """Return the seek status and whether the first post-seek packet is kept."""

    before_known_end = duration is None or target < duration
    if not (require_avio_seek and before_known_end):
        return Status.SUCCESS, False

    status = classify_read(first_read, direct_url=direct_url,
                           cancelled=cancelled)
    if status is Status.END_OF_FILE:
        # An unknown-duration seek may legitimately land on the real end.
        # The next ordinary read owns EOS/ENDED. Before a known end it is a
        # truncated progressive-network response, not clean end-of-stream.
        if duration is None:
            return Status.SUCCESS, False
        return Status.CONNECTION_DISCONNECTED, False
    return status, status is Status.SUCCESS


@dataclass
class Source:
    terminal_status: Status = Status.SUCCESS
    event_count: int = 0
    shutdown: bool = False
    eos: bool = False

    def record_terminal(self, status: Status) -> bool:
        if status not in (
            Status.CANCELLED,
            Status.IO_TIMEOUT,
            Status.CONNECTION_DISCONNECTED,
        ):
            return False
        if self.terminal_status is not Status.SUCCESS:
            return False
        self.terminal_status = status
        self.event_count += 1
        self.shutdown = True
        return True

    def claim_runtime_terminal(
        self, status: Status, *, read_epoch: int, current_epoch: int
    ) -> bool:
        """The read owns an error only while it still belongs to the timeline."""

        if read_epoch != current_epoch or self.shutdown:
            return False
        return self.record_terminal(status)

    def start(self) -> Status:
        """A retry re-signals internally so the session command can unwind."""

        if self.terminal_status is not Status.SUCCESS:
            self.event_count += 1
        return self.terminal_status


@dataclass
class Engine:
    source: object
    source_generation: int = 1
    presentation_generation: int = 1
    error: MediaError = MediaError.NONE
    extended_status: Status = Status.SUCCESS
    playback_requested: bool = True
    seeking: bool = True
    play_pending: bool = True
    pause_pending: bool = True
    waiting: bool = True
    scrubbing: bool = True
    events: list[str] = field(default_factory=list)

    def source_error(self, source: object, status: Status) -> None:
        if (
            source is not self.source
            or self.presentation_generation != self.source_generation
        ):
            return
        if status not in (
            Status.CANCELLED,
            Status.IO_TIMEOUT,
            Status.CONNECTION_DISCONNECTED,
        ):
            return

        self.error = (
            MediaError.ABORTED
            if status is Status.CANCELLED
            else MediaError.NETWORK
        )
        self.extended_status = status
        self.playback_requested = False
        self.seeking = False
        self.play_pending = False
        self.pause_pending = False
        self.waiting = False
        self.scrubbing = False
        self.events.append("ERROR")

    def end_of_presentation(self) -> None:
        self.events.append("ENDED")

    def begin_set_source(self) -> None:
        """Start replacement while the old presentation may still be installed."""

        self.source_generation += 1
        self.error = MediaError.NONE
        self.extended_status = Status.SUCCESS

    def install_source(self, source: object) -> None:
        self.source = source
        self.presentation_generation = self.source_generation
        self.error = MediaError.NONE
        self.extended_status = Status.SUCCESS


@dataclass
class Session:
    """Model internal retry signaling versus one user-visible source error."""

    engine: Engine
    forwarded: bool = False
    command_pending: bool = False
    completed_commands: int = 0

    def source_error(self, source: object, status: Status) -> None:
        if not self.forwarded:
            self.engine.source_error(source, status)
            self.forwarded = True
        if self.command_pending:
            self.command_pending = False
            self.completed_commands += 1


class ClassificationTests(unittest.TestCase):
    def test_true_network_eof_remains_eof(self) -> None:
        self.assertIs(
            classify_read(DemuxResult.EOF, direct_url=True),
            Status.END_OF_FILE,
        )

    def test_local_bytestream_failure_keeps_legacy_eof_policy(self) -> None:
        self.assertIs(
            classify_read(DemuxResult.READ_FAILURE, direct_url=False),
            Status.END_OF_FILE,
        )

    def test_direct_timeout_and_read_failure_remain_distinct(self) -> None:
        self.assertIs(
            classify_read(DemuxResult.TIMEOUT, direct_url=True),
            Status.IO_TIMEOUT,
        )
        self.assertIs(
            classify_read(DemuxResult.READ_FAILURE, direct_url=True),
            Status.CONNECTION_DISCONNECTED,
        )

    def test_bitstream_filter_failure_is_not_mislabeled_as_network(self) -> None:
        self.assertIs(
            classify_read(DemuxResult.FILTER_FAILURE, direct_url=True),
            Status.END_OF_FILE,
        )

    def test_explicit_cancellation_wins_over_generic_ffmpeg_failure(self) -> None:
        self.assertIs(
            classify_read(
                DemuxResult.READ_FAILURE, direct_url=True, cancelled=True
            ),
            Status.CANCELLED,
        )

    def test_direct_progressive_sync_seek_failure_is_network_error(self) -> None:
        self.assertIs(
            classify_seek_failure(
                DemuxResult.READ_FAILURE,
                direct_url=True,
                require_avio_seek=True,
            ),
            Status.CONNECTION_DISCONNECTED,
        )

    def test_local_sync_seek_failure_keeps_legacy_policy(self) -> None:
        self.assertIs(
            classify_seek_failure(
                DemuxResult.READ_FAILURE,
                direct_url=False,
                require_avio_seek=True,
            ),
            Status.UNSUCCESSFUL,
        )

    def test_sync_seek_eof_returns_to_normal_eos_path(self) -> None:
        self.assertIs(
            classify_seek_failure(
                DemuxResult.EOF,
                direct_url=True,
                require_avio_seek=True,
            ),
            Status.SUCCESS,
        )

    def test_local_sync_seek_eof_keeps_legacy_failure(self) -> None:
        self.assertIs(
            classify_seek_failure(
                DemuxResult.EOF,
                direct_url=False,
                require_avio_seek=True,
            ),
            Status.UNSUCCESSFUL,
        )


class DeferredSeekTests(unittest.TestCase):
    def test_progressive_http_seek_prefetches_and_retains_first_packet(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=True,
            target=83,
            duration=120,
            first_read=DemuxResult.OK,
        )
        self.assertIs(status, Status.SUCCESS)
        self.assertTrue(retained)

    def test_failed_range_reopen_fails_seek_before_seeked(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=True,
            target=83,
            duration=120,
            first_read=DemuxResult.READ_FAILURE,
        )
        self.assertIs(status, Status.CONNECTION_DISCONNECTED)
        self.assertFalse(retained)

    def test_seek_to_known_end_remains_ordinary_eos_path(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=True,
            target=120,
            duration=120,
            first_read=DemuxResult.EOF,
        )
        self.assertIs(status, Status.SUCCESS)
        self.assertFalse(retained)

    def test_unknown_duration_eof_returns_to_normal_eos_path(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=True,
            target=120,
            duration=None,
            first_read=DemuxResult.EOF,
        )
        self.assertIs(status, Status.SUCCESS)
        self.assertFalse(retained)

    def test_known_duration_premature_eof_is_terminal(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=True,
            target=83,
            duration=120,
            first_read=DemuxResult.EOF,
        )
        self.assertIs(status, Status.CONNECTION_DISCONNECTED)
        self.assertFalse(retained)

    def test_non_progressive_transport_is_not_forced_through_prefetch(self) -> None:
        status, retained = seek_result(
            direct_url=True,
            require_avio_seek=False,
            target=83,
            duration=120,
            first_read=DemuxResult.READ_FAILURE,
        )
        self.assertIs(status, Status.SUCCESS)
        self.assertFalse(retained)


class PropagationTests(unittest.TestCase):
    def test_range_failure_is_one_network_error_without_ended(self) -> None:
        identity = object()
        source = Source()
        engine = Engine(source=identity)

        source.record_terminal(Status.CONNECTION_DISCONNECTED)
        engine.source_error(identity, source.terminal_status)

        self.assertEqual(source.event_count, 1)
        self.assertTrue(source.shutdown)
        self.assertFalse(source.eos)
        self.assertIs(engine.error, MediaError.NETWORK)
        self.assertEqual(engine.events, ["ERROR"])
        self.assertNotIn("ENDED", engine.events)
        self.assertFalse(engine.seeking)
        self.assertFalse(engine.waiting)

    def test_first_terminal_error_wins(self) -> None:
        source = Source()
        source.record_terminal(Status.IO_TIMEOUT)
        source.record_terminal(Status.CONNECTION_DISCONNECTED)
        self.assertIs(source.terminal_status, Status.IO_TIMEOUT)
        self.assertEqual(source.event_count, 1)

    def test_retrying_start_unwinds_without_duplicate_user_error(self) -> None:
        source = Source()
        identity = object()
        engine = Engine(source=identity)
        session = Session(engine=engine, command_pending=True)

        self.assertTrue(source.record_terminal(Status.IO_TIMEOUT))
        session.source_error(identity, source.terminal_status)
        self.assertEqual(engine.events, ["ERROR"])
        self.assertEqual(session.completed_commands, 1)

        session.command_pending = True
        self.assertIs(source.start(), Status.IO_TIMEOUT)
        session.source_error(identity, source.terminal_status)

        self.assertEqual(source.event_count, 2)
        self.assertEqual(engine.events, ["ERROR"])
        self.assertEqual(session.completed_commands, 2)
        self.assertFalse(session.command_pending)

    def test_read_error_loses_to_a_new_seek_epoch(self) -> None:
        source = Source()
        self.assertFalse(
            source.claim_runtime_terminal(
                Status.CONNECTION_DISCONNECTED,
                read_epoch=4,
                current_epoch=5,
            )
        )
        self.assertIs(source.terminal_status, Status.SUCCESS)
        self.assertEqual(source.event_count, 0)

    def test_stale_old_source_error_is_ignored(self) -> None:
        current = object()
        stale = object()
        engine = Engine(source=current)
        engine.source_error(stale, Status.CONNECTION_DISCONNECTED)
        self.assertIs(engine.error, MediaError.NONE)
        self.assertEqual(engine.events, [])

    def test_cancelled_current_source_maps_to_aborted(self) -> None:
        identity = object()
        engine = Engine(source=identity)
        engine.source_error(identity, Status.CANCELLED)
        self.assertIs(engine.error, MediaError.ABORTED)
        self.assertEqual(engine.events, ["ERROR"])

    def test_new_source_clears_previous_error(self) -> None:
        old = object()
        engine = Engine(source=old)
        engine.source_error(old, Status.IO_TIMEOUT)
        engine.begin_set_source()
        new = object()
        engine.install_source(new)
        self.assertIs(engine.source, new)
        self.assertIs(engine.error, MediaError.NONE)
        self.assertIs(engine.extended_status, Status.SUCCESS)

    def test_old_presentation_error_during_pending_replacement_is_ignored(self) -> None:
        old = object()
        engine = Engine(source=old)
        engine.begin_set_source()
        self.assertIs(engine.source, old)
        self.assertNotEqual(
            engine.presentation_generation, engine.source_generation
        )

        engine.source_error(old, Status.CONNECTION_DISCONNECTED)

        self.assertIs(engine.error, MediaError.NONE)
        self.assertEqual(engine.events, [])

    def test_clean_eof_still_owns_ended(self) -> None:
        identity = object()
        engine = Engine(source=identity)
        engine.end_of_presentation()
        self.assertEqual(engine.events, ["ENDED"])
        self.assertIs(engine.error, MediaError.NONE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
