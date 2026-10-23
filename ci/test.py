"""Запускает тесты и сохраняет результат в JUnit XML для отчёта CI."""
import sys
import unittest
import xmlrunner
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

suite = unittest.defaultTestLoader.discover("tests")
result = xmlrunner.XMLTestRunner(output="reports/junit", verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
