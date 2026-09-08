/* SPDX-License-Identifier: LGPL-2.1-or-later
 *
 * Copyright 2019 Jactry Zeng for CodeWeavers
 * Copyright 2026 Proton RTSP on GE contributors
 *
 * The IMFTransform pass-through effect in this test driver is adapted from
 * Wine's dlls/mfmediaengine/tests/mfmediaengine.c. This file is therefore
 * distributed under the GNU Lesser General Public License version 2.1 or,
 * at your option, any later version. See COPYING.LIB in this directory.
 */

#define COBJMACROS
#define INITGUID

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <math.h>

#include <initguid.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfmediaengine.h>
#include <mftransform.h>
#include <dxgiformat.h>

/*
 * A deliberately small IMFMediaEngineEx scenario runner.  It exercises the
 * public MediaEngine boundary used by Unity/AVPro; it does not replace or
 * emulate any Wine media backend.
 */

#define MAX_ACTIONS 8192
#define MAX_LINE 8192
#define MAX_TEXT 4096
#define MAX_LOOP_DEPTH 32
#define MAX_EVENT_CODE 2048
#define MAX_EXECUTED_ACTIONS 100000
#define MAX_AUDIO_SCAN_BYTES_PER_SAMPLE 4096

enum audio_payload_format
{
    AUDIO_PAYLOAD_UNKNOWN,
    AUDIO_PAYLOAD_PCM16,
    AUDIO_PAYLOAD_PCM32,
    AUDIO_PAYLOAD_FLOAT32,
};

enum exit_code
{
    EXIT_OK = 0,
    EXIT_SCENARIO_FAILED = 1,
    EXIT_USAGE_OR_PARSE = 2,
    EXIT_INITIALIZATION = 3,
    EXIT_ACTION_API = 4,
    EXIT_TIMEOUT = 5,
};

enum action_kind
{
    ACTION_LOAD,
    ACTION_REPLACE,
    ACTION_WAIT_EVENT,
    ACTION_WAIT_TIME,
    ACTION_WAIT_SEEK_SETTLED,
    ACTION_WAIT_MS,
    ACTION_PLAY,
    ACTION_PAUSE,
    ACTION_SEEK,
    ACTION_RATE,
    ACTION_AUTOPLAY,
    ACTION_MEDIA_LOOP,
    ACTION_SNAPSHOT,
    ACTION_LOOP,
    ACTION_ENDLOOP,
    ACTION_SHUTDOWN,
};

struct action
{
    enum action_kind kind;
    unsigned int line;
    char text[MAX_TEXT];
    double number;
    double tolerance;
    DWORD timeout_ms;
    DWORD event;
    unsigned int repeat;
    int pair;
};

struct scenario
{
    struct action actions[MAX_ACTIONS];
    unsigned int count;
};

struct audio_monitor;

struct engine_snapshot
{
    BOOL alive;
    double time;
    double duration;
    double rate;
    BOOL paused;
    BOOL seeking;
    BOOL ended;
    BOOL has_audio;
    BOOL has_video;
    USHORT network;
    USHORT ready;
    USHORT media_error;
    HRESULT media_error_hr;
    BOOL audio_monitor_enabled;
    unsigned long long audio_samples_total;
    unsigned long long audio_bytes_total;
    unsigned long long audio_samples_generation;
    unsigned long long audio_bytes_generation;
    BOOL audio_timestamp_valid;
    LONGLONG audio_last_sample_time;
    BOOL audio_duration_valid;
    LONGLONG audio_last_sample_duration;
    BOOL audio_monotonic_valid;
    ULONGLONG audio_last_monotonic_ms;
    BOOL audio_payload_observed;
    enum audio_payload_format audio_payload_format;
    UINT32 audio_channels;
    UINT32 audio_rate;
    UINT32 audio_bits;
    unsigned long long audio_payload_bytes_scanned_total;
    unsigned long long audio_nonzero_bytes_total;
    unsigned long long audio_nonzero_units_total;
    unsigned long long audio_nonzero_units_generation;
    double audio_peak_abs;
    BOOL audio_nonzero_monotonic_valid;
    ULONGLONG audio_last_nonzero_monotonic_ms;
};

struct notify
{
    IMFMediaEngineNotify IMFMediaEngineNotify_iface;
    LONG refcount;
    CRITICAL_SECTION lock;
    HANDLE changed_event;
    FILE *output;
    IMFMediaEngineEx *engine;
    ULONGLONG started_ms;
    unsigned long long sequence;
    unsigned int source_generation;
    unsigned int timeline_generation;
    unsigned long event_count[MAX_EVENT_CODE];
    unsigned long event_consumed[MAX_EVENT_CODE];
    BOOL output_failed;
    BOOL shutdown;
    BOOL result_emitted;
    struct audio_monitor *audio_monitor;
    unsigned long long audio_sample_base;
    unsigned long long audio_byte_base;
    unsigned long long audio_nonzero_unit_base;
    char driver_sha256[65];
    char scenario_sha256[65];
};

struct loop_frame
{
    unsigned int body_pc;
    unsigned int end_pc;
    unsigned int remaining;
};

struct event_name
{
    DWORD code;
    const char *name;
};

static const struct event_name event_names[] =
{
    {MF_MEDIA_ENGINE_EVENT_LOADSTART, "LOADSTART"},
    {MF_MEDIA_ENGINE_EVENT_PROGRESS, "PROGRESS"},
    {MF_MEDIA_ENGINE_EVENT_SUSPEND, "SUSPEND"},
    {MF_MEDIA_ENGINE_EVENT_ABORT, "ABORT"},
    {MF_MEDIA_ENGINE_EVENT_ERROR, "ERROR"},
    {MF_MEDIA_ENGINE_EVENT_EMPTIED, "EMPTIED"},
    {MF_MEDIA_ENGINE_EVENT_STALLED, "STALLED"},
    {MF_MEDIA_ENGINE_EVENT_PLAY, "PLAY"},
    {MF_MEDIA_ENGINE_EVENT_PAUSE, "PAUSE"},
    {MF_MEDIA_ENGINE_EVENT_LOADEDMETADATA, "LOADEDMETADATA"},
    {MF_MEDIA_ENGINE_EVENT_LOADEDDATA, "LOADEDDATA"},
    {MF_MEDIA_ENGINE_EVENT_WAITING, "WAITING"},
    {MF_MEDIA_ENGINE_EVENT_PLAYING, "PLAYING"},
    {MF_MEDIA_ENGINE_EVENT_CANPLAY, "CANPLAY"},
    {MF_MEDIA_ENGINE_EVENT_CANPLAYTHROUGH, "CANPLAYTHROUGH"},
    {MF_MEDIA_ENGINE_EVENT_SEEKING, "SEEKING"},
    {MF_MEDIA_ENGINE_EVENT_SEEKED, "SEEKED"},
    {MF_MEDIA_ENGINE_EVENT_TIMEUPDATE, "TIMEUPDATE"},
    {MF_MEDIA_ENGINE_EVENT_ENDED, "ENDED"},
    {MF_MEDIA_ENGINE_EVENT_RATECHANGE, "RATECHANGE"},
    {MF_MEDIA_ENGINE_EVENT_DURATIONCHANGE, "DURATIONCHANGE"},
    {MF_MEDIA_ENGINE_EVENT_VOLUMECHANGE, "VOLUMECHANGE"},
    {MF_MEDIA_ENGINE_EVENT_FORMATCHANGE, "FORMATCHANGE"},
    {MF_MEDIA_ENGINE_EVENT_PURGEQUEUEDEVENTS, "PURGEQUEUEDEVENTS"},
    {MF_MEDIA_ENGINE_EVENT_TIMELINE_MARKER, "TIMELINE_MARKER"},
    {MF_MEDIA_ENGINE_EVENT_BALANCECHANGE, "BALANCECHANGE"},
    {MF_MEDIA_ENGINE_EVENT_DOWNLOADCOMPLETE, "DOWNLOADCOMPLETE"},
    {MF_MEDIA_ENGINE_EVENT_BUFFERINGSTARTED, "BUFFERINGSTARTED"},
    {MF_MEDIA_ENGINE_EVENT_BUFFERINGENDED, "BUFFERINGENDED"},
    {MF_MEDIA_ENGINE_EVENT_FRAMESTEPCOMPLETED, "FRAMESTEPCOMPLETED"},
    {MF_MEDIA_ENGINE_EVENT_NOTIFYSTABLESTATE, "NOTIFYSTABLESTATE"},
    {MF_MEDIA_ENGINE_EVENT_FIRSTFRAMEREADY, "FIRSTFRAMEREADY"},
    {MF_MEDIA_ENGINE_EVENT_TRACKSCHANGE, "TRACKSCHANGE"},
    {MF_MEDIA_ENGINE_EVENT_OPMINFO, "OPMINFO"},
    {MF_MEDIA_ENGINE_EVENT_RESOURCELOST, "RESOURCELOST"},
    {MF_MEDIA_ENGINE_EVENT_DELAYLOADEVENT_CHANGED, "DELAYLOADEVENT_CHANGED"},
    {MF_MEDIA_ENGINE_EVENT_STREAMRENDERINGERROR, "STREAMRENDERINGERROR"},
    {MF_MEDIA_ENGINE_EVENT_SUPPORTEDRATES_CHANGED, "SUPPORTEDRATES_CHANGED"},
    {MF_MEDIA_ENGINE_EVENT_AUDIOENDPOINTCHANGE, "AUDIOENDPOINTCHANGE"},
};

static const char *network_name(USHORT state)
{
    switch (state)
    {
    case MF_MEDIA_ENGINE_NETWORK_EMPTY: return "EMPTY";
    case MF_MEDIA_ENGINE_NETWORK_IDLE: return "IDLE";
    case MF_MEDIA_ENGINE_NETWORK_LOADING: return "LOADING";
    case MF_MEDIA_ENGINE_NETWORK_NO_SOURCE: return "NO_SOURCE";
    default: return "UNKNOWN";
    }
}

