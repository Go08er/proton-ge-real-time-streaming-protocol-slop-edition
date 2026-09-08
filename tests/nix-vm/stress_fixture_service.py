#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Validate and launch the bounded loopback fixture used by the stress VM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from urllib.parse import urlsplit


MAX_DOCUMENT_BYTES = 1024 * 1024
CONFIG_KEYS = {
    "bind",
    "caseId",
    "chunkBytes",
    "failCount",
    "failStatus",
    "fixtureRelativePath",
    "headerDelaySeconds",
    "maxConcurrent",
    "maxErrorResponses",
    "maxLogBytes",
    "maxObservedRequests",
    "maxRangeRepeats",
    "maxRequests",
    "maxStartupMs",
    "minPostSeekRangeResponses",
    "minTransferTailMs",
    "mode",
    "port",
    "rateKiB",
    "schema",
    "service",
    "stallAfterBytes",
    "stallSeconds",
    "truncateAfterBytes",
    "transportRole",
}
MODES = {
    "range",
    "no-range",
    "redirect",
    "fail-first",
    "fail-post-open-range",
    "fail-post-open-range-recovery",
    "truncate",
    "chunked",
    "stall",
}
FAIL_STATUSES = {404, 408, 429, 500, 502, 503, 504}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FINITE_NONNEGATIVE_RE = re.compile(r"^\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
GE_RUNNING_SEEK_SUPPRESSION_SECONDS = 3.0
MAX_SCENARIO_ACTIONS = 100_000


class FixtureError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FixtureError(message)


def read_json(path: Path) -> object:
    payload = path.read_bytes()
    require(len(payload) <= MAX_DOCUMENT_BYTES, "JSON input is too large")
    require(b"\x00" not in payload, "JSON input contains a NUL byte")
    return json.loads(payload.decode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_relative(value: str) -> PurePosixPath:
    require(type(value) is str and value != "", "fixtureRelativePath must be nonempty text")
    require("\\" not in value and "\x00" not in value, "fixtureRelativePath is not canonical")
    relative = PurePosixPath(value)
    require(not relative.is_absolute(), "fixtureRelativePath must be relative")
    require(str(relative) == value, "fixtureRelativePath is not normalized")
    require(all(part not in {"", ".", ".."} for part in relative.parts), "fixtureRelativePath traverses")
    return relative


def positive_integer(value: object, label: str, maximum: int) -> int:
    require(type(value) is int and 1 <= value <= maximum, f"{label} is outside [1, {maximum}]")
    return value


def finite_number(value: object, label: str, lower: float, upper: float) -> float:
    require(type(value) in (int, float) and not isinstance(value, bool), f"{label} must be numeric")
    number = float(value)
    require(number == number and number not in (float("inf"), float("-inf")), f"{label} must be finite")
    require(lower <= number <= upper, f"{label} is outside [{lower}, {upper}]")
    return number


def validate_config(value: object) -> dict[str, object]:
    require(type(value) is dict, "service config must be an object")
    require(set(value) == CONFIG_KEYS, "service config keys differ from schema 1")
    require(type(value["schema"]) is int and value["schema"] == 1, "unsupported service schema")
    require(value["service"] == "progressive-http-v1", "unsupported fixture service")
    require(value["bind"] == "127.0.0.1", "fixture must bind IPv4 loopback")
    require(value["mode"] in MODES, "unsupported progressive HTTP mode")
    require(type(value["caseId"]) is str and CASE_ID_RE.fullmatch(value["caseId"]), "invalid caseId")
    normalized_relative(value["fixtureRelativePath"])
    positive_integer(value["port"], "port", 65535)
    require(type(value["rateKiB"]) is int and 0 <= value["rateKiB"] <= 1024 * 1024, "rateKiB is invalid")
    positive_integer(value["chunkBytes"], "chunkBytes", 16 * 1024 * 1024)
    positive_integer(value["failCount"], "failCount", 64)
    require(type(value["failStatus"]) is int and value["failStatus"] in FAIL_STATUSES, "failStatus is invalid")
    positive_integer(value["truncateAfterBytes"], "truncateAfterBytes", 4 * 1024 * 1024 * 1024)
    positive_integer(value["stallAfterBytes"], "stallAfterBytes", 4 * 1024 * 1024 * 1024)
    finite_number(value["stallSeconds"], "stallSeconds", 0, 300)
    finite_number(value["headerDelaySeconds"], "headerDelaySeconds", 0, 300)
    max_requests = positive_integer(value["maxRequests"], "maxRequests", 4096)
    positive_integer(value["maxConcurrent"], "maxConcurrent", 64)
    max_log_bytes = positive_integer(value["maxLogBytes"], "maxLogBytes", 64 * 1024 * 1024)
    require(max_log_bytes >= max_requests * 512, "maxLogBytes does not reserve 512 bytes per request")
    max_observed = positive_integer(value["maxObservedRequests"], "maxObservedRequests", max_requests)
    require(max_observed <= max_requests, "maxObservedRequests exceeds the service request cap")
    require(type(value["maxErrorResponses"]) is int and 0 <= value["maxErrorResponses"] <= max_observed,
            "maxErrorResponses is invalid")
    positive_integer(value["maxRangeRepeats"], "maxRangeRepeats", max_observed)
    require(type(value["minPostSeekRangeResponses"]) is int
            and 0 <= value["minPostSeekRangeResponses"] <= max_observed,
            "minPostSeekRangeResponses is invalid")
    require(type(value["maxStartupMs"]) is int and 1 <= value["maxStartupMs"] <= 60_000,
            "maxStartupMs is invalid")
    require(type(value["minTransferTailMs"]) is int and 0 <= value["minTransferTailMs"] <= 60_000,
            "minTransferTailMs is invalid")
    require(value["transportRole"] in {"media-engine-diagnostic", "streaming-first-qualification"},
            "transportRole is invalid")
    if value["transportRole"] == "streaming-first-qualification":
        require(value["minTransferTailMs"] >= 10_000,
                "streaming-first qualification needs at least ten seconds of transfer tail")
    return value


def validate_fixture_set(root: Path, manifest: Path, config: dict[str, object]) -> tuple[Path, float]:
    root = root.resolve(strict=True)
    require(root.is_dir(), "fixture bytes input must be a directory")
    manifest = manifest.resolve(strict=True)
    require(manifest == root / "provenance/manifest.json", "fixture manifest is not the root manifest")
    require(manifest.is_file(), "fixture manifest is not a regular file")

    ready = read_json(root / "READY")
    require(type(ready) is dict and set(ready) == {"manifest_sha256", "profile", "schema", "sha256sums_sha256"},
            "fixture READY keys differ")
    require(type(ready["schema"]) is int and ready["schema"] == 1, "unsupported fixture READY schema")
    require(ready["profile"] in {"smoke", "full"}, "unsupported fixture profile")
    for key in ("manifest_sha256", "sha256sums_sha256"):
        require(type(ready[key]) is str and SHA256_RE.fullmatch(ready[key]), f"invalid READY {key}")
    require(sha256_file(manifest) == ready["manifest_sha256"], "fixture manifest hash differs from READY")

    sums_path = root / "SHA256SUMS"
    require(sha256_file(sums_path) == ready["sha256sums_sha256"], "SHA256SUMS hash differs from READY")
    sums: dict[str, str] = {}
    lines = sums_path.read_text(encoding="utf-8").splitlines()
    require(1 <= len(lines) <= 4096, "SHA256SUMS entry count is outside bounds")
    for line in lines:
        require(len(line) >= 67 and line[64:66] == "  ", "malformed SHA256SUMS line")
        digest, name = line[:64], line[66:]
        require(SHA256_RE.fullmatch(digest) is not None, "invalid SHA256SUMS digest")
        relative = normalized_relative(name)
        require(name not in sums, "duplicate SHA256SUMS path")
        target = (root / Path(*relative.parts)).resolve(strict=True)
        require(target.is_file() and target.is_relative_to(root), "SHA256SUMS target escaped fixture root")
        require(sha256_file(target) == digest, "fixture byte hash differs from SHA256SUMS")
        sums[name] = digest

    fixture_relative = str(normalized_relative(config["fixtureRelativePath"]))
    require(fixture_relative in sums, "configured fixture is absent from SHA256SUMS")
    fixture = (root / Path(*PurePosixPath(fixture_relative).parts)).resolve(strict=True)

    document = read_json(manifest)
    require(type(document) is dict and type(document.get("artifacts")) is list, "fixture manifest has no artifacts")
    matches = [item for item in document["artifacts"] if type(item) is dict and item.get("path") == fixture_relative]
    require(len(matches) == 1, "configured fixture has no unique manifest artifact")
    require(matches[0].get("sha256") == sums[fixture_relative], "fixture manifest byte hash differs")
    require(matches[0].get("bytes") == fixture.stat().st_size, "fixture manifest byte size differs")
    ffprobe = matches[0].get("ffprobe")
    require(type(ffprobe) is dict and ffprobe.get("status") == "accepted", "fixture has no accepted probe report")
    probe_relative = str(normalized_relative(ffprobe.get("path")))
    require(probe_relative in sums, "fixture probe report is absent from SHA256SUMS")
    require(ffprobe.get("sha256") == sums[probe_relative], "fixture probe report hash differs")
    probe = read_json(root / Path(*PurePosixPath(probe_relative).parts))
    require(type(probe) is dict and type(probe.get("format")) is dict, "fixture probe report has no format")
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise FixtureError("fixture probe duration is invalid") from error
    require(duration == duration and 0.5 <= duration <= 3600, "fixture duration is outside bounds")
    return fixture, duration


def expanded_scenario_commands(text: str) -> list[tuple[int, list[str]]]:
    """Return the bounded command stream needed by static scenario contracts."""
    parsed: list[tuple[int, list[str]]] = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise FixtureError(f"scenario line {number} has invalid quoting") from error
        if words:
            parsed.append((number, words))

    def expand(position: int, nested: bool, depth: int) -> tuple[list[tuple[int, list[str]]], int]:
        output: list[tuple[int, list[str]]] = []
        while position < len(parsed):
            number, words = parsed[position]
            command = words[0].lower()
            if command == "endloop":
                require(nested and len(words) == 1, f"unexpected endloop at line {number}")
                return output, position + 1
            if command == "loop":
                require(len(words) == 2 and words[1].isdigit(),
                        f"loop at line {number} has no canonical count")
                count = int(words[1])
                require(1 <= count <= 10_000, f"loop at line {number} is outside [1, 10000]")
                require(depth < 32, f"loop at line {number} exceeds nesting depth 32")
                body, position = expand(position + 1, True, depth + 1)
                require(len(output) + len(body) * count <= MAX_SCENARIO_ACTIONS,
                        "expanded scenario exceeds the action limit")
                output.extend(body * count)
                continue
            output.append((number, words))
            require(len(output) <= MAX_SCENARIO_ACTIONS,
                    "expanded scenario exceeds the action limit")
            position += 1
        require(not nested, "scenario has an unterminated loop")
        return output, position

    commands, _ = expand(0, False, 0)
    return commands


def validate_seek_event_contract(text: str) -> None:
    """Reject SEEKED waits for running seeks GE intentionally suppresses."""
    running = False
    clock: float | None = None
    pending_seek: tuple[int, float, bool, float | None] | None = None

    for number, words in expanded_scenario_commands(text):
        command = words[0].lower()
        if command == "play":
            running = True
        elif command == "pause":
            running = False
        elif command == "wait_time" and len(words) >= 2 \
                and FINITE_NONNEGATIVE_RE.fullmatch(words[1]):
            clock = float(words[1])
        elif command == "wait_seek_settled" and len(words) >= 2 \
                and FINITE_NONNEGATIVE_RE.fullmatch(words[1]):
            # A successful settle observation proves the final queued seek is
            # idle at this target, regardless of how many earlier requests
            # the backend physically completed.
            clock = float(words[1])
            pending_seek = None
        elif command == "wait_ms" and len(words) >= 2 and words[1].isdigit():
            if running and clock is not None:
                clock += int(words[1]) / 1000.0
        elif command == "seek" and len(words) >= 2 \
                and FINITE_NONNEGATIVE_RE.fullmatch(words[1]):
            pending_seek = (number, float(words[1]), running, clock)
        elif command == "wait_event" and len(words) >= 2:
            event = words[1].upper()
            if event == "PLAYING":
                running = True
            elif event == "PAUSE":
                running = False
            elif event == "SEEKED" and pending_seek is not None:
                seek_line, target, was_running, origin = pending_seek
                if was_running:
                    require(origin is not None,
                            f"running seek at line {seek_line} waits for SEEKED without a clock anchor")
                    require(abs(target - origin) > GE_RUNNING_SEEK_SUPPRESSION_SECONDS,
                            f"running seek at line {seek_line} waits for SEEKED inside GE's "
                            f"{GE_RUNNING_SEEK_SUPPRESSION_SECONDS:.1f}-second suppression window")
                clock = target
                pending_seek = None


def validate_scenario(
    path: Path,
    port: int,
    fixture_duration: float,
    mode: str = "range",
) -> None:
    payload = path.read_bytes()
    require(len(payload) <= MAX_DOCUMENT_BYTES, "scenario is too large")
    require(b"\x00" not in payload, "scenario contains a NUL byte")
    text = payload.decode("utf-8")
    validate_seek_event_contract(text)
    sources: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        source_command = re.match(r"^\s*(?:load|replace)\b", line, flags=re.IGNORECASE)
        if not source_command:
            continue
        match = re.match(
            r'^\s*(?:load|replace)\s+"([^"\\]*(?:\\.[^"\\]*)*)"(?:\s*(?:#.*)?)?$',
            line,
            flags=re.IGNORECASE,
        )
        require(match is not None, f"scenario source at line {number} is not canonical quoted syntax")
        raw = match.group(1).replace(r'\"', '"').replace(r'\\', '\\')
        parsed = urlsplit(raw)
        require(
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == port
            and parsed.path in {"/media", "/recovery"}
            and parsed.username is None
            and parsed.password is None
            and parsed.fragment == "",
            f"scenario source at line {number} is not the declared loopback fixture",
        )
        command = source_command.group(0).strip().lower()
        if parsed.path == "/recovery":
            require(
                mode == "fail-post-open-range-recovery" and command == "replace",
                f"scenario recovery source at line {number} is not a recovery-mode replacement",
            )
            require(
                any(route == "/media" for _prior_command, route in sources),
                f"scenario recovery source at line {number} precedes the failing media source",
            )
        sources.append((command, parsed.path))
    for number, line in enumerate(text.splitlines(), 1):
        command = re.match(
            r"^\s*(seek|wait_time|wait_seek_settled)\b",
            line,
            flags=re.IGNORECASE,
        )
        if not command:
            continue
        body = line.split("#", 1)[0].split()
        command_name = command.group(1).lower()
        expected_parts = {
            "seek": 2,
            "wait_time": 3,
            "wait_seek_settled": 4,
        }[command_name]
        require(len(body) == expected_parts, f"scenario media-time command at line {number} is not canonical")
        require(FINITE_NONNEGATIVE_RE.fullmatch(body[1]) is not None,
                f"scenario media-time target at line {number} is not a finite nonnegative number")
        value = float(body[1])
        require(
            value <= fixture_duration - 0.25,
            f"scenario media-time target at line {number} exceeds the fixture duration margin",
        )
        if command_name == "wait_seek_settled":
            require(
                FINITE_NONNEGATIVE_RE.fullmatch(body[2]) is not None
                and float(body[2]) <= 60.0,
                f"scenario seek-settle tolerance at line {number} is outside [0, 60]",
            )
            require(
                body[3].isdigit() and int(body[3]) <= 3_600_000,
                f"scenario seek-settle timeout at line {number} is outside [0, 3600000]",
            )
    require(sources, "scenario contains no quoted load or replace source")
    if mode == "fail-post-open-range-recovery":
        require(
            sources[0] == ("load", "/media"),
            "failed-Range recovery scenario must first load the failing /media route",
        )
        require(
            any(command == "replace" and route == "/recovery" for command, route in sources),
            "failed-Range recovery scenario has no healthy /recovery replacement",
        )
    else:
        require(
            all(route == "/media" for _command, route in sources),
            "non-recovery scenario uses the reserved /recovery route",
        )


def load_and_validate(args: argparse.Namespace) -> tuple[dict[str, object], Path]:
    config = validate_config(read_json(args.config))
    fixture, duration = validate_fixture_set(args.fixture_root, args.fixture_manifest, config)
    validate_scenario(args.scenario, config["port"], duration, str(config["mode"]))
    return config, fixture


def command_check(args: argparse.Namespace) -> None:
    config, _fixture = load_and_validate(args)
    print(config["port"])


def command_serve(args: argparse.Namespace) -> None:
    config, fixture = load_and_validate(args)
    fixture_script = args.fixture_script.resolve(strict=True)
    require(fixture_script.is_file(), "progressive HTTP implementation is missing")
    arguments = [
        sys.executable,
        os.fspath(fixture_script),
        "--file", os.fspath(fixture),
        "--mode", str(config["mode"]),
        "--case-id", str(config["caseId"]),
        "--bind-scope", "loopback",
        "--host", "127.0.0.1",
        "--port", str(config["port"]),
        "--rate-kib", str(config["rateKiB"]),
        "--chunk-bytes", str(config["chunkBytes"]),
        "--fail-count", str(config["failCount"]),
        "--fail-status", str(config["failStatus"]),
        "--truncate-after", str(config["truncateAfterBytes"]),
        "--stall-after", str(config["stallAfterBytes"]),
        "--stall-seconds", str(config["stallSeconds"]),
        "--header-delay-seconds", str(config["headerDelaySeconds"]),
        "--max-requests", str(config["maxRequests"]),
        "--max-concurrent", str(config["maxConcurrent"]),
        "--max-log-bytes", str(config["maxLogBytes"]),
        "--log", os.fspath(args.log),
        "--completion-marker", os.fspath(args.completion_marker),
    ]
    os.execv(sys.executable, arguments)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--fixture-manifest", required=True, type=Path)
    parser.add_argument("--scenario", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    add_common(check)
    check.set_defaults(function=command_check)
    serve = subparsers.add_parser("serve")
    add_common(serve)
    serve.add_argument("--fixture-script", required=True, type=Path)
    serve.add_argument("--log", required=True, type=Path)
    serve.add_argument("--completion-marker", required=True, type=Path)
    serve.set_defaults(function=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
    except (FixtureError, json.JSONDecodeError, OSError, UnicodeError, ValueError) as error:
        print(f"stress fixture service: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
