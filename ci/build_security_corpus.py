"""Snapshot all unique inputs in the reviewed upstream Intruder lists."""

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "security/PayloadsAllTheThings"


def main():
    commit = subprocess.check_output(["git", "-C", str(REFERENCE), "rev-parse", "HEAD"], text=True).strip()
    base = f"https://github.com/swisskyrepo/PayloadsAllTheThings/blob/{commit}/"
    cases, sources = {}, []
    count = 0
    for category, folder in (("path_traversal", "Directory Traversal/Intruder"), ("xxe", "XXE Injection/Intruders")):
        for path in sorted((REFERENCE / folder).glob("*.txt")):
            relative = path.relative_to(REFERENCE).as_posix()
            sources.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                count += 1
                key = (category, line)
                if key not in cases:
                    digest = hashlib.sha256(line.encode()).hexdigest()[:16]
                    cases[key] = {"id": f"{category}-{digest}", "category": category, "payload": line, "sources": []}
                cases[key]["sources"].append(base + quote(relative) + f"#L{number}")
    selected = {
        "Directory Traversal": "Lexical path confinement and ZIP entry validation",
        "XXE Injection": "Entity/DTD rejection before parser construction; no expansion or network",
        "Zip Slip": "ZIP entry confinement and bounded real archive fixtures",
        "Encoding Transformations": "Literal encoded paths and Unicode filenames",
        "Upload Insecure Files": "Filename and extension handling only; no upload endpoint",
        "Command Injection": "Literal filenames only; application has no shell execution sink",
        "Denial of Service": "Bounded file/parser/archive limit tests only",
    }
    inventory = []
    for directory in sorted(REFERENCE.iterdir()):
        if not directory.is_dir() or directory.name.startswith((".", "_")):
            continue
        inventory.append({"category": directory.name, "selected": directory.name in selected,
                          "reason": selected.get(directory.name, "Outside the verified filesystem regression suite"),
                          "source": base + quote(directory.name) + "/README.md"})
    excluded = [
        {"cases": "TOCTOU, symlink and POSIX permission fixtures requiring Linux validation",
         "reason": "Unfinished scenarios removed at the user's request"},
        {"cases": "System file reads, remote resource resolution, executable payloads and exhaustion",
         "reason": "Only lexical/barrier checks or bounded local markers are used"},
        {"cases": "SQLi, XSS, PHP/JVM execution chains and web upload payloads",
         "reason": "Outside the requested file manager regression scope"},
    ]
    output = ROOT / "tests/security"
    output.mkdir(parents=True, exist_ok=True)
    data = {"commit": commit, "source_lines": count, "unique_payloads": len(cases), "sources": sources, "cases": list(cases.values())}
    (output / "corpus.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "inventory.json").write_text(json.dumps({"categories": inventory, "excluded": excluded}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "LICENSE-PayloadsAllTheThings.txt").write_bytes((REFERENCE / "LICENSE").read_bytes())
    print(f"Imported {count} source lines, {len(cases)} unique payloads")


if __name__ == "__main__":
    main()
