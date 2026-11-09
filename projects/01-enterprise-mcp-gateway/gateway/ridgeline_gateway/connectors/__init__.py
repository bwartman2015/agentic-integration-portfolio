"""Connectors: one per enterprise system, each with a `seed` backend and a live backend (see config.py)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx2

from ..config import Settings
from .base import (ArConnector, AuthRejected, CarrierConnector, ConnectorError, CrmConnector, ErpConnector,
                   Forbidden, HttpConnector, NotFound, begin_upstream)
from .http_mocks import ArHttp, CarrierHttp
from .salesforce import SalesforceAuth, SalesforceCrm
from .sap import SapSandboxErp
from .seed import SeedAr, SeedCarrier, SeedCrm, SeedErp

__all__ = ["Connectors", "build_connectors", "salesforce_connectors", "ConnectorError", "AuthRejected", "Forbidden",
           "NotFound", "begin_upstream"]


@dataclass
class Connectors:
    crm: CrmConnector
    erp: ErpConnector
    carrier: CarrierConnector
    ar: ArConnector

    async def aclose(self) -> None:
        """Close every HTTP connection pool (server shutdown)."""
        for c in (self.crm, self.erp, self.carrier, self.ar):
            for http in (getattr(c, "http", None), getattr(c, "api", None),
                         getattr(getattr(c, "auth", None), "http", None)):
                if isinstance(http, HttpConnector):
                    await http.aclose()


def salesforce_connectors(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None, *,
                          write: bool = False) -> SalesforceCrm:
    """The Salesforce CRM connector. The gateway's is read-only (GET, plus the token POST on the login
    host); only the operator seed tool asks for `write=True` (POST/PATCH, never DELETE or PUT)."""
    t = settings.http_timeout_s
    login = HttpConnector("Salesforce", "salesforce", settings.sf_login_url, t, transport,
                          allowed_methods=frozenset({"POST"}))
    api = HttpConnector("Salesforce", "salesforce", settings.sf_login_url, t, transport,
                        allowed_methods=frozenset({"GET", "POST", "PATCH"}) if write else frozenset({"GET"}))
    return SalesforceCrm(SalesforceAuth(login, settings.sf_client_id, settings.sf_client_secret), api,
                         settings.sf_api_version)


def build_connectors(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> Connectors:
    """`transport` lets tests swap the network for httpx2.MockTransport; production passes None."""
    t = settings.http_timeout_s

    crm: CrmConnector = (salesforce_connectors(settings, transport) if settings.crm_backend == "salesforce"
                         else SeedCrm())

    erp: ErpConnector = (SapSandboxErp(HttpConnector("SAP", "sap_sandbox", settings.sap_base_url, t, transport),
                                       settings.sap_api_key)
                         if settings.erp_backend == "sap_sandbox" else SeedErp())
    carrier: CarrierConnector = (CarrierHttp(HttpConnector("Carrier API", "carrier_api", settings.carrier_api_url, t, transport),
                                             settings.carrier_api_key)
                                 if settings.carrier_backend == "http" else SeedCarrier())
    ar: ArConnector = (ArHttp(HttpConnector("AR API", "ar_api", settings.ar_api_url, t, transport), settings.ar_api_token)
                       if settings.ar_backend == "http" else SeedAr())
    return Connectors(crm, erp, carrier, ar)
