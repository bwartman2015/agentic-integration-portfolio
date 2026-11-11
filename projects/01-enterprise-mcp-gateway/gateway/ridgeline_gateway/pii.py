"""PII masking (Milestone 5). Contact emails and phone numbers leave the gateway only for callers whose
token holds the PII scope (`pii_scope` in policy/data.json, `crm.pii`); everyone else gets
"[EMAIL REDACTED]" / "[PHONE REDACTED]". The same mask is applied to argument values before they are
written to the audit table, whoever the caller is.

Only string values are rewritten, so a masked result keeps its shape and its numbers.
"""
from __future__ import annotations

import json
import re
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# North American phone numbers: 555-555-0142, 555.555.0142, (555) 555-0142, +1 555 555 0142, and the
# 7-digit 555-0142 (hyphen required, so "PRO 123.4567" or "120 1500" are left alone). The copilot's
# mask (agent-core Guardrails) uses the same pattern; tests/test_milestone0.py checks they agree.
PHONE = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?\d{3}[ .-]\d{4}|\d{3}[ .-]\d{3}[ .-]\d{4}|\d{3}-\d{4})(?![\w-])")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
MAX_AUDIT_VALUE_CHARS = 200


def mask_text(text: str) -> tuple[str, int]:
    """Returns the masked text and how many values were masked."""
    text, emails = EMAIL.subn("[EMAIL REDACTED]", text)
    text, ssns = SSN.subn("[SSN REDACTED]", text)
    text, phones = PHONE.subn("[PHONE REDACTED]", text)
    return text, emails + ssns + phones


def mask_value(value: Any) -> tuple[Any, int]:
    """Mask every string in a JSON-like value. Keys, numbers and structure are unchanged."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            out[key], n = mask_value(item)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        items, total = [], 0
        for item in value:
            masked, n = mask_value(item)
            items.append(masked)
            total += n
        return items, total
    return value, 0


def mask_json_text(text: str) -> tuple[str, int]:
    """Mask a text block that is normally the JSON form of a tool result. JSON is masked value by value
    (numbers are never touched); anything else is masked as plain text."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return mask_text(text)
    if not isinstance(parsed, (dict, list)):
        return mask_text(text)
    masked, n = mask_value(parsed)
    return (json.dumps(masked, indent=2), n) if n else (text, 0)


def audit_args(args: dict[str, Any]) -> dict[str, Any]:
    """Argument values as stored in the audit table: PII masked, long text cut, structure flattened to
    JSON-safe values. Record ids (SO-, ACCT-, ...) stay readable; that is what an auditor searches for."""
    def clean(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            masked, _ = mask_text(value)
            return masked if len(masked) <= MAX_AUDIT_VALUE_CHARS else masked[:MAX_AUDIT_VALUE_CHARS] + "..."
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if depth >= 3:
            return "<nested>"
        if isinstance(value, dict):
            return {str(k)[:64]: clean(v, depth + 1) for k, v in list(value.items())[:20]}
        if isinstance(value, (list, tuple)):
            return [clean(v, depth + 1) for v in list(value)[:20]]
        return f"<{type(value).__name__}>"
    return clean(args) if isinstance(args, dict) else {}
