"""Сохраняет CycloneDX по тому же результату pip-audit, что и отчёт о проверке."""

import json
import sys
from pathlib import Path

from cyclonedx import output
from cyclonedx.model.bom import Bom
from cyclonedx.model.component import Component
from cyclonedx.model.vulnerability import BomTarget, Vulnerability


def export(source, destination):
    scan = json.loads(source.read_text(encoding="utf-8"))
    bom = Bom()
    findings = {}
    for package in scan["dependencies"]:
        if "skip_reason" in package:
            continue  # Неполное покрытие блокируется в dependency_report.py.
        name, version = package["name"], package["version"]
        reference = f"pkg:pypi/{name}@{version}"
        bom.components.add(Component(name=name, version=version, bom_ref=reference))
        for finding in package["vulns"]:
            advisory = finding["id"]
            if advisory not in findings:
                findings[advisory] = Vulnerability(
                    id=advisory, description=finding.get("description")
                )
            findings[advisory].affects.add(BomTarget(ref=reference))
    bom.vulnerabilities.update(findings.values())
    writer = output.make_outputter(
        bom=bom, output_format=output.OutputFormat.JSON,
        schema_version=output.SchemaVersion.V1_4,
    )
    destination.write_text(writer.output_as_string(), encoding="utf-8")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