static const char *ready_name(USHORT state)
{
    switch (state)
    {
    case MF_MEDIA_ENGINE_READY_HAVE_NOTHING: return "HAVE_NOTHING";
    case MF_MEDIA_ENGINE_READY_HAVE_METADATA: return "HAVE_METADATA";
    case MF_MEDIA_ENGINE_READY_HAVE_CURRENT_DATA: return "HAVE_CURRENT_DATA";
    case MF_MEDIA_ENGINE_READY_HAVE_FUTURE_DATA: return "HAVE_FUTURE_DATA";
    case MF_MEDIA_ENGINE_READY_HAVE_ENOUGH_DATA: return "HAVE_ENOUGH_DATA";
    default: return "UNKNOWN";
    }
}

static const char *event_name(DWORD event)
{
    unsigned int i;
    for (i = 0; i < sizeof(event_names) / sizeof(event_names[0]); ++i)
        if (event_names[i].code == event)
            return event_names[i].name;
    return "UNKNOWN";
}

static BOOL parse_event(const char *text, DWORD *event)
{
    char *end;
    unsigned long value;
    unsigned int i;

    for (i = 0; i < sizeof(event_names) / sizeof(event_names[0]); ++i)
    {
        if (!_stricmp(text, event_names[i].name))
        {
            *event = event_names[i].code;
            return TRUE;
        }
    }

    errno = 0;
    value = strtoul(text, &end, 10);
    if (!errno && *text && !*end && value < MAX_EVENT_CODE)
    {
        *event = value;
        return TRUE;
    }
    return FALSE;
}

static struct notify *notify_from_iface(IMFMediaEngineNotify *iface)
{
    return CONTAINING_RECORD(iface, struct notify, IMFMediaEngineNotify_iface);
}

static void json_string(FILE *file, const char *text)
{
    const unsigned char *p = (const unsigned char *)(text ? text : "");
    fputc('"', file);
    while (*p)
    {
        switch (*p)
        {
        case '"': fputs("\\\"", file); break;
        case '\\': fputs("\\\\", file); break;
        case '\b': fputs("\\b", file); break;
        case '\f': fputs("\\f", file); break;
        case '\n': fputs("\\n", file); break;
        case '\r': fputs("\\r", file); break;
        case '\t': fputs("\\t", file); break;
        default:
            if (*p < 0x20)
                fprintf(file, "\\u%04x", *p);
            else
                fputc(*p, file);
        }
        ++p;
    }
    fputc('"', file);
}

/* Optional pass-through audio effect derived from Wine's mfmediaengine tests.
 * Counting happens only when ProcessOutput hands a sample to the downstream
 * pipeline, so track discovery cannot masquerade as continuing delivery. */
struct audio_monitor
{
    IMFTransform IMFTransform_iface;
    LONG refcount;
    CRITICAL_SECTION lock;
    IMFMediaType *offered[6];
    unsigned int offered_count;
    IMFMediaType *input_type;
    IMFMediaType *output_type;
    IMFSample *sample;
    unsigned long long sample_count;
    unsigned long long byte_count;
    BOOL timestamp_valid;
    LONGLONG last_sample_time;
    BOOL duration_valid;
    LONGLONG last_sample_duration;
    BOOL monotonic_valid;
    ULONGLONG last_monotonic_ms;
    BOOL payload_observed;
    enum audio_payload_format payload_format;
    UINT32 channels;
    UINT32 rate;
    UINT32 bits;
    unsigned long long payload_bytes_scanned;
    unsigned long long nonzero_bytes;
    unsigned long long nonzero_units;
    double peak_abs;
    BOOL nonzero_monotonic_valid;
    ULONGLONG last_nonzero_monotonic_ms;
};

static struct audio_monitor *audio_monitor_from_iface(IMFTransform *iface)
{
    return CONTAINING_RECORD(iface, struct audio_monitor, IMFTransform_iface);
}

static HRESULT WINAPI audio_monitor_QueryInterface(IMFTransform *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IMFTransform))
    {
        *out = iface;
        IMFTransform_AddRef(iface);
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI audio_monitor_AddRef(IMFTransform *iface)
{
    return InterlockedIncrement(&audio_monitor_from_iface(iface)->refcount);
}

static ULONG WINAPI audio_monitor_Release(IMFTransform *iface)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    ULONG refcount = InterlockedDecrement(&monitor->refcount);
    if (!refcount)
    {
        unsigned int i;
        if (monitor->sample) IMFSample_Release(monitor->sample);
        if (monitor->input_type) IMFMediaType_Release(monitor->input_type);
        if (monitor->output_type) IMFMediaType_Release(monitor->output_type);
        for (i = 0; i < monitor->offered_count; ++i)
            IMFMediaType_Release(monitor->offered[i]);
        DeleteCriticalSection(&monitor->lock);
        free(monitor);
    }
    return refcount;
}

static HRESULT WINAPI audio_monitor_GetStreamLimits(IMFTransform *iface, DWORD *input_minimum,
        DWORD *input_maximum, DWORD *output_minimum, DWORD *output_maximum)
{
    *input_minimum = *input_maximum = 1;
    *output_minimum = *output_maximum = 1;
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetStreamCount(IMFTransform *iface, DWORD *inputs, DWORD *outputs)
{
    *inputs = *outputs = 1;
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetStreamIDs(IMFTransform *iface, DWORD input_size,
        DWORD *inputs, DWORD output_size, DWORD *outputs)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_GetInputStreamInfo(IMFTransform *iface, DWORD id,
        MFT_INPUT_STREAM_INFO *info)
{
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    memset(info, 0, sizeof(*info));
    info->dwFlags = MFT_INPUT_STREAM_WHOLE_SAMPLES | MFT_INPUT_STREAM_SINGLE_SAMPLE_PER_BUFFER;
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetOutputStreamInfo(IMFTransform *iface, DWORD id,
        MFT_OUTPUT_STREAM_INFO *info)
{
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    memset(info, 0, sizeof(*info));
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetAttributes(IMFTransform *iface, IMFAttributes **attributes)
{
    *attributes = NULL;
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_GetInputStreamAttributes(IMFTransform *iface, DWORD id,
        IMFAttributes **attributes)
{
    *attributes = NULL;
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_GetOutputStreamAttributes(IMFTransform *iface, DWORD id,
        IMFAttributes **attributes)
{
    *attributes = NULL;
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_DeleteInputStream(IMFTransform *iface, DWORD id)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_AddInputStreams(IMFTransform *iface, DWORD streams, DWORD *ids)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_GetInputAvailableType(IMFTransform *iface, DWORD id,
        DWORD index, IMFMediaType **type)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    if (index >= monitor->offered_count)
    {
        *type = NULL;
        return MF_E_NO_MORE_TYPES;
    }
    *type = monitor->offered[index];
    IMFMediaType_AddRef(*type);
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetOutputAvailableType(IMFTransform *iface, DWORD id,
        DWORD index, IMFMediaType **type)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    EnterCriticalSection(&monitor->lock);
    if (monitor->input_type)
    {
        if (index)
        {
            *type = NULL;
            LeaveCriticalSection(&monitor->lock);
            return MF_E_NO_MORE_TYPES;
        }
        *type = monitor->input_type;
        IMFMediaType_AddRef(*type);
        LeaveCriticalSection(&monitor->lock);
        return S_OK;
    }
    LeaveCriticalSection(&monitor->lock);
    return audio_monitor_GetInputAvailableType(iface, id, index, type);
}

static BOOL audio_monitor_type_supported(IMFMediaType *type)
{
    GUID major, subtype;
    if (!type || FAILED(IMFMediaType_GetGUID(type, &MF_MT_MAJOR_TYPE, &major)) ||
            FAILED(IMFMediaType_GetGUID(type, &MF_MT_SUBTYPE, &subtype)))
        return FALSE;
    return IsEqualGUID(&major, &MFMediaType_Audio) &&
            (IsEqualGUID(&subtype, &MFAudioFormat_PCM) ||
             IsEqualGUID(&subtype, &MFAudioFormat_Float));
}

static void audio_monitor_update_format_locked(struct audio_monitor *monitor,
        IMFMediaType *type)
{
    GUID subtype;
    monitor->payload_format = AUDIO_PAYLOAD_UNKNOWN;
    monitor->channels = monitor->rate = monitor->bits = 0;
    if (!type || FAILED(IMFMediaType_GetGUID(type, &MF_MT_SUBTYPE, &subtype))) return;
    IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_NUM_CHANNELS, &monitor->channels);
    IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_SAMPLES_PER_SECOND, &monitor->rate);
    IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_BITS_PER_SAMPLE, &monitor->bits);
    if (IsEqualGUID(&subtype, &MFAudioFormat_PCM) && monitor->bits == 16)
        monitor->payload_format = AUDIO_PAYLOAD_PCM16;
    else if (IsEqualGUID(&subtype, &MFAudioFormat_PCM) && monitor->bits == 32)
        monitor->payload_format = AUDIO_PAYLOAD_PCM32;
    else if (IsEqualGUID(&subtype, &MFAudioFormat_Float) && monitor->bits == 32)
        monitor->payload_format = AUDIO_PAYLOAD_FLOAT32;
}

static HRESULT audio_monitor_set_type(struct audio_monitor *monitor, IMFMediaType **slot,
        IMFMediaType *type, DWORD flags)
{
    if (type && !audio_monitor_type_supported(type))
        return MF_E_INVALIDMEDIATYPE;
    if (flags & ~MFT_SET_TYPE_TEST_ONLY)
        return E_INVALIDARG;
    if (flags & MFT_SET_TYPE_TEST_ONLY)
        return S_OK;
    EnterCriticalSection(&monitor->lock);
    if (*slot) IMFMediaType_Release(*slot);
    *slot = type;
    if (*slot) IMFMediaType_AddRef(*slot);
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

static BOOL audio_monitor_types_equal(IMFMediaType *left, IMFMediaType *right)
{
    static const GUID *attributes[] =
    {
        &MF_MT_AUDIO_NUM_CHANNELS,
        &MF_MT_AUDIO_SAMPLES_PER_SECOND,
        &MF_MT_AUDIO_BITS_PER_SAMPLE,
        &MF_MT_AUDIO_BLOCK_ALIGNMENT,
        &MF_MT_AUDIO_AVG_BYTES_PER_SECOND,
    };
    GUID left_subtype, right_subtype;
    UINT32 left_value, right_value;
    unsigned int i;
    if (!left || !right ||
            FAILED(IMFMediaType_GetGUID(left, &MF_MT_SUBTYPE, &left_subtype)) ||
            FAILED(IMFMediaType_GetGUID(right, &MF_MT_SUBTYPE, &right_subtype)) ||
            !IsEqualGUID(&left_subtype, &right_subtype))
        return FALSE;
    for (i = 0; i < sizeof(attributes) / sizeof(attributes[0]); ++i)
    {
        if (FAILED(IMFMediaType_GetUINT32(left, attributes[i], &left_value)) ||
                FAILED(IMFMediaType_GetUINT32(right, attributes[i], &right_value)) ||
                left_value != right_value)
            return FALSE;
    }
    return TRUE;
}

static HRESULT WINAPI audio_monitor_SetInputType(IMFTransform *iface, DWORD id,
        IMFMediaType *type, DWORD flags)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    if (type && monitor->output_type &&
            !audio_monitor_types_equal(type, monitor->output_type))
        return MF_E_INVALIDMEDIATYPE;
    return audio_monitor_set_type(monitor, &monitor->input_type, type, flags);
}

static HRESULT WINAPI audio_monitor_SetOutputType(IMFTransform *iface, DWORD id,
        IMFMediaType *type, DWORD flags)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    HRESULT hr;
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    if (type && monitor->input_type &&
            !audio_monitor_types_equal(monitor->input_type, type))
        return MF_E_INVALIDMEDIATYPE;
    hr = audio_monitor_set_type(monitor, &monitor->output_type, type, flags);
    if (SUCCEEDED(hr) && !(flags & MFT_SET_TYPE_TEST_ONLY))
    {
        EnterCriticalSection(&monitor->lock);
        audio_monitor_update_format_locked(monitor, type);
        LeaveCriticalSection(&monitor->lock);
    }
    return hr;
}

static HRESULT audio_monitor_get_current_type(struct audio_monitor *monitor,
        IMFMediaType *current, IMFMediaType **type)
{
    EnterCriticalSection(&monitor->lock);
    if (!current)
    {
        *type = NULL;
        LeaveCriticalSection(&monitor->lock);
        return MF_E_TRANSFORM_TYPE_NOT_SET;
    }
    *type = current;
    IMFMediaType_AddRef(*type);
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetInputCurrentType(IMFTransform *iface, DWORD id,
        IMFMediaType **type)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    return audio_monitor_get_current_type(monitor, monitor->input_type, type);
}

static HRESULT WINAPI audio_monitor_GetOutputCurrentType(IMFTransform *iface, DWORD id,
        IMFMediaType **type)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    return audio_monitor_get_current_type(monitor, monitor->output_type, type);
}

static HRESULT WINAPI audio_monitor_GetInputStatus(IMFTransform *iface, DWORD id, DWORD *flags)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    EnterCriticalSection(&monitor->lock);
    *flags = monitor->sample ? 0 : MFT_INPUT_STATUS_ACCEPT_DATA;
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

static HRESULT WINAPI audio_monitor_GetOutputStatus(IMFTransform *iface, DWORD *flags)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    EnterCriticalSection(&monitor->lock);
    *flags = monitor->sample ? MFT_OUTPUT_STATUS_SAMPLE_READY : 0;
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

static HRESULT WINAPI audio_monitor_SetOutputBounds(IMFTransform *iface, LONGLONG lower,
        LONGLONG upper)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_ProcessEvent(IMFTransform *iface, DWORD id,
        IMFMediaEvent *event)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI audio_monitor_ProcessMessage(IMFTransform *iface,
        MFT_MESSAGE_TYPE message, ULONG_PTR param)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (message == MFT_MESSAGE_COMMAND_FLUSH)
    {
        EnterCriticalSection(&monitor->lock);
        if (monitor->sample)
        {
            IMFSample_Release(monitor->sample);
            monitor->sample = NULL;
        }
        LeaveCriticalSection(&monitor->lock);
    }
    return S_OK;
}

