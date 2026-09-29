from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.catalog import Catalog, load_catalog
from tests.conftest import FIXTURES


def test_loads_agents_with_frontmatter_only(catalog: Catalog) -> None:
    assert set(catalog.agents) == {
        "engineering-frontend-developer",
        "engineering-devops-automator",
        "marketing-seo-specialist",
        "security-penetration-tester",
    }
    assert catalog.divisions == ["engineering", "marketing", "security"]


def test_parses_metadata_and_body(catalog: Catalog) -> None:
    agent = catalog.agents["engineering-frontend-developer"]
    assert agent.name == "Frontend Developer"
    assert agent.division == "engineering"
    assert agent.vibe == "Pixel-perfect and fast."
    assert agent.system_prompt.startswith("# Frontend Developer")
    assert "React user interfaces" in agent.index_text()


def test_tolerates_invalid_yaml_with_unquoted_colons(catalog: Catalog) -> None:
    agent = catalog.agents["marketing-seo-specialist"]
    assert agent.description.startswith("Search engine optimization expert: keyword")


def test_version_is_stable() -> None:
    assert load_catalog(FIXTURES).version == load_catalog(FIXTURES).version


def test_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "nope")


def test_empty_catalog_raises(tmp_path: Path) -> None:
    (tmp_path / "engineering").mkdir()
    with pytest.raises(ValueError):
        load_catalog(tmp_path)
