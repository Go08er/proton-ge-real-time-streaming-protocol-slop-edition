/* SPDX-License-Identifier: BSD-3-Clause
 * Stubs around the verbatim transform_node_pull_samples() from a selected
 * Wine tree. Ownership/loop test only, not a decoder or playback oracle.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef int HRESULT;
typedef unsigned DWORD, UINT;
#define S_OK 0
#define E_OUTOFMEMORY (-1)
#define MF_E_TRANSFORM_NEED_MORE_INPUT (-2)
#define MF_E_TRANSFORM_STREAM_CHANGE (-3)
#define FAILED(x) ((x) < 0)
#define SUCCEEDED(x) ((x) >= 0)
#define TOPO_NODE_TRANSFORM_NEEDS_INPUT 1
#define MFT_OUTPUT_DATA_BUFFER_NO_SAMPLE 0x100
#define WARN(...) ((void)0)
#define IMFQualityManager_NotifyProcessOutput(...) ((void)0)
struct sample { int early; };
typedef struct { struct sample *pSample; void *pEvents; DWORD dwStatus; } MFT_OUTPUT_DATA_BUFFER;
struct transform_stream { struct sample *allocated_sample; };
struct topo_node {
    struct { struct { UINT output_count; struct transform_stream outputs[2]; } transform; } u;
    struct { void *transform; } object;
    void *node;
    int flags;
};
struct media_session { void *quality_manager; };
static int calls, allocations, live, pushed, event_count;
static int provided, early_count, events, allocation_failure, stream_change, need_input;
static struct sample *new_sample(void) { ++live; return calloc(1, sizeof(struct sample)); }
static void IMFSample_Release(struct sample *s) { assert(s); --live; free(s); }
static void IMFCollection_Release(void *events) { free(events); }
static HRESULT allocate_output_samples(const struct media_session *s, struct topo_node *n,
        MFT_OUTPUT_DATA_BUFFER *b)
{
    (void)s;
    for (UINT i = 0; i < n->u.transform.output_count; ++i)
    {
        assert(!b[i].pSample && !b[i].pEvents && !b[i].dwStatus);
        if (++allocations == allocation_failure) return E_OUTOFMEMORY;
        if (!provided) b[i].pSample = new_sample();
    }
    return S_OK;
}
static void release_output_samples(struct topo_node *n, MFT_OUTPUT_DATA_BUFFER *b)
{
    for (UINT i = 0; i < n->u.transform.output_count; ++i)
    {
        if (b[i].pSample) IMFSample_Release(b[i].pSample);
        if (b[i].pEvents) IMFCollection_Release(b[i].pEvents);
    }
}
static HRESULT IMFTransform_ProcessOutput(void *t, DWORD flags, UINT count,
        MFT_OUTPUT_DATA_BUFFER *b, DWORD *status)
{
    (void)t; (void)flags; (void)status;
    ++calls;
    if (stream_change && calls == 1) return MF_E_TRANSFORM_STREAM_CHANGE;
    for (UINT i = 0; i < count; ++i)
        if ((!provided && !b[i].pSample) || b[i].dwStatus) return -4;
    if (need_input) return MF_E_TRANSFORM_NEED_MORE_INPUT;
    for (UINT i = 0; i < count; ++i)
    {
        if (provided) b[i].pSample = new_sample();
        b[i].pSample->early = calls <= early_count;
        if (events) b[i].pEvents = malloc(1);
    }
    return S_OK;
}
static HRESULT transform_node_format_changed(struct topo_node *n, MFT_OUTPUT_DATA_BUFFER *b)
{ (void)n; (void)b; return S_OK; }
static int transform_node_markin_need_more_input(const struct media_session *s,
        struct topo_node *n, MFT_OUTPUT_DATA_BUFFER *b)
{
    int again = 1;
    (void)s;
    for (UINT i = 0; i < n->u.transform.output_count; ++i)
    {
        if (b[i].pEvents) again = 0;
        if (b[i].pSample->early)
        {
            IMFSample_Release(b[i].pSample);
            b[i].pSample = NULL;
            b[i].dwStatus |= MFT_OUTPUT_DATA_BUFFER_NO_SAMPLE;
        }
        else again = 0;
    }
    return again;
}
static HRESULT transform_stream_push_sample(struct transform_stream *s, struct sample *sample)
{ (void)s; assert(sample); ++pushed; return S_OK; }
static HRESULT transform_stream_push_events(struct transform_stream *s, void *e)
{ (void)s; assert(e); ++event_count; return S_OK; }

/* SELECTED_WINE_FUNCTION */

int main(int argc, char **argv)
{
    struct media_session session = {0};
    struct topo_node node = {0};
    HRESULT hr;
    assert(argc == 2);
    node.u.transform.output_count = 1;
    early_count = 3;
    if (!strcmp(argv[1], "provided")) provided = 1;
    if (!strcmp(argv[1], "ordinary")) early_count = 0;
    if (!strcmp(argv[1], "events")) events = 1;
    if (!strcmp(argv[1], "alloc-fail")) allocation_failure = 2;
    if (!strcmp(argv[1], "partial-fail")) { node.u.transform.output_count = 2; allocation_failure = 4; }
    if (!strcmp(argv[1], "change")) stream_change = 1;
    if (!strcmp(argv[1], "multi")) node.u.transform.output_count = 2;
    if (!strcmp(argv[1], "need-input")) need_input = 1;
    hr = transform_node_pull_samples(&session, &node);
    for (UINT i = 0; i < node.u.transform.output_count; ++i)
        if (node.u.transform.outputs[i].allocated_sample)
            IMFSample_Release(node.u.transform.outputs[i].allocated_sample);
    printf("hr=%d calls=%d live=%d pushed=%d events=%d flags=%d\n",
            hr, calls, live, pushed, event_count, node.flags);
    assert(live == 0);
    return 0;
}
