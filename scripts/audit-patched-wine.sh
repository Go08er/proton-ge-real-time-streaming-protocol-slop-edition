#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WINE_TREE=""
PATCH_SERIES=""
REQUIRE_ALPHA_SERIES=0
REQUIRE_A36_SERIES=0
REQUIRE_A37_SERIES=0
REQUIRE_A38_SERIES=0
REQUIRE_A39_SERIES=0
REQUIRE_A310_SERIES=0
REQUIRE_A311_SERIES=0
REQUIRE_A312_SERIES=0

usage() {
  cat <<'EOF'
Usage: audit-patched-wine.sh --wine-tree DIR [--series FILE] [--require-alpha-series] [--require-a36-series] [--require-a37-series] [--require-a38-series] [--require-a39-series] [--require-a310-series] [--require-a311-series] [--require-a312-series]

Audit a source-only GE Wine patch result. This does not compile or run Wine.
The command fails on patch rejects, a surviving WineGStreamer backend, or a
missing WineDMO media implementation. With --require-alpha-series, it also
requires the pinned transport, cancellation/deadline ordering, live
seekability, audio seek-timeline continuity, and WOW64 teardown corrections.
With --require-a36-series, it additionally requires the state-preserving SAR
flush, first-start presentation-clock correction, and bounded opt-in PCM probe.
With --require-a37-series, it requires the A3.6 foundation plus the effect-input
and AVPro-visible audio-state diagnostic contracts.
With --require-a38-series, it additionally requires latest-intent pause/scrub
reconciliation and narrow MediaEngine control-state tracing.
With --require-a39-series, it additionally requires current-generation pending
start-position ownership across running source replacement.
With --require-a310-series, it also requires an already-stopped immediate
replacement presentation to use MediaSession's subscribed fresh-start path.
With --require-a311-series, it additionally requires outgoing sinks to detach
and the physical presentation clock to stop before replacement sinks attach.
With --require-a312-series, it additionally requires deferred progressive-HTTP
Range failures to reach the current MediaEngine as errors rather than EOF.
Series-specific audits require --series. If that series declares the bounded
PCM/AVPro diagnostic patch, its complete source contracts remain mandatory. If
it does not, every diagnostic symbol must be absent from the Wine source.
The independent bounded `rtspdrain` patch is audited when declared and must be
absent when omitted.
Manual control-flow review and runtime tests are still required.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wine-tree)
      [[ $# -ge 2 && -n "$2" ]] || fail "--wine-tree requires a directory"
      WINE_TREE="$2"
      shift 2
      ;;
    --series)
      [[ $# -ge 2 && -n "$2" ]] || fail "--series requires a file"
      PATCH_SERIES="$2"
      shift 2
      ;;
    --require-alpha-series)
      REQUIRE_ALPHA_SERIES=1
      shift
      ;;
    --require-a36-series)
      REQUIRE_A36_SERIES=1
      shift
      ;;
    --require-a37-series)
      REQUIRE_A37_SERIES=1
      shift
      ;;
    --require-a38-series)
      REQUIRE_A38_SERIES=1
      shift
      ;;
    --require-a39-series)
      REQUIRE_A39_SERIES=1
      shift
      ;;
    --require-a310-series)
      REQUIRE_A310_SERIES=1
      shift
      ;;
    --require-a311-series)
      REQUIRE_A311_SERIES=1
      shift
      ;;
    --require-a312-series)
      REQUIRE_A312_SERIES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ -n "$WINE_TREE" ]] || fail "--wine-tree is required"
[[ -d "$WINE_TREE" ]] || fail "missing Wine tree: $WINE_TREE"
[[ -f "$WINE_TREE/dlls/winedmo/media_source.c" ]] \
  || fail "missing WineDMO media source"
[[ -f "$WINE_TREE/dlls/winedmo/unix_demuxer.c" ]] \
  || fail "missing WineDMO FFmpeg demuxer"

SERIES_SPECIFIC=0
if [[ -n "$PATCH_SERIES" \
    || "$REQUIRE_A36_SERIES" == 1 || "$REQUIRE_A37_SERIES" == 1 \
    || "$REQUIRE_A38_SERIES" == 1 || "$REQUIRE_A39_SERIES" == 1 \
    || "$REQUIRE_A310_SERIES" == 1 || "$REQUIRE_A311_SERIES" == 1 \
    || "$REQUIRE_A312_SERIES" == 1 ]]; then
  SERIES_SPECIFIC=1
fi

DIAGNOSTIC_SERIES=0
RTSP_DRAIN_SERIES=0
A320_SERIES=0
if [[ "$SERIES_SPECIFIC" == 1 ]]; then
  [[ -n "$PATCH_SERIES" ]] \
    || fail "--series is required for a series-specific audit"
  PATCH_SERIES="$(realpath -e -- "$PATCH_SERIES")" \
    || fail "could not resolve patch series: $PATCH_SERIES"
  [[ -f "$PATCH_SERIES" && ! -L "$PATCH_SERIES" ]] \
    || fail "patch series is not a regular file: $PATCH_SERIES"
  DIAGNOSTIC_SERIES="$(
    awk '
      /^[[:space:]]*($|#)/ { next }
      NF != 1 { exit 2 }
      {
        count = split($1, component, "/")
        if (component[count] == "0012-mf-session-trace-selected-audio-payloads.patch")
          diagnostic++
      }
      END {
        if (diagnostic > 1) exit 3
        print diagnostic + 0
      }
    ' "$PATCH_SERIES"
  )" || fail "patch series is malformed or declares the diagnostic more than once"
  [[ "$DIAGNOSTIC_SERIES" == 0 || "$DIAGNOSTIC_SERIES" == 1 ]] \
    || fail "could not classify the diagnostic patch declaration"
  RTSP_DRAIN_SERIES="$(
    awk '
      /^[[:space:]]*($|#)/ { next }
      NF != 1 { exit 2 }
      {
        count = split($1, component, "/")
        if (component[count] == "0019-winedmo-trace-bounded-rtsp-drain-state.patch")
          diagnostic++
      }
      END {
        if (diagnostic > 1) exit 3
        print diagnostic + 0
      }
    ' "$PATCH_SERIES"
  )" || fail "patch series is malformed or declares rtspdrain more than once"
  [[ "$RTSP_DRAIN_SERIES" == 0 || "$RTSP_DRAIN_SERIES" == 1 ]] \
    || fail "could not classify the rtspdrain patch declaration"
fi

