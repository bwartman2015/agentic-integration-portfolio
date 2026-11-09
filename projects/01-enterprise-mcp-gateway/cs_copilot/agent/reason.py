"""THINK: the only module that talks to an LLM. Prompts are versioned here."""
import os

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, MANAGER_PROMPT, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

AGENT_PROMPTS = {
    "cs-copilot": "You are Ridgeline's Customer Service Copilot. Answer order, shipment and invoice questions using only gateway tools and cited policy documents. Recommend a next step; creating a case needs approval.",
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
