#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import copy
from collections.abc import Callable
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("rtsp_live_case.py")
SPEC = importlib.util.spec_from_file_location("rtsp_live_case", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
rtsp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rtsp)

FFMPEG = "/nix/store/biq1laydwsk5vbc26qblm7n8bwz08hnm-ffmpeg-8.1.2-bin/bin/ffmpeg"
FFPROBE = "/nix/store/biq1laydwsk5vbc26qblm7n8bwz08hnm-ffmpeg-8.1.2-bin/bin/ffprobe"
MEDIAMTX = "/nix/store/06l7w6iqaal1rqgmp1yh309wp0xa7an8-mediamtx-rtsp-test-1.19.2/bin/mediamtx"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "backendPort": 18555,
        "bind": "127.0.0.1",
        "blackholeAfterServerBytes": 0,
        "caseId": "rtsp-live-tcp-reopen",
        "ffmpegBin": FFMPEG,
        "ffprobeBin": FFPROBE,
        "fixtureRelativePath": "av/faststart.mp4",
        "fixtureSha256": "dd69952540d0f1756d8094e84814d0ba8a361f1dc906e8ef3bd07bf8713a3934",
        "holdDurationMs": 0,
        "maxBytesPerDirection": 1024 * 1024,
        "maxConnections": 16,
        "maxLogBytes": 1024 * 1024,
        "maxObservedConnections": 12,
        "mediamtxBin": MEDIAMTX,
        "minObservedConnections": 3,
        "nativeProbeSeconds": 2,
        "port": 18554,
        "relayMode": "forward",
        "schema": 1,
        "service": "rtsp-live-v1",
        "sourceScheme": "rtsp",
        "streamPath": "fixture",
    }
    value.update(updates)
    return value


class EchoBackend:
    def __init__(self, port: int) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run)
        self.connections: list[socket.socket] = []

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    break
                raise
            self.connections.append(client)
            try:
                payload = client.recv(4096)
                if payload:
                    client.sendall(b"reply:" + payload)
            finally:
                client.close()

    def __enter__(self) -> "EchoBackend":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=2)


class PushBackend:
    def __init__(self, port: int, payload: bytes) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.payload = payload
        self.initial = b""
        self.after_cutoff = b""
        self.thread = threading.Thread(target=self.run)

    def run(self) -> None:
        client, _ = self.listener.accept()
        client.settimeout(2)
        try:
            self.initial = client.recv(4096)
            client.sendall(self.payload)
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                self.after_cutoff += chunk
        finally:
            client.close()

    def __enter__(self) -> "PushBackend":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.listener.close()
        self.thread.join(timeout=3)


