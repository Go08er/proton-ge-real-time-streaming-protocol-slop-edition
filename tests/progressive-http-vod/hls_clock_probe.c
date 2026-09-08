/* SPDX-License-Identifier: BSD-3-Clause
 * Focused MediaEngine playback/seek check with software frame consumption.
 * Not a DXGI/VR renderer or a remote-service test. Use run_hls_clock.py only.
 */
#define COBJMACROS
#include <windows.h>
#include <stdio.h>
#include <math.h>
#include <mfapi.h>
#include <mfmediaengine.h>
#include <wincodec.h>
#include <dxgiformat.h>

struct callback
{
    IMFMediaEngineNotify iface;
    LONG refs, loaded, errors, seeks;
};

static HRESULT WINAPI query(IMFMediaEngineNotify *iface, REFIID iid, void **obj)
{
    if (!obj) return E_POINTER;
    *obj = NULL;
    if (!IsEqualIID(iid, &IID_IUnknown) && !IsEqualIID(iid, &IID_IMFMediaEngineNotify))
        return E_NOINTERFACE;
    *obj = iface;
    IMFMediaEngineNotify_AddRef(iface);
    return S_OK;
}

static ULONG WINAPI addref(IMFMediaEngineNotify *iface)
{
    return InterlockedIncrement(&((struct callback *)iface)->refs);
}

static ULONG WINAPI release(IMFMediaEngineNotify *iface)
{
    return InterlockedDecrement(&((struct callback *)iface)->refs);
}

static HRESULT WINAPI notify(IMFMediaEngineNotify *iface, DWORD event, DWORD_PTR p1, DWORD p2)
{
    struct callback *cb = (struct callback *)iface;
    (void)p2;
    printf("event=%lu\n", (unsigned long)event);
    fflush(stdout);
    if (event == MF_MEDIA_ENGINE_EVENT_NOTIFYSTABLESTATE) SetEvent((HANDLE)p1);
    if (event == MF_MEDIA_ENGINE_EVENT_CANPLAY) InterlockedExchange(&cb->loaded, 1);
    if (event == MF_MEDIA_ENGINE_EVENT_SEEKED) InterlockedIncrement(&cb->seeks);
    if (event == MF_MEDIA_ENGINE_EVENT_ERROR)
    {
        InterlockedIncrement(&cb->errors);
        printf("event=error code=%llu\n", (unsigned long long)p1);
        fflush(stdout);
    }
    return S_OK;
}

static IMFMediaEngineNotifyVtbl vtbl = {query, addref, release, notify};

static LONG read_counter(LONG *value)
{
    return InterlockedCompareExchange(value, 0, 0);
}

static BOOL progress(IMFMediaEngine *engine, IWICBitmap *bitmap, struct callback *cb,
        unsigned phase, double target)
{
    ULONGLONG start = GetTickCount64(), next = start;
    LONGLONG pts, first = MINLONGLONG, last = MINLONGLONG;
    DWORD frames = 0, transfers = 0;
    RECT rect = {0, 0, 160, 90};
    double clock = 0;
    BOOL success = FALSE;
    while (GetTickCount64() - start < 10000 && !read_counter(&cb->errors))
    {
        HRESULT hr = IMFMediaEngine_OnVideoStreamTick(engine, &pts);
        clock = IMFMediaEngine_GetCurrentTime(engine);
        if (hr == S_OK && pts != last)
        {
            if (first == MINLONGLONG) first = pts;
            last = pts;
            ++frames;
            hr = IMFMediaEngine_TransferVideoFrame(engine, (IUnknown *)bitmap, NULL, &rect, NULL);
            if (FAILED(hr))
            {
                printf("phase=%u transfer_error=%08lx\n", phase, (unsigned long)hr);
                break;
            }
            ++transfers;
        }
        if (GetTickCount64() >= next)
        {
            printf("phase=%u ms=%llu clock=%.6f pts=%lld frames=%lu seeking=%d paused=%d\n",
                    phase, (unsigned long long)(GetTickCount64() - start), clock,
                    (long long)last, (unsigned long)frames,
                    IMFMediaEngine_IsSeeking(engine), IMFMediaEngine_IsPaused(engine));
            fflush(stdout);
            next = GetTickCount64() + 500;
        }
        if (isfinite(clock) && clock >= target + 2 && clock < target + 8 &&
                transfers >= 30 && first != MINLONGLONG && last - first >= 20000000 &&
                last >= (LONGLONG)((target + 2) * 10000000) &&
                !IMFMediaEngine_IsSeeking(engine) && !IMFMediaEngine_IsPaused(engine))
        {
            success = TRUE;
            break;
        }
        Sleep(16);
    }
    printf("phase=%u pass=%d clock=%.6f first=%lld last=%lld frames=%lu transfers=%lu seeks=%ld\n",
            phase, success, clock, (long long)first, (long long)last,
            (unsigned long)frames, (unsigned long)transfers, read_counter(&cb->seeks));
    fflush(stdout);
    return success;
}

