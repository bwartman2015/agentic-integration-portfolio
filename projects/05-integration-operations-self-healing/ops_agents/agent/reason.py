"""THINK: the only module that talks to an LLM. Prompts are versioned here."""
import os

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, MANAGER_PROMPT, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

AGENT_PROMPTS = {
    "diagnoser": "You diagnose integration incidents. Classify using rules first (HTTP 503 = transient, 401/403 = config-security, schema errors = mapping-code, unknown reference data = data). Read-only. State the business impact (e.g. 'BBO 855s delayed').",
    "remediator": 'You remediate a diagnosed incident with the smallest allow-listed action, then verify metrics. Security/credential issues are never auto-fixed: page on-call.',
    "incident-commander": MANAGER_PROMPT + ' Domain: IT Operations · Integration Platform at Ridgeline Supply Co.',
}


def get_llm() -> LLMProvider:
    """LLM_PROVIDER=ollama (default, free) | anthropic | fake."""
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    if provider == "fake":
        return FakeLLM([{"type": "escalate", "reasoning": "fake provider: script responses in tests"}])
    if provider == "anthropic":
        return AnthropicLLM(model=os.environ["ANTHROPIC_MODEL"],
                            price_in_per_mtok=float(os.environ.get("PRICE_IN_PER_MTOK", 0)),
                            price_out_per_mtok=float(os.environ.get("PRICE_OUT_PER_MTOK", 0)))
    return OllamaLLM(model=os.environ.get("OLLAMA_MODEL", "llama3.1"))


def get_reasoner(agent_name: str, llm: LLMProvider) -> Reasoner:
    return Reasoner(llm, system_prompt=AGENT_PROMPTS[agent_name] + "\n\n" + SYSTEM_PROMPT)
