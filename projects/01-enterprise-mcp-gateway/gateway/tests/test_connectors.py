"""Connectors against fake HTTP backends (httpx2.MockTransport): request shape, auth, mapping, and
sanitized failures. No network; the fakes record every request so tests can assert on them."""
from __future__ import annotations

import json
import logging
import unittest
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import httpx2

from mock_systems import get_systems, reset_systems
from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import AuthRejected, ConnectorError, Forbidden, build_connectors, begin_upstream
from ridgeline_gateway.connectors.base import MAX_RESPONSE_BYTES, HttpConnector
from ridgeline_gateway.connectors.salesforce import (parse_description, render_description, soql_like, soql_literal,
                                                     sosl_term)
from ridgeline_gateway.connectors.sap import odata_date, to_ridgeline_id, to_sap_id

SF_LOGIN = "https://ridgeline-dev-ed.develop.my.salesforce.com"
SF_INSTANCE = "https://ridgeline-dev-ed.develop.my.salesforce.com"
SF_SECRET = "sf-client-secret-never-printed-01"
SAP_KEY = "sap-api-key-never-printed-0002"
CARRIER_KEY = "carrier-key-never-printed-0003"
AR_TOKEN = "ar-token-never-printed-000000004"
SECRETS = (SF_SECRET, SAP_KEY, CARRIER_KEY, AR_TOKEN)

LIVE_ENV = {"CRM_BACKEND": "salesforce", "SF_LOGIN_URL": SF_LOGIN, "SF_CLIENT_ID": "client-id-1",
            "SF_CLIENT_SECRET": SF_SECRET, "ERP_BACKEND": "sap_sandbox", "SAP_API_KEY": SAP_KEY,
            "CARRIER_BACKEND": "http", "CARRIER_API_URL": "http://carrier-api:8001", "CARRIER_API_KEY": CARRIER_KEY,
            "AR_BACKEND": "http", "AR_API_URL": "http://ar-api:8002", "AR_API_TOKEN": AR_TOKEN}

SF_RECORD = {"Id": "001A00000000001AAA", "AccountNumber": "ACCT-1002", "Name": "Summit Contractors LLC", "Site": "Central",
             "Description": render_description("Contractor", ["Summit"]),
             "Contacts": {"records": [{"Title": "Purchasing", "Email": "purchasing.summit@example.com"}]}}
SAP_ORDER = {"d": {"SalesOrder": "1", "SoldToParty": "17100001", "PurchaseOrderByCustomer": "PO-SAP-1",
                   "SalesOrderDate": "/Date(1478649600000)/", "RequestedDeliveryDate": "/Date(1479254400000+0000)/",
                   "TotalNetAmount": "52.65", "TransactionCurrency": "USD", "OverallSDProcessStatus": "A",
                   "OverallDeliveryStatus": "A",
                   "to_Item": {"results": [{"SalesOrderItem": "10", "Material": "TG11", "SalesOrderItemText": "Trading Good 11",
                                            "RequestedQuantity": "1", "RequestedQuantityUnit": "PC", "NetAmount": "52.65",
                                            "NetPriceAmount": "52.65", "NetPriceQuantity": "1"}]}}}


