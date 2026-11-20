"""Previously passing mechanism checks; unverified platform/race cases excluded."""

import contextlib
import io
import os
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from app.files import file_safety as fs
from app.files import formats
from app.files.file_manager import FileManager
from app.cli import print_value, split_command


@pytest.fixture(autouse=True)
def metadata(request, record_property):
    name = request.node.originalname or request.node.name
    record_property("category", name.split("_", 2)[1])
    record_property("adaptation", "Mechanism fixture derived from reviewed categories, not a verbatim payload")
    record_property("source", "https://github.com/swisskyrepo/PayloadsAllTheThings/tree/08ea40e77db3f42ef8f20c65196f3936a19b6c86/")


BAD_NAMES = sorted(set(
    [variant + suffix for name in sorted(fs.RESERVED) for variant in (name, name.lower())
     for suffix in ("", ".txt", ".tar.gz")]
    + [".fm-hidden", "dir/.fm-hidden", "file:stream", "file::$DATA", "file.asax:.jpg",
       "file.asp::$data.", "file.php......", "file.php ", 'file"name', "file<name", "file>name",
       "file|name", "file?name", "file*name", "file\x00.txt", "file\n.txt", "file\r.txt", "file\t.txt"]
))


@pytest.mark.parametrize("name", BAD_NAMES)
def test_filename_rejection(name, tmp_path):
    with pytest.raises(ValueError):
        fs.safe_path(tmp_path, tmp_path, name)


LITERAL_NAMES = ["name.jpg.php", "name.php.jpg", "name.pHp", "name.php%00.gif", "name.php%20",
                 "name.php%0d%0a.jpg", "name.%E2%80%AEphp.jpg", "file.svg", "file.csv", "file.xml",
                 "file.exe", "file.py", "file.tar.gz", "name;marker", "name&marker", "$(marker)",
                 "`marker`", "name%2fmarker", "%252e%252e%252fmarker", "--help", "-rf", "a=b"]


@pytest.mark.parametrize("name", LITERAL_NAMES)
def test_literal_filename_roundtrip(name, tmp_path):
    path = fs.safe_path(tmp_path, tmp_path, name)
    fs.write_bytes(path, b"inert test data")
    assert fs.read_bytes(path) == b"inert test data"
    assert path.name == name
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("name", ["café.txt", "café.txt", "文件.txt", "файл.txt", "．．／marker",
                                 "․․∕marker", "name\u202ephp.jpg", "name\u200b.txt", "U0001f512.txt"])
def test_unicode_roundtrip(name, tmp_path):
    path = fs.safe_path(tmp_path, tmp_path, name)
    assert path.is_relative_to(tmp_path)
    fs.write_bytes(path, b"unicode")
    assert fs.read_bytes(path) == b"unicode"


@pytest.mark.parametrize("value", ["\x1b[31mMARKER\x1b[0m", "\x1b]52;c;MARKER\x07", "name\rmarker", "name\u202emarker"])
def test_terminal_controls_are_escaped(value):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        print_value(value)
    text = output.getvalue()
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text and "\u202e" not in text


@pytest.mark.parametrize("value", LITERAL_NAMES)
def test_cli_quoted_names(value):
    assert split_command(f'cat "{value}"') == ["cat", value]


def test_hardlink_read(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"marker")
    os.link(target, tmp_path / "alias")
    with pytest.raises(ValueError):
        fs.read_bytes(tmp_path / "alias")


def test_read_inode_replacement(tmp_path):
    path = tmp_path / "sample"
    replacement = tmp_path / "replacement"
    path.write_bytes(b"first")
    replacement.write_bytes(b"second")
    real_open = os.open
    def swap(*args, **kwargs):
        os.replace(replacement, path)
        return real_open(*args, **kwargs)
    with patch.object(fs.os, "open", swap), pytest.raises(ValueError):
        fs.read_bytes(path)


def test_atomic_creation_has_one_winner(tmp_path):
    path = tmp_path / "sample"
    barrier = threading.Barrier(2)
    def create(data):
        barrier.wait(timeout=5)
        try:
            fs.write_bytes(path, data)
            return data
        except FileExistsError:
            return None
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(create, data) for data in (b"one", b"two")]
        results = [future.result(timeout=10) for future in futures]
    assert sum(value is not None for value in results) == 1
    assert path.read_bytes() in (b"one", b"two")
    assert not list(tmp_path.glob(".fm-*"))


