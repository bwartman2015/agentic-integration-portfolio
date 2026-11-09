"""THINK: the only module that talks to an LLM. Prompts are versioned here."""
import os

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, MANAGER_PROMPT, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

AGENT_PROMPTS = {
    "order-exception-triage": 'You triage retail purchase orders that failed automated checks. Explain in plain English why each was flagged, check stock and history, and recommend accept, accept-partial, reject or hold. You never create or change orders yourself.',
    "partner-onboarding-copilot": 'You onboard new retail EDI partners. Use the retrieved implementation guide to draft a segment-by-segment mapping to the canonical order model, run the test harness, and fix failures.',
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
