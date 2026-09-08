#!/usr/bin/env python3
"""End-to-end native smoke test for the loopback security fixture."""

from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from security_media_fixture import (
    FixtureServers,
    SafeLogger,
    cleanup_signal_guard,
    default_source,
    prepare_assets,
)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required smoke-test tool is unavailable: {name}")
    return path


def fetch(
    url: str,
    *,
    context: ssl.SSLContext | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if url.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=5) as response:
        return response.status, dict(response.headers.items()), response.read()


def expect_tls_failure(url: str, context: ssl.SSLContext) -> None:
    try:
        fetch(url, context=context)
    except urllib.error.URLError as error:
        if isinstance(error.reason, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return
        failure_type = type(error.reason).__name__
        raise AssertionError(f"unexpected TLS failure type: {failure_type}") from error
    raise AssertionError("an invalid TLS endpoint was accepted")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def run(command: list[str], *, expect_success: bool) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=30,
        check=False,
    )
    if expect_success and result.returncode:
        tool = Path(command[0]).name
        raise AssertionError(f"{tool} unexpectedly failed with {result.returncode}")
    if not expect_success and not result.returncode:
        raise AssertionError(f"{Path(command[0]).name} unexpectedly accepted a negative case")
    return result


def ffmpeg_input_command(ffmpeg: str, url: str, ca: Path, protocols: str) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-tls_verify",
        "1",
        "-ca_file",
        str(ca),
        "-protocol_whitelist",
        protocols,
        "-i",
        url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]


def ffmpeg_plain_hls_seek_command(ffmpeg: str, url: str) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "http,tcp",
        "-ss",
        "4",
        "-i",
        url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-map",
        "0:a:0",
        "-frames:a",
        "1",
        "-f",
        "null",
        "-",
    ]


