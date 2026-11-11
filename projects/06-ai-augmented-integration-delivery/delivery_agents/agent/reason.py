"""THINK: the only module that talks to an LLM. Prompts are versioned here."""
import os

from agentcore import AnthropicLLM, FakeLLM, LLMProvider, MANAGER_PROMPT, OllamaLLM, Reasoner
from agentcore.reason import SYSTEM_PROMPT

AGENT_PROMPTS = {
    "test-generation": "You write API tests for changed endpoints of Ridgeline's integration APIs: happy path, auth failures, error model, pagination, idempotency. Only write under tests/ai_generated/.",
    "integration-review": 'You review integration code changes with the review checklist skill. Your review is advisory.',
    "pr-orchestrator": MANAGER_PROMPT + ' Domain: Engineering · API Lifecycle at Ridgeline Supply Co.',
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


class HeadlessCodingAgentLLM:
    """THINK provider that runs a headless coding agent (its non-interactive CLI) inside the CI runner.
    TODO(Milestone 3): run the agent's CLI as a subprocess with JSON output; parse text + usage into
    LLMResponse. Never pass secrets in the prompt."""

    def complete(self, system, messages, max_tokens=1024):
        raise NotImplementedError("HeadlessCodingAgentLLM.complete")
