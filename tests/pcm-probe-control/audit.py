#!/usr/bin/env python3
"""Audit prepared MF source for the bounded A3.7 PCM boundary probe."""

from __future__ import annotations

from pathlib import Path
import re
import sys


def fail(message: str) -> None:
    raise SystemExit(f"pcm-probe audit failed: {message}")


def require(text: str, needle: str, message: str) -> None:
    if needle not in text:
        fail(message)


def region(text: str, start: str, end: str) -> str:
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError as exc:
        fail(f"could not isolate source region: {exc}")
    return text[begin:finish]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} /path/to/src-wine")

    source_root = Path(sys.argv[1]).resolve()
    source_path = source_root / "dlls" / "mf" / "session.c"
    if not source_path.is_file():
        fail(f"missing source file: {source_path}")

    text = source_path.read_text(encoding="utf-8")
    require(text, "WINE_DECLARE_DEBUG_CHANNEL(pcmprobe);",
            "probe does not use a dedicated opt-in debug channel")
    require(text, "#define PCM_PROBE_SAMPLE_LIMIT 16",
            "probe is not bounded to sixteen attempts per selected node")
    require(text, "#define PCM_PROBE_VALUE_LIMIT 16384",
            "probe does not hard-cap scalar inspection at 16,384 values")
    require(text, "struct pcm_probe_summary",
            "pre-ProcessInput aggregates are not retained in a stack summary")
    summary_type = region(text, "struct pcm_probe_summary\n{", "\n};")
    require(summary_type, "IMFMediaSession *session;",
            "PCM summary lacks an opaque Media Session correlation identity")
    topo_node = region(text, "struct topo_node\n{", "\n};")
    if "pcm_probe_summary" in topo_node:
        fail("content-derived aggregate is retained persistently on a topology node")

    append = region(text, "static HRESULT session_append_node(",
                    "static HRESULT session_collect_nodes(")
    require(append, "CLSID_MSAACDecMFT", "AAC decoder classification is absent")
    require(append, "MF_TOPONODE_NOSHUTDOWN_ON_REMOVE",
            "MediaEngine effect classification is absent")
    require(append, "session_set_transform_stream_info(topo_node)",
            "effect classification is not based on configured transform streams")
    require(append, "raw_audio_effect",
            "MediaEngine video effects are not excluded from the input probe")
    if append.index("raw_audio_effect = FALSE") < append.index("session_set_transform_stream_info(topo_node)"):
        fail("raw-audio effect selection happens before stream information is available")
    require(append, "topo_node->pcm_probe_kind = PCM_PROBE_NONE",
            "non-audio MediaEngine effects are not deselected")

    inspect = region(text, "static BOOL inspect_pcm_probe_sample(",
                     "static void trace_pcm_probe_summary(")
    require(inspect, "!TRACE_ON(pcmprobe)", "disabled probe does not return immediately")
    require(inspect, "node->pcm_probe_count >= PCM_PROBE_SAMPLE_LIMIT",
            "per-node attempt bound is not enforced")
    increment = re.search(r"(?:\+\+\s*node->pcm_probe_count|node->pcm_probe_count\s*\+\+)", inspect)
    if not increment:
        fail("accepted inspection attempts do not increment the per-node counter")
    if increment.start() < inspect.index("node->pcm_probe_count >= PCM_PROBE_SAMPLE_LIMIT"):
        fail("sample counter increments before its hard limit is checked")

    require(inspect, "PCM_PROBE_AAC_DECODER && !output",
            "AAC decoder inputs are not excluded")
    require(inspect, "PCM_PROBE_MEDIA_ENGINE_EFFECT && output",
            "intentional MediaEngine effect outputs are not excluded")
    require(inspect, '"aac_decoder_output"', "AAC output boundary label is absent")
    require(inspect, '"media_engine_audio_effect_input"',
            "MediaEngine effect input boundary label is absent")
    require(inspect, "IMFTransform_GetOutputCurrentType",
            "AAC output inspection does not use the output media type")
    require(inspect, "IMFTransform_GetInputCurrentType",
            "effect input inspection does not use the input media type")

    require(inspect, "IMFSample_GetBufferCount(sample, &buffer_count)",
            "sample inspection does not query buffer geometry")
    require(inspect, "buffer_count != 1",
            "multi-buffer samples are not rejected without mutation")
    require(inspect, '"unsupported_buffers"',
            "multi-buffer samples lack an explicit unsupported status")
    buffer_check = inspect.index("buffer_count != 1")
    buffer_status = inspect.index('"unsupported_buffers"', buffer_check)
    buffer_access = inspect.index("IMFSample_GetBufferByIndex", buffer_check)
    if not buffer_check < buffer_status < buffer_access:
        fail("multi-buffer rejection does not set status before buffer access")
    require(inspect[buffer_check:buffer_access], "inspection_hr = E_NOTIMPL;",
            "unsupported buffer layout lacks a failing inspection HRESULT")
    if "IMFSample_ConvertToContiguousBuffer" in inspect:
        fail("probe converts or mutates a multi-buffer playback sample")

    require(inspect, "IMFMediaBuffer_Lock", "probe never locks the selected buffer for reading")
    require(inspect, "IMFMediaBuffer_Unlock", "probe does not unlock the selected buffer")
    if inspect.index("IMFMediaBuffer_Unlock") < inspect.index("IMFMediaBuffer_Lock"):
        fail("buffer unlock precedes lock")
    for mutation in ("memmove(", "memset(data", "memcpy(data", "RtlCopyMemory(data",
                     "IMFMediaBuffer_SetCurrentLength",
                     "IMFSample_SetSampleTime", "IMFSample_SetSampleDuration"):
        if mutation in inspect:
            fail(f"probe mutates the playback sample: {mutation}")

    require(inspect, "PCM_PROBE_VALUE_LIMIT",
            "scalar loop is not constrained by the hard value limit")
    cap_patterns = (
        r"inspected_values\s*=\s*min\s*\(\s*total_values\s*,\s*PCM_PROBE_VALUE_LIMIT\s*\)\s*;",
        r"inspected_values\s*=\s*total_values\s*<\s*PCM_PROBE_VALUE_LIMIT\s*"
        r"\?\s*total_values\s*:\s*PCM_PROBE_VALUE_LIMIT\s*;",
    )
    if not any(re.search(pattern, inspect) for pattern in cap_patterns):
        fail("inspected scalar count is not hard-capped from the total count")
    scalar_loops = re.findall(
        r"for\s*\(\s*i\s*=\s*0\s*;\s*i\s*<\s*inspected_values\s*;\s*\+\+i\s*\)",
        inspect,
    )
    if len(scalar_loops) < 2:
        fail("all supported PCM loops must stop at inspected_values")
    require(inspect, '*inspection_status = "failed"',
            "inspection does not default explicitly to failed")
    require(inspect, "HRESULT inspection_hr = E_FAIL;",
            "inspection HRESULT does not default to a failure")
    for status in ("complete", "truncated", "invalid", "failed",
                   "unsupported", "unsupported_buffers"):
        require(inspect, f'"{status}"', f"inspection cannot report {status} status")
    require(inspect, "memcpy(&value", "numeric reads are not alignment-safe")
    for field in ("total_values", "inspected_values", "nonzero", "peak",
                  "mean_square", "nonfinite"):
        require(inspect, f"summary->{field}", f"summary does not retain {field}")
    require(inspect, "summary->session = &node->session->IMFMediaSession_iface;",
            "inspection does not snapshot its owning Media Session identity")
    if "TRACE_(pcmprobe)" in inspect or re.search(r"\bTRACE\s*\(", inspect):
        fail("inspector emits a trace before ProcessInput instead of returning a summary")

    trace = region(text, "static void trace_pcm_probe_summary(",
                   "static HRESULT transform_node_pull_samples(")
    require(trace, "TRACE_(pcmprobe)", "combined summary is not emitted on pcmprobe")
    require(trace, "session %p", "PCM trace omits Media Session identity")
    require(trace, "summary->session",
            "PCM trace does not use the IMFMediaSession identity shared with MediaEngine")
    for field in ("process_valid", "process_hr", "queue_attempted", "queue_hr",
                  "delivery_valid", "delivery_hr", "accepted", "queued"):
        require(trace, field, f"combined trace omits {field}")
    for forbidden in ("hash", "hexdump", "%02x", "writefile", "fwrite", "url", "endpoint"):
        if forbidden in (inspect + trace).lower():
            fail(f"probe contains content-revealing operation: {forbidden}")

    pull = region(text, "static HRESULT transform_node_pull_samples(",
                  "static BOOL transform_node_is_drained(")
    require(pull, "PCM_PROBE_AAC_DECODER", "AAC output path is not selected")
    require(pull, "struct pcm_probe_summary summary;",
            "AAC aggregate is not retained in a local stack summary")
    require(pull, "inspect_pcm_probe_sample(node, TRUE",
            "AAC output is not inspected at the output boundary")
    aac_trace = re.search(
        r"trace_pcm_probe_summary\s*\(\s*&summary\s*,\s*FALSE\s*,\s*E_NOTIMPL\s*,"
        r"\s*FALSE\s*,\s*E_NOTIMPL\s*,\s*FALSE\s*,\s*E_NOTIMPL\s*\)",
        pull,
    )
    if not aac_trace:
        fail("AAC summary does not mark ProcessInput and delivery fields invalid")
    if pull.index("inspect_pcm_probe_sample(node, TRUE") > pull.index("trace_pcm_probe_summary"):
        fail("AAC summary is emitted before inspection")

    push = region(text, "static HRESULT transform_node_push_sample(",
                  "static void session_deliver_sample_to_node(")
    require(push, "PCM_PROBE_MEDIA_ENGINE_EFFECT",
            "effect input path is not restricted to the selected MediaEngine effect")
    require(push, "struct pcm_probe_summary probe_summary;",
            "effect aggregate is not retained in a local stack summary")
    require(push, "inspect_pcm_probe_sample(topo_node, FALSE",
            "effect sample is not inspected at its input boundary")
    require(push, "process_hr = IMFTransform_ProcessInput",
            "raw ProcessInput result is not preserved")
    require(push, "process_hr == MF_E_NOTACCEPTING",
            "MF_E_NOTACCEPTING is not distinguished from accepted input")
    require(push, "queue_attempted = TRUE", "queue attempt is not recorded explicitly")
    require(push, "queue_hr = transform_stream_push_sample",
            "queue result is not preserved separately")
    require(push, "trace_pcm_probe_summary", "effect input lacks one combined result trace")
    effect_trace = re.search(
        r"trace_pcm_probe_summary\s*\(\s*&probe_summary\s*,\s*TRUE\s*,\s*process_hr\s*,"
        r"\s*queue_attempted\s*,\s*queue_hr\s*,\s*TRUE\s*,\s*hr\s*\)",
        push,
    )
    if not effect_trace:
        fail("effect input does not mark ProcessInput and final delivery fields valid")
    if push.count("trace_pcm_probe_summary") != 1:
        fail("effect input must emit exactly one combined trace per inspected attempt")
    if len(re.findall(r"IMFTransform_ProcessInput\s*\(", push)) != 1:
        fail("instrumented path must retain exactly one ProcessInput call")

    inspect_call = push.index("inspect_pcm_probe_sample(topo_node, FALSE")
    process_call = push.index("process_hr = IMFTransform_ProcessInput")
    trace_call = push.index("trace_pcm_probe_summary")
    if not inspect_call < process_call < trace_call:
        fail("required order is inspect/unlock, ProcessInput, then combined trace")
    if "TRACE_(pcmprobe)" in push[:process_call]:
        fail("push path performs diagnostic log I/O before ProcessInput")

    print("pcm-probe source audit passed")


if __name__ == "__main__":
    main()
