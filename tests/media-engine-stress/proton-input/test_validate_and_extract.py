#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Pure hostile-archive tests for validate_and_extract.py."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

import validate_and_extract as subject


IDENTITY = "Proton-Test-1"
DISPLAY_NAME = "Proton Test 1"


def directory(name: str, mode: int = 0o755) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE
    item.mode = mode
    item.mtime = 1234
    return item


def regular(name: str, data: bytes, mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    item = tarfile.TarInfo(name)
    item.type = tarfile.REGTYPE
    item.mode = mode
    item.size = len(data)
    item.mtime = 1234
    return item, data


def symlink(name: str, target: str) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.SYMTYPE
    item.mode = 0o777
    item.linkname = target
    item.mtime = 1234
    return item


def hardlink(name: str, target: str) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.LNKTYPE
    item.mode = 0o644
    item.linkname = target
    item.mtime = 1234
    return item


def base_entries(
    identity: str = IDENTITY,
    *,
    display_name: str | None = None,
    install_path: str = ".",
) -> list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]:
    if display_name is None:
        display_name = identity
    vdf = (
        '"compatibilitytools"\n{\n  "compat_tools"\n  {\n'
        f'    "{identity}" // exact key\n    {{\n'
        f'      "install_path" "{install_path}"\n'
        f'      "display_name" "{display_name}"\n'
        '    }\n  }\n}\n'
    ).encode()
    return [
        directory(identity),
        regular(f"{identity}/LICENSE", b"test license\n"),
        regular(f"{identity}/compatibilitytool.vdf", vdf),
        regular(f"{identity}/proton", b"#!/bin/sh\nexit 0\n", 0o755),
        directory(f"{identity}/files"),
        directory(f"{identity}/files/bin"),
        regular(f"{identity}/files/bin/wine", b"wine\n", 0o755),
        symlink(f"{identity}/files/bin/wine-link", "wine"),
    ]


