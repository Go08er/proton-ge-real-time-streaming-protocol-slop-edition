# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

import store_input_digest


class StoreInputDigestTests(unittest.TestCase):
    def test_file_digest_is_raw_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"abc")
            self.assertEqual(
                store_input_digest.file_sha256(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_tree_digest_is_order_independent_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root, second_root = Path(first), Path(second)
            (first_root / "z").write_bytes(b"last")
            (first_root / "a").mkdir()
            (first_root / "a" / "item").write_bytes(b"payload")
            (first_root / "link").symlink_to("a/item")

            (second_root / "link").symlink_to("a/item")
            (second_root / "a").mkdir()
            (second_root / "a" / "item").write_bytes(b"payload")
            (second_root / "z").write_bytes(b"last")
            self.assertEqual(
                store_input_digest.tree_sha256(first_root),
                store_input_digest.tree_sha256(second_root),
            )
            (second_root / "a" / "item").write_bytes(b"changed")
            self.assertNotEqual(
                store_input_digest.tree_sha256(first_root),
                store_input_digest.tree_sha256(second_root),
            )

    def test_tree_digest_binds_executable_bit_and_link_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "tool"
            executable.write_bytes(b"same")
            link = root / "link"
            link.symlink_to("tool")
            baseline = store_input_digest.tree_sha256(root)
            executable.chmod(0o755)
            executable_digest = store_input_digest.tree_sha256(root)
            self.assertNotEqual(baseline, executable_digest)
            link.unlink()
            link.symlink_to("missing")
            self.assertNotEqual(executable_digest, store_input_digest.tree_sha256(root))

    def test_rejects_special_node(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.mkfifo(root / "fifo")
            with self.assertRaises(store_input_digest.DigestError):
                store_input_digest.tree_sha256(root)

    def test_matching_writable_copy_retains_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as copy_dir:
            source, copied = Path(source_dir), Path(copy_dir)
            (source / "tool").write_bytes(b"payload")
            (source / "tool").chmod(0o555)
            (copied / "tool").write_bytes(b"payload")
            (copied / "tool").chmod(0o755)
            self.assertEqual(
                store_input_digest.require_matching_tree_copy(source, copied),
                store_input_digest.tree_sha256(source),
            )
            (copied / "tool").write_bytes(b"changed")
            with self.assertRaisesRegex(store_input_digest.DigestError, "differs"):
                store_input_digest.require_matching_tree_copy(source, copied)


if __name__ == "__main__":
    unittest.main()
