#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Deterministic localhost HTTP media fixture for progressive-playback tests."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Callable, TextIO


CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)\Z", re.IGNORECASE)
MEDIA_ROUTE = "/media"
RECOVERY_ROUTE = "/recovery"
REDIRECT_TARGET_ROUTE = "/redirect-target"
CLOCK_BASIS = "linux-clock-monotonic-raw-v1"
COMPLETION_SCHEMA = 1
HANDLER_CANCEL_GRACE_SECONDS = 0.5
STOPPER_JOIN_SECONDS = 2.0


def monotonic_raw_ns() -> int:
    """Return the same absolute monotonic clock Wine uses for GetTickCount64."""

    if not hasattr(time, "CLOCK_MONOTONIC_RAW"):
        raise RuntimeError("CLOCK_MONOTONIC_RAW is unavailable")
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


class RangeNotSatisfiable(ValueError):
    """Raised when a single HTTP byte range cannot be served."""


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def label(self) -> str:
        return f"bytes={self.start}-{self.end}"


@dataclass(frozen=True)
class ResponsePlan:
    status: int
    offset: int = 0
    declared_bytes: int = 0
    range_label: str | None = None
    content_range: str | None = None
    accept_ranges: str | None = None
    location: str | None = None
    truncate_after: int | None = None
    chunked: bool = False


@dataclass(frozen=True)
class TransferResult:
    bytes_sent: int
    disconnected: bool


def parse_byte_range(value: str, size: int) -> ByteRange:
    """Parse one RFC 7233-style byte range and clamp its end to the file."""

    if size <= 0 or "," in value:
        raise RangeNotSatisfiable(value)

    match = RANGE_RE.fullmatch(value.strip())
    if not match:
        raise RangeNotSatisfiable(value)

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise RangeNotSatisfiable(value)

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise RangeNotSatisfiable(value)
        suffix_length = min(suffix_length, size)
        return ByteRange(size - suffix_length, size - 1)

    start = int(start_text)
    if start >= size:
        raise RangeNotSatisfiable(value)

    end = size - 1 if not end_text else int(end_text)
    if end < start:
        raise RangeNotSatisfiable(value)
    return ByteRange(start, min(end, size - 1))


def safe_range_label(value: str | None, size: int) -> str | None:
    """Return only a canonical range or the literal ``invalid`` for logging."""

    if value is None:
        return None
    try:
        return parse_byte_range(value, size).label
    except (RangeNotSatisfiable, ValueError):
        return "invalid"


class FailureGate:
    """Thread-safe counter used by the fail-first compatibility mode."""

    def __init__(self, failure_count: int) -> None:
        self._remaining = failure_count
        self._lock = threading.Lock()

    def should_fail(self) -> bool:
        with self._lock:
            if not self._remaining:
                return False
            self._remaining -= 1
            return True


class PostOpenRangeGate:
    """Allow the first media GET, then reject later Range GETs."""

    def __init__(self) -> None:
        self._opened = False
        self._lock = threading.Lock()

    def should_fail(self, *, method: str, range_requested: bool) -> bool:
        """Arm on the first valid body GET and reject later Range GETs."""

        if method != "GET":
            return False
        with self._lock:
            if not self._opened:
                self._opened = True
                return False
            return range_requested


def route_path(request_target: str) -> str:
    """Strip query and fragment data without ever retaining it for logging."""

    return request_target.partition("?")[0].partition("#")[0]


