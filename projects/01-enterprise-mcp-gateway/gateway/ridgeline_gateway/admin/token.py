"""Get a Keycloak access token for one of the agent clients (OAuth 2.0 client credentials).

    make token P=01 CLIENT=cs-rep      # prints a 5-minute access token, e.g. to paste into MCP Inspector

Runs in the one-off `ops` container, which holds the agent client secrets; the gateway container never
does. The token is printed only to the terminal of whoever runs it. It is a short-lived bearer
credential: don't paste it anywhere public.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import sys

import httpx2

from ..connectors.base import ConnectorError, HttpConnector

CLIENT_SECRET_ENV = {"cs-rep": "CS_REP_CLIENT_SECRET", "cs-lead": "CS_LEAD_CLIENT_SECRET",
                     "p01-expiry-test": "P01_EXPIRY_TEST_CLIENT_SECRET",
                     "p01-crm-only-test": "P01_CRM_ONLY_TEST_CLIENT_SECRET"}
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


async def fetch_token(client_id: str, secret: str, token_url: str, timeout_s: float = 10.0,
                      transport: httpx2.AsyncBaseTransport | None = None) -> str:
    http = HttpConnector("Keycloak", "keycloak", token_url, timeout_s, transport, allowed_methods=frozenset({"POST"}))
    try:
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()   # client_secret_basic (RFC 6749 2.3.1)
        data = await http.json("POST", token_url, data={"grant_type": "client_credentials"},
                               headers={"Accept": "application/json", "Authorization": f"Basic {basic}"})
    finally:
        await http.aclose()
    token = (data or {}).get("access_token")
    if not isinstance(token, str) or not _JWT_SHAPE.match(token):
        raise ConnectorError("Keycloak returned an unusable token response.")
    return token


def token_for(client_id: str) -> str:
    if client_id not in CLIENT_SECRET_ENV:
        raise SystemExit(f"unknown client {client_id!r}; choose one of {', '.join(CLIENT_SECRET_ENV)}")
    secret = os.environ.get(CLIENT_SECRET_ENV[client_id], "")
    token_url = os.environ.get("OIDC_TOKEN_URL", "")
    if not secret or not token_url:
        raise SystemExit(f"{CLIENT_SECRET_ENV[client_id]} and OIDC_TOKEN_URL must be set (run via `make token`)")
    return asyncio.run(fetch_token(client_id, secret, token_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a Keycloak access token for an agent client")
    parser.add_argument("client", nargs="?", default="cs-rep", choices=sorted(CLIENT_SECRET_ENV))
    args = parser.parse_args(argv)
    try:
        print(token_for(args.client))
    except ConnectorError as exc:
        print(f"Keycloak error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
