"""Salesforce CRM connector (Developer Edition org; REST API, OAuth 2.0 client credentials flow).

Ridgeline accounts live in standard fields so no org customisation is needed (see
`admin/seed_salesforce.py`, which writes them):
  AccountNumber = ACCT-1002          Site = region            Name = account name
  Description   = "Ridgeline synthetic account (fictional)\\nType: Contractor\\nAliases: BBO"
  first Contact = primary contact (Title = role, Email @example.com)
Search = SOQL on name/number, plus SOSL across all fields so aliases ("BBO") match.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import SALESFORCE_HOST_SUFFIXES, Secret, host_allowed
from .base import AuthRejected, ConnectorError, HttpConnector

RIDGELINE_PREFIX = "ACCT-"
DESCRIPTION_HEADER = "Ridgeline synthetic account (fictional)"
_SOSL_RESERVED = set('?&|!{}[]()^~*:\\"\'+-')
MAX_MATCHES = 20
SF_ID = re.compile(r"^[A-Za-z0-9]{15,18}$")
TOKEN_TTL_S = 15 * 60   # client-credentials tokens carry no expiry; refresh proactively and on 401


def soql_literal(value: str) -> str:
    """Escape a value for a SOQL string literal (quotes and backslashes)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def soql_like(value: str) -> str:
    """Escape a value for use inside a SOQL LIKE pattern (wildcards are literal)."""
    return soql_literal(value).replace("%", "\\%").replace("_", "\\_")


def sosl_term(value: str) -> str:
    """Escape SOSL reserved characters so the term is searched literally."""
    return "".join(f"\\{c}" if c in _SOSL_RESERVED else c for c in value)


def parse_description(text: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": None, "aliases": []}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "type":
            out["type"] = value.strip() or None
        elif key.strip().lower() == "aliases":
            out["aliases"] = [a.strip() for a in value.split(",") if a.strip()]
    return out


def render_description(kind: str, aliases: list[str]) -> str:
    return f"{DESCRIPTION_HEADER}\nType: {kind}\nAliases: {', '.join(aliases)}"


def to_account(rec: dict[str, Any]) -> dict[str, Any]:
    contacts = ((rec.get("Contacts") or {}).get("records") or [])
    contact = contacts[0] if contacts else None
    return {"account_id": rec.get("AccountNumber"), "name": rec.get("Name"),
            "type": parse_description(rec.get("Description"))["type"], "region": rec.get("Site"),
            "primary_contact": {"role": contact.get("Title"), "email": contact.get("Email")} if contact else None}


@dataclass
class SalesforceAuth:
    http: HttpConnector            # pinned to the login (My Domain) host
    client_id: Secret
    client_secret: Secret
    _token: str | None = field(default=None, repr=False)
    _instance_url: str | None = None
    _fetched_at: float = 0.0

    async def token(self, force: bool = False) -> tuple[str, str]:
        if force or not self._token or time.monotonic() - self._fetched_at > TOKEN_TTL_S:
            data = await self.http.json("POST", f"{self.http.base_url}/services/oauth2/token",
                                        data={"grant_type": "client_credentials",
                                              "client_id": self.client_id.reveal(),
                                              "client_secret": self.client_secret.reveal()},
                                        headers={"Accept": "application/json"})
            token, instance = (data or {}).get("access_token"), (data or {}).get("instance_url", "")
            # Never send the bearer token to a host Salesforce did not vouch for.
            if not token or not host_allowed(instance, SALESFORCE_HOST_SUFFIXES):
                raise ConnectorError("Salesforce returned an unusable token response.")
            self._token, self._instance_url, self._fetched_at = token, instance.rstrip("/"), time.monotonic()
        assert self._token and self._instance_url
        return self._token, self._instance_url


@dataclass
class SalesforceCrm:
    auth: SalesforceAuth
    api: HttpConnector             # same transport; allowlist extended to the instance host on first token
    api_version: str = "v62.0"

    async def call(self, method: str, path: str, *, params: dict[str, str] | None = None,
                   json_body: dict[str, Any] | None = None) -> Any:
        """One authorized REST call under /services/data/<version>/. Returns parsed JSON, or None for an
        empty (204) response. A 401 refreshes the token and retries once; that is safe for writes too,
        because a 401 means Salesforce rejected the request before doing anything."""
        for attempt in (0, 1):
            token, instance = await self.auth.token(force=attempt == 1)
            host = (urlsplit(instance).hostname or "").lower()
            if host not in self.api.allowed_hosts:
                self.api.allowed_hosts = (*self.api.allowed_hosts, host)
            try:
                resp = await self.api.request(method, f"{instance}/services/data/{self.api_version}/{path}",
                                              params=params, json=json_body,
                                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            except AuthRejected:
                if attempt == 0:
                    continue        # token revoked or expired: fetch a new one, once
                raise
            if resp is None or resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise ConnectorError("Salesforce returned a response the gateway could not read.") from None
        raise AuthRejected("Salesforce rejected the gateway's credentials.")  # pragma: no cover

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        return await self.call("GET", path, params=params)

    async def find_accounts(self, query: str) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        ids: list[str] = []
        # SOSL (aliases in Description) needs at least 2 alphanumeric characters; a SOSL 400 (a term
        # Salesforce won't search) only means "no alias hits", the SOQL name search still runs.
        if sum(c.isalnum() for c in q) >= 2:
            sosl = (f"FIND {{{sosl_term(q)}}} IN ALL FIELDS RETURNING "
                    f"Account(Id WHERE AccountNumber LIKE '{RIDGELINE_PREFIX}%') LIMIT {MAX_MATCHES}")
            try:
                found = await self._get("search", {"q": sosl})
            except ConnectorError as exc:
                if exc.status != 400:
                    raise
                found = None
            for rec in (found or {}).get("searchRecords", []):
                if isinstance(rec.get("Id"), str) and SF_ID.match(rec["Id"]):
                    ids.append(rec["Id"])
        where = f"Name LIKE '%{soql_like(q)}%' OR AccountNumber LIKE '%{soql_like(q)}%'"
        if ids:
            where += " OR Id IN (" + ", ".join(f"'{soql_literal(i)}'" for i in ids) + ")"
        soql = ("SELECT Id, AccountNumber, Name, Site, Description, "
                "(SELECT Title, Email FROM Contacts ORDER BY CreatedDate ASC LIMIT 1) FROM Account "
                f"WHERE AccountNumber LIKE '{RIDGELINE_PREFIX}%' AND ({where}) ORDER BY AccountNumber LIMIT {MAX_MATCHES}")
        data = await self._get("query", {"q": soql})
        return [to_account(r) for r in (data or {}).get("records", [])]
