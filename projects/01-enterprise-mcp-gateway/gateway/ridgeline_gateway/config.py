"""Gateway settings from the environment (Docker Compose passes `.env`). Secrets never leave this module
unmasked except through `Secret.reveal()`, and the gateway refuses to start on a half-configured
backend (fail closed) instead of silently falling back to seeded data."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

CRM_BACKENDS = ("seed", "salesforce")
ERP_BACKENDS = ("seed", "sap_sandbox")
HTTP_BACKENDS = ("seed", "http")
DEFAULT_SAP_BASE_URL = "https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV"
# Client credentials work only on My Domain hosts, and Salesforce returns instance URLs on them too.
SALESFORCE_HOST_SUFFIXES = (".my.salesforce.com",)
SF_API_VERSION_PATTERN = re.compile(r"^v\d{2,3}\.0$")
SAP_HOST_SUFFIXES = (".api.sap.com",)
MIN_SHARED_SECRET_LEN = 16


class ConfigError(RuntimeError):
    """Raised at startup. Messages name variables, never values."""


class Secret:
    """A string that cannot be printed, logged or compared by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('********')" if self._value else "Secret('')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:  # pragma: no cover - deliberately unsupported
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


def host_allowed(url: str, suffixes: tuple[str, ...]) -> bool:
    """https only, default port only, host under one of `suffixes`, no credentials in the URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and port is None and not parts.username and not parts.password
            and any(host.endswith(s) and len(host) > len(s) for s in suffixes))


@dataclass(frozen=True)
class Settings:
    crm_backend: str = "seed"
    erp_backend: str = "seed"
    carrier_backend: str = "seed"
    ar_backend: str = "seed"
    sf_login_url: str = ""
    sf_client_id: Secret = field(default_factory=lambda: Secret(""))
    sf_client_secret: Secret = field(default_factory=lambda: Secret(""))
    sf_api_version: str = "v62.0"
    sap_base_url: str = DEFAULT_SAP_BASE_URL
    sap_api_key: Secret = field(default_factory=lambda: Secret(""))
    carrier_api_url: str = "http://carrier-api:8001"
    carrier_api_key: Secret = field(default_factory=lambda: Secret(""))
    ar_api_url: str = "http://ar-api:8002"
    ar_api_token: Secret = field(default_factory=lambda: Secret(""))
    http_timeout_s: float = 10.0

    def backends(self) -> dict[str, str]:
        return {"crm": self.crm_backend, "erp": self.erp_backend,
                "carrier": self.carrier_backend, "ar": self.ar_backend}

    def validate(self) -> "Settings":
        errors: list[str] = []
        for name, value, allowed in (("CRM_BACKEND", self.crm_backend, CRM_BACKENDS),
                                     ("ERP_BACKEND", self.erp_backend, ERP_BACKENDS),
                                     ("CARRIER_BACKEND", self.carrier_backend, HTTP_BACKENDS),
                                     ("AR_BACKEND", self.ar_backend, HTTP_BACKENDS)):
            if value not in allowed:
                errors.append(f"{name} must be one of {', '.join(allowed)}")
        if self.crm_backend == "salesforce":
            for name, secret in (("SF_CLIENT_ID", self.sf_client_id), ("SF_CLIENT_SECRET", self.sf_client_secret)):
                if not secret:
                    errors.append(f"{name} is required when CRM_BACKEND=salesforce")
            if not host_allowed(self.sf_login_url, SALESFORCE_HOST_SUFFIXES):
                errors.append("SF_LOGIN_URL must be https://<your-domain>.my.salesforce.com")
            if not SF_API_VERSION_PATTERN.match(self.sf_api_version):
                errors.append("SF_API_VERSION must look like v62.0")
        if self.erp_backend == "sap_sandbox":
            if not self.sap_api_key:
                errors.append("SAP_API_KEY is required when ERP_BACKEND=sap_sandbox")
            if not host_allowed(self.sap_base_url, SAP_HOST_SUFFIXES):
                errors.append("SAP_BASE_URL must be an https URL on api.sap.com")
        for backend, name, url, secret_name, secret in (
                (self.carrier_backend, "CARRIER_API_URL", self.carrier_api_url, "CARRIER_API_KEY", self.carrier_api_key),
                (self.ar_backend, "AR_API_URL", self.ar_api_url, "AR_API_TOKEN", self.ar_api_token)):
            if backend == "http":
                if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
                    errors.append(f"{name} must be an http(s) URL")
                if len(secret.reveal()) < MIN_SHARED_SECRET_LEN:
                    errors.append(f"{secret_name} must be set (at least {MIN_SHARED_SECRET_LEN} characters)")
        if not 0 < self.http_timeout_s <= 60:
            errors.append("HTTP_TIMEOUT_S must be between 0 and 60")
        if errors:
            raise ConfigError("invalid gateway configuration: " + "; ".join(errors))
        return self


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return (e.get(name) or default).strip()

    try:
        timeout = float(get("HTTP_TIMEOUT_S", "10"))
    except ValueError:
        raise ConfigError("invalid gateway configuration: HTTP_TIMEOUT_S must be a number") from None
    return Settings(
        crm_backend=get("CRM_BACKEND", "seed"), erp_backend=get("ERP_BACKEND", "seed"),
        carrier_backend=get("CARRIER_BACKEND", "seed"), ar_backend=get("AR_BACKEND", "seed"),
        sf_login_url=get("SF_LOGIN_URL").rstrip("/"), sf_client_id=Secret(get("SF_CLIENT_ID")),
        sf_client_secret=Secret(get("SF_CLIENT_SECRET")), sf_api_version=get("SF_API_VERSION", "v62.0"),
        sap_base_url=get("SAP_BASE_URL", DEFAULT_SAP_BASE_URL).rstrip("/"), sap_api_key=Secret(get("SAP_API_KEY")),
        carrier_api_url=get("CARRIER_API_URL", "http://carrier-api:8001").rstrip("/"),
        carrier_api_key=Secret(get("CARRIER_API_KEY")),
        ar_api_url=get("AR_API_URL", "http://ar-api:8002").rstrip("/"), ar_api_token=Secret(get("AR_API_TOKEN")),
        http_timeout_s=timeout,
    ).validate()
