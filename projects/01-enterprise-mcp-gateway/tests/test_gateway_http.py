"""Wire-level MCP contract test against the running gateway container, using only the stdlib.

It speaks raw JSON-RPC over MCP streamable HTTP (initialize -> notifications/initialized -> tools/list
-> tools/call), so it checks protocol compliance independently of the SDK the server is built with.
Start the gateway first (`make up P=01`); otherwise these tests are skipped with the reason.
Set REQUIRE_GATEWAY=1 (CI) to turn that skip into a failure, so a run cannot pass by skipping.
The URL comes from GATEWAY_URL, else http://localhost:${GATEWAY_PORT:-8765}. `make test` does not
read .env, so export GATEWAY_PORT (and KEYCLOAK_PORT) if you changed them there.

The data assertions compare against the seeded dataset (`mock_systems/`), which is what the default
Compose stack serves: seed CRM/ERP in the gateway, carrier and AR over HTTP from the mock APIs. With
live CRM/ERP backends configured (Salesforce, SAP sandbox) the seed-only comparisons are skipped and
`make live-check P=01` covers those tools instead. The mode is detected from the running gateway (the
seeded order SO-104233 is not in the SAP sandbox); GATEWAY_LIVE_BACKENDS=1 or =0 overrides the detection.

Authorization (Milestone 4): the stack's real OPA decides every call. cs-rep is a Central-region rep,
cs-lead sees every region, p01-expiry-test has no scopes and p01-crm-only-test holds crm.read only.

Identity (Milestone 3): every /mcp call needs a Keycloak access token. These tests get real tokens from
the running Keycloak (client credentials) using only the client secrets they need, read from the
project's .env (git-ignored) or the environment; secrets and tokens are never printed.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import time
import unittest
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("GATEWAY_URL", f"http://localhost:{os.environ.get('GATEWAY_PORT', '8765')}").rstrip("/")
REQUIRE_GATEWAY = os.environ.get("REQUIRE_GATEWAY") == "1"
KEYCLOAK = os.environ.get("KEYCLOAK_URL", f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8080')}").rstrip("/")
TOKEN_URL = f"{KEYCLOAK}/realms/ridgeline/protocol/openid-connect/token"
ISSUER = f"{KEYCLOAK}/realms/ridgeline"
ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
SECRET_KEYS = {"cs-rep": "CS_REP_CLIENT_SECRET", "cs-lead": "CS_LEAD_CLIENT_SECRET",
               "p01-expiry-test": "P01_EXPIRY_TEST_CLIENT_SECRET",
               "p01-crm-only-test": "P01_CRM_ONLY_TEST_CLIENT_SECRET"}
TOOLS = ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mock_systems import get_systems  # noqa: E402  (stdlib-only seeded dataset, for comparisons)
PROTOCOL_VERSION = "2025-06-18"


def _client_secret(client_id: str) -> str:
    """The one secret this test needs: from the environment, else from the project's .env."""
    key = SECRET_KEYS[client_id]
    if os.environ.get(key):
        return os.environ[key]
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.split(" #", 1)[0].strip()
    message = f"{key} not available (run `make up P=01`, which generates it)"
    if REQUIRE_GATEWAY:
        raise AssertionError(f"REQUIRE_GATEWAY=1 but {message}")     # a CI run must not pass by skipping
    raise unittest.SkipTest(message)


def fetch_token(client_id: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(f"{client_id}:{_client_secret(client_id)}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Authorization": f"Basic {basic}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


class McpHttpSession:
    """Minimal MCP client over streamable HTTP: JSON or SSE responses, session id header."""

    def __init__(self, base: str, token: str | None = None):
        self.url = f"{base}/mcp"
        self.token = token
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode()

    @staticmethod
    def _parse(headers: dict[str, str], body: str) -> dict[str, Any]:
        if headers.get("content-type", "").startswith("text/event-stream"):
            events = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            messages = [json.loads(e) for e in events if e]
            responses = [m for m in messages if "id" in m and ("result" in m or "error" in m)]
            if not responses:
                raise AssertionError(f"no JSON-RPC response in SSE stream: {body[:300]}")
            return responses[-1]
        return json.loads(body)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        status, headers, body = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                                            "params": params or {}})
        if status != 200:
            raise AssertionError(f"{method}: HTTP {status}: {body[:300]}")
        if method == "initialize":
            self.session_id = headers.get("mcp-session-id")
        return self._parse(headers, body)

    def notify(self, method: str) -> int:
        status, _, _ = self._post({"jsonrpc": "2.0", "method": method})
        return status

    def initialize(self) -> dict[str, Any]:
        resp = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "p01-contract-test", "version": "0.1.0"}})
        status = self.notify("notifications/initialized")
        if status not in (200, 202):
            raise AssertionError(f"notifications/initialized: HTTP {status}")
        return resp