int wmain(int argc, WCHAR **argv)
{
    struct callback cb = {{&vtbl}, 1, 0, 0, 0};
    IMFMediaEngineClassFactory *factory = NULL;
    IMFMediaEngine *engine = NULL;
    IMFAttributes *attrs = NULL;
    IWICImagingFactory *wic = NULL;
    IWICBitmap *bitmap = NULL;
    BSTR url = NULL;
    ULONGLONG start;
    double targets[] = {25.0, 8.0};
    unsigned i;
    HRESULT hr;
    int result = 1;
    if (argc != 2 || wcsncmp(argv[1], L"http://127.0.0.1:", 17)) return 2;
    if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) return 3;
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_FULL))) return 3;
#define CHECK(call) do { hr = (call); if (FAILED(hr)) { printf("line=%d hr=%08lx\n", __LINE__, (unsigned long)hr); fflush(stdout); goto done; } } while (0)
    CHECK(CoCreateInstance(&CLSID_MFMediaEngineClassFactory, NULL, CLSCTX_INPROC_SERVER,
            &IID_IMFMediaEngineClassFactory, (void **)&factory));
    CHECK(MFCreateAttributes(&attrs, 2));
    CHECK(IMFAttributes_SetUnknown(attrs, &MF_MEDIA_ENGINE_CALLBACK, (IUnknown *)&cb.iface));
    CHECK(IMFAttributes_SetUINT32(attrs, &MF_MEDIA_ENGINE_VIDEO_OUTPUT_FORMAT, DXGI_FORMAT_B8G8R8X8_UNORM));
    CHECK(IMFMediaEngineClassFactory_CreateInstance(factory, 0, attrs, &engine));
    CHECK(CoCreateInstance(&CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
            &IID_IWICImagingFactory, (void **)&wic));
    CHECK(IWICImagingFactory_CreateBitmap(wic, 160, 90, &GUID_WICPixelFormat32bppBGR,
            WICBitmapCacheOnLoad, &bitmap));
    url = SysAllocString(argv[1]);
    if (!url) goto done;
    CHECK(IMFMediaEngine_SetSource(engine, url));
    printf("stage=source-set\n");
    fflush(stdout);
    start = GetTickCount64();
    while (!read_counter(&cb.loaded) && !read_counter(&cb.errors) && GetTickCount64() - start < 15000) Sleep(10);
    printf("stage=ready-wait-ended loaded=%ld errors=%ld\n", read_counter(&cb.loaded), read_counter(&cb.errors));
    fflush(stdout);
    printf("ready=%ld errors=%ld audio=%d video=%d\n", read_counter(&cb.loaded), read_counter(&cb.errors),
            IMFMediaEngine_HasAudio(engine), IMFMediaEngine_HasVideo(engine));
    fflush(stdout);
    if (!read_counter(&cb.loaded) || read_counter(&cb.errors) || !IMFMediaEngine_HasAudio(engine) || !IMFMediaEngine_HasVideo(engine)) goto done;
    CHECK(IMFMediaEngine_Play(engine));
    if (!progress(engine, bitmap, &cb, 0, 0)) goto done;
    for (i = 0; i < sizeof(targets) / sizeof(targets[0]); ++i)
    {
        CHECK(IMFMediaEngine_SetCurrentTime(engine, targets[i]));
        if (!progress(engine, bitmap, &cb, i + 1, targets[i])) goto done;
    }
    result = 0;
done:
    printf("result=%d errors=%ld\n", result, read_counter(&cb.errors));
    fflush(stdout);
    if (engine) IMFMediaEngine_Shutdown(engine);
    if (bitmap) IWICBitmap_Release(bitmap);
    if (wic) IWICImagingFactory_Release(wic);
    if (engine) IMFMediaEngine_Release(engine);
    if (attrs) IMFAttributes_Release(attrs);
    if (factory) IMFMediaEngineClassFactory_Release(factory);
    SysFreeString(url);
    MFShutdown();
    CoUninitialize();
    return result;
}
