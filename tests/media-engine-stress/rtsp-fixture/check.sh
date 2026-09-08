#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)

export PYTHONDONTWRITEBYTECODE=1
python3 -W error -m unittest discover \
    -s "$repo_root/tests/host-runtime" \
    -p 'test_rtsp_live_case.py' \
    -v
python3 -W error -m unittest discover \
    -s "$repo_root/tests/host-runtime" \
    -p 'test_scheduler_pressure.py' \
    -v
python3 -c 'import pathlib,sys; [compile(pathlib.Path(name).read_bytes(), name, "exec") for name in sys.argv[1:]]' \
    "$repo_root/tests/host-runtime/rtsp_live_case.py" \
    "$repo_root/tests/host-runtime/scheduler_pressure.py" \
    "$script_dir/native_smoke.py"
python3 -m json.tool "$script_dir/examples/live-tcp-reopen.control.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-reopen.instrumented.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-blackhole-cancel.control.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-blackhole-cancel.instrumented.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-finite-hold-recovery.control.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-finite-hold-recovery.instrumented.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-drain-diagnostics.control.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/live-tcp-drain-diagnostics.instrumented.oracle.json.in" >/dev/null
python3 -m json.tool "$script_dir/examples/service-config.json.in" >/dev/null
