#!/usr/bin/env python3
"""Bounded loopback MF open comparison on actual Wine; never uses Steam.

Requires a compiled http_open_probe.exe and two synthetic fixture files:
fixture.mp4 and segment.ts. Runs Wine headless inside Bubblewrap's private
network/PID/mount namespaces, with a fresh prefix and read-only tool inputs.
The fixture intentionally rejects non-WMF clients. A failure reproduces that
compatibility gap, not the remote site's unknown access policy or playback.
With --content-probe, both tools must already send the WMF identity: the
prior-failure oracle instead checks MP4 behind HLS-looking URLs/redirects.
With --segment-probe, it checks real TS bytes behind web-looking HLS segment
names, matching the naming pattern of the privately captured service playlist.
"""

import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import threading


USER_AGENT = "NSPlayer/12.00.19041.4894 WMFSDK/12.00.19041.4894"

# Option isolation uses the selected tool's actual library, not host FFmpeg.
# It is deliberately not counted as a patched-Wine runtime result.
NATIVE_HLS_OPEN = r'''
import ctypes as c
import sys
av, util = c.CDLL('libavformat.so.62'), c.CDLL('libavutil.so.60')
ptr = c.c_void_p
util.av_dict_set.argtypes = [c.POINTER(ptr), c.c_char_p, c.c_char_p, c.c_int]
util.av_dict_count.argtypes = [ptr]
util.av_dict_free.argtypes = [c.POINTER(ptr)]
av.av_find_input_format.argtypes = [c.c_char_p]
av.av_find_input_format.restype = ptr
av.avformat_open_input.argtypes = [c.POINTER(ptr), c.c_char_p, ptr, c.POINTER(ptr)]
av.avformat_close_input.argtypes = [c.POINTER(ptr)]
mode = sys.argv[1]
for case, url in enumerate(sys.argv[2:], 1):
    assert url.startswith('http://127.0.0.1:')
    ctx, opts = ptr(), ptr()
    hls = url.endswith('.m3u8')
    options = [('protocol_whitelist', 'http,https,tcp,tls,httpproxy,crypto' if hls else 'http,https,tcp,tls,httpproxy'),
               ('tls_verify', '1'), ('rw_timeout', '5000000'),
               ('user_agent', 'NSPlayer/12.00.19041.4894 WMFSDK/12.00.19041.4894')]
    if hls and mode != 'picky':
        options.append(('extension_picky', '0'))
    if hls and mode == 'fenced':
        options.append(('format_whitelist', 'hls,mpegts,mov,aac,ac3,eac3,mp3,webvtt'))
    for key, value in options:
        assert util.av_dict_set(c.byref(opts), key.encode(), value.encode(), 0) == 0
    fmt = av.av_find_input_format(b'hls') if hls else None
    assert not hls or fmt
    ret = av.avformat_open_input(c.byref(ctx), url.encode(), fmt, c.byref(opts))
    util.av_dict_set(c.byref(opts), b'tls_verify', None, 0)
    unused = util.av_dict_count(opts)
    print(f'case={case} open_hr={0 if ret >= 0 and not unused else 0x80004005:08x} native_ret={ret} unused={unused}', flush=True)
    av.avformat_close_input(c.byref(ctx))
    util.av_dict_free(c.byref(opts))
'''


