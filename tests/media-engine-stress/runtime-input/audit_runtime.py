#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Audit a copied Steam Runtime input and write deterministic provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


STORE_PATH_RE = re.compile(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+\Z")
PLATFORM_SUFFIX_RE = re.compile(r"[A-Za-z0-9._+-]+\Z")
METADATA_DIRECTORY = ".runtime-input"
RUNTIME_FAMILIES = {
    "sniper": "sniper_platform_",
    "steamrt4": "steamrt4_platform_",
}


class RuntimeRejected(ValueError):
    """The copied runtime does not meet the immutable-input contract."""


def reject(message: str) -> None:
    raise RuntimeRejected(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_store_source(path: Path) -> Path:
    raw = os.fspath(path)
    if STORE_PATH_RE.fullmatch(raw) is None:
        reject("runtime source is not a strict top-level Nix store path")
    if os.path.normpath(raw) != raw or os.path.realpath(raw) != raw:
        reject("runtime source is not a canonical Nix store path")
    try:
        source_mode = os.lstat(raw).st_mode
    except FileNotFoundError:
        reject("runtime source does not exist")
    if not stat.S_ISDIR(source_mode):
        reject("runtime source is not a directory")
    return Path(raw)


def validate_runtime_family(runtime_family: str) -> str:
    if not isinstance(runtime_family, str) or runtime_family not in RUNTIME_FAMILIES:
        supported = ", ".join(sorted(RUNTIME_FAMILIES))
        reject(f"unsupported runtime family; expected one of: {supported}")
    return RUNTIME_FAMILIES[runtime_family]


def is_family_platform(name: str, platform_prefix: str) -> bool:
    if not name.startswith(platform_prefix):
        return False
    return PLATFORM_SUFFIX_RE.fullmatch(name[len(platform_prefix) :]) is not None


def verify_executable(path: Path, relative: str) -> dict[str, object]:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        reject(f"required runtime executable is missing: {relative}")
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size == 0:
        reject(f"required runtime executable is not a nonempty regular file: {relative}")
    if stat.S_IMODE(path_stat.st_mode) & 0o111 == 0:
        reject(f"required runtime executable is not executable: {relative}")
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "size": path_stat.st_size,
    }


def platform_has_payload(platform: Path) -> bool:
    files = platform / "files"
    if not files.is_dir() or files.is_symlink():
        return False
    for directory, directory_names, file_names in os.walk(files, followlinks=False):
        directory_names.sort(key=lambda value: value.encode("utf-8"))
        file_names.sort(key=lambda value: value.encode("utf-8"))
        for file_name in file_names:
            path = Path(directory) / file_name
            path_stat = os.lstat(path)
            if stat.S_ISREG(path_stat.st_mode) and path_stat.st_size > 0:
                return True
    return False


def payload_tree_evidence(
    output: Path, *, exclude_generated_metadata: bool = False
) -> tuple[str, dict[str, int], int]:
    """Digest the Nix-preserved tree semantics in lexical UTF-8 order."""

    digest = hashlib.sha256()
    counts = {"directory": 0, "file": 0, "symlink": 0}
    regular_bytes = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal regular_bytes
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.encode("utf-8"))
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if exclude_generated_metadata and relative == METADATA_DIRECTORY:
                continue
            encoded = relative.encode("utf-8")
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                counts["directory"] += 1
                digest.update(b"D\0" + encoded + b"\n")
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(entry_stat.st_mode):
                counts["file"] += 1
                regular_bytes += entry_stat.st_size
                executable = b"1" if stat.S_IMODE(entry_stat.st_mode) & 0o111 else b"0"
                content_sha256 = sha256_file(Path(entry.path)).encode("ascii")
                digest.update(
                    b"F\0"
                    + encoded
                    + b"\0"
                    + executable
                    + b"\0"
                    + str(entry_stat.st_size).encode("ascii")
                    + b"\0"
                    + content_sha256
                    + b"\n"
                )
            elif stat.S_ISLNK(entry_stat.st_mode):
                counts["symlink"] += 1
                target = os.readlink(entry.path).encode("utf-8")
                digest.update(b"L\0" + encoded + b"\0" + target + b"\n")
            else:
                reject(f"runtime contains unsupported node type: {relative}")

    visit(output, "")
    return digest.hexdigest(), counts, regular_bytes


