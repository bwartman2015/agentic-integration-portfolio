"""Mock carrier tracking API (Granite Freight Lines, fictional). Auth: `X-API-Key` header.

  GET /v1/orders/{sales_order}/tracking   -> tracking events, lateness, proof of delivery
Run: uvicorn ridgeline_mock_apis.carrier:create_app --factory   (needs CARRIER_API_KEY)
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Header, Path
from fastapi.responses import JSONResponse

from mock_systems import get_systems

from .common import SALES_ORDER_PATTERN, base_app, required_secret, same_secret, unauthorized


def create_app(api_key: str | None = None):
    key = required_secret("CARRIER_API_KEY", api_key)
    app = base_app("Ridgeline mock carrier API")

    @app.get("/v1/orders/{sales_order}/tracking", response_model=None)
    async def tracking(sales_order: Annotated[str, Path(pattern=SALES_ORDER_PATTERN)],
                       x_api_key: Annotated[str | None, Header()] = None) -> Any:
        if not same_secret(x_api_key, key):
            return unauthorized("ApiKey")
        systems = get_systems()
        if sales_order not in systems.dataset.orders:
            return JSONResponse({"detail": "no carrier record for this order"}, status_code=404)
        return systems.carrier.tracking(sales_order)

    return app
