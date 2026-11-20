"""Payloads adapted from PayloadsAllTheThings for isolated local tests."""

import tempfile
import unittest
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.files.file_safety import safe_path, write_bytes, read_bytes
from app.files.formats import parse_xml, extract_zip


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
                "a/../../marker.txt",
                "C:/marker.txt",
                "C:marker.txt",
                "\\\\server\\share\\marker.txt",
                "/marker.txt",
                "marker.txt\x00ignored",
                "%2e%2e%2fmarker.txt",
                "%252e%252e%252fmarker.txt",
                "..%2fmarker.txt",
                "%c0%ae%c0%ae%c0%afmarker.txt",
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

    def test_xml_entity_variants_are_rejected(self):
        # XXE Injection: internal entities, external DTD, SYSTEM/PUBLIC,
        # parameter entities and bounded entity expansion.
        payloads = (
            '<!DOCTYPE r [<!ENTITY e "marker">]><r>&e;</r>',
            '<!DOCTYPE r SYSTEM "https://example.invalid/test.dtd"><r/>',
            '<!DOCTYPE r [<!ENTITY e PUBLIC "test" "https://example.invalid/entity">]><r>&e;</r>',
            '<!DOCTYPE r [<!ENTITY % e SYSTEM "https://example.invalid/test.dtd">%e;]><r/>',
            '<!DOCTYPE r [<!ENTITY a "x"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>',
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY e "marker">]><r>&e;</r>',
        )
        # Assert rejection precedes parser construction, preventing external access.
        with patch("app.files.formats.ET.XMLPullParser") as parser:
            for payload in payloads:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    parse_xml(payload)
            parser.assert_not_called()

    def test_xinclude_is_not_expanded(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory).resolve() / "marker.txt"
            marker.write_text("LOCAL_MARKER", encoding="utf-8")
            payload = (
                '<r xmlns:xi="http://www.w3.org/2001/XInclude">'
                f'<xi:include href="{marker.as_uri()}" parse="text"/></r>'
            )
            root = parse_xml(payload)
            self.assertNotIn("LOCAL_MARKER", "".join(root.itertext()))
            self.assertEqual(root[0].tag, "{http://www.w3.org/2001/XInclude}include")

    def test_zip_slip_and_archive_symlink_payloads(self):
        # https://github.com/swisskyrepo/PayloadsAllTheThings/tree/master/Zip%20Slip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / "marker.txt"
            marker.write_bytes(b"original")
            cases = [
                ("../marker.txt", stat.S_IFREG),
                ("nested/../../marker.txt", stat.S_IFREG),
                ("..\\marker.txt", stat.S_IFREG),
                ("/marker.txt", stat.S_IFREG),
                ("C:/marker.txt", stat.S_IFREG),
                ("link", stat.S_IFLNK),
                ("pipe", stat.S_IFIFO),
            ]
            for number, (name, kind) in enumerate(cases):
                with self.subTest(name=name):
                    source = root / f"bad{number}.zip"
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = (kind | 0o600) << 16
                    with zipfile.ZipFile(source, "w") as archive:
                        archive.writestr("valid.txt", b"valid")
                        archive.writestr(entry, b"../marker.txt")
                    destination = root / f"out{number}"
                    with self.assertRaises(ValueError):
                        extract_zip(source, destination)
                    self.assertFalse(destination.exists())
                    self.assertEqual(marker.read_bytes(), b"original")
                    self.assertEqual(list(root.glob(".fm-*")), [])

    def test_shell_metacharacters_remain_literal_file_names(self):
        # Command Injection: delimiters/substitution adapted to inert marker names.
        # Windows forbids some names, so rejection is also a valid outcome.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("name;marker", "name&marker", "$(marker)", "`marker`"):
                with self.subTest(name=name):
                    try:
                        path = safe_path(root, root, name)
                    except ValueError:
                        continue
                    write_bytes(path, b"literal")
                    self.assertEqual(read_bytes(path), b"literal")
            self.assertFalse((root / "marker").exists())
