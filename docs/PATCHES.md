# Reading the patch series

This repository carries changes **on top of GE-Proton**, rather than a copy
of GE's complete Git history. The build obtains the upstream source named by
an immutable pin. Patches remain separate, reviewable diffs; the selected
series files, not directory order or filename numbers, define application
order. See [building](BUILDING.md) for the supported preparation workflow.

## Find the release inputs

Start with the [release notes](../RELEASE.md) and [source account](SOURCE.md)
for the checkout or release tag you are reviewing. For SE1, the selected pair
is:

- [Base/build pin](../config/ge-proton11-6-se1.env).
- [Ordered Wine series](../patches/series-ge-proton11-6-se1), containing 21
  patch paths relative to `patches/`.

The [source-build entrypoint](../scripts/build-se1.sh) explicitly selects
both files. The pin records the GE commit, component revisions, input hashes,
SDK and build options; it does not itself select the Wine series.

`patches/series` is a retained A3.21 selection, **not the SE1 release list**.
Other `series-*` files and candidate directories preserve earlier inputs for
comparison. Do not apply every patch in the repository or mix revisions of
the same numbered patch. Gaps in numbering and cross-directory references
are intentional; diagnostic patch 0019 comes last in SE1.

## Follow the changes

Open each path in the selected series and read its header for intent, then
its `diff --git` sections for the actual code changes. To print the SE1
application order and inspect one patch without applying anything, run from
the repository root:

```sh
awk 'NF && $1 !~ /^#/ {printf "%2d  %s\n", ++n, $0}' patches/series-ge-proton11-6-se1
git apply --stat -- patches/ge-proton11-6-a323/0026-mf-replenish-output-buffers-after-preroll.patch
```

The Wine series covers transport and source lifecycle, seeking and Media
Session demand, audio clocks, MediaEngine state, WineWayland VR extensions,
and diagnostics. It is reviewable, but not uniformly one fix per patch:
0004 bundles several loading, queue and seek behaviors and needs particular
care. Later patches can refine earlier behavior, so review the complete
selected series as well as individual diffs. A clean replay alone is not
proof of correctness or playback compatibility.

## Application boundaries

- [Source preparation](../scripts/prepare-pinned-ge-source.sh) inserts our
  Wine hook immediately after GE's `ge-video-rework` set. The rest of GE's
  preparation still follows its driver; this is not a series for bare Wine.
- The [Wine hook](../scripts/apply-pinned-rtsp-series.sh) first applies the
  pin's separate GE media-cleanup normalization, then checks and applies the
  selected Wine patches in order, with Git validation and zero GNU patch fuzz.
- [FFmpeg source patches](../patches/ffmpeg-security/README.md) have their
  own [series](../patches/ffmpeg-security/series), separate from Wine.
- [FFmpeg build integration](../patches/ffmpeg-build/README.md) has a separate
  [series](../patches/ffmpeg-build/series) affecting GE's `Makefile.in`.
  The selected pin supplies its release-specific expected postimage.
- Launcher behavior is a separate pin policy. SE1 uses GE's unchanged
  launcher; the retained launcher compatibility patch is not applied.

Use the pinned preparation tooling rather than invoking the Wine hook alone:
it requires the GE preparation state and writes the per-patch audit. For a
new base or changed patch, create new candidate inputs instead of rewriting
the pins and series that identify a previously built artifact.