static HRESULT WINAPI audio_monitor_ProcessInput(IMFTransform *iface, DWORD id,
        IMFSample *sample, DWORD flags)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    if (id) return MF_E_INVALIDSTREAMNUMBER;
    if (!sample || flags) return E_INVALIDARG;
    EnterCriticalSection(&monitor->lock);
    if (monitor->sample)
    {
        LeaveCriticalSection(&monitor->lock);
        return MF_E_NOTACCEPTING;
    }
    monitor->sample = sample;
    IMFSample_AddRef(sample);
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

/* Caller owns monitor->lock. Inspection is capped so the diagnostic effect
 * cannot scan an unbounded compressed or malformed sample. */
static void audio_monitor_scan_sample_locked(struct audio_monitor *monitor,
        IMFSample *sample)
{
    DWORD buffer_count = 0, index;
    size_t remaining = MAX_AUDIO_SCAN_BYTES_PER_SAMPLE;
    unsigned long long units_before = monitor->nonzero_units;

    if (FAILED(IMFSample_GetBufferCount(sample, &buffer_count))) return;
    for (index = 0; index < buffer_count && remaining; ++index)
    {
        IMFMediaBuffer *buffer = NULL;
        BYTE *data = NULL;
        DWORD maximum = 0, current = 0;
        size_t length, i;
        if (FAILED(IMFSample_GetBufferByIndex(sample, index, &buffer))) continue;
        if (FAILED(IMFMediaBuffer_Lock(buffer, &data, &maximum, &current)))
        {
            IMFMediaBuffer_Release(buffer);
            continue;
        }
        length = current < remaining ? current : remaining;
        if (length)
        {
            monitor->payload_observed = TRUE;
            monitor->payload_bytes_scanned += length;
            for (i = 0; i < length; ++i)
                if (data[i]) ++monitor->nonzero_bytes;

            if (monitor->payload_format == AUDIO_PAYLOAD_PCM16)
            {
                for (i = 0; i + sizeof(INT16) <= length; i += sizeof(INT16))
                {
                    INT16 value;
                    double magnitude;
                    memcpy(&value, data + i, sizeof(value));
                    magnitude = value < 0 ? -(double)value : (double)value;
                    if (magnitude > 0.0) ++monitor->nonzero_units;
                    if (magnitude > monitor->peak_abs) monitor->peak_abs = magnitude;
                }
            }
            else if (monitor->payload_format == AUDIO_PAYLOAD_PCM32)
            {
                for (i = 0; i + sizeof(INT32) <= length; i += sizeof(INT32))
                {
                    INT32 value;
                    double magnitude;
                    memcpy(&value, data + i, sizeof(value));
                    magnitude = value < 0 ? -(double)value : (double)value;
                    if (magnitude > 0.0) ++monitor->nonzero_units;
                    if (magnitude > monitor->peak_abs) monitor->peak_abs = magnitude;
                }
            }
            else if (monitor->payload_format == AUDIO_PAYLOAD_FLOAT32)
            {
                for (i = 0; i + sizeof(float) <= length; i += sizeof(float))
                {
                    float value;
                    double magnitude;
                    memcpy(&value, data + i, sizeof(value));
                    if (!isfinite(value)) continue;
                    magnitude = value < 0.0f ? -(double)value : (double)value;
                    if (magnitude > 0.0) ++monitor->nonzero_units;
                    if (magnitude > monitor->peak_abs) monitor->peak_abs = magnitude;
                }
            }
        }
        IMFMediaBuffer_Unlock(buffer);
        IMFMediaBuffer_Release(buffer);
        remaining -= length;
    }
    if (monitor->nonzero_units > units_before)
    {
        monitor->nonzero_monotonic_valid = TRUE;
        monitor->last_nonzero_monotonic_ms = GetTickCount64();
    }
}

static HRESULT WINAPI audio_monitor_ProcessOutput(IMFTransform *iface, DWORD flags,
        DWORD count, MFT_OUTPUT_DATA_BUFFER *data, DWORD *status)
{
    struct audio_monitor *monitor = audio_monitor_from_iface(iface);
    IMFSample *sample;
    DWORD bytes = 0;
    LONGLONG value;
    if (flags || !count || !data || !status) return E_INVALIDARG;
    EnterCriticalSection(&monitor->lock);
    if (!monitor->sample)
    {
        LeaveCriticalSection(&monitor->lock);
        return MF_E_TRANSFORM_NEED_MORE_INPUT;
    }
    sample = monitor->sample;
    monitor->sample = NULL;
    audio_monitor_scan_sample_locked(monitor, sample);
    if (SUCCEEDED(IMFSample_GetTotalLength(sample, &bytes)))
        monitor->byte_count += bytes;
    ++monitor->sample_count;
    if (SUCCEEDED(IMFSample_GetSampleTime(sample, &value)))
    {
        monitor->timestamp_valid = TRUE;
        monitor->last_sample_time = value;
    }
    if (SUCCEEDED(IMFSample_GetSampleDuration(sample, &value)))
    {
        monitor->duration_valid = TRUE;
        monitor->last_sample_duration = value;
    }
    monitor->monotonic_valid = TRUE;
    monitor->last_monotonic_ms = GetTickCount64();
    data[0].pSample = sample;
    data[0].dwStatus = 0;
    *status = 0;
    LeaveCriticalSection(&monitor->lock);
    return S_OK;
}

static IMFTransformVtbl audio_monitor_vtbl =
{
    audio_monitor_QueryInterface,
    audio_monitor_AddRef,
    audio_monitor_Release,
    audio_monitor_GetStreamLimits,
    audio_monitor_GetStreamCount,
    audio_monitor_GetStreamIDs,
    audio_monitor_GetInputStreamInfo,
    audio_monitor_GetOutputStreamInfo,
    audio_monitor_GetAttributes,
    audio_monitor_GetInputStreamAttributes,
    audio_monitor_GetOutputStreamAttributes,
    audio_monitor_DeleteInputStream,
    audio_monitor_AddInputStreams,
    audio_monitor_GetInputAvailableType,
    audio_monitor_GetOutputAvailableType,
    audio_monitor_SetInputType,
    audio_monitor_SetOutputType,
    audio_monitor_GetInputCurrentType,
    audio_monitor_GetOutputCurrentType,
    audio_monitor_GetInputStatus,
    audio_monitor_GetOutputStatus,
    audio_monitor_SetOutputBounds,
    audio_monitor_ProcessEvent,
    audio_monitor_ProcessMessage,
    audio_monitor_ProcessInput,
    audio_monitor_ProcessOutput,
};

