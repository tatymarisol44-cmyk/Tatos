"""Regressions from the architecture and scalability review of 2026-10-20."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse

from orchestrator.embeddings import LiteLLMEmbedder
from orchestrator.vectorstore import QdrantVectorStore, ensure_tenant_collection


async def test_embedding_response_indices_preserve_document_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    async def embed(**kwargs: Any) -> Any:
        return SimpleNamespace(
            data=[{"index": 1, "embedding": [20.0]}, {"index": 0, "embedding": [10.0]}]
        )

    monkeypatch.setattr(litellm, "aembedding", embed)
    assert await LiteLLMEmbedder("synthetic").embed(["first", "second"]) == [[10.0], [20.0]]


@pytest.mark.parametrize("indices", [[0, 0], [0, 2], [0], [True, 1]])
async def test_invalid_embedding_indices_fail_before_indexing(
    monkeypatch: pytest.MonkeyPatch, indices: list[int]
) -> None:
    import litellm

    async def embed(**kwargs: Any) -> Any:
        return SimpleNamespace(data=[{"index": index, "embedding": [1.0]} for index in indices])

    monkeypatch.setattr(litellm, "aembedding", embed)
    with pytest.raises(ValueError, match="embedding"):
        await LiteLLMEmbedder("synthetic").embed(["first", "second"])


class RacingCollections:
    def __init__(self) -> None:
        self.exists = False
        self.observers = 0
        self.both = asyncio.Event()
        self.indexes: set[str] = set()

    async def collection_exists(self, name: str) -> bool:
        if self.exists:
            return True
        self.observers += 1
        if self.observers == 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 2)
        return False

    async def create_collection(self, name: str, **kwargs: Any) -> None:
        if self.exists:
            raise UnexpectedResponse(409, "Conflict", b"already exists", httpx.Headers())
        self.exists = True

    async def get_collection(self, name: str) -> Any:
        return SimpleNamespace(points_count=0)

    async def create_payload_index(self, name: str, field: str, **kwargs: Any) -> None:
        self.indexes.add(field)


async def test_two_replicas_can_initialize_the_same_agent_collection() -> None:
    client = RacingCollections()
    stores = [QdrantVectorStore(":memory:") for _ in range(2)]
    for store in stores:
        await store._client.close()
        store._client = client  # type: ignore[assignment]
    assert await asyncio.gather(*(store.ensure("shared", 8) for store in stores)) == [False, False]


async def test_existing_tenant_collection_repairs_missing_indexes() -> None:
    client = RacingCollections()
    client.exists = True
    await ensure_tenant_collection(client, "shared", 8, ("subject_id",), ("expires_at",))  # type: ignore[arg-type]
    assert client.indexes == {"tenant", "subject_id", "expires_at"}


async def test_two_replicas_can_initialize_the_same_tenant_collection() -> None:
    client = RacingCollections()
    await asyncio.gather(*(ensure_tenant_collection(client, "shared", 8) for _ in range(2)))  # type: ignore[arg-type]
    assert "tenant" in client.indexes


@pytest.mark.parametrize("status", [403, 500])
async def test_collection_initialization_does_not_hide_service_errors(status: int) -> None:
    from orchestrator.vectorstore import ensure_collection

    class FailingCollections:
        async def collection_exists(self, name: str) -> bool:
            return False

        async def create_collection(self, name: str, **kwargs: Any) -> None:
            raise UnexpectedResponse(status, "Synthetic failure", b"failure", httpx.Headers())

    with pytest.raises(UnexpectedResponse) as error:
        await ensure_collection(FailingCollections(), "shared", 8)  # type: ignore[arg-type]
    assert error.value.status_code == status


async def test_a_conflict_without_an_existing_collection_is_not_success() -> None:
    from orchestrator.vectorstore import ensure_collection

    class MissingCollections:
        async def collection_exists(self, name: str) -> bool:
            return False

        async def create_collection(self, name: str, **kwargs: Any) -> None:
            raise UnexpectedResponse(409, "Conflict", b"failure", httpx.Headers())

    with pytest.raises(UnexpectedResponse):
        await ensure_collection(MissingCollections(), "shared", 8)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "vectors", [[[]], [[float("nan")]], [[float("inf")]], [[True]], [["1"]], [[1.0], [1.0, 2.0]]]
)
def test_invalid_embedding_vectors_are_rejected(vectors: list[list[Any]]) -> None:
    from orchestrator.embeddings import ordered_embeddings

    with pytest.raises(ValueError, match="embedding"):
        ordered_embeddings([{"embedding": vector} for vector in vectors], len(vectors))


async def test_invalid_embedding_output_still_records_provider_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    from orchestrator.usage import metered, summarize

    async def embed(**kwargs: Any) -> Any:
        return SimpleNamespace(data=[], usage=SimpleNamespace(prompt_tokens=10))

    monkeypatch.setattr(litellm, "aembedding", embed)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kwargs: 0.02)
    with metered() as entries, pytest.raises(ValueError, match="embedding"):
        await LiteLLMEmbedder("synthetic").embed(["first"])
    assert summarize(entries)["cost_usd"] == 0.02


def test_duplicate_catalog_ids_fail_explicitly(tmp_path: Any) -> None:
    from orchestrator.catalog import load_catalog

    for division in ("clinical", "marketing"):
        folder = tmp_path / division
        folder.mkdir()
        (folder / "same-id.md").write_text(
            "---\nname: Synthetic agent\ndescription: Synthetic description\n---\nPrompt",
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        load_catalog(tmp_path)
