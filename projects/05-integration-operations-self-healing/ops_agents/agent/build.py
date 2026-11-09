"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import IncidentPerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import DiagnosisVerifier, MetricsBaselineVerifier, CommanderVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


DIAGNOSER_TOOLS = ['get_traces', 'sample_dlq', 'get_metrics']


def build_diagnoser(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(DIAGNOSER_TOOLS)
    return Agent(name='diagnoser', perceiver=IncidentPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=4), verifier=DiagnosisVerifier(),
                 **_common('diagnoser', llm))


def diagnoser_principal() -> Principal:
    """Service identity for diagnoser. In real runs these scopes come from a verified Keycloak token."""
    return Principal('diagnoser', frozenset(['ops.read']))


REMEDIATOR_TOOLS = ['get_metrics', 'replay_dlq', 'pause_consumer', 'set_rate_limit', 'quarantine', 'open_fix_pr', 'notify_oncall']


def build_remediator(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(REMEDIATOR_TOOLS)
    return Agent(name='remediator', perceiver=IncidentPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=4), verifier=MetricsBaselineVerifier(),
                 **_common('remediator', llm))


def remediator_principal() -> Principal:
    """Service identity for remediator. In real runs these scopes come from a verified Keycloak token."""
    return Principal('remediator', frozenset(['ops.code', 'ops.notify', 'ops.read', 'ops.remediate']))


INCIDENT_COMMANDER_TOOLS = []


def build_incident_commander(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(INCIDENT_COMMANDER_TOOLS)
    tools.register(agent_as_tool(build_diagnoser(llm), name='ask_diagnoser',
                                 description='Delegate to the diagnoser specialist',
                                 principal=diagnoser_principal(), scope='delegate.diagnoser'))
    tools.register(agent_as_tool(build_remediator(llm), name='ask_remediator',
                                 description='Delegate to the remediator specialist',
                                 principal=remediator_principal(), scope='delegate.remediator'))
    return Agent(name='incident-commander', perceiver=IncidentPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5), verifier=CommanderVerifier(),
                 **_common('incident-commander', llm))


def incident_commander_principal() -> Principal:
    """Service identity for incident-commander. In real runs these scopes come from a verified Keycloak token."""
    return Principal('incident-commander', frozenset(['delegate.diagnoser', 'delegate.remediator']))


BUILDERS = {'diagnoser': build_diagnoser, 'remediator': build_remediator, 'incident-commander': build_incident_commander}
PRINCIPALS = {'diagnoser': diagnoser_principal, 'remediator': remediator_principal, 'incident-commander': incident_commander_principal}
