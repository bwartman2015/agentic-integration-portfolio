"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here."""
import os
import pathlib

from agentcore import Agent, Checkpointer, LLMProvider, Principal, SkillLibrary, Tool, agent_as_tool

from .guardrails import get_guardrails
from .knowledge import get_retriever
from .memory import get_memory
from .observe import get_tracer
from .perceive import PRContextPerceiver
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import build_registry
from .verify import TestsPassVerifier, ReviewQualityVerifier, PROrchestratorVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(), retriever=get_retriever(), skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress")), tracer=get_tracer())


TESTGEN_AGENT_TOOLS = ['read_file', 'get_spec_diff', 'write_test_file', 'run_tests']


def build_testgen_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(TESTGEN_AGENT_TOOLS)
    return Agent(name='test-generation', perceiver=PRContextPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=3), verifier=TestsPassVerifier(),
                 **_common('test-generation', llm))


def testgen_agent_principal() -> Principal:
    """Service identity for test-generation. In real runs these scopes come from a verified Keycloak token."""
    return Principal('test-generation', frozenset(['repo.read', 'repo.tests']))


REVIEW_AGENT_TOOLS = ['read_file', 'get_spec_diff', 'post_review_comment']


def build_review_agent(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(REVIEW_AGENT_TOOLS)
    return Agent(name='integration-review', perceiver=PRContextPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=3), verifier=ReviewQualityVerifier(),
                 **_common('integration-review', llm))


def review_agent_principal() -> Principal:
    """Service identity for integration-review. In real runs these scopes come from a verified Keycloak token."""
    return Principal('integration-review', frozenset(['repo.read', 'repo.review']))


PR_ORCHESTRATOR_TOOLS = []


def build_pr_orchestrator(llm: LLMProvider | None = None) -> Agent:
    llm = llm or get_llm()
    tools = build_registry(PR_ORCHESTRATOR_TOOLS)
    tools.register(agent_as_tool(build_testgen_agent(llm), name='ask_test_generation',
                                 description='Delegate to the test-generation specialist',
                                 principal=testgen_agent_principal(), scope='delegate.test-generation'))
    tools.register(agent_as_tool(build_review_agent(llm), name='ask_integration_review',
                                 description='Delegate to the integration-review specialist',
                                 principal=review_agent_principal(), scope='delegate.integration-review'))
    return Agent(name='pr-orchestrator', perceiver=PRContextPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=4), verifier=PROrchestratorVerifier(),
                 **_common('pr-orchestrator', llm))


def pr_orchestrator_principal() -> Principal:
    """Service identity for pr-orchestrator. In real runs these scopes come from a verified Keycloak token."""
    return Principal('pr-orchestrator', frozenset(['delegate.integration-review', 'delegate.test-generation']))


BUILDERS = {'test-generation': build_testgen_agent, 'integration-review': build_review_agent, 'pr-orchestrator': build_pr_orchestrator}
PRINCIPALS = {'test-generation': testgen_agent_principal, 'integration-review': review_agent_principal, 'pr-orchestrator': pr_orchestrator_principal}