static HRESULT create_audio_type(REFGUID subtype, UINT32 channels, UINT32 rate,
        UINT32 bits, IMFMediaType **out)
{
    IMFMediaType *type;
    UINT32 block = channels * bits / 8;
    HRESULT hr;
    *out = NULL;
    if (FAILED(hr = MFCreateMediaType(&type))) return hr;
    if (FAILED(hr = IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, &MFMediaType_Audio)) ||
            FAILED(hr = IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, subtype)) ||
            FAILED(hr = IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_NUM_CHANNELS, channels)) ||
            FAILED(hr = IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_SAMPLES_PER_SECOND, rate)) ||
            FAILED(hr = IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_BITS_PER_SAMPLE, bits)) ||
            FAILED(hr = IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_BLOCK_ALIGNMENT, block)) ||
            FAILED(hr = IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_AVG_BYTES_PER_SECOND, block * rate)))
    {
        IMFMediaType_Release(type);
        return hr;
    }
    *out = type;
    return S_OK;
}

static HRESULT create_audio_monitor(struct audio_monitor **out)
{
    static const struct
    {
        const GUID *subtype;
        UINT32 rate, bits;
    } formats[] =
    {
        {&MFAudioFormat_PCM, 48000, 16},
        {&MFAudioFormat_PCM, 44100, 16},
        {&MFAudioFormat_PCM, 48000, 32},
        {&MFAudioFormat_PCM, 44100, 32},
        {&MFAudioFormat_Float, 48000, 32},
        {&MFAudioFormat_Float, 44100, 32},
    };
    struct audio_monitor *monitor;
    unsigned int i;
    HRESULT hr;

    *out = NULL;
    if (!(monitor = calloc(1, sizeof(*monitor)))) return E_OUTOFMEMORY;
    monitor->IMFTransform_iface.lpVtbl = &audio_monitor_vtbl;
    monitor->refcount = 1;
    InitializeCriticalSection(&monitor->lock);
    for (i = 0; i < sizeof(formats) / sizeof(formats[0]); ++i)
    {
        if (FAILED(hr = create_audio_type(formats[i].subtype, 2, formats[i].rate,
                formats[i].bits, &monitor->offered[monitor->offered_count])))
        {
            IMFTransform_Release(&monitor->IMFTransform_iface);
            return hr;
        }
        ++monitor->offered_count;
    }
    *out = monitor;
    return S_OK;
}

static void audio_monitor_get_counts(struct audio_monitor *monitor,
        unsigned long long *samples, unsigned long long *bytes,
        unsigned long long *nonzero_units)
{
    *samples = *bytes = *nonzero_units = 0;
    if (!monitor) return;
    EnterCriticalSection(&monitor->lock);
    *samples = monitor->sample_count;
    *bytes = monitor->byte_count;
    *nonzero_units = monitor->nonzero_units;
    LeaveCriticalSection(&monitor->lock);
}

static void capture_snapshot(IMFMediaEngineEx *engine, struct audio_monitor *monitor,
        unsigned long long audio_sample_base, unsigned long long audio_byte_base,
        unsigned long long audio_nonzero_unit_base,
        ULONGLONG started_ms, struct engine_snapshot *snapshot)
{
    IMFMediaError *error = NULL;

    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->duration = NAN;
    snapshot->time = NAN;
    snapshot->rate = NAN;
    if (monitor)
    {
        snapshot->audio_monitor_enabled = TRUE;
        EnterCriticalSection(&monitor->lock);
        snapshot->audio_samples_total = monitor->sample_count;
        snapshot->audio_bytes_total = monitor->byte_count;
        snapshot->audio_samples_generation = monitor->sample_count >= audio_sample_base ?
                monitor->sample_count - audio_sample_base : 0;
        snapshot->audio_bytes_generation = monitor->byte_count >= audio_byte_base ?
                monitor->byte_count - audio_byte_base : 0;
        snapshot->audio_timestamp_valid = monitor->timestamp_valid;
        snapshot->audio_last_sample_time = monitor->last_sample_time;
        snapshot->audio_duration_valid = monitor->duration_valid;
        snapshot->audio_last_sample_duration = monitor->last_sample_duration;
        snapshot->audio_monotonic_valid = monitor->monotonic_valid;
        snapshot->audio_last_monotonic_ms = monitor->last_monotonic_ms >= started_ms ?
                monitor->last_monotonic_ms - started_ms : 0;
        snapshot->audio_payload_observed = monitor->payload_observed;
        snapshot->audio_payload_format = monitor->payload_format;
        snapshot->audio_channels = monitor->channels;
        snapshot->audio_rate = monitor->rate;
        snapshot->audio_bits = monitor->bits;
        snapshot->audio_payload_bytes_scanned_total = monitor->payload_bytes_scanned;
        snapshot->audio_nonzero_bytes_total = monitor->nonzero_bytes;
        snapshot->audio_nonzero_units_total = monitor->nonzero_units;
        snapshot->audio_nonzero_units_generation = monitor->nonzero_units >= audio_nonzero_unit_base ?
                monitor->nonzero_units - audio_nonzero_unit_base : 0;
        snapshot->audio_peak_abs = monitor->peak_abs;
        snapshot->audio_nonzero_monotonic_valid = monitor->nonzero_monotonic_valid;
        snapshot->audio_last_nonzero_monotonic_ms = monitor->last_nonzero_monotonic_ms >= started_ms ?
                monitor->last_nonzero_monotonic_ms - started_ms : 0;
        LeaveCriticalSection(&monitor->lock);
    }
    if (!engine)
        return;

    snapshot->alive = TRUE;
    snapshot->time = IMFMediaEngineEx_GetCurrentTime(engine);
    snapshot->duration = IMFMediaEngineEx_GetDuration(engine);
    snapshot->rate = IMFMediaEngineEx_GetPlaybackRate(engine);
    snapshot->paused = IMFMediaEngineEx_IsPaused(engine);
    snapshot->seeking = IMFMediaEngineEx_IsSeeking(engine);
    snapshot->ended = IMFMediaEngineEx_IsEnded(engine);
    snapshot->has_audio = IMFMediaEngineEx_HasAudio(engine);
    snapshot->has_video = IMFMediaEngineEx_HasVideo(engine);
    snapshot->network = IMFMediaEngineEx_GetNetworkState(engine);
    snapshot->ready = IMFMediaEngineEx_GetReadyState(engine);
    if (SUCCEEDED(IMFMediaEngineEx_GetError(engine, &error)) && error)
    {
        snapshot->media_error = IMFMediaError_GetErrorCode(error);
        snapshot->media_error_hr = IMFMediaError_GetExtendedErrorCode(error);
        IMFMediaError_Release(error);
    }
}

static void json_number_or_null(FILE *file, double value)
{
    if (isfinite(value))
        fprintf(file, "%.9f", value);
    else
        fputs("null", file);
}

/* Caller owns notify->lock. */
static void write_state_locked(struct notify *notify, const struct engine_snapshot *snapshot,
        unsigned int source_generation, unsigned int timeline_generation)
{
    fprintf(notify->output,
            ",\"source_generation\":%u,\"timeline_generation\":%u"
            ",\"engine_alive\":%s,\"time\":" ,
            source_generation, timeline_generation,
            snapshot->alive ? "true" : "false");
    json_number_or_null(notify->output, snapshot->time);
    fputs(",\"duration\":", notify->output);
    json_number_or_null(notify->output, snapshot->duration);
    fputs(",\"rate\":", notify->output);
    json_number_or_null(notify->output, snapshot->rate);
    fprintf(notify->output,
            ",\"paused\":%s,\"seeking\":%s,\"ended\":%s"
            ",\"has_audio\":%s,\"has_video\":%s"
            ",\"network\":%u,\"network_name\":",
            snapshot->paused ? "true" : "false",
            snapshot->seeking ? "true" : "false",
            snapshot->ended ? "true" : "false",
            snapshot->has_audio ? "true" : "false",
            snapshot->has_video ? "true" : "false",
            snapshot->network);
    json_string(notify->output, network_name(snapshot->network));
    fprintf(notify->output, ",\"ready\":%u,\"ready_name\":", snapshot->ready);
    json_string(notify->output, ready_name(snapshot->ready));
    fprintf(notify->output, ",\"media_error\":%u,\"media_error_hr\":\"0x%08lx\"",
            snapshot->media_error, (unsigned long)snapshot->media_error_hr);
    fprintf(notify->output,
            ",\"audio_monitor_enabled\":%s"
            ",\"audio_samples_total\":%llu,\"audio_bytes_total\":%llu"
            ",\"audio_samples_generation\":%llu,\"audio_bytes_generation\":%llu"
            ",\"audio_last_sample_time_100ns\":",
            snapshot->audio_monitor_enabled ? "true" : "false",
            snapshot->audio_samples_total, snapshot->audio_bytes_total,
            snapshot->audio_samples_generation, snapshot->audio_bytes_generation);
    if (snapshot->audio_timestamp_valid)
        fprintf(notify->output, "%lld", (long long)snapshot->audio_last_sample_time);
    else
        fputs("null", notify->output);
    fputs(",\"audio_last_sample_duration_100ns\":", notify->output);
    if (snapshot->audio_duration_valid)
        fprintf(notify->output, "%lld", (long long)snapshot->audio_last_sample_duration);
    else
        fputs("null", notify->output);
    fputs(",\"audio_last_monotonic_ms\":", notify->output);
    if (snapshot->audio_monotonic_valid)
        fprintf(notify->output, "%llu", snapshot->audio_last_monotonic_ms);
    else
        fputs("null", notify->output);
    fprintf(notify->output,
            ",\"audio_payload_observed\":%s,\"audio_payload_format\":",
            snapshot->audio_payload_observed ? "true" : "false");
    switch (snapshot->audio_payload_format)
    {
    case AUDIO_PAYLOAD_PCM16: json_string(notify->output, "pcm16"); break;
    case AUDIO_PAYLOAD_PCM32: json_string(notify->output, "pcm32"); break;
    case AUDIO_PAYLOAD_FLOAT32: json_string(notify->output, "float32"); break;
    default: json_string(notify->output, "unknown"); break;
    }
    fprintf(notify->output,
            ",\"audio_channels\":%u,\"audio_rate\":%u,\"audio_bits\":%u"
            ",\"audio_payload_bytes_scanned_total\":%llu"
            ",\"audio_nonzero_bytes_total\":%llu"
            ",\"audio_nonzero_units_total\":%llu"
            ",\"audio_nonzero_units_generation\":%llu"
            ",\"audio_peak_abs\":",
            snapshot->audio_channels, snapshot->audio_rate, snapshot->audio_bits,
            snapshot->audio_payload_bytes_scanned_total,
            snapshot->audio_nonzero_bytes_total,
            snapshot->audio_nonzero_units_total,
            snapshot->audio_nonzero_units_generation);
    json_number_or_null(notify->output, snapshot->audio_peak_abs);
    fputs(",\"audio_last_nonzero_monotonic_ms\":", notify->output);
    if (snapshot->audio_nonzero_monotonic_valid)
        fprintf(notify->output, "%llu", snapshot->audio_last_nonzero_monotonic_ms);
    else
        fputs("null", notify->output);
    fputs(",\"driver_sha256\":", notify->output);
    if (notify->driver_sha256[0]) json_string(notify->output, notify->driver_sha256);
    else fputs("null", notify->output);
    fputs(",\"scenario_sha256\":", notify->output);
    if (notify->scenario_sha256[0]) json_string(notify->output, notify->scenario_sha256);
    else fputs("null", notify->output);
}