mapfile -t REJECTS < <(
  find "$WINE_TREE" -path '*/.git' -prune -o -type f -name '*.rej' -print \
    | LC_ALL=C sort
)
if [[ ${#REJECTS[@]} -gt 0 ]]; then
  printf 'Patch rejects remain:\n' >&2
  printf '  %s\n' "${REJECTS[@]}" >&2
  fail "patched Wine tree is not reviewable"
fi

if [[ -d "$WINE_TREE/dlls/winegstreamer" ]] \
    && find "$WINE_TREE/dlls/winegstreamer" -type f \
      ! -name '*.orig' ! -name '*.rej' -print -quit | grep -q .; then
  fail "WineGStreamer source files survived the no-GStreamer media transition"
fi

has_pattern() {
  local pattern="$1"
  shift
  rg -q -- "$pattern" "$@" 2>/dev/null
}

fact() {
  local name="$1"
  local value="$2"
  printf '%s\t%s\n' "$name" "$value"
}

if [[ "$SERIES_SPECIFIC" == 1 ]]; then
  if [[ "$DIAGNOSTIC_SERIES" == 1 ]]; then
    fact diagnostic_patch_declared yes
  else
    diagnostic_files=(
      "$WINE_TREE/dlls/mf/session.c"
      "$WINE_TREE/dlls/mfmediaengine/main.c"
    )
    for diagnostic_file in "${diagnostic_files[@]}"; do
      [[ -f "$diagnostic_file" ]] \
        || fail "missing diagnostic-audit source file: $diagnostic_file"
    done
    set +e
    diagnostic_hits="$(
      rg -n -e 'avprostate|pcmprobe|pcm_probe_|PCM_PROBE_' \
        "${diagnostic_files[@]}" 2>&1
    )"
    diagnostic_status=$?
    set -e
    case "$diagnostic_status" in
      1) ;;
      0)
        printf '%s\n' "$diagnostic_hits" >&2
        fail "diagnostic-free series left PCM/AVPro diagnostic symbols in Wine"
        ;;
      *)
        printf '%s\n' "$diagnostic_hits" >&2
        fail "could not audit diagnostic-symbol absence"
        ;;
    esac
    fact diagnostic_patch_declared no
    fact diagnostic_source_absence passed
  fi
  if [[ "$RTSP_DRAIN_SERIES" == 1 ]]; then
    python3 "$ROOT_DIR/tests/rtsp-drain-diagnostics/audit.py" \
      --wine-tree "$WINE_TREE" >/dev/null
    fact rtsp_drain_diagnostic_declared yes
    fact rtsp_drain_diagnostic_contract passed
  else
    if rg -n -e 'rtspdrain|drain_diag_' \
        "$WINE_TREE/dlls/winedmo/media_source.c" >/dev/null; then
      fail "series without Patch 0019 left rtspdrain symbols in Wine"
    fi
    fact rtsp_drain_diagnostic_declared no
    fact rtsp_drain_diagnostic_source_absence passed
  fi
  if rg -q '^ge-proton11-6/0020-quiet-ge-media-diagnostics\.patch$' "$PATCH_SERIES"; then
    python3 "$ROOT_DIR/tests/ge-proton11-6/audit.py" --wine-tree "$WINE_TREE" >/dev/null
    fact ge11_6_integration_contract passed
  fi
  if rg -q '^ge-proton11-6-a320/0022-mfmediaengine-preserve-selected-frame-tick-result\.patch$' "$PATCH_SERIES"; then
    A320_SERIES=1
    for test in seek_restart frame_tick recovery_seek; do
      python3 "$ROOT_DIR/tests/vod-control/test_${test}.py" --wine-tree "$WINE_TREE" >/dev/null
    done
    python3 "$ROOT_DIR/tests/network-terminal-error/test_seek_failure.py" --wine-tree "$WINE_TREE" >/dev/null
    fact a320_seek_frame_source_contracts passed
  fi
  if rg -q '^ge-proton11-6-a32[12]/0005-winedmo-stream-progressive-http-vod\.patch$' "$PATCH_SERIES"; then
    python3 "$ROOT_DIR/tests/progressive-http-vod/test_http_user_agent.py" --wine-tree "$WINE_TREE" >/dev/null
    fact a321_http_user_agent_source_contract passed
  fi
  if rg -q '^ge-proton11-6-a322/0005-winedmo-stream-progressive-http-vod\.patch$' "$PATCH_SERIES"; then
    python3 "$ROOT_DIR/tests/progressive-http-vod/test_hls_segment_policy.py" --wine-tree "$WINE_TREE" >/dev/null
    fact a322_hls_segment_policy_source_contract passed
  fi
  if rg -q '^ge-proton11-6-a322-r2/0025-winedmo-normalize-hls-vod-timeline\.patch$' "$PATCH_SERIES"; then
    python3 "$ROOT_DIR/tests/progressive-http-vod/test_hls_timeline.py" --wine-tree "$WINE_TREE" >/dev/null
    fact a322_hls_timeline_source_contract passed
  fi
  if rg -q '^ge-proton11-6-a323/0026-mf-replenish-output-buffers-after-preroll\.patch$' "$PATCH_SERIES"; then
    python3 "$ROOT_DIR/tests/vod-control/test_preroll_buffers.py" --wine-tree "$WINE_TREE" >/dev/null
    fact a323_preroll_output_buffer_source_contract passed
  fi
fi

bool_fact() {
  local name="$1"
  local pattern="$2"
  shift 2
  if has_pattern "$pattern" "$@"; then
    fact "$name" yes
  else
    fact "$name" no
  fi
}

bool_fact source_demux_thread 'source_demux_thread' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact directshow_demux_thread 'dmo_demux_thread' \
  "$WINE_TREE/dlls/winedmo/winedmo_quartz_parser.c"
bool_fact source_reader_io_queue 'MFASYNC_CALLBACK_QUEUE_IO' \
  "$WINE_TREE/dlls/mfreadwrite/reader.c"
bool_fact source_reader_serial_queue 'MFAllocateSerialWorkQueue' \
  "$WINE_TREE/dlls/mfreadwrite/reader.c"
bool_fact direct_hls_url 'is_http_hls_url' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact rtsp_registry_scheme 'rtsp:' \
  "$WINE_TREE/dlls/winedmo/winedmo.rgs"
if has_pattern 'rtspt:' "$WINE_TREE/dlls/winedmo/winedmo.rgs"; then
  RTSPT_REGISTRY=yes
else
  RTSPT_REGISTRY=no
fi
if has_pattern 'rtspt:' "$WINE_TREE/dlls/winedmo/unix_demuxer.c"; then
  RTSPT_DEMUXER=yes
else
  RTSPT_DEMUXER=no
fi
fact rtspt_registry_scheme "$RTSPT_REGISTRY"
fact rtspt_demuxer_handling "$RTSPT_DEMUXER"
if [[ "$RTSPT_REGISTRY" == yes && "$RTSPT_DEMUXER" == yes ]]; then
  fact rtspt_complete_hint yes
else
  fact rtspt_complete_hint no
fi
bool_fact ffmpeg_interrupt_callback_assignment \
  'interrupt_callback([.]callback)?[[:space:]]*=' \
  "$WINE_TREE/dlls/winedmo"
bool_fact rtsp_timeout_dictionary_option \
  'av_dict_set(_int)?[[:space:]]*\([^,]+,[[:space:]]*"timeout"' \
  "$WINE_TREE/dlls/winedmo"
bool_fact ffmpeg_unseekable_flag_propagation 'AVFMTCTX_UNSEEKABLE' \
  "$WINE_TREE/dlls/winedmo"
bool_fact conditional_mf_seek_capability_hint \
  '(seekable|can_seek).*MFMEDIASOURCE_CAN_SEEK|MFMEDIASOURCE_CAN_SEEK.*(seekable|can_seek)' \
  "$WINE_TREE/dlls/winedmo"