def inner(expect, content_probe=False, segment_probe=False, native_hls_mode=None, hls_format_probe=False, decode_probe=False, seek_probe=False, live_hls_probe=False, balanced_reads=False):
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    bodies = {
        "/plain.mp4": Path("/srv/inputs/fixture.mp4").read_bytes(),
        "/segment.ts": Path("/srv/inputs/segment.ts").read_bytes(),
        "/root.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nchild.m3u8\n",
        "/child.m3u8": b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:9.0,\nsegment.ts\n#EXT-X-ENDLIST\n",
    }
    bodies["/gated.mp4"] = bodies["/plain.mp4"]
    if hls_format_probe:
        bodies['/fragmented.mp4'] = Path('/srv/inputs/fragmented.mp4').read_bytes()
        bodies['/packed.aac'] = Path('/srv/inputs/packed.aac').read_bytes()
        bodies['/caption.vtt'] = (b'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n'
                                  b'00:00:00.000 --> 00:00:09.000\nSynthetic caption.\n')
        for label, suffix in [('fragmented', 'mp4'), ('packed', 'aac'), ('caption', 'vtt')]:
            bodies[f'/{label}.m3u8'] = (f'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n'
                                       f'#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:9.0,\n/{label}.{suffix}\n#EXT-X-ENDLIST\n').encode()
    if content_probe:
        bodies["/media.m3u8"] = bodies["/plain.mp4"]
        bodies["/invalid.m3u8"] = b"<!doctype html><title>Not media</title>\n"
    if segment_probe:
        extensions = ('html', 'html', 'js', 'css', 'txt', 'vtt', 'srt', 'woff', 'php', 'ico', 'svg', 'tff')
        for label, suffixes in [('normal-segments', ('ts',) * len(extensions)),
                                ('web-segments', extensions), ('html-segment', ('html',))]:
            segments = []
            for index, suffix in enumerate(suffixes):
                path = f'/{label}-{index}.{suffix}'
                bodies[path] = (b'<!doctype html><title>Not media</title>\n' if label == 'html-segment'
                                else bodies['/segment.ts'])
                segments.append(f'#EXTINF:9.0,\n{path}\n')
            bodies[f'/{label}.m3u8'] = ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n'
                                       '#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n'
                                       + ''.join(segments) + '#EXT-X-ENDLIST\n').encode()
        bodies['/file-segment.m3u8'] = (b'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:9.0,\n'
                                          b'file:///srv/inputs/fixture.mp4\n#EXT-X-ENDLIST\n')
        bodies['/image-segment.m3u8'] = (b'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:9.0,\n'
                                           b'/image.js\n#EXT-X-ENDLIST\n')
        bodies['/image.js'] = b'P6\n2 2\n255\n' + b'\xff\x00\x00' * 4
    if live_hls_probe:
        # A static live-window startup control, not a moving-window soak.
        for path, body in bodies.items():
            if path.endswith('.m3u8'):
                bodies[path] = body.replace(b'#EXT-X-ENDLIST\n', b'').replace(b'#EXT-X-PLAYLIST-TYPE:VOD\n', b'')
    redirects = {"/redirect.mp4"}
    if content_probe:
        redirects.add("/redirect.m3u8")
    observed = []
    total_bytes = 0
    budget_exhausted = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            nonlocal total_bytes, budget_exhausted
            self.connection.settimeout(5)
            ua = self.headers.get("User-Agent", "")
            ua_kind = "wmf" if ua == USER_AGENT else "lavf" if ua.startswith("Lavf/") else "other"
            path = self.path
            if len(observed) >= 64 or total_bytes >= 16 * 1024 * 1024:
                budget_exhausted = True
                self.close_connection = True
                return
            status = 200
            body = bodies.get(path, b"")
            offset = 0
            location = None
            if path == "/denied.mp4" or (path != "/plain.mp4" and ua != USER_AGENT):
                status, body = 403, b""
            elif path in redirects:
                status, body, location = 302, b"", "/gated.mp4"
            elif path not in bodies:
                status, body = 404, b""
            elif path.endswith(".mp4") and self.headers.get("Range"):
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers["Range"])
                if not match:
                    status, body = 416, b""
                else:
                    offset = int(match[1])
                    if offset >= len(body):
                        status, body = 416, b""
                    else:
                        end = min(int(match[2]) + 1, len(body)) if match[2] else len(body)
                        body, status = body[offset:end], 206
            if total_bytes + len(body) > 16 * 1024 * 1024:
                budget_exhausted = True
                self.close_connection = True
                return
            observed.append({"path": path if path in bodies or path in redirects or path == "/denied.mp4" else "unknown",
                             "agent": ua_kind, "status": status, "offset": offset})
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            if status == 206:
                self.send_header("Content-Range", f"bytes {offset}-{offset + len(body) - 1}/{len(bodies[path])}")
            self.send_header("Accept-Ranges", "bytes")
            mime = ("video/mp4" if path == "/media.m3u8" else
                    "text/html" if path == "/invalid.m3u8" else
                    "application/vnd.apple.mpegurl" if path.endswith(".m3u8") else
                    "video/mp2t" if path.endswith(".ts") else "video/mp4")
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            total_bytes += len(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    paths = ["/plain.mp4", "/gated.mp4", "/redirect.mp4", "/root.m3u8", "/denied.mp4"]
    if content_probe:
        paths.extend(("/media.m3u8", "/redirect.m3u8", "/invalid.m3u8"))
    if segment_probe:
        paths.extend(('/normal-segments.m3u8', '/web-segments.m3u8', '/html-segment.m3u8',
                      '/file-segment.m3u8', '/image-segment.m3u8'))
    if hls_format_probe:
        paths.extend(('/fragmented.m3u8', '/packed.m3u8', '/caption.m3u8'))
    urls = [f"http://127.0.0.1:{server.server_port}{path}" for path in paths]
    env = dict(os.environ, HOME="/srv/lab/home", WINEPREFIX="/srv/lab/prefix", WINEDEBUG="-all",
               WINEDLLOVERRIDES="winemenubuilder.exe=d", WINEESYNC="0", WINEFSYNC="0",
               WINEDLLPATH="/srv/tool/files/lib/wine", LD_LIBRARY_PATH="/srv/tool/files/lib/x86_64-linux-gnu:/srv/tool/files/lib:" + os.environ["PROBE_RUNTIME_LIB"],
               SteamGameId=os.environ["PROBE_APP_ID"], SteamAppId=os.environ["PROBE_APP_ID"])
    # A read-only sister-tool comparison must use that tool's plugins, just
    # as its Proton launcher does, not the host steam-run plugin collection.
    gst_plugins = "/srv/tool/files/lib/x86_64-linux-gnu/gstreamer-1.0"
    if Path(gst_plugins).is_dir():
        env.update(GST_PLUGIN_SYSTEM_PATH_1_0=gst_plugins,
                   GST_PLUGIN_PATH_1_0="", WINE_GST_REGISTRY_DIR="/srv/lab/gst-registry")
    Path("/srv/lab/home").mkdir()
    try:
        command = ([sys.executable, '-c', NATIVE_HLS_OPEN, native_hls_mode, *urls] if native_hls_mode
                   else ["/srv/tool/files/bin/wine", r"Z:\srv\inputs\probe.exe", *urls])
        result = subprocess.run(command,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=100, check=False)
    except subprocess.TimeoutExpired as error:
        # Keep the last completed stage even if a synchronous MF call hangs.
        # Exit 124 still fails the oracle; a timeout is never a media pass.
        result = subprocess.CompletedProcess(command, 124, error.stdout or b'', error.stderr or b'')
    finally:
        server.shutdown()
        server.server_close()
    Path("/srv/lab/probe.stdout").write_bytes(result.stdout)
    Path("/srv/lab/probe.stderr").write_bytes(result.stderr)
    hrs = re.findall(rb"case=(\d+) open_hr=([0-9a-f]{8})", result.stdout)
    outcomes = [int(hr, 16) < 0x80000000 for _, hr in hrs]
    expected = [True, False, False, False, False] if expect == "old-failure" else [True, True, True, True, False]
    if content_probe:
        expected = [True, True, True, True, False] + (
            [False, False, False] if expect == "old-failure" else [True, True, False])
    if segment_probe:
        expected = [True, True, True, True, False, True, expect == 'candidate-pass', False, False, False]
    if hls_format_probe:
        expected = [True, True, True, True, False, True, True, True]
    good = (result.returncode == 0 and not budget_exhausted
            and [int(case) for case, _ in hrs] == list(range(1, len(paths) + 1))
            and outcomes == expected)
    decoded = []
    seeks = []
    if decode_probe:
        records = re.findall(rb'case=(\d+) read_hr=([0-9a-f]{8}) audio=(\d+) video=(\d+) audio_span=(-?\d+) video_span=(-?\d+)', result.stdout)
        decoded = [{'case': int(case), 'ok': int(hr, 16) == 0,
                    'audio': int(audio), 'video': int(video),
                    'audio_span': int(audio_span), 'video_span': int(video_span)}
                   for case, hr, audio, video, audio_span, video_span in records]
        good &= [r['case'] for r in decoded] == [i for i, ok in enumerate(expected, 1) if ok]
        good &= all(r['ok'] and r['audio'] >= 8 and r['video'] >= 8
                    and r['audio_span'] >= 20000000 and r['video_span'] >= 20000000 for r in decoded)
    if seek_probe:
        records = re.findall(rb'case=(\d+) seek_progress=1 audio_last=(-?\d+) video_last=(-?\d+)', result.stdout)
        seeks = [{'case': int(case), 'audio_last': int(audio), 'video_last': int(video)}
                 for case, audio, video in records]
        good &= [r['case'] for r in seeks] == [i for i, ok in enumerate(expected, 1)
                                             if ok and paths[i - 1].endswith('.m3u8')]
        good &= all(r['audio_last'] >= 60000000 and r['video_last'] >= 60000000 for r in seeks)
    # Require actual traffic, including child requests on the successful HLS route.
    good &= any(r["agent"] == ("lavf" if expect == "old-failure" and not (content_probe or segment_probe or hls_format_probe) else "wmf") for r in observed)
    if expect == "candidate-pass" or content_probe or segment_probe or hls_format_probe:
        good &= all(r["agent"] == "wmf" for r in observed)
        good &= all(any(r["path"] == p and r["status"] in {200, 206} and r["agent"] == "wmf" for r in observed)
                    for p in ("/gated.mp4", "/root.m3u8", "/child.m3u8", "/segment.ts"))
        good &= any(r["path"] == "/redirect.mp4" and r["status"] == 302 for r in observed)
    if content_probe:
        good &= all(any(r["path"] == p and r["status"] == status for r in observed)
                    for p, status in (("/media.m3u8", 200), ("/redirect.m3u8", 302), ("/invalid.m3u8", 200)))
    if segment_probe:
        good &= all(any(r['path'] == p and r['status'] == 200 for r in observed)
                    for p in ('/normal-segments.m3u8', '/web-segments.m3u8', '/html-segment.m3u8',
                              '/file-segment.m3u8', '/image-segment.m3u8'))
        web_reads = sum(r['path'].startswith('/web-segments-') for r in observed)
        good &= (web_reads == 0 if expect == 'old-failure' else web_reads > 0)
    good &= sum(r["path"] == "/denied.mp4" for r in observed) == 1
    if hls_format_probe:
        good &= all(any(r['path'] == p and r['status'] in (200, 206) for r in observed)
                    for p in ('/fragmented.mp4', '/packed.aac', '/caption.vtt'))
    summary = {"expect": expect, "scenario": "hls-live-window-start" if live_hls_probe else "hls-formats" if hls_format_probe else "segment-names" if segment_probe else "content-detection" if content_probe else "user-agent",
               "passed": bool(good), "open_success": outcomes,
               "request_agents": dict(Counter(r["agent"] for r in observed)),
               "requests": observed, "served_bytes": total_bytes,
               "budget_exhausted": budget_exhausted, "process_exit": result.returncode,
               "decoded": decoded,
               "seeks": seeks,
               "read_cadence": "balanced per-stream progress" if balanced_reads else "ANY_STREAM arrival order",
               "scope": f"native shipped FFmpeg option isolation ({native_hls_mode}); not patched Wine or playback proof" if native_hls_mode
                        else "actual MF decoded PCM/NV12 samples spanning two seconds; not renderer/sync or remote-site proof" if decode_probe
                        else "actual MF source open; synthetic content/HTTP policy; not remote-site or playback proof"}
    Path("/srv/lab/result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0 if good else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--inner":
        if os.environ.get("PROBE_CONTAINED") != "1":
            raise SystemExit("use the outer runner")
        return inner(sys.argv[2], os.environ.get("PROBE_CONTENT") == "1", os.environ.get("PROBE_SEGMENTS") == "1",
                     os.environ.get('PROBE_NATIVE_HLS') or None, os.environ.get('PROBE_HLS_FORMATS') == '1',
                     os.environ.get('PROBE_DECODE') == '1', os.environ.get('PROBE_SEEK') == '1',
                     os.environ.get('PROBE_LIVE_HLS') == '1', os.environ.get('PROBE_BALANCED') == '1')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-lib", type=Path, required=True,
                        help="Nix-store libvdpau library directory missing from the host steam-run FHS")
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--expect", choices=["old-failure", "candidate-pass"], required=True)
    probes = parser.add_mutually_exclusive_group()
    probes.add_argument("--content-probe", action="store_true",
                        help="compare content detection with identical WMF identities; old-failure means A3.21's forced-HLS failure")
    probes.add_argument('--segment-probe', action='store_true',
                        help='compare TS behind web-looking HLS segment names; old-failure means A3.21 rejects the playlist before reading any segment')
    probes.add_argument('--hls-format-probe', action='store_true',
                        help='check fragmented MP4, packed AAC and WebVTT HLS; requires fragmented.mp4 and packed.aac fixtures')
    parser.add_argument('--native-hls-mode', choices=['picky', 'relaxed', 'fenced'],
                        help='segment-probe only: isolate options in the tool\'s FFmpeg 62 library, NOT Wine')
    parser.add_argument('--decode-probe', action='store_true',
                        help='segment-probe only: require http_decode_probe.exe and H264/AAC inputs; verify decoded PCM/NV12 progress')
    parser.add_argument('--seek-probe', action='store_true',
                        help='decode-probe only: also seek finite HLS to four seconds and require decoded A/V through six seconds')
    parser.add_argument('--live-hls-probe', action='store_true',
                        help='decode-probe only: omit ENDLIST for a static live-window startup control, not a continuity soak')
    parser.add_argument('--balanced-reads', action='store_true',
                        help='decode-probe only: request the stream with less delivered progress; retain the same sample cap')
    args = parser.parse_args()
    if args.native_hls_mode and not (args.segment_probe or args.hls_format_probe):
        parser.error('--native-hls-mode requires --segment-probe or --hls-format-probe')
    if args.decode_probe and (not args.segment_probe or args.native_hls_mode):
        parser.error('--decode-probe requires --segment-probe and actual Wine (no --native-hls-mode)')
    if args.seek_probe and not args.decode_probe:
        parser.error('--seek-probe requires --decode-probe')
    if args.live_hls_probe and (not args.decode_probe or args.seek_probe):
        parser.error('--live-hls-probe requires --decode-probe without --seek-probe')
    if args.balanced_reads and not args.decode_probe:
        parser.error('--balanced-reads requires --decode-probe')
    if not 990000 <= args.app_id <= 999999:
        parser.error("only synthetic AppIDs 990000..999999 are permitted; never 438100")
    tool, probe, fixtures = (p.resolve(strict=True) for p in (args.tool, args.probe, args.fixtures))
    runtime_lib = args.runtime_lib.resolve(strict=True)
    if not str(runtime_lib).startswith("/nix/store/") or not (runtime_lib / "libvdpau.so.1").is_file():
        parser.error("runtime-lib must be an existing Nix-store libvdpau directory")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("output must be a fresh private directory")
    for file in (tool / "files/bin/wine", probe, fixtures / "fixture.mp4", fixtures / "segment.ts"):
        if not file.is_file():
            parser.error(f"missing input: {file}")
    if args.hls_format_probe:
        for name in ('fragmented.mp4', 'packed.aac'):
            if not (fixtures / name).is_file():
                parser.error(f'missing input: {fixtures / name}')
    os.umask(0o077)
    output.mkdir(mode=0o700)
    command = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session",
               "--ro-bind", "/", "/", "--tmpfs", "/home", "--tmpfs", "/root",
               "--tmpfs", "/mnt", "--tmpfs", "/srv", "--tmpfs", "/run",
               "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--proc", "/proc", "--dev", "/dev",
               "--ro-bind", str(tool), "/srv/tool", "--ro-bind", str(probe), "/srv/inputs/probe.exe",
               "--ro-bind", str(fixtures / "fixture.mp4"), "/srv/inputs/fixture.mp4",
               "--ro-bind", str(fixtures / "segment.ts"), "/srv/inputs/segment.ts",
               "--ro-bind", str(Path(__file__).resolve()), "/srv/inputs/runner.py",
               "--bind", str(output), "/srv/lab", "--chdir", "/srv/lab", "--clearenv",
               "--setenv", "PATH", os.environ["PATH"], "--setenv", "HOME", "/srv/lab/home",
               "--setenv", "PROBE_APP_ID", str(args.app_id), "--setenv", "PROBE_CONTAINED", "1",
               "--setenv", "PROBE_RUNTIME_LIB", str(runtime_lib),
               "--setenv", "PROBE_CONTENT", "1" if args.content_probe else "0",
               "--setenv", "PROBE_SEGMENTS", "1" if args.segment_probe else "0",
               "--setenv", "PROBE_NATIVE_HLS", args.native_hls_mode or '',
               "--setenv", "PROBE_HLS_FORMATS", '1' if args.hls_format_probe else '0',
               "--setenv", "PROBE_DECODE", '1' if args.decode_probe else '0',
               "--setenv", "PROBE_LIVE_HLS", '1' if args.live_hls_probe else '0',
               str(Path(shutil.which("steam-run")).resolve()), str(Path(sys.executable).resolve()),
               "/srv/inputs/runner.py", "--inner", args.expect]
    if args.seek_probe:
        index = command.index('--setenv')
        command[index:index] = ['--setenv', 'PROBE_SEEK', '1']
    if args.balanced_reads:
        index = command.index('--setenv')
        command[index:index] = ['--setenv', 'PROBE_BALANCED', '1']
    if args.hls_format_probe:
        for name in ('fragmented.mp4', 'packed.aac'):
            index = command.index('--bind')
            command[index:index] = ['--ro-bind', str(fixtures / name), f'/srv/inputs/{name}']
    return subprocess.run(command, timeout=150, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
