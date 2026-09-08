/* SPDX-License-Identifier: BSD-3-Clause
 * Bounded headless MF source-reader check: decoded PCM/NV12 samples with
 * advancing timestamps, not a renderer, A/V synchronization or game oracle.
 * Run only through run_http_user_agent.py --segment-probe --decode-probe.
 */
#define COBJMACROS
#include <windows.h>
#include <stdio.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <propvarutil.h>

struct samples
{
    DWORD stream, count;
    LONGLONG first, last;
};

static HRESULT set_type(IMFSourceReader *reader, DWORD stream,
        const GUID *major, const GUID *subtype)
{
    IMFMediaType *type = NULL;
    GUID actual;
    HRESULT hr = MFCreateMediaType(&type);

    if (FAILED(hr)) return hr;
    hr = IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, major);
    if (SUCCEEDED(hr)) hr = IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, subtype);
    if (SUCCEEDED(hr)) hr = IMFSourceReader_SetCurrentMediaType(reader, stream, NULL, type);
    IMFMediaType_Release(type);
    if (FAILED(hr)) return hr;
    hr = IMFSourceReader_GetCurrentMediaType(reader, stream, &type);
    if (FAILED(hr)) return hr;
    hr = IMFMediaType_GetGUID(type, &MF_MT_SUBTYPE, &actual);
    IMFMediaType_Release(type);
    return SUCCEEDED(hr) && !IsEqualGUID(&actual, subtype) ? E_FAIL : hr;
}

static HRESULT decode(IMFMediaSource *source, struct samples *audio, struct samples *video,
        int test_case, BOOL seek_test)
{
    IMFSourceReader *reader = NULL;
    DWORD i;
    BOOL seeking = FALSE;
    BOOL balanced = GetEnvironmentVariableW(L"PROBE_BALANCED", NULL, 0) > 0;
    ULONGLONG started;
    HRESULT hr = MFCreateSourceReaderFromMediaSource(source, NULL, &reader);

    if (FAILED(hr)) return hr;
    printf("case=%d stage=reader-created\n", test_case);
    fflush(stdout);
    hr = IMFSourceReader_SetStreamSelection(reader, MF_SOURCE_READER_ALL_STREAMS, FALSE);
    if (FAILED(hr)) goto done;
    for (i = 0; i < 16; ++i)
    {
        IMFMediaType *type = NULL;
        GUID major;

        hr = IMFSourceReader_GetNativeMediaType(reader, i, 0, &type);
        if (FAILED(hr)) break;
        hr = IMFMediaType_GetGUID(type, &MF_MT_MAJOR_TYPE, &major);
        IMFMediaType_Release(type);
        if (FAILED(hr)) goto done;
        if (IsEqualGUID(&major, &MFMediaType_Audio) && audio->stream == (DWORD)-1) audio->stream = i;
        if (IsEqualGUID(&major, &MFMediaType_Video) && video->stream == (DWORD)-1) video->stream = i;
    }
    hr = E_FAIL;
    if (audio->stream == (DWORD)-1 || video->stream == (DWORD)-1) goto done;
    hr = set_type(reader, audio->stream, &MFMediaType_Audio, &MFAudioFormat_PCM);
    if (FAILED(hr)) goto done;
    hr = set_type(reader, video->stream, &MFMediaType_Video, &MFVideoFormat_NV12);
    if (FAILED(hr)) goto done;
    hr = IMFSourceReader_SetStreamSelection(reader, audio->stream, TRUE);
    if (FAILED(hr)) goto done;
    hr = IMFSourceReader_SetStreamSelection(reader, video->stream, TRUE);
    if (FAILED(hr)) goto done;
    printf("case=%d stage=decode-ready\n", test_case);
    fflush(stdout);
read_samples:
    started = GetTickCount64();
    for (i = 0; i < 512 && GetTickCount64() - started < 10000; ++i)
    {
        IMFSample *sample = NULL;
        DWORD stream, flags, size = 0, requested = MF_SOURCE_READER_ANY_STREAM;
        LONGLONG timestamp;
        struct samples *stats;

        if (balanced)
            requested = !audio->count ? audio->stream : !video->count ? video->stream :
                    audio->last - audio->first <= video->last - video->first ? audio->stream : video->stream;
        hr = IMFSourceReader_ReadSample(reader, requested, 0,
                &stream, &flags, &timestamp, &sample);
        if (SUCCEEDED(hr) && sample) hr = IMFSample_GetTotalLength(sample, &size);
        if (sample) IMFSample_Release(sample);
        if (FAILED(hr)) goto done;
        if (flags & (MF_SOURCE_READERF_ERROR | MF_SOURCE_READERF_ENDOFSTREAM)) break;
        if (!size) continue;
        stats = stream == audio->stream ? audio : stream == video->stream ? video : NULL;
        if (!stats) { hr = E_FAIL; goto done; }
        if (!stats->count) stats->first = timestamp;
        stats->last = timestamp;
        ++stats->count;
        if (audio->count >= 8 && video->count >= 8 &&
                audio->last - audio->first >= 20000000 && video->last - video->first >= 20000000 &&
                (!seeking || (audio->last >= 60000000 && video->last >= 60000000)))
        {
            hr = S_OK;
            if (!seeking && seek_test)
            {
                PROPVARIANT position;
                PropVariantInit(&position);
                position.vt = VT_I8;
                position.hVal.QuadPart = 40000000;
                /* Cancel reader read-ahead before positioning; pending sample
                 * requests make SetCurrentPosition reject with INVALIDREQUEST. */
                hr = IMFSourceReader_Flush(reader, MF_SOURCE_READER_ALL_STREAMS);
                if (FAILED(hr)) goto done;
                hr = IMFSourceReader_SetCurrentPosition(reader, &GUID_NULL, &position);
                printf("case=%d seek_hr=%08lx target=40000000\n", test_case, (unsigned long)hr);
                fflush(stdout);
                if (FAILED(hr)) goto done;
                audio->count = video->count = 0;
                audio->first = audio->last = video->first = video->last = 0;
                seeking = TRUE;
                goto read_samples;
            }
            if (seeking)
            {
                printf("case=%d seek_progress=1 audio_last=%lld video_last=%lld\n",
                        test_case, (long long)audio->last, (long long)video->last);
                fflush(stdout);
            }
            goto done;
        }
    }
    hr = E_FAIL;
done:
    printf("case=%d stage=reader-release hr=%08lx audio=%lu video=%lu\n",
            test_case, (unsigned long)hr, (unsigned long)audio->count, (unsigned long)video->count);
    fflush(stdout);
    IMFSourceReader_Release(reader);
    return hr;
}