bool_fact direct_rtsp_url_path 'get_rtsp_url_kind' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact rtsp_tcp_alias_path 'DIRECT_NETWORK_RTSP_TCP' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact demuxer_cancellation_api 'winedmo_demuxer_set_cancelled' \
  "$WINE_TREE/dlls/winedmo/main.c" "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact atomic_interrupt_cancellation \
  '__atomic_load_n[[:space:]]*\([[:space:]]*&demuxer->cancelled' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact rtsp_open_deadline 'RTSP_OPEN_TIMEOUT_US' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact rtsp_io_deadline 'RTSP_IO_TIMEOUT_US' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact hls_io_deadline 'HLS_IO_TIMEOUT_US' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact demuxer_seekability_query 'winedmo_demuxer_get_flags' \
  "$WINE_TREE/dlls/winedmo/main.c" "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact stale_inflight_packet_discard \
  'read_epoch != media_source_get_demux_transition' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact demux_transition_epoch 'demux_transition_epoch' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact multithreaded_sample_request_queue \
  'MFAllocateWorkQueueEx\(MF_MULTITHREADED_WORKQUEUE' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact per_stream_fifo_sample_worker 'sample_worker_pending' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact sample_request_generation_guard 'request_generation' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact counted_source_work_items 'pending_work_items' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact one_shot_stream_eos 'eos_sent' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact aggregate_emergency_queue_bound \
  'queued_packets >= MEDIA_SOURCE_EMERGENCY_QUEUED_PACKETS' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact simple_buffer_overflow_guard 'SIZE_MAX - header_size' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact late_bytestream_buffer_ownership 'cb->buffer' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact bytestream_endread_in_callback \
  'cb->status = IMFByteStream_EndRead' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact source_load_generation 'source_generation' \
  "$WINE_TREE/dlls/mfmediaengine/main.c"
bool_fact playback_intent_tracking 'playback_requested' \
  "$WINE_TREE/dlls/mfmediaengine/main.c"
bool_fact seek_sink_demand_reprime 'session_flush_sinks' \
  "$WINE_TREE/dlls/mf/session.c"
bool_fact deferred_duplicate_seek_collapse \
  'fabs\(engine->deferred_seek - current_seek\) <= 0[.]001' \
  "$WINE_TREE/dlls/mfmediaengine/main.c"
bool_fact demux_stream_index_mapping 'media_source_find_stream' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact hls_urlmon_bypass '!is_http_hls_url\(context->url\)' \
  "$WINE_TREE/dlls/winedmo/media_source.c"
bool_fact case_insensitive_hls_classification 'strncasecmp.*[.]m3u8' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
bool_fact hardened_custom_seek_origin 'whence &= ~AVSEEK_FORCE' \
  "$WINE_TREE/dlls/winedmo/unixlib.c"
bool_fact overflow_safe_seek_rescale \
  'av_rescale\([[:space:]]*params->timestamp,[[:space:]]*AV_TIME_BASE,[[:space:]]*10000000' \
  "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
if has_pattern 'BOOL queued = FALSE' "$WINE_TREE/dlls/winedmo/media_source.c" \
    && has_pattern 'read_epoch == media_source_get_demux_transition' \
      "$WINE_TREE/dlls/winedmo/media_source.c" \
    && has_pattern 'if \(queued\)' "$WINE_TREE/dlls/winedmo/media_source.c"; then
  fact flush_safe_packet_queue_handoff yes
else
  fact flush_safe_packet_queue_handoff no
fi

if python3 - "$WINE_TREE/dlls/winedmo/media_source.c" 2>/dev/null <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()

def region(start, end):
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]

def require_order(body, *needles):
    positions = [body.index(needle) for needle in needles]
    if positions != sorted(positions):
        raise ValueError("required cancellation/ownership order changed")

require_order(
    region("static HRESULT WINAPI media_source_Shutdown", "static const IMFMediaSourceVtbl"),
    "winedmo_demuxer_set_cancelled(source->demuxer, TRUE)",
    "WaitForSingleObject(source->demux_thread, INFINITE)",
    "winedmo_demuxer_destroy(&source->demuxer)",
)
require_order(
    region("static HRESULT media_source_create", "static HRESULT result_entry_create"),
    "winedmo_demuxer_set_cancelled(object->demuxer, TRUE)",
    "WaitForSingleObject(object->demux_thread, INFINITE)",
    "winedmo_demuxer_destroy(&object->demuxer)",
)
require_order(
    region("static HRESULT media_source_start", "static HRESULT media_source_pause"),
    "winedmo_demuxer_set_cancelled(source->demuxer, TRUE)",
    "EnterCriticalSection(&source->demuxer_cs)",
    "winedmo_demuxer_set_cancelled(source->demuxer, FALSE)",
    "winedmo_demuxer_seek(source->demuxer, position->hVal.QuadPart)",
)
PY
then
  ALPHA_ORDERING=yes
else
  ALPHA_ORDERING=no
fi
fact cancellation_and_seek_ordering "$ALPHA_ORDERING"

if python3 - "$WINE_TREE/dlls/winedmo/media_source.c" \
    "$WINE_TREE/dlls/mfmediaengine/main.c" \
    "$WINE_TREE/dlls/winedmo/unix_demuxer.c" \
    "$WINE_TREE/dlls/winedmo/unixlib.c" \
    "$WINE_TREE/dlls/mf/session.c" \
    "$REQUIRE_A39_SERIES" \
    "$REQUIRE_A310_SERIES" \
    "$REQUIRE_A311_SERIES" \
    "$REQUIRE_A312_SERIES" \
    "$RTSP_DRAIN_SERIES" "$A320_SERIES" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
engine = Path(sys.argv[2]).read_text()
demux = Path(sys.argv[3]).read_text()
unixlib = Path(sys.argv[4]).read_text()
session = Path(sys.argv[5]).read_text()
require_a39_series = any(value == "1" for value in sys.argv[6:10])
require_a312_series = sys.argv[9] == "1"
rtsp_drain_series = sys.argv[10] == "1"
a320_series = sys.argv[11] == "1"

def region(text, start, end):
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]

def require_order(body, *needles):
    positions = [body.index(needle) for needle in needles]
    assert positions == sorted(positions)

begin = region(source, "static HRESULT WINAPI scheme_handler_BeginCreateObject",\
        "static HRESULT WINAPI scheme_handler_EndCreateObject")
callback = region(source, "static HRESULT WINAPI scheme_handler_callback_Invoke",\
        "static const IMFAsyncCallbackVtbl scheme_handler_callback_vtbl")
cache_http = region(source, "static HRESULT cache_http_url",\
        "static HRESULT WINAPI scheme_handler_callback_Invoke")
queues = region(source, "static BOOL media_source_stream_queues_full",\
        "/* ========================================================================\n * source_async_command")
tokens = region(source, "static BOOL enqueue_token",\
        "/* ========================================================================\n * winedmo stream callbacks")
read_callback = region(source, "static HRESULT WINAPI winedmo_bytestream_read_cb_Invoke",\
        "static const IMFAsyncCallbackVtbl winedmo_bytestream_read_cb_vtbl")
