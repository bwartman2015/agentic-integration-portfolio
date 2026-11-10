"""Operator tools: seed_salesforce (the only code that writes to an external system) and live_check.
All against a fake Salesforce (httpx2.MockTransport) that keeps an in-memory org and records requests."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx2

from mock_systems import reset_systems
from ridgeline_gateway.admin import live_check, seed_salesforce
from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import ConnectorError

LOGIN = "https://ridgeline-dev-ed.develop.my.salesforce.com"
ENV = {"SF_LOGIN_URL": LOGIN, "SF_CLIENT_ID": "client-id-1", "SF_CLIENT_SECRET": "sf-secret-never-printed-0001"}


class FakeOrg:
    """Just enough of the Salesforce REST API for the seeder: token, SOQL by AccountNumber/Contact, POST, PATCH."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.contacts: list[dict[str, Any]] = []
        self.requests: list[httpx2.Request] = []
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:015d}"

    def add_account(self, number: str, description: str) -> str:
        sf_id = self._id("001")
        self.accounts[sf_id] = {"Id": sf_id, "AccountNumber": number, "Description": description, "Name": "Someone else"}
        return sf_id

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        path = req.url.path
        if path.endswith("/services/oauth2/token"):
            return httpx2.Response(200, json={"access_token": "tok", "instance_url": LOGIN})
        if req.method == "GET" and path.endswith("/query"):
            q = parse_qs(urlsplit(str(req.url)).query)["q"][0]
            if "FROM Account" in q:
                number = q.split("AccountNumber = '")[1].split("'")[0]
                recs = [{"Id": a["Id"], "Description": a["Description"]} for a in self.accounts.values()
                        if a["AccountNumber"] == number]
            else:
                acct = q.split("AccountId = '")[1].split("'")[0]
                email = q.split("Email = '")[1].split("'")[0]
                recs = [{"Id": c["Id"]} for c in self.contacts if c["AccountId"] == acct and c["Email"] == email]
            return httpx2.Response(200, json={"totalSize": len(recs), "done": True, "records": recs})
        if req.method == "POST" and path.endswith("/sobjects/Account"):
            body = json.loads(req.content)
            sf_id = self._id("001")
            self.accounts[sf_id] = {"Id": sf_id, **body}
            return httpx2.Response(201, json={"id": sf_id, "success": True, "errors": []})
        if req.method == "POST" and path.endswith("/sobjects/Contact"):
            body = json.loads(req.content)
            self.contacts.append({"Id": self._id("003"), **body})
            return httpx2.Response(201, json={"id": self.contacts[-1]["Id"], "success": True, "errors": []})
        if req.method == "PATCH" and "/sobjects/Account/" in path:
            self.accounts[path.rsplit("/", 1)[1]].update(json.loads(req.content))
            return httpx2.Response(204)
        return httpx2.Response(500, json={"unexpected": f"{req.method} {path}"})


class SeedSalesforceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_systems()
        self.org = FakeOrg()
        self.sf = seed_salesforce.salesforce_from(load_settings(ENV), httpx2.MockTransport(self.org))

    def methods(self) -> set[str]:
        return {r.method for r in self.org.requests if not r.url.path.endswith("/token")}

    async def test_dry_run_only_reads(self):
        actions = await seed_salesforce.seed(self.sf, apply=False)
        self.assertEqual(self.methods(), {"GET"})
        self.assertEqual(sum(a.startswith("CREATE ACCT") for a in actions), 8)
        self.assertEqual(self.org.accounts, {})

    async def test_apply_creates_synthetic_accounts_and_contacts_then_is_idempotent(self):
        await seed_salesforce.seed(self.sf, apply=True)
        self.assertEqual(len(self.org.accounts), 8)
        self.assertEqual(len(self.org.contacts), 8)
        self.assertTrue(all(c["Email"].endswith("@example.com") for c in self.org.contacts))
        self.assertTrue(all(a["Description"].startswith("Ridgeline synthetic account (fictional)")
                            for a in self.org.accounts.values()))
        self.org.requests.clear()
        actions = await seed_salesforce.seed(self.sf, apply=True)
        self.assertEqual((len(self.org.accounts), len(self.org.contacts)), (8, 8), "second run created records")
        self.assertFalse(any(a.startswith("CREATE") for a in actions))
        self.assertLessEqual(self.methods(), {"GET", "PATCH"})

    async def test_never_deletes_or_puts(self):
        await seed_salesforce.seed(self.sf, apply=True)
        await seed_salesforce.seed(self.sf, apply=True)
        self.assertFalse({"DELETE", "PUT"} & {r.method for r in self.org.requests})

    async def test_accounts_it_did_not_create_are_left_alone(self):
        foreign = self.org.add_account("ACCT-1002", "Imported from another system")
        actions = await seed_salesforce.seed(self.sf, apply=True)
        self.assertIn("SKIP   ACCT-1002: an account with this number exists but was not created by the seeder; "
                      "left unchanged", actions)
        self.assertEqual(self.org.accounts[foreign]["Name"], "Someone else")
        self.assertFalse(any(r.method == "PATCH" and r.url.path.endswith(foreign) for r in self.org.requests))

    async def test_duplicate_account_numbers_are_skipped(self):
        self.org.add_account("ACCT-1003", "Ridgeline synthetic account (fictional)\nType: x\nAliases: ")
        self.org.add_account("ACCT-1003", "Ridgeline synthetic account (fictional)\nType: x\nAliases: ")
        actions = await seed_salesforce.seed(self.sf, apply=True)
        self.assertTrue(any(a.startswith("SKIP   ACCT-1003: more than one account") for a in actions))
        self.assertFalse(any(r.method == "PATCH" for r in self.org.requests))

    async def test_non_synthetic_data_aborts_before_any_request(self):
        for bad in ({"account_id": "CUST-1", "name": "X", "type": "t", "region": "r", "aliases": [],
                     "primary_contact": {"role": "r", "email": "a@example.com"}},
                    {"account_id": "ACCT-0001", "name": "X", "type": "t", "region": "r", "aliases": [],
                     "primary_contact": {"role": "r", "email": "real.person@gmail.example.org"}}):
            with self.assertRaises(seed_salesforce.UnsafeData):
                await seed_salesforce.seed(self.sf, apply=True, accounts=[bad])
        self.assertEqual(self.org.requests, [])

    async def test_bad_record_ids_from_salesforce_are_refused(self):
        self.org.add_account("ACCT-1001", "Ridgeline synthetic account (fictional)")
        next(iter(self.org.accounts.values()))["Id"] = "../../sobjects/User/005"
        with self.assertRaisesRegex(ConnectorError, "unexpected record id"):
            await seed_salesforce.seed(self.sf, apply=True)
        self.assertFalse(any(r.method == "PATCH" for r in self.org.requests))

    def test_partial_apply_failure_still_reports_what_was_written(self):
        calls = {"n": 0}
        real = self.org.__call__

        def fail_on_third_post(req: httpx2.Request) -> httpx2.Response:
            if req.method == "POST" and req.url.path.endswith("/sobjects/Account"):
                calls["n"] += 1
                if calls["n"] == 3:
                    return httpx2.Response(500, json=[{"errorCode": "UNKNOWN_EXCEPTION"}])
            return real(req)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", ENV, clear=True), redirect_stdout(out), mock.patch("sys.stderr", err):
            code = seed_salesforce.main(["--apply"], transport=httpx2.MockTransport(fail_on_third_post))
        self.assertEqual(code, 1)
        self.assertIn("CREATE ACCT-1001", out.getvalue())
        self.assertIn("CREATE ACCT-1002", out.getvalue())
        self.assertIn("stopped after", out.getvalue())

    def test_missing_settings_exit_cleanly_without_secrets(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"SF_CLIENT_SECRET": "sf-secret-never-printed-0001"}, clear=True), \
                redirect_stdout(out), mock.patch("sys.stderr", err):
            code = seed_salesforce.main([])
        self.assertEqual(code, 2)
        self.assertIn("SF_LOGIN_URL", err.getvalue())
        self.assertNotIn("sf-secret-never-printed-0001", out.getvalue() + err.getvalue())


