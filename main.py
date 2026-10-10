"""Ввод и вывод. Здесь выбирается команда, но сами файлы обрабатывает FileManager."""

import argparse
import getpass
import json
import os
import shlex
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg

from database import Database, PROJECT_DIR
from file_manager import FileManager, TEXT_LIMIT

HELP = """
Учётные записи: register, login, logout, whoami
Каталоги: ls [путь], pwd, cd <путь>, mkdir <путь>, rmdir <путь>
Файлы: create <путь>, cat <путь>, write <путь>, append <путь>, rm <путь>
       cp <источник> <назначение>, mv <источник> <назначение>, info <путь>
JSON/XML: json-write <путь>, json-read <путь>, xml-write <путь>, xml-read <путь>
Архивы: zip <файл/папка> <новый.zip>, unzip <архив.zip> <новая папка>
Система/БД: disks, dbfiles, history [1..100], reconcile (сверка без изменений)
Справка/выход: help, exit
Пути с пробелами: в кавычках. cd / возвращает в корень пользователя.
Текст вводите построчно; .end завершает ввод, .cancel отменяет.
"""
# Минимальное и максимальное число аргументов каждой файловой команды.
ARGUMENTS = {
    "reconcile": (0, 0),
    "ls": (0, 1), "pwd": (0, 0), "cd": (1, 1), "mkdir": (1, 1), "rmdir": (1, 1),
    "create": (1, 1), "cat": (1, 1), "write": (1, 1), "append": (1, 1), "rm": (1, 1),
    "cp": (2, 2), "mv": (2, 2), "info": (1, 1), "json-write": (1, 1), "json-read": (1, 1),
    "xml-write": (1, 1), "xml-read": (1, 1), "zip": (2, 2), "unzip": (2, 2),
    "disks": (0, 0), "dbfiles": (0, 0), "history": (0, 1),
}


def read_password(prompt):
    # В терминале пароль скрыт. Pipe нужен только для автоматических проверок.
    return getpass.getpass(prompt) if sys.stdin.isatty() else input(prompt)


def read_text():
    print("Введите текст; завершите отдельной строкой .end:")
    lines = []
    size = 0
    while True:
        line = input()
        if line == ".end":
            return "".join(lines)
        if line == ".cancel":
            return None
        line += "\n"
        size += len(line.encode("utf-8"))
        if size > TEXT_LIMIT:
            raise ValueError("Ввод больше 1 МиБ.")
        lines.append(line)


def print_value(value):
    if value is None:
        print("Готово.")
    elif isinstance(value, str):
        # Управляющий текст файла не должен выполнять команды терминала.
        print("".join(char if char.isprintable() or char in "\n\t" else f"\\u{ord(char):04x}" for char in value))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def console(database, data_dir):
    user = manager = None
    print("Консольный файловый менеджер — Python + PostgreSQL 17. Введите help.")
    while True:
        try:
            prompt = user["username"] + ":" + manager.pwd() if user else "guest"
            tokens = shlex.split(input(prompt + "> "))
            if not tokens:
                continue
            command, *arguments = tokens
            if command in ("register", "login", "logout", "whoami", "help", "exit") and arguments:
                raise ValueError("Эта команда не принимает аргументов.")
            if command == "exit":
                break
            if command == "help":
                print(HELP)
                database.log(user["id"] if user else None, "help", "success")
            elif command in ("register", "login"):
                if user:
                    raise ValueError("Сначала выполните logout.")
                username = input("Логин: ").strip()
                password = read_password("Пароль: ")
                if command == "register":
                    if password != read_password("Повторите пароль: "):
                        raise ValueError("Пароли не совпадают.")
                    database.register(username, password)
                    print("Пользователь создан. Теперь выполните login.")
                else:
                    candidate = database.login(username, password)
                    manager = FileManager(database, candidate["id"], data_dir)
                    user = candidate
                    print("Вход выполнен. Рабочая папка:", manager.root)
                    if database.unfinished(user["id"]):
                        print("Есть незавершённые операции. Выполните reconcile для сверки.")
            elif not user:
                raise ValueError("Сначала выполните login.")
            elif command == "logout":
                database.log(user["id"], "logout", "success")
                user = manager = None
                print("Вы вышли из системы.")
            elif command == "whoami":
                print_value(user)
                database.log(user["id"], "whoami", "success")
            else:
                if command in ARGUMENTS:
                    minimum, maximum = ARGUMENTS[command]
                    if not minimum <= len(arguments) <= maximum:
                        raise ValueError("Неверное число аргументов. Введите help.")
                if command == "rm" or command in ("write", "json-write", "xml-write") and manager.path(arguments[0]).exists():
                    if input("Подтвердите удаление/перезапись (да): ").strip().lower() != "да":
                        database.log(user["id"], command + ":cancel", "success")
                        print("Отменено.")
                        continue
                if command in ("create", "write", "append", "json-write", "xml-write"):
                    text = read_text()
                    if text is None:
                        database.log(user["id"], command + ":cancel", "success")
                        continue
                    arguments.append(text)
                print_value(manager.execute(command, arguments))
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break
        except (ValueError, OSError, TypeError, RecursionError, ET.ParseError, zipfile.BadZipFile) as error:
            database.log(user["id"] if user else None, "command:failed", "failed", {"error": type(error).__name__})
            print("Ошибка:", error)
    if user:
        database.log(user["id"], "logout", "success", {"reason": "exit"})


def main():
    parser = argparse.ArgumentParser(description="Консольный файловый менеджер на Python")
    parser.add_argument("--data", type=Path, default=PROJECT_DIR / "data", help="Каталог рабочих файлов")
    parser.add_argument("--check-db", action="store_true", help="Проверить БД и хранимые процедуры")
    arguments = parser.parse_args()
    os.umask(0o077)
    database = Database()
    lock = arguments.data / "manager.lock"
    lock_created = False
    try:
        if arguments.check_db:
            print_value(database.check())
            return
        arguments.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with lock.open("x", encoding="utf-8") as output:
                output.write(str(os.getpid()))
            lock_created = True
        except FileExistsError:
            raise ValueError("Найден manager.lock: закройте другую копию программы. После аварии проверьте, что она завершена.") from None
        console(database, arguments.data)
    finally:
        if lock_created:
            lock.unlink(missing_ok=True)
        database.close()


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error:
        print("Ошибка PostgreSQL. Проверьте параметры PG* в .env, запустите docker compose up -d --wait db и подготовьте схему через python init_db.py.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as error:
        print("Ошибка запуска:", error, file=sys.stderr)
        sys.exit(1)
