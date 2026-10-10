"""Работа с файлами. Database передаётся в конструктор для метаданных и журнала."""

import io
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path, PureWindowsPath

import psutil

TEXT_LIMIT = 1024 * 1024
FILE_LIMIT = 20 * 1024 * 1024
ZIP_LIMIT = 32 * 1024 * 1024
UNPACK_LIMIT = 50 * 1024 * 1024
ENTRY_LIMIT = 1000
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{name}{i}" for name in ("COM", "LPT") for i in range(1, 10)}


def safe_path(root, current, name, allow_root=False):
    """Любой путь остаётся внутри root; ссылки и специальные файлы запрещены."""
    if not name or Path(name).is_absolute() or PureWindowsPath(name).drive or "\\" in name:
        raise ValueError("Используйте относительный путь внутри рабочей папки.")
    for part in Path(name).parts:
        if (part.startswith(".fm-") or part.split(".")[0].upper() in RESERVED
                or part.endswith((" ", ".")) and part not in (".", "..")
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)):
            raise ValueError("Недопустимое или служебное имя файла.")
    target = Path(os.path.abspath(current / name))
    if not target.is_relative_to(root) or (target == root and not allow_root):
        raise ValueError("Выход за рабочую папку и изменение её корня запрещены.")
    if len(target.relative_to(root).parts) > 32:
        raise ValueError("Слишком глубокий путь.")
    for item in (target, *target.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Символические ссылки запрещены.")
        if item == root:
            break
    if target.exists():
        info = target.stat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_nlink > 1 and target.is_file():
            raise ValueError("Специальные файлы и жёсткие ссылки запрещены.")
    return target


def parse_json(text):
    if len(text.encode("utf-8")) > TEXT_LIMIT:
        raise ValueError("JSON больше 1 МиБ.")
    value = json.loads(text)
    # Ограничиваем глубину и число элементов уже разобранной структуры.
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 32 or count > 10_000:
            raise ValueError("JSON слишком глубокий или содержит слишком много элементов.")
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        stack.extend((child, depth + 1) for child in children)
    json.dumps(value, allow_nan=False)  # NaN и Infinity не входят в обычный JSON.
    return value


def parse_xml(text):
    if len(text.encode("utf-8")) > TEXT_LIMIT:
        raise ValueError("XML больше 1 МиБ.")
    # Сущности объявляются в DTD: запрещаем их до обращения к XML-парсеру.
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD и объявления сущностей XML запрещены.")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root = None
    for offset in range(0, len(text), 4096):
        parser.feed(text[offset:offset + 4096])
        for event, element in parser.read_events():
            if event == "start":
                root = element if root is None else root
                depth += 1
                count += 1
                if depth > 32 or count > 10_000:
                    raise ValueError("XML слишком глубокий или содержит слишком много узлов.")
            else:
                depth -= 1
    parser.close()
    return root


def xml_to_dict(element):
    """Рекурсивно превращаем XML-дерево в обычные словари и списки."""
    return {"tag": element.tag, "attributes": element.attrib,
            "text": element.text or "", "tail": element.tail or "",
            "children": [xml_to_dict(child) for child in element]}


class FileManager:
    def __init__(self, database, user_id, data_dir):
        self.database = database
        self.user_id = user_id
        workspaces = Path(data_dir).resolve() / "workspaces"
        self.root = workspaces / str(user_id)
        for directory in (workspaces, self.root):
            if directory.is_symlink() or directory.is_junction():
                raise ValueError("Рабочая папка не должна быть ссылкой.")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cwd = self.root
        self.file_id = None  # ID основного файла последней операции для журнала.
        self.quota_bytes = int(os.getenv("FM_QUOTA_BYTES", "209715200"))
        self.quota_entries = int(os.getenv("FM_QUOTA_ENTRIES", "2000"))
        self.min_free = int(os.getenv("FM_MIN_FREE_BYTES", "16777216"))
        if min(self.quota_bytes, self.quota_entries) <= 0 or self.min_free < 0:
            raise ValueError("Некорректные квоты или резерв свободного места.")

    def inventory(self):
        result = {}
        def fail(error):
            raise error
        for directory, folders, files in os.walk(self.root, followlinks=False, onerror=fail):
            folders[:] = [name for name in folders if not name.startswith(".fm-")]
            files = [name for name in files if not name.startswith(".fm-")]
            for name in folders + files:
                item = Path(directory) / name
                relative = item.relative_to(self.root).as_posix()
                safe_path(self.root, self.root, relative)
                result[relative] = {"kind": "directory" if item.is_dir() else "file",
                                    "size": 0 if item.is_dir() else item.stat().st_size}
        return result

    def check_quota(self, added_bytes=0, added_entries=0, temporary_bytes=0):
        inventory = self.inventory()
        if sum(row["size"] for row in inventory.values()) + added_bytes > self.quota_bytes:
            raise ValueError("Превышена общая квота пользователя в байтах.")
        if len(inventory) + added_entries > self.quota_entries:
            raise ValueError("Превышена квота файлов и папок пользователя.")
        if shutil.disk_usage(self.root).free < temporary_bytes + self.min_free:
            raise ValueError("Недостаточно свободного места с учётом резерва.")

    def reconcile(self):
        actual = self.inventory()
        recorded = {row["location"]: {"kind": row["kind"], "size": row["size"]}
                    for row in self.database.files(self.user_id) if not row["deleted"]}
        return {"untracked": sorted(actual.keys() - recorded.keys()),
                "missing": sorted(recorded.keys() - actual.keys()),
                "changed": sorted(name for name in actual.keys() & recorded.keys()
                                  if actual[name] != recorded[name]),
                "unfinished": self.database.unfinished(self.user_id)}

    def path(self, name, allow_root=False):
        return safe_path(self.root, self.cwd, name, allow_root)

    def remember(self, path):
        kind = "directory" if path.is_dir() else "file"
        self.file_id = self.database.save_file(self.user_id, path.relative_to(self.root).as_posix(),
                                                0 if kind == "directory" else path.stat().st_size, kind)
        return self.file_id

    def read_bytes(self, path, limit=FILE_LIMIT):
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError("Файл подменён, является ссылкой или специальным файлом.")
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"Файл превышает лимит {limit // 1024} КиБ.")
        return data

    def save_bytes(self, path, data, replace=False):
        previous_size = path.stat().st_size if replace and path.is_file() else 0
        self.check_quota(len(data) - previous_size, int(not path.exists()), len(data))
        # Сначала пишем временный файл. Пользователь получает завершённую запись.
        temporary = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fm-", delete=False)
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            if replace:
                os.replace(temporary_path, path)
            else:
                os.link(temporary_path, path)  # Существующий файл не перезапишется.
        finally:
            temporary_path.unlink(missing_ok=True)
        self.remember(path)

    def execute(self, action, arguments):
        """Одна команда = один метод. Общая часть только записывает аудит."""
        methods = {"ls": self.ls, "pwd": self.pwd, "cd": self.cd, "mkdir": self.mkdir,
                   "rmdir": self.rmdir, "create": self.create, "cat": self.cat,
                   "write": self.write, "append": self.append, "rm": self.rm,
                   "cp": self.cp, "mv": self.mv, "info": self.info,
                   "json-write": self.json_write, "json-read": self.json_read,
                   "xml-write": self.xml_write, "xml-read": self.xml_read,
                   "zip": self.zip, "unzip": self.unzip, "disks": self.disks,
                   "dbfiles": self.dbfiles, "history": self.history, "reconcile": self.reconcile}
        self.file_id = None
        previous_dir = self.cwd
        operation = {"operation_id": uuid.uuid4().hex}
        self.database.log(self.user_id, action, "started", operation)
        try:
            with self.database.connection.transaction():
                self.database.connection.execute("SELECT pg_advisory_xact_lock(734912, %s)", (self.user_id,))
                if action not in methods:
                    raise ValueError("Неизвестная команда. Введите help.")
                result = methods[action](*arguments)
                self.database.log(self.user_id, action, "success", operation, file_id=self.file_id)
            return result
        except Exception as error:
            self.cwd = previous_dir
            try:
                self.database.log(self.user_id, action, "failed", dict(operation, error=type(error).__name__))
            except Exception:
                pass  # started сохранён; не скрываем первоначальную ошибку отказом журнала.
            raise

    def pwd(self):
        relative = self.cwd.relative_to(self.root)
        return "/" if relative == Path(".") else "/" + relative.as_posix()

    def ls(self, name="."):
        result = []
        for item in sorted(self.path(name, allow_root=True).iterdir()):
            if item.name.startswith(".fm-"):
                continue
            try:
                safe_path(self.root, self.root, item.relative_to(self.root).as_posix())
                result.append({"name": item.name, "kind": "directory" if item.is_dir() else "file", "size": item.stat().st_size})
            except ValueError:
                result.append({"name": item.name, "kind": "blocked"})
        return result

    def cd(self, name):
        target = self.root if name == "/" else self.path(name, allow_root=True)
        if not target.is_dir():
            raise ValueError("Каталог не найден.")
        self.cwd = target
        return self.pwd()

    def mkdir(self, name):
        target = self.path(name)
        self.check_quota(added_entries=1)
        target.mkdir(mode=0o700)
        self.remember(target)

    def rmdir(self, name):
        target = self.path(name)
        if self.cwd.is_relative_to(target):
            raise ValueError("Нельзя удалить текущую папку.")
        self.remember(target)
        target.rmdir()  # rmdir откажется удалять непустую папку.
        self.database.delete_file(self.user_id, target.relative_to(self.root).as_posix())

    def cat(self, name):
        target = self.path(name)
        text = self.read_bytes(target, TEXT_LIMIT).decode("utf-8")
        self.remember(target)
        return text

    def write_text(self, name, text, mode):
        target = self.path(name)
        data = text.encode("utf-8")
        if mode == "append" and target.exists():
            previous = self.read_bytes(target, TEXT_LIMIT)
            previous.decode("utf-8")
            data = previous + data
        if len(data) > TEXT_LIMIT:
            raise ValueError("Текстовый файл больше 1 МиБ.")
        self.save_bytes(target, data, replace=mode != "create")

    def create(self, name, text):
        self.write_text(name, text, "create")

    def write(self, name, text):
        self.write_text(name, text, "write")

    def append(self, name, text):
        self.write_text(name, text, "append")

    def rm(self, name):
        target = self.path(name)
        self.remember(target)
        target.unlink()
        self.database.delete_file(self.user_id, target.relative_to(self.root).as_posix())

    def cp(self, source, destination):
        source, destination = self.path(source), self.path(destination)
        data = self.read_bytes(source)
        self.remember(source)
        self.save_bytes(destination, data)

    def mv(self, source, destination):
        self.cp(source, destination)
        target = self.path(source)
        target.unlink()
        self.database.delete_file(self.user_id, target.relative_to(self.root).as_posix())

    def info(self, name):
        target = self.path(name, allow_root=True)
        info = target.stat()
        if target != self.root:
            self.remember(target)
        return {"name": target.name, "kind": "directory" if target.is_dir() else "file", "size": info.st_size,
                "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(), "permissions": stat.filemode(info.st_mode)}

    def json_write(self, name, text):
        value = parse_json(text)
        self.write(name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    def json_read(self, name):
        return parse_json(self.cat(name))

    def xml_write(self, name, text):
        root = parse_xml(text)
        self.write(name, ET.tostring(root, encoding="unicode"))

    def xml_read(self, name):
        root = parse_xml(self.cat(name))
        return xml_to_dict(root)

    def zip(self, source, destination):
        source, destination = self.path(source, allow_root=True), self.path(destination)
        if destination.exists() or source.is_dir() and source != self.root and destination.is_relative_to(source):
            raise ValueError("Архив должен быть новым и находиться вне архивируемой папки.")
        items = []
        total = 0
        candidates = chain([source], source.rglob("*")) if source.is_dir() else [source]
        for item in candidates:
            if item == self.root or any(part.startswith(".fm-") for part in item.relative_to(self.root).parts):
                continue
            safe_path(self.root, self.root, item.relative_to(self.root).as_posix())
            if item.is_file():
                size = item.stat().st_size
                total += size
                if size > FILE_LIMIT or total > UNPACK_LIMIT:
                    raise ValueError("Лимит ZIP: файл до 20 МиБ, всего до 50 МиБ.")
            items.append(item)
            if len(items) > ENTRY_LIMIT:
                raise ValueError("ZIP содержит больше 1000 записей.")
        self.check_quota(temporary_bytes=ZIP_LIMIT * 2)
        with tempfile.TemporaryDirectory(prefix="fm-zip-") as temporary:
            if shutil.disk_usage(temporary).free < UNPACK_LIMIT + ENTRY_LIMIT * 1024 + self.min_free:
                raise ValueError("Недостаточно места для временного ZIP.")
            archive_path = Path(temporary) / "archive.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                base = source if source == self.root else source.parent
                for item in items:
                    name = item.relative_to(base).as_posix()
                    if item.is_dir():
                        archive.writestr(name + "/", b"")
                    else:
                        archive.writestr(name, self.read_bytes(item))
            data = self.read_bytes(archive_path, ZIP_LIMIT)
            self.save_bytes(destination, data)
        return {"entries": len(items), "size": len(data)}

    def unzip(self, source, destination):
        source, destination = self.path(source), self.path(destination)
        if destination.exists():
            raise ValueError("Распаковка требует новую папку.")
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".fm-") as temporary:
            stage = Path(temporary) / "result"
            stage.mkdir()
            with zipfile.ZipFile(io.BytesIO(self.read_bytes(source, ZIP_LIMIT))) as archive:
                entries = archive.infolist()
                if len(entries) > ENTRY_LIMIT or sum(item.file_size for item in entries) > UNPACK_LIMIT:
                    raise ValueError("Превышен лимит распакованного ZIP.")
                paths = set()
                for item in entries:
                    safe_path(stage, stage, item.orig_filename)
                    target = safe_path(stage, stage, item.filename)
                    paths.add(target.relative_to(stage).as_posix())
                    paths.update(parent.relative_to(stage).as_posix() for parent in target.parents if parent != stage and parent.is_relative_to(stage))
                size = sum(item.file_size for item in entries)
                self.check_quota(size, len(paths) + 1, size)
                seen = set()
                total = 0
                for item in entries:
                    if ".." in Path(item.filename).parts or item.flag_bits & 1 or item.compress_type not in (0, 8):
                        raise ValueError("Недопустимый путь, шифрование или метод сжатия ZIP.")
                    mode = stat.S_IFMT(item.external_attr >> 16)
                    if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or item.file_size > FILE_LIMIT:
                        raise ValueError("Ссылка, специальный файл или слишком большой файл в ZIP.")
                    target = safe_path(stage, stage, item.filename)
                    key = target.relative_to(stage).as_posix().casefold()
                    if key in seen:
                        raise ValueError("Повторяющееся имя в ZIP.")
                    seen.add(key)
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with archive.open(item) as incoming, target.open("xb") as output:
                        while chunk := incoming.read(64 * 1024):
                            written += len(chunk)
                            total += len(chunk)
                            if written > FILE_LIMIT or total > UNPACK_LIMIT:
                                raise ValueError("Превышен фактический размер распаковки.")
                            output.write(chunk)  # zipfile проверяет CRC при чтении.
                    if written != item.file_size:
                        raise ValueError("Размер записи ZIP не совпадает с содержимым.")
            if destination.exists():
                raise ValueError("Папка назначения уже существует.")
            stage.rename(destination)  # Публикуем только полностью проверенную папку.
        for item in destination.rglob("*"):
            self.remember(item)
        self.remember(destination)
        return {"entries": len(entries), "size": total}

    def disks(self):
        result = []
        try:
            partitions = psutil.disk_partitions() or psutil.disk_partitions(all=True)
        except OSError:
            partitions = []  # В ограниченной среде список дисков может быть недоступен.
        for partition in partitions:
            if partition.fstype in ("proc", "sysfs", "tmpfs", "devpts", "cgroup2", "mqueue"):
                continue
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                result.append({"device": partition.device, "mountpoint": partition.mountpoint,
                               "filesystem": partition.fstype, "total": usage.total, "free": usage.free})
            except OSError:
                continue
        if not result:
            usage = shutil.disk_usage(self.root)
            result.append({"mountpoint": self.root.anchor, "filesystem": "не определена", "total": usage.total, "free": usage.free})
        return result

    def dbfiles(self):
        return self.database.files(self.user_id)

    def history(self, limit="20"):
        return self.database.history(self.user_id, int(limit))