def make_response_plan(
    *,
    mode: str,
    request_target: str,
    range_header: str | None,
    file_size: int,
    method: str = "GET",
    failure_gate: FailureGate | None = None,
    post_open_range_gate: PostOpenRangeGate | None = None,
    truncate_after: int | None = None,
    failure_status: int = 503,
) -> ResponsePlan:
    """Create a response plan without performing file or socket I/O."""

    path = route_path(request_target)
    range_label = safe_range_label(range_header, file_size)

    if mode == "redirect" and path == MEDIA_ROUTE:
        return ResponsePlan(status=307, range_label=range_label, location=REDIRECT_TARGET_ROUTE)
    if mode == "fail-post-open-range-recovery":
        if path not in (MEDIA_ROUTE, RECOVERY_ROUTE):
            return ResponsePlan(status=404, range_label=range_label)
    elif path not in (MEDIA_ROUTE, REDIRECT_TARGET_ROUTE):
        return ResponsePlan(status=404, range_label=range_label)
    if mode != "redirect" and path != MEDIA_ROUTE:
        if not (mode == "fail-post-open-range-recovery" and path == RECOVERY_ROUTE):
            return ResponsePlan(status=404, range_label=range_label)

    if mode == "fail-first" and failure_gate is not None and failure_gate.should_fail():
        return ResponsePlan(status=failure_status, range_label=range_label)

    if mode == "no-range":
        return ResponsePlan(
            status=200,
            declared_bytes=file_size,
            range_label=range_label,
            accept_ranges="none",
        )

    if range_header is None:
        if (
            mode in {"fail-post-open-range", "fail-post-open-range-recovery"}
            and path == MEDIA_ROUTE
            and post_open_range_gate is not None
        ):
            post_open_range_gate.should_fail(method=method, range_requested=False)
        return ResponsePlan(
            status=200,
            declared_bytes=file_size,
            range_label=None,
            accept_ranges="bytes",
            truncate_after=truncate_after if mode == "truncate" else None,
            chunked=mode == "chunked",
        )

    try:
        byte_range = parse_byte_range(range_header, file_size)
    except (RangeNotSatisfiable, ValueError):
        return ResponsePlan(
            status=416,
            range_label="invalid",
            content_range=f"bytes */{file_size}",
            accept_ranges="bytes",
        )

    if (
        mode in {"fail-post-open-range", "fail-post-open-range-recovery"}
        and path == MEDIA_ROUTE
        and post_open_range_gate is not None
        and post_open_range_gate.should_fail(method=method, range_requested=True)
    ):
        return ResponsePlan(
            status=503,
            range_label=byte_range.label,
            accept_ranges="bytes",
        )

    return ResponsePlan(
        status=206,
        offset=byte_range.start,
        declared_bytes=byte_range.length,
        range_label=byte_range.label,
        content_range=f"bytes {byte_range.start}-{byte_range.end}/{file_size}",
        accept_ranges="bytes",
        truncate_after=truncate_after if mode == "truncate" else None,
        chunked=mode == "chunked",
    )


def transfer_body(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    byte_count: int,
    chunk_bytes: int,
    bytes_per_second: int,
    truncate_after: int | None = None,
    stall_after: int | None = None,
    stall_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
    cancel_event: threading.Event | None = None,
) -> TransferResult:
    """Copy a bounded body with an injectable, cancellable throttle clock."""

    def cancelled_wait(delay: float) -> bool:
        if cancel_event is not None:
            return cancel_event.wait(delay)
        sleeper(delay)
        return False

    transfer_limit = byte_count
    deliberate_disconnect = False
    if truncate_after is not None and truncate_after < byte_count:
        transfer_limit = max(0, truncate_after)
        deliberate_disconnect = True

    bytes_sent = 0
    stalled = False
    try:
        while bytes_sent < transfer_limit:
            if cancel_event is not None and cancel_event.is_set():
                return TransferResult(bytes_sent=bytes_sent, disconnected=True)
            if stall_after is not None and not stalled and bytes_sent >= stall_after:
                if cancelled_wait(stall_seconds):
                    return TransferResult(bytes_sent=bytes_sent, disconnected=True)
                stalled = True
            read_size = min(chunk_bytes, transfer_limit - bytes_sent)
            if stall_after is not None and not stalled:
                read_size = min(read_size, stall_after - bytes_sent)
            data = source.read(read_size)
            if not data:
                deliberate_disconnect = bytes_sent < byte_count
                break
            destination.write(data)
            destination.flush()
            bytes_sent += len(data)
            if bytes_sent < transfer_limit and bytes_per_second:
                if cancelled_wait(len(data) / bytes_per_second):
                    return TransferResult(bytes_sent=bytes_sent, disconnected=True)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return TransferResult(bytes_sent=bytes_sent, disconnected=True)

    return TransferResult(
        bytes_sent=bytes_sent,
        disconnected=deliberate_disconnect or bytes_sent < byte_count,
    )


