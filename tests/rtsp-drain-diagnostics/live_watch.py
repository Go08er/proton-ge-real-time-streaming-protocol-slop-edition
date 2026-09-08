#!/usr/bin/env python3
"""Compact local monitor for an A3.16 cooperative VRChat session."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile
import time
from typing import BinaryIO, Callable


FIELD_RE = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
WINE_ISSUE_RE = re.compile(r":(warn|err):(dmo|mfplat):", re.IGNORECASE)
MTX_SESSION_RE = re.compile(r"\[session ([^\]]+)\]")
QUEUE_EVENTS = {
    "queue_enter",
    "queue_leave",
    "queue_change",
    "queue_wait",
    "progress",
}
REASON_FIELDS = (
    ("idle", "IDLE"),
    ("normal_packets", "NPKT"),
    ("normal_bytes", "NBYTE"),
    ("emergency_packets", "EPKT"),
    ("emergency_bytes", "EBYTE"),
)


def parse_fields(line: str) -> dict[str, str]:
    """Return every whitespace-delimited key=value token, in any order."""
    return dict(FIELD_RE.findall(line))


def number(value: str | None, default: int = 0) -> int:
    try:
        return int(value, 0) if value is not None else default
    except ValueError:
        return default


def kib(value: str | None) -> str:
    return f"{number(value) / 1024:.0f}KiB"


def safe_token(value: str | None, default: str = "?") -> str:
    if value is None:
        return default
    return re.sub(r"[^A-Za-z0-9_.#-]", "?", value)[:32] or default


@dataclass
class Source:
    source_id: str
    backend_attempt: int | None = None
    started_at: float | None = None
    streams: dict[str, dict[str, str]] = field(default_factory=dict)
    reasons_seen: set[frozenset[str]] = field(default_factory=set)
    clears_seen: set[frozenset[str]] = field(default_factory=set)
    current_reason: frozenset[str] = frozenset()
    emergency_recovery: bool = False
    summary_seen: bool = False
    stale_reported: bool = False
    final_reported: bool = False


class Monitor:
    """Turn three local logs into bounded, privacy-safe state changes."""

    def __init__(
        self,
        emit: Callable[[str], None] | None = None,
        *,
        unresolved_after: float = 10.0,
    ) -> None:
        self.emit = emit or (lambda line: print(line, flush=True))
        self.unresolved_after = unresolved_after
        self.backend_attempts = 0
        self.sources: dict[str, Source] = {}
        self.source_order: list[Source] = []
        self.source_summaries = 0
        self.sar_summaries = 0
        self.sar_in_generation = 0
        self.wine_issues: dict[str, int] = {}
        self.mtx_ready = False
        self.mtx_publish_total = 0
        self.mtx_read_total = 0
        self.mtx_roles: dict[str, str] = {}
        self.mtx_issues = 0
        self.ffmpeg_notices = 0
        self.ffmpeg_errors = 0

    @staticmethod
    def _milestone(count: int) -> bool:
        return count <= 3 or count == 10 or count % 50 == 0

    def file_event(
        self,
        kind: str,
        event: str,
        capture_name: str | None = None,
    ) -> None:
        label = {"publisher": "ffmpeg"}.get(kind, kind)
        capture = f" capture={capture_name}" if capture_name else ""
        if event == "opened":
            self.emit(f"LOG {label} open{capture}")
            return

        self.emit(f"LOG {label} {event}{capture}")
        if kind == "proton":
            if event in {"recreated", "truncated"}:
                self._finalize_unresolved(f"log {event}", stopping=False)
                self.sources.clear()
                self.source_order.clear()
                self.sar_in_generation = 0
        elif kind == "mediamtx":
            self.mtx_ready = False
            self.mtx_publish_total = 0
            self.mtx_read_total = 0
            self.mtx_roles.clear()
            self.mtx_issues = 0
        elif kind == "publisher":
            self.ffmpeg_notices = 0
            self.ffmpeg_errors = 0

    def feed(self, kind: str, line: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if kind == "proton":
            self._feed_proton(line, now)
        elif kind == "mediamtx":
            self._feed_mediamtx(line)
        elif kind == "publisher":
            self._feed_publisher(line)

    def _source(self, fields: dict[str, str]) -> Source | None:
        source_id = fields.get("source_id")
        if source_id is None:
            return None
        if source_id not in self.sources:
            self.sources[source_id] = Source(source_id)
        return self.sources[source_id]

    @staticmethod
    def _reasons(fields: dict[str, str]) -> list[str]:
        return [
            label
            for key, label in REASON_FIELDS
            if number(fields.get(key))
        ]

    def _source_label(self, source: Source | None) -> str:
        if source is None or source.backend_attempt is None:
            return "B--"
        return f"B{source.backend_attempt:02d}"

    def _feed_proton(self, line: str, now: float) -> None:
        if "trace:rtspdrain:" not in line:
            match = WINE_ISSUE_RE.search(line)
            if match:
                key = f"{match.group(1).lower()}:{match.group(2).lower()}"
                self.wine_issues[key] = self.wine_issues.get(key, 0) + 1
                count = self.wine_issues[key]
                if self._milestone(count):
                    self.emit(f"WINE {key} #{count} (inspect local log)")
            return

        fields = parse_fields(line)
        event = fields.get("event")
        if event is None:
            return
        source = self._source(fields)

        if event == "source_start":
            if source is None:
                return
            if source.backend_attempt is None:
                self.backend_attempts += 1
                source.backend_attempt = self.backend_attempts
                source.started_at = now
                self.source_order.append(source)
            self.emit(
                f"{self._source_label(source)} START src={safe_token(source.source_id)} "
                f"streams={safe_token(fields.get('streams'))} "
                f"tcp={safe_token(fields.get('forced_tcp'))}"
            )
            return

        if event in QUEUE_EVENTS:
            if source is None:
                return
            reasons = frozenset(self._reasons(fields))
            previous = source.current_reason
            source.current_reason = reasons
            removed_emergency = bool({"EPKT", "EBYTE"} & previous) and not bool(
                {"EPKT", "EBYTE"} & reasons
            )
            if removed_emergency:
                source.emergency_recovery = True
            recovery_chain = (
                source.emergency_recovery
                and reasons != previous
            )
            new_reason = bool(reasons) and reasons not in source.reasons_seen
            recovered = (
                not reasons
                and bool(previous)
                and previous not in source.clears_seen
            )
            source.reasons_seen.add(reasons)
            if recovered:
                source.clears_seen.add(previous)
            emergency = bool({"EPKT", "EBYTE"} & reasons)
            if (
                event in {"queue_wait", "progress"}
                or emergency
                or new_reason
                or recovered
                or recovery_chain
            ):
                bounds = "+".join(sorted(reasons)) if reasons else "none"
                self.emit(
                    f"{self._source_label(source)} {event.upper()} "
                    f"src={safe_token(source.source_id)} "
                    f"q={safe_token(fields.get('queued_packets'), '0')}p/"
                    f"{kib(fields.get('queued_bytes'))} bound={bounds} "
                    f"packets={safe_token(fields.get('completed_packets'), '0')}"
                )
            if source.emergency_recovery and not reasons:
                source.emergency_recovery = False
            return

        if event == "read_result":
            if source is None:
                return
            pressure = "+".join(sorted(self._reasons(fields))) or "none"
            self.emit(
                f"{self._source_label(source)} READ src={safe_token(source.source_id)} "
                f"status={safe_token(fields.get('status'))} "
                f"time={safe_token(fields.get('elapsed_ms'), '0')}ms "
                f"q={safe_token(fields.get('queued_packets'), '0')}p/"
                f"{kib(fields.get('queued_bytes'))} bound={pressure} "
                f"age={safe_token(fields.get('last_packet_age_ms'), '0')}ms"
            )
            return

        if event == "stream_summary":
            if source is None:
                return
            stream_id = fields.get("stream", f"unknown-{len(source.streams)}")
            source.streams[stream_id] = fields
            return

        if event == "source_summary":
            if source is None or source.summary_seen:
                return
            source.summary_seen = True
            source.final_reported = True
            self.source_summaries += 1
            video = self._stream_totals(source, video=True)
            audio = self._stream_totals(source, video=False)
            delivered = self._delivery_label(video, audio)
            live = safe_token(fields.get("live_records"), "0")
            if number(fields.get("live_exhausted")):
                live += "/capped"
            self.emit(
                f"{self._source_label(source)} END src={safe_token(source.source_id)} "
                f"delivered={delivered} "
                f"v={video[1]}/{video[0]} a={audio[1]}/{audio[0]} "
                f"reads={safe_token(fields.get('read_calls'), '0')} "
                f"packets={safe_token(fields.get('completed_packets'), '0')} "
                f"peak={safe_token(fields.get('high_packets'), '0')}p/"
                f"{kib(fields.get('high_bytes'))} live={live}"
            )
            return

        if event == "sar_summary":
            self._sar_summary(fields)
            return

        self.emit(
            f"{self._source_label(source)} EVENT {safe_token(event)} "
            f"src={safe_token(fields.get('source_id'))}"
        )

    @staticmethod
    def _stream_totals(source: Source, *, video: bool) -> tuple[int, int]:
        selected = [
            fields
            for fields in source.streams.values()
            if bool(number(fields.get("video"))) is video
        ]
        return (
            sum(number(fields.get("requests")) for fields in selected),
            sum(number(fields.get("delivered")) for fields in selected),
        )

    @staticmethod
    def _delivery_label(
        video: tuple[int, int],
        audio: tuple[int, int],
    ) -> str:
        video_flow = video[1] > 0
        audio_flow = audio[1] > 0
        if video_flow and audio_flow:
            return "A+V"
        if video_flow:
            return "V"
        if audio_flow:
            return "A"
        if video[0] or audio[0]:
            return "NONE"
        return "NO_DEMAND"

    def _sar_summary(self, fields: dict[str, str]) -> None:
        self.sar_summaries += 1
        ordinal = self.sar_in_generation
        self.sar_in_generation += 1
        source = (
            self.source_order[ordinal]
            if ordinal < len(self.source_order)
            else None
        )
        clock_calls = number(fields.get("clock_start_calls"))
        clock_successes = number(fields.get("clock_start_successes"))
        client_calls = number(fields.get("audio_client_start_calls"))
        client_successes = number(fields.get("audio_client_start_successes"))
        callbacks = number(fields.get("render_callbacks"))
        pending = number(fields.get("start_pending"))

        if not clock_calls:
            verdict = "CLOCK_NEVER"
        elif not clock_successes:
            verdict = "CLOCK_FAIL"
        elif not client_calls:
            verdict = "CLIENT_NOT_CALLED"
        elif not client_successes:
            verdict = "CLIENT_FAIL"
        elif not callbacks:
            verdict = "NO_RENDER"
        else:
            verdict = "STARTED"
        if pending:
            verdict += "+PENDING"

        self.emit(
            f"SAR id={safe_token(fields.get('sar_id'))} "
            f"n={self.sar_summaries} ~{self._source_label(source)} "
            f"audio={verdict} clock={clock_successes}/{clock_calls} "
            f"client={client_successes}/{client_calls} "
            f"samples={safe_token(fields.get('process_samples'), '0')} "
            f"pre={safe_token(fields.get('preclock_samples'), '0')} "
            f"callbacks={safe_token(fields.get('render_callbacks'), '0')} "
            f"q={safe_token(fields.get('queued_frames'), '0')}/"
            f"{safe_token(fields.get('peak_queued_frames'), '0')} "
            f"state={safe_token(fields.get('clock_state'))} pending={pending}"
        )

    def _feed_mediamtx(self, line: str) -> None:
        lowered = line.lower()
        session_match = MTX_SESSION_RE.search(line)
        session = session_match.group(1) if session_match else None

        if "listener opened" in lowered and "[rtsp]" in lowered:
            if not self.mtx_ready:
                self.mtx_ready = True
                self.emit("MTX READY")
        if "is publishing to path" in lowered:
            self.mtx_publish_total += 1
            if session:
                self.mtx_roles[session] = "publish"
            self.emit(
                f"MTX PUBLISH total={self.mtx_publish_total} "
                f"active={sum(role == 'publish' for role in self.mtx_roles.values())}"
            )
        if "is reading from path" in lowered:
            self.mtx_read_total += 1
            if session:
                self.mtx_roles[session] = "read"
            self.emit(
                f"MTX READ total={self.mtx_read_total} "
                f"active={sum(role == 'read' for role in self.mtx_roles.values())}"
            )
        if session and ("destroyed:" in lowered or "closed:" in lowered):
            role = self.mtx_roles.pop(session, None)
            if role:
                self.emit(
                    f"MTX {role.upper()}_END "
                    f"active={sum(item == role for item in self.mtx_roles.values())}"
                )
        if re.search(r"\b(?:err|error|war|warning)\b", lowered):
            self.mtx_issues += 1
            if self._milestone(self.mtx_issues):
                self.emit(f"MTX ISSUE #{self.mtx_issues} (inspect local log)")

    def _feed_publisher(self, line: str) -> None:
        if not line.strip():
            return
        lowered = line.lower()
        if re.match(r"^\s*frame=\s*\d+", line):
            return
        if re.search(
            r"\b(?:error|failed|reset|broken pipe|timed out|timeout|refused)\b",
            lowered,
        ):
            self.ffmpeg_errors += 1
            count = self.ffmpeg_errors
            label = "ERROR"
        else:
            self.ffmpeg_notices += 1
            count = self.ffmpeg_notices
            label = "LOG"
        if self._milestone(count):
            self.emit(f"FFMPEG {label} #{count} (inspect local log)")

    def tick(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for source in self.source_order:
            if (
                source.started_at is not None
                and not source.summary_seen
                and not source.stale_reported
                and now - source.started_at >= self.unresolved_after
            ):
                source.stale_reported = True
                self.emit(
                    f"{self._source_label(source)} OPEN "
                    f"src={safe_token(source.source_id)} "
                    f"source_summary pending after {self.unresolved_after:g}s"
                )

    def _finalize_unresolved(self, reason: str, *, stopping: bool) -> None:
        for source in self.source_order:
            if not source.summary_seen and not source.final_reported:
                source.final_reported = True
                label = "OPEN_AT_STOP" if stopping else "UNRESOLVED"
                self.emit(
                    f"{self._source_label(source)} {label} "
                    f"src={safe_token(source.source_id)} ({reason})"
                )

    def finish(self) -> None:
        self._finalize_unresolved("watch end", stopping=True)
        wine = ",".join(
            f"{key}={count}" for key, count in sorted(self.wine_issues.items())
        ) or "0"
        self.emit(
            f"STOP backend={self.backend_attempts} "
            f"summaries={self.source_summaries} sar={self.sar_summaries} "
            f"mtx_reads={self.mtx_read_total} "
            f"ffmpeg_log={self.ffmpeg_notices} "
            f"ffmpeg_errors={self.ffmpeg_errors} "
            f"wine_issues={wine}"
        )


@dataclass
class PollResult:
    before_transition: list[str] = field(default_factory=list)
    transition: str | None = None
    after_transition: list[str] = field(default_factory=list)


class FollowedLog:
    """Follow one source while preserving each file generation locally."""

    def __init__(self, kind: str, path: Path, capture_dir: Path) -> None:
        self.kind = kind
        self.path = path
        self.capture_dir = capture_dir
        self.handle: BinaryIO | None = None
        self.capture: BinaryIO | None = None
        self.capture_path: Path | None = None
        self.identity: tuple[int, int] | None = None
        self.partial = b""
        self.anchor = b""
        self.ever_opened = False
        self.generation = 0
        self.path_gone = False

    def _open(self) -> None:
        self.handle = self.path.open("rb", buffering=0)
        stat = os.fstat(self.handle.fileno())
        self.identity = (stat.st_dev, stat.st_ino)
        self.partial = b""
        self.anchor = b""
        self.generation += 1
        self.capture_path = (
            self.capture_dir / f"{self.kind}-{self.generation:02d}.log"
        )
        fd = os.open(
            self.capture_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self.capture = os.fdopen(fd, "wb", buffering=0)
        self.ever_opened = True
        self.path_gone = False

    def _close(self) -> None:
        if self.handle:
            self.handle.close()
        if self.capture:
            self.capture.close()
        self.handle = None
        self.capture = None
        self.capture_path = None
        self.identity = None
        self.partial = b""
        self.anchor = b""

    def _anchor_matches(self) -> bool:
        if not self.handle or not self.anchor:
            return True
        position = self.handle.tell()
        if position < len(self.anchor):
            return False
        self.handle.seek(position - len(self.anchor))
        observed = self.handle.read(len(self.anchor))
        self.handle.seek(position)
        return observed == self.anchor

    def _read(self) -> list[str]:
        if not self.handle:
            return []
        data = self.handle.read()
        if not data:
            return []
        assert self.capture is not None
        self.capture.write(data)
        self.anchor = (self.anchor + data)[-64:]
        chunks = (self.partial + data).split(b"\n")
        self.partial = chunks.pop()
        return [
            chunk.decode("utf-8", errors="replace") for chunk in chunks
        ]

    def poll(self) -> PollResult:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self.handle is not None:
                old_lines = self._read()
                if not self.path_gone:
                    self.path_gone = True
                    return PollResult(old_lines, "gone", [])
                return PollResult(before_transition=old_lines)
            return PollResult()

        identity = (stat.st_dev, stat.st_ino)
        if self.handle is None:
            event = "recreated" if self.ever_opened else "opened"
            try:
                self._open()
            except FileNotFoundError:
                return PollResult()
            return PollResult(
                transition=event,
                after_transition=self._read(),
            )
        elif identity != self.identity:
            old_lines = self._read()
            self._close()
            try:
                self._open()
            except FileNotFoundError:
                return PollResult(old_lines, "gone", [])
            return PollResult(
                old_lines,
                "recreated",
                self._read(),
            )
        elif stat.st_size < self.handle.tell() or not self._anchor_matches():
            self._close()
            try:
                self._open()
            except FileNotFoundError:
                return PollResult(transition="gone")
            return PollResult(
                transition="truncated",
                after_transition=self._read(),
            )

        self.path_gone = False
        return PollResult(after_transition=self._read())

    def flush_partial(self) -> list[str]:
        if not self.partial:
            return []
        line = self.partial.decode("utf-8", errors="replace")
        self.partial = b""
        return [line]

    def close(self) -> None:
        self._close()


def parser() -> argparse.ArgumentParser:
    downloads = Path.home() / "Downloads"
    result = argparse.ArgumentParser(
        description="Follow local A3.16 Proton, MediaMTX, and FFmpeg logs.",
    )
    result.add_argument(
        "--proton-log",
        type=Path,
        default=downloads / "steam-438100.log",
    )
    result.add_argument(
        "--mediamtx-log",
        type=Path,
        default=downloads / "a316-mediamtx.log",
    )
    result.add_argument(
        "--publisher-log",
        type=Path,
        default=downloads / "a316-ffmpeg.log",
    )
    result.add_argument(
        "--once",
        action="store_true",
        help="read current files once, report unresolved sources, and exit",
    )
    result.add_argument(
        "--capture-dir",
        type=Path,
        help="new private directory for preserved raw log generations",
    )
    result.add_argument("--poll", type=float, default=0.25)
    result.add_argument("--unresolved-after", type=float, default=10.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.poll <= 0 or args.unresolved_after < 0:
        raise SystemExit("--poll must be positive and --unresolved-after nonnegative")

    if args.capture_dir:
        capture_dir = args.capture_dir.expanduser()
        capture_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        downloads = Path.home() / "Downloads"
        capture_dir = Path(
            tempfile.mkdtemp(
                prefix=time.strftime("a316-coop-%Y%m%d-%H%M%S-"),
                dir=downloads,
            )
        )
    os.chmod(capture_dir, 0o700)

    monitor = Monitor(unresolved_after=args.unresolved_after)
    logs = [
        FollowedLog("mediamtx", args.mediamtx_log.expanduser(), capture_dir),
        FollowedLog("publisher", args.publisher_log.expanduser(), capture_dir),
        FollowedLog("proton", args.proton_log.expanduser(), capture_dir),
    ]
    print(f"CAPTURE {capture_dir}", flush=True)
    print(
        "WATCH "
        + " ".join(f"{log.kind}={log.path.name}" for log in logs),
        flush=True,
    )

    def consume(log: FollowedLog) -> None:
        result = log.poll()
        for line in result.before_transition:
            monitor.feed(log.kind, line)
        if result.transition:
            capture_name = log.capture_path.name if log.capture_path else None
            monitor.file_event(log.kind, result.transition, capture_name)
        for line in result.after_transition:
            monitor.feed(log.kind, line)

    try:
        while True:
            for log in logs:
                consume(log)
            monitor.tick()
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        for log in logs:
            consume(log)
            for line in log.flush_partial():
                monitor.feed(log.kind, line)
            log.close()

    monitor.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
