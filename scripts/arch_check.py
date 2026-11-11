#!/usr/bin/env python3
"""Static architecture conformance check for every project in projects/.

Rules (each violation is printed; exit code 1 if any):
  R1  <pkg>/agent/ contains one module per layer: perceive, reason, memory, plan, tools,
      guardrails, observe, verify, build.
  R2  Only reason.py may construct LLM providers or import LLM SDKs (THINK owns the LLM).
  R3  Only agent/tools.py defines tool handlers, and every function there is registered in TOOLS
      with a scope (ACT is governed).
  R4  build.py constructs agentcore.Agent passing every layer explicitly.
  R5  Code outside agent/ must not call a tool handler directly (it must go through the agent loop).
  R6  docs/agent-architecture.md exists and contains a Mermaid diagram.
  R7  skills/*/SKILL.md files parse (name, description, trigger) and at least one exists.
  R8  knowledge/ has at least one document (RAG) and evals/cases.json has >= 3 valid cases (Evals).
  R9  a2a/*.card.json Agent Cards have name, description and skills (A2A).
Usage: python3 scripts/arch_check.py [project-dir ...]
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-core"))
from agentcore.skills import parse_skill  # noqa: E402
LAYER_MODULES = ["perceive", "state", "reason", "memory", "knowledge", "plan", "tools", "guardrails", "observe", "verify", "build"]
LLM_SDKS = {"anthropic", "openai", "ollama", "litellm", "langchain_openai", "langchain_anthropic"}
LLM_PROVIDERS = {"AnthropicLLM", "OllamaLLM", "HeadlessCodingAgentLLM"}
AGENT_KWARGS = {"perceiver", "tools", "guardrails", "verifier"}            # passed explicitly
COMMON_KWARGS = {"reasoner", "memory", "planner", "state", "retriever", "skills", "checkpointer", "tracer"}  # via _common()


def find_pkg(project: pathlib.Path) -> pathlib.Path | None:
    hits = [p.parent.parent for p in project.glob("*/agent/build.py")]
    return hits[0] if hits else None


def check(project: pathlib.Path) -> list[str]:
    errs: list[str] = []
    pkg = find_pkg(project)
    if pkg is None:
        return [f"R1 no <pkg>/agent/build.py found in {project.name}"]
    agent_dir = pkg / "agent"
    for m in LAYER_MODULES:
        if not (agent_dir / f"{m}.py").exists():
            errs.append(f"R1 missing layer module {agent_dir.relative_to(ROOT)}/{m}.py")

    handler_names: set[str] = set()
    tools_py = agent_dir / "tools.py"
    if tools_py.exists():
        tree = ast.parse(tools_py.read_text())
        defs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)} - {"build_registry"}
        registered, unscoped = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Tool":
                kw = {k.arg: k.value for k in node.keywords}
                h = kw.get("handler")
                if isinstance(h, ast.Name):
                    registered.add(h.id)
                scope = kw.get("scope")
                if scope is None or (isinstance(scope, ast.Constant) and not scope.value):
                    unscoped.append(ast.unparse(node.args[0]) if node.args else "?")
        for d in sorted(defs - registered):
            if not d.startswith("_"):
                errs.append(f"R3 tools.py function {d!r} is not registered as a Tool")
        for u in unscoped:
            errs.append(f"R3 tool {u} has no scope")
        handler_names = registered

    build_py = agent_dir / "build.py"
    if build_py.exists():
        calls = [n for n in ast.walk(ast.parse(build_py.read_text()))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Agent"]
        if not calls:
            errs.append("R4 build.py never constructs Agent(...)")
        src = build_py.read_text()
        for c in calls:
            given = {k.arg for k in c.keywords}
            missing = AGENT_KWARGS - given
            if None in given:  # **_common(...) supplies the shared layers; verify _common sets them all
                missing |= {k for k in COMMON_KWARGS if f"{k}=" not in src}
            else:
                missing |= COMMON_KWARGS - given
            if missing:
                errs.append(f"R4 Agent(...) in build.py line {c.lineno} missing layers/concepts: {sorted(missing)}")

    for py in project.rglob("*.py"):
        rel = py.relative_to(project)
        if "tests" in rel.parts or ".venv" in rel.parts:
            continue
        tree = ast.parse(py.read_text())
        in_agent = py.parent == agent_dir
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                names = [a.name for a in node.names]
                if py.name != "reason.py":
                    for m in mods:
                        if m.split(".")[0] in LLM_SDKS:
                            errs.append(f"R2 {rel}:{node.lineno} imports LLM SDK {m!r} outside reason.py")
                    for n in names:
                        if n in LLM_PROVIDERS:
                            errs.append(f"R2 {rel}:{node.lineno} imports LLM provider {n!r} outside reason.py")
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if py.name != "reason.py" and fname in LLM_PROVIDERS:
                    errs.append(f"R2 {rel}:{node.lineno} constructs {fname} outside reason.py")
                if not in_agent and fname in handler_names:
                    errs.append(f"R5 {rel}:{node.lineno} calls tool handler {fname}() directly; go through the agent")

    sk = project / "skills"
    skill_files = sorted(sk.glob("*/SKILL.md")) if sk.exists() else []
    if not skill_files:
        errs.append("R7 no runtime skills in skills/*/SKILL.md")
    for f in skill_files:
        try:
            s = parse_skill(f.read_text(), str(f))
            if not s.trigger:
                errs.append(f"R7 {f.relative_to(ROOT)} has no trigger")
        except ValueError as e:
            errs.append(f"R7 {e}")
    if not list((project / "knowledge").glob("*.md")):
        errs.append("R8 no RAG documents in knowledge/")
    try:
        cases = json.loads((project / "evals" / "cases.json").read_text())
        if len(cases) < 3 or not all(c.get("id") and c.get("input") for c in cases):
            errs.append("R8 evals/cases.json needs >= 3 cases with id and input")
    except (OSError, json.JSONDecodeError) as e:
        errs.append(f"R8 evals/cases.json unreadable: {e}")
    for f in sorted((project / "a2a").glob("*.card.json")):
        try:
            card = json.loads(f.read_text())
            if not (card.get("name") and card.get("description") and card.get("skills")):
                errs.append(f"R9 {f.name} needs name, description and skills")
        except json.JSONDecodeError as e:
            errs.append(f"R9 {f.name}: {e}")

    doc = project / "docs" / "agent-architecture.md"
    if not doc.exists() or "```mermaid" not in doc.read_text():
        errs.append("R6 docs/agent-architecture.md missing or has no Mermaid diagram")
    return errs


def main() -> int:
    targets = [pathlib.Path(a).resolve() for a in sys.argv[1:]] or sorted((ROOT / "projects").glob("*/"))
    failed = False
    for t in targets:
        errs = check(t)
        status = "PASS" if not errs else f"FAIL ({len(errs)})"
        print(f"[{status}] {t.name}")
        for e in errs:
            print(f"    - {e}")
        failed |= bool(errs)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