/* Caller owns notify->lock. */
static void finish_record_locked(struct notify *notify)
{
    fputs("}\n", notify->output);
    if (fflush(notify->output) || ferror(notify->output))
        notify->output_failed = TRUE;
}

static void write_event_locked(struct notify *notify, DWORD event, DWORD_PTR param1,
        DWORD param2, const struct engine_snapshot *snapshot,
        unsigned int source_generation, unsigned int timeline_generation,
        BOOL generation_current)
{
    fprintf(notify->output,
            "{\"type\":\"event\",\"seq\":%llu,\"monotonic_ms\":%llu"
            ",\"monotonic_origin_ms\":%llu,\"event\":",
            ++notify->sequence,
            (unsigned long long)(GetTickCount64() - notify->started_ms),
            (unsigned long long)notify->started_ms);
    json_string(notify->output, event_name(event));
    fprintf(notify->output,
            ",\"event_id\":%lu,\"param1\":\"0x%llx\",\"param2\":\"0x%08lx\""
            ",\"generation_current\":%s",
            (unsigned long)event, (unsigned long long)param1, (unsigned long)param2,
            generation_current ? "true" : "false");
    write_state_locked(notify, snapshot, source_generation, timeline_generation);
    finish_record_locked(notify);
}

static void emit_snapshot(struct notify *notify, const char *action, const char *label,
        const char *status, HRESULT hr, double action_value)
{
    struct engine_snapshot snapshot;
    IMFMediaEngineEx *engine;
    struct audio_monitor *audio_monitor;
    unsigned int source_generation, timeline_generation;
    unsigned long long audio_sample_base, audio_byte_base, audio_nonzero_unit_base;

    EnterCriticalSection(&notify->lock);
    engine = notify->engine;
    if (engine) IMFMediaEngineEx_AddRef(engine);
    audio_monitor = notify->audio_monitor;
    if (audio_monitor) IMFTransform_AddRef(&audio_monitor->IMFTransform_iface);
    audio_sample_base = notify->audio_sample_base;
    audio_byte_base = notify->audio_byte_base;
    audio_nonzero_unit_base = notify->audio_nonzero_unit_base;
    source_generation = notify->source_generation;
    timeline_generation = notify->timeline_generation;
    LeaveCriticalSection(&notify->lock);

    capture_snapshot(engine, audio_monitor, audio_sample_base, audio_byte_base,
            audio_nonzero_unit_base,
            notify->started_ms, &snapshot);

    EnterCriticalSection(&notify->lock);
    fprintf(notify->output,
            "{\"type\":\"snapshot\",\"seq\":%llu,\"monotonic_ms\":%llu"
            ",\"monotonic_origin_ms\":%llu,\"event\":null,\"action\":",
            ++notify->sequence,
            (unsigned long long)(GetTickCount64() - notify->started_ms),
            (unsigned long long)notify->started_ms);
    json_string(notify->output, action);
    fputs(",\"label\":", notify->output);
    if (label)
        json_string(notify->output, label);
    else
        fputs("null", notify->output);
    fputs(",\"status\":", notify->output);
    json_string(notify->output, status);
    fprintf(notify->output, ",\"hr\":\"0x%08lx\"", (unsigned long)hr);
    fputs(",\"action_value\":", notify->output);
    json_number_or_null(notify->output, action_value);
    write_state_locked(notify, &snapshot, source_generation, timeline_generation);
    finish_record_locked(notify);
    LeaveCriticalSection(&notify->lock);
    if (engine) IMFMediaEngineEx_Release(engine);
    if (audio_monitor) IMFTransform_Release(&audio_monitor->IMFTransform_iface);
}

static void emit_result(struct notify *notify, const char *status, int code,
        unsigned int actions_completed, const char *message)
{
    struct engine_snapshot snapshot;
    IMFMediaEngineEx *engine;
    struct audio_monitor *audio_monitor;
    unsigned int source_generation, timeline_generation;
    unsigned long long audio_sample_base, audio_byte_base, audio_nonzero_unit_base;

    EnterCriticalSection(&notify->lock);
    notify->result_emitted = TRUE;
    engine = notify->engine;
    if (engine) IMFMediaEngineEx_AddRef(engine);
    audio_monitor = notify->audio_monitor;
    if (audio_monitor) IMFTransform_AddRef(&audio_monitor->IMFTransform_iface);
    audio_sample_base = notify->audio_sample_base;
    audio_byte_base = notify->audio_byte_base;
    audio_nonzero_unit_base = notify->audio_nonzero_unit_base;
    source_generation = notify->source_generation;
    timeline_generation = notify->timeline_generation;
    LeaveCriticalSection(&notify->lock);

    capture_snapshot(engine, audio_monitor, audio_sample_base, audio_byte_base,
            audio_nonzero_unit_base,
            notify->started_ms, &snapshot);

    EnterCriticalSection(&notify->lock);
    fprintf(notify->output,
            "{\"type\":\"result\",\"seq\":%llu,\"monotonic_ms\":%llu"
            ",\"monotonic_origin_ms\":%llu,\"event\":null,\"status\":",
            ++notify->sequence,
            (unsigned long long)(GetTickCount64() - notify->started_ms),
            (unsigned long long)notify->started_ms);
    json_string(notify->output, status);
    fprintf(notify->output, ",\"exit_code\":%d,\"actions_completed\":%u,\"message\":",
            code, actions_completed);
    json_string(notify->output, message);
    write_state_locked(notify, &snapshot, source_generation, timeline_generation);
    finish_record_locked(notify);
    LeaveCriticalSection(&notify->lock);
    if (engine) IMFMediaEngineEx_Release(engine);
    if (audio_monitor) IMFTransform_Release(&audio_monitor->IMFTransform_iface);
}

