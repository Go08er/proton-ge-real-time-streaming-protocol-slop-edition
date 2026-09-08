#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Remove unpaired or partial terminal segments from separate live HLS tracks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re


EXTINF_RE = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?),$")


def parse_playlist(path: Path) -> tuple[list[str], list[tuple[str, float, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header: list[str] = []
    segments: list[tuple[str, float, str]] = []
    seen_uris: set[str] = set()
    index = 0
    while index < len(lines) and not lines[index].startswith("#EXTINF:"):
        if lines[index] == "#EXT-X-ENDLIST":
            raise ValueError(f"live playlist unexpectedly has ENDLIST: {path}")
        header.append(lines[index])
        index += 1
    while index < len(lines):
        match = EXTINF_RE.fullmatch(lines[index])
        if match is None or index + 1 >= len(lines):
            raise ValueError(f"unsupported generated live playlist shape: {path}")
        uri = lines[index + 1]
        relative = PurePosixPath(uri)
        if (
            not uri
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != uri
            or uri.startswith("#")
        ):
            raise ValueError(f"non-canonical generated segment URI: {uri}")
        if uri in seen_uris:
            raise ValueError(f"duplicate generated segment URI: {uri}")
        seen_uris.add(uri)
        segments.append((lines[index], float(match.group(1)), uri))
        index += 2
    if not header or header[0] != "#EXTM3U" or not segments:
        raise ValueError(f"generated live playlist is incomplete: {path}")
    return header, segments


def checked_child(playlist: Path, uri: str) -> Path:
    root = playlist.parent.resolve(strict=True)
    child = (root / Path(*PurePosixPath(uri).parts)).resolve(strict=True)
    if not child.is_file() or not child.is_relative_to(root):
        raise ValueError(f"generated segment escaped its track root: {uri}")
    return child


def rewrite_prefix(
    path: Path,
    header: list[str],
    segments: list[tuple[str, float, str]],
    count: int,
) -> None:
    retained = segments[:count]
    removed = [checked_child(path, uri) for _extinf, _duration, uri in segments[count:]]
    lines = list(header)
    for extinf, _duration, uri in retained:
        lines.extend((extinf, uri))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    for child in removed:
        child.unlink()


def align_pair(audio: Path, video: Path, segment_seconds: float) -> int:
    if not 0.25 <= segment_seconds <= 60.0:
        raise ValueError("segment_seconds is outside the supported range")
    audio_header, audio_segments = parse_playlist(audio)
    video_header, video_segments = parse_playlist(video)
    common = min(len(audio_segments), len(video_segments))
    minimum_complete_duration = segment_seconds * 0.90
    for index in range(common):
        if (
            audio_segments[index][1] < minimum_complete_duration
            or video_segments[index][1] < minimum_complete_duration
        ):
            common = index
            break
    if common < 2:
        raise ValueError("fewer than two aligned complete live segments remain")
    rewrite_prefix(audio, audio_header, audio_segments, common)
    rewrite_prefix(video, video_header, video_segments, common)
    return common


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--segment-seconds", required=True, type=float)
    args = parser.parse_args()
    align_pair(args.audio, args.video, args.segment_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
