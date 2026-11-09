"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import DisputePerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import ResolutionVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


COMMAND_CENTER_TOOLS = ['get_case', 'record_resolution']


def build_command_center(llm: LLMProvider | None = None, specialists: list[Tool] | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(COMMAND_CENTER_TOOLS)
    if specialists is None and os.environ.get("A2A_DISCOVER") == "1":
        from .mesh import discover_specialists
        specialists = discover_specialists()
    for tool in specialists or []:
        tools.register(tool)
    return Agent(name='operations-command-center', perceiver=DisputePerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=6), verifier=ResolutionVerifier(),
                 **_common('operations-command-center', llm))


def command_center_principal() -> Principal:
    """Service identity for operations-command-center. In real runs these scopes come from a verified Keycloak token."""
    return Principal('operations-command-center', frozenset(['cases.read', 'cases.write', 'delegate.mesh']))


BUILDERS = {'operations-command-center': build_command_center}
PRINCIPALS = {'operations-command-center': command_center_principal}
