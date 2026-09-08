#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Hash an immutable store input without trusting its store-path label."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import struct
import sys


class DigestError(ValueError):
    pass


def _field(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def file_sha256(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise DigestError("input is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise DigestError("input is not a directory")
    digest = hashlib.sha256(b"rtsp-media-lab-tree-sha256-v1\0")
    entries: list[tuple[bytes, Path]] = [(b".", root)]
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort(key=os.fsencode)
        files.sort(key=os.fsencode)
        base = Path(directory)
        for name in [*names, *files]:
            path = base / name
            relative = os.fsencode(os.path.relpath(path, root))
            entries.append((relative, path))
    entries.sort(key=lambda item: item[0])

    seen: set[bytes] = set()
    for relative, path in entries:
        if relative in seen:
            raise DigestError("tree contains a duplicate relative path")
        seen.add(relative)
        info = path.lstat()
        _field(digest, relative)
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"d")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"x" if info.st_mode & stat.S_IXUSR else b"f")
            digest.update(struct.pack(">Q", info.st_size))
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        elif stat.S_ISLNK(info.st_mode):
            digest.update(b"l")
            _field(digest, os.fsencode(os.readlink(path)))
        else:
            raise DigestError("tree contains a special filesystem node")
    return digest.hexdigest()


def require_store_path(path: Path) -> None:
    text = os.fspath(path)
    if not text.startswith("/nix/store/") or text == "/nix/store/":
        raise DigestError("input must be an explicit /nix/store path")
    resolved = os.path.realpath(text)
    if not resolved.startswith("/nix/store/"):
        raise DigestError("input resolves outside /nix/store")


def require_matching_tree_copy(source: Path, copied: Path) -> str:
    source_digest = tree_sha256(source)
    copied_digest = tree_sha256(copied)
    if copied_digest != source_digest:
        raise DigestError("writable tree copy differs from its immutable source")
    return source_digest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--expect", required=True, choices=("file", "directory"))
    parser.add_argument("--compare-copy", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        require_store_path(args.path)
        if args.compare_copy is not None:
            if args.expect != "directory":
                raise DigestError("copy comparison requires a directory input")
            value = require_matching_tree_copy(args.path, args.compare_copy)
        else:
            value = file_sha256(args.path) if args.expect == "file" else tree_sha256(args.path)
    except (DigestError, OSError) as error:
        print(f"store input digest: {error}", file=sys.stderr)
        return 2
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
