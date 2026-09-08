#!/usr/bin/env python3
"""Self-test and no-game demonstration for the cooperative log monitor."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import live_watch


PROTON_LINES = [
    (
        "1.000:00d8:00f0:trace:rtspdrain:source_demux_thread "
        "unknown=future streams=2 event=source_start source_id=1 forced_tcp=1 "
        "normal_packets=8 normal_bytes=8388608 emergency_packets=1024 "
        "emergency_bytes=67108864 source_limit=64 live_record_limit=16"
    ),
    (
        "1.100:00d8:00f0:trace:rtspdrain:media_source_drain_diag_queue "
        "queued_bytes=134443 event=queue_enter source_id=1 forced_tcp=1 "
        "queued_packets=12 high_packets=12 high_bytes=134443 idle=0 "
        "normal_packets=1 normal_bytes=0 emergency_packets=0 emergency_bytes=0 "
        "read_calls=15 completed_packets=15 packet_bytes_completed=173974"
    ),
    (
        "2.000:00d8:00f0:trace:rtspdrain:media_source_drain_diag_summary "
        "event=stream_summary source_id=1 stream=0 video=1 high_packets=12 "
        "high_bytes=134443 enqueued=8 requests=6 dequeued=6 delivered=6"
    ),
    (
        "2.001:00d8:00f0:trace:rtspdrain:media_source_drain_diag_summary "
        "event=stream_summary source_id=1 stream=1 video=0 high_packets=4 "
        "high_bytes=2048 enqueued=14 requests=12 dequeued=12 delivered=12"
    ),
    (
        "2.002:00d8:00f0:trace:rtspdrain:media_source_drain_diag_summary "
        "event=source_summary source_id=1 forced_tcp=1 streams=2 "
        "stream_record_limit=8 high_packets=12 high_bytes=134443 read_calls=20 "
        "completed_packets=20 packet_bytes_completed=200000 max_read_ms=50 "
        "live_records=3 live_exhausted=0"
    ),
    (
        "2.003:00d8:00dc:trace:rtspdrain:audio_renderer_drain_diag_summary "
        "event=sar_summary sar_id=1 lifetime_ms=1000 clock_start_calls=1 "
        "clock_start_successes=1 audio_client_start_calls=1 "
        "audio_client_start_successes=1 process_samples=12 preclock_samples=0 "
        "render_callbacks=30 queued_frames=0 peak_queued_frames=2048 "
        "max_frames=4800 sample_rate=48000 frame_size=8 clock_state=2 "
        "start_pending=0"
    ),
    (
        "3.000:00d8:0114:trace:rtspdrain:source_demux_thread "
        "event=source_start source_id=2 forced_tcp=1 streams=2 "
        "normal_packets=8 normal_bytes=8388608 emergency_packets=1024 "
        "emergency_bytes=67108864 source_limit=64 live_record_limit=16"
    ),
    (
        "3.100:00d8:00dc:trace:rtspdrain:audio_renderer_drain_diag_summary "
        "event=sar_summary sar_id=2 lifetime_ms=900 clock_start_calls=0 "
        "clock_start_successes=0 audio_client_start_calls=0 "
        "audio_client_start_successes=0 process_samples=4 preclock_samples=4 "
        "render_callbacks=0 queued_frames=1024 peak_queued_frames=1024 "
        "max_frames=4800 sample_rate=48000 frame_size=8 clock_state=0 "
        "start_pending=1"
    ),
]

MTX_LINES = [
    "2026/07/30 12:00:00 INF [RTSP] listener opened on loopback",
    (
        "2026/07/30 12:00:01 INF [RTSP] [session pub1] "
        "is publishing to path 'fixture'"
    ),
    (
        "2026/07/30 12:00:02 INF [RTSP] [session read1] "
        "is reading from path 'fixture'"
    ),
]

FFMPEG_LINES = ["Connection reset by peer"]


class MonitorTests(unittest.TestCase):
    def test_generic_parser_ignores_field_order_and_unknown_fields(self) -> None:
        fields = live_watch.parse_fields(PROTON_LINES[0])
        self.assertEqual(fields["event"], "source_start")
        self.assertEqual(fields["source_id"], "1")
        self.assertEqual(fields["unknown"], "future")

    def test_healthy_unresolved_and_clock_never_are_glanceable(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append, unresolved_after=10)
        for line in MTX_LINES:
            monitor.feed("mediamtx", line, now=0)
        for line in FFMPEG_LINES:
            monitor.feed("publisher", line, now=0)
        for line in PROTON_LINES:
            monitor.feed("proton", line, now=0)
        monitor.tick(now=11)
        monitor.finish()

        joined = "\n".join(output)
        self.assertIn("MTX READY", joined)
        self.assertIn("MTX PUBLISH total=1", joined)
        self.assertIn("MTX READ total=1", joined)
        self.assertIn("FFMPEG ERROR #1", joined)
        self.assertIn("B01 END src=1 delivered=A+V v=6/6 a=12/12", joined)
        self.assertIn("SAR id=1 n=1 ~B01 audio=STARTED", joined)
        self.assertIn(
            "SAR id=2 n=2 ~B02 audio=CLOCK_NEVER+PENDING",
            joined,
        )
        self.assertIn("clock=0/0 client=0/0 samples=4 pre=4", joined)
        self.assertIn("B02 OPEN src=2 source_summary pending after 10s", joined)
        self.assertIn("B02 OPEN_AT_STOP src=2", joined)

    def test_file_follow_handles_truncation_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proton.log"
            capture = Path(directory) / "capture"
            capture.mkdir()
            path.write_text("first line\n", encoding="utf-8")
            followed = live_watch.FollowedLog("proton", path, capture)

            result = followed.poll()
            self.assertEqual(result.transition, "opened")
            self.assertEqual(result.after_transition, ["first line"])

            path.write_text("x\n", encoding="utf-8")
            result = followed.poll()
            self.assertEqual(result.transition, "truncated")
            self.assertEqual(result.after_transition, ["x"])

            path.write_text(
                "new file that rapidly regrew beyond the prior offset\n",
                encoding="utf-8",
            )
            result = followed.poll()
            self.assertEqual(result.transition, "truncated")
            self.assertEqual(
                result.after_transition,
                ["new file that rapidly regrew beyond the prior offset"],
            )

            replacement = Path(directory) / "replacement.log"
            replacement.write_text("new inode\n", encoding="utf-8")
            os.replace(replacement, path)
            result = followed.poll()
            self.assertEqual(result.transition, "recreated")
            self.assertEqual(result.after_transition, ["new inode"])
            followed.close()
            self.assertEqual(
                (capture / "proton-01.log").read_text(encoding="utf-8"),
                "first line\n",
            )
            self.assertEqual(
                (capture / "proton-02.log").read_text(encoding="utf-8"),
                "x\n",
            )
            self.assertEqual(
                (capture / "proton-03.log").read_text(encoding="utf-8"),
                "new file that rapidly regrew beyond the prior offset\n",
            )
            self.assertEqual(
                (capture / "proton-04.log").read_text(encoding="utf-8"),
                "new inode\n",
            )

    def test_follow_waits_for_creation_and_joins_partial_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "later.log"
            capture = root / "capture"
            capture.mkdir()
            followed = live_watch.FollowedLog("proton", path, capture)
            self.assertEqual(followed.poll(), live_watch.PollResult())

            path.write_text("partial", encoding="utf-8")
            opened = followed.poll()
            self.assertEqual(opened.transition, "opened")
            self.assertEqual(opened.after_transition, [])

            with path.open("a", encoding="utf-8") as stream:
                stream.write(" line\n")
            completed = followed.poll()
            self.assertEqual(completed.transition, None)
            self.assertEqual(completed.after_transition, ["partial line"])
            followed.close()
            self.assertEqual(
                (capture / "proton-01.log").read_text(encoding="utf-8"),
                "partial line\n",
            )

    def test_backend_counter_survives_proton_recreation(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append)
        monitor.feed("proton", PROTON_LINES[0], now=0)
        monitor.file_event("proton", "gone", "proton-01.log")
        second = PROTON_LINES[0].replace("source_id=1", "source_id=2")
        monitor.file_event("proton", "recreated", "proton-02.log")
        monitor.feed("proton", second, now=1)
        self.assertTrue(any(item.startswith("B01 START") for item in output))
        self.assertTrue(any(item.startswith("B02 START") for item in output))

    def test_unlink_relaunch_preserves_both_proton_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "steam-438100.log"
            capture = root / "capture"
            capture.mkdir()
            path.write_text("A3.16 first\n", encoding="utf-8")
            followed = live_watch.FollowedLog("proton", path, capture)
            self.assertEqual(followed.poll().transition, "opened")

            with path.open("a", encoding="utf-8") as stream:
                stream.write("A3.16 final before unlink\n")
            path.unlink()
            gone = followed.poll()
            self.assertEqual(gone.transition, "gone")
            self.assertEqual(
                gone.before_transition,
                ["A3.16 final before unlink"],
            )

            path.write_text("A3.14 replacement\n", encoding="utf-8")
            recreated = followed.poll()
            self.assertEqual(recreated.transition, "recreated")
            self.assertEqual(
                recreated.after_transition,
                ["A3.14 replacement"],
            )
            followed.close()

            first = capture / "proton-01.log"
            second = capture / "proton-02.log"
            self.assertEqual(
                first.read_text(encoding="utf-8"),
                "A3.16 first\nA3.16 final before unlink\n",
            )
            self.assertEqual(
                second.read_text(encoding="utf-8"),
                "A3.14 replacement\n",
            )
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(second.stat().st_mode & 0o777, 0o600)

    def test_idle_and_recovery_transitions_are_visible(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append)
        monitor.feed("proton", PROTON_LINES[0], now=0)
        idle = (
            "1.010:00d8:00f0:trace:rtspdrain:queue "
            "event=queue_enter source_id=1 forced_tcp=1 queued_packets=0 "
            "queued_bytes=0 high_packets=0 high_bytes=0 idle=1 "
            "normal_packets=0 normal_bytes=0 emergency_packets=0 "
            "emergency_bytes=0 read_calls=0 completed_packets=0 "
            "packet_bytes_completed=0"
        )
        leave = idle.replace("event=queue_enter", "event=queue_leave").replace(
            "idle=1",
            "idle=0",
        )
        monitor.feed("proton", idle, now=0)
        monitor.feed("proton", leave, now=0)
        joined = "\n".join(output)
        self.assertIn("bound=IDLE", joined)
        self.assertIn("QUEUE_LEAVE", joined)
        self.assertIn("bound=none", joined)

    def test_emergency_recovery_chain_is_never_deduplicated(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append)
        monitor.feed("proton", PROTON_LINES[0], now=0)
        queue = (
            "1.010:00d8:00f0:trace:rtspdrain:queue "
            "event={event} source_id=1 forced_tcp=1 queued_packets={queued} "
            "queued_bytes=4096 high_packets=1024 high_bytes=4096 idle=0 "
            "normal_packets={normal} normal_bytes=0 "
            "emergency_packets={emergency} emergency_bytes=0 read_calls=2 "
            "completed_packets=2 packet_bytes_completed=4096"
        )

        # Establish that ordinary NPKT and its clear have already been seen.
        monitor.feed(
            "proton",
            queue.format(
                event="queue_enter",
                queued=8,
                normal=1,
                emergency=0,
            ),
            now=0,
        )
        monitor.feed(
            "proton",
            queue.format(
                event="queue_leave",
                queued=0,
                normal=0,
                emergency=0,
            ),
            now=0,
        )
        start = len(output)

        # The emergency downgrade and eventual clear must remain visible even
        # though both resulting states were emitted in the earlier cycle.
        monitor.feed(
            "proton",
            queue.format(
                event="queue_enter",
                queued=1024,
                normal=1,
                emergency=1,
            ),
            now=0,
        )
        monitor.feed(
            "proton",
            queue.format(
                event="queue_change",
                queued=8,
                normal=1,
                emergency=0,
            ),
            now=0,
        )
        monitor.feed(
            "proton",
            queue.format(
                event="queue_leave",
                queued=0,
                normal=0,
                emergency=0,
            ),
            now=0,
        )

        recovery = output[start:]
        self.assertEqual(len(recovery), 3)
        self.assertIn("bound=EPKT+NPKT", recovery[0])
        self.assertIn("QUEUE_CHANGE", recovery[1])
        self.assertIn("bound=NPKT", recovery[1])
        self.assertIn("QUEUE_LEAVE", recovery[2])
        self.assertIn("bound=none", recovery[2])

    def test_repeated_wine_warnings_are_milestone_collapsed(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append)
        line = (
            "1.000:00d8:00f0:warn:mfplat:stub "
            "private detail intentionally not echoed"
        )
        for _ in range(60):
            monitor.feed("proton", line)
        self.assertEqual(
            [item for item in output if item.startswith("WINE ")],
            [
                "WINE warn:mfplat #1 (inspect local log)",
                "WINE warn:mfplat #2 (inspect local log)",
                "WINE warn:mfplat #3 (inspect local log)",
                "WINE warn:mfplat #10 (inspect local log)",
                "WINE warn:mfplat #50 (inspect local log)",
            ],
        )

    def test_ffmpeg_progress_is_not_labeled_as_a_warning(self) -> None:
        output: list[str] = []
        monitor = live_watch.Monitor(output.append)
        monitor.feed(
            "publisher",
            "frame=  123 fps=30 q=-1.0 size=N/A time=00:00:04.10",
        )
        monitor.feed("publisher", "co located POCs unavailable")
        self.assertEqual(
            output,
            ["FFMPEG LOG #1 (inspect local log)"],
        )

    def test_once_cli_processes_all_three_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proton = root / "steam-438100.log"
            mediamtx = root / "mediamtx.log"
            publisher = root / "ffmpeg.log"
            capture = root / "capture"
            proton.write_text("\n".join(PROTON_LINES) + "\n", encoding="utf-8")
            mediamtx.write_text("\n".join(MTX_LINES) + "\n", encoding="utf-8")
            publisher.write_text(
                "\n".join(FFMPEG_LINES) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(live_watch.__file__)),
                    "--once",
                    "--proton-log",
                    str(proton),
                    "--mediamtx-log",
                    str(mediamtx),
                    "--publisher-log",
                    str(publisher),
                    "--capture-dir",
                    str(capture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("B01 END src=1 delivered=A+V", result.stdout)
            self.assertIn(
                "SAR id=2 n=2 ~B02 audio=CLOCK_NEVER+PENDING",
                result.stdout,
            )
            self.assertIn("B02 OPEN_AT_STOP", result.stdout)
            self.assertIn("STOP backend=2 summaries=1 sar=2", result.stdout)
            self.assertEqual(
                (capture / "proton-01.log").read_text(encoding="utf-8"),
                proton.read_text(encoding="utf-8"),
            )
            self.assertEqual(capture.stat().st_mode & 0o777, 0o700)

    def test_live_event_is_flushed_through_a_pipe_before_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proton = root / "steam-438100.log"
            capture = root / "capture"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(live_watch.__file__)),
                    "--poll",
                    "0.01",
                    "--proton-log",
                    str(proton),
                    "--mediamtx-log",
                    str(root / "mediamtx.log"),
                    "--publisher-log",
                    str(root / "ffmpeg.log"),
                    "--capture-dir",
                    str(capture),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            observed: list[str] = []
            buffered = b""
            proton_created = False
            try:
                deadline = time.monotonic() + 3
                while (
                    time.monotonic() < deadline
                    and not any(line.startswith("B01 START") for line in observed)
                ):
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            continue
                        pieces = (buffered + chunk).split(b"\n")
                        buffered = pieces.pop()
                        observed.extend(
                            piece.decode("utf-8", errors="replace")
                            for piece in pieces
                        )
                    if (
                        not proton_created
                        and any(line.startswith("WATCH ") for line in observed)
                    ):
                        proton.write_text(
                            PROTON_LINES[0] + "\n",
                            encoding="utf-8",
                        )
                        proton_created = True
                self.assertIsNone(
                    process.poll(),
                    "watcher exited before the live flush assertion",
                )
                self.assertTrue(
                    any(line.startswith("B01 START") for line in observed),
                    observed,
                )
            finally:
                selector.close()
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                process.communicate(timeout=3)


def demo() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = {
            "proton": root / "steam-438100.log",
            "mediamtx": root / "a316-mediamtx.log",
            "publisher": root / "a316-ffmpeg.log",
            "capture": root / "capture",
        }
        paths["proton"].write_text(
            "\n".join(PROTON_LINES) + "\n",
            encoding="utf-8",
        )
        paths["mediamtx"].write_text(
            "\n".join(MTX_LINES) + "\n",
            encoding="utf-8",
        )
        paths["publisher"].write_text(
            "\n".join(FFMPEG_LINES) + "\n",
            encoding="utf-8",
        )
        return live_watch.main(
            [
                "--once",
                "--proton-log",
                str(paths["proton"]),
                "--mediamtx-log",
                str(paths["mediamtx"]),
                "--publisher-log",
                str(paths["publisher"]),
                "--capture-dir",
                str(paths["capture"]),
            ]
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        with contextlib.redirect_stderr(io.StringIO()):
            raise SystemExit(demo())
    unittest.main(verbosity=2)
