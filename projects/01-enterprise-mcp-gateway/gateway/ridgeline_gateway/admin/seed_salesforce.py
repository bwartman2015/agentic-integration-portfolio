"""Write the synthetic Ridgeline accounts (and one primary contact each) into a Salesforce Developer
Edition org, so `CRM_BACKEND=salesforce` returns the same accounts as the seed backend.

    make seed-salesforce P=01                 # dry run: shows what would be created or updated
    make seed-salesforce P=01 ARGS=--apply    # writes

Safety rules (tested in gateway/tests/test_admin.py):
- Synthetic data only: account numbers must be ACCT-xxxx and emails @example.com, checked before any request.
- Idempotent: accounts are matched on AccountNumber, contacts on account + email; a second run creates nothing.
- Owns only its own records: an existing account is updated only when its Description starts with the
  seeder's header ("Ridgeline synthetic account (fictional)"); any other account with the same number is
  skipped and reported. Duplicate account numbers are skipped too.
- Never deletes and never sends PUT or DELETE: the connector it uses allows only GET, POST and PATCH.
Uses standard fields only (see connectors/salesforce.py), so the org needs no customisation. Secrets come
from the environment; nothing secret is printed.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

import httpx2

from mock_systems import get_systems

from ..config import (SALESFORCE_HOST_SUFFIXES, SF_API_VERSION_PATTERN, ConfigError, Settings, host_allowed,
                      load_settings)
from ..connectors import salesforce_connectors
from ..connectors.base import ConnectorError
from ..connectors.salesforce import DESCRIPTION_HEADER, SF_ID, SalesforceCrm, render_description, soql_literal

_ACCOUNT_ID = re.compile(r"^ACCT-[0-9]{4}$")
_SYNTHETIC_EMAIL = re.compile(r"^[A-Za-z0-9._+-]+@example\.com$")


class UnsafeData(SystemExit):
    """Raised before any request when the dataset contains something that is not clearly synthetic."""


def synthetic_accounts(accounts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = sorted(accounts if accounts is not None else get_systems().dataset.accounts.values(),
                  key=lambda a: a["account_id"])
    for a in rows:
        email = (a.get("primary_contact") or {}).get("email") or ""
        if not _ACCOUNT_ID.match(a["account_id"]) or (email and not _SYNTHETIC_EMAIL.match(email)):
            raise UnsafeData(f"refusing to write non-synthetic record {a['account_id']}")
    return rows


def account_fields(a: dict[str, Any]) -> dict[str, Any]:
    return {"Name": a["name"], "AccountNumber": a["account_id"], "Site": a["region"],
            "Description": render_description(a["type"], a["aliases"])}


def salesforce_from(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> SalesforceCrm:
    if not (settings.sf_client_id and settings.sf_client_secret
            and host_allowed(settings.sf_login_url, SALESFORCE_HOST_SUFFIXES)
            and SF_API_VERSION_PATTERN.match(settings.sf_api_version)):
        raise ConfigError("set SF_LOGIN_URL (https://<your-domain>.my.salesforce.com), SF_CLIENT_ID, "
                          "SF_CLIENT_SECRET and a valid SF_API_VERSION in .env first (docs/live-sandbox-setup.md)")
    return salesforce_connectors(settings, transport, write=True)


def _sf_id(value: Any) -> str:
    if not isinstance(value, str) or not SF_ID.match(value):
        raise ConnectorError("Salesforce returned an unexpected record id.")
    return value


async def seed(sf: SalesforceCrm, apply: bool, accounts: list[dict[str, Any]] | None = None,
               actions: list[str] | None = None) -> list[str]:
    """Each action is appended *before* its write, so after a failure the caller can still report what was
    attempted (the last entry may not have been written)."""
    rows = synthetic_accounts(accounts)          # validates everything before the first request
    actions = [] if actions is None else actions
    for a in rows:
        found = await sf.call("GET", "query", params={
            "q": f"SELECT Id, Description FROM Account WHERE AccountNumber = '{soql_literal(a['account_id'])}' LIMIT 2"})
        records = (found or {}).get("records", [])
        if len(records) > 1:
            actions.append(f"SKIP   {a['account_id']}: more than one account has this number; fix it in the org")
            continue
        account_sf_id: str | None
        if records:
            if not (records[0].get("Description") or "").startswith(DESCRIPTION_HEADER):
                actions.append(f"SKIP   {a['account_id']}: an account with this number exists but was not "
                               "created by the seeder; left unchanged")
                continue
            account_sf_id = _sf_id(records[0].get("Id"))
            actions.append(f"UPDATE {a['account_id']} {a['name']}")
            if apply:
                await sf.call("PATCH", f"sobjects/Account/{account_sf_id}", json_body=account_fields(a))
        else:
            actions.append(f"CREATE {a['account_id']} {a['name']}")
            account_sf_id = None
            if apply:
                created = await sf.call("POST", "sobjects/Account", json_body=account_fields(a))
                account_sf_id = _sf_id((created or {}).get("id"))
        contact = a.get("primary_contact")
        if not contact:
            continue
        if account_sf_id and records:
            existing = await sf.call("GET", "query", params={
                "q": (f"SELECT Id FROM Contact WHERE AccountId = '{soql_literal(account_sf_id)}' "
                      f"AND Email = '{soql_literal(contact['email'])}' LIMIT 1")})
            if (existing or {}).get("records"):
                continue
        actions.append(f"CREATE   contact {contact['role']} <{contact['email']}> for {a['account_id']}")
        if apply and account_sf_id:
            await sf.call("POST", "sobjects/Contact", json_body={
                "LastName": contact["role"], "Title": contact["role"], "Email": contact["email"],
                "AccountId": account_sf_id})
    return actions


def main(argv: list[str] | None = None, transport: httpx2.AsyncBaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write to the org (default: dry run)")
    args = parser.parse_args(argv)
    actions: list[str] = []
    try:
        sf = salesforce_from(load_settings(), transport)
        asyncio.run(seed(sf, args.apply, actions=actions))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except ConnectorError as exc:
        print(f"Salesforce error: {exc}", file=sys.stderr)
        if actions:
            print(f"stopped after {len(actions)} action(s); the last one may not have been written:")
            for line in actions:
                print("  " + line)
        return 1
    print(("APPLIED" if args.apply else "DRY RUN (add ARGS=--apply to write)") + f": {len(actions)} action(s)")
    for line in actions:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
