"""Infrastructure adapters chosen by configuration: the only place that knows which
embedding service and vector database back the domain's ports (Embedder, VectorStore,
ChunkStore, MemoryStore). Domain and application code receive the ports."""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.embeddings import Embedder, HashingEmbedder, LiteLLMEmbedder
from orchestrator.knowledge import ChunkStore, InMemoryChunkStore, QdrantChunkStore
from orchestrator.memory import InMemoryMemoryStore, MemoryStore, QdrantMemoryStore
from orchestrator.vectorstore import InMemoryVectorStore, QdrantVectorStore, VectorStore


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_backend == "litellm":
        return LiteLLMEmbedder(settings.embedding_model)
    return HashingEmbedder()


def build_store(settings: Settings) -> VectorStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantVectorStore(settings.qdrant_url, key)
    return InMemoryVectorStore()


def build_chunk_store(settings: Settings) -> ChunkStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantChunkStore(settings.qdrant_url, key)
    return InMemoryChunkStore()


def build_memory_store(settings: Settings) -> MemoryStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantMemoryStore(settings.qdrant_url, key)
    return InMemoryMemoryStore()
