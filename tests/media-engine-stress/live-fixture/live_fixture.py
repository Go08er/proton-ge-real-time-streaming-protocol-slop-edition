#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Bounded loopback HLS live publisher for MediaEngine fault reproduction."""

from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, TextIO


LOOPBACK_HOST = "127.0.0.1"
SCHEMA = 1
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CHECKSUM_LINE_RE = re.compile(r"([0-9a-f]{64})  ([\x21-\x7e]+)\Z")
CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
WINDOW_NAME_RE = re.compile(r"window-(\d{4,10})\.m3u8\Z")
MEDIA_SEQUENCE_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)\Z")
URI_ATTRIBUTE_RE = re.compile(r'(?:^|,)URI="([^"]+)"(?:,|$)')

MAX_READY_BYTES = 16 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024 * 1024
MAX_CHECKSUM_ENTRIES = 50_000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 128 * 1024 * 1024
MAX_SCHEDULE_ENTRIES = 4096

FAULT_MODES = (
    "normal",
    "hold",
    "delayed-audio-window",
    "audio-404",
    "audio-503",
)

ROUTE_MUX_PLAYLIST = "mux-playlist"
ROUTE_MUX_SEGMENT = "mux-segment"
ROUTE_MASTER = "separate-master"
ROUTE_AUDIO_PLAYLIST = "audio-playlist"
ROUTE_AUDIO_SEGMENT = "audio-segment"
ROUTE_VIDEO_PLAYLIST = "video-playlist"
ROUTE_VIDEO_SEGMENT = "video-segment"
ROUTE_CONTROL_ADVANCE = "control-advance"
ROUTE_OTHER = "other"
ROUTES = frozenset(
    {
        ROUTE_MUX_PLAYLIST,
        ROUTE_MUX_SEGMENT,
        ROUTE_MASTER,
        ROUTE_AUDIO_PLAYLIST,
        ROUTE_AUDIO_SEGMENT,
        ROUTE_VIDEO_PLAYLIST,
        ROUTE_VIDEO_SEGMENT,
        ROUTE_CONTROL_ADVANCE,
        ROUTE_OTHER,
    }
)
EVIDENCE_KEYS = frozenset(
    {
        "bytes_sent",
        "case_id",
        "clock_basis",
        "fault_mode",
        "finished_monotonic_ns",
        "generation",
        "method",
        "request_seq",
        "route",
        "schema",
        "started_monotonic_ns",
        "status",
    }
)


