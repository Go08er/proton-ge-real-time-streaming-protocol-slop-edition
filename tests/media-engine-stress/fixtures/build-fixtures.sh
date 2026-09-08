#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die() {
    printf 'fixture build: %s\n' "$*" >&2
    exit 1
}

[[ $# -ge 1 && $# -le 2 ]] || die 'usage: build-fixtures.sh /nix/store/<pinned-nixpkgs-source> [smoke|full]'
nixpkgs_path=$1
profile=${2:-smoke}
[[ "$nixpkgs_path" == /nix/store/* ]] || die 'nixpkgs must be an immutable /nix/store path'
[[ -f "$nixpkgs_path/default.nix" ]] || die 'nixpkgs default.nix is missing'
[[ "$profile" == smoke || "$profile" == full ]] || die 'profile must be smoke or full'

cores=${NIX_BUILD_CORES:-2}
[[ "$cores" =~ ^[1-9][0-9]*$ ]] || die 'NIX_BUILD_CORES must be a positive integer'
((cores <= 8)) || die 'NIX_BUILD_CORES must not exceed 8'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
expected_hash=$(python3 -c 'import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["nar_hash_sri"])' \
    "$script_dir/nixpkgs-pin.json")
actual_hash=$(nix hash path "$nixpkgs_path")
[[ "$actual_hash" == "$expected_hash" ]] ||
    die "nixpkgs NAR hash mismatch: expected $expected_hash, got $actual_hash"
nix-build "$script_dir/default.nix" \
    --argstr nixpkgsPath "$nixpkgs_path" \
    --argstr profile "$profile" \
    --max-jobs 1 \
    --cores "$cores" \
    --no-out-link
