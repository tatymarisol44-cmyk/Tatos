from __future__ import annotations

import math

from orchestrator.embeddings import HashingEmbedder, tokenize
from orchestrator.vectorstore import InMemoryVectorStore


def test_tokenize_folds_accents_and_drops_stopwords() -> None:
    assert tokenize("¿Cómo optimizo la aplicación?") == ["optimizo", "aplicacion"]
    assert "ci/cd" not in tokenize("CI/CD with c++ and c#")
    assert "c++" in tokenize("CI/CD with c++ and c#")


async def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    emb = HashingEmbedder(dim=256)
    [a], [b] = await emb.embed(["docker kubernetes"]), await emb.embed(["docker kubernetes"])
    assert a == b
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)
    assert emb.signature == "hashing-256"


async def test_empty_text_embeds_to_zero_vector() -> None:
    [vec] = await HashingEmbedder(dim=64).embed(["the and of"])
    assert all(v == 0.0 for v in vec)


async def test_in_memory_store_ranks_by_similarity() -> None:
    emb = HashingEmbedder()
    store = InMemoryVectorStore()
    assert await store.ensure("idx", emb.dim) is False
    docs = {"seo": "google search rankings keywords", "k8s": "kubernetes docker deploy"}
    await store.upsert(list(docs), await emb.embed(list(docs.values())))
    assert await store.ensure("idx", emb.dim) is True
    [query] = await emb.embed(["deploy with kubernetes"])
    hits = await store.search(query, k=2)
    assert [h.agent_id for h in hits] == ["k8s", "seo"]
    assert hits[0].score > hits[1].score