static HRESULT WINAPI notify_QueryInterface(IMFMediaEngineNotify *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IMFMediaEngineNotify))
    {
        *out = iface;
        IMFMediaEngineNotify_AddRef(iface);
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI notify_AddRef(IMFMediaEngineNotify *iface)
{
    return InterlockedIncrement(&notify_from_iface(iface)->refcount);
}

static ULONG WINAPI notify_Release(IMFMediaEngineNotify *iface)
{
    struct notify *notify = notify_from_iface(iface);
    ULONG refcount = InterlockedDecrement(&notify->refcount);
    if (!refcount)
    {
        CloseHandle(notify->changed_event);
        DeleteCriticalSection(&notify->lock);
        free(notify);
    }
    return refcount;
}

static HRESULT WINAPI notify_EventNotify(IMFMediaEngineNotify *iface, DWORD event,
        DWORD_PTR param1, DWORD param2)
{
    struct notify *notify = notify_from_iface(iface);
    struct engine_snapshot snapshot;
    IMFMediaEngineEx *engine;
    struct audio_monitor *audio_monitor;
    unsigned int source_generation, timeline_generation;
    unsigned long long audio_sample_base, audio_byte_base, audio_nonzero_unit_base;
    BOOL generation_current;

    EnterCriticalSection(&notify->lock);
    if (notify->result_emitted)
    {
        LeaveCriticalSection(&notify->lock);
        return S_OK;
    }
    engine = notify->engine;
    if (engine) IMFMediaEngineEx_AddRef(engine);
    audio_monitor = notify->audio_monitor;
    if (audio_monitor) IMFTransform_AddRef(&audio_monitor->IMFTransform_iface);
    audio_sample_base = notify->audio_sample_base;
    audio_byte_base = notify->audio_byte_base;
    audio_nonzero_unit_base = notify->audio_nonzero_unit_base;
    source_generation = notify->source_generation;
    timeline_generation = notify->timeline_generation;
    LeaveCriticalSection(&notify->lock);

    capture_snapshot(engine, audio_monitor, audio_sample_base, audio_byte_base,
            audio_nonzero_unit_base,
            notify->started_ms, &snapshot);

    EnterCriticalSection(&notify->lock);
    if (notify->result_emitted)
    {
        LeaveCriticalSection(&notify->lock);
        if (engine) IMFMediaEngineEx_Release(engine);
        if (audio_monitor) IMFTransform_Release(&audio_monitor->IMFTransform_iface);
        return S_OK;
    }
    generation_current = source_generation == notify->source_generation &&
            timeline_generation == notify->timeline_generation;
    if (generation_current && event < MAX_EVENT_CODE)
        ++notify->event_count[event];
    write_event_locked(notify, event, param1, param2, &snapshot,
            source_generation, timeline_generation, generation_current);
    SetEvent(notify->changed_event);
    LeaveCriticalSection(&notify->lock);
    if (engine) IMFMediaEngineEx_Release(engine);
    if (audio_monitor) IMFTransform_Release(&audio_monitor->IMFTransform_iface);
    return S_OK;
}

static IMFMediaEngineNotifyVtbl notify_vtbl =
{
    notify_QueryInterface,
    notify_AddRef,
    notify_Release,
    notify_EventNotify,
};

static char *trim(char *text)
{
    char *end;
    while (isspace((unsigned char)*text)) ++text;
    end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = 0;
    return text;
}

static BOOL next_word(const char **cursor, char *out, size_t out_size)
{
    const char *p = *cursor;
    size_t length = 0;
    BOOL quoted = FALSE, closed = FALSE;

    while (isspace((unsigned char)*p)) ++p;
    if (!*p)
    {
        *cursor = p;
        return FALSE;
    }
    if (*p == '"')
    {
        quoted = TRUE;
        ++p;
    }
    while (*p)
    {
        if (quoted)
        {
            if (*p == '"')
            {
                ++p;
                closed = TRUE;
                break;
            }
            if (*p == '\\' && (p[1] == '\\' || p[1] == '"'))
                ++p;
        }
        else if (isspace((unsigned char)*p))
            break;

        if (length + 1 >= out_size)
            return FALSE;
        out[length++] = *p++;
    }
    if (quoted && !closed)
        return FALSE;
    out[length] = 0;
    while (isspace((unsigned char)*p)) ++p;
    *cursor = p;
    return TRUE;
}

static BOOL no_more_words(const char *cursor)
{
    while (isspace((unsigned char)*cursor)) ++cursor;
    return !*cursor || *cursor == '#';
}

static BOOL parse_uint(const char *text, unsigned long maximum, unsigned int *value)
{
    char *end;
    unsigned long parsed;
    errno = 0;
    parsed = strtoul(text, &end, 10);
    if (errno || !*text || *end || parsed > maximum)
        return FALSE;
    *value = (unsigned int)parsed;
    return TRUE;
}

static BOOL parse_double_value(const char *text, double minimum, double maximum,
        double *value)
{
    char *end;
    double parsed;
    errno = 0;
    parsed = strtod(text, &end);
    if (errno || !*text || *end || !isfinite(parsed) || parsed < minimum || parsed > maximum)
        return FALSE;
    *value = parsed;
    return TRUE;
}

static int parse_script(const WCHAR *path, struct scenario *scenario, char *error,
        size_t error_size)
{
    FILE *file;
    char line[MAX_LINE];
    unsigned int line_number = 0;
    int loop_stack[MAX_LOOP_DEPTH];
    unsigned int loop_depth = 0;
    unsigned int shutdown_count = 0;

    memset(scenario, 0, sizeof(*scenario));
    if (!(file = _wfopen(path, L"rb")))
    {
        snprintf(error, error_size, "cannot open scenario (Win32 error %lu)", GetLastError());
        return EXIT_USAGE_OR_PARSE;
    }

    while (fgets(line, sizeof(line), file))
    {
        struct action *action;
        const char *cursor;
        char command[64], arg1[MAX_TEXT], arg2[128], arg3[128];
        char *clean;
        unsigned int parsed_uint;

        ++line_number;
        if (!strchr(line, '\n') && !feof(file))
        {
            snprintf(error, error_size, "line %u exceeds %u bytes", line_number, MAX_LINE - 2);
            fclose(file);
            return EXIT_USAGE_OR_PARSE;
        }
        clean = trim(line);
        if (!*clean || *clean == '#')
            continue;
        if (scenario->count >= MAX_ACTIONS)
        {
            snprintf(error, error_size, "more than %u actions", MAX_ACTIONS);
            fclose(file);
            return EXIT_USAGE_OR_PARSE;
        }
        action = &scenario->actions[scenario->count];
        memset(action, 0, sizeof(*action));
        action->line = line_number;
        action->pair = -1;
        cursor = clean;
        if (!next_word(&cursor, command, sizeof(command)))
            goto bad_syntax;

        if (!_stricmp(command, "load") || !_stricmp(command, "replace"))
        {
            if (!next_word(&cursor, action->text, sizeof(action->text)) || !no_more_words(cursor))
                goto bad_syntax;
            action->kind = !_stricmp(command, "load") ? ACTION_LOAD : ACTION_REPLACE;
        }
        else if (!_stricmp(command, "wait_event"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) ||
                    !next_word(&cursor, arg2, sizeof(arg2)) || !no_more_words(cursor) ||
                    !parse_event(arg1, &action->event) ||
                    !parse_uint(arg2, 3600000, &parsed_uint))
                goto bad_syntax;
            action->kind = ACTION_WAIT_EVENT;
            action->timeout_ms = parsed_uint;
        }
        else if (!_stricmp(command, "wait_time"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) ||
                    !next_word(&cursor, arg2, sizeof(arg2)) || !no_more_words(cursor) ||
                    !parse_double_value(arg1, 0.0, 86400.0, &action->number) ||
                    !parse_uint(arg2, 3600000, &parsed_uint))
                goto bad_syntax;
            action->kind = ACTION_WAIT_TIME;
            action->timeout_ms = parsed_uint;
        }
        else if (!_stricmp(command, "wait_seek_settled"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) ||
                    !next_word(&cursor, arg2, sizeof(arg2)) ||
                    !next_word(&cursor, arg3, sizeof(arg3)) || !no_more_words(cursor) ||
                    !parse_double_value(arg1, 0.0, 86400.0, &action->number) ||
                    !parse_double_value(arg2, 0.0, 60.0, &action->tolerance) ||
                    !parse_uint(arg3, 3600000, &parsed_uint))
                goto bad_syntax;
            action->kind = ACTION_WAIT_SEEK_SETTLED;
            action->timeout_ms = parsed_uint;
        }
        else if (!_stricmp(command, "wait_ms"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) || !no_more_words(cursor) ||
                    !parse_uint(arg1, 3600000, &parsed_uint))
                goto bad_syntax;
            action->kind = ACTION_WAIT_MS;
            action->timeout_ms = parsed_uint;
        }
        else if (!_stricmp(command, "play") || !_stricmp(command, "pause") ||
                !_stricmp(command, "shutdown"))
        {
            if (!no_more_words(cursor)) goto bad_syntax;
            if (!_stricmp(command, "play")) action->kind = ACTION_PLAY;
            else if (!_stricmp(command, "pause")) action->kind = ACTION_PAUSE;
            else
            {
                if (loop_depth)
                {
                    snprintf(error, error_size, "line %u: shutdown cannot be inside a loop", line_number);
                    fclose(file);
                    return EXIT_USAGE_OR_PARSE;
                }
                action->kind = ACTION_SHUTDOWN;
                ++shutdown_count;
            }
        }
        else if (!_stricmp(command, "seek") || !_stricmp(command, "rate"))
        {
            double minimum = !_stricmp(command, "seek") ? 0.0 : 0.0;
            double maximum = !_stricmp(command, "seek") ? 86400.0 : 16.0;
            if (!next_word(&cursor, arg1, sizeof(arg1)) || !no_more_words(cursor) ||
                    !parse_double_value(arg1, minimum, maximum, &action->number))
                goto bad_syntax;
            action->kind = !_stricmp(command, "seek") ? ACTION_SEEK : ACTION_RATE;
        }
        else if (!_stricmp(command, "autoplay") || !_stricmp(command, "media_loop"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) || !no_more_words(cursor) ||
                    (_stricmp(arg1, "on") && _stricmp(arg1, "off")))
                goto bad_syntax;
            action->kind = !_stricmp(command, "autoplay") ? ACTION_AUTOPLAY : ACTION_MEDIA_LOOP;
            action->repeat = !_stricmp(arg1, "on");
        }
        else if (!_stricmp(command, "snapshot"))
        {
            action->kind = ACTION_SNAPSHOT;
            if (!no_more_words(cursor))
            {
                if (!next_word(&cursor, action->text, sizeof(action->text)) || !no_more_words(cursor))
                    goto bad_syntax;
            }
        }
        else if (!_stricmp(command, "loop"))
        {
            if (!next_word(&cursor, arg1, sizeof(arg1)) || !no_more_words(cursor) ||
                    !parse_uint(arg1, 10000, &action->repeat))
                goto bad_syntax;
            if (loop_depth >= MAX_LOOP_DEPTH)
            {
                snprintf(error, error_size, "line %u: loop nesting exceeds %u", line_number, MAX_LOOP_DEPTH);
                fclose(file);
                return EXIT_USAGE_OR_PARSE;
            }
            action->kind = ACTION_LOOP;
            loop_stack[loop_depth++] = scenario->count;
        }
        else if (!_stricmp(command, "endloop"))
        {
            int start;
            if (!no_more_words(cursor) || !loop_depth)
                goto bad_syntax;
            action->kind = ACTION_ENDLOOP;
            start = loop_stack[--loop_depth];
            action->pair = start;
            scenario->actions[start].pair = scenario->count;
        }
        else
        {
            snprintf(error, error_size, "line %u: unknown command '%s'", line_number, command);
            fclose(file);
            return EXIT_USAGE_OR_PARSE;
        }

        ++scenario->count;
        continue;

bad_syntax:
        snprintf(error, error_size, "line %u: invalid arguments for '%s'", line_number, command);
        fclose(file);
        return EXIT_USAGE_OR_PARSE;
    }

    if (ferror(file))
    {
        snprintf(error, error_size, "error reading scenario");
        fclose(file);
        return EXIT_USAGE_OR_PARSE;
    }
    fclose(file);
    if (loop_depth)
    {
        snprintf(error, error_size, "line %u: loop has no endloop", scenario->actions[loop_stack[loop_depth - 1]].line);
        return EXIT_USAGE_OR_PARSE;
    }
    if (!scenario->count)
    {
        snprintf(error, error_size, "scenario has no actions");
        return EXIT_USAGE_OR_PARSE;
    }
    if (shutdown_count != 1 || scenario->actions[scenario->count - 1].kind != ACTION_SHUTDOWN)
    {
        snprintf(error, error_size, "scenario must end with one top-level shutdown action");
        return EXIT_USAGE_OR_PARSE;
    }
    return EXIT_OK;
}

