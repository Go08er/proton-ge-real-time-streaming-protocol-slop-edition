# SPDX-License-Identifier: BSD-3-Clause
"""Pure contracts and opt-in loopback checks for the host HLS adapter."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
LIVE_DIR = ROOT.parent / "media-engine-stress" / "live-fixture"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE_DIR))
import live_hls_case as case
import test_live_fixture as fixture_test


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(case.canonical_bytes(value))


def config(port: int = 18082, source: str = "mux") -> dict[str, object]:
    value: dict[str, object] = {
        "adapterSha256": digest(Path(case.__file__).resolve()),
        "autoAdvance": True,
        "bind": "127.0.0.1",
        "caseId": f"hls-live-{source}-test",
        "controlToken": "host-live-control-token",
        "faultMode": "normal",
        "initialGeneration": 0,
        "maxConcurrent": 8,
        "maxErrorResponses": 0,
        "maxLogBytes": 1024 * 1024,
        "maxRequests": 128,
        "maxStartupMs": 10_000,
        "minPlaybackSpan": 5.0,
        "port": port,
        "publisherSha256": digest(case.LIVE_FIXTURE_PATH),
        "requiredInstrumentation": "audio-monitor",
        "schema": 1,
        "service": "live-hls-v1",
        "source": source,
    }
    if source == "mux":
        value.update({
            "minMuxPlaylistGenerations": 3,
            "minMuxPlaylistRequests": 3,
            "minMuxSegmentRequests": 6,
        })
    elif source == "separate":
        value.update({
            "minAudioPlaylistRequests": 3,
            "minAudioSegmentRequests": 3,
            "minMasterRequests": 1,
            "minPairedPlaylistGenerations": 3,
            "minVideoPlaylistRequests": 3,
            "minVideoSegmentRequests": 3,
        })
    return value


def scenario(port: int, source: str = "mux") -> str:
    endpoint = "mux/index.m3u8" if source == "mux" else "separate/master.m3u8"
    return "\n".join((
        f'load "http://127.0.0.1:{port}/{endpoint}"',
        "wait_event CANPLAY 30000",
        "play",
        "wait_event PLAYING 15000",
        "loop 8",
        "wait_ms 1000",
        'snapshot "live-steady"',
        "endloop",
        "shutdown",
        "",
    ))


def driver_records() -> tuple[list[dict[str, object]], int]:
    observed_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    origin_ms = observed_ns // 1_000_000 - 8_000
    records: list[dict[str, object]] = [{
        "event": "PLAYING",
        "generation_current": True,
        "monotonic_ms": 500,
        "monotonic_origin_ms": origin_ms,
        "seq": 1,
        "source_generation": 1,
        "type": "event",
    }]
    for index in range(8):
        records.append({
            "action": "snapshot",
            "audio_bytes_generation": 10_000 + index * 1_000,
            "audio_last_nonzero_monotonic_ms": 900 + index * 1_000,
            "audio_monitor_enabled": True,
            "audio_nonzero_units_generation": 100 + index * 10,
            "audio_payload_format": "pcm16",
            "audio_payload_observed": True,
            "audio_samples_generation": 100 + index * 10,
            "duration": None,
            "ended": False,
            "has_audio": True,
            "has_video": True,
            "label": "live-steady",
            "monotonic_ms": 1_000 + index * 1_000,
            "monotonic_origin_ms": origin_ms,
            "paused": False,
            "seeking": False,
            "seq": len(records) + 1,
            "source_generation": 1,
            "status": "ok",
            "time": 10.0 + index,
            "type": "snapshot",
        })
    records.append({
        "monotonic_ms": 8_001,
        "monotonic_origin_ms": origin_ms,
        "exit_code": 0,
        "seq": len(records) + 1,
        "status": "pass",
        "type": "result",
    })
    return records, observed_ns


def request_record(sequence: int, route: str, generation: int, started_ns: int,
                   case_id: str = "hls-live-mux-test") -> dict[str, object]:
    return {
        "bytes_sent": 100,
        "case_id": case_id,
        "clock_basis": case.CLOCK_BASIS,
        "fault_mode": "normal",
        "finished_monotonic_ns": started_ns + 1_000_000,
        "generation": generation,
        "method": "GET",
        "request_seq": sequence,
        "route": route,
        "schema": 1,
        "started_monotonic_ns": started_ns,
        "status": 200,
    }


class LiveHlsCaseContractTests(unittest.TestCase):
    def test_case_builder_binds_the_selected_audio_observation_mode(self) -> None:
        generator = (ROOT / "live-hls-case.nix").read_text(encoding="utf-8")
        self.assertIn('instrumentation ? "audio-monitor"', generator)
        self.assertIn(
            'assert builtins.elem instrumentation [ "audio-monitor" "endpoint-monitor" ];',
            generator,
        )
        self.assertIn("--replace-fail '@INSTRUMENTATION@' ${instrumentation}", generator)
        for name in (
            "live-hls-muxed.service.json.in",
            "live-hls-separate.service.json.in",
        ):
            template = (ROOT / "examples" / name).read_text(encoding="utf-8")
            self.assertEqual(template.count("@INSTRUMENTATION@"), 1)

    def test_config_accepts_only_declared_healthy_audio_monitored_topologies(self) -> None:
        case.validate_config(config())
        case.validate_config(config(source="separate"))
        endpoint = config()
        endpoint["requiredInstrumentation"] = "endpoint-monitor"
        case.validate_config(endpoint)
        for field, value in (
            ("bind", "localhost"),
            ("source", "other"),
            ("faultMode", "audio-404"),
            ("requiredInstrumentation", "control"),
        ):
            changed = copy.deepcopy(config())
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(case.LiveCaseError):
                case.validate_config(changed)

    def test_fixture_scenario_closure_is_exact_and_nonseeking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = fixture_test.SyntheticFixture(root / "fixture")
            config_path = root / "config.json"
            scenario_path = root / "scenario"
            for source in ("mux", "separate"):
                write_json(config_path, config(source=source))
                scenario_path.write_text(scenario(18082, source), encoding="utf-8")
                selected, bundle = case.validate_inputs(
                    config_path,
                    fixture.root,
                    fixture.root / "provenance/manifest.json",
                    scenario_path,
                )
                self.assertEqual(source, selected["source"])
                self.assertEqual("full", bundle.ready["profile"])
                config_path.unlink()
            scenario_path.write_text(scenario(18082).replace("play\n", "seek 2\nplay\n"), encoding="utf-8")
            write_json(config_path, config())
            with self.assertRaisesRegex(case.LiveCaseError, "cannot replace or seek"):
                case.validate_inputs(
                    config_path,
                    fixture.root,
                    fixture.root / "provenance/manifest.json",
                    scenario_path,
                )

    def test_watch_requires_null_duration_and_continuous_active_av(self) -> None:
        records, observed_ns = driver_records()
        value = case.live_watch_value(records, observed_ns, 5.0, "audio-monitor")
        self.assertEqual(8, value["snapshotCount"])
        self.assertEqual(7.0, value["playbackSpan"])
        self.assertLessEqual(value["playingMonotonicNs"], value["firstPlaybackMonotonicNs"])
        changed = copy.deepcopy(records)
        changed[2]["duration"] = 14.0
        with self.assertRaisesRegex(case.LiveCaseError, "finite seekable duration"):
            case.live_watch_value(changed, observed_ns, 5.0, "audio-monitor")
        changed = copy.deepcopy(records)
        changed[4]["has_audio"] = False
        with self.assertRaisesRegex(case.LiveCaseError, "simultaneous audio and video"):
            case.live_watch_value(changed, observed_ns, 5.0, "audio-monitor")

        endpoint_records = copy.deepcopy(records)
        for record in endpoint_records:
            if record.get("label") == "live-steady":
                record["audio_monitor_enabled"] = False
                record["audio_bytes_generation"] = 0
                record["audio_last_nonzero_monotonic_ms"] = None
                record["audio_nonzero_units_generation"] = 0
                record["audio_payload_format"] = "none"
                record["audio_payload_observed"] = False
                record["audio_samples_generation"] = 0
        endpoint_value = case.live_watch_value(
            endpoint_records, observed_ns, 5.0, "endpoint-monitor"
        )
        self.assertEqual(endpoint_value["snapshotCount"], 8)

    def test_incremental_watch_calibrates_records_when_observed(self) -> None:
        records, _observed_ns = driver_records()
        observed_times = [
            (record["monotonic_origin_ms"] + record["monotonic_ms"]) * 1_000_000
            + 1_000_000
            for record in records
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            driver_path = root / "driver.jsonl"
            done_path = root / "producer.done"
            output_path = root / "watch.json"
            write_json(config_path, config())
            driver_path.write_bytes(b"".join(case.canonical_bytes(record) for record in records))
            done_path.write_bytes(b"done\n")
            with mock.patch.object(
                case.time,
                "clock_gettime_ns",
                side_effect=observed_times,
            ):
                case.command_watch(SimpleNamespace(
                    config=config_path,
                    driver_json=driver_path,
                    instrumentation="audio-monitor",
                    output=output_path,
                    producer_done=done_path,
                    timeout_seconds=1,
                ))
            value = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(1, value["maxObservationLagMs"])
            self.assertEqual(observed_times[-2], value["lastPlaybackObservedMonotonicNs"])
            self.assertEqual(8, value["snapshotCount"])
            mismatched = config()
            mismatched["requiredInstrumentation"] = "endpoint-monitor"
            write_json(config_path, mismatched)
            with self.assertRaisesRegex(case.LiveCaseError, "differs from its immutable config"):
                case.command_watch(SimpleNamespace(
                    config=config_path,
                    driver_json=driver_path,
                    instrumentation="audio-monitor",
                    output=output_path,
                    producer_done=done_path,
                    timeout_seconds=1,
                ))

    def test_transport_score_requires_advancing_playlist_during_playback(self) -> None:
        records, observed_ns = driver_records()
        watch = case.live_watch_value(records, observed_ns, 5.0, "audio-monitor")
        first_ns = watch["firstPlaybackMonotonicNs"]
        playing_ns = watch["playingMonotonicNs"]
        evidence = [
            request_record(1, case.live_fixture.ROUTE_MUX_PLAYLIST, 0, playing_ns - 100_000_000),
            request_record(2, case.live_fixture.ROUTE_MUX_PLAYLIST, 1, first_ns + 500_000_000),
            request_record(3, case.live_fixture.ROUTE_MUX_PLAYLIST, 2, first_ns + 2_500_000_000),
        ]
        for index in range(6):
            evidence.append(request_record(
                len(evidence) + 1,
                case.live_fixture.ROUTE_MUX_SEGMENT,
                min(index // 3, 2),
                first_ns + (index + 1) * 100_000_000,
            ))
        payload = b"".join(case.canonical_bytes(record) for record in evidence)
        completion = {
            "allocatedRequestCount": len(evidence),
            "caseId": "hls-live-mux-test",
            "clockBasis": case.CLOCK_BASIS,
            "faultMode": "normal",
            "finalGeneration": 4,
            "logBytes": len(payload),
            "logSha256": hashlib.sha256(payload).hexdigest(),
            "rejectedConnectionCount": 0,
            "schema": 1,
            "status": "complete",
            "terminalRecordCount": len(evidence),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("config", "watch", "requests", "completion", "summary")}
            write_json(paths["config"], config())
            write_json(paths["watch"], watch)
            paths["requests"].write_bytes(payload)
            write_json(paths["completion"], completion)
            case.command_score(SimpleNamespace(
                config=paths["config"],
                watch=paths["watch"],
                http_log=paths["requests"],
                service_completion=paths["completion"],
                output=paths["summary"],
            ))
            summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
            self.assertEqual("passed", summary["status"])
            self.assertEqual(3, summary["muxPlaylistGenerationCount"])
            self.assertEqual(6, summary["muxSegmentRequestCount"])
            self.assertLessEqual(summary["startupAfterRequestMs"], 10_000)

            strict_startup = config()
            strict_startup["maxStartupMs"] = 1
            write_json(paths["config"], strict_startup)
            paths["summary"].unlink()
            with self.assertRaisesRegex(case.LiveCaseError, "startup exceeded"):
                case.command_score(SimpleNamespace(
                    config=paths["config"],
                    watch=paths["watch"],
                    http_log=paths["requests"],
                    service_completion=paths["completion"],
                    output=paths["summary"],
                ))

    def test_separate_score_requires_matching_generations_and_both_tracks_during_playback(self) -> None:
        records, observed_ns = driver_records()
        watch = case.live_watch_value(records, observed_ns, 5.0, "audio-monitor")
        first_ns = watch["firstPlaybackMonotonicNs"]
        case_id = "hls-live-separate-test"
        evidence = [request_record(
            1, case.live_fixture.ROUTE_MASTER, 0, watch["playingMonotonicNs"] - 100_000_000,
            case_id,
        )]
        for generation in range(3):
            for route in (
                case.live_fixture.ROUTE_AUDIO_PLAYLIST,
                case.live_fixture.ROUTE_VIDEO_PLAYLIST,
                case.live_fixture.ROUTE_AUDIO_SEGMENT,
                case.live_fixture.ROUTE_VIDEO_SEGMENT,
            ):
                evidence.append(request_record(
                    len(evidence) + 1,
                    route,
                    generation,
                    first_ns + (generation + 1) * 500_000_000,
                    case_id,
                ))
        payload = b"".join(case.canonical_bytes(record) for record in evidence)
        completion = {
            "allocatedRequestCount": len(evidence),
            "caseId": case_id,
            "clockBasis": case.CLOCK_BASIS,
            "faultMode": "normal",
            "finalGeneration": 4,
            "logBytes": len(payload),
            "logSha256": hashlib.sha256(payload).hexdigest(),
            "rejectedConnectionCount": 0,
            "schema": 1,
            "status": "complete",
            "terminalRecordCount": len(evidence),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("config", "watch", "requests", "completion", "summary")}
            write_json(paths["config"], config(source="separate"))
            write_json(paths["watch"], watch)
            paths["requests"].write_bytes(payload)
            write_json(paths["completion"], completion)
            arguments = SimpleNamespace(
                config=paths["config"],
                watch=paths["watch"],
                http_log=paths["requests"],
                service_completion=paths["completion"],
                output=paths["summary"],
            )
            case.command_score(arguments)
            summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
            self.assertEqual("live-hls-separate-qualification", summary["role"])
            self.assertEqual(3, summary["pairedPlaylistGenerationCount"])

            paths["summary"].unlink()
            broken = copy.deepcopy(evidence)
            for record in broken:
                if record["route"] == case.live_fixture.ROUTE_VIDEO_PLAYLIST:
                    record["generation"] += 10
            broken_payload = b"".join(case.canonical_bytes(record) for record in broken)
            paths["requests"].write_bytes(broken_payload)
            completion["logBytes"] = len(broken_payload)
            completion["logSha256"] = hashlib.sha256(broken_payload).hexdigest()
            write_json(paths["completion"], completion)
            with self.assertRaisesRegex(case.LiveCaseError, "matching generations"):
                case.command_score(arguments)


class LoopbackLiveHlsCaseTests(unittest.TestCase):
    def test_service_seals_bounded_evidence_after_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = fixture_test.SyntheticFixture(root / "fixture")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            selected = config(port)
            selected["minMuxPlaylistGenerations"] = 1
            selected["minMuxPlaylistRequests"] = 1
            selected["minMuxSegmentRequests"] = 1
            config_path = root / "config.json"
            scenario_path = root / "scenario"
            log_path = root / "requests.jsonl"
            completion_path = root / "completion.json"
            write_json(config_path, selected)
            scenario_path.write_text(scenario(port), encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable, "-I", str(ROOT / "live_hls_case.py"), "serve",
                    "--config", str(config_path),
                    "--fixture-root", str(fixture.root),
                    "--fixture-manifest", str(fixture.root / "provenance/manifest.json"),
                    "--scenario", str(scenario_path),
                    "--log", str(log_path),
                    "--completion-marker", str(completion_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/mux/index.m3u8", timeout=0.2) as response:
                            playlist = response.read().decode("ascii")
                        break
                    except OSError:
                        time.sleep(0.02)
                else:
                    self.fail("live HLS service did not become ready")
                segment = next(line for line in playlist.splitlines() if line and not line.startswith("#"))
                with urlopen(f"http://127.0.0.1:{port}/mux/{segment}", timeout=2) as response:
                    self.assertTrue(response.read())
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(0, process.returncode, stderr)
            self.assertIn('"service":"live-hls-v1"', stdout)
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            self.assertEqual(completion["allocatedRequestCount"], completion["terminalRecordCount"])
            self.assertEqual(0, completion["rejectedConnectionCount"])
            self.assertEqual(completion["logSha256"], digest(log_path))

    def test_separate_service_publishes_master_and_both_tracks_from_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = fixture_test.SyntheticFixture(root / "fixture")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            selected = config(port, "separate")
            for field in (
                "minAudioPlaylistRequests",
                "minAudioSegmentRequests",
                "minMasterRequests",
                "minPairedPlaylistGenerations",
                "minVideoPlaylistRequests",
                "minVideoSegmentRequests",
            ):
                selected[field] = 1
            config_path = root / "config.json"
            scenario_path = root / "scenario"
            log_path = root / "requests.jsonl"
            completion_path = root / "completion.json"
            write_json(config_path, selected)
            scenario_path.write_text(scenario(port, "separate"), encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable, "-I", str(ROOT / "live_hls_case.py"), "serve",
                    "--config", str(config_path),
                    "--fixture-root", str(fixture.root),
                    "--fixture-manifest", str(fixture.root / "provenance/manifest.json"),
                    "--scenario", str(scenario_path),
                    "--log", str(log_path),
                    "--completion-marker", str(completion_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with urlopen(
                            f"http://127.0.0.1:{port}/separate/master.m3u8", timeout=0.2
                        ) as response:
                            master = response.read().decode("ascii")
                        break
                    except OSError:
                        time.sleep(0.02)
                else:
                    self.fail("separate live HLS service did not become ready")
                self.assertIn("audio/index.m3u8", master)
                self.assertIn("video/index.m3u8", master)
                for track in ("audio", "video"):
                    with urlopen(
                        f"http://127.0.0.1:{port}/separate/{track}/index.m3u8", timeout=2
                    ) as response:
                        playlist = response.read().decode("ascii")
                        generation = response.headers["X-Fixture-Generation"]
                    self.assertEqual("0", generation)
                    segment = next(
                        line for line in playlist.splitlines() if line and not line.startswith("#")
                    )
                    with urlopen(
                        f"http://127.0.0.1:{port}/separate/{track}/{segment}", timeout=2
                    ) as response:
                        self.assertTrue(response.read())
                        self.assertEqual("0", response.headers["X-Fixture-Generation"])
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(0, process.returncode, stderr)
            self.assertIn('"separate":"http://127.0.0.1:', stdout)
            routes = {
                json.loads(line)["route"] for line in log_path.read_text(encoding="utf-8").splitlines()
            }
            self.assertEqual({
                case.live_fixture.ROUTE_MASTER,
                case.live_fixture.ROUTE_AUDIO_PLAYLIST,
                case.live_fixture.ROUTE_AUDIO_SEGMENT,
                case.live_fixture.ROUTE_VIDEO_PLAYLIST,
                case.live_fixture.ROUTE_VIDEO_SEGMENT,
            }, routes)


if __name__ == "__main__":
    unittest.main()
