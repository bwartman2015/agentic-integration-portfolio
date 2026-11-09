"""Identity (Milestone 3): every /mcp request needs a valid Keycloak access token; anything else is 401.
A test RSA key pair stands in for Keycloak's signing key, served from a fake JWKS endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import unittest
from typing import Any
from unittest import mock

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from mock_systems import reset_systems
from ridgeline_gateway import server
from ridgeline_gateway.auth import AuthConfig, AuthConfigError, build_verifier
from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import build_connectors
from ridgeline_gateway.server import configure_logging, mcp, transport_security

configure_logging()
ISSUER = "http://localhost:8080/realms/ridgeline"
JWKS_URL = "http://keycloak:8080/realms/ridgeline/protocol/openid-connect/certs"
CONFIG = AuthConfig(issuer=ISSUER, jwks_url=JWKS_URL, audience="ridgeline-gateway",
                    resource_url="http://localhost:8765/mcp", leeway_s=5)
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def reset_auth() -> None:
    """Tests only: back to the unauthenticated in-process app (production never does this)."""
    mcp.settings.auth = None
    mcp._token_verifier = None


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_of(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeKeycloak:
    def __init__(self) -> None:
        self.keys = {"k1": new_key()}
        self.fetches = 0
        self.down = False

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.fetches += 1
        if self.down:
            raise httpx2.ConnectError("refused", request=req)
        return httpx2.Response(200, json={"keys": [jwk_of(k, kid) for kid, k in self.keys.items()]})

    def mint(self, kid: str = "k1", key: rsa.RSAPrivateKey | None = None, alg: str = "RS256",
             drop: tuple[str, ...] = (), **overrides: Any) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": ["ridgeline-gateway"], "azp": "cs-rep", "typ": "Bearer",
                  "scope": "crm.read erp.read shipping.read ar.read", "iat": now, "exp": now + 300,
                  "sub": "service-account-cs-rep", **overrides}
        for c in drop:
            claims.pop(c, None)
        signing_key: Any = key or self.keys[kid]
        return jwt.encode(claims, signing_key, algorithm=alg, headers={"kid": kid})


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.idp = FakeKeycloak()
        self.verifier = build_verifier(CONFIG, transport=httpx2.MockTransport(self.idp))

    async def rejected(self, token: str, reason: str) -> None:
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            self.assertIsNone(await self.verifier.verify_token(token))
        lines = [json.loads(r.getMessage()) for r in cm.records]
        self.assertIn({"event": "auth_rejected", "reason": reason}, lines)
        self.assertNotIn(token, "\n".join(r.getMessage() for r in cm.records))

    async def test_valid_token_yields_client_and_scopes(self):
        at = await self.verifier.verify_token(self.idp.mint())
        self.assertEqual(at.client_id, "cs-rep")
        self.assertEqual(at.scopes, ["ar.read", "crm.read", "erp.read", "shipping.read"])
        self.assertEqual(at.claims, {"iss": ISSUER})

    async def test_expired_token(self):
        now = int(time.time())
        await self.rejected(self.idp.mint(iat=now - 400, exp=now - 60), "expired")

    async def test_exp_is_strict_but_nbf_iat_get_clock_leeway(self):
        now = int(time.time())
        await self.rejected(self.idp.mint(iat=now - 300, exp=now - 2), "expired")      # same as the SDK's own check
        self.assertIsNotNone(await self.verifier.verify_token(self.idp.mint(nbf=now + 3, iat=now + 3)))

    async def test_not_yet_valid(self):
        await self.rejected(self.idp.mint(nbf=int(time.time()) + 120), "not_yet_valid")

    async def test_wrong_audience_and_issuer(self):
        await self.rejected(self.idp.mint(aud=["account"]), "wrong_audience")
        await self.rejected(self.idp.mint(iss="http://localhost:8080/realms/master"), "wrong_issuer")

    async def test_required_claims(self):
        for claim in ("exp", "iat", "iss", "aud"):
            with self.subTest(claim=claim):
                await self.rejected(self.idp.mint(drop=(claim,)), "missing_claim")

    async def test_not_an_access_token_and_no_client(self):
        await self.rejected(self.idp.mint(typ="ID"), "not_an_access_token")
        await self.rejected(self.idp.mint(drop=("typ",)), "not_an_access_token")
        await self.rejected(self.idp.mint(drop=("azp",)), "no_client_id")

    async def test_forged_or_malformed_tokens(self):
        await self.rejected("not-a-jwt", "malformed")
        await self.rejected(self.idp.mint(key=new_key()), "bad_signature")           # right kid, wrong key
        await self.rejected(self.idp.mint(kid="k-unknown", key=new_key()), "unknown_signing_key")
        header = jwt.utils.base64url_encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).decode()
        body = jwt.utils.base64url_encode(json.dumps({"iss": ISSUER, "aud": "ridgeline-gateway", "azp": "x",
                                                      "iat": 1, "exp": 9999999999}).encode()).decode()
        await self.rejected(f"{header}.{body}.", "algorithm_not_allowed")
        # HS256 signed with the (public) RSA key bytes: the classic key-confusion forgery. Only RS256 is accepted.
        public_pem = self.idp.keys["k1"].public_key().public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        b64 = lambda raw: jwt.utils.base64url_encode(raw).decode()   # noqa: E731
        signing_input = (b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode()) + "." +
                         b64(json.dumps({"iss": ISSUER, "aud": "ridgeline-gateway", "azp": "cs-rep",
                                         "iat": int(time.time()), "exp": int(time.time()) + 300}).encode()))
        mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
        await self.rejected(f"{signing_input}.{b64(mac)}", "algorithm_not_allowed")

    async def test_keys_are_cached_and_rotation_refetches_rate_limited(self):
        await self.verifier.verify_token(self.idp.mint())
        await self.verifier.verify_token(self.idp.mint())
        self.assertEqual(self.idp.fetches, 1)
        self.idp.keys["k2"] = new_key()
        self.verifier._fetched_at -= 61                       # past the unknown-kid rate limit
        self.assertIsNotNone(await self.verifier.verify_token(self.idp.mint(kid="k2")))
        self.assertEqual(self.idp.fetches, 2)
        await self.rejected(self.idp.mint(kid="k3", key=new_key()), "unknown_signing_key")
        self.assertEqual(self.idp.fetches, 2, "unknown kid within 60s must not refetch")

    async def test_keycloak_unreachable_on_cold_start_fails_closed(self):
        self.idp.down = True
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            self.assertIsNone(await self.verifier.verify_token(self.idp.mint()))
        events = [json.loads(r.getMessage()) for r in cm.records]
        self.assertEqual([(e["event"], e.get("reason")) for e in events],
                         [("jwks_refresh_failed", None), ("auth_rejected", "unknown_signing_key")])

    async def test_keycloak_down_with_warm_cache_keeps_keys_only_up_to_the_stale_cap(self):
        self.assertIsNotNone(await self.verifier.verify_token(self.idp.mint()))
        self.idp.down = True
        self.verifier._fetched_at -= 601                     # periodic refresh due; it fails
        self.assertIsNotNone(await self.verifier.verify_token(self.idp.mint()), "cached key within the cap")
        self.verifier._last_ok -= 3601                       # Keycloak unreachable for over an hour
        await self.rejected(self.idp.mint(), "unknown_signing_key")

    async def test_concurrent_unknown_kids_trigger_a_single_fetch(self):
        tokens = [self.idp.mint(kid=f"rand-{i}", key=new_key()) for i in range(20)]
        results = await asyncio.gather(*(self.verifier.verify_token(t) for t in tokens))
        self.assertEqual(results, [None] * 20)
        self.assertEqual(self.idp.fetches, 1)

    async def test_valid_burst_during_a_slow_cold_fetch_is_all_accepted_with_one_fetch(self):
        fetches = {"n": 0}

        async def slow_jwks(req: httpx2.Request) -> httpx2.Response:
            fetches["n"] += 1
            await asyncio.sleep(0.05)
            return httpx2.Response(200, json={"keys": [jwk_of(self.idp.keys["k1"], "k1")]})
        verifier = build_verifier(CONFIG, transport=httpx2.MockTransport(slow_jwks))
        results = await asyncio.gather(*(verifier.verify_token(self.idp.mint()) for _ in range(20)))
        self.assertTrue(all(r is not None for r in results), "a valid token was rejected during the fetch")
        self.assertEqual(fetches["n"], 1)

    async def test_one_malformed_jwk_does_not_drop_the_others(self):
        good = jwk_of(self.idp.keys["k1"], "k1")
        verifier = build_verifier(CONFIG, transport=httpx2.MockTransport(lambda r: httpx2.Response(
            200, json={"keys": [{"kty": "RSA", "kid": "bad", "use": "sig", "n": "!!", "e": "AQAB"}, good,
                                "not-a-dict"]})))
        self.assertIsNotNone(await verifier.verify_token(self.idp.mint()))

    async def test_rejection_logs_are_rate_limited_per_reason(self):
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            for _ in range(15):
                await self.verifier.verify_token("garbage")
        self.assertEqual(len(cm.records), 10)
        self.verifier._limiter._state["malformed"][0] -= 11   # next window
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            await self.verifier.verify_token("garbage")
        self.assertEqual(json.loads(cm.records[0].getMessage())["suppressed_since_last"], 5)

    async def test_config_validation(self):
        with self.assertRaises(AuthConfigError):
            AuthConfig.from_env({})
        with self.assertRaises(AuthConfigError):
            AuthConfig.from_env({"OIDC_ISSUER": ISSUER, "OIDC_JWKS_URL": JWKS_URL, "OIDC_LEEWAY_S": "abc"})
        cfg = AuthConfig.from_env({"OIDC_ISSUER": ISSUER + "/", "OIDC_JWKS_URL": JWKS_URL})
        self.assertEqual((cfg.issuer, cfg.audience, cfg.leeway_s), (ISSUER, "ridgeline-gateway", 5))


class HttpAuthTests(unittest.TestCase):
    """The real gateway app as uvicorn serves it, with auth enabled exactly as main() does."""

    def setUp(self) -> None:
        reset_systems()
        server.set_connectors(build_connectors(load_settings({})))
        self.idp = FakeKeycloak()
        mcp.enable_auth(CONFIG, build_verifier(CONFIG, transport=httpx2.MockTransport(self.idp)))
        self.addCleanup(reset_auth)
        app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=transport_security())
        self.http = TestClient(app, base_url="http://localhost:8765")
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def post(self, body: dict, token: str | None = None, session: str | None = None):
        headers = dict(MCP_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["Mcp-Session-Id"] = session
            headers["MCP-Protocol-Version"] = "2025-06-18"
        return self.http.post("/mcp", headers=headers, json=body)

    def test_no_token_is_401_with_resource_metadata_pointer(self):
        resp = self.post(INIT)
        self.assertEqual(resp.status_code, 401)
        challenge = resp.headers["www-authenticate"]
        self.assertTrue(challenge.startswith("Bearer"))
        self.assertIn('resource_metadata="http://localhost:8765/.well-known/oauth-protected-resource/mcp"', challenge)

    def test_expired_wrong_and_garbage_tokens_are_401(self):
        now = int(time.time())
        for token in (self.idp.mint(iat=now - 400, exp=now - 60), self.idp.mint(aud="other"), "garbage",
                      self.idp.mint(key=new_key())):
            self.assertEqual(self.post(INIT, token).status_code, 401)

    def test_non_bearer_scheme_is_401(self):
        resp = self.http.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Basic Y3MtcmVwOnNlY3JldA=="}, json=INIT)
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_can_initialize_and_call_a_tool_and_is_logged_by_client(self):
        token = self.idp.mint()
        init = self.post(INIT, token)
        self.assertEqual(init.status_code, 200)
        session = init.headers.get("mcp-session-id")
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, session)
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            resp = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "get_open_invoices", "arguments": {"account_id": "ACCT-1002"}}},
                             token, session)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("INV-90388", resp.text)
        calls = [json.loads(r.getMessage()) for r in cm.records if '"tool_call"' in r.getMessage()]
        self.assertEqual(calls[0]["client_id"], "cs-rep")
        self.assertEqual(calls[0]["scopes"], ["ar.read", "crm.read", "erp.read", "shipping.read"])
        self.assertNotIn(token, "\n".join(r.getMessage() for r in cm.records))

    def _session(self, token: str) -> str:
        init = self.post(INIT, token)
        self.assertEqual(init.status_code, 200)
        session = init.headers["mcp-session-id"]
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, session)
        return session

    def test_an_existing_session_still_needs_a_token_on_every_request(self):
        session = self._session(self.idp.mint())
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        self.assertEqual(self.post(call, None, session).status_code, 401)
        for method in ("GET", "DELETE"):
            resp = self.http.request(method, "/mcp", headers={"Mcp-Session-Id": session, "Accept": "text/event-stream"})
            self.assertEqual(resp.status_code, 401, method)

    def test_another_client_cannot_ride_a_session(self):
        session = self._session(self.idp.mint())
        lead = self.idp.mint(azp="cs-lead", sub="service-account-cs-lead", scope="crm.read crm.write")
        resp = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, lead, session)
        self.assertEqual(resp.status_code, 404)          # SDK binds a session to (client_id, iss, sub)

    def test_health_and_protected_resource_metadata_stay_open(self):
        self.assertEqual(self.http.get("/healthz").status_code, 200)
        meta = self.http.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(meta.status_code, 200)
        body = meta.json()
        self.assertEqual(body["resource"], "http://localhost:8765/mcp")
        self.assertEqual([s.rstrip("/") for s in body["authorization_servers"]], [ISSUER])


class StartupTests(unittest.TestCase):
    def test_gateway_refuses_to_start_without_identity(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(mcp, "run") as run, \
                self.assertLogs("ridgeline.gateway", logging.ERROR) as cm:
            with self.assertRaises(SystemExit) as ctx:
                server.main()
        self.assertEqual(ctx.exception.code, 2)
        run.assert_not_called()
        reason = json.loads(cm.records[-1].getMessage())["reason"]
        self.assertIn("OIDC_ISSUER", reason)
        self.assertIn("never runs unauthenticated", reason)
        reset_auth()


if __name__ == "__main__":
    unittest.main()
