"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import ShipmentExceptionPerceiver, FreightInvoicePerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import ExceptionDispositionVerifier, FreightAuditVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


EXCEPTION_AGENT_TOOLS = ['track_shipment', 'get_proof_of_delivery', 'get_shipment_order', 'reroute_or_reschedule', 'notify_customer', 'open_freight_claim']


def build_exception_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(EXCEPTION_AGENT_TOOLS)
    return Agent(name='shipment-exception-agent', perceiver=ShipmentExceptionPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5), verifier=ExceptionDispositionVerifier(),
                 **_common('shipment-exception-agent', llm))


def exception_agent_principal() -> Principal:
    """Service identity for shipment-exception-agent. In real runs these scopes come from a verified Keycloak token."""
    return Principal('shipment-exception-agent', frozenset(['freight.claim', 'shipping.act', 'shipping.notify', 'shipping.read']))


FREIGHT_AUDIT_AGENT_TOOLS = ['get_contract_rate', 'dispute_freight_invoice']


def build_freight_audit_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(FREIGHT_AUDIT_AGENT_TOOLS)
    return Agent(name='freight-audit-agent', perceiver=FreightInvoicePerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=4), verifier=FreightAuditVerifier(),
                 **_common('freight-audit-agent', llm))


def freight_audit_agent_principal() -> Principal:
    """Service identity for freight-audit-agent. In real runs these scopes come from a verified Keycloak token."""
    return Principal('freight-audit-agent', frozenset(['freight.audit', 'freight.read']))


BUILDERS = {'shipment-exception-agent': build_exception_agent, 'freight-audit-agent': build_freight_audit_agent}
PRINCIPALS = {'shipment-exception-agent': exception_agent_principal, 'freight-audit-agent': freight_audit_agent_principal}
