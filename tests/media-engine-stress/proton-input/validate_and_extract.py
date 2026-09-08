#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Validate and reproducibly unpack one immutable Proton tool archive.

The command-line interface is intentionally strict: production inputs must be
regular files/directories already in /nix/store.  Archive extraction is done
without ``TarFile.extract*`` so a validated archive cannot ask tarfile to
follow a path it created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
LICENSE_BASENAME_RE = re.compile(
    r"(?:LICENSE|LICENCE|COPYING|NOTICE)(?:[._-].*)?\Z", re.IGNORECASE
)
RESERVED_COMPONENT = ".proton-input"
MAX_DISPLAY_NAME_BYTES = 256
MAX_MEMBERS = 1_000_000
MAX_MEMBER_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_LINK_BYTES = 4096
MAX_TOTAL_REGULAR_BYTES = 64 * 1024 * 1024 * 1024
NORMALIZED_MTIME_NS = 1_000_000_000


class ArchiveRejected(ValueError):
    """The input violates the immutable Proton archive contract."""


@dataclass(frozen=True)
class MemberPlan:
    name: str
    relative: str
    kind: str
    mode: int
    size: int
    linkname: str


@dataclass(frozen=True)
class ArchivePlan:
    members: tuple[MemberPlan, ...]
    root_mode: int
    total_regular_bytes: int
    member_manifest_sha256: str


@dataclass(frozen=True)
class VdfObject:
    entries: tuple[tuple[str, str | VdfObject], ...]


def _reject(message: str) -> None:
    raise ArchiveRejected(message)


def validate_identity(identity: str) -> str:
    if not isinstance(identity, str) or IDENTITY_RE.fullmatch(identity) is None:
        _reject(
            "top-level identity must be 1-128 ASCII characters and match "
            "[A-Za-z0-9][A-Za-z0-9._+-]*"
        )
    if identity in {".", "..", RESERVED_COMPONENT}:
        _reject("reserved top-level identity")
    return identity


def validate_sha256(expected: str) -> str:
    if not isinstance(expected, str) or SHA256_RE.fullmatch(expected) is None:
        _reject("archive SHA-256 must be exactly 64 lowercase hexadecimal characters")
    return expected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_store_path(path: Path, *, directory: bool) -> Path:
    raw = os.fspath(path)
    if not raw.startswith("/nix/store/"):
        _reject(f"production input is not beneath /nix/store: {raw!r}")
    if os.path.normpath(raw) != raw or os.path.realpath(raw) != raw:
        _reject(f"production input is not a canonical immutable store path: {raw!r}")
    try:
        mode = os.lstat(raw).st_mode
    except FileNotFoundError:
        _reject(f"production input does not exist: {raw!r}")
    expected_type = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected_type:
        _reject(
            f"production input must be a {'directory' if directory else 'regular file'}: "
            f"{raw!r}"
        )
    return Path(raw)


def validate_store_archive(path: Path) -> Path:
    return _validate_store_path(path, directory=False)


def validate_store_nixpkgs(path: Path) -> Path:
    result = _validate_store_path(path, directory=True)
    if not (result / "default.nix").is_file():
        _reject("pinned nixpkgs source does not contain default.nix")
    return result


def _validate_text(value: str, *, what: str, byte_limit: int) -> None:
    if not value:
        _reject(f"{what} is empty")
    if value != unicodedata.normalize("NFC", value):
        _reject(f"{what} is not Unicode NFC canonical")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _reject(f"{what} contains an ASCII control character")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as error:
        _reject(f"{what} is not strict UTF-8: {error}")
    if len(encoded) > byte_limit:
        _reject(f"{what} exceeds the {byte_limit}-byte limit")


def validate_display_name(display_name: str) -> str:
    if not isinstance(display_name, str):
        _reject("display name is not text")
    _validate_text(
        display_name,
        what="display name",
        byte_limit=MAX_DISPLAY_NAME_BYTES,
    )
    if not display_name.isprintable():
        _reject("display name contains a non-printable character")
    if '"' in display_name or "\\" in display_name:
        _reject('display name contains a VDF-unsafe quote or backslash')
    if display_name != display_name.strip():
        _reject("display name has leading or trailing whitespace")
    return display_name


