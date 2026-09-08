#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Generate normalized ffprobe evidence and a hash manifest for fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


PROBE_SUFFIXES = {
    ".aac",
    ".asf",
    ".avi",
    ".bin",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".ts",
    ".wav",
    ".webm",
}
STREAM_FIELDS = (
    "index",
    "codec_name",
    "codec_long_name",
    "profile",
    "codec_type",
    "codec_tag_string",
    "width",
    "height",
    "pix_fmt",
    "sample_fmt",
    "sample_rate",
    "channels",
    "channel_layout",
    "time_base",
    "start_pts",
    "start_time",
    "duration_ts",
    "duration",
    "bit_rate",
    "nb_frames",
    "r_frame_rate",
    "avg_frame_rate",
)
FORMAT_FIELDS = (
    "format_name",
    "format_long_name",
    "start_time",
    "duration",
    "size",
    "bit_rate",
    "probe_score",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
        }
    )
    return environment


def first_float(values: list[str]) -> float | None:
    parsed: list[float] = []
    for value in values:
        if value in {"", "N/A"}:
            continue
        try:
            parsed.append(float(value))
        except ValueError:
            continue
    return min(parsed) if parsed else None


def last_float(values: list[str]) -> float | None:
    parsed: list[float] = []
    for value in values:
        if value in {"", "N/A"}:
            continue
        try:
            parsed.append(float(value))
        except ValueError:
            continue
    return max(parsed) if parsed else None


def normalize_probe(document: dict[str, Any], command: list[str]) -> dict[str, Any]:
    streams = []
    stream_types: dict[int, str] = {}
    for stream in document.get("streams", []):
        normalized = {field: stream[field] for field in STREAM_FIELDS if field in stream}
        disposition = stream.get("disposition")
        if isinstance(disposition, dict):
            normalized["disposition"] = {
                key: disposition[key] for key in sorted(disposition) if disposition[key]
            }
        tags = stream.get("tags")
        if isinstance(tags, dict):
            normalized["tags"] = {
                key: tags[key]
                for key in ("language", "title")
                if key in tags
            }
        streams.append(normalized)
        if "index" in stream and "codec_type" in stream:
            stream_types[int(stream["index"])] = str(stream["codec_type"])

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for packet in document.get("packets", []):
        index = int(packet.get("stream_index", -1))
        media_type = stream_types.get(index, f"stream-{index}")
        grouped[media_type].append(
            {
                key: packet[key]
                for key in (
                    "pts_time",
                    "dts_time",
                    "duration_time",
                    "size",
                    "flags",
                    "data_hash",
                )
                if key in packet
            }
        )

    packet_summary: dict[str, dict[str, Any]] = {}
    for media_type, packets in sorted(grouped.items()):
        payload_identity = [
            {key: packet[key] for key in ("size", "data_hash") if key in packet}
            for packet in packets
        ]
        packet_summary[media_type] = {
            "count": len(packets),
            "first_pts_time": first_float(
                [str(packet.get("pts_time", "")) for packet in packets]
            ),
            "last_pts_time": last_float(
                [str(packet.get("pts_time", "")) for packet in packets]
            ),
            "payload_sha256": canonical_hash(payload_identity),
            "timeline_sha256": canonical_hash(packets),
        }

    format_document = document.get("format", {})
    normalized_format = {
        field: format_document[field]
        for field in FORMAT_FIELDS
        if field in format_document
    }
    return {
        "schema": 1,
        "status": "accepted",
        "command": command,
        "format": normalized_format,
        "streams": streams,
        "packets": packet_summary,
    }


def probe_file(ffprobe: str, root: Path, relative: Path) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_packets",
        "-show_data_hash",
        "sha256",
        "-of",
        "json",
        relative.as_posix(),
    ]
    display_command = ["ffprobe", *command[1:]]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=command_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr or b""
        return {
            "schema": 1,
            "status": "timeout",
            "command": display_command,
            "stderr_sha256": sha256_bytes(stderr),
        }

    if result.returncode != 0:
        return {
            "schema": 1,
            "status": "rejected",
            "command": display_command,
            "returncode": result.returncode,
            "stderr_sha256": sha256_bytes(result.stderr),
        }
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ffprobe returned invalid JSON for {relative}: {error}") from error
    return normalize_probe(document, display_command)


def should_probe(relative: Path) -> bool:
    if relative.suffix.lower() in PROBE_SUFFIXES:
        return True
    return relative.parts[0] == "invalid" and relative.suffix.lower() == ".m3u8"


