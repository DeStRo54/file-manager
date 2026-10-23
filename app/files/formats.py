"""Читает и проверяет JSON/XML, создаёт ZIP-архивы и распаковывает их."""

import io
import json
import stat
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from itertools import chain
from pathlib import Path

from .file_safety import (
    ENTRY_LIMIT, UNPACK_LIMIT,
    read_bytes, resolve_path, safe_path, write_bytes,
)

def parse_json(text):
    """Разбирает JSON, проверяя формат, глубину и количество элементов."""
    value = json.loads(text)
    # Ограничиваем глубину и число элементов уже разобранной структуры.
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 32 or count > 10_000:
            raise ValueError("JSON слишком глубокий или содержит слишком много элементов.")
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            children = ()
        stack.extend((child, depth + 1) for child in children)
    json.dumps(value, allow_nan=False)  # NaN и Infinity не входят в обычный JSON.
    return value


def parse_xml(text):
    """Разбирает XML, запрещая объявления сущностей и слишком большое или глубокое дерево."""
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD и объявления сущностей XML запрещены.")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root = None
    for offset in range(0, len(text), 4096):
        parser.feed(text[offset:offset + 4096])
        for event, element in parser.read_events():
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                count += 1
                if depth > 32 or count > 10_000:
                    raise ValueError("XML слишком глубокий или содержит слишком много узлов.")
            else:
                depth -= 1
    parser.close()
    return root


def xml_to_dict(element):
    """Превращает XML-элемент и его дочерние элементы в словари и списки."""
    return {"tag": element.tag, "attributes": element.attrib,
            "text": element.text or "", "tail": element.tail or "",
            "children": [xml_to_dict(child) for child in element]}


def create_zip(source, destination):
    """Проверяет исходные файлы и размеры, затем создаёт новый ZIP-архив."""
    if destination.exists() or source.is_dir() and destination.is_relative_to(source):
        raise ValueError("Архив должен быть новым и находиться вне архивируемой папки.")
    items = []
    total = 0
    candidates = chain([source], source.rglob("*")) if source.is_dir() else [source]
    for item in candidates:
        base = source if source.is_dir() else source.parent
        if item == Path(item.anchor) or any(part.startswith(".fm-") for part in item.relative_to(base).parts):
            continue
        resolve_path(source.parent, str(item), allow_root=True)
        if item.is_file():
            size = item.stat().st_size
            total += size
            if total > UNPACK_LIMIT:
                raise ValueError("Общий размер файлов для ZIP превышает 50 МиБ.")
        items.append(item)
        if len(items) > ENTRY_LIMIT:
            raise ValueError("ZIP содержит больше 1000 записей.")
    with tempfile.TemporaryDirectory(prefix="fm-zip-") as temporary:
        archive_path = Path(temporary) / "archive.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            base = source if source == Path(source.anchor) else source.parent
            for item in items:
                name = item.relative_to(base).as_posix()
                if item.is_dir():
                    archive.writestr(name + "/", b"")
                else:
                    archive.writestr(name, read_bytes(item))
        data = read_bytes(archive_path)
        write_bytes(destination, data)
    return {"entries": len(items), "size": len(data)}


def validate_zip(entries, stage):
    """Проверяет пути, типы записей и размеры ZIP до начала распаковки."""
    if len(entries) > ENTRY_LIMIT:
        raise ValueError("Превышен лимит распакованного ZIP.")

    total_size = 0
    seen = set()
    for entry in entries:
        # orig_filename сохраняет исходное имя, в том числе запрещённый NUL.
        safe_path(stage, stage, entry.orig_filename)
        target = safe_path(stage, stage, entry.filename)
        if ".." in Path(entry.filename).parts:
            raise ValueError("Недопустимый путь в ZIP.")
        if entry.flag_bits & 1 or entry.compress_type not in (0, 8):
            raise ValueError("Недопустимое шифрование или метод сжатия ZIP.")

        file_type = stat.S_IFMT(entry.external_attr >> 16)
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("Ссылка или специальный файл в ZIP.")
        total_size += entry.file_size
        if total_size > UNPACK_LIMIT:
            raise ValueError("Превышен лимит распакованного ZIP.")

        relative = target.relative_to(stage).as_posix()
        if relative.casefold() in seen:
            raise ValueError("Повторяющееся имя в ZIP.")
        seen.add(relative.casefold())


def unpack_zip(archive, entries, stage):
    """Распаковывает проверенные записи во временную папку, следя за размером записанных данных."""
    total_written = 0
    for entry in entries:
        target = safe_path(stage, stage, entry.filename)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        file_written = 0
        with archive.open(entry) as incoming, target.open("xb") as output:
            chunk = incoming.read(64 * 1024)
            while chunk:
                file_written += len(chunk)
                total_written += len(chunk)
                if total_written > UNPACK_LIMIT:
                    raise ValueError("Превышен фактический размер распаковки.")
                output.write(chunk)  # zipfile проверяет CRC при чтении.
                chunk = incoming.read(64 * 1024)
        if file_written != entry.file_size:
            raise ValueError("Размер записи ZIP не совпадает с содержимым.")
    return total_written


def extract_zip(source, destination):
    """Проверяет архив и распаковывает его во временную папку; переносит результат на место только при успехе."""
    if destination.exists():
        raise ValueError("Распаковка требует новую папку.")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".fm-") as temporary:
        stage = Path(temporary) / "result"
        stage.mkdir()
        data = read_bytes(source)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            validate_zip(entries, stage)
            total = unpack_zip(archive, entries, stage)
        if destination.exists():
            raise ValueError("Папка назначения уже существует.")
        stage.rename(destination)  # Публикуем только полностью проверенную папку.
    return {"entries": len(entries), "size": total}
