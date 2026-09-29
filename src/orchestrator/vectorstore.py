"""Vector stores for the agent index: in-process for dev/tests, Qdrant for production."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Hit:
    agent_id: str
    score: float


class VectorStore(Protocol):
    async def ensure(self, name: str, dim: int) -> bool:
        """Point the store at collection `name`. Returns True if it already holds data."""
        ...

    async def upsert(self, ids: list[str], vectors: list[list[float]]) -> None: ...

    async def search(self, vector: list[float], k: int) -> list[Hit]: ...


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
        from qdrant_client import AsyncQdrantClient

        # ":memory:" runs Qdrant's embedded local mode (used by the tests).
        self._client = (
            AsyncQdrantClient(location=":memory:")
            if url == ":memory:"
            else AsyncQdrantClient(url=url, api_key=api_key)
        )
        self._active = ""

    async def ensure(self, name: str, dim: int) -> bool:
        from qdrant_client.models import Distance, VectorParams

        self._active = name
        if await self._client.collection_exists(name):
            info = await self._client.get_collection(name)
            return bool(info.points_count)
        await self._client.create_collection(
            name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
        )
        return False

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
