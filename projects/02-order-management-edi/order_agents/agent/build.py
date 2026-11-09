"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import FlaggedOrderPerceiver, PartnerGuidePerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import TriageVerifier, MappingVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


TRIAGE_AGENT_TOOLS = ['get_order_history', 'get_partner_profile', 'get_available_to_promise', 'request_order_decision']


def build_triage_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(TRIAGE_AGENT_TOOLS)
    return Agent(name='order-exception-triage', perceiver=FlaggedOrderPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=4), verifier=TriageVerifier(),
                 **_common('order-exception-triage', llm))


def triage_agent_principal() -> Principal:
    """Service identity for order-exception-triage. In real runs these scopes come from a verified Keycloak token."""
    return Principal('order-exception-triage', frozenset(['orders.read', 'orders.triage']))


ONBOARDING_COPILOT_TOOLS = ['parse_x12', 'draft_mapping', 'run_mapping_tests']


def build_onboarding_copilot(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(ONBOARDING_COPILOT_TOOLS)
    return Agent(name='partner-onboarding-copilot', perceiver=PartnerGuidePerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=6), verifier=MappingVerifier(),
                 **_common('partner-onboarding-copilot', llm))


def onboarding_copilot_principal() -> Principal:
    """Service identity for partner-onboarding-copilot. In real runs these scopes come from a verified Keycloak token."""
    return Principal('partner-onboarding-copilot', frozenset(['orders.mapping', 'orders.read']))


BUILDERS = {'order-exception-triage': build_triage_agent, 'partner-onboarding-copilot': build_onboarding_copilot}
PRINCIPALS = {'order-exception-triage': triage_agent_principal, 'partner-onboarding-copilot': onboarding_copilot_principal}