class ChunkedWriter:
    """Encode payload writes as one valid HTTP/1.1 chunk each."""

    def __init__(self, destination: BinaryIO) -> None:
        self._destination = destination

    def write(self, data: bytes) -> int:
        self._destination.write(f"{len(data):X}\r\n".encode("ascii"))
        self._destination.write(data)
        self._destination.write(b"\r\n")
        return len(data)

    def flush(self) -> None:
        self._destination.flush()

    def finish(self) -> None:
        self._destination.write(b"0\r\n\r\n")
        self._destination.flush()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_completion_marker(
    path: Path,
    *,
    case_id: str,
    log_path: Path,
    allocated_requests: int,
    terminal_records: int,
    rejected_connections: int,
) -> None:
    """Atomically seal the final, already-closed JSONL byte sequence."""

    log_bytes = log_path.stat().st_size
    value = {
        "allocatedRequestCount": allocated_requests,
        "caseId": case_id,
        "clockBasis": CLOCK_BASIS,
        "logBytes": log_bytes,
        "logSha256": sha256_file(log_path),
        "rejectedConnectionCount": rejected_connections,
        "schema": COMPLETION_SCHEMA,
        "status": "complete",
        "terminalRecordCount": terminal_records,
    }
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class SafeJsonlLogger:
    """Write a strict allow-list record containing no request target or headers."""

    def __init__(self, stream: TextIO, case_id: str, max_bytes: int = 8 * 1024 * 1024) -> None:
        if not CASE_ID_RE.fullmatch(case_id):
            raise ValueError("case ID must be 1-64 safe ASCII characters")
        if max_bytes <= 0:
            raise ValueError("log byte budget must be greater than zero")
        self._stream = stream
        self._case_id = case_id
        self._max_bytes = max_bytes
        try:
            position = stream.tell()
        except (AttributeError, OSError):
            position = 0
        if position < 0 or position > max_bytes:
            raise ValueError("existing sanitized log exceeds its byte budget")
        self._bytes_written = position
        self._record_count = 0
        self._lock = threading.Lock()

    @property
    def bytes_written(self) -> int:
        with self._lock:
            return self._bytes_written

    @property
    def record_count(self) -> int:
        with self._lock:
            return self._record_count

    def record(
        self,
        *,
        method: str,
        status: int,
        range_label: str | None,
        bytes_expected: int,
        bytes_sent: int,
        started_at: str,
        finished_at: str,
        disconnected: bool,
        request_seq: int,
        started_monotonic_ns: int,
        finished_monotonic_ns: int,
    ) -> None:
        if (
            type(started_monotonic_ns) is not int
            or type(finished_monotonic_ns) is not int
            or not 0 <= started_monotonic_ns <= finished_monotonic_ns
        ):
            raise ValueError("monotonic request interval is invalid")
        record = {
            "bytes_expected": bytes_expected,
            "bytes_sent": bytes_sent,
            "case_id": self._case_id,
            "clock_basis": CLOCK_BASIS,
            "disconnected": disconnected,
            "finished_at": finished_at,
            "finished_monotonic_ns": finished_monotonic_ns,
            "method": method,
            "range": range_label,
            "request_seq": request_seq,
            "started_at": started_at,
            "started_monotonic_ns": started_monotonic_ns,
            "status": status,
        }
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            if self._bytes_written + len(encoded) > self._max_bytes:
                raise RuntimeError("sanitized fixture log byte budget exhausted")
            self._stream.write(encoded.decode("utf-8"))
            self._stream.flush()
            self._bytes_written += len(encoded)
            self._record_count += 1


