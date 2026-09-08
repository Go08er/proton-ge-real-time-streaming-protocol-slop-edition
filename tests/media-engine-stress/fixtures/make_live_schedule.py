#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Turn an FFmpeg omit-endlist HLS playlist into deterministic live windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


MEDIA_SEQUENCE_RE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)$")
TARGET_DURATION_RE = re.compile(r"^#EXT-X-TARGETDURATION:(\d+)$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_segments(lines: list[str]) -> tuple[int, int, list[tuple[str, str]]]:
    sequence = 0
    target_duration = 0
    segments: list[tuple[str, str]] = []
    pending_extinf: str | None = None

    for line in lines:
        media_match = MEDIA_SEQUENCE_RE.match(line)
        if media_match:
            sequence = int(media_match.group(1))
            continue
        target_match = TARGET_DURATION_RE.match(line)
        if target_match:
            target_duration = int(target_match.group(1))
            continue
        if line.startswith("#EXTINF:"):
            pending_extinf = line
            continue
        if line and not line.startswith("#"):
            if pending_extinf is None:
                raise ValueError(f"segment URI has no EXTINF: {line}")
            segments.append((pending_extinf, line))
            pending_extinf = None

    if pending_extinf is not None:
        raise ValueError("playlist ends after EXTINF")
    if target_duration <= 0:
        raise ValueError("playlist has no positive target duration")
    if not segments:
        raise ValueError("playlist contains no segments")
    return sequence, target_duration, segments


def write_windows(source: Path, output: Path, window_size: int, interval: float) -> None:
    output.mkdir(parents=True, exist_ok=True)
    lines = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()]
    base_sequence, target_duration, segments = parse_segments(lines)
    if not 2 <= window_size <= len(segments):
        raise ValueError(
            f"window size must be between 2 and {len(segments)}, got {window_size}"
        )

    schedule_entries: list[dict[str, object]] = []
    last_start = len(segments) - window_size

    for offset in range(last_start + 1):
        sequence = base_sequence + offset
        name = f"window-{sequence:04d}.m3u8"
        path = output / name
        selected = segments[offset : offset + window_size]
        body = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for extinf, uri in selected:
            body.extend((extinf, uri))
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        schedule_entries.append(
            {
                "media_sequence": sequence,
                "playlist": name,
                "sha256": sha256(path),
            }
        )

    schedule = {
        "schema": 1,
        "description": (
            "Serve each immutable playlist in order at the declared interval; "
            "do not mutate these Nix-store inputs."
        ),
        "interval_seconds": interval,
        "window_segments": window_size,
        "segment_count": len(segments),
        "entries": schedule_entries,
    }
    (output / "schedule.json").write_text(
        json.dumps(schedule, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--window-size", type=int, required=True)
    parser.add_argument("--interval-seconds", type=float, required=True)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    write_windows(args.source, args.output, args.window_size, args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
