"""Temporary laboratory, blocked network and per-case JSON results."""

import json
import socket
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(scope="session")
def lab_root(tmp_path_factory):
    return tmp_path_factory.mktemp("offline-lab")


@pytest.fixture(autouse=True)
def isolated_lab(lab_root, monkeypatch):
    monkeypatch.chdir(lab_root)
    for name in ("HOME", "USERPROFILE", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(lab_root))
    monkeypatch.setattr(tempfile, "tempdir", str(lab_root))
    def no_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in the offline security suite")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)


def pytest_addoption(parser):
    parser.addoption("--security-results", default=None)


def pytest_configure(config):
    config.security_rows = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    row = item.config.security_rows.setdefault(item.nodeid, {
        "id": item.nodeid, "status": "passed", "duration_seconds": 0, "properties": {}, "details": [],
    })
    row["duration_seconds"] += report.duration
    row["properties"].update(dict(report.user_properties))
    if report.failed:
        row["status"] = "failed"
        row["details"].append(str(report.longrepr))
    elif report.skipped:
        row["status"] = "skipped"
        row["details"].append(str(report.longrepr))


def pytest_sessionfinish(session, exitstatus):
    destination = session.config.getoption("--security-results")
    if destination:
        Path(destination).write_text(json.dumps({
            "pytest_exit_code": int(exitstatus), "results": list(session.config.security_rows.values()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
