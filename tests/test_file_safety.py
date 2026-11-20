import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.files.file_safety import safe_path, read_bytes, write_bytes


class FileSafetyTests(unittest.TestCase):
    def test_create_replace_and_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            write_bytes(path, b"first")
            self.assertEqual(read_bytes(path), b"first")
            with self.assertRaises(FileExistsError):
                write_bytes(path, b"second")
            self.assertEqual(read_bytes(path), b"first")
            write_bytes(path, b"replacement", replace=True)
            self.assertEqual(read_bytes(path), b"replacement")
            self.assertEqual(list(path.parent.glob(".fm-*")), [])

    def test_size_limit_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_bytes(b"12345")
            with patch("app.files.file_safety.MAX_FILE_SIZE", 4):
                with self.assertRaises(ValueError):
                    read_bytes(path)
                with self.assertRaises(ValueError):
                    write_bytes(path, b"12345", replace=True)
            self.assertEqual(path.read_bytes(), b"12345")
            self.assertEqual(list(path.parent.glob(".fm-*")), [])

    def test_invalid_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("../outside", "CON.txt", "NUL", ".fm-private", "x\x00y", "file:stream", "a/" * 33 + "file"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    safe_path(root, root, name)

    def test_symbolic_link_and_linked_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real = root / "real"
            real.mkdir()
            (real / "sample").write_bytes(b"marker")
            link = root / "linked"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Cannot create symbolic links: {exc}")
            with self.assertRaises(ValueError):
                safe_path(root, root, "linked/sample")
            with self.assertRaises(ValueError):
                read_bytes(link)

    def test_failed_replace_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample"
            path.write_bytes(b"original")
            with patch("app.files.file_safety.os.replace", side_effect=OSError("test failure")):
                with self.assertRaises(OSError):
                    write_bytes(path, b"replacement", replace=True)
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.glob(".fm-*")), [])

    def test_hard_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / "original.txt"
            original.write_text("sample data", encoding="utf-8")
            self.assertEqual(safe_path(root, root, original.name), original)

            alias = root / "alias.txt"
            try:
                os.link(original, alias)
            except OSError as exc:
                self.skipTest(f"Cannot create hard links on this filesystem: {exc}")
            with self.assertRaisesRegex(ValueError, "Жёсткие ссылки"):
                safe_path(root, root, alias.name)
            with self.assertRaises(ValueError):
                read_bytes(alias)
