#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/rtsp-ge-prefetch-test.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

TEST_PROJECT="$TMP_ROOT/project"
TEST_SOURCE="$TMP_ROOT/source"
SECOND_SOURCE="$TMP_ROOT/source-from-cache"
BAD_SOURCE="$TMP_ROOT/source-bad-download"
TEST_CACHE="$TMP_ROOT/cache"
FIXTURES="$TMP_ROOT/fixtures"
FETCH_COUNT="$TMP_ROOT/fetch-count"
MANIFEST="$TEST_PROJECT/config/build-contrib.tsv"
CONFIG="$TEST_PROJECT/config/test.env"
mkdir -p "$TEST_PROJECT/scripts" "$TEST_PROJECT/config" "$FIXTURES"
cp -- "$ROOT_DIR/scripts/pinned-ge-common.sh" \
  "$ROOT_DIR/scripts/prefetch-pinned-ge-contrib.sh" \
  "$TEST_PROJECT/scripts/"
chmod 0755 "$TEST_PROJECT/scripts/prefetch-pinned-ge-contrib.sh"

mkdir -p "$TEST_SOURCE"
git -C "$TEST_SOURCE" init -q
printf 'pinned source\n' >"$TEST_SOURCE/tracked.txt"
git -C "$TEST_SOURCE" add tracked.txt
git -C "$TEST_SOURCE" \
  -c user.name=Test -c user.email=test.invalid commit -qm initial
source_commit="$(git -C "$TEST_SOURCE" rev-parse HEAD)"
source_tree="$(git -C "$TEST_SOURCE" rev-parse 'HEAD^{tree}')"

printf 'first declared input\n' >"$FIXTURES/first.bin"
printf 'second declared input\n' >"$FIXTURES/second.bin"
first_size="$(stat -c '%s' -- "$FIXTURES/first.bin")"
second_size="$(stat -c '%s' -- "$FIXTURES/second.bin")"
first_sha="$(sha256sum "$FIXTURES/first.bin" | awk '{print $1}')"
second_sha="$(sha256sum "$FIXTURES/second.bin" | awk '{print $1}')"

{
  printf 'build_contrib_manifest_version\t3\n'
  printf 'source_commit\t%s\n' "$source_commit"
  printf '# Version 3 fetch records bind URLs to the existing exact entries.\n'
  printf 'entry\tcontrib/first.bin\t0644\t%s\t%s\n' \
    "$first_size" "$first_sha"
  printf 'fetch\tcontrib/first.bin\thttps://fixtures.invalid/first.bin\n'
  printf 'entry\tcontrib/nested/second.bin\t0644\t%s\t%s\n' \
    "$second_size" "$second_sha"
  printf 'fetch\tcontrib/nested/second.bin\thttps://fixtures.invalid/second.bin\n'
} >"$MANIFEST"
manifest_sha="$(sha256sum "$MANIFEST" | awk '{print $1}')"

sed \
  -e "s|^PIN_SOURCE_COMMIT=.*|PIN_SOURCE_COMMIT=$source_commit|" \
  -e "s|^PIN_SOURCE_TREE=.*|PIN_SOURCE_TREE=$source_tree|" \
  -e 's|^PIN_BUILD_CONTRIB_MANIFEST=.*|PIN_BUILD_CONTRIB_MANIFEST=config/build-contrib.tsv|' \
  -e "s|^PIN_BUILD_CONTRIB_MANIFEST_SHA256=.*|PIN_BUILD_CONTRIB_MANIFEST_SHA256=$manifest_sha|" \
  "$ROOT_DIR/config/ge-proton11-3-a314.env" >"$CONFIG"

FAKE_CURL="$TMP_ROOT/fake-curl"
cat >"$FAKE_CURL" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

output=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --)
      url="$2"
      shift 2
      ;;
    --fail|--location|--silent|--show-error)
      shift
      ;;
    *)
      printf 'unexpected fake-curl argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done
[[ -n "$output" && -n "$url" ]]
case "$url" in
  https://fixtures.invalid/first.bin)
    input="$PREFETCH_FIXTURES/first.bin"
    ;;
  https://fixtures.invalid/second.bin)
    input="$PREFETCH_FIXTURES/second.bin"
    ;;
  *)
    printf 'unexpected fake-curl URL: %s\n' "$url" >&2
    exit 2
    ;;
esac
printf '%s\n' "$url" >>"$PREFETCH_FETCH_COUNT"
cp -- "$input" "$output"
EOF
chmod 0755 "$FAKE_CURL"
export PINNED_GE_CURL="$FAKE_CURL"
export PREFETCH_FIXTURES="$FIXTURES"
export PREFETCH_FETCH_COUNT="$FETCH_COUNT"