read_helper = region(source, "static HRESULT winedmo_bytestream_read",\
        "static NTSTATUS CDECL source_stream_seek_cb")
submit = region(source, "static HRESULT source_submit_work_item",\
        "/* ========================================================================\n * async_commands_callback")
schedule = region(source, "static HRESULT schedule_sample_worker",\
        "static void flush_token_queue")
start = region(source, "static HRESULT media_source_start",\
        "/* ========================================================================\n * media_source_pause")
pause = region(source, "static HRESULT media_source_pause",\
        "static HRESULT media_source_stop")
stop = region(source, "static HRESULT media_source_stop",\
        "/* ========================================================================\n * media_stream_send_eos")
eos = region(source, "static HRESULT media_stream_send_eos",\
        "static HRESULT media_stream_send_sample")
wait = region(source, "static HRESULT wait_on_sample",\
        "/* ========================================================================\n * Demux thread")
invoke = region(source, "static HRESULT WINAPI source_async_commands_Invoke",\
        "static const IMFAsyncCallbackVtbl source_async_commands_callback_vtbl")
request_case = region(invoke, "case SOURCE_ASYNC_REQUEST_SAMPLE:", "            break;")
completion = invoke[invoke.index("LeaveCriticalSection(&source->cs)", invoke.index("switch (command->op)")):]
request = region(source, "static HRESULT WINAPI media_stream_RequestSample",\
        "static const IMFMediaStreamVtbl media_stream_vtbl")
create = region(source, "static HRESULT media_source_create",\
        "static HRESULT result_entry_create")
shutdown = region(source, "static HRESULT WINAPI media_source_Shutdown",\
        "static const IMFMediaSourceVtbl")
shutdown_active = shutdown[shutdown.index("source->state = SOURCE_SHUTDOWN"):]
load_end = region(engine, "static HRESULT media_engine_end_source_load",\
        "static void media_engine_discard_source_object")
load_invoke = region(engine, "static HRESULT WINAPI media_engine_load_handler_Invoke",\
        "static const IMFAsyncCallbackVtbl media_engine_load_handler_vtbl")
set_source = region(engine, "static HRESULT media_engine_set_source",\
        "static HRESULT WINAPI media_engine_SetSource")
clear_presentation = region(engine, "static void media_engine_clear_presentation",\
        "static void media_engine_clear_effects")
free_engine = region(engine, "static void free_media_engine",\
        "static ULONG WINAPI media_engine_Release")
topology = region(engine, "static HRESULT media_engine_create_topology",\
        "static void media_engine_start_playback")
topology_after_clear = topology[topology.index("media_engine_clear_presentation(engine)"):]
seekable = region(engine, "static HRESULT WINAPI media_engine_GetSeekable",\
        "static BOOL WINAPI media_engine_IsEnded")
set_time_ex = region(engine, "static HRESULT WINAPI media_engine_SetCurrentTimeEx",\
        "static HRESULT WINAPI media_engine_EnableTimeUpdateTimer")
defer_seek = region(engine, "static HRESULT media_engine_defer_current_time(struct media_engine *engine, double seektime)\n{",\
        "static HRESULT WINAPI media_engine_SetCurrentTime")
schedule_seek = region(engine, "static HRESULT media_engine_schedule_deferred_seek(struct media_engine *engine, DWORD delay)\n{",\
        "static ULONG WINAPI media_engine_deferred_seek_callback_AddRef")
deferred_callback = region(engine, "static HRESULT WINAPI media_engine_deferred_seek_callback_Invoke",\
        "static const IMFAsyncCallbackVtbl media_engine_deferred_seek_callback_vtbl")
flush_deferred = region(engine, "static HRESULT media_engine_flush_deferred_seek(struct media_engine *engine)\n{",\
        "static HRESULT media_engine_schedule_deferred_seek")
set_current_helper = region(engine, "static HRESULT media_engine_set_current_time(struct media_engine *engine, double seektime)\n{",\
        "static HRESULT media_engine_flush_deferred_seek")
inflight_seek = region(set_current_helper, "if (engine->flags & FLAGS_ENGINE_SEEKING)",\
        "position.vt = VT_I8")
session_restart = region(session, "static void session_flush_sinks",\
        "static void session_flush_nodes")
session_source_state = region(session, "static void session_set_source_object_state",\
        "static void session_set_sink_stream_state")
session_request = region(session, "static void session_request_sample(struct media_session *session, IMFStreamSink *sink_stream)\n{",\
        "static void session_deliver_sample")
hls_wide = region(source, "static BOOL is_http_hls_url",\
        "static HRESULT WINAPI scheme_handler_callback_Invoke")
hls_unix = region(demux, "static BOOL is_http_hls_url",\
        "enum direct_network_url")

assert "URLOpenBlockingStreamW" not in begin
assert "CreateStreamOnHGlobal" in begin
assert "IMFByteStream_Release(bytestream)" in begin
assert cache_http.count("URLOpenBlockingStreamW") == 1
assert "cache_http_url(context)" in callback
assert "MFASYNC_CALLBACK_QUEUE_IO" in source
assert "IMFByteStream_GetLength" in cache_http
assert "IMFByteStream_GetCurrentPosition" in cache_http
assert "context->file_size = file_size" in cache_http
assert "context->stream_offset = stream_offset" in cache_http
assert "!is_http_hls_url(context->url)" in callback
assert "url && !tmp_url" in source
assert "SIZE_MAX - header_size" in source
require_order(read_callback, "IMFByteStream_EndRead", "SetEvent(cb->event)")
assert "IMFAsyncResult_AddRef" not in read_callback
assert "IMFByteStream_BeginRead(stream, cb->buffer" in read_helper
assert "memcpy(buffer, cb->buffer, cb->read_size)" in read_helper
assert "HRESULT_FROM_WIN32(ERROR_TIMEOUT)" in read_helper
assert "wcsnicmp" in hls_wide
assert "strncasecmp" in hls_unix
assert hls_wide.count(".m3u8") == hls_unix.count(".m3u8") == 1
assert hls_wide.count("/hls_playlist/") == hls_unix.count("/hls_playlist/") == 1
assert "queued_packets += stream->queued_packets" in queues
assert "queued_bytes += stream->queued_bytes" in queues
assert "queued_packets >= MEDIA_SOURCE_EMERGENCY_QUEUED_PACKETS" in queues
assert "queued_bytes >= MEDIA_SOURCE_EMERGENCY_QUEUED_BYTES" in queues
if rtsp_drain_series:
    assert "if (primed && any_packet_full)" in queues
    assert "if (primed && any_byte_full)" in queues
    assert "media_source_drain_diag_queue(source, reason" in queues
    assert "return !!reason" in queues
else:
    assert "(primed && any_full)" in queues
