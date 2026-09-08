#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Positive and mutation-negative tests for the source-generation audit."""

from __future__ import annotations

import json
from pathlib import Path

import audit as source_audit


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PATCH = ROOT / "patches/candidates/0002-mfmediaengine-track-source-load-generations.patch"
FIXTURE = HERE / "fixtures/completion-contract.json"
ENGINE = "dlls/mfmediaengine/main.c"


def mutate(sources: dict[str, str], old: str, new: str = "", *, occurrence: int = 1) -> dict[str, str]:
    changed = dict(sources)
    text = changed[ENGINE]
    start = -1
    for _ in range(occurrence):
        start = text.find(old, start + 1)
        if start < 0:
            raise AssertionError(f"self-test mutation token is absent: {old}")
    changed[ENGINE] = text[:start] + new + text[start + len(old):]
    return changed


def expect_failure(name: str, fixture: dict, sources: dict[str, str]) -> None:
    try:
        source_audit.audit(fixture, sources)
    except source_audit.AuditFailure:
        print(f"negative case passed: {name}")
        return
    raise AssertionError(f"audit accepted negative case: {name}")


def main() -> int:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sources = source_audit.load_patch(PATCH)

    source_audit.audit(fixture, sources)
    print("positive case passed: complete candidate")

    negative_cases = (
        (
            "presentation remains attached across lock drop",
            mutate(sources, "memset(&engine->presentation, 0, sizeof(engine->presentation))"),
        ),
        (
            "callback keeps context generation past state release",
            mutate(sources, "generation = context->generation"),
        ),
        (
            "LOADSTART reentrancy is not rechecked",
            mutate(
                sources,
                "MF_MEDIA_ENGINE_EVENT_LOADSTART, 0, 0);\n"
                "    if (!media_engine_source_is_current(engine, generation))",
                "MF_MEDIA_ENGINE_EVENT_LOADSTART, 0, 0);",
            ),
        ),
        (
            "topology does not receive the captured generation",
            mutate(
                sources,
                "media_engine_create_topology(engine, source, generation)",
                "media_engine_create_topology(engine, source)",
            ),
        ),
        (
            "old-source shutdown reentrancy is not rechecked",
            mutate(
                sources,
                "media_engine_clear_presentation(engine);\n\n"
                "    /* media_engine_clear_presentation() drops engine->cs while the previous\n"
                "     * source shuts down.  Do not let an older callback replace a source that\n"
                "     * became current during that window. */\n"
                "    if (!media_engine_source_is_current(engine, generation))\n"
                "        return MF_E_OPERATION_CANCELLED;",
                "media_engine_clear_presentation(engine);",
            ),
        ),
        (
            "PURGE reentrancy loses the local generation",
            mutate(
                sources,
                "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS, 0, 0);\n"
                "    if (!media_engine_source_is_current(engine, generation))\n"
                "        return S_OK;",
                "MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS, 0, 0);",
                occurrence=1,
            ),
        ),
        (
            "Begin context reads a reentrant shared generation",
            mutate(
                sources,
                "media_engine_load_context_create(origin, generation, bytestream, &context)",
                "media_engine_load_context_create(origin, engine->source_generation, bytestream, &context)",
            ),
        ),
        (
            "Begin reentrancy is not rechecked",
            mutate(
                sources,
                "IUnknown_Release(&context->IUnknown_iface);\n\n"
                "        if (!media_engine_source_is_current(engine, generation))\n"
                "            return S_OK;",
                "IUnknown_Release(&context->IUnknown_iface);",
            ),
        ),
        (
            "null source leaves pending flags set",
            mutate(
                sources,
                "engine->playback_requested = FALSE;\n"
                "        media_engine_set_flag(engine, FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING, FALSE);\n"
                "        return S_OK;",
                "engine->playback_requested = FALSE;\n        return S_OK;",
                occurrence=1,
            ),
        ),
        (
            "Play does not replace pending-load intent",
            mutate(sources, "engine->playback_requested = TRUE;"),
        ),
        (
            "Pause does not replace pending-load intent",
            mutate(sources, "engine->playback_requested = FALSE;", occurrence=2),
        ),
        (
            "broad non-shutdown MEError forwarding returns",
            {**sources, ENGINE: sources[ENGINE] + "\ncase MEError:\n"},
        ),
    )

    for name, changed in negative_cases:
        expect_failure(name, fixture, changed)

    print(f"source-load generation audit self-test passed ({len(negative_cases)} negative cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
