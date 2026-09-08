# Release-specific source approvals

This directory is a trust boundary, not generated scratch space. Add an
approval for GE-Proton11-3 only after the exact official tag exists and the
named content has been reviewed.

Three tab-separated approvals can be required for a release:

1. `GE-Proton11-3.wine-driver.tsv` locks the tag, root commit, patch-driver Git
   blob, and SHA-256. Create it only after reviewing the complete driver and
   confirming that its extracted Wine section performs source preparation
   only: no network retrieval, compilation, installation, packaging, or host
   mutation.
2. `GE-Proton11-3.wine-normalizations.tsv` is needed only if the replay finds
   either the known pair of stale whole-file WineGStreamer deletion rejects or
   the known three unchanged backend files whose deletion bodies are absent
   from GE's tracked transition patch. The failed replay writes
   `normalization-candidates.tsv`. Review the full source/reject bodies and
   upstream intent before copying that exact, release-scoped tuple here. Any
   partial set, modified source, present-but-unapplied diff body, or other
   reject remains a blocker. Run discovery in a separate output so the
   committed approval is consumed only by a fresh final replay.
3. `GE-Proton11-3.wine-tree.tsv` accepts the complete materialized Wine tree.
   After source replay and manual review, copy the exact
   `wine-tree-approval-candidate.tsv` here. Its normalized archive hash and
   inventory cover every directory, regular file, symlink, mode, generated
   file, deletion, and patch backup in the Wine worktree.
   `verify-baseline.sh` refuses a tree which differs.

These files are intentionally absent while the project waits for the release.
They should be committed with the deterministic release manifest and review
notes. Never invent hashes from moving `master` or reuse an approval from a
different tag, Wine pin, or replay attempt.

The materializer and baseline verifier accept only these canonical filenames,
require them to exist in the project's current `HEAD`, and reject staged or
working-tree changes. Command-line or environment paths cannot redirect an
approval to a generated candidate or another untrusted file.