class LiveCheckSummaryTests(unittest.TestCase):
    def test_summaries_require_data(self):
        self.assertEqual(live_check.summarize("search_accounts", {"matches": []})[0], False)
        ok, text = live_check.summarize("search_accounts", {"matches": [{"name": "Summit Contractors LLC",
                                                                          "account_id": "ACCT-1002"}]})
        self.assertTrue(ok)
        self.assertIn("Summit Contractors LLC (ACCT-1002)", text)
        self.assertTrue(live_check.summarize("get_sales_order", {"sales_order": "SO-000001", "status": "Open"})[0])
        self.assertFalse(live_check.summarize("get_sales_order", {})[0])
        self.assertTrue(live_check.summarize("track_shipment", {"shipments": []})[0])
        self.assertTrue(live_check.summarize("get_open_invoices", {"invoices": [], "count": 0, "total_open": 0.0})[0])


class LiveCheckPlanTests(unittest.TestCase):
    def test_rep_makes_every_call_on_ridgeline_data(self):
        calls = live_check.plan_calls({"crm": "salesforce", "erp": "seed", "carrier": "carrier_api", "ar": "ar_api"},
                                      "cs-rep", "SO-000001")
        self.assertEqual({who for _, _, who in calls}, {"cs-rep"})
        self.assertEqual(calls[1][1], {"sales_order": "SO-104233"})

    def test_sap_sandbox_orders_have_no_region_so_a_lead_reads_them(self):
        calls = live_check.plan_calls({"crm": "seed", "erp": "sap_sandbox", "carrier": "carrier_api", "ar": "ar_api"},
                                      "cs-rep", "SO-000007")
        self.assertEqual([(tool, who) for tool, _, who in calls],
                         [("search_accounts", "cs-rep"), ("get_sales_order", "cs-lead"),
                          ("track_shipment", "cs-lead"), ("get_open_invoices", "cs-rep")])
        self.assertEqual(calls[1][1], {"sales_order": "SO-000007"})


class TokenToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_credentials_request_and_shape_check(self):
        from ridgeline_gateway.admin.token import fetch_token
        seen: list[httpx2.Request] = []

        def kc(req: httpx2.Request) -> httpx2.Response:
            seen.append(req)
            return httpx2.Response(200, json={"access_token": "aaa.bbb.ccc", "expires_in": 300})
        url = "http://keycloak:8080/realms/ridgeline/protocol/openid-connect/token"
        self.assertEqual(await fetch_token("cs-rep", "s3cret-value-0001", url, transport=httpx2.MockTransport(kc)),
                         "aaa.bbb.ccc")
        self.assertEqual(seen[0].method, "POST")
        self.assertEqual(seen[0].headers["Authorization"], "Basic Y3MtcmVwOnMzY3JldC12YWx1ZS0wMDAx")
        self.assertEqual(seen[0].content, b"grant_type=client_credentials")

    async def test_unusable_token_response_is_an_error(self):
        from ridgeline_gateway.admin.token import fetch_token
        url = "http://keycloak:8080/realms/ridgeline/protocol/openid-connect/token"
        for body in ({"error": "unauthorized_client"}, {"access_token": "not a jwt"}):
            with self.assertRaises(ConnectorError):
                await fetch_token("cs-rep", "x", url, transport=httpx2.MockTransport(lambda r, b=body: httpx2.Response(200, json=b)))


if __name__ == "__main__":
    unittest.main()
