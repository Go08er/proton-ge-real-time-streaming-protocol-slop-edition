#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.client import HTTPResponse
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Iterator
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import live_fixture


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class SyntheticFixture:
    def __init__(self, root: Path, profile: str = "full", mux_count: int = 4) -> None:
        self.root = root
        self.profile = profile
        self.mux_count = mux_count
        self.build()

    def build(self) -> None:
        manifest = self.root / "provenance/manifest.json"
        write_json(manifest, {"fixture": "synthetic-live-contract", "schema": 1})

        mux_root = self.root / "hls/live"
        audio_root = self.root / "hls/live-separate/audio"
        video_root = self.root / "hls/live-separate/video"
        for index in range(max(4, self.mux_count)):
            for base, name, marker in (
                (mux_root, f"segment-{index:03d}.ts", b"mux"),
                (audio_root, f"audio-{index:03d}.ts", b"audio"),
                (video_root, f"video-{index:03d}.ts", b"video"),
            ):
                segment = base / "segments" / name
                segment.parent.mkdir(parents=True, exist_ok=True)
                segment.write_bytes(marker + bytes([index]) * 64)

        mux_schedule = self._track_schedule(mux_root, "segment", self.mux_count)
        audio_schedule = self._track_schedule(audio_root, "audio", 4)
        video_schedule = self._track_schedule(video_root, "video", 4)
        write_json(mux_root / "schedule.json", mux_schedule)
        write_json(audio_root / "schedule.json", audio_schedule)
        write_json(video_root / "schedule.json", video_schedule)

        pair_entries = []
        for audio_entry, video_entry in zip(
            audio_schedule["entries"], video_schedule["entries"], strict=True
        ):
            pair_entries.append(
                {
                    "audio": {
                        "playlist": f"audio/{audio_entry['playlist']}",
                        "sha256": audio_entry["sha256"],
                    },
                    "media_sequence": audio_entry["media_sequence"],
                    "video": {
                        "playlist": f"video/{video_entry['playlist']}",
                        "sha256": video_entry["sha256"],
                    },
                }
            )
        write_json(
            self.root / "hls/live-separate/schedule.json",
            {
                "description": "Synthetic paired publication schedule.",
                "entries": pair_entries,
                "interval_seconds": 0.05,
                "schema": 1,
                "segment_count": 4,
                "window_segments": 2,
            },
        )
        (self.root / "hls/live-separate/master.m3u8").write_text(
            "\n".join(
                [
                    "#EXTM3U",
                    "#EXT-X-VERSION:6",
                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="Stereo",DEFAULT=YES,URI="audio/index.m3u8"',
                    '#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="stereo"',
                    "video/index.m3u8",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.refresh_integrity()

    def _track_schedule(self, root: Path, stem: str, count: int) -> dict:
        entries = []
        for sequence in range(count - 1):
            playlist = root / f"window-{sequence:04d}.m3u8"
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:6",
                "#EXT-X-TARGETDURATION:1",
                f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
                "#EXT-X-INDEPENDENT-SEGMENTS",
            ]
            for segment in range(sequence, sequence + 2):
                lines.extend(("#EXTINF:1.000000,", f"segments/{stem}-{segment:03d}.ts"))
            playlist.parent.mkdir(parents=True, exist_ok=True)
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
            entries.append(
                {
                    "media_sequence": sequence,
                    "playlist": playlist.name,
                    "sha256": digest(playlist),
                }
            )
        return {
            "description": "Synthetic immutable live windows.",
            "entries": entries,
            "interval_seconds": 0.05,
            "schema": 1,
            "segment_count": count,
            "window_segments": 2,
        }

    def refresh_integrity(self) -> None:
        ready = self.root / "READY"
        sums = self.root / "SHA256SUMS"
        if ready.exists():
            ready.unlink()
        if sums.exists():
            sums.unlink()
        paths = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )
        sums.write_text(
            "".join(f"{digest(self.root / relative)}  {relative}\n" for relative in paths),
            encoding="ascii",
        )
        write_json(
            ready,
            {
                "manifest_sha256": digest(self.root / "provenance/manifest.json"),
                "profile": self.profile,
                "schema": 1,
                "sha256sums_sha256": digest(sums),
            },
        )


@contextmanager
def running_server(
    fixture: SyntheticFixture,
    *,
    mode: str = "normal",
    start_generation: int | None = None,
    success_budget: int | None = None,
    delay_seconds: float = 0.05,
) -> Iterator[tuple[str, live_fixture.LiveFixtureState, StringIO]]:
    if mode in ("delayed-audio-window", "audio-404", "audio-503"):
        if start_generation is None and success_budget is None:
            start_generation = 1
    bundle = live_fixture.FixtureBundle.load(fixture.root)
    stream = StringIO()
    logger = live_fixture.SafeRequestLogger(stream, "live-integration")
    publisher = live_fixture.LivePublisher(bundle, mode)
    gate = live_fixture.AudioFaultGate(
        mode, start_generation, success_budget, delay_seconds
    )
    state = live_fixture.LiveFixtureState(
        bundle,
        publisher,
        gate,
        logger,
        "test-control-token",
        max_requests=256,
        max_concurrent=8,
    )
    server = live_fixture.LiveFixtureServer((live_fixture.LOOPBACK_HOST, 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state, stream
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("loopback live server did not stop")


def read_response(response: HTTPResponse) -> tuple[int, bytes, int]:
    return response.status, response.read(), int(response.headers["X-Fixture-Generation"])


def get(url: str, method: str = "GET") -> tuple[int, bytes, int]:
    request = Request(url, method=method)
    with urlopen(request, timeout=2) as response:
        return read_response(response)


def status_for(url: str, method: str = "GET") -> int:
    try:
        return get(url, method)[0]
    except HTTPError as error:
        with error:
            error.read()
            return error.code


def advance(base: str, token: str = "test-control-token") -> dict:
    request = Request(
        base + "/__control__/advance",
        data=b"",
        method="POST",
        headers={"X-Live-Fixture-Token": token},
    )
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read())


def first_segment(playlist: bytes) -> str:
    for line in playlist.decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            return line
    raise AssertionError("playlist has no segment")


class FixtureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"
        self.fixture = SyntheticFixture(self.root)

    def test_complete_fixture_loads_and_binds_all_schedules(self) -> None:
        bundle = live_fixture.FixtureBundle.load(self.root)
        self.assertEqual("full", bundle.ready["profile"])
        self.assertEqual([0, 1, 2], [item.sequence for item in bundle.mux.windows])
        self.assertEqual(
            [item.sequence for item in bundle.audio.windows],
            [item.sequence for item in bundle.video.windows],
        )
        self.assertEqual(4, len(bundle.audio_segments))

    def test_mux_may_have_one_more_window_than_paired_renditions(self) -> None:
        other_root = Path(self.temporary.name) / "unequal-fixture"
        SyntheticFixture(other_root, mux_count=5)
        bundle = live_fixture.FixtureBundle.load(other_root)
        self.assertEqual(4, len(bundle.mux.windows))
        self.assertEqual(3, len(bundle.audio.windows))
        self.assertEqual(3, bundle.publication_count)
        publisher = live_fixture.LivePublisher(bundle, "normal")
        self.assertEqual((1, True), publisher.advance())
        self.assertEqual((2, True), publisher.advance())
        self.assertEqual((2, False), publisher.advance())

    def test_rejects_non_full_and_missing_ready_contracts(self) -> None:
        self.fixture.profile = "smoke"
        self.fixture.refresh_integrity()
        with self.assertRaisesRegex(live_fixture.ContractError, "full fixture"):
            live_fixture.FixtureBundle.load(self.root)
        (self.root / "READY").unlink()
        with self.assertRaisesRegex(live_fixture.ContractError, "READY"):
            live_fixture.FixtureBundle.load(self.root)

    def test_rejects_sha256sums_or_manifest_tampering(self) -> None:
        sums = self.root / "SHA256SUMS"
        sums.write_bytes(sums.read_bytes() + b"0" * 64 + b"  extra\n")
        with self.assertRaisesRegex(live_fixture.ContractError, "SHA256SUMS does not match"):
            live_fixture.FixtureBundle.load(self.root)

        self.fixture.refresh_integrity()
        (self.root / "provenance/manifest.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(live_fixture.ContractError, "SHA256SUMS mismatch"):
            live_fixture.FixtureBundle.load(self.root)

    def test_rejects_stale_schedule_entry_digest_even_when_outer_checksums_match(self) -> None:
        playlist = self.root / "hls/live/window-0000.m3u8"
        playlist.write_text(playlist.read_text(encoding="utf-8") + "#CHANGED\n", encoding="utf-8")
        self.fixture.refresh_integrity()
        with self.assertRaisesRegex(live_fixture.ContractError, "schedule SHA-256 mismatch"):
            live_fixture.FixtureBundle.load(self.root)

    def test_rejects_playlist_traversal_even_when_all_hashes_are_refreshed(self) -> None:
        playlist = self.root / "hls/live/window-0000.m3u8"
        text = playlist.read_text(encoding="utf-8").replace(
            "segments/segment-000.ts", "../segment-000.ts"
        )
        playlist.write_text(text, encoding="utf-8")
        schedule_path = self.root / "hls/live/schedule.json"
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        schedule["entries"][0]["sha256"] = digest(playlist)
        write_json(schedule_path, schedule)
        self.fixture.refresh_integrity()
        with self.assertRaisesRegex(live_fixture.ContractError, "traversing|outside"):
            live_fixture.FixtureBundle.load(self.root)

    def test_rejects_paired_track_mismatch(self) -> None:
        pair_path = self.root / "hls/live-separate/schedule.json"
        pair = json.loads(pair_path.read_text(encoding="utf-8"))
        pair["entries"][1]["audio"]["playlist"] = "audio/window-0000.m3u8"
        write_json(pair_path, pair)
        self.fixture.refresh_integrity()
        with self.assertRaisesRegex(live_fixture.ContractError, "paired audio playlist differs"):
            live_fixture.FixtureBundle.load(self.root)

    def test_rejects_nonliteral_loopback_binding(self) -> None:
        bundle = live_fixture.FixtureBundle.load(self.root)
        state = live_fixture.LiveFixtureState(
            bundle,
            live_fixture.LivePublisher(bundle, "normal"),
            live_fixture.AudioFaultGate("normal", None, None, 0),
            live_fixture.SafeRequestLogger(StringIO(), "bind-check"),
            "test-control-token",
            10,
            1,
        )
        for host in ("localhost", "0.0.0.0", "192.168.1.4", "::1"):
            with self.subTest(host=host), self.assertRaisesRegex(
                live_fixture.ContractError, "literal IPv4 loopback"
            ):
                live_fixture.LiveFixtureServer((host, 0), state)


class PublisherAndLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        fixture = SyntheticFixture(Path(self.temporary.name) / "fixture")
        self.bundle = live_fixture.FixtureBundle.load(fixture.root)

    def test_generation_update_never_tears_audio_and_video(self) -> None:
        publisher = live_fixture.LivePublisher(self.bundle, "normal")
        snapshots = []

        def reader() -> None:
            for _ in range(100):
                snapshot = publisher.snapshot()
                snapshots.append(
                    (snapshot.generation, snapshot.mux.sequence, snapshot.audio.sequence, snapshot.video.sequence)
                )

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers:
            thread.start()
        publisher.advance()
        publisher.advance()
        publisher.advance()
        for thread in readers:
            thread.join()
        self.assertTrue(snapshots)
        self.assertTrue(all(len(set(values)) == 1 for values in snapshots))
        self.assertEqual((2, False), publisher.advance())

    def test_hold_mode_never_advances(self) -> None:
        publisher = live_fixture.LivePublisher(self.bundle, "hold", initial_generation=1)
        self.assertEqual((1, False), publisher.advance())
        self.assertEqual(1, publisher.snapshot().generation)

    def test_fault_triggers_are_mutually_exclusive_and_mode_specific(self) -> None:
        with self.assertRaisesRegex(live_fixture.ContractError, "mutually exclusive"):
            live_fixture.AudioFaultGate(
                "audio-503", start_generation=1, success_budget=2, delay_seconds=0
            )
        with self.assertRaisesRegex(live_fixture.ContractError, "exactly one"):
            live_fixture.AudioFaultGate(
                "audio-503", start_generation=None, success_budget=None, delay_seconds=0
            )
        with self.assertRaisesRegex(live_fixture.ContractError, "does not accept"):
            live_fixture.AudioFaultGate(
                "normal", start_generation=1, success_budget=None, delay_seconds=0
            )

    def test_audio_success_budget_is_atomic_and_playlist_independent(self) -> None:
        budget = 7
        gate = live_fixture.AudioFaultGate(
            "audio-503", start_generation=None, success_budget=budget, delay_seconds=0
        )
        barrier = threading.Barrier(65)

        def playlist_poll(index: int) -> tuple[int | None, float]:
            barrier.wait()
            method = "HEAD" if index % 2 else "GET"
            return gate.plan(live_fixture.ROUTE_AUDIO_PLAYLIST, index % 3, method)

        def segment_get(_index: int) -> tuple[int | None, float]:
            barrier.wait()
            return gate.plan(live_fixture.ROUTE_AUDIO_SEGMENT, 0, "GET")

        with ThreadPoolExecutor(max_workers=65) as pool:
            playlist_results = [pool.submit(playlist_poll, index) for index in range(48)]
            segment_results = [pool.submit(segment_get, index) for index in range(16)]
            barrier.wait()
            playlist_values = [future.result(timeout=2) for future in playlist_results]
            segment_values = [future.result(timeout=2) for future in segment_results]

        self.assertEqual([(None, 0.0)] * 48, playlist_values)
        self.assertEqual(budget, segment_values.count((None, 0.0)))
        self.assertEqual(16 - budget, segment_values.count((503, 0.0)))
        self.assertEqual(
            (None, 0.0), gate.plan(live_fixture.ROUTE_AUDIO_SEGMENT, 0, "HEAD")
        )
        self.assertEqual(
            (None, 0.0), gate.plan(live_fixture.ROUTE_VIDEO_SEGMENT, 0, "GET")
        )

    def test_logger_schema_is_allowlisted_monotonic_and_bounded(self) -> None:
        values = iter((100, 110, 120))
        stream = StringIO()
        logger = live_fixture.SafeRequestLogger(stream, "safe-case", monotonic_ns=lambda: next(values))
        started = logger.now()
        finished = logger.now()
        logger.record(
            request_seq=1,
            method="GET",
            route=live_fixture.ROUTE_AUDIO_PLAYLIST,
            status=200,
            generation=0,
            fault_mode="normal",
            bytes_sent=100,
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
        )
        result = json.loads(stream.getvalue())
        self.assertEqual(
            {
                "bytes_sent", "case_id", "clock_basis", "fault_mode", "finished_monotonic_ns",
                "generation", "method", "request_seq", "route", "schema",
                "started_monotonic_ns", "status",
            },
            set(result),
        )
        self.assertEqual(live_fixture.CLOCK_BASIS, result["clock_basis"])
        self.assertEqual((110, 120), (
            result["started_monotonic_ns"], result["finished_monotonic_ns"]
        ))
        self.assertLessEqual(result["started_monotonic_ns"], result["finished_monotonic_ns"])
        changed = dict(result)
        changed["clock_basis"] = "linux-clock-monotonic-v1"
        with self.assertRaisesRegex(live_fixture.ContractError, "clock basis"):
            live_fixture.validate_evidence_record(changed, "safe-case")
        changed = dict(result)
        changed["schema"] = 2
        with self.assertRaisesRegex(live_fixture.ContractError, "schema"):
            live_fixture.validate_evidence_record(changed, "safe-case")
        changed = dict(result)
        changed["case_id"] = "different-case"
        with self.assertRaisesRegex(live_fixture.ContractError, "case ID differs"):
            live_fixture.validate_evidence_record(changed, "safe-case")
        tiny = live_fixture.SafeRequestLogger(StringIO(), "tiny", max_bytes=1)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            tiny.record(
                request_seq=1,
                method="GET",
                route=live_fixture.ROUTE_OTHER,
                status=404,
                generation=0,
                fault_mode="normal",
                bytes_sent=0,
                started_monotonic_ns=0,
                finished_monotonic_ns=1,
            )

    def test_default_logger_clock_is_absolute_clock_monotonic_raw(self) -> None:
        before = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        logger = live_fixture.SafeRequestLogger(StringIO(), "raw-clock")
        observed = logger.now()
        after = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        self.assertLessEqual(before, observed)
        self.assertLessEqual(observed, after)
        # A process-relative origin would be near zero and cannot satisfy this
        # same-basis equality against the guest-wide raw clock.
        self.assertGreater(observed, 1_000_000_000)


class LoopbackIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = SyntheticFixture(Path(self.temporary.name) / "fixture")

    def test_mux_and_paired_playlists_advance_together(self) -> None:
        with running_server(self.fixture) as (base, _state, _log):
            status, master, generation = get(base + "/separate/master.m3u8")
            self.assertEqual((200, 0), (status, generation))
            self.assertIn(b"audio/index.m3u8", master)
            _, audio_zero, audio_generation = get(base + "/separate/audio/index.m3u8")
            _, video_zero, video_generation = get(base + "/separate/video/index.m3u8")
            _, mux_zero, mux_generation = get(base + "/mux/index.m3u8")
            self.assertEqual((0, 0, 0), (audio_generation, video_generation, mux_generation))
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:0", audio_zero)
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:0", video_zero)
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:0", mux_zero)

            self.assertEqual({"changed": True, "generation": 1, "schema": 1}, advance(base))
            _, audio_one, audio_generation = get(base + "/separate/audio/index.m3u8")
            _, video_one, video_generation = get(base + "/separate/video/index.m3u8")
            _, mux_one, mux_generation = get(base + "/mux/index.m3u8")
            self.assertEqual((1, 1, 1), (audio_generation, video_generation, mux_generation))
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:1", audio_one)
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:1", video_one)
            self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:1", mux_one)

    def test_audio_request_budget_reproduces_brief_audio_then_video_only(self) -> None:
        with running_server(
            self.fixture, mode="audio-404", start_generation=None, success_budget=2
        ) as (base, _state, _log):
            _, audio_playlist, _ = get(base + "/separate/audio/index.m3u8")
            first_audio = first_segment(audio_playlist)
            missing = base + "/separate/audio/segments/not-published.ts"
            self.assertEqual(404, status_for(missing))
            for _ in range(8):
                self.assertEqual(200, status_for(base + "/separate/audio/index.m3u8"))
                self.assertEqual(200, status_for(base + "/separate/audio/index.m3u8", "HEAD"))
            self.assertEqual(200, status_for(base + "/separate/audio/" + first_audio, "HEAD"))
            self.assertEqual(200, status_for(base + "/separate/audio/" + first_audio))
            self.assertEqual(200, status_for(base + "/separate/audio/" + first_audio))
            self.assertEqual(404, status_for(base + "/separate/audio/" + first_audio))
            self.assertEqual(200, status_for(base + "/separate/audio/" + first_audio, "HEAD"))
            _, video_playlist, _ = get(base + "/separate/video/index.m3u8")
            self.assertEqual(200, status_for(base + "/separate/video/" + first_segment(video_playlist)))

    def test_audio_503_generation_fault_leaves_video_available(self) -> None:
        with running_server(self.fixture, mode="audio-503", start_generation=1) as (
            base, _state, _log
        ):
            self.assertEqual(200, status_for(base + "/separate/audio/index.m3u8"))
            self.assertEqual(1, advance(base)["generation"])
            self.assertEqual(503, status_for(base + "/separate/audio/index.m3u8"))
            self.assertEqual(503, status_for(base + "/separate/audio/segments/audio-001.ts"))
            self.assertEqual(200, status_for(base + "/separate/video/index.m3u8"))

    def test_unpublished_mux_tail_is_outside_common_prefix_closure(self) -> None:
        unequal = SyntheticFixture(
            Path(self.temporary.name) / "unequal-fixture", mux_count=5
        )
        self.assertTrue(
            (unequal.root / "hls/live/segments/segment-004.ts").is_file(),
            "synthetic tail artifact must exist to prove routing closure",
        )
        with running_server(unequal) as (base, _state, _log):
            # Segment 003 overlaps the final common-prefix window. Segment 004
            # appears only in the extra mux window and must never be exposed.
            self.assertEqual(200, status_for(base + "/mux/segments/segment-003.ts"))
            self.assertEqual(404, status_for(base + "/mux/segments/segment-004.ts"))

    def test_delayed_audio_window_does_not_delay_video_child(self) -> None:
        with running_server(
            self.fixture,
            mode="delayed-audio-window",
            start_generation=0,
            delay_seconds=0.05,
        ) as (base, _state, _log):
            started = time.monotonic()
            self.assertEqual(200, status_for(base + "/separate/audio/index.m3u8"))
            audio_elapsed = time.monotonic() - started
            started = time.monotonic()
            self.assertEqual(200, status_for(base + "/separate/video/index.m3u8"))
            video_elapsed = time.monotonic() - started
            self.assertGreaterEqual(audio_elapsed, 0.04)
            self.assertLess(video_elapsed, audio_elapsed)

    def test_hold_and_bad_control_token_are_fail_closed(self) -> None:
        with running_server(self.fixture, mode="hold") as (base, _state, _log):
            request = Request(
                base + "/__control__/advance",
                data=b"",
                method="POST",
                headers={"X-Live-Fixture-Token": "incorrect-token"},
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=2)
            with caught.exception:
                caught.exception.read()
                self.assertEqual(404, caught.exception.code)
            self.assertEqual({"changed": False, "generation": 0, "schema": 1}, advance(base))

    def test_head_and_query_are_served_without_leaking_request_text(self) -> None:
        with running_server(self.fixture) as (base, _state, log):
            status, body, generation = get(
                base + "/mux/index.m3u8?token=DO_NOT_LOG", method="HEAD"
            )
            self.assertEqual((200, b"", 0), (status, body, generation))
            self.assertEqual(404, status_for(base + "/unknown?credential=DO_NOT_LOG"))
        # Closing the threaded server joins every request handler. Read the
        # in-memory log only after that join; receiving the HTTP body can race
        # the handler's finally-block evidence flush.
        serialized = log.getvalue()
        self.assertNotIn("DO_NOT_LOG", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("token", serialized)
        records = [json.loads(line) for line in serialized.splitlines()]
        self.assertEqual([1, 2], [record["request_seq"] for record in records])
        self.assertEqual(
            [live_fixture.ROUTE_MUX_PLAYLIST, live_fixture.ROUTE_OTHER],
            [record["route"] for record in records],
        )
        self.assertLessEqual(
            records[0]["finished_monotonic_ns"], records[1]["finished_monotonic_ns"]
        )


if __name__ == "__main__":
    unittest.main()
