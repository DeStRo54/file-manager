"""Run the verified offline security corpus and publish JSON/Markdown results."""

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="ci/security-regression.json")
    parser.add_argument("--output", default="reports/security-regression.json")
    args = parser.parse_args()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(), "results": []}
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if config["target"] != "local-functions" or config["target"] not in config["allowed_targets"]:
            raise ValueError("Only the explicitly allowed local-functions target is supported")
        if config["categories"] != ["all"]:
            raise ValueError("The verified snapshot runs with categories=['all']")
        report["configuration"] = config
        corpus = json.loads((ROOT / "tests/security/corpus.json").read_text(encoding="utf-8"))
        report["provenance"] = {k: v for k, v in corpus.items() if k != "cases"}
        report["inventory"] = json.loads((ROOT / "tests/security/inventory.json").read_text(encoding="utf-8"))
        harness = ROOT / ".test-harness"
        harness.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="security-", dir=harness) as temporary:
            temporary = Path(temporary)
            raw = temporary / "results.json"
            env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, "-B", "-m", "pytest", str(ROOT / "tests/security"), "-q", "--tb=short",
                       "--basetemp", str(temporary / "lab"), "--security-results", str(raw)]
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            print(completed.stdout[-6000:])
            data = json.loads(raw.read_text(encoding="utf-8"))
            report["results"] = data["results"]
            report["pytest_exit_code"] = completed.returncode
            report["status"] = "passed" if completed.returncode == 0 and report["results"] and all(
                row["status"] == "passed" for row in report["results"]) else "failed"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report.update(status="failed", error=str(exc))
    report["counts"] = {status: sum(row["status"] == status for row in report["results"])
                        for status in ("passed", "failed", "skipped")}
    report["integrated_checks"] = len(report["results"])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Security regression", "", f"Status: {report['status']}",
             f"Platform: {report['platform']}", f"Counts: {report['counts']}", ""]
    if "provenance" in report:
        lines += [f"Source commit: {report['provenance']['commit']}",
                  f"Unique payloads: {report['provenance']['unique_payloads']}", ""]
    if "error" in report:
        lines.append(report["error"])
    for row in report["results"]:
        if row["status"] != "passed":
            lines.extend([f"## {row['id']}", f"Status: {row['status']}", "```text", *row["details"], "```", ""])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Security regression: {report['status']} ({output})")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
