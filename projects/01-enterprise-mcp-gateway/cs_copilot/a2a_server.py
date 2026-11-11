"""A2A endpoint for the CS copilot (Milestone 7): its Agent Card and `message/send`, so the Project 08
Command Center (or any A2A client) can discover the copilot and delegate customer service questions.

    make a2a-up P=01      start it on http://localhost:8701 (profile "a2a")
    make a2a-check P=01   fetch the card, then send a task as cs-rep and expect `completed` + a trace id

Identity: Keycloak token passthrough, no shared static token. The caller gets an access token for its
own Keycloak client (client credentials) and sends it as `Authorization: Bearer`. This server verifies
it exactly as the gateway does (RS256 against Keycloak's keys, exact issuer, audience
`ridgeline-gateway`, expiry, `typ` Bearer, a client id), builds the caller's principal from the verified
scopes and `regions` claim, and runs the copilot with that same token on every gateway call. The gateway
verifies it again and authorizes and audits each call as that caller: a rep delegating over A2A gets
exactly a rep's rights, and the audit log names the real caller. A missing or invalid token is a 401;
the card itself is public (it holds no hosts beyond localhost and no secrets).

Trade-off of passthrough: the token's audience is the gateway's, so any token valid for the gateway is
also accepted here (it grants nothing more than at the gateway, which re-checks every call). A stricter
setup adds a Keycloak audience mapper so tokens name both services and this server requires its own.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from agentcore import AgentCard, Outcome, Principal, serve_agent

from .agent.build import build_cs_copilot
from .agent.guardrails import TOOL_SCOPES
from .agent.mcp_client import McpToolSource
from .agent.reason import get_llm
from .ask import script_for

CARD_FILE = pathlib.Path(__file__).resolve().parents[1] / "a2a" / "cs-copilot.card.json"
ALGORITHMS = ["RS256"]
KNOWN_SCOPES = frozenset(TOOL_SCOPES.values())
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REGION = re.compile(r"^(?:\*|[A-Za-z][A-Za-z -]{0,31})$")      # the gateway's own rule


@dataclass(frozen=True)
class VerifierConfig:
    issuer: str
    audience: str
    jwks_url: str
    leeway_s: int = 5

    @staticmethod
    def from_env(env: dict[str, str]) -> "VerifierConfig":
        missing = [k for k in ("OIDC_ISSUER", "OIDC_JWKS_URL") if not env.get(k)]
        if missing:
            raise ValueError(f"set {', '.join(missing)}")
        return VerifierConfig(env["OIDC_ISSUER"], env.get("OIDC_AUDIENCE", "ridgeline-gateway"), env["OIDC_JWKS_URL"])


class TokenVerifier:
    """Verified token -> Principal, or None. `keys(token)` returns the signing key (PyJWKClient, cached)."""

    def __init__(self, config: VerifierConfig, keys: Callable[[str], Any] | None = None):
        self.config = config
        if keys is None:
            import jwt
            client = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600, timeout=5)
            keys = lambda token: client.get_signing_key_from_jwt(token).key   # noqa: E731
        self._keys = keys

    def principal(self, token: str | None) -> Principal | None:
        import jwt
        if not token:
            return None
        try:
            claims = jwt.decode(token, key=self._keys(token), algorithms=ALGORITHMS, audience=self.config.audience,
                                issuer=self.config.issuer, leeway=self.config.leeway_s,
                                options={"require": ["exp", "iat", "iss", "aud"]})
        except Exception:  # noqa: BLE001  any failure to verify is a refusal
            return None
        client_id = claims.get("azp") or claims.get("client_id")
        if claims.get("typ") != "Bearer" or not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
            return None
        scope = claims.get("scope", "")
        scopes = frozenset(s for s in scope.split() if s in KNOWN_SCOPES) if isinstance(scope, str) else frozenset()
        raw = claims.get("regions")
        regions = sorted({r for r in raw if isinstance(r, str) and _REGION.match(r)}) if isinstance(raw, list) else []
        return Principal(client_id, scopes, {"regions": regions})


class CopilotForCaller:
    """One task for one verified caller: a fresh copilot whose gateway calls carry the caller's token."""

    def __init__(self, token: str, gateway_url: str, llm_factory: Callable[[str], Any] | None = None,
                 builder: Callable[..., Any] = build_cs_copilot):
        self.token, self.gateway_url, self.builder = token, gateway_url, builder
        self.llm_factory = llm_factory or _llm_for

    def run(self, text: str, principal: Principal) -> Outcome:
        llm = self.llm_factory(text)
        if llm is None:
            return Outcome("rejected", "This copilot runs a scripted model that has no plan for that question; "
                           "ask one of its card's examples, or run it with a real model.", 0, 0, 0.0, "")
        source = McpToolSource(self.gateway_url, lambda: self.token)
        return self.builder(llm, source=source).run(text, principal)


def _llm_for(text: str) -> Any:
    if os.environ.get("LLM_PROVIDER", "scripted") == "scripted":
        script = script_for(text)
        return get_llm(script=script) if script else None
    return get_llm()


def load_card(public_url: str) -> AgentCard:
    card = AgentCard.from_dict(json.loads(CARD_FILE.read_text()))
    card.url = public_url
    return card


def make_authenticator(verifier: TokenVerifier, gateway_url: str) -> Callable[[str | None], Any]:
    def authenticate(token: str | None) -> tuple[CopilotForCaller, Principal] | None:
        principal = verifier.principal(token)
        if principal is None or token is None:
            return None
        return CopilotForCaller(token, gateway_url), principal
    return authenticate


def main() -> int:
    env = dict(os.environ)
    gateway_url = env.get("GATEWAY_URL", "").strip()
    if not gateway_url:
        raise SystemExit("GATEWAY_URL must be set (the A2A server runs the copilot on the gateway)")
    verifier = TokenVerifier(VerifierConfig.from_env(env))
    public_url = env.get("A2A_PUBLIC_URL", "http://localhost:8701")
    card = load_card(public_url)
    serve_agent(None, card, authenticate=make_authenticator(verifier, gateway_url),
                host=env.get("A2A_HOST", "0.0.0.0"), port=int(env.get("A2A_PORT", "8701")), public_url=public_url)
    print(json.dumps({"event": "a2a_ready", "card": card.name, "url": public_url}), flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
