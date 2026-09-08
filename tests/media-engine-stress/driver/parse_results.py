#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Strict parser and declarative oracle for media-engine-stress JSONL."""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import sys
from typing import Any, Iterable


COMMON_FIELDS = {
    "type",
    "seq",
    "monotonic_ms",
    "monotonic_origin_ms",
    "event",
    "source_generation",
    "timeline_generation",
    "engine_alive",
    "time",
    "duration",
    "rate",
    "paused",
    "seeking",
    "ended",
    "has_audio",
    "has_video",
    "network",
    "network_name",
    "ready",
    "ready_name",
    "media_error",
    "media_error_hr",
    "audio_monitor_enabled",
    "audio_samples_total",
    "audio_bytes_total",
    "audio_samples_generation",
    "audio_bytes_generation",
    "audio_last_sample_time_100ns",
    "audio_last_sample_duration_100ns",
    "audio_last_monotonic_ms",
    "audio_payload_observed",
    "audio_payload_format",
    "audio_channels",
    "audio_rate",
    "audio_bits",
    "audio_payload_bytes_scanned_total",
    "audio_nonzero_bytes_total",
    "audio_nonzero_units_total",
    "audio_nonzero_units_generation",
    "audio_peak_abs",
    "audio_last_nonzero_monotonic_ms",
    "driver_sha256",
    "scenario_sha256",
}

TYPE_FIELDS = {
    "event": {"event_id", "param1", "param2", "generation_current"},
    "snapshot": {"action", "label", "status", "hr", "action_value"},
    "result": {"status", "exit_code", "actions_completed", "message"},
}

ORACLE_KEYS = {
    "expected_result",
    "expected_exit_code",
    "min_source_generations",
    "max_source_generations",
    "required_events",
    "forbidden_events",
    "max_event_counts",
    "required_events_per_generation",
    "required_actions",
    "require_audio_each_generation",
    "require_video_each_generation",
    "require_av_same_snapshot_each_generation",
    "max_media_error_events",
    "max_timeout_snapshots",
    "max_time_regression",
    "min_playback_advance",
    "event_order",
    "max_monotonic_ms",
    "max_stale_events",
    "audio_check_labels",
    "max_audio_silence_ms",
    "min_audio_samples_generation",
    "min_audio_bytes_generation",
    "expected_driver_sha256",
    "expected_scenario_sha256",
    "required_seek_targets",
    "required_completed_seek_targets",
    "seek_target_tolerance",
    "max_seek_action_span_ms",
    "require_seek_completion",
    "min_action_counts",
    "action_order",
    "expected_actions_completed",
    "min_post_seek_advance",
    "min_post_seek_audio_samples",
    "min_post_seek_audio_bytes",
    "min_post_seek_nonzero_units",
    "min_audio_nonzero_units_generation",
    "max_audio_nonzero_silence_ms",
    "required_audio_payload_formats",
    "checkpoint_expectations",
    "event_expectations",
}

CHECKPOINT_KEYS = {
    "action",
    "label",
    "min_matches",
    "max_matches",
    "min_time",
    "max_time",
    "min_time_span",
    "paused",
    "seeking",
    "ended",
    "has_audio",
    "has_video",
    "min_source_generation",
    "max_source_generation",
    "min_timeline_generation",
    "max_timeline_generation",
    "media_error",
    "media_error_hr",
}

EVENT_EXPECTATION_KEYS = {
    "event",
    "min_matches",
    "max_matches",
    "after_action",
    "param1",
    "param2",
    "generation_current",
    "media_error",
    "media_error_hr",
    "paused",
    "seeking",
    "ended",
}