PREFETCH="$TEST_PROJECT/scripts/prefetch-pinned-ge-contrib.sh"
"$PREFETCH" --config "$CONFIG" --source "$TEST_SOURCE" \
  --cache-dir "$TEST_CACHE" >/dev/null
[[ "$(wc -l <"$FETCH_COUNT")" == 2 ]] \
  || fail "initial prefetch did not retrieve exactly two inputs"
cmp -- "$FIXTURES/first.bin" "$TEST_SOURCE/contrib/first.bin"
cmp -- "$FIXTURES/second.bin" "$TEST_SOURCE/contrib/nested/second.bin"

"$PREFETCH" --config "$CONFIG" --source "$TEST_SOURCE" \
  --cache-dir "$TEST_CACHE" >/dev/null
[[ "$(wc -l <"$FETCH_COUNT")" == 2 ]] \
  || fail "idempotent prefetch retrieved an already-correct input"

git clone -q "$TEST_SOURCE" "$SECOND_SOURCE"
"$PREFETCH" --config "$CONFIG" --source "$SECOND_SOURCE" \
  --cache-dir "$TEST_CACHE" >"$TMP_ROOT/from-cache.out"
[[ "$(wc -l <"$FETCH_COUNT")" == 2 ]] \
  || fail "cache-backed prefetch accessed the network"
rg -Fq '2 from cache, 0 fetched' "$TMP_ROOT/from-cache.out" \
  || fail "cache-backed prefetch did not report its source"

printf 'tampered\n' >"$SECOND_SOURCE/contrib/first.bin"
if "$PREFETCH" --config "$CONFIG" --source "$SECOND_SOURCE" \
    --cache-dir "$TEST_CACHE" >"$TMP_ROOT/tampered.out" 2>&1; then
  fail "prefetch overwrote a mismatched existing input"
fi
rg -Fq 'existing build input; refusing to overwrite it differs for contrib/first.bin' \
  "$TMP_ROOT/tampered.out" \
  || fail "mismatched existing input did not produce an actionable error"
[[ "$(cat "$SECOND_SOURCE/contrib/first.bin")" == tampered ]] \
  || fail "mismatched existing input was modified"

git clone -q "$TEST_SOURCE" "$BAD_SOURCE"
rm -rf -- "$BAD_SOURCE/contrib"
printf 'mirror changed\n' >"$FIXTURES/first.bin"
if "$PREFETCH" --config "$CONFIG" --source "$BAD_SOURCE" \
    >"$TMP_ROOT/bad-download.out" 2>&1; then
  fail "prefetch accepted a downloaded input with the wrong digest"
fi
rg -Fq 'downloaded build input differs for contrib/first.bin' \
  "$TMP_ROOT/bad-download.out" \
  || fail "download mismatch did not identify the affected input"
[[ ! -e "$BAD_SOURCE/contrib/first.bin" ]] \
  || fail "mismatched download was accepted into the source tree"

# The shared parser remains compatible with every immutable version-2 pin.
# Version 3 adds URL records, but its established four-column entry interface
# must not change for existing digest and source-state consumers.
# shellcheck source=../../scripts/pinned-ge-common.sh
source "$ROOT_DIR/scripts/pinned-ge-common.sh"
PIN_SOURCE_COMMIT="$source_commit"
V2_MANIFEST="$TMP_ROOT/v2.tsv"
{
  printf 'build_contrib_manifest_version\t2\n'
  printf 'source_commit\t%s\n' "$source_commit"
  printf 'entry\tcontrib/legacy.bin\t0644\t1\t%s\n' \
    "$(printf x | sha256sum | awk '{print $1}')"
} >"$V2_MANIFEST"
[[ "$(pinned_ge_build_contrib_entries "$V2_MANIFEST" | awk -F '\t' '{print NF}')" == 4 ]] \
  || fail "version-2 manifest compatibility changed"
if pinned_ge_build_contrib_fetch_entries "$V2_MANIFEST" \
    >"$TMP_ROOT/v2-fetch.out" 2>&1; then
  fail "version-2 manifest invented absent fetch metadata"
fi

MISSING_FETCH="$TMP_ROOT/missing-fetch.tsv"
{
  printf 'build_contrib_manifest_version\t3\n'
  printf 'source_commit\t%s\n' "$source_commit"
  printf 'entry\tcontrib/missing.bin\t0644\t1\t%s\n' \
    "$(printf x | sha256sum | awk '{print $1}')"
} >"$MISSING_FETCH"
if pinned_ge_build_contrib_entries "$MISSING_FETCH" \
    >"$TMP_ROOT/missing-fetch.out" 2>&1; then
  fail "version-3 manifest accepted an entry without a fetch record"
fi
rg -Fq 'requires one fetch record per entry' "$TMP_ROOT/missing-fetch.out" \
  || fail "missing version-3 fetch metadata was not explained"

printf 'prefetch_build_contrib passed\n'
