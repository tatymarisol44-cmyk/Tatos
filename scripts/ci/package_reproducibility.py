"""Check two wheels and the installed package, without importing source via editable mode."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile

import orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    first = next(args.first.glob("*.whl"))
    second = next(args.second.glob("*.whl"))
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    if first.read_bytes() != second.read_bytes():
        raise SystemExit("Two builds of the same source produced different wheels")
    installed = Path(orchestrator.__file__).resolve()
    if installed.is_relative_to(root / "src"):
        raise SystemExit("Package verification requires a non-editable installation")
    suffixes = {".py", ".html", ".js", ".css", ".yaml", ".mako", ".png", ".ini"}
    sources = [
        p for p in (root / "src/orchestrator").rglob("*") if p.is_file() and p.suffix in suffixes
    ]
    with ZipFile(first) as wheel:
        for source in sources:
            name = source.relative_to(root / "src").as_posix()
            content = wheel.read(name)
            if content != source.read_bytes():
                raise SystemExit(f"Missing or changed packaged resource: {name}")
            installed_resource = installed.parent.parent / name
            if not installed_resource.is_file() or installed_resource.read_bytes() != content:
                raise SystemExit(f"Installed package differs from the candidate wheel: {name}")
    print(f"Identical wheels: sha256:{digest}; {len(sources)} source resources verified")
    print(f"Non-editable import: {installed}")


if __name__ == "__main__":
    main()
