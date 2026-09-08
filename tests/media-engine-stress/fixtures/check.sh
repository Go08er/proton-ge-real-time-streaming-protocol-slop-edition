#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest -v test_fixture_contract.py
bash -n generate-fixtures.sh check-eval.sh build-fixtures.sh check.sh
python3 -c 'import pathlib,sys; [compile(pathlib.Path(name).read_bytes(), name, "exec") for name in sys.argv[1:]]' \
    align_live_renditions.py make_live_schedule.py generate_manifest.py validate_fixtures.py test_fixture_contract.py
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck generate-fixtures.sh check-eval.sh build-fixtures.sh check.sh
fi

if [[ $# -eq 1 ]]; then
    ./check-eval.sh "$1" smoke
elif [[ $# -ne 0 ]]; then
    printf 'usage: check.sh [/nix/store/<pinned-nixpkgs-source>]\n' >&2
    exit 1
fi
