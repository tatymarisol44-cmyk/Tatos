"""Retrieval ports and value objects, independent of databases and provider SDKs.

Adapters implement these contracts; application policy imports them directly.
Compatibility re-exports remain in the original adapter modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol


class Embedder(Protocol):
    @property
    def signature(self) -> str:
        """Stable id of the embedding space; part of the index name."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


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


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    index: int
    text: str
    score: float = 0.0
    version: str = ""  # which upload of the document this chunk belongs to

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChunkStore(Protocol):
    """Vector storage of chunks. It holds every version of a document side by side; which
    one is live is decided by the SQL pointer in KnowledgeBase, never by the store."""

    async def ensure(self, name: str, dim: int) -> None: ...

    def attach(self, name: str) -> None:
        """Use an existing collection without creating it (maintenance jobs)."""
        ...

    async def upsert(
        self, tenant: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None: ...

    async def search(self, tenant: str, vector: list[float], k: int) -> list[Chunk]: ...

    async def delete(self, tenant: str, doc_id: str, version: str | None = None) -> int: ...


@dataclass(frozen=True)
class MemoryFact:
    id: str
    tenant: str
    subject_id: str
    text: str
    created_at: str  # ISO 8601, UTC
    expires_at: str
    source_thread: str
    key: str = ""
    value: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore(Protocol):
    async def ensure(self, name: str, dim: int) -> None: ...

    def attach(self, name: str) -> None:
        """Use an existing collection without creating it (maintenance jobs)."""
        ...

    async def upsert(self, facts: list[MemoryFact], vectors: list[list[float]]) -> None: ...

    async def search(
        self, tenant: str, subject_id: str, vector: list[float], k: int, now: datetime
    ) -> list[MemoryFact]: ...

    async def list(self, tenant: str, subject_id: str) -> list[MemoryFact]: ...

    async def delete_subject(self, tenant: str, subject_id: str) -> int: ...

    async def purge_expired(self, now: datetime, dry_run: bool = False) -> int: ...
