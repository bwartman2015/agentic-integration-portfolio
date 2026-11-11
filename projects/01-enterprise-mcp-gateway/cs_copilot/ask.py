"""Ask the CS copilot a question, end to end: `make ask P=01 Q="..." [ROLE=cs-lead]` (from the repo root).

Runs in the copilot container. Tools and state go through the MCP gateway with the role's Keycloak token
(so every call is authorized by OPA and audited with this run's trace id), and long-term memory is
Postgres + pgvector. Printed: the answer with its citations, what memory recalled, the gateway calls, and
the trace id to look the calls up with `make audit P=01 ARGS=--trace=<id>`.

The model: until Milestone 7 picks a local chat model, LLM_PROVIDER=scripted replays the scripted plan
for a known question (the demo question and the eval cases' questions; `--list` shows them). Everything
else in the run is real: the gateway, its policy and approvals, the systems behind it, memory.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("TRACE_FILE", "/tmp/traces.jsonl")
os.environ.setdefault("PROGRESS_DIR", "/tmp/progress")

from .agent.build import build_cs_copilot, principal_for_role, ROLE_SCOPES  # noqa: E402
from .agent.mcp_client import TokenError  # noqa: E402
from .agent.reason import get_llm  # noqa: E402

DEMO_QUESTION = "Summit Contractors says SO-104233 hasn't arrived and they were billed. What's going on?"
# A write: the gateway asks a human in n8n (`make approvals P=01` lists the form link) and waits.
CASE_REQUEST = "Open a case for Summit Contractors about the late delivery of SO-104233 and confirm the new delivery window."
CITATION = re.compile(r"\[([^\[\]\s]+\.md#\d+)\]")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 -]", " ", text.lower()).split())


def known_questions() -> dict[str, str]:
    """question -> scripted plan (fixtures/llm/<name>.json): the demo question and every eval case."""
    questions = {DEMO_QUESTION: "demo-late-and-billed", CASE_REQUEST: "ask-lead-create-case"}
    for case in json.loads((HERE / "evals" / "cases.json").read_text()):
        if isinstance(case.get("input"), str):
            questions.setdefault(case["input"], case["id"])
    return questions


def script_for(question: str) -> str | None:
    wanted = _norm(question)
    return next((script for q, script in known_questions().items() if _norm(q) == wanted), None)


def gateway_calls(trace_file: str, trace_id: str) -> list[dict[str, Any]]:
    try:
        lines = pathlib.Path(trace_file).read_text().splitlines()
    except OSError:
        return []
    spans = [json.loads(line) for line in lines if line.strip()]
    return [s for s in sorted(spans, key=lambda s: s["start"]) if s.get("trace_id") == trace_id
            and s.get("name", "").startswith("act.")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the Ridgeline CS copilot (through the MCP gateway)")
    parser.add_argument("question", nargs="?", default="")
    parser.add_argument("--role", default="cs-rep", choices=sorted(ROLE_SCOPES))
    parser.add_argument("--list", action="store_true", help="show the questions the scripted model knows")
    args = parser.parse_args(argv)
    scripted = os.environ.get("LLM_PROVIDER", "scripted") == "scripted"
    if args.list or not args.question.strip():
        print("Questions the scripted model knows (Milestone 7 adds a real model):")
        for q in known_questions():
            print(f"  {q}")
        return 0 if args.list else 2

    script = script_for(args.question) if scripted else None
    if scripted and script is None:
        print("The scripted model has no plan for that question (a real model comes in Milestone 7).\n"
              "Run `make ask P=01 Q=--list` to see the questions it knows.", file=sys.stderr)
        return 2
    try:
        agent = build_cs_copilot(get_llm(script=script), role=args.role)
    except TokenError as exc:
        print(f"could not get a token: {exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"could not reach the gateway: {type(exc).__name__}: {exc}"[:400], file=sys.stderr)
        return 1

    out = agent.run(args.question, principal_for_role(args.role))
    memory = agent.memory
    print(f"role={args.role}  status={out.status}  iterations={out.iterations}  tool_calls={out.tool_calls}"
          f"  tools={','.join(out.tools_used) or '-'}")
    print(f"memory: {type(memory).__name__} ({getattr(getattr(memory, 'embedder', None), 'model', 'keyword')})")
    recalled = getattr(memory, "last_recall", None) or []
    if recalled:
        for item in recalled:
            print(f"  recalled {item.kind} (similarity {item.score:.2f}, from trace "
                  f"{str(item.meta.get('trace_id', '?'))[:12]}): {item.text.splitlines()[0][:110]}")
    else:
        print("  recalled nothing (first time this audience asks something like this)")
    if getattr(memory, "last_error", None):
        print(f"  memory problem: {memory.last_error} (the answer is unaffected)")
    print("\nANSWER\n" + out.answer)
    cites = sorted(set(CITATION.findall(out.answer)))
    if cites:
        print("\nCITATIONS  " + "  ".join(cites))
    calls = gateway_calls(os.environ["TRACE_FILE"], out.trace_id)
    if calls:
        print("\nTOOL CALLS through the gateway (each authorized by OPA and audited; state reads are too)")
        for s in calls:
            a = s.get("attrs", {})
            print(f"  {s['name'].removeprefix('act.'):<18} ok={a.get('ok')}  {s.get('latency_ms', 0):>7.1f} ms"
                  + (f"  {str(a.get('error'))[:90]}" if a.get("error") else ""))
    print(f"\ntrace id {out.trace_id}\n  its gateway audit rows: make audit P=01 ARGS=--trace={out.trace_id}")
    return 0 if out.status == "answered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
