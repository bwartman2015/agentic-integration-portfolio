"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input.

CaseRequestPerceiver accepts a rep's free-text question or an inbound case payload
({"subject", "description", "account"?}), validates and normalizes it, and extracts entities.
It makes no system calls: account aliases come from a static table of Ridgeline's synthetic
customers; aliases learned from searches (Milestone 6, build.learn_aliases) reach the reasoner
through long-term memory recall instead, so this stays free of I/O. Oversized input is not
truncated here; the input guardrail rejects it so the rejection is explicit and traced.
"""
from __future__ import annotations

import re
from typing import Any

from agentcore import Percept, Principal

ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "sales_order": re.compile(r"\bSO-1\d{5}\b", re.I),
    "po_number": re.compile(r"\bPO-7\d{3}\b", re.I),
    "invoice": re.compile(r"\bINV-9\d{4}\b", re.I),
    "shipment": re.compile(r"\bSHP-\d{5}\b", re.I),
}

# canonical account name -> lower-case surface forms that refer to it (synthetic customers only).
# Static on purpose: the perceiver makes no system calls. Generated background accounts are not listed,
# so the reasoner finds them with search_accounts (and recall then offers the learned alias).
ACCOUNT_ALIASES: dict[str, tuple[str, ...]] = {
    "Big Box Outdoor": ("big box outdoor", "bbo"),
    "Summit Contractors LLC": ("summit contractors llc", "summit contractors"),
    "Pinecrest Hardware": ("pinecrest hardware", "pinecrest"),
}


def mentions_account(text: str, canonical: str) -> bool:
    """True when `text` names the account by its canonical name or a known alias (whole words)."""
    forms = ACCOUNT_ALIASES.get(canonical, (canonical.lower(),))
    return any(re.search(rf"\b{re.escape(f)}\b", text, re.I) for f in forms)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class CaseRequestPerceiver:
    """
    Normalize a rep's question (or an inbound case description) into a Percept; attach the rep's
    verified principal from the JWT; extract entities {account, sales_order, po_number, invoice, shipment}.
    Entity values are lists (a request can name more than one order).

    Percept.kind = 'cs_request'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        declared_account = None
        if isinstance(raw, str):
            text, source = raw, "text"
        elif isinstance(raw, dict):
            subject = str(raw.get("subject") or "").strip()
            description = str(raw.get("description") or "").strip()
            text = f"{subject}: {description}" if subject and description else subject or description
            source, declared_account = "case", raw.get("account")
        else:
            raise ValueError(f"CaseRequestPerceiver expects text or a case dict, got {type(raw).__name__}")
        content = " ".join(text.split())
        if not content:
            raise ValueError("empty customer service request")

        entities: dict[str, list[str]] = {}
        for kind, rx in ID_PATTERNS.items():
            ids = _unique([m.upper() for m in rx.findall(content)])
            if ids:
                entities[kind] = ids
        accounts = [name for name in ACCOUNT_ALIASES if mentions_account(content, name)]
        if declared_account:
            declared = " ".join(str(declared_account).split())
            known = [name for name in ACCOUNT_ALIASES if mentions_account(declared, name)]
            accounts = _unique((known or [declared]) + accounts)
        if accounts:
            entities["account"] = accounts
        return Percept(kind="cs_request", content=content, principal=principal, entities=entities,
                       source=source, raw=raw)