class ParseError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_or_none(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ParseError(message)


def load_records(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, 1):
            if not raw.strip():
                raise ParseError(f"line {line_number}: blank JSONL records are forbidden")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParseError(f"line {line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ParseError(f"line {line_number}: record is not an object")
            validate_record(record, line_number)
            records.append(record)

    if not records:
        raise ParseError("result file is empty")
    validate_sequence(records)
    return records


def validate_record(record: dict[str, Any], line_number: int) -> None:
    prefix = f"line {line_number}"
    kind = record.get("type")
    _require(kind in TYPE_FIELDS, f"{prefix}: invalid record type {kind!r}")
    required = COMMON_FIELDS | TYPE_FIELDS[kind]
    missing = sorted(required - record.keys())
    _require(not missing, f"{prefix}: missing fields: {', '.join(missing)}")
    extra = sorted(record.keys() - required)
    _require(not extra, f"{prefix}: unexpected fields: {', '.join(extra)}")

    _require(_is_int(record["seq"]) and record["seq"] > 0, f"{prefix}: seq must be positive int")
    _require(
        _is_int(record["monotonic_ms"]) and record["monotonic_ms"] >= 0,
        f"{prefix}: monotonic_ms must be nonnegative int",
    )
    for field in (
        "monotonic_origin_ms",
        "source_generation",
        "timeline_generation",
        "network",
        "ready",
        "media_error",
    ):
        _require(_is_int(record[field]) and record[field] >= 0, f"{prefix}: {field} must be nonnegative int")
    for field in ("engine_alive", "paused", "seeking", "ended", "has_audio", "has_video"):
        _require(isinstance(record[field], bool), f"{prefix}: {field} must be bool")
    _require(isinstance(record["audio_monitor_enabled"], bool), f"{prefix}: audio_monitor_enabled must be bool")
    _require(isinstance(record["audio_payload_observed"], bool), f"{prefix}: audio_payload_observed must be bool")
    _require(record["audio_payload_format"] in {"unknown", "pcm16", "pcm32", "float32"}, f"{prefix}: bad audio_payload_format")
    for field in (
        "audio_samples_total",
        "audio_bytes_total",
        "audio_samples_generation",
        "audio_bytes_generation",
        "audio_channels",
        "audio_rate",
        "audio_bits",
        "audio_payload_bytes_scanned_total",
        "audio_nonzero_bytes_total",
        "audio_nonzero_units_total",
        "audio_nonzero_units_generation",
    ):
        _require(_is_int(record[field]) and record[field] >= 0, f"{prefix}: {field} must be nonnegative int")
    for field in (
        "audio_last_sample_time_100ns",
        "audio_last_sample_duration_100ns",
        "audio_last_monotonic_ms",
        "audio_last_nonzero_monotonic_ms",
    ):
        _require(record[field] is None or _is_int(record[field]), f"{prefix}: {field} must be int or null")
    _require(_is_number_or_none(record["audio_peak_abs"]), f"{prefix}: audio_peak_abs must be finite number or null")
    _require(
        record["audio_samples_generation"] <= record["audio_samples_total"],
        f"{prefix}: per-generation audio samples exceed total",
    )
    _require(
        record["audio_bytes_generation"] <= record["audio_bytes_total"],
        f"{prefix}: per-generation audio bytes exceed total",
    )
    _require(
        record["audio_nonzero_bytes_total"] <= record["audio_payload_bytes_scanned_total"],
        f"{prefix}: nonzero audio bytes exceed scanned bytes",
    )
    _require(
        record["audio_nonzero_units_generation"] <= record["audio_nonzero_units_total"],
        f"{prefix}: per-generation nonzero units exceed total",
    )
    if record["audio_last_monotonic_ms"] is not None:
        _require(
            0 <= record["audio_last_monotonic_ms"] <= record["monotonic_ms"],
            f"{prefix}: audio_last_monotonic_ms is outside the record timeline",
        )
    if record["audio_last_nonzero_monotonic_ms"] is not None:
        _require(
            0 <= record["audio_last_nonzero_monotonic_ms"] <= record["monotonic_ms"],
            f"{prefix}: audio_last_nonzero_monotonic_ms is outside the record timeline",
        )
    if not record["audio_monitor_enabled"]:
        _require(
            not record["audio_payload_observed"]
            and record["audio_payload_format"] == "unknown"
            and record["audio_peak_abs"] in (0, 0.0)
            and not any(record[field] for field in (
                "audio_samples_total",
                "audio_bytes_total",
                "audio_samples_generation",
                "audio_bytes_generation",
                "audio_channels",
                "audio_rate",
                "audio_bits",
                "audio_payload_bytes_scanned_total",
                "audio_nonzero_bytes_total",
                "audio_nonzero_units_total",
                "audio_nonzero_units_generation",
            )) and all(record[field] is None for field in (
                "audio_last_sample_time_100ns",
                "audio_last_sample_duration_100ns",
                "audio_last_monotonic_ms",
                "audio_last_nonzero_monotonic_ms",
            )),
            f"{prefix}: disabled audio monitor reported delivery",
        )
    for field in ("time", "duration", "rate"):
        _require(_is_number_or_none(record[field]), f"{prefix}: {field} must be finite number or null")
    for field in ("network_name", "ready_name", "media_error_hr"):
        _require(isinstance(record[field], str), f"{prefix}: {field} must be string")
    for field in ("driver_sha256", "scenario_sha256"):
        value = record[field]
        _require(
            value is None or (
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            ),
            f"{prefix}: {field} must be null or lowercase SHA-256",
        )

    if kind == "event":
        _require(isinstance(record["event"], str), f"{prefix}: event must be string")
        _require(_is_int(record["event_id"]) and record["event_id"] >= 0, f"{prefix}: event_id must be int")
        _require(isinstance(record["param1"], str) and isinstance(record["param2"], str), f"{prefix}: params must be strings")
        _require(isinstance(record["generation_current"], bool), f"{prefix}: generation_current must be bool")
    else:
        _require(record["event"] is None, f"{prefix}: non-event event field must be null")

    if kind == "snapshot":
        _require(isinstance(record["action"], str), f"{prefix}: action must be string")
        _require(record["label"] is None or isinstance(record["label"], str), f"{prefix}: label must be string or null")
        _require(record["status"] in {"ok", "api_error", "timeout"}, f"{prefix}: bad snapshot status")
        _require(isinstance(record["hr"], str), f"{prefix}: hr must be string")
        _require(_is_number_or_none(record["action_value"]), f"{prefix}: action_value must be finite number or null")
    elif kind == "result":
        _require(record["status"] in {"pass", "fail"}, f"{prefix}: bad result status")
        _require(_is_int(record["exit_code"]) and 0 <= record["exit_code"] <= 255, f"{prefix}: bad exit_code")
        _require(
            (record["status"] == "pass") == (record["exit_code"] == 0),
            f"{prefix}: result status and exit_code disagree",
        )
        _require(_is_int(record["actions_completed"]) and record["actions_completed"] >= 0, f"{prefix}: bad actions_completed")
        _require(isinstance(record["message"], str), f"{prefix}: message must be string")


def validate_sequence(records: list[dict[str, Any]]) -> None:
    previous_ms = -1
    previous_snapshot_source = 0
    previous_snapshot_timeline = 0
    maximum_source = 0
    maximum_timeline = 0
    previous_audio_samples = 0
    previous_audio_bytes = 0
    previous_audio_payload_bytes_scanned = 0
    previous_audio_nonzero_bytes = 0
    previous_audio_nonzero_units = 0
    result_positions: list[int] = []
    driver_sha256 = records[0]["driver_sha256"]
    scenario_sha256 = records[0]["scenario_sha256"]
    monotonic_origin_ms = records[0]["monotonic_origin_ms"]

    for index, record in enumerate(records, 1):
        _require(record["seq"] == index, f"record {index}: expected seq {index}, got {record['seq']}")
        _require(record["monotonic_ms"] >= previous_ms, f"record {index}: monotonic_ms regressed")
        _require(record["driver_sha256"] == driver_sha256, f"record {index}: driver_sha256 changed")
        _require(record["scenario_sha256"] == scenario_sha256, f"record {index}: scenario_sha256 changed")
        _require(
            record["monotonic_origin_ms"] == monotonic_origin_ms,
            f"record {index}: monotonic_origin_ms changed",
        )
        _require(
            record["timeline_generation"] >= record["source_generation"],
            f"record {index}: timeline_generation is behind source_generation",
        )
        _require(
            record["source_generation"] <= maximum_source + 1,
            f"record {index}: source_generation jumped by more than one",
        )
        _require(
            record["timeline_generation"] <= maximum_timeline + 1,
            f"record {index}: timeline_generation jumped by more than one",
        )
        maximum_source = max(maximum_source, record["source_generation"])
        maximum_timeline = max(maximum_timeline, record["timeline_generation"])
        if record["type"] == "event" and record["generation_current"]:
            _require(
                record["source_generation"] == maximum_source
                and record["timeline_generation"] == maximum_timeline,
                f"record {index}: event claims a stale generation is current",
            )
        if record["type"] != "event":
            _require(
                record["source_generation"] >= previous_snapshot_source,
                f"record {index}: snapshot source_generation regressed",
            )
            _require(
                record["timeline_generation"] >= previous_snapshot_timeline,
                f"record {index}: snapshot timeline_generation regressed",
            )
            previous_snapshot_source = record["source_generation"]
            previous_snapshot_timeline = record["timeline_generation"]
            _require(
                record["audio_samples_total"] >= previous_audio_samples,
                f"record {index}: audio sample total regressed",
            )
            _require(
                record["audio_bytes_total"] >= previous_audio_bytes,
                f"record {index}: audio byte total regressed",
            )
            _require(
                record["audio_payload_bytes_scanned_total"] >= previous_audio_payload_bytes_scanned,
                f"record {index}: scanned audio payload total regressed",
            )
            _require(
                record["audio_nonzero_bytes_total"] >= previous_audio_nonzero_bytes,
                f"record {index}: nonzero audio byte total regressed",
            )
            _require(
                record["audio_nonzero_units_total"] >= previous_audio_nonzero_units,
                f"record {index}: nonzero audio unit total regressed",
            )
            previous_audio_samples = record["audio_samples_total"]
            previous_audio_bytes = record["audio_bytes_total"]
            previous_audio_payload_bytes_scanned = record["audio_payload_bytes_scanned_total"]
            previous_audio_nonzero_bytes = record["audio_nonzero_bytes_total"]
            previous_audio_nonzero_units = record["audio_nonzero_units_total"]
        if record["type"] == "result":
            result_positions.append(index)
        previous_ms = record["monotonic_ms"]

    _require(result_positions == [len(records)], "exactly one result record must be last")


def load_oracle(path: pathlib.Path) -> dict[str, Any]:
    try:
        oracle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"oracle is invalid JSON: {exc.msg}") from exc
    if not isinstance(oracle, dict):
        raise ParseError("oracle must be a JSON object")
    unknown = sorted(set(oracle) - ORACLE_KEYS)
    if unknown:
        raise ParseError(f"oracle has unknown keys: {', '.join(unknown)}")
    return oracle


def _expect_type(oracle: dict[str, Any], key: str, expected: type | tuple[type, ...]) -> None:
    if key not in oracle:
        return
    value = oracle[key]
    if expected is int:
        valid = _is_int(value)
    elif expected == (int, float):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        valid = isinstance(value, expected)
    if not valid:
        raise ParseError(f"oracle key {key!r} has wrong type")


def validate_oracle(oracle: dict[str, Any]) -> None:
    unknown = sorted(set(oracle) - ORACLE_KEYS)
    if unknown:
        raise ParseError(f"oracle has unknown keys: {', '.join(unknown)}")
    for key in ("expected_result",):
        _expect_type(oracle, key, str)
    for key in ("expected_driver_sha256", "expected_scenario_sha256"):
        _expect_type(oracle, key, str)
    for key in (
        "expected_exit_code",
        "min_source_generations",
        "max_source_generations",
        "max_media_error_events",
        "max_timeout_snapshots",
        "max_monotonic_ms",
        "max_stale_events",
        "max_audio_silence_ms",
        "min_audio_samples_generation",
        "min_audio_bytes_generation",
        "min_audio_nonzero_units_generation",
        "max_audio_nonzero_silence_ms",
        "expected_actions_completed",
        "min_post_seek_audio_samples",
        "min_post_seek_audio_bytes",
        "min_post_seek_nonzero_units",
    ):
        _expect_type(oracle, key, int)
    for key in (
        "max_time_regression",
        "min_playback_advance",
        "seek_target_tolerance",
        "max_seek_action_span_ms",
        "min_post_seek_advance",
    ):
        _expect_type(oracle, key, (int, float))
    for key in (
        "require_audio_each_generation",
        "require_video_each_generation",
        "require_av_same_snapshot_each_generation",
        "require_seek_completion",
    ):
        _expect_type(oracle, key, bool)
    for key in (
        "required_events",
        "forbidden_events",
        "required_events_per_generation",
        "required_actions",
        "event_order",
        "audio_check_labels",
        "action_order",
        "required_audio_payload_formats",
    ):
        if key in oracle:
            if not isinstance(oracle[key], list) or not all(isinstance(item, str) for item in oracle[key]):
                raise ParseError(f"oracle key {key!r} must be a list of strings")
    if "required_audio_payload_formats" in oracle and any(
        value not in {"pcm16", "pcm32", "float32"}
        for value in oracle["required_audio_payload_formats"]
    ):
        raise ParseError("required_audio_payload_formats contains an unsupported format")
    if "required_seek_targets" in oracle and (
        not isinstance(oracle["required_seek_targets"], list)
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in oracle["required_seek_targets"]
        )
    ):
        raise ParseError("oracle key 'required_seek_targets' must be a list of finite numbers")
    if "required_completed_seek_targets" in oracle and (
        not isinstance(oracle["required_completed_seek_targets"], list)
        or not oracle["required_completed_seek_targets"]
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value >= 0
            for value in oracle["required_completed_seek_targets"]
        )
    ):
        raise ParseError(
            "oracle key 'required_completed_seek_targets' must be a nonempty list "
            "of finite nonnegative numbers"
        )
    if "min_action_counts" in oracle and (
        not isinstance(oracle["min_action_counts"], dict)
        or not all(
            isinstance(name, str) and _is_int(count) and count >= 0
            for name, count in oracle["min_action_counts"].items()
        )
    ):
        raise ParseError("oracle key 'min_action_counts' must map action names to nonnegative ints")
    if "max_event_counts" in oracle and (
        not isinstance(oracle["max_event_counts"], dict)
        or not all(
            isinstance(name, str) and name and _is_int(count) and count >= 0
            for name, count in oracle["max_event_counts"].items()
        )
    ):
        raise ParseError("oracle key 'max_event_counts' must map event names to nonnegative ints")
    if "checkpoint_expectations" in oracle:
        expectations = oracle["checkpoint_expectations"]
        if not isinstance(expectations, list) or not expectations:
            raise ParseError("oracle key 'checkpoint_expectations' must be a nonempty list")
        for index, expectation in enumerate(expectations):
            prefix = f"checkpoint expectation {index}"
            if not isinstance(expectation, dict):
                raise ParseError(f"{prefix} must be an object")
            unknown_checkpoint_keys = sorted(set(expectation) - CHECKPOINT_KEYS)
            if unknown_checkpoint_keys:
                raise ParseError(
                    f"{prefix} has unknown keys: {', '.join(unknown_checkpoint_keys)}"
                )
            if set(expectation) & {"action", "label"} != {"action", "label"}:
                raise ParseError(f"{prefix} requires action and label")
            for key in ("action", "label"):
                value = expectation[key]
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ParseError(f"{prefix} {key} must be nonempty text up to 128 characters")
            for key in ("min_matches", "max_matches"):
                if key in expectation and (
                    not _is_int(expectation[key]) or not 0 <= expectation[key] <= 10000
                ):
                    raise ParseError(f"{prefix} {key} must be an integer in [0, 10000]")
            for key in (
                "min_source_generation",
                "max_source_generation",
                "min_timeline_generation",
                "max_timeline_generation",
            ):
                if key in expectation and (
                    not _is_int(expectation[key]) or expectation[key] < 0
                ):
                    raise ParseError(f"{prefix} {key} must be a nonnegative integer")
            for key in ("min_time", "max_time", "min_time_span"):
                if key in expectation and (
                    not isinstance(expectation[key], (int, float))
                    or isinstance(expectation[key], bool)
                    or not math.isfinite(float(expectation[key]))
                    or expectation[key] < 0
                ):
                    raise ParseError(f"{prefix} {key} must be a finite nonnegative number")
            for key in ("paused", "seeking", "ended", "has_audio", "has_video"):
                if key in expectation and not isinstance(expectation[key], bool):
                    raise ParseError(f"{prefix} {key} must be boolean")
            if "media_error" in expectation and (
                not _is_int(expectation["media_error"])
                or not 0 <= expectation["media_error"] <= 0xffff
            ):
                raise ParseError(f"{prefix} media_error must be an integer in [0, 65535]")
            if "media_error_hr" in expectation and (
                not isinstance(expectation["media_error_hr"], str)
                or len(expectation["media_error_hr"]) != 10
                or not expectation["media_error_hr"].startswith("0x")
                or any(character not in "0123456789abcdef"
                       for character in expectation["media_error_hr"][2:])
            ):
                raise ParseError(f"{prefix} media_error_hr must be lowercase 32-bit hex")
            for minimum, maximum in (
                ("min_matches", "max_matches"),
                ("min_time", "max_time"),
                ("min_source_generation", "max_source_generation"),
                ("min_timeline_generation", "max_timeline_generation"),
            ):
                if (
                    minimum in expectation
                    and maximum in expectation
                    and expectation[minimum] > expectation[maximum]
                ):
                    raise ParseError(f"{prefix} {minimum} exceeds {maximum}")

    if "event_expectations" in oracle:
        expectations = oracle["event_expectations"]
        if not isinstance(expectations, list) or not expectations:
            raise ParseError("oracle key 'event_expectations' must be a nonempty list")
        for index, expectation in enumerate(expectations):
            prefix = f"event expectation {index}"
            if not isinstance(expectation, dict):
                raise ParseError(f"{prefix} must be an object")
            unknown_event_keys = sorted(set(expectation) - EVENT_EXPECTATION_KEYS)
            if unknown_event_keys:
                raise ParseError(
                    f"{prefix} has unknown keys: {', '.join(unknown_event_keys)}"
                )
            event_name = expectation.get("event")
            if not isinstance(event_name, str) or not event_name or len(event_name) > 128:
                raise ParseError(f"{prefix} event must be nonempty text up to 128 characters")
            for key in ("min_matches", "max_matches"):
                if key in expectation and (
                    not _is_int(expectation[key]) or not 0 <= expectation[key] <= 10000
                ):
                    raise ParseError(f"{prefix} {key} must be an integer in [0, 10000]")
            if (
                "min_matches" in expectation
                and "max_matches" in expectation
                and expectation["min_matches"] > expectation["max_matches"]
            ):
                raise ParseError(f"{prefix} min_matches exceeds max_matches")
            if "after_action" in expectation and (
                not isinstance(expectation["after_action"], str)
                or not expectation["after_action"]
                or len(expectation["after_action"]) > 128
            ):
                raise ParseError(
                    f"{prefix} after_action must be nonempty text up to 128 characters"
                )
            for key in ("param1", "param2", "media_error_hr"):
                if key not in expectation:
                    continue
                value = expectation[key]
                digits = value[2:] if isinstance(value, str) and value.startswith("0x") else ""
                expected_digits = None if key == "param1" else 8
                if (
                    not isinstance(value, str)
                    or not digits
                    or (expected_digits is not None and len(digits) != expected_digits)
                    or any(character not in "0123456789abcdef" for character in digits)
                ):
                    suffix = "lowercase hex" if expected_digits is None else "lowercase 32-bit hex"
                    raise ParseError(f"{prefix} {key} must be {suffix}")
            if "media_error" in expectation and (
                not _is_int(expectation["media_error"])
                or not 0 <= expectation["media_error"] <= 0xffff
            ):
                raise ParseError(f"{prefix} media_error must be an integer in [0, 65535]")
            for key in ("generation_current", "paused", "seeking", "ended"):
                if key in expectation and not isinstance(expectation[key], bool):
                    raise ParseError(f"{prefix} {key} must be boolean")

    if oracle.get("expected_result", "pass") not in {"pass", "fail"}:
        raise ParseError("expected_result must be 'pass' or 'fail'")
    for key in ("expected_driver_sha256", "expected_scenario_sha256"):
        if key in oracle and (
            len(oracle[key]) != 64
            or any(character not in "0123456789abcdef" for character in oracle[key])
        ):
            raise ParseError(f"oracle key {key!r} must be lowercase SHA-256")
    for key in (
        "expected_exit_code",
        "min_source_generations",
        "max_source_generations",
        "max_media_error_events",
        "max_timeout_snapshots",
        "max_monotonic_ms",
        "max_stale_events",
        "max_audio_silence_ms",
        "min_audio_samples_generation",
        "min_audio_bytes_generation",
        "expected_actions_completed",
        "min_post_seek_audio_samples",
        "min_post_seek_audio_bytes",
        "min_post_seek_nonzero_units",
        "min_audio_nonzero_units_generation",
        "max_audio_nonzero_silence_ms",
    ):
        if key in oracle and oracle[key] < 0:
            raise ParseError(f"oracle key {key!r} cannot be negative")
    for key in (
        "max_time_regression",
        "min_playback_advance",
        "seek_target_tolerance",
        "max_seek_action_span_ms",
        "min_post_seek_advance",
    ):
        if key in oracle and (not math.isfinite(float(oracle[key])) or oracle[key] < 0):
            raise ParseError(f"oracle key {key!r} must be finite and nonnegative")


