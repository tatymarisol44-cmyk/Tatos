"""Dependency rules (audit 2026-10-08, item 7: SOLID / clean architecture), checked on the
import graph so they cannot erode silently.

- Only the HTTP adapter (`orchestrator.api`) knows the web framework.
- The application services extracted from the orchestrator talk to conversations
  through ports: they import no graph engine, vector database or model SDK.
- Infrastructure adapters are chosen in one place (`factories`), not by the services."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
WEB = {"fastapi", "starlette", "uvicorn"}
ENGINES = {"langgraph", "langchain_core", "qdrant_client", "litellm"}


def imports(module: Path) -> set[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def modules() -> list[Path]:
    return sorted(p for p in SRC.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.stem)
def test_only_the_http_adapter_knows_the_web_framework(path: Path) -> None:
    # mcp_server serves MCP over stdio/HTTP itself; cli starts the server.
    if path.stem in {"cli", "mcp_server"}:
        return
    assert not imports(path) & WEB, f"{path.name} imports {imports(path) & WEB}"


@pytest.mark.parametrize("name", ["portal", "subject_rights", "answers", "classification"])
def test_application_services_use_ports_not_engines(name: str) -> None:
    used = imports(SRC / f"{name}.py")
    assert not used & ENGINES, f"{name} imports {used & ENGINES}"
    assert "orchestrator.service" not in (SRC / f"{name}.py").read_text(encoding="utf-8")


def test_vector_and_embedding_adapters_are_chosen_in_one_place() -> None:
    builders = ("QdrantChunkStore(", "QdrantMemoryStore(", "QdrantVectorStore(", "LiteLLMEmbedder(")
    for path in modules():
        if path.stem in {"factories", "knowledge", "memory", "vectorstore", "embeddings"}:
            continue  # the factory, and the adapters' own modules
        text = path.read_text(encoding="utf-8")
        assert not any(b in text for b in builders), f"{path.name} builds an adapter itself"