assert "empty = !stream->queued_packets" in queues
assert "flush_token_queue(source->streams[i], source->streams[i]->active)" in start
assert "prepend_token" in tokens and "dequeue_token" in tokens
assert "MFAllocateWorkQueueEx(MF_MULTITHREADED_WORKQUEUE" in create
assert "source_submit_work_item(source, source->sample_requests_queue, op)" in schedule
assert "stream->sample_worker_pending || !stream->token_queue_count" in schedule
assert "command->u.request_sample.generation = stream->request_generation" in schedule
assert "stream->sample_worker_pending = TRUE" in schedule
assert "++source->pending_work_items" in submit
assert "MFPutWorkItem(queue, &source->async_commands_callback, op)" in submit
assert "--source->pending_work_items" in submit
assert "enqueue_token(stream, token)" in request
assert "object->active = FALSE" in source
assert "media_source_begin_demux_transition(source)" in start
assert "media_source_end_demux_transition(source)" in start
assert "status = winedmo_demuxer_seek" in start
assert "cancel_transition_failed" not in start
seek_transition = start[start.index("status = winedmo_demuxer_seek"):]
if require_a312_series:
    assert "return media_source_get_demux_error(source)" in start
    assert "media_source_queue_demux_error(source, error)" in start
    if a320_series:
        # A3.20 routes every post-flush terminal seek failure through the same
        # first-error helper. The source-bound regression also checks that
        # helper stops the producer and preserves identity/generation guards.
        assert "if (error != MF_E_OPERATION_CANCELLED && error != MF_E_NET_TIMEOUT" in start
        require_order(seek_transition, "if (FAILED(seek_error) || status)",
                "error = MF_E_INVALID_POSITION", "media_source_queue_demux_error(source, error)")
        assert "media_source_set_demux_error(source, error)" not in start
    else:
        assert "if (error == MF_E_OPERATION_CANCELLED || error == MF_E_NET_TIMEOUT" in start
        assert "source->demux_thread_shutdown = true" in start
        assert "media_source_set_demux_error(source, error)" in start
    assert "stream->eos = TRUE" not in start
    require_order(seek_transition, "status = winedmo_demuxer_seek",
            "if (FAILED(seek_error) || status)",
            "media_source_queue_demux_error(source, error)",
            "media_source_end_demux_transition(source)")
else:
    assert "return HRESULT_FROM_NT(status)" in start
    assert "source->demux_terminal_error = HRESULT_FROM_NT(status)" in start
    require_order(seek_transition, "status = winedmo_demuxer_seek", "if (status)",
            "source->demux_thread_shutdown = true",
            "source->demux_terminal_error = HRESULT_FROM_NT(status)", "else",
            "media_source_end_demux_transition(source)")
require_order(pause, "stream->paused = TRUE", "WakeAllConditionVariable(&stream->queue_cv)")
require_order(stop, "++stream->request_generation", "stream->active = FALSE",
        "WakeAllConditionVariable(&stream->queue_cv)")
require_order(wait, "stream_requeue_packet(stream, pkt)", "prepend_token(stream, *token)")
require_order(request_case, "command->u.request_sample.generation == stream->request_generation",
        "dequeue_token(stream, &command->u.request_sample.token)",
        "wait_on_sample(stream, &command->u.request_sample.token",
        "stream->sample_worker_pending = FALSE", "flush_token_queue(stream, TRUE)")
require_order(completion, "LeaveCriticalSection(&source->cs)", "IUnknown_Release(state)",
        "EnterCriticalSection(&source->cs)", "--source->pending_work_items")
require_order(eos, "if (stream->eos_sent)", "stream->eos_sent = TRUE",
        "MEEndOfStream", "if (s->active && !s->eos_sent)",
        "media_source_queue_end_of_presentation(source)")
require_order(shutdown_active, "source->state = SOURCE_SHUTDOWN",
        "WakeAllConditionVariable(&stream->queue_cv)",
        "while (source->pending_work_items)",
        "LeaveCriticalSection(&source->cs)",
        "MFUnlockWorkQueue(sample_requests_queue)",
        "WaitForSingleObject(source->demux_thread, INFINITE)",
        "winedmo_demuxer_destroy(&source->demuxer)")
assert "++stream->request_generation" in start
assert "generation != stream->request_generation" in wait
assert "pkt->epoch != epoch" in wait
assert "eos_epoch == epoch" in wait
assert "MEDIA_ENGINE_LOAD_EXTENSION" in load_end
assert "IMFMediaEngineExtension_EndCreateObject" in load_end
assert "IMFSourceResolver_EndCreateObjectFromURL" in load_end
assert "IMFSourceResolver_EndCreateObjectFromByteStream" in load_end
require_order(load_invoke, "media_engine_end_source_load(engine, context, result, &object)",
        "generation = context->generation", "IUnknown_Release(state)",
        "EnterCriticalSection(&engine->cs)",
        "!media_engine_source_is_current(engine, generation)",
        "media_engine_discard_source_object(object)")
assert load_invoke.count("media_engine_source_is_current(engine, generation)") >= 4
require_order(load_invoke, "media_engine_create_topology(engine, source, generation)",
        "start_playback = engine->playback_requested",
        "FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING, FALSE")
require_order(set_source, "++engine->source_generation",
        "media_engine_load_context_create(origin, generation",
        "BeginCreateObject")
assert "if (!url && !bytestream)" in set_source
assert "FLAGS_ENGINE_SOURCE_PENDING | FLAGS_ENGINE_PLAY_PENDING, FALSE" in set_source
assert "engine->playback_requested = start_pending" in set_source
assert engine.count("engine->playback_requested = TRUE") >= 1
assert engine.count("engine->playback_requested = FALSE") >= 2
assert "media_source_find_stream(source, id, &source_index)" in source
assert "media_source_find_stream(source, stream_idx, NULL)" in source
if require_a39_series:
    require_order(clear_presentation,
            "IMFMediaSource *source = engine->presentation.source",
            "engine->presentation.source = NULL",
            "engine->presentation.pd = NULL",
            "engine->presentation.frame_sink = NULL",
            "LeaveCriticalSection(&engine->cs)",
            "IMFMediaSource_Shutdown(source)", "IMFMediaSource_Release(source)")
    assert "memset(&engine->presentation" not in clear_presentation
    assert "engine->presentation.start_position =" not in clear_presentation
    assert "engine->presentation.start_position.vt =" not in clear_presentation
else:
    require_order(clear_presentation,
            "IMFMediaSource *source = engine->presentation.source",
            "memset(&engine->presentation", "LeaveCriticalSection(&engine->cs)",
            "IMFMediaSource_Shutdown(source)", "IMFMediaSource_Release(source)")
require_order(free_engine, "EnterCriticalSection(&engine->cs)",
        "shutdown_session = !(engine->flags & FLAGS_ENGINE_SHUT_DOWN)",
        "media_engine_clear_presentation(engine)", "LeaveCriticalSection(&engine->cs)",
        "if (shutdown_session && engine->session)",
        "IMFMediaSession_Shutdown(engine->session)",
        "IMFMediaSession_Release(engine->session)")
assert topology.count("media_engine_source_is_current(engine, generation)") >= 3
require_order(topology_after_clear, "media_engine_clear_presentation(engine)",
        "if (!media_engine_source_is_current(engine, generation))",
        "engine->presentation.source = source")
