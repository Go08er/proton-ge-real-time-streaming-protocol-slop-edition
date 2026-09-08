/* SPDX-License-Identifier: BSD-3-Clause
 * Headless public-MF source-open probe; no frame/playback claim.
 */
#define COBJMACROS
#include <windows.h>
#include <stdio.h>
#include <mfapi.h>
#include <mfidl.h>

int wmain(int argc, WCHAR **argv)
{
    IMFSourceResolver *resolver;
    HRESULT hr;
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

        if (wcsncmp(argv[i], L"http://127.0.0.1:", 17)) return 2;
        hr = IMFSourceResolver_CreateObjectFromURL(resolver, argv[i],
                MF_RESOLUTION_MEDIASOURCE, NULL, &type, &object);
        printf("case=%d open_hr=%08lx\n", i, (unsigned long)hr);
        fflush(stdout);
        if (object)
        {
            if (SUCCEEDED(IUnknown_QueryInterface(object, &IID_IMFMediaSource, (void **)&source)))
            {
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
