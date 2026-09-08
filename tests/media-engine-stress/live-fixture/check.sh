#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir"

PYTHONDONTWRITEBYTECODE=1 python3 -W error -m unittest -v \
    test_live_fixture.py \
    test_rtsp_contract.py

PYTHONDONTWRITEBYTECODE=1 python3 -W error check_rtsp_contract.py
