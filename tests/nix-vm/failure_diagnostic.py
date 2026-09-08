#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Classify ephemeral launcher logs without reproducing their contents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


MAX_FILES = 64
MAX_ENTRIES = 256
MAX_DEPTH = 4
MAX_HEAD_BYTES = 256 * 1024
MAX_TAIL_BYTES = 768 * 1024
MAX_FILE_SAMPLE_BYTES = MAX_HEAD_BYTES + MAX_TAIL_BYTES
MAX_TOTAL_SAMPLE_BYTES = 4 * 1024 * 1024
MAX_SIGNAL_LINES = 3
MAX_SIGNAL_TOKENS = 32

PRESSURE_VESSEL_COMPONENTS = {
    b"pressure-vessel-unruntime",
    b"pressure-vessel-wrap",
    b"pv-adverb",
    b"srt-bwrap",
}
STEAM_RUNTIME_COMPONENTS = {
    b"steam-runtime-launch-client",
    b"steam-runtime-launcher-interface-0",
    b"steam-runtime-supervisor",
    b"x86_64-linux-gnu-srt-launcher-service",
}

SAFE_TOKENS = {
    "argument", "bind", "bwrap", "cannot", "cgroup", "child", "command",
    "container", "create", "denied", "dev", "directory", "error", "executable",
    "execute", "exit", "failed", "failure", "file", "filesystem", "fuse", "gid", "home",
    "invalid", "ipc", "ldconfig", "library", "map", "missing", "mount", "namespace",
    "network", "no", "not", "open", "operation", "permitted", "permission", "pid",
    "platform", "pressure-vessel", "proc", "python", "read-only", "run", "runtime",
    "setuid", "shared", "socket", "status", "supported", "symlink", "sys", "timeout",
    "tmp", "traceback", "uid",
    "unshare", "user", "userns", "uts", "variable", "wine", "wrap",
}


class DiagnosticError(ValueError):
    pass


def regular_files(
    console: Path,
    runtime_log_dir: Path,
    proton_log: Path | None = None,
) -> list[Path]:
    files: list[Path] = []

    def add_regular(path: Path) -> None:
        if path.is_file() and not path.is_symlink() and path not in files:
            files.append(path)
            if len(files) > MAX_FILES:
                raise DiagnosticError("diagnostic input file limit exceeded")

    # Give the two exact caller-selected files priority over a verbose runtime
    # directory so neither can be starved by the aggregate sampling budget.
    add_regular(console)
    if proton_log is not None:
        add_regular(proton_log)
    if runtime_log_dir.is_dir() and not runtime_log_dir.is_symlink():
        stack = [(runtime_log_dir, 0)]
        entries_seen = 0
        while stack:
            directory, depth = stack.pop()
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda candidate: candidate.name):
                    entries_seen += 1
                    if entries_seen > MAX_ENTRIES:
                        raise DiagnosticError("diagnostic input entry limit exceeded")
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        add_regular(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False):
                        if depth >= MAX_DEPTH:
                            raise DiagnosticError("diagnostic input depth limit exceeded")
                        stack.append((Path(entry.path), depth + 1))
    return files


def has_all(line: bytes, *tokens: bytes) -> bool:
    return all(token in line for token in tokens)


def safe_words(line: bytes) -> list[str]:
    words: list[str] = []
    current = bytearray()
    for value in line.lower():
        if 97 <= value <= 122 or 48 <= value <= 57 or value == 45:
            current.append(value)
        elif current:
            word = current.decode("ascii")
            if word in SAFE_TOKENS:
                words.append(word)
            current.clear()
    if current:
        word = current.decode("ascii")
        if word in SAFE_TOKENS:
            words.append(word)
    return words


def component_record(line: bytes) -> tuple[bytes, bytes] | None:
    """Return a structurally parsed SLR component record, if present."""

    close = line.find(b"]: ")
    if close >= 0:
        opened = line.rfind(b"[", 0, close)
        if opened >= 0 and line[opened + 1:close].isdigit():
            prefix = line[:opened].rstrip()
            fields = prefix.rsplit(None, 1)
            if not fields:
                return None
            component = fields[-1].rsplit(b"/", 1)[-1]
            return component, line[close + 3:].lstrip()

    # Older/synthetic diagnostics sometimes omit the PID. Keep this exact to
    # known components so a component name in a wrapped command is not enough.
    known_components = PRESSURE_VESSEL_COMPONENTS | STEAM_RUNTIME_COMPONENTS | {
        b"_v2-entry-point",
    }
    for component in known_components:
        marker = component + b": "
        position = line.find(marker)
        if position < 0:
            continue
        prefix = line[:position].rstrip()
        if prefix.endswith(component):
            return component, line[position + len(marker):].lstrip()
    return None


def is_missing_executable_or_library(line: bytes) -> bool:
    if b"cannot open shared object file" in line:
        return True
    if b"error while loading shared libraries" in line:
        return True
    if b"command not found" in line:
        return True
    missing = b"no such file or directory" in line or b"not found" in line
    execution = any(token in line for token in (
        b"execve", b"execvp", b"execv", b"failed to execute", b"unable to execute",
    ))
    return missing and execution


