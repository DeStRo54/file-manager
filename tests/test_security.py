"""Проверки квот и файловой защиты без PostgreSQL."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from database import Database
from file_manager import FileManager


class SecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Mock()
        self.database.files.return_value = []
        self.database.unfinished.return_value = []
        self.manager = FileManager(self.database, 1, temporary.name)

    def test_quota_counts_actual_files_and_replacement_delta(self):
        self.manager.quota_bytes = 5
        self.manager.create("a", "12345")
        with self.assertRaisesRegex(ValueError, "квота"):
            self.manager.cp("a", "b")
        self.assertFalse((self.manager.root / "b").exists())
        self.manager.write("a", "12")
        self.manager.create("b", "345")
        with self.assertRaises(ValueError):
            self.manager.append("a", "x")
        self.assertEqual(self.manager.cat("a"), "12")

    def test_quota_counts_directories(self):
        self.manager.quota_entries = 1
        self.manager.mkdir("a")
        with self.assertRaises(ValueError):
            self.manager.mkdir("b")

    def test_unzip_checks_implicit_directories_before_publication(self):
        with zipfile.ZipFile(self.manager.root / "input.zip", "w") as archive:
            archive.writestr("a/b/c", "x")
        self.manager.quota_entries = 4
        with self.assertRaises(ValueError):
            self.manager.unzip("input.zip", "out")
        self.assertFalse((self.manager.root / "out").exists())
        self.assertFalse(list(self.manager.root.glob(".fm-*")))

    def test_low_disk_space_preserves_file(self):
        self.manager.create("a", "original")
        with patch("file_manager.shutil.disk_usage", return_value=Mock(free=0)):
            with self.assertRaises(ValueError):
                self.manager.write("a", "changed")
        self.assertEqual(self.manager.cat("a"), "original")

    def test_reconcile_does_not_modify_data(self):
        (self.manager.root / "untracked").write_text("hello")
        self.database.files.return_value = [
            {"location": "missing", "kind": "file", "size": 3, "deleted": False}]
        report = self.manager.reconcile()
        self.assertEqual(report["untracked"], ["untracked"])
        self.assertEqual(report["missing"], ["missing"])
        self.database.save_file.assert_not_called()

    def test_password_is_required_before_connection(self):
        with patch.dict(os.environ, {}, clear=True), patch("database.load_env"), patch("database.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "PGPASSWORD"):
                Database()
            connect.assert_not_called()

    def test_administrative_role_is_rejected(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "rolsuper": True, "rolcreatedb": False, "rolcreaterole": False}
        with patch.dict(os.environ, {"PGPASSWORD": "test-only"}), patch("database.load_env"), patch("database.psycopg.connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "SUPERUSER"):
                Database()
        connection.close.assert_called_once()

    def test_audit_failure_does_not_hide_original_error(self):
        self.database.connection = MagicMock()
        self.database.log.side_effect = [None, RuntimeError("audit unavailable")]
        with self.assertRaises(FileNotFoundError):
            self.manager.execute("create", ["missing/child", "hello"])
        started, failed = self.database.log.call_args_list
        self.assertEqual(started.args[3]["operation_id"], failed.args[3]["operation_id"])


if __name__ == "__main__":
    unittest.main()
