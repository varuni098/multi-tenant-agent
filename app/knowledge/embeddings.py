"""Text embeddings: Ollama (nomic-embed-text) in deployments, a deterministic hashing embedder in tests."""

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import httpx

from app.core.config import get_settings


class Embedder(Protocol):
    dimensions: int
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingUnavailable(Exception):
    pass


class OllamaEmbedder:
    """nomic-embed-text expects task prefixes: 'search_document: ' and 'search_query: '."""

    def __init__(self, model: str, dimensions: int, base_url: str) -> None:
        self.name, self.dimensions, self._url = model, dimensions, f"{base_url}/api/embed"

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            resp = httpx.post(self._url, json={"model": self.name, "input": inputs}, timeout=120)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingUnavailable(f"Embedding model unavailable: {e}") from e
        vectors = resp.json()["embeddings"]
        if vectors and len(vectors[0]) != self.dimensions:
            raise EmbeddingUnavailable(f"{self.name} returned {len(vectors[0])} dims, expected {self.dimensions}")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 16):
            out.extend(self._embed([f"search_document: {t}" for t in texts[i : i + 16]]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"search_query: {text}"])[0]


class HashEmbedder:
    """Feature-hashed bag of words (+ bigrams), L2-normalised. Lexical, deterministic, no model needed."""

    name = "hash"

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9_]+", text.lower())
        vec = [0.0] * self.dimensions
        for feature in words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            vec[h % self.dimensions] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "hash":
        return HashEmbedder(s.embedding_dimensions)
    return OllamaEmbedder(s.embedding_model, s.embedding_dimensions, s.ollama_native_url)
