#!/usr/bin/env python3
"""Portable contract checks for the project-authored A3.12 patch."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/ge-proton11-6-a320/0017-winedmo-propagate-terminal-network-read-errors.patch"
SERIES = ROOT / "patches/series"


def added_lines(text: str) -> str:
    return "\n".join(
        line[1:]
        for line in text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def added_lines_by_file(text: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^diff --git a/(\S+) b/\S+$", line)
        if match:
            current = match.group(1)
            files[current] = []
        elif (
            current is not None
            and line.startswith("+")
            and not line.startswith("+++")
        ):
            files[current].append(line[1:])
    return {name: "\n".join(lines) for name, lines in files.items()}


def validate_added_contract(text: str) -> list[str]:
    files = added_lines_by_file(text)
    required = {
        "dlls/winedmo/unix_demuxer.c": (
            "input_error = ret < 0 && ret != AVERROR_EOF",
            "if (!demuxer->direct_url || !input_error) return STATUS_END_OF_FILE;",
            "if (demuxer->direct_url && demuxer->require_avio_seek)",
            "if (ret == AVERROR_EOF && demuxer->direct_url "
            "&& demuxer->require_avio_seek)",
            "return STATUS_CONNECTION_DISCONNECTED;",
            "av_packet_free( &demuxer->last_packet );",
            "if ((status = demuxer_filter_packet( demuxer, &packet )))",
            "return demuxer->duration <= 0 ? STATUS_SUCCESS",
            "demuxer->last_packet = packet;",
        ),
        "dlls/winedmo/media_source.c": (
            "source->demux_seek_pending = true;",
            "LeaveCriticalSection(&source->cs);",
            "source->demux_seek_pending = false;",
            "media_source_queue_demux_error(source, error);",
            "CRITICAL_SECTION demux_error_cs;",
            "EnterCriticalSection(&source->demux_error_cs);",
            "InterlockedIncrement(&source->demux_error_event_generation);",
            "EnterCriticalSection(&stream->queue_cs);\n"
            "        WakeAllConditionVariable(&stream->queue_cv);\n"
            "        LeaveCriticalSection(&stream->queue_cs);",
            "if (!error_queued)",
        ),
        "dlls/mf/session.c": (
            "SOURCE_FLAG_TERMINAL_ERROR_FORWARDED",
            "IMFMediaEventQueue_QueueEventParamUnk",
            "session_command_complete(session);",
        ),
        "dlls/mfmediaengine/main.c": (
            "engine->presentation.generation != engine->source_generation",
            "source.punkVal != (IUnknown *)engine->presentation.source",
            "MF_MEDIA_ENGINE_EVENT_ERROR",
            "engine->presentation.generation = 0;",
            "engine->presentation.generation = generation;",
            "engine->error_code = MF_MEDIA_ENGINE_ERR_NOERROR;",
        ),
    }
    missing: list[str] = []
    for filename, needles in required.items():
        body = files.get(filename, "")
        missing.extend(f"{filename}: {needle}" for needle in needles if needle not in body)
    return missing


class PatchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patch = PATCH.read_text(encoding="utf-8")
        cls.added = added_lines(cls.patch)

    def test_terminal_patch_order_and_exact_four_file_surface(self) -> None:
        entries = [
            line
            for line in SERIES.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(entries), 19)
        self.assertEqual(
            entries[-6:],
            [
                "ge-proton11-6-a320/0017-winedmo-propagate-terminal-network-read-errors.patch",
                "ge-proton11-6/0018-winewayland-advertise-wine-vr-device-extensions.patch",
                "ge-proton11-6/0020-quiet-ge-media-diagnostics.patch",
                "ge-proton11-6-a320/0022-mfmediaengine-preserve-selected-frame-tick-result.patch",
                "ge-proton11-6-a320/0023-mfmediaengine-check-seekability-before-recovery-reset.patch",
                "ge-proton11-6/0019-winedmo-trace-bounded-rtsp-drain-state.patch",
            ],
        )
        targets = re.findall(
            r"^diff --git a/(\S+) b/(\S+)$", self.patch, re.MULTILINE
        )
        self.assertEqual(
            targets,
            [
                ("dlls/mf/session.c", "dlls/mf/session.c"),
                ("dlls/mfmediaengine/main.c", "dlls/mfmediaengine/main.c"),
                ("dlls/winedmo/media_source.c", "dlls/winedmo/media_source.c"),
                ("dlls/winedmo/unix_demuxer.c", "dlls/winedmo/unix_demuxer.c"),
            ],
        )

    def test_required_contract_is_present(self) -> None:
        self.assertEqual(validate_added_contract(self.patch), [])

    def test_network_error_path_does_not_add_ended_or_stream_eos(self) -> None:
        self.assertNotIn("MF_MEDIA_ENGINE_EVENT_ENDED", self.added)
        self.assertNotIn("stream->eos = TRUE", self.added)
        self.assertNotIn("s->eos = TRUE", self.added)

    def test_prefetch_is_bounded_to_progressive_before_end(self) -> None:
        self.assertIn("demuxer->require_avio_seek", self.added)
        self.assertIn(
            "demuxer->duration <= 0 || params->timestamp < demuxer->duration",
            self.added,
        )

    def test_mutation_removing_current_source_guard_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "source.punkVal != (IUnknown *)engine->presentation.source",
            "source.punkVal == NULL",
            1,
        )
        self.assertIn(
            "dlls/mfmediaengine/main.c: "
            "source.punkVal != (IUnknown *)engine->presentation.source",
            validate_added_contract(mutated),
        )

    def test_mutation_removing_deferred_read_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "if ((status = demuxer_filter_packet( demuxer, &packet )))",
            "if ((status = STATUS_SUCCESS))",
            1,
        )
        self.assertIn(
            "dlls/winedmo/unix_demuxer.c: "
            "if ((status = demuxer_filter_packet( demuxer, &packet )))",
            validate_added_contract(mutated),
        )

    def test_mutation_removing_generation_guard_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "engine->presentation.generation != engine->source_generation",
            "engine->presentation.generation == 0",
            1,
        )
        self.assertIn(
            "dlls/mfmediaengine/main.c: "
            "engine->presentation.generation != engine->source_generation",
            validate_added_contract(mutated),
        )

    def test_mutation_removing_queue_lock_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "EnterCriticalSection(&stream->queue_cs);\n"
            "+        WakeAllConditionVariable(&stream->queue_cv);\n"
            "+        LeaveCriticalSection(&stream->queue_cs);",
            "/* queue lock removed */\n"
            "+        WakeAllConditionVariable(&stream->queue_cv);",
            1,
        )
        self.assertIn(
            "dlls/winedmo/media_source.c: "
            "EnterCriticalSection(&stream->queue_cs);\n"
            "        WakeAllConditionVariable(&stream->queue_cv);\n"
            "        LeaveCriticalSection(&stream->queue_cs);",
            validate_added_contract(mutated),
        )

    def test_mutation_mislabeling_filter_failure_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "if (!demuxer->direct_url || !input_error) return STATUS_END_OF_FILE;",
            "if (!demuxer->direct_url) return STATUS_END_OF_FILE;",
            1,
        )
        self.assertIn(
            "dlls/winedmo/unix_demuxer.c: "
            "if (!demuxer->direct_url || !input_error) return STATUS_END_OF_FILE;",
            validate_added_contract(mutated),
        )

    def test_mutation_removing_internal_error_dedupe_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "SOURCE_FLAG_TERMINAL_ERROR_FORWARDED",
            "SOURCE_FLAG_END_OF_PRESENTATION",
        )
        self.assertIn(
            "dlls/mf/session.c: SOURCE_FLAG_TERMINAL_ERROR_FORWARDED",
            validate_added_contract(mutated),
        )

    def test_mutation_removing_retained_packet_cleanup_is_rejected(self) -> None:
        mutated = self.patch.replace(
            "av_packet_free( &demuxer->last_packet );",
            "demuxer->last_packet = NULL;",
            1,
        )
        self.assertIn(
            "dlls/winedmo/unix_demuxer.c: "
            "av_packet_free( &demuxer->last_packet );",
            validate_added_contract(mutated),
        )
        # A second occurrence exists in seek cleanup, so require destruction
        # ordering separately rather than accepting a global added-line match.
        demuxer_patch = added_lines_by_file(mutated)[
            "dlls/winedmo/unix_demuxer.c"
        ]
        cleanup = demuxer_patch.split(
            "static void demuxer_free_context", 1
        )[-1].split("NTSTATUS demuxer_create", 1)[0]
        self.assertNotIn(
            "av_packet_free( &demuxer->last_packet );", cleanup
        )

    def test_known_duration_premature_eof_is_not_success(self) -> None:
        self.assertIn(
            "return demuxer->duration <= 0 ? STATUS_SUCCESS\n"
            "                        : STATUS_CONNECTION_DISCONNECTED;",
            self.added,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