def relative_files(root: Path) -> list[Path]:
    return sorted(
        (path.relative_to(root) for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix().encode("utf-8"),
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--nixpkgs-pin", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--nixpkgs-path", required=True)
    parser.add_argument("--nixpkgs-revision", required=True)
    parser.add_argument("--nixpkgs-nar-hash", required=True)
    parser.add_argument("--nixpkgs-version", required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    profiles_document = json.loads(args.profiles.read_text(encoding="utf-8"))
    try:
        selected_profile = profiles_document["profiles"][args.profile]
    except KeyError as error:
        raise SystemExit(f"unknown fixture profile: {args.profile}") from error

    profile_copy = root / "provenance" / "profiles.json"
    profile_copy.write_bytes(args.profiles.read_bytes())
    pin_copy = root / "provenance" / "nixpkgs-pin.json"
    pin_copy.write_bytes(args.nixpkgs_pin.read_bytes())
    write_json(
        root / "provenance" / "selected-profile.json",
        {"name": args.profile, "parameters": selected_profile},
    )

    ffmpeg_version = subprocess.run(
        [args.ffmpeg, "-version"],
        env=command_environment(),
        check=True,
        capture_output=True,
        timeout=15,
    ).stdout
    ffprobe_version = subprocess.run(
        [args.ffprobe, "-version"],
        env=command_environment(),
        check=True,
        capture_output=True,
        timeout=15,
    ).stdout
    (root / "provenance" / "ffmpeg-version.txt").write_bytes(ffmpeg_version)
    (root / "provenance" / "ffprobe-version.txt").write_bytes(ffprobe_version)

    originals = relative_files(root)
    probe_records: dict[str, dict[str, str]] = {}
    for relative in originals:
        if not should_probe(relative):
            continue
        report = probe_file(args.ffprobe, root, relative)
        report_path = Path("provenance") / "ffprobe" / relative.parent / (
            relative.name + ".json"
        )
        write_json(root / report_path, report)
        probe_records[relative.as_posix()] = {
            "path": report_path.as_posix(),
            "sha256": sha256_file(root / report_path),
            "status": str(report["status"]),
        }

    excluded = {
        Path("provenance/manifest.json"),
        Path("SHA256SUMS"),
        Path("READY"),
    }
    artifacts = []
    for relative in relative_files(root):
        if relative in excluded:
            continue
        path = root / relative
        record: dict[str, Any] = {
            "path": relative.as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if relative.as_posix() in probe_records:
            record["ffprobe"] = probe_records[relative.as_posix()]
        artifacts.append(record)

    command_transcript = root / "provenance" / "ffmpeg-commands.sh"
    manifest = {
        "schema": 1,
        "profile": {"name": args.profile, "parameters": selected_profile},
        "provenance": {
            "nixpkgs": {
                "path": args.nixpkgs_path,
                "revision": args.nixpkgs_revision,
                "nar_hash": args.nixpkgs_nar_hash,
                "version": args.nixpkgs_version,
            },
            "ffmpeg": {
                "store_path": str(Path(args.ffmpeg).resolve().parents[1]),
                "binary": args.ffmpeg,
                "version_sha256": sha256_file(
                    root / "provenance" / "ffmpeg-version.txt"
                ),
            },
            "ffprobe": {
                "store_path": str(Path(args.ffprobe).resolve().parents[1]),
                "binary": args.ffprobe,
                "version_sha256": sha256_file(
                    root / "provenance" / "ffprobe-version.txt"
                ),
            },
            "profiles_sha256": sha256_file(profile_copy),
            "nixpkgs_pin_sha256": sha256_file(pin_copy),
            "command_transcript_sha256": sha256_file(command_transcript),
        },
        "artifacts": artifacts,
    }
    manifest_path = root / "provenance" / "manifest.json"
    write_json(manifest_path, manifest)

    checksum_paths = [
        path
        for path in relative_files(root)
        if path not in {Path("SHA256SUMS"), Path("READY")}
    ]
    checksums = "".join(
        f"{sha256_file(root / relative)}  {relative.as_posix()}\n"
        for relative in checksum_paths
    )
    checksum_path = root / "SHA256SUMS"
    checksum_path.write_text(checksums, encoding="utf-8")
    ready = {
        "schema": 1,
        "profile": args.profile,
        "manifest_sha256": sha256_file(manifest_path),
        "sha256sums_sha256": sha256_file(checksum_path),
    }
    write_json(root / "READY", ready)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
