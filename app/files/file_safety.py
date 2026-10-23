"""Проверяет пути и размеры файлов, читает содержимое и записывает его через временный файл."""

import os
import stat
import tempfile
from pathlib import Path, PureWindowsPath
 
UNPACK_LIMIT = 50 * 1024 * 1024 # Общий объём файлов после распаковки ZIP — не больше 50 МиБ.
ENTRY_LIMIT = 1000 # До 1000 записей (файлов и папок) в ZIP: даже множество пустых файлов
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{name}{i}" for name in ("COM", "LPT") for i in range(1, 10)} # Служебные имена устройств Windows: их нельзя использовать как имена файлов.

MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 ГиБ


def safe_path(root, current, name, allow_root=False):
    """Проверяет, что путь остаётся внутри указанной папки и не содержит запрещённых имён или ссылок."""
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
        is_file = stat.S_ISREG(info.st_mode)
        is_directory = stat.S_ISDIR(info.st_mode)
        if not is_file and not is_directory:
            raise ValueError("Специальные файлы запрещены.")
        if is_file and info.st_nlink > 1:
            raise ValueError("Жёсткие ссылки запрещены.")
    return target


def resolve_path(current, name, allow_root=False):
    """Превращает введённый путь в полный и проверяет его."""
    if not name:
        raise ValueError("Укажите путь к файлу или папке.")
    requested = Path(name).expanduser()
    if PureWindowsPath(name).drive and (os.name != "nt" or not requested.is_absolute()):
        raise ValueError("Укажите полный путь с диском, например D:/Documents/note.txt.")
    target = Path(os.path.abspath(current / requested))
    filesystem_root = Path(target.anchor)
    relative = target.relative_to(filesystem_root).as_posix()
    return safe_path(filesystem_root, filesystem_root, relative, allow_root)


def read_bytes(path):
    """Открывает обычный файл, проверяет подмену."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Можно читать только обычные файлы.")
    if before.st_size > MAX_FILE_SIZE:
        raise ValueError("Файл слишком большой. Максимум 1 ГиБ.")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("Файл подменён, является ссылкой или специальным файлом.")
        if opened.st_size > MAX_FILE_SIZE:
            raise ValueError("Файл слишком большой. Максимум 1 ГиБ.")
        data = source.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise ValueError("Превышен допустимый размер файла.")
    return data


def write_bytes(path, data, replace=False):
    """Записывает данные через временный файл."""
    if not isinstance(data, bytes):
        raise TypeError("Данные должны иметь тип bytes.")
    if len(data) > MAX_FILE_SIZE:
        raise ValueError("Файл слишком большой. Максимум 1 ГиБ.")
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