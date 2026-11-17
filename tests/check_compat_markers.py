"""Lists the backwards-compatibility code and fails when some of it is due.

Code kept only for older Home Assistant releases is marked with
``HA-COMPAT(<version>)``: the release from which it is no longer needed.
Once the minimum in hacs.json reaches that release, the marked code has to
be cleaned up. Run from the repository root: python3 tests/check_compat_markers.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"HA-COMPAT\((\d{4})\.(\d{1,2})\)")
SEARCHED = ["custom_components", "tests", "README.md", "DEVELOPER.md"]
SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml"}


def version(text: str) -> tuple[int, int]:
    major, minor = text.split(".")[:2]
    return int(major), int(minor)


def files():
    for name in SEARCHED:
        path = ROOT / name
        if path.is_file():
            yield path
        else:
            yield from (
                p for p in path.rglob("*")
                if p.suffix in SUFFIXES and "__pycache__" not in p.parts
            )


def main() -> int:
    minimum = version(json.loads((ROOT / "hacs.json").read_text())["homeassistant"])
    due = 0
    for path in sorted(files()):
        if path == Path(__file__).resolve():
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = MARKER.search(line)
            if not match:
                continue
            marked = (int(match[1]), int(match[2]))
            state = "DUE" if marked <= minimum else "ok "
            due += marked <= minimum
            print(f"{state} {path.relative_to(ROOT)}:{number}: {line.strip()}")
    print(f"Minimum Home Assistant version: {minimum[0]}.{minimum[1]}, {due} marker(s) due")
    return 1 if due else 0


if __name__ == "__main__":
    sys.exit(main())
