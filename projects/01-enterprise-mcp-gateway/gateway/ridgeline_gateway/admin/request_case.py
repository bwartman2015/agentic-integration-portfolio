"""Ask the gateway, as the cs-lead agent, to create a CRM case, and wait for the human decision.

    make request-case P=01      # then, in another terminal: make approvals P=01, open the link, decide

This is the approval flow end to end with real services: the call goes through Keycloak, OPA, the audit
log and the n8n approval workflow, and returns only after a person approves or rejects it in the n8n
form (or APPROVAL_TIMEOUT_S passes). Runs in the one-off `ops` container. Synthetic data only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from ..connectors import ConnectorError
from .token import token_for

URL = os.environ.get("LIVE_CHECK_URL", "http://gateway:8000/mcp")
DEFAULT_CASE = {"account_id": "ACCT-1002", "subject": "Late delivery SO-104233",
                "summary": "Delivery attempted 2026-11-05, dock closed; new ETA 2026-11-10. Confirm the window with the customer."}


async def request_case(case: dict[str, str], wait_s: float) -> tuple[bool, str]:
    token = await asyncio.to_thread(token_for, "cs-lead")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=wait_s) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            result = await client.call_tool("create_case", case)
    if result.is_error:
        return False, result.content[0].text if result.content else "error"
    return True, json.dumps(result.structured_content, indent=2)


DROPPED = ("The gateway closed the connection before answering: it was restarted or stopped while the "
           "request was waiting for a decision. Nothing was written. The request shows as `unavailable` in "
           "`make audit P=01` once the gateway is back up; run `make request-case P=01` again.")


def explain_failure(exc: BaseException) -> str:
    """One clear line for the ways a waiting request can fail, instead of a traceback."""
    leaves: list[BaseException] = []
    stack = [exc]
    while stack:                                   # anyio wraps transport errors in exception groups
        e = stack.pop()
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        else:
            leaves.append(e)
    for e in leaves:
        if isinstance(e, MCPError) or isinstance(e, (httpx2.RemoteProtocolError, httpx2.ReadError)):
            return DROPPED
    for e in leaves:
        if isinstance(e, httpx2.ConnectError):
            return "The gateway is not reachable. Start the stack with `make up P=01` and try again."
        if isinstance(e, httpx2.TimeoutException):
            return "No answer from the gateway in time. Check `make approvals P=01` and `make audit P=01`."
        if isinstance(e, ConnectorError):
            return f"Could not get a token from Keycloak: {e}"
    return f"The request failed ({type(leaves[0]).__name__ if leaves else type(exc).__name__}). Check `make audit P=01`."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Request a CRM case as cs-lead and wait for the human approval")
    parser.add_argument("--account", default=DEFAULT_CASE["account_id"], help="account id, e.g. ACCT-1002")
    args = parser.parse_args(argv)
    wait_s = float(os.environ.get("APPROVAL_TIMEOUT_S", "120")) + 30
    print(f"cs-lead asks the gateway to create a case for {args.account}.")
    print("Waiting for a human decision. In another terminal run `make approvals P=01` and open the link.")
    try:
        ok, text = asyncio.run(request_case({**DEFAULT_CASE, "account_id": args.account}, wait_s))
    except Exception as exc:  # noqa: BLE001  every failure becomes one readable line, never a traceback
        print(f"NOT CREATED: {explain_failure(exc)}", file=sys.stderr)
        return 1
    print("APPROVED, case created:" if ok else "NOT CREATED:")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
