#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail
umask 022
unset BASH_ENV ENV TAR_OPTIONS POSIXLY_CORRECT SIMPLE_BACKUP_SUFFIX VERSION_CONTROL
export LC_ALL=C

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$ROOT_DIR/sources/ge-proton11}"
EXPECTED_TAG="${EXPECTED_TAG:-GE-Proton11-3}"
OUTPUT=""
PRINT_DRIVER_REVIEW_CANDIDATE=0
export GIT_NO_LAZY_FETCH=1

usage() {
  cat <<'EOF'
Usage: materialize-ge-wine.sh [OPTIONS]

Replay GE's complete, reviewed Wine patch section in isolated source worktrees
without building Proton. The exact release checkout and its Wine/Wine-Staging
pins must already be available locally.

Options:
  --source DIR                  GE Proton checkout
  --tag TAG                     exact release tag (default: GE-Proton11-3)
  --output DIR                  new output root (default: work/ge-wine-TAG)
  --print-driver-review-candidate
                                print identity; do not execute the driver
  -h, --help                    show this help

The command never substitutes master, initializes submodules, deletes an
existing output, or starts a Proton build. It refuses to execute an unreviewed
patch-driver digest. Driver review must confirm that the extracted Wine section
performs source preparation only and contains no retrieval or compilation step.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || fail "--source requires a directory"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 && -n "$2" ]] || fail "--tag requires a value"
      EXPECTED_TAG="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 && -n "$2" ]] || fail "--output requires a directory"
      OUTPUT="$2"
      shift 2
      ;;
    --print-driver-review-candidate)
      PRINT_DRIVER_REVIEW_CANDIDATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

SAFE_TAG="${EXPECTED_TAG//\//_}"
[[ -n "$OUTPUT" ]] || OUTPUT="$ROOT_DIR/work/ge-wine-$SAFE_TAG"
OUTPUT="$(realpath -m -- "$OUTPUT")"
MANIFEST="$ROOT_DIR/release-manifests/$SAFE_TAG.manifest.tsv"
DRIVER_APPROVAL="$ROOT_DIR/release-approvals/$SAFE_TAG.wine-driver.tsv"
NORMALIZATION_APPROVAL="$ROOT_DIR/release-approvals/$SAFE_TAG.wine-normalizations.tsv"

require_committed_project_file() {
  local absolute_path="$1"
  local description="$2"
  local relative_path
  local head_blob working_blob

  [[ "$absolute_path" == "$ROOT_DIR/"* && -f "$absolute_path" \
      && ! -L "$absolute_path" ]] \
    || fail "$description is not a regular file at its canonical project path: $absolute_path"
  relative_path="${absolute_path#"$ROOT_DIR/"}"
  head_blob="$(git -C "$ROOT_DIR" rev-parse --verify "HEAD:$relative_path" 2>/dev/null)" \
    || fail "$description is not committed in project HEAD: $relative_path"
  working_blob="$(git -C "$ROOT_DIR" hash-object --no-filters "$absolute_path")"
  [[ "$working_blob" == "$head_blob" ]] \
    || fail "$description bytes differ from project HEAD: $relative_path"
  git -C "$ROOT_DIR" diff --quiet HEAD -- "$relative_path" \
    || fail "$description differs from the committed project HEAD: $relative_path"
  git -C "$ROOT_DIR" diff --cached --quiet HEAD -- "$relative_path" \
    || fail "$description has a staged change relative to project HEAD: $relative_path"
}

"$ROOT_DIR/scripts/check-ge-release.sh" \
  --source "$SOURCE_DIR" --tag "$EXPECTED_TAG" --quiet

ROOT_COMMIT="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  rev-parse --verify "refs/tags/$EXPECTED_TAG^{commit}")"
SOURCE_HEAD="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
[[ "$SOURCE_HEAD" == "$ROOT_COMMIT" ]] \
  || fail "GE source checkout must be detached at $EXPECTED_TAG ($ROOT_COMMIT)"
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || fail "GE source checkout is dirty"

require_committed_project_file "$MANIFEST" "accepted release manifest"
TEMP_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/ge-manifest-check.XXXXXX")"
TEMP_DRIVER_APPROVAL="$(mktemp "${TMPDIR:-/tmp}/ge-driver-approval.XXXXXX")"
cleanup() {
  rm -f "$TEMP_MANIFEST" "$TEMP_DRIVER_APPROVAL"
}
trap cleanup EXIT

"$ROOT_DIR/scripts/record-release-manifest.sh" \
  --source "$SOURCE_DIR" --tag "$EXPECTED_TAG" --output - >"$TEMP_MANIFEST"
