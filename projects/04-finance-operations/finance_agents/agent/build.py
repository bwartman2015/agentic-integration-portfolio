"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import FinanceRequestPerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import APResolutionVerifier, CashApplicationVerifier, ManagerVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


AP_AGENT_TOOLS = ['get_purchase_order', 'get_goods_receipts', 'get_supplier_invoice', 'three_way_match', 'check_duplicate_invoice', 'propose_ap_resolution']


def build_ap_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(AP_AGENT_TOOLS)
    return Agent(name='ap-exception-agent', perceiver=FinanceRequestPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5), verifier=APResolutionVerifier(),
                 **_common('ap-exception-agent', llm))


def ap_agent_principal() -> Principal:
    """Service identity for ap-exception-agent. In real runs these scopes come from a verified Keycloak token."""
    return Principal('ap-exception-agent', frozenset(['ap.propose', 'ap.read']))


AR_AGENT_TOOLS = ['get_remittance', 'get_open_items', 'match_remittance', 'propose_cash_application']


def build_ar_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(AR_AGENT_TOOLS)
    return Agent(name='ar-cash-application-agent', perceiver=FinanceRequestPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5), verifier=CashApplicationVerifier(),
                 **_common('ar-cash-application-agent', llm))


def ar_agent_principal() -> Principal:
    """Service identity for ar-cash-application-agent. In real runs these scopes come from a verified Keycloak token."""
    return Principal('ar-cash-application-agent', frozenset(['ar.propose', 'ar.read']))


FINANCE_MANAGER_TOOLS = []


def build_finance_manager(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(FINANCE_MANAGER_TOOLS)
    tools.register(agent_as_tool(build_ap_agent(llm), name='ask_ap_exception_agent',
                                 description='Delegate to the ap-exception-agent specialist',
                                 principal=ap_agent_principal(), scope='delegate.ap-exception-agent'))
    tools.register(agent_as_tool(build_ar_agent(llm), name='ask_ar_cash_application_agent',
                                 description='Delegate to the ar-cash-application-agent specialist',
                                 principal=ar_agent_principal(), scope='delegate.ar-cash-application-agent'))
    return Agent(name='finance-ops-manager', perceiver=FinanceRequestPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=6), verifier=ManagerVerifier(),
                 **_common('finance-ops-manager', llm))


def finance_manager_principal() -> Principal:
    """Service identity for finance-ops-manager. In real runs these scopes come from a verified Keycloak token."""
    return Principal('finance-ops-manager', frozenset(['delegate.ap-exception-agent', 'delegate.ar-cash-application-agent']))


BUILDERS = {'ap-exception-agent': build_ap_agent, 'ar-cash-application-agent': build_ar_agent, 'finance-ops-manager': build_finance_manager}
PRINCIPALS = {'ap-exception-agent': ap_agent_principal, 'ar-cash-application-agent': ar_agent_principal, 'finance-ops-manager': finance_manager_principal}