def write_archive(path: Path, entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]) -> str:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            if isinstance(entry, tuple):
                info, data = entry
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(entry)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProtonInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "input.tar.gz"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_rejected(
        self,
        entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]],
        pattern: str,
        *,
        identity: str = IDENTITY,
    ) -> None:
        digest = write_archive(self.archive, entries)
        with self.assertRaisesRegex(subject.ArchiveRejected, pattern):
            subject.validate_archive(self.archive, digest, identity)

    def test_accepts_and_extracts_safe_tool_reproducibly(self) -> None:
        entries = base_entries() + [
            hardlink(f"{IDENTITY}/LICENSE.copy", f"{IDENTITY}/LICENSE"),
            regular(f"{IDENTITY}/NOTICE.third-party", b"notice\n"),
        ]
        digest = write_archive(self.archive, entries)
        first = self.root / "first"
        second = self.root / "second"
        provenance_one = subject.extract_archive(
            self.archive, first, digest, IDENTITY, Path("/nix/store/test-source")
        )
        provenance_two = subject.extract_archive(
            self.archive, second, digest, IDENTITY, Path("/nix/store/test-source")
        )
        self.assertEqual(provenance_one, provenance_two)
        self.assertEqual(
            provenance_one["payload_tree_sha256"],
            subject.payload_tree_digest_without_generated_metadata(first),
        )
        self.assertTrue(os.access(first / "proton", os.X_OK))
        self.assertEqual(os.readlink(first / "files/bin/wine-link"), "wine")
        self.assertEqual((first / "LICENSE").stat().st_ino, (first / "LICENSE.copy").stat().st_ino)
        metadata = json.loads((first / ".proton-input/provenance.json").read_text())
        self.assertEqual(metadata, provenance_one)
        self.assertEqual(metadata["top_level_identity"], IDENTITY)
        self.assertEqual(metadata["display_name"], IDENTITY)
        self.assertEqual(
            [item["path"] for item in metadata["license_files"]],
            ["LICENSE", "LICENSE.copy", "NOTICE.third-party"],
        )

    def test_accepts_distinct_display_name_without_rewriting_payload(self) -> None:
        entries = base_entries(display_name=DISPLAY_NAME)
        archived_vdf = next(
            item[1]
            for item in entries
            if isinstance(item, tuple)
            and item[0].name.endswith("/compatibilitytool.vdf")
        )
        digest = write_archive(self.archive, entries)
        output = self.root / "distinct-display-name"
        provenance = subject.extract_archive(
            self.archive,
            output,
            digest,
            IDENTITY,
            Path("/nix/store/test-source"),
            display_name=DISPLAY_NAME,
        )
        self.assertEqual(provenance["top_level_identity"], IDENTITY)
        self.assertEqual(provenance["display_name"], DISPLAY_NAME)
        self.assertEqual((output / "compatibilitytool.vdf").read_bytes(), archived_vdf)

    def test_payload_digest_survives_nix_read_only_mode_normalization(self) -> None:
        digest = write_archive(self.archive, base_entries())
        output = self.root / "normalized"
        subject.extract_archive(
            self.archive, output, digest, IDENTITY, Path("/nix/store/test-source")
        )
        expected = subject.payload_tree_digest_without_generated_metadata(output)
        for path in output.rglob("*"):
            if path == output / ".proton-input" or output / ".proton-input" in path.parents:
                continue
            if path.is_symlink():
                continue
            current = stat.S_IMODE(path.stat().st_mode)
            os.chmod(path, 0o555 if path.is_dir() or current & 0o111 else 0o444)
        self.assertEqual(
            expected,
            subject.payload_tree_digest_without_generated_metadata(output),
        )
        os.chmod(output / "proton", 0o444)
        self.assertNotEqual(
            expected,
            subject.payload_tree_digest_without_generated_metadata(output),
        )

    def test_rejects_wrong_raw_hash(self) -> None:
        write_archive(self.archive, base_entries())
        with self.assertRaisesRegex(subject.ArchiveRejected, "SHA-256 mismatch"):
            subject.validate_archive(self.archive, "0" * 64, IDENTITY)

    def test_rejects_malformed_hash_and_identity(self) -> None:
        with self.assertRaisesRegex(subject.ArchiveRejected, "64 lowercase"):
            subject.validate_sha256("A" * 64)
        for identity in ("", "../tool", ".hidden", "name/child", "x" * 129):
            with self.subTest(identity=identity):
                with self.assertRaises(subject.ArchiveRejected):
                    subject.validate_identity(identity)

    def test_rejects_unsafe_or_overlong_display_name(self) -> None:
        bad_names = (
            "",
            "line\nbreak",
            "zero\u200bwidth",
            'quoted"name',
            "back\\slash",
            " padded",
            "padded ",
            "   ",
            "e\u0301",
            "x" * (subject.MAX_DISPLAY_NAME_BYTES + 1),
        )
        for display_name in bad_names:
            with self.subTest(display_name=display_name):
                with self.assertRaises(subject.ArchiveRejected):
                    subject.validate_display_name(display_name)

    def test_rejects_absolute_traversal_dot_empty_and_backslash_names(self) -> None:
        bad_names = (
            f"/{IDENTITY}/evil",
            f"{IDENTITY}/../evil",
            f"./{IDENTITY}/evil",
            f"{IDENTITY}//evil",
            f"{IDENTITY}/evil/",
            f"{IDENTITY}\\evil",
        )
        for name in bad_names:
            with self.subTest(name=name):
                self.assert_rejected(base_entries() + [regular(name, b"x")], "archive member")

    def test_rejects_duplicate_member(self) -> None:
        self.assert_rejected(
            base_entries() + [regular(f"{IDENTITY}/LICENSE", b"again")],
            "duplicate archive member",
        )

    def test_rejects_other_or_missing_root(self) -> None:
        self.assert_rejected(
            base_entries() + [regular("OtherRoot/file", b"x")],
            "unexpected top-level root",
        )
        self.assert_rejected(base_entries()[1:], "explicit top-level directory")

    def test_rejects_special_nodes_and_special_mode_bits(self) -> None:
        fifo = tarfile.TarInfo(f"{IDENTITY}/fifo")
        fifo.type = tarfile.FIFOTYPE
        fifo.mode = 0o644
        self.assert_rejected(base_entries() + [fifo], "unsupported special")
        self.assert_rejected(
            base_entries() + [regular(f"{IDENTITY}/setuid", b"x", 0o4755)],
            "special or invalid mode",
        )

    def test_rejects_escaping_or_absolute_symlinks(self) -> None:
        for target in ("../../outside", "/etc/passwd", "..\\outside"):
            with self.subTest(target=target):
                self.assert_rejected(
                    base_entries() + [symlink(f"{IDENTITY}/escape", target)],
                    "symlink",
                )

    def test_rejects_escaping_missing_or_nonfile_hardlinks(self) -> None:
        cases = (
            ("../../outside", "traversal component"),
            (f"{IDENTITY}/missing", "regular archive member"),
            (f"{IDENTITY}/files", "regular archive member"),
        )
        for target, pattern in cases:
            with self.subTest(target=target):
                self.assert_rejected(
                    base_entries() + [hardlink(f"{IDENTITY}/hard", target)], pattern
                )

    def test_rejects_child_beneath_symlink(self) -> None:
        entries = base_entries() + [
            symlink(f"{IDENTITY}/alias", "files"),
            regular(f"{IDENTITY}/alias/child", b"x"),
        ]
        self.assert_rejected(entries, "non-directory parent")

    def test_rejects_reserved_provenance_collision(self) -> None:
        self.assert_rejected(
            base_entries() + [directory(f"{IDENTITY}/.proton-input")],
            "reserved provenance",
        )

    def test_rejects_missing_or_invalid_required_payload(self) -> None:
        cases = (
            ([entry for entry in base_entries() if not (isinstance(entry, tuple) and entry[0].name.endswith("/proton"))], "proton launcher"),
            ([entry for entry in base_entries() if not (isinstance(entry, tuple) and entry[0].name.endswith("/LICENSE"))], "root LICENSE"),
            ([entry for entry in base_entries() if not (isinstance(entry, tuple) and entry[0].name.endswith("/compatibilitytool.vdf"))], "compatibilitytool.vdf"),
        )
        for entries, pattern in cases:
            with self.subTest(pattern=pattern):
                digest = write_archive(self.archive, entries)
                with self.assertRaisesRegex(subject.ArchiveRejected, pattern):
                    subject.extract_archive(
                        self.archive,
                        self.root / f"output-{pattern.replace(' ', '-')}",
                        digest,
                        IDENTITY,
                        Path("/nix/store/test-source"),
                    )

    def test_rejects_nonexecutable_proton(self) -> None:
        entries = [
            regular(item[0].name, item[1], 0o644)
            if isinstance(item, tuple) and item[0].name.endswith("/proton")
            else item
            for item in base_entries()
        ]
        digest = write_archive(self.archive, entries)
        with self.assertRaisesRegex(subject.ArchiveRejected, "not executable"):
            subject.extract_archive(
                self.archive,
                self.root / "nonexec",
                digest,
                IDENTITY,
                Path("/nix/store/test-source"),
            )

    def test_rejects_wrong_manifest_internal_identity(self) -> None:
        wrong = base_entries("Wrong-Identity")
        # Rename members to the expected root while leaving VDF content wrong.
        for entry in wrong:
            info = entry[0] if isinstance(entry, tuple) else entry
            info.name = info.name.replace("Wrong-Identity", IDENTITY, 1)
        digest = write_archive(self.archive, wrong)
        with self.assertRaisesRegex(subject.ArchiveRejected, "expected internal identity"):
            subject.extract_archive(
                self.archive,
                self.root / "wrong-vdf",
                digest,
                IDENTITY,
                Path("/nix/store/test-source"),
            )

    def test_rejects_wrong_manifest_display_name(self) -> None:
        entries = base_entries(display_name="Wrong Display")
        digest = write_archive(self.archive, entries)
        with self.assertRaisesRegex(subject.ArchiveRejected, "expected display name"):
            subject.extract_archive(
                self.archive,
                self.root / "wrong-display",
                digest,
                IDENTITY,
                Path("/nix/store/test-source"),
                display_name=DISPLAY_NAME,
            )

    def test_rejects_wrong_manifest_install_path(self) -> None:
        entries = base_entries(
            display_name=DISPLAY_NAME,
            install_path="files",
        )
        digest = write_archive(self.archive, entries)
        with self.assertRaisesRegex(subject.ArchiveRejected, 'install_path "."'):
            subject.extract_archive(
                self.archive,
                self.root / "wrong-install-path",
                digest,
                IDENTITY,
                Path("/nix/store/test-source"),
                display_name=DISPLAY_NAME,
            )

    def test_manifest_values_in_another_tool_do_not_satisfy_binding(self) -> None:
        vdf = (
            '"compatibilitytools"\n{\n  "compat_tools"\n  {\n'
            f'    "{IDENTITY}"\n    {{\n'
            '      "install_path" "wrong"\n'
            '      "display_name" "Wrong Display"\n'
            '    }\n'
            '    "Decoy"\n    {\n'
            '      "install_path" "."\n'
            f'      "display_name" "{DISPLAY_NAME}"\n'
            '    }\n  }\n}\n'
        ).encode()
        entries = [
            regular(item[0].name, vdf, item[0].mode)
            if isinstance(item, tuple)
            and item[0].name.endswith("/compatibilitytool.vdf")
            else item
            for item in base_entries()
        ]
        digest = write_archive(self.archive, entries)
        with self.assertRaisesRegex(subject.ArchiveRejected, "expected display name"):
            subject.extract_archive(
                self.archive,
                self.root / "decoy-bindings",
                digest,
                IDENTITY,
                Path("/nix/store/test-source"),
                display_name=DISPLAY_NAME,
            )

    def test_manifest_rejects_ambiguous_duplicate_bindings(self) -> None:
        duplicate_identity = (
            '"compatibilitytools" { "compat_tools" { '
            f'"{IDENTITY}" {{ "install_path" "." "display_name" "{DISPLAY_NAME}" }} '
            f'"{IDENTITY}" {{ "install_path" "." "display_name" "{DISPLAY_NAME}" }} '
            "} }"
        )
        with self.assertRaisesRegex(subject.ArchiveRejected, "expected internal identity"):
            subject._verify_compatibilitytool_vdf(
                duplicate_identity,
                IDENTITY,
                DISPLAY_NAME,
            )

        duplicate_display = (
            '"compatibilitytools" { "compat_tools" { '
            f'"{IDENTITY}" {{ "install_path" "." '
            f'"display_name" "{DISPLAY_NAME}" "display_name" "{DISPLAY_NAME}" }} '
            "} }"
        )
        with self.assertRaisesRegex(subject.ArchiveRejected, "expected display name"):
            subject._verify_compatibilitytool_vdf(
                duplicate_display,
                IDENTITY,
                DISPLAY_NAME,
            )

    def test_production_path_gate_rejects_nonstore_and_symlink(self) -> None:
        self.archive.write_bytes(b"archive")
        with self.assertRaisesRegex(subject.ArchiveRejected, "/nix/store"):
            subject.validate_store_archive(self.archive)


if __name__ == "__main__":
    unittest.main(verbosity=2)
