"""agentcore: a small, dependency-free framework for the seven-layer agent architecture.

PERCEIVE  perceive.py    raw input -> typed Percept
THINK     reason.py      LLM providers + Reasoner (ReAct decision, "can I answer directly?")
REMEMBER  memory.py      short-term working memory + long-term episodic/pattern memory
PLAN      plan.py        goal -> ordered Steps
ACT       act.py         Tool registry + Executor (MCP, API, code, DB, file, external)
GUARDRAILS guardrails.py permissions, policy, approval gates, filters, rate limits, budgets
OBSERVE   observe.py     spans with tokens, cost, latency for every layer
loop.py wires them together with the "goal achieved?" loop and escalation (the harness).

Agentic AI concepts:
 1 Memory & State   memory.py, state.py        6 MCP               toolsource.py
 2 Orchestration    orchestrate.py              7 Skills            skills.py
 3 RAG              rag.py                      8 A2A               a2a.py
 4 Harness          loop.py, harness.py         9 Multi-agent       a2a.py + orchestrate.py
 5 Evals            evals.py
"""
from .act import Executor, Tool, ToolRegistry
from .guardrails import Guardrails, GuardrailViolation
from .loop import AcceptNonEmpty, Agent, GoalVerifier
from .memory import LongTermMemory, MemoryItem, ShortTermMemory, SQLiteMemory
from .observe import InMemorySink, JSONLSink, Span, Tracer, current_trace_id
from .perceive import Perceiver, TextPerceiver
from .plan import LLMPlanner, Planner
from .reason import AnthropicLLM, FakeLLM, LLMProvider, LLMResponse, OllamaLLM, Reasoner
from .types import Decision, Outcome, Percept, Plan, Principal, Step, ToolResult
from .state import CallableState, StateProvider, StaticState
from .rag import Chunk, KeywordRetriever, Retriever, chunk_text
from .skills import Skill, SkillLibrary, parse_skill
from .harness import Checkpointer, Compactor, LLMCompactor, TruncatingCompactor, build_context
from .toolsource import ToolSource, register_tool_source
from .orchestrate import MANAGER_PROMPT, agent_as_tool
from .a2a import A2AClient, AgentCard, AgentSkill, remote_agent_tool, serve_agent, task_text
from .evals import EvalCase, EvalReport, compare_to_baseline, load_cases, run_evals

__all__ = [n for n in dir() if not n.startswith("_")]
