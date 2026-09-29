"""Text embedders. `HashingEmbedder` is offline and deterministic (CI, tests, cheap
dev); `LiteLLMEmbedder` calls any hosted embedding model through LiteLLM."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import unicodedata
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
_STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from how i in is it me my of on or our
    so that the this to we what when where which who why will with you your need want help
    el la los las un una unos unas y o de del en para por con como que qué mi mis me necesito
    quiero ayuda es son se su sus al lo le les hacer puedo""".split()
)


class Embedder(Protocol):
    @property
    def signature(self) -> str:
        """Stable id of the embedding space; part of the index name."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = [t.strip(".-") for t in _TOKEN.findall(folded)]
    return [t for t in tokens if t and t not in _STOPWORDS]


class HashingEmbedder:
    """Feature-hashed bag of unigrams + bigrams with sublinear TF, L2-normalised.

    IDF weighting was evaluated and lowered recall on evals/routing.jsonl, so it is
    intentionally left out."""

    def __init__(self, dim: int = 2048) -> None:
        self.dim = dim

    @property
    def signature(self) -> str:
        return f"hashing-{self.dim}"

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self.dim, 1.0 if (value >> 63) & 1 else -1.0

    def embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        counts: dict[str, int] = {}
        for feature in tokens + [f"{a}_{b}" for a, b in itertools.pairwise(tokens)]:
            counts[feature] = counts.get(feature, 0) + 1
        vec = [0.0] * self.dim
        for feature, count in counts.items():
            idx, sign = self._bucket(feature)
            vec[idx] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class LiteLLMEmbedder:
    def __init__(self, model: str, batch_size: int = 64) -> None:
        self.model = model
        self.batch_size = batch_size

    @property
    def signature(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.model.lower())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import litellm

        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            resp = await litellm.aembedding(model=self.model, input=batch)
            out.extend(list(item["embedding"]) for item in resp.data)
        return out
