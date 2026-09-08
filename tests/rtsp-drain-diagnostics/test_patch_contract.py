#!/usr/bin/env python3
"""Patch-surface, privacy, and immutability checks for diagnostic Patch 0019."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/ge-proton11-6/0019-winedmo-trace-bounded-rtsp-drain-state.patch"
SERIES = ROOT / "patches/series"
MONOLITH = ROOT / "patches/ge-proton11-6/0004-winedmo-async-http-and-repair-vod-seeking.patch"
MONOLITH_SHA256 = "dbea5107f2073cfee6ac65610967f1fbb66036d296b3067c0c9374e8ae33687a"


def added_lines(text: str) -> str:
    return "\n".join(
        line[1:]
        for line in text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


class PatchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patch = PATCH.read_text(encoding="utf-8")
        cls.added = added_lines(cls.patch)

    def test_patch_targets_only_diagnostic_boundaries_and_is_final_in_series(self) -> None:
        targets = re.findall(
            r"^diff --git a/(\S+) b/(\S+)$", self.patch, re.MULTILINE
        )
        self.assertCountEqual(
            targets,
            [
                ("dlls/winedmo/media_source.c", "dlls/winedmo/media_source.c"),
                ("dlls/mf/sar.c", "dlls/mf/sar.c"),
            ],
        )
        entries = [
            line
            for line in SERIES.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(entries), 19)
        self.assertEqual(
            entries[-1],
            "ge-proton11-6/0019-winedmo-trace-bounded-rtsp-drain-state.patch",
        )

    def test_rebased_patch_0004_is_sealed_independently_of_diagnostics(self) -> None:
        digest = hashlib.sha256(MONOLITH.read_bytes()).hexdigest()
        self.assertEqual(digest, MONOLITH_SHA256)

    def test_output_is_hard_bounded_and_route_opt_in(self) -> None:
        for needle in (
            "MEDIA_SOURCE_DRAIN_DIAG_SOURCE_LIMIT 64",
            "MEDIA_SOURCE_DRAIN_DIAG_LIVE_RECORDS 16",
            "MEDIA_SOURCE_DRAIN_DIAG_STREAM_RECORDS 8",
            "SAR_DRAIN_DIAG_SINK_LIMIT 64",
            "records = ++source->drain_diag_live_records",
            "object->drain_diag_id <= MEDIA_SOURCE_DRAIN_DIAG_SOURCE_LIMIT",
            "renderer->drain_diag_id <= SAR_DRAIN_DIAG_SINK_LIMIT",
            "TRACE_ON(rtspdrain)",
            'L"rtsp://"',
            'L"rtspt://"',
        ):
            self.assertIn(needle, self.added)
        for obsolete in (
            "rtspdrain_live_records",
            "rtspdrain_final_records",
            "media_source_drain_diag_final_record",
            "drain_diag_suppressed",
        ):
            self.assertNotIn(obsolete, self.added)

    def test_exhausted_source_has_no_hot_suppression_loop(self) -> None:
        self.assertIn(
            "source->drain_diag_live_exhausted = TRUE",
            self.added,
        )
        self.assertIn(
            "if (source->drain_diag_live_exhausted)",
            self.added,
        )
        for forbidden in ("InterlockedIncrement(&source->drain_diag_live",
                          "InterlockedDecrement(&source->drain_diag_live",
                          "InterlockedCompareExchange(&source->drain_diag_live"):
            self.assertNotIn(forbidden, self.added)

    def test_final_summaries_are_per_source_and_unconditional(self) -> None:
        summary_start = self.added.index(
            "static void media_source_drain_diag_summary("
        )
        summary_end = self.added.index(
            "    BOOL any_packet_full = FALSE;",
            summary_start,
        )
        summary = self.added[summary_start:summary_end]
        self.assertIn(
            "i < MEDIA_SOURCE_DRAIN_DIAG_STREAM_RECORDS",
            summary,
        )
        self.assertIn('TRACE_(rtspdrain)("event=stream_summary', summary)
        self.assertIn('TRACE_(rtspdrain)("event=source_summary', summary)
        self.assertNotIn("live_record(source)", summary)
        self.assertNotIn("final_record(source)", summary)

    def test_sar_summary_is_once_guarded_and_lifecycle_unconditional(self) -> None:
        summary_start = self.added.index(
            "static void audio_renderer_drain_diag_summary("
        )
        summary_end = self.added.index(
            "audio_renderer_drain_diag_summary(renderer);",
            summary_start,
        )
        summary = self.added[summary_start:summary_end]
        self.assertIn(
            "if (!renderer->drain_diag_enabled || renderer->drain_diag_summary_emitted)",
            summary,
        )
        self.assertIn("renderer->drain_diag_summary_emitted = TRUE;", summary)
        self.assertIn('TRACE_(rtspdrain)("event=sar_summary', summary)
        self.assertNotIn("live_record", summary)
        self.assertNotIn("final_record", summary)
        self.assertEqual(
            self.added.count("audio_renderer_drain_diag_summary(renderer);"),
            2,
        )

    def test_sar_counters_preserve_start_and_order_distinctions(self) -> None:
        for needle in (
            "drain_diag_clock_start_calls",
            "drain_diag_clock_start_successes",
            "drain_diag_audio_client_start_calls",
            "drain_diag_audio_client_start_successes",
            "drain_diag_process_samples",
            "drain_diag_preclock_samples",
            "drain_diag_render_callbacks",
            "drain_diag_peak_queued_frames",
            "sar_id=%lu lifetime_ms=%I64u",
            "clock_start_calls=%I64u clock_start_successes=%I64u",
            "audio_client_start_calls=%I64u audio_client_start_successes=%I64u",
            "process_samples=%I64u preclock_samples=%I64u render_callbacks=%I64u",
            "queued_frames=%u peak_queued_frames=%u max_frames=%u sample_rate=%lu",
            "frame_size=%u clock_state=%u start_pending=%u",
            "if (!renderer->drain_diag_clock_start_calls)",
        ):
            self.assertIn(needle, self.added)
        self.assertIn(
            "if (FAILED(hr = IAudioClient_Start(renderer->audio_client)))",
            self.patch,
        )
        self.assertNotIn(
            "renderer->clock_state != MFCLOCK_STATE_RUNNING)\n"
            "                ++renderer->drain_diag_preclock_samples",
            self.added,
        )
        for forbidden in (
            "InterlockedIncrement(&renderer->drain_diag_clock",
            "InterlockedIncrement(&renderer->drain_diag_audio_client",
            "InterlockedIncrement(&renderer->drain_diag_process",
            "InterlockedIncrement(&renderer->drain_diag_render",
        ):
            self.assertNotIn(forbidden, self.added)

    def test_combined_process_line_bound_is_1728(self) -> None:
        source_lines = 64 * (16 + 1 + 1 + 8)
        sar_lines = 64
        self.assertEqual(source_lines + sar_lines, 1728)

    def test_trace_surface_has_no_source_derived_private_values(self) -> None:
        trace_statements = re.findall(
            r"TRACE_\(rtspdrain\)\((.*?)\);",
            self.added,
            flags=re.DOTALL,
        )
        self.assertEqual(len(trace_statements), 6)
        surface = "\n".join(trace_statements).lower()
        for forbidden in (
            "%p",
            "debugstr_",
            "context->url",
            "mime",
            "header",
            "cookie",
            "credential",
            "token",
            "address",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, surface)
        self.assertNotIn("transport_bytes=", surface)
        self.assertIn("packet_bytes_completed=", surface)

    def test_patch_does_not_change_policy_or_add_recovery(self) -> None:
        self.assertNotIn("RTSP_IO_TIMEOUT_US", self.added)
        self.assertNotIn("RTSP_OPEN_TIMEOUT_US", self.added)
        for forbidden in ("reconnect", "backoff", "retry loop", "Sleep("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.added)


if __name__ == "__main__":
    unittest.main(verbosity=2)
