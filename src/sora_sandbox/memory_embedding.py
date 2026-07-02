from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict


class EmbeddingProvider(Protocol):
    model: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


@dataclass
class DeterministicEmbeddingProvider:
    """Local deterministic embedding for tests and offline development.

    This is intentionally not token-overlap retrieval: it produces a dense vector
    from hashed lexical features, stores it in the vector index, and exercises
    the same upsert/search/delete path as a remote embedding provider.
    """

    dimension: int = 96
    model: str = "deterministic-hash-embedding-v1"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        features = search_tokens(text)
        if not features:
            features = [text.strip().lower()] if text.strip() else ["empty"]
        vector = [0.0] * self.dimension
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            primary = int.from_bytes(digest[:4], "big") % self.dimension
            secondary = int.from_bytes(digest[4:8], "big") % self.dimension
            sign = 1.0 if digest[8] % 2 == 0 else -1.0
            vector[primary] += sign
            vector[secondary] += sign * 0.5
        return _normalize(vector)


@dataclass
class OpenAICompatibleEmbeddingProvider:
    api_key: str
    base_url: str
    model: str
    dimension: int = 0
    timeout_seconds: float = 20.0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            _embeddings_url(self.base_url),
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network/provider path.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"embedding provider HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network/provider path.
            raise RuntimeError(f"embedding provider unavailable: {exc.reason}") from exc

        data = json.loads(body)
        rows = sorted(data.get("data", []), key=lambda item: int(item.get("index", 0)))
        vectors = [list(map(float, row["embedding"])) for row in rows]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        if vectors and not self.dimension:
            self.dimension = len(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def create_embedding_provider_from_env() -> EmbeddingProvider:
    provider = os.getenv("SORA_EMBEDDING_PROVIDER", "").strip().lower()
    api_key = os.getenv("SORA_EMBEDDING_API_KEY", "").strip()
    if provider in {"openai", "openai_compatible", "compatible"} or api_key:
        return OpenAICompatibleEmbeddingProvider(
            api_key=api_key or os.getenv("OPENAI_API_KEY", "").strip(),
            base_url=(
                os.getenv("SORA_EMBEDDING_BASE_URL", "").strip()
                or os.getenv("OPENAI_BASE_URL", "").strip()
                or "https://api.openai.com/v1"
            ),
            model=(
                os.getenv("SORA_EMBEDDING_MODEL", "").strip()
                or "text-embedding-3-small"
            ),
        )
    return DeterministicEmbeddingProvider()


def vector_hash(vector: list[float]) -> str:
    rounded = [round(value, 8) for value in vector]
    return hashlib.sha256(json.dumps(rounded, sort_keys=True).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def embedding_provider_info(provider: EmbeddingProvider) -> JsonDict:
    return {
        "model": provider.model,
        "dimension": provider.dimension,
        "provider": provider.__class__.__name__,
    }


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def _embeddings_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/embeddings"):
        return clean
    return f"{clean}/embeddings"
