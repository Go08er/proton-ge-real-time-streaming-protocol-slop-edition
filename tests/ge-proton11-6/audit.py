#!/usr/bin/env python3
"""Source contracts for the A3.19 merge. Not a frame-transfer/runtime oracle."""

import argparse
from pathlib import Path
import re


def function(text, name):
    match = re.search(r"^[A-Za-z_][^\n]*\b" + re.escape(name)
                      + r"\([^;]*?\n\{.*?^}\n", text, re.M | re.S)
    if not match:
        raise ValueError("missing function " + name)
    return match.group()


def audit(engine, demux):
    failures = []

    def require(condition, message):
        if not condition:
            failures.append(message)

    load = function(engine, "media_engine_Load")
    require("FLAGS_ENGINE_AUTO_PLAY" in load
            and "if (autoplay)\n        hr = IMFMediaEngineEx_Play(iface);" in load,
            "GE Load autoplay must flow through explicit Play intent")
    require(0 <= load.find("LeaveCriticalSection") < load.find("IMFMediaEngineEx_Play"),
            "Load must release its lock before calling Play")
    seekable = function(engine, "media_engine_GetSeekable")
    require("MFMEDIASOURCE_CAN_SEEK" in seekable
            and "isinf(engine->duration)" in seekable,
            "preserve GE live-readiness range and the correct source capability enum")
    require("MFBYTESTREAM_IS_SEEKABLE" not in seekable,
            "do not test a bytestream flag against media-source characteristics")
    scrub = engine.split("case MESessionScrubSampleComplete:", 1)[1].split("break;", 1)[0]
    require("!(engine->flags & FLAGS_ENGINE_PAUSE_PENDING)" in scrub,
            "scrub completion must not issue a duplicate already-pending Pause")
    require("else if (is_http_url( params->url ))" in demux
            and "media-route=direct-http-progressive" in demux,
            "direct HTTP must include extensionless Googlevideo URLs")
    require("is_googlevideo_url" not in demux and '"reconnect' not in demux,
            "no separate Googlevideo retry policy alongside bounded direct HTTP")
    require("demuxer->ctx->interrupt_callback.callback = demuxer_interrupt_callback" in demux
            and 'av_dict_set_int( &options, "tls_verify", 1, 0 )' in demux,
            "retain cancellation and TLS verification (full contract is audited separately)")
    require("media_engine_diagnose_live_texture_pixels" not in engine
            and "rgb_sum" not in engine and "alpha_sum" not in engine,
            "diagnostic-only pixel scans/readbacks must be absent")
    require(not re.search(r'WARN\("Live (?!DXGI transfer %ld (?:failed|rejected|could not lock))'
                          r'|WARN\("Load requested|WARN\("Media Engine sink event', engine),
            "routine GE diagnostics must not enter the warning stream")
    for failure in ("failed at", "rejected by geometry", "could not lock the same-device path"):
        require('WARN("Live DXGI transfer %ld ' + failure in engine,
                "retain the bounded real transfer-failure warning: " + failure)
    require('debugstr_a(params->url)' not in function(demux, "demuxer_create"),
            "demuxer creation diagnostics must omit the resolved remote URL")
    require("Live client texture probe" not in engine,
            "remove the client probe which printed the resolved source URL")
    for operation in ("ID3D11DeviceContext_CopySubresourceRegion",
                      "ID3D11DeviceContext_Draw", "ID3D11DeviceContext_UpdateSubresource",
                      "IMF2DBuffer2_Lock2DSize"):
        require(operation in engine, "production frame operation missing: " + operation)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wine-tree", required=True, type=Path)
    args = parser.parse_args()
    try:
        failures = audit(
            (args.wine_tree / "dlls/mfmediaengine/main.c").read_text(),
            (args.wine_tree / "dlls/winedmo/unix_demuxer.c").read_text(),
        )
    except (OSError, ValueError, IndexError) as error:
        parser.exit(1, f"A3.19 source audit: {error}\n")
    if failures:
        parser.exit(1, "A3.19 source audit FAILED:\n  " + "\n  ".join(failures) + "\n")
    print("A3.19 GE integration source contracts: PASS (not playback evidence)")


if __name__ == "__main__":
    main()