def _validate_member_name(name: str, identity: str) -> str:
    _validate_text(name, what="archive member name", byte_limit=MAX_MEMBER_PATH_BYTES)
    if "\\" in name:
        _reject(f"archive member uses a backslash: {name!r}")
    if name.startswith("/") or name.endswith("/"):
        _reject(f"archive member is absolute or has a noncanonical trailing slash: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _reject(f"archive member has an empty, dot, or traversal component: {name!r}")
    if any(len(part.encode("utf-8")) > MAX_COMPONENT_BYTES for part in parts):
        _reject(f"archive member has an overlong path component: {name!r}")
    if PurePosixPath(name).as_posix() != name or posixpath.normpath(name) != name:
        _reject(f"archive member is not a canonical POSIX path: {name!r}")
    if parts[0] != identity:
        _reject(
            f"archive member has unexpected top-level root {parts[0]!r}; "
            f"expected {identity!r}"
        )
    if len(parts) > 1 and parts[1] == RESERVED_COMPONENT:
        _reject(f"archive member collides with reserved provenance path: {name!r}")
    return "/".join(parts[1:])


def _validate_symlink_target(member_name: str, linkname: str, identity: str) -> None:
    _validate_text(linkname, what=f"symlink target for {member_name!r}", byte_limit=MAX_LINK_BYTES)
    if "\\" in linkname or posixpath.isabs(linkname):
        _reject(f"symlink target is absolute or uses a backslash: {member_name!r} -> {linkname!r}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member_name), linkname))
    if resolved != identity and not resolved.startswith(identity + "/"):
        _reject(f"symlink escapes the Proton root: {member_name!r} -> {linkname!r}")


def _validate_hardlink_target(member_name: str, linkname: str, identity: str) -> str:
    _validate_text(linkname, what=f"hardlink target for {member_name!r}", byte_limit=MAX_LINK_BYTES)
    if "\\" in linkname or posixpath.isabs(linkname):
        _reject(f"hardlink target is absolute or uses a backslash: {member_name!r} -> {linkname!r}")
    # POSIX tar hardlink linknames are archive-root member names, not paths
    # relative to the link's parent.  Requiring that form removes ambiguity.
    relative = _validate_member_name(linkname, identity)
    return relative


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isreg():
        return "file"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    _reject(f"unsupported special tar member type {member.type!r}: {member.name!r}")
    raise AssertionError("unreachable")


def _manifest_digest(members: Iterable[MemberPlan]) -> str:
    digest = hashlib.sha256()
    for member in members:
        fields = (
            member.kind,
            member.name,
            f"{member.mode:04o}",
            str(member.size),
            member.linkname,
        )
        digest.update("\0".join(fields).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_archive(archive: Path, expected_sha256: str, identity: str) -> ArchivePlan:
    identity = validate_identity(identity)
    expected_sha256 = validate_sha256(expected_sha256)
    archive = Path(archive)
    if not archive.is_file() or not stat.S_ISREG(os.lstat(archive).st_mode):
        _reject("archive is not a regular file")
    actual_sha256 = sha256_file(archive)
    if actual_sha256 != expected_sha256:
        _reject(
            f"raw archive SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    plans: list[MemberPlan] = []
    by_name: dict[str, MemberPlan] = {}
    total_regular_bytes = 0
    root_mode: int | None = None
    try:
        with tarfile.open(archive, mode="r:*") as stream:
            for index, member in enumerate(stream):
                if index >= MAX_MEMBERS:
                    _reject(f"archive exceeds the {MAX_MEMBERS}-member limit")
                relative = _validate_member_name(member.name, identity)
                if member.name in by_name:
                    _reject(f"duplicate archive member: {member.name!r}")
                kind = _member_kind(member)
                if member.mode < 0 or member.mode & ~0o777:
                    _reject(f"archive member has special or invalid mode bits: {member.name!r}")
                if member.size < 0:
                    _reject(f"archive member has a negative size: {member.name!r}")
                if getattr(member, "sparse", None):
                    _reject(f"sparse archive members are unsupported: {member.name!r}")
                if kind == "file":
                    total_regular_bytes += member.size
                    if total_regular_bytes > MAX_TOTAL_REGULAR_BYTES:
                        _reject(
                            "archive exceeds the declared regular-file size limit of "
                            f"{MAX_TOTAL_REGULAR_BYTES} bytes"
                        )
                elif member.size != 0:
                    _reject(f"non-file archive member carries data: {member.name!r}")
                linkname = member.linkname or ""
                if kind == "symlink":
                    _validate_symlink_target(member.name, linkname, identity)
                elif kind == "hardlink":
                    _validate_hardlink_target(member.name, linkname, identity)
                elif linkname:
                    _reject(f"non-link archive member has a link target: {member.name!r}")
                plan = MemberPlan(
                    name=member.name,
                    relative=relative,
                    kind=kind,
                    mode=member.mode,
                    size=member.size,
                    linkname=linkname,
                )
                plans.append(plan)
                by_name[member.name] = plan
                if relative == "":
                    if kind != "directory":
                        _reject("top-level archive member is not a directory")
                    root_mode = member.mode
    except (tarfile.TarError, EOFError, OSError) as error:
        _reject(f"cannot parse archive: {error}")

    if not plans:
        _reject("archive is empty")
    if root_mode is None:
        _reject("archive does not contain one explicit top-level directory member")

    # No file or link may act as a directory for another member.  This closes
    # the classic "symlink first, child later" extraction primitive.
    for member in plans:
        parts = member.name.split("/")
        for end in range(1, len(parts)):
            ancestor_name = "/".join(parts[:end])
            ancestor = by_name.get(ancestor_name)
            if ancestor is not None and ancestor.kind != "directory":
                _reject(
                    f"archive member {member.name!r} has non-directory parent "
                    f"{ancestor_name!r}"
                )

    for member in plans:
        if member.kind != "hardlink":
            continue
        target_relative = _validate_hardlink_target(member.name, member.linkname, identity)
        target = by_name.get(identity + ("/" + target_relative if target_relative else ""))
        if target is None or target.kind != "file":
            _reject(
                f"hardlink target must name one regular archive member: "
                f"{member.name!r} -> {member.linkname!r}"
            )

    return ArchivePlan(
        members=tuple(plans),
        root_mode=root_mode,
        total_regular_bytes=total_regular_bytes,
        member_manifest_sha256=_manifest_digest(plans),
    )


def _output_path(output: Path, relative: str) -> Path:
    return output if not relative else output.joinpath(*relative.split("/"))


def _create_directories(output: Path, plan: ArchivePlan) -> dict[str, int]:
    directory_modes: dict[str, int] = {"": plan.root_mode}
    for member in plan.members:
        if member.kind == "directory":
            directory_modes[member.relative] = member.mode
        parts = member.relative.split("/") if member.relative else []
        for end in range(1, len(parts)):
            directory_modes.setdefault("/".join(parts[:end]), 0o755)
    output.mkdir(mode=0o700)
    for relative in sorted(directory_modes, key=lambda value: (value.count("/"), value)):
        if not relative:
            continue
        _output_path(output, relative).mkdir(mode=0o700)
    return directory_modes


def _copy_regular_files(archive: Path, output: Path, plans: dict[str, MemberPlan]) -> None:
    with tarfile.open(archive, mode="r|*") as stream:
        for member in stream:
            plan = plans[member.name]
            if plan.kind != "file":
                continue
            source: BinaryIO | None = stream.extractfile(member)
            if source is None:
                _reject(f"tar reader did not expose regular-file data: {member.name!r}")
            destination = _output_path(output, plan.relative)
            with source, destination.open("xb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            if destination.stat().st_size != plan.size:
                _reject(f"extracted file size mismatch: {member.name!r}")
            os.chmod(destination, plan.mode, follow_symlinks=False)


def _create_links(output: Path, plan: ArchivePlan, identity: str) -> None:
    for member in plan.members:
        if member.kind != "hardlink":
            continue
        target_relative = _validate_hardlink_target(member.name, member.linkname, identity)
        os.link(
            _output_path(output, target_relative),
            _output_path(output, member.relative),
            follow_symlinks=False,
        )
    for member in plan.members:
        if member.kind == "symlink":
            os.symlink(member.linkname, _output_path(output, member.relative))


def _sha256_regular(path: Path) -> str:
    if not stat.S_ISREG(os.lstat(path).st_mode):
        _reject(f"required payload path is not a regular file: {path.name!r}")
    return sha256_file(path)


def _tokenize_vdf(text: str) -> tuple[tuple[str, str], ...]:
    tokens: list[tuple[str, str]] = []
    position = 0
    length = len(text)
    while position < length:
        character = text[position]
        if character.isspace():
            position += 1
            continue
        if text.startswith("//", position):
            newline = text.find("\n", position + 2)
            position = length if newline < 0 else newline + 1
            continue
        if character == "{":
            tokens.append(("open", character))
            position += 1
            continue
        if character == "}":
            tokens.append(("close", character))
            position += 1
            continue
        if character != '"':
            _reject(
                "compatibilitytool.vdf is malformed: expected a quoted string "
                f"or brace at character {position}"
            )
        position += 1
        value: list[str] = []
        while position < length:
            character = text[position]
            if character == '"':
                position += 1
                tokens.append(("string", "".join(value)))
                break
            if character == "\\":
                if position + 1 >= length:
                    _reject("compatibilitytool.vdf has an unterminated quoted string")
                escaped = text[position + 1]
                if escaped in {'"', "\\"}:
                    value.append(escaped)
                    position += 2
                    continue
            if character in {"\r", "\n"}:
                _reject("compatibilitytool.vdf has a newline in a quoted string")
            value.append(character)
            position += 1
        else:
            _reject("compatibilitytool.vdf has an unterminated quoted string")
    return tuple(tokens)


def _parse_vdf(text: str) -> VdfObject:
    tokens = _tokenize_vdf(text)

    def parse_object(position: int, *, nested: bool) -> tuple[VdfObject, int]:
        entries: list[tuple[str, str | VdfObject]] = []
        while position < len(tokens):
            kind, value = tokens[position]
            if kind == "close":
                if not nested:
                    _reject("compatibilitytool.vdf has an unexpected closing brace")
                return VdfObject(tuple(entries)), position + 1
            if kind != "string":
                _reject("compatibilitytool.vdf is malformed: expected a quoted key")
            key = value
            position += 1
            if position >= len(tokens):
                _reject(f"compatibilitytool.vdf key {key!r} has no value")
            value_kind, scalar = tokens[position]
            if value_kind == "string":
                entries.append((key, scalar))
                position += 1
            elif value_kind == "open":
                child, position = parse_object(position + 1, nested=True)
                entries.append((key, child))
            else:
                _reject(f"compatibilitytool.vdf key {key!r} has no value")
        if nested:
            _reject("compatibilitytool.vdf has an unterminated object")
        return VdfObject(tuple(entries)), position

    root, consumed = parse_object(0, nested=False)
    if consumed != len(tokens):
        _reject("compatibilitytool.vdf contains trailing tokens")
    return root


def _vdf_values(parent: VdfObject, key: str) -> tuple[str | VdfObject, ...]:
    return tuple(
        value
        for entry_key, value in parent.entries
        if entry_key == key
    )


def _verify_compatibilitytool_vdf(
    manifest_text: str,
    identity: str,
    display_name: str,
) -> None:
    root = _parse_vdf(manifest_text)
    compatibilitytools = _vdf_values(root, "compatibilitytools")
    if len(compatibilitytools) != 1 or not isinstance(
        compatibilitytools[0], VdfObject
    ):
        _reject(
            "compatibilitytool.vdf does not bind the exact expected internal "
            f"identity {identity!r}"
        )
    compat_tools = _vdf_values(compatibilitytools[0], "compat_tools")
    if len(compat_tools) != 1 or not isinstance(compat_tools[0], VdfObject):
        _reject(
            "compatibilitytool.vdf does not bind the exact expected internal "
            f"identity {identity!r}"
        )
    tools = _vdf_values(compat_tools[0], identity)
    if len(tools) != 1 or not isinstance(tools[0], VdfObject):
        _reject(
            "compatibilitytool.vdf does not bind the exact expected internal "
            f"identity {identity!r}"
        )
    tool = tools[0]
    if _vdf_values(tool, "display_name") != (display_name,):
        _reject(
            "compatibilitytool.vdf does not bind the exact expected display "
            f"name {display_name!r} to internal identity {identity!r}"
        )
    if _vdf_values(tool, "install_path") != (".",):
        _reject(
            'compatibilitytool.vdf does not bind install_path "." to internal '
            f"identity {identity!r}"
        )


def verify_proton_payload(
    output: Path,
    identity: str,
    display_name: str | None = None,
) -> list[dict[str, object]]:
    display_name = validate_display_name(
        identity if display_name is None else display_name
    )
    proton = output / "proton"
    proton_mode = os.lstat(proton).st_mode if proton.exists() else 0
    if not stat.S_ISREG(proton_mode) or proton.stat().st_size == 0:
        _reject("payload proton launcher is missing, empty, or not a regular file")
    if stat.S_IMODE(proton_mode) & 0o111 == 0:
        _reject("payload proton launcher is not executable")

    manifest = output / "compatibilitytool.vdf"
    if not manifest.is_file() or manifest.stat().st_size == 0:
        _reject("payload compatibilitytool.vdf is missing, empty, or not a regular file")
    try:
        manifest_text = manifest.read_text(encoding="utf-8", errors="strict")
    except UnicodeError as error:
        _reject(f"compatibilitytool.vdf is not strict UTF-8: {error}")
    _verify_compatibilitytool_vdf(manifest_text, identity, display_name)

    root_license = output / "LICENSE"
    if not root_license.is_file() or root_license.stat().st_size == 0:
        _reject("payload root LICENSE is missing, empty, or not a regular file")
    licenses: list[dict[str, object]] = []
    for path in sorted(output.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
        if path.is_symlink() or not path.is_file():
            continue
        if LICENSE_BASENAME_RE.fullmatch(path.name) is None:
            continue
        relative = path.relative_to(output).as_posix()
        licenses.append(
            {
                "path": relative,
                "sha256": _sha256_regular(path),
                "size": path.stat().st_size,
            }
        )
    if not licenses or not any(item["path"] == "LICENSE" for item in licenses):
        _reject("payload license inventory does not contain root LICENSE")
    return licenses


def payload_tree_digest(output: Path, *, exclude_generated_metadata: bool = False) -> str:
    """Hash payload types, paths, modes, sizes, contents, and link targets.

    The generated ``.proton-input`` directory does not exist when this runs,
    avoiding a circular provenance digest.
    """

    digest = hashlib.sha256()

    def visit(directory: Path, prefix: str) -> None:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.encode("utf-8"))
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if exclude_generated_metadata and relative == RESERVED_COMPONENT:
                continue
            encoded_relative = relative.encode("utf-8")
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode):
                target = os.readlink(entry.path).encode("utf-8")
                digest.update(b"L\0" + encoded_relative + b"\0" + target + b"\n")
            elif stat.S_ISDIR(entry_stat.st_mode):
                digest.update(b"D\0" + encoded_relative + b"\n")
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(entry_stat.st_mode):
                executable = b"1" if stat.S_IMODE(entry_stat.st_mode) & 0o111 else b"0"
                content = sha256_file(Path(entry.path)).encode("ascii")
                size = str(entry_stat.st_size).encode("ascii")
                digest.update(
                    b"F\0"
                    + encoded_relative
                    + b"\0"
                    + executable
                    + b"\0"
                    + size
                    + b"\0"
                    + content
                    + b"\n"
                )
            else:
                _reject(f"unexpected extracted payload node: {relative!r}")

    visit(output, "")
    return digest.hexdigest()


def payload_tree_digest_without_generated_metadata(output: Path) -> str:
    """Recompute the recorded payload digest from a finished result."""

    return payload_tree_digest(output, exclude_generated_metadata=True)


def _normalize_times(output: Path) -> None:
    paths = [output, *output.rglob("*")]
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        try:
            os.utime(
                path,
                ns=(NORMALIZED_MTIME_NS, NORMALIZED_MTIME_NS),
                follow_symlinks=False,
            )
        except (NotImplementedError, PermissionError):
            if not path.is_symlink():
                raise


def extract_archive(
    archive: Path,
    output: Path,
    expected_sha256: str,
    identity: str,
    nixpkgs_source: Path,
    display_name: str | None = None,
) -> dict[str, object]:
    display_name = validate_display_name(
        identity if display_name is None else display_name
    )
    plan = validate_archive(archive, expected_sha256, identity)
    output = Path(output)
    if output.exists():
        _reject("output path already exists")
    directory_modes = _create_directories(output, plan)
    plans = {member.name: member for member in plan.members}
    try:
        _copy_regular_files(archive, output, plans)
        _create_links(output, plan, identity)
        for relative, mode in sorted(
            directory_modes.items(), key=lambda item: item[0].count("/"), reverse=True
        ):
            if relative:
                os.chmod(_output_path(output, relative), mode, follow_symlinks=False)

        licenses = verify_proton_payload(output, identity, display_name)
        tree_sha256 = payload_tree_digest(output)
        counts = {
            kind: sum(member.kind == kind for member in plan.members)
            for kind in ("directory", "file", "hardlink", "symlink")
        }
        provenance: dict[str, object] = {
            "archive_member_manifest_sha256": plan.member_manifest_sha256,
            "archive_sha256": expected_sha256,
            "archive_store_path": os.fspath(archive),
            "display_name": display_name,
            "license_files": licenses,
            "member_counts": counts,
            "nixpkgs_source_store_path": os.fspath(nixpkgs_source),
            "payload_tree_algorithm": "sha256(type\\0path\\0Nix-executable-bit/size/content-or-target\\n), lexical UTF-8 order, provenance excluded",
            "payload_tree_sha256": tree_sha256,
            "provenance_version": 1,
            "required_payload": {
                "compatibilitytool.vdf": (
                    "verified exact internal identity/display_name/install_path"
                ),
                "LICENSE": "verified regular nonempty",
                "proton": "verified regular nonempty executable",
            },
            "top_level_identity": identity,
            "total_regular_bytes": plan.total_regular_bytes,
        }
        metadata = output / RESERVED_COMPONENT
        metadata.mkdir(mode=0o755)
        (metadata / "archive.sha256").write_text(
            f"{expected_sha256}  {archive.name}\n", encoding="ascii"
        )
        (metadata / "payload-tree.sha256").write_text(
            f"{tree_sha256}  payload\n", encoding="ascii"
        )
        (metadata / "provenance.json").write_text(
            json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for path in metadata.iterdir():
            os.chmod(path, 0o444)
        os.chmod(metadata, 0o555)
        os.chmod(output, plan.root_mode)
        _normalize_times(output)
        return provenance
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--top-level-identity", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--nixpkgs-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        archive = validate_store_archive(arguments.archive)
        nixpkgs_source = validate_store_nixpkgs(arguments.nixpkgs_source)
        provenance = extract_archive(
            archive=archive,
            output=arguments.output,
            expected_sha256=arguments.archive_sha256,
            identity=arguments.top_level_identity,
            nixpkgs_source=nixpkgs_source,
            display_name=arguments.display_name,
        )
    except ArchiveRejected as error:
        print(f"immutable Proton input rejected: {error}", file=sys.stderr)
        return 2
    print(
        "immutable Proton input accepted: "
        f"identity={provenance['top_level_identity']} "
        f"display_name={provenance['display_name']!r} "
        f"tree_sha256={provenance['payload_tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
