"""Assembly + orchestration: wires every layer and concept into agentcore.Agent. No business logic here.

Tool backends (Milestone 6): with GATEWAY_URL set (the copilot container) the tools are the gateway's MCP
tools, registered through agentcore.register_tool_source with scopes and side effects from the copilot's
own policy data, never from the server; STATE reads through the same source. Without it, the in-process
tools over the mock systems (offline demo, tests, scripted evals).
"""
from __future__ import annotations

import os
import pathlib
import re
from typing import Any, Callable

from agentcore import (Agent, Checkpointer, LLMProvider, MemoryItem, Outcome, Percept, Principal, SkillLibrary,
                       Tool, ToolRegistry, ToolResult, register_tool_source)

from .guardrails import TOOL_SCOPES, WRITE_TOOLS, get_guardrails, mask_pii
from .knowledge import get_retriever, known_citations
from .mcp_client import KeycloakTokens, McpToolSource, gateway_url
from .memory import get_memory
from .observe import get_tracer
from .perceive import CaseRequestPerceiver, mentions_account
from .plan import get_planner
from .reason import get_llm, get_reasoner
from .state import get_state
from .tools import TOOLS, build_registry
from .verify import EvidenceBackedAnswerVerifier

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"


def _common(name: str, llm: LLMProvider, source: Any = None) -> dict:
    return dict(reasoner=get_reasoner(name, llm), memory=get_memory(), planner=get_planner(name, llm),
                state=get_state(source.call_tool if source else None), retriever=get_retriever(),
                skills=SkillLibrary.from_dir(SKILLS_DIR),
                checkpointer=Checkpointer(os.environ.get("PROGRESS_DIR", "progress"), redact=mask_pii),
                tracer=get_tracer())


def memory_audience(principal: Principal) -> str:
    """Long-term memory is partitioned by what a caller may see: an answered question is recalled only
    for callers with the same scopes and regions, so a lead's answer about a West account never reaches a
    Central rep's prompt (and a rep's never reaches a caller without the scopes it was built from)."""
    return (f"scopes={','.join(sorted(principal.scopes))};"
            f"regions={','.join(sorted(principal.attr('regions')))}")


CS_COPILOT_TOOLS = ['search_accounts', 'get_sales_order', 'track_shipment', 'get_open_invoices', 'create_case']


class _OnlyTools:
    """A tool source limited to the tools this agent is built for, whatever else the server offers."""

    def __init__(self, source: Any, names: list[str]):
        self.source, self.names = source, set(names)

    def list_tools(self) -> list[dict[str, Any]]:
        return [t for t in self.source.list_tools() if t.get("name") in self.names]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.source.call_tool(name, arguments)


def gateway_registry(source: Any, names: list[str]) -> ToolRegistry:
    """The gateway's tools as governed agentcore Tools. The gateway lists only the tools the caller's token
    may use (a rep sees no create_case), so the registry holds that subset. A tool the copilot's policy
    data does not know is not registered (scope None would mean "no scope needed")."""
    reg = ToolRegistry()
    register_tool_source(reg, _OnlyTools(source, [n for n in names if n in TOOL_SCOPES]),
                         scope_for=TOOL_SCOPES.get,
                         side_effect_for=lambda n: "write" if n in WRITE_TOOLS else "read")
    if not any(reg.get(n) for n in names):
        raise RuntimeError("the gateway lists none of the copilot's tools for this caller")
    # A tool the gateway did not list for this caller keeps its local declaration (name, arguments, scope),
    # so the copilot's guardrails explain the refusal ("lacks scope crm.write") exactly as offline. Its
    # handler only refuses: nothing reaches the gateway.
    for local in TOOLS:
        if local.name in names and local.name in TOOL_SCOPES and reg.get(local.name) is None:
            reg.register(Tool(local.name, local.description, handler=_not_offered(local.name), params=local.params,
                              required=local.required, scope=TOOL_SCOPES[local.name],
                              side_effect="write" if local.name in WRITE_TOOLS else "read", category="mcp"))
    return reg


