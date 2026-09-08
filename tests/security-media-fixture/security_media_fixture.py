#!/usr/bin/env python3
"""Loopback-only HTTPS/HLS fixture for phase-one media security tests.

The server deliberately exposes a small, fixed route table.  It never logs a
request target, header, client address, filename, or filesystem path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, TextIO
from urllib.parse import urlsplit


BIND_HOST = "127.0.0.1"
SEGMENT_RE = re.compile(r"segment[0-9]{3}\.ts\Z")
RANGE_RE = re.compile(r"bytes=([0-9]+)-([0-9]*)\Z", re.IGNORECASE)
SAFE_CASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
LOG_KEYS = {
    "bytes_sent",
    "case_id",
    "method",
    "range",
    "resource",
    "server",
    "status",
    "timestamp",
}
SERVER_IDS = {"http", "https_valid", "https_self_signed", "https_wrong_host"}
RESOURCE_IDS = {
    "aes-hls-key",
    "aes-hls-playlist",
    "aes-hls-segment",
    "connection-error",
    "direct-mp4",
    "https-to-http-redirect",
    "invalid-child-hls-playlist",
    "method-rejected",
    "mixed-hls-playlist",
    "plain-hls-playlist",
    "plain-hls-segment",
    "unknown",
}


class FixtureStop(KeyboardInterrupt):
    """Raised by a catchable process signal so context cleanup can run."""


@contextlib.contextmanager
def cleanup_signal_guard():
    """Turn catchable termination signals into a normal Python unwind."""

    caught = tuple(
        value
        for value in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if value is not None
    )
    previous: dict[signal.Signals, object] = {}

    def stop(_signum: int, _frame: object) -> None:
        for candidate in caught:
            signal.signal(candidate, signal.SIG_IGN)
        raise FixtureStop()

    try:
        for candidate in caught:
            previous[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, stop)
    except ValueError:
        # Signal handlers are restricted to the main Python thread.  The CLI
        # and smoke test run there; library callers in workers still get the
        # surrounding TemporaryDirectory cleanup on ordinary exceptions.
        previous.clear()

    try:
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


@dataclass(frozen=True)
class Assets:
    root: Path
    ca_cert: Path
    valid_cert: Path
    valid_key: Path
    self_signed_cert: Path
    self_signed_key: Path
    wrong_host_cert: Path
    wrong_host_key: Path
    direct_mp4: Path
    plain_playlist: Path
    aes_playlist: Path
    aes_key: Path


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
class Route:
    resource: str
    content_type: str | None = None
    path: Path | None = None
    body: bytes | None = None
    redirect: str | None = None


def default_source() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / ".test-tools"
        / "fixtures"
        / "http-vod-faststart-45s.mp4"
    )


def _run_tool(command: list[str], label: str) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _stdout, _stderr = process.communicate()
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"{label} failed with exit status {process.returncode}")


def _write_private(path: Path, data: bytes | str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
    finally:
        os.close(descriptor)


def _generate_leaf(
    *,
    openssl: str,
    pki: Path,
    name: str,
    common_name: str,
    subject_alt_name: str,
) -> tuple[Path, Path]:
    key = pki / f"{name}.key"
    csr = pki / f"{name}.csr"
    cert = pki / f"{name}.crt"
    extensions = pki / f"{name}.ext"
    _write_private(
        extensions,
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName={subject_alt_name}\n",
    )
    _run_tool(
        [
            openssl,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={common_name}",
        ],
        f"OpenSSL {name} request generation",
    )
    _run_tool(
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(pki / "ca.crt"),
            "-CAkey",
            str(pki / "ca.key"),
            "-CAcreateserial",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-out",
            str(cert),
        ],
        f"OpenSSL {name} certificate signing",
    )
    csr.unlink()
    extensions.unlink()
    with contextlib.suppress(FileNotFoundError):
        (pki / "ca.srl").unlink()
    os.chmod(key, 0o600)
    os.chmod(cert, 0o600)
    return cert, key


def prepare_assets(root: Path, source: Path, *, openssl: str, ffmpeg: str) -> Assets:
    """Generate one short-lived PKI and a small direct/HLS media set."""

    root = root.resolve()
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError("the source MP4 fixture is missing")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("the generated session directory must start empty")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    pki = root / "pki"
    plain = root / "hls" / "plain"
    aes = root / "hls" / "aes"
    for directory in (pki, plain, aes):
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)

    ca_key = pki / "ca.key"
    ca_cert = pki / "ca.crt"
    _run_tool(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-sha256",
            "-subj",
            "/CN=Loopback Media Fixture Test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        "OpenSSL local CA generation",
    )
    os.chmod(ca_key, 0o600)
    os.chmod(ca_cert, 0o600)

    valid_cert, valid_key = _generate_leaf(
        openssl=openssl,
        pki=pki,
        name="localhost",
        common_name="localhost",
        subject_alt_name="DNS:localhost,IP:127.0.0.1",
    )
    wrong_host_cert, wrong_host_key = _generate_leaf(
        openssl=openssl,
        pki=pki,
        name="wrong-host",
        common_name="wrong-host.invalid",
        subject_alt_name="DNS:wrong-host.invalid",
    )

    self_signed_cert = pki / "self-signed.crt"
    self_signed_key = pki / "self-signed.key"
    _run_tool(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(self_signed_key),
            "-out",
            str(self_signed_cert),
            "-days",
            "1",
            "-sha256",
            "-subj",
            "/CN=localhost",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "extendedKeyUsage=serverAuth",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        "OpenSSL self-signed certificate generation",
    )
    os.chmod(self_signed_key, 0o600)
    os.chmod(self_signed_cert, 0o600)

    direct_mp4 = root / "direct.mp4"
    _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            "8",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(direct_mp4),
        ],
        "FFmpeg direct MP4 derivation",
    )

    _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(direct_mp4),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            str(plain / "segment%03d.ts"),
            str(plain / "index.m3u8"),
        ],
        "FFmpeg plain HLS derivation",
    )

    key = secrets.token_bytes(16)
    iv = secrets.token_hex(16)
    aes_key = aes / "key.bin"
    key_info = aes / "key-info.txt"
    _write_private(aes_key, key)
    _write_private(key_info, f"/hls/aes/key.bin\n{aes_key}\n{iv}\n")
    _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(direct_mp4),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_key_info_file",
            str(key_info),
            "-hls_segment_filename",
            str(aes / "segment%03d.ts"),
            str(aes / "index.m3u8"),
        ],
        "FFmpeg AES-128 HLS derivation",
    )
    key_info.unlink()
    ca_key.unlink()

    for path in root.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)

    return load_assets(root)


def load_assets(root: Path) -> Assets:
    root = root.resolve()
    assets = Assets(
        root=root,
        ca_cert=root / "pki" / "ca.crt",
        valid_cert=root / "pki" / "localhost.crt",
        valid_key=root / "pki" / "localhost.key",
        self_signed_cert=root / "pki" / "self-signed.crt",
        self_signed_key=root / "pki" / "self-signed.key",
        wrong_host_cert=root / "pki" / "wrong-host.crt",
        wrong_host_key=root / "pki" / "wrong-host.key",
        direct_mp4=root / "direct.mp4",
        plain_playlist=root / "hls" / "plain" / "index.m3u8",
        aes_playlist=root / "hls" / "aes" / "index.m3u8",
        aes_key=root / "hls" / "aes" / "key.bin",
    )
    for value in assets.__dict__.values():
        if isinstance(value, Path) and value != root and not value.is_file():
            raise FileNotFoundError("the generated fixture is incomplete")
    if assets.aes_key.stat().st_size != 16:
        raise RuntimeError("the generated AES-128 key is not 16 bytes")
    return assets


def parse_range(value: str | None, size: int) -> ByteRange | None:
    if value is None:
        return None
    match = RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise ValueError("invalid byte range")
    start = int(match.group(1))
    if start >= size:
        raise ValueError("unsatisfied byte range")
    end = size - 1 if not match.group(2) else int(match.group(2))
    if end < start:
        raise ValueError("invalid byte range")
    return ByteRange(start, min(end, size - 1))


def safe_range_label(value: str | None, size: int) -> str | None:
    if value is None:
        return None
    try:
        byte_range = parse_range(value, size)
    except ValueError:
        return "invalid"
    return byte_range.label if byte_range else None


def rewrite_aes_playlist(playlist: str, child_base: str) -> str:
    """Point every AES child at one fixed loopback origin."""

    lines: list[str] = []
    for line in playlist.splitlines():
        if line.startswith("#EXT-X-KEY:"):
            line = re.sub(
                r'URI="[^"]+"',
                f'URI="{child_base}/hls/aes/key.bin"',
                line,
                count=1,
            )
        elif SEGMENT_RE.fullmatch(line):
            line = f"{child_base}/hls/aes/{line}"
        lines.append(line)
    return "\n".join(lines) + "\n"


class SafeLogger:
    def __init__(self, stream: TextIO, case_id: str) -> None:
        if not SAFE_CASE_RE.fullmatch(case_id):
            raise ValueError("case ID must be 1-64 impersonal safe ASCII characters")
        self._stream = stream
        self._case_id = case_id
        self._lock = threading.Lock()

    def record(
        self,
        *,
        server: str,
        method: str,
        resource: str,
        status: int,
        bytes_sent: int,
        range_label: str | None,
    ) -> None:
        if server not in SERVER_IDS or resource not in RESOURCE_IDS:
            raise ValueError("request log label is outside the fixed allowlist")
        if status != 0 and not 100 <= status <= 599:
            raise ValueError("request log status is invalid")
        if bytes_sent < 0:
            raise ValueError("request log byte count is invalid")
        if range_label not in (None, "invalid") and not re.fullmatch(
            r"bytes=[0-9]+-[0-9]+", range_label
        ):
            raise ValueError("request log range is not canonical")
        record = {
            "bytes_sent": bytes_sent,
            "case_id": self._case_id,
            "method": method if method in ("GET", "HEAD") else "OTHER",
            "range": range_label,
            "resource": resource,
            "server": server,
            "status": status,
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
        if set(record) != LOG_KEYS:
            raise AssertionError("request log schema changed")
        with self._lock:
            self._stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._stream.flush()


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    fixture_label: str
    safe_logger: SafeLogger

    def handle_error(self, _request: object, _client_address: object) -> None:
        """Record a fixed event instead of printing a traceback or peer data."""

        self.safe_logger.record(
            server=self.fixture_label,
            method="OTHER",
            resource="connection-error",
            status=0,
            bytes_sent=0,
            range_label=None,
        )


class FixtureServers:
    """Four fixed loopback origins: HTTP and three certificate cases."""

    def __init__(self, assets: Assets, logger: SafeLogger) -> None:
        self.assets = assets
        self.logger = logger
        self._servers: dict[str, LoopbackHTTPServer] = {}
        self._threads: list[threading.Thread] = []
        self._create_server("http", None, None)
        self._create_server("https_valid", assets.valid_cert, assets.valid_key)
        self._create_server(
            "https_self_signed", assets.self_signed_cert, assets.self_signed_key
        )
        self._create_server("https_wrong_host", assets.wrong_host_cert, assets.wrong_host_key)

    def _create_server(self, label: str, cert: Path | None, key: Path | None) -> None:
        handler = self._handler_class(label)
        server = LoopbackHTTPServer((BIND_HOST, 0), handler)
        server.fixture_label = label
        server.safe_logger = self.logger
        if server.server_address[0] != BIND_HOST:
            server.server_close()
            raise RuntimeError("fixture escaped the loopback bind")
        if cert is not None and key is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=cert, keyfile=key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self._servers[label] = server

    def base(self, label: str) -> str:
        scheme = "http" if label == "http" else "https"
        port = self._servers[label].server_address[1]
        return f"{scheme}://{BIND_HOST}:{port}"

    def endpoint_document(self) -> dict[str, object]:
        valid = self.base("https_valid")
        plain = self.base("http")
        self_signed = self.base("https_self_signed")
        wrong_host = self.base("https_wrong_host")
        return {
            "bind": BIND_HOST,
            "ca_cert": str(self.assets.ca_cert),
            "endpoints": {
                "direct_http": f"{plain}/media/direct.mp4",
                "direct_https": f"{valid}/media/direct.mp4",
                "direct_self_signed_https": f"{self_signed}/media/direct.mp4",
                "direct_wrong_host_https": f"{wrong_host}/media/direct.mp4",
                "hls_aes_http": f"{plain}/hls/aes/index.m3u8",
                "hls_aes_https": f"{valid}/hls/aes/index.m3u8",
                "hls_invalid_child_https": f"{valid}/hls/invalid-child/index.m3u8",
                "hls_mixed_https": f"{valid}/hls/mixed/index.m3u8",
                "hls_plain_http": f"{plain}/hls/plain/index.m3u8",
                "hls_plain_https": f"{valid}/hls/plain/index.m3u8",
                "redirect_https_to_http": f"{valid}/redirect/http",
            },
            "schema": 1,
        }

    def _route(self, request_target: str) -> Route:
        try:
            path = urlsplit(request_target).path
        except ValueError:
            return Route("unknown")

        if path == "/media/direct.mp4":
            return Route("direct-mp4", "video/mp4", path=self.assets.direct_mp4)
        if path == "/redirect/http":
            return Route(
                "https-to-http-redirect",
                redirect=f'{self.base("http")}/media/direct.mp4',
            )
        if path == "/hls/plain/index.m3u8":
            return Route(
                "plain-hls-playlist",
                "application/vnd.apple.mpegurl",
                path=self.assets.plain_playlist,
            )
        if path == "/hls/aes/index.m3u8":
            return Route(
                "aes-hls-playlist",
                "application/vnd.apple.mpegurl",
                path=self.assets.aes_playlist,
            )
        if path == "/hls/aes/key.bin":
            return Route("aes-hls-key", "application/octet-stream", path=self.assets.aes_key)
        if path == "/hls/mixed/index.m3u8":
            body = rewrite_aes_playlist(
                self.assets.aes_playlist.read_text(encoding="utf-8"), self.base("http")
            ).encode("utf-8")
            return Route("mixed-hls-playlist", "application/vnd.apple.mpegurl", body=body)
        if path == "/hls/invalid-child/index.m3u8":
            body = rewrite_aes_playlist(
                self.assets.aes_playlist.read_text(encoding="utf-8"),
                self.base("https_self_signed"),
            ).encode("utf-8")
            return Route("invalid-child-hls-playlist", "application/vnd.apple.mpegurl", body=body)

        for family in ("plain", "aes"):
            prefix = f"/hls/{family}/"
            if path.startswith(prefix):
                name = path.removeprefix(prefix)
                if SEGMENT_RE.fullmatch(name):
                    candidate = self.assets.root / "hls" / family / name
                    expected_parent = self.assets.root / "hls" / family
                    if candidate.is_file() and candidate.parent == expected_parent:
                        return Route(f"{family}-hls-segment", "video/mp2t", path=candidate)
        return Route("unknown")

    def _handler_class(self, server_label: str) -> type[BaseHTTPRequestHandler]:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "LoopbackMediaFixture/1"
            sys_version = ""

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                self._serve(send_body=True)

            def do_HEAD(self) -> None:
                self._serve(send_body=False)

            def do_POST(self) -> None:
                self._reject_method()

            def do_PUT(self) -> None:
                self._reject_method()

            def _reject_method(self) -> None:
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                fixture.logger.record(
                    server=server_label,
                    method=self.command,
                    resource="method-rejected",
                    status=405,
                    bytes_sent=0,
                    range_label=None,
                )

            def _serve(self, *, send_body: bool) -> None:
                route = fixture._route(self.path)
                if route.redirect is not None:
                    self.send_response(302)
                    self.send_header("Location", route.redirect)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    fixture.logger.record(
                        server=server_label,
                        method=self.command,
                        resource=route.resource,
                        status=302,
                        bytes_sent=0,
                        range_label=None,
                    )
                    return

                if route.path is None and route.body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    fixture.logger.record(
                        server=server_label,
                        method=self.command,
                        resource="unknown",
                        status=404,
                        bytes_sent=0,
                        range_label=None,
                    )
                    return

                size = (
                    route.path.stat().st_size
                    if route.path is not None
                    else len(route.body or b"")
                )
                range_value = self.headers.get("Range")
                range_label = safe_range_label(range_value, size)
                try:
                    requested_range = parse_range(range_value, size)
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    fixture.logger.record(
                        server=server_label,
                        method=self.command,
                        resource=route.resource,
                        status=416,
                        bytes_sent=0,
                        range_label="invalid",
                    )
                    return

                start = requested_range.start if requested_range else 0
                length = requested_range.length if requested_range else size
                status = 206 if requested_range else 200
                self.send_response(status)
                self.send_header("Content-Type", route.content_type or "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if requested_range:
                    self.send_header(
                        "Content-Range",
                        f"bytes {requested_range.start}-{requested_range.end}/{size}",
                    )
                self.end_headers()

                bytes_sent = 0
                if send_body:
                    try:
                        if route.path is not None:
                            with route.path.open("rb") as stream:
                                stream.seek(start)
                                bytes_sent = self._copy(stream, length)
                        else:
                            body = route.body or b""
                            payload = body[start : start + length]
                            self.wfile.write(payload)
                            bytes_sent = len(payload)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                fixture.logger.record(
                    server=server_label,
                    method=self.command,
                    resource=route.resource,
                    status=status,
                    bytes_sent=bytes_sent,
                    range_label=range_label,
                )

            def _copy(self, stream: BinaryIO, remaining: int) -> int:
                sent = 0
                while sent < remaining:
                    block = stream.read(min(64 * 1024, remaining - sent))
                    if not block:
                        break
                    self.wfile.write(block)
                    sent += len(block)
                return sent

        return Handler

    def start(self) -> None:
        for label, server in self._servers.items():
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"fixture-{label}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        for server in self._servers.values():
            server.shutdown()
        for thread in self._threads:
            thread.join(timeout=5)
        for server in self._servers.values():
            server.server_close()
        self._threads.clear()

    def __enter__(self) -> "FixtureServers":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _safe_log_stream(path: Path) -> TextIO:
    if path.is_symlink():
        raise RuntimeError("refusing a symlink request-log path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)


def _write_ready(path: Path, document: dict[str, object]) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise RuntimeError("refusing a symlink ready-file path")
    if not path.parent.is_dir():
        raise RuntimeError("the ready-file parent directory does not exist")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fixture-ready-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source())
    parser.add_argument("--generated-root", type=Path, default=script_dir / "generated")
    parser.add_argument(
        "--ready-file",
        type=Path,
        default=Path("/tmp/proton-rtsp-security-media-endpoints.json"),
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("/tmp/proton-rtsp-security-media-requests.jsonl"),
    )
    parser.add_argument("--case-id", default="a31-security-01")
    parser.add_argument("--openssl", default=shutil.which("openssl") or "openssl")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    args = parser.parse_args(argv)

    generated_root = args.generated_root.resolve()
    generated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(generated_root, 0o700)
    old_umask = os.umask(0o077)
    try:
        with cleanup_signal_guard():
            try:
                with tempfile.TemporaryDirectory(
                    prefix="session-", dir=generated_root
                ) as session:
                    assets = prepare_assets(
                        Path(session),
                        args.source,
                        openssl=args.openssl,
                        ffmpeg=args.ffmpeg,
                    )
                    with _safe_log_stream(args.log) as log_stream:
                        logger = SafeLogger(log_stream, args.case_id)
                        with FixtureServers(assets, logger) as servers:
                            document = servers.endpoint_document()
                            ready_path = _write_ready(args.ready_file, document)
                            try:
                                print(
                                    json.dumps(
                                        {
                                            "bind": BIND_HOST,
                                            "endpoints": document["endpoints"],
                                            "schema": 1,
                                        },
                                        sort_keys=True,
                                    )
                                )
                                print(
                                    "The private test CA is not installed or trusted. "
                                    "Press Ctrl+C to stop.",
                                    file=sys.stderr,
                                )
                                while True:
                                    time.sleep(1)
                            finally:
                                with contextlib.suppress(FileNotFoundError):
                                    ready_path.unlink()
            except FixtureStop:
                pass
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