int wmain(int argc, WCHAR **argv)
{
    IMFSourceResolver *resolver;
    int i;

    if (argc < 2) return 2;
    if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) return 3;
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_FULL))) return 3;
    if (FAILED(MFCreateSourceResolver(&resolver))) return 3;
    for (i = 1; i < argc; ++i)
    {
        MF_OBJECT_TYPE type;
        IUnknown *object = NULL;
        IMFMediaSource *source = NULL;
        HRESULT hr;

        if (wcsncmp(argv[i], L"http://127.0.0.1:", 17)) return 2;
        hr = IMFSourceResolver_CreateObjectFromURL(resolver, argv[i],
                MF_RESOLUTION_MEDIASOURCE, NULL, &type, &object);
        printf("case=%d open_hr=%08lx\n", i, (unsigned long)hr);
        fflush(stdout);
        if (object)
        {
            hr = IUnknown_QueryInterface(object, &IID_IMFMediaSource, (void **)&source);
            if (SUCCEEDED(hr))
            {
                struct samples audio = {(DWORD)-1, 0, 0, 0}, video = {(DWORD)-1, 0, 0, 0};
                hr = decode(source, &audio, &video, i,
                        GetEnvironmentVariableW(L"PROBE_SEEK", NULL, 0) > 0 && wcsstr(argv[i], L".m3u8"));
                printf("case=%d read_hr=%08lx audio=%lu video=%lu audio_span=%lld video_span=%lld\n",
                        i, (unsigned long)hr, (unsigned long)audio.count, (unsigned long)video.count,
                        (long long)(audio.last - audio.first), (long long)(video.last - video.first));
                fflush(stdout);
                IMFMediaSource_Shutdown(source);
                IMFMediaSource_Release(source);
            }
            IUnknown_Release(object);
        }
    }
    IMFSourceResolver_Release(resolver);
    MFShutdown();
    CoUninitialize();
    return 0;
}
