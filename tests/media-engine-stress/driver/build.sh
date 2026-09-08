#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

usage()
{
    cat <<'EOF'
Usage: build.sh [--arch x86_64|i686|all] [--out DIR] [--check]

Builds the standalone Windows MediaEngine scenario driver with MinGW.  The
default architecture is x86_64 and the default output directory is ./build.
--check also runs the pure-Python JSONL parser/oracle unit tests.

Compiler overrides:
  MINGW_CC_X64   default x86_64-w64-mingw32-gcc
  MINGW_CC_X86   default i686-w64-mingw32-gcc
EOF
}

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
ARCH=x86_64
OUT="$SCRIPT_DIR/build"
CHECK=0

while (($#)); do
    case "$1" in
        --arch)
            (($# >= 2)) || { usage >&2; exit 2; }
            ARCH=$2
            shift 2
            ;;
        --out)
            (($# >= 2)) || { usage >&2; exit 2; }
            OUT=$2
            shift 2
            ;;
        --check)
            CHECK=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            printf 'unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

case "$ARCH" in
    x86_64|i686|all) ;;
    *) printf 'unsupported architecture: %s\n' "$ARCH" >&2; exit 2 ;;
esac

CC_X64=${MINGW_CC_X64:-x86_64-w64-mingw32-gcc}
CC_X86=${MINGW_CC_X86:-i686-w64-mingw32-gcc}
mkdir -p -- "$OUT"

build_one()
{
    local name=$1 cc=$2
    command -v "$cc" >/dev/null || {
        printf 'missing MinGW compiler %s (enter the project nix-shell)\n' "$cc" >&2
        exit 2
    }
    "$cc" \
        -std=gnu11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter \
        -Wframe-larger-than=1048576 -municode \
        -ffile-prefix-map="$SCRIPT_DIR"=media-engine-stress-driver \
        -fdebug-prefix-map="$SCRIPT_DIR"=media-engine-stress-driver \
        -D_WIN32_WINNT=0x0602 \
        -Wl,--no-insert-timestamp \
        -o "$OUT/media-engine-stress-$name.exe" \
        "$SCRIPT_DIR/media_engine_stress.c" \
        -lole32 -loleaut32 -lmf -lmfplat -lmfuuid -luuid
}

case "$ARCH" in
    x86_64) build_one x86_64 "$CC_X64" ;;
    i686) build_one i686 "$CC_X86" ;;
    all)
        build_one x86_64 "$CC_X64"
        build_one i686 "$CC_X86"
        ;;
esac

if ((CHECK)); then
    (
        cd "$SCRIPT_DIR"
        PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
            test_parse_results.py test_driver_source.py
    )
fi
