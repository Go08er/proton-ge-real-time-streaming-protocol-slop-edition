#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cache_dir="$(mktemp -d "${TMPDIR:-/tmp}/runtime-input-pycache.XXXXXX")"
trap 'rm -rf -- "$cache_dir"' EXIT
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$cache_dir"

python3 -m py_compile "$here/audit_runtime.py" "$here/test_audit_runtime.py"
python3 "$here/test_audit_runtime.py"

echo "runtime-input pure checks passed"
