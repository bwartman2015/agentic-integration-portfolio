"""REMEMBER (Memory & State): long-term memory. Short-term memory is per run, managed by the loop.

Long-term here: pgvector store of answered questions, account-name aliases (BBO = Big Box Outdoor), and tool sequences that worked.

Answers hold customer data, so recall obeys the access policy: `build.memory_audience` tags every episode
with the caller's scopes and regions, and the agent loop recalls only episodes with the same tag.
"""
import os

from agentcore import LongTermMemory, MemoryItem, SQLiteMemory


class PgVectorMemory:
    """TODO(Milestone 6): Postgres + pgvector implementation of LongTermMemory (embeddings from a local
    Ollama embedding model; cosine top-k; filter by kind and memory audience in the SQL).
    Connection: DATABASE_URL, the same database as the gateway's audit log: the local Postgres by
    default, Neon (or any hosted Postgres, sslmode=require) when DATABASE_URL is set. Its tables go in a
    separate `memory` schema and need the pgvector extension (`CREATE EXTENSION vector`, available on
    Neon; the local container moves to a pgvector image)."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def recall(self, query: str, k: int = 3) -> list[MemoryItem]:
        raise NotImplementedError("PgVectorMemory.recall")

    def remember(self, item: MemoryItem) -> None:
        raise NotImplementedError("PgVectorMemory.remember")


def get_memory() -> LongTermMemory:
    # Until PgVectorMemory exists (Milestone 6) only an explicit MEMORY_DSN selects it; Milestone 6
    # switches this to DATABASE_URL so memory and audit share one database.
    dsn = os.environ.get("MEMORY_DSN")
    return PgVectorMemory(dsn) if dsn else SQLiteMemory(os.environ.get("MEMORY_SQLITE", "memory.db"))
