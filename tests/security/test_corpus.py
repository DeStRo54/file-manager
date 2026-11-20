"""All unique inputs from the reviewed upstream lists, tested without execution."""

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.files.file_safety import safe_path
from app.files.formats import parse_xml, validate_zip

DATA = json.loads(Path(__file__).with_name("corpus.json").read_text(encoding="utf-8"))
PATHS = [case for case in DATA["cases"] if case["category"] == "path_traversal"]
XML = [case for case in DATA["cases"] if case["category"] == "xxe"]


def provenance(case, category, record_property):
    record_property("category", category)
    record_property("payload_id", case["id"])
    record_property("payload", case["payload"])
    record_property("sources", case["sources"])


@pytest.mark.parametrize("case", PATHS, ids=lambda case: case["id"])
def test_path_corpus(case, lab_root, record_property):
    provenance(case, "path_traversal", record_property)
    record_property("expected", "reject or return a lexical path inside the supplied root")
    value = case["payload"].replace("{FILE}", "marker.txt")
    try:
        target = safe_path(lab_root, lab_root, value)
    except (ValueError, OSError):
        return
    assert target.is_relative_to(lab_root)
    assert target != lab_root


@pytest.mark.parametrize("case", PATHS, ids=lambda case: case["id"])
def test_zip_path_corpus(case, lab_root, record_property):
    provenance(case, "zip_slip", record_property)
    record_property("expected", "archive entry rejected or confined without parent components")
    entry = zipfile.ZipInfo(case["payload"].replace("{FILE}", "marker.txt"))
    try:
        validate_zip([entry], lab_root)
    except (ValueError, OSError):
        return
    assert ".." not in Path(entry.filename).parts
    assert safe_path(lab_root, lab_root, entry.filename).is_relative_to(lab_root)


@pytest.mark.parametrize("case", XML, ids=lambda case: case["id"])
def test_xml_corpus(case, record_property):
    provenance(case, "xxe", record_property)
    payload = case["payload"]
    if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
        record_property("expected", "DTD/entity rejected before parser construction")
        with patch("app.files.formats.ET.XMLPullParser") as parser:
            with pytest.raises(ValueError):
                parse_xml(payload)
            parser.assert_not_called()
    else:
        record_property("expected", "parse as inert XML or reject malformed input; no network")
        try:
            parse_xml(payload)
        except (ValueError, ET.ParseError):
            pass
