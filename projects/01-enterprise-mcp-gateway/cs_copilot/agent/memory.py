"""REMEMBER (Memory & State): long-term memory. Short-term memory is per run, managed by the loop.

Long-term here: answered questions (episodes) and learned account aliases, in Postgres + pgvector
(Milestone 6), table memory.items in the same database as the gateway's audit log: the local Postgres
by default, Neon (or any hosted Postgres) when MEMORY_DATABASE_URL is set (see db/memory.sql).

Answers hold customer data, so recall obeys the access policy: `build.memory_audience` tags every item
with the caller's scopes and regions, and `PgVectorMemory.recall` filters on that tag in SQL (with the
embedding model), so one caller's memories never reach another caller's prompt. Text is PII-masked
before it is stored, and an episode's vector is computed from its question, so recall compares a new
question with past questions. The copilot's database role can only add and read memory rows.

MEMORY_BACKEND=pgvector selects it (the copilot container sets it); the default is SQLiteMemory, the
offline stand-in used by the demo, the tests and the scripted evals.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agentcore import LongTermMemory, MemoryItem, SQLiteMemory

from .aio import run_sync
from .embeddings import Embedder, HashingEmbedder, get_embedder
from .guardrails import mask_pii

LOCAL_HOSTS = frozenset({"db", "localhost", "127.0.0.1", "::1"})
TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
KINDS = frozenset({"episode", "alias", "fact", "pattern"})

INSERT = ("INSERT INTO memory.items (audience, kind, text, meta, model, embedding) "
          "VALUES ($1, $2, $3, $4::jsonb, $5, $6::vector)")
ALIAS_EXISTS = ("SELECT 1 FROM memory.items WHERE audience = $1 AND model = $2 AND kind = 'alias' AND text = $3 "
                "LIMIT 1")
RECALL = ("SELECT kind, text, meta, 1 - (embedding <=> $1::vector) AS score FROM memory.items "
          "WHERE audience = $2 AND model = $3 ORDER BY embedding <=> $1::vector LIMIT $4")


class MemoryConfigError(RuntimeError):
    """Messages name variables, never values."""


@dataclass(frozen=True)
class MemoryConfig:
    dsn: str = field(default="", repr=False)
    host: str = "db"
    database: str = "ridgeline"
    user: str = "copilot_memory"
    password: str = field(default="", repr=False)
    timeout_s: float = 5.0

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "MemoryConfig":
        url = (env.get("MEMORY_DATABASE_URL") or "").strip()
        if url:
            try:
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
                parts.port
            except ValueError:
                raise MemoryConfigError("MEMORY_DATABASE_URL is not a valid URL") from None
            query = parse_qsl(parts.query, keep_blank_values=True)
            sslmode = dict(query).get("sslmode", "")
            if parts.scheme not in ("postgres", "postgresql") or not host or not parts.username:
                raise MemoryConfigError("MEMORY_DATABASE_URL must be postgresql://user@host/database")
            if host not in LOCAL_HOSTS and sslmode not in TLS_MODES:
                raise MemoryConfigError("MEMORY_DATABASE_URL must use TLS for a remote database (sslmode=require)")
            kept = [(k, v) for k, v in query if k != "channel_binding"]   # libpq-only option asyncpg lacks
            if sslmode in ("verify-ca", "verify-full") and "sslrootcert" not in dict(query):
                kept.append(("sslrootcert", SYSTEM_CA_BUNDLE))
            return MemoryConfig(dsn=urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), "")))
        password = env.get("COPILOT_MEMORY_DB_PASSWORD") or ""
        host = (env.get("MEMORY_DB_HOST") or "db").strip()
        database = (env.get("MEMORY_DB_NAME") or "ridgeline").strip()
        if len(password) < 16 or not _IDENT.match(database):
            raise MemoryConfigError("set MEMORY_DATABASE_URL, or COPILOT_MEMORY_DB_PASSWORD for the local database")
        return MemoryConfig(host=host, database=database, password=password)


Runner = Callable[[str, str, tuple], Awaitable[Any]]     # (method, query, params) -> result


def asyncpg_runner(config: MemoryConfig) -> Runner:
    async def run(method: str, query: str, params: tuple) -> Any:
        import asyncpg                                   # only the copilot container has it
        common = dict(timeout=config.timeout_s, command_timeout=config.timeout_s, statement_cache_size=0)
        conn = (await asyncpg.connect(dsn=config.dsn, **common) if config.dsn else
                await asyncpg.connect(host=config.host, user=config.user, password=config.password,
                                      database=config.database, **common))
        try:
            return await getattr(conn, method)(query, *params)
        finally:
            await conn.close()
    return run


def _vector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


class PgVectorMemory:
    """LongTermMemory on Postgres + pgvector. Memory is not a security control and is never allowed to
    lose an answer: if the database or the embedder fails, recall returns nothing and the write is
    skipped, and the error is kept in `last_error` and printed to stderr."""
    filters_audience = True

    def __init__(self, runner: Runner, embedder: Embedder, min_score: float | None = None):
        self.runner, self.embedder = runner, embedder
        default = 0.3 if isinstance(embedder, HashingEmbedder) else 0.6
        self.min_score = default if min_score is None else min_score
        self.last_error: str | None = None
        self.last_recall: list[MemoryItem] = []          # what the latest recall returned (make ask shows it)

    def _fail(self, what: str, exc: Exception) -> None:
        self.last_error = f"{what}: {type(exc).__name__}"
        print(f"memory {what} skipped ({type(exc).__name__})", file=sys.stderr)

    def remember(self, item: MemoryItem) -> None:
        audience = item.meta.get("audience")
        if not isinstance(audience, str) or not audience or item.kind not in KINDS:
            return                                       # untagged items would be recallable by no one
        text = mask_pii(item.text)
        try:
            if item.kind == "alias" and run_sync(self.runner("fetchval", ALIAS_EXISTS, (audience, self.embedder.model, text))):
                return
            # An episode is found by its question ("Q: ...\nA: ..."): questions are compared with questions.
            key = text.split("\nA:", 1)[0] if item.kind == "episode" else text
            vec = self.embedder.embed_document(key)
            run_sync(self.runner("execute", INSERT, (audience, item.kind, text, json.dumps(item.meta, default=str),
                                                     self.embedder.model, _vector(vec))))
        except Exception as exc:  # noqa: BLE001  database or embedder down: never lose the answer
            self._fail("write", exc)

    def recall(self, query: str, k: int = 3, audience: str | None = None) -> list[MemoryItem]:
        self.last_recall = []
        if not audience:
            return []                                    # recall is always scoped to one audience
        try:
            vec = self.embedder.embed_query(query)
            rows = run_sync(self.runner("fetch", RECALL, (_vector(vec), audience, self.embedder.model, max(1, k) * 3)))
        except Exception as exc:  # noqa: BLE001  database or embedder down: answer without memory
            self._fail("recall", exc)
            return []
        items, seen = [], set()
        for row in rows:
            score = float(row["score"])
            if score < self.min_score or row["text"] in seen:
                continue
            seen.add(row["text"])
            meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
            items.append(MemoryItem(text=row["text"], kind=row["kind"], meta=meta, score=round(score, 4)))
        self.last_recall = items[:k]
        return self.last_recall


def get_memory() -> LongTermMemory:
    if os.environ.get("MEMORY_BACKEND", "").strip() == "pgvector":
        config = MemoryConfig.from_env(dict(os.environ))
        return PgVectorMemory(asyncpg_runner(config), get_embedder(),
                              float(os.environ["MEMORY_MIN_SCORE"]) if os.environ.get("MEMORY_MIN_SCORE") else None)
    return SQLiteMemory(os.environ.get("MEMORY_SQLITE", "memory.db"))