def _generations(records: Iterable[dict[str, Any]]) -> list[int]:
    return sorted({record["source_generation"] for record in records if record["source_generation"] > 0})


def _ordered_subsequence(values: list[str], expected: list[str]) -> bool:
    position = 0
    for value in values:
        if position < len(expected) and value == expected[position]:
            position += 1
    return position == len(expected)


def _is_playback_observation(record: dict[str, Any]) -> bool:
    """Exclude teardown/result state, whose MediaEngine clock may reset to zero."""
    return record["type"] == "event" or (
        record["type"] == "snapshot" and record["action"] != "shutdown"
    )


def _playback_boundaries(records: list[dict[str, Any]]) -> dict[tuple[int, int], int]:
    """Return the first record belonging to each active playback timeline.

    SetSource begins a source generation before its asynchronous Start(0)
    completes.  Readiness events in that interval may therefore expose the
    stopped outgoing clock.  A seek similarly begins a timeline before the
    requested position becomes current.  PLAYING and SEEKED are the first
    observations at which those respective timeline clocks are active.
    """
    seek_timelines = {
        (record["source_generation"], record["timeline_generation"])
        for record in records
        if record["type"] == "snapshot"
        and record["action"] == "seek"
        and record["status"] == "ok"
    }
    boundaries: dict[tuple[int, int], int] = {}
    for record in records:
        if (
            record["type"] != "event"
            or not record["generation_current"]
            or record["source_generation"] == 0
        ):
            continue
        key = (record["source_generation"], record["timeline_generation"])
        expected = "SEEKED" if key in seek_timelines else "PLAYING"
        if record["event"] == expected:
            boundaries.setdefault(key, record["seq"])
    return boundaries


