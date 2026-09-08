#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../scripts/pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command -v make >/dev/null 2>&1 || fail "GNU Make is required"
command -v rsync >/dev/null 2>&1 || fail "rsync is required"
command -v rg >/dev/null 2>&1 || fail "ripgrep is required"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-source-race.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT
ORIGIN="$TMP_ROOT/origin"
mkdir -p "$ORIGIN/libpcap" "$ORIGIN/xz"
printf 'AC_INIT([libpcap-fixture], [1])\n' >"$ORIGIN/libpcap/configure.ac"
printf 'AC_INIT([xz-fixture], [1])\n' >"$ORIGIN/xz/configure.ac"

MAKEFILE="$TMP_ROOT/Makefile"
cat >"$MAKEFILE" <<'MAKE'
OBJ ?= missing-obj
ORIGIN ?= missing-origin

libpcap-rebuild:
.PHONY: libpcap-rebuild
LIBPCAP_SYNC_DEP := $(shell rsync --dry-run --filter=:C --exclude '*~' --exclude .git --exclude compile_commands.json --info=name -Oarx --delete "$(ORIGIN)/libpcap/" "$(OBJ)/src-libpcap" | grep -v -e '^$$' | grep -q '^' && echo libpcap-rebuild)
$(OBJ)/.libpcap-source: $(LIBPCAP_SYNC_DEP)
	rsync --filter=:C --exclude '*~' --exclude .git --exclude compile_commands.json --info=name -Oarx --delete "$(ORIGIN)/libpcap/" "$(OBJ)/src-libpcap"
	touch "$@"
$(OBJ)/.libpcap-post-source: $(OBJ)/.libpcap-source
	touch "$@"

xz-rebuild:
.PHONY: xz-rebuild
XZ_SYNC_DEP := $(shell rsync --dry-run --filter=:C --exclude '*~' --exclude .git --exclude compile_commands.json --info=name -Oarx --delete "$(ORIGIN)/xz/" "$(OBJ)/src-xz" | grep -v -e '^$$' | grep -q '^' && echo xz-rebuild)
$(OBJ)/.xz-source: $(XZ_SYNC_DEP)
	rsync --filter=:C --exclude '*~' --exclude .git --exclude compile_commands.json --info=name -Oarx --delete "$(ORIGIN)/xz/" "$(OBJ)/src-xz"
	touch "$@"
$(OBJ)/.xz-post-source: $(OBJ)/.xz-source
	touch "$@"

all-source: $(OBJ)/.libpcap-post-source $(OBJ)/.xz-post-source
.PHONY: all-source

$(OBJ)/src-xz/configure: $(ORIGIN)/xz/configure.ac | $(OBJ)/.xz-post-source
	mkdir -p "$(OBJ)/src-xz/build-aux"
	printf '#!/bin/sh\nexit 0\n' >"$(OBJ)/src-xz/configure"
	printf '#!/bin/sh\nexit 0\n' >"$(OBJ)/src-xz/build-aux/missing"
	chmod 0755 "$(OBJ)/src-xz/configure" "$(OBJ)/src-xz/build-aux/missing"

$(OBJ)/.xz-configure: $(OBJ)/src-xz/configure
	test -x "$(OBJ)/src-xz/build-aux/missing"
	touch "$@"

build: all-source $(OBJ)/.xz-configure
	test -x "$(OBJ)/src-xz/configure"
	test -x "$(OBJ)/src-xz/build-aux/missing"
.PHONY: build
MAKE

setup_case() {
  local object_root="$1"
  mkdir -p "$object_root/src-libpcap/build-aux" "$object_root/src-xz/build-aux"
  rsync -a "$ORIGIN/libpcap/" "$object_root/src-libpcap/"
  rsync -a "$ORIGIN/xz/" "$object_root/src-xz/"
  for package in libpcap xz; do
    printf '#!/bin/sh\nexit 0\n' >"$object_root/src-$package/configure"
    printf '#!/bin/sh\nexit 0\n' >"$object_root/src-$package/build-aux/missing"
    chmod 0755 "$object_root/src-$package/configure" \
      "$object_root/src-$package/build-aux/missing"
    touch "$object_root/.$package-source" "$object_root/.$package-post-source"
  done
  touch "$object_root/.xz-configure"
}

SINGLE="$TMP_ROOT/single"
setup_case "$SINGLE"
if make -j2 -f "$MAKEFILE" OBJ="$SINGLE" ORIGIN="$ORIGIN" build \
    >"$TMP_ROOT/single.log" 2>&1; then
  fail "single Make graph unexpectedly survived the parse-time rsync deletion race"
fi
rg -Fq "src-xz/configure" "$TMP_ROOT/single.log" \
  || fail "single-graph failure did not reach the deleted generated source"

TWO_PASS="$TMP_ROOT/two-pass"
setup_case "$TWO_PASS"
make -j2 -f "$MAKEFILE" OBJ="$TWO_PASS" ORIGIN="$ORIGIN" all-source \
  >"$TMP_ROOT/all-source.log" 2>&1
pinned_ge_require_resume_source_barrier "$ORIGIN" "$TWO_PASS"
make -j2 -f "$MAKEFILE" OBJ="$TWO_PASS" ORIGIN="$ORIGIN" build \
  >"$TMP_ROOT/build.log" 2>&1
[[ -x "$TWO_PASS/src-xz/configure" \
    && -x "$TWO_PASS/src-xz/build-aux/missing" ]] \
  || fail "fresh post-barrier Make graph did not regenerate XZ Autoconf files"

printf 'GNU Make/rsync resume source-barrier regression passed.\n'