cmp -s "$MANIFEST" "$TEMP_MANIFEST" \
  || fail "accepted release manifest does not match the selected tag"

read_gitlink() {
  local path="$1"
  local record
  record="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
    ls-tree "$ROOT_COMMIT" -- "$path")"
  [[ -n "$record" ]] || fail "release has no $path gitlink"
  awk '$1 == "160000" && $2 == "commit" {print $3}' <<<"$record"
}

WINE_COMMIT="$(read_gitlink wine)"
STAGING_COMMIT="$(read_gitlink wine-staging)"
[[ -n "$WINE_COMMIT" && -n "$STAGING_COMMIT" ]] \
  || fail "could not resolve Wine source pins"

for path in wine wine-staging; do
  [[ -e "$SOURCE_DIR/$path/.git" ]] \
    || fail "$path is not initialized at the release checkout"
done
[[ "$(git -C "$SOURCE_DIR/wine" rev-parse HEAD)" == "$WINE_COMMIT" ]] \
  || fail "Wine checkout does not match release gitlink $WINE_COMMIT"
[[ "$(git -C "$SOURCE_DIR/wine-staging" rev-parse HEAD)" == "$STAGING_COMMIT" ]] \
  || fail "Wine-Staging checkout does not match release gitlink $STAGING_COMMIT"

REVERT_COMMIT="e813ca5771658b00875924ab88d525322e50d39f"
git -C "$SOURCE_DIR/wine" cat-file -e "$REVERT_COMMIT^" 2>/dev/null \
  || fail "Wine object $REVERT_COMMIT and its parent are missing; fetch them explicitly before source replay"

DRIVER_PATH="patches/protonprep-valve-staging.sh"
DRIVER_RECORD="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  ls-tree "$ROOT_COMMIT" -- "$DRIVER_PATH")"
read -r _ DRIVER_TYPE DRIVER_BLOB _ <<<"$DRIVER_RECORD"
[[ "$DRIVER_TYPE" == blob && -n "$DRIVER_BLOB" ]] \
  || fail "accepted release has no tracked GE patch driver"
DRIVER_SHA256="$(GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" \
  cat-file blob "$DRIVER_BLOB" | sha256sum | awk '{print $1}')"
MANIFEST_DRIVER_SHA="$(awk -F '\t' -v path="$DRIVER_PATH" \
  '$1 == "source_file" && $2 == path {print $4}' "$MANIFEST")"
[[ "$MANIFEST_DRIVER_SHA" == "$DRIVER_SHA256" ]] \
  || fail "release manifest does not lock the GE patch-driver content"

{
  printf 'driver_approval_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'patch_driver_blob\t%s\n' "$DRIVER_BLOB"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
} >"$TEMP_DRIVER_APPROVAL"

if [[ $PRINT_DRIVER_REVIEW_CANDIDATE -eq 1 ]]; then
  cat "$TEMP_DRIVER_APPROVAL"
  exit 0
fi

if [[ ! -f "$DRIVER_APPROVAL" ]] \
    || ! cmp -s "$DRIVER_APPROVAL" "$TEMP_DRIVER_APPROVAL"; then
  printf 'The exact patch driver must be reviewed before execution.\n' >&2
  printf 'After review, the required approval file is %s with contents:\n' \
    "$DRIVER_APPROVAL" >&2
  cat "$TEMP_DRIVER_APPROVAL" >&2
  fail "missing or mismatched patch-driver approval"
fi
require_committed_project_file "$DRIVER_APPROVAL" \
  "reviewed patch-driver approval"

[[ ! -e "$OUTPUT" ]] || fail "refusing to replace existing output: $OUTPUT"
mkdir -p "$OUTPUT"

git -C "$SOURCE_DIR/wine" worktree add --detach "$OUTPUT/wine" "$WINE_COMMIT"
git -C "$SOURCE_DIR/wine-staging" \
  worktree add --detach "$OUTPUT/wine-staging" "$STAGING_COMMIT"

RELEASE_ARCHIVE="$OUTPUT/release-inputs.tar"
GIT_NO_LAZY_FETCH=1 git -C "$SOURCE_DIR" archive \
  -o "$RELEASE_ARCHIVE" "$ROOT_COMMIT" wineopenxr patches
tar -xf "$RELEASE_ARCHIVE" -C "$OUTPUT"
rm -f "$RELEASE_ARCHIVE"

