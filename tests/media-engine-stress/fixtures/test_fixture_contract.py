#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Pure tests for fixture specifications and fail-closed parsing helpers."""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from align_live_renditions import align_pair
from make_live_schedule import write_windows
from pair_live_schedules import write_paired_schedule
from validate_fixtures import (
    ValidationError,
    checked_relative,
    parse_checksums,
    playlist_segment_uris,
    top_level_mp4_atoms,
)


HERE = Path(__file__).resolve().parent


def atom(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


class FixtureContractTests(unittest.TestCase):
    def test_profiles_have_smoke_and_full_contracts(self) -> None:
        document = json.loads((HERE / "profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], 1)
        self.assertEqual(set(document["profiles"]), {"smoke", "full"})
        smoke = document["profiles"]["smoke"]
        full = document["profiles"]["full"]
        self.assertEqual(document["marker_contract"]["seek_video_tolerance_frames"], 1)
        self.assertEqual(document["marker_contract"]["seek_audio_tolerance_milliseconds"], 40)
        self.assertLess(smoke["primary_duration_seconds"], full["primary_duration_seconds"])
        for profile in (smoke, full):
            self.assertEqual(profile["audio_sample_rate"], 48000)
            self.assertEqual(profile["audio_channels"], 2)
            self.assertEqual(
                profile["frames_per_second"] * profile["gop_seconds"],
                30,
            )
            self.assertGreaterEqual(
                profile["hls_duration_seconds"] / profile["hls_segment_seconds"],
                profile["hls_live_window_segments"] + 1,
            )

    def test_rtsp_payload_ladder_changes_one_media_axis_per_rung(self) -> None:
        document = json.loads((HERE / "profiles.json").read_text(encoding="utf-8"))
        ladder = document["rtsp_payload_ladder"]
        common = ladder["common"]
        rungs = ladder["rungs"]
        self.assertEqual(ladder["included_profiles"], ["full"])
        self.assertEqual(set(rungs), {"a", "b", "c", "d"})
        self.assertEqual(common["duration_seconds"], 45)
        self.assertEqual(common["frames_per_second"], common["gop_frames"])
        self.assertEqual(common["audio_bitrate_bps"], 96000)
        self.assertEqual(common["reference_frames"], 3)
        self.assertEqual(common["b_frames"], 2)

        media_fields = {"width", "height", "h264_profile", "video_bitrate_bps"}

        def differences(left: str, right: str) -> set[str]:
            return {
                key for key in media_fields
                if rungs[left][key] != rungs[right][key]
            }

        self.assertEqual(differences("a", "b"), {"width", "height"})
        self.assertEqual(differences("b", "c"), {"h264_profile"})
        self.assertEqual(differences("c", "d"), {"video_bitrate_bps"})
        self.assertEqual(rungs["a"]["video_bitrate_bps"], 270000)
        self.assertEqual(rungs["d"]["video_bitrate_bps"], 8200000)

    def test_observed_rtsp_reproduction_keeps_the_measured_shape(self) -> None:
        document = json.loads((HERE / "profiles.json").read_text(encoding="utf-8"))
        reproduction = document["rtsp_reproduction_inputs"]
        observed = reproduction["observed-heavy-v1"]
        self.assertEqual(reproduction["included_profiles"], ["full"])
        self.assertEqual(
            observed["captured_reference_sha256"],
            "8e2a8df84d9f257d3d0d3c24372ec644d9366cd9d734b284fe50d3b6a6346bde",
        )
        self.assertEqual(
            (
                observed["width"],
                observed["height"],
                observed["frames_per_second"],
                observed["h264_profile"],
                observed["video_bitrate_bps"],
            ),
            (1280, 720, 30, "High", 8000000),
        )
        self.assertEqual(
            (
                observed["gop_frames"],
                observed["reference_frames"],
                observed["b_frames"],
            ),
            (60, 1, 3),
        )
        self.assertEqual(observed["audio_bitrate_bps"], 128000)
        midgop = reproduction["observed-midgop-v1"]
        self.assertEqual(midgop["derived_from"], "observed-heavy-v1")
        self.assertEqual(midgop["trim_seconds"], 0.5)
        self.assertEqual(midgop["first_keyframe_deadline_seconds"], 1.6)

    def test_mp4_atom_order_is_observable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(atom(b"ftyp") + atom(b"moov") + atom(b"mdat", b"payload"))
            self.assertEqual(
                [kind for kind, _offset, _size in top_level_mp4_atoms(path)],
                ["ftyp", "moov", "mdat"],
            )

    def test_truncated_mp4_atom_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(struct.pack(">I4s", 100, b"mdat") + b"short")
            with self.assertRaisesRegex(ValidationError, "exceeds file"):
                top_level_mp4_atoms(path)

    def test_checksum_parser_rejects_duplicate_and_traversal(self) -> None:
        digest = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "SHA256SUMS"
            path.write_text(f"{digest}  same\n{digest}  same\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate"):
                parse_checksums(path)
            path.write_text(f"{digest}  ../escape\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "traversal"):
                parse_checksums(path)

    def test_playlist_rejects_network_and_parent_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.m3u8"
            path.write_text("#EXTM3U\nhttps://example.invalid/a.ts\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "network"):
                playlist_segment_uris(path)
            path.write_text("#EXTM3U\n../a.ts\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "traversal"):
                playlist_segment_uris(path)

    def test_playlist_validates_uri_attributes_and_master_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.m3u8"
            path.write_text(
                "#EXTM3U\n"
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio/index.m3u8"\n'
                '#EXT-X-MAP:URI="video/init.mp4"\n'
                '#EXT-X-KEY:METHOD=AES-128,URI="keys/current.bin"\n'
                "video/index.m3u8\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    "audio/index.m3u8",
                    "video/init.mp4",
                    "keys/current.bin",
                    "video/index.m3u8",
                ],
                playlist_segment_uris(path),
            )
            path.write_text(
                '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://example.invalid/key"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "network"):
                playlist_segment_uris(path)

    def test_live_windows_are_immutable_ordered_inputs(self) -> None:
        playlist = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:9
#EXTINF:2.000000,
segments/a.ts
#EXTINF:2.000000,
segments/b.ts
#EXTINF:2.000000,
segments/c.ts
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "all.m3u8"
            source.write_text(playlist, encoding="utf-8")
            output = root / "live"
            write_windows(source, output, 2, 2.0)
            schedule = json.loads((output / "schedule.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["media_sequence"] for entry in schedule["entries"]], [9, 10])
            self.assertNotIn(
                "#EXT-X-ENDLIST",
                (output / "window-0009.m3u8").read_text(encoding="utf-8"),
            )

    def test_separate_live_schedules_are_paired_atomically(self) -> None:
        digest_a = "a" * 64
        digest_b = "b" * 64
        base = {
            "schema": 1,
            "interval_seconds": 2,
            "window_segments": 3,
            "segment_count": 4,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.json"
            video = root / "video.json"
            output = root / "paired.json"
            audio.write_text(json.dumps({
                **base,
                "entries": [{
                    "media_sequence": 7,
                    "playlist": "window-0007.m3u8",
                    "sha256": digest_a,
                }],
            }), encoding="utf-8")
            video.write_text(json.dumps({
                **base,
                "entries": [{
                    "media_sequence": 7,
                    "playlist": "window-0007.m3u8",
                    "sha256": digest_b,
                }],
            }), encoding="utf-8")
            write_paired_schedule(audio, video, output)
            paired = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(7, paired["entries"][0]["media_sequence"])
            self.assertEqual("audio/window-0007.m3u8",
                             paired["entries"][0]["audio"]["playlist"])
            self.assertEqual(digest_b, paired["entries"][0]["video"]["sha256"])

            broken = json.loads(video.read_text(encoding="utf-8"))
            broken["entries"][0]["media_sequence"] = 8
            video.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "media sequences differ"):
                write_paired_schedule(audio, video, output)

    def test_separate_live_tail_alignment_removes_partial_and_unpaired_segments(self) -> None:
        def write_track(root: Path, name: str, durations: list[float]) -> Path:
            track = root / name
            segments = track / "segments"
            segments.mkdir(parents=True)
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
            for index, duration in enumerate(durations):
                child = segments / f"{name}-{index:03d}.ts"
                child.write_bytes(bytes([index]))
                lines.extend((f"#EXTINF:{duration:.6f},", f"segments/{child.name}"))
            playlist = track / "all.m3u8"
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return playlist

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = write_track(root, "audio", [2.005, 1.984, 2.005, 0.021, 0.021])
            video = write_track(root, "video", [2.000, 2.000, 2.000, 0.033])
            self.assertEqual(align_pair(audio, video, 2.0), 3)
            self.assertEqual(playlist_segment_uris(audio), [
                "segments/audio-000.ts",
                "segments/audio-001.ts",
                "segments/audio-002.ts",
            ])
            self.assertEqual(playlist_segment_uris(video), [
                "segments/video-000.ts",
                "segments/video-001.ts",
                "segments/video-002.ts",
            ])
            self.assertFalse((audio.parent / "segments/audio-003.ts").exists())
            self.assertFalse((audio.parent / "segments/audio-004.ts").exists())
            self.assertFalse((video.parent / "segments/video-003.ts").exists())

    def test_separate_live_alignment_rejects_an_interior_partial_segment(self) -> None:
        def write_track(root: Path, name: str) -> Path:
            track = root / name
            segments = track / "segments"
            segments.mkdir(parents=True)
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
            for index, duration in enumerate((2.0, 0.02, 2.0)):
                child = segments / f"{name}-{index:03d}.ts"
                child.write_bytes(bytes([index]))
                lines.extend((f"#EXTINF:{duration:.6f},", f"segments/{child.name}"))
            playlist = track / "all.m3u8"
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return playlist

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "fewer than two"):
                align_pair(write_track(root, "audio"), write_track(root, "video"), 2.0)

    def test_separate_live_alignment_rejects_duplicate_segment_uris(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segments = root / "segments"
            segments.mkdir()
            (segments / "same.ts").write_bytes(b"fixture")
            playlist = root / "all.m3u8"
            playlist.write_text(
                "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
                "#EXTINF:2.000000,\nsegments/same.ts\n"
                "#EXTINF:2.000000,\nsegments/same.ts\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                align_pair(playlist, playlist, 2.0)

    def test_checked_relative_rejects_noncanonical_paths(self) -> None:
        self.assertEqual(checked_relative("a/b"), Path("a/b"))
        for value in ("/a", "../a", "a/../b", "a//b"):
            with self.assertRaises(ValidationError):
                checked_relative(value)


if __name__ == "__main__":
    unittest.main()