assert "MFMEDIASOURCE_CAN_SEEK" in seekable
assert "MFBYTESTREAM_IS_SEEKABLE" not in seekable
assert "media_engine_defer_current_time(engine, seektime)" in set_time_ex
assert "media_engine_set_current_time(engine, seektime)" not in set_time_ex
assert "fabs(current - seektime) <= 3.0" in defer_seek
assert "engine->flags & FLAGS_ENGINE_PAUSED" in defer_seek
assert "media_engine_schedule_deferred_seek(engine, 250)" in defer_seek
assert "MFScheduleWorkItem(&engine->deferred_seek_callback" in schedule_seek
assert "engine->deferred_seek_work_scheduled" in schedule_seek
assert "&engine->deferred_seek_work_key" in schedule_seek
assert "media_engine_flush_deferred_seek(engine)" in schedule_seek
require_order(deferred_callback, "engine->deferred_seek_work_scheduled = FALSE",
        "GetTickCount() - engine->deferred_seek_time", "elapsed < 250",
        "media_engine_schedule_deferred_seek(engine, 250 - elapsed)")
session_events = region(engine, "static HRESULT WINAPI media_engine_session_events_Invoke",\
        "static const IMFAsyncCallbackVtbl media_engine_session_events_vtbl")
assert "BOOL deferred_seek_pending = engine->flags & FLAGS_ENGINE_DEFERRED_SEEK" in session_events
assert "fabs(engine->deferred_seek - current_seek) <= 0.001" in session_events
assert "fabs(engine->deferred_seek - current_seek) <= 3.0" not in session_events
assert "next_seek_from_deferred" not in engine
require_order(session_events, "if (deferred_seek_pending && isfinite(current_seek)",
        "engine->deferred_seek = NAN", "deferred_seek_pending = FALSE",
        "engine->current_seek = NAN",
        "engine->next_seek = NAN", "if (isfinite(next_seek) &&",
        "else if (!deferred_seek_pending")
require_order(flush_deferred, "seektime = engine->deferred_seek",
        "engine->deferred_seek = NAN",
        "media_engine_set_flag(engine, FLAGS_ENGINE_DEFERRED_SEEK, FALSE)",
        "return media_engine_set_current_time(engine, seektime)")
assert "duplicate_current" not in engine
require_order(inflight_seek,
        "if (isfinite(engine->current_seek) && fabs(engine->current_seek - seektime) <= 0.001)",
        "engine->next_seek = NAN", "return S_OK",
        "if (isfinite(engine->next_seek) && fabs(engine->next_seek - seektime) <= 0.001)",
        "engine->next_seek = seektime")
require_order(session_restart, "if (node->u.sink.requests)",
        "node->u.sink.requests--", "session_request_sample(session",
        "IMFStreamSink_Flush")
assert "session_sources_are_restarting" not in session
if a320_series:
    require_order(session_request[session_request.index("sink_node->u.sink.requests++"):],
            "sink_node->u.sink.requests++",
            "session->command_state == COMMAND_STATE_RESTARTING_SOURCES",
            "session->command_state == COMMAND_STATE_STARTING_SOURCES",
            "session->presentation.flags & SESSION_FLAG_RESTARTING",
            "return;", "session_request_sample_from_node(session, up_node, output)")
else:
    assert "SESSION_FLAG_RESTARTING" not in session_request
require_order(session_source_state,
        "session->presentation.flags &= ~SESSION_FLAG_RESTARTING",
        "session_flush_sinks(session)", "session_start_clock(session)")
shutdown_engine = region(engine, "static HRESULT WINAPI media_engine_Shutdown",\
        "static void set_rect")
require_order(shutdown_engine, "deferred_seek_work_key = engine->deferred_seek_work_key",
        "LeaveCriticalSection(&engine->cs)", "MFCancelWorkItem(deferred_seek_work_key)",
        "IMFMediaSession_Shutdown(engine->session)")
assert "engine->deferred_seek = NAN" in defer_seek
set_time = region(engine, "static HRESULT WINAPI media_engine_SetCurrentTime",\
        "static double WINAPI media_engine_GetStartTime")
require_order(set_time, "engine->deferred_seek = NAN",
        "media_engine_set_flag(engine, FLAGS_ENGINE_DEFERRED_SEEK, FALSE)",
        "media_engine_set_current_time(engine, time)")
assert "engine->deferred_seek_callback.lpVtbl = &media_engine_deferred_seek_callback_vtbl" in engine
assert "av_rescale( params->timestamp, AV_TIME_BASE, 10000000 )" in demux
assert "whence &= ~AVSEEK_FORCE" in unixlib
assert "offset > INT64_MAX - (int64_t)base" in unixlib
assert "if (level > av_log_get_level()) return;" in unixlib
PY
then
  MEDIA_REGRESSION_FIXES=yes
else
  MEDIA_REGRESSION_FIXES=no
fi
fact async_http_and_vod_seek_repairs "$MEDIA_REGRESSION_FIXES"
fact failed_seek_terminal_state "$MEDIA_REGRESSION_FIXES"
fact sample_lifecycle_repairs "$MEDIA_REGRESSION_FIXES"
fact source_load_generation_repairs "$MEDIA_REGRESSION_FIXES"
fact media_engine_destructor_shutdown_order "$MEDIA_REGRESSION_FIXES"
fact seek_restart_demand_recovery "$MEDIA_REGRESSION_FIXES"
fact deferred_duplicate_seek_recovery "$MEDIA_REGRESSION_FIXES"

if sed -n '/static NTSTATUS wow64_demuxer_destroy/,/^}/p' \
    "$WINE_TREE/dlls/winedmo/unixlib.c" \
    | rg -q 'struct demuxer_destroy_params'; then
  fact wow64_destroy_uses_destroy_params yes
else
  fact wow64_destroy_uses_destroy_params no
fi

if python3 - "$WINE_TREE/dlls/winedmo/unix_transform.c" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()

def region(start, end):
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]

output = region("NTSTATUS transform_get_output", "NTSTATUS transform_drain")
push = region("NTSTATUS transform_push_input", "NTSTATUS transform_get_output")
flush = region("NTSTATUS transform_flush", "NTSTATUS transform_get_output_format")

assert "if (!t->audio_started && t->audio_output_pts_adjust == INT64_MIN" in output
assert "t->audio_output_pts_adjust = INT64_MIN" in push
assert "t->audio_output_pts_adjust = INT64_MIN" not in flush
assert "t->audio_pts_offset   = 0" not in flush
assert "t->audio_started      = false" in flush
PY
then
  AUDIO_SEEK_TIMELINE=yes
else
  AUDIO_SEEK_TIMELINE=no
fi
fact audio_seek_timeline_preserved "$AUDIO_SEEK_TIMELINE"

if python3 - "$WINE_TREE/dlls/mf/sar.c" <<'PY'
from pathlib import Path
import sys

sar = Path(sys.argv[1]).read_text()

def region(text, start, end):
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]

def require_order(body, *needles):
    positions = [body.index(needle) for needle in needles]
    assert positions == sorted(positions)

start_client = region(sar, "static HRESULT audio_renderer_start_audio_client",\
        "static HRESULT WINAPI audio_renderer_clock_sink_QueryInterface")