PATCH_DRIVER="$OUTPUT/$DRIVER_PATH"
[[ "$(sha256sum "$PATCH_DRIVER" | awk '{print $1}')" == "$DRIVER_SHA256" ]] \
  || fail "exported patch driver does not match its approved Git blob"

OPENXR_LOG="$OUTPUT/wineopenxr-patch.log"
(
  cd "$OUTPUT/wineopenxr"
  shopt -s nullglob
  patches=("$OUTPUT"/patches/wineopenxr/*.patch)
  [[ ${#patches[@]} -gt 0 ]] || fail "release contains no WineOpenXR patches"
  for patch_file in "${patches[@]}"; do
    LC_ALL=C patch -Np1 --batch <"$patch_file"
  done
) >"$OPENXR_LOG" 2>&1

EXTRACT_COUNT="$(awk '
  /^### \(2\) WINE PATCHING ###/ {capture=1}
  capture {count++}
  /^### END WINE PATCHING ###/ {found=1; exit}
  END {if (!found) exit 1; print count}
' "$PATCH_DRIVER")" || fail "could not locate GE Wine patch section"
[[ "$EXTRACT_COUNT" -gt 20 ]] || fail "GE Wine patch section is unexpectedly short"

EXTRACTED_DRIVER="$OUTPUT/reviewed-wine-section.sh"
{
  cat <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

apply_patch() {
    local patch_path="$1"
    if ! patch -Np1 --batch < "$patch_path"; then
        printf 'MATERIALIZER_PATCH_FAILURE\t%s\n' "$patch_path" >&2
    fi
    return 0
}

apply_all_in_dir() {
    local dir="${1%/}"
    local patch_file
    local -a patch_files=()
    shopt -s nullglob
    patch_files=("$dir"/*.patch)
    shopt -u nullglob
    ((${#patch_files[@]} > 0)) || {
        printf 'ERROR: patch directory is empty: %s\n' "$dir" >&2
        return 1
    }
    for patch_file in "${patch_files[@]}"; do
        apply_patch "$patch_file"
    done
}
EOF
  awk '
    /^### \(2\) WINE PATCHING ###/ {capture=1}
    capture {
      if ($0 ~ /^[[:space:]]*\.\/tools\/make_requests[[:space:]]*$/)
        sub(/\.\/tools\/make_requests/, "perl ./tools/make_requests")
      print
    }
    /^### END WINE PATCHING ###/ {found=1; exit}
    END {if (!found) exit 1}
  ' "$PATCH_DRIVER"
} >"$EXTRACTED_DRIVER"
chmod 0555 "$EXTRACTED_DRIVER"

PATCH_LOG="$OUTPUT/wine-patch-replay.log"
if ! (cd "$OUTPUT" && LC_ALL=C bash "$EXTRACTED_DRIVER") \
    >"$PATCH_LOG" 2>&1; then
  fail "reviewed GE Wine patch section did not complete; inspect $PATCH_LOG"
fi

NORMALIZATION_LOG="$OUTPUT/normalized-rejects.tsv"
NORMALIZATION_CANDIDATE="$OUTPUT/normalization-candidates.tsv"
: >"$NORMALIZATION_LOG"

normalize_stale_gstreamer_deletions() {
  local wine_tree="$OUTPUT/wine"
  local first='dlls/winegstreamer/media-converter/videoconv.c'
  local second='dlls/winegstreamer/wg_parser.c'
  local third='dlls/winegstreamer/main.c'
  local first_reject="$wine_tree/$first.rej"
  local second_reject="$wine_tree/$second.rej"
  local source_path reject_path source_blob source_sha reject_sha patch_sha
  local missing_body_count=0
  local normalization_patch="$OUTPUT/patches/ge-video-rework/0001-winedmo-remove-winegstreamer-media-backend.patch"

  {
    printf 'normalization_approval_version\t1\n'
    printf 'tag\t%s\n' "$EXPECTED_TAG"
    printf 'root_commit\t%s\n' "$ROOT_COMMIT"
    printf 'wine_commit\t%s\n' "$WINE_COMMIT"
    printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
  } >"$NORMALIZATION_CANDIDATE"

  [[ -f "$normalization_patch" ]] \
    || fail "release lacks the exact stale-deletion patch expected by the normalizer"
  patch_sha="$(sha256sum "$normalization_patch" | awk '{print $1}')"

  if [[ -f "$first_reject" || -f "$second_reject" ]]; then
    [[ -f "$first_reject" && -f "$second_reject" ]] \
      || fail "only one member of the known stale WineGStreamer deletion pair was rejected"

    for source_path in "$first" "$second"; do
      reject_path="$source_path.rej"
      [[ -f "$wine_tree/$source_path" ]] \
        || fail "known stale deletion target is already absent: $source_path"
      git -C "$wine_tree" diff --quiet HEAD -- "$source_path" \
        || fail "refusing to normalize a modified WineGStreamer source: $source_path"

      awk -v path="$source_path" '
        NR == 1 {if ($0 != "--- " path) exit 1; next}
        NR == 2 {if ($0 != "+++ " path) exit 1; next}
        NR == 3 {if ($0 !~ /^@@ -1,[0-9]+ \+0,0 @@$/) exit 1; next}
        NR > 3 {if (substr($0, 1, 1) != "-") exit 1; body++}
        END {if (NR < 4 || !body) exit 1}
      ' "$wine_tree/$reject_path" \
        || fail "reject is not a complete stale deletion: $reject_path"

      source_blob="$(git -C "$wine_tree" rev-parse "HEAD:$source_path")"
      source_sha="$(sha256sum "$wine_tree/$source_path" | awk '{print $1}')"
      reject_sha="$(sha256sum "$wine_tree/$reject_path" | awk '{print $1}')"
      printf 'deleted_backend_file\t%s\t%s\t%s\t%s\t%s\n' \
        "$source_path" "$source_blob" "$source_sha" "$reject_sha" "$patch_sha" \
        >>"$NORMALIZATION_CANDIDATE"
    done
  else
    for source_path in "$first" "$second" "$third"; do
      [[ ! -f "$wine_tree/$source_path" ]] \
        || missing_body_count=$((missing_body_count + 1))
    done
    [[ "$missing_body_count" -ne 0 ]] || return 0
    [[ "$missing_body_count" -eq 3 ]] \
      || fail "only part of the known bodyless WineGStreamer deletion trio survived"

    for source_path in "$first" "$second" "$third"; do
      git -C "$wine_tree" diff --quiet HEAD -- "$source_path" \
        || fail "refusing to normalize a modified WineGStreamer source: $source_path"
      if rg -Fq \
          -e "diff --git a/$source_path " \
          -e " b/$source_path" \
          -e "--- a/$source_path" \
          -e "+++ b/$source_path" \
          -- "$normalization_patch"; then
        fail "release patch unexpectedly contains a diff body for $source_path"
      fi

      source_blob="$(git -C "$wine_tree" rev-parse "HEAD:$source_path")"
      source_sha="$(sha256sum "$wine_tree/$source_path" | awk '{print $1}')"
      printf 'missing_deletion_body\t%s\t%s\t%s\t%s\n' \
        "$source_path" "$source_blob" "$source_sha" "$patch_sha" \
        >>"$NORMALIZATION_CANDIDATE"
    done
  fi

  if [[ ! -f "$NORMALIZATION_APPROVAL" ]] \
      || ! cmp -s "$NORMALIZATION_APPROVAL" "$NORMALIZATION_CANDIDATE"; then
    fail "stale deletion tuples are not approved; review $NORMALIZATION_CANDIDATE and create $NORMALIZATION_APPROVAL before a fresh replay"
  fi
  require_committed_project_file "$NORMALIZATION_APPROVAL" \
    "reviewed normalization approval"

  awk -F '\t' \
    '$1 == "deleted_backend_file" || $1 == "missing_deletion_body"' \
    "$NORMALIZATION_CANDIDATE" \
    >"$NORMALIZATION_LOG"
  while IFS=$'\t' read -r _ source_path _; do
    [[ -n "$source_path" ]] || continue
    reject_path="$source_path.rej"
    git -C "$wine_tree" rm -f -- "$source_path" >/dev/null
    git -C "$wine_tree" clean -f -- "$reject_path" >/dev/null
  done <"$NORMALIZATION_LOG"
}

normalize_stale_gstreamer_deletions

REJECT_NORMALIZATION_COUNT="$(awk -F '\t' \
  '$1 == "deleted_backend_file" {count++} END {print count + 0}' \
  "$NORMALIZATION_LOG")"
MISSING_BODY_NORMALIZATION_COUNT="$(awk -F '\t' \
  '$1 == "missing_deletion_body" {count++} END {print count + 0}' \
  "$NORMALIZATION_LOG")"
NORMALIZATION_COUNT="$((REJECT_NORMALIZATION_COUNT + MISSING_BODY_NORMALIZATION_COUNT))"
REPORTED_REJECT_COUNT="$(awk '/saving rejects to file / {count++} END {print count + 0}' "$PATCH_LOG")"
[[ "$REPORTED_REJECT_COUNT" -eq "$REJECT_NORMALIZATION_COUNT" ]] \
  || fail "patch log reports $REPORTED_REJECT_COUNT rejected files but only $REJECT_NORMALIZATION_COUNT approved reject-backed deletions were normalized"

mapfile -t PATCH_FAILURES < <(
  awk -F '\t' '$1 == "MATERIALIZER_PATCH_FAILURE" {print $2}' "$PATCH_LOG"
)
case "$REJECT_NORMALIZATION_COUNT:$MISSING_BODY_NORMALIZATION_COUNT" in
  0:0|0:3)
    [[ ${#PATCH_FAILURES[@]} -eq 0 ]] \
      || fail "a patch command failed without an approved reject-backed normalization"
    ;;
  2:0)
    [[ ${#PATCH_FAILURES[@]} -eq 1 \
        && "${PATCH_FAILURES[0]}" == '../patches/ge-video-rework/0001-winedmo-remove-winegstreamer-media-backend.patch' ]] \
      || fail "patch failures are not limited to the approved WineGStreamer deletion patch"
    ;;
  *)
    fail "unexpected approved normalization shape: rejects=$REJECT_NORMALIZATION_COUNT missing_bodies=$MISSING_BODY_NORMALIZATION_COUNT"
    ;;
esac

if rg -n 'malformed patch|can.t find file to patch|bad interpreter|command not found|: No such file or directory|(^|[[:space:]])(fatal|error):|Traceback \(most recent call last\)|cannot stat|cannot open' \
    "$PATCH_LOG"; then
  fail "GE Wine patch replay reported an error or missing command"
fi

"$ROOT_DIR/scripts/audit-patched-wine.sh" --wine-tree "$OUTPUT/wine" \
  >"$OUTPUT/wine-source-audit.tsv"

BACKUP_COUNT="$(
  find "$OUTPUT/wine" -path '*/.git' -prune -o -type f -name '*.orig' -print \
    | wc -l
)"
find "$OUTPUT/wine" -path '*/.git' -prune -o -type f -name '*.orig' \
  -printf '%P\n' | LC_ALL=C sort >"$OUTPUT/patch-backup-paths.txt"

TREE_MANIFEST="$OUTPUT/wine-tree.manifest.tsv"
"$ROOT_DIR/scripts/hash-source-tree.sh" --tree "$OUTPUT/wine" \
  >"$TREE_MANIFEST"
TREE_MANIFEST_SHA256="$(sha256sum "$TREE_MANIFEST" | awk '{print $1}')"
SOURCE_AUDIT_SHA256="$(sha256sum "$OUTPUT/wine-source-audit.tsv" | awk '{print $1}')"

{
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'wine_commit\t%s\n' "$WINE_COMMIT"
  printf 'wine_staging_commit\t%s\n' "$STAGING_COMMIT"
  printf 'patch_driver_blob\t%s\n' "$DRIVER_BLOB"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
  printf 'extracted_driver_sha256\t%s\n' "$(sha256sum "$EXTRACTED_DRIVER" | awk '{print $1}')"
  printf 'wine_tree_manifest_sha256\t%s\n' "$TREE_MANIFEST_SHA256"
  printf 'patch_backup_file_count\t%s\n' "$BACKUP_COUNT"
  printf 'audited_normalization_count\t%s\n' "$NORMALIZATION_COUNT"
} >"$OUTPUT/source-identity.tsv"

{
  printf 'materialization_approval_version\t1\n'
  printf 'tag\t%s\n' "$EXPECTED_TAG"
  printf 'root_commit\t%s\n' "$ROOT_COMMIT"
  printf 'wine_commit\t%s\n' "$WINE_COMMIT"
  printf 'wine_staging_commit\t%s\n' "$STAGING_COMMIT"
  printf 'patch_driver_sha256\t%s\n' "$DRIVER_SHA256"
  printf 'wine_tree_manifest_sha256\t%s\n' "$TREE_MANIFEST_SHA256"
  printf 'wine_source_audit_sha256\t%s\n' "$SOURCE_AUDIT_SHA256"
  printf 'audited_normalization_count\t%s\n' "$NORMALIZATION_COUNT"
} >"$OUTPUT/wine-tree-approval-candidate.tsv"

printf 'Materialized source-only GE Wine tree: %s\n' "$OUTPUT/wine"
printf 'Patch replay log: %s\n' "$PATCH_LOG"
printf 'Locked tree manifest: %s\n' "$TREE_MANIFEST"
printf 'Review/commit approval candidate: %s\n' \
  "$OUTPUT/wine-tree-approval-candidate.tsv"
printf 'No Proton build was started.\n'
