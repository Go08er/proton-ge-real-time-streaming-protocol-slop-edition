#define COBJMACROS
#define INITGUID

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

#include <initguid.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfmediaengine.h>
#include <dxgiformat.h>

struct notify
{
    IMFMediaEngineNotify IMFMediaEngineNotify_iface;
    LONG refcount;
    IMFMediaEngineEx *engine;
    HANDLE raced_event;
    LONG raced;
    LONG ended;
    HRESULT play_hr;
    HRESULT pause_hr;
};

static struct notify *notify_from_iface(IMFMediaEngineNotify *iface)
{
    return CONTAINING_RECORD(iface, struct notify, IMFMediaEngineNotify_iface);
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
        CloseHandle(notify->raced_event);
        free(notify);
    }
    return refcount;
}

static HRESULT WINAPI notify_EventNotify(IMFMediaEngineNotify *iface, DWORD event,
        DWORD_PTR param1, DWORD param2)
{
    struct notify *notify = notify_from_iface(iface);
    FILE *events = fopen("C:\\probe-events.txt", "a");

    if (events)
    {
        fprintf(events, "event=%lu param1=%llu param2=%lu\n", (unsigned long)event,
                (unsigned long long)param1, (unsigned long)param2);
        fclose(events);
    }

    if (event == MF_MEDIA_ENGINE_EVENT_FIRSTFRAMEREADY &&
            InterlockedCompareExchange(&notify->raced, 1, 0) == 0)
    {
        notify->play_hr = IMFMediaEngineEx_Play(notify->engine);
        notify->pause_hr = IMFMediaEngineEx_Pause(notify->engine);
        SetEvent(notify->raced_event);
    }
    else if (event == MF_MEDIA_ENGINE_EVENT_ENDED)
    {
        InterlockedIncrement(&notify->ended);
    }
    else if (event == MF_MEDIA_ENGINE_EVENT_ERROR)
    {
        fprintf(stderr, "media error: code=%llu extended=%#lx\n",
                (unsigned long long)param1, (unsigned long)param2);
    }

    return S_OK;
}

static IMFMediaEngineNotifyVtbl notify_vtbl =
{
    notify_QueryInterface,
    notify_AddRef,
    notify_Release,
    notify_EventNotify,
};

static HRESULT load_test_file(const WCHAR *path, IMFByteStream **out)
{
    IMFAttributes *attributes = NULL;
    IMFByteStream *stream = NULL;
    unsigned char *data = NULL;
    FILE *input = NULL;
    ULONG size, written;
    long file_size;
    HRESULT hr;

    *out = NULL;
    if (!(input = _wfopen(path, L"rb"))) return HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND);
    if (fseek(input, 0, SEEK_END) || (file_size = ftell(input)) < 0 ||
            fseek(input, 0, SEEK_SET))
    {
        fclose(input);
        return E_FAIL;
    }
    size = file_size;
    if (!(data = malloc(size)) || fread(data, 1, size, input) != size)
    {
        free(data);
        fclose(input);
        return E_FAIL;
    }
    fclose(input);

    if (FAILED(hr = MFCreateTempFile(MF_ACCESSMODE_READWRITE, MF_OPENMODE_DELETE_IF_EXIST,
            MF_FILEFLAGS_NONE, &stream))) goto done;
    if (FAILED(hr = IMFByteStream_Write(stream, data, size, &written))) goto done;
    if (written != size)
    {
        hr = E_FAIL;
        goto done;
    }
    if (FAILED(hr = IMFByteStream_SetCurrentPosition(stream, 0))) goto done;
    if (FAILED(hr = IMFByteStream_QueryInterface(stream, &IID_IMFAttributes,
            (void **)&attributes))) goto done;
    if (FAILED(hr = IMFAttributes_SetString(attributes, &MF_BYTESTREAM_CONTENT_TYPE,
            L"video/avi"))) goto done;

    *out = stream;
    stream = NULL;

done:
    if (attributes) IMFAttributes_Release(attributes);
    if (stream) IMFByteStream_Release(stream);
    free(data);
    return hr;
}

