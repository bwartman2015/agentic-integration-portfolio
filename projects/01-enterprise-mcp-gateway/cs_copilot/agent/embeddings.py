"""Text embeddings for long-term memory (Milestone 6).

Two embedders behind one interface, so memory works the same offline and with a real model:
  HashingEmbedder   deterministic, stdlib only, no model: feature-hashed words and word pairs. The
                    default for tests and offline runs. It finds shared wording, not meaning.
  LlamaCppEmbedder  nomic-embed-text-v1.5 served by llama.cpp's OpenAI-compatible server
                    (EMBEDDINGS_URL, e.g. http://embeddings:8080). Semantic recall.
Both return 768 unit-length floats and a `model` name. Memory stores the model with every vector and
only compares vectors of the same model, so switching embedders never mixes them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from typing import Protocol

DIM = 768
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class Embedder(Protocol):
    model: str

    def embed_document(self, text: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    model = "hashing-v1-768"

    def _embed(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
        vec = [0.0] * DIM
        for feature in features:
            digest = hashlib.sha256(feature.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % DIM
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        return _unit(vec)

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class EmbeddingError(RuntimeError):
    """The embedding service failed or answered something unusable."""


class LlamaCppEmbedder:
    """nomic-embed-text-v1.5 expects task prefixes: `search_document:` for what is stored and
    `search_query:` for what is looked up."""
    model = "nomic-embed-text-v1.5"

    def __init__(self, url: str, timeout_s: float = 30.0):
        self.url, self.timeout_s = url.rstrip("/"), timeout_s

    def _embed(self, text: str) -> list[float]:
        body = json.dumps({"input": [text], "model": self.model}).encode()
        req = urllib.request.Request(f"{self.url}/v1/embeddings", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
            vec = data["data"][0]["embedding"]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError(f"embedding service failed ({type(exc).__name__})") from None
        if not isinstance(vec, list) or len(vec) != DIM or not all(isinstance(x, (int, float)) for x in vec):
            raise EmbeddingError(f"embedding service returned a vector that is not {DIM} numbers")
        return _unit([float(x) for x in vec])

    def embed_document(self, text: str) -> list[float]:
        return self._embed("search_document: " + text)

    def embed_query(self, text: str) -> list[float]:
        return self._embed("search_query: " + text)


def get_embedder() -> Embedder:
    url = os.environ.get("EMBEDDINGS_URL", "").strip()
    return LlamaCppEmbedder(url) if url else HashingEmbedder()