clock_start = region(sar, "audio_renderer_clock_sink_OnClockStart",\
        "audio_renderer_clock_sink_OnClockStop")
clock_restart = region(sar, "audio_renderer_clock_sink_OnClockRestart",\
        "audio_renderer_clock_sink_OnClockSetRate")
flush = region(sar, "audio_renderer_stream_Flush",\
        "static const IMFStreamSinkVtbl audio_renderer_stream_vtbl")

assert "SAR_AUDIO_START_PENDING" in sar
assert "IAudioClient_Start" in start_client
assert "renderer->flags |= SAR_AUDIO_START_PENDING" in start_client
assert "renderer->flags &= ~SAR_AUDIO_START_PENDING" in start_client
assert "renderer->flags & SAR_AUDIO_START_PENDING" in clock_start
assert "renderer->flags & SAR_AUDIO_START_PENDING" in clock_restart
require_order(flush, "IAudioClient_Stop", "IAudioClient_Reset",\
        "audio_renderer_start_audio_client")
assert "renderer->flags |= SAR_AUDIO_START_PENDING" in flush
PY
then
  SAR_FLUSH_CONTROL=yes
else
  SAR_FLUSH_CONTROL=no
fi
fact sar_state_preserving_flush "$SAR_FLUSH_CONTROL"

INFINITE_WAITS="$(
  { rg -n 'WaitForSingleObject\([^;]*INFINITE' \
      "$WINE_TREE/dlls/winedmo" 2>/dev/null || true; } | wc -l
)"
fact winedmo_infinite_wait_sites "$INFINITE_WAITS"

if ! has_pattern 'source_demux_thread' "$WINE_TREE/dlls/winedmo/media_source.c"; then
  fail "WineDMO media-source demux thread is absent"
fi
if ! has_pattern 'is_http_hls_url' "$WINE_TREE/dlls/winedmo/unix_demuxer.c"; then
  fail "GE direct-HLS baseline is absent"
fi

if [[ "$REQUIRE_ALPHA_SERIES" == 1 ]]; then
  require_alpha_pattern() {
    local pattern="$1"
    local description="$2"
    shift 2
    has_pattern "$pattern" "$@" || fail "$description"
  }

  require_alpha_pattern 'get_rtsp_url_kind' \
    "direct RTSP URL path is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'DIRECT_NETWORK_RTSP_TCP' \
    "RTSP-over-TCP alias path is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'rtspt:' \
    "RTSP-over-TCP scheme registration is absent" "$WINE_TREE/dlls/winedmo/winedmo.rgs"
  require_alpha_pattern \
    'av_dict_set(_int)?[[:space:]]*\([^,]+,[[:space:]]*"timeout"' \
    "RTSP socket timeout option is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  callback_count="$(rg -c \
    'interrupt_callback[.]callback[[:space:]]*=[[:space:]]*demuxer_interrupt_callback' \
    "$WINE_TREE/dlls/winedmo/unix_demuxer.c" || true)"
  [[ "$callback_count" -ge 2 ]] \
    || fail "interrupt callbacks are not assigned to both direct HLS and RTSP"
  require_alpha_pattern \
    '__atomic_load_n[[:space:]]*\([[:space:]]*&demuxer->cancelled' \
    "atomic demux cancellation check is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'winedmo_demuxer_set_cancelled' \
    "Media Foundation cancellation call is absent" \
    "$WINE_TREE/dlls/winedmo/main.c" "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'demuxer_set_cancelled' \
    "Unix cancellation entry point is absent" \
    "$WINE_TREE/dlls/winedmo/unix_demuxer.c" \
    "$WINE_TREE/dlls/winedmo/unixlib.c" "$WINE_TREE/dlls/winedmo/unixlib.h"
  require_alpha_pattern 'RTSP_OPEN_TIMEOUT_US' \
    "bounded RTSP open/probe deadline is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'RTSP_IO_TIMEOUT_US' \
    "bounded RTSP read/seek/close deadline is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'HLS_IO_TIMEOUT_US' \
    "bounded HLS network deadline is absent" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'AVFMTCTX_UNSEEKABLE' \
    "FFmpeg live unseekability is not propagated" "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern \
    'seekable.*MFMEDIASOURCE_CAN_SEEK|MFMEDIASOURCE_CAN_SEEK.*seekable' \
    "Media Foundation seek capability is not conditional" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'read_epoch != media_source_get_demux_transition' \
    "epoch-based in-flight packet discard is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'BOOL queued = FALSE' \
    "flush-safe packet queue handoff state is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'read_epoch == media_source_get_demux_transition' \
    "packet queue handoff does not recheck the transition epoch under its lock" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'MFAllocateWorkQueueEx\(MF_MULTITHREADED_WORKQUEUE' \
    "multithreaded sample-request queue is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'sample_worker_pending' \
    "per-stream FIFO worker ownership is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'prepend_token' \
    "paused sample-token handoff is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'request_generation' \
    "sample-request generation guard is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'pending_work_items' \
    "source work-item drain accounting is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'eos_sent' \
    "one-shot stream EOS accounting is absent" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'queued_packets >= MEDIA_SOURCE_EMERGENCY_QUEUED_PACKETS' \
    "packet queues lack an aggregate emergency high-water mark" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'media_engine_schedule_deferred_seek' \
    "MediaEngine deferred seeks lack a guaranteed scheduler" \
    "$WINE_TREE/dlls/mfmediaengine/main.c"
  require_alpha_pattern 'session_flush_sinks' \
    "post-seek sink demand is not re-primed" \
    "$WINE_TREE/dlls/mf/session.c"
  require_alpha_pattern 'fabs\(engine->deferred_seek - current_seek\) <= 0[.]001' \
    "in-flight deferred duplicate seeks are not collapsed safely" \
    "$WINE_TREE/dlls/mfmediaengine/main.c"
  require_alpha_pattern 'SIZE_MAX - header_size' \
    "simple demux-buffer allocation lacks an overflow guard" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'media_source_find_stream' \
    "demux stream indices are not mapped to compact Media Foundation streams" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'cb->status = IMFByteStream_EndRead' \
    "late asynchronous byte-stream completion is not owned safely" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern '!is_http_hls_url\(context->url\)' \
    "direct HLS still takes the redundant URLMon path" \
    "$WINE_TREE/dlls/winedmo/media_source.c"
  require_alpha_pattern 'source_generation' \
    "MediaEngine source-load generation tracking is absent" \
    "$WINE_TREE/dlls/mfmediaengine/main.c"
  require_alpha_pattern 'playback_requested' \
    "MediaEngine load completion does not preserve current Play/Pause intent" \
    "$WINE_TREE/dlls/mfmediaengine/main.c"
  require_alpha_pattern \
    'av_rescale\([[:space:]]*params->timestamp,[[:space:]]*AV_TIME_BASE,[[:space:]]*10000000' \
    "overflow-safe Media Foundation timestamp scaling is absent" \
    "$WINE_TREE/dlls/winedmo/unix_demuxer.c"
  require_alpha_pattern 'whence &= ~AVSEEK_FORCE' \
    "custom AVIO seek-origin hardening is absent" \
    "$WINE_TREE/dlls/winedmo/unixlib.c"
  require_alpha_pattern 'struct demuxer_destroy_params params = \{0\}' \
    "WOW64 demux destroy parameters are not corrected" \
    "$WINE_TREE/dlls/winedmo/unixlib.c"
  [[ "$ALPHA_ORDERING" == yes ]] \
    || fail "cancellation must precede joins/destruction and bracket seek locking"
  [[ "$MEDIA_REGRESSION_FIXES" == yes ]] \
    || fail "asynchronous HTTP open and VOD seek regression repairs are incomplete"
  [[ "$AUDIO_SEEK_TIMELINE" == yes ]] \
    || fail "decoder flush resets the established audio presentation timeline"
  python3 "$ROOT_DIR/tests/progressive-http-vod/audit.py" \
    --wine-tree "$WINE_TREE" >/dev/null \
    || fail "progressive direct-HTTP source contract is incomplete"
  fact progressive_http_vod_audit passed
  python3 "$ROOT_DIR/tests/vod-control/test_model.py" >/dev/null \
    || fail "VOD seek/backpressure policy model failed"
  fact vod_control_model passed
  fact alpha_series_audit passed
