"""Принимает команды из терминала, запрашивает данные и показывает результат."""

import argparse
import getpass
import json
import os
import shlex
import sys
import zipfile
import xml.etree.ElementTree as ET

import psycopg
from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_session
from prompt_toolkit.key_binding import KeyBindings

from db.database import Database
from .files import FileManager

HELP = """
Учётные записи: register, login, logout, whoami
Каталоги: ls [путь], pwd, cd <путь>, mkdir <путь>, rmdir <путь>
Файлы: create <путь>, cat <путь>, write <путь>, append <путь>, rm <путь>
       cp <источник> <назначение>, mv <источник> <назначение>, info <путь>
JSON/XML: json-write <путь>, json-read <путь>, xml-write <путь>, xml-read <путь>
Архивы: zip <файл/папка> <новый.zip>, unzip <архив.zip> <новая папка>
Система/БД: disks, dbfiles, history [1..100], reconcile (сверка без изменений)
Справка/выход: help, exit
Можно указывать абсолютные пути. Пути с пробелами: в кавычках. cd .. — на уровень выше; cd / — в корень диска.
При вводе текста: Enter — новая строка, Ctrl+S — сохранить, Ctrl+C — отменить.
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
    """Запрашивает пароль и скрывает ввод, если программа запущена в терминале."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return input(prompt)


def edit_text():
    """Открывает ввод текста: Enter — новая строка, Ctrl+S — сохранить, Ctrl+C — отменить."""
    bindings = KeyBindings()

    @bindings.add("c-s")
    def save(event):
        event.app.exit(result=event.current_buffer.text)

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    # Явный вывод не даёт TERM=dumb отключить наши горячие клавиши.
    session = PromptSession(multiline=True, key_bindings=bindings, output=get_app_session().output)
    return session.prompt("")


def read_text():
    """Получает текст для файла; при отмене возвращает None."""
    if sys.stdin.isatty():
        print("Введите текст: Enter — новая строка, Ctrl+S — сохранить, Ctrl+C — отмена.")
        text = edit_text()
    else:
        # Скрипты и автоматические проверки передают содержимое одной строкой.
        text = input()
    if text is None or text == ".cancel":
        return None
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def print_value(value):
    """Показывает результат команды: текст как текст, списки и словари — в виде JSON."""
    if value is None:
        print("Готово.")
    elif isinstance(value, str):
        # Управляющий текст файла не должен выполнять команды терминала.
        characters = []
        for char in value:
            if char.isprintable() or char in "\n\t":
                characters.append(char)
            else:
                characters.append(f"\\u{ord(char):04x}")
        print("".join(characters))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def split_command(line):
    """Разделяет команду и аргументы, сохраняя пути в кавычках и обратные слеши Windows."""
    parser = shlex.shlex(line, posix=True)
    parser.whitespace_split = True
    parser.commenters = ""
    parser.escape = ""
    return list(parser)


def register_user(database):
    """Спрашивает логин и пароль, проверяет повтор пароля и создаёт учётную запись."""
    username = input("Логин: ").strip()
    password = read_password("Пароль: ")
    if password != read_password("Повторите пароль: "):
        raise ValueError("Пароли не совпадают.")
    database.register(username, password)
    print("Пользователь создан. Теперь выполните login.")


def login_user(database):
    """Проверяет логин и пароль и готовит файловый менеджер для вошедшего пользователя."""
    username = input("Логин: ").strip()
    password = read_password("Пароль: ")
    user = database.login(username, password)
    manager = FileManager(database, user["id"])
    print("Вход выполнен. Рабочая папка:", manager.cwd)
    if database.unfinished(user["id"]):
        print("Есть незавершённые операции. Выполните reconcile для сверки.")
    return user, manager


def run_file_command(manager, command, arguments):
    """Проверяет аргументы, при необходимости спрашивает подтверждение и запускает команду."""
    if command in ARGUMENTS:
        minimum, maximum = ARGUMENTS[command]
        if not minimum <= len(arguments) <= maximum:
            raise ValueError("Неверное число аргументов. Введите help.")

    needs_confirmation = command == "rm"
    if command in ("write", "json-write", "xml-write"):
        needs_confirmation = manager.path(arguments[0]).exists()
    if needs_confirmation:
        answer = input("Подтвердите удаление/перезапись (да): ").strip().lower()
        if answer != "да":
            manager.database.log(manager.user_id, command + ":cancel", "success")
            print("Отменено.")
            return

    if command in ("create", "write", "append", "json-write", "xml-write"):
        text = read_text()
        if text is None:
            manager.database.log(manager.user_id, command + ":cancel", "success")
            return
        arguments = [*arguments, text]
    print_value(manager.execute(command, arguments))


def console(database):
    """Читает и выполняет команды, пока пользователь не введёт exit."""
    user = manager = None
    print("Консольный файловый менеджер — Python + PostgreSQL 17. Введите help.")
    while True:
        try:
            if user:
                prompt = user["username"] + ":" + manager.pwd()
            else:
                prompt = "guest"
            tokens = split_command(input(prompt + "> "))
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
                if command == "register":
                    register_user(database)
                else:
                    user, manager = login_user(database)
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
                run_file_command(manager, command, arguments)
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break
        except (ValueError, OSError, TypeError, RecursionError, ET.ParseError, zipfile.BadZipFile) as error:
            database.log(user["id"] if user else None, "command:failed", "failed", {"error": type(error).__name__})
            print("Ошибка:", error)
    if user:
        database.log(user["id"], "logout", "success", {"reason": "exit"})


def main():
    """Читает параметры запуска, подключается к БД и открывает консоль."""
    parser = argparse.ArgumentParser(description="Консольный файловый менеджер на Python")
    parser.add_argument("--check-db", action="store_true", help="Проверить БД и хранимые процедуры")
    arguments = parser.parse_args()
    os.umask(0o077)
    database = Database()
    try:
        if arguments.check_db:
            print_value(database.check())
            return
        database.lock()
        console(database)
    finally:
        database.close()


def run():
    """Запускает приложение и выводит понятное сообщение, если запуск не удался."""
    try:
        main()
    except psycopg.Error:
        print("Ошибка PostgreSQL. Проверьте параметры PG* в .env, запустите docker compose up -d --wait db и подготовьте схему через python -m db.init_db.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as error:
        print("Ошибка запуска:", error, file=sys.stderr)
        sys.exit(1)
