"""REMEMBER layer.

Short-term: conversation context and working state for one run.
Long-term: episodic memory and learned patterns that persist across runs.

SQLiteMemory is a zero-dependency stand-in using keyword overlap. Each project's
"memory" milestone replaces it with PgVectorMemory (Postgres + pgvector embeddings)
behind the same LongTermMemory protocol.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ShortTermMemory:
    max_messages: int = 40
    messages: list[dict[str, str]] = field(default_factory=list)
    working: dict[str, Any] = field(default_factory=dict)

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > self.max_messages:
            # keep the earliest message (task framing) and the most recent window
            self.messages = self.messages[:1] + self.messages[-(self.max_messages - 1):]


@dataclass
class MemoryItem:
    text: str
    kind: str = "episode"          # episode | pattern | fact
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


class LongTermMemory(Protocol):
    """A store may set `filters_audience = True` and accept `recall(query, k, audience=...)`: it then
    returns only items whose meta["audience"] equals `audience` (filtered in the store, e.g. in SQL).
    Stores without it are filtered by the agent loop."""
    def recall(self, query: str, k: int = 3) -> list[MemoryItem]: ...
    def remember(self, item: MemoryItem) -> None: ...


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}


class SQLiteMemory:
    filters_audience = True

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False + a lock: A2A servers run agents on worker threads
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.db.execute("create table if not exists memory (id integer primary key, kind text, text text, meta text, ts real)")

    def remember(self, item: MemoryItem) -> None:
        with self._lock:
            self.db.execute("insert into memory (kind, text, meta, ts) values (?,?,?,?)",
                            (item.kind, item.text, json.dumps(item.meta), time.time()))
            self.db.commit()

    def recall(self, query: str, k: int = 3, audience: str | None = None) -> list[MemoryItem]:
        q = _tokens(query)
        scored = []
        with self._lock:
            if audience is None:
                rows = self.db.execute("select kind, text, meta from memory").fetchall()
            else:
                rows = self.db.execute("select kind, text, meta from memory where json_extract(meta, '$.audience') = ?",
                                       (audience,)).fetchall()
        for kind, text, meta in rows:
            overlap = len(q & _tokens(text)) / (len(q) or 1)
            if overlap > 0:
                scored.append(MemoryItem(text=text, kind=kind, meta=json.loads(meta), score=overlap))
        return sorted(scored, key=lambda m: m.score, reverse=True)[:k]
