#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Score dense PCM delivery across declared run-private endpoint windows."""

from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


SCHEMA = 2
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
SOURCE = "rtsp_host_null.monitor"
SAMPLE_FORMAT = "s16le"
SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2
MAX_DRIVER_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_PCM_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_NS = 360 * 1_000_000_000
MAX_CAPTURE_STARTUP_NS = 5 * 1_000_000_000
MAX_POST_ANCHOR_CLOCK_ERROR_NS = 150 * 1_000_000
WINDOW_BEFORE_NS = 800 * 1_000_000
WINDOW_AFTER_NS = 0
BUCKET_COUNT = 4
MAX_BUCKET_NS = WINDOW_BEFORE_NS // BUCKET_COUNT
MIN_CHECKPOINT_INTERVAL_NS = 100 * 1_000_000
MAX_CHECKPOINT_INTERVAL_NS = 2 * 1_000_000_000
MIN_BUCKET_PEAK = 128
MIN_BUCKET_RMS = 32
MIN_BUCKET_NONZERO_PER_MILLE = 900
MAX_EXPECTED_LABELS = 16
MAX_EXPECTED_CHECKPOINTS = 256
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class EndpointAudioError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EndpointAudioError(message)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def atomic_exclusive_json(path: Path, value: Any) -> None:
    payload = canonical_bytes(value)
    require(len(payload) <= 1024 * 1024, "endpoint summary exceeds its size limit")
    require(path.parent.is_dir() and not path.parent.is_symlink(),
            "endpoint summary parent is not a nonsymlink directory")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_driver_records(path: Path) -> list[dict[str, Any]]:
    require(path.is_file() and not path.is_symlink(),
            "driver evidence is not a regular nonsymlink file")
    size = path.stat().st_size
    require(0 < size <= MAX_DRIVER_BYTES, "driver evidence size is outside its limit")
    payload = path.read_bytes()
    require(payload.endswith(b"\n") and b"\x00" not in payload,
            "driver evidence is incomplete or contains a NUL byte")
    records: list[dict[str, Any]] = []
    for expected_seq, line in enumerate(payload.splitlines(), 1):
        require(0 < len(line) <= MAX_RECORD_BYTES, "driver record is empty or oversized")
        record = json.loads(line.decode("utf-8"))
        require(type(record) is dict, "driver record is not an object")
        require(record.get("seq") == expected_seq, "driver sequence is not contiguous")
        records.append(record)
    require(records[-1].get("type") == "result"
            and records[-1].get("status") == "pass"
            and records[-1].get("exit_code") == 0,
            "driver did not finish with a passing result")
    origins = {record.get("monotonic_origin_ms") for record in records}
    require(len(origins) == 1, "driver monotonic origin changed")
    origin_ms = next(iter(origins))
    require(type(origin_ms) is int and origin_ms >= 0, "driver monotonic origin is invalid")
    return records


def parse_expectations(values: list[str]) -> dict[str, int]:
    require(values, "at least one checkpoint expectation is required")
    result: dict[str, int] = {}
    for value in values:
        label, separator, count_text = value.rpartition("=")
        require(separator == "=" and LABEL_RE.fullmatch(label) is not None,
                f"invalid checkpoint expectation: {value!r}")
        require(count_text.isascii() and count_text.isdecimal(),
                f"invalid checkpoint count: {value!r}")
        count = int(count_text)
        require(1 <= count <= 64, f"checkpoint count is outside [1, 64]: {value!r}")
        require(label not in result, f"duplicate checkpoint expectation: {label}")
        result[label] = count
        require(len(result) <= MAX_EXPECTED_LABELS,
                f"checkpoint label count exceeds {MAX_EXPECTED_LABELS}")
        require(sum(result.values()) <= MAX_EXPECTED_CHECKPOINTS,
                f"total checkpoint count exceeds {MAX_EXPECTED_CHECKPOINTS}")
    return result


