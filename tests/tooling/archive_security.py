#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Negative fixtures for strict Proton archive/tree equivalence."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT_DIR = Path(__file__).resolve().parents[2]
VERIFIER = ROOT_DIR / "scripts" / "verify-artifact-archive.py"
ROOT_NAME = "Fixture-Proton"


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    mode: int
    content: bytes = b""
    target: str = ""


BASE_ENTRIES = (
    Entry(ROOT_NAME, "directory", 0o755),
    Entry(f"{ROOT_NAME}/bin", "directory", 0o755),
    Entry(f"{ROOT_NAME}/bin/tool", "file", 0o755, b"#!/bin/sh\nexit 0\n"),
    Entry(f"{ROOT_NAME}/bin/tool-link", "symlink", 0o777, target="tool"),
    Entry(f"{ROOT_NAME}/payload.bin", "file", 0o644, b"trusted\n"),
)


def write_archive(path: Path, entries: tuple[Entry, ...]) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.mode = entry.mode
            if entry.kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry.kind == "file":
                info.type = tarfile.REGTYPE
                info.size = len(entry.content)
                archive.addfile(info, io.BytesIO(entry.content))
            elif entry.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.target
                archive.addfile(info)
            elif entry.kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = entry.target
                archive.addfile(info)
            elif entry.kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            else:
                raise AssertionError(f"unknown fixture kind: {entry.kind}")


def run_verifier(archive: Path, tool_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--archive",
            str(archive),
            "--tool-dir",
            str(tool_dir),
            "--root-name",
            ROOT_NAME,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def replace_entry(
    entries: tuple[Entry, ...], name: str, replacement: Entry
) -> tuple[Entry, ...]:
    return tuple(replacement if entry.name == name else entry for entry in entries)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rtsp-ge-archive-security.") as temporary:
        temp = Path(temporary)
        tool_dir = temp / ROOT_NAME
        (tool_dir / "bin").mkdir(parents=True)
        os.chmod(tool_dir, 0o755)
        os.chmod(tool_dir / "bin", 0o755)
        (tool_dir / "bin" / "tool").write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(tool_dir / "bin" / "tool", 0o755)
        (tool_dir / "bin" / "tool-link").symlink_to("tool")
        (tool_dir / "payload.bin").write_bytes(b"trusted\n")
        os.chmod(tool_dir / "payload.bin", 0o644)

        fixtures: dict[str, tuple[Entry, ...]] = {"good": BASE_ENTRIES}
        fixtures["altered-payload"] = replace_entry(
            BASE_ENTRIES,
            f"{ROOT_NAME}/payload.bin",
            Entry(f"{ROOT_NAME}/payload.bin", "file", 0o644, b"altered\n"),
        )
        fixtures["unsafe-symlink"] = replace_entry(
            BASE_ENTRIES,
            f"{ROOT_NAME}/bin/tool-link",
            Entry(
                f"{ROOT_NAME}/bin/tool-link",
                "symlink",
                0o777,
                target="../../../outside",
            ),
        )
        fixtures["special-type"] = replace_entry(
            BASE_ENTRIES,
            f"{ROOT_NAME}/payload.bin",
            Entry(f"{ROOT_NAME}/payload.bin", "fifo", 0o644),
        )
        fixtures["hardlink-type"] = replace_entry(
            BASE_ENTRIES,
            f"{ROOT_NAME}/payload.bin",
            Entry(
                f"{ROOT_NAME}/payload.bin",
                "hardlink",
                0o644,
                target=f"{ROOT_NAME}/bin/tool",
            ),
        )
        fixtures["unsafe-mode"] = replace_entry(
            BASE_ENTRIES,
            f"{ROOT_NAME}/payload.bin",
            Entry(f"{ROOT_NAME}/payload.bin", "file", 0o4777, b"trusted\n"),
        )
        fixtures["duplicate-member"] = BASE_ENTRIES + (BASE_ENTRIES[-1],)

        results = {}
        for label, entries in fixtures.items():
            archive = temp / f"{label}.tar.gz"
            write_archive(archive, entries)
            results[label] = run_verifier(archive, tool_dir)

        trailing_archive = temp / "concatenated-gzip-payload.tar.gz"
        write_archive(trailing_archive, BASE_ENTRIES)
        with trailing_archive.open("ab") as stream:
            stream.write(gzip.compress(b"appended non-tar payload"))
        results["concatenated-gzip-payload"] = run_verifier(
            trailing_archive, tool_dir
        )

        if results["good"].returncode != 0:
            print(results["good"].stderr, file=sys.stderr)
            raise SystemExit("valid archive/tree fixture was rejected")
        for label, result in results.items():
            if label == "good":
                continue
            if result.returncode == 0:
                raise SystemExit(f"unsafe archive fixture was accepted: {label}")

    print("Strict archive/tree equivalence regressions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