def sample_file(path: Path, allowance: int) -> tuple[bytes, bool]:
    """Read a bounded launch-oriented head/tail sample from a regular file."""

    size = path.stat().st_size
    take = min(size, MAX_FILE_SAMPLE_BYTES, allowance)
    with path.open("rb") as stream:
        if size <= take:
            return stream.read(take), False
        if take <= 1:
            return stream.read(take), True

        # Preserve both startup and terminal evidence. For a full-sized sample
        # this is 256 KiB of head and the remaining budget from the tail.
        head_take = min(MAX_HEAD_BYTES, take // 2)
        tail_take = take - head_take - 1
        head = stream.read(head_take)
        stream.seek(size - tail_take)
        tail = stream.read(tail_take)
        return head + b"\n" + tail, True


def classify_lines(
    payload: bytes,
    process_exit: int | None = None,
) -> tuple[list[str], list[str]]:
    codes: set[str] = set()
    signals: list[str] = []
    for raw_line in payload.splitlines():
        line = raw_line.lower()
        line_codes: set[str] = set()
        record = component_record(line)
        component = record[0] if record is not None else None
        message = record[1] if record is not None else b""

        if has_all(line, b"coreutils", b"unrecognized option", b"--signal"):
            line_codes.add("timeout-multicall-dispatch-failed")

        pressure_vessel_error = (
            component in PRESSURE_VESSEL_COMPONENTS and message.startswith(b"e:")
        )
        steam_runtime_error = (
            component in STEAM_RUNTIME_COMPONENTS and message.startswith(b"e:")
        ) or (
            component == b"_v2-entry-point" and message.startswith(b"error:")
        )
        if pressure_vessel_error:
            line_codes.add("pressure-vessel-launch-failed")
        if steam_runtime_error:
            line_codes.add("steam-runtime-launch-failed")

        namespace_denied = (b"bwrap" in line or b"namespace" in line) and (
            b"operation not permitted" in line or b"permission denied" in line
        )
        direct_bwrap_failure = (
            record is None
            and b"bwrap:" in line
            and b"namespace" in line
            and b"failed" in line
        )
        sandbox_denied = namespace_denied and (
            pressure_vessel_error or direct_bwrap_failure
        )
        if sandbox_denied:
            line_codes.add("sandbox-namespace-denied")
            line_codes.add("pressure-vessel-launch-failed")

        nonfatal_runtime_record = record is not None and message.startswith((
            b"d:", b"i:", b"w:",
        ))
        if is_missing_executable_or_library(line) and not nonfatal_runtime_record:
            line_codes.add("missing-executable-or-library")
        if b"permission denied" in line and (
            pressure_vessel_error or steam_runtime_error or sandbox_denied
        ):
            line_codes.add("permission-denied")
        if b"traceback (most recent call last)" in line:
            line_codes.add("python-launcher-traceback")
        if b"modulenotfounderror:" in line or b"importerror:" in line:
            line_codes.add("python-module-missing")
        if b"wine:" in line and any(token in line for token in (
            b"could not load", b"could not exec", b"failed to load", b"failed to open",
        )):
            line_codes.add("wine-load-failed")

        if (
            component == b"pv-adverb"
            and message.startswith(b"i: command exited with status ")
        ):
            status = message.removeprefix(b"i: command exited with status ").strip()
            if status.isdigit() and int(status) != 0:
                line_codes.add("wrapped-command-nonzero")

        codes.update(line_codes)
        if line_codes and len(signals) < MAX_SIGNAL_LINES:
            words = safe_words(line)[:MAX_SIGNAL_TOKENS]
            signal = " ".join(words)
            if signal and signal not in signals:
                signals.append(signal)

    if process_exit == 124:
        codes.add("process-timeout")
    if not codes:
        codes.add("unclassified-nonzero-exit")
    return sorted(codes), signals


def classify(
    console: Path,
    runtime_log_dir: Path,
    proton_log: Path | None = None,
    process_exit: int | None = None,
) -> dict[str, object]:
    if process_exit is not None and not 0 <= process_exit <= 255:
        raise DiagnosticError("process exit status is out of range")
    files = regular_files(console, runtime_log_dir, proton_log)
    samples: list[bytes] = []
    truncated = False
    remaining = MAX_TOTAL_SAMPLE_BYTES
    for path in files:
        if remaining == 0:
            truncated = True
            break
        if samples:
            # Account for the separator inserted below as part of the total
            # byte budget, rather than allowing one extra byte per source.
            remaining -= 1
            if remaining == 0:
                truncated = True
                break
        sample, file_truncated = sample_file(path, remaining)
        truncated = truncated or file_truncated
        samples.append(sample)
        remaining -= len(sample)
    payload = b"\n".join(samples)
    codes, signals = classify_lines(payload, process_exit)
    return {
        "codes": codes,
        "sampleTruncated": truncated,
        "schema": 1,
        "signals": signals,
        "sourceFileCount": len(files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", required=True, type=Path)
    parser.add_argument("--runtime-log-dir", required=True, type=Path)
    parser.add_argument("--proton-log", type=Path)
    parser.add_argument("--process-exit", type=int)
    try:
        args = parser.parse_args(argv)
        print(json.dumps(classify(
            args.console,
            args.runtime_log_dir,
            proton_log=args.proton_log,
            process_exit=args.process_exit,
        ), sort_keys=True, separators=(",", ":")))
    except (DiagnosticError, OSError):
        print("failure diagnostic unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
