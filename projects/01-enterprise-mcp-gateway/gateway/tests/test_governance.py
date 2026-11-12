"""Milestone 5: audit, rate limits, PII masking and human approval of the write tool.

In-process against the real OPA (opa_fixture). n8n is an httpx2 mock transport: the tests play the
workflow (acknowledge with a form link) and the human (call the gateway's callback route).
Run inside the container: `make gateway-test P=01`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
from typing import Any
from unittest import mock

import httpx2
from mcp import Client
from starlette.testclient import TestClient

from mock_systems import get_systems, reset_systems
from opa_fixture import LEAD, REP, act_as, use_policy
from ridgeline_gateway import pii, server
from ridgeline_gateway.approvals import (ApprovalConfig, ApprovalConfigError, NoApprover, build_approver)
from ridgeline_gateway.audit import (AUDIT_COLUMNS, INSERT_AUDIT, AuditConfig, AuditConfigError, AuditUnavailable,
                                     PostgresAudit, UnavailableAudit)
from ridgeline_gateway.auth import build_verifier
from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import build_connectors
from ridgeline_gateway.policy import (AUDIT_SCOPE, GATEWAY_REASONS, PII_SCOPE, POLICY_REASONS, Decision,
                                      denial_message, load_policy_data)
from ridgeline_gateway.ratelimit import RateLimitConfig, RateLimitConfigError, RateLimiter
from ridgeline_gateway.server import configure_logging, mcp, set_connectors, transport_security
from test_auth import CONFIG, FakeKeycloak, reset_auth

configure_logging()
OPERATOR_TOKEN = "operator-" + "x" * 40
WEBHOOK_SECRET = "webhook" + "w" * 40
FORM_BASE = "http://localhost:5678"
CASE = {"account_id": "ACCT-1002", "subject": "Late delivery SO-104233",
        "summary": "Dock closed on 2026-11-05; new ETA 2026-11-10. Contact purchasing.summit@example.com"}


class FakeN8n:
    """The approval workflow's first two nodes: take the request, answer with the form link."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[str | None] = []      # the shared secret each request carried
        self.reply: Any = None                     # override the acknowledgement
        self.status = 200
        self.down = False
        self.unsigned = False                      # answer with a form link that has no signature

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("refused", request=req)
        body = json.loads(req.content)
        self.requests.append(body)
        self.headers.append(req.headers.get("x-ridgeline-approval"))
        link = f"{FORM_BASE}/form-waiting/1" + ("" if self.unsigned else "?signature=abc")
        reply = self.reply if self.reply is not None else {
            "received": True, "request_id": body["request_id"], "approval_url": link}
        return httpx2.Response(self.status, json=reply)