@pytest.mark.parametrize("size", [7, 8, 9])
def test_write_size_boundary(size, tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_SIZE", 8)
    path = tmp_path / "sample"
    if size > 8:
        with pytest.raises(ValueError):
            fs.write_bytes(path, b"a" * size)
        path.write_bytes(b"a" * size)
        with pytest.raises(ValueError):
            fs.read_bytes(path)
    else:
        fs.write_bytes(path, b"a" * size)
        assert len(fs.read_bytes(path)) == size


@pytest.mark.parametrize("count", [3, 4, 5])
def test_archive_count_boundary(count, tmp_path, monkeypatch):
    monkeypatch.setattr(formats, "ENTRY_LIMIT", 4)
    entries = [zipfile.ZipInfo(f"entry{i}") for i in range(count)]
    if count > 4:
        with pytest.raises(ValueError):
            formats.validate_zip(entries, tmp_path)
    else:
        formats.validate_zip(entries, tmp_path)


@pytest.mark.parametrize("case", ["duplicate", "casefold", "symlink", "fifo", "block", "encrypted", "compression", "size", "nul"])
def test_archive_metadata_rejected(case, tmp_path, monkeypatch):
    entries = [zipfile.ZipInfo("sample")]
    entry = entries[0]
    if case in ("duplicate", "casefold"):
        entries.append(zipfile.ZipInfo("sample" if case == "duplicate" else "SAMPLE"))
    elif case in ("symlink", "fifo", "block"):
        entry.external_attr = {"symlink": stat.S_IFLNK, "fifo": stat.S_IFIFO, "block": stat.S_IFBLK}[case] << 16
    elif case == "encrypted":
        entry.flag_bits |= 1
    elif case == "compression":
        entry.compress_type = zipfile.ZIP_BZIP2
    elif case == "size":
        monkeypatch.setattr(formats, "UNPACK_LIMIT", 8)
        entry.file_size = 9
    else:
        entries = [zipfile.ZipInfo("sample\x00hidden")]
    with pytest.raises(ValueError):
        formats.validate_zip(entries, tmp_path)


@pytest.mark.parametrize("name", ["../marker", "nested/../../marker", "/marker", "C:/marker", "..\\marker"])
def test_archive_no_partial_publication(name, tmp_path):
    marker = tmp_path / "marker"
    marker.write_bytes(b"original")
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("good", b"data")
        archive.writestr(name, b"changed")
    with pytest.raises(ValueError):
        formats.extract_zip(source, tmp_path / "out")
    assert marker.read_bytes() == b"original"
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".fm-*"))


@pytest.mark.parametrize("extension", ["zip", "txt", "png", "tar"])
def test_archive_content_controls_format(extension, tmp_path):
    source = tmp_path / ("source." + extension)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sample.txt", b"data")
    formats.extract_zip(source, tmp_path / "out")
    assert (tmp_path / "out/sample.txt").read_bytes() == b"data"


def test_archive_corrupt_crc_is_atomic(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("sample", b"UNIQUE_BODY")
    source.write_bytes(source.read_bytes().replace(b"UNIQUE_BODY", b"CORRUPT_BODY"))
    with pytest.raises(zipfile.BadZipFile):
        formats.extract_zip(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".fm-*"))


@pytest.mark.parametrize("kind", ["xml_depth", "xml_nodes", "json_depth", "json_nodes", "nan", "infinity"])
def test_json_xml_limits(kind):
    if kind.startswith("xml"):
        text = "<a>" * 34 + "</a>" * 34 if kind.endswith("depth") else "<a>" + "<b/>" * 10001 + "</a>"
        with pytest.raises(ValueError):
            formats.parse_xml(text)
    else:
        text = {"json_depth": "[" * 34 + "0" + "]" * 34,
                "json_nodes": "[" + ",".join("0" for _ in range(10001)) + "]",
                "nan": "NaN", "infinity": "Infinity"}[kind]
        with pytest.raises(ValueError):
            formats.parse_json(text)


@pytest.mark.parametrize("action", ["create", "write", "append", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "json", "xml", "zip"])
def test_manager_operations_in_lab(action, tmp_path):
    database = MagicMock()
    database.save_file.return_value = 1
    manager = FileManager(database, 1, tmp_path)
    source = tmp_path / "source"
    source.write_text("original", encoding="utf-8")
    if action in ("create", "write", "append"):
        name = "source" if action != "create" else "new"
        manager.execute(action, [name, "data"])
        assert (tmp_path / name).read_text() == ("originaldata" if action == "append" else "data")
    elif action == "cat":
        assert manager.execute("cat", ["source"]) == "original"
    elif action in ("cp", "mv"):
        manager.execute(action, ["source", "target"])
        assert (tmp_path / "target").read_text() == "original"
        assert source.exists() == (action == "cp")
    elif action == "rm":
        manager.execute("rm", ["source"])
        assert not source.exists()
    elif action in ("mkdir", "rmdir"):
        if action == "rmdir":
            (tmp_path / "dir").mkdir()
        manager.execute(action, ["dir"])
        assert (tmp_path / "dir").exists() == (action == "mkdir")
    elif action in ("json", "xml"):
        text = '{"value":1}' if action == "json" else "<root/>"
        manager.execute(action + "-write", ["data", text])
        assert manager.execute(action + "-read", ["data"])
    else:
        manager.execute("zip", ["source", "archive.zip"])
        manager.execute("unzip", ["archive.zip", "out"])
        assert (tmp_path / "out/source").read_text() == "original"


def test_resolve_policy_allows_absolute_and_parent_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "marker"
    sibling.write_bytes(b"lab data")
    assert fs.resolve_path(workspace, "../marker") == sibling
    assert fs.resolve_path(workspace, str(sibling)) == sibling
