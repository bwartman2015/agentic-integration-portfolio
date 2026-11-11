"""Milestone 0 demo: the cs-copilot runs end to end offline with a scripted LLM.

Run 1  A rep asks the main demo question; the copilot plans, calls four tools, verifies its answer
       against the evidence, cites policy, masks the contact email, writes memory, a progress file
       and a JSONL trace. The trace is read back and printed span by span, grouped by layer.
Run 2  A cs-rep (no crm.write) asks to open a case: denied by the permission guardrail.
Run 3  A cs-lead (crm.write) asks the same: denied by the approval gate. Offline there is no approver,
       so writes fail closed; in the copilot container (`make ask`) create_case goes to the gateway, which
       asks a human in n8n.
Usage: make demo P=01   (from the repo root)
"""
from __future__ import annotations

import json
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("LLM_PROVIDER", "scripted")
os.environ.setdefault("TRACE_FILE", str(HERE / "traces.jsonl"))
os.environ.setdefault("PROGRESS_DIR", str(HERE / "progress"))
os.environ.setdefault("MEMORY_SQLITE", ":memory:")

from cs_copilot.agent.build import build_cs_copilot, principal_for_role  # noqa: E402
from cs_copilot.agent.reason import get_llm  # noqa: E402
from mock_systems import get_systems, reset_systems  # noqa: E402

QUESTION = "Summit Contractors says SO-104233 hasn't arrived and they were billed. What's going on?"
CASE_REQUEST = "Open a case for Summit Contractors about the late delivery of SO-104233."
SHOWN_ATTRS = ("kind", "hits", "cites", "active", "decision", "steps", "tool", "ok", "error", "achieved", "reason",
               "status", "keys", "iteration", "input_tokens", "output_tokens", "cost_usd")   # most telling first


def _short(value: object, width: int = 90) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= width else text[: width - 3] + "..."


def trace_spans(trace_id: str) -> list[dict]:
    lines = pathlib.Path(os.environ["TRACE_FILE"]).read_text().splitlines()
    spans = [json.loads(line) for line in lines]
    return sorted((s for s in spans if s["trace_id"] == trace_id), key=lambda s: s["start"])


def denial_line(history: list[dict]) -> str:
    for m in history:
        if m["content"].startswith("Result of create_case"):
            return m["content"]
    return "(create_case was not attempted)"


def main() -> None:
    reset_systems()
    print("=" * 100)
    print("RUN 1  cs-rep asks:", QUESTION)
    print("=" * 100)
    agent = build_cs_copilot(get_llm(script="demo-late-and-billed"))
    out = agent.run(QUESTION, principal_for_role("cs-rep"))
    print(f"status={out.status}  iterations={out.iterations}  tool_calls={out.tool_calls}  "
          f"tools={out.tools_used}  cost=${out.cost_usd:.4f}")
    print("\nANSWER\n" + out.answer)
    print(f"\nTRACE {out.trace_id}  (from {os.environ['TRACE_FILE']})")
    for s in trace_spans(out.trace_id):
        attrs = {k: s["attrs"][k] for k in SHOWN_ATTRS if s["attrs"].get(k) not in (None, "", [])}
        print(f"  {s['layer']:<10} {s['name']:<24} {s['latency_ms']:>7.1f} ms  {_short(attrs)}")
    progress = pathlib.Path(os.environ["PROGRESS_DIR"]) / f"{out.trace_id}.json"
    print(f"\nPROGRESS FILE {progress} (status={json.loads(progress.read_text())['status']})")

    for n, (role, script) in enumerate([("cs-rep", "demo-rep-create-case"), ("cs-lead", "demo-lead-create-case")], start=2):
        print("\n" + "=" * 100)
        print(f"RUN {n}  {role} asks:", CASE_REQUEST)
        print("=" * 100)
        out = build_cs_copilot(get_llm(script=script)).run(CASE_REQUEST, principal_for_role(role))
        print("guardrail:", _short(denial_line(out.history), 120))
        print(f"status={out.status}  answer: {out.answer}")
    print(f"\nCRM cases created: {len(get_systems().crm.cases)} (offline demo, no approver: writes fail closed; `make ask` uses the gateway's human approval)")


if __name__ == "__main__":
    main()
