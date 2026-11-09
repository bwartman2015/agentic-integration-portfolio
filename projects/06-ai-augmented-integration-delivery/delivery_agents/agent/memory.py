"""REMEMBER (Memory & State): long-term memory. Short-term memory is per run, managed by the loop.

Long-term here: repo conventions and learned patterns from which AI tests humans kept or deleted and which review findings were accepted.
"""
import os

from agentcore import LongTermMemory, MemoryItem, SQLiteMemory


class PgVectorMemory:
    """TODO(memory milestone): Postgres + pgvector implementation of LongTermMemory
    (embeddings from a local Ollama embedding model; cosine top-k; filter by kind)."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def recall(self, query: str, k: int = 3) -> list[MemoryItem]:
        raise NotImplementedError("PgVectorMemory.recall")

    def remember(self, item: MemoryItem) -> None:
        raise NotImplementedError("PgVectorMemory.remember")


def get_memory() -> LongTermMemory:
    dsn = os.environ.get("MEMORY_DSN")
    return PgVectorMemory(dsn) if dsn else SQLiteMemory(os.environ.get("MEMORY_SQLITE", "memory.db"))