def _not_offered(name: str) -> Callable[..., Any]:
    def refuse(**_: Any) -> Any:
        raise PermissionError(f"the gateway does not offer {name} to this caller")
    return refuse


_ALIAS_WORDS = re.compile(r"^[a-z0-9][a-z0-9 &'.-]{1,58}[a-z0-9.]$")


def learn_aliases(memory_of: Callable[[], Any]) -> Callable[[Percept, Outcome, list[ToolResult]], None]:
    """`on_answer` hook (Remember): after an answered run, a search_accounts query that found exactly one
    account under a different name ("BBO" -> Big Box Outdoor) is kept as an alias memory, tagged with the
    caller's audience like every memory. Recall puts it in later prompts of callers who may see that
    account; the perceiver itself stays static (no system calls)."""
    def hook(percept: Percept, outcome: Outcome, results: list[ToolResult]) -> None:
        audience = memory_audience(percept.principal)
        for r in results:
            if r.tool != "search_accounts" or not r.ok or not isinstance(r.output, dict):
                continue
            query = str(r.output.get("query") or "").strip().lower()
            matches = r.output.get("matches") or []
            if len(matches) != 1 or not _ALIAS_WORDS.match(query):
                continue
            name, account_id = str(matches[0].get("name") or ""), str(matches[0].get("account_id") or "")
            if not name or not account_id or query == account_id.lower() or mentions_account(query, name):
                continue
            memory_of().remember(MemoryItem(text=f"Account alias: '{query}' means {name} ({account_id}).", kind="alias",
                                       meta={"alias": query, "account_id": account_id, "audience": audience,
                                             "trace_id": outcome.trace_id}))
    return hook


def build_cs_copilot(llm: LLMProvider | None = None, role: str | None = None,
                     source: Any = None) -> Agent:
    """Gateway backend when GATEWAY_URL is set (or a tool `source` is given): `role` (cs-rep | cs-lead) is
    whose Keycloak token the gateway calls carry. Otherwise the in-process tools."""
    llm = llm or get_llm()
    if source is None and gateway_url():
        source = McpToolSource(gateway_url(), KeycloakTokens.for_role(role or "cs-rep"))
    tools = gateway_registry(source, CS_COPILOT_TOOLS) if source is not None else build_registry(CS_COPILOT_TOOLS)
    common = _common('cs-copilot', llm, source)
    agent = Agent(name='cs-copilot', perceiver=CaseRequestPerceiver(), tools=tools,
                 guardrails=get_guardrails(max_iterations=5, gateway=source is not None),
                 memory_audience=memory_audience,
                 verifier=EvidenceBackedAnswerVerifier(known_citations()), **common)
    agent.on_answer = learn_aliases(lambda: agent.memory)
    return agent


def cs_copilot_principal() -> Principal:
    """Service identity for cs-copilot. In real runs these scopes come from a verified Keycloak token."""
    return Principal('cs-copilot', frozenset(['ar.read', 'crm.read', 'crm.write', 'erp.read', 'shipping.read']),
                     {'regions': ['*']})


# Human roles the copilot acts for, matching the Keycloak clients (keycloak/ridgeline-realm.json): scopes
# plus the regions claim the policy's data rule reads. cs-rep is a Central-region rep; a lead sees all.
ROLE_SCOPES = {
    'cs-rep': frozenset(['ar.read', 'crm.read', 'erp.read', 'shipping.read']),
    'cs-lead': frozenset(['ar.read', 'crm.read', 'crm.write', 'erp.read', 'shipping.read']),
}
ROLE_REGIONS = {'cs-rep': ('Central',), 'cs-lead': ('*',)}


def principal_for_role(role: str) -> Principal:
    return Principal(role, ROLE_SCOPES[role], {'regions': ROLE_REGIONS[role]})


BUILDERS = {'cs-copilot': build_cs_copilot}
PRINCIPALS = {'cs-copilot': cs_copilot_principal}
