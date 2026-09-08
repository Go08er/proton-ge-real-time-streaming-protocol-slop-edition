#!/usr/bin/env python3
"""One loopback MediaEngine clock/seek case with private null audio, no host graphics.

Inputs: hls_clock_probe.exe and a synthetic fixture.mp4 / playlist.m3u8 /
segNNN.ts set. This tests software frame consumption, not VR/DXGI or a service.
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
import time


def inner(case):
    # libpulse allocates a 64 MiB memfd even with the private server's SHM
    # transport disabled. A 32 MiB cap killed Wine with SIGXFSZ before playback.
    # This is a per-file/IPC cap; the HTTP byte budget below stays 32 MiB.
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    bodies = {p.name: p.read_bytes() for p in Path('/srv/fixtures').iterdir()
              if p.name in {'fixture.mp4', 'playlist.m3u8'} or re.fullmatch(r'seg\d{3}\.ts', p.name)}
    if case == 'web':
        bodies['playlist.m3u8'] = bodies['playlist.m3u8'].replace(b'.ts', b'.js')
        bodies.update({k[:-3] + '.js': v for k, v in list(bodies.items()) if k.endswith('.ts')})
    requests = Counter()
    served = 0
    exhausted = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            nonlocal served, exhausted
            self.connection.settimeout(3)
            key = self.path.lstrip('/')
            requests[key] += 1
            if sum(requests.values()) > 128:
                exhausted = True
                self.close_connection = True
                return
            if key not in bodies:
                self.send_error(404)
                return
            body = bodies[key]
            status, offset, end = 200, 0, len(body)
            if self.headers.get('Range'):
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers['Range'])
                if not match or int(match[1]) >= len(body):
                    self.send_error(416)
                    return
                offset = int(match[1])
                if match[2]:
                    end = min(end, int(match[2]) + 1)
                status, body = 206, body[offset:end]
            if served + len(body) > 32 * 1024 * 1024:
                exhausted = True
                self.close_connection = True
                return
            self.send_response(status)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl' if key.endswith('.m3u8')
                             else 'video/mp4' if key.endswith('.mp4') else 'video/mp2t')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {offset}-{end - 1}/{len(bodies[key])}')
            self.end_headers()
            served += len(body)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for name in ('home', 'run'):
        Path('/srv/lab', name).mkdir(mode=0o700)
    env = dict(os.environ, HOME='/srv/lab/home', XDG_RUNTIME_DIR='/srv/lab/run',
               WINEPREFIX='/srv/lab/prefix',
               WINEDEBUG='-all,err+all,warn+dmo,warn+mfplat' if os.environ.get('PROBE_DIAGNOSTIC') == '1' else '-all',
               WINEDLLOVERRIDES='winemenubuilder.exe=d', WINEESYNC='0', WINEFSYNC='0',
               WINEDLLPATH='/srv/tool/files/lib/vkd3d:/srv/tool/files/lib/wine',
               LD_LIBRARY_PATH='/srv/tool/files/lib/x86_64-linux-gnu:/srv/tool/files/lib:' + os.environ['PROBE_RUNTIME_LIB'],
               PULSE_SERVER='unix:/srv/lab/pulse.sock', PULSE_SINK='probe',
               SteamGameId=os.environ['PROBE_APP_ID'], SteamAppId=os.environ['PROBE_APP_ID'])
    # The outer namespace masks every host service socket; this server has only
    # a null sink and no module-udev-detect or physical audio devices.
    with Path('/srv/lab/pulse.log').open('wb') as pulse_log:
        pulse = subprocess.Popen([os.environ['PROBE_PULSE'], '-n', '--daemonize=no',
                                  '--exit-idle-time=-1', '--disable-shm=true', '--use-pid-file=no',
                                  '--load=module-native-protocol-unix socket=/srv/lab/pulse.sock auth-anonymous=1',
                                  '--load=module-null-sink sink_name=probe rate=48000 channels=2'],
                                 env=env, stdout=pulse_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 5
            while not Path('/srv/lab/pulse.sock').exists() and time.monotonic() < deadline:
                if pulse.poll() is not None:
                    raise RuntimeError('private audio server failed; inspect pulse.log')
                time.sleep(0.05)
            if not Path('/srv/lab/pulse.sock').exists():
                raise RuntimeError('private audio server readiness timed out')
            path = 'fixture.mp4' if case == 'mp4' else 'playlist.m3u8'
            command = ['/srv/tool/files/bin/wine', r'Z:\srv\inputs\probe.exe',
                       f'http://127.0.0.1:{server.server_port}/{path}']
            try:
                result = subprocess.run(command, env=env, capture_output=True, timeout=75)
            except subprocess.TimeoutExpired as error:
                result = subprocess.CompletedProcess(command, 124, error.stdout or b'', error.stderr or b'')
        finally:
            pulse.terminate()
            try:
                pulse.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pulse.kill()
                pulse.wait()
            server.shutdown()
            server.server_close()
    Path('/srv/lab/probe.stdout').write_bytes(result.stdout)
    Path('/srv/lab/probe.stderr').write_bytes(result.stderr)
    phases = [(int(n), bool(int(ok))) for n, ok in re.findall(rb'phase=(\d+) pass=([01])', result.stdout)]
    passed = result.returncode == 0 and phases == [(0, True), (1, True), (2, True)] and not exhausted
    summary = dict(passed=passed, case=case, exit=result.returncode, phases=phases,
                   requests=dict(requests), served_bytes=served, budget_exhausted=exhausted,
                   scope='MediaEngine clock and software frame transfer; null audio; not endpoint/audio-sync/VR proof')
    Path('/srv/lab/result.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return 0 if passed else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--inner':
        if os.environ.get('PROBE_CONTAINED') != '1':
            raise SystemExit('use outer runner')
        return inner(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('tool', 'probe', 'fixtures', 'output', 'runtime-lib', 'pulseaudio'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--app-id', type=int, required=True)
    parser.add_argument('--case', choices=('mp4', 'hls', 'web'), required=True)
    parser.add_argument('--diagnostic', action='store_true', help='bounded warnings/errors on synthetic media only; no trace')
    args = parser.parse_args()
    if not 990000 <= args.app_id <= 999999:
        parser.error('synthetic AppID 990000..999999 required; 438100 refused')
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must be a new directory')
    for path in (args.tool / 'files/bin/wine', args.probe, args.fixtures / 'fixture.mp4',
                 args.fixtures / 'playlist.m3u8', args.pulseaudio):
        if not path.is_file():
            parser.error(f'missing input: {path}')
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True)
    command = [shutil.which('bwrap'), '--unshare-all', '--die-with-parent', '--new-session',
               '--ro-bind', '/', '/', '--tmpfs', '/home', '--tmpfs', '/root', '--tmpfs', '/mnt',
               '--tmpfs', '/srv', '--tmpfs', '/run', '--tmpfs', '/tmp', '--tmpfs', '/var/tmp',
               '--proc', '/proc', '--dev', '/dev',
               '--ro-bind', str(args.tool.resolve()), '/srv/tool',
               '--ro-bind', str(args.probe.resolve()), '/srv/inputs/probe.exe',
               '--ro-bind', str(args.fixtures.resolve()), '/srv/fixtures',
               '--ro-bind', str(Path(__file__).resolve()), '/srv/inputs/runner.py',
               '--bind', str(args.output.resolve()), '/srv/lab', '--chdir', '/srv/lab', '--clearenv',
               '--setenv', 'PATH', os.environ['PATH'], '--setenv', 'HOME', '/srv/lab/home',
               '--setenv', 'PROBE_CONTAINED', '1', '--setenv', 'PROBE_APP_ID', str(args.app_id),
               '--setenv', 'PROBE_DIAGNOSTIC', '1' if args.diagnostic else '0',
               '--setenv', 'PROBE_RUNTIME_LIB', str(args.runtime_lib.resolve()),
               '--setenv', 'PROBE_PULSE', str(args.pulseaudio.resolve()),
               str(Path(shutil.which('steam-run')).resolve()), str(Path(sys.executable).resolve()),
               '/srv/inputs/runner.py', '--inner', args.case]
    return subprocess.run(command, timeout=110).returncode


if __name__ == '__main__':
    raise SystemExit(main())
