#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Bounded loopback integration tests for the progressive HTTP fixture."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from http_stream_fixture import (
    FailureGate,
    FixtureConfig,
    FixtureServer,
    PostOpenRangeGate,
    SafeJsonlLogger,
)


PAYLOAD = bytes(range(256)) * 32


@contextmanager
def running_fixture(
    mode: str,
    *,
    failure_status: int = 503,
    header_delay_seconds: float = 0.0,
    stall_after: int | None = None,
    stall_seconds: float = 0.0,
) -> Iterator[tuple[str, int]]:
    with tempfile.TemporaryDirectory(prefix="http-fixture-smoke.") as directory:
        media_file = Path(directory, "fixture.bin")
        media_file.write_bytes(PAYLOAD)
        config = FixtureConfig(
            media_file=media_file,
            mode=mode,
            logger=SafeJsonlLogger(io.StringIO(), f"smoke-{mode}"),
            failure_gate=FailureGate(1),
            post_open_range_gate=PostOpenRangeGate(),
            truncate_after=None,
            chunk_bytes=512,
            bytes_per_second=0,
            content_type="application/octet-stream",
            failure_status=failure_status,
            header_delay_seconds=header_delay_seconds,
            stall_after=stall_after,
            stall_seconds=stall_seconds,
            max_requests=32,
            max_concurrent=4,
        )
        server = FixtureServer(("127.0.0.1", 0), config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            yield str(host), int(port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if thread.is_alive():
                raise RuntimeError("fixture thread did not stop")


def get(
    host: str,
    port: int,
    *,
    route: str = "/media",
    range_header: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    headers = {} if range_header is None else {"Range": range_header}
    try:
        connection.request("GET", route, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


class LoopbackSmokeTests(unittest.TestCase):
    def test_sigterm_interrupts_long_stall_and_seals_all_terminal_records(self) -> None:
        script = Path(__file__).with_name("http_stream_fixture.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "fixture.bin"
            media.write_bytes(b"x" * 4096)
            log = root / "http.jsonl"
            completion = root / "http.complete.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--file", str(media),
                    "--mode", "stall",
                    "--case-id", "graceful-stall",
                    "--host", "127.0.0.1",
                    "--port", "0",
                    "--rate-kib", "0",
                    "--chunk-bytes", "1",
                    "--stall-after", "1",
                    "--stall-seconds", "300",
                    "--max-requests", "2",
                    "--max-concurrent", "2",
                    "--max-log-bytes", "4096",
                    "--log", str(log),
                    "--completion-marker", str(completion),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            client: socket.socket | None = None
            try:
                assert process.stderr is not None
                listening = process.stderr.readline()
                match = re.search(r"127\.0\.0\.1:([0-9]+)", listening)
                self.assertIsNotNone(match, listening)
                client = socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=2.0)
                client.sendall(b"GET /media HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                response = b""
                while b"\r\n\r\n" not in response or not response.partition(b"\r\n\r\n")[2]:
                    response += client.recv(4096)

                started = time.monotonic()
                os.kill(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3.0)
                self.assertIsNone(stdout)
                self.assertEqual(0, process.returncode, stderr)
                self.assertLess(time.monotonic() - started, 3.0)

                marker_payload = completion.read_bytes()
                marker = json.loads(marker_payload.decode("ascii"))
                self.assertEqual(
                    (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"),
                    marker_payload,
                )
                self.assertEqual(
                    {
                        "allocatedRequestCount", "caseId", "clockBasis", "logBytes",
                        "logSha256", "rejectedConnectionCount", "schema", "status",
                        "terminalRecordCount",
                    },
                    set(marker),
                )
                self.assertEqual((1, 1, 0), (
                    marker["allocatedRequestCount"],
                    marker["terminalRecordCount"],
                    marker["rejectedConnectionCount"],
                ))
                self.assertEqual(log.stat().st_size, marker["logBytes"])
                self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(), marker["logSha256"])
                record = json.loads(log.read_text(encoding="utf-8"))
                self.assertTrue(record["disconnected"])
                self.assertLess(record["bytes_sent"], record["bytes_expected"])
            finally:
                if client is not None:
                    client.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2.0)

    def test_chunked_body_and_range_are_protocol_valid(self) -> None:
        with running_fixture("chunked") as (host, port):
            status, headers, body = get(host, port)
            self.assertEqual(200, status)
            self.assertEqual(PAYLOAD, body)
            self.assertEqual("chunked", headers.get("transfer-encoding"))
            self.assertNotIn("content-length", headers)

            status, headers, body = get(host, port, range_header="bytes=100-199")
            self.assertEqual(206, status)
            self.assertEqual(PAYLOAD[100:200], body)
            self.assertEqual(f"bytes 100-199/{len(PAYLOAD)}", headers.get("content-range"))
            self.assertEqual("chunked", headers.get("transfer-encoding"))

    def test_exact_stall_resumes_the_same_response(self) -> None:
        with running_fixture("stall", stall_after=1024, stall_seconds=0.05) as (host, port):
            started = time.monotonic()
            status, _, body = get(host, port)
            elapsed = time.monotonic() - started
            self.assertEqual((200, PAYLOAD), (status, body))
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertLess(elapsed, 1.0)

    def test_selected_failure_status_recovers_on_the_same_url(self) -> None:
        with running_fixture("fail-first", failure_status=429) as (host, port):
            first_status, _, first_body = get(host, port)
            second_status, _, second_body = get(host, port)
            self.assertEqual((429, b""), (first_status, first_body))
            self.assertEqual((200, PAYLOAD), (second_status, second_body))

    def test_failed_media_ranges_do_not_poison_the_recovery_route(self) -> None:
        with running_fixture("fail-post-open-range-recovery") as (host, port):
            opened_status, _, opened_body = get(
                host, port, range_header="bytes=0-1023"
            )
            failed_status, _, failed_body = get(
                host, port, range_header="bytes=4096-5119"
            )
            recovery_status, _, recovery_body = get(
                host,
                port,
                route="/recovery",
                range_header="bytes=4096-5119",
            )
            media_retry_status, _, media_retry_body = get(
                host, port, range_header="bytes=4096-5119"
            )

            self.assertEqual((206, PAYLOAD[:1024]), (opened_status, opened_body))
            self.assertEqual((503, b""), (failed_status, failed_body))
            self.assertEqual(
                (206, PAYLOAD[4096:5120]),
                (recovery_status, recovery_body),
            )
            self.assertEqual((503, b""), (media_retry_status, media_retry_body))

    def test_header_delay_is_bounded_and_body_remains_exact(self) -> None:
        with running_fixture("range", header_delay_seconds=0.05) as (host, port):
            started = time.monotonic()
            status, _, body = get(host, port)
            elapsed = time.monotonic() - started
            self.assertEqual((200, PAYLOAD), (status, body))
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
