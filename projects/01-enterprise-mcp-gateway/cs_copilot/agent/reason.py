"""THINK: the only module that talks to an LLM. Prompts are versioned here.

Providers (LLM_PROVIDER): ollama (default, free) | anthropic | scripted | fake.
`scripted` replays a FakeLLM script from fixtures/llm/<name>.json so the demo, tests and the
scripted eval baseline run offline and deterministically. A script step may branch on the real result
of an earlier tool call ({"branch_on": tool, "ok": step, "failed": step}): `make ask` runs scripts
against the live gateway, where a human decides a write, so the answer must follow that decision. Scripted runs test the harness
(tools, guardrails, verifier, citations), not model quality; that is what the Ollama baseline is for.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "llm"

AGENT_PROMPTS = {
    "cs-copilot": "You are Ridgeline's Customer Service Copilot. Answer order, shipment and invoice questions using only gateway tools and cited policy documents. Recommend a next step; creating a case needs approval.",
}


class ScriptedLLM(FakeLLM):
    """FakeLLM whose branch steps pick `ok` or `failed` from the latest "Result of <tool>: ..." message,
    and fill {placeholders} from that result (e.g. the case id the CRM really assigned)."""

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1024) -> Any:
        if self.responses:
            step = json.loads(self.responses[0])
            if isinstance(step, dict) and "branch_on" in step:
                result = _last_result(messages, step["branch_on"])
                ok = isinstance(result, dict) and "error" not in result
                chosen = json.dumps(step["ok" if ok else "failed"])
                values = {k: v for k, v in (result or {}).items() if isinstance(v, (str, int, float))}
                if isinstance(values.get("error"), str):          # the refusal's own words, without the class name
                    err = values["error"]
                    values["error"] = err[err.find("Access denied"):] if "Access denied" in err else err.split(": ", 1)[-1]
                self.responses[0] = re.sub(r"\{(\w+)\}", lambda m: json.dumps(str(values[m.group(1)]))[1:-1]
                                           if m.group(1) in values else m.group(0), chosen)
        return super().complete(system, messages, max_tokens)


def _last_result(messages: list[dict[str, str]], tool: str) -> Any:
    prefix = f"Result of {tool}: "
    for m in reversed(messages):
        if m.get("content", "").startswith(prefix):
            try:
                return json.loads(m["content"][len(prefix):])
            except ValueError:
                return {"error": "unreadable result"}
    return None


def scripted_llm(script: str) -> FakeLLM:
    """Replay fixtures/llm/<script>.json: {"description": ..., "responses": [decision or plan JSON, ...]}."""
    data = json.loads((FIXTURES_DIR / f"{script}.json").read_text())
    return ScriptedLLM(data["responses"])


def scripted_approval(script: str) -> str | None:
    """The human decision a script assumes for write tools ("approve" | "reject" | "timeout"), if any."""
    return json.loads((FIXTURES_DIR / f"{script}.json").read_text()).get("approval")


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
