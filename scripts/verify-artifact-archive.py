#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Fail-closed comparison of a Proton tarball with its verified directory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import sys
import tarfile


class VerificationError(Exception):
    pass


@dataclass(frozen=True)
class Node:
    path: Path
    kind: str
    mode: int
    size: int
    link_target: str | None
    device: int
    inode: int


def fail(message: str) -> None:
    raise VerificationError(message)


def node_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def require_safe_mode(label: str, kind: str, mode: int) -> None:
    permissions = stat.S_IMODE(mode)
    if kind == "symlink":
        return
    if permissions & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        fail(f"{label} has set-id/sticky mode {permissions:#06o}")
    if permissions & stat.S_IWOTH:
        fail(f"{label} is world-writable ({permissions:#06o})")


def require_safe_symlink(path: Path, root: Path, relative: str, target: str) -> None:
    if not target:
        fail(f"empty symlink target: {relative}")
    if os.path.isabs(target):
        fail(f"absolute symlink: {relative} -> {target}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        fail(f"broken or cyclic symlink: {relative} -> {target} ({error})")
    try:
        resolved.relative_to(root)
    except ValueError:
        fail(f"out-of-root symlink: {relative} -> {target}")


def inventory_tree(root: Path, root_name: str) -> dict[str, Node]:
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        fail(f"artifact directory cannot be resolved: {error}")
    if root.name != root_name:
        fail(f"artifact directory name is {root.name!r}, expected {root_name!r}")

    nodes: dict[str, Node] = {}
    stack = [(root, root_name)]
    while stack:
        path, archive_name = stack.pop()
        try:
            metadata = path.lstat()
        except OSError as error:
            fail(f"cannot stat artifact node {archive_name}: {error}")
        kind = node_kind(metadata.st_mode)
        if kind == "special":
            fail(f"special node in artifact directory: {archive_name}")
        require_safe_mode(archive_name, kind, metadata.st_mode)
        target = None
        if kind == "symlink":
            try:
                target = os.readlink(path)
            except OSError as error:
                fail(f"cannot read symlink {archive_name}: {error}")
            require_safe_symlink(path, root, archive_name, target)
        nodes[archive_name] = Node(
            path=path,
            kind=kind,
            mode=stat.S_IMODE(metadata.st_mode),
            size=metadata.st_size if kind == "regular file" else 0,
            link_target=target,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
        if kind != "directory":
            continue
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError as error:
            fail(f"cannot enumerate artifact directory {archive_name}: {error}")
        for entry in reversed(entries):
            child_name = f"{archive_name}/{entry.name}"
            stack.append((Path(entry.path), child_name))
    return nodes


def canonical_member_name(name: str, root_name: str) -> str:
    if not name or name.startswith("/"):
        fail(f"absolute or empty archive path: {name!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        fail(f"control character in archive path: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail(f"non-canonical archive path: {name!r}")
    if PurePosixPath(name).is_absolute() or posixpath.normpath(name) != name:
        fail(f"unsafe archive path: {name!r}")
    if name != root_name and not name.startswith(f"{root_name}/"):
        fail(f"archive path escapes named root: {name!r}")
    return name


def tar_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isfile():
        return "regular file"
    if member.issym():
        return "symlink"
    fail(f"unsupported archive node type {member.type!r}: {member.name}")
    raise AssertionError("unreachable")


def compare_regular_payload(
    archive: tarfile.TarFile, member: tarfile.TarInfo, expected: Node
) -> None:
    payload = archive.extractfile(member)
    if payload is None:
        fail(f"regular archive member has no payload: {member.name}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(expected.path, flags)
    except OSError as error:
        fail(f"cannot open artifact file {member.name}: {error}")
    with payload, os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (
            opened.st_dev != expected.device
            or opened.st_ino != expected.inode
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != expected.mode
            or opened.st_size != expected.size
        ):
            fail(f"artifact file changed during archive verification: {member.name}")
        offset = 0
        while True:
            archive_chunk = payload.read(1024 * 1024)
            source_chunk = source.read(1024 * 1024)
            if archive_chunk != source_chunk:
                fail(f"archive payload differs from artifact file at {member.name}:{offset}")
            if not archive_chunk:
                break
            offset += len(archive_chunk)


def compare_archive(archive_path: Path, root: Path, root_name: str) -> tuple[int, int]:
    expected_nodes = inventory_tree(root, root_name)
    seen: set[str] = set()
    regular_files = 0
    try:
        with archive_path.open("rb") as raw_archive, gzip.GzipFile(
            fileobj=raw_archive, mode="rb"
        ) as gzip_stream, tarfile.open(fileobj=gzip_stream, mode="r|") as archive:
            for member in archive:
                name = canonical_member_name(member.name, root_name)
                if name in seen:
                    fail(f"duplicate archive member: {name}")
                seen.add(name)
                if member.pax_headers:
                    fail(f"unexpected PAX metadata on archive member: {name}")
                if member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                    fail(f"archive ownership is not normalized to numeric root: {name}")
                if member.sparse:
                    fail(f"sparse archive member is not allowed: {name}")

                expected = expected_nodes.get(name)
                if expected is None:
                    fail(f"archive contains a node absent from artifact directory: {name}")
                kind = tar_kind(member)
                permissions = member.mode & 0o7777
                require_safe_mode(name, kind, permissions)
                if kind != expected.kind:
                    fail(
                        f"archive type differs at {name}: {kind}, expected {expected.kind}"
                    )
                if permissions != expected.mode:
                    fail(
                        f"archive mode differs at {name}: {permissions:#06o}, "
                        f"expected {expected.mode:#06o}"
                    )

                if kind == "regular file":
                    if member.size != expected.size:
                        fail(
                            f"archive size differs at {name}: {member.size}, "
                            f"expected {expected.size}"
                        )
                    compare_regular_payload(archive, member, expected)
                    regular_files += 1
                elif kind == "symlink":
                    target = member.linkname
                    if target != expected.link_target:
                        fail(
                            f"archive symlink differs at {name}: {target!r}, "
                            f"expected {expected.link_target!r}"
                        )
                    if target.startswith("/"):
                        fail(f"absolute archive symlink: {name} -> {target}")
                    lexical_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), target)
                    )
                    if lexical_target != root_name and not lexical_target.startswith(
                        f"{root_name}/"
                    ):
                        fail(f"out-of-root archive symlink: {name} -> {target}")
                elif member.size != 0:
                    fail(f"non-file archive member has a payload size: {name}")

            # TarFile stops at the end marker. Drain the decompressed gzip
            # stream so CRC/truncation failures and appended non-zero payloads
            # cannot hide after an otherwise valid first tar archive.
            trailing_size = 0
            while trailing := archive.fileobj.read(1024 * 1024):
                trailing_size += len(trailing)
                if trailing_size > 1024 * 1024:
                    fail("archive has excessive zero padding after its tar end marker")
                if trailing.strip(b"\0"):
                    fail("archive has non-zero payload after its tar end marker")
            if archive.pax_headers:
                fail("unexpected global PAX metadata in archive")
    except (OSError, tarfile.TarError) as error:
        fail(f"archive parsing failed: {error}")

    missing = sorted(set(expected_nodes) - seen)
    if missing:
        preview = ", ".join(missing[:5])
        fail(f"archive omits {len(missing)} artifact nodes (first: {preview})")
    if not seen:
        fail("archive contains no payload nodes")
    return len(seen), regular_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="validate a Proton tarball and compare it to its artifact directory"
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--tool-dir", required=True, type=Path)
    parser.add_argument("--root-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.root_name or "/" in args.root_name or args.root_name in (".", ".."):
        print("ERROR: archive root name is not a single safe path component", file=sys.stderr)
        return 1
    try:
        node_count, regular_count = compare_archive(
            args.archive, args.tool_dir, args.root_name
        )
    except VerificationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        f"Archive matches artifact tree exactly: {node_count} nodes, "
        f"{regular_count} regular files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
