"""Mock AR (accounts receivable) API. Auth: `Authorization: Bearer <token>`.

  GET /v1/accounts/{account_id}/open-items   -> open invoices (totals are the gateway's job)
Run: uvicorn ridgeline_mock_apis.ar:create_app --factory   (needs AR_API_TOKEN)
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Header, Path
from fastapi.responses import JSONResponse

from mock_systems import get_systems

from .common import ACCOUNT_ID_PATTERN, base_app, required_secret, same_secret, unauthorized


def create_app(token: str | None = None):
    expected = required_secret("AR_API_TOKEN", token)
    app = base_app("Ridgeline mock AR API")

    @app.get("/v1/accounts/{account_id}/open-items", response_model=None)
    async def open_items(account_id: Annotated[str, Path(pattern=ACCOUNT_ID_PATTERN)],
                         authorization: Annotated[str | None, Header()] = None) -> Any:
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not same_secret(given, expected):
            return unauthorized("Bearer")
        systems = get_systems()
        if account_id not in systems.dataset.accounts:
            return JSONResponse({"detail": "unknown account"}, status_code=404)
        return {"account_id": account_id, "invoices": systems.ar.open_items(account_id)}

    return app
