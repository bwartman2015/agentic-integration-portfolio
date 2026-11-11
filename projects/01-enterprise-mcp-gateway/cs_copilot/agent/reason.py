"""THINK: the only module that talks to an LLM. Prompts are versioned here.

Providers (LLM_PROVIDER):
  scripted   (default) replays a FakeLLM script from fixtures/llm/<name>.json, so the demo, tests and the
             scripted eval baseline run offline and deterministically. A script step may branch on the
             real result of an earlier tool call ({"branch_on": tool, "ok": step, "failed": step}):
             `make ask` runs scripts against the live gateway, where a human decides a write, so the
             answer must follow that decision. Scripted runs test the harness (tools, guardrails,
             verifier, citations), not model quality.
  local      (Milestone 7) a small open model (Qwen2.5-1.5B-Instruct) on the llama.cpp server
             (LOCAL_LLM_URL): free and offline. The official real-model eval baseline
             (evals/baseline.local.json); the project runs entirely on free components.
  anthropic  (optional, paid; not used by the project's baselines) Claude through the official
             `anthropic` SDK, for anyone who wants to plug in a hosted model: ANTHROPIC_API_KEY,
             ANTHROPIC_MODEL, ANTHROPIC_PRICE_IN_PER_MTOK / _OUT_PER_MTOK (cost tracking), optional
             ANTHROPIC_EFFORT. Any other hosted model would be one more provider class here.
  fake       a fixed escalation, for tests.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

import urllib.error
import urllib.request

from agentcore import FakeLLM, LLMProvider, Reasoner
from agentcore.reason import LLMResponse, SYSTEM_PROMPT

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "llm"

# v2 (Milestone 7): states the checks a deterministic verifier applies to every answer (verify.py), so a
# real model is told the rules it is judged by. The scripted model ignores the prompt.
CS_COPILOT_PROMPT = """You are Ridgeline's Customer Service Copilot. Answer order, shipment and invoice questions using only the tools in the catalog and the retrieved policy documents. Recommend a next step; creating a case needs a human's approval.

Every answer is checked before the rep sees it:
- Look up every account, order, invoice and shipment the request names with a tool before answering. If a lookup finds nothing, say it was not found; never guess.
- Every ID (SO-, PO-, INV-, SHP-, ACCT-, CASE-) and every dollar amount you write must appear in a tool result or in the request. Quote totals exactly as a tool returned them; never add up or invent numbers.
- Cite policy with the document's exact cite value in square brackets, for example [customer-service-policy.md#0]. Cite only documents you were given.
- Call tools with exactly the argument names in the catalog. One tool call per decision unless you make a plan.
- If a tool result says access is denied because a record is outside the caller's region, stop: do not try other tools to reach it. Escalate, and in `reasoning` tell the rep plainly that the record is outside their region, that a CS lead can help, and cite the data access policy.
- Pricing, credit-limit and payment-term changes are for humans only: escalate, do not attempt them.
- create_case runs only after a human approves it. Report what actually happened: the case id if it was created, or that no case was created and why."""

AGENT_PROMPTS = {
    "cs-copilot": CS_COPILOT_PROMPT,
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


REFUSED = json.dumps({"type": "escalate", "reasoning": "The model declined this request; a person should handle it."})


class ClaudeLLM:
    """Claude through the official SDK. Thinking is adaptive (the model's default); `effort` sets its
    depth. Server-side refusal fallbacks are on ("default" routing), and the model that actually answered
    is kept in `served_models`, so an eval report shows if a fallback ever ran. A refusal that reaches us
    becomes an escalation, never an answer. Cost is computed from the response's token usage and the
    per-million-token prices you set in .env (none are hard-coded)."""

    def __init__(self, model: str, price_in_per_mtok: float, price_out_per_mtok: float, effort: str | None = None,
                 client: Any = None, max_tokens: int = 16000):
        if client is None:
            import anthropic                      # only the copilot image has the SDK
            client = anthropic.Anthropic(max_retries=3)
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens
        self.pin, self.pout = price_in_per_mtok, price_out_per_mtok
        self.served_models: set[str] = set()

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1024) -> LLMResponse:
        params: dict[str, Any] = dict(model=self.model, max_tokens=max(max_tokens, self.max_tokens), system=system,
                                      messages=messages, betas=["server-side-fallback-2026-07-01"],
                                      fallbacks="default")
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        resp = self.client.beta.messages.create(**params)
        served = getattr(resp, "model", self.model) or self.model
        self.served_models.add(served)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason == "refusal":
            text = REFUSED
        usage = resp.usage
        tin, tout = int(usage.input_tokens or 0), int(usage.output_tokens or 0)
        return LLMResponse(text=text, input_tokens=tin, output_tokens=tout, model=served,
                           cost_usd=(tin * self.pin + tout * self.pout) / 1_000_000)


class LocalLLM:
    """A small open model on llama.cpp's OpenAI-compatible server. JSON mode (a grammar) makes every reply
    one valid JSON object, which a 1.5B model needs. Free: cost 0; tokens are still counted."""

    def __init__(self, url: str, model: str = "qwen2.5-1.5b-instruct", timeout_s: float = 900.0,
                 max_tokens: int = 768):
        self.url, self.model, self.timeout_s, self.max_tokens = url.rstrip("/"), model, timeout_s, max_tokens
        self.served_models = {model}

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1024) -> LLMResponse:
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages],
                "max_tokens": min(max_tokens, self.max_tokens), "temperature": 0,
                "response_format": {"type": "json_object"}}
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"] or ""
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"local model failed ({type(exc).__name__})") from None
        usage = data.get("usage") or {}
        return LLMResponse(text=text, input_tokens=int(usage.get("prompt_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or 0), model=self.model, cost_usd=0.0)


def _price(name: str) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise ValueError(f"set {name} in .env (USD per million tokens) so cost is tracked")
    return float(raw)


def get_llm(script: str | None = None) -> LLMProvider:
    """LLM_PROVIDER=scripted (default) | anthropic | local | fake. `script` is used only by scripted."""
    provider = os.environ.get("LLM_PROVIDER", "scripted").strip()
    if provider == "scripted":
        name = script or os.environ.get("LLM_SCRIPT")
        if not name:
            raise ValueError("LLM_PROVIDER=scripted needs a script name (argument or LLM_SCRIPT)")
        return scripted_llm(name)
    if provider == "fake":
        return FakeLLM([{"type": "escalate", "reasoning": "fake provider: script responses in tests"}])
    if provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        if not model:
            raise ValueError("set ANTHROPIC_MODEL in .env")
        return ClaudeLLM(model, _price("ANTHROPIC_PRICE_IN_PER_MTOK"), _price("ANTHROPIC_PRICE_OUT_PER_MTOK"),
                         effort=os.environ.get("ANTHROPIC_EFFORT", "").strip() or None)
    if provider == "local":
        return LocalLLM(os.environ.get("LOCAL_LLM_URL", "http://llm:8080"))
    raise ValueError(f"unknown LLM_PROVIDER {provider!r}: scripted, anthropic, local or fake")


def get_reasoner(agent_name: str, llm: LLMProvider) -> Reasoner:
    return Reasoner(llm, system_prompt=AGENT_PROMPTS[agent_name] + "\n\n" + SYSTEM_PROMPT)