def verify_cli_cleanup(
    *, script_dir: Path, source: Path, openssl: str, ffmpeg: str
) -> None:
    """Exercise failed preparation and catchable SIGTERM cleanup."""

    with tempfile.TemporaryDirectory(prefix="cli-cleanup-") as outer_name:
        outer = Path(outer_name)
        generated = outer / "generated"
        generated.mkdir(mode=0o700)
        ready = outer / "ready.json"
        request_log = outer / "requests.jsonl"
        base_command = [
            sys.executable,
            str(script_dir / "security_media_fixture.py"),
            "--generated-root",
            str(generated),
            "--ready-file",
            str(ready),
            "--log",
            str(request_log),
            "--openssl",
            openssl,
            "--ffmpeg",
            ffmpeg,
        ]

        failed = subprocess.run(
            base_command + ["--source", str(outer / "missing.mp4")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if not failed.returncode:
            raise AssertionError("missing-source preparation unexpectedly succeeded")
        if any(generated.iterdir()) or ready.exists():
            raise AssertionError("failed preparation left generated session state")

        process = subprocess.Popen(
            base_command + ["--source", str(source), "--case-id", "signal-cleanup"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.is_file():
                raise AssertionError("CLI fixture did not become ready for the signal test")
            process.terminate()
            process.communicate(timeout=10)
            if process.returncode:
                raise AssertionError("CLI fixture did not cleanly handle SIGTERM")
            if ready.exists() or any(generated.iterdir()):
                raise AssertionError("SIGTERM left generated session state")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def main() -> int:
    openssl = require_tool("openssl")
    ffmpeg = require_tool("ffmpeg")
    curl = require_tool("curl")
    source = default_source()
    if not source.is_file():
        raise RuntimeError("the repository's 45-second MP4 fixture is missing")

    script_dir = Path(__file__).resolve().parent
    generated_root = script_dir / "generated"
    generated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = io.StringIO()

    with cleanup_signal_guard(), tempfile.TemporaryDirectory(
        prefix="smoke-", dir=generated_root
    ) as session:
        assets = prepare_assets(
            Path(session), source, openssl=openssl, ffmpeg=ffmpeg
        )
        logger = SafeLogger(log, "native-security-smoke")
        with FixtureServers(assets, logger) as servers:
            document = servers.endpoint_document()
            endpoints = document["endpoints"]
            assert isinstance(endpoints, dict)
            for server in servers._servers.values():
                assert server.server_address[0] == "127.0.0.1"

            explicit_ca = ssl.create_default_context(cafile=str(assets.ca_cert))
            default_ca = ssl.create_default_context()

            status, headers, body = fetch(
                endpoints["direct_https"] + "?query=DO_NOT_LOG",
                context=explicit_ca,
                headers={
                    "Range": "bytes=0-31",
                    "Authorization": "Bearer DO_NOT_LOG",
                    "Cookie": "secret=DO_NOT_LOG",
                },
            )
            assert status == 206
            assert len(body) == 32
            assert headers["Content-Range"].startswith("bytes 0-31/")

            expect_tls_failure(endpoints["direct_https"], default_ca)
            expect_tls_failure(endpoints["direct_self_signed_https"], explicit_ca)
            expect_tls_failure(endpoints["direct_wrong_host_https"], explicit_ca)

            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=explicit_ca),
                NoRedirect(),
            )
            try:
                opener.open(endpoints["redirect_https_to_http"], timeout=5)
            except urllib.error.HTTPError as error:
                assert error.code == 302
                assert error.headers["Location"] == endpoints["direct_http"]
            else:
                raise AssertionError("redirect endpoint did not emit a redirect")

            assert endpoints["hls_plain_http"].startswith("http://127.0.0.1:")
            _, _, plain_http_playlist = fetch(endpoints["hls_plain_http"])
            _, _, plain_playlist = fetch(endpoints["hls_plain_https"], context=explicit_ca)
            _, _, aes_playlist = fetch(endpoints["hls_aes_https"], context=explicit_ca)
            _, _, mixed_playlist = fetch(endpoints["hls_mixed_https"], context=explicit_ca)
            _, _, invalid_child = fetch(
                endpoints["hls_invalid_child_https"], context=explicit_ca
            )
            plain_text = plain_playlist.decode("utf-8")
            aes_text = aes_playlist.decode("utf-8")
            mixed_text = mixed_playlist.decode("utf-8")
            invalid_child_text = invalid_child.decode("utf-8")
            plain_http_text = plain_http_playlist.decode("utf-8")
            for marker in ("#EXTM3U", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-ENDLIST"):
                assert marker in plain_http_text
            assert "segment000.ts" in plain_http_text
            assert "#EXTM3U" in plain_text and "segment000.ts" in plain_text
            segment_status, segment_headers, segment = fetch(
                f'{servers.base("http")}/hls/plain/segment000.ts'
            )
            assert segment_status == 200
            assert segment_headers["Content-Type"] == "video/mp2t"
            assert segment
            assert "METHOD=AES-128" in aes_text and 'URI="/hls/aes/key.bin"' in aes_text
            assert servers.base("http") in mixed_text
            assert servers.base("https_self_signed") in invalid_child_text
            _, _, key = fetch(
                f'{servers.base("https_valid")}/hls/aes/key.bin', context=explicit_ca
            )
            assert len(key) == 16

            curl_base = [curl, "--silent", "--show-error", "--max-time", "5", "--noproxy", "*"]
            run(
                curl_base
                + [
                    "--cacert",
                    str(assets.ca_cert),
                    endpoints["direct_https"],
                    "-o",
                    os.devnull,
                ],
                expect_success=True,
            )
            run(curl_base + [endpoints["direct_https"], "-o", os.devnull], expect_success=False)
            for endpoint_name in (
                "direct_self_signed_https",
                "direct_wrong_host_https",
            ):
                run(
                    curl_base
                    + [
                        "--cacert",
                        str(assets.ca_cert),
                        endpoints[endpoint_name],
                        "-o",
                        os.devnull,
                    ],
                    expect_success=False,
                )

            run(
                ffmpeg_plain_hls_seek_command(
                    ffmpeg,
                    endpoints["hls_plain_http"],
                ),
                expect_success=True,
            )
            run(
                ffmpeg_input_command(
                    ffmpeg,
                    endpoints["hls_aes_https"],
                    assets.ca_cert,
                    "https,tls,tcp,crypto",
                ),
                expect_success=True,
            )
            run(
                ffmpeg_input_command(
                    ffmpeg,
                    endpoints["direct_self_signed_https"],
                    assets.ca_cert,
                    "https,tls,tcp",
                ),
                expect_success=False,
            )
            run(
                ffmpeg_input_command(
                    ffmpeg,
                    endpoints["direct_wrong_host_https"],
                    assets.ca_cert,
                    "https,tls,tcp",
                ),
                expect_success=False,
            )
            run(
                ffmpeg_input_command(
                    ffmpeg,
                    endpoints["redirect_https_to_http"],
                    assets.ca_cert,
                    "https,tls,tcp",
                ),
                expect_success=False,
            )
            run(
                ffmpeg_input_command(
                    ffmpeg,
                    endpoints["hls_mixed_https"],
                    assets.ca_cert,
                    "https,tls,tcp,crypto",
                ),
                expect_success=False,
            )
        serialized_log = log.getvalue()
        assert "DO_NOT_LOG" not in serialized_log
        assert "Authorization" not in serialized_log
        assert "Cookie" not in serialized_log
        assert "127.0.0.1" not in serialized_log
        assert "query" not in serialized_log.lower()
        assert "url" not in serialized_log.lower()
        assert "path" not in serialized_log.lower()
        for line in serialized_log.splitlines():
            record = json.loads(line)
            assert set(record) == {
                "bytes_sent",
                "case_id",
                "method",
                "range",
                "resource",
                "server",
                "status",
                "timestamp",
            }

    verify_cli_cleanup(
        script_dir=script_dir,
        source=source,
        openssl=openssl,
        ffmpeg=ffmpeg,
    )

    print("PASS: loopback binds, endpoint semantics, explicit-CA TLS, negative TLS,")
    print("      HTTPS-to-HTTP redirect, HLS children, plain-HLS A/V seek,")
    print("      AES-128 decode, redacted logs,")
    print("      failed-preparation cleanup, and catchable SIGTERM cleanup")
    print(
        "AES functional proof used native FFmpeg over HTTPS with only the "
        "ephemeral CA passed explicitly."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
