"""Выполняет файловые команды и сохраняет сведения о файлах и действиях пользователя в БД."""

import json
import shutil
import stat
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psutil

from .file_safety import (
    read_bytes, resolve_path, write_bytes,
)
from .formats import create_zip, extract_zip, parse_json, parse_xml, xml_to_dict


def file_details(path):
    """Возвращает тип и размер объекта: для папки размер считается равным нулю."""
    if path.is_dir():
        return {"kind": "directory", "size": 0}
    return {"kind": "file", "size": path.stat().st_size}


class FileManager:
    """Хранит текущую папку пользователя и выполняет его команды."""
    def __init__(self, database, user_id, start_dir=None):
        """Запоминает пользователя и папку, с которой начинается работа."""
        self.database = database
        self.user_id = user_id
        self.start_dir = resolve_path(Path.cwd(), str(start_dir or Path.cwd()), allow_root=True)
        if not self.start_dir.is_dir():
            raise ValueError("Начальный каталог не существует.")
        self.cwd = self.start_dir
        self.file_id = None

    def inventory(self):
        """Проверяет на диске файлы и папки, учтённые в БД пользователя, и возвращает их тип и размер."""
        result = {}
        for row in self.database.files(self.user_id):
            if row["deleted"]:
                continue
            item = self.path(row["location"], allow_root=True)
            if item.exists():
                result[item.as_posix()] = file_details(item)
        return result

    def reconcile(self):
        """Находит учтённые в БД файлы, которые исчезли или изменили тип либо размер."""
        actual = self.inventory()
        missing = []
        changed = []
        for row in self.database.files(self.user_id):
            if row["deleted"]:
                continue
            location = row["location"]
            recorded = {"kind": row["kind"], "size": row["size"]}
            if location not in actual:
                missing.append(location)
            elif actual[location] != recorded:
                changed.append(location)
        return {
            "missing": sorted(missing),
            "changed": sorted(changed),
            "unfinished": self.database.unfinished(self.user_id),
        }

    def path(self, name, allow_root=False):
        """Получает полный путь от текущей папки и проверяет, можно ли с ним работать."""
        return resolve_path(self.cwd, name, allow_root)

    def remember(self, path):
        """Сохраняет в БД путь, тип и размер объекта, а его ID запоминает для журнала."""
        details = file_details(path)
        self.file_id = self.database.save_file(
            self.user_id, path.as_posix(), details["size"], details["kind"]
        )
        return self.file_id

    def read_bytes(self, path):
        """Читает содержимое файла с проверками безопасности."""
        return read_bytes(path)

    def save_bytes(self, path, data, replace=False):
        """Записывает содержимое файла и обновляет сведения о нём в БД."""
        write_bytes(path, data, replace)
        self.remember(path)

    def execute(self, action, arguments):
        """Находит метод команды, выполняет его и записывает начало и результат в журнал БД."""
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
                command = methods[action]
                result = command(*arguments)
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
        """Возвращает полный путь к текущей папке."""
        return self.cwd.as_posix()

    def ls(self, name="."):
        """Возвращает список файлов и папок в указанном каталоге или в текущем."""
        result = []
        for item in sorted(self.path(name, allow_root=True).iterdir()):
            if item.name.startswith(".fm-"):
                continue
            try:
                self.path(str(item), allow_root=True)
                result.append({"name": item.name, "kind": "directory" if item.is_dir() else "file", "size": item.stat().st_size})
            except ValueError:
                result.append({"name": item.name, "kind": "blocked"})
        return result

    def cd(self, name):
        """Меняет текущую папку, предварительно проверив, что она существует."""
        target = self.path(name, allow_root=True)
        if not target.is_dir():
            raise ValueError("Каталог не найден.")
        self.cwd = target
        return self.pwd()

    def mkdir(self, name):
        """Создаёт папку и записывает сведения о ней в БД."""
        target = self.path(name)
        target.mkdir(mode=0o700)
        self.remember(target)

    def rmdir(self, name):
        """Удаляет пустую папку и помечает её запись в БД как удалённую."""
        target = self.path(name)
        if self.cwd.is_relative_to(target):
            raise ValueError("Нельзя удалить текущую папку.")
        self.remember(target)
        target.rmdir()  # rmdir откажется удалять непустую папку.
        self.database.delete_file(self.user_id, target.as_posix())

    def cat(self, name):
        """Читает файл как текст UTF-8 и обновляет сведения о нём в БД."""
        target = self.path(name)
        text = self.read_bytes(target).decode("utf-8")
        self.remember(target)
        return text

    def write_text(self, name, text, mode):
        """Подготавливает текст к записи: создаёт файл, заменяет содержимое или добавляет текст в конец."""
        target = self.path(name)
        data = text.encode("utf-8")
        if mode == "append" and target.exists():
            previous = self.read_bytes(target)
            previous.decode("utf-8")
            data = previous + data
        self.save_bytes(target, data, replace=mode != "create")

    def create(self, name, text):
        """Создаёт новый текстовый файл; существующий файл не перезаписывает."""
        self.write_text(name, text, "create")

    def write(self, name, text):
        """Записывает текст в файл, заменяя прежнее содержимое, если файл уже есть."""
        self.write_text(name, text, "write")

    def append(self, name, text):
        """Добавляет текст в конец файла; если файла нет, создаёт его."""
        self.write_text(name, text, "append")

    def rm(self, name):
        """Удаляет файл с диска и помечает его запись в БД как удалённую."""
        target = self.path(name)
        self.remember(target)
        target.unlink()
        self.database.delete_file(self.user_id, target.as_posix())

    def cp(self, source, destination):
        """Копирует файл в новое место и сохраняет сведения о копии в БД."""
        source = self.path(source)
        destination = self.path(destination)
        data = self.read_bytes(source)
        self.remember(source)
        self.save_bytes(destination, data)

    def mv(self, source, destination):
        """Копирует файл в новое место, затем удаляет исходный файл."""
        self.cp(source, destination)
        target = self.path(source)
        target.unlink()
        self.database.delete_file(self.user_id, target.as_posix())

    def info(self, name):
        """Возвращает имя, тип, размер, время изменения и права доступа к объекту."""
        target = self.path(name, allow_root=True)
        info = target.stat()
        self.remember(target)
        return {"name": target.name, "kind": "directory" if target.is_dir() else "file", "size": info.st_size,
                "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(), "permissions": stat.filemode(info.st_mode)}

    def json_write(self, name, text):
        """Проверяет JSON и записывает его в файл с отступами для удобного чтения."""
        value = parse_json(text)
        self.write(name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    def json_read(self, name):
        """Читает JSON-файл и возвращает разобранные данные."""
        return parse_json(self.cat(name))

    def xml_write(self, name, text):
        """Проверяет XML и сохраняет его в файл."""
        root = parse_xml(text)
        self.write(name, ET.tostring(root, encoding="unicode"))

    def xml_read(self, name):
        """Читает XML-файл и превращает его дерево в словарь для вывода."""
        root = parse_xml(self.cat(name))
        return xml_to_dict(root)

    def zip(self, source, destination):
        """Создаёт ZIP из файла или папки и записывает сведения об архиве в БД."""
        source = self.path(source, allow_root=True)
        destination = self.path(destination)
        result = create_zip(source, destination)
        self.remember(destination)
        return result

    def unzip(self, source, destination):
        """Распаковывает ZIP в новую папку и записывает сведения об извлечённых объектах в БД."""
        source = self.path(source)
        destination = self.path(destination)
        result = extract_zip(source, destination)
        for item in destination.rglob("*"):
            self.remember(item)
        self.remember(destination)
        return result

    def disks(self):
        """Возвращает список доступных дисков, их объём и свободное место."""
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
            usage = shutil.disk_usage(self.cwd)
            result.append({"mountpoint": self.cwd.anchor, "filesystem": "не определена", "total": usage.total, "free": usage.free})
        return result

    def dbfiles(self):
        """Возвращает записи БД о файлах и папках текущего пользователя, включая удалённые."""
        return self.database.files(self.user_id)

    def history(self, limit="20"):
        """Возвращает последние события из журнала текущего пользователя."""
        return self.database.history(self.user_id, int(limit))