@dataclass(frozen=True)
class FixtureConfig:
    media_file: Path
    mode: str
    logger: SafeJsonlLogger
    failure_gate: FailureGate
    post_open_range_gate: PostOpenRangeGate
    truncate_after: int | None
    chunk_bytes: int
    bytes_per_second: int
    content_type: str
    failure_status: int
    header_delay_seconds: float
    stall_after: int | None
    stall_seconds: float
    max_requests: int
    max_concurrent: int
    monotonic_ns: Callable[[], int] = monotonic_raw_ns


class FixtureServer(ThreadingHTTPServer):
    # A successful completion marker is written only after server_close() has
    # joined every admitted request thread.  Daemon threads would make that
    # marker capable of racing a late terminal JSONL record.
    daemon_threads = False
    block_on_close = True

    def __init__(self, server_address: tuple[str, int], config: FixtureConfig) -> None:
        self.fixture = config
        self._request_lock = threading.Lock()
        self._next_request = 1
        self._connection_slots = threading.BoundedSemaphore(config.max_concurrent)
        self.stop_requested = threading.Event()
        self._connections_changed = threading.Condition()
        self._active_connections: set[socket.socket] = set()
        self._terminal_sequences: set[int] = set()
        self._rejected_connections = 0
        self._fatal_error = False
        super().__init__(server_address, FixtureRequestHandler)

    def allocate_request(self) -> tuple[int, bool] | None:
        """Allocate one bounded request and identify the final admitted request."""

        with self._request_lock:
            if self.stop_requested.is_set() or self._next_request > self.fixture.max_requests:
                return None
            sequence = self._next_request
            self._next_request += 1
            return sequence, sequence == self.fixture.max_requests

    def request_stop(self) -> None:
        """Idempotently stop admission and wake cancellable request delays."""

        self.stop_requested.set()

    def stop_and_cancel_connections(self) -> None:
        """Stop accepting and bound request-thread shutdown.

        Cancellable delays get one short grace interval to write their own
        terminal record.  Sockets still active after that interval are shut
        down to wake a blocked read or write before server_close() joins them.
        """

        self.stop_requested.wait()
        deadline = time.monotonic() + HANDLER_CANCEL_GRACE_SECONDS
        with self._connections_changed:
            while self._active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._connections_changed.wait(remaining)
            active = tuple(self._active_connections)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # BaseServer.shutdown() must run outside the serve_forever() thread.
        self.shutdown()

    def note_terminal_record(self, request_seq: int) -> None:
        with self._request_lock:
            if request_seq in self._terminal_sequences:
                self._fatal_error = True
            self._terminal_sequences.add(request_seq)

    def note_fatal_error(self) -> None:
        with self._request_lock:
            self._fatal_error = True

    def completion_counts(self) -> tuple[int, int, int, bool]:
        with self._request_lock:
            return (
                self._next_request - 1,
                len(self._terminal_sequences),
                self._rejected_connections,
                self._fatal_error,
            )

    def _reject_connection(self, request: object, *, count: bool) -> None:
        if count:
            with self._request_lock:
                self._rejected_connections += 1
        self.shutdown_request(request)  # type: ignore[arg-type]

    def process_request(self, request: object, client_address: object) -> None:
        """Refuse excess connections before ThreadingMixIn creates a thread."""

        if not self._connection_slots.acquire(blocking=False):
            self._reject_connection(request, count=not self.stop_requested.is_set())
            return
        with self._connections_changed:
            if self.stop_requested.is_set():
                self._connection_slots.release()
                self._reject_connection(request, count=False)
                return
            self._active_connections.add(request)  # type: ignore[arg-type]
        try:
            super().process_request(request, client_address)  # type: ignore[arg-type]
        except BaseException:
            with self._connections_changed:
                self._active_connections.discard(request)  # type: ignore[arg-type]
                self._connections_changed.notify_all()
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)  # type: ignore[arg-type]
        finally:
            with self._connections_changed:
                self._active_connections.discard(request)  # type: ignore[arg-type]
                self._connections_changed.notify_all()
            self._connection_slots.release()

    def handle_error(self, request: object, client_address: object) -> None:
        # ThreadingMixIn's default includes a client address and traceback.
        self.note_fatal_error()
        print("fixture request failed; inspect the sanitized JSONL result", file=sys.stderr)


class FixtureRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ProgressiveHTTPFixture/1"
    sys_version = ""

    @property
    def fixture_server(self) -> FixtureServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler includes the raw request target in its default log.
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return

    def do_GET(self) -> None:
        self._handle_media_request("GET")

    def do_HEAD(self) -> None:
        self._handle_media_request("HEAD")

    def _range_header(self) -> str | None:
        values = self.headers.get_all("Range", failobj=[])
        if not values:
            return None
        return ",".join(values)

    def _handle_media_request(self, method: str) -> None:
        config = self.fixture_server.fixture
        allocation = self.fixture_server.allocate_request()
        if allocation is None:
            self.close_connection = True
            return
        request_seq, stop_after = allocation
        started_at = utc_timestamp()
        started_monotonic_ns = config.monotonic_ns()
        bytes_sent = 0
        disconnected = False
        plan = ResponsePlan(status=500, range_label="invalid")

        try:
            file_size = config.media_file.stat().st_size
            plan = make_response_plan(
                mode=config.mode,
                request_target=self.path,
                range_header=self._range_header(),
                file_size=file_size,
                method=method,
                failure_gate=config.failure_gate,
                post_open_range_gate=config.post_open_range_gate,
                truncate_after=config.truncate_after,
                failure_status=config.failure_status,
            )
        except OSError:
            file_size = 0
            plan = ResponsePlan(status=500, range_label=safe_range_label(self._range_header(), 0))

        try:
            if self.fixture_server.stop_requested.is_set() or (
                config.header_delay_seconds
                and self.fixture_server.stop_requested.wait(config.header_delay_seconds)
            ):
                disconnected = True
                self.close_connection = True
                return
            self.send_response(plan.status)
            if plan.chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(plan.declared_bytes))
            self.send_header("Cache-Control", "no-store")
            if plan.accept_ranges is not None:
                self.send_header("Accept-Ranges", plan.accept_ranges)
            if plan.content_range is not None:
                self.send_header("Content-Range", plan.content_range)
            if plan.location is not None:
                self.send_header("Location", plan.location)
            if plan.status in (200, 206):
                self.send_header("Content-Type", config.content_type)
            self.end_headers()

            if method == "GET" and plan.status in (200, 206) and plan.declared_bytes:
                destination: BinaryIO = ChunkedWriter(self.wfile) if plan.chunked else self.wfile
                with config.media_file.open("rb") as media:
                    media.seek(plan.offset)
                    result = transfer_body(
                        media,
                        destination,
                        byte_count=plan.declared_bytes,
                        chunk_bytes=config.chunk_bytes,
                        bytes_per_second=config.bytes_per_second,
                        truncate_after=plan.truncate_after,
                        stall_after=config.stall_after,
                        stall_seconds=config.stall_seconds,
                        cancel_event=self.fixture_server.stop_requested,
                    )
                bytes_sent = result.bytes_sent
                disconnected = result.disconnected
                if result.disconnected:
                    self.close_connection = True
                elif plan.chunked:
                    assert isinstance(destination, ChunkedWriter)
                    destination.finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            disconnected = True
            self.close_connection = True
        finally:
            finished_monotonic_ns = config.monotonic_ns()
            try:
                config.logger.record(
                    method=method,
                    status=plan.status,
                    range_label=plan.range_label,
                    bytes_expected=plan.declared_bytes,
                    bytes_sent=bytes_sent,
                    started_at=started_at,
                    finished_at=utc_timestamp(),
                    disconnected=disconnected,
                    request_seq=request_seq,
                    started_monotonic_ns=started_monotonic_ns,
                    finished_monotonic_ns=finished_monotonic_ns,
                )
                self.fixture_server.note_terminal_record(request_seq)
            except BaseException:
                self.fixture_server.note_fatal_error()
                raise
            finally:
                if stop_after:
                    self.fixture_server.request_stop()