static WCHAR *utf8_to_wide(const char *text)
{
    int count;
    WCHAR *wide;
    count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, NULL, 0);
    if (!count || !(wide = malloc(count * sizeof(*wide))))
        return NULL;
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, wide, count))
    {
        free(wide);
        return NULL;
    }
    return wide;
}

static void begin_source_generation(struct notify *notify)
{
    unsigned long long samples, bytes, nonzero_units;
    audio_monitor_get_counts(notify->audio_monitor, &samples, &bytes, &nonzero_units);
    EnterCriticalSection(&notify->lock);
    ++notify->source_generation;
    ++notify->timeline_generation;
    notify->audio_sample_base = samples;
    notify->audio_byte_base = bytes;
    notify->audio_nonzero_unit_base = nonzero_units;
    memset(notify->event_count, 0, sizeof(notify->event_count));
    memset(notify->event_consumed, 0, sizeof(notify->event_consumed));
    ResetEvent(notify->changed_event);
    LeaveCriticalSection(&notify->lock);
}

static void begin_timeline_generation(struct notify *notify)
{
    EnterCriticalSection(&notify->lock);
    ++notify->timeline_generation;
    LeaveCriticalSection(&notify->lock);
}

/* Fence only events caused by the next action. Source-readiness events must
 * survive load -> pre-ready seek -> wait_event CANPLAY. */
static void begin_event_window(struct notify *notify, DWORD first, DWORD second)
{
    EnterCriticalSection(&notify->lock);
    if (first < MAX_EVENT_CODE)
        notify->event_consumed[first] = notify->event_count[first];
    if (second < MAX_EVENT_CODE)
        notify->event_consumed[second] = notify->event_count[second];
    ResetEvent(notify->changed_event);
    LeaveCriticalSection(&notify->lock);
}

static HRESULT set_source(struct notify *notify, const char *url)
{
    WCHAR *wide;
    BSTR bstr;
    HRESULT hr;

    if (!(wide = utf8_to_wide(url)))
        return E_INVALIDARG;
    if (!(bstr = SysAllocString(wide)))
    {
        free(wide);
        return E_OUTOFMEMORY;
    }
    free(wide);
    begin_source_generation(notify);
    hr = IMFMediaEngineEx_SetSource(notify->engine, bstr);
    SysFreeString(bstr);
    return hr;
}

static int wait_for_event(struct notify *notify, DWORD event, DWORD timeout_ms)
{
    ULONGLONG deadline = GetTickCount64() + timeout_ms;

    for (;;)
    {
        DWORD remaining, wait;
        ULONGLONG now;

        EnterCriticalSection(&notify->lock);
        if (event < MAX_EVENT_CODE && notify->event_count[event] > notify->event_consumed[event])
        {
            ++notify->event_consumed[event];
            LeaveCriticalSection(&notify->lock);
            return EXIT_OK;
        }
        ResetEvent(notify->changed_event);
        LeaveCriticalSection(&notify->lock);

        now = GetTickCount64();
        if (now >= deadline)
            return EXIT_TIMEOUT;
        remaining = (DWORD)(deadline - now);
        wait = WaitForSingleObject(notify->changed_event, remaining);
        if (wait == WAIT_TIMEOUT)
            return EXIT_TIMEOUT;
        if (wait != WAIT_OBJECT_0)
            return EXIT_ACTION_API;
    }
}

static int wait_for_time(struct notify *notify, double target, DWORD timeout_ms)
{
    ULONGLONG deadline = GetTickCount64() + timeout_ms;
    for (;;)
    {
        double current = IMFMediaEngineEx_GetCurrentTime(notify->engine);
        if (isfinite(current) && current + 0.001 >= target)
            return EXIT_OK;
        if (GetTickCount64() >= deadline)
            return EXIT_TIMEOUT;
        Sleep(10);
    }
}

static int wait_for_seek_settled(struct notify *notify, double target,
        double tolerance, DWORD timeout_ms, double *last_time, BOOL *last_seeking)
{
    ULONGLONG deadline = GetTickCount64() + timeout_ms;

    *last_time = NAN;
    *last_seeking = FALSE;
    for (;;)
    {
        ULONGLONG now;
        double current = IMFMediaEngineEx_GetCurrentTime(notify->engine);
        BOOL seeking = IMFMediaEngineEx_IsSeeking(notify->engine);

        *last_time = current;
        *last_seeking = seeking;
        if (!seeking && isfinite(current) && fabs(current - target) <= tolerance)
            return EXIT_OK;

        now = GetTickCount64();
        if (now >= deadline)
            return EXIT_TIMEOUT;
        Sleep((DWORD)(deadline - now) < 10 ? (DWORD)(deadline - now) : 10);
    }
}

static int execute_scenario(struct notify *notify, const struct scenario *scenario,
        unsigned int *completed, char *message, size_t message_size)
{
    struct loop_frame loops[MAX_LOOP_DEPTH];
    unsigned int loop_depth = 0, pc = 0, executed = 0;
    HRESULT hr;

    while (pc < scenario->count)
    {
        const struct action *action = &scenario->actions[pc];
        const char *action_name = "unknown";
        double action_value = NAN;
        double observed_time;
        BOOL observed_seeking;
        int wait_result;

        if (executed >= MAX_EXECUTED_ACTIONS)
        {
            snprintf(message, message_size, "execution limit exceeded at line %u", action->line);
            return EXIT_SCENARIO_FAILED;
        }

        if (action->kind == ACTION_LOOP)
        {
            if (!action->repeat)
            {
                pc = action->pair + 1;
                continue;
            }
            loops[loop_depth].body_pc = pc + 1;
            loops[loop_depth].end_pc = action->pair;
            loops[loop_depth].remaining = action->repeat;
            ++loop_depth;
            ++pc;
            continue;
        }
        if (action->kind == ACTION_ENDLOOP)
        {
            struct loop_frame *frame;
            if (!loop_depth)
            {
                snprintf(message, message_size, "internal loop mismatch at line %u", action->line);
                return EXIT_SCENARIO_FAILED;
            }
            frame = &loops[loop_depth - 1];
            if (--frame->remaining)
                pc = frame->body_pc;
            else
            {
                --loop_depth;
                ++pc;
            }
            continue;
        }

        ++executed;
        hr = S_OK;
        switch (action->kind)
        {
        case ACTION_LOAD:
            action_name = "load";
            hr = set_source(notify, action->text);
            break;
        case ACTION_REPLACE:
            action_name = "replace";
            hr = set_source(notify, action->text);
            break;
        case ACTION_PLAY:
            action_name = "play";
            begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_PLAYING, MAX_EVENT_CODE);
            hr = IMFMediaEngineEx_Play(notify->engine);
            break;
        case ACTION_PAUSE:
            action_name = "pause";
            begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_PAUSE, MAX_EVENT_CODE);
            hr = IMFMediaEngineEx_Pause(notify->engine);
            break;
        case ACTION_SEEK:
            action_name = "seek";
            action_value = action->number;
            begin_timeline_generation(notify);
            begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_SEEKING,
                    MF_MEDIA_ENGINE_EVENT_SEEKED);
            /* A scenario which expects ERROR after this seek must not consume
             * a retained error from an earlier action or source state. */
            begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_ERROR, MAX_EVENT_CODE);
            hr = IMFMediaEngineEx_SetCurrentTimeEx(notify->engine, action->number,
                    MF_MEDIA_ENGINE_SEEK_MODE_NORMAL);
            break;
        case ACTION_RATE:
            action_name = "rate";
            action_value = action->number;
            begin_event_window(notify, MF_MEDIA_ENGINE_EVENT_RATECHANGE, MAX_EVENT_CODE);
            hr = IMFMediaEngineEx_SetPlaybackRate(notify->engine, action->number);
            break;
        case ACTION_AUTOPLAY:
            action_name = "autoplay";
            action_value = action->repeat;
            hr = IMFMediaEngineEx_SetAutoPlay(notify->engine, action->repeat);
            break;
        case ACTION_MEDIA_LOOP:
            action_name = "media_loop";
            action_value = action->repeat;
            hr = IMFMediaEngineEx_SetLoop(notify->engine, action->repeat);
            break;
        case ACTION_WAIT_EVENT:
            action_name = "wait_event";
            wait_result = wait_for_event(notify, action->event, action->timeout_ms);
            if (wait_result)
            {
                emit_snapshot(notify, action_name, event_name(action->event), "timeout", HRESULT_FROM_WIN32(WAIT_TIMEOUT), NAN);
                snprintf(message, message_size, "line %u: timed out waiting %lu ms for %s",
                        action->line, (unsigned long)action->timeout_ms, event_name(action->event));
                *completed = executed - 1;
                return wait_result;
            }
            emit_snapshot(notify, action_name, event_name(action->event), "ok", S_OK, NAN);
            ++pc;
            *completed = executed;
            continue;
        case ACTION_WAIT_TIME:
            action_name = "wait_time";
            wait_result = wait_for_time(notify, action->number, action->timeout_ms);
            if (wait_result)
            {
                emit_snapshot(notify, action_name, NULL, "timeout", HRESULT_FROM_WIN32(WAIT_TIMEOUT), action->number);
                snprintf(message, message_size, "line %u: timed out waiting %lu ms for media time %.6f",
                        action->line, (unsigned long)action->timeout_ms, action->number);
                *completed = executed - 1;
                return wait_result;
            }
            emit_snapshot(notify, action_name, NULL, "ok", S_OK, action->number);
            ++pc;
            *completed = executed;
            continue;
        case ACTION_WAIT_SEEK_SETTLED:
            action_name = "wait_seek_settled";
            wait_result = wait_for_seek_settled(notify, action->number,
                    action->tolerance, action->timeout_ms,
                    &observed_time, &observed_seeking);
            if (wait_result)
            {
                emit_snapshot(notify, action_name, NULL, "timeout",
                        HRESULT_FROM_WIN32(WAIT_TIMEOUT), action->number);
                if (isfinite(observed_time))
                    snprintf(message, message_size,
                            "line %u: timed out after %lu ms waiting for seek target %.6f "
                            "within +/- %.6f; last media time %.6f, seeking=%s",
                            action->line, (unsigned long)action->timeout_ms,
                            action->number, action->tolerance, observed_time,
                            observed_seeking ? "true" : "false");
                else
                    snprintf(message, message_size,
                            "line %u: timed out after %lu ms waiting for seek target %.6f "
                            "within +/- %.6f; last media time unavailable, seeking=%s",
                            action->line, (unsigned long)action->timeout_ms,
                            action->number, action->tolerance,
                            observed_seeking ? "true" : "false");
                *completed = executed - 1;
                return wait_result;
            }
            emit_snapshot(notify, action_name, NULL, "ok", S_OK, action->number);
            ++pc;
            *completed = executed;
            continue;
        case ACTION_WAIT_MS:
            action_name = "wait_ms";
            Sleep(action->timeout_ms);
            emit_snapshot(notify, action_name, NULL, "ok", S_OK, action->timeout_ms);
            ++pc;
            *completed = executed;
            continue;
        case ACTION_SNAPSHOT:
            emit_snapshot(notify, "snapshot", action->text[0] ? action->text : NULL, "ok", S_OK, NAN);
            ++pc;
            *completed = executed;
            continue;
        case ACTION_SHUTDOWN:
            action_name = "shutdown";
            hr = IMFMediaEngineEx_Shutdown(notify->engine);
            if (SUCCEEDED(hr))
            {
                EnterCriticalSection(&notify->lock);
                notify->shutdown = TRUE;
                LeaveCriticalSection(&notify->lock);
            }
            break;
        default:
            snprintf(message, message_size, "line %u: unsupported action", action->line);
            return EXIT_SCENARIO_FAILED;
        }

        /* URLs may contain bearer tokens. Source generation identifies the
         * transition without copying the URL into runtime evidence. */
        emit_snapshot(notify, action_name, NULL,
                SUCCEEDED(hr) ? "ok" : "api_error", hr, action_value);
        if (FAILED(hr))
        {
            snprintf(message, message_size, "line %u: %s failed with HRESULT 0x%08lx",
                    action->line, action_name, (unsigned long)hr);
            *completed = executed - 1;
            return EXIT_ACTION_API;
        }
        ++pc;
        *completed = executed;
    }

    snprintf(message, message_size, "scenario completed");
    return EXIT_OK;
}

