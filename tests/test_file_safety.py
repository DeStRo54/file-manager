import os
import tempfile
import unittest
from pathlib import Path

from app.files.file_safety import safe_path


class FileSafetyTests(unittest.TestCase):
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
