#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Structural audit for the quarantined source-load-generation candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


TARGETS = (
    "dlls/mfmediaengine/main.c",
    "dlls/winedmo/media_source.c",
)


class AuditFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditFailure(message)


def require_order(text: str, *needles: str) -> None:
    position = -1
    for needle in needles:
        next_position = text.find(needle, position + 1)
        require(next_position >= 0, f"missing ordered token: {needle}")
        require(next_position > position, f"token is out of order: {needle}")
        position = next_position


def region(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    require(begin >= 0, f"missing region start: {start}")
    finish = text.find(end, begin)
    require(finish >= 0, f"missing region end: {end}")
    return text[begin:finish]


def tail(text: str, start: str) -> str:
    begin = text.find(start)
    require(begin >= 0, f"missing region start: {start}")
    return text[begin:]


def load_patch(path: Path) -> dict[str, str]:
    sources: dict[str, list[str]] = {target: [] for target in TARGETS}
    current: str | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git a/"):
            candidate = line.split()[2][2:]
            current = candidate if candidate in sources else None
            continue
        if current is not None and line.startswith("@@ "):
            context = line.rsplit("@@", 1)[-1].strip()
            if context:
                sources[current].append(context)
            continue
        if current is None or line.startswith(("--- ", "+++ ")):
            continue
        if line.startswith("+") or line.startswith(" "):
            sources[current].append(line[1:])

    result = {name: "\n".join(lines) for name, lines in sources.items()}
    for target, text in result.items():
        require(text, f"patch has no reviewable fragment for {target}")
    return result


def load_tree(path: Path) -> dict[str, str]:
    result = {}
    for target in TARGETS:
        source = path / target
        require(source.is_file(), f"missing source file: {source}")
        result[target] = source.read_text(encoding="utf-8")
    return result


def audit(fixture: dict, sources: dict[str, str]) -> None:
    engine = sources["dlls/mfmediaengine/main.c"]
    winedmo = sources["dlls/winedmo/media_source.c"]

    require(tuple(fixture["target_files"]) == TARGETS,
            "fixture target list does not match the candidate scope")
    combined = "\n".join(sources.values())
    for token in fixture["excluded_tokens"]:
        require(token not in combined, f"candidate reintroduces deferred behavior: {token}")

    end_dispatch = region(
        engine,
        "static HRESULT media_engine_end_source_load",
        "static void media_engine_discard_source_object",
    )
    discard = region(
        engine,
        "static void media_engine_discard_source_object",
        "static HRESULT WINAPI media_engine_load_handler_Invoke",
    )
    current_helper = region(
        engine,
        "static BOOL media_engine_source_is_current",
        "/* must be called with engine->cs held */",
    )
    clear_presentation = region(
        engine,
        "static void media_engine_clear_presentation",
        "static void media_engine_clear_effects",
    )
    create_topology = region(
        engine,
        "static HRESULT media_engine_create_topology",
        "static void media_engine_start_playback",
    )
    callback = region(
        engine,
        "static HRESULT WINAPI media_engine_load_handler_Invoke",
        "static HRESULT media_engine_set_source",
    )
    set_source = region(
        engine,
        "static HRESULT media_engine_set_source",
        "static HRESULT WINAPI media_engine_Play",
    )
    play = region(
        engine,
        "static HRESULT WINAPI media_engine_Play",
        "static HRESULT WINAPI media_engine_Pause",
    )
    pause = tail(engine, "static HRESULT WINAPI media_engine_Pause")

    for pair in fixture["begin_end_pairs"]:
        origin = pair["origin"]
        begin = pair["begin"]
        end = pair["end"]
        require(origin in end_dispatch, f"{origin} is absent from End dispatch")
        require(end in end_dispatch, f"{end} is absent from End dispatch")
        require(begin in set_source, f"{begin} is absent from Begin dispatch")

        statement = set_source[set_source.index(begin):]
        statement = statement[: statement.index(");") + 2]
        require("&context->IUnknown_iface" in statement,
                f"{begin} does not retain the explicit load context")

    require_order(
        callback,
        "IMFAsyncResult_GetState(result, &state)",
        "media_engine_end_source_load(engine, context, result, &object)",
        "generation = context->generation",
        "IUnknown_Release(state)",
        "EnterCriticalSection(&engine->cs)",
        "if (!media_engine_source_is_current(engine, generation))",
        "MF_MEDIA_ENGINE_EVENT_LOADSTART",
        "if (!media_engine_source_is_current(engine, generation))",
        "media_engine_create_topology(engine, source, generation)",
        "if (!media_engine_source_is_current(engine, generation))",
        "IUnknown_Release(object)",
        "object = NULL",
        "start_playback = engine->playback_requested",
        "FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING",
    )
    require(callback.count("if (!media_engine_source_is_current(engine, generation))") >= 3,
            "callback does not revalidate the generation at every reentrant boundary")
    require(callback.count("IUnknown_Release(state)") == 1,
            "callback state is not released once after copying its generation")
    require("context->generation != engine->source_generation" not in callback,
            "callback dereferences its context after releasing callback state")
    require("(engine->flags & FLAGS_ENGINE_PLAY_PENDING) ||" not in callback,
            "callback snapshots stale flag-derived Play/Pause intent")
    require_order(
        callback,
        "stale:",
        "LeaveCriticalSection(&engine->cs)",
        "media_engine_discard_source_object(object)",
        "return S_OK",
    )

    require("engine->flags & FLAGS_ENGINE_SHUT_DOWN" in current_helper,
            "shutdown is not part of the shared current-generation gate")
    require("generation == engine->source_generation" in current_helper,
            "shared current-generation gate does not compare the captured generation")

    require_order(
        clear_presentation,
        "IMFPresentationDescriptor *pd = engine->presentation.pd",
        "struct video_frame_sink *frame_sink = engine->presentation.frame_sink",
        "IMFMediaSource *source = engine->presentation.source",
        "memset(&engine->presentation, 0, sizeof(engine->presentation))",
        "LeaveCriticalSection(&engine->cs)",
        "IMFMediaSource_Shutdown(source)",
        "EnterCriticalSection(&engine->cs)",
        "IMFMediaSource_Release(source)",
        "IMFPresentationDescriptor_Release(pd)",
        "video_frame_sink_release(frame_sink)",
    )
    require("IMFMediaSource_Shutdown(engine->presentation.source)" not in clear_presentation,
            "presentation cleanup uses mutable engine state after dropping the lock")

    require("struct media_engine *engine, IMFMediaSource *source, UINT64 generation" in create_topology,
            "topology creation does not receive the source generation")
    require(create_topology.count("if (!media_engine_source_is_current(engine, generation))") >= 3,
            "topology creation lacks generation checks around cleanup and installation")
    require_order(
        create_topology,
        "if (!media_engine_source_is_current(engine, generation))",
        "media_engine_release_video_frame_resources(engine)",
        "media_engine_clear_presentation(engine)",
        "if (!media_engine_source_is_current(engine, generation))",
        "IMFMediaSource_CreatePresentationDescriptor(source, &pd)",
        "if (!media_engine_source_is_current(engine, generation))",
        "IMFStreamDescriptor_Release(sd_video)",
        "IMFStreamDescriptor_Release(sd_audio)",
        "IMFPresentationDescriptor_Release(pd)",
        "return MF_E_OPERATION_CANCELLED",
        "engine->presentation.source = source",
    )

    require_order(
        discard,
        "IUnknown_QueryInterface",
        "IMFMediaSource_Shutdown(source)",
        "IMFMediaSource_Release(source)",
        "IUnknown_Release(object)",
    )
    require_order(
        set_source,
        "generation = ++engine->source_generation",
        "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS",
        "if (!media_engine_source_is_current(engine, generation))",
        "start_pending =",
        "engine->playback_requested = start_pending",
        "media_engine_load_context_create(origin, generation, bytestream, &context)",
        "IMFMediaEngineExtension_BeginCreateObject",
        "IUnknown_Release(&context->IUnknown_iface)",
        "if (!media_engine_source_is_current(engine, generation))",
        "if (FAILED(hr))",
        "FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING",
    )
    no_load = region(set_source, "if (!url && !bytestream)", "if (url || bytestream)")
    require_order(
        no_load,
        "engine->playback_requested = FALSE",
        "FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING",
        "return S_OK",
    )
    require("media_engine_load_context_create(origin, engine->source_generation" not in set_source,
            "Begin context does not preserve the generation captured before reentrant callbacks")
    require_order(play, "engine->playback_requested = TRUE", "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS")
    require("engine->playback_requested = FALSE" in pause,
            "Pause does not replace the intent consumed by a pending source load")
    if "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS" in pause:
        require_order(pause, "engine->playback_requested = FALSE", "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS")
    require("IMFByteStream_AddRef(context->bytestream)" in engine,
            "load context does not retain its byte stream")
    require("IMFByteStream_Release(context->bytestream)" in engine,
            "load context does not release its byte stream")

    for case in fixture["completion_cases"]:
        action = "shutdown_release" if case["shutdown"] or not case["generation_matches"] else "install"
        require(action == case["expected"], f"fixture expectation is inconsistent: {case['name']}")

    require_order(winedmo, "WCHAR *tmp_url = url ? wcsdup(url) : NULL",
                  "if (url && !tmp_url)", "return E_OUTOFMEMORY")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--patch", type=Path, help="candidate patch to audit")
    source.add_argument("--wine-tree", type=Path, help="already-patched Wine tree to audit")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).parent / "fixtures" / "completion-contract.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        sources = load_patch(args.patch) if args.patch else load_tree(args.wine_tree)
        audit(fixture, sources)
    except (AuditFailure, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("source-load generation candidate: structural audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
