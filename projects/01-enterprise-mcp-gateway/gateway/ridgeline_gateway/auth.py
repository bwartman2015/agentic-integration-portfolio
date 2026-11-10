"""Identity (Milestone 3): the gateway is an OAuth 2.0 resource server for Keycloak-issued access tokens.

`KeycloakJwtVerifier` plugs into the MCP SDK's bearer-auth middleware (`TokenVerifier`). Every /mcp
request must carry `Authorization: Bearer <JWT>`; anything that fails a check gets 401 with a
`WWW-Authenticate` header that points at /.well-known/oauth-protected-resource/mcp (RFC 9728), which
names Keycloak as the authorization server. Checks:
  - RS256 only (no `none`, no HMAC with the public key), signed by a key in Keycloak's JWKS
  - `iss` equals the configured issuer exactly; `aud` contains `ridgeline-gateway`; `typ` is Bearer
  - `exp`, `iat`, `iss`, `aud` present; `exp` enforced exactly (as the SDK middleware also does);
    a small leeway (OIDC_LEEWAY_S, default 5 s) only for clock skew on `nbf`/`iat`
  - an `azp` (the agent's client id) is present
Signing keys come from Keycloak over the internal network and are cached. They are refetched every
10 minutes, and at most once a minute when a token names an unknown key (key rotation). Requests that
arrive during a fetch wait for it, so a burst after a restart makes one fetch and every valid token
in it is accepted. A token that cannot be verified is rejected (401):
  - cold start with Keycloak unreachable: no keys, every token is rejected;
  - Keycloak unreachable later: cached keys keep working for up to 1 hour, then are dropped and every
    token is rejected until Keycloak is back. A key Keycloak rotated out stays trusted at most that long.
Rejections are logged with a reason, rate-limited per reason; the token is never logged.

Authorization (which scopes may call which tool, and which regions' data a caller may see) is decided
by OPA (policy.py, Milestone 4) from the verified client id, scopes and `regions` claim read here.
Every call log line records them; tokens are never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx2
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from .connectors.base import ConnectorError, HttpConnector

log = logging.getLogger("ridgeline.gateway")

ALGORITHMS = ["RS256"]
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SCOPE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_REGION = re.compile(r"^(?:\*|[A-Za-z][A-Za-z -]{0,31})$")


class AuthConfigError(RuntimeError):
    """Raised at startup when identity is not configured (the gateway never runs unauthenticated)."""


@dataclass(frozen=True)
class AuthConfig:
    issuer: str                 # exact `iss` in tokens, e.g. http://localhost:8080/realms/ridgeline
    jwks_url: str               # where the gateway fetches signing keys (internal network)
    audience: str               # e.g. ridgeline-gateway
    resource_url: str           # this server's public MCP URL, for RFC 9728 metadata
    leeway_s: int = 5

    @staticmethod
    def from_env(env: dict[str, str]) -> "AuthConfig":
        def get(name: str, default: str = "") -> str:
            return (env.get(name) or default).strip()

        leeway = get("OIDC_LEEWAY_S", "5")
        if not leeway.isdigit():
            raise AuthConfigError("invalid identity configuration: OIDC_LEEWAY_S must be a whole number of seconds")
        cfg = AuthConfig(issuer=get("OIDC_ISSUER").rstrip("/"), jwks_url=get("OIDC_JWKS_URL"),
                         audience=get("OIDC_AUDIENCE", "ridgeline-gateway"),
                         resource_url=get("GATEWAY_PUBLIC_URL", "http://localhost:8765/mcp"), leeway_s=int(leeway))
        errors = []
        for name, url in (("OIDC_ISSUER", cfg.issuer), ("OIDC_JWKS_URL", cfg.jwks_url),
                          ("GATEWAY_PUBLIC_URL", cfg.resource_url)):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
                errors.append(f"{name} must be an http(s) URL")
        if not _CLIENT_ID.match(cfg.audience):
            errors.append("OIDC_AUDIENCE must be a plain identifier")
        if not 0 <= cfg.leeway_s <= 60:
            errors.append("OIDC_LEEWAY_S must be between 0 and 60")
        if errors:
            raise AuthConfigError("invalid identity configuration (the gateway never runs unauthenticated): "
                                  + "; ".join(errors))
        return cfg

    def sdk_settings(self) -> AuthSettings:
        # validate_token_resource=False: the verifier checks `aud` itself (Keycloak does not bind
        # tokens to an RFC 8707 resource indicator).
        return AuthSettings(issuer_url=self.issuer, resource_server_url=self.resource_url,
                            validate_token_resource=False, required_scopes=None)


@dataclass
class _LogLimiter:
    """At most `burst` log lines per reason per `window_s`; the next allowed line reports how many were
    suppressed. Keeps unauthenticated floods from flooding the log."""
    burst: int = 10
    window_s: float = 10.0
    _state: dict[str, list[float]] = field(default_factory=dict)   # reason -> [window_start, count, suppressed]

    def allow(self, reason: str) -> tuple[bool, int]:
        now = time.monotonic()
        st = self._state.setdefault(reason, [now, 0, 0])
        if now - st[0] >= self.window_s:
            st[0], st[1] = now, 0
        if st[1] < self.burst:
            st[1] += 1
            suppressed, st[2] = int(st[2]), 0
            return True, suppressed
        st[2] += 1
        return False, 0


@dataclass
class KeycloakJwtVerifier:
    config: AuthConfig
    http: HttpConnector                       # GET-only, pinned to the JWKS host
    min_refetch_s: float = 60.0               # unknown-kid refetch rate limit (also the retry pause when down)
    max_key_age_s: float = 600.0              # periodic refresh
    max_stale_s: float = 3600.0               # cached keys are dropped if Keycloak can't be reached this long
    _keys: dict[str, jwt.PyJWK] = field(default_factory=dict, repr=False)
    _fetched_at: float = field(default=float("-inf"), repr=False)   # last attempt (ok or not)
    _last_ok: float = field(default=float("-inf"), repr=False)      # last successful fetch
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _limiter: _LogLimiter = field(default_factory=_LogLimiter, repr=False)

    def _reject(self, reason: str, detail: str | None = None) -> None:
        """Log why a token was refused (never the token or its claims; rate-limited per reason) and
        return None, which the SDK middleware turns into 401."""
        allowed, suppressed = self._limiter.allow(reason)
        if allowed:
            record: dict[str, Any] = {"event": "auth_rejected", "reason": reason}
            if detail:
                record["detail"] = detail
            if suppressed:
                record["suppressed_since_last"] = suppressed
            log.info(json.dumps(record, sort_keys=True))
        return None

    async def _refresh_keys(self) -> None:
        self._fetched_at = time.monotonic()          # set first: concurrent callers see the window closed
        try:
            data = await self.http.json("GET", self.config.jwks_url, headers={"Accept": "application/json"})
            entries = (data or {}).get("keys", [])
            if not isinstance(entries, list):
                raise ValueError("keys is not a list")
        except (ConnectorError, ValueError, TypeError, AttributeError) as exc:
            log.info(json.dumps({"event": "jwks_refresh_failed", "detail": type(exc).__name__}, sort_keys=True))
            return
        keys: dict[str, jwt.PyJWK] = {}
        for jwk in entries:
            if not (isinstance(jwk, dict) and jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig"
                    and isinstance(jwk.get("kid"), str) and jwk.get("alg", "RS256") in ALGORITHMS):
                continue
            try:
                keys[jwk["kid"]] = jwt.PyJWK.from_dict(jwk, algorithm="RS256")
            except (jwt.PyJWTError, ValueError, TypeError):
                continue                              # one malformed key must not drop the others
        self._keys, self._last_ok = keys, time.monotonic()

    def _needs_refresh(self, kid: str) -> bool:
        age = time.monotonic() - self._fetched_at
        return (kid not in self._keys and age >= self.min_refetch_s) or age >= self.max_key_age_s

    async def _key_for(self, kid: str) -> jwt.PyJWK | None:
        # Unknown kid or refresh due: queue on the lock, so a request that arrives while a fetch is in
        # flight waits for its result instead of reading a stale or empty cache. The re-check inside
        # the lock keeps it to one fetch, and the 60 s limit still stops random kids forcing refetches.
        if kid not in self._keys or self._needs_refresh(kid):
            async with self._lock:
                if self._needs_refresh(kid):
                    await self._refresh_keys()
        if time.monotonic() - self._last_ok > self.max_stale_s:
            self._keys = {}                           # Keycloak unreachable too long: stop trusting old keys
        return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return self._reject("malformed")
        if header.get("alg") not in ALGORITHMS:
            return self._reject("algorithm_not_allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return self._reject("no_key_id")
        key = await self._key_for(kid)
        if key is None:
            return self._reject("unknown_signing_key")
        try:
            # The leeway covers small clock skew on nbf/iat; exp is enforced exactly below, matching the
            # SDK middleware's own strict expires_at check, so the two layers agree.
            claims: dict[str, Any] = jwt.decode(
                token, key=key.key, algorithms=ALGORITHMS, audience=self.config.audience, issuer=self.config.issuer,
                leeway=self.config.leeway_s, options={"require": ["exp", "iat", "iss", "aud"]})
        except jwt.ExpiredSignatureError:
            return self._reject("expired")
        except jwt.ImmatureSignatureError:
            return self._reject("not_yet_valid")
        except jwt.InvalidAudienceError:
            return self._reject("wrong_audience")
        except jwt.InvalidIssuerError:
            return self._reject("wrong_issuer")
        except jwt.InvalidSignatureError:
            return self._reject("bad_signature")
        except jwt.MissingRequiredClaimError:
            return self._reject("missing_claim")
        except jwt.PyJWTError:
            return self._reject("invalid")
        if int(claims["exp"]) < int(time.time()):
            return self._reject("expired")
        if claims.get("typ") != "Bearer":
            return self._reject("not_an_access_token")
        client_id = claims.get("azp") or claims.get("client_id")
        if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
            return self._reject("no_client_id")
        scope = claims.get("scope", "")
        scopes = sorted({s for s in scope.split() if _SCOPE.match(s)}) if isinstance(scope, str) else []
        # `regions` (Milestone 4): the account regions this agent may see; "*" means all. Anything that
        # is not a list of plain names is dropped, and a token without regions sees no regional data.
        raw_regions = claims.get("regions")
        regions = (sorted({r for r in raw_regions if isinstance(r, str) and _REGION.match(r)})
                   if isinstance(raw_regions, list) else [])
        return AccessToken(token=token, client_id=client_id, scopes=scopes, expires_at=int(claims["exp"]),
                           subject=claims.get("sub") if isinstance(claims.get("sub"), str) else None,
                           claims={"iss": claims["iss"], "regions": regions})


def build_verifier(config: AuthConfig, timeout_s: float = 10.0,
                   transport: httpx2.AsyncBaseTransport | None = None) -> KeycloakJwtVerifier:
    http = HttpConnector("Keycloak", "keycloak", config.jwks_url, timeout_s, transport)
    return KeycloakJwtVerifier(config, http)


def current_principal() -> dict[str, Any]:
    """Identity of the caller for the current request (empty for in-process calls without HTTP auth)."""
    token = get_access_token()
    if token is None:
        return {}
    return {"client_id": token.client_id, "scopes": list(token.scopes),
            "regions": list((token.claims or {}).get("regions") or [])}
