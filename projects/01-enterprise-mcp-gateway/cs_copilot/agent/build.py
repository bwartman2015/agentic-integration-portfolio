"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary

from .guardrails import get_guardrails
from .knowledge import get_retriever, known_citations
from .memory import get_memory
from .observe import get_tracer
from .perceive import CaseRequestPerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import EvidenceBackedAnswerVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


CS_COPILOT_TOOLS = ['search_accounts', 'get_sales_order', 'track_shipment', 'get_open_invoices', 'create_case']


def build_cs_copilot(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(CS_COPILOT_TOOLS)
    return Agent(name='cs-copilot', perceiver=CaseRequestPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5),
                 verifier=EvidenceBackedAnswerVerifier(known_citations()),
                 **_common('cs-copilot', llm))


def cs_copilot_principal() -> Principal:
    """Service identity for cs-copilot. In real runs these scopes come from a verified Keycloak token."""
    return Principal('cs-copilot', frozenset(['ar.read', 'crm.read', 'crm.write', 'erp.read', 'shipping.read']))


# Human roles the copilot acts for. Milestone 3 replaces these with Keycloak clients (cs-rep, cs-lead).
ROLE_SCOPES = {
    'cs-rep': frozenset(['ar.read', 'crm.read', 'erp.read', 'shipping.read']),
    'cs-lead': frozenset(['ar.read', 'crm.read', 'crm.write', 'erp.read', 'shipping.read']),
}


def principal_for_role(role: str) -> Principal:
    return Principal(role, ROLE_SCOPES[role])


BUILDERS = {'cs-copilot': build_cs_copilot}
PRINCIPALS = {'cs-copilot': cs_copilot_principal}
