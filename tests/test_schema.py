"""One list of tables (orchestrator.schema): the running API, the maintenance jobs and the
migrations see the same tables, and infrastructure knows no feature.

Before it, the migrations' own list missed 8 of 34 tables (clinical files, test results,
privacy cases...): Alembic's autogenerate would have proposed dropping them."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "orchestrator"
TABLE = re.compile(r"^\w+ = Table\(", re.MULTILINE)


def _imported_by(module: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "orchestrator":
            names |= {a.name for a in node.names}
    return names


def test_every_module_that_defines_a_table_is_in_the_schema() -> None:
    defining = {
        p.stem
        for p in PKG.glob("*.py")
        if TABLE.search(p.read_text(encoding="utf-8")) and p.stem != "schema"
    }
    assert defining  # the scan works
    assert defining - _imported_by(PKG / "schema.py") == set()


def test_migrations_and_the_running_api_see_the_same_tables() -> None:
    # Fresh interpreters: no earlier import may register a table by accident.
    def tables(code: str) -> list[str]:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
        )
        return sorted(out.stdout.split())

    from_schema = tables("import orchestrator.schema as s; print(' '.join(s.metadata.tables))")
    from_service = tables(
        "import orchestrator.service; from orchestrator.db import metadata;"
        " print(' '.join(metadata.tables))"
    )
    env = (PKG / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "from orchestrator.schema import metadata" in env
    assert from_schema == from_service and len(from_schema) >= 34


def test_infrastructure_knows_no_feature() -> None:
    # db.py depends on configuration only: no import cycle closes through it.
    tree = ast.parse((PKG / "db.py").read_text(encoding="utf-8"))
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("orchestrator")
    }
    assert internal <= {"orchestrator.config"}
