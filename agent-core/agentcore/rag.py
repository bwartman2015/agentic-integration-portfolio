"""RAG (concept 3): fetch -> inject -> generate.

Documents are chunked, retrieved for the request, and injected into the prompt with a
citation id ([source#n]) so answers can cite where facts came from.

KeywordRetriever is a dependency-free BM25-style stand-in. Projects swap in a pgvector
retriever (embeddings from a local Ollama model) behind the same Retriever protocol.
"""
from __future__ import annotations

import math
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Chunk:
    source: str
    index: int
    text: str
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cite(self) -> str:
        return f"{self.source}#{self.index}"


class Retriever(Protocol):
    def retrieve(self, query: str, k: int = 4) -> list[Chunk]: ...


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    """Split on paragraphs, then pack paragraphs into ~size-char chunks with overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
        while len(cur) > size * 1.5:
            chunks.append(cur[:size])
            cur = cur[size - overlap:]
    if cur:
        chunks.append(cur)
    return chunks


def _terms(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", s.lower()) if len(t) > 1]


class KeywordRetriever:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.chunks: list[Chunk] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()

    def add(self, source: str, text: str, **meta: Any) -> int:
        n = 0
        for i, c in enumerate(chunk_text(text)):
            tf = Counter(_terms(c))
            self.chunks.append(Chunk(source=source, index=i, text=c, meta=meta))
            self._tf.append(tf)
            self._df.update(tf.keys())
            n += 1
        return n

    def add_dir(self, path: str | pathlib.Path, patterns: tuple[str, ...] = ("*.md", "*.txt")) -> int:
        n = 0
        for pat in patterns:
            for f in sorted(pathlib.Path(path).rglob(pat)):
                n += self.add(f.name, f.read_text(errors="ignore"))
        return n

    def retrieve(self, query: str, k: int = 4) -> list[Chunk]:
        if not self.chunks:
            return []
        q = _terms(query)
        N = len(self.chunks)
        avg = sum(sum(tf.values()) for tf in self._tf) / N
        scored = []
        for ch, tf in zip(self.chunks, self._tf):
            dl = sum(tf.values()) or 1
            s = 0.0
            for t in q:
                if t in tf:
                    idf = math.log(1 + (N - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    s += idf * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * dl / avg))
            if s > 0:
                scored.append(Chunk(ch.source, ch.index, ch.text, s, ch.meta))
        return sorted(scored, key=lambda c: c.score, reverse=True)[:k]
