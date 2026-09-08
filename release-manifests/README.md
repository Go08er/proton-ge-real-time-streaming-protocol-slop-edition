# Deterministic release manifests

`record-release-manifest.sh` writes the accepted GE tag's immutable root,
gitlink, and reviewed media/XR patch identities here. A GE-Proton11-3 manifest
is checked in only after the official tag and every recorded object are
available and reviewed.

Generated manifests contain no timestamps, local paths, remote URLs, branches,
or host information. An existing differing manifest is never overwritten. The
source materializer accepts a release manifest only after that canonical file
is committed unchanged in the project's current `HEAD`.
