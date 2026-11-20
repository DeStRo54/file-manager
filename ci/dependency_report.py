"""Publish security scan reports and apply temporary dependency exceptions."""

import html
import json
import os
import sys
from datetime import date, timezone, datetime
from pathlib import Path


def cell(value):
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")


def render(scan, exceptions, audit_status):
    today = datetime.now(timezone.utc).date()
    approved = {}
    for entry in exceptions:
        reason = entry["reason"].strip()
        expires = date.fromisoformat(entry["expires"])
        if not reason:
            raise ValueError("An exception requires a reason")
        if expires >= today:
            key = (entry["package"], entry["version"], entry["id"])
            approved[key] = f"{reason} (through {expires} UTC)"

    lines = [
        "## Python dependency audit",
        "",
        "| Package | Version | Advisory | Severity | Description | Suggested fix | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    findings = open_findings = skipped = 0
    for package in scan["dependencies"]:
        name = package["name"]
        if "skip_reason" in package:
            skipped += 1
            lines.append(f"| {cell(name)} | — | — | — | {cell(package['skip_reason'])} | Review scan coverage | Skipped |")
            continue
        version = package["version"]
        for finding in package["vulns"]:
            findings += 1
            identifiers = [finding["id"], *(finding.get("aliases") or [])]
            reason = next(
                (approved[(name, version, identifier)] for identifier in identifiers
                 if (name, version, identifier) in approved),
                None,
            )
            open_findings += reason is None
            versions = finding.get("fix_versions") or []
            fix = "Upgrade to a fixed release: " + ", ".join(versions) if versions else "Review advisory for mitigation"
            row = (
                name, version, ", ".join(dict.fromkeys(identifiers)),
                finding.get("severity") or "Unavailable in scanner data",
                finding.get("description") or "See advisory details", fix,
                f"Exception: {reason}" if reason else "Open",
            )
            lines.append("| " + " | ".join(map(cell, row)) + " |")

    failed = audit_status != int(findings > 0)
    lines.extend(["", f"Findings: {findings}. Open: {open_findings}. Skipped packages: {skipped}.",
                  "Findings are informational and do not block deployment."])
    if failed:
        lines.append("Scanner returned an unexpected exit status. Check the job log.")
    return "\n".join(lines) + "\n", int(bool(skipped or failed))


def publish(path, report):
    path.write_text(report, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(report)


def semgrep_report(path):
    lines = ["## Semgrep static analysis", ""]
    try:
        scan = json.loads(path.read_text(encoding="utf-8"))
        results, errors = scan["results"], scan["errors"]
        lines.extend([
            f"Findings: {len(results)}. Scanner errors: {len(errors)}.", "",
            "| Rule | Location | Semgrep severity | Rule impact | Suggested fix |",
            "|---|---|---|---|---|",
        ])
        error_findings = 0
        high_impact = 0
        for finding in results:
            extra = finding["extra"]
            error_findings += extra["severity"] == "ERROR"
            impact = str(extra.get("metadata", {}).get("impact", "")).upper()
            high_impact += impact in ("HIGH", "CRITICAL")
            row = (
                finding["check_id"], f"{finding['path']}:{finding['start']['line']}",
                extra["severity"], extra.get("metadata", {}).get("impact", "Unspecified"),
                extra["message"],
            )
            lines.append("| " + " | ".join(map(cell, row)) + " |")
        for error in errors:
            lines.append(f"\n- Scanner error: {cell(error.get('message', error))}")
        lines.append(f"\nHigh-impact findings: {high_impact}. ERROR-level findings: {error_findings}.")
        lines.append("Findings are informational and do not block deployment.")
        status = int(bool(errors))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        lines.append(f"Report unavailable: {cell(exc)}")
        status = 1
    publish(path.with_suffix(".md"), "\n".join(lines) + "\n")
    return status


def main():
    if sys.argv[1:2] == ["--semgrep"]:
        return semgrep_report(Path(sys.argv[2]))
    scan_path, policy_path, audit_status = sys.argv[1:]
    scan_path = Path(scan_path)
    try:
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        exceptions = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        report, status = render(scan, exceptions, int(audit_status))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report = f"## Python dependency audit\n\nAudit could not be completed. Check the job log.\n"
        print(f"Dependency audit: {exc}", file=sys.stderr)
        status = 1
    publish(scan_path.with_name("dependency-audit.md"), report)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