static BOOL parse_sha256(const WCHAR *wide, char output[65])
{
    unsigned int i;
    if (!wide || wcslen(wide) != 64) return FALSE;
    for (i = 0; i < 64; ++i)
    {
        WCHAR c = wide[i];
        if (c >= L'0' && c <= L'9') output[i] = (char)c;
        else if (c >= L'a' && c <= L'f') output[i] = (char)c;
        else if (c >= L'A' && c <= L'F') output[i] = (char)(c - L'A' + 'a');
        else return FALSE;
    }
    output[64] = 0;
    return TRUE;
}

static void usage(const WCHAR *program)
{
    fwprintf(stderr,
            L"Usage: %ls --script SCENARIO --output RESULT.jsonl [--audio-monitor] "
            L"[--driver-sha256 HEX] [--scenario-sha256 HEX]\n", program);
}

int wmain(int argc, WCHAR **argv)
{
    const WCHAR *script_path = NULL, *output_path = NULL;
    const WCHAR *driver_sha256 = NULL, *scenario_sha256 = NULL;
    IMFMediaEngineClassFactory *factory = NULL;
    IMFMediaEngine *base_engine = NULL;
    IMFMediaEngineEx *engine = NULL;
    IMFAttributes *attributes = NULL;
    struct audio_monitor *audio_monitor = NULL;
    struct notify *notify = NULL;
    struct scenario *scenario = NULL;
    char message[512] = "initialization failed";
    unsigned int completed = 0;
    HRESULT hr;
    int i, ret = EXIT_INITIALIZATION;
    BOOL com_started = FALSE, mf_started = FALSE, enable_audio_monitor = FALSE;

    for (i = 1; i < argc; ++i)
    {
        if (!wcscmp(argv[i], L"--script") && i + 1 < argc)
            script_path = argv[++i];
        else if (!wcscmp(argv[i], L"--output") && i + 1 < argc)
            output_path = argv[++i];
        else if (!wcscmp(argv[i], L"--audio-monitor"))
            enable_audio_monitor = TRUE;
        else if (!wcscmp(argv[i], L"--driver-sha256") && i + 1 < argc)
            driver_sha256 = argv[++i];
        else if (!wcscmp(argv[i], L"--scenario-sha256") && i + 1 < argc)
            scenario_sha256 = argv[++i];
        else
        {
            usage(argv[0]);
            return EXIT_USAGE_OR_PARSE;
        }
    }
    if (!script_path || !output_path)
    {
        usage(argv[0]);
        return EXIT_USAGE_OR_PARSE;
    }
    if (!(scenario = calloc(1, sizeof(*scenario))))
    {
        fprintf(stderr, "cannot allocate scenario state\n");
        return EXIT_INITIALIZATION;
    }
    if ((ret = parse_script(script_path, scenario, message, sizeof(message))))
    {
        fprintf(stderr, "scenario parse failed: %s\n", message);
        goto done;
    }

    if (FAILED(hr = CoInitializeEx(NULL, COINIT_MULTITHREADED)))
    {
        fprintf(stderr, "CoInitializeEx failed 0x%08lx\n", (unsigned long)hr);
        goto done;
    }
    com_started = TRUE;
    if (FAILED(hr = MFStartup(MF_VERSION, MFSTARTUP_FULL)))
    {
        fprintf(stderr, "MFStartup failed 0x%08lx\n", (unsigned long)hr);
        goto done;
    }
    mf_started = TRUE;

    if (!(notify = calloc(1, sizeof(*notify))))
        goto done;
    notify->IMFMediaEngineNotify_iface.lpVtbl = &notify_vtbl;
    notify->refcount = 1;
    InitializeCriticalSection(&notify->lock);
    notify->changed_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    notify->started_ms = GetTickCount64();
    if ((driver_sha256 && !parse_sha256(driver_sha256, notify->driver_sha256)) ||
            (scenario_sha256 && !parse_sha256(scenario_sha256, notify->scenario_sha256)))
    {
        fprintf(stderr, "identity hashes must be exactly 64 hexadecimal characters\n");
        ret = EXIT_USAGE_OR_PARSE;
        goto done;
    }
    if (!notify->changed_event || !(notify->output = _wfopen(output_path, L"wb")))
    {
        fprintf(stderr, "cannot create output file (Win32 error %lu)\n", GetLastError());
        goto done;
    }

    if (FAILED(hr = CoCreateInstance(&CLSID_MFMediaEngineClassFactory, NULL,
            CLSCTX_INPROC_SERVER, &IID_IMFMediaEngineClassFactory, (void **)&factory)))
    {
        snprintf(message, sizeof(message), "class factory failed with HRESULT 0x%08lx", (unsigned long)hr);
        goto result;
    }
    if (FAILED(hr = MFCreateAttributes(&attributes, 2)) ||
            FAILED(hr = IMFAttributes_SetUnknown(attributes, &MF_MEDIA_ENGINE_CALLBACK,
            (IUnknown *)&notify->IMFMediaEngineNotify_iface)) ||
            FAILED(hr = IMFAttributes_SetUINT32(attributes, &MF_MEDIA_ENGINE_VIDEO_OUTPUT_FORMAT,
            DXGI_FORMAT_B8G8R8X8_UNORM)) ||
            FAILED(hr = IMFMediaEngineClassFactory_CreateInstance(factory, 0, attributes, &base_engine)) ||
            FAILED(hr = IMFMediaEngine_QueryInterface(base_engine, &IID_IMFMediaEngineEx, (void **)&engine)))
    {
        snprintf(message, sizeof(message), "MediaEngine creation failed with HRESULT 0x%08lx", (unsigned long)hr);
        goto result;
    }
    notify->engine = engine;

    if (enable_audio_monitor)
    {
        if (FAILED(hr = create_audio_monitor(&audio_monitor)) ||
                FAILED(hr = IMFMediaEngineEx_InsertAudioEffect(engine,
                (IUnknown *)&audio_monitor->IMFTransform_iface, FALSE)))
        {
            snprintf(message, sizeof(message), "audio delivery monitor failed with HRESULT 0x%08lx",
                    (unsigned long)hr);
            goto result;
        }
        EnterCriticalSection(&notify->lock);
        notify->audio_monitor = audio_monitor;
        LeaveCriticalSection(&notify->lock);
    }

    emit_snapshot(notify, "initialize", NULL, "ok", S_OK, NAN);
    ret = execute_scenario(notify, scenario, &completed, message, sizeof(message));

result:
    if (notify && notify->output)
    {
        if (notify->output_failed && ret == EXIT_OK)
        {
            ret = EXIT_SCENARIO_FAILED;
            snprintf(message, sizeof(message), "JSONL output failed before result record");
        }
        emit_result(notify, ret == EXIT_OK ? "pass" : "fail", ret, completed, message);
        if (notify->output_failed && ret == EXIT_OK)
            ret = EXIT_SCENARIO_FAILED;
    }

done:
    if (engine && notify && !notify->shutdown)
        IMFMediaEngineEx_Shutdown(engine);
    if (notify)
    {
        EnterCriticalSection(&notify->lock);
        notify->engine = NULL;
        notify->audio_monitor = NULL;
        LeaveCriticalSection(&notify->lock);
    }
    if (engine) IMFMediaEngineEx_Release(engine);
    if (audio_monitor) IMFTransform_Release(&audio_monitor->IMFTransform_iface);
    if (base_engine) IMFMediaEngine_Release(base_engine);
    if (attributes) IMFAttributes_Release(attributes);
    if (factory) IMFMediaEngineClassFactory_Release(factory);
    if (notify)
    {
        if (notify->output) fclose(notify->output);
        IMFMediaEngineNotify_Release(&notify->IMFMediaEngineNotify_iface);
    }
    if (mf_started) MFShutdown();
    if (com_started) CoUninitialize();
    free(scenario);
    return ret;
}