def case_id(value: str) -> str:
    if not CASE_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("use 1-64 letters, digits, dots, underscores, or dashes")
    return value


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def bounded_nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 300:
        raise argparse.ArgumentTypeError("must be finite and between zero and 300 seconds")
    return parsed


def validate_bind_address(value: str, scope: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("bind host must be a literal IP address") from error
    if scope == "loopback" and not address.is_loopback:
        raise argparse.ArgumentTypeError("host-safe mode may bind only a loopback address")
    documentation_networks = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
    )
    if scope == "vm-private" and not any(address in network for network in documentation_networks):
        raise argparse.ArgumentTypeError("VM-private mode requires an RFC 5737 documentation address")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="media fixture to serve")
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "range",
            "no-range",
            "redirect",
            "fail-first",
            "fail-post-open-range",
            "fail-post-open-range-recovery",
            "truncate",
            "chunked",
            "stall",
        ),
    )
    parser.add_argument("--case-id", required=True, type=case_id, help="opaque test identifier")
    parser.add_argument(
        "--bind-scope",
        choices=("loopback", "vm-private"),
        default="loopback",
        help="non-loopback binding is limited to an isolated VM documentation network",
    )
    parser.add_argument("--host", default="127.0.0.1", help="literal listen address")
    parser.add_argument("--port", type=nonnegative_int, default=8765)
    parser.add_argument("--rate-kib", type=nonnegative_int, default=1024, help="KiB/s; zero disables throttling")
    parser.add_argument("--chunk-bytes", type=positive_int, default=64 * 1024)
    parser.add_argument("--fail-count", type=positive_int, default=1)
    parser.add_argument(
        "--fail-status",
        type=int,
        choices=(404, 408, 429, 500, 502, 503, 504),
        default=503,
        help="status used by fail-first mode",
    )
    parser.add_argument("--truncate-after", type=positive_int, default=256 * 1024)
    parser.add_argument("--stall-after", type=positive_int, default=256 * 1024)
    parser.add_argument("--stall-seconds", type=bounded_nonnegative_float, default=2.0)
    parser.add_argument("--header-delay-seconds", type=bounded_nonnegative_float, default=0.0)
    parser.add_argument("--max-requests", type=positive_int, default=4096)
    parser.add_argument("--max-concurrent", type=positive_int, default=16)
    parser.add_argument("--max-log-bytes", type=positive_int, default=8 * 1024 * 1024)
    parser.add_argument("--log", type=Path, help="append sanitized JSONL here (default: stdout)")
    parser.add_argument(
        "--completion-marker",
        type=Path,
        help="atomically seal a file-backed log after graceful request-thread shutdown",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        host = validate_bind_address(args.host, args.bind_scope)
    except argparse.ArgumentTypeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.max_concurrent > 64:
        print("error: --max-concurrent must not exceed 64", file=sys.stderr)
        return 2
    if args.max_log_bytes < args.max_requests * 512:
        print("error: --max-log-bytes must reserve at least 512 bytes per request", file=sys.stderr)
        return 2
    if args.completion_marker is not None:
        if args.log is None:
            print("error: --completion-marker requires --log", file=sys.stderr)
            return 2
        if args.completion_marker.resolve() == args.log.resolve():
            print("error: completion marker and HTTP log must be different files", file=sys.stderr)
            return 2
        if args.completion_marker.exists() or args.completion_marker.is_symlink():
            print("error: completion marker must be fresh", file=sys.stderr)
            return 2
        if (args.log.exists() or args.log.is_symlink()) and args.log.stat().st_size != 0:
            print("error: a sealed HTTP log must start empty", file=sys.stderr)
            return 2
    try:
        monotonic_raw_ns()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        media_file = args.file.resolve(strict=True)
        if not media_file.is_file() or not os.access(media_file, os.R_OK):
            raise OSError
        file_size = media_file.stat().st_size
    except OSError:
        print("error: --file must identify a readable regular file", file=sys.stderr)
        return 2

    if not file_size:
        print("error: --file must not be empty", file=sys.stderr)
        return 2

    truncate_after = args.truncate_after if args.mode == "truncate" else None
    if truncate_after is not None and truncate_after >= file_size:
        print("error: --truncate-after must be smaller than the media file", file=sys.stderr)
        return 2
    stall_after = args.stall_after if args.mode == "stall" else None
    if stall_after is not None and stall_after >= file_size:
        print("error: --stall-after must be smaller than the media file", file=sys.stderr)
        return 2

    content_type = mimetypes.guess_type(media_file.name)[0] or "application/octet-stream"
    log_stream: TextIO
    close_log = False
    if args.log is None:
        log_stream = sys.stdout
    else:
        try:
            log_stream = args.log.open("a", encoding="utf-8")
            close_log = True
        except OSError:
            print("error: could not open the requested log", file=sys.stderr)
            return 2

    try:
        logger = SafeJsonlLogger(log_stream, args.case_id, args.max_log_bytes)
    except ValueError as error:
        if close_log:
            log_stream.close()
        print(f"error: {error}", file=sys.stderr)
        return 2

    config = FixtureConfig(
        media_file=media_file,
        mode=args.mode,
        logger=logger,
        failure_gate=FailureGate(args.fail_count),
        post_open_range_gate=PostOpenRangeGate(),
        truncate_after=truncate_after,
        chunk_bytes=args.chunk_bytes,
        bytes_per_second=args.rate_kib * 1024,
        content_type=content_type,
        failure_status=args.fail_status,
        header_delay_seconds=args.header_delay_seconds,
        stall_after=stall_after,
        stall_seconds=args.stall_seconds,
        max_requests=args.max_requests,
        max_concurrent=args.max_concurrent,
    )

    try:
        server = FixtureServer((host, args.port), config)
    except OSError:
        if close_log:
            log_stream.close()
        print("error: could not bind the fixture server", file=sys.stderr)
        return 2

    previous_sigterm = signal.signal(
        signal.SIGTERM,
        lambda _signum, _frame: server.request_stop(),
    )
    stopper = threading.Thread(
        target=server.stop_and_cancel_connections,
        name="fixture-graceful-stopper",
        daemon=True,
    )
    stopper.start()
    host, port = server.server_address[:2]
    print(f"fixture listening on {host}:{port}; request {MEDIA_ROUTE}", file=sys.stderr)
    service_failed = False
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        server.request_stop()
    except Exception:
        server.note_fatal_error()
        service_failed = True
    finally:
        server.request_stop()
        server.server_close()
        stopper.join(STOPPER_JOIN_SECONDS)
        if stopper.is_alive():
            server.note_fatal_error()
            service_failed = True
        signal.signal(signal.SIGTERM, previous_sigterm)
        if close_log:
            try:
                log_stream.flush()
                os.fsync(log_stream.fileno())
            except OSError:
                service_failed = True
            log_stream.close()

    allocated, terminal, rejected, fatal = server.completion_counts()
    if terminal != logger.record_count or allocated != terminal:
        fatal = True
    if fatal or service_failed:
        print("fixture did not reach a complete terminal-record boundary", file=sys.stderr)
        return 2
    if args.completion_marker is not None:
        try:
            write_completion_marker(
                args.completion_marker,
                case_id=args.case_id,
                log_path=args.log,
                allocated_requests=allocated,
                terminal_records=terminal,
                rejected_connections=rejected,
            )
        except OSError:
            print("error: could not seal the HTTP fixture log", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
