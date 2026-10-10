"""Запуск: python -m unittest discover -s tests -v (нужен PostgreSQL)."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg import sql

from database import Database, PROJECT_DIR, password_key, check_password
from file_manager import FileManager, parse_json, parse_xml, TEXT_LIMIT
from init_db import provision


class FormatTests(unittest.TestCase):
    def test_json_and_xml_checks(self):
        self.assertEqual(parse_json('{"name":"Иван","age":20}')["age"], 20)
        self.assertEqual(parse_xml('<person name="Иван"><age>20</age></person>').tag, "person")
        for text in ('{bad}', '{"x":NaN}', '[' * 40 + '0' + ']' * 40):
            with self.assertRaises((ValueError, RecursionError)):
                parse_json(text)
        with self.assertRaises(ValueError):
            parse_xml('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
        with self.assertRaises(ValueError):
            parse_xml('<x>' * 40 + '</x>' * 40)

    def test_scrypt_uses_salt(self):
        password = "Password42!"
        first, second = password_key(password, "0" * 32), password_key(password, "1" * 32)
        self.assertNotEqual(first, second)
        stored = "scrypt$32768$8$3$" + "0" * 32 + "$" + first
        self.assertTrue(check_password(password, stored))
        self.assertFalse(check_password("wrong", stored))

    def test_previous_node_hash(self):
        # Значение получено предыдущей Node.js-версией с той же ASCII-солью.
        stored = ("scrypt$32768$8$3$0123456789abcdef0123456789abcdef$"
                  "2740dba459e983294efcde9adca40393a9fb8ac4c0f0d5ea215bf192f971a2b9c"
                  "ab56da28fcbcc22da3e90e4886deef369907078f2f811e6d0f0f1d9fdc6aecd")
        self.assertTrue(check_password("LegacyPassword42!", stored))


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Одна новая схема на весь запуск; рабочая public не затрагивается.
        cls.schema = "fm_test_" + uuid.uuid4().hex
        provision(cls.schema)
        cls.database = Database(schema=cls.schema)

    @classmethod
    def tearDownClass(cls):
        try:
            admin = Database(schema=cls.schema, initialize=True)
            try:
                admin.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
            finally:
                admin.close()
        finally:
            cls.database.close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user = self.database.register("user_" + uuid.uuid4().hex[:12], "Password42!")
        self.manager = FileManager(self.database, self.user["id"], self.temporary.name)

    def run_command(self, action, *arguments):
        return self.manager.execute(action, list(arguments))

    def test_database_privileges(self):
        for query in ("CREATE TABLE forbidden(id integer)", "DELETE FROM operations", "UPDATE operations SET status='success'", "INSERT INTO operations(operation_type,status) VALUES ('forged','success')"):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.database.connection.execute(query)

    def test_quota_and_reconciliation(self):
        self.manager.quota_bytes = 5
        self.run_command("create", "a", "12345")
        with self.assertRaises(ValueError):
            self.run_command("cp", "a", "b")
        self.assertFalse((self.manager.root / "b").exists())
        self.run_command("write", "a", "1")
        self.manager.quota_entries = 1
        with self.assertRaises(ValueError):
            self.run_command("mkdir", "extra")
        (self.manager.root / "a").write_text("different")
        self.database.log(self.user["id"], "write", "started", {"operation_id": "interrupted"})
        report = self.manager.reconcile()
        self.assertEqual(report["changed"], ["a"])
        self.assertTrue(report["unfinished"])

    def test_unzip_quota_includes_implicit_directories(self):
        with zipfile.ZipFile(self.manager.root / "input.zip", "w") as archive:
            archive.writestr("a/b/c", "x")
        self.manager.quota_entries = 4
        with self.assertRaises(ValueError):
            self.run_command("unzip", "input.zip", "out")
        self.assertFalse((self.manager.root / "out").exists())

    def test_file_operations_and_deleted_history(self):
        self.run_command("mkdir", "docs")
        self.run_command("create", "docs/note.txt", "Привет")
        self.run_command("append", "docs/note.txt", ", мир")
        self.assertEqual(self.run_command("cat", "docs/note.txt"), "Привет, мир")
        self.run_command("cp", "docs/note.txt", "copy.txt")
        self.run_command("mv", "copy.txt", "moved.txt")
        self.run_command("write", "moved.txt", "Обновлено")
        self.assertEqual(self.run_command("info", "moved.txt")["size"], len("Обновлено".encode()))
        self.run_command("rm", "moved.txt")
        row = next(row for row in self.database.files(self.user["id"]) if row["location"] == "moved.txt")
        self.assertTrue(row["deleted"])
        event = next(row for row in self.database.history(self.user["id"], 100) if row["operation_type"] == "rm" and row["status"] == "success")
        self.assertEqual(event["file_id"], row["id"])
        self.run_command("rm", "docs/note.txt")
        self.run_command("rmdir", "docs")

    def test_navigation_and_no_overwrite(self):
        self.run_command("mkdir", "docs")
        self.run_command("cd", "docs")
        self.assertEqual(self.run_command("pwd"), "/docs")
        self.run_command("create", "a", "first")
        with self.assertRaises(FileExistsError):
            self.run_command("create", "a", "lost")
        self.run_command("create", "b", "second")
        with self.assertRaises(FileExistsError):
            self.run_command("cp", "a", "b")
        self.assertEqual(self.run_command("cat", "a"), "first")
        self.run_command("cd", "/")
        with self.assertRaises(OSError):
            self.run_command("rmdir", "docs")

    def test_paths_links_and_text_limit(self):
        for name in ("../outside", "/etc/passwd", "C:/Windows", "CON.txt", ".fm-secret", "bad\0name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.run_command("cat", name)
        self.run_command("create", "file", "A")
        if os.name != "nt":
            os.symlink(self.manager.root / "file", self.manager.root / "link")
            os.link(self.manager.root / "file", self.manager.root / "hard")
            for name in ("link", "hard"):
                with self.assertRaises(ValueError):
                    self.run_command("cat", name)
        with self.assertRaises(ValueError):
            self.run_command("create", "large", "x" * (TEXT_LIMIT + 1))

    def test_users_and_owner_trigger(self):
        self.run_command("create", "secret", "A")
        other = self.database.register("other_" + uuid.uuid4().hex[:12], "Password42!")
        second = FileManager(self.database, other["id"], self.temporary.name)
        with self.assertRaises(FileNotFoundError):
            second.execute("cat", ["secret"])
        second.execute("create", ["secret", "B"])
        self.assertEqual(self.run_command("cat", "secret"), "A")
        file_id = self.database.files(self.user["id"])[0]["id"]
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.database.log(other["id"], "spoof", "success", file_id=file_id)

    def test_authentication_and_lockout(self):
        name = self.user["username"]
        self.assertEqual(self.database.login(name, "Password42!")["id"], self.user["id"])
        with self.assertRaises(ValueError):
            self.database.register(name, "Password42!")
        for _ in range(5):
            with self.assertRaisesRegex(ValueError, "Неверный"):
                self.database.login(name, "wrong")
        with self.assertRaisesRegex(ValueError, "заблокирован"):
            self.database.login(name, "Password42!")
        self.database.connection.execute("UPDATE users SET locked_until=1 WHERE id=%s", (self.user["id"],))
        self.assertEqual(self.database.login(name, "Password42!")["id"], self.user["id"])

    def test_parallel_login_counter(self):
        connections = [Database(schema=self.database.schema) for _ in range(2)]
        for database in connections:
            self.addCleanup(database.close)
        def failed_login(database):
            with self.assertRaisesRegex(ValueError, "Неверный"):
                database.login(self.user["username"], "wrong")
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(failed_login, connections))
        row = self.database.connection.execute("SELECT failed_attempts FROM users WHERE id=%s", (self.user["id"],)).fetchone()
        self.assertEqual(row["failed_attempts"], 2)

    def test_json_xml_and_invalid_write(self):
        self.run_command("json-write", "a.json", '{"name":"Иван","age":20}')
        self.assertEqual(self.run_command("json-read", "a.json")["age"], 20)
        with self.assertRaises(ValueError):
            self.run_command("json-write", "a.json", "{bad}")
        self.assertEqual(self.run_command("json-read", "a.json")["age"], 20)
        self.run_command("xml-write", "a.xml", '<person name="Иван">Привет <b>мир</b>!</person>')
        tree = self.run_command("xml-read", "a.xml")
        self.assertEqual(tree["attributes"]["name"], "Иван")
        self.assertEqual(tree["children"][0]["tail"], "!")

    def test_zip_round_trip(self):
        self.run_command("mkdir", "docs")
        self.run_command("mkdir", "docs/empty")
        self.run_command("create", "docs/привет.txt", "x" * 100_000)
        self.run_command("zip", "docs", "backup.zip")
        self.run_command("unzip", "backup.zip", "restored")
        self.assertEqual(self.run_command("cat", "restored/docs/привет.txt"), "x" * 100_000)
        self.assertTrue((self.manager.root / "restored/docs/empty").is_dir())
        with self.assertRaises(ValueError):
            self.run_command("unzip", "backup.zip", "restored")

    def test_bad_zip_cleanup(self):
        for number, name in enumerate(("../escape", "/absolute", "CON.txt", "a\\b")):
            archive = self.manager.root / f"bad{number}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                entry = zipfile.ZipInfo()
                entry.filename = name  # Не даём Windows нормализовать вредоносное имя при записи фикстуры.
                entry.orig_filename = name
                output.writestr(entry, "attack")
            with self.assertRaises(ValueError):
                self.run_command("unzip", archive.name, "out")
            self.assertFalse((self.manager.root / "out").exists())
        self.assertFalse(any(item.name.startswith(".fm-") for item in self.manager.root.iterdir()))

    def test_zip_symlink_crc_and_size(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, "outside")
        (self.manager.root / "link.zip").write_bytes(buffer.getvalue())
        with self.assertRaises(ValueError):
            self.run_command("unzip", "link.zip", "out")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr("data", "ORIGINAL")
        (self.manager.root / "crc.zip").write_bytes(buffer.getvalue().replace(b"ORIGINAL", b"CORRUPTD"))
        with self.assertRaises(zipfile.BadZipFile):
            self.run_command("unzip", "crc.zip", "out")
        with zipfile.ZipFile(self.manager.root / "huge.zip", "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("large", b"x" * (21 * 1024 * 1024))
        with self.assertRaises(ValueError):
            self.run_command("unzip", "huge.zip", "out")

    def test_sql_parameters_and_rollback(self):
        filename = "Robert'); DROP TABLE users;--.txt"
        self.run_command("create", filename, "A")
        self.assertEqual(self.run_command("cat", filename), "A")
        with self.assertRaisesRegex(ValueError, "rollback"):
            with self.database.connection.transaction():
                file_id = self.database.save_file(self.user["id"], "rollback", 1, "file")
                self.database.log(self.user["id"], "rollback", "success", file_id=file_id)
                raise ValueError("rollback")
        self.assertFalse(any(row["location"] == "rollback" for row in self.database.files(self.user["id"])))
        self.assertFalse(any(row["operation_type"] == "rollback" for row in self.database.history(self.user["id"], 100)))

    def test_disks_and_cli_persistence(self):
        disks = self.run_command("disks")
        self.assertTrue(disks)
        self.assertTrue(all(0 <= row["free"] <= row["total"] for row in disks))
        name = "cli_" + uuid.uuid4().hex[:12]
        commands = ["register", name, "Password42!", "Password42!", "login", name, "Password42!",
                    'create "my file.txt"', "initial", ".end", 'write "my file.txt"', "нет",
                    'cat "my file.txt"', 'write "my file.txt"', "да", "updated", ".end",
                    "mkdir docs", "create docs/note.txt", "Привет", ".end",
                    "append docs/note.txt", "Дополнение", ".end", "cat docs/note.txt",
                    "json-write docs/person.json", '{"name":"Иван","age":20}', ".end", "json-read docs/person.json",
                    "xml-write docs/person.xml", '<person name="Иван"><age>20</age></person>', ".end", "xml-read docs/person.xml",
                    "cp docs/note.txt copy.txt", "mv copy.txt moved.txt", "info moved.txt",
                    "zip docs backup.zip", "unzip backup.zip restored", "cat restored/docs/note.txt",
                    "rm moved.txt", "да", "dbfiles", "history 8", "exit"]
        environment = dict(os.environ, PGSCHEMA=self.database.schema)
        entry = [sys.executable, str(PROJECT_DIR / "main.py"), "--data", self.temporary.name]
        first = subprocess.run(entry, input="\n".join(commands) + "\n", text=True, capture_output=True, env=environment, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("initial", first.stdout)
        self.assertNotIn("Password42!", first.stdout)
        self.assertNotIn("Ошибка:", first.stdout)
        self.assertIn('"age": 20', first.stdout)
        self.assertIn('"tag": "person"', first.stdout)
        if os.getenv("FM_DEMO_TRANSCRIPT"):
            Path(os.environ["FM_DEMO_TRANSCRIPT"]).write_text(first.stdout, encoding="utf-8")
        second = subprocess.run(entry, input=f'login\n{name}\nPassword42!\ncat "my file.txt"\nexit\n',
                                text=True, capture_output=True, env=environment, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("updated", second.stdout)
        self.assertFalse((Path(self.temporary.name) / "manager.lock").exists())
        lock = Path(self.temporary.name) / "manager.lock"
        lock.write_text("test-other-instance")
        blocked = subprocess.run(entry, input="exit\n", text=True, capture_output=True, env=environment, timeout=30)
        self.assertEqual(blocked.returncode, 1)
        self.assertTrue(lock.exists())
        lock.unlink()
        details = self.database.connection.execute("SELECT details FROM operations").fetchall()
        self.assertNotIn("Password42!", json.dumps(details))
        self.assertNotIn("updated", json.dumps(details))


if __name__ == "__main__":
    unittest.main()
