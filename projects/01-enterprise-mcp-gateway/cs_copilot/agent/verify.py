"""Goal achieved? Prefer deterministic checks over asking the LLM.

EvidenceBackedAnswerVerifier accepts an answer only when:
  1. every entity in the request (account, order, PO, invoice, shipment) is addressed in the answer AND was
     looked up: a successful tool result mentions it, or a lookup came back "not found" for it. A policy
     citation never counts as evidence about a customer's order;
  2. no invented IDs: every ID-shaped token in the answer (SO-, PO-, INV-, SHP-, ACCT-, CASE- followed by
     digits, any length) appears in the request or in a successful tool result;
  3. no invented money: every dollar amount in the answer equals a number in a successful tool result or
     the request (deterministic code computes totals; the model only quotes them);
  4. every [source.md#n] citation is a real chunk of the knowledge base;
  5. requests without entities (policy questions) rest on a tool result or a valid citation.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from agentcore import Percept, ToolResult

from .perceive import mentions_account

ID_TOKEN = re.compile(r"\b(?:SO|PO|INV|SHP|ACCT|CASE)-\d+\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
CITATION = re.compile(r"\[([^\[\]\s]+\.md#\d+)\]")
NON_EVIDENCE_TOOLS = {"load_skill"}   # reading a skill is not evidence about the customer


def _ids(text: str) -> set[str]:
    return {t.upper() for t in ID_TOKEN.findall(text)}


def _amounts(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in AMOUNT.findall(text)]


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers(v)]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in _numbers(v)]
    return []


class EvidenceBackedAnswerVerifier:
    """
    Goal achieved when every entity in the request is addressed and each factual claim is backed by
    a successful tool result or a cited policy document.
    """

    def __init__(self, known_citations: Iterable[str]):
        self.known_citations = set(known_citations)

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        text = answer or ""
        if not text.strip():
            return False, "empty answer"
        evidence = [r for r in results if r.ok and r.tool not in NON_EVIDENCE_TOOLS]
        evidence_text = json.dumps([r.output for r in evidence], default=str)
        not_found_text = " ".join(r.error or "" for r in results if not r.ok and not r.denied)
        problems: list[str] = []

        for kind, values in percept.entities.items():
            for value in values:
                if kind == "account":
                    addressed = mentions_account(text, value)
                    looked_up = mentions_account(evidence_text, value) or mentions_account(not_found_text, value)
                else:
                    addressed = value.upper() in text.upper()
                    looked_up = value.upper() in evidence_text.upper() or value.upper() in not_found_text.upper()
                if not addressed:
                    problems.append(f"{kind} {value} not addressed")
                if not looked_up:
                    problems.append(f"{kind} {value} not backed by a tool result")

        invented = sorted(_ids(text) - _ids(percept.content) - _ids(evidence_text))
        if invented:
            problems.append(f"IDs not backed by the request or a tool result: {invented}")

        known_numbers = _numbers([r.output for r in evidence]) + _amounts(percept.content)
        bad_amounts = [a for a in _amounts(text) if not any(abs(a - n) < 0.005 for n in known_numbers)]
        if bad_amounts:
            problems.append(f"amounts not in any tool result: {bad_amounts}")

        cites = set(CITATION.findall(text))
        unknown = sorted(cites - self.known_citations)
        if unknown:
            problems.append(f"unknown citations {unknown}")
        if not percept.entities and not evidence and not (cites & self.known_citations):
            problems.append("no evidence: call a tool or cite a policy document")
        return (not problems, "; ".join(problems))
