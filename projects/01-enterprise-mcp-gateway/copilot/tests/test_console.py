"""The local web console (Milestone 8), in-process: Starlette's test client, a fake copilot that emits real
tracer spans, and a fake gateway for the operator reads (audit rows, the waiting approval)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from starlette.testclient import TestClient

from agentcore import InMemorySink, MemoryItem, Outcome, Tracer
from cs_copilot.ask import DEMO_QUESTION
from cs_copilot.console import server as S
from cs_copilot.console.server import Console, Gateway, create_app

HOSTS = ["localhost", "127.0.0.1", "console"]
ORIGINS = ["http://localhost:8790", "http://127.0.0.1:8790"]
WRITE = {"X-Ridgeline-Console": "1"}
TOKEN = "t" * 40
CASE_Q = "Open a case for Summit Contractors about the late delivery of SO-104233 and confirm the new delivery window."


class FakeAgent:
    """Emits the spans a real run emits and returns a cited answer; optionally waits on a 'human'."""

    def __init__(self, gate: threading.Event | None = None, write: bool = False, refused: bool = False,
                 crash: bool = False):
        self.refused, self.crash = refused, crash
        self.tracer = Tracer(InMemorySink())
        self.memory = SimpleNamespace(last_recall=[MemoryItem("Q: an earlier question\nA: ...", "episode",
                                                               {"trace_id": "earliertrace01"}, score=0.91)])
        self.gate, self.write = gate, write

    def run(self, text, principal):
        if self.crash:
            raise ConnectionError("could not connect to ep-secret-host.db.example:5432")
        trace = "trace" + "0" * 27
        with self.tracer.span(trace, "perceive.parse", kind="cs_request"):
            pass
        for tool in ("search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"):
            with self.tracer.span(trace, f"act.{tool}", category="mcp") as s:
                s.attrs.update(ok=True, error=None)
        history = []
        if self.write:
            with self.tracer.span(trace, "act.create_case", category="mcp") as s:
                if self.gate is not None:
                    self.gate.wait(10)
                s.attrs.update(ok=not self.refused, error="Access denied: no human approved this in time." if self.refused else None)
            history.append({"role": "user", "content": 'Result of create_case: {"error": "GatewayToolError: Access denied: '
                                                       'no human approved this in time.", "denied": false}'}
                           if self.refused else
                           {"role": "user", "content": 'Result of create_case: {"case_id": "CASE-10001", '
                                                       '"approval": {"approved_by": "Dana Lead"}}'})
        with self.tracer.span(trace, "act.get_open_invoices", category="mcp") as s:
            s.attrs.update(ok=False, error="GatewayToolError: Error executing tool x: Access denied: outside your region.")
        return Outcome("answered", "Shipped late [customer-service-policy.md#0]; notify Logistics [escalation-matrix.md#0].",
                       2, 5, 0.0, trace, history=history)


class FakeGateway:
    def __init__(self):
        self.waiting = None
        self.audit_calls = []

    def approval_for(self, trace_id):
        return self.waiting

    def audit_rows(self, trace_id):
        self.audit_calls.append(trace_id)
        return [{"at": "2026-11-12T10:00:00", "client_id": "cs-rep", "tool": "search_accounts", "outcome": "ok",
                 "reason": None, "args": {"query": "Summit"}}]


def make(agent_factory=lambda: FakeAgent(), local=True):
    gateway = FakeGateway()
    console = Console(gateway_url="http://gateway:8000/mcp", gateway=gateway, tokens=lambda role: (lambda: "tok"),
                      llm_for=lambda model, q: object(), builder=lambda llm, role, source: agent_factory(),
                      local_available=lambda: local)
    client = TestClient(create_app(console, HOSTS, ORIGINS, TOKEN), base_url="http://localhost:8790")
    client.get(f"/?token={TOKEN}")                   # sign in: sets the console cookie
    return console, gateway, client


def wait(client, run_id, until=lambda r: r["status"] != "running"):
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}").json()
        if until(run):
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached the state")


class ConsoleApiTests(unittest.TestCase):
    def test_config_lists_roles_models_and_examples(self):
        _, _, client = make(local=False)
        cfg = client.get("/api/config").json()
        self.assertEqual([r["id"] for r in cfg["roles"]], ["cs-rep", "cs-lead"])
        models = {m["id"]: m["available"] for m in cfg["models"]}
        self.assertEqual(models, {"scripted": True, "local": False, "hosted": False})
        self.assertIn(DEMO_QUESTION, cfg["examples"])

    def test_a_run_shows_steps_tools_recall_citations_and_its_trace(self):
        _, gateway, client = make()
        resp = client.post("/api/runs", json={"role": "cs-rep", "model": "scripted", "question": DEMO_QUESTION}, headers=WRITE)
        self.assertEqual(resp.status_code, 202, resp.text)
        run = wait(client, resp.json()["id"])
        self.assertEqual(run["status"], "answered")
        self.assertEqual(run["citations"], ["customer-service-policy.md#0", "escalation-matrix.md#0"])
        self.assertEqual([t["tool"] for t in run["tools"]][:4],
                         ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"])
        denied = run["tools"][-1]
        self.assertEqual((denied["ok"], denied["error"]), (False, "Access denied: outside your region."))
        self.assertTrue(any(s["name"] == "perceive.parse" for s in run["steps"]))
        self.assertEqual(run["recalled"][0]["score"], 0.91)
        self.assertTrue(run["trace_id"].startswith("trace"))
        rows = client.get(f"/api/runs/{run['id']}/audit").json()["rows"]
        self.assertEqual(rows[0]["tool"], "search_accounts")
        self.assertEqual(gateway.audit_calls, [run["trace_id"]])

    def test_a_case_request_shows_the_waiting_approval_then_the_created_case(self):
        gate = threading.Event()
        _, gateway, client = make(agent_factory=lambda: FakeAgent(gate=gate, write=True))
        gateway.waiting = {"state": "waiting", "request_id": "apr-1", "url": "http://localhost:5678/form-waiting/7?signature=x",
                           "expires_in_s": 110}
        run_id = client.post("/api/runs", json={"role": "cs-lead", "model": "scripted", "question": CASE_Q},
                             headers=WRITE).json()["id"]
        waiting = wait(client, run_id, until=lambda r: (r.get("approval") or {}).get("state") == "waiting")
        self.assertEqual(waiting["approval"]["url"], "http://localhost:5678/form-waiting/7?signature=x")
        gateway.waiting = None
        gate.set()
        done = wait(client, run_id)
        self.assertEqual(done["approval"], {"state": "approved", "case_id": "CASE-10001", "approved_by": "Dana Lead"})

    def test_bad_requests_are_refused_with_a_reason(self):
        console, _, client = make()
        post = lambda body, headers=WRITE: client.post("/api/runs", json=body, headers=headers)
        self.assertEqual(post({"role": "admin", "model": "scripted", "question": DEMO_QUESTION}).status_code, 400)
        r = post({"role": "cs-rep", "model": "scripted", "question": "What is the meaning of life?"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("example questions", r.json()["error"])
        self.assertEqual(post({"role": "cs-rep", "model": "hosted", "question": DEMO_QUESTION}).status_code, 400)
        self.assertEqual(post({"role": "cs-rep", "model": "local", "question": "x" * 5000}).status_code, 400)
        self.assertEqual(client.get("/api/runs/nope").status_code, 404)
        self.assertEqual(client.get("/api/citations/customer-service-policy.md#0".replace("#", "%23")).status_code, 200)
        self.assertEqual(client.get("/api/citations/nope.md%230").status_code, 404)

    def test_a_refused_case_request_shows_no_case_was_created_with_the_reason(self):
        _, _, client = make(agent_factory=lambda: FakeAgent(write=True, refused=True))
        run_id = client.post("/api/runs", json={"role": "cs-lead", "model": "scripted", "question": CASE_Q},
                             headers=WRITE).json()["id"]
        done = wait(client, run_id)
        self.assertEqual(done["approval"], {"state": "not_created", "reason": "Access denied: no human approved this in time."})

    def test_a_crashed_run_shows_a_generic_error_without_hosts(self):
        _, _, client = make(agent_factory=lambda: FakeAgent(crash=True))
        run_id = client.post("/api/runs", json={"role": "cs-rep", "model": "scripted", "question": DEMO_QUESTION},
                             headers=WRITE).json()["id"]
        done = wait(client, run_id)
        self.assertEqual(done["status"], "error")
        self.assertIn("ConnectionError", done["error"])
        self.assertNotIn("ep-secret-host", json.dumps(done))

    def test_at_most_two_runs_at_a_time(self):
        gate = threading.Event()
        _, _, client = make(agent_factory=lambda: FakeAgent(gate=gate, write=True))
        body = {"role": "cs-lead", "model": "scripted", "question": CASE_Q}
        codes = [client.post("/api/runs", json=body, headers=WRITE).status_code for _ in range(3)]
        gate.set()
        self.assertEqual(codes, [202, 202, 429])


class ConsoleTokenTests(unittest.TestCase):
    def test_without_the_token_nothing_but_the_locked_page_and_health(self):
        console = Console(gateway_url="g", gateway=FakeGateway(), tokens=lambda r: (lambda: "tok"),
                          llm_for=lambda m, q: object(), builder=lambda llm, role, source: FakeAgent(),
                          local_available=lambda: True)
        client = TestClient(create_app(console, HOSTS, ORIGINS, TOKEN), base_url="http://localhost:8790")
        self.assertEqual(client.get("/").status_code, 401)
        self.assertEqual(client.get("/api/config").status_code, 401)
        self.assertEqual(client.post("/api/runs", json={}, headers=WRITE).status_code, 401)
        self.assertEqual(client.get("/api/runs/x/audit").status_code, 401)
        self.assertEqual(client.get("/?token=wrong").status_code, 401)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/config", headers={"X-Console-Token": TOKEN}).status_code, 200)
        signed_in = client.get(f"/?token={TOKEN}", follow_redirects=False)
        self.assertEqual(signed_in.status_code, 303)
        cookie = signed_in.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(client.get("/api/config").status_code, 200)

    def test_a_short_token_is_refused_at_startup(self):
        with self.assertRaises(ValueError):
            create_app(None, HOSTS, ORIGINS, "short")


class GatewayClientTests(unittest.TestCase):
    def fake_open(self, payloads):
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("Authorization")))
            import io
            return io.BytesIO(json.dumps(payloads[req.full_url.split("8000")[1].split("?")[0]]).encode())
        return opener, seen

    def test_audit_rows_are_filtered_to_the_runs_trace_with_the_auditor_token(self):
        from unittest import mock
        opener, seen = self.fake_open({"/audit": {"recent": [
            {"trace_id": "t1", "tool": "a", "outcome": "ok", "args": {}, "at": "x", "client_id": "cs-rep"},
            {"trace_id": "t2", "tool": "b", "outcome": "ok"}]}})
        gw = Gateway("http://gateway:8000", lambda: "auditor-tok", "op-tok")
        with mock.patch("urllib.request.urlopen", opener):
            rows = gw.audit_rows("t1")
        self.assertEqual([r["tool"] for r in rows], ["a"])
        self.assertEqual(seen[0], ("http://gateway:8000/audit?limit=200", "Bearer auditor-tok"))

    def test_the_waiting_approval_is_matched_by_trace_and_only_http_links_pass(self):
        from unittest import mock
        pending = {"/approvals/pending": {"pending": [
            {"trace_id": "other", "approval_url": "http://localhost:5678/form-waiting/1"},
            {"trace_id": "t1", "request_id": "apr-1", "approval_url": "javascript:alert(1)", "expires_in_s": 90}]}}
        opener, seen = self.fake_open(pending)
        gw = Gateway("http://gateway:8000", None, "op-tok")
        with mock.patch("urllib.request.urlopen", opener):
            waiting = gw.approval_for("t1")
        self.assertEqual((waiting["request_id"], waiting["url"]), ("apr-1", None))
        self.assertEqual(seen[0][1], "Bearer op-tok")
        self.assertIsNone(Gateway("http://gateway:8000", None, "").approval_for("t1"))     # no operator token
        with self.assertRaises(RuntimeError):
            Gateway("http://gateway:8000", None, "op").audit_rows("t1")                    # no auditor identity


class ConsoleSecurityTests(unittest.TestCase):
    def test_writes_need_the_console_header_and_an_allowed_origin(self):
        _, _, client = make()
        body = {"role": "cs-rep", "model": "scripted", "question": DEMO_QUESTION}
        self.assertEqual(client.post("/api/runs", json=body).status_code, 403)                     # no header
        evil = client.post("/api/runs", json=body, headers={**WRITE, "Origin": "http://evil.example"})
        self.assertEqual(evil.status_code, 403)
        ok = client.post("/api/runs", json=body, headers={**WRITE, "Origin": "http://localhost:8790"})
        self.assertEqual(ok.status_code, 202)

    def test_a_foreign_host_header_is_refused(self):
        _, _, client = make()
        self.assertEqual(client.get("/api/config", headers={"Host": "attacker.example"}).status_code, 400)

    def test_pages_carry_a_strict_content_security_policy_and_no_inline_script(self):
        _, _, client = make()
        page = client.get("/")
        csp = page.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("<script>", page.text)                      # only <script src=...>
        self.assertNotIn("style=", page.text)
        static = client.get("/static/console.js")
        self.assertEqual(static.status_code, 200)
        self.assertEqual(static.headers["x-content-type-options"], "nosniff")      # headers on static files too
        self.assertIn("default-src 'self'", static.headers["content-security-policy"])
        js = client.get("/static/console.js").text
        self.assertNotIn(".innerHTML", js)                           # server text is never parsed as markup
        self.assertNotIn("insertAdjacentHTML", js)

    def test_run_views_never_carry_credentials(self):
        _, _, client = make()
        run_id = client.post("/api/runs", json={"role": "cs-rep", "model": "scripted", "question": DEMO_QUESTION},
                             headers=WRITE).json()["id"]
        view = json.dumps(wait(client, run_id))
        for secret in ("tok", "Bearer", "operator"):
            self.assertNotIn(secret, view)


if __name__ == "__main__":
    unittest.main()