class ContractError(ValueError):
    """Raised when immutable fixture or result data violates its contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def monotonic_raw_ns() -> int:
    """Return the absolute Linux clock used by the VM's Wine correlator."""

    require(hasattr(time, "CLOCK_MONOTONIC_RAW"), "CLOCK_MONOTONIC_RAW is unavailable")
    value = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    require(type(value) is int and value >= 0, "CLOCK_MONOTONIC_RAW returned an invalid value")
    return value


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    require(path.is_file(), f"missing {label}")
    require(not path.is_symlink(), f"{label} must not be a symlink")
    size = path.stat().st_size
    require(0 < size <= limit, f"{label} size is outside (0, {limit}]")
    return path.read_bytes()


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_bytes(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid {label}: {error}") from error


def exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    actual = set(value)
    require(actual == keys, f"{label} keys differ: {sorted(actual ^ keys)}")
    return value


def checked_relative(value: Any, label: str) -> str:
    require(type(value) is str and value != "", f"{label} must be a nonempty string")
    require("\\" not in value and "?" not in value and "#" not in value, f"unsafe {label}")
    path = PurePosixPath(value)
    require(not path.is_absolute(), f"absolute {label}")
    require(path.as_posix() == value, f"noncanonical {label}")
    require(all(part not in ("", ".", "..") for part in path.parts), f"traversing {label}")
    return value


class ChecksumIndex:
    """Validated SHA256SUMS index rooted at one generated fixture tree."""

    def __init__(self, root: Path, entries: dict[str, str]) -> None:
        self.root = root.resolve()
        self.entries = entries
        self._verified: dict[str, Path] = {}

    @classmethod
    def load(cls, root: Path) -> tuple["ChecksumIndex", dict[str, Any]]:
        resolved_root = root.resolve()
        require(resolved_root.is_dir(), "fixture root is not a directory")
        ready_path = resolved_root / "READY"
        ready = exact_object(
            load_json_bytes(read_bounded(ready_path, MAX_READY_BYTES, "READY"), "READY"),
            {"manifest_sha256", "profile", "schema", "sha256sums_sha256"},
            "READY",
        )
        require(type(ready["schema"]) is int and ready["schema"] == SCHEMA, "READY schema differs")
        require(ready["profile"] == "full", "live qualification requires the full fixture profile")
        for field in ("manifest_sha256", "sha256sums_sha256"):
            require(type(ready[field]) is str and SHA256_RE.fullmatch(ready[field]) is not None,
                    f"READY {field} is not a SHA-256")

        sums_path = resolved_root / "SHA256SUMS"
        sums_payload = read_bounded(sums_path, MAX_CHECKSUM_BYTES, "SHA256SUMS")
        require(sha256_bytes(sums_payload) == ready["sha256sums_sha256"],
                "SHA256SUMS does not match READY")
        try:
            text = sums_payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise ContractError("SHA256SUMS is not ASCII") from error
        require(text.endswith("\n"), "SHA256SUMS must end with one newline")

        entries: dict[str, str] = {}
        ordered_paths: list[str] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = CHECKSUM_LINE_RE.fullmatch(line)
            require(match is not None, f"malformed SHA256SUMS line {line_number}")
            digest, relative = match.groups()
            checked_relative(relative, f"SHA256SUMS path on line {line_number}")
            require(relative not in entries, f"duplicate SHA256SUMS path: {relative}")
            entries[relative] = digest
            ordered_paths.append(relative)
            require(len(entries) <= MAX_CHECKSUM_ENTRIES, "SHA256SUMS has too many entries")
        require(ordered_paths == sorted(ordered_paths), "SHA256SUMS paths are not sorted")
        require(entries, "SHA256SUMS is empty")

        index = cls(resolved_root, entries)
        manifest = index.verify("provenance/manifest.json", MAX_JSON_BYTES)
        require(sha256_file(manifest) == ready["manifest_sha256"],
                "provenance manifest does not match READY")
        return index, ready

    def verify(self, relative: str, max_bytes: int, expected: str | None = None) -> Path:
        relative = checked_relative(relative, "artifact path")
        require(relative in self.entries, f"artifact is absent from SHA256SUMS: {relative}")
        if relative in self._verified:
            if expected is not None:
                require(self.entries[relative] == expected,
                        f"schedule SHA-256 mismatch: {relative}")
            return self._verified[relative]
        path = self.root.joinpath(*PurePosixPath(relative).parts)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ContractError(f"artifact escaped fixture root: {relative}") from error
        require(path == resolved, f"artifact uses a symlink or noncanonical path: {relative}")
        require(path.is_file(), f"missing artifact: {relative}")
        size = path.stat().st_size
        require(0 < size <= max_bytes, f"artifact size is outside bounds: {relative}")
        actual = sha256_file(path)
        require(actual == self.entries[relative], f"SHA256SUMS mismatch: {relative}")
        if expected is not None:
            require(SHA256_RE.fullmatch(expected) is not None, f"invalid schedule SHA-256: {relative}")
            require(actual == expected, f"schedule SHA-256 mismatch: {relative}")
        self._verified[relative] = path
        return path


class Window:
    def __init__(self, sequence: int, playlist_path: Path, payload: bytes, segments: dict[str, Path]) -> None:
        self.sequence = sequence
        self.playlist_path = playlist_path
        self.payload = payload
        self.segments = segments


class Schedule:
    def __init__(
        self,
        relative: str,
        interval_seconds: float,
        window_segments: int,
        segment_count: int,
        windows: list[Window],
    ) -> None:
        self.relative = relative
        self.interval_seconds = interval_seconds
        self.window_segments = window_segments
        self.segment_count = segment_count
        self.windows = windows


def validate_schedule_header(data: Any, label: str) -> tuple[dict[str, Any], float, int, int, list[Any]]:
    document = exact_object(
        data,
        {"description", "entries", "interval_seconds", "schema", "segment_count", "window_segments"},
        label,
    )
    require(type(document["schema"]) is int and document["schema"] == SCHEMA,
            f"{label} schema differs")
    require(type(document["description"]) is str and 1 <= len(document["description"]) <= 512,
            f"{label} description is invalid")
    interval = document["interval_seconds"]
    require(type(interval) in (int, float) and math.isfinite(float(interval)) and 0 < interval <= 60,
            f"{label} interval is invalid")
    window_segments = document["window_segments"]
    segment_count = document["segment_count"]
    require(type(window_segments) is int and not isinstance(window_segments, bool) and 2 <= window_segments <= 256,
            f"{label} window size is invalid")
    require(type(segment_count) is int and not isinstance(segment_count, bool)
            and window_segments <= segment_count <= 100_000,
            f"{label} segment count is invalid")
    entries = document["entries"]
    require(type(entries) is list and 1 <= len(entries) <= MAX_SCHEDULE_ENTRIES,
            f"{label} entries are invalid")
    return document, float(interval), window_segments, segment_count, entries


def parse_window(
    index: ChecksumIndex,
    playlist_relative: str,
    expected_digest: str,
    expected_sequence: int,
) -> Window:
    path = index.verify(playlist_relative, MAX_PLAYLIST_BYTES, expected_digest)
    payload = path.read_bytes()
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ContractError(f"playlist is not UTF-8: {playlist_relative}") from error
    require(lines and lines[0] == "#EXTM3U", f"playlist lacks EXTM3U: {playlist_relative}")
    require("#EXT-X-ENDLIST" not in lines, f"live playlist is finite: {playlist_relative}")
    sequences = [int(match.group(1)) for line in lines if (match := MEDIA_SEQUENCE_RE.fullmatch(line))]
    require(sequences == [expected_sequence], f"playlist media sequence differs: {playlist_relative}")

    playlist_root = PurePosixPath(playlist_relative).parent
    segments: dict[str, Path] = {}
    for line in lines:
        require("\x00" not in line and "\r" not in line, f"unsafe playlist line: {playlist_relative}")
        if not line or line.startswith("#"):
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-MAP:", "#EXT-X-MEDIA:")):
                raise ContractError(f"unexpected child URI tag in generated window: {playlist_relative}")
            continue
        uri = checked_relative(line, f"segment URI in {playlist_relative}")
        uri_path = PurePosixPath(uri)
        require(len(uri_path.parts) == 2 and uri_path.parts[0] == "segments",
                f"segment URI is outside its generated segment directory: {playlist_relative}")
        artifact_relative = (playlist_root / uri_path).as_posix()
        segments[uri] = index.verify(artifact_relative, MAX_SEGMENT_BYTES)
    require(segments, f"playlist has no segments: {playlist_relative}")
    return Window(expected_sequence, path, payload, segments)


def load_track_schedule(index: ChecksumIndex, schedule_relative: str) -> Schedule:
    schedule_path = index.verify(schedule_relative, MAX_JSON_BYTES)
    data = load_json_bytes(schedule_path.read_bytes(), schedule_relative)
    _, interval, window_segments, segment_count, entries = validate_schedule_header(data, schedule_relative)
    root = PurePosixPath(schedule_relative).parent
    windows: list[Window] = []
    prior_sequence: int | None = None
    for position, entry_value in enumerate(entries):
        entry = exact_object(entry_value, {"media_sequence", "playlist", "sha256"},
                             f"{schedule_relative} entry {position}")
        sequence = entry["media_sequence"]
        require(type(sequence) is int and not isinstance(sequence, bool) and sequence >= 0,
                f"invalid media sequence in {schedule_relative}")
        if prior_sequence is not None:
            require(sequence == prior_sequence + 1, f"noncontiguous media sequence in {schedule_relative}")
        prior_sequence = sequence
        playlist = checked_relative(entry["playlist"], f"playlist in {schedule_relative}")
        match = WINDOW_NAME_RE.fullmatch(playlist)
        require(match is not None and int(match.group(1)) == sequence,
                f"playlist name does not bind its sequence in {schedule_relative}")
        digest = entry["sha256"]
        require(type(digest) is str and SHA256_RE.fullmatch(digest) is not None,
                f"invalid playlist digest in {schedule_relative}")
        windows.append(parse_window(index, (root / playlist).as_posix(), digest, sequence))
    return Schedule(schedule_relative, interval, window_segments, segment_count, windows)


class FixtureBundle:
    """Fully verified live HLS schedules and immutable media paths."""

    def __init__(
        self,
        root: Path,
        ready: dict[str, Any],
        mux: Schedule,
        audio: Schedule,
        video: Schedule,
        master_payload: bytes,
    ) -> None:
        self.root = root
        self.ready = ready
        self.mux = mux
        self.audio = audio
        self.video = video
        self.master_payload = master_payload
        self.publication_count = len(audio.windows)
        self.mux_segments = self._route_segments(mux.windows[:self.publication_count])
        self.audio_segments = self._route_segments(audio.windows)
        self.video_segments = self._route_segments(video.windows)

    @staticmethod
    def _route_segments(windows: list[Window]) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for window in windows:
            for uri, path in window.segments.items():
                existing = result.get(uri)
                require(existing is None or existing == path, f"segment URI maps to multiple artifacts: {uri}")
                result[uri] = path
        return result

    @classmethod
    def load(cls, root: Path) -> "FixtureBundle":
        index, ready = ChecksumIndex.load(root)
        mux = load_track_schedule(index, "hls/live/schedule.json")
        audio = load_track_schedule(index, "hls/live-separate/audio/schedule.json")
        video = load_track_schedule(index, "hls/live-separate/video/schedule.json")
        require(
            (audio.interval_seconds, audio.window_segments, audio.segment_count)
            == (video.interval_seconds, video.window_segments, video.segment_count),
            "separate audio/video schedules differ",
        )
        require(
            (mux.interval_seconds, mux.window_segments)
            == (audio.interval_seconds, audio.window_segments),
            "mux and separate publication cadence differs",
        )
        mux_sequences = [window.sequence for window in mux.windows]
        audio_sequences = [window.sequence for window in audio.windows]
        video_sequences = [window.sequence for window in video.windows]
        require(audio_sequences == video_sequences,
                "separate audio/video schedule sequences differ")
        require(len(mux_sequences) >= len(audio_sequences)
                and mux_sequences[:len(audio_sequences)] == audio_sequences,
                "mux schedule does not contain the paired A/V sequence prefix")

        paired_relative = "hls/live-separate/schedule.json"
        paired_path = index.verify(paired_relative, MAX_JSON_BYTES)
        paired = load_json_bytes(paired_path.read_bytes(), paired_relative)
        _, pair_interval, pair_window, pair_count, pair_entries = validate_schedule_header(
            paired, paired_relative
        )
        require((pair_interval, pair_window, pair_count)
                == (audio.interval_seconds, audio.window_segments, audio.segment_count),
                "paired schedule header differs from track schedules")
        require(len(pair_entries) == len(audio.windows), "paired schedule entry count differs")
        for position, pair_value in enumerate(pair_entries):
            pair = exact_object(pair_value, {"audio", "media_sequence", "video"},
                                f"paired entry {position}")
            sequence = audio.windows[position].sequence
            require(type(pair["media_sequence"]) is int and pair["media_sequence"] == sequence,
                    "paired media sequence differs")
            for track_name, schedule in (("audio", audio), ("video", video)):
                side = exact_object(pair[track_name], {"playlist", "sha256"},
                                    f"paired {track_name} entry {position}")
                expected_playlist = f"{track_name}/{schedule.windows[position].playlist_path.name}"
                require(side["playlist"] == expected_playlist,
                        f"paired {track_name} playlist differs")
                require(side["sha256"] == sha256_file(schedule.windows[position].playlist_path),
                        f"paired {track_name} digest differs")

        master_path = index.verify("hls/live-separate/master.m3u8", MAX_PLAYLIST_BYTES)
        master_payload = master_path.read_bytes()
        try:
            master_lines = master_payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ContractError("separate master is not UTF-8") from error
        require(master_lines and master_lines[0] == "#EXTM3U", "separate master lacks EXTM3U")
        child_uris: list[str] = []
        for line in master_lines:
            if line.startswith("#EXT-X-MEDIA:"):
                match = URI_ATTRIBUTE_RE.search(line)
                require(match is not None, "separate master audio entry lacks URI")
                child_uris.append(checked_relative(match.group(1), "master child URI"))
            elif line and not line.startswith("#"):
                child_uris.append(checked_relative(line, "master child URI"))
        require(child_uris == ["audio/index.m3u8", "video/index.m3u8"],
                "separate master topology differs")
        return cls(index.root, ready, mux, audio, video, master_payload)


class PublisherSnapshot:
    def __init__(self, generation: int, mux: Window, audio: Window, video: Window) -> None:
        self.generation = generation
        self.mux = mux
        self.audio = audio
        self.video = video


class LivePublisher:
    """One locked generation controls muxed and paired A/V publication."""

    def __init__(self, bundle: FixtureBundle, mode: str, initial_generation: int = 0) -> None:
        require(mode in FAULT_MODES, "unknown fault mode")
        require(type(initial_generation) is int and not isinstance(initial_generation, bool),
                "initial generation must be an integer")
        require(0 <= initial_generation < bundle.publication_count,
                "initial generation is out of the common publication range")
        self.bundle = bundle
        self.mode = mode
        self._generation = initial_generation
        self._lock = threading.Lock()

    @property
    def interval_seconds(self) -> float:
        return self.bundle.mux.interval_seconds

    def snapshot(self) -> PublisherSnapshot:
        with self._lock:
            generation = self._generation
            return PublisherSnapshot(
                generation,
                self.bundle.mux.windows[generation],
                self.bundle.audio.windows[generation],
                self.bundle.video.windows[generation],
            )

    def advance(self) -> tuple[int, bool]:
        with self._lock:
            if self.mode == "hold" or self._generation + 1 >= self.bundle.publication_count:
                return self._generation, False
            self._generation += 1
            return self._generation, True


class AudioFaultGate:
    """Deterministic child-track fault admission independent of wall time."""

    def __init__(
        self,
        mode: str,
        start_generation: int | None,
        success_budget: int | None,
        delay_seconds: float,
    ) -> None:
        require(mode in FAULT_MODES, "unknown audio fault mode")
        require(start_generation is None or (type(start_generation) is int
                and not isinstance(start_generation, bool) and start_generation >= 0),
                "audio fault generation is invalid")
        require(success_budget is None or (type(success_budget) is int
                and not isinstance(success_budget, bool) and 0 <= success_budget <= 10_000),
                "audio success budget is invalid")
        require(not (start_generation is not None and success_budget is not None),
                "generation and segment-budget audio fault triggers are mutually exclusive")
        require(math.isfinite(delay_seconds) and 0 <= delay_seconds <= 10,
                "audio delay is outside [0, 10]")
        if mode in ("normal", "hold"):
            require(start_generation is None and success_budget is None,
                    f"{mode} does not accept an audio fault trigger")
        elif mode == "delayed-audio-window":
            require(start_generation is not None and success_budget is None and delay_seconds > 0,
                    "delayed-audio-window requires only a generation trigger and positive delay")
        else:
            require((start_generation is None) != (success_budget is None),
                    f"{mode} requires exactly one audio fault trigger")
        self.mode = mode
        self.start_generation = start_generation
        self.success_budget = success_budget
        self.delay_seconds = delay_seconds
        self._successful_audio_segment_gets = 0
        self._lock = threading.Lock()

    def plan(self, route: str, generation: int, method: str) -> tuple[int | None, float]:
        if route not in (ROUTE_AUDIO_PLAYLIST, ROUTE_AUDIO_SEGMENT):
            return None, 0.0
        with self._lock:
            if self.mode == "delayed-audio-window":
                start_generation = self.start_generation
                require(start_generation is not None,
                        "delayed audio trigger invariant was lost")
                if route == ROUTE_AUDIO_PLAYLIST and generation >= start_generation:
                    return None, self.delay_seconds
                return None, 0.0

            if self.mode in ("audio-404", "audio-503"):
                fault_status = 404 if self.mode == "audio-404" else 503

                # Generation-triggered faults model an unavailable audio
                # child at and after one publication window. They affect both
                # that child playlist and its segments.
                if self.start_generation is not None:
                    if generation >= self.start_generation:
                        return fault_status, 0.0
                    return None, 0.0

                # Budget-triggered faults keep the playlist and HEAD probes
                # available. Only valid audio-segment GETs reach this method
                # from the handler, and this locked reservation guarantees
                # exactly N successful GET responses under concurrency.
                if route == ROUTE_AUDIO_SEGMENT and method == "GET":
                    success_budget = self.success_budget
                    require(success_budget is not None,
                            "audio success-budget invariant was lost")
                    if self._successful_audio_segment_gets >= success_budget:
                        return fault_status, 0.0
                    self._successful_audio_segment_gets += 1
            return None, 0.0


def validate_evidence_record(value: Any, expected_case_id: str | None = None) -> dict[str, Any]:
    """Validate one strict, URL-free request-evidence record."""

    record = exact_object(value, set(EVIDENCE_KEYS), "request evidence")
    require(type(record["schema"]) is int and record["schema"] == SCHEMA,
            "request evidence schema differs")
    require(record["clock_basis"] == CLOCK_BASIS,
            "request evidence clock basis differs")
    require(type(record["case_id"]) is str
            and CASE_ID_RE.fullmatch(record["case_id"]) is not None,
            "request evidence case ID is invalid")
    if expected_case_id is not None:
        require(record["case_id"] == expected_case_id,
                "request evidence case ID differs")
    require(type(record["fault_mode"]) is str and record["fault_mode"] in FAULT_MODES,
            "request evidence fault mode is invalid")
    require(type(record["route"]) is str and record["route"] in ROUTES,
            "request evidence route is invalid")
    require(type(record["method"]) is str
            and record["method"] in ("GET", "HEAD", "POST", "OTHER"),
            "request evidence method is invalid")
    require(type(record["request_seq"]) is int
            and not isinstance(record["request_seq"], bool)
            and record["request_seq"] > 0,
            "request evidence sequence is invalid")
    require(type(record["generation"]) is int
            and not isinstance(record["generation"], bool)
            and record["generation"] >= 0,
            "request evidence generation is invalid")
    require(type(record["status"]) is int
            and not isinstance(record["status"], bool)
            and 100 <= record["status"] <= 599,
            "request evidence status is invalid")
    require(type(record["bytes_sent"]) is int
            and not isinstance(record["bytes_sent"], bool)
            and 0 <= record["bytes_sent"] <= MAX_SEGMENT_BYTES,
            "request evidence byte count is invalid")
    for key in ("started_monotonic_ns", "finished_monotonic_ns"):
        require(type(record[key]) is int and not isinstance(record[key], bool)
                and record[key] >= 0,
                f"request evidence {key} is invalid")
    require(record["finished_monotonic_ns"] >= record["started_monotonic_ns"],
            "request evidence clock regressed")
    return record


class SafeRequestLogger:
    """Bounded JSONL logger that never records target text, headers, or peers."""

    def __init__(
        self,
        stream: TextIO,
        case_id: str,
        monotonic_ns: Callable[[], int] = monotonic_raw_ns,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        require(CASE_ID_RE.fullmatch(case_id) is not None, "case ID is not safe ASCII")
        require(type(max_bytes) is int and not isinstance(max_bytes, bool) and 1 <= max_bytes <= 64 * 1024 * 1024,
                "log budget is invalid")
        try:
            existing = stream.tell()
        except (AttributeError, OSError):
            existing = 0
        require(0 <= existing <= max_bytes, "existing log exceeds its byte budget")
        self._stream = stream
        self._case_id = case_id
        self._clock = monotonic_ns
        sample = monotonic_ns()
        require(type(sample) is int and not isinstance(sample, bool) and sample >= 0,
                "monotonic evidence clock returned an invalid value")
        self._max_bytes = max_bytes
        self._written = existing
        self._lock = threading.Lock()

    def now(self) -> int:
        value = self._clock()
        require(type(value) is int and not isinstance(value, bool) and value >= 0,
                "monotonic evidence clock returned an invalid value")
        return value

    def record(
        self,
        *,
        request_seq: int,
        method: str,
        route: str,
        status: int,
        generation: int,
        fault_mode: str,
        bytes_sent: int,
        started_monotonic_ns: int,
        finished_monotonic_ns: int,
    ) -> None:
        record = {
            "bytes_sent": bytes_sent,
            "case_id": self._case_id,
            "clock_basis": CLOCK_BASIS,
            "fault_mode": fault_mode,
            "finished_monotonic_ns": finished_monotonic_ns,
            "generation": generation,
            "method": method if method in ("GET", "HEAD", "POST") else "OTHER",
            "request_seq": request_seq,
            "route": route,
            "schema": SCHEMA,
            "started_monotonic_ns": started_monotonic_ns,
            "status": status,
        }
        validate_evidence_record(record, self._case_id)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._written + len(encoded) > self._max_bytes:
                raise RuntimeError("sanitized request log byte budget exhausted")
            self._stream.write(encoded.decode("utf-8"))
            self._stream.flush()
            self._written += len(encoded)


class LiveFixtureState:
    def __init__(
        self,
        bundle: FixtureBundle,
        publisher: LivePublisher,
        audio_fault: AudioFaultGate,
        logger: SafeRequestLogger,
        control_token: str,
        max_requests: int,
        max_concurrent: int,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        require(TOKEN_RE.fullmatch(control_token) is not None, "control token is not safe ASCII")
        require(type(max_requests) is int and not isinstance(max_requests, bool)
                and 1 <= max_requests <= 1_000_000, "max requests is invalid")
        require(type(max_concurrent) is int and not isinstance(max_concurrent, bool)
                and 1 <= max_concurrent <= 64, "max concurrency is invalid")
        self.bundle = bundle
        self.publisher = publisher
        self.audio_fault = audio_fault
        self.logger = logger
        self.control_token = control_token
        self.max_requests = max_requests
        self.max_concurrent = max_concurrent
        self.sleeper = sleeper


class LiveFixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address: tuple[str, int], state: LiveFixtureState) -> None:
        host, port = address
        require(host == LOOPBACK_HOST, "live fixture must bind literal IPv4 loopback")
        require(type(port) is int and not isinstance(port, bool) and 0 <= port <= 65535,
                "port is invalid")
        self.state = state
        self._request_lock = threading.Lock()
        self._next_request = 1
        self._terminal_records = 0
        self._rejected_connections = 0
        self._slots = threading.BoundedSemaphore(state.max_concurrent)
        self.fatal_error: str | None = None
        super().__init__(address, LiveRequestHandler)

    def allocate_request(self) -> tuple[int, bool] | None:
        with self._request_lock:
            if self._next_request > self.state.max_requests:
                return None
            sequence = self._next_request
            self._next_request += 1
            return sequence, sequence == self.state.max_requests

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            with self._request_lock:
                self._rejected_connections += 1
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def fail_closed(self, message: str) -> None:
        with self._request_lock:
            if self.fatal_error is None:
                self.fatal_error = message
        threading.Thread(target=self.shutdown, name="live-fixture-fail-closed", daemon=True).start()

    def note_terminal_record(self) -> None:
        with self._request_lock:
            self._terminal_records += 1

    def completion_counts(self) -> tuple[int, int, int]:
        """Return allocated, terminally logged, and rejected request counts."""

        with self._request_lock:
            return (
                self._next_request - 1,
                self._terminal_records,
                self._rejected_connections,
            )


def request_route(target: str) -> tuple[str, str]:
    path = target.partition("?")[0].partition("#")[0]
    fixed = {
        "/mux/index.m3u8": ROUTE_MUX_PLAYLIST,
        "/separate/master.m3u8": ROUTE_MASTER,
        "/separate/audio/index.m3u8": ROUTE_AUDIO_PLAYLIST,
        "/separate/video/index.m3u8": ROUTE_VIDEO_PLAYLIST,
        "/__control__/advance": ROUTE_CONTROL_ADVANCE,
    }
    if path in fixed:
        return fixed[path], path
    for prefix, route in (
        ("/mux/segments/", ROUTE_MUX_SEGMENT),
        ("/separate/audio/segments/", ROUTE_AUDIO_SEGMENT),
        ("/separate/video/segments/", ROUTE_VIDEO_SEGMENT),
    ):
        if path.startswith(prefix):
            return route, path
    return ROUTE_OTHER, path


class LiveRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MediaEngineLiveFixture/1"
    sys_version = ""

    @property
    def fixture_server(self) -> LiveFixtureServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("OTHER")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("OTHER")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OTHER")

    def _send(self, status: int, payload: bytes, content_type: str, generation: int, method: str) -> int:
        self.send_response_only(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", content_type)
        self.send_header("X-Fixture-Generation", str(generation))
        self.send_header("Cache-Control", "no-store" if content_type != "video/mp2t" else "public, max-age=31536000, immutable")
        self.end_headers()
        if method == "HEAD" or not payload:
            return 0
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return len(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return 0

    def _media_payload(self, route: str, path: str, snapshot: PublisherSnapshot) -> tuple[int, bytes, str]:
        bundle = self.fixture_server.state.bundle
        if route == ROUTE_MUX_PLAYLIST:
            return 200, snapshot.mux.payload, "application/vnd.apple.mpegurl"
        if route == ROUTE_MASTER:
            return 200, bundle.master_payload, "application/vnd.apple.mpegurl"
        if route == ROUTE_AUDIO_PLAYLIST:
            return 200, snapshot.audio.payload, "application/vnd.apple.mpegurl"
        if route == ROUTE_VIDEO_PLAYLIST:
            return 200, snapshot.video.payload, "application/vnd.apple.mpegurl"

        mappings: tuple[str, dict[str, Path]] | None = None
        if route == ROUTE_MUX_SEGMENT:
            mappings = ("/mux/", bundle.mux_segments)
        elif route == ROUTE_AUDIO_SEGMENT:
            mappings = ("/separate/audio/", bundle.audio_segments)
        elif route == ROUTE_VIDEO_SEGMENT:
            mappings = ("/separate/video/", bundle.video_segments)
        if mappings is not None:
            prefix, segment_map = mappings
            relative = path.removeprefix(prefix)
            artifact = segment_map.get(relative)
            if artifact is not None:
                return 200, artifact.read_bytes(), "video/mp2t"
        return 404, b"", "application/octet-stream"

    def _handle(self, method: str) -> None:
        server = self.fixture_server
        allocation = server.allocate_request()
        if allocation is None:
            self.close_connection = True
            self._send(503, b"", "application/octet-stream", 0, method)
            threading.Thread(target=server.shutdown, name="live-fixture-request-limit", daemon=True).start()
            return
        request_seq, final_request = allocation
        started = server.state.logger.now()
        snapshot = server.state.publisher.snapshot()
        route, path = request_route(self.path)
        status = 500
        bytes_sent = 0
        try:
            if method == "POST" and route == ROUTE_CONTROL_ADVANCE:
                supplied = self.headers.get("X-Live-Fixture-Token", "")
                length = self.headers.get("Content-Length", "0")
                if not hmac.compare_digest(supplied, server.state.control_token):
                    status = 404
                    bytes_sent = self._send(status, b"", "application/octet-stream", snapshot.generation, method)
                elif length != "0":
                    status = 413
                    bytes_sent = self._send(status, b"", "application/octet-stream", snapshot.generation, method)
                else:
                    generation, changed = server.state.publisher.advance()
                    payload = (json.dumps(
                        {"changed": changed, "generation": generation, "schema": SCHEMA},
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n").encode("utf-8")
                    snapshot = server.state.publisher.snapshot()
                    status = 200
                    bytes_sent = self._send(status, payload, "application/json", generation, method)
            elif method == "POST" or method == "OTHER":
                status = 405
                bytes_sent = self._send(status, b"", "application/octet-stream", snapshot.generation, method)
            else:
                status, payload, content_type = self._media_payload(route, path, snapshot)
                if status == 200:
                    fault_status, delay = server.state.audio_fault.plan(
                        route, snapshot.generation, method
                    )
                    if delay:
                        server.state.sleeper(delay)
                    if fault_status is not None:
                        status = fault_status
                        payload = b""
                        content_type = "application/octet-stream"
                bytes_sent = self._send(status, payload, content_type, snapshot.generation, method)
        except (ContractError, OSError, RuntimeError):
            status = 500
            try:
                bytes_sent = self._send(status, b"", "application/octet-stream", snapshot.generation, method)
            except OSError:
                bytes_sent = 0
            server.fail_closed("request handling failed")
        finally:
            finished = server.state.logger.now()
            try:
                server.state.logger.record(
                    request_seq=request_seq,
                    method=method,
                    route=route,
                    status=status,
                    generation=snapshot.generation,
                    fault_mode=server.state.publisher.mode,
                    bytes_sent=bytes_sent,
                    started_monotonic_ns=started,
                    finished_monotonic_ns=finished,
                )
                server.note_terminal_record()
            except (ContractError, OSError, RuntimeError):
                server.fail_closed("sanitized request logging failed")
            if final_request:
                threading.Thread(target=server.shutdown, name="live-fixture-final-request", daemon=True).start()


class AutoAdvancer:
    """Advance at absolute monotonic deadlines without accumulating drift."""

    def __init__(self, publisher: LivePublisher) -> None:
        self.publisher = publisher
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-fixture-advancer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.publisher.interval_seconds + 0.5))

    def _run(self) -> None:
        interval = self.publisher.interval_seconds
        deadline = time.monotonic() + interval
        while not self._stop.wait(max(0.0, deadline - time.monotonic())):
            self.publisher.advance()
            deadline += interval


def bounded_int(text: str, lower: int, upper: int) -> int:
    try:
        value = int(text, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if not lower <= value <= upper:
        raise argparse.ArgumentTypeError(f"value must be within [{lower}, {upper}]")
    return value


def bounded_float(text: str, lower: float, upper: float) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a number") from error
    if not math.isfinite(value) or not lower <= value <= upper:
        raise argparse.ArgumentTypeError(f"value must be finite and within [{lower}, {upper}]")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--host", default=LOOPBACK_HOST, choices=[LOOPBACK_HOST])
    parser.add_argument("--port", type=lambda value: bounded_int(value, 0, 65535), default=0)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--fault-mode", choices=FAULT_MODES, default="normal")
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument("--audio-fault-start-generation", type=lambda value: bounded_int(value, 0, 4095))
    trigger.add_argument("--audio-success-budget", type=lambda value: bounded_int(value, 0, 10_000))
    parser.add_argument("--audio-delay-seconds", type=lambda value: bounded_float(value, 0, 10), default=1.0)
    parser.add_argument("--initial-generation", type=lambda value: bounded_int(value, 0, 4095), default=0)
    parser.add_argument("--auto-advance", action="store_true")
    parser.add_argument("--max-requests", type=lambda value: bounded_int(value, 1, 1_000_000), default=4096)
    parser.add_argument("--max-concurrent", type=lambda value: bounded_int(value, 1, 64), default=8)
    parser.add_argument("--max-log-bytes", type=lambda value: bounded_int(value, 1, 64 * 1024 * 1024), default=8 * 1024 * 1024)
    parser.add_argument("--log", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    bundle = FixtureBundle.load(args.fixture_root)
    require(CASE_ID_RE.fullmatch(args.case_id) is not None, "case ID is invalid")
    require(TOKEN_RE.fullmatch(args.control_token) is not None, "control token is invalid")
    require(not args.log.exists(), "refusing to append to an existing evidence log")
    require(args.log.parent.is_dir(), "log parent does not exist")
    require(not args.log.parent.is_symlink(), "log parent must not be a symlink")

    with args.log.open("x", encoding="utf-8", buffering=1) as log_stream:
        os.chmod(args.log, 0o600)
        logger = SafeRequestLogger(log_stream, args.case_id, max_bytes=args.max_log_bytes)
        publisher = LivePublisher(bundle, args.fault_mode, args.initial_generation)
        fault_gate = AudioFaultGate(
            args.fault_mode,
            args.audio_fault_start_generation,
            args.audio_success_budget,
            args.audio_delay_seconds,
        )
        state = LiveFixtureState(
            bundle,
            publisher,
            fault_gate,
            logger,
            args.control_token,
            args.max_requests,
            args.max_concurrent,
        )
        server = LiveFixtureServer((args.host, args.port), state)
        port = server.server_address[1]
        advancer = AutoAdvancer(publisher) if args.auto_advance else None

        stopping = threading.Event()

        def stop_server(_signum: int, _frame: Any) -> None:
            if not stopping.is_set():
                stopping.set()
                threading.Thread(target=server.shutdown, name="live-fixture-signal", daemon=True).start()

        prior_handlers: dict[int, Any] = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[signum] = signal.signal(signum, stop_server)
        try:
            readiness = {
                "control": f"http://{args.host}:{port}/__control__/advance",
                "fault_mode": args.fault_mode,
                "mux": f"http://{args.host}:{port}/mux/index.m3u8",
                "profile": bundle.ready["profile"],
                "schema": SCHEMA,
                "separate": f"http://{args.host}:{port}/separate/master.m3u8",
            }
            print(json.dumps(readiness, sort_keys=True, separators=(",", ":")), flush=True)
            if advancer is not None:
                advancer.start()
            server.serve_forever(poll_interval=0.1)
        finally:
            if advancer is not None:
                advancer.stop()
            server.server_close()
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)
        if server.fatal_error is not None:
            raise ContractError(server.fatal_error)
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (ContractError, OSError) as error:
        print(f"live fixture: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