class RTSPCaseContractTests(unittest.TestCase):
    def test_fixture_variants_map_to_reviewed_case_identities(self) -> None:
        fixture = Path(__file__).parents[1] / "media-engine-stress/rtsp-fixture"
        generator = (fixture / "default.nix").read_text(encoding="utf-8")
        template = (fixture / "examples/service-config.json.in").read_text(encoding="utf-8")
        expected = {
            "running-replace": ("rtsp-live-tcp-reopen", "forward", 0, 0, 3),
            "pause-before-replace": (
                "rtsp-live-tcp-pause-before-replace", "forward", 0, 0, 3,
            ),
            "blackhole-cancel": (
                "rtsp-live-tcp-blackhole-cancel",
                "server-to-client-blackhole",
                2097152,
                0,
                2,
            ),
            "finite-hold-recovery": (
                "rtsp-live-tcp-finite-hold-recovery",
                "server-to-client-finite-hold",
                4194304,
                3000,
                2,
            ),
            "drain-diagnostics": (
                "rtsp-live-tcp-drain-diagnostics", "forward", 0, 0, 10,
            ),
        }

        self.assertIn("selectedCaseId = builtins.getAttr variant caseIds;", generator)
        self.assertIn("--replace-fail '@CASE_ID@' '${selectedCaseId}'", generator)
        self.assertIn('assert builtins.elem sourceScheme [ "rtsp" "rtspt" ];', generator)
        self.assertIn(
            'sourceScheme == "rtspt" && reproductionInput == "observed-heavy-v1"',
            generator,
        )
        for rung, path in {
            "a": "rtsp/payload-a-320x180-main-270k.mp4",
            "b": "rtsp/payload-b-1280x720-main-270k.mp4",
            "c": "rtsp/payload-c-1280x720-high-270k.mp4",
            "d": "rtsp/payload-d-1280x720-high-8200k.mp4",
        }.items():
            self.assertIn(f'{rung} = "{path}";', generator)
        self.assertEqual(template.count("@CASE_ID@"), 1)
        for variant, (case_id, relay_mode, threshold, hold_ms, minimum) in expected.items():
            with self.subTest(variant=variant):
                self.assertIn(f'{variant} = "{case_id}";', generator)
                rendered = json.loads(
                    template
                    .replace("@CASE_ID@", case_id)
                    .replace("@RELAY_MODE@", relay_mode)
                    .replace('"@BLACKHOLE_AFTER_SERVER_BYTES@"', str(threshold))
                    .replace('"@HOLD_DURATION_MS@"', str(hold_ms))
                    .replace('"@MIN_OBSERVED_CONNECTIONS@"', str(minimum))
                    .replace("@MEDIAMTX_BIN@", MEDIAMTX)
                    .replace("@FFMPEG_BIN@", FFMPEG)
                    .replace("@FFPROBE_BIN@", FFPROBE)
                    .replace("@SOURCE_SCHEME@", "rtsp")
                    .replace("@FIXTURE_RELATIVE_PATH@", "av/faststart.mp4")
                    .replace("@FIXTURE_SHA256@", config()["fixtureSha256"])
                )
                self.assertEqual(rendered["caseId"], case_id)
                self.assertEqual(rendered["relayMode"], relay_mode)
                self.assertEqual(rendered["blackholeAfterServerBytes"], threshold)
                self.assertEqual(rendered["holdDurationMs"], hold_ms)
                self.assertEqual(rendered["sourceScheme"], "rtsp")
                self.assertEqual(rendered["fixtureRelativePath"], "av/faststart.mp4")

    def test_config_requires_literal_loopback_and_distinct_ports(self) -> None:
        with mock.patch.object(rtsp, "require_store_executable", return_value=Path("/nix/store/test-tool")):
            rtsp.validate_config(config())
            historical = config()
            del historical["holdDurationMs"]
            self.assertEqual(rtsp.validate_config(historical)["holdDurationMs"], 0)
            historical_hold = config(
                relayMode="server-to-client-finite-hold",
                blackholeAfterServerBytes=64 * 1024,
                holdDurationMs=3000,
            )
            del historical_hold["holdDurationMs"]
            with self.assertRaises(rtsp.RTSPCaseError):
                rtsp.validate_config(historical_hold)
            for updates in (
                {"bind": "0.0.0.0"},
                {"backendPort": 18554},
                {"port": 9},
                {"service": "progressive-http-v1"},
                {"sourceScheme": "https"},
                {"sourceScheme": []},
                {"relayMode": "blackhole"},
                {"blackholeAfterServerBytes": 1},
                {
                    "relayMode": "server-to-client-blackhole",
                    "blackholeAfterServerBytes": 0,
                },
                {
                    "relayMode": "server-to-client-finite-hold",
                    "blackholeAfterServerBytes": 64 * 1024,
                    "holdDurationMs": 0,
                },
                {
                    "relayMode": "server-to-client-finite-hold",
                    "blackholeAfterServerBytes": 64 * 1024,
                    "holdDurationMs": 5000,
                },
            ):
                with self.subTest(updates=updates), self.assertRaises(rtsp.RTSPCaseError):
                    rtsp.validate_config(config(**updates))

    def test_scenarios_are_literal_rtsp_three_generation_nonseekable(self) -> None:
        examples = Path(__file__).parents[1] / "media-engine-stress/rtsp-fixture/examples"
        for name in ("live-tcp-reopen.scenario", "live-tcp-pause-reopen.scenario"):
            with self.subTest(name=name):
                rtsp.validate_scenario(examples / name, config())
        rtsp.validate_scenario(
            examples / "live-tcp-blackhole-cancel.scenario",
            config(
                relayMode="server-to-client-blackhole",
                blackholeAfterServerBytes=2097152,
                caseId="rtsp-live-tcp-blackhole-cancel",
            ),
        )
        rtsp.validate_scenario(
            examples / "live-tcp-finite-hold-recovery.scenario",
            config(
                relayMode="server-to-client-finite-hold",
                blackholeAfterServerBytes=4194304,
                holdDurationMs=3000,
                caseId="rtsp-live-tcp-finite-hold-recovery",
            ),
        )
        rtsp.validate_scenario(
            examples / "live-tcp-drain-diagnostics.scenario",
            config(
                caseId="rtsp-live-tcp-drain-diagnostics",
                minObservedConnections=10,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "scenario"
            bad.write_text('load "rtsp://127.0.0.1:18554/fixture"\nseek 1\nshutdown\n', encoding="utf-8")
            with self.assertRaisesRegex(rtsp.RTSPCaseError, "ordinary finite seek"):
                rtsp.validate_scenario(bad, config())

    def test_drain_diagnostics_is_ten_generations_in_one_process(self) -> None:
        scenario = (
            Path(__file__).parents[1]
            / "media-engine-stress/rtsp-fixture/examples/live-tcp-drain-diagnostics.scenario"
        )
        commands = [
            line.split()
            for line in scenario.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(sum(words[:2] == ["loop", "9"] for words in commands), 1)
        self.assertEqual(sum(words[:2] == ["loop", "4"] for words in commands), 2)
        self.assertEqual(sum(words[0] == "load" for words in commands), 1)
        self.assertEqual(sum(words[0] == "replace" for words in commands), 1)
        self.assertEqual(commands[-1], ["shutdown"])

    def test_rtspt_scenario_changes_only_the_reviewed_scheme_prefix(self) -> None:
        source = (Path(__file__).parents[1]
                  / "media-engine-stress/rtsp-fixture/examples/live-tcp-reopen.scenario")
        with tempfile.TemporaryDirectory() as directory:
            rtspt = Path(directory) / "scenario"
            rtsp_text = source.read_text(encoding="utf-8")
            rtspt_text = rtsp_text.replace("rtsp://", "rtspt://")
            rtspt.write_text(rtspt_text, encoding="utf-8")
            rtsp.validate_scenario(source, config(sourceScheme="rtsp"))
            rtsp.validate_scenario(rtspt, config(sourceScheme="rtspt"))
            rtsp_urls = re.findall(r'"(rtsp://[^"]+)"', rtsp_text)
            rtspt_urls = re.findall(r'"(rtspt://[^"]+)"', rtspt_text)
            self.assertEqual(len(rtsp_urls), len(rtspt_urls))
            self.assertEqual(
                [url.partition(":")[2] for url in rtsp_urls],
                [url.partition(":")[2] for url in rtspt_urls],
            )
            with self.assertRaisesRegex(rtsp.RTSPCaseError, "literal loopback RTSP"):
                rtsp.validate_scenario(source, config(sourceScheme="rtspt"))

    def test_interleaved_setup_observer_counts_only_reviewed_transport(self) -> None:
        observer = rtsp.InterleavedSetupObserver()
        observer.feed(
            b"SETUP rtsp://127.0.0.1:18554/fixture/trackID=0 RTSP/1.0\r\n"
            b"Transport: RTP/AVP;unicast;client_port=5000-5001\r\n\r\n"
        )
        observer.feed(
            b"SETUP rtsp://127.0.0.1:18554/fixture/trackID=0 RTSP/1.0\r\n"
            b"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n"
            b"SETUP rtsp://127.0.0.1:18554/fixture/trackID=1 RTSP/1.0\r\n"
            b"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n\r\n"
        )
        self.assertEqual(observer.count, 2)

    def test_pause_discriminator_pauses_before_both_replacements(self) -> None:
        scenario = (Path(__file__).parents[1]
                    / "media-engine-stress/rtsp-fixture/examples/live-tcp-pause-reopen.scenario")
        commands = []
        for line in scenario.read_text(encoding="utf-8").splitlines():
            words = line.split()
            if words and not words[0].startswith("#"):
                commands.append(words)
        replace_indexes = [index for index, words in enumerate(commands) if words[0] == "replace"]
        self.assertEqual(len(replace_indexes), 2)
        for index in replace_indexes:
            self.assertEqual(commands[index - 2], ["pause"])
            self.assertEqual(commands[index - 1], ["wait_event", "PAUSE", "5000"])


class LoopbackRTSPCaseTests(unittest.TestCase):
    def test_tcp_gate_only_relays_literal_loopback_and_seals_counts(self) -> None:
        try:
            public = free_port()
        except PermissionError:
            self.skipTest("the command sandbox forbids even literal-loopback sockets")
        backend = free_port()
        while backend == public:
            backend = free_port()
        selected = config(port=public, backendPort=backend)
        with tempfile.TemporaryDirectory() as directory, EchoBackend(backend):
            path = Path(directory) / "connections.jsonl"
            evidence = rtsp.ConnectionLog(path, selected)
            gate = rtsp.TCPGate(selected, evidence)
            gate.start()
            with socket.create_connection(("127.0.0.1", public), timeout=2) as client:
                client.sendall(b"test")
                self.assertEqual(client.recv(64), b"reply:test")
            gate.stop()
            evidence.close()
            self.assertEqual(gate.accepted, 1)
            self.assertEqual(gate.rejected, 0)
            records = [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["client_to_server_bytes"], 4)
            self.assertEqual(records[0]["server_to_client_bytes"], 10)
            self.assertIn(records[0]["status"], {"eof", "service-stop"})

    def test_tcp_gate_blackholes_at_exact_threshold_but_keeps_control_open(self) -> None:
        try:
            public = free_port()
        except PermissionError:
            self.skipTest("the command sandbox forbids even literal-loopback sockets")
        backend = free_port()
        while backend == public:
            backend = free_port()
        threshold = 64 * 1024
        selected = config(
            port=public,
            backendPort=backend,
            relayMode="server-to-client-blackhole",
            blackholeAfterServerBytes=threshold,
        )
        payload = b"x" * (threshold + 32 * 1024)
        with tempfile.TemporaryDirectory() as directory, PushBackend(backend, payload) as source:
            path = Path(directory) / "connections.jsonl"
            evidence = rtsp.ConnectionLog(path, selected)
            gate = rtsp.TCPGate(selected, evidence)
            gate.start()
            received = b""
            with socket.create_connection(("127.0.0.1", public), timeout=2) as client:
                client.sendall(b"SETUP")
                while len(received) < threshold:
                    received += client.recv(threshold - len(received))
                self.assertEqual(len(received), threshold)
                client.settimeout(0.15)
                with self.assertRaises(socket.timeout):
                    client.recv(1)
                client.sendall(b"TEARDOWN")
                client.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 2
            while evidence.records == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(evidence.records, 1)
            gate.stop()
            evidence.close()
            source.thread.join(timeout=2)
            self.assertFalse(source.thread.is_alive())
            record = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(source.initial, b"SETUP")
            self.assertEqual(source.after_cutoff, b"TEARDOWN")
            self.assertEqual(record["server_to_client_bytes"], threshold)
            self.assertEqual(record["client_to_server_bytes_after_blackhole"], len(b"TEARDOWN"))
            self.assertIsInstance(record["blackhole_started_monotonic_ns"], int)
            self.assertEqual(record["status"], "eof")
            self.assertEqual(record["terminal_reason"], "client-eof")

    def test_tcp_gate_resumes_once_after_a_finite_hold(self) -> None:
        try:
            public = free_port()
        except PermissionError:
            self.skipTest("the command sandbox forbids even literal-loopback sockets")
        backend = free_port()
        while backend == public:
            backend = free_port()
        threshold = 64 * 1024
        tail = 32 * 1024
        selected = config(
            port=public,
            backendPort=backend,
            relayMode="server-to-client-finite-hold",
            blackholeAfterServerBytes=threshold,
            holdDurationMs=300,
        )
        with tempfile.TemporaryDirectory() as directory, PushBackend(
            backend, b"x" * (threshold + tail)
        ) as source:
            path = Path(directory) / "connections.jsonl"
            evidence = rtsp.ConnectionLog(path, selected)
            gate = rtsp.TCPGate(selected, evidence)
            gate.start()
            received = b""
            with socket.create_connection(("127.0.0.1", public), timeout=2) as client:
                client.sendall(b"SETUP")
                while len(received) < threshold:
                    received += client.recv(threshold - len(received))
                client.settimeout(0.1)
                with self.assertRaises(socket.timeout):
                    client.recv(1)
                client.sendall(b"GET_PARAMETER")
                client.settimeout(2)
                while len(received) < threshold + tail:
                    received += client.recv(threshold + tail - len(received))
                client.sendall(b"TEARDOWN")
                client.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 2
            while evidence.records == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(evidence.records, 1)
            gate.stop()
            evidence.close()
            source.thread.join(timeout=2)
            self.assertFalse(source.thread.is_alive())
            record = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(len(received), threshold + tail)
            self.assertEqual(source.initial, b"SETUP")
            self.assertEqual(source.after_cutoff, b"GET_PARAMETERTEARDOWN")
            self.assertEqual(record["server_to_client_bytes"], threshold + tail)
            self.assertGreater(
                record["hold_resumed_monotonic_ns"],
                record["blackhole_started_monotonic_ns"],
            )
            self.assertGreaterEqual(
                record["hold_resumed_monotonic_ns"]
                - record["blackhole_started_monotonic_ns"],
                300_000_000,
            )
            self.assertEqual(record["status"], "eof")
            self.assertEqual(record["terminal_reason"], "client-eof")


class RTSPCaseRuntimeOracleTests(unittest.TestCase):
    def test_watcher_requires_nonfinite_duration_av_and_pcm_per_generation(self) -> None:
        origin = 1_000_000
        records = []
        sequence = 0
        relative = 0

        def add(**values: object) -> None:
            nonlocal sequence, relative
            sequence += 1
            relative += 100
            record = {
                "seq": sequence,
                "monotonic_origin_ms": origin,
                "monotonic_ms": relative,
                "type": "snapshot",
                "action": "load",
                "event": None,
                "label": None,
                "source_generation": 0,
            }
            record.update(values)
            records.append(record)

        for generation in (1, 2, 3):
            add(source_generation=generation, action="load" if generation == 1 else "replace")
            for checkpoint in range(4):
                relative += 900
                add(
                    source_generation=generation,
                    label=f"rtsp-live-g{generation}",
                    action="snapshot",
                    duration=None,
                    has_audio=True,
                    has_video=True,
                    paused=False,
                    seeking=False,
                    ended=False,
                    audio_monitor_enabled=True,
                    audio_samples_generation=100 + checkpoint,
                    audio_bytes_generation=8192 + checkpoint,
                    audio_nonzero_units_generation=1000 + checkpoint,
                    audio_last_monotonic_ms=relative + 100,
                    audio_last_nonzero_monotonic_ms=relative + 100,
                    time=float(checkpoint),
                )
        add(type="result", action=None, source_generation=3)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            done = root / "done"
            done.write_text("0\n", encoding="ascii")
            output = root / "watch.json"
            original = rtsp.monotonic_raw_ns
            for instrumentation in ("audio-monitor", "endpoint-monitor"):
                selected = [dict(record) for record in records]
                if instrumentation == "endpoint-monitor":
                    for record in selected:
                        if record.get("label") in {
                            "rtsp-live-g1", "rtsp-live-g2", "rtsp-live-g3",
                        }:
                            record["audio_monitor_enabled"] = False
                            record["audio_samples_generation"] = 0
                            record["audio_bytes_generation"] = 0
                            record["audio_nonzero_units_generation"] = 0
                            record["audio_last_monotonic_ms"] = None
                            record["audio_last_nonzero_monotonic_ms"] = None
                driver.write_text(
                    "".join(json.dumps(record) + "\n" for record in selected),
                    encoding="utf-8",
                )
                observed = iter(
                    (record["monotonic_origin_ms"] + record["monotonic_ms"] + 1) * 1_000_000
                    for record in selected
                )
                rtsp.monotonic_raw_ns = lambda: next(observed)
                try:
                    rtsp.command_watch(types.SimpleNamespace(
                        driver_json=driver,
                        producer_done=done,
                        output=output,
                        timeout_seconds=1.0,
                        instrumentation=instrumentation,
                    ))
                finally:
                    rtsp.monotonic_raw_ns = original
                watch = json.loads(output.read_text(encoding="ascii"))
                self.assertEqual(watch["sourceGenerations"], 3)
                self.assertEqual(
                    [item["labelCount"] for item in watch["generationWindows"]],
                    [4, 4, 4],
                )
                expected_last = [
                    (record["monotonic_origin_ms"] + record["monotonic_ms"]) * 1_000_000
                    for record in selected
                    if record.get("label") in {
                        "rtsp-live-g1", "rtsp-live-g2", "rtsp-live-g3",
                    }
                ][3::4]
                self.assertEqual(
                    [item["lastMonotonicNs"] for item in watch["generationWindows"]],
                    expected_last,
                )
                self.assertLess(
                    watch["generationWindows"][-1]["lastMonotonicNs"],
                    watch["resultMonotonicNs"],
                )

    def test_drain_diagnostics_watcher_requires_all_ten_generations(self) -> None:
        origin = 2_000_000
        records: list[dict[str, object]] = []
        sequence = 0
        relative = 0

        def add(**values: object) -> None:
            nonlocal sequence, relative
            sequence += 1
            relative += 100
            record: dict[str, object] = {
                "seq": sequence,
                "monotonic_origin_ms": origin,
                "monotonic_ms": relative,
                "type": "snapshot",
                "action": "replace",
                "event": None,
                "label": None,
                "source_generation": 0,
            }
            record.update(values)
            records.append(record)

        for generation in range(1, 11):
            add(
                source_generation=generation,
                action="load" if generation == 1 else "replace",
            )
            for checkpoint in range(4):
                relative += 900
                add(
                    source_generation=generation,
                    action="snapshot",
                    label="rtsp-drain-diagnostics",
                    duration=None,
                    has_audio=True,
                    has_video=True,
                    paused=False,
                    seeking=False,
                    ended=False,
                    audio_monitor_enabled=True,
                    audio_samples_generation=100 + checkpoint,
                    audio_bytes_generation=8192 + checkpoint,
                    audio_nonzero_units_generation=1000 + checkpoint,
                    audio_last_monotonic_ms=relative + 100,
                    audio_last_nonzero_monotonic_ms=relative + 100,
                    time=float(checkpoint),
                )
        add(type="result", action=None, source_generation=10)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            driver.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            done = root / "done"
            done.write_text("0\n", encoding="ascii")
            config_path = root / "config.json"
            config_path.write_bytes(rtsp.canonical_bytes(config(
                caseId="rtsp-live-tcp-drain-diagnostics",
                sourceScheme="rtspt",
                minObservedConnections=10,
            )))
            output = root / "watch.json"
            observed = iter(
                (record["monotonic_origin_ms"] + record["monotonic_ms"] + 1)
                * 1_000_000
                for record in records
            )
            original = rtsp.monotonic_raw_ns
            rtsp.monotonic_raw_ns = lambda: next(observed)
            with mock.patch.object(
                rtsp,
                "require_store_executable",
                return_value=Path("/nix/store/test-tool"),
            ):
                try:
                    rtsp.command_watch(types.SimpleNamespace(
                        config=config_path,
                        driver_json=driver,
                        producer_done=done,
                        output=output,
                        timeout_seconds=1.0,
                        instrumentation="audio-monitor",
                    ))
                finally:
                    rtsp.monotonic_raw_ns = original
            watch = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(watch["sourceGenerations"], 10)
            self.assertEqual(
                [window["generation"] for window in watch["generationWindows"]],
                list(range(1, 11)),
            )
            self.assertEqual(
                [window["labelCount"] for window in watch["generationWindows"]],
                [4] * 10,
            )

    def test_blackhole_watcher_correlates_bounded_api_windows(self) -> None:
        origin = 1_000_000
        sequence = 0
        records: list[dict[str, object]] = []

        def add(relative: int, **values: object) -> None:
            nonlocal sequence
            sequence += 1
            record: dict[str, object] = {
                "seq": sequence,
                "monotonic_origin_ms": origin,
                "monotonic_ms": relative,
                "type": "snapshot",
                "action": "snapshot",
                "event": None,
                "label": None,
                "source_generation": 1,
                "duration": None,
                "has_audio": True,
                "has_video": True,
                "paused": False,
                "seeking": False,
                "ended": False,
                "status": "ok",
                "audio_monitor_enabled": True,
                "audio_samples_generation": 100,
                "audio_bytes_generation": 8192,
                "audio_nonzero_units_generation": 1000,
            }
            record.update(values)
            records.append(record)

        add(100, action="load")
        add(200, type="event", action=None, event="CANPLAY")
        add(300, action="wait_event", label="CANPLAY")
        add(400, action="play")
        add(450, type="event", action=None, event="PLAYING")
        add(500, action="wait_event", label="PLAYING")
        add(1500, action="wait_ms")
        add(1600, label="rtsp-blackhole-precondition")
        add(8000, action="wait_ms")
        add(8100, label="rtsp-blackhole-before-pause")
        add(8200, type="event", action=None, event="PAUSE", paused=True)
        add(8250, action="pause", paused=True)
        add(8300, action="wait_event", label="PAUSE", paused=True)
        add(8300, label="rtsp-blackhole-before-resume", paused=True)
        add(8350, action="play")
        add(8400, type="event", action=None, event="PLAYING")
        add(8450, action="wait_event", label="PLAYING")
        add(8450, label="rtsp-blackhole-before-shutdown")
        add(8550, action="shutdown")
        add(8600, type="result", action=None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "driver.jsonl"
            driver.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            done = root / "done"
            done.write_text("0\n", encoding="ascii")
            config_path = root / "config.json"
            selected = config(
                caseId="rtsp-live-tcp-blackhole-cancel",
                relayMode="server-to-client-blackhole",
                blackholeAfterServerBytes=2097152,
                maxBytesPerDirection=4 * 1024 * 1024,
                minObservedConnections=2,
            )
            config_path.write_bytes(rtsp.canonical_bytes(selected))
            output = root / "watch.json"
            observed = iter(
                (record["monotonic_origin_ms"] + record["monotonic_ms"] + 1) * 1_000_000
                for record in records
            )
            original = rtsp.monotonic_raw_ns
            rtsp.monotonic_raw_ns = lambda: next(observed)
            with mock.patch.object(
                rtsp,
                "require_store_executable",
                return_value=Path("/nix/store/test-tool"),
            ):
                try:
                    rtsp.command_watch(types.SimpleNamespace(
                        config=config_path,
                        driver_json=driver,
                        producer_done=done,
                        output=output,
                        timeout_seconds=1.0,
                        instrumentation="audio-monitor",
                    ))
                finally:
                    rtsp.monotonic_raw_ns = original
            watch = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(
                [window["api"] for window in watch["apiWindows"]],
                ["pause", "resume", "shutdown"],
            )
            self.assertEqual(
                [window["waitCompletedMonotonicNs"] - window["beginMonotonicNs"]
                 for window in watch["apiWindows"]],
                [200_000_000, 150_000_000, 150_000_000],
            )
            pause_window, resume_window, shutdown_window = watch["apiWindows"]
            self.assertEqual(
                pause_window["waitCompletedMonotonicNs"],
                resume_window["beginMonotonicNs"],
            )
            self.assertLess(
                pause_window["waitCompletedSeq"],
                resume_window["beginSeq"],
            )
            self.assertEqual(
                resume_window["waitCompletedMonotonicNs"],
                shutdown_window["beginMonotonicNs"],
            )
            self.assertLess(
                resume_window["waitCompletedSeq"],
                shutdown_window["beginSeq"],
            )

            def reject(mutated: list[dict[str, object]], message: str) -> None:
                for seq, record in enumerate(mutated, 1):
                    record["seq"] = seq
                driver.write_text(
                    "".join(json.dumps(record) + "\n" for record in mutated),
                    encoding="utf-8",
                )
                observed_mutation = iter(
                    (record["monotonic_origin_ms"] + record["monotonic_ms"] + 1)
                    * 1_000_000
                    for record in mutated
                )
                rtsp.monotonic_raw_ns = lambda: next(observed_mutation)
                try:
                    with mock.patch.object(
                        rtsp,
                        "require_store_executable",
                        return_value=Path("/nix/store/test-tool"),
                    ), self.assertRaisesRegex(rtsp.RTSPCaseError, message):
                        rtsp.command_watch(types.SimpleNamespace(
                            config=config_path,
                            driver_json=driver,
                            producer_done=done,
                            output=output,
                            timeout_seconds=1.0,
                            instrumentation="audio-monitor",
                        ))
                finally:
                    rtsp.monotonic_raw_ns = original

            pause_wait_index = next(
                index for index, record in enumerate(records)
                if record.get("action") == "wait_event" and record.get("label") == "PAUSE"
            )
            resume_begin_index = next(
                index for index, record in enumerate(records)
                if record.get("label") == "rtsp-blackhole-before-resume"
            )

            reversed_equal = copy.deepcopy(records)
            reversed_equal[pause_wait_index], reversed_equal[resume_begin_index] = (
                reversed_equal[resume_begin_index],
                reversed_equal[pause_wait_index],
            )
            reject(reversed_equal, "pause API/event ordering")

            true_overlap = copy.deepcopy(records)
            true_overlap[resume_begin_index]["monotonic_ms"] = 8299
            reject(true_overlap, "pause API/event ordering")

            late = copy.deepcopy(records)

            def move_one(
                predicate: Callable[[dict[str, object]], bool],
                relative_ms: int,
            ) -> None:
                matches = [record for record in late if predicate(record)]
                self.assertEqual(len(matches), 1)
                matches[0]["monotonic_ms"] = relative_ms

            move_one(
                lambda record: record.get("action") == "wait_event"
                and record.get("label") == "PAUSE",
                9200,
            )
            move_one(
                lambda record: record.get("label") == "rtsp-blackhole-before-resume",
                9300,
            )
            move_one(
                lambda record: record.get("action") == "play"
                and record["monotonic_ms"] > 8000,
                9350,
            )
            move_one(
                lambda record: record.get("type") == "event"
                and record.get("event") == "PLAYING"
                and record["monotonic_ms"] > 8000,
                9400,
            )
            move_one(
                lambda record: record.get("action") == "wait_event"
                and record.get("label") == "PLAYING"
                and record["monotonic_ms"] > 8000,
                9450,
            )
            move_one(
                lambda record: record.get("label") == "rtsp-blackhole-before-shutdown",
                9450,
            )
            move_one(lambda record: record.get("action") == "shutdown", 9550)
            move_one(lambda record: record.get("type") == "result", 9600)
            reject(late, "pause exceeded one second")

    def test_score_requires_three_distinct_interleaved_media_connections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            for name in ("ffmpeg", "ffprobe", "mediamtx"):
                path = tools / name
                path.write_bytes((name + "\n").encode("ascii"))
                path.chmod(0o700)
            selected = config(
                ffmpegBin=str(tools / "ffmpeg"),
                ffprobeBin=str(tools / "ffprobe"),
                mediamtxBin=str(tools / "mediamtx"),
            )
            config_path = root / "config.json"
            config_path.write_bytes(rtsp.canonical_bytes(selected))
            records = []
            for sequence, start in enumerate((1_000_000_000, 5_000_000_000, 9_000_000_000), 1):
                records.append({
                    "blackhole_after_server_bytes": 0,
                    "blackhole_started_monotonic_ns": None,
                    "case_id": selected["caseId"],
                    "client_to_server_bytes": 1024,
                    "client_to_server_bytes_after_blackhole": 0,
                    "clock_basis": rtsp.CLOCK_BASIS,
                    "connection_seq": sequence,
                    "finished_monotonic_ns": start + 3_000_000_000,
                    "interleaved_tcp_setups": 2,
                    "relay_mode": "forward",
                    "schema": 1,
                    "server_to_client_bytes": 8192,
                    "started_monotonic_ns": start,
                    "status": "eof",
                    "terminal_reason": "client-eof",
                })
            log = root / "connections.jsonl"
            log.write_bytes(b"".join(rtsp.canonical_bytes(record) for record in records))
            completion = {
                "acceptedConnectionCount": 3,
                "caseId": selected["caseId"],
                "clockBasis": rtsp.CLOCK_BASIS,
                "ffmpegSha256": rtsp.sha256_file(tools / "ffmpeg"),
                "ffprobeSha256": rtsp.sha256_file(tools / "ffprobe"),
                "fixtureSha256": selected["fixtureSha256"],
                "logBytes": log.stat().st_size,
                "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "mediamtxSha256": rtsp.sha256_file(tools / "mediamtx"),
                "nativeAudioNonzeroUnits": 1000,
                "nativeAudioPcmBytes": 384000,
                "nativeVideoFramesMinimum": 30,
                "rejectedConnectionCount": 0,
                "schema": 1,
                "status": "complete",
                "terminalRecordCount": 3,
            }
            completion_path = root / "complete.json"
            completion_path.write_bytes(rtsp.canonical_bytes(completion))
            windows = []
            for generation, start in enumerate((1_000_000_000, 5_000_000_000, 9_000_000_000), 1):
                windows.append({
                    "firstMonotonicNs": start + 100_000_000,
                    "generation": generation,
                    "labelCount": 4,
                    "lastMonotonicNs": start + 2_500_000_000,
                    "maxMediaTime": 3.0,
                    "minMediaTime": 0.0,
                })
            watch_path = root / "watch.json"
            watch_path.write_bytes(rtsp.canonical_bytes({
                "clockBasis": rtsp.CLOCK_BASIS,
                "generationWindows": windows,
                "maxObservationLagMs": 2,
                "resultMonotonicNs": 12_000_000_000,
                "schema": 1,
                "sourceGenerations": 3,
            }))
            output = root / "score.json"
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                rtsp.command_score(types.SimpleNamespace(
                    config=config_path,
                    http_log=log,
                    service_completion=completion_path,
                    watch=watch_path,
                    output=output,
                ))
            self.assertEqual(json.loads(output.read_text(encoding="ascii"))["matchedGenerationConnections"], 3)

            selected["sourceScheme"] = "rtspt"
            config_path.write_bytes(rtsp.canonical_bytes(selected))
            records[1]["interleaved_tcp_setups"] = 0
            log.write_bytes(b"".join(rtsp.canonical_bytes(record) for record in records))
            completion["logBytes"] = log.stat().st_size
            completion["logSha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
            completion_path.write_bytes(rtsp.canonical_bytes(completion))
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                with self.assertRaisesRegex(
                    rtsp.RTSPCaseError,
                    "generation 2 lacks two interleaved-TCP SETUP requests",
                ):
                    rtsp.command_score(types.SimpleNamespace(
                        config=config_path,
                        http_log=log,
                        service_completion=completion_path,
                        watch=watch_path,
                        output=output,
                    ))

    def test_blackhole_score_rejects_timing_cutoff_and_terminal_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            for name in ("ffmpeg", "ffprobe", "mediamtx"):
                path = tools / name
                path.write_bytes((name + "\n").encode("ascii"))
                path.chmod(0o700)
            threshold = 2097152
            selected = config(
                caseId="rtsp-live-tcp-blackhole-cancel",
                relayMode="server-to-client-blackhole",
                blackholeAfterServerBytes=threshold,
                maxBytesPerDirection=4 * 1024 * 1024,
                minObservedConnections=2,
                ffmpegBin=str(tools / "ffmpeg"),
                ffprobeBin=str(tools / "ffprobe"),
                mediamtxBin=str(tools / "mediamtx"),
            )
            config_path = root / "config.json"
            config_path.write_bytes(rtsp.canonical_bytes(selected))
            base_records = [
                {
                    "blackhole_after_server_bytes": threshold,
                    "blackhole_started_monotonic_ns": None,
                    "case_id": selected["caseId"],
                    "client_to_server_bytes": 0,
                    "client_to_server_bytes_after_blackhole": 0,
                    "clock_basis": rtsp.CLOCK_BASIS,
                    "connection_seq": 1,
                    "finished_monotonic_ns": 600_000_000,
                    "interleaved_tcp_setups": 0,
                    "relay_mode": selected["relayMode"],
                    "schema": 1,
                    "server_to_client_bytes": 0,
                    "started_monotonic_ns": 500_000_000,
                    "status": "eof",
                    "terminal_reason": "client-eof",
                },
                {
                    "blackhole_after_server_bytes": threshold,
                    "blackhole_started_monotonic_ns": 3_000_000_000,
                    "case_id": selected["caseId"],
                    "client_to_server_bytes": 1024,
                    "client_to_server_bytes_after_blackhole": 64,
                    "clock_basis": rtsp.CLOCK_BASIS,
                    "connection_seq": 2,
                    "finished_monotonic_ns": 4_030_000_000,
                    "interleaved_tcp_setups": 2,
                    "relay_mode": selected["relayMode"],
                    "schema": 1,
                    "server_to_client_bytes": threshold,
                    "started_monotonic_ns": 1_000_000_000,
                    "status": "eof",
                    "terminal_reason": "client-eof",
                },
            ]
            base_watch = {
                "apiWindows": [
                    {
                        "api": "pause",
                        "beginMonotonicNs": 4_000_000_000,
                        "beginSeq": 20,
                        "eventMonotonicNs": 4_000_000_000,
                        "eventSeq": 22,
                        "maxLatencyMs": 1000,
                        "returnMonotonicNs": 4_000_000_000,
                        "returnSeq": 24,
                        "waitCompletedMonotonicNs": 4_000_000_000,
                        "waitCompletedSeq": 25,
                    },
                    {
                        "api": "resume",
                        "beginMonotonicNs": 4_000_000_000,
                        "beginSeq": 26,
                        "eventMonotonicNs": 4_012_000_000,
                        "eventSeq": 31,
                        "maxLatencyMs": 1000,
                        "returnMonotonicNs": 4_009_000_000,
                        "returnSeq": 30,
                        "waitCompletedMonotonicNs": 4_012_000_000,
                        "waitCompletedSeq": 32,
                    },
                    {
                        "api": "shutdown",
                        "beginMonotonicNs": 4_012_000_000,
                        "beginSeq": 33,
                        "eventMonotonicNs": None,
                        "eventSeq": None,
                        "maxLatencyMs": 5000,
                        "returnMonotonicNs": 4_040_000_000,
                        "returnSeq": 34,
                        "waitCompletedMonotonicNs": 4_040_000_000,
                        "waitCompletedSeq": 35,
                    },
                ],
                "clockBasis": rtsp.CLOCK_BASIS,
                "preconditionMonotonicNs": 2_000_000_000,
                "preconditionSeq": 18,
                "relayMode": selected["relayMode"],
                "resultMonotonicNs": 4_040_000_000,
                "resultSeq": 35,
                "schema": 1,
                "sourceGenerations": 1,
            }
            log = root / "connections.jsonl"
            completion_path = root / "complete.json"
            watch_path = root / "watch.json"
            output = root / "score.json"

            def write_inputs(records: list[dict[str, object]], watch: dict[str, object]) -> None:
                log.write_bytes(b"".join(rtsp.canonical_bytes(record) for record in records))
                completion = {
                    "acceptedConnectionCount": len(records),
                    "caseId": selected["caseId"],
                    "clockBasis": rtsp.CLOCK_BASIS,
                    "ffmpegSha256": rtsp.sha256_file(tools / "ffmpeg"),
                    "ffprobeSha256": rtsp.sha256_file(tools / "ffprobe"),
                    "fixtureSha256": selected["fixtureSha256"],
                    "logBytes": log.stat().st_size,
                    "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "mediamtxSha256": rtsp.sha256_file(tools / "mediamtx"),
                    "nativeAudioNonzeroUnits": 1000,
                    "nativeAudioPcmBytes": 384000,
                    "nativeVideoFramesMinimum": 30,
                    "rejectedConnectionCount": 0,
                    "schema": 1,
                    "status": "complete",
                    "terminalRecordCount": len(records),
                }
                completion_path.write_bytes(rtsp.canonical_bytes(completion))
                watch_path.write_bytes(rtsp.canonical_bytes(watch))

            def score() -> None:
                rtsp.command_score(types.SimpleNamespace(
                    config=config_path,
                    http_log=log,
                    service_completion=completion_path,
                    watch=watch_path,
                    output=output,
                ))

            write_inputs(base_records, base_watch)
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                score()
            passed = json.loads(output.read_text(encoding="ascii"))
            self.assertTrue(passed["clientDrivenEof"])
            self.assertEqual(passed["blackholeAfterServerBytes"], threshold)

            def pause_late(
                records: list[dict[str, object]],
                watch: dict[str, object],
            ) -> None:
                windows = watch["apiWindows"]
                windows[0].update(
                    waitCompletedMonotonicNs=5_100_000_000,
                    waitCompletedSeq=25,
                )
                windows[1].update(
                    beginMonotonicNs=5_200_000_000,
                    returnMonotonicNs=5_209_000_000,
                    eventMonotonicNs=5_212_000_000,
                    waitCompletedMonotonicNs=5_212_000_000,
                )
                windows[2].update(
                    beginMonotonicNs=5_212_000_000,
                    returnMonotonicNs=5_240_000_000,
                    waitCompletedMonotonicNs=5_240_000_000,
                )
                watch.update(resultMonotonicNs=5_240_000_000)
                records[1].update(finished_monotonic_ns=5_230_000_000)

            def resume_late(
                records: list[dict[str, object]],
                watch: dict[str, object],
            ) -> None:
                windows = watch["apiWindows"]
                windows[1].update(
                    eventMonotonicNs=5_200_000_000,
                    waitCompletedMonotonicNs=5_200_000_000,
                )
                windows[2].update(
                    beginMonotonicNs=5_300_000_000,
                    returnMonotonicNs=5_330_000_000,
                    waitCompletedMonotonicNs=5_340_000_000,
                    waitCompletedSeq=35,
                )
                watch.update(
                    resultMonotonicNs=5_340_000_000,
                    resultSeq=35,
                )
                records[1].update(finished_monotonic_ns=5_320_000_000)

            mutations = {
                "cutoff-off-by-one": lambda records, _watch: records[1].update(
                    server_to_client_bytes=threshold - 1
                ),
                "upstream-eof": lambda records, _watch: records[1].update(
                    terminal_reason="upstream-eof"
                ),
                "service-stop": lambda records, _watch: records[1].update(
                    status="service-stop", terminal_reason="service-stop"
                ),
                "cutoff-after-pause": lambda records, _watch: records[1].update(
                    blackhole_started_monotonic_ns=4_100_000_000
                ),
                "equal-time-sequence-reversal": lambda _records, watch: (
                    watch["apiWindows"][1].update(beginSeq=24)
                ),
                "true-timestamp-overlap": lambda _records, watch: (
                    watch["apiWindows"][1].update(beginMonotonicNs=3_999_999_999)
                ),
                "pause-over-one-second": pause_late,
                "resume-over-one-second": resume_late,
                "shutdown-over-five-seconds": lambda _records, watch: (
                    watch["apiWindows"][2].update(
                        returnMonotonicNs=9_013_000_000,
                        waitCompletedMonotonicNs=9_013_000_000,
                    ),
                    watch.update(resultMonotonicNs=9_013_000_000),
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    records = copy.deepcopy(base_records)
                    watch = copy.deepcopy(base_watch)
                    mutate(records, watch)
                    write_inputs(records, watch)
                    with mock.patch.object(rtsp, "validate_config", return_value=selected):
                        with self.assertRaises(rtsp.RTSPCaseError):
                            score()

    def test_finite_hold_score_requires_duration_and_same_media_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            for name in ("ffmpeg", "ffprobe", "mediamtx"):
                path = tools / name
                path.write_bytes((name + "\n").encode("ascii"))
                path.chmod(0o700)
            threshold = 64 * 1024
            selected = config(
                caseId="rtsp-live-tcp-finite-hold-recovery",
                relayMode="server-to-client-finite-hold",
                blackholeAfterServerBytes=threshold,
                holdDurationMs=3000,
                minObservedConnections=2,
                ffmpegBin=str(tools / "ffmpeg"),
                ffprobeBin=str(tools / "ffprobe"),
                mediamtxBin=str(tools / "mediamtx"),
            )
            config_path = root / "config.json"
            config_path.write_bytes(rtsp.canonical_bytes(selected))
            base_records = [
                {
                    "blackhole_after_server_bytes": threshold,
                    "blackhole_started_monotonic_ns": None,
                    "case_id": selected["caseId"],
                    "client_to_server_bytes": 0,
                    "client_to_server_bytes_after_blackhole": 0,
                    "clock_basis": rtsp.CLOCK_BASIS,
                    "connection_seq": 1,
                    "finished_monotonic_ns": 600_000_000,
                    "hold_resumed_monotonic_ns": None,
                    "interleaved_tcp_setups": 0,
                    "relay_mode": selected["relayMode"],
                    "schema": 1,
                    "server_to_client_bytes": 0,
                    "started_monotonic_ns": 500_000_000,
                    "status": "eof",
                    "terminal_reason": "client-eof",
                },
                {
                    "blackhole_after_server_bytes": threshold,
                    "blackhole_started_monotonic_ns": 3_000_000_000,
                    "case_id": selected["caseId"],
                    "client_to_server_bytes": 1024,
                    "client_to_server_bytes_after_blackhole": 64,
                    "clock_basis": rtsp.CLOCK_BASIS,
                    "connection_seq": 2,
                    "finished_monotonic_ns": 10_100_000_000,
                    "hold_resumed_monotonic_ns": 6_000_000_000,
                    "interleaved_tcp_setups": 2,
                    "relay_mode": selected["relayMode"],
                    "schema": 1,
                    "server_to_client_bytes": threshold + 8192,
                    "started_monotonic_ns": 1_000_000_000,
                    "status": "eof",
                    "terminal_reason": "client-reset",
                },
            ]
            watch = {
                "clockBasis": rtsp.CLOCK_BASIS,
                "generationWindows": [{
                    "firstMonotonicNs": 7_000_000_000,
                    "generation": 1,
                    "labelCount": 4,
                    "lastMonotonicNs": 10_000_000_000,
                    "maxMediaTime": 4.0,
                    "minMediaTime": 1.0,
                }],
                "holdPreconditionMonotonicNs": 2_000_000_000,
                "holdPreconditionSeq": 18,
                "maxObservationLagMs": 2,
                "resultMonotonicNs": 10_200_000_000,
                "schema": 1,
                "sourceGenerations": 1,
            }
            log = root / "connections.jsonl"
            completion_path = root / "complete.json"
            watch_path = root / "watch.json"
            output = root / "score.json"

            def write_inputs(records: list[dict[str, object]]) -> None:
                log.write_bytes(b"".join(rtsp.canonical_bytes(record) for record in records))
                completion = {
                    "acceptedConnectionCount": len(records),
                    "caseId": selected["caseId"],
                    "clockBasis": rtsp.CLOCK_BASIS,
                    "ffmpegSha256": rtsp.sha256_file(tools / "ffmpeg"),
                    "ffprobeSha256": rtsp.sha256_file(tools / "ffprobe"),
                    "fixtureSha256": selected["fixtureSha256"],
                    "logBytes": log.stat().st_size,
                    "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "mediamtxSha256": rtsp.sha256_file(tools / "mediamtx"),
                    "nativeAudioNonzeroUnits": 1000,
                    "nativeAudioPcmBytes": 384000,
                    "nativeVideoFramesMinimum": 30,
                    "rejectedConnectionCount": 0,
                    "schema": 1,
                    "status": "complete",
                    "terminalRecordCount": len(records),
                }
                completion_path.write_bytes(rtsp.canonical_bytes(completion))
                watch_path.write_bytes(rtsp.canonical_bytes(watch))

            def score() -> None:
                rtsp.command_score(types.SimpleNamespace(
                    config=config_path,
                    http_log=log,
                    service_completion=completion_path,
                    watch=watch_path,
                    output=output,
                ))

            write_inputs(base_records)
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                score()
            passed = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(passed["holdDurationMs"], 3000)

            short_hold = copy.deepcopy(base_records)
            short_hold[1]["hold_resumed_monotonic_ns"] = 3_100_000_000
            write_inputs(short_hold)
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                with self.assertRaisesRegex(
                    rtsp.RTSPCaseError,
                    "finite hold did not resume",
                ):
                    score()

            reconnect = copy.deepcopy(base_records)
            reconnect.append({
                "blackhole_after_server_bytes": threshold,
                "blackhole_started_monotonic_ns": None,
                "case_id": selected["caseId"],
                "client_to_server_bytes": 1024,
                "client_to_server_bytes_after_blackhole": 0,
                "clock_basis": rtsp.CLOCK_BASIS,
                "connection_seq": 3,
                "finished_monotonic_ns": 10_050_000_000,
                "hold_resumed_monotonic_ns": None,
                "interleaved_tcp_setups": 2,
                "relay_mode": selected["relayMode"],
                "schema": 1,
                "server_to_client_bytes": 8192,
                "started_monotonic_ns": 6_100_000_000,
                "status": "eof",
                "terminal_reason": "client-eof",
            })
            write_inputs(reconnect)
            with mock.patch.object(rtsp, "validate_config", return_value=selected):
                with self.assertRaisesRegex(
                    rtsp.RTSPCaseError,
                    "not isolated to the impaired connection",
                ):
                    score()


if __name__ == "__main__":
    unittest.main()
