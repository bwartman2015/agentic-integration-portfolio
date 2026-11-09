"""PERCEIVE layer: turn raw input (text, API payloads, events, files) into a typed Percept."""
from __future__ import annotations

from typing import Any, Protocol

from .types import Percept, Principal


class Perceiver(Protocol):
    def perceive(self, raw: Any, principal: Principal) -> Percept: ...


class TextPerceiver:
    """Default perceiver for plain-text questions. Projects replace this with domain parsers
    (X12 -> canonical JSON, alert webhook -> Incident, PR diff -> PR context)."""

    def __init__(self, max_chars: int = 8000):
        self.max_chars = max_chars

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("TextPerceiver expects non-empty text")
        text = " ".join(raw.split())[: self.max_chars]
        return Percept(kind="question", content=text, principal=principal, source="text", raw=raw)
