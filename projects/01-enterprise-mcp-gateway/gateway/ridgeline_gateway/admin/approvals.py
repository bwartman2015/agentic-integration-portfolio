"""List the write requests that are waiting for a human decision, with their approval links.

    make approvals P=01

Runs in the one-off `ops` container with the operator token (APPROVAL_OPERATOR_TOKEN). No agent client
holds that token, so an agent cannot fetch the link and approve its own request. Open a link in a
browser, choose Approve or Reject, give your name and submit.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from ..connectors.base import ConnectorError, HttpConnector


async def fetch_pending(base_url: str, operator_token: str) -> list[dict[str, Any]]:
    http = HttpConnector("The gateway", "gateway", base_url, 10.0)
    try:
        data = await http.json("GET", f"{base_url}/approvals/pending",
                               headers={"Authorization": f"Bearer {operator_token}", "Accept": "application/json"})
    finally:
        await http.aclose()
    return list((data or {}).get("pending") or [])


def render(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return "no requests are waiting for approval"
    lines = [f"{len(pending)} request(s) waiting for a human decision:"]
    for p in pending:
        lines += ["", f"  {p.get('request_id')}  {p.get('client_id')} asks to run {p.get('tool')} for {p.get('account_id')}",
                  f"  subject: {p.get('subject')}",
                  f"  expires in {p.get('expires_in_s')} s",
                  f"  open to decide: {p.get('approval_url')}"]
    return "\n".join(lines)


def main() -> int:
    base_url = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
    token = os.environ.get("APPROVAL_OPERATOR_TOKEN", "")
    if not base_url or not token:
        raise SystemExit("GATEWAY_BASE_URL and APPROVAL_OPERATOR_TOKEN must be set (run via `make approvals`)")
    try:
        print(render(asyncio.run(fetch_pending(base_url, token))))
    except ConnectorError as exc:
        print(f"could not list approvals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