def audit_runtime(
    output: Path, source_store_path: str, runtime_family: str
) -> dict[str, object]:
    platform_prefix = validate_runtime_family(runtime_family)
    output = Path(output)
    if not output.is_dir() or output.is_symlink():
        reject("copied runtime output is not a directory")
    if os.path.lexists(output / "var"):
        reject("copied runtime contains excluded top-level var")
    if os.path.lexists(output / METADATA_DIRECTORY):
        reject("copied runtime collides with generated provenance directory")

    required_paths = (
        "_v2-entry-point",
        "run",
        "pressure-vessel/bin/pressure-vessel-wrap",
        "pressure-vessel/bin/pressure-vessel-unruntime",
    )
    required = [verify_executable(output / relative, relative) for relative in required_paths]

    pressure_vessel = output / "pressure-vessel"
    if not pressure_vessel.is_dir() or pressure_vessel.is_symlink():
        reject("pressure-vessel is not a real directory")
    platforms = sorted(
        (
            child
            for child in output.iterdir()
            if is_family_platform(child.name, platform_prefix)
            and child.is_dir()
            and not child.is_symlink()
        ),
        key=lambda path: path.name.encode("utf-8"),
    )
    payload_platforms = [platform.name for platform in platforms if platform_has_payload(platform)]
    if not payload_platforms:
        reject(f"runtime has no nonempty {platform_prefix}* files payload")

    tree_sha256, counts, regular_bytes = payload_tree_evidence(output)
    source_basename = Path(source_store_path).name
    source_name = source_basename.split("-", 1)[1]
    provenance: dict[str, object] = {
        "excluded_top_level": ["var"],
        "nonempty_platform_payloads": payload_platforms,
        "payload_tree_algorithm": "sha256(type\\0path\\0Nix-executable-bit/size/content-or-target\\n), lexical UTF-8 order, provenance excluded",
        "payload_tree_sha256": tree_sha256,
        "platform_prefix": platform_prefix,
        "provenance_version": 2,
        "required_executables": required,
        "runtime_family": runtime_family,
        # Do not embed the Nix store hash in the output: Nix would then retain
        # the excluded multi-GiB source as a runtime reference.  The derivation
        # binds that exact input; this digest and descriptive name identify it
        # without inflating the sanitized result's closure.
        "runtime_source_name": source_name,
        "runtime_source_path_sha256": hashlib.sha256(
            source_store_path.encode("utf-8")
        ).hexdigest(),
        "tree_counts": counts,
        "tree_regular_bytes": regular_bytes,
    }
    metadata = output / METADATA_DIRECTORY
    metadata.mkdir(mode=0o755)
    (metadata / "payload-tree.sha256").write_text(
        f"{tree_sha256}  payload\n", encoding="ascii"
    )
    (metadata / "provenance.json").write_text(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    for path in metadata.iterdir():
        os.chmod(path, 0o444)
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    os.chmod(metadata, 0o555)
    os.utime(metadata, ns=(1_000_000_000, 1_000_000_000))
    return provenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--runtime-family",
        choices=sorted(RUNTIME_FAMILIES),
        required=True,
        help="declared Steam Runtime family; selects the audited platform prefix",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        source = validate_store_source(arguments.source)
        provenance = audit_runtime(
            arguments.output, os.fspath(source), arguments.runtime_family
        )
    except RuntimeRejected as error:
        print(f"immutable runtime input rejected: {error}", file=sys.stderr)
        return 2
    print(
        "immutable runtime input accepted: "
        f"family={provenance['runtime_family']} "
        f"tree_sha256={provenance['payload_tree_sha256']} "
        f"regular_bytes={provenance['tree_regular_bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
