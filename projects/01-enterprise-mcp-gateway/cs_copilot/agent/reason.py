"""THINK: the only module that talks to an LLM. Prompts are versioned here.

Providers (LLM_PROVIDER): ollama (default, free) | anthropic | scripted | fake.
`scripted` replays a FakeLLM script from fixtures/llm/<name>.json so the demo, tests and the
scripted eval baseline run offline and deterministically. Scripted runs test the harness
(tools, guardrails, verifier, citations), not model quality; that is what the Ollama baseline is for.
"""
from __future__ import annotations

import json
import os
import pathlib

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "llm"

AGENT_PROMPTS = {
    "cs-copilot": "You are Ridgeline's Customer Service Copilot. Answer order, shipment and invoice questions using only gateway tools and cited policy documents. Recommend a next step; creating a case needs approval.",
}


def scripted_llm(script: str) -> FakeLLM:
    """Replay fixtures/llm/<script>.json: {"description": ..., "responses": [decision or plan JSON, ...]}."""
    data = json.loads((FIXTURES_DIR / f"{script}.json").read_text())
    return FakeLLM(data["responses"])


def get_llm(script: str | None = None) -> LLMProvider:
    """LLM_PROVIDER=ollama (default, free) | anthropic | scripted | fake. `script` is used only by scripted."""
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    if provider == "scripted":
        name = script or os.environ.get("LLM_SCRIPT")
        if not name:
            raise ValueError("LLM_PROVIDER=scripted needs a script name (argument or LLM_SCRIPT)")
        return scripted_llm(name)
    if provider == "fake":
        return FakeLLM([{"type": "escalate", "reasoning": "fake provider: script responses in tests"}])
    if provider == "anthropic":
        return AnthropicLLM(model=os.environ["ANTHROPIC_MODEL"],
                            price_in_per_mtok=float(os.environ.get("PRICE_IN_PER_MTOK", 0)),
                            price_out_per_mtok=float(os.environ.get("PRICE_OUT_PER_MTOK", 0)))
    return OllamaLLM(model=os.environ.get("OLLAMA_MODEL", "llama3.1"))


def get_reasoner(agent_name: str, llm: LLMProvider) -> Reasoner:
    return Reasoner(llm, system_prompt=AGENT_PROMPTS[agent_name] + "\n\n" + SYSTEM_PROMPT)
