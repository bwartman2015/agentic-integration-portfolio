"""THINK layer: the reasoning engine. All LLM calls in a project go through this module.

The Reasoner runs a ReAct-style step: given the percept, recalled memory, short-term
history and the tool catalog, it returns one Decision (answer / tool / plan / escalate).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .types import Decision


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0


class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1024) -> LLMResponse: ...


class FakeLLM:
    """Scripted provider for tests and offline demos. Each call pops the next response."""

    def __init__(self, responses: list[str | dict]):
        self.responses = [r if isinstance(r, str) else json.dumps(r) for r in responses]
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, messages, max_tokens=1024):
        self.calls.append({"system": system, "messages": messages})
        if not self.responses:
            raise RuntimeError("FakeLLM ran out of scripted responses")
        text = self.responses.pop(0)
        return LLMResponse(text=text, input_tokens=100, output_tokens=20, model="fake", cost_usd=0.001)


class OllamaLLM:
    """Free local model via Ollama (http://localhost:11434). Cost is reported as 0."""

    def __init__(self, model: str = "llama3.1", host: str | None = None, timeout: float = 120):
        self.model, self.timeout = model, timeout
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def complete(self, system, messages, max_tokens=1024):
        body = {"model": self.model, "stream": False, "options": {"num_predict": max_tokens},
                "messages": [{"role": "system", "content": system}, *messages]}
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        return LLMResponse(text=data["message"]["content"], input_tokens=data.get("prompt_eval_count", 0),
                           output_tokens=data.get("eval_count", 0), model=self.model)


class AnthropicLLM:
    """Optional Claude provider. Set ANTHROPIC_API_KEY. Pass your own per-million-token prices
    so cost tracking is accurate (check current pricing; none are hard-coded here)."""

    def __init__(self, model: str, price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0,
                 timeout: float = 120):
        self.model, self.timeout = model, timeout
        self.pin, self.pout = price_in_per_mtok, price_out_per_mtok

    def complete(self, system, messages, max_tokens=1024):
        body = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data["content"])
        u = data.get("usage", {})
        tin, tout = u.get("input_tokens", 0), u.get("output_tokens", 0)
        return LLMResponse(text=text, input_tokens=tin, output_tokens=tout, model=self.model,
                           cost_usd=(tin * self.pin + tout * self.pout) / 1_000_000)


SYSTEM_PROMPT = """You are a careful enterprise integration agent. Think step by step, then reply with ONE JSON object only.
First ask yourself: can I answer directly from the request, memory and results so far?
- If yes: {"type": "answer", "answer": "...", "reasoning": "..."}
- If one tool call is needed next: {"type": "tool", "tool": "<name>", "args": {...}, "reasoning": "..."}
- If the goal needs several ordered steps: {"type": "plan", "reasoning": "..."}
- If you cannot or must not proceed: {"type": "escalate", "reasoning": "..."}
Only use tools from the catalog. Never invent tool results.
Context you may receive: current_state (live snapshot), long_term_memory (past runs),
retrieved_context (documents; cite them as [cite]), skills_catalog / active_skills (follow an active
skill's steps; call load_skill to read one from the catalog)."""


def extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]}")
    return json.loads(match.group(0))


class Reasoner:
    def __init__(self, llm: LLMProvider, system_prompt: str = SYSTEM_PROMPT):
        self.llm, self.system_prompt = llm, system_prompt
        self.last_response: LLMResponse | None = None

    def decide(self, context: dict[str, Any], history: list[dict[str, str]]) -> Decision:
        """context comes from harness.build_context (request, state, memory, RAG, skills, tools)."""
        messages = [{"role": "user", "content": json.dumps(context)}, *history]
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": "Decide the next action. JSON only."})
        resp = self.llm.complete(self.system_prompt, messages)
        self.last_response = resp
        try:
            d = extract_json(resp.text)
        except (ValueError, json.JSONDecodeError) as e:
            return Decision(kind="escalate", reasoning=f"unparseable model output: {e}")
        kind = d.get("type")
        if kind not in {"answer", "tool", "plan", "escalate"}:
            return Decision(kind="escalate", reasoning=f"unknown decision type {kind!r}")
        return Decision(kind=kind, answer=d.get("answer"), tool=d.get("tool"), args=d.get("args") or {},
                        reasoning=d.get("reasoning", ""))