def _is_active_playback_observation(
        record: dict[str, Any], boundaries: dict[tuple[int, int], int]) -> bool:
    key = (record["source_generation"], record["timeline_generation"])
    boundary = boundaries.get(key)
    return (
        boundary is not None
        and record["seq"] >= boundary
        and _is_playback_observation(record)
        and (record["type"] != "event" or record["generation_current"])
    )


def evaluate(records: list[dict[str, Any]], oracle: dict[str, Any]) -> list[str]:
    validate_oracle(oracle)
    failures: list[str] = []
    result = records[-1]
    all_events = [record for record in records if record["type"] == "event"]
    events = [record for record in all_events if record["generation_current"]]
    snapshots = [record for record in records if record["type"] == "snapshot"]
    event_values = [record["event"] for record in events]
    action_values = [record["action"] for record in snapshots]
    generations = _generations(records)

    expected_result = oracle.get("expected_result", "pass")
    if result["status"] != expected_result:
        failures.append(f"result status {result['status']!r}, expected {expected_result!r}")
    if "expected_exit_code" in oracle and result["exit_code"] != oracle["expected_exit_code"]:
        failures.append(f"exit_code {result['exit_code']}, expected {oracle['expected_exit_code']}")
    if "expected_actions_completed" in oracle and result["actions_completed"] != oracle["expected_actions_completed"]:
        failures.append(
            f"actions_completed {result['actions_completed']}, expected {oracle['expected_actions_completed']}"
        )
    for key, field in (
        ("expected_driver_sha256", "driver_sha256"),
        ("expected_scenario_sha256", "scenario_sha256"),
    ):
        if key in oracle and result[field] != oracle[key]:
            failures.append(f"{field} {result[field]!r}, expected {oracle[key]!r}")

    generation_count = len(generations)
    if generation_count < oracle.get("min_source_generations", 0):
        failures.append(f"only {generation_count} source generations")
    if "max_source_generations" in oracle and generation_count > oracle["max_source_generations"]:
        failures.append(f"{generation_count} source generations exceeds limit")

    for required in oracle.get("required_events", []):
        if required not in event_values:
            failures.append(f"required event {required} absent")
    for forbidden in oracle.get("forbidden_events", []):
        if any(record["event"] == forbidden for record in all_events):
            failures.append(f"forbidden event {forbidden} observed")
    for event_name, maximum in oracle.get("max_event_counts", {}).items():
        count = sum(record["event"] == event_name for record in all_events)
        if count > maximum:
            failures.append(f"event {event_name} count {count} exceeds {maximum}")
    for required in oracle.get("required_actions", []):
        if required not in action_values:
            failures.append(f"required action {required} absent")
    for action, minimum in oracle.get("min_action_counts", {}).items():
        count = action_values.count(action)
        if count < minimum:
            failures.append(f"action {action} count {count} is below {minimum}")
    for expectation in oracle.get("checkpoint_expectations", []):
        action = expectation["action"]
        label = expectation["label"]
        matches = [
            record for record in snapshots
            if record["action"] == action and record["label"] == label
        ]
        minimum_matches = expectation.get("min_matches", 1)
        maximum_matches = expectation.get("max_matches")
        if len(matches) < minimum_matches:
            failures.append(
                f"checkpoint {action}/{label!r} count {len(matches)} is below {minimum_matches}"
            )
        if maximum_matches is not None and len(matches) > maximum_matches:
            failures.append(
                f"checkpoint {action}/{label!r} count {len(matches)} exceeds {maximum_matches}"
            )
        for record in matches:
            for key, field in (
                ("paused", "paused"),
                ("seeking", "seeking"),
                ("ended", "ended"),
                ("has_audio", "has_audio"),
                ("has_video", "has_video"),
                ("media_error", "media_error"),
                ("media_error_hr", "media_error_hr"),
            ):
                if key in expectation and record[field] != expectation[key]:
                    failures.append(
                        f"checkpoint {action}/{label!r} {field} is {record[field]}, "
                        f"expected {expectation[key]}"
                    )
            if "min_time" in expectation or "max_time" in expectation:
                if record["time"] is None:
                    failures.append(f"checkpoint {action}/{label!r} has no media time")
                else:
                    if "min_time" in expectation and record["time"] < expectation["min_time"]:
                        failures.append(
                            f"checkpoint {action}/{label!r} time {record['time']} is below "
                            f"{expectation['min_time']}"
                        )
                    if "max_time" in expectation and record["time"] > expectation["max_time"]:
                        failures.append(
                            f"checkpoint {action}/{label!r} time {record['time']} exceeds "
                            f"{expectation['max_time']}"
                        )
            for minimum, maximum, field in (
                ("min_source_generation", "max_source_generation", "source_generation"),
                ("min_timeline_generation", "max_timeline_generation", "timeline_generation"),
            ):
                if minimum in expectation and record[field] < expectation[minimum]:
                    failures.append(
                        f"checkpoint {action}/{label!r} {field} {record[field]} is below "
                        f"{expectation[minimum]}"
                    )
                if maximum in expectation and record[field] > expectation[maximum]:
                    failures.append(
                        f"checkpoint {action}/{label!r} {field} {record[field]} exceeds "
                        f"{expectation[maximum]}"
                    )
        if "min_time_span" in expectation:
            times = [float(record["time"]) for record in matches if record["time"] is not None]
            if len(times) < 2:
                failures.append(
                    f"checkpoint {action}/{label!r} has fewer than two media-time observations"
                )
            else:
                span = max(times) - min(times)
                if span < float(expectation["min_time_span"]):
                    failures.append(
                        f"checkpoint {action}/{label!r} time span {span:.6f} is below "
                        f"{float(expectation['min_time_span']):.6f}"
                    )
    for expectation in oracle.get("event_expectations", []):
        event_name = expectation["event"]
        matches = [record for record in all_events if record["event"] == event_name]
        minimum_matches = expectation.get("min_matches", 1)
        maximum_matches = expectation.get("max_matches")
        if len(matches) < minimum_matches:
            failures.append(
                f"event expectation {event_name!r} count {len(matches)} is below {minimum_matches}"
            )
        if maximum_matches is not None and len(matches) > maximum_matches:
            failures.append(
                f"event expectation {event_name!r} count {len(matches)} exceeds {maximum_matches}"
            )
        for record in matches:
            for key in (
                "param1",
                "param2",
                "generation_current",
                "media_error",
                "media_error_hr",
                "paused",
                "seeking",
                "ended",
            ):
                if key in expectation and record[key] != expectation[key]:
                    failures.append(
                        f"event expectation {event_name!r} at seq {record['seq']} "
                        f"{key} is {record[key]!r}, expected {expectation[key]!r}"
                    )
            if "after_action" in expectation:
                action = expectation["after_action"]
                preceding = [
                    snapshot
                    for snapshot in snapshots
                    if snapshot["action"] == action
                    and snapshot["status"] == "ok"
                    and snapshot["seq"] < record["seq"]
                    and snapshot["source_generation"] == record["source_generation"]
                    and snapshot["timeline_generation"] == record["timeline_generation"]
                ]
                if not preceding:
                    failures.append(
                        f"event expectation {event_name!r} at seq {record['seq']} "
                        f"was not fenced after successful action {action!r} "
                        "in the same source/timeline"
                    )
    required_action_order = oracle.get("action_order", [])
    if required_action_order and not _ordered_subsequence(action_values, required_action_order):
        failures.append(f"action order {required_action_order!r} not observed")
    expected_order = oracle.get("event_order", [])
    if expected_order and not _ordered_subsequence(event_values, expected_order):
        failures.append(f"event order {expected_order!r} not observed")

    seek_snapshots = [
        record
        for record in snapshots
        if record["action"] == "seek" and record["status"] == "ok"
    ]
    required_targets = [float(value) for value in oracle.get("required_seek_targets", [])]
    if required_targets:
        actual_targets = [float(record["action_value"]) for record in seek_snapshots]
        tolerance = float(oracle.get("seek_target_tolerance", 0.05))
        position = 0
        for target in actual_targets:
            if position < len(required_targets) and abs(target - required_targets[position]) <= tolerance:
                position += 1
        if position != len(required_targets):
            failures.append(f"required seek target order {required_targets!r} not observed")
    required_completed_targets = [
        float(value) for value in oracle.get("required_completed_seek_targets", [])
    ]
    if required_completed_targets:
        completed_seek_times = [
            float(record["time"])
            for record in all_events
            if record["event"] == "SEEKED"
            and record["generation_current"]
            and record["param2"] == "0x00000000"
            and record["time"] is not None
        ]
        tolerance = float(oracle.get("seek_target_tolerance", 0.05))
        position = 0
        for completed_time in completed_seek_times:
            if (
                position < len(required_completed_targets)
                and abs(completed_time - required_completed_targets[position]) <= tolerance
            ):
                position += 1
        if position != len(required_completed_targets):
            failures.append(
                f"required completed seek target order {required_completed_targets!r} "
                "not observed in current-generation successful SEEKED times "
                f"{completed_seek_times!r}; first unmatched target "
                f"{required_completed_targets[position]!r} at required index {position}"
            )
    if "max_seek_action_span_ms" in oracle:
        if not seek_snapshots:
            failures.append(
                "successful seek action span unavailable: no successful seek action snapshots"
            )
        else:
            first_seek = seek_snapshots[0]
            last_seek = seek_snapshots[-1]
            span_ms = last_seek["monotonic_ms"] - first_seek["monotonic_ms"]
            maximum_span_ms = float(oracle["max_seek_action_span_ms"])
            if span_ms > maximum_span_ms:
                failures.append(
                    f"successful seek action span {span_ms} ms "
                    f"(seq {first_seek['seq']} to {last_seek['seq']}) exceeds "
                    f"{maximum_span_ms:g} ms"
                )
    post_seek_checks = any(key in oracle for key in (
        "min_post_seek_advance",
        "min_post_seek_audio_samples",
        "min_post_seek_audio_bytes",
        "min_post_seek_nonzero_units",
    ))
    if oracle.get("require_seek_completion") or post_seek_checks:
        tolerance = float(oracle.get("seek_target_tolerance", 0.5))
        for seek in seek_snapshots:
            target = seek["action_value"]
            candidates = [
                record
                for record in records
                if record["source_generation"] == seek["source_generation"]
                and record["timeline_generation"] == seek["timeline_generation"]
                and record["time"] is not None
                and (
                    (record["type"] == "event" and record["event"] == "SEEKED" and record["generation_current"])
                    or (
                        record["type"] == "snapshot"
                        and record["action"] == "wait_event"
                        and record["label"] == "SEEKED"
                        and record["status"] == "ok"
                    )
                )
            ]
            matching = [] if target is None else [
                record for record in candidates
                if abs(float(record["time"]) - float(target)) <= tolerance
            ]
            if not matching:
                failures.append(
                    f"timeline {(seek['source_generation'], seek['timeline_generation'])}: "
                    f"seek target {target!r} has no matching SEEKED state"
                )
                continue
            completion = min(matching, key=lambda record: record["seq"])
            later = [
                record
                for record in records
                if record["source_generation"] == seek["source_generation"]
                and record["timeline_generation"] == seek["timeline_generation"]
                and record["seq"] > completion["seq"]
            ]
            if "min_post_seek_advance" in oracle:
                times = [
                    float(record["time"])
                    for record in later
                    if record["time"] is not None and not record["paused"] and not record["seeking"]
                ]
                advance = max(times, default=float(completion["time"])) - float(completion["time"])
                if advance < float(oracle["min_post_seek_advance"]):
                    failures.append(
                        f"timeline {(seek['source_generation'], seek['timeline_generation'])}: "
                        f"post-seek advance {advance:.6f} below {float(oracle['min_post_seek_advance']):.6f}"
                    )
            if "min_post_seek_audio_samples" in oracle:
                delivered = max(
                    (record["audio_samples_total"] for record in later),
                    default=completion["audio_samples_total"],
                ) - completion["audio_samples_total"]
                if delivered < oracle["min_post_seek_audio_samples"]:
                    failures.append(
                        f"timeline {(seek['source_generation'], seek['timeline_generation'])}: "
                        f"post-seek audio samples {delivered} below {oracle['min_post_seek_audio_samples']}"
                    )
            if "min_post_seek_audio_bytes" in oracle:
                delivered = max(
                    (record["audio_bytes_total"] for record in later),
                    default=completion["audio_bytes_total"],
                ) - completion["audio_bytes_total"]
                if delivered < oracle["min_post_seek_audio_bytes"]:
                    failures.append(
                        f"timeline {(seek['source_generation'], seek['timeline_generation'])}: "
                        f"post-seek audio bytes {delivered} below {oracle['min_post_seek_audio_bytes']}"
                    )
            if "min_post_seek_nonzero_units" in oracle:
                delivered = max(
                    (record["audio_nonzero_units_total"] for record in later),
                    default=completion["audio_nonzero_units_total"],
                ) - completion["audio_nonzero_units_total"]
                if delivered < oracle["min_post_seek_nonzero_units"]:
                    failures.append(
                        f"timeline {(seek['source_generation'], seek['timeline_generation'])}: "
                        f"post-seek nonzero audio units {delivered} below {oracle['min_post_seek_nonzero_units']}"
                    )

    required_per_generation = oracle.get("required_events_per_generation", [])
    for generation in generations:
        values = [record["event"] for record in events if record["source_generation"] == generation]
        for required in required_per_generation:
            if required not in values:
                failures.append(f"generation {generation}: required event {required} absent")

    # Track discovery is meaningful only once metadata exists.  This avoids
    # treating expected pre-load false values as an audio/video failure.
    for generation in generations:
        eligible = [
            record
            for record in records
            if record["source_generation"] == generation and record["ready"] >= 1
        ]
        if oracle.get("require_audio_each_generation") and not any(record["has_audio"] for record in eligible):
            failures.append(f"generation {generation}: no metadata-or-later snapshot reports audio")
        if oracle.get("require_video_each_generation") and not any(record["has_video"] for record in eligible):
            failures.append(f"generation {generation}: no metadata-or-later snapshot reports video")
        if oracle.get("require_av_same_snapshot_each_generation") and not any(
            record["has_audio"] and record["has_video"] for record in eligible
        ):
            failures.append(f"generation {generation}: audio and video never coexist in one snapshot")

    media_error_events = sum(record["event"] in {"ERROR", "STREAMRENDERINGERROR"} for record in all_events)
    if media_error_events > oracle.get("max_media_error_events", media_error_events):
        failures.append(f"{media_error_events} media error events observed")
    timeout_snapshots = sum(record["status"] == "timeout" for record in snapshots)
    if timeout_snapshots > oracle.get("max_timeout_snapshots", timeout_snapshots):
        failures.append(f"{timeout_snapshots} timeout snapshots observed")
    stale_events = sum(not record["generation_current"] for record in all_events)
    if stale_events > oracle.get("max_stale_events", stale_events):
        failures.append(f"{stale_events} stale-generation events observed")

    audio_labels = oracle.get("audio_check_labels", [])
    if any(key in oracle for key in (
        "max_audio_silence_ms",
        "min_audio_samples_generation",
        "min_audio_bytes_generation",
        "min_audio_nonzero_units_generation",
        "max_audio_nonzero_silence_ms",
        "required_audio_payload_formats",
    )) and not audio_labels:
        raise ParseError("audio delivery thresholds require nonempty audio_check_labels")
    for label in audio_labels:
        matches = [record for record in snapshots if record["label"] == label]
        if not matches:
            failures.append(f"audio checkpoint label {label!r} absent")
            continue
        for record in matches:
            if not record["audio_monitor_enabled"]:
                failures.append(f"audio checkpoint {label!r}: audio monitor was not enabled")
                continue
            minimum_samples = oracle.get("min_audio_samples_generation", 1)
            minimum_bytes = oracle.get("min_audio_bytes_generation", 1)
            minimum_nonzero = oracle.get("min_audio_nonzero_units_generation", 0)
            if record["audio_samples_generation"] < minimum_samples:
                failures.append(
                    f"audio checkpoint {label!r}: {record['audio_samples_generation']} samples below {minimum_samples}"
                )
            if record["audio_bytes_generation"] < minimum_bytes:
                failures.append(
                    f"audio checkpoint {label!r}: {record['audio_bytes_generation']} bytes below {minimum_bytes}"
                )
            if not record["audio_payload_observed"]:
                failures.append(f"audio checkpoint {label!r}: no payload buffer was inspected")
            required_formats = oracle.get("required_audio_payload_formats", [])
            if required_formats and record["audio_payload_format"] not in required_formats:
                failures.append(
                    f"audio checkpoint {label!r}: payload format {record['audio_payload_format']!r} "
                    f"not in {required_formats!r}"
                )
            if record["audio_nonzero_units_generation"] < minimum_nonzero:
                failures.append(
                    f"audio checkpoint {label!r}: {record['audio_nonzero_units_generation']} "
                    f"nonzero units below {minimum_nonzero}"
                )
            if "max_audio_silence_ms" in oracle:
                last = record["audio_last_monotonic_ms"]
                if last is None:
                    failures.append(f"audio checkpoint {label!r}: no delivered audio timestamp")
                elif record["monotonic_ms"] - last > oracle["max_audio_silence_ms"]:
                    failures.append(
                        f"audio checkpoint {label!r}: no delivered sample for "
                        f"{record['monotonic_ms'] - last} ms"
                    )
            if "max_audio_nonzero_silence_ms" in oracle:
                last_nonzero = record["audio_last_nonzero_monotonic_ms"]
                if last_nonzero is None:
                    failures.append(f"audio checkpoint {label!r}: no nonzero audio unit observed")
                elif record["monotonic_ms"] - last_nonzero > oracle["max_audio_nonzero_silence_ms"]:
                    failures.append(
                        f"audio checkpoint {label!r}: no nonzero audio unit for "
                        f"{record['monotonic_ms'] - last_nonzero} ms"
                    )

    if "max_monotonic_ms" in oracle and records[-1]["monotonic_ms"] > oracle["max_monotonic_ms"]:
        failures.append(
            f"runtime {records[-1]['monotonic_ms']} ms exceeds {oracle['max_monotonic_ms']} ms"
        )

    # A source generation begins before asynchronous Start(0) completes, and
    # an explicit seek begins its timeline before SEEKED.  Only compare clock
    # values after the corresponding playback boundary; otherwise a valid
    # source/seek transition can look like spontaneous clock regression.
    playback_boundaries = _playback_boundaries(records)
    if "max_time_regression" in oracle:
        limit = float(oracle["max_time_regression"])
        previous: dict[tuple[int, int], float] = {}
        for record in records:
            value = record["time"]
            key = (record["source_generation"], record["timeline_generation"])
            if (not _is_active_playback_observation(record, playback_boundaries)
                    or value is None or record["seeking"]):
                continue
            if key in previous and float(value) + limit < previous[key]:
                failures.append(
                    f"timeline {key}: time regressed from {previous[key]:.6f} to {float(value):.6f}"
                )
                break
            previous[key] = float(value)

    if "min_playback_advance" in oracle:
        spans: dict[tuple[int, int], list[float]] = collections.defaultdict(list)
        for record in records:
            if (
                _is_active_playback_observation(record, playback_boundaries)
                and record["time"] is not None
                and not record["paused"]
                and not record["seeking"]
                and (record["rate"] is None or record["rate"] > 0)
            ):
                spans[(record["source_generation"], record["timeline_generation"])].append(float(record["time"]))
        greatest = max((max(values) - min(values) for values in spans.values() if values), default=0.0)
        if greatest < float(oracle["min_playback_advance"]):
            failures.append(
                f"greatest uninterrupted playback advance {greatest:.6f} is below {float(oracle['min_playback_advance']):.6f}"
            )

    return failures


def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    event_records = [record for record in records if record["type"] == "event"]
    events = [record["event"] for record in event_records]
    return {
        "records": len(records),
        "duration_ms": records[-1]["monotonic_ms"],
        "source_generations": len(_generations(records)),
        "event_counts": dict(sorted(collections.Counter(events).items())),
        "stale_events": sum(not record["generation_current"] for record in event_records),
        "result": records[-1]["status"],
        "exit_code": records[-1]["exit_code"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=pathlib.Path)
    parser.add_argument("--oracle", type=pathlib.Path)
    parser.add_argument("--summary", type=pathlib.Path)
    args = parser.parse_args(argv)

    try:
        records = load_records(args.result)
        oracle = load_oracle(args.oracle) if args.oracle else {"expected_result": "pass", "expected_exit_code": 0}
        failures = evaluate(records, oracle)
    except (OSError, ParseError) as exc:
        print(f"media-engine-stress parser: {exc}", file=sys.stderr)
        return 2

    report = summary(records)
    if args.summary:
        args.summary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