def selected_checkpoints(records: list[dict[str, Any]],
                         expectations: dict[str, int]) -> list[dict[str, Any]]:
    origin_ms = records[0]["monotonic_origin_ms"]
    selected: list[dict[str, Any]] = []
    counts = {label: 0 for label in expectations}
    for record in records:
        label = record.get("label")
        if label not in expectations:
            continue
        require(record.get("type") == "snapshot" and record.get("action") == "snapshot",
                f"checkpoint {label!r} is not a snapshot")
        require(record.get("status") == "ok", f"checkpoint {label!r} did not complete")
        require(record.get("has_audio") is True, f"checkpoint {label!r} lacks an audio stream")
        require(record.get("paused") is False and record.get("ended") is False,
                f"checkpoint {label!r} is not active playback")
        require(record.get("audio_monitor_enabled") is False,
                f"checkpoint {label!r} used the in-driver audio monitor")
        relative_ms = record.get("monotonic_ms")
        require(type(relative_ms) is int and relative_ms >= 0,
                f"checkpoint {label!r} has an invalid monotonic time")
        generation = record.get("source_generation")
        require(type(generation) is int and generation >= 1,
                f"checkpoint {label!r} has an invalid source generation")
        counts[label] += 1
        selected.append({
            "checkpointMonotonicNs": (origin_ms + relative_ms) * 1_000_000,
            "label": label,
            "seq": record["seq"],
            "sourceGeneration": generation,
        })
    require(counts == expectations,
            f"checkpoint counts differ: observed={counts!r} expected={expectations!r}")
    require(all(right["checkpointMonotonicNs"] >= left["checkpointMonotonicNs"]
                for left, right in zip(selected, selected[1:])),
            "checkpoint clock regressed")
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def monotonic_raw_ns() -> int:
    require(hasattr(time, "CLOCK_MONOTONIC_RAW"), "CLOCK_MONOTONIC_RAW is unavailable")
    value = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    require(type(value) is int and value > 0, "CLOCK_MONOTONIC_RAW returned an invalid value")
    return value


def observe_capture_anchor(path: Path) -> tuple[int, int]:
    require(path.is_file() and not path.is_symlink(),
            "endpoint PCM is not a regular nonsymlink file")
    size = path.stat().st_size
    require(0 < size <= MAX_PCM_BYTES, "endpoint PCM anchor size is outside its limit")
    require(size % FRAME_BYTES == 0, "endpoint PCM anchor ends with a partial stereo frame")
    # Take the clock after stat. Every frame included by the observed size
    # therefore existed no later than this timestamp. Scoring only the suffix
    # and assigning its boundary to this later timestamp cannot pull future
    # audio into a pre-checkpoint window.
    return size // FRAME_BYTES, monotonic_raw_ns()


def validate_capture(path: Path, started_ns: int, anchor_frame: int,
                     anchor_ns: int, finished_ns: int) -> tuple[int, int, int, int, str]:
    require(type(started_ns) is int and type(anchor_ns) is int
            and type(finished_ns) is int
            and 0 <= started_ns < anchor_ns < finished_ns,
            "capture monotonic interval is invalid")
    elapsed_ns = finished_ns - started_ns
    require(elapsed_ns <= MAX_CAPTURE_NS, "capture exceeded its duration limit")
    startup_ns = anchor_ns - started_ns
    require(startup_ns <= MAX_CAPTURE_STARTUP_NS,
            "endpoint PCM recorder did not become ready within its startup limit")
    require(path.is_file() and not path.is_symlink(),
            "endpoint PCM is not a regular nonsymlink file")
    size = path.stat().st_size
    require(0 < size <= MAX_PCM_BYTES, "endpoint PCM size is outside its limit")
    require(size % FRAME_BYTES == 0, "endpoint PCM ends with a partial stereo frame")
    frames = size // FRAME_BYTES
    require(type(anchor_frame) is int and 1 <= anchor_frame < frames,
            "endpoint PCM anchor frame is outside the completed capture")
    suffix_frames = frames - anchor_frame
    sampled_ns = suffix_frames * 1_000_000_000 // SAMPLE_RATE
    clock_gap_ns = finished_ns - anchor_ns - sampled_ns
    require(-MAX_POST_ANCHOR_CLOCK_ERROR_NS <= clock_gap_ns
            <= MAX_POST_ANCHOR_CLOCK_ERROR_NS,
            "endpoint PCM post-anchor duration does not calibrate to "
            f"CLOCK_MONOTONIC_RAW (gap_ns={clock_gap_ns})")
    return size, frames, startup_ns, clock_gap_ns, sha256_file(path)


def read_window(path: Path, first_frame: int, last_frame: int) -> bytes:
    require(0 <= first_frame < last_frame, "endpoint window frame range is empty")
    length = (last_frame - first_frame) * FRAME_BYTES
    with path.open("rb") as stream:
        stream.seek(first_frame * FRAME_BYTES)
        payload = stream.read(length)
    require(len(payload) == length, "endpoint PCM was truncated while scoring")
    return payload


