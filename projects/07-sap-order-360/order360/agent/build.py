"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import Order360QueryPerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import CorrelatedAnswerVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


ORDER360_ASSISTANT_TOOLS = ['get_order_360', 'get_credit_exposure']


def build_order360_assistant(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(ORDER360_ASSISTANT_TOOLS)
    return Agent(name='order360-assistant', perceiver=Order360QueryPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=3), verifier=CorrelatedAnswerVerifier(),
                 **_common('order360-assistant', llm))


def order360_assistant_principal() -> Principal:
    """Service identity for order360-assistant. In real runs these scopes come from a verified Keycloak token."""
    return Principal('order360-assistant', frozenset(['credit.read', 'erp.read']))


BUILDERS = {'order360-assistant': build_order360_assistant}
PRINCIPALS = {'order360-assistant': order360_assistant_principal}