fi

if [[ "$REQUIRE_A36_SERIES" == 1 || "$REQUIRE_A37_SERIES" == 1 \
    || "$REQUIRE_A38_SERIES" == 1 || "$REQUIRE_A39_SERIES" == 1 \
    || "$REQUIRE_A310_SERIES" == 1 || "$REQUIRE_A311_SERIES" == 1 \
    || "$REQUIRE_A312_SERIES" == 1 ]]; then
  [[ "$SAR_FLUSH_CONTROL" == yes ]] \
    || fail "A3.6 state-preserving audio-flush control flow is incomplete"
  python3 "$ROOT_DIR/tests/sar-flush-control/test_model.py" >/dev/null \
    || fail "A3.6 SAR audio-flush control-policy model failed"
  python3 "$ROOT_DIR/tests/clock-start-control/test_model.py" >/dev/null \
    || fail "A3.6 clock-start control-policy model failed"
  python3 "$ROOT_DIR/tests/clock-start-control/audit.py" "$WINE_TREE" >/dev/null \
    || fail "A3.6 clock-start source contract is incomplete"
  fact sar_flush_control_model passed
  fact clock_start_control_model passed
  fact clock_start_source_audit passed
  if [[ "$DIAGNOSTIC_SERIES" == 1 ]]; then
    python3 "$ROOT_DIR/tests/pcm-probe-control/test_model.py" >/dev/null \
      || fail "A3.6 PCM-probe aggregate model failed"
    python3 "$ROOT_DIR/tests/pcm-probe-control/audit.py" "$WINE_TREE" >/dev/null \
      || fail "A3.6 PCM-probe source contract is incomplete"
    fact pcm_probe_control_model passed
    fact pcm_probe_source_audit passed
  fi
  fact a36_series_audit passed
fi

if [[ "$REQUIRE_A37_SERIES" == 1 || "$REQUIRE_A38_SERIES" == 1 \
    || "$REQUIRE_A39_SERIES" == 1 || "$REQUIRE_A310_SERIES" == 1 \
    || "$REQUIRE_A311_SERIES" == 1 || "$REQUIRE_A312_SERIES" == 1 ]]; then
  if [[ "$DIAGNOSTIC_SERIES" == 1 ]]; then
    python3 "$ROOT_DIR/tests/avpro-audio-state/test_model.py" >/dev/null \
      || fail "A3.7 AVPro audio-state model failed"
    python3 "$ROOT_DIR/tests/avpro-audio-state/audit.py" "$WINE_TREE" >/dev/null \
      || fail "A3.7 AVPro audio-state source contract is incomplete"
    fact avpro_audio_state_model passed
    fact avpro_audio_state_source_audit passed
  fi
  fact a37_series_audit passed
fi

if [[ "$REQUIRE_A38_SERIES" == 1 || "$REQUIRE_A39_SERIES" == 1 \
    || "$REQUIRE_A310_SERIES" == 1 || "$REQUIRE_A311_SERIES" == 1 \
    || "$REQUIRE_A312_SERIES" == 1 ]]; then
  python3 "$ROOT_DIR/tests/media-engine-pause-scrub/test_model.py" >/dev/null \
    || fail "A3.8 pause/scrub policy model failed"
  diagnostic_contract=absent
  [[ "$DIAGNOSTIC_SERIES" == 1 ]] && diagnostic_contract=present
  python3 "$ROOT_DIR/tests/media-engine-pause-scrub/audit.py" \
    "$WINE_TREE" --diagnostic-contract "$diagnostic_contract" >/dev/null \
    || fail "A3.8 pause/scrub source contract is incomplete"
  fact media_engine_pause_scrub_model passed
  fact media_engine_pause_scrub_source_audit passed
  fact a38_series_audit passed
fi

if [[ "$REQUIRE_A39_SERIES" == 1 || "$REQUIRE_A310_SERIES" == 1 \
    || "$REQUIRE_A311_SERIES" == 1 || "$REQUIRE_A312_SERIES" == 1 ]]; then
  python3 "$ROOT_DIR/tests/source-replacement-lifecycle/test_model.py" \
    --series "$PATCH_SERIES" >/dev/null \
    || fail "A3.9 source-replacement ownership model failed"
  python3 "$ROOT_DIR/tests/source-replacement-lifecycle/audit.py" "$WINE_TREE" >/dev/null \
    || fail "A3.9 source-replacement source contract is incomplete"
  fact source_replacement_lifecycle_model passed
  fact source_replacement_lifecycle_source_audit passed
  fact a39_series_audit passed
fi

if [[ "$REQUIRE_A310_SERIES" == 1 || "$REQUIRE_A311_SERIES" == 1 \
    || "$REQUIRE_A312_SERIES" == 1 ]]; then
  python3 "$ROOT_DIR/tests/source-replacement-lifecycle/audit_a310.py" "$WINE_TREE" >/dev/null \
    || fail "A3.10 replacement-start source contract is incomplete"
  fact a310_series_audit passed
fi

if [[ "$REQUIRE_A311_SERIES" == 1 || "$REQUIRE_A312_SERIES" == 1 ]]; then
  python3 "$ROOT_DIR/tests/source-replacement-lifecycle/audit_a311.py" "$WINE_TREE" >/dev/null \
    || fail "A3.11 replacement-clock source contract is incomplete"
  fact a311_series_audit passed
fi

if [[ "$REQUIRE_A312_SERIES" == 1 ]]; then
  python3 "$ROOT_DIR/tests/network-terminal-error/test_model.py" >/dev/null \
    || fail "A3.12 network terminal-error model failed"
  python3 "$ROOT_DIR/tests/network-terminal-error/audit.py" "$WINE_TREE" >/dev/null \
    || fail "A3.12 network terminal-error source contract is incomplete"
  fact network_terminal_error_model passed
  fact network_terminal_error_source_audit passed
  fact a312_series_audit passed
fi

fact structural_audit passed
