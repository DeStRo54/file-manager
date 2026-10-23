"""Практика 1: основной функционал с настоящей PostgreSQL и временными файлами."""

import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from psycopg import sql

from app.files import FileManager
from app.files.file_safety import UNPACK_LIMIT
from db.database import Database, check_password
from db.init_db import provision


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Новая схема для тестов; рабочая public не используется.
        cls.schema = "fm_test_" + uuid.uuid4().hex
        provision(cls.schema)
        cls.database = Database(schema=cls.schema)

    @classmethod
    def tearDownClass(cls):
        cls.database.close()
        admin = Database(schema=cls.schema, initialize=True)
        try:
            admin.connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )
        finally:
            admin.close()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.user = self.database.register("user_" + uuid.uuid4().hex[:12], "Password42!")
        self.manager = FileManager(self.database, self.user["id"], self.folder.name)

    def command(self, name, *arguments):
        return self.manager.execute(name, list(arguments))

    def test_registration_and_login(self):
        user = self.database.login(self.user["username"], "Password42!")
        self.assertEqual(user["id"], self.user["id"])

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(ValueError):
            self.database.login(self.user["username"], "wrong")

    def test_password_is_hashed(self):
        row = self.database.connection.execute(
            "SELECT password_hash FROM users WHERE id=%s", (self.user["id"],)
        ).fetchone()
        self.assertNotEqual(row["password_hash"], "Password42!")
        self.assertTrue(check_password("Password42!", row["password_hash"]))

    def test_directories_and_navigation(self):
        self.command("mkdir", "docs")
        self.assertIn("docs", [item["name"] for item in self.command("ls")])
        self.command("cd", "docs")
        self.assertEqual(self.command("pwd"), (Path(self.folder.name) / "docs").as_posix())
        self.command("cd", "..")
        self.command("rmdir", "docs")
        self.assertFalse((Path(self.folder.name) / "docs").exists())

    def test_create_and_read_file(self):
        self.command("create", "note.txt", "Привет")
        self.assertEqual(self.command("cat", "note.txt"), "Привет")

    def test_write_and_append_file(self):
        self.command("create", "note.txt", "old")
        self.command("write", "note.txt", "new")
        self.command("append", "note.txt", " text")
        self.assertEqual(self.command("cat", "note.txt"), "new text")

    def test_copy_and_move_file(self):
        self.command("create", "note.txt", "hello")
        self.command("cp", "note.txt", "copy.txt")
        self.assertEqual(self.command("cat", "copy.txt"), "hello")
        self.command("mv", "copy.txt", "moved.txt")
        self.assertFalse((Path(self.folder.name) / "copy.txt").exists())
        self.assertEqual(self.command("cat", "moved.txt"), "hello")

    def test_delete_file(self):
        self.command("create", "note.txt", "hello")
        self.command("rm", "note.txt")
        self.assertFalse((Path(self.folder.name) / "note.txt").exists())
        self.assertTrue(self.command("dbfiles")[0]["deleted"])

    def test_file_metadata(self):
        self.command("create", "note.txt", "hello")
        row = self.command("dbfiles")[0]
        self.assertEqual(row["filename"], "note.txt")
        self.assertEqual(row["size"], 5)
        self.assertTrue(Path(row["location"]).is_absolute())
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(self.command("info", "note.txt")["size"], 5)

    def test_operations_are_logged(self):
        self.command("create", "note.txt", "first")
        self.command("write", "note.txt", "second")
        self.command("rm", "note.txt")
        events = self.command("history")
        successful = [event["operation_type"] for event in events if event["status"] == "success"]
        self.assertIn("create", successful)
        self.assertIn("write", successful)
        self.assertIn("rm", successful)
        file_id = self.command("dbfiles")[0]["id"]
        removal = next(event for event in events if event["operation_type"] == "rm" and event["status"] == "success")
        self.assertEqual(removal["file_id"], file_id)

    def test_json_file(self):
        self.command("json-write", "person.json", '{"name":"Иван","age":20}')
        self.assertEqual(self.command("json-read", "person.json"), {"name": "Иван", "age": 20})

    def test_xml_file(self):
        self.command("xml-write", "person.xml", '<person name="Иван">hello</person>')
        result = self.command("xml-read", "person.xml")
        self.assertEqual(result["tag"], "person")
        self.assertEqual(result["attributes"]["name"], "Иван")
        self.assertEqual(result["text"], "hello")

    def test_zip_creation_and_extraction(self):
        self.command("mkdir", "docs")
        self.command("create", "docs/note.txt", "hello")
        self.command("zip", "docs", "backup.zip")
        self.command("unzip", "backup.zip", "restored")
        self.assertEqual(self.command("cat", "restored/docs/note.txt"), "hello")

    def test_zip_cannot_write_outside_destination(self):
        with zipfile.ZipFile(Path(self.folder.name) / "bad.zip", "w") as archive:
            archive.writestr("../outside.txt", "hello")
        with self.assertRaises(ValueError):
            self.command("unzip", "bad.zip", "restored")
        self.assertFalse((Path(self.folder.name) / "restored").exists())

    def test_zip_extraction_size_limit(self):
        with zipfile.ZipFile(Path(self.folder.name) / "large.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # Три допустимых файла вместе превышают общий лимит распаковки.
            for number in range(3):
                archive.writestr(f"file{number}.txt", b"x" * (UNPACK_LIMIT // 3 + 1))
        with self.assertRaises(ValueError):
            self.command("unzip", "large.zip", "restored")
        self.assertFalse((Path(self.folder.name) / "restored").exists())

    def test_text_larger_than_one_mebibyte(self):
        text = "x" * (2 * 1024 * 1024)
        self.command("create", "text.txt", text)
        self.assertEqual(self.command("cat", "text.txt"), text)

    def test_large_zip_and_file_without_old_limits(self):
        size = 33 * 1024 * 1024
        with zipfile.ZipFile(Path(self.folder.name) / "large.zip", "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("large.bin", b"x" * size)
        self.command("unzip", "large.zip", "restored")
        self.command("cp", "restored/large.bin", "copy.bin")
        self.assertEqual((Path(self.folder.name) / "copy.bin").stat().st_size, size)

    def test_disk_information(self):
        disks = self.command("disks")
        self.assertTrue(disks)
        for disk in disks:
            self.assertGreater(disk["total"], 0)
            self.assertGreaterEqual(disk["free"], 0)
            self.assertLessEqual(disk["free"], disk["total"])

    def test_sql_parameters_preserve_filename(self):
        name = "Robert'); DROP TABLE users;--.txt"
        self.command("create", name, "hello")
        self.assertEqual(self.command("dbfiles")[0]["filename"], name)
        self.assertEqual(self.command("cat", name), "hello")

    def test_required_procedures_exist(self):
        procedures = self.database.check()["procedures"]
        names = [row["routine_name"] for row in procedures]
        self.assertIn("record_operation", names)
        self.assertIn("soft_delete_file", names)


if __name__ == "__main__":
    unittest.main()