class _Governed(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_systems()
        set_connectors(build_connectors(load_settings({})))
        use_policy(self, principal=None)
        self.n8n = FakeN8n()

    def use_approver(self, timeout_s: float = 5.0):
        config = ApprovalConfig("http://n8n:5678/webhook/ridgeline-approval", OPERATOR_TOKEN, timeout_s, FORM_BASE, WEBHOOK_SECRET)
        approver = build_approver(config, self.audit, transport=httpx2.MockTransport(self.n8n))
        server.set_approver(approver)
        return approver

    async def call(self, name: str, args: dict):
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    async def logged(self, name: str, args: dict) -> tuple[Any, dict]:
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            result = await self.call(name, args)
        calls = [r for r in (json.loads(x.getMessage()) for x in cm.records) if r["event"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        return result, calls[0]

    async def decide_when_asked(self, approver, decision: str = "Approve", name: str = "Dana Lead",
                                token: str | None = None) -> bool:
        """The human: wait until the request reached n8n, then answer through the callback."""
        for _ in range(200):
            if self.n8n.requests and approver.pending():
                break
            await asyncio.sleep(0.01)
        req = self.n8n.requests[-1]
        return approver.resolve(req["request_id"], token if token is not None else req["callback_token"], decision, name)


class AuditTests(_Governed):
    async def test_every_outcome_is_audited_with_its_reason(self):
        act_as(self, REP)
        await self.call("get_sales_order", {"sales_order": "SO-104233"})                 # ok
        await self.call("get_open_invoices", {"account_id": "ACCT-1003"})                # out of region
        await self.call("create_case", CASE)                                             # missing scope
        await self.call("get_sales_order", {"sales_order": "SO-999999"})                 # not found
        await self.call("get_sales_order", {"sales_order": "nope"})                      # invalid arguments
        await self.call("drop_tables", {"secret": "hunter2"})                            # unknown tool
        rows = self.audit.rows
        self.assertEqual([(r["tool"], r["outcome"], r["reason"]) for r in rows], [
            ("get_sales_order", "ok", None), ("get_open_invoices", "denied", "out_of_region"),
            ("create_case", "denied", "missing_scope"), ("get_sales_order", "not_found", None),
            ("get_sales_order", "invalid_arguments", None), ("<unknown>", "unknown_tool", None)])
        for row in rows:
            self.assertEqual((row["client_id"], row["regions"]), ("cs-rep", ["Central"]))
            self.assertEqual(row["scopes"], REP["scopes"])
        self.assertEqual(rows[0]["args"], {"sales_order": "SO-104233"})
        self.assertEqual(rows[-1]["args"], {}, "an unknown tool's arguments are caller text: not stored")
        self.assertNotIn("hunter2", json.dumps(rows))

    async def test_argument_values_are_stored_masked_and_bounded(self):
        act_as(self, REP)
        await self.call("create_case", {**CASE, "summary": "Call 555-555-0142 or mail jo@example.com " + "x" * 400})
        args = self.audit.rows[-1]["args"]
        self.assertEqual(args["account_id"], "ACCT-1002")
        self.assertIn("[PHONE REDACTED]", args["summary"])
        self.assertIn("[EMAIL REDACTED]", args["summary"])
        self.assertNotIn("jo@example.com", json.dumps(args))
        self.assertLessEqual(len(args["summary"]), pii.MAX_AUDIT_VALUE_CHARS + 3)

    async def test_a_read_that_cannot_be_audited_is_not_returned(self):
        act_as(self, LEAD)
        self.audit.fail = True
        result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].text,
                         "Access denied: this call could not be recorded in the audit log, so it was refused.")
        self.assertNotIn("INV-90388", str(result.content))
        self.assertEqual((rec["outcome"], rec["policy_reason"], rec["audited"]), ("denied", "audit_unavailable", False))

    async def test_a_denial_stays_a_denial_when_audit_is_down(self):
        act_as(self, REP)
        self.audit.fail = True
        result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1003"})
        self.assertIn("outside your region", result.content[0].text)
        self.assertEqual((rec["policy_reason"], rec["audited"]), ("out_of_region", False))

    async def test_without_a_configured_store_nothing_is_returned(self):
        act_as(self, LEAD)
        server.set_audit(None)
        self.assertIsInstance(server.get_audit(), UnavailableAudit)
        self.assertTrue((await self.call("search_accounts", {"query": "Summit"})).is_error)

    def test_database_url_selects_a_hosted_store_and_requires_tls(self):
        secret = "s" * 24
        neon = f"postgresql://gateway_audit:{secret}@ep-test-endpoint-pooler.us-east-2.db.example/neondb"
        local = {"AUDIT_DB_HOST": "db", "AUDIT_DB_PASSWORD": "p" * 32}
        self.assertEqual(AuditConfig.from_env(local).source, "local")
        self.assertEqual(AuditConfig.from_env({**local, "DATABASE_URL": "  "}).source, "local")
        cfg = AuditConfig.from_env({**local, "DATABASE_URL": neon + "?sslmode=require&channel_binding=require"})
        self.assertEqual((cfg.source, cfg.host), ("database_url", "*.db.example"))
        self.assertEqual(cfg.dsn, neon + "?sslmode=require")        # asyncpg cannot take channel_binding
        self.assertNotIn(secret, repr(cfg))
        self.assertNotIn("ep-test-endpoint", repr(cfg) + cfg.host)
        verified = AuditConfig.from_env({"DATABASE_URL": neon + "?sslmode=verify-full&channel_binding=require"})
        self.assertEqual(verified.dsn, neon + "?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fcerts%2Fca-certificates.crt")
        own_ca = AuditConfig.from_env({"DATABASE_URL": neon + "?sslmode=verify-ca&sslrootcert=/run/ca.pem"})
        self.assertEqual(own_ca.dsn, neon + "?sslmode=verify-ca&sslrootcert=%2Frun%2Fca.pem")
        import os
        self.assertTrue(os.path.isfile("/etc/ssl/certs/ca-certificates.crt"), "the image must ship a CA bundle")
        # A database on this machine or the Compose network may skip TLS; anything else may not.
        self.assertEqual(AuditConfig.from_env({"DATABASE_URL": f"postgresql://u:{secret}@db:5432/ridgeline"}).host, "db")
        for bad in (neon, neon + "?sslmode=disable", neon + "?sslmode=prefer", "mysql://u:x@db.example/x?sslmode=require",
                    f"postgresql://{secret}@/neondb", "postgresql://u@db.example?sslmode=require", "postgresql://[::1"):
            with self.subTest(url=bad[:30]), self.assertRaises(AuditConfigError) as ctx:
                AuditConfig.from_env({**local, "DATABASE_URL": bad})
            self.assertNotIn(secret, str(ctx.exception))
            self.assertNotIn("db.example", str(ctx.exception))

    async def test_hosted_store_failure_also_fails_closed(self):
        secret = "s" * 24
        sink = PostgresAudit(AuditConfig.from_env({"DATABASE_URL": f"postgresql://u:{secret}@127.0.0.1:1/audit",
                                                   "AUDIT_TIMEOUT_S": "0.3"}))
        with self.assertRaises(AuditUnavailable) as ctx:
            await sink.write({"tool": "get_sales_order", "outcome": "ok"})
        self.assertNotIn(secret, str(ctx.exception))

    def test_config_validation(self):
        good = {"AUDIT_DB_HOST": "db", "AUDIT_DB_PASSWORD": "p" * 32}
        cfg = AuditConfig.from_env(good)
        self.assertEqual((cfg.host, cfg.database, cfg.user, cfg.port), ("db", "ridgeline", "gateway_audit", 5432))
        self.assertNotIn("p" * 32, repr(cfg))
        for bad in ({}, {**good, "AUDIT_DB_PASSWORD": "short"}, {**good, "AUDIT_DB_HOST": "db/evil"},
                    {**good, "AUDIT_DB_USER": "Robert'); DROP"}, {**good, "AUDIT_DB_PORT": "0"},
                    {**good, "AUDIT_TIMEOUT_S": "60"}):
            with self.assertRaises(AuditConfigError, msg=bad):
                AuditConfig.from_env(bad)

    async def test_postgres_sink_is_parameterized_and_fails_closed(self):
        self.assertEqual(INSERT_AUDIT.count("$"), len(AUDIT_COLUMNS))
        self.assertNotIn("%", INSERT_AUDIT)
        sink = PostgresAudit(AuditConfig("127.0.0.1", "audit", "gateway_audit", "p" * 32, 1, 0.3))
        with self.assertRaises(AuditUnavailable):                     # nothing listens on port 1
            await sink.write({"tool": "get_sales_order", "outcome": "ok"})
        with self.assertRaises(AuditUnavailable):
            await sink.report()


class CancelledCallTests(_Governed):
    """Every call is audited, including one the client cancels (MCP notifications/cancelled or a dropped
    connection). Found by the Milestone 5 architecture review."""

    async def test_a_cancelled_read_is_audited_as_cancelled(self):
        act_as(self, LEAD)
        started = asyncio.Event()

        async def slow(_query: str):
            started.set()
            await asyncio.sleep(30)
            return []
        with mock.patch.object(server.get_connectors().crm, "find_accounts", side_effect=slow):
            with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
                task = asyncio.create_task(server.mcp.call_tool("search_accounts", {"query": "Summit"}))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit.rows], [("search_accounts", "cancelled")])
        self.assertEqual(json.loads(cm.records[-1].getMessage())["outcome"], "cancelled")

    async def test_a_create_case_cancelled_while_waiting_is_traceable(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        task = asyncio.create_task(server.mcp.call_tool("create_case", CASE))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        request_id = self.n8n.requests[0]["request_id"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.audit.rows[-1]
        self.assertEqual((row["tool"], row["outcome"], row["approval_id"]), ("create_case", "cancelled", request_id))
        self.assertEqual(self.audit.approvals[request_id]["status"], "timed_out")
        self.assertEqual(get_systems().crm.cases, [])


class RateLimitTests(_Governed):
    def test_window_slides_and_clients_are_separate(self):
        now = [0.0]
        limiter = RateLimiter(RateLimitConfig(2, (("slow", 1),)), clock=lambda: now[0])
        self.assertEqual([limiter.allow("a"), limiter.allow("a"), limiter.allow("a")], [True, True, False])
        self.assertTrue(limiter.allow("b"))
        self.assertEqual([limiter.allow("slow"), limiter.allow("slow")], [True, False])
        now[0] = 59.9
        self.assertFalse(limiter.allow("a"))
        now[0] = 60.0
        self.assertTrue(limiter.allow("a"))

    def test_config_validation(self):
        cfg = RateLimitConfig.from_env({"RATE_LIMIT_PER_MINUTE": "30", "RATE_LIMIT_OVERRIDES": "p01-rate-test=5, x=9"})
        self.assertEqual((cfg.per_minute, cfg.overrides), (30, (("p01-rate-test", 5), ("x", 9))))
        self.assertEqual(RateLimitConfig.from_env({}).per_minute, 120)
        for bad in ({"RATE_LIMIT_PER_MINUTE": "0"}, {"RATE_LIMIT_PER_MINUTE": "many"},
                    {"RATE_LIMIT_OVERRIDES": "x"}, {"RATE_LIMIT_OVERRIDES": "x=0"}, {"RATE_LIMIT_OVERRIDES": "a b=3"}):
            with self.assertRaises(RateLimitConfigError, msg=bad):
                RateLimitConfig.from_env(bad)

    async def test_over_the_limit_is_denied_before_policy_or_backend_and_audited(self):
        act_as(self, REP)
        server.set_limiter(RateLimiter(RateLimitConfig(2)))
        for _ in range(2):
            self.assertFalse((await self.call("get_sales_order", {"sales_order": "SO-104233"})).is_error)
        result, rec = await self.logged("get_sales_order", {"sales_order": "SO-104233"})
        self.assertEqual(result.content[0].text,
                         "Access denied: cs-rep is over its call rate limit. Wait a minute and try again.")
        self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "rate_limited"))
        self.assertNotIn("backend", rec)
        self.assertEqual(rec["policy_ms"], 0.0)
        self.assertEqual(self.audit.rows[-1]["reason"], "rate_limited")
        act_as(self, LEAD)                               # another client has its own budget
        self.assertFalse((await self.call("get_sales_order", {"sales_order": "SO-104233"})).is_error)


