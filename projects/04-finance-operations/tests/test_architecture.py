"""Architecture + concept conformance. Passes on the skeleton and must keep passing as the project is built."""
import json
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("MEMORY_SQLITE", ":memory:")
os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from agentcore import Agent, FakeLLM, Principal, SkillLibrary, Tool, load_cases
from finance_agents.agent import build
from finance_agents.agent.guardrails import get_guardrails
from finance_agents.agent.tools import TOOLS

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_agents_assemble_with_every_layer_and_concept(self):
        for name, builder in build.BUILDERS.items():
            agent = builder(FakeLLM([]))
            self.assertIsInstance(agent, Agent)
            for attr in ("perceiver", "reasoner", "memory", "tools", "guardrails", "verifier", "tracer",
                         "state", "retriever", "skills", "checkpointer"):
                self.assertIsNotNone(getattr(agent, attr), f"{name} missing {attr}")
            self.assertTrue(agent.tools.catalog(build.PRINCIPALS[name]()), f"{name}: principal sees no tools")

    def test_every_tool_is_governed(self):
        for tool in TOOLS:
            self.assertIsInstance(tool, Tool)
            self.assertTrue(tool.scope, f"{tool.name} has no scope")

    def test_writes_fail_closed_by_default(self):
        g = get_guardrails(3)
        for t in [t for t in TOOLS if t.side_effect == "write"]:
            with self.assertRaises(Exception):
                g.authorize(Principal("x", frozenset({t.scope})), t, {})

    def test_skills_knowledge_evals_present(self):
        self.assertTrue(SkillLibrary.from_dir(ROOT / "skills").skills, "no runtime skills")
        self.assertTrue(list((ROOT / "knowledge").glob("*.md")), "no RAG knowledge docs")
        cases = load_cases(ROOT / "evals" / "cases.json")
        self.assertGreaterEqual(len(cases), 3)
        for c in cases:
            for tag in c.tags:
                self.assertIn(tag, build.BUILDERS, f"eval {c.id} targets unknown agent {tag}")

    def test_agent_cards_valid(self):
        for f in (ROOT / "a2a").glob("*.card.json"):
            card = json.loads(f.read_text())
            self.assertTrue(card["name"] and card["description"] and card["skills"], f.name)


if __name__ == "__main__":
    unittest.main()
