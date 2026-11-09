"""REMEMBER (Memory & State): long-term memory. Short-term memory is per run, managed by the loop.

Long-term here: past disputes by customer and deduction code with outcomes (episodic), learned 'who to ask first' patterns.
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
