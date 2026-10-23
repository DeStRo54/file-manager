"""Простые модульные тесты JSON/XML; PostgreSQL не требуется."""

import unittest
import xml.etree.ElementTree as ET

from app.files.formats import parse_json, parse_xml


class FormatTests(unittest.TestCase):
    def test_json(self):
        self.assertEqual(parse_json('{"name":"Иван"}'), {"name": "Иван"})

    def test_invalid_json(self):
        with self.assertRaises(ValueError):
            parse_json('{bad}')

    def test_xml(self):
        root = parse_xml('<person name="Иван">hello</person>')
        self.assertEqual(root.tag, "person")
        self.assertEqual(root.attrib["name"], "Иван")

    def test_invalid_xml(self):
        with self.assertRaises(ET.ParseError):
            parse_xml('<person>')

    def test_xml_entities_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_xml('<!DOCTYPE x [<!ENTITY e "hello">]><x>&e;</x>')


if __name__ == "__main__":
    unittest.main()