int wmain(int argc, WCHAR **argv)
{
    IMFMediaEngineClassFactory *factory = NULL;
    IMFMediaEngineEx *engine = NULL;
    IMFMediaEngine *base_engine = NULL;
    IMFAttributes *attributes = NULL;
    IMFAttributes *stream_attributes = NULL;
    IMFByteStream *stream = NULL;
    struct notify *notify = NULL;
    BSTR url = NULL;
    HRESULT hr;
    DWORD wait;
    double t0, t1;
    BOOL paused;
    int ret = 2;

    FILE *result = fopen("C:\\probe-result.txt", "w");

    if (argc != 2)
    {
        fwprintf(stderr, L"usage: %ls Z:\\path\\fixture.avi\n", argv[0]);
        if (result) fprintf(result, "usage failure\n");
        return 2;
    }

    if (FAILED(hr = CoInitializeEx(NULL, COINIT_MULTITHREADED)))
    {
        fprintf(stderr, "CoInitializeEx failed %#lx\n", (unsigned long)hr);
        if (result) fprintf(result, "CoInitializeEx failed %#lx\n", (unsigned long)hr);
        return 2;
    }
    if (FAILED(hr = MFStartup(MF_VERSION, MFSTARTUP_FULL)))
    {
        fprintf(stderr, "MFStartup failed %#lx\n", (unsigned long)hr);
        if (result) fprintf(result, "MFStartup failed %#lx\n", (unsigned long)hr);
        goto done;
    }

    notify = calloc(1, sizeof(*notify));
    notify->IMFMediaEngineNotify_iface.lpVtbl = &notify_vtbl;
    notify->refcount = 1;
    notify->raced_event = CreateEventW(NULL, TRUE, FALSE, NULL);

    if (FAILED(hr = CoCreateInstance(&CLSID_MFMediaEngineClassFactory, NULL,
            CLSCTX_INPROC_SERVER, &IID_IMFMediaEngineClassFactory, (void **)&factory)))
    {
        fprintf(stderr, "class factory failed %#lx\n", (unsigned long)hr);
        if (result) fprintf(result, "class factory failed %#lx\n", (unsigned long)hr);
        goto done;
    }
    if (FAILED(hr = MFCreateAttributes(&attributes, 2))) goto failed;
    if (FAILED(hr = IMFAttributes_SetUnknown(attributes, &MF_MEDIA_ENGINE_CALLBACK,
            (IUnknown *)&notify->IMFMediaEngineNotify_iface))) goto failed;
    if (FAILED(hr = IMFAttributes_SetUINT32(attributes, &MF_MEDIA_ENGINE_VIDEO_OUTPUT_FORMAT,
            DXGI_FORMAT_B8G8R8X8_UNORM))) goto failed;
    if (FAILED(hr = IMFMediaEngineClassFactory_CreateInstance(factory, 0, attributes, &base_engine)))
        goto failed;
    if (FAILED(hr = IMFMediaEngine_QueryInterface(base_engine, &IID_IMFMediaEngineEx, (void **)&engine)))
        goto failed;
    notify->engine = engine;

    if (FAILED(hr = load_test_file(argv[1], &stream))) goto failed;
    if (FAILED(hr = IMFByteStream_QueryInterface(stream, &IID_IMFAttributes,
            (void **)&stream_attributes))) goto failed;
    if (FAILED(hr = IMFAttributes_SetString(stream_attributes, &MF_BYTESTREAM_CONTENT_TYPE,
            L"video/avi"))) goto failed;
    url = SysAllocString(L"pause-scrub-fixture.avi");
    if (FAILED(hr = IMFMediaEngineEx_SetSourceFromByteStream(engine, stream, url))) goto failed;

    wait = WaitForSingleObject(notify->raced_event, 10000);
    if (wait != WAIT_OBJECT_0)
    {
        fprintf(stderr, "FAIL: FIRSTFRAMEREADY race hook timed out (%#lx)\n", (unsigned long)wait);
        if (result) fprintf(result, "FAIL: FIRSTFRAMEREADY race hook timed out (%#lx)\n", (unsigned long)wait);
        ret = 1;
        goto done;
    }
    if (FAILED(notify->play_hr) || FAILED(notify->pause_hr))
    {
        fprintf(stderr, "FAIL: Play=%#lx Pause=%#lx\n",
                (unsigned long)notify->play_hr, (unsigned long)notify->pause_hr);
        if (result) fprintf(result, "FAIL: Play=%#lx Pause=%#lx\n",
                (unsigned long)notify->play_hr, (unsigned long)notify->pause_hr);
        ret = 1;
        goto done;
    }

    Sleep(200);
    t0 = IMFMediaEngineEx_GetCurrentTime(engine);
    Sleep(1000);
    t1 = IMFMediaEngineEx_GetCurrentTime(engine);
    paused = IMFMediaEngineEx_IsPaused(engine);

    printf("Play=%#lx Pause=%#lx paused=%d t0=%.6f t1=%.6f delta=%.6f ended=%ld\n",
            (unsigned long)notify->play_hr, (unsigned long)notify->pause_hr, paused,
            t0, t1, t1 - t0, notify->ended);
    if (result) fprintf(result, "Play=%#lx Pause=%#lx paused=%d t0=%.6f t1=%.6f delta=%.6f ended=%ld\n",
            (unsigned long)notify->play_hr, (unsigned long)notify->pause_hr, paused,
            t0, t1, t1 - t0, notify->ended);
    if (!paused || t1 - t0 > 0.050 || notify->ended)
    {
        fprintf(stderr, "FAIL: final Pause intent did not keep presentation paused\n");
        if (result) fprintf(result, "FAIL: final Pause intent did not keep presentation paused\n");
        ret = 1;
    }
    else
    {
        printf("PASS: final Pause intent owns initial scrub completion\n");
        if (result) fprintf(result, "PASS: final Pause intent owns initial scrub completion\n");
        ret = 0;
    }
    goto done;

failed:
    fprintf(stderr, "setup failed %#lx\n", (unsigned long)hr);
    if (result) fprintf(result, "setup failed %#lx\n", (unsigned long)hr);

done:
    if (engine) IMFMediaEngineEx_Shutdown(engine);
    if (stream_attributes) IMFAttributes_Release(stream_attributes);
    if (stream) IMFByteStream_Release(stream);
    SysFreeString(url);
    if (engine) IMFMediaEngineEx_Release(engine);
    if (base_engine) IMFMediaEngine_Release(base_engine);
    if (attributes) IMFAttributes_Release(attributes);
    if (factory) IMFMediaEngineClassFactory_Release(factory);
    if (notify) IMFMediaEngineNotify_Release(&notify->IMFMediaEngineNotify_iface);
    MFShutdown();
    CoUninitialize();
    if (result) fclose(result);
    return ret;
}
