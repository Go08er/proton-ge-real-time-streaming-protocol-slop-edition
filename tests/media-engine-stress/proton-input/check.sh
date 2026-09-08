#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cache_dir="$(mktemp -d "${TMPDIR:-/tmp}/proton-input-pycache.XXXXXX")"
trap 'rm -rf -- "$cache_dir"' EXIT
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$cache_dir"

python3 -m py_compile "$here/validate_and_extract.py" "$here/test_validate_and_extract.py"
python3 "$here/test_validate_and_extract.py"

echo "proton-input pure checks passed"
