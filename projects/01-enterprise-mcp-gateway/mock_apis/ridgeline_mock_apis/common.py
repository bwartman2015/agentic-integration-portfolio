"""Shared pieces: secret loading (fail closed), constant-time comparison, a locked-down FastAPI app."""
from __future__ import annotations

import hmac
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

MIN_SECRET_LEN = 16
SALES_ORDER_PATTERN = r"^SO-[0-9]{6}$"
ACCOUNT_ID_PATTERN = r"^ACCT-[0-9]{4}$"


def required_secret(name: str, value: str | None = None) -> str:
    secret = (value if value is not None else os.environ.get(name, "")).strip()
    if len(secret) < MIN_SECRET_LEN:
        raise RuntimeError(f"{name} must be set (at least {MIN_SECRET_LEN} characters); refusing to start")
    return secret


def same_secret(given: str | None, expected: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


def base_app(title: str) -> FastAPI:
    # No interactive docs or schema endpoint: the surface is exactly the routes below.
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def unauthorized(scheme: str) -> JSONResponse:
    return JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": scheme})
