"""Vector stores for the agent index: in-process for dev/tests, Qdrant for production."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from orchestrator.retrieval import Hit as Hit
from orchestrator.retrieval import VectorStore as VectorStore

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient


def qdrant_client(url: str, api_key: str | None = None) -> AsyncQdrantClient:
    """The async Qdrant client every store uses. ":memory:" runs Qdrant's embedded local
    mode (the tests)."""
    from qdrant_client import AsyncQdrantClient

    if url == ":memory:":
        return AsyncQdrantClient(location=":memory:")
    return AsyncQdrantClient(url=url, api_key=api_key)


async def ensure_collection(client: AsyncQdrantClient, name: str, dim: int) -> bool:
    """Create once across replicas; only an observed create conflict is recoverable."""
    from qdrant_client.http.exceptions import UnexpectedResponse
    from qdrant_client.models import Distance, VectorParams

    if await client.collection_exists(name):
        return False
    try:
        await client.create_collection(
            name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
        )
    except UnexpectedResponse as exc:
        if exc.status_code != 409 or not await client.collection_exists(name):
            raise
        return False
    return True


async def ensure_tenant_collection(
    client: AsyncQdrantClient,
    name: str,
    dim: int,
    keyword_fields: tuple[str, ...] = (),
    float_fields: tuple[str, ...] = (),
) -> None:
    """One collection for all tenants, partitioned by a `tenant` payload index
    (`is_tenant=True` lets Qdrant co-locate each tenant's points): it scales to many
    tenants far better than one collection per tenant. Indexes `keyword_fields` and
    `float_fields` for filtering. Repairs missing indexes on an existing collection."""
    from qdrant_client.models import (
        KeywordIndexParams,
        KeywordIndexType,
        PayloadSchemaType,
    )

    await ensure_collection(client, name, dim)
    # A prior process may have stopped between collection creation and indexing.
    # These operations are idempotent, including when another replica does them too.
    await client.create_payload_index(
        name,
        "tenant",
        field_schema=KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True),
        wait=True,
    )
    for field in keyword_fields:
        await client.create_payload_index(
            name, field, field_schema=PayloadSchemaType.KEYWORD, wait=True
        )
    for field in float_fields:
        await client.create_payload_index(
            name, field, field_schema=PayloadSchemaType.FLOAT, wait=True
        )


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, list[float]]] = {}
        self._active = ""

    async def ensure(self, name: str, dim: int) -> bool:
        self._active = name
        existing = name in self._collections
        self._collections.setdefault(name, {})
        return existing and bool(self._collections[name])

    async def upsert(self, ids: list[str], vectors: list[list[float]]) -> None:
        self._collections[self._active].update(zip(ids, vectors, strict=True))

    async def search(self, vector: list[float], k: int) -> list[Hit]:
        rows = self._collections.get(self._active, {})
        scored = [
            Hit(agent_id, sum(a * b for a, b in zip(vector, vec, strict=True)))
            for agent_id, vec in rows.items()
        ]
        scored.sort(key=lambda h: (-h.score, h.agent_id))
        return scored[:k]


class QdrantVectorStore:
    """Collections are immutable per (catalog version, embedder): a catalog change
    produces a new collection, so every deployed index is reproducible."""

    def __init__(self, url: str, api_key: str | None = None) -> None:
        self._client = qdrant_client(url, api_key)
        self._active = ""

    async def ensure(self, name: str, dim: int) -> bool:
        self._active = name
        await ensure_collection(self._client, name, dim)
        info = await self._client.get_collection(name)
        return bool(info.points_count)

    async def upsert(self, ids: list[str], vectors: list[list[float]]) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, i)), vector=v, payload={"agent_id": i}
            )
            for i, v in zip(ids, vectors, strict=True)
        ]
        await self._client.upsert(self._active, points=points, wait=True)

    async def search(self, vector: list[float], k: int) -> list[Hit]:
        result = await self._client.query_points(
            self._active, query=vector, limit=k, with_payload=True
        )
        hits: list[Hit] = []
        for point in result.points:
            payload: dict[str, Any] = point.payload or {}
            hits.append(Hit(str(payload["agent_id"]), float(point.score)))
        return hits
