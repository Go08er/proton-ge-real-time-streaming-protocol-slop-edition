#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Join flushed MediaEngine records to sanitized HTTP monotonic timing."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

import stress_fixture_service


MAX_JSONL_BYTES = 16 * 1024 * 1024
MAX_COMPLETION_BYTES = 4096
MAX_OBSERVATION_LAG_MS = 250
MIN_OBSERVATION_LAG_MS = -2
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
HTTP_KEYS = {
    "bytes_expected", "bytes_sent", "case_id", "clock_basis", "disconnected",
    "finished_at", "finished_monotonic_ns", "method", "range",
    "request_seq", "started_at", "started_monotonic_ns", "status",
}
COMPLETION_KEYS = {
    "allocatedRequestCount", "caseId", "clockBasis", "logBytes", "logSha256",
    "rejectedConnectionCount", "schema", "status", "terminalRecordCount",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RANGE_RE = re.compile(r"^bytes=([0-9]+)-([0-9]+)$")
UTC_MILLISECOND_RE = re.compile(
    r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
MAX_HTTP_INTERVAL_NS = 600 * 1_000_000_000


class TransportError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TransportError(message)


def atomic_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def monotonic_raw_ns() -> int:
    """Use the Linux clock Wine uses for GetTickCount64 in this VM."""

    require(hasattr(time, "CLOCK_MONOTONIC_RAW"), "CLOCK_MONOTONIC_RAW is unavailable")
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def parse_utc_milliseconds(value: object, label: str) -> datetime:
    """Validate the bounded, canonical wall-clock diagnostics in HTTP logs."""

    require(type(value) is str and UTC_MILLISECOND_RE.fullmatch(value) is not None,
            f"HTTP {label} is not a canonical UTC millisecond timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise TransportError(f"HTTP {label} is not a valid calendar timestamp") from error
    require(2020 <= parsed.year <= 2099, f"HTTP {label} is outside the accepted year range")
    return parsed


def range_start(record: dict[str, object]) -> int | None:
    value = record["range"]
    if type(value) is not str:
        return None
    match = RANGE_RE.fullmatch(value)
    return int(match.group(1)) if match else None


def command_watch(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    offset = 0
    pending = b""
    expected_seq = 1
    origin_ms: int | None = None
    first_playback: int | None = None
    first_observed: int | None = None
    seek_times: list[int] = []
    max_lag_ms = 0
    saw_result = False
    result_count = 0
    last_event_ns: int | None = None
    producer_done_observed = False

    while time.monotonic() < deadline:
        done_before_read = args.producer_done.exists()
        producer_done_observed = producer_done_observed or done_before_read
        if not args.driver_json.exists():
            if done_before_read:
                break
            time.sleep(0.01)
            continue
        with args.driver_json.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            offset = stream.tell()
        if not chunk:
            if done_before_read:
                break
            time.sleep(0.01)
            continue
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for payload in lines:
            require(payload and len(payload) <= 1024 * 1024, "driver record is empty or oversized")
            record = json.loads(payload.decode("utf-8"))
            require(type(record) is dict, "driver record is not an object")
            require(not saw_result, "driver record followed the terminal result")
            require(type(record.get("seq")) is int and record["seq"] == expected_seq,
                    "driver record sequence is not contiguous")
            expected_seq += 1
            require(type(record.get("monotonic_origin_ms")) is int and record["monotonic_origin_ms"] >= 0,
                    "driver record has no monotonic origin")
            require(type(record.get("monotonic_ms")) is int and record["monotonic_ms"] >= 0,
                    "driver record has no relative monotonic time")
            if origin_ms is None:
                origin_ms = record["monotonic_origin_ms"]
            require(record["monotonic_origin_ms"] == origin_ms, "driver monotonic origin changed")
            event_ns = (origin_ms + record["monotonic_ms"]) * 1_000_000
            require(last_event_ns is None or event_ns >= last_event_ns,
                    "driver monotonic timeline regressed")
            last_event_ns = event_ns
            observed_ns = monotonic_raw_ns()
            lag_ms = (observed_ns - event_ns) // 1_000_000
            require(
                MIN_OBSERVATION_LAG_MS <= lag_ms <= MAX_OBSERVATION_LAG_MS,
                "driver and guest CLOCK_MONOTONIC_RAW timestamps do not calibrate",
            )
            max_lag_ms = max(max_lag_ms, max(0, lag_ms))

            media_time = record.get("time")
            if (
                first_playback is None
                and record.get("type") == "snapshot"
                and record.get("action") == "wait_time"
                and record.get("status") == "ok"
                and type(media_time) in (int, float)
                and not isinstance(media_time, bool)
                and math.isfinite(float(media_time))
                and float(media_time) >= 0.5
                and type(record.get("source_generation")) is int
                and record["source_generation"] > 0
                and record.get("paused") is False
                and record.get("seeking") is False
            ):
                first_playback = event_ns
                first_observed = observed_ns
            if (
                record.get("type") == "snapshot"
                and record.get("action") == "seek"
                and record.get("status") == "ok"
            ):
                seek_times.append(event_ns)
            if record.get("type") == "result":
                saw_result = True
                result_count += 1

        # If the sentinel existed before this read, the producer had already
        # exited and this unbounded read reached the final EOF.
        if done_before_read:
            break

    require(producer_done_observed, "producer completion was not observed before watcher timeout")
    require(not pending.strip(), "driver JSONL ended with a partial record")
    require(saw_result, "driver result was not observed before producer completion")
    require(result_count == 1, "driver terminal result count differs")
    require(first_playback is not None and first_observed is not None, "no advancing playback record was observed")
    atomic_json(args.output, {
        "clockBasis": CLOCK_BASIS,
        "firstPlaybackMonotonicNs": first_playback,
        "firstPlaybackObservedMonotonicNs": first_observed,
        "maxObservationLagMs": max_lag_ms,
        "schema": 1,
        "seekMonotonicNs": seek_times,
    })


def load_http_payload(payload: bytes, case_id: str) -> list[dict[str, object]]:
    require(0 < len(payload) <= MAX_JSONL_BYTES, "HTTP JSONL is empty or oversized")
    require(b"\x00" not in payload and payload.endswith(b"\n"), "HTTP JSONL is not complete text")
    records = []
    for line in payload.splitlines():
        value = json.loads(line.decode("utf-8"))
        require(type(value) is dict and set(value) == HTTP_KEYS, "HTTP record keys differ")
        require(type(value["request_seq"]) is int and value["request_seq"] > 0,
                "HTTP request sequence is invalid")
        require(value["case_id"] == case_id, "HTTP record case identity differs")
        require(value["clock_basis"] == CLOCK_BASIS, "HTTP record clock basis differs")
        require(value["method"] in {"GET", "HEAD"}, "HTTP record method is invalid")
        for key in ("bytes_expected", "bytes_sent", "started_monotonic_ns", "finished_monotonic_ns", "status"):
            require(type(value[key]) is int and value[key] >= 0, f"HTTP {key} is invalid")
        require(value["finished_monotonic_ns"] >= value["started_monotonic_ns"],
                "HTTP monotonic interval regressed")
        require(value["finished_monotonic_ns"] - value["started_monotonic_ns"] <= MAX_HTTP_INTERVAL_NS,
                "HTTP monotonic interval exceeds the bounded run")
        require(type(value["disconnected"]) is bool, "HTTP disconnected flag is invalid")
        require(value["range"] is None or value["range"] == "invalid" or RANGE_RE.fullmatch(value["range"]),
                "HTTP range label is invalid")
        started_at = parse_utc_milliseconds(value["started_at"], "started_at")
        finished_at = parse_utc_milliseconds(value["finished_at"], "finished_at")
        require(finished_at >= started_at, "HTTP wall-clock interval regressed")
        require((finished_at - started_at).total_seconds() <= 600,
                "HTTP wall-clock interval exceeds the bounded run")
        require(value["status"] in range(100, 600), "HTTP status is outside the protocol range")
        require(value["bytes_sent"] <= value["bytes_expected"],
                "HTTP response sent more than its declared bytes")
        successful_body = (
            value["method"] == "GET"
            and value["status"] in {200, 206}
            and value["bytes_expected"] > 0
        )
        if successful_body:
            if value["disconnected"]:
                require(value["bytes_sent"] < value["bytes_expected"],
                        "disconnected HTTP body is not partial")
            else:
                require(value["bytes_sent"] == value["bytes_expected"],
                        "completed HTTP body differs from its declared bytes")
        else:
            require(value["bytes_sent"] == 0, "non-body HTTP response logged payload bytes")
        records.append(value)
    records.sort(key=lambda record: record["request_seq"])
    require([record["request_seq"] for record in records] == list(range(1, len(records) + 1)),
            "HTTP request sequence is not contiguous")
    return records


def load_http(path: Path, case_id: str) -> list[dict[str, object]]:
    with path.open("rb") as stream:
        payload = stream.read(MAX_JSONL_BYTES + 1)
    return load_http_payload(payload, case_id)


def validate_completion(
    path: Path,
    *,
    case_id: str,
    max_log_bytes: int,
    max_requests: int,
    http_payload: bytes,
) -> dict[str, object]:
    with path.open("rb") as stream:
        payload = stream.read(MAX_COMPLETION_BYTES + 1)
    require(0 < len(payload) <= MAX_COMPLETION_BYTES, "HTTP completion marker is empty or oversized")
    require(b"\x00" not in payload, "HTTP completion marker contains a NUL byte")
    value = json.loads(payload.decode("ascii"))
    require(type(value) is dict and set(value) == COMPLETION_KEYS,
            "HTTP completion marker keys differ")
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    require(payload == canonical, "HTTP completion marker is not canonical JSON")
    require(type(value["schema"]) is int and value["schema"] == 1,
            "unsupported HTTP completion marker schema")
    require(value["status"] == "complete", "HTTP fixture did not complete cleanly")
    require(value["caseId"] == case_id, "HTTP completion case identity differs")
    require(value["clockBasis"] == CLOCK_BASIS, "HTTP completion clock basis differs")
    for key in (
        "allocatedRequestCount", "terminalRecordCount", "rejectedConnectionCount", "logBytes"
    ):
        require(type(value[key]) is int and value[key] >= 0,
                f"HTTP completion {key} is invalid")
    require(value["allocatedRequestCount"] <= max_requests,
            "HTTP completion allocation count exceeds the configured cap")
    require(value["allocatedRequestCount"] == value["terminalRecordCount"],
            "HTTP completion has an unflushed terminal request")
    require(value["rejectedConnectionCount"] == 0,
            "HTTP fixture rejected a connection before graceful shutdown")
    require(value["logBytes"] <= max_log_bytes,
            "HTTP completion byte count exceeds the configured log budget")
    require(value["logBytes"] == len(http_payload), "HTTP completion byte count differs")
    require(type(value["logSha256"]) is str and SHA256_RE.fullmatch(value["logSha256"]),
            "HTTP completion log digest is invalid")
    require(value["logSha256"] == hashlib.sha256(http_payload).hexdigest(),
            "HTTP completion log digest differs")
    return value


def command_score(args: argparse.Namespace) -> None:
    config = stress_fixture_service.validate_config(stress_fixture_service.read_json(args.config))
    watch = stress_fixture_service.read_json(args.watch)
    require(type(watch) is dict and set(watch) == {
        "clockBasis", "firstPlaybackMonotonicNs", "firstPlaybackObservedMonotonicNs",
        "maxObservationLagMs", "schema", "seekMonotonicNs",
    }, "transport watcher keys differ")
    require(type(watch["schema"]) is int and watch["schema"] == 1, "unsupported watcher schema")
    require(watch["clockBasis"] == CLOCK_BASIS, "transport watcher clock basis differs")
    playback_ns = watch["firstPlaybackMonotonicNs"]
    observed_ns = watch["firstPlaybackObservedMonotonicNs"]
    require(type(playback_ns) is int and type(observed_ns) is int,
            "playback observation timing is invalid")
    playback_lag_ms = (observed_ns - playback_ns) // 1_000_000
    require(MIN_OBSERVATION_LAG_MS <= playback_lag_ms <= MAX_OBSERVATION_LAG_MS,
            "playback observation lag exceeds its calibration bound")
    max_observation_lag_ms = watch["maxObservationLagMs"]
    require(type(max_observation_lag_ms) is int
            and 0 <= max_observation_lag_ms <= MAX_OBSERVATION_LAG_MS,
            "maximum watcher lag exceeds its calibration bound")
    require(max_observation_lag_ms >= max(0, playback_lag_ms),
            "maximum watcher lag is inconsistent with playback observation")
    seek_times = watch["seekMonotonicNs"]
    require(type(seek_times) is list and all(type(value) is int for value in seek_times),
            "seek timing list is invalid")
    if config["minPostSeekRangeResponses"]:
        require(seek_times, "post-seek Range evidence requires a completed seek action")

    with args.http_log.open("rb") as stream:
        http_payload = stream.read(MAX_JSONL_BYTES + 1)
    require(0 < len(http_payload) <= MAX_JSONL_BYTES, "HTTP JSONL is empty or oversized")
    completion = validate_completion(
        args.service_completion,
        case_id=config["caseId"],
        max_log_bytes=config["maxLogBytes"],
        max_requests=config["maxRequests"],
        http_payload=http_payload,
    )
    records = load_http_payload(http_payload, config["caseId"])
    require(len(records) == completion["terminalRecordCount"],
            "HTTP terminal record count differs from the sealed service count")
    require(len(records) <= config["maxObservedRequests"], "HTTP request count exceeded oracle bound")
    # Redirects are protocol transitions, not HTTP failures.  Count only
    # terminal client/server statuses as errors and score redirects separately
    # from successful media bodies.
    errors = [record for record in records if record["status"] >= 400]
    redirects = [record for record in records if 300 <= record["status"] < 400]
    failed_range_mode = config["mode"] in {
        "fail-post-open-range",
        "fail-post-open-range-recovery",
    }
    failed_range_recovery_mode = config["mode"] == "fail-post-open-range-recovery"
    min_error_responses = 1 if failed_range_mode else 0
    require(len(errors) >= min_error_responses,
            "HTTP error response count is below the expected bound")
    require(len(errors) <= config["maxErrorResponses"], "HTTP error response count exceeded oracle bound")
    body = [record for record in records if record["method"] == "GET" and record["status"] in {200, 206}
            and record["bytes_expected"] > 0]
    require(body, "no successful body response was recorded")
    # A no-Range origin may echo the request's sanitized Range label while
    # correctly returning 200.  Only a successful 206 response is Range proof.
    ranges = [record for record in body if record["status"] == 206
              and type(record["range"]) is str and RANGE_RE.fullmatch(record["range"])]
    post_seek = [record for record in ranges if range_start(record) not in {None, 0} and any(
        record["started_monotonic_ns"] >= seek_ns for seek_ns in seek_times
    )]
    require(len(post_seek) >= config["minPostSeekRangeResponses"], "post-seek Range response count is too small")

    if failed_range_mode:
        require(config["failStatus"] == 503 and config["failCount"] == 1,
                "failed-Range qualification requires one configured 503")
        require(config["maxErrorResponses"] == 1,
                "failed-Range qualification requires an exact one-error bound")
        require(len(seek_times) == 1,
                "failed-Range qualification requires exactly one completed seek action")
        require(len(errors) == 1,
                "failed-Range qualification requires exactly one HTTP error response")
        failure = errors[0]
        require(failure["method"] == "GET" and failure["status"] == 503,
                "failed-Range terminal response is not an exact GET 503")
        match = (
            RANGE_RE.fullmatch(failure["range"])
            if type(failure["range"]) is str
            else None
        )
        require(
            match is not None
            and int(match.group(1)) > 0
            and int(match.group(2)) >= int(match.group(1)),
            "failed-Range terminal GET has no valid nonzero byte Range",
        )
        seek_ns = seek_times[0]
        require(failure["started_monotonic_ns"] >= seek_ns,
                "failed-Range terminal GET was not issued after the seek")
        successful_before_failure = [
            record for record in body
            if record["started_monotonic_ns"] >= seek_ns
            and record["request_seq"] < failure["request_seq"]
        ]
        require(not successful_before_failure,
                "a successful media response preceded the failed-Range result")

        if failed_range_recovery_mode:
            recovery_body = [
                record for record in body
                if record["request_seq"] > failure["request_seq"]
            ]
            require(recovery_body,
                    "failed-Range recovery produced no later healthy media response")
            require(all(
                record["started_monotonic_ns"] >= failure["finished_monotonic_ns"]
                for record in recovery_body
            ), "healthy recovery response overlapped the failed source")
        else:
            require(failure["request_seq"] == records[-1]["request_seq"],
                    "a media request followed the terminal failed-Range GET")
            require(not any(
                record["started_monotonic_ns"] >= seek_ns
                for record in body
            ), "a successful media response retried the failed seek")

    repetitions = Counter(record["range"] for record in ranges)
    max_repeat = max(repetitions.values(), default=0)
    require(max_repeat <= config["maxRangeRepeats"], "identical Range response count indicates a retry storm")

    full_bodies = [record for record in body if record["status"] == 200]
    if config["mode"] == "no-range":
        require(not ranges, "no-Range fixture produced a satisfiable Range response")
        require(len(full_bodies) == 1,
                "no-Range playback started a duplicate full-body transfer")
    elif config["mode"] == "redirect":
        require(1 <= len(redirects) <= 4,
                "fixed redirect was absent or entered a redirect retry loop")
        require(len(full_bodies) <= 1,
                "fixed redirect started duplicate full-body transfers")
    else:
        require(not redirects, "nonredirect fixture produced a redirect response")

    role = config["transportRole"]
    progressive = role == "streaming-first-qualification"
    startup_ms = None
    tail_ms = None
    initial_complete = None
    initial_expected = None
    initial_sent = None
    initial_request_seq = None
    initial_request_range = None
    if progressive:
        # Bind progressive playback to the first successful media body.  A
        # later redundant or retry Range request cannot keep the socket open
        # and turn a completed download-before-play run into a false pass.
        initial = body[0]
        require(initial["started_monotonic_ns"] <= playback_ns < initial["finished_monotonic_ns"],
                "playback did not advance during the first successful media response")
        startup_ms = (playback_ns - initial["started_monotonic_ns"]) // 1_000_000
        tail_ms = (initial["finished_monotonic_ns"] - playback_ns) // 1_000_000
        require(startup_ms <= config["maxStartupMs"], "progressive startup exceeded its request-relative bound")
        require(tail_ms >= config["minTransferTailMs"],
                "first successful response ended too soon after playback began")
        initial_expected = initial["bytes_expected"]
        initial_sent = initial["bytes_sent"]
        initial_complete = not initial["disconnected"]
        initial_request_seq = initial["request_seq"]
        initial_request_range = initial["range"]

    atomic_json(args.output, {
        "bodyResponseCount": len(body),
        "clockBasis": CLOCK_BASIS,
        "errorResponseCount": len(errors),
        "firstPlaybackObservationLagMs": playback_lag_ms,
        "getRequestCount": sum(record["method"] == "GET" for record in records),
        "initialResponseBytesExpected": initial_expected,
        "initialResponseBytesSent": initial_sent,
        "initialResponseCompleted": initial_complete,
        "initialRequestRange": initial_request_range,
        "initialRequestSequence": initial_request_seq,
        "maxAllowedErrorResponses": config["maxErrorResponses"],
        "maxRangeRepeatCount": max_repeat,
        "maxWatcherObservationLagMs": max_observation_lag_ms,
        "minExpectedErrorResponses": min_error_responses,
        "minExpectedPostSeekRangeResponses": config["minPostSeekRangeResponses"],
        "postSeekRangeResponseCount": len(post_seek),
        "progressiveScored": progressive,
        "rangeResponseCount": len(ranges),
        "requestCount": len(records),
        "role": role,
        "schema": 1,
        "startupAfterRequestMs": startup_ms,
        "status": "passed",
        "transferTailAfterPlaybackMs": tail_ms,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser("watch")
    watch.add_argument("--driver-json", required=True, type=Path)
    watch.add_argument("--output", required=True, type=Path)
    watch.add_argument("--producer-done", required=True, type=Path)
    watch.add_argument("--timeout-seconds", type=int, choices=range(1, 601), default=330)
    watch.set_defaults(function=command_watch)
    score = subparsers.add_parser("score")
    score.add_argument("--http-log", required=True, type=Path)
    score.add_argument("--service-completion", required=True, type=Path)
    score.add_argument("--watch", required=True, type=Path)
    score.add_argument("--config", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.set_defaults(function=command_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (TransportError, stress_fixture_service.FixtureError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(f"transport oracle: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
