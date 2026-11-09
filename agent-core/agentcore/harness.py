"""HARNESS (concept 4): what makes an LLM actually do things.

  Context injection  build_context(): request + state + memory + retrieved docs + skills + tools
  Control            Compactor: keeps the context window bounded (compaction)
                     (orchestration lives in orchestrate.py; the loop itself is the controller)
  Action             act.py tools (bash/grep-style code tools are just Tools with category "code")
  Persist            Checkpointer: progress file per run (JSON) so work is inspectable and resumable
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Protocol

from .memory import ShortTermMemory
from .rag import Chunk
from .reason import LLMProvider
from .skills import Skill
from .types import Percept, ToolResult


def build_context(percept: Percept, *, state: dict[str, Any], recalled: list[str], chunks: list[Chunk],
                  skills_catalog: list[dict[str, str]], selected_skills: list[Skill],
                  tool_catalog: list[dict[str, Any]]) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "request": {"kind": percept.kind, "content": percept.content, "entities": percept.entities},
        "current_state": state,
        "long_term_memory": recalled,
        "tool_catalog": tool_catalog,
    }
    if chunks:
        ctx["retrieved_context"] = [{"cite": c.cite, "text": c.text} for c in chunks]
    if skills_catalog:
        ctx["skills_catalog"] = skills_catalog
    if selected_skills:
        ctx["active_skills"] = [{"name": s.name, "instructions": s.body} for s in selected_skills]
    return ctx


class Compactor(Protocol):
    def compact(self, stm: ShortTermMemory) -> bool: ...


def _size(stm: ShortTermMemory) -> int:
    return sum(len(m["content"]) for m in stm.messages)


class TruncatingCompactor:
    """Replace the middle of the conversation with a short note once it exceeds max_chars."""

    def __init__(self, max_chars: int = 12000, keep_last: int = 6):
        self.max_chars, self.keep_last = max_chars, keep_last

    def compact(self, stm: ShortTermMemory) -> bool:
        if _size(stm) <= self.max_chars or len(stm.messages) <= self.keep_last + 1:
            return False
        dropped = stm.messages[1:-self.keep_last]
        note = (f"[compacted {len(dropped)} earlier messages; "
                f"{sum('Result of' in m['content'] for m in dropped)} tool results summarized away]")
        stm.messages = stm.messages[:1] + [{"role": "user", "content": note}] + stm.messages[-self.keep_last:]
        return True


class LLMCompactor(TruncatingCompactor):
    """Same trigger, but asks the LLM to summarize what was dropped (keeps facts, drops noise)."""

    def __init__(self, llm: LLMProvider, max_chars: int = 12000, keep_last: int = 6):
        super().__init__(max_chars, keep_last)
        self.llm = llm

    def compact(self, stm: ShortTermMemory) -> bool:
        if _size(stm) <= self.max_chars or len(stm.messages) <= self.keep_last + 1:
            return False
        dropped = stm.messages[1:-self.keep_last]
        resp = self.llm.complete("Summarize these agent steps as short bullet facts. Keep ids, numbers, errors.",
                                 [{"role": "user", "content": json.dumps(dropped)[:20000]}], max_tokens=400)
        stm.messages = (stm.messages[:1] + [{"role": "user", "content": f"[summary of earlier steps]\n{resp.text}"}]
                        + stm.messages[-self.keep_last:])
        return True


class Checkpointer:
    """Persist: write a progress file per run after every iteration (<dir>/<trace_id>.json)."""

    def __init__(self, directory: str):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, *, trace_id: str, agent: str, percept: Percept, iteration: int, status: str,
             history: list[dict[str, str]], results: list[ToolResult]) -> pathlib.Path:
        path = self.dir / f"{trace_id}.json"
        path.write_text(json.dumps({
            "trace_id": trace_id, "agent": agent, "updated": time.time(), "status": status,
            "iteration": iteration, "request": percept.content,
            "tool_results": [{"tool": r.tool, "ok": r.ok, "error": r.error} for r in results],
            "history": history,
        }, indent=2, default=str))
        return path

    def load(self, trace_id: str) -> dict[str, Any]:
        return json.loads((self.dir / f"{trace_id}.json").read_text())
