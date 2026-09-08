#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Bind separate HLS audio/video window schedules into atomic update pairs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def load_schedule(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError(f"unsupported schedule: {path.name}")
    return data


def checked_playlist(value: object, track: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{track} schedule has an invalid playlist")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"{track} schedule playlist is not canonical")
    return f"{track}/{value}"


def write_paired_schedule(audio_path: Path, video_path: Path, output: Path) -> None:
    audio = load_schedule(audio_path)
    video = load_schedule(video_path)
    for key in ("interval_seconds", "window_segments", "segment_count"):
        if audio.get(key) != video.get(key):
            raise ValueError(f"audio/video schedule {key} differs")

    audio_entries = audio.get("entries")
    video_entries = video.get("entries")
    if not isinstance(audio_entries, list) or not isinstance(video_entries, list):
        raise ValueError("audio/video schedule entries must be lists")
    if not audio_entries or len(audio_entries) != len(video_entries):
        raise ValueError("audio/video schedule entry counts differ")

    paired_entries: list[dict[str, object]] = []
    for audio_entry, video_entry in zip(audio_entries, video_entries, strict=True):
        if not isinstance(audio_entry, dict) or not isinstance(video_entry, dict):
            raise ValueError("audio/video schedule entry is not an object")
        sequence = audio_entry.get("media_sequence")
        if not isinstance(sequence, int) or video_entry.get("media_sequence") != sequence:
            raise ValueError("audio/video media sequences differ")
        pair: dict[str, object] = {"media_sequence": sequence}
        for track, entry in (("audio", audio_entry), ("video", video_entry)):
            digest = entry.get("sha256")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise ValueError(f"{track} schedule has an invalid SHA-256")
            pair[track] = {
                "playlist": checked_playlist(entry.get("playlist"), track),
                "sha256": digest,
            }
        paired_entries.append(pair)

    document = {
        "description": (
            "Publish each audio/video playlist pair as one service generation; "
            "never advance either track independently."
        ),
        "entries": paired_entries,
        "interval_seconds": audio["interval_seconds"],
        "schema": 1,
        "segment_count": audio["segment_count"],
        "window_segments": audio["window_segments"],
    }
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_paired_schedule(args.audio, args.video, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