_UP = _gateway_up()


def _live_crm_erp() -> bool:
    """True when the running gateway serves live CRM/ERP backends instead of the seeded data.
    GATEWAY_LIVE_BACKENDS=1/0 decides when set; otherwise ask the gateway, as cs-lead (who sees every
    region), for the seeded order SO-104233: only the seed ERP returns it for Summit (ACCT-1002)."""
    forced = os.environ.get("GATEWAY_LIVE_BACKENDS")
    if forced in ("0", "1"):
        return forced == "1"
    if not _UP:
        return False
    try:
        mcp = McpHttpSession(BASE, fetch_token("cs-lead"))
        mcp.initialize()
        result = mcp.request("tools/call", {"name": "get_sales_order",
                                            "arguments": {"sales_order": "SO-104233"}})["result"]
    except Exception:  # noqa: BLE001  no secret or no answer: assume the default stack; the tests then report why
        return False
    return bool(result.get("isError")) or (result.get("structuredContent") or {}).get("account_id") != "ACCT-1002"


LIVE_CRM_ERP = _live_crm_erp()
_LIVE_SKIP = "live CRM/ERP backends detected (or GATEWAY_LIVE_BACKENDS=1)"


class GatewayRequiredTest(unittest.TestCase):
    def test_gateway_reachable_when_required(self):
        if not REQUIRE_GATEWAY:
            self.skipTest("set REQUIRE_GATEWAY=1 to fail when the gateway is down")
        self.assertTrue(_UP, f"REQUIRE_GATEWAY=1 but no gateway at {BASE}")


