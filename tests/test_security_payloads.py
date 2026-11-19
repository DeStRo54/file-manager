"""Payloads adapted from PayloadsAllTheThings for isolated local tests."""

import tempfile
import unittest
from pathlib import Path

from app.files.file_safety import safe_path
from app.files.formats import parse_xml


class SecurityPayloadTests(unittest.TestCase):
    # https://github.com/swisskyrepo/PayloadsAllTheThings/tree/master/Directory%20Traversal
    def test_traversal_payloads_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "workspace"
            root.mkdir()
            outside = base / "marker.txt"
            outside.write_text("outside marker", encoding="utf-8")
            payloads = (
                "../marker.txt",
                "../../marker.txt",
                "..\\marker.txt",
                "..\\/marker.txt",
                "%2e%2e%2fmarker.txt",
                "%252e%252e%252fmarker.txt",
            )
            for payload in payloads:
                with self.subTest(payload=payload):
                    try:
                        target = safe_path(root, root, payload)
                    except ValueError:
                        continue
                    # CLI keeps URL escapes literal. Accepted paths must stay confined.
                    self.assertTrue(target.is_relative_to(root))
                    self.assertNotEqual(target, outside)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside marker")

    # https://github.com/swisskyrepo/PayloadsAllTheThings/tree/master/XXE%20Injection
    def test_external_entity_payload_cannot_read_local_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory).resolve() / "marker.txt"
            marker.write_text("XXE_TEST_MARKER", encoding="utf-8")
            payload = (
                '<?xml version="1.0"?>'
                f'<!DOCTYPE root [<!ENTITY test SYSTEM "{marker.as_uri()}">]>'
                '<root>&test;</root>'
            )
            with self.assertRaises(ValueError):
                parse_xml(payload)
            self.assertEqual(marker.read_text(encoding="utf-8"), "XXE_TEST_MARKER")