class PiiTests(_Governed):
    def test_mask_rewrites_only_strings(self):
        value = {"email": "ap.bbo@example.com", "phone": "(555) 555-0142", "amount": 5550142.0, "n": 5550142,
                 "ids": ["SO-104233", "INV-90388", "2026-11-05T14:12"], "note": "call 555-0199 today"}
        masked, n = pii.mask_value(value)
        self.assertEqual(masked, {"email": "[EMAIL REDACTED]", "phone": "[PHONE REDACTED]", "amount": 5550142.0,
                                  "n": 5550142, "ids": ["SO-104233", "INV-90388", "2026-11-05T14:12"],
                                  "note": "call [PHONE REDACTED] today"})
        self.assertEqual(n, 3)
        text, n = pii.mask_json_text(json.dumps(value))
        self.assertEqual((json.loads(text), n), (masked, 3))
        self.assertEqual(pii.mask_json_text("not json: jo@example.com"), ("not json: [EMAIL REDACTED]", 1))
        for keep in ("PRO 123.4567", "weight 120 1500", "1Z 999 1234", "INV-2026 0042", "5550142", "2026-11-05"):
            self.assertEqual(pii.mask_text(keep), (keep, 0), keep)
        self.assertEqual(pii.mask_text("SSN 123-45-6789"), ("SSN [SSN REDACTED]", 1))

    async def test_caller_without_the_pii_scope_gets_masked_contacts(self):
        self.assertEqual(PII_SCOPE, "crm.pii")
        act_as(self, REP)
        result, rec = await self.logged("search_accounts", {"query": "Summit"})
        contact = result.structured_content["matches"][0]["primary_contact"]
        self.assertEqual(contact["email"], "[EMAIL REDACTED]")
        self.assertNotIn("@example.com", json.dumps(result.structured_content) + str(result.content))
        self.assertIn("[EMAIL REDACTED]", result.content[0].text)
        self.assertEqual(rec["pii_masked"], 1)
        self.assertEqual(self.audit.rows[-1]["pii_masked"], 1)
        self.assertEqual(result.structured_content["matches"][0]["account_id"], "ACCT-1002")

    async def test_lead_with_the_pii_scope_sees_contacts(self):
        act_as(self, LEAD)
        result, rec = await self.logged("search_accounts", {"query": "Summit"})
        self.assertEqual(result.structured_content["matches"][0]["primary_contact"]["email"],
                         "purchasing.summit@example.com")
        self.assertNotIn("pii_masked", rec)

    async def test_money_and_ids_survive_masking(self):
        act_as(self, REP)
        result = await self.call("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertEqual(result.structured_content["total_open"], 8370.0)
        self.assertEqual(result.structured_content["invoices"][0]["invoice"], "INV-90388")


class ApprovalTests(_Governed):
    async def test_approved_write_creates_the_case_and_is_fully_recorded(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        call = asyncio.create_task(self.logged("create_case", CASE))
        self.assertTrue(await self.decide_when_asked(approver))
        result, rec = await call
        self.assertFalse(result.is_error, result.content)
        case = result.structured_content
        self.assertEqual((case["case_id"], case["account_id"], case["status"]), ("CASE-10001", "ACCT-1002", "New"))
        self.assertEqual(case["approval"]["approved_by"], "Dana Lead")
        self.assertEqual(len(get_systems().crm.cases), 1)
        sent = self.n8n.requests[0]
        self.assertEqual((sent["client_id"], sent["tool"], sent["account_id"], sent["account_name"]),
                         ("cs-lead", "create_case", "ACCT-1002", "Summit Contractors LLC"))
        approval = self.audit.approvals[case["approval"]["request_id"]]
        self.assertEqual((approval["status"], approval["approver"], approval["tool"]), ("approved", "Dana Lead", "create_case"))
        self.assertEqual((rec["outcome"], rec["approval_id"]), ("ok", approval["request_id"]))
        row = self.audit.rows[-1]
        self.assertEqual((row["outcome"], row["approval_id"]), ("ok", approval["request_id"]))
        self.assertNotIn(sent["callback_token"], json.dumps(self.audit.rows) + json.dumps(self.audit.approvals) + json.dumps(rec))
        self.assertEqual(approver.pending(), [])

    async def test_the_callers_trace_id_is_recorded_with_the_request_and_listed_for_the_operator_only(self):
        approver = self.use_approver()
        details = {"trace_id": "trace-abc123", "account_id": "ACCT-1002", "subject": "Late delivery", "summary": "x"}
        ask = asyncio.create_task(approver.request({"client_id": "cs-lead"}, "create_case", details))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        waiting = approver.pending()[0]
        self.assertEqual(waiting["trace_id"], "trace-abc123")
        self.assertEqual(self.audit.approvals[waiting["request_id"]]["trace_id"], "trace-abc123")
        self.assertNotIn("trace_id", self.n8n.requests[-1])                 # the form does not need it
        self.assertTrue(await self.decide_when_asked(approver))
        self.assertEqual((await ask).status, "approved")

    def test_only_a_well_formed_trace_header_becomes_the_requests_trace_id(self):
        self.assertEqual(server.caller_trace_id({"x_request_id": "trace-1"}), "trace-1")
        tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        self.assertEqual(server.caller_trace_id({"traceparent": tp, "x_request_id": "trace-1"}), tp)
        self.assertIsNone(server.caller_trace_id({"x_request_id": "invalid"}))
        self.assertIsNone(server.caller_trace_id({"request_id": "7"}))

    def test_the_approval_insert_writes_the_trace_id(self):
        from ridgeline_gateway.audit import INSERT_APPROVAL
        self.assertIn("trace_id", INSERT_APPROVAL)
        self.assertIn("$6", INSERT_APPROVAL)

    async def test_n8n_gets_the_shared_secret_and_the_deadline_and_the_audit_gets_a_masked_subject(self):
        act_as(self, LEAD)
        approver = self.use_approver(timeout_s=45)
        call = asyncio.create_task(self.call("create_case", {**CASE, "subject": "Call back 555-555-0142 today"}))
        await self.decide_when_asked(approver)
        self.assertFalse((await call).is_error)
        self.assertEqual(self.n8n.headers, [WEBHOOK_SECRET])
        self.assertEqual(self.n8n.requests[0]["expires_in_s"], 45)
        stored = list(self.audit.approvals.values())[0]["subject"]
        self.assertEqual(stored, "Call back [PHONE REDACTED] today")

    async def test_an_unsigned_form_link_is_refused(self):
        act_as(self, LEAD)
        self.use_approver()
        self.n8n.unsigned = True
        result, rec = await self.logged("create_case", CASE)
        self.assertEqual(rec["policy_reason"], "approval_unavailable")
        self.assertEqual(get_systems().crm.cases, [])

    async def test_a_decision_on_an_already_closed_request_does_not_count(self):
        """Another gateway process (same database) closed the request as an orphan while the human was
        deciding. The approval is not recorded, so nothing is written."""
        act_as(self, LEAD)
        approver = self.use_approver()
        call = asyncio.create_task(self.call("create_case", CASE))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(await self.audit.close_orphaned_approvals()), 1)
        req = self.n8n.requests[0]
        self.assertTrue(approver.resolve(req["request_id"], req["callback_token"], "Approve", "Dana Lead"))
        self.assertIn("could not be recorded in the audit log", (await call).content[0].text)
        self.assertEqual(get_systems().crm.cases, [])
        self.assertEqual(self.audit.approvals[req["request_id"]]["status"], "unavailable")

    async def test_rejection_writes_nothing(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        call = asyncio.create_task(self.logged("create_case", CASE))
        await self.decide_when_asked(approver, "Reject")
        result, rec = await call
        self.assertEqual(result.content[0].text,
                         "Access denied: a human approver rejected this create_case request. Nothing was written.")
        self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "approval_denied"))
        self.assertEqual(get_systems().crm.cases, [])
        self.assertEqual([a["status"] for a in self.audit.approvals.values()], ["rejected"])

    async def test_anything_but_an_explicit_named_approval_is_a_rejection(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        for decision, name in (("approve", "Dana Lead"), ("Approve", ""), ("Approve", "<script>"), (True, "Dana Lead"),
                               ("Approve", None)):
            call = asyncio.create_task(self.call("create_case", CASE))
            await self.decide_when_asked(approver, decision, name)
            self.assertIn("rejected", (await call).content[0].text, (decision, name))
        self.assertEqual(get_systems().crm.cases, [])

    async def test_no_decision_in_time_is_denied_and_a_late_approval_does_nothing(self):
        act_as(self, LEAD)
        approver = self.use_approver(timeout_s=1.0)
        result, rec = await self.logged("create_case", CASE)
        self.assertIn("no human approved this create_case request in time", result.content[0].text)
        self.assertEqual(rec["policy_reason"], "approval_timeout")
        req = self.n8n.requests[0]
        self.assertFalse(approver.resolve(req["request_id"], req["callback_token"], "Approve", "Dana Lead"))
        self.assertEqual(get_systems().crm.cases, [])
        self.assertEqual([a["status"] for a in self.audit.approvals.values()], ["timed_out"])

    async def test_wrong_token_or_replay_cannot_approve(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        call = asyncio.create_task(self.call("create_case", CASE))
        self.assertFalse(await self.decide_when_asked(approver, token="guess"))
        req = self.n8n.requests[0]
        self.assertFalse(approver.resolve("apr-" + "0" * 16, req["callback_token"], "Approve", "Dana Lead"))
        self.assertFalse(approver.resolve(None, None, "Approve", "Dana Lead"))
        self.assertTrue(approver.resolve(req["request_id"], req["callback_token"], "Approve", "Dana Lead"))
        self.assertFalse(approver.resolve(req["request_id"], req["callback_token"], "Approve", "Dana Lead"))   # replay
        self.assertFalse((await call).is_error)
        self.assertEqual(len(get_systems().crm.cases), 1)

    async def test_approval_service_down_or_answering_nonsense_is_denied(self):
        act_as(self, LEAD)
        self.use_approver()
        replies = [{"received": False}, {"received": True, "request_id": "apr-other", "approval_url": f"{FORM_BASE}/form-waiting/1"},
                   {"received": True}, ["x"], {"received": True, "approval_url": "http://evil.example/form-waiting/1"}]
        for reply in replies:
            self.n8n.reply = reply
            result, rec = await self.logged("create_case", CASE)
            self.assertIn("approval service could not be reached", result.content[0].text, reply)
            self.assertEqual(rec["policy_reason"], "approval_unavailable")
        self.n8n.reply, self.n8n.status = None, 500
        self.assertTrue((await self.call("create_case", CASE)).is_error)
        self.n8n.status, self.n8n.down = 200, True
        self.assertTrue((await self.call("create_case", CASE)).is_error)
        self.assertEqual(get_systems().crm.cases, [])
        self.assertEqual({a["status"] for a in self.audit.approvals.values()}, {"unavailable"})

    async def test_form_link_must_be_on_the_configured_n8n_address(self):
        act_as(self, LEAD)
        self.use_approver()
        real = self.n8n.__call__

        def lookalike(req: httpx2.Request) -> httpx2.Response:      # right request id, look-alike host
            body = json.loads(real(req).content)
            body["approval_url"] = "http://localhost:5678.evil.example/form-waiting/1"
            return httpx2.Response(200, json=body)
        server.get_approver().http.transport = httpx2.MockTransport(lookalike)
        result = await self.call("create_case", CASE)
        self.assertIn("approval service could not be reached", result.content[0].text)
        self.assertEqual(len(self.n8n.requests), 1)

    async def test_without_an_approver_or_an_audit_record_no_write_happens(self):
        act_as(self, LEAD)
        self.assertIsInstance(server.get_approver(), NoApprover)
        self.assertIn("approval service could not be reached", (await self.call("create_case", CASE)).content[0].text)
        self.use_approver()
        self.audit.fail = True
        result = await self.call("create_case", CASE)
        self.assertIn("could not be recorded in the audit log", result.content[0].text)
        self.assertEqual(self.n8n.requests, [], "no audit record, so no human is even asked")
        self.assertEqual(get_systems().crm.cases, [])

    async def test_an_approval_that_cannot_be_recorded_does_not_count(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        call = asyncio.create_task(self.call("create_case", CASE))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        self.audit.fail = True                         # the store goes away while the human decides
        req = self.n8n.requests[0]
        approver.resolve(req["request_id"], req["callback_token"], "Approve", "Dana Lead")
        self.assertIn("could not be recorded in the audit log", (await call).content[0].text)
        self.assertEqual(get_systems().crm.cases, [])

    async def test_policy_runs_before_a_human_is_asked(self):
        self.use_approver()
        act_as(self, REP)                                                        # no crm.write
        self.assertIn("lacks scope crm.write", (await self.call("create_case", CASE)).content[0].text)
        act_as(self, LEAD)
        nested = await self.call("create_case", {**CASE, "fields": {"credit_limit": 50000}})
        self.assertIn("human-only", nested.content[0].text)
        self.assertEqual(self.audit.rows[-1]["reason"], "restricted_change")
        act_as(self, {**LEAD, "regions": ["Central"]})                           # a regional lead
        west = await self.call("create_case", {**CASE, "account_id": "ACCT-1003"})
        self.assertIn("outside your region", west.content[0].text)
        act_as(self, LEAD)
        self.assertIn("ACCT-9999 was not found in CRM", (await self.call("create_case", {**CASE, "account_id": "ACCT-9999"})).content[0].text)
        for bad in ({**CASE, "subject": "<b>hi</b>"}, {**CASE, "summary": "{{ $env.SECRET }}"}, {**CASE, "subject": "x"}):
            self.assertTrue((await self.call("create_case", bad)).is_error)
        self.assertEqual(self.n8n.requests, [])
        self.assertEqual(get_systems().crm.cases, [])

    async def test_a_restart_closes_orphaned_approvals_and_a_late_decision_changes_nothing(self):
        """The 2026-11-11 failure: the gateway was restarted while a human was deciding. The request
        stayed `pending` forever and the late form submission had nowhere to go."""
        act_as(self, LEAD)
        approver = self.use_approver(timeout_s=30)
        call = asyncio.create_task(self.call("create_case", CASE))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        request = self.n8n.requests[0]
        call.cancel()                                        # the process holding the request goes away
        with self.assertRaises(BaseException):
            await call
        approvals = self.audit.approvals
        orphan = request["request_id"]
        approvals[orphan]["status"] = "pending"              # a killed process records nothing at all
        # The next gateway process starts with no memory of the request.
        server.set_approver(None)
        restarted = self.use_approver()
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            closed = await server.close_orphaned_approvals(self.audit)
        self.assertEqual(closed, [orphan])
        self.assertEqual(approvals[orphan]["status"], "unavailable")
        self.assertIn('"event": "orphaned_approvals_closed"', cm.records[0].getMessage())
        self.assertFalse(restarted.resolve(orphan, request["callback_token"], "Approve", "Dana Lead"))
        self.assertEqual(get_systems().crm.cases, [])
        self.assertEqual(await server.close_orphaned_approvals(self.audit), [])     # idempotent
        self.audit.fail = True                               # store down at startup: logged, not fatal
        self.assertEqual(await server.close_orphaned_approvals(self.audit), [])

    async def test_database_environment_is_read_from_the_stamp_and_unknown_on_failure(self):
        self.audit.stamp = "test"
        self.assertEqual(await server.read_environment(self.audit), "test")
        self.audit.stamp = None                              # never stamped
        self.assertEqual(await server.read_environment(self.audit), "unknown")
        self.audit.fail = True                               # store down at startup: logged, not fatal
        with self.assertLogs("ridgeline.gateway", logging.WARNING):
            self.assertEqual(await server.read_environment(self.audit), "unknown")

    def test_environment_stamp_sql_and_values(self):
        from ridgeline_gateway.audit import ENVIRONMENTS, SELECT_ENVIRONMENT
        self.assertEqual(ENVIRONMENTS, {"main", "test", "local"})
        self.assertIn("ridgeline_environment", SELECT_ENVIRONMENT)

    def test_orphan_cleanup_sql_touches_only_pending_requests(self):
        from ridgeline_gateway.audit import CLOSE_ORPHANED
        self.assertIn("WHERE status = 'pending'", CLOSE_ORPHANED)
        self.assertIn("status = 'unavailable'", CLOSE_ORPHANED)
        self.assertNotIn("approver", CLOSE_ORPHANED)        # nobody approved it; the column stays empty

    async def test_pending_list_carries_the_link_and_nothing_else_does(self):
        act_as(self, LEAD)
        approver = self.use_approver()
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            call = asyncio.create_task(self.call("create_case", CASE))
            for _ in range(200):
                if approver.pending():
                    break
                await asyncio.sleep(0.01)
            pending = approver.pending()
            await self.decide_when_asked(approver)
            result = await call
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["approval_url"], f"{FORM_BASE}/form-waiting/1?signature=abc")
        self.assertEqual((pending[0]["client_id"], pending[0]["account_id"]), ("cs-lead", "ACCT-1002"))
        everything = (str(result.content) + json.dumps(result.structured_content) + json.dumps(self.audit.rows)
                      + json.dumps(self.audit.approvals) + " ".join(r.getMessage() for r in cm.records))
        self.assertNotIn("form-waiting", everything)
        self.assertNotIn("signature", everything)

    def test_config_validation(self):
        good = {"N8N_APPROVAL_URL": "http://n8n:5678/webhook/ridgeline-approval", "N8N_FORM_BASE_URL": "http://localhost:5678/",
                "APPROVAL_OPERATOR_TOKEN": OPERATOR_TOKEN, "APPROVAL_WEBHOOK_SECRET": WEBHOOK_SECRET}
        cfg = ApprovalConfig.from_env(good)
        self.assertEqual((cfg.timeout_s, cfg.form_base_url), (120.0, "http://localhost:5678"))
        self.assertNotIn(OPERATOR_TOKEN, repr(cfg))
        self.assertNotIn(WEBHOOK_SECRET, repr(cfg))
        self.assertEqual(ApprovalConfig.from_env({**good, "APPROVAL_TIMEOUT_S": "30"}).timeout_s, 30.0)
        for bad in ({}, {**good, "N8N_APPROVAL_URL": "n8n:5678"}, {**good, "N8N_APPROVAL_URL": "http://u:p@n8n/x"},
                    {**good, "N8N_FORM_BASE_URL": ""}, {**good, "APPROVAL_OPERATOR_TOKEN": "short"},
                    {**good, "APPROVAL_TIMEOUT_S": "0"}, {**good, "APPROVAL_TIMEOUT_S": "soon"},
                    {**good, "APPROVAL_TIMEOUT_S": "100000"}, {**good, "APPROVAL_WEBHOOK_SECRET": ""},
                    {**good, "APPROVAL_WEBHOOK_SECRET": "short"}, {**good, "APPROVAL_WEBHOOK_SECRET": "x" * 40 + "\r\nX-Evil: 1"}):
            with self.assertRaises(ApprovalConfigError, msg=bad):
                ApprovalConfig.from_env(bad)


class ReasonCodeTests(unittest.TestCase):
    def test_one_shared_list_and_every_code_has_its_own_text(self):
        data = load_policy_data()
        self.assertEqual(POLICY_REASONS, frozenset(data["reasons"]))
        self.assertEqual(GATEWAY_REASONS, frozenset(data["gateway_reasons"]))
        self.assertFalse(POLICY_REASONS & GATEWAY_REASONS)
        self.assertEqual(AUDIT_SCOPE, "audit.read")
        generic = denial_message(Decision(False, "no-such-code"), LEAD, "create_case")
        own_text = (POLICY_REASONS | GATEWAY_REASONS) - {"allowed", "invalid_request", "policy_unavailable",
                                                         "policy_error", "resource_not_checked"}
        for reason in own_text:
            with self.subTest(reason=reason):
                self.assertNotEqual(denial_message(Decision(False, reason, "crm.write"), LEAD, "create_case"), generic)
        for reason in ("policy_unavailable", "policy_error", "resource_not_checked", "invalid_request"):
            self.assertEqual(denial_message(Decision(False, reason), LEAD, "create_case"), generic)

    def test_the_gateway_only_raises_codes_from_the_list(self):
        import pathlib, re
        source = "".join(p.read_text() for p in pathlib.Path(server.__file__).parent.glob("*.py"))
        used = set(re.findall(r'"((?:approval|audit|rate|policy|resource)_[a-z_]+)"', source))
        used &= {u for u in used if u.endswith(("_denied", "_timeout", "_unavailable", "_limited", "_error", "_not_checked"))}
        self.assertTrue(used)
        self.assertLessEqual(used, set(GATEWAY_REASONS), used - set(GATEWAY_REASONS))


class RouteTests(unittest.TestCase):
    """The three HTTP routes next to /mcp, on the real app with auth enabled as main() does."""

    def setUp(self) -> None:
        reset_systems()
        set_connectors(build_connectors(load_settings({})))
        use_policy(self, principal=None)
        self.idp = FakeKeycloak()
        mcp.enable_auth(CONFIG, build_verifier(CONFIG, transport=httpx2.MockTransport(self.idp)))
        self.addCleanup(reset_auth)
        self.n8n = FakeN8n()
        config = ApprovalConfig("http://n8n:5678/webhook/ridgeline-approval", OPERATOR_TOKEN, 5.0, FORM_BASE, WEBHOOK_SECRET)
        self.approver = build_approver(config, self.audit, transport=httpx2.MockTransport(self.n8n))
        server.set_approver(self.approver)
        app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=transport_security())
        self.http = TestClient(app, base_url="http://localhost:8765")
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def lead(self, scope: str = "crm.read erp.read shipping.read ar.read crm.write crm.pii") -> str:
        return self.idp.mint(azp="cs-lead", sub="service-account-cs-lead", regions=["*"], scope=scope)

    def auditor(self) -> str:
        return self.idp.mint(azp="p01-auditor", sub="service-account-p01-auditor", scope="audit.read", drop=("regions",))

    def test_audit_report_needs_a_valid_token_with_the_audit_scope(self):
        self.assertEqual(self.http.get("/audit").status_code, 401)
        self.assertEqual(self.http.get("/audit", headers={"Authorization": "Bearer nonsense"}).status_code, 401)
        self.assertEqual(self.http.get("/audit", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}).status_code, 401)
        rep = self.http.get("/audit", headers={"Authorization": f"Bearer {self.idp.mint()}"})
        self.assertEqual((rep.status_code, rep.json()), (403, {"error": "scope audit.read is required"}))
        lead = self.http.get("/audit", headers={"Authorization": f"Bearer {self.lead()}"})
        self.assertEqual(lead.status_code, 403, "no agent identity may read the audit log")
        expired = self.idp.mint(azp="cs-lead", scope="audit.read", exp=1)
        self.assertEqual(self.http.get("/audit", headers={"Authorization": f"Bearer {expired}"}).status_code, 401)
        asyncio.run(self.audit.write({"client_id": "cs-rep", "tool": "get_sales_order", "outcome": "denied",
                                      "policy_reason": "x", "reason": "out_of_region", "args": {"sales_order": "SO-104301"}}))
        asyncio.run(self.audit.write({"client_id": "cs-rep", "tool": "get_sales_order", "outcome": "ok"}))
        ok = self.http.get("/audit?limit=1", headers={"Authorization": f"Bearer {self.auditor()}"})
        self.assertEqual(ok.status_code, 200)
        report = ok.json()
        self.assertEqual((report["total"], report["by_outcome"], report["by_reason"]),
                         (2, {"denied": 1, "ok": 1}, {"out_of_region": 1}))
        self.assertEqual(len(report["recent"]), 1)
        self.audit.fail = True
        self.assertEqual(self.http.get("/audit", headers={"Authorization": f"Bearer {self.auditor()}"}).status_code, 503)

    def test_pending_approvals_need_the_operator_token_not_an_agent_token(self):
        self.assertEqual(self.http.get("/approvals/pending").status_code, 401)
        agent = self.http.get("/approvals/pending", headers={"Authorization": f"Bearer {self.lead()}"})
        self.assertEqual(agent.status_code, 401, "an agent token must never reach the approval links")
        ok = self.http.get("/approvals/pending", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"})
        self.assertEqual((ok.status_code, ok.json()), (200, {"pending": []}))

    def test_callback_rejects_garbage_unknown_requests_and_oversized_bodies(self):
        for body, status in ((b"not json", 400), (b"[]", 400), (b"{" + b" " * 5000 + b"}", 400),
                             (json.dumps({"request_id": "apr-" + "0" * 16, "token": "x", "decision": "Approve",
                                          "approver": "Dana Lead"}).encode(), 404),
                             (json.dumps({"decision": "Approve"}).encode(), 404)):
            resp = self.http.post("/approvals/callback", content=body, headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status_code, status, body[:40])
            self.assertFalse(resp.json()["ok"])


if __name__ == "__main__":
    unittest.main()