class FakeBackend:
    """Routes by (method, host, path) to a handler; records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]] = {}

    def on(self, method: str, url_prefix: str, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.routes[(method, url_prefix)] = handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        for (method, prefix), handler in sorted(self.routes.items(), key=lambda kv: -len(kv[0][1])):
            if request.method == method and url.startswith(prefix):
                return handler(request)
        return httpx2.Response(500, json={"error": "no fake route", "url": url})


def ok(body: Any, status: int = 200) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _r: httpx2.Response(status, json=body)


def sf_token(_r: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"access_token": "tok-1", "instance_url": SF_INSTANCE, "token_type": "Bearer"})


def connectors_for(fake: FakeBackend, **overrides: str):
    return build_connectors(load_settings({**LIVE_ENV, **overrides}), transport=httpx2.MockTransport(fake))


class Helpers(unittest.TestCase):
    def test_soql_and_sosl_escaping(self):
        self.assertEqual(soql_literal("O'Brien\\x"), "O\\'Brien\\\\x")
        self.assertEqual(soql_like("50%_off"), "50\\%\\_off")
        self.assertEqual(sosl_term("A&B (x)"), "A\\&B \\(x\\)")

    def test_description_round_trip(self):
        self.assertEqual(parse_description(render_description("Retail (EDI)", ["BBO", "Big Box"])),
                         {"type": "Retail (EDI)", "aliases": ["BBO", "Big Box"]})
        self.assertEqual(parse_description(None), {"type": None, "aliases": []})

    def test_sap_id_mapping_and_dates(self):
        self.assertEqual(to_sap_id("SO-000001"), "1")
        self.assertEqual(to_ridgeline_id("1"), "SO-000001")
        self.assertIsNone(to_ridgeline_id("1234567"))
        self.assertIsNone(to_ridgeline_id("ABC"))
        self.assertEqual(odata_date("/Date(1478649600000)/"), "2016-11-09")
        self.assertEqual(odata_date("/Date(1479254400000+0000)/"), "2016-11-16")
        self.assertIsNone(odata_date("2016-11-09"))


class SalesforceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = FakeBackend()
        self.fake.on("POST", f"{SF_LOGIN}/services/oauth2/token", sf_token)
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/search", ok({"searchRecords": [{"Id": "001A00000000001AAA"}]}))
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/query", ok({"records": [SF_RECORD]}))

    async def test_client_credentials_then_sosl_and_soql(self):
        matches = await connectors_for(self.fake).crm.find_accounts("BBO")
        self.assertEqual(matches, [{"account_id": "ACCT-1002", "name": "Summit Contractors LLC", "type": "Contractor",
                                    "region": "Central",
                                    "primary_contact": {"role": "Purchasing", "email": "purchasing.summit@example.com"}}])
        token_req, search_req, query_req = self.fake.requests
        form = parse_qs(token_req.content.decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["client_secret"], [SF_SECRET])
        self.assertEqual(search_req.headers["Authorization"], "Bearer tok-1")
        self.assertIn("FIND {BBO} IN ALL FIELDS", parse_qs(urlsplit(str(search_req.url)).query)["q"][0])
        soql = parse_qs(urlsplit(str(query_req.url)).query)["q"][0]
        self.assertIn("AccountNumber LIKE 'ACCT-%'", soql)
        self.assertIn("Id IN ('001A00000000001AAA')", soql)

    async def test_query_text_is_escaped(self):
        await connectors_for(self.fake).crm.find_accounts("O'Brien & Sons")
        sosl = parse_qs(urlsplit(str(self.fake.requests[1].url)).query)["q"][0]
        soql = parse_qs(urlsplit(str(self.fake.requests[2].url)).query)["q"][0]
        self.assertIn("FIND {O\\'Brien \\& Sons}", sosl)
        self.assertIn("Name LIKE '%O\\'Brien & Sons%'", soql)

    async def test_token_is_reused_then_refreshed_once_on_401(self):
        crm = connectors_for(self.fake).crm
        await crm.find_accounts("Summit")
        self.assertEqual(sum(r.url.path.endswith("/token") for r in self.fake.requests), 1)
        calls = {"n": 0}

        def expire_once(_r: httpx2.Request) -> httpx2.Response:
            calls["n"] += 1
            return httpx2.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}]) if calls["n"] == 1 \
                else httpx2.Response(200, json={"searchRecords": []})
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/search", expire_once)
        await crm.find_accounts("Summit")
        self.assertEqual(sum(r.url.path.endswith("/token") for r in self.fake.requests), 2)

    async def test_persistent_401_is_auth_rejected_without_secrets(self):
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/search", ok([{"errorCode": "INVALID_SESSION_ID"}], 401))
        with self.assertRaises(AuthRejected) as ctx:
            await connectors_for(self.fake).crm.find_accounts("Summit")
        self.assertEqual(str(ctx.exception), "Salesforce rejected the gateway's credentials.")

    async def test_403_is_forbidden_and_not_retried(self):
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/search", ok([{"errorCode": "REQUEST_LIMIT_EXCEEDED"}], 403))
        with self.assertRaises(Forbidden) as ctx:
            await connectors_for(self.fake).crm.find_accounts("Summit")
        self.assertIn("refused the request", str(ctx.exception))
        self.assertEqual(sum(r.url.path.endswith("/token") for r in self.fake.requests), 1)

    async def test_short_terms_skip_sosl_and_sosl_400_still_searches_by_name(self):
        await connectors_for(self.fake).crm.find_accounts("A.")
        self.assertFalse(any(r.url.path.endswith("/search") for r in self.fake.requests))
        self.fake.requests.clear()
        self.fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/search", ok([{"errorCode": "INVALID_SEARCH"}], 400))
        matches = await connectors_for(self.fake).crm.find_accounts("Summit")
        self.assertEqual([m["account_id"] for m in matches], ["ACCT-1002"])

    async def test_gateway_crm_connector_is_read_only(self):
        crm = connectors_for(self.fake).crm
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.assertRaisesRegex(ConnectorError, f"does not send {method} requests"):
                await crm.call(method, "sobjects/Account", json_body={"Name": "x"})
        self.assertFalse(any(r.method in ("PATCH", "PUT", "DELETE") for r in self.fake.requests))

    async def test_token_for_an_untrusted_instance_is_refused(self):
        self.fake.on("POST", f"{SF_LOGIN}/services/oauth2/token",
                     ok({"access_token": "tok-1", "instance_url": "https://attacker.example"}))
        with self.assertRaisesRegex(ConnectorError, "unusable token response"):
            await connectors_for(self.fake).crm.find_accounts("Summit")
        self.assertFalse(any(r.url.host == "attacker.example" for r in self.fake.requests))

    async def test_bad_client_credentials(self):
        self.fake.on("POST", f"{SF_LOGIN}/services/oauth2/token", ok({"error": "invalid_client"}, 400))
        with self.assertRaisesRegex(ConnectorError, r"Salesforce returned an error \(HTTP 400\)"):
            await connectors_for(self.fake).crm.find_accounts("Summit")


class SapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = FakeBackend()
        self.base = "https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV"
        self.fake.on("GET", f"{self.base}/A_SalesOrder('1')", ok(SAP_ORDER))
        self.fake.on("GET", f"{self.base}/A_SalesOrder('2')", ok({"error": {"code": "404"}}, 404))
        self.fake.on("GET", f"{self.base}/A_SalesOrder?", ok({"d": {"results": [{"SalesOrder": "1"},
                                                                                {"SalesOrder": "12345678"}]}}))

    async def test_order_has_the_seed_key_set_plus_source(self):
        order = await connectors_for(self.fake).erp.fetch_order("SO-000001")
        seed = get_systems().erp.order("SO-104233")
        self.assertEqual(set(order) - {"source"}, set(seed))
        self.assertEqual(set(order["lines"][0]), set(seed["lines"][0]))
        self.assertEqual(order["promised_delivery"], "2016-11-16")
        self.assertEqual(order["source"]["sold_to_party"], "17100001")
        self.assertEqual(order["source"]["overall_delivery_status"], "Not delivered")

    async def test_order_is_mapped_with_api_key_header(self):
        order = await connectors_for(self.fake).erp.fetch_order("SO-000001")
        req = self.fake.requests[0]
        self.assertEqual(req.headers["APIKey"], SAP_KEY)
        self.assertEqual(parse_qs(urlsplit(str(req.url)).query), {"$expand": ["to_Item"], "$format": ["json"]})
        self.assertEqual(order["sales_order"], "SO-000001")
        self.assertEqual(order["source"]["system"], "sap_sandbox")
        self.assertIsNone(order["account_id"])
        self.assertEqual((order["order_date"], order["status"]), ("2016-11-09", "Open"))
        self.assertEqual(order["lines"], [{"line": 10, "sku": "TG11", "description": "Trading Good 11", "qty": 1.0,
                                           "unit_price": 52.65, "amount": 52.65}])
        self.assertEqual(order["order_total"], 52.65)

    async def test_unknown_order_is_none(self):
        self.assertIsNone(await connectors_for(self.fake).erp.fetch_order("SO-000002"))

    async def test_payload_for_a_different_order_is_rejected(self):
        self.fake.on("GET", f"{self.base}/A_SalesOrder('3')", ok(SAP_ORDER))
        with self.assertRaisesRegex(ConnectorError, "unexpected payload"):
            await connectors_for(self.fake).erp.fetch_order("SO-000003")

    async def test_sample_ids_skip_unaddressable_orders(self):
        self.assertEqual(await connectors_for(self.fake).erp.sample_order_ids(), ["SO-000001"])


class CarrierArHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_systems()
        self.fake = FakeBackend()
        self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-104233/tracking",
                     ok(get_systems().carrier.tracking("SO-104233")))
        self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-999999/tracking", ok({"detail": "x"}, 404))
        self.fake.on("GET", "http://ar-api:8002/v1/accounts/ACCT-1002/open-items",
                     ok({"account_id": "ACCT-1002", "invoices": get_systems().ar.open_items("ACCT-1002")}))

    async def test_carrier_sends_api_key_and_returns_seed_shape(self):
        data = await connectors_for(self.fake).carrier.fetch_tracking("SO-104233")
        self.assertEqual(self.fake.requests[0].headers["X-API-Key"], CARRIER_KEY)
        self.assertEqual(data, get_systems().carrier.tracking("SO-104233"))
        self.assertIsNone(await connectors_for(self.fake).carrier.fetch_tracking("SO-999999"))

    async def test_ar_sends_bearer_token(self):
        items = await connectors_for(self.fake).ar.fetch_open_items("ACCT-1002")
        self.assertEqual(self.fake.requests[0].headers["Authorization"], f"Bearer {AR_TOKEN}")
        self.assertEqual(items, get_systems().ar.open_items("ACCT-1002"))

    async def test_failures_are_sanitized(self):
        cases: list[tuple[Callable[[httpx2.Request], httpx2.Response], str]] = [
            (ok({"detail": "bad key"}, 401), "Carrier API rejected the gateway's credentials."),
            (ok({"detail": "slow down"}, 429), "Carrier API is rate limiting the gateway; try again later."),
            (ok({"trace": "stack at http://internal.example"}, 500), "Carrier API returned an error (HTTP 500)."),
            (lambda r: httpx2.Response(200, content=b"<html>not json"), "Carrier API returned a response the gateway could not read."),
            (ok({"sales_order": "SO-000000", "shipments": []}), "Carrier API returned an unexpected payload."),
        ]
        for handler, message in cases:
            with self.subTest(message=message):
                self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-104233/tracking", handler)
                with self.assertRaises(ConnectorError) as ctx:
                    await connectors_for(self.fake).carrier.fetch_tracking("SO-104233")
                self.assertEqual(str(ctx.exception), message)

    async def test_malformed_ar_items_are_rejected(self):
        for bad in ([{"invoice": "INV-1", "account_id": "ACCT-1002"}], [{"account_id": "ACCT-1002", "amount": "12"}],
                    [{"account_id": "ACCT-9999", "amount": 1.0}], [{"account_id": "ACCT-1002", "amount": True}], ["x"],
                    '[{"account_id": "ACCT-1002", "amount": NaN}]', '[{"account_id": "ACCT-1002", "amount": Infinity}]'):
            # NaN/Infinity are not valid JSON, but parsers accept them, so the fake sends them raw.
            raw = (f'{{"account_id": "ACCT-1002", "invoices": {bad}}}' if isinstance(bad, str)
                   else json.dumps({"account_id": "ACCT-1002", "invoices": bad}))
            self.fake.on("GET", "http://ar-api:8002/v1/accounts/ACCT-1002/open-items",
                         lambda r, raw=raw: httpx2.Response(200, content=raw.encode(),
                                                            headers={"Content-Type": "application/json"}))
            with self.assertRaisesRegex(ConnectorError, "AR API returned an unexpected payload."):
                await connectors_for(self.fake).ar.fetch_open_items("ACCT-1002")

    async def test_oversized_responses_are_rejected_while_streaming(self):
        chunks_sent = {"n": 0}

        async def endless():
            chunk = b"a" * 65536
            for _ in range(MAX_RESPONSE_BYTES // len(chunk) + 50):
                chunks_sent["n"] += 1
                yield chunk

        cases = {
            "declared": lambda r: httpx2.Response(200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
                                                  content=b"{}"),
            "no content-length": lambda r: httpx2.Response(200, content=endless()),
        }
        for name, handler in cases.items():
            with self.subTest(name):
                self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-104233/tracking", handler)
                with self.assertRaisesRegex(ConnectorError, "larger than the gateway accepts"):
                    await connectors_for(self.fake).carrier.fetch_tracking("SO-104233")
        # stopped soon after the cap instead of reading the whole stream
        self.assertLessEqual(chunks_sent["n"], MAX_RESPONSE_BYTES // 65536 + 2)

    async def test_gzip_responses_are_decoded(self):
        import gzip
        body = json.dumps(get_systems().carrier.tracking("SO-104233")).encode()
        self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-104233/tracking",
                     lambda r: httpx2.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
                                               content=gzip.compress(body)))
        self.assertEqual(await connectors_for(self.fake).carrier.fetch_tracking("SO-104233"),
                         get_systems().carrier.tracking("SO-104233"))

    async def test_other_ports_on_the_same_host_are_refused(self):
        http = HttpConnector("Carrier API", "carrier_api", "http://carrier-api:8001", transport=httpx2.MockTransport(self.fake))
        with self.assertRaisesRegex(ConnectorError, "outside the connector's allowlist"):
            await http.request("GET", "http://carrier-api:9999/admin")
        self.assertEqual(self.fake.requests, [])

    async def test_timeouts_and_connection_errors_are_sanitized(self):
        def timeout(r: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out talking to http://ar-api:8002 with token", request=r)

        def refused(r: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError(f"refused {AR_TOKEN}", request=r)
        for handler, message in ((timeout, "AR API did not respond in time."), (refused, "AR API is unreachable.")):
            self.fake.on("GET", "http://ar-api:8002/v1/accounts/ACCT-1002/open-items", handler)
            with self.assertRaises(ConnectorError) as ctx:
                await connectors_for(self.fake).ar.fetch_open_items("ACCT-1002")
            self.assertEqual(str(ctx.exception), message)
            self.assertIsNone(ctx.exception.__cause__)

    async def test_upstream_info_is_recorded(self):
        info = begin_upstream()
        await connectors_for(self.fake).ar.fetch_open_items("ACCT-1002")
        self.assertEqual((info.backend, info.status, info.calls), ("ar_api", 200, 1))

    async def test_redirects_are_not_followed(self):
        self.fake.on("GET", "http://carrier-api:8001/v1/orders/SO-104233/tracking",
                     lambda r: httpx2.Response(302, headers={"Location": "http://attacker.example/steal"}))
        with self.assertRaisesRegex(ConnectorError, r"HTTP 302"):
            await connectors_for(self.fake).carrier.fetch_tracking("SO-104233")
        self.assertEqual(len(self.fake.requests), 1)


class NoSecretsInLogsTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_activity_logs_no_secrets(self):
        fake = FakeBackend()
        fake.on("POST", f"{SF_LOGIN}/services/oauth2/token", sf_token)
        fake.on("GET", f"{SF_INSTANCE}/services/data/v62.0/", ok({"records": [], "searchRecords": []}))
        fake.on("GET", "http://ar-api:8002/", ok({"detail": "x"}, 401))
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            with self.assertLogs(level=logging.DEBUG) as cm:
                logging.getLogger("probe").debug("start")
                c = connectors_for(fake)
                await c.crm.find_accounts("Summit")
                with self.assertRaises(ConnectorError):
                    await c.ar.fetch_open_items("ACCT-1002")
        finally:
            root.setLevel(old)
        text = "\n".join(r.getMessage() for r in cm.records)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