def bucket_metrics(payload: bytes) -> dict[str, int]:
    require(len(payload) >= FRAME_BYTES and len(payload) % FRAME_BYTES == 0,
            "endpoint window is not aligned stereo PCM")
    samples = array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    frame_count = len(samples) // CHANNELS
    nonzero_frames = 0
    peak = 0
    square_sum = 0
    for offset in range(0, len(samples), CHANNELS):
        frame_peak = max(abs(samples[offset]), abs(samples[offset + 1]))
        peak = max(peak, frame_peak)
        nonzero_frames += int(frame_peak >= MIN_BUCKET_RMS)
        square_sum += samples[offset] * samples[offset] + samples[offset + 1] * samples[offset + 1]
    rms = math.isqrt(square_sum // len(samples))
    return {
        "frames": frame_count,
        "nonzeroFrames": nonzero_frames,
        "peakAbs": peak,
        "rms": rms,
    }


def score_window(path: Path, first_frame: int, last_frame: int, bucket_count: int,
                 checkpoint: dict[str, Any]) -> dict[str, Any]:
    payload = read_window(path, first_frame, last_frame)
    total_frames = last_frame - first_frame
    bucket_metrics_values: list[dict[str, int]] = []
    require(1 <= bucket_count <= 16, "endpoint window bucket count is outside [1, 16]")
    for index in range(bucket_count):
        bucket_first = total_frames * index // bucket_count
        bucket_last = total_frames * (index + 1) // bucket_count
        metrics = bucket_metrics(payload[bucket_first * FRAME_BYTES:bucket_last * FRAME_BYTES])
        require(metrics["peakAbs"] >= MIN_BUCKET_PEAK,
                f"checkpoint {checkpoint['label']!r} bucket {index + 1} has no endpoint signal")
        require(metrics["rms"] >= MIN_BUCKET_RMS,
                f"checkpoint {checkpoint['label']!r} bucket {index + 1} endpoint RMS is too low")
        require(metrics["nonzeroFrames"] * 1000
                >= metrics["frames"] * MIN_BUCKET_NONZERO_PER_MILLE,
                f"checkpoint {checkpoint['label']!r} bucket {index + 1} is mostly silent")
        bucket_metrics_values.append(metrics)
    whole = bucket_metrics(payload)
    return {
        **checkpoint,
        "activeBucketCount": bucket_count,
        "firstFrame": first_frame,
        "frames": total_frames,
        "lastFrameExclusive": last_frame,
        "nonzeroFrames": whole["nonzeroFrames"],
        "peakAbs": whole["peakAbs"],
        "rms": whole["rms"],
    }


def score(driver_json: Path, pcm: Path, started_ns: int, anchor_frame: int,
          anchor_ns: int, finished_ns: int,
          expectations: dict[str, int]) -> dict[str, Any]:
    records = read_driver_records(driver_json)
    checkpoints = selected_checkpoints(records, expectations)
    pcm_bytes, captured_frames, startup_ns, clock_gap_ns, pcm_sha256 = validate_capture(
        pcm, started_ns, anchor_frame, anchor_ns, finished_ns
    )
    suffix_frames = captured_frames - anchor_frame
    # The anchor size is observed before its timestamp, so the first suffix
    # frame may already exist but cannot be newer than the anchor clock. A
    # positive post-anchor gap may represent any mixture of scheduling, dropped
    # capture time, and shutdown. Assign all of it before the scored suffix.
    # For a frame before an internal gap this is deliberately late; for a frame
    # after the gap it includes at least that frame's elapsed gap. A small
    # negative gap means bytes raced the size observation and is already
    # conservative, so it needs no shift. In both cases a checkpoint can never
    # borrow PCM captured after it.
    sample_clock_anchor_ns = anchor_ns + max(0, clock_gap_ns)
    sampled_end_ns = (
        sample_clock_anchor_ns
        + suffix_frames * 1_000_000_000 // SAMPLE_RATE
    )
    windows = []
    previous_by_label: dict[str, dict[str, Any]] = {}
    for checkpoint in checkpoints:
        previous = previous_by_label.get(checkpoint["label"])
        if previous is None:
            window_start_ns = checkpoint["checkpointMonotonicNs"] - WINDOW_BEFORE_NS
            bridges_previous = False
        else:
            require(previous["sourceGeneration"] == checkpoint["sourceGeneration"],
                    f"checkpoint {checkpoint['label']!r} changed source generation")
            interval_ns = checkpoint["checkpointMonotonicNs"] - previous["checkpointMonotonicNs"]
            require(MIN_CHECKPOINT_INTERVAL_NS <= interval_ns <= MAX_CHECKPOINT_INTERVAL_NS,
                    f"checkpoint {checkpoint['label']!r} interval is outside the continuity bound")
            window_start_ns = previous["checkpointMonotonicNs"]
            bridges_previous = True
        window_end_ns = checkpoint["checkpointMonotonicNs"] + WINDOW_AFTER_NS
        require(window_start_ns >= sample_clock_anchor_ns
                and window_end_ns <= sampled_end_ns,
                f"checkpoint {checkpoint['label']!r} is outside the complete "
                "post-readiness endpoint capture")
        first_frame = anchor_frame + (
            (window_start_ns - sample_clock_anchor_ns) * SAMPLE_RATE
            + 1_000_000_000 - 1
        ) // 1_000_000_000
        last_frame = anchor_frame + (
            (window_end_ns - sample_clock_anchor_ns) * SAMPLE_RATE
            + 1_000_000_000 - 1
        ) // 1_000_000_000
        require(last_frame <= captured_frames, "endpoint window exceeds captured PCM")
        frames_per_bucket = SAMPLE_RATE * MAX_BUCKET_NS // 1_000_000_000
        bucket_count = (last_frame - first_frame + frames_per_bucket - 1) // frames_per_bucket
        windows.append(score_window(
            pcm,
            first_frame,
            last_frame,
            bucket_count,
            {
                **checkpoint,
                "bridgesPreviousCheckpoint": bridges_previous,
                "windowFinishedMonotonicNs": window_end_ns,
                "windowStartedMonotonicNs": window_start_ns,
            },
        ))
        previous_by_label[checkpoint["label"]] = checkpoint
    generations = sorted({window["sourceGeneration"] for window in windows})
    return {
        "captureFinishedMonotonicNs": finished_ns,
        "captureAnchorFrame": anchor_frame,
        "captureAnchorMonotonicNs": anchor_ns,
        "capturePostAnchorClockGapNs": clock_gap_ns,
        "captureStartupNs": startup_ns,
        "captureStartedMonotonicNs": started_ns,
        "capturedFrames": captured_frames,
        "channels": CHANNELS,
        "checkpointCount": len(windows),
        "clockBasis": CLOCK_BASIS,
        "frameBytes": FRAME_BYTES,
        "minimumSignalFramesPerMille": MIN_BUCKET_NONZERO_PER_MILLE,
        "firstCheckpointBucketCount": BUCKET_COUNT,
        "maximumBucketMs": MAX_BUCKET_NS // 1_000_000,
        "pcmBytes": pcm_bytes,
        "pcmSha256": pcm_sha256,
        "requiredCheckpointCounts": expectations,
        "sampleFormat": SAMPLE_FORMAT,
        "sampleRate": SAMPLE_RATE,
        "sampleClockAnchorMonotonicNs": sample_clock_anchor_ns,
        "schema": SCHEMA,
        "source": SOURCE,
        "sourceGenerations": generations,
        "status": "passed",
        "windowAfterMs": WINDOW_AFTER_NS // 1_000_000,
        "windowBeforeMs": WINDOW_BEFORE_NS // 1_000_000,
        "windows": windows,
    }


def command_now(_args: argparse.Namespace) -> None:
    print(monotonic_raw_ns())


def command_anchor(args: argparse.Namespace) -> None:
    frame, observed_ns = observe_capture_anchor(args.pcm)
    print(frame, observed_ns)


def command_score(args: argparse.Namespace) -> None:
    value = score(
        args.driver_json,
        args.pcm,
        args.capture_started_ns,
        args.capture_anchor_frame,
        args.capture_anchor_ns,
        args.capture_finished_ns,
        parse_expectations(args.checkpoint),
    )
    atomic_exclusive_json(args.output, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    now = commands.add_parser("now")
    now.set_defaults(function=command_now)
    anchor = commands.add_parser("anchor")
    anchor.add_argument("--pcm", required=True, type=Path)
    anchor.set_defaults(function=command_anchor)
    scoring = commands.add_parser("score")
    scoring.add_argument("--driver-json", required=True, type=Path)
    scoring.add_argument("--pcm", required=True, type=Path)
    scoring.add_argument("--capture-started-ns", required=True, type=int)
    scoring.add_argument("--capture-anchor-frame", required=True, type=int)
    scoring.add_argument("--capture-anchor-ns", required=True, type=int)
    scoring.add_argument("--capture-finished-ns", required=True, type=int)
    scoring.add_argument("--checkpoint", action="append", required=True)
    scoring.add_argument("--output", required=True, type=Path)
    scoring.set_defaults(function=command_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (EndpointAudioError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"endpoint audio oracle: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
