"""Human approval for write tools (Milestone 5). Agents propose; a human approves.

Flow for one write (`create_case`):
  1. The gateway records the request in the audit store (no record, no approval: fail closed).
  2. It posts the request to an n8n workflow (N8N_APPROVAL_URL). n8n acknowledges with a signed,
     single-use link to an approval form and waits for a human.
  3. The human opens the form, chooses Approve or Reject and gives their name. n8n calls the gateway
     back at /approvals/callback with the request id and a one-time token that only this request's
     n8n execution was given.
  4. The tool call, which has been waiting, continues: Approve runs the write; anything else denies.

Fail closed: n8n unreachable or answering anything unexpected (`approval_unavailable`), no decision
within APPROVAL_TIMEOUT_S (`approval_timeout`), a rejection (`approval_denied`), an audit failure
(`audit_unavailable`). A late or repeated callback finds nothing to resolve and changes nothing.

The approval link is the approver's credential, so it never reaches an agent: it is not returned by any
tool, not logged and not stored in the audit tables. Only GET /approvals/pending shows it, and that
route needs the operator token (APPROVAL_OPERATOR_TOKEN), which no agent client holds.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import httpx2

from .audit import AuditSink, AuditUnavailable
from .connectors.base import ConnectorError, HttpConnector, side_channel
from .pii import mask_text

APPROVED, REJECTED, TIMED_OUT, UNAVAILABLE = "approved", "rejected", "timed_out", "unavailable"
# Outcome -> the reason code a denied caller and the audit row get.
DENIAL_REASONS = {REJECTED: "approval_denied", TIMED_OUT: "approval_timeout", UNAVAILABLE: "approval_unavailable"}
MIN_OPERATOR_TOKEN_LEN = 32
MAX_PENDING = 100
_REQUEST_ID = re.compile(r"^apr-[0-9a-f]{16}$")
_APPROVER = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,63}$")
_HEADER_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")
WEBHOOK_SECRET_HEADER = "X-Ridgeline-Approval"


class ApprovalConfigError(RuntimeError):
    """Raised at startup. Messages name variables, never values."""


@dataclass(frozen=True)
class ApprovalConfig:
    url: str                                   # n8n production webhook, e.g. http://n8n:5678/webhook/ridgeline-approval
    operator_token: str = field(repr=False)
    timeout_s: float = 120.0
    form_base_url: str = ""                    # approval links must start with this (n8n's public URL)
    webhook_secret: str = field(default="", repr=False)   # sent as X-Ridgeline-Approval; n8n checks it

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "ApprovalConfig":
        get = lambda name, default="": (env.get(name) or default).strip()   # noqa: E731
        errors: list[str] = []
        url, form_base = get("N8N_APPROVAL_URL"), get("N8N_FORM_BASE_URL").rstrip("/")
        for name, value in (("N8N_APPROVAL_URL", url), ("N8N_FORM_BASE_URL", form_base)):
            try:
                parts = urlsplit(value)
                ok = (parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username
                      and not parts.password and not parts.query and not parts.fragment)
                parts.port
            except ValueError:
                ok = False
            if not ok:
                errors.append(f"{name} must be an http(s) URL")
        token = env.get("APPROVAL_OPERATOR_TOKEN") or ""
        if len(token) < MIN_OPERATOR_TOKEN_LEN:
            errors.append(f"APPROVAL_OPERATOR_TOKEN must be set (at least {MIN_OPERATOR_TOKEN_LEN} characters)")
        webhook_secret = env.get("APPROVAL_WEBHOOK_SECRET") or ""
        if len(webhook_secret) < MIN_OPERATOR_TOKEN_LEN or not _HEADER_SAFE.match(webhook_secret):
            errors.append(f"APPROVAL_WEBHOOK_SECRET must be set (at least {MIN_OPERATOR_TOKEN_LEN} letters or digits)")
        try:
            timeout = float(get("APPROVAL_TIMEOUT_S", "120"))
        except ValueError:
            timeout = -1.0
        if not 1 <= timeout <= 900:
            errors.append("APPROVAL_TIMEOUT_S must be a number of seconds between 1 and 900")
        if errors:
            raise ApprovalConfigError("invalid approval configuration (write tools never run without an "
                                      "approver): " + "; ".join(errors))
        return ApprovalConfig(url, token, timeout, form_base, webhook_secret)


@dataclass(frozen=True)
class ApprovalResult:
    status: str                                # approved | rejected | timed_out | unavailable
    request_id: str
    approver: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == APPROVED


@dataclass
class _Pending:
    request_id: str
    token: str
    client_id: str
    tool: str
    details: dict[str, Any]
    requested_at: float
    decision: asyncio.Future[tuple[str, str]]
    approval_url: str = ""


class Approver(Protocol):
    async def request(self, principal: Mapping[str, Any], tool: str, details: Mapping[str, Any],
                      on_request_id: Callable[[str], None] | None = None) -> ApprovalResult: ...

    def resolve(self, request_id: Any, token: Any, decision: Any, approver: Any) -> bool: ...

    def pending(self) -> list[dict[str, Any]]: ...

    def operator_token_ok(self, presented: str) -> bool: ...

    async def aclose(self) -> None: ...


class NoApprover:
    """What the gateway uses until approvals are configured: every write is refused."""

    async def request(self, principal: Mapping[str, Any], tool: str, details: Mapping[str, Any],
                      on_request_id: Callable[[str], None] | None = None) -> ApprovalResult:
        return ApprovalResult(UNAVAILABLE, "apr-" + "0" * 16)

    def resolve(self, request_id: Any, token: Any, decision: Any, approver: Any) -> bool:
        return False

    def pending(self) -> list[dict[str, Any]]:
        return []

    def operator_token_ok(self, presented: str) -> bool:
        return False

    async def aclose(self) -> None:
        return None


@dataclass
class N8nApprover:
    config: ApprovalConfig
    http: HttpConnector                        # POST-only, pinned to the n8n host
    audit: AuditSink
    _pending: dict[str, _Pending] = field(default_factory=dict, repr=False)

    async def request(self, principal: Mapping[str, Any], tool: str, details: Mapping[str, Any],
                      on_request_id: Callable[[str], None] | None = None) -> ApprovalResult:
        """Ask a human and wait. Raises AuditUnavailable when the request cannot be recorded.
        `on_request_id` learns the id at once, so a call cancelled while waiting is still traceable."""
        request_id = "apr-" + secrets.token_hex(8)
        if on_request_id is not None:
            on_request_id(request_id)
        if len(self._pending) >= MAX_PENDING:
            return ApprovalResult(UNAVAILABLE, request_id)
        client_id = str(principal.get("client_id") or "")
        entry = _Pending(request_id, secrets.token_urlsafe(32), client_id, tool, dict(details), time.time(),
                         asyncio.get_running_loop().create_future())
        subject = details.get("subject")
        await self.audit.approval_requested({"request_id": request_id, "client_id": client_id, "tool": tool,
                                             "account_id": details.get("account_id"),
                                             "subject": mask_text(subject)[0] if isinstance(subject, str) else None})
        self._pending[request_id] = entry
        status, approver = UNAVAILABLE, None
        try:
            entry.approval_url = await self._ask_n8n(entry)
            if entry.approval_url:
                try:
                    status, approver = await asyncio.wait_for(entry.decision, self.config.timeout_s)
                except asyncio.TimeoutError:
                    status = TIMED_OUT
        except asyncio.CancelledError:
            # The caller went away while a human was deciding: close the record, then stop.
            self._pending.pop(request_id, None)
            with contextlib.suppress(Exception):
                await asyncio.shield(self.audit.approval_decided(request_id, TIMED_OUT, None))
            raise
        finally:
            # Removed before anything else can run: a callback that arrives from here on resolves nothing.
            self._pending.pop(request_id, None)
        try:
            await self.audit.approval_decided(request_id, status, approver)
        except AuditUnavailable:
            if status == APPROVED:
                raise                          # an approval that cannot be recorded does not count
        return ApprovalResult(status, request_id, approver)

    async def _ask_n8n(self, entry: _Pending) -> str:
        """Post the request to the workflow; returns the approval form link, or "" on any problem."""
        payload = {"request_id": entry.request_id, "callback_token": entry.token, "client_id": entry.client_id,
                   "tool": entry.tool, "expires_in_s": int(self.config.timeout_s),
                   **{k: v for k, v in entry.details.items() if isinstance(v, str)}}
        headers = {"Accept": "application/json"}
        if self.config.webhook_secret:
            headers[WEBHOOK_SECRET_HEADER] = self.config.webhook_secret
        try:
            with side_channel():
                data = await self.http.json("POST", self.config.url, json=payload, headers=headers)
        except ConnectorError:
            return ""
        url = data.get("approval_url") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("received") is not True
                or data.get("request_id") != entry.request_id or not isinstance(url, str)
                or len(url) > 500 or not url.startswith(self.config.form_base_url + "/")
                or "signature=" not in urlsplit(url).query):      # n8n signs every waiting-form link
            return ""
        return url

    def resolve(self, request_id: Any, token: Any, decision: Any, approver: Any) -> bool:
        """Called by /approvals/callback. True only for a pending request with its own one-time token."""
        if not (isinstance(request_id, str) and _REQUEST_ID.match(request_id) and isinstance(token, str)):
            return False
        entry = self._pending.get(request_id)
        if entry is None or entry.decision.done() or not hmac.compare_digest(entry.token.encode(), token.encode()):
            return False
        name = approver.strip() if isinstance(approver, str) else ""
        # Anything but an explicit "Approve" from a named person is a rejection.
        approved = decision == "Approve" and bool(_APPROVER.match(name))
        entry.decision.set_result((APPROVED if approved else REJECTED, name if _APPROVER.match(name) else None))
        return True

    def pending(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"request_id": p.request_id, "client_id": p.client_id, "tool": p.tool,
                 "account_id": p.details.get("account_id"), "subject": p.details.get("subject"),
                 "waiting_s": round(now - p.requested_at, 1),
                 "expires_in_s": max(0.0, round(self.config.timeout_s - (now - p.requested_at), 1)),
                 "approval_url": p.approval_url}
                for p in self._pending.values() if p.approval_url]

    def operator_token_ok(self, presented: str) -> bool:
        return hmac.compare_digest(self.config.operator_token.encode(), presented.encode())

    async def aclose(self) -> None:
        await self.http.aclose()


def build_approver(config: ApprovalConfig, audit: AuditSink, http_timeout_s: float = 10.0,
                   transport: httpx2.AsyncBaseTransport | None = None) -> N8nApprover:
    http = HttpConnector("The approval service", "n8n", config.url, http_timeout_s, transport,
                         allowed_methods=frozenset({"POST"}))
    return N8nApprover(config, http, audit)