@unittest.skipUnless(_UP, f"gateway not running at {BASE} (start it with `make up P=01`)")
class GatewayHttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = fetch_token("cs-rep")

    def setUp(self) -> None:
        self.mcp = McpHttpSession(BASE, self.token)
        self.init = self.mcp.initialize()

    def test_initialize_advertises_server_and_tools_capability(self):
        result = self.init["result"]
        self.assertEqual(result["serverInfo"]["name"], "ridgeline-gateway")
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(self.mcp.session_id, "server issued no Mcp-Session-Id")

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.mcp.request("tools/call", {"name": name, "arguments": args})["result"]

    def test_tools_list_has_the_four_read_only_tools(self):
        tools = {t["name"]: t for t in self.mcp.request("tools/list")["result"]["tools"]}
        self.assertEqual(sorted(tools), sorted(TOOLS))
        for t in tools.values():
            self.assertTrue(t["annotations"]["readOnlyHint"], t["name"])
            self.assertIs(t["inputSchema"]["additionalProperties"], False, t["name"])
        self.assertEqual(tools["get_sales_order"]["inputSchema"]["properties"]["sales_order"]["pattern"], "^SO-[0-9]{6}$")

    def test_every_read_tool_returns_data(self):
        """Milestone 2 acceptance on the default stack: all read tools return data (a Central account,
        read by the Central rep). With live CRM/ERP the order tools are covered by `make live-check`."""
        calls = [("search_accounts", {"query": "Summit"}), ("get_sales_order", {"sales_order": "SO-104233"}),
                 ("track_shipment", {"sales_order": "SO-104233"}), ("get_open_invoices", {"account_id": "ACCT-1002"})]
        for name, args in calls if not LIVE_CRM_ERP else [calls[0], calls[3]]:
            with self.subTest(tool=name):
                result = self._call(name, args)
                self.assertFalse(result.get("isError", False), result)
                self.assertTrue(result["structuredContent"], name)

    @unittest.skipIf(LIVE_CRM_ERP, f"{_LIVE_SKIP}: SO-104233 is not in the SAP sandbox, so its region is unknown")
    def test_carrier_and_ar_over_http_match_the_seeded_systems(self):
        systems = get_systems()       # Summit Contractors (Central): the records a Central rep may read
        self.assertEqual(self._call("track_shipment", {"sales_order": "SO-104233"})["structuredContent"],
                         systems.carrier.tracking("SO-104233"))
        items = systems.ar.open_items("ACCT-1002")
        self.assertEqual(self._call("get_open_invoices", {"account_id": "ACCT-1002"})["structuredContent"],
                         {"account_id": "ACCT-1002", "invoices": items, "count": len(items),
                          "total_open": round(sum(i["amount"] for i in items), 2)})

    @unittest.skipIf(LIVE_CRM_ERP, f"{_LIVE_SKIP}: CRM/ERP are live, not seeded")
    def test_seeded_crm_alias_search(self):
        lead = McpHttpSession(BASE, fetch_token("cs-lead"))       # Big Box Outdoor is an East account
        lead.initialize()
        result = lead.request("tools/call", {"name": "search_accounts", "arguments": {"query": "BBO"}})["result"]
        self.assertEqual([m["account_id"] for m in result["structuredContent"]["matches"]], ["ACCT-1001"])

    @unittest.skipIf(LIVE_CRM_ERP, f"{_LIVE_SKIP}: CRM/ERP are live, not seeded")
    def test_tools_call_returns_the_order(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-104233"}})["result"]
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["account_name"], "Summit Contractors LLC")
        self.assertEqual(json.loads(result["content"][0]["text"])["sales_order"], "SO-104233")

    def test_tools_call_unknown_order_is_error_result(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-999999"}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("not found", result["content"][0]["text"])

    def test_tools_call_extra_argument_is_error_result(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-104233", "extra": "x"}})["result"]
        self.assertTrue(result["isError"])

    def _rejected_status(self, header: str, value: str) -> int:
        # Authenticated, so the request reaches the Host/Origin check (identity is checked first).
        req = urllib.request.Request(f"{BASE}/mcp", method="POST", data=b"{}",
                                     headers={header: value, "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream",
                                              "Authorization": f"Bearer {self.token}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        ctx.exception.close()
        return ctx.exception.code

    def test_foreign_host_header_is_421(self):
        self.assertEqual(self._rejected_status("Host", "attacker.example"), 421)

    def test_foreign_origin_is_403(self):
        self.assertEqual(self._rejected_status("Origin", "http://attacker.example"), 403)


@unittest.skipUnless(_UP, f"gateway not running at {BASE} (start it with `make up P=01`)")
class GatewayIdentityTests(unittest.TestCase):
    """Milestone 3 acceptance: no token / expired token -> 401, against the real Keycloak and gateway."""

    def _init_status(self, token: str | None, scheme: str = "Bearer") -> tuple[int, dict[str, str]]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"{scheme} {token}"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                      "clientInfo": {"name": "p01-identity-test", "version": "0.1.0"}}}).encode()
        req = urllib.request.Request(f"{BASE}/mcp", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as err:
            err.read()
            err.close()
            return err.code, {k.lower(): v for k, v in err.headers.items()}

    def test_no_token_is_401_with_challenge(self):
        status, headers = self._init_status(None)
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata=", headers.get("www-authenticate", ""))

    def test_tampered_and_wrong_scheme_tokens_are_401(self):
        token = fetch_token("cs-rep")
        head, payload, sig = token.split(".")
        forged = dict(claims_of(token), azp="cs-lead", scope="crm.write")
        forged_payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        self.assertEqual(self._init_status(f"{head}.{forged_payload}.{sig}")[0], 401)
        self.assertEqual(self._init_status(token, scheme="Basic")[0], 401)
        self.assertEqual(self._init_status("not-a-token")[0], 401)

    def test_real_expired_token_is_401(self):
        token = fetch_token("p01-expiry-test")            # Keycloak issues it with a 5-second lifetime
        c = claims_of(token)
        self.assertLessEqual(c["exp"] - c["iat"], 10)
        self.assertEqual(self._init_status(token)[0], 200, "fresh token should be accepted")
        time.sleep(max(0, c["exp"] + 2 - time.time()))    # exp is enforced exactly (no leeway on exp)
        self.assertEqual(self._init_status(token)[0], 401)

    def test_both_agent_clients_reach_all_tools_with_their_scopes(self):
        expected = {"cs-rep": {"crm.read", "erp.read", "shipping.read", "ar.read"},
                    "cs-lead": {"crm.read", "erp.read", "shipping.read", "ar.read", "crm.write"}}
        for client, scopes in expected.items():
            with self.subTest(client=client):
                token = fetch_token(client)
                c = claims_of(token)
                self.assertEqual((c["iss"], c["azp"]), (ISSUER, client))
                self.assertIn("ridgeline-gateway", c["aud"] if isinstance(c["aud"], list) else [c["aud"]])
                self.assertEqual(set(c["scope"].split()), scopes)
                self.assertEqual(c["regions"], ["Central"] if client == "cs-rep" else ["*"])
                session = McpHttpSession(BASE, token)
                session.initialize()
                names = [t["name"] for t in session.request("tools/list")["result"]["tools"]]
                self.assertEqual(sorted(names), sorted(TOOLS))
                result = session.request("tools/call", {"name": "get_open_invoices",
                                                        "arguments": {"account_id": "ACCT-1002"}})["result"]
                self.assertFalse(result.get("isError", False))

    def test_protected_resource_metadata_names_keycloak(self):
        with urllib.request.urlopen(f"{BASE}/.well-known/oauth-protected-resource/mcp", timeout=5) as resp:
            meta = json.loads(resp.read())
        self.assertEqual(meta["resource"], f"{BASE}/mcp")
        self.assertEqual([a.rstrip("/") for a in meta["authorization_servers"]], [ISSUER])


@unittest.skipUnless(_UP, f"gateway not running at {BASE} (start it with `make up P=01`)")
class GatewayAuthorizationTests(unittest.TestCase):
    """Milestone 4 acceptance: an unauthorized tool gets a clear MCP error, decided by the stack's OPA
    from the scopes and regions in real Keycloak tokens."""

    CALLS = [("search_accounts", {"query": "Summit"}, "crm.read"),
             ("get_sales_order", {"sales_order": "SO-104233"}, "erp.read"),
             ("track_shipment", {"sales_order": "SO-104233"}, "shipping.read"),
             ("get_open_invoices", {"account_id": "ACCT-1002"}, "ar.read")]

    @staticmethod
    def session(client_id: str) -> McpHttpSession:
        mcp = McpHttpSession(BASE, fetch_token(client_id))
        mcp.initialize()
        return mcp

    @staticmethod
    def call(mcp: McpHttpSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return mcp.request("tools/call", {"name": name, "arguments": args})["result"]

    @staticmethod
    def tools(mcp: McpHttpSession) -> list[str]:
        return sorted(t["name"] for t in mcp.request("tools/list")["result"]["tools"])

    def assertDenied(self, result: dict[str, Any], text: str) -> None:
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(result["content"][0]["text"], text)
        self.assertNotIn("structuredContent", result)

    def test_client_without_scopes_sees_no_tools_and_is_denied_every_tool(self):
        mcp = self.session("p01-expiry-test")                    # authenticates, but holds no scope (5 s token)
        self.assertEqual(self.tools(mcp), [])
        for name, args, scope in self.CALLS:
            with self.subTest(tool=name):
                self.assertDenied(self.call(mcp, name, args),
                                  f"Access denied: p01-expiry-test lacks scope {scope} for {name}.")

    def test_partly_scoped_client_sees_and_reaches_only_its_tool(self):
        mcp = self.session("p01-crm-only-test")
        self.assertEqual(self.tools(mcp), ["search_accounts"])
        self.assertFalse(self.call(mcp, "search_accounts", {"query": "Summit"}).get("isError", False))
        for name, args, scope in self.CALLS[1:]:
            with self.subTest(tool=name):
                self.assertDenied(self.call(mcp, name, args),
                                  f"Access denied: p01-crm-only-test lacks scope {scope} for {name}.")

    def test_rep_sees_only_read_tools_and_cannot_call_a_create_style_tool(self):
        mcp = self.session("cs-rep")
        self.assertEqual(self.tools(mcp), sorted(TOOLS))
        for hidden in ("create_case", "update_pricing"):
            self.assertNotIn(hidden, self.tools(mcp))
            self.assertTrue(self.call(mcp, hidden, {"account_id": "ACCT-1002"})["isError"], hidden)

    def test_rep_is_denied_accounts_outside_its_region_and_lead_is_not(self):
        out = "Access denied: this record belongs to an account outside your region (cs-rep may see: Central). Ask a CS lead."
        rep, lead = self.session("cs-rep"), self.session("cs-lead")
        self.assertDenied(self.call(rep, "get_open_invoices", {"account_id": "ACCT-1003"}), out)   # Pinecrest: West
        invoices = self.call(lead, "get_open_invoices", {"account_id": "ACCT-1003"})
        self.assertFalse(invoices.get("isError", False), invoices)
        self.assertEqual(invoices["structuredContent"]["account_id"], "ACCT-1003")
        if LIVE_CRM_ERP:
            return                                                # SO-104301 exists only in the seeded ERP
        for name in ("get_sales_order", "track_shipment"):
            with self.subTest(tool=name):
                self.assertDenied(self.call(rep, name, {"sales_order": "SO-104301"}), out)
                self.assertFalse(self.call(lead, name, {"sales_order": "SO-104301"}).get("isError", False))

    def test_rep_search_hides_accounts_outside_its_region(self):
        rep, lead = self.session("cs-rep"), self.session("cs-lead")
        mine = self.call(rep, "search_accounts", {"query": "Pinecrest"})["structuredContent"]
        self.assertEqual((mine["matches"], mine["hidden_out_of_region"]), ([], 1))
        full = self.call(lead, "search_accounts", {"query": "Pinecrest"})["structuredContent"]
        self.assertEqual([m["account_id"] for m in full["matches"]], ["ACCT-1003"])
        self.assertNotIn("hidden_out_of_region", full)
        summit = self.call(rep, "search_accounts", {"query": "Summit"})["structuredContent"]
        self.assertEqual([(m["account_id"], m["region"]) for m in summit["matches"]], [("ACCT-1002", "Central")])


if __name__ == "__main__":
    unittest.main()
