#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Fail-closed validation for generated media-engine stress fixtures."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


class ValidationError(RuntimeError):
    """The fixture tree does not satisfy its declared contract."""


URI_ATTRIBUTE_RE = re.compile(r'(?:^|,)URI=(?:"([^"]*)"|([^,]*))')
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read JSON {path}: {error}") from error


def checked_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    require(not pure.is_absolute(), f"absolute path in manifest: {value}")
    require(".." not in pure.parts, f"parent traversal in manifest: {value}")
    require(value == pure.as_posix(), f"non-canonical path in manifest: {value}")
    return Path(*pure.parts)


def discover_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"fixture tree contains a symlink: {path}")
        if path.is_file():
            files.append(path.relative_to(root))
    return sorted(files, key=lambda item: item.as_posix().encode("utf-8"))


def parse_checksums(path: Path) -> dict[Path, str]:
    records: dict[Path, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split("  ", 1)
        require(len(parts) == 2, f"invalid SHA256SUMS line {line_number}")
        digest, raw_path = parts
        require(
            len(digest) == 64 and all(character in "0123456789abcdef" for character in digest),
            f"invalid SHA-256 at SHA256SUMS line {line_number}",
        )
        relative = checked_relative(raw_path)
        require(relative not in records, f"duplicate SHA256SUMS path: {raw_path}")
        records[relative] = digest
    return records


def top_level_mp4_atoms(path: Path) -> list[tuple[str, int, int]]:
    atoms: list[tuple[str, int, int]] = []
    data = path.read_bytes()
    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        raw_kind = data[offset + 4 : offset + 8]
        try:
            kind = raw_kind.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValidationError(f"non-ASCII MP4 atom at {path}:{offset}") from error
        header_size = 8
        if size == 1:
            require(offset + 16 <= len(data), f"truncated large MP4 atom at {path}:{offset}")
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        require(size >= header_size, f"invalid MP4 atom size at {path}:{offset}")
        require(offset + size <= len(data), f"MP4 atom exceeds file at {path}:{offset}")
        atoms.append((kind, offset, size))
        offset += size
    require(offset == len(data), f"trailing bytes outside MP4 atoms: {path}")
    return atoms


def playlist_segment_uris(path: Path) -> list[str]:
    uris = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        candidates: list[str] = []
        if line.startswith("#"):
            if "URI=" not in line:
                continue
            attribute_text = line.partition(":")[2]
            matches = URI_ATTRIBUTE_RE.findall(attribute_text)
            require(bool(matches), f"malformed HLS URI attribute in {path}")
            candidates.extend(quoted or unquoted for quoted, unquoted in matches)
        else:
            candidates.append(line)
        for candidate in candidates:
            require(candidate != "", f"empty HLS child URI in {path}")
            require("\\" not in candidate, f"backslash HLS child URI in {path}: {candidate}")
            require("?" not in candidate and "#" not in candidate,
                    f"query or fragment in deterministic HLS child URI in {path}: {candidate}")
            require(not URI_SCHEME_RE.match(candidate), f"network HLS child URI in {path}: {candidate}")
            pure = PurePosixPath(candidate)
            require(not pure.is_absolute(), f"absolute HLS child URI in {path}: {candidate}")
            require(".." not in pure.parts, f"HLS child traversal in {path}: {candidate}")
            require(candidate == pure.as_posix(), f"non-canonical HLS child URI in {path}: {candidate}")
            uris.append(candidate)
    return uris


def load_probe(root: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    probe_record = artifact.get("ffprobe")
    require(isinstance(probe_record, dict), f"missing ffprobe record: {artifact['path']}")
    report_path = checked_relative(str(probe_record.get("path", "")))
    report = load_json(root / report_path)
    require(
        sha256_file(root / report_path) == probe_record.get("sha256"),
        f"ffprobe report hash mismatch: {report_path}",
    )
    require(report.get("status") == probe_record.get("status"), f"probe status mismatch: {report_path}")
    return report


def codec_types(report: dict[str, Any]) -> list[str]:
    return [str(stream.get("codec_type")) for stream in report.get("streams", [])]


def stream_of_type(report: dict[str, Any], media_type: str) -> dict[str, Any]:
    matches = [
        stream
        for stream in report.get("streams", [])
        if stream.get("codec_type") == media_type
    ]
    require(len(matches) == 1, f"expected exactly one {media_type} stream")
    return matches[0]


def packet_first_pts(report: dict[str, Any], media_type: str) -> float:
    value = report.get("packets", {}).get(media_type, {}).get("first_pts_time")
    require(isinstance(value, (int, float)), f"missing first {media_type} packet PTS")
    return float(value)


def validate_duration(report: dict[str, Any], expected: float, label: str) -> None:
    raw = report.get("format", {}).get("duration")
    require(raw is not None, f"{label} has no probed duration")
    actual = float(raw)
    require(abs(actual - expected) <= 0.20, f"{label} duration {actual} != {expected}")


def validate_decoded_audio_duration(
    ffmpeg: str,
    root: Path,
    relative: str,
    expected: float,
    sample_rate: int,
    channels: int,
) -> None:
    """Measure raw elementary audio by decoded samples, not bitrate estimation.

    ADTS has no authoritative container duration. FFprobe estimates its
    duration from average encoded bitrate, which can drift substantially on a
    longer variable-size AAC stream even when the decoded timeline is correct.
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        relative,
        "-map",
        "0:a:0",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"decoded-duration check timed out: {relative}") from error
    require(result.returncode == 0, f"decoded-duration check failed: {relative}")
    bytes_per_frame = channels * 2
    require(len(result.stdout) % bytes_per_frame == 0,
            f"decoded PCM has a partial frame: {relative}")
    actual = (len(result.stdout) // bytes_per_frame) / sample_rate
    require(abs(actual - expected) <= 0.10,
            f"{relative} decoded duration {actual} != {expected}")


def run_decode(ffmpeg: str, root: Path, relative: str, should_succeed: bool) -> None:
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        relative,
        "-map",
        "0",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"decode timed out: {relative}") from error
    if should_succeed:
        require(result.returncode == 0, f"valid fixture does not decode: {relative}")
    else:
        require(result.returncode != 0, f"truncated fixture decoded without an error: {relative}")


def rms(samples: list[int]) -> float:
    require(bool(samples), "cannot measure an empty PCM window")
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def crossing_frequency(samples: list[int], sample_rate: int) -> float:
    require(len(samples) >= 2, "cannot estimate frequency from an empty PCM window")
    average = sum(samples) / len(samples)
    signs = [sample >= average for sample in samples]
    crossings = sum(left != right for left, right in zip(signs, signs[1:]))
    return crossings * sample_rate / (2.0 * len(samples))


def validate_audio_markers(
    ffmpeg: str,
    root: Path,
    relative: str,
    duration: int,
    sample_rate: int,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        relative,
        "-map",
        "0:a:0",
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"PCM marker decode timed out: {relative}") from error
    require(result.returncode == 0, f"PCM marker decode failed: {relative}")
    pcm = array.array("h")
    pcm.frombytes(result.stdout)
    if sys.byteorder != "little":
        pcm.byteswap()
    frame_count = len(pcm) // 2
    require(frame_count >= int(duration * sample_rate * 0.98), "decoded PCM is unexpectedly short")

    seconds = sorted({1, duration // 3, (2 * duration) // 3, max(1, duration - 2)})
    observed: dict[int, tuple[float, float]] = {}
    for second in seconds:
        marker_start = int((second + 0.02) * sample_rate)
        marker_end = int((second + 0.09) * sample_rate)
        quiet_start = int((second + 0.25) * sample_rate)
        quiet_end = int((second + 0.75) * sample_rate)
        require(quiet_end <= frame_count, f"PCM marker window exceeds decoded data at second {second}")
        channels: list[tuple[float, float]] = []
        for channel in (0, 1):
            marker = list(pcm[marker_start * 2 + channel : marker_end * 2 + channel : 2])
            quiet = list(pcm[quiet_start * 2 + channel : quiet_end * 2 + channel : 2])
            marker_rms = rms(marker)
            quiet_rms = rms(quiet)
            require(quiet_rms > 100.0, f"continuous carrier is absent at second {second}, channel {channel}")
            require(marker_rms > quiet_rms * 2.0, f"marker energy is absent at second {second}, channel {channel}")
            channels.append((marker_rms, crossing_frequency(marker, sample_rate)))
        observed[second] = (channels[0][1], channels[1][1])

    for second, (left, right) in observed.items():
        expected_left = 400.0 + 20.0 * second
        expected_right = 3400.0 + 20.0 * second
        require(abs(left - expected_left) / expected_left <= 0.10,
                f"left marker identity mismatch at second {second}: {left:.1f} Hz")
        require(abs(right - expected_right) / expected_right <= 0.10,
                f"right marker identity mismatch at second {second}: {right:.1f} Hz")
    require(len(set(observed.values())) == len(observed), "decoded audio markers are not unique")


def validate_video_frames(
    ffmpeg: str,
    root: Path,
    relative: str,
    duration: int,
    frames_per_second: int,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        relative,
        "-map",
        "0:v:0",
        "-f",
        "framemd5",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
            check=False,
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"video frame identity decode timed out: {relative}") from error
    require(result.returncode == 0, f"video frame identity decode failed: {relative}")
    rows = [line for line in result.stdout.decode("ascii").splitlines() if line and not line.startswith("#")]
    expected = duration * frames_per_second
    require(abs(len(rows) - expected) <= frames_per_second, "decoded video frame count is incorrect")
    hashes = [row.rsplit(",", 1)[-1].strip() for row in rows]
    require(len(set(hashes)) >= int(len(hashes) * 0.98), "video fixture is frozen or excessively repetitive")


def probe_video_packets(
    ffprobe: str,
    root: Path,
    relative: str,
) -> list[dict[str, Any]]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,dts_time,flags",
        "-of",
        "json",
        relative,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
            check=False,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"RTSP reproduction timing probe timed out: {relative}") from error
    require(result.returncode == 0, f"RTSP reproduction timing probe failed: {relative}")
    try:
        packets = json.loads(result.stdout).get("packets", [])
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as error:
        raise ValidationError(f"RTSP reproduction timing probe is invalid: {relative}") from error
    require(isinstance(packets, list) and packets, f"RTSP reproduction has no video packets: {relative}")
    return packets


def validate_observed_rtsp_timing(
    ffprobe: str,
    root: Path,
    relative: str,
    expected_keyframe_gap: float,
) -> None:
    packets = probe_video_packets(ffprobe, root, relative)
    key_pts = [
        float(packet["pts_time"])
        for packet in packets
        if "K" in str(packet.get("flags", "")) and "pts_time" in packet
    ]
    require(len(key_pts) == 23, f"RTSP reproduction has {len(key_pts)} keyframes instead of 23")
    gaps = [right - left for left, right in zip(key_pts, key_pts[1:])]
    require(
        gaps and all(abs(gap - expected_keyframe_gap) <= 0.01 for gap in gaps),
        "RTSP reproduction keyframe spacing is not two seconds",
    )
    reorder = [
        float(packet["pts_time"]) - float(packet["dts_time"])
        for packet in packets
        if "pts_time" in packet and "dts_time" in packet
    ]
    require(max(reorder, default=0.0) >= 0.16,
            "RTSP reproduction does not preserve the observed reorder depth")


def validate_midgop_rtsp_timing(
    ffprobe: str,
    root: Path,
    relative: str,
    first_keyframe_deadline: float,
) -> None:
    packets = probe_video_packets(ffprobe, root, relative)
    require("K" not in str(packets[0].get("flags", "")),
            "mid-GOP reproduction starts on a keyframe")
    key_pts = [
        float(packet["pts_time"])
        for packet in packets
        if "K" in str(packet.get("flags", "")) and "pts_time" in packet
    ]
    require(key_pts and 0.0 < key_pts[0] <= first_keyframe_deadline,
            "mid-GOP reproduction does not recover an IDR within its deadline")
    gaps = [right - left for left, right in zip(key_pts, key_pts[1:])]
    require(gaps and all(abs(gap - 2.0) <= 0.01 for gap in gaps),
            "mid-GOP reproduction changed the later keyframe cadence")


def validate(
    root: Path,
    profiles_path: Path,
    ffmpeg: str | None,
    ffprobe: str | None,
) -> None:
    root = root.resolve()
    require(root.is_dir(), f"fixture root is not a directory: {root}")

    manifest_path = root / "provenance" / "manifest.json"
    sums_path = root / "SHA256SUMS"
    ready_path = root / "READY"
    manifest = load_json(manifest_path)
    ready = load_json(ready_path)
    profiles = load_json(profiles_path)
    pin_path = root / "provenance" / "nixpkgs-pin.json"
    pin = load_json(pin_path)
    require(manifest.get("schema") == 1, "unsupported fixture manifest schema")
    require(ready.get("schema") == 1, "unsupported READY schema")
    profile_name = manifest.get("profile", {}).get("name")
    require(profile_name in profiles.get("profiles", {}), f"unknown profile: {profile_name}")
    profile = profiles["profiles"][profile_name]
    require(profiles.get("marker_contract", {}).get("seek_video_tolerance_frames") == 1,
            "unknown video marker contract")
    require(profiles.get("marker_contract", {}).get("seek_audio_tolerance_milliseconds") == 40,
            "unknown audio marker contract")
    ladder = profiles.get("rtsp_payload_ladder", {})
    ladder_rungs = ladder.get("rungs", {})
    ladder_common = ladder.get("common", {})
    reproduction = profiles.get("rtsp_reproduction_inputs", {})
    observed = reproduction.get("observed-heavy-v1", {})
    midgop = reproduction.get("observed-midgop-v1", {})
    require(set(ladder_rungs) == {"a", "b", "c", "d"},
            "RTSP payload ladder rung set differs")
    require(reproduction.get("included_profiles") == ["full"],
            "RTSP reproduction profile selection differs")
    require(observed.get("path") == "rtsp/repro-observed-heavy-v1.mp4",
            "RTSP reproduction path differs")
    require(midgop.get("path") == "rtsp/repro-observed-midgop-v1.mp4"
            and midgop.get("derived_from") == "observed-heavy-v1",
            "mid-GOP reproduction contract differs")
    require(manifest["profile"].get("parameters") == profile, "selected profile parameters drifted")
    require(ready.get("profile") == profile_name, "READY profile mismatch")
    require(ready.get("manifest_sha256") == sha256_file(manifest_path), "READY manifest hash mismatch")
    require(ready.get("sha256sums_sha256") == sha256_file(sums_path), "READY sums hash mismatch")

    all_files = discover_files(root)
    checksum_expected = set(all_files) - {Path("SHA256SUMS"), Path("READY")}
    checksums = parse_checksums(sums_path)
    require(set(checksums) == checksum_expected, "SHA256SUMS file set is incomplete or has extras")
    for relative, expected_hash in checksums.items():
        require(sha256_file(root / relative) == expected_hash, f"hash mismatch: {relative}")

    artifact_expected = checksum_expected - {Path("provenance/manifest.json")}
    artifact_map: dict[str, dict[str, Any]] = {}
    for record in manifest.get("artifacts", []):
        require(isinstance(record, dict), "non-object artifact record")
        relative = checked_relative(str(record.get("path", "")))
        require(relative.as_posix() not in artifact_map, f"duplicate artifact: {relative}")
        require((root / relative).is_file(), f"missing artifact: {relative}")
        require((root / relative).stat().st_size == record.get("bytes"), f"size mismatch: {relative}")
        require(sha256_file(root / relative) == record.get("sha256"), f"manifest hash mismatch: {relative}")
        artifact_map[relative.as_posix()] = record
    require(
        set(artifact_map) == {path.as_posix() for path in artifact_expected},
        "artifact manifest file set is incomplete or has extras",
    )
    provenance = manifest.get("provenance", {})
    nixpkgs_provenance = provenance.get("nixpkgs", {})
    require(pin.get("schema") == 1, "unsupported nixpkgs pin schema")
    require(re.fullmatch(r"[0-9a-f]{40}", str(pin.get("revision", ""))) is not None,
            "nixpkgs pin lacks a full revision")
    require(nixpkgs_provenance.get("revision") == pin.get("revision"), "nixpkgs revision drifted")
    require(nixpkgs_provenance.get("nar_hash") == pin.get("nar_hash_sri"), "nixpkgs NAR hash drifted")
    require(provenance.get("nixpkgs_pin_sha256") == sha256_file(pin_path),
            "nixpkgs pin document hash mismatch")
    for label, value in (
        ("nixpkgs", provenance.get("nixpkgs", {}).get("path")),
        ("ffmpeg", provenance.get("ffmpeg", {}).get("binary")),
        ("ffprobe", provenance.get("ffprobe", {}).get("binary")),
    ):
        require(isinstance(value, str) and value.startswith("/nix/store/"),
                f"{label} provenance is not an immutable store path")

    required = {
        "av/faststart.mp4",
        "av/tail-moov.mp4",
        "av/fragmented.mp4",
        "topology/audio-only.m4a",
        "topology/video-only.mp4",
        "topology/delayed-audio.mp4",
        "topology/delayed-video.mp4",
        "hls/vod/index.m3u8",
        "hls/live/all.m3u8",
        "hls/live/schedule.json",
        "hls/vod-separate/master.m3u8",
        "hls/live-separate/master.m3u8",
        "hls/live-separate/schedule.json",
        "hls/cmaf/index.m3u8",
        "discovery/containers/h264-aac.mp4",
        "discovery/containers/h264-aac.mov",
        "discovery/containers/h264-aac.mkv",
        "discovery/containers/h264-aac.ts",
        "discovery/codecs/hevc-aac.mp4",
        "discovery/codecs/vp8-vorbis.webm",
        "discovery/codecs/vp9-opus.webm",
        "discovery/codecs/mpeg4-mp3.avi",
        "discovery/codecs/wmv2-wmav2.asf",
        "discovery/codecs/av1-opus.webm",
        "discovery/audio/aac-5.1.m4a",
        "discovery/audio/two-audio-tracks.mp4",
        "invalid/not-media.bin",
        "invalid/empty.bin",
        "invalid/truncated-faststart.mp4",
        "invalid/truncated-tail.mp4",
        "invalid/missing-segment.m3u8",
        "invalid/malformed.m3u8",
    }
    require(required <= set(artifact_map), f"missing required fixtures: {sorted(required - set(artifact_map))}")
    for sample_rate in (44100, 48000):
        for stem, suffix in (
            ("mp3", "mp3"),
            ("aac-adts", "aac"),
            ("pcm-s16", "wav"),
            ("flac", "flac"),
            ("vorbis", "ogg"),
        ):
            path = f"discovery/audio/{stem}-{sample_rate}.{suffix}"
            require(path in artifact_map, f"missing audio discovery fixture: {path}")
    require("discovery/audio/opus-48000.opus" in artifact_map, "missing 48 kHz Opus fixture")
    ladder_paths = {
        str(specification.get("path"))
        for specification in ladder_rungs.values()
        if isinstance(specification, dict)
    }
    if profile_name in ladder.get("included_profiles", []):
        require(ladder_paths <= set(artifact_map),
                f"missing RTSP payload rungs: {sorted(ladder_paths - set(artifact_map))}")
    else:
        require(not (ladder_paths & set(artifact_map)),
                "RTSP payload ladder leaked into a profile that excludes it")
    observed_path = str(observed.get("path"))
    midgop_path = str(midgop.get("path"))
    if profile_name in reproduction.get("included_profiles", []):
        require(observed_path in artifact_map, "missing observed RTSP reproduction input")
        require(midgop_path in artifact_map, "missing mid-GOP RTSP reproduction input")
    else:
        require(observed_path not in artifact_map and midgop_path not in artifact_map,
                "RTSP reproduction inputs leaked into an excluded profile")

    fast_atoms = top_level_mp4_atoms(root / "av/faststart.mp4")
    tail_atoms = top_level_mp4_atoms(root / "av/tail-moov.mp4")
    fragment_atoms = top_level_mp4_atoms(root / "av/fragmented.mp4")
    fast_names = [atom[0] for atom in fast_atoms]
    tail_names = [atom[0] for atom in tail_atoms]
    fragment_names = [atom[0] for atom in fragment_atoms]
    require(fast_names.index("moov") < fast_names.index("mdat"), "fast-start MP4 moov is not first")
    require(tail_names.index("mdat") < tail_names.index("moov"), "tail-moov MP4 metadata is not last")
    require("moov" in fragment_names and "moof" in fragment_names, "fragmented MP4 lacks moov/moof")
    require(fragment_names.index("moov") < fragment_names.index("moof"), "fragmented MP4 lacks init metadata")
    require(fragment_names.count("moof") >= 2, "fragmented MP4 has fewer than two fragments")

    fast_probe = load_probe(root, artifact_map["av/faststart.mp4"])
    tail_probe = load_probe(root, artifact_map["av/tail-moov.mp4"])
    fragment_probe = load_probe(root, artifact_map["av/fragmented.mp4"])
    for label, report in (
        ("faststart", fast_probe),
        ("tail-moov", tail_probe),
        ("fragmented", fragment_probe),
    ):
        require(report.get("status") == "accepted", f"{label} MP4 was rejected")
        require(sorted(codec_types(report)) == ["audio", "video"], f"{label} topology mismatch")
        video = stream_of_type(report, "video")
        audio = stream_of_type(report, "audio")
        require(video.get("codec_name") == "h264", f"{label} video codec is not H.264")
        require(audio.get("codec_name") == "aac", f"{label} audio codec is not AAC")
        require(video.get("width") == profile["width"], f"{label} width mismatch")
        require(video.get("height") == profile["height"], f"{label} height mismatch")
        require(int(audio.get("sample_rate", 0)) == profile["audio_sample_rate"],
                f"{label} sample rate mismatch")
        require(audio.get("channels") == profile["audio_channels"], f"{label} channel count mismatch")
        require(report.get("packets", {}).get("video", {}).get("count", 0) > 0,
                f"{label} has no video packets")
        require(report.get("packets", {}).get("audio", {}).get("count", 0) > 0,
                f"{label} has no audio packets")
        validate_duration(report, float(profile["primary_duration_seconds"]), label)
    for media_type in ("audio", "video"):
        fast_packets = fast_probe["packets"][media_type]
        tail_packets = tail_probe["packets"][media_type]
        fragment_packets = fragment_probe["packets"][media_type]
        require(
            fast_packets["payload_sha256"] == tail_packets["payload_sha256"],
            f"fast-start and tail-moov {media_type} payloads differ",
        )
        require(
            fast_packets["payload_sha256"] == fragment_packets["payload_sha256"],
            f"flat and fragmented MP4 {media_type} payloads differ",
        )

    ladder_reports: dict[str, dict[str, Any]] = {}
    if profile_name in ladder.get("included_profiles", []):
        for rung, specification in ladder_rungs.items():
            path = str(specification["path"])
            report = load_probe(root, artifact_map[path])
            require(report.get("status") == "accepted", f"RTSP payload rung {rung} was rejected")
            require(sorted(codec_types(report)) == ["audio", "video"],
                    f"RTSP payload rung {rung} topology mismatch")
            video = stream_of_type(report, "video")
            audio = stream_of_type(report, "audio")
            require(video.get("codec_name") == "h264"
                    and video.get("profile") == specification["h264_profile"],
                    f"RTSP payload rung {rung} H.264 identity mismatch")
            require(video.get("width") == specification["width"]
                    and video.get("height") == specification["height"],
                    f"RTSP payload rung {rung} dimensions differ")
            require(video.get("pix_fmt") == ladder_common["pixel_format"]
                    and video.get("avg_frame_rate") == "30/1",
                    f"RTSP payload rung {rung} frame contract differs")
            expected_rate = int(specification["video_bitrate_bps"])
            actual_rate = int(video.get("bit_rate", 0))
            require(abs(actual_rate - expected_rate) <= expected_rate * 0.08,
                    f"RTSP payload rung {rung} bitrate {actual_rate} misses {expected_rate}")
            require(audio.get("codec_name") == ladder_common["audio_codec"]
                    and int(audio.get("sample_rate", 0)) == ladder_common["audio_sample_rate"]
                    and audio.get("channels") == ladder_common["audio_channels"],
                    f"RTSP payload rung {rung} audio contract differs")
            validate_duration(report, float(ladder_common["duration_seconds"]),
                              f"RTSP payload rung {rung}")
            ladder_reports[rung] = report
        baseline_audio = ladder_reports["a"]["packets"]["audio"]
        for rung, report in ladder_reports.items():
            audio_packets = report["packets"]["audio"]
            require(audio_packets["payload_sha256"] == baseline_audio["payload_sha256"]
                    and audio_packets["timeline_sha256"] == baseline_audio["timeline_sha256"],
                    f"RTSP payload rung {rung} does not carry identical audio packets")

    if profile_name in reproduction.get("included_profiles", []):
        report = load_probe(root, artifact_map[observed_path])
        require(report.get("status") == "accepted", "observed RTSP reproduction was rejected")
        require(sorted(codec_types(report)) == ["audio", "video"],
                "observed RTSP reproduction topology mismatch")
        video = stream_of_type(report, "video")
        audio = stream_of_type(report, "audio")
        require(video.get("codec_name") == "h264"
                and video.get("profile") == observed["h264_profile"],
                "observed RTSP reproduction H.264 identity mismatch")
        require(video.get("width") == observed["width"]
                and video.get("height") == observed["height"]
                and video.get("avg_frame_rate") == "30/1",
                "observed RTSP reproduction video shape differs")
        expected_rate = int(observed["video_bitrate_bps"])
        actual_rate = int(video.get("bit_rate", 0))
        require(abs(actual_rate - expected_rate) <= expected_rate * 0.08,
                "observed RTSP reproduction bitrate differs")
        require(audio.get("codec_name") == observed["audio_codec"]
                and int(audio.get("sample_rate", 0)) == observed["audio_sample_rate"]
                and audio.get("channels") == observed["audio_channels"],
                "observed RTSP reproduction audio shape differs")
        validate_duration(report, float(observed["duration_seconds"]),
                          "observed RTSP reproduction")
        source_record = (root / "provenance/rtsp-observed-heavy-source.txt").read_text(
            encoding="ascii"
        )
        captured_record = (
            "mode=captured-reference\n"
            f"sha256={observed['captured_reference_sha256']}\n"
        )
        generated_record = (
            "mode=deterministic-reconstruction\n"
            f"reference_sha256={observed['captured_reference_sha256']}\n"
        )
        require(source_record in {captured_record, generated_record},
                "observed RTSP reproduction provenance differs")
        if source_record == captured_record:
            require(artifact_map[observed_path]["sha256"] == observed["captured_reference_sha256"],
                    "captured RTSP reproduction bytes differ")
        require(ffprobe is not None, "RTSP reproduction validation requires FFprobe")
        validate_observed_rtsp_timing(
            ffprobe,
            root,
            observed_path,
            observed["gop_frames"] / observed["frames_per_second"],
        )
        midgop_report = load_probe(root, artifact_map[midgop_path])
        require(midgop_report.get("status") == "accepted",
                "mid-GOP RTSP reproduction was rejected")
        require(sorted(codec_types(midgop_report)) == ["audio", "video"],
                "mid-GOP RTSP reproduction topology mismatch")
        midgop_video = stream_of_type(midgop_report, "video")
        midgop_audio = stream_of_type(midgop_report, "audio")
        require(
            midgop_video.get("codec_name") == video.get("codec_name")
            and midgop_video.get("profile") == video.get("profile")
            and midgop_video.get("width") == video.get("width")
            and midgop_video.get("height") == video.get("height"),
            "mid-GOP reproduction changed the video identity",
        )
        require(midgop_audio.get("codec_name") == audio.get("codec_name")
                and midgop_audio.get("sample_rate") == audio.get("sample_rate")
                and midgop_audio.get("channels") == audio.get("channels"),
                "mid-GOP reproduction changed the audio identity")
        validate_duration(midgop_report, float(midgop["duration_seconds"]),
                          "mid-GOP RTSP reproduction")
        validate_midgop_rtsp_timing(
            ffprobe,
            root,
            midgop_path,
            float(midgop["first_keyframe_deadline_seconds"]),
        )

    audio_probe = load_probe(root, artifact_map["topology/audio-only.m4a"])
    video_probe = load_probe(root, artifact_map["topology/video-only.mp4"])
    require(codec_types(audio_probe) == ["audio"], "audio-only fixture has another track")
    require(codec_types(video_probe) == ["video"], "video-only fixture has another track")
    topology_duration = float(profile["topology_duration_seconds"])
    validate_duration(audio_probe, topology_duration, "audio-only")
    validate_duration(video_probe, topology_duration, "video-only")

    delay = float(profile["delayed_track_seconds"])
    delayed_audio = load_probe(root, artifact_map["topology/delayed-audio.mp4"])
    delayed_video = load_probe(root, artifact_map["topology/delayed-video.mp4"])
    require(sorted(codec_types(delayed_audio)) == ["audio", "video"], "delayed-audio topology mismatch")
    require(sorted(codec_types(delayed_video)) == ["audio", "video"], "delayed-video topology mismatch")
    require(abs(packet_first_pts(delayed_audio, "video")) <= 0.10, "delayed-audio video does not start at zero")
    require(abs(packet_first_pts(delayed_audio, "audio") - delay) <= 0.12, "audio delay is incorrect")
    require(abs(packet_first_pts(delayed_video, "audio")) <= 0.10, "delayed-video audio does not start at zero")
    require(abs(packet_first_pts(delayed_video, "video") - delay) <= 0.12, "video delay is incorrect")

    matrix_duration = float(profile["matrix_duration_seconds"])
    same_track_paths = [
        "discovery/containers/h264-aac.mp4",
        "discovery/containers/h264-aac.mov",
        "discovery/containers/h264-aac.mkv",
        "discovery/containers/h264-aac.ts",
    ]
    same_track_probes = []
    for path in same_track_paths:
        report = load_probe(root, artifact_map[path])
        require(report.get("status") == "accepted", f"container discovery member rejected: {path}")
        require(sorted(codec_types(report)) == ["audio", "video"], f"container topology mismatch: {path}")
        require(stream_of_type(report, "video").get("codec_name") == "h264", f"container video drift: {path}")
        require(stream_of_type(report, "audio").get("codec_name") == "aac", f"container audio drift: {path}")
        validate_duration(report, matrix_duration, path)
        same_track_probes.append(report)
    for media_type in ("audio", "video"):
        baseline_payload = same_track_probes[0]["packets"][media_type]["payload_sha256"]
        for path, report in zip(same_track_paths[1:3], same_track_probes[1:3]):
            require(report["packets"][media_type]["payload_sha256"] == baseline_payload,
                    f"same-track remux changed {media_type} payload: {path}")

    codec_members = {
        "discovery/codecs/hevc-aac.mp4": ("hevc", "aac"),
        "discovery/codecs/vp8-vorbis.webm": ("vp8", "vorbis"),
        "discovery/codecs/vp9-opus.webm": ("vp9", "opus"),
        "discovery/codecs/mpeg4-mp3.avi": ("mpeg4", "mp3"),
        "discovery/codecs/wmv2-wmav2.asf": ("wmv2", "wmav2"),
        "discovery/codecs/av1-opus.webm": ("av1", "opus"),
    }
    for path, (video_codec, audio_codec) in codec_members.items():
        report = load_probe(root, artifact_map[path])
        require(report.get("status") == "accepted", f"codec discovery member rejected: {path}")
        require(stream_of_type(report, "video").get("codec_name") == video_codec,
                f"video codec identity mismatch: {path}")
        require(stream_of_type(report, "audio").get("codec_name") == audio_codec,
                f"audio codec identity mismatch: {path}")
        validate_duration(report, matrix_duration, path)

    audio_codecs = {
        "mp3": ("mp3", "mp3"),
        "aac-adts": ("aac", "aac"),
        "pcm-s16": ("wav", "pcm_s16le"),
        "flac": ("flac", "flac"),
        "vorbis": ("ogg", "vorbis"),
    }
    for sample_rate in (44100, 48000):
        for stem, (suffix, codec) in audio_codecs.items():
            path = f"discovery/audio/{stem}-{sample_rate}.{suffix}"
            report = load_probe(root, artifact_map[path])
            require(report.get("status") == "accepted", f"audio discovery member rejected: {path}")
            require(codec_types(report) == ["audio"], f"audio-only topology mismatch: {path}")
            stream = stream_of_type(report, "audio")
            require(stream.get("codec_name") == codec, f"audio codec identity mismatch: {path}")
            require(int(stream.get("sample_rate", 0)) == sample_rate, f"audio rate mismatch: {path}")
            require(stream.get("channels") == 2, f"audio channel count mismatch: {path}")
            if stem == "aac-adts":
                validate_decoded_audio_duration(
                    ffmpeg,
                    root,
                    path,
                    matrix_duration,
                    sample_rate,
                    2,
                )
            else:
                validate_duration(report, matrix_duration, path)
    opus_path = "discovery/audio/opus-48000.opus"
    opus_report = load_probe(root, artifact_map[opus_path])
    opus_stream = stream_of_type(opus_report, "audio")
    require(opus_stream.get("codec_name") == "opus", "Opus codec identity mismatch")
    require(int(opus_stream.get("sample_rate", 0)) == 48000, "Opus sample rate is not 48 kHz")
    require(opus_stream.get("channels") == 2, "Opus channel count mismatch")
    validate_duration(opus_report, matrix_duration, opus_path)

    surround = load_probe(root, artifact_map["discovery/audio/aac-5.1.m4a"])
    surround_audio = stream_of_type(surround, "audio")
    require(surround_audio.get("codec_name") == "aac", "5.1 fixture codec is not AAC")
    require(surround_audio.get("channels") == 6, "5.1 fixture does not have six channels")
    validate_duration(surround, matrix_duration, "AAC 5.1")

    multi = load_probe(root, artifact_map["discovery/audio/two-audio-tracks.mp4"])
    require(codec_types(multi).count("video") == 1 and codec_types(multi).count("audio") == 2,
            "multi-track fixture topology mismatch")
    audio_streams = [stream for stream in multi["streams"] if stream.get("codec_type") == "audio"]
    require([stream.get("tags", {}).get("language") for stream in audio_streams] == ["eng", "jpn"],
            "multi-track language identity drifted")
    require([stream.get("disposition", {}).get("default", 0) for stream in audio_streams] == [1, 0],
            "multi-track default selection drifted")
    validate_duration(multi, matrix_duration, "multi-track")

    vod_playlist = root / "hls/vod/index.m3u8"
    vod_text = vod_playlist.read_text(encoding="utf-8")
    require("#EXT-X-ENDLIST" in vod_text, "VOD HLS has no ENDLIST")
    vod_segments = playlist_segment_uris(vod_playlist)
    require(len(vod_segments) >= 3, "VOD HLS has too few segments")
    for uri in vod_segments:
        require((vod_playlist.parent / uri).is_file(), f"missing VOD HLS segment: {uri}")

    separate_vod_root = root / "hls/vod-separate"
    separate_vod_master = separate_vod_root / "master.m3u8"
    require(
        playlist_segment_uris(separate_vod_master) == ["audio/index.m3u8", "video/index.m3u8"],
        "separate-rendition VOD master topology drifted",
    )
    for media_type in ("audio", "video"):
        playlist = separate_vod_root / media_type / "index.m3u8"
        require("#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8"),
                f"separate {media_type} VOD playlist has no ENDLIST")
        children = playlist_segment_uris(playlist)
        require(len(children) >= 3, f"separate {media_type} VOD has too few segments")
        for uri in children:
            require((playlist.parent / uri).is_file(), f"missing separate {media_type} segment: {uri}")

    cmaf_playlist = root / "hls/cmaf/index.m3u8"
    cmaf_text = cmaf_playlist.read_text(encoding="utf-8")
    require("#EXT-X-MAP:" in cmaf_text, "CMAF HLS lacks an initialization map")
    require("#EXT-X-ENDLIST" in cmaf_text, "CMAF HLS has no ENDLIST")
    cmaf_children = playlist_segment_uris(cmaf_playlist)
    require(len(cmaf_children) >= 4, "CMAF HLS has too few children")
    for uri in cmaf_children:
        require((cmaf_playlist.parent / uri).is_file(), f"missing CMAF HLS child: {uri}")

    live_root = root / "hls/live"
    all_playlist = live_root / "all.m3u8"
    require("#EXT-X-ENDLIST" not in all_playlist.read_text(encoding="utf-8"), "live HLS has ENDLIST")
    all_segments = playlist_segment_uris(all_playlist)
    schedule = load_json(live_root / "schedule.json")
    require(schedule.get("schema") == 1, "unsupported live schedule schema")
    require(schedule.get("segment_count") == len(all_segments), "live segment count mismatch")
    require(
        schedule.get("window_segments") == profile["hls_live_window_segments"],
        "live window size mismatch",
    )
    entries = schedule.get("entries")
    require(isinstance(entries, list) and len(entries) >= 2, "live schedule needs at least two windows")
    prior_sequence: int | None = None
    for entry in entries:
        sequence = entry.get("media_sequence")
        require(isinstance(sequence, int), "non-integer live media sequence")
        if prior_sequence is not None:
            require(sequence == prior_sequence + 1, "live media sequence does not advance by one")
        prior_sequence = sequence
        playlist_rel = checked_relative(str(entry.get("playlist", "")))
        playlist = live_root / playlist_rel
        require(playlist.is_file(), f"missing live window: {playlist_rel}")
        require(sha256_file(playlist) == entry.get("sha256"), f"live window hash mismatch: {playlist_rel}")
        text = playlist.read_text(encoding="utf-8")
        require("#EXT-X-ENDLIST" not in text, f"live window has ENDLIST: {playlist_rel}")
        require(f"#EXT-X-MEDIA-SEQUENCE:{sequence}" in text, f"live sequence mismatch: {playlist_rel}")
        for uri in playlist_segment_uris(playlist):
            resolved = (playlist.parent / uri).resolve()
            require(resolved.is_relative_to(live_root), f"live segment escaped fixture root: {uri}")
            require(resolved.is_file(), f"missing live HLS segment: {uri}")

    separate_live_root = root / "hls/live-separate"
    separate_live_master = separate_live_root / "master.m3u8"
    require(
        playlist_segment_uris(separate_live_master) == ["audio/index.m3u8", "video/index.m3u8"],
        "separate-rendition live master topology drifted",
    )
    track_schedules = {}
    track_segments: dict[str, list[str]] = {}
    for media_type in ("audio", "video"):
        track_root = separate_live_root / media_type
        index = track_root / "index.m3u8"
        require(index.is_file(), f"separate live {media_type} index is absent")
        require("#EXT-X-ENDLIST" not in index.read_text(encoding="utf-8"),
                f"separate live {media_type} index is finite")
        track_schedule = load_json(track_root / "schedule.json")
        require(track_schedule.get("schema") == 1, f"separate live {media_type} schedule schema")
        track_entries = track_schedule.get("entries")
        require(isinstance(track_entries, list) and len(track_entries) >= 2,
                f"separate live {media_type} schedule is too short")
        for entry in track_entries:
            playlist_rel = checked_relative(str(entry.get("playlist", "")))
            playlist = track_root / playlist_rel
            require(playlist.is_file(), f"missing separate live {media_type} window: {playlist_rel}")
            require(sha256_file(playlist) == entry.get("sha256"),
                    f"separate live {media_type} window hash mismatch")
            for uri in playlist_segment_uris(playlist):
                resolved = (playlist.parent / uri).resolve()
                require(resolved.is_relative_to(track_root),
                        f"separate live {media_type} child escaped its track root")
                require(resolved.is_file(), f"missing separate live {media_type} child: {uri}")
        track_schedules[media_type] = track_schedule
        all_track_segments = playlist_segment_uris(track_root / "all.m3u8")
        require(len(all_track_segments) == track_schedule.get("segment_count"),
                f"separate live {media_type} segment count mismatch")
        track_segments[media_type] = all_track_segments
    audio_sequences = [entry["media_sequence"] for entry in track_schedules["audio"]["entries"]]
    video_sequences = [entry["media_sequence"] for entry in track_schedules["video"]["entries"]]
    require(audio_sequences == video_sequences, "separate live audio/video schedules do not advance together")

    require(len(track_segments["audio"]) == len(track_segments["video"]),
            "separate live audio/video segment counts differ")
    for index, (audio_uri, video_uri) in enumerate(
        zip(track_segments["audio"], track_segments["video"], strict=True)
    ):
        audio_relative = f"hls/live-separate/audio/{audio_uri}"
        video_relative = f"hls/live-separate/video/{video_uri}"
        audio_probe = load_probe(root, artifact_map[audio_relative])
        video_probe = load_probe(root, artifact_map[video_relative])
        audio_packets = audio_probe.get("packets", {}).get("audio", {})
        video_packets = video_probe.get("packets", {}).get("video", {})
        for boundary in ("first_pts_time", "last_pts_time"):
            audio_pts = audio_packets.get(boundary)
            video_pts = video_packets.get(boundary)
            require(isinstance(audio_pts, (int, float)) and isinstance(video_pts, (int, float)),
                    f"separate live segment {index} lacks {boundary}")
            require(abs(float(audio_pts) - float(video_pts)) <= 0.150,
                    f"separate live segment {index} {boundary} skew exceeds 150 ms")

    paired_schedule = load_json(separate_live_root / "schedule.json")
    require(paired_schedule.get("schema") == 1, "paired live schedule schema mismatch")
    require(paired_schedule.get("interval_seconds") ==
            track_schedules["audio"].get("interval_seconds"),
            "paired live interval mismatch")
    paired_entries = paired_schedule.get("entries")
    require(isinstance(paired_entries, list) and len(paired_entries) == len(audio_sequences),
            "paired live schedule entry count mismatch")
    for index, paired in enumerate(paired_entries):
        require(isinstance(paired, dict), "paired live schedule entry is not an object")
        require(paired.get("media_sequence") == audio_sequences[index],
                "paired live schedule sequence mismatch")
        for media_type in ("audio", "video"):
            side = paired.get(media_type)
            require(isinstance(side, dict), f"paired live {media_type} entry is absent")
            expected = track_schedules[media_type]["entries"][index]
            expected_path = f"{media_type}/{expected['playlist']}"
            require(side.get("playlist") == expected_path,
                    f"paired live {media_type} playlist mismatch")
            require(side.get("sha256") == expected.get("sha256"),
                    f"paired live {media_type} hash mismatch")
            require(sha256_file(separate_live_root / expected_path) == side.get("sha256"),
                    f"paired live {media_type} file hash mismatch")

    for rejected in (
        "invalid/not-media.bin",
        "invalid/empty.bin",
        "invalid/truncated-tail.mp4",
        "invalid/missing-segment.m3u8",
    ):
        report = load_probe(root, artifact_map[rejected])
        require(report.get("status") == "rejected", f"invalid input was accepted: {rejected}")
    malformed = load_probe(root, artifact_map["invalid/malformed.m3u8"])
    require(malformed.get("status") in {"accepted", "rejected"}, "malformed HLS probe hung")
    if malformed.get("status") == "accepted":
        require(not codec_types(malformed), "malformed HLS exposed a playable stream")
    truncated_fast = load_probe(root, artifact_map["invalid/truncated-faststart.mp4"])
    require(truncated_fast.get("status") in {"accepted", "rejected"}, "truncated fast-start probe hung")

    if ffmpeg is not None:
        run_decode(ffmpeg, root, "av/faststart.mp4", True)
        run_decode(ffmpeg, root, "av/fragmented.mp4", True)
        run_decode(ffmpeg, root, "hls/vod/index.m3u8", True)
        run_decode(ffmpeg, root, "hls/vod-separate/master.m3u8", True)
        run_decode(ffmpeg, root, "hls/cmaf/index.m3u8", True)
        run_decode(ffmpeg, root, "invalid/truncated-faststart.mp4", False)
        for path in [*same_track_paths, *codec_members]:
            run_decode(ffmpeg, root, path, True)
        for sample_rate in (44100, 48000):
            for stem, (suffix, _codec) in audio_codecs.items():
                run_decode(ffmpeg, root, f"discovery/audio/{stem}-{sample_rate}.{suffix}", True)
        run_decode(ffmpeg, root, "discovery/audio/opus-48000.opus", True)
        run_decode(ffmpeg, root, "discovery/audio/aac-5.1.m4a", True)
        run_decode(ffmpeg, root, "discovery/audio/two-audio-tracks.mp4", True)
        for rung, specification in ladder_rungs.items():
            if rung not in ladder_reports:
                continue
            path = str(specification["path"])
            run_decode(ffmpeg, root, path, True)
            validate_audio_markers(
                ffmpeg,
                root,
                path,
                int(ladder_common["duration_seconds"]),
                int(ladder_common["audio_sample_rate"]),
            )
            validate_video_frames(
                ffmpeg,
                root,
                path,
                int(ladder_common["duration_seconds"]),
                int(ladder_common["frames_per_second"]),
            )
        if profile_name in reproduction.get("included_profiles", []):
            run_decode(ffmpeg, root, observed_path, True)
            run_decode(ffmpeg, root, midgop_path, True)
        validate_audio_markers(
            ffmpeg,
            root,
            "av/faststart.mp4",
            int(profile["primary_duration_seconds"]),
            int(profile["audio_sample_rate"]),
        )
        validate_video_frames(
            ffmpeg,
            root,
            "av/faststart.mp4",
            int(profile["primary_duration_seconds"]),
            int(profile["frames_per_second"]),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args()
    try:
        validate(args.root, args.profiles, args.ffmpeg, args.ffprobe)
    except ValidationError as error:
        print(f"fixture validation failed: {error}")
        return 1
    print(f"validated deterministic media fixtures: {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
